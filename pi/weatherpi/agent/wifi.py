"""WiFi via NetworkManager's nmcli (terse output, C locale)."""

import logging
import os
import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from weatherpi import hw, paths
from weatherpi.agent.errors import ApiError, Job, JobFailed, busy
from weatherpi.agent.oscmd import Runner

log = logging.getLogger("weatherpi.agent")

WIFI_TYPE = "802-11-wireless"
CONNECT_WAIT_S = 45
TMP_PREFIX = "weatherpi-new-"
PRIORITY_MIN, PRIORITY_MAX = -100, 100  # API range for connection.autoconnect-priority
SSID_KEY = "802-11-wireless.ssid"
HIDDEN_KEY = "802-11-wireless.hidden"
KEY_MGMT_KEY = "802-11-wireless-security.key-mgmt"
EDIT_LOCK_TIMEOUT_S = 30
_UUID_RE = re.compile(r"\(([0-9a-fA-F-]{36})\)")


def connect_lock_file() -> Path:
    """Exists while a wifi/connect job runs; the watchdog skips its recovery meanwhile."""
    return paths.ROOT / "run/weatherpi/wifi-connect.lock"


def split_terse(line: str) -> list[str]:
    """Split one `nmcli -t` line on unescaped ':' and undo `\\:` / `\\\\` escapes."""
    fields, cur, i = [], [], 0
    while i < len(line):
        c = line[i]
        if c == "\\" and i + 1 < len(line):
            cur.append(line[i + 1])
            i += 2
            continue
        if c == ":":
            fields.append("".join(cur))
            cur = []
        else:
            cur.append(c)
        i += 1
    fields.append("".join(cur))
    return fields


def normalize_security(raw: str) -> str:
    tokens = set(raw.replace("/", " ").split())
    if not tokens or tokens == {"--"}:
        return "open"
    if "802.1X" in tokens:
        return "enterprise"
    if {"WPA2", "WPA3"} <= tokens:
        return "WPA2/WPA3"  # transition mode: a WPA2 (PSK) profile works
    for name in ("WPA3", "WPA2", "WPA1", "WEP"):
        if name in tokens:
            return name
    return raw.strip()


def check_ssid(ssid: str) -> None:
    if not ssid or len(ssid.encode("utf-8")) > 32 or any(c in ssid for c in "\r\n\x00"):
        raise ApiError(400, "bad_request", "ssid must be 1-32 bytes")


def check_psk(password: str | None) -> None:
    """An empty/None password means an open network; otherwise WPA-PSK rules apply."""
    if password and not (8 <= len(password) <= 63 or re.fullmatch(r"[0-9a-fA-F]{64}", password)):
        raise ApiError(400, "bad_request", "WiFi passwords are 8-63 characters")
    if any(c in (password or "") for c in "\r\n\x00"):
        raise ApiError(400, "bad_request", "bad characters in the WiFi password")


def check_priority(priority: object) -> int:
    if (isinstance(priority, bool) or not isinstance(priority, int)
            or not PRIORITY_MIN <= priority <= PRIORITY_MAX):
        raise ApiError(400, "bad_request",
                       f"priority must be a whole number from {PRIORITY_MIN} to {PRIORITY_MAX}")
    return priority


def _to_int(value: str) -> int:
    try:
        return int(value.strip())
    except ValueError:
        return 0


def parse_props(stdout: str) -> dict[str, str]:
    """`nmcli -t -m multiline -f a.b,c.d connection show ...` -> {"a.b": value, ...}."""
    out: dict[str, str] = {}
    for line in stdout.splitlines():
        f = split_terse(line)
        if len(f) >= 2 and f[0]:
            out[f[0]] = ":".join(f[1:])
    return out


def classify_failure(output: str, timed_out: bool) -> str:
    text = output.lower()
    if timed_out or "timeout expired" in text:
        return "timeout"
    if "secrets were required" in text or "802-11-wireless-security.psk" in text \
            or "supplicant" in text or "psk: property is invalid" in text:
        return "wrong_password"
    if "could not be found" in text or "no network with ssid" in text:
        return "not_found"
    return "failed"


class WifiManager:
    def __init__(self, runner: Runner, cfg: dict):
        self.r = runner
        self.ping_target = str(cfg.get("wifi", {}).get("ping_target", "1.1.1.1"))
        self.job = Job("wifi-connect")
        self.last_error: str | None = None
        # Serialises profile edits (save/priority/delete) with the start of a connect job,
        # so an edit never interleaves with a connect: the edit either finishes first or
        # sees the job running and answers 409 busy.
        self._edit_lock = threading.Lock()

    @contextmanager
    def _editing(self) -> Iterator[None]:
        if not self._edit_lock.acquire(timeout=EDIT_LOCK_TIMEOUT_S):
            raise busy("a WiFi change")
        try:
            yield
        finally:
            self._edit_lock.release()

    @contextmanager
    def _editing_idle(self) -> Iterator[None]:
        """_editing(), refusing with 409 busy while a connect job runs."""
        with self._editing():
            if self.job.running:
                raise busy("wifi-connect")
            yield

    # ---------- radio (Pi OS soft-blocks WiFi until a country is set) ----------

    def ensure_radio(self) -> None:
        self.r.run(["rfkill", "unblock", "wifi"], timeout=10)
        self.r.run(["nmcli", "radio", "wifi", "on"], timeout=10)

    def radio_blocked(self) -> bool:
        res = self.r.run(["rfkill", "list", "wifi"], timeout=10)
        if res.ok and re.search(r"blocked:\s*yes", res.stdout, re.IGNORECASE):
            return True
        res = self.r.run(["nmcli", "radio", "wifi"], timeout=10)
        return res.ok and res.stdout.strip().lower() == "disabled"

    # ---------- reading state ----------

    def device(self) -> tuple[str, str, str]:
        """(device, state, connection name) of the first wifi device."""
        res = self.r.run(["nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device", "status"])
        for line in res.stdout.splitlines():
            f = split_terse(line)
            if len(f) >= 3 and f[1] == "wifi":
                return f[0], f[2], (f[3] if len(f) > 3 else "")
        return "wlan0", "unavailable", ""

    def _list(self, rescan: bool) -> list[dict]:
        argv = ["nmcli", "-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY", "device", "wifi", "list"]
        res = self.r.run([*argv, "--rescan", "yes" if rescan else "no"], timeout=30)
        if not res.ok and rescan:  # e.g. "Scanning not allowed while already scanning"
            res = self.r.run([*argv, "--rescan", "no"], timeout=30)
        out = []
        for line in res.stdout.splitlines():
            f = split_terse(line)
            if len(f) < 4:
                continue
            in_use, ssid, signal, security = f[0], f[1], f[2], ":".join(f[3:])
            if not ssid:
                continue  # hidden networks aren't listed
            try:
                sig = int(signal)
            except ValueError:
                sig = 0
            out.append({
                "ssid": ssid,
                "signal": sig,
                "security": normalize_security(security),
                "in_use": in_use.strip() == "*",
            })
        return out

    def profile_props(self, uuid: str, *keys: str) -> dict[str, str]:
        """Selected properties of one profile. A setting the profile doesn't have (e.g. no
        802-11-wireless-security on an open network) is simply missing from the result."""
        res = self.r.run(["nmcli", "-t", "-m", "multiline", "-f", ",".join(keys),
                          "connection", "show", "uuid", uuid])
        return parse_props(res.stdout) if res.ok else {}

    def profiles(self) -> list[dict]:
        """WiFi profiles: [{"uuid", "name", "ssid", "priority", "active", "hidden"}].

        Only type 802-11-wireless, so the USB gadget's ethernet profiles never appear. This
        includes an in-progress connect's temporary profile (see managed_profiles()).
        """
        res = self.r.run(["nmcli", "-t", "-f", "UUID,TYPE,AUTOCONNECT-PRIORITY,ACTIVE,NAME",
                          "connection", "show"])
        out = []
        for line in res.stdout.splitlines():
            f = split_terse(line)
            if len(f) < 5 or f[1] != WIFI_TYPE:
                continue
            uuid, name = f[0], ":".join(f[4:])
            props = self.profile_props(uuid, SSID_KEY, HIDDEN_KEY)
            out.append({
                "uuid": uuid,
                "name": name,
                "ssid": props.get(SSID_KEY) or name,
                "priority": _to_int(f[2]),
                "active": f[3].strip().lower() == "yes",
                "hidden": props.get(HIDDEN_KEY, "").strip().lower() in ("yes", "true"),
            })
        return out

    def managed_profiles(self) -> list[dict]:
        """The saved networks the API manages: WiFi profiles minus a connect's temp profile."""
        return [p for p in self.profiles() if not p["name"].startswith(TMP_PREFIX)]

    def saved_ssids(self) -> list[str]:
        seen: list[str] = []
        for p in self.managed_profiles():
            if p["ssid"] not in seen:
                seen.append(p["ssid"])
        return seen

    def list_saved(self) -> dict:
        """GET /v1/wifi/saved: one entry per SSID, highest priority first, then by SSID."""
        merged: dict[str, dict] = {}
        for p in self.managed_profiles():
            cur = merged.get(p["ssid"])
            if cur is None:
                merged[p["ssid"]] = {"ssid": p["ssid"], "priority": p["priority"],
                                     "hidden": p["hidden"], "in_use": p["active"]}
            else:  # several profiles for one SSID (e.g. one made by hand): merge them
                cur["priority"] = max(cur["priority"], p["priority"])
                cur["hidden"] = cur["hidden"] or p["hidden"]
                cur["in_use"] = cur["in_use"] or p["active"]
        nets = sorted(merged.values(),
                      key=lambda n: (-n["priority"], n["ssid"].lower(), n["ssid"]))
        return {"networks": nets}

    def scan(self) -> dict:
        if self.job.running:
            raise busy("wifi-connect")
        self.ensure_radio()
        networks = self._list(rescan=True)
        known = set(self.saved_ssids())
        best: dict[str, dict] = {}
        for n in networks:
            cur = best.get(n["ssid"])
            if cur is None or n["signal"] > cur["signal"]:
                merged = dict(n, in_use=n["in_use"] or bool(cur and cur["in_use"]))
                best[n["ssid"]] = merged
            elif n["in_use"]:
                cur["in_use"] = True
        result = sorted(best.values(), key=lambda n: (-n["signal"], n["ssid"].lower()))
        for n in result:
            n["known"] = n["ssid"] in known
        if not result and self.radio_blocked():
            self.last_error = "radio_blocked"
        elif result and self.last_error == "radio_blocked":
            self.last_error = None
        return {
            "networks": [
                {k: n[k] for k in ("ssid", "signal", "security", "known", "in_use")}
                for n in result
            ]
        }

    def internet(self, dev: str | None = None) -> bool:
        argv = ["ping", "-c", "1", "-W", "2"]
        if dev:
            argv += ["-I", dev]
        return self.r.run([*argv, self.ping_target], timeout=6).ok

    def status(self) -> dict:
        dev, nm_state, conn_name = self.device()
        saved = self.saved_ssids()
        ssid = ip = signal = None
        if nm_state == "connected":
            for n in self._list(rescan=False):
                if n["in_use"]:
                    ssid, signal = n["ssid"], n["signal"]
                    break
            if ssid is None:  # hidden networks aren't in the scan list
                ssid = next((p["ssid"] for p in self.profiles() if p["name"] == conn_name),
                            conn_name or None)
            res = self.r.run(["nmcli", "-g", "IP4.ADDRESS", "device", "show", dev])
            first = res.stdout.strip().split(" | ")[0].strip() if res.ok else ""
            ip = first.split("/")[0] or None
        if self.job.running:
            state = "connecting"
            ssid = self.job.detail.get("ssid")
        elif nm_state == "connected":
            state = "connected"
        elif self.job.state == "failed":
            state = "failed"
        elif nm_state.startswith("connecting"):
            state = "connecting"
        else:
            state = "disconnected"
        return {
            "state": state,
            "ssid": ssid,
            "ip": ip,
            "signal": signal,
            "internet": state == "connected" and self.internet(dev),
            "last_error": self.last_error,
            "saved": saved,
        }

    # ---------- changing state ----------

    def start_connect(self, ssid: str, password: str, hidden: bool,
                      priority: int | None = None) -> dict:
        """priority None: an existing profile for the SSID keeps its priority, a new one gets 0."""
        check_ssid(ssid)
        check_psk(password)
        if priority is not None:
            check_priority(priority)
        with self._editing():  # never starts in the middle of a save/priority/delete
            self.job.start(lambda job: self._connect(ssid, password, hidden, priority),
                           ssid=ssid)
        return {"job": self.job.name}

    def _connect(self, ssid: str, password: str, hidden: bool,
                 priority: int | None = None) -> None:
        lock = connect_lock_file()
        try:
            lock.parent.mkdir(parents=True, exist_ok=True)
            lock.write_text(f"{os.getpid()}\n", encoding="utf-8")
        except OSError as exc:
            log.warning("can't create %s: %s", lock, exc)
        try:
            self._connect_inner(ssid, password, hidden, priority)
        except JobFailed as exc:
            self.last_error = exc.code
            hw.led_blink("fail")
            raise
        finally:
            lock.unlink(missing_ok=True)
        self.last_error = None
        hw.led_blink("success")
        self.r.run(["systemctl", "try-restart", "systemd-timesyncd.service"], timeout=15)

    def _connect_inner(self, ssid: str, password: str, hidden: bool,
                       priority: int | None = None) -> None:
        self.ensure_radio()
        dev = self.device()[0]
        visible = {n["ssid"]: n for n in self._list(rescan=not hidden)}
        if not visible and self.radio_blocked():
            raise JobFailed("radio_blocked")
        if not hidden and ssid not in visible:
            raise JobFailed("not_found")
        security = visible.get(ssid, {}).get("security")
        tmp_name = TMP_PREFIX + ssid
        existing: list[int] = []
        for p in self.profiles():
            if p["name"] == tmp_name:  # leftovers from an interrupted attempt
                self.r.run(["nmcli", "connection", "delete", "uuid", p["uuid"]])
            elif p["ssid"] == ssid:
                existing.append(p["priority"])
        if priority is None:  # an existing profile keeps its priority, a new one gets 0
            priority = max(existing, default=0)
        argv = ["nmcli", "connection", "add", "type", "wifi", "con-name", tmp_name,
                "ifname", dev, "ssid", ssid, "connection.autoconnect", "yes",
                "connection.autoconnect-priority", str(priority)]
        if password and security != "open":
            mgmt = "sae" if security == "WPA3" else "wpa-psk"
            argv += ["wifi-sec.key-mgmt", mgmt, "wifi-sec.psk", password]
        if hidden:
            argv += ["wifi.hidden", "yes"]
        added = self.r.run(argv, timeout=30)
        m = _UUID_RE.search(added.stdout)
        if not added.ok or not m:
            log.warning("nmcli connection add failed: %s", added.stderr.strip()[:200])
            raise JobFailed("failed")
        uuid = m.group(1)
        up = self.r.run(
            ["nmcli", "--wait", str(CONNECT_WAIT_S), "connection", "up", "uuid", uuid],
            timeout=CONNECT_WAIT_S + 15,
        )
        if not up.ok:
            self.r.run(["nmcli", "connection", "delete", "uuid", uuid])
            raise JobFailed(classify_failure(up.output, up.timed_out))
        # success: this profile replaces any older ones for the same SSID
        for p in self.profiles():
            if p["ssid"] == ssid and p["uuid"] != uuid:
                self.r.run(["nmcli", "connection", "delete", "uuid", p["uuid"]])
        self.r.run(["nmcli", "connection", "modify", "uuid", uuid, "connection.id", ssid])

    def delete_saved(self, ssid: str) -> dict:
        with self._editing_idle():
            matches = [p for p in self.profiles() if p["ssid"] == ssid]
            if not matches:
                raise ApiError(404, "not_found", f"No saved network called {ssid!r}")
            for p in matches:
                self.r.run(["nmcli", "connection", "delete", "uuid", p["uuid"]])
        return {"deleted": ssid}

    # ---------- several saved networks (agent 2.1.0) ----------

    def _visible_security(self, ssid: str) -> str | None:
        """Security of ssid in NetworkManager's last scan (no rescan), None if not seen."""
        for n in self._list(rescan=False):
            if n["ssid"] == ssid:
                return n["security"]
        return None

    def save(self, ssid: str, password: str | None, hidden: bool = False,
             priority: int = 0) -> dict:
        """POST /v1/wifi/saved: store a network for autoconnect without connecting to it.

        The current connection is never touched: if the SSID is the one in use, its active
        profile is updated in place (`nmcli connection modify` doesn't re-activate), so the
        new settings only apply the next time the Pi joins that network.
        """
        check_ssid(ssid)
        check_psk(password)
        check_priority(priority)
        with self._editing_idle():
            security = self._visible_security(ssid)
            use_psk = bool(password) and security != "open"
            existing = [p for p in self.managed_profiles() if p["ssid"] == ssid]
            active = next((p for p in existing if p["active"]), None)
            if active is not None:
                self._update_in_place(active["uuid"], password if use_psk else None, hidden,
                                      priority, security)
                old = [p for p in existing if p is not active]
            else:
                self._add_saved(ssid, password if use_psk else None, hidden, priority,
                                security)
                old = existing
            for p in old:  # the new profile replaces older ones for the same SSID
                self.r.run(["nmcli", "connection", "delete", "uuid", p["uuid"]])
        return {"ssid": ssid}

    @staticmethod
    def _key_mgmt(security: str | None) -> str:
        # Out of range we can't tell; WPA2-PSK is the safe default (WPA2/WPA3 transition
        # networks accept it too). A WPA3-only network should be joined with wifi/connect.
        return "sae" if security == "WPA3" else "wpa-psk"

    def _add_saved(self, ssid: str, password: str | None, hidden: bool, priority: int,
                   security: str | None) -> None:
        argv = ["nmcli", "connection", "add", "type", "wifi", "con-name", ssid,
                "ifname", self.device()[0], "ssid", ssid, "connection.autoconnect", "yes",
                "connection.autoconnect-priority", str(priority)]
        if password:
            argv += ["wifi-sec.key-mgmt", self._key_mgmt(security), "wifi-sec.psk", password]
        if hidden:
            argv += ["wifi.hidden", "yes"]
        added = self.r.run(argv, timeout=30)
        if not added.ok or not _UUID_RE.search(added.stdout):
            log.warning("nmcli connection add failed: %s", added.stderr.strip()[:200])
            raise ApiError(500, "internal", "NetworkManager couldn't save the network")

    def _update_in_place(self, uuid: str, password: str | None, hidden: bool, priority: int,
                         security: str | None) -> None:
        argv = ["nmcli", "connection", "modify", "uuid", uuid,
                "connection.autoconnect", "yes",
                "connection.autoconnect-priority", str(priority),
                "802-11-wireless.hidden", "yes" if hidden else "no"]
        current = self.profile_props(uuid, KEY_MGMT_KEY).get(KEY_MGMT_KEY, "").strip()
        if password:
            mgmt = current if current in ("wpa-psk", "sae") else self._key_mgmt(security)
            argv += ["wifi-sec.key-mgmt", mgmt, "wifi-sec.psk", password]
        elif current:
            argv += ["remove", "802-11-wireless-security"]  # now saved as an open network
        res = self.r.run(argv, timeout=30)
        if not res.ok:
            log.warning("nmcli connection modify failed: %s", res.stderr.strip()[:200])
            raise ApiError(500, "internal", "NetworkManager couldn't update the network")

    def set_priority(self, ssid: str, priority: int) -> dict:
        """PATCH /v1/wifi/saved/{ssid}: change only the autoconnect priority."""
        check_priority(priority)
        with self._editing_idle():
            matches = [p for p in self.managed_profiles() if p["ssid"] == ssid]
            if not matches:
                raise ApiError(404, "not_found", f"No saved network called {ssid!r}")
            for p in matches:
                res = self.r.run(["nmcli", "connection", "modify", "uuid", p["uuid"],
                                  "connection.autoconnect-priority", str(priority)])
                if not res.ok:
                    log.warning("nmcli connection modify failed: %s",
                                res.stderr.strip()[:200])
                    raise ApiError(500, "internal",
                                   "NetworkManager couldn't change the priority")
        return {"ssid": ssid, "priority": priority}
