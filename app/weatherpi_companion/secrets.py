"""App data folder, per-station secrets and profile.

Layout (docs/companion-app.md, "Secrets and app data")::

    <home>/stations/<serial>/profile.json
                             id_ed25519.pub
                             id_ed25519.dpapi   (Windows)  | id_ed25519   (0600, Linux)
                             api_token.dpapi    (Windows)  | api_token    (0600, Linux)
                             recovery.dpapi     (Windows)  | recovery     (0600, Linux)
                             RECOVERY.txt       readable on purpose (D6)
    <home>/backups/
    <home>/logs/companion.log

``<home>`` is ``%LOCALAPPDATA%\\WeatherPi`` on Windows, ``~/.local/share/weatherpi`` on
Linux, or ``$WEATHERPI_COMPANION_HOME`` when set (tests). The owner's password is never
stored anywhere.
"""

from __future__ import annotations

import json
import os
import re
import secrets as _stdlib_secrets
import socket
import sys
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ENV_HOME = "WEATHERPI_COMPANION_HOME"
_SERIAL_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

#: alphabet for the recovery password: lowercase + digits without look-alikes (0 o 1 l i)
RECOVERY_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"
RECOVERY_GROUPS = 5
RECOVERY_GROUP_LEN = 5


# --------------------------------------------------------------------------- locations


def app_home() -> Path:
    override = os.environ.get(ENV_HOME)
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "WeatherPi"
    xdg = os.environ.get("XDG_DATA_HOME")
    return (Path(xdg) if xdg else Path.home() / ".local" / "share") / "weatherpi"


@dataclass(frozen=True)
class AppData:
    """The app data folder. Create with ``AppData()`` (default location) or a path."""

    home: Path = field(default_factory=app_home)

    @property
    def stations_dir(self) -> Path:
        return self.home / "stations"

    @property
    def backups_dir(self) -> Path:
        return self.home / "backups"

    @property
    def logs_dir(self) -> Path:
        return self.home / "logs"

    @property
    def log_file(self) -> Path:
        return self.logs_dir / "companion.log"

    def ensure(self) -> AppData:
        for p in (self.stations_dir, self.backups_dir, self.logs_dir):
            p.mkdir(parents=True, exist_ok=True)
        return self

    def station(self, serial: str) -> StationStore:
        return StationStore(self, serial)

    def list_stations(self) -> list[str]:
        """Serials of stations that have a folder, sorted."""
        if not self.stations_dir.is_dir():
            return []
        return sorted(p.name for p in self.stations_dir.iterdir()
                      if p.is_dir() and _SERIAL_RE.match(p.name))

    def list_profiles(self) -> list[StationProfile]:
        out = []
        for serial in self.list_stations():
            prof = self.station(serial).load_profile()
            if prof:
                out.append(prof)
        return out

    def log_tail(self, max_lines: int = 60) -> str:
        try:
            lines = self.log_file.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return ""
        return "\n".join(lines[-max_lines:])


# --------------------------------------------------------------------------- generation


def generate_api_token() -> str:
    """32 random bytes, base64url (what ``/v1/claim`` expects)."""
    return _stdlib_secrets.token_urlsafe(32)


def generate_recovery_password() -> str:
    """Five groups of five easy-to-read characters, e.g. ``k7mq4-xr9ta-...`` (29 chars).

    About 124 bits of entropy; no 0/o/1/l/i so it can be typed from a printout.
    """
    groups = [
        "".join(_stdlib_secrets.choice(RECOVERY_ALPHABET) for _ in range(RECOVERY_GROUP_LEN))
        for _ in range(RECOVERY_GROUPS)
    ]
    return "-".join(groups)


@dataclass(frozen=True)
class SshKeyPair:
    private_openssh: bytes  # "-----BEGIN OPENSSH PRIVATE KEY-----" (unencrypted)
    public_openssh: str  # "ssh-ed25519 AAAA... comment"


def default_key_comment() -> str:
    host = re.sub(r"[^A-Za-z0-9._-]", "", socket.gethostname()) or "pc"
    return f"weatherpi-companion@{host}"


def generate_ssh_keypair(comment: str | None = None) -> SshKeyPair:
    key = Ed25519PrivateKey.generate()
    priv = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.OpenSSH,
        serialization.NoEncryption(),
    )
    pub = key.public_key().public_bytes(
        serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH
    ).decode("ascii")
    return SshKeyPair(priv, f"{pub} {comment or default_key_comment()}")


@dataclass(frozen=True)
class StationSecrets:
    """What the app needs to manage one station. Never contains the owner's password."""

    ssh_key: SshKeyPair
    api_token: str
    recovery_password: str

    @classmethod
    def generate(cls, comment: str | None = None) -> StationSecrets:
        return cls(generate_ssh_keypair(comment), generate_api_token(),
                   generate_recovery_password())

    def known_values(self) -> list[str]:
        """Secret strings that must never appear in anything shown/shared (for redaction)."""
        return [self.api_token, self.recovery_password,
                self.ssh_key.private_openssh.decode("ascii", "replace")]


# --------------------------------------------------------------------------- protection


class ProtectionError(Exception):
    pass


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    class _DataBlob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    _crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob), wintypes.LPCWSTR, ctypes.POINTER(_DataBlob),
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_DataBlob),
    ]
    _crypt32.CryptProtectData.restype = wintypes.BOOL
    _crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DataBlob), ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(_DataBlob),
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_DataBlob),
    ]
    _crypt32.CryptUnprotectData.restype = wintypes.BOOL
    _kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    _kernel32.LocalFree.restype = ctypes.c_void_p

    _CRYPTPROTECT_UI_FORBIDDEN = 0x1
    _ENTROPY = b"WeatherPi Companion v2"

    def _blob(data: bytes) -> tuple[_DataBlob, Any]:
        buf = ctypes.create_string_buffer(data, len(data))
        return _DataBlob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf

    def _take(blob: _DataBlob) -> bytes:
        try:
            return ctypes.string_at(blob.pbData, blob.cbData)
        finally:
            _kernel32.LocalFree(ctypes.cast(blob.pbData, ctypes.c_void_p))

    def dpapi_protect(data: bytes, description: str = "WeatherPi") -> bytes:
        """Encrypt for the current Windows user (CryptProtectData)."""
        inp, _k1 = _blob(data)
        ent, _k2 = _blob(_ENTROPY)
        out = _DataBlob()
        if not _crypt32.CryptProtectData(ctypes.byref(inp), description, ctypes.byref(ent),
                                         None, None, _CRYPTPROTECT_UI_FORBIDDEN,
                                         ctypes.byref(out)):
            raise ProtectionError(f"CryptProtectData failed ({ctypes.get_last_error()})")
        return _take(out)

    def dpapi_unprotect(data: bytes) -> bytes:
        inp, _k1 = _blob(data)
        ent, _k2 = _blob(_ENTROPY)
        out = _DataBlob()
        if not _crypt32.CryptUnprotectData(ctypes.byref(inp), None, ctypes.byref(ent),
                                           None, None, _CRYPTPROTECT_UI_FORBIDDEN,
                                           ctypes.byref(out)):
            raise ProtectionError(
                "CryptUnprotectData failed "
                f"({ctypes.get_last_error()}); the file belongs to another Windows user or PC"
            )
        return _take(out)

    DPAPI_AVAILABLE = True
else:  # pragma: no cover - exercised on Linux only

    def dpapi_protect(data: bytes, description: str = "WeatherPi") -> bytes:
        raise ProtectionError("DPAPI is only available on Windows")

    def dpapi_unprotect(data: bytes) -> bytes:
        raise ProtectionError("DPAPI is only available on Windows")

    DPAPI_AVAILABLE = False


def _write_private(path: Path, data: bytes) -> None:
    """Write a file only the current user can read (0600 on POSIX), atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0)
    fd = os.open(tmp, flags, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    if os.name == "posix":
        os.chmod(tmp, 0o600)
    os.replace(tmp, path)


class SecretFile:
    """A secret stored as ``<name>.dpapi`` (Windows) or ``<name>`` with mode 0600."""

    def __init__(self, folder: Path, name: str, use_dpapi: bool | None = None) -> None:
        self.use_dpapi = DPAPI_AVAILABLE if use_dpapi is None else use_dpapi
        self.path = folder / (f"{name}.dpapi" if self.use_dpapi else name)

    def exists(self) -> bool:
        return self.path.is_file()

    def write(self, data: bytes) -> None:
        _write_private(self.path, dpapi_protect(data) if self.use_dpapi else data)

    def read(self) -> bytes:
        raw = self.path.read_bytes()
        return dpapi_unprotect(raw) if self.use_dpapi else raw


# --------------------------------------------------------------------------- profile


@dataclass
class StationProfile:
    serial: str
    hostname: str = "weatherpi"
    model: str = ""
    owner_name: str | None = None
    claimed_at: str | None = None
    tailnet_ip: str | None = None
    dns_name: str | None = None
    tailnet: str | None = None
    serve_url: str | None = None
    image_version: str | None = None
    agent_version: str | None = None
    ssh_host_key: str | None = None  # "ssh-ed25519 AAAA..." seen at claim (trust on first use)
    setup_complete: bool = False
    last_backup_at: str | None = None

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> StationProfile:
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})


RECOVERY_TEXT = """\
WeatherPi recovery password
===========================

Station:   {hostname} (serial {serial})
Created:   {created}

    Recovery password:  {recovery_password}

KEEP THIS FILE SAFE. Anyone with this password and a USB cable plugged into the
WeatherPi can set a new password for it.

What it is for
--------------
If you forget the password you chose for your WeatherPi, this recovery password
lets you set a new one. You do not need it for anything else.

How to use it
-------------
1. Plug the WeatherPi's USB port (not PWR) into this computer with the data cable.
   That also powers it. Unplug its power supply from PWR first: power in both
   ports at once can destroy the WeatherPi.
2. Open WeatherPi Companion and choose "Forgot password".
3. The app reads this file automatically. If you are on a different computer,
   copy this file over, or type the recovery password when asked.

If you lose both your password and this file, the only way back in is to
re-flash the SD card (your recorded weather data would need a backup to restore).

Your own password is never stored by the app.
"""


class StationStore:
    """The ``stations/<serial>/`` folder for one Pi."""

    def __init__(self, app: AppData, serial: str) -> None:
        if not _SERIAL_RE.match(serial or ""):
            raise ValueError(f"not a valid serial: {serial!r}")
        self.app = app
        self.serial = serial
        self.dir = app.stations_dir / serial

    # files
    @property
    def profile_path(self) -> Path:
        return self.dir / "profile.json"

    @property
    def public_key_path(self) -> Path:
        return self.dir / "id_ed25519.pub"

    @property
    def recovery_txt_path(self) -> Path:
        return self.dir / "RECOVERY.txt"

    def _private_key(self) -> SecretFile:
        return SecretFile(self.dir, "id_ed25519")

    def _token(self) -> SecretFile:
        return SecretFile(self.dir, "api_token")

    def _recovery(self) -> SecretFile:
        return SecretFile(self.dir, "recovery")

    # secrets
    def has_secrets(self) -> bool:
        return self._private_key().exists() and self._token().exists()

    def save_secrets(self, s: StationSecrets) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self._private_key().write(s.ssh_key.private_openssh)
        self.public_key_path.write_text(s.ssh_key.public_openssh + "\n", encoding="ascii")
        self._token().write(s.api_token.encode("ascii"))
        self._recovery().write(s.recovery_password.encode("ascii"))

    def load_secrets(self) -> StationSecrets | None:
        if not self.has_secrets():
            return None
        pub = self.public_key_path.read_text(encoding="ascii").strip()
        recovery = (self._recovery().read().decode("ascii") if self._recovery().exists()
                    else self.read_recovery_password_from_txt() or "")
        return StationSecrets(
            SshKeyPair(self._private_key().read(), pub),
            self._token().read().decode("ascii"),
            recovery,
        )

    def replace_token(self, token: str) -> None:
        self._token().write(token.encode("ascii"))

    # recovery file
    def write_recovery_file(self, recovery_password: str, hostname: str = "weatherpi") -> Path:
        self.dir.mkdir(parents=True, exist_ok=True)
        text = RECOVERY_TEXT.format(
            hostname=hostname,
            serial=self.serial,
            created=datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
            recovery_password=recovery_password,
        )
        self.recovery_txt_path.write_text(text, encoding="utf-8")
        return self.recovery_txt_path

    def read_recovery_password_from_txt(self, path: Path | None = None) -> str | None:
        return read_recovery_password(path or self.recovery_txt_path)

    # profile
    def save_profile(self, profile: StationProfile) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.profile_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(profile.to_json(), indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, self.profile_path)

    def load_profile(self) -> StationProfile | None:
        try:
            data = json.loads(self.profile_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        data.setdefault("serial", self.serial)
        return StationProfile.from_json(data)

    def update_profile(self, **changes: Any) -> StationProfile:
        prof = self.load_profile() or StationProfile(serial=self.serial)
        for k, v in changes.items():
            if not hasattr(prof, k):
                raise AttributeError(k)
            setattr(prof, k, v)
        self.save_profile(prof)
        return prof


_RECOVERY_LINE_RE = re.compile(r"Recovery password:\s*([a-z0-9-]{20,})", re.IGNORECASE)


def read_recovery_password(path: Path) -> str | None:
    """Pull the recovery password out of a RECOVERY.txt (e.g. one copied from another PC)."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = _RECOVERY_LINE_RE.search(text)
    return m.group(1) if m else None
