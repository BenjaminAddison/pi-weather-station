"""A stateful in-memory imitation of the OS commands the agent runs (WEATHERPI_FAKE=1).

It answers the same argv the real code uses with output in the real tools' formats, so the
parsers in wifi.py / tailscale.py / system.py are exercised in tests.

Fake world (example data only):
- WiFi networks: ExampleWiFi (WPA2), ExampleWiFi-Guest, Neighbour-5G, Cafe Free WiFi (open),
  Example:Colon (escaping test), a hidden network "HiddenExample", and "Example Phone" (a
  hotspot that's out of range until `set_in_range()`). Every secured network's password is
  `correct-horse`; anything else fails with "Secrets were required".
- NetworkManager profiles carry autoconnect-priority / hidden / key-mgmt; `connection show`
  also lists the USB gadget's two ethernet profiles (as image/build.sh creates them).
  `autoconnect()` imitates NM joining the best saved network when wlan0 is idle.
- The WiFi radio starts soft-blocked (like stock Pi OS) until `rfkill unblock wifi`.
- Tailscale: NeedsLogin -> `up` sets an AuthURL -> Running after `approve()` (test hook) or
  after `auto_approve_after` seconds -> `serve` works unless `serve_allowed` is False.
- chpasswd / sshd / systemctl / date / timedatectl / weatherpi-commit-etc calls are recorded
  in memory.
"""

import json
import re
import threading
import time
import uuid as uuidlib
from collections.abc import Sequence
from dataclasses import dataclass, field

from weatherpi.agent.oscmd import Result

FAKE_PASSWORD = "correct-horse"
AUTH_URL = "https://login.tailscale.com/a/f4ke0123abcd"
SERVE_ADMIN_URL = "https://login.tailscale.com/f/serve?node=nFAKE0123CNTRL"


@dataclass
class FakeNetwork:
    ssid: str
    signal: int
    security: str  # nmcli SECURITY column; "" = open
    bssid_dupes: int = 0  # extra weaker BSSIDs with the same SSID (tests de-duplication)
    hidden: bool = False
    in_range: bool = True  # False: exists in the fake world but the Pi can't see it now


def default_networks() -> list[FakeNetwork]:
    return [
        FakeNetwork("ExampleWiFi", 72, "WPA2", bssid_dupes=1),
        FakeNetwork("Neighbour-5G", 54, "WPA2 WPA3"),
        FakeNetwork("ExampleWiFi-Guest", 61, "WPA2"),
        FakeNetwork("Cafe Free WiFi", 33, ""),
        FakeNetwork("Example:Colon", 20, "WPA1 WPA2"),
        FakeNetwork("", 40, "WPA2"),  # a hidden network as seen in a scan (no SSID)
        FakeNetwork("HiddenExample", 45, "WPA2", hidden=True),
        FakeNetwork("Example Phone", 58, "WPA2", in_range=False),  # a hotspot that's away
    ]


#: the USB gadget's ethernet profiles, as image/build.sh creates them (never listed as WiFi)
GADGET_PROFILES = (
    ("0f1e2d3c-0000-4000-8000-000000000001", "USB Gadget (shared)", 10),
    ("0f1e2d3c-0000-4000-8000-000000000002", "USB Gadget (client)", 100),
)

#: `nmcli connection modify/add` property aliases -> canonical names the fake understands
_PROP_ALIASES = {
    "wifi.hidden": "802-11-wireless.hidden",
    "wifi-sec.key-mgmt": "802-11-wireless-security.key-mgmt",
    "wifi-sec.psk": "802-11-wireless-security.psk",
    "con-name": "connection.id",
}


@dataclass
class FakeProfile:
    uuid: str
    name: str
    ssid: str
    psk: str | None
    hidden: bool = False
    priority: int = 0
    autoconnect: bool = True
    key_mgmt: str | None = None  # None: no 802-11-wireless-security setting (open)

    def props(self) -> dict[str, str]:
        """Property values as `nmcli connection show <id>` reports them."""
        out = {
            "connection.id": self.name,
            "connection.uuid": self.uuid,
            "connection.type": "802-11-wireless",
            "connection.autoconnect": "yes" if self.autoconnect else "no",
            "connection.autoconnect-priority": str(self.priority),
            "802-11-wireless.ssid": self.ssid,
            "802-11-wireless.hidden": "yes" if self.hidden else "no",
        }
        if self.key_mgmt is not None:
            out["802-11-wireless-security.key-mgmt"] = self.key_mgmt
            out["802-11-wireless-security.psk"] = self.psk or ""
        return out


@dataclass
class FakeOS:
    connect_delay: float = 0.0
    scan_delay: float = 0.0
    auth_url_delay: float = 0.0
    auto_approve_after: float | None = None  # seconds after `up`; None = only approve()
    serve_allowed: bool = True
    radio_soft_blocked: bool = True
    radio_hard_blocked: bool = False
    tailscale_installed: bool = True
    clock_synced: bool = False
    networks: list[FakeNetwork] = field(default_factory=default_networks)
    profiles: dict[str, FakeProfile] = field(default_factory=dict)
    active_uuid: str | None = None
    nm_radio_on: bool = True
    # tailscale
    ts_state: str = "NeedsLogin"
    ts_auth_url: str = ""
    ts_hostname: str = "weatherpi"
    ts_serve: dict = field(default_factory=dict)
    # recorded effects
    calls: list[list[str]] = field(default_factory=list)
    passwords: dict[str, str] = field(default_factory=dict)
    timezone: str = "UTC"
    clock_set_to: int | None = None
    power: list[str] = field(default_factory=list)
    units: dict[str, str] = field(default_factory=dict)
    journal_lines: dict[str, list[str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._lock = threading.RLock()
        self._approved = threading.Event()
        self._cancel = threading.Event()
        for unit in ("weatherpi-logger", "weatherpi-web", "weatherpi-agent", "NetworkManager",
                     "tailscaled", "ssh", "nftables", "weatherpi-watchdog"):
            self.units.setdefault(unit, "active")
        self.journal_lines.setdefault("weatherpi-agent", [
            "2026-10-19T11:00:00+0000 weatherpi weatherpi-agent[412]: started on :8765",
            ("2026-10-19T11:00:02+0000 weatherpi weatherpi-agent[412]: test authkey "
             "tskey-auth-kFAKE1CNTRL-0123456789abcdef in a log line"),
        ])
        self.journal_lines.setdefault("NetworkManager", [
            ("2026-10-19T11:00:05+0000 weatherpi NetworkManager[300]: <info> device (wlan0): "
             "state change: disconnected -> prepare"),
            "2026-10-19T11:00:06+0000 weatherpi wpa_supplicant[280]: CTRL-EVENT psk=fake-psk-value",
        ])

    # ---------- test hooks ----------

    def approve(self) -> bool:
        """Simulate the owner signing in at the AuthURL. False if no login is pending."""
        with self._lock:
            if self.ts_state != "NeedsLogin" or not self.ts_auth_url:
                return False
        self._approved.set()
        return True

    def set_online(self) -> None:
        """Jump straight to 'WiFi connected + Tailscale Running + serve on'."""
        with self._lock:
            self.radio_soft_blocked = False
            uid = str(uuidlib.uuid4())
            self.profiles[uid] = FakeProfile(uid, "ExampleWiFi", "ExampleWiFi", FAKE_PASSWORD,
                                             key_mgmt="wpa-psk")
            self.active_uuid = uid
            self.ts_state, self.ts_auth_url = "Running", ""
            self.ts_serve = self._serve_config(8080)
            self.clock_synced = True

    def set_in_range(self, ssid: str, in_range: bool, security: str = "WPA2",
                     signal: int = 50) -> str | None:
        """Move a network into or out of range (adds it if unknown), then let the fake
        NetworkManager react: drop a connection that went away, autoconnect if idle.
        Returns the SSID the Pi is connected to afterwards."""
        with self._lock:
            net = next((n for n in self.networks if n.ssid == ssid), None)
            if net is None:
                net = FakeNetwork(ssid, signal, security)
                self.networks.append(net)
            net.in_range = in_range
            if self.wifi_connected and self.profiles[self.active_uuid].ssid == ssid \
                    and not in_range:
                self.active_uuid = None
        return self.autoconnect()

    def autoconnect(self) -> str | None:
        """What NetworkManager does when wlan0 is idle: join the visible saved network with
        the highest autoconnect-priority whose password works. Returns the connected SSID."""
        with self._lock:
            if not self.wifi_connected:
                visible = {n.ssid: n for n in self._visible() if n.ssid}
                for p in sorted(self.profiles.values(), key=lambda p: -p.priority):
                    net = visible.get(p.ssid)
                    if not p.autoconnect or net is None or (net.hidden and not p.hidden):
                        continue
                    if net.security and p.psk != FAKE_PASSWORD:
                        continue
                    self.active_uuid = p.uuid
                    break
            return self.profiles[self.active_uuid].ssid if self.wifi_connected else None

    @property
    def wifi_connected(self) -> bool:
        return self.active_uuid is not None

    # ---------- dispatcher ----------

    def run(self, argv: Sequence[str], timeout: float = 30, input: str | None = None) -> Result:
        args = [str(a) for a in argv]
        with self._lock:
            self.calls.append(args)
        handler = getattr(self, "_cmd_" + args[0].replace("-", "_"), None)
        if handler is None:
            return Result(127, "", f"{args[0]}: command not found")
        return handler(args[1:], timeout, input)

    # ---------- nmcli ----------

    def _visible(self) -> list[FakeNetwork]:
        if self.radio_soft_blocked or self.radio_hard_blocked or not self.nm_radio_on:
            return []
        return [n for n in self.networks if n.in_range]

    @staticmethod
    def _esc(value: str) -> str:
        return value.replace("\\", "\\\\").replace(":", "\\:")

    def _cmd_nmcli(self, args: list[str], timeout: float, input: str | None) -> Result:
        opts: dict[str, str] = {}
        while args and args[0].startswith("-"):
            flag = args.pop(0)
            if flag in ("-f", "--fields", "-g", "--get-values", "-w", "--wait", "-m", "--mode",
                        "-e", "--escape"):
                opts[flag] = args.pop(0)
            else:
                opts[flag] = ""
        joined = " ".join(args)
        with self._lock:
            if joined == "radio wifi":
                return Result(0, "enabled\n" if self.nm_radio_on and not self.radio_soft_blocked
                              and not self.radio_hard_blocked else "disabled\n")
            if joined == "radio wifi on":
                self.nm_radio_on = True
                return Result(0)
            if joined == "device status":
                state = "connected" if self.wifi_connected else (
                    "unavailable" if self.radio_soft_blocked or self.radio_hard_blocked
                    else "disconnected")
                conn = self.profiles[self.active_uuid].name if self.wifi_connected else ""
                return Result(0, f"wlan0:wifi:{state}:{self._esc(conn)}\n"
                                 f"usb0:ethernet:connected:{GADGET_PROFILES[0][1]}\n"
                                 "lo:loopback:unmanaged:\n")
            if args[:3] == ["device", "wifi", "list"]:
                return self._nm_wifi_list(args)
            if args[:2] == ["device", "show"]:
                if "IP4.ADDRESS" in opts.get("-g", "") and self.wifi_connected:
                    return Result(0, "192.168.7.9/24\n")
                return Result(0, "\n")
            if args[:2] == ["connection", "show"]:
                if len(args) == 2:
                    return self._nm_connection_list(opts.get("-f", "NAME,UUID,TYPE,DEVICE"))
                prof = self._find_profile(args[2:])
                if prof is None:
                    return Result(10, "", "Error: no such connection profile.")
                return self._nm_connection_props(prof, opts)
            if args[:2] == ["connection", "add"]:
                return self._nm_add(args[2:])
            if args[:2] == ["connection", "delete"]:
                prof = self._find_profile(args[2:])
                if prof is None:
                    return Result(10, "", "Error: unknown connection.")
                del self.profiles[prof.uuid]
                if self.active_uuid == prof.uuid:
                    self.active_uuid = None
                return Result(0, f"Connection '{prof.name}' ({prof.uuid}) successfully deleted.\n")
            if args[:2] == ["connection", "modify"]:
                prof = self._find_profile(args[2:4])
                if prof is None:
                    return Result(10, "", "Error: unknown connection.")
                return self._nm_modify(prof, args[4:])
        if args[:2] == ["connection", "up"]:
            return self._nm_up(args[2:])
        return Result(2, "", f"Error: fake nmcli doesn't know '{joined}'")

    def _find_profile(self, ref: list[str]) -> FakeProfile | None:
        if not ref:
            return None
        if ref[0] in ("uuid", "id") and len(ref) > 1:
            kind, value = ref[0], ref[1]
        else:
            kind, value = "id", ref[0]
        for p in self.profiles.values():
            if (kind == "uuid" and p.uuid == value) or (kind == "id" and p.name == value):
                return p
        return None

    def _nm_wifi_list(self, args: list[str]) -> Result:
        rescan = "--rescan" in args and args[args.index("--rescan") + 1] == "yes"
        if rescan and self.scan_delay:
            time.sleep(self.scan_delay)
        active_ssid = self.profiles[self.active_uuid].ssid if self.wifi_connected else None
        lines = []
        for n in self._visible():
            if n.hidden:
                continue
            in_use = "*" if n.ssid and n.ssid == active_ssid else " "
            lines.append(f"{in_use}:{self._esc(n.ssid)}:{n.signal}:{self._esc(n.security)}")
            for i in range(n.bssid_dupes):
                lines.append(f" :{self._esc(n.ssid)}:{max(n.signal - 25 - i, 1)}:{n.security}")
        return Result(0, "\n".join(lines) + ("\n" if lines else ""))

    def _nm_connection_list(self, fields: str) -> Result:
        """`nmcli -t -f <FIELDS> connection show`: WiFi profiles plus the gadget's ethernet."""
        rows: list[dict[str, str]] = []
        for p in self.profiles.values():
            active = p.uuid == self.active_uuid
            rows.append({"NAME": p.name, "UUID": p.uuid, "TYPE": "802-11-wireless",
                         "AUTOCONNECT": "yes" if p.autoconnect else "no",
                         "AUTOCONNECT-PRIORITY": str(p.priority),
                         "ACTIVE": "yes" if active else "no",
                         "DEVICE": "wlan0" if active else "",
                         "FILENAME": f"/etc/NetworkManager/system-connections/{p.name}"
                                     ".nmconnection"})
        for uid, name, prio in GADGET_PROFILES:
            active = prio == 10  # "shared" is the one that's up (no ICS)
            rows.append({"NAME": name, "UUID": uid, "TYPE": "802-3-ethernet",
                         "AUTOCONNECT": "yes" if active else "no",
                         "AUTOCONNECT-PRIORITY": str(prio), "ACTIVE": "yes" if active else "no",
                         "DEVICE": "usb0" if active else "",
                         "FILENAME": f"/etc/NetworkManager/system-connections/{name}"
                                     ".nmconnection"})
        names = fields.split(",")
        try:
            lines = [":".join(self._esc(r[f]) for f in names) for r in rows]
        except KeyError as exc:
            return Result(2, "", f"Error: invalid field {exc.args[0]!r}")
        return Result(0, "\n".join(lines) + "\n")

    def _nm_connection_props(self, prof: FakeProfile, opts: dict[str, str]) -> Result:
        """`nmcli -g a,b connection show <id>` (values, one per line) or
        `nmcli -t -m multiline -f a,b connection show <id>` (`name:value` lines)."""
        props = prof.props()
        if "-g" in opts:
            keys = opts["-g"].split(",")
            return Result(0, "".join(self._esc(props[k]) + "\n" for k in keys if k in props))
        keys = opts.get("-f", "").split(",")
        lines = [f"{k}:{self._esc(props[k])}" for k in keys if k in props]
        return Result(0, "\n".join(lines) + ("\n" if lines else ""))

    def _apply_props(self, prof: FakeProfile, pairs: list[str]) -> Result | None:
        """Apply `key value` / `remove <setting>` pairs like nmcli add/modify. None = ok."""
        if len(pairs) % 2:
            return Result(2, "", f"Error: value for '{pairs[-1]}' is missing.")
        for key, value in zip(pairs[::2], pairs[1::2], strict=True):
            key = _PROP_ALIASES.get(key, key)
            if key == "remove":
                if value != "802-11-wireless-security":
                    return Result(2, "", f"Error: fake nmcli can't remove setting '{value}'.")
                prof.key_mgmt, prof.psk = None, None
            elif key == "connection.id":
                prof.name = value
            elif key in ("connection.autoconnect", "802-11-wireless.hidden"):
                if value not in ("yes", "no", "true", "false"):
                    return Result(2, "", f"Error: failed to modify {key}: '{value}' is not "
                                         "valid; use 'yes' or 'no'.")
                flag = value in ("yes", "true")
                if key == "connection.autoconnect":
                    prof.autoconnect = flag
                else:
                    prof.hidden = flag
            elif key == "connection.autoconnect-priority":
                try:
                    prio = int(value)
                except ValueError:
                    prio = 10_000
                if not -999 <= prio <= 999:
                    return Result(2, "", f"Error: failed to modify {key}: '{value}' is not "
                                         "valid; use <-999-999>.")
                prof.priority = prio
            elif key == "802-11-wireless-security.key-mgmt":
                prof.key_mgmt = value
            elif key == "802-11-wireless-security.psk":
                prof.psk = value
            elif key in ("type", "ifname", "ssid"):
                continue  # handled by _nm_add
            else:
                return Result(2, "", f"Error: fake nmcli doesn't know property '{key}'.")
        if prof.psk and prof.key_mgmt is None:
            return Result(4, "", "Error: Failed to modify connection: "
                                 "802-11-wireless-security.key-mgmt: property is missing")
        return None

    def _nm_modify(self, prof: FakeProfile, pairs: list[str]) -> Result:
        trial = FakeProfile(**{k: getattr(prof, k) for k in prof.__dataclass_fields__})
        err = self._apply_props(trial, pairs)
        if err is not None:
            return err  # nmcli changes nothing when one property is bad
        for k in prof.__dataclass_fields__:
            setattr(prof, k, getattr(trial, k))
        return Result(0)

    def _nm_add(self, args: list[str]) -> Result:
        kv = dict(zip(args[::2], args[1::2], strict=False))
        if kv.get("type") != "wifi" or "ssid" not in kv:
            return Result(2, "", "Error: invalid connection.")
        uid = str(uuidlib.uuid4())
        prof = FakeProfile(uid, kv.get("con-name", kv["ssid"]), kv["ssid"], None)
        err = self._apply_props(prof, args)
        if err is not None:
            return err
        self.profiles[uid] = prof
        return Result(0, f"Connection '{prof.name}' ({uid}) successfully added.\n")

    def _nm_up(self, ref: list[str]) -> Result:
        with self._lock:
            prof = self._find_profile(ref)
        if prof is None:
            return Result(10, "", "Error: unknown connection.")
        if self.connect_delay:
            time.sleep(self.connect_delay)
        with self._lock:
            pool = [n for n in self._visible() if n.ssid == prof.ssid and n.ssid]
            if not pool or (pool[0].hidden and not prof.hidden):
                return Result(4, "", "Error: Connection activation failed: The Wi-Fi network "
                                     "could not be found.")
            net = pool[0]
            if net.security and prof.psk != FAKE_PASSWORD:
                return Result(4, "", "Error: Connection activation failed: Secrets were "
                                     "required, but not provided.")
            self.active_uuid = prof.uuid
            self.clock_synced = True  # timesyncd would sync once online
        return Result(0, "Connection successfully activated (D-Bus active path: "
                         "/org/freedesktop/NetworkManager/ActiveConnection/3)\n")

    def _cmd_rfkill(self, args: list[str], timeout: float, input: str | None) -> Result:
        with self._lock:
            if args[:1] == ["unblock"]:
                self.radio_soft_blocked = False
                return Result(0)
            if args[:1] == ["list"]:
                yn = {True: "yes", False: "no"}
                return Result(0, f"0: phy0: Wireless LAN\n\tSoft blocked: "
                                 f"{yn[self.radio_soft_blocked]}\n\tHard blocked: "
                                 f"{yn[self.radio_hard_blocked]}\n")
        return Result(1, "", "rfkill: bad usage")

    def _cmd_ping(self, args: list[str], timeout: float, input: str | None) -> Result:
        if self.wifi_connected:
            return Result(0, "1 packets transmitted, 1 received, 0% packet loss\n")
        return Result(1, "1 packets transmitted, 0 received, 100% packet loss\n")

    def _cmd_ip(self, args: list[str], timeout: float, input: str | None) -> Result:
        if args[:1] == ["-j"]:
            return Result(0, json.dumps([{"ifname": "usb0",
                                           "addr_info": [{"local": "10.12.194.1"}]}]))
        if args == ["route"]:
            gw = "default via 192.168.7.1 dev wlan0 proto dhcp metric 600\n" \
                if self.wifi_connected else ""
            return Result(0, gw + "10.12.194.0/28 dev usb0 proto kernel scope link "
                                  "src 10.12.194.1\n")
        wlan = "wlan0 UP 192.168.7.9/24" if self.wifi_connected else "wlan0 DOWN"
        ts = "\ntailscale0 UNKNOWN 100.94.2.31/32" if self.ts_state == "Running" else ""
        return Result(0, f"lo UNKNOWN 127.0.0.1/8\nusb0 UP 10.12.194.1/28\n{wlan}{ts}\n")

    # ---------- tailscale ----------

    def _serve_config(self, port: int) -> dict:
        return {"TCP": {"443": {"HTTPS": True}},
                "Web": {"weatherpi.example.ts.net:443": {
                    "Handlers": {"/": {"Proxy": f"http://127.0.0.1:{port}"}}}}}

    def _status_json(self) -> dict:
        running = self.ts_state == "Running"
        return {
            "Version": "1.94.2-tfake0123-gfake4567",
            "BackendState": self.ts_state,
            "AuthURL": self.ts_auth_url,
            "Self": {
                "HostName": self.ts_hostname,
                "DNSName": f"{self.ts_hostname}.example.ts.net." if running else "",
                "TailscaleIPs": ["100.94.2.31", "fd7a:115c:a1e0::5e01:21f"] if running else None,
                "Online": running,
            },
            "CurrentTailnet": {"Name": "owner@example.com", "MagicDNSSuffix": "example.ts.net",
                               "MagicDNSEnabled": True} if running else None,
        }

    def _cmd_tailscale(self, args: list[str], timeout: float, input: str | None) -> Result:
        if not self.tailscale_installed:
            return Result(127, "", "tailscale: command not found")
        with self._lock:
            if args == ["status", "--json"]:
                return Result(0, json.dumps(self._status_json(), indent=2))
            if args == ["serve", "status", "--json"]:
                return Result(0, json.dumps(self.ts_serve) if self.ts_serve else "{}\n")
            if args[:1] == ["serve"]:
                if self.ts_state != "Running":
                    return Result(1, "", "serve: not logged in")
                if not self.serve_allowed:
                    return Result(1, "", "Serve is not enabled on your tailnet.\nTo enable, "
                                         f"visit:\n\n         {SERVE_ADMIN_URL}\n\n")
                m = re.search(r":(\d+)$", args[-1])
                self.ts_serve = self._serve_config(int(m.group(1)) if m else 8080)
                return Result(0, "Available within your tailnet:\n\n"
                                 "https://weatherpi.example.ts.net/\n|-- proxy http://127.0.0.1:8080\n")
            if args[:1] == ["logout"]:
                self.ts_state, self.ts_auth_url, self.ts_serve = "NeedsLogin", "", {}
                self._cancel.set()
                return Result(0)
            if args[:1] == ["down"]:
                if self.ts_state == "Running":
                    self.ts_state = "Stopped"
                self._cancel.set()
                return Result(0)
        if args[:1] == ["up"]:
            return self._ts_up(args[1:], timeout)
        return Result(1, "", f"fake tailscale doesn't know {args}")

    def _ts_up(self, args: list[str], timeout: float) -> Result:
        for a in args:
            if a.startswith("--hostname="):
                self.ts_hostname = a.split("=", 1)[1]
        with self._lock:
            if self.ts_state == "Running":
                return Result(0)
            if not self.wifi_connected:
                return Result(1, "", "backend error: no internet")
            self._approved.clear()
            self._cancel.clear()
            self.ts_state = "NeedsLogin"
        if self.auth_url_delay:
            time.sleep(self.auth_url_delay)
        with self._lock:
            self.ts_auth_url = AUTH_URL
        deadline = time.monotonic() + min(timeout, 3600)
        approve_at = (time.monotonic() + self.auto_approve_after
                      if self.auto_approve_after is not None else None)
        while time.monotonic() < deadline:
            if self._approved.wait(0.05):
                break
            if self._cancel.is_set():
                return Result(1, "", "backend error: logged out")
            if approve_at is not None and time.monotonic() >= approve_at:
                break
        else:
            return Result(-1, f"\nTo authenticate, visit:\n\n\t{AUTH_URL}\n\n", "",
                          timed_out=True)
        with self._lock:
            self.ts_state, self.ts_auth_url = "Running", ""
        return Result(0, f"\nTo authenticate, visit:\n\n\t{AUTH_URL}\n\nSuccess.\n")

    # ---------- system ----------

    def _cmd_chpasswd(self, args: list[str], timeout: float, input: str | None) -> Result:
        user, _, pw = (input or "").rstrip("\n").partition(":")
        if not user or not pw or "\n" in pw:
            return Result(1, "", "chpasswd: line 1: missing new password")
        with self._lock:
            self.passwords[user] = pw
        return Result(0)

    def _cmd_sshd(self, args: list[str], timeout: float, input: str | None) -> Result:
        return Result(0)

    def _cmd_weatherpi_commit_etc(self, args: list[str], timeout: float,
                                  input: str | None) -> Result:
        # the real helper copies /etc files to the read-only lower root (D14); recorded only
        return Result(0)

    def _cmd_systemctl(self, args: list[str], timeout: float, input: str | None) -> Result:
        with self._lock:
            verb = args[0] if args else ""
            if verb in ("reboot", "poweroff"):
                self.power.append(verb)
                return Result(0)
            if verb == "show":
                unit = args[1].removesuffix(".service").removesuffix(".timer")
                state = self.units.get(unit, "active" if unit.startswith("weatherpi") else
                                       "inactive")
                return Result(0, f"ActiveState={state}\nSubState="
                                 f"{'running' if state == 'active' else 'dead'}\n"
                                 "UnitFileState=enabled\nNRestarts=0\n")
            if verb in ("stop", "start", "restart", "try-restart", "try-reload-or-restart",
                        "reload"):
                for unit in args[1:]:
                    name = unit.removesuffix(".service")
                    self.units[name] = "inactive" if verb == "stop" else "active"
                return Result(0)
        return Result(1, "", "fake systemctl: unknown verb")

    def _cmd_journalctl(self, args: list[str], timeout: float, input: str | None) -> Result:
        unit = args[args.index("-u") + 1] if "-u" in args else ""
        lines = self.journal_lines.get(unit, [f"-- No entries for {unit} (fake) --"])
        return Result(0, "\n".join(lines) + "\n")

    def _cmd_timedatectl(self, args: list[str], timeout: float, input: str | None) -> Result:
        with self._lock:
            if args[:1] == ["set-timezone"] and len(args) == 2:
                self.timezone = args[1]
                return Result(0)
            if args[:1] == ["show"]:
                return Result(0, "yes\n" if self.clock_synced else "no\n")
        return Result(1, "", "fake timedatectl: bad usage")

    def _cmd_date(self, args: list[str], timeout: float, input: str | None) -> Result:
        if "-s" in args:
            value = args[args.index("-s") + 1]
            with self._lock:
                self.clock_set_to = int(value.lstrip("@"))
            return Result(0, value + "\n")
        return Result(0, time.strftime("%a %b %d %H:%M:%S UTC %Y\n", time.gmtime()))

    def _cmd_fake_hwclock(self, args: list[str], timeout: float, input: str | None) -> Result:
        return Result(0)

    def _cmd_vcgencmd(self, args: list[str], timeout: float, input: str | None) -> Result:
        if args == ["measure_temp"]:
            return Result(0, "temp=41.2'C\n")
        if args == ["get_throttled"]:
            return Result(0, "throttled=0x0\n")
        return Result(1, "", "bad command")

    def _cmd_pinctrl(self, args: list[str], timeout: float, input: str | None) -> Result:
        pin = args[-1] if args else "17"
        return Result(0, f"{pin}: ip    pu | hi // GPIO{pin} = input\n")

    def _cmd_nft(self, args: list[str], timeout: float, input: str | None) -> Result:
        return Result(0, "table inet weatherpi {\n\t# fake ruleset\n}\n")
