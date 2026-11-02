"""Claim state, API token, recovery password, owner password and the arrival-interface check.

State lives in paths.AGENT_STATE_DIR (0700, root):
  claimed           ISO timestamp; present = claimed
  token.sha256      hex sha256 of the app's API token
  recovery.hash     JSON: salted scrypt (n=2**14, r=8, p=1) of the recovery password
  recover.lockout   JSON: {"failures": n, "locked_until": epoch|null}
  app_key.pub       the SSH public key installed by the app (so /recover can swap it)
  owner.json        {"owner_name": ..., "claimed_at": ...}
"""

import base64
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import shutil
import threading
import time
from collections.abc import Callable
from pathlib import Path

from weatherpi import paths
from weatherpi.agent.errors import ApiError, iso, utc_now
from weatherpi.agent.oscmd import Runner

OWNER_USER = "main"
MIN_PASSWORD = 8
MAX_PASSWORD = 128
MIN_RECOVERY = 20
SCRYPT = {"n": 2**14, "r": 8, "p": 1}
MAX_RECOVER_FAILURES = 5
LOCKOUT_SECONDS = 600

USB_ADDRESS = "10.12.194.1"
TAILSCALE_NETS = (
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("fd7a:115c:a1e0::/48"),
)

SSH_KEY_TYPES = (
    "ssh-ed25519",
    "ssh-rsa",
    "ecdsa-sha2-nistp256",
    "ecdsa-sha2-nistp384",
    "ecdsa-sha2-nistp521",
    "sk-ssh-ed25519@openssh.com",
    "sk-ecdsa-sha2-nistp256@openssh.com",
)
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_\-]{32,256}={0,2}$")
_TZ_RE = re.compile(r"^[A-Za-z0-9_+\-]+(/[A-Za-z0-9_+\-]+){0,2}$")


def sshd_password_conf() -> Path:
    return paths.ROOT / "etc/ssh/sshd_config.d/10-weatherpi-password.conf"


def authorized_keys_file() -> Path:
    return paths.MAIN_HOME / ".ssh/authorized_keys"


# ---------- small file helpers ----------

def _ensure_dir(path: Path, mode: int) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def write_private(path: Path, text: str, mode: int = 0o600) -> None:
    """Atomic write with restrictive permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def _chown_main(path: Path) -> None:
    if not paths.FAKE:
        shutil.chown(path, OWNER_USER, OWNER_USER)


# ---------- validation ----------

def check_password(password: str) -> None:
    """Owner password policy: >= 8 chars and safe for `chpasswd` (one line)."""
    if not isinstance(password, str) or len(password) < MIN_PASSWORD:
        raise ApiError(400, "weak_password", f"Use at least {MIN_PASSWORD} characters.")
    if len(password) > MAX_PASSWORD:
        raise ApiError(400, "weak_password", f"Use at most {MAX_PASSWORD} characters.")
    if any(c in password for c in "\r\n\x00"):
        raise ApiError(400, "weak_password", "The password can't contain line breaks.")


def check_recovery(recovery: str) -> None:
    if len(recovery) < MIN_RECOVERY or len(recovery) > 512 or any(c in recovery for c in "\r\n"):
        raise ApiError(400, "bad_request", f"recovery_password must be >= {MIN_RECOVERY} chars")


def check_token(token: str) -> None:
    if not _TOKEN_RE.match(token or ""):
        raise ApiError(400, "bad_request", "api_token must be base64url (32 random bytes)")


def check_ssh_pubkey(key: str) -> str:
    key = (key or "").strip()
    parts = key.split()
    if "\n" in key or "\r" in key or len(parts) < 2 or parts[0] not in SSH_KEY_TYPES:
        raise ApiError(400, "bad_request", "ssh_pubkey must be one OpenSSH public key line")
    try:
        blob = base64.b64decode(parts[1], validate=True)
    except ValueError:
        raise ApiError(400, "bad_request", "ssh_pubkey is not valid base64") from None
    if len(blob) < 4 or len(key) > 16384:
        raise ApiError(400, "bad_request", "ssh_pubkey is malformed")
    return key


def check_timezone(tz: str) -> str:
    if not _TZ_RE.match(tz or "") or ".." in tz:
        raise ApiError(400, "bad_request", "timezone must be an IANA name like Africa/Johannesburg")
    if not paths.FAKE and not (paths.ROOT / "usr/share/zoneinfo" / tz).is_file():
        raise ApiError(400, "bad_request", f"unknown timezone {tz}")
    return tz


# ---------- hashing ----------

def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def scrypt_hash(secret: str) -> dict:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(secret.encode("utf-8"), salt=salt, dklen=32, **SCRYPT)
    return {
        "alg": "scrypt",
        **SCRYPT,
        "salt": base64.b64encode(salt).decode(),
        "hash": base64.b64encode(digest).decode(),
    }


def scrypt_verify(secret: str, record: dict) -> bool:
    try:
        salt = base64.b64decode(record["salt"])
        expected = base64.b64decode(record["hash"])
        got = hashlib.scrypt(
            secret.encode("utf-8"),
            salt=salt,
            n=int(record["n"]),
            r=int(record["r"]),
            p=int(record["p"]),
            dklen=len(expected),
        )
    except (KeyError, ValueError, TypeError):
        return False
    return hmac.compare_digest(got, expected)


# ---------- interface classification ----------

def classify_local_address(local_ip: str | None, usb_addresses: set[str] = frozenset()) -> str | None:
    """'usb' | 'tailscale' | None (refuse) from the address the request arrived on."""
    if not local_ip:
        return None
    try:
        addr = ipaddress.ip_address(local_ip.split("%")[0])
    except ValueError:
        return None
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    if str(addr) == USB_ADDRESS or str(addr) in usb_addresses:
        return "usb"
    if any(addr in net for net in TAILSCALE_NETS):
        return "tailscale"
    return None


class UsbAddresses:
    """usb0's current IPv4 addresses (it becomes a DHCP client when the host shares internet
    over the cable, so it isn't always 10.12.194.1). Cached for a few seconds."""

    def __init__(self, runner: Runner, ttl: float = 10.0):
        self._runner = runner
        self._ttl = ttl
        self._at = 0.0
        self._value: set[str] = set()

    def get(self) -> set[str]:
        if time.monotonic() - self._at > self._ttl:
            self._value = set()
            res = self._runner.run(["ip", "-j", "-4", "addr", "show", "dev", "usb0"], timeout=5)
            if res.ok:
                try:
                    for link in json.loads(res.stdout or "[]"):
                        for info in link.get("addr_info", []):
                            if info.get("local"):
                                self._value.add(info["local"])
                except (ValueError, AttributeError):
                    pass
            self._at = time.monotonic()
        return self._value


# ---------- claim state ----------

class ClaimStore:
    def __init__(self, runner: Runner, commit_etc: Callable[..., bool] = lambda *f: True):
        self.runner = runner
        # read-only root (D14): /etc changes must be copied to the lower root to persist
        self.commit_etc = commit_etc
        self.dir = paths.AGENT_STATE_DIR
        self._lock = threading.Lock()

    def _p(self, name: str) -> Path:
        return self.dir / name

    def _ensure_state_dir(self) -> None:
        _ensure_dir(self.dir, 0o700)

    @property
    def claimed(self) -> bool:
        return self._p("claimed").exists()

    def claimed_at(self) -> str | None:
        try:
            return self._p("claimed").read_text().strip() or None
        except OSError:
            return None

    def verify_token(self, token: str | None) -> bool:
        if not token:
            return False
        try:
            stored = self._p("token.sha256").read_text().strip()
        except OSError:
            return False
        return bool(stored) and hmac.compare_digest(token_digest(token), stored)

    def secret_values(self) -> list[str]:
        """Strings from the state dir that must never appear in diagnostics."""
        out: list[str] = []
        try:
            out.append(self._p("token.sha256").read_text().strip())
        except OSError:
            pass
        try:
            rec = json.loads(self._p("recovery.hash").read_text())
            out += [rec.get("salt", ""), rec.get("hash", "")]
        except (OSError, ValueError):
            pass
        return [s for s in out if len(s) >= 8]

    # --- OS effects ---

    def set_password(self, password: str) -> None:
        check_password(password)
        res = self.runner.run(["chpasswd"], timeout=30, input=f"{OWNER_USER}:{password}\n")
        if not res.ok:
            raise ApiError(500, "internal", "chpasswd failed")
        self.commit_etc("/etc/shadow")

    def install_ssh_key(self, key: str, replace: str | None = None) -> None:
        """Add the app key to ~main/.ssh/authorized_keys (dropping `replace`, the old app key)."""
        ssh_dir = authorized_keys_file().parent
        _ensure_dir(ssh_dir, 0o700)
        _chown_main(ssh_dir)
        f = authorized_keys_file()
        try:
            lines = f.read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []

        def blob(line: str) -> str:
            parts = line.split()
            return parts[1] if len(parts) > 1 else line

        drop = {blob(key)} | ({blob(replace)} if replace else set())
        kept = [ln for ln in lines if ln.strip() and blob(ln) not in drop]
        write_private(f, "\n".join([*kept, key]) + "\n", 0o600)
        _chown_main(f)
        write_private(self._p("app_key.pub"), key + "\n", 0o600)

    def enable_ssh_password_auth(self) -> list[str]:
        """D8: password login is allowed once the Pi is claimed. Returns warnings.

        sshd is first-match-wins, so this 10- file beats the image's 40-weatherpi.conf (`no`).
        """
        conf = sshd_password_conf()
        conf.parent.mkdir(parents=True, exist_ok=True)
        write_private(conf, "PasswordAuthentication yes\n", 0o644)
        warnings = []
        test = self.runner.run(["sshd", "-t"], timeout=15)
        if not test.ok and not test.missing:
            conf.unlink(missing_ok=True)
            warnings.append("sshd_config_invalid")
            return warnings
        if not self.commit_etc(str(conf)):
            warnings.append("commit_failed")
        res = self.runner.run(["systemctl", "reload", "ssh"], timeout=30)
        if not res.ok:
            warnings.append("ssh_reload_failed")
        return warnings

    def set_timezone(self, tz: str) -> bool:
        res = self.runner.run(["timedatectl", "set-timezone", tz], timeout=15)
        if res.ok:
            self.commit_etc("/etc/localtime", "/etc/timezone")
        if res.ok and hasattr(time, "tzset"):
            os.environ.pop("TZ", None)
            time.tzset()
        return res.ok

    # --- flows ---

    def claim(
        self,
        password: str,
        recovery_password: str,
        ssh_pubkey: str,
        api_token: str,
        owner_name: str | None,
        timezone: str | None,
    ) -> dict:
        check_password(password)
        check_recovery(recovery_password)
        check_token(api_token)
        key = check_ssh_pubkey(ssh_pubkey)
        tz = check_timezone(timezone) if timezone else "UTC"
        if not self._lock.acquire(blocking=False):
            raise ApiError(409, "busy", "a claim is already in progress")
        try:
            if self.claimed:
                raise ApiError(409, "already_claimed", "This WeatherPi already has an owner.")
            self._ensure_state_dir()
            self.set_password(password)
            self.install_ssh_key(key)
            write_private(self._p("token.sha256"), token_digest(api_token) + "\n")
            write_private(self._p("recovery.hash"), json.dumps(scrypt_hash(recovery_password)))
            self._p("recover.lockout").unlink(missing_ok=True)
            warnings = self.enable_ssh_password_auth()
            if not self.set_timezone(tz):
                warnings.append("timezone_failed")
            now = iso(utc_now())
            write_private(
                self._p("owner.json"),
                json.dumps({"owner_name": (owner_name or "")[:100], "claimed_at": now}),
            )
            write_private(self._p("claimed"), f"{now}\n", 0o644)
        finally:
            self._lock.release()
        out: dict = {"claimed": True}
        if warnings:
            out["warnings"] = warnings
        return out

    # recovery rate limit: persisted so a restart doesn't reset it
    def _lockout(self) -> dict:
        try:
            data = json.loads(self._p("recover.lockout").read_text())
            return {"failures": int(data.get("failures", 0)), "locked_until": data.get("locked_until")}
        except (OSError, ValueError, TypeError, AttributeError):
            return {"failures": 0, "locked_until": None}

    def _save_lockout(self, data: dict) -> None:
        self._ensure_state_dir()
        write_private(self._p("recover.lockout"), json.dumps(data))

    def recover(
        self,
        recovery_password: str,
        new_password: str,
        new_ssh_pubkey: str | None = None,
        new_api_token: str | None = None,
    ) -> dict:
        if not self.claimed:
            raise ApiError(409, "not_claimed", "This WeatherPi hasn't been set up yet.")
        check_password(new_password)
        key = check_ssh_pubkey(new_ssh_pubkey) if new_ssh_pubkey else None
        if new_api_token:
            check_token(new_api_token)
        with self._lock:
            state = self._lockout()
            now = time.time()
            locked_until = state.get("locked_until")
            if locked_until and now < float(locked_until):
                wait = int(float(locked_until) - now) + 1
                raise ApiError(
                    403, "bad_recovery",
                    f"Too many wrong attempts. Try again in {wait // 60 + 1} minutes.",
                    locked=True, retry_after_s=wait,
                )
            if locked_until:  # lockout expired: start counting again
                state = {"failures": 0, "locked_until": None}
            try:
                record = json.loads(self._p("recovery.hash").read_text())
            except (OSError, ValueError):
                record = {}
            if not scrypt_verify(recovery_password or "", record):
                state["failures"] += 1
                remaining = MAX_RECOVER_FAILURES - state["failures"]
                if remaining <= 0:
                    state["locked_until"] = now + LOCKOUT_SECONDS
                self._save_lockout(state)
                raise ApiError(
                    403, "bad_recovery", "The recovery password is wrong.",
                    locked=remaining <= 0, attempts_left=max(remaining, 0),
                    **({"retry_after_s": LOCKOUT_SECONDS} if remaining <= 0 else {}),
                )
            self._p("recover.lockout").unlink(missing_ok=True)
            self.set_password(new_password)
            if key:
                try:
                    old = self._p("app_key.pub").read_text().strip() or None
                except OSError:
                    old = None
                self.install_ssh_key(key, replace=old)
            if new_api_token:
                write_private(self._p("token.sha256"), token_digest(new_api_token) + "\n")
        return {
            "recovered": True,
            "ssh_key_replaced": bool(key),
            "api_token_replaced": bool(new_api_token),
        }
