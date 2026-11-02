"""Test doubles: a stateful fake agent (httpx.MockTransport), a fake command runner, and a
backup-archive builder. Nothing here touches the network or runs real programs."""

from __future__ import annotations

import hashlib
import io
import json
import re
import sqlite3
import tarfile
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from weatherpi_companion.agent_client import AgentClient
from weatherpi_companion.host.runner import NOT_FOUND, CommandResult

SERIAL = "00000000abcd1234"
SETTINGS_CHOICES = (60, 300, 600, 900, 1800, 3600)
SERVE_ADMIN_URL = "https://login.tailscale.com/f/serve?node=n1"


# --------------------------------------------------------------------------- backups


def make_db(path: Path, temps: int = 3, rains: int = 2) -> None:
    con = sqlite3.connect(path)
    con.executescript(
        "CREATE TABLE temperature_readings(id INTEGER PRIMARY KEY, timestamp TEXT, temp_c REAL);"
        "CREATE TABLE rain_events(id INTEGER PRIMARY KEY, timestamp TEXT);"
        "CREATE TABLE status(key TEXT PRIMARY KEY, value TEXT);"
    )
    con.executemany("INSERT INTO temperature_readings(timestamp, temp_c) VALUES (?, ?)",
                    [(f"2026-10-19T10:{i:02d}:00Z", 20.0 + i) for i in range(temps)])
    con.executemany("INSERT INTO rain_events(timestamp) VALUES (?)",
                    [(f"2026-10-19T11:{i:02d}:00Z",) for i in range(rains)])
    con.commit()
    con.close()


def make_backup_bytes(
    *,
    temps: int = 3,
    rains: int = 2,
    manifest: dict[str, Any] | None = None,
    manifest_rows: dict[str, Any] | None = None,
    created_at: str = "2026-10-19T11:05:00Z",
    hostname: str = "weatherpi",
    db_bytes: bytes | None = None,
    extra: Sequence[tarfile.TarInfo] = (),
    omit: Sequence[str] = (),
) -> bytes:
    with tempfile.TemporaryDirectory() as td:
        dbp = Path(td) / "weatherpi.db"
        if db_bytes is None:
            make_db(dbp, temps, rains)
            db_bytes = dbp.read_bytes()
    m = manifest if manifest is not None else {
        "format": 1, "created_at": created_at, "hostname": hostname,
        "image_version": "2026.10.19-1", "agent_version": "2.0.0",
        "rows": manifest_rows if manifest_rows is not None else {
            "temperature_readings": temps, "rain_events": rains},
    }
    files = {
        "weatherpi.db": db_bytes,
        "config.yaml": b"station: {name: WeatherPi}\n",
        "manifest.json": json.dumps(m).encode(),
    }
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in files.items():
            if name in omit:
                continue
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            tar.addfile(ti, io.BytesIO(data))
        for ti in extra:
            tar.addfile(ti, io.BytesIO(b"x" * ti.size) if ti.isfile() else None)
    return buf.getvalue()


# --------------------------------------------------------------------------- agent


@dataclass
class FakeAgent:
    serial: str = SERIAL
    hostname: str = "weatherpi"
    agent_version: str = "2.0.0"
    claimed: bool = False
    clock_synced: bool = False
    token_sha: str | None = None
    recovery_password: str | None = None
    owner_password: str | None = None  # the fake remembers it only to assert in tests
    ssh_pubkey: str | None = None
    timezone: str | None = None
    # wifi
    wifi_networks: list[dict] = field(default_factory=lambda: [
        {"ssid": "ExampleWiFi", "signal": 72, "security": "WPA2", "known": False,
         "in_use": False},
        {"ssid": "Neighbour", "signal": 30, "security": "WPA2", "known": False,
         "in_use": False},
    ])
    wifi_passwords: dict[str, str] = field(default_factory=lambda: {"ExampleWiFi": "goodpass1"})
    wifi_state: str = "disconnected"
    wifi_ssid: str | None = None
    wifi_error: str | None = None
    wifi_polls_to_connect: int = 2
    wifi_internet: bool = True
    _wifi_polls: int = 0
    _wifi_target: tuple[str, str] | None = None
    saved: list[str] = field(default_factory=list)
    #: per saved SSID: {"priority": int, "hidden": bool, "password": str | None}
    saved_meta: dict[str, dict] = field(default_factory=dict)
    saved_supported: bool = True  # False: an agent older than 2.1.0 (no /v1/wifi/saved)
    # tailscale
    ts_state: str = "NeedsLogin"
    ts_auth_url: str | None = None
    ts_polls_to_url: int = 1
    _ts_polls: int = 0
    ts_up_called: bool = False
    tailnet: str = "example.ts.net"
    tailnet_name: str = "owner@example.com"
    ts_ip: str = "100.94.2.31"
    serve_blocked: bool = False
    serve_enabled: bool = False
    serve_timeouts: int = 0  # the next N publish requests time out (a slow Pi)
    serve_late: bool = False  # ...but the Pi still finishes them after the app gave up
    serve_error: str | None = None
    # sensors
    temp_present: bool = True
    tips_today: int = 3
    last_tip_at: str | None = "2026-10-19T10:00:00Z"
    logger_alive: bool = True
    # agent 2.2.0: settings (D26) and maintenance mode (D14); False = an older agent (404)
    has_settings: bool = True
    record_interval_s: int = 300
    has_maintenance: bool = True
    maintenance_on: bool = False  # what cmdline.txt says (overlay off on the next boot)
    overlay_active: bool = True  # what the Pi booted with
    # misc
    time_set_to: str | None = None
    restored: bool = False
    backup_bytes: bytes = field(default_factory=make_backup_bytes)
    requests: list[httpx.Request] = field(default_factory=list)
    down: bool = False  # simulate unreachable

    # -- helpers for tests

    def complete_tailscale_login(self, tailnet: str | None = None) -> None:
        self.ts_state = "Running"
        self.ts_auth_url = None
        if tailnet:
            self.tailnet = tailnet

    def tip(self) -> None:
        self.tips_today += 1
        self.last_tip_at = f"2026-10-19T12:{self.tips_today:02d}:00Z"

    def paths(self) -> list[str]:
        return [f"{r.method} {r.url.path}" for r in self.requests]

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def client_factory(self) -> Callable[[str, str | None, str], AgentClient]:
        def make(url: str, token: str | None, via: str) -> AgentClient:
            return AgentClient(url, token, via=via, transport=self.transport())
        return make

    # -- server

    @staticmethod
    def _err(status: int, code: str, message: str = "", **extra: Any) -> httpx.Response:
        return httpx.Response(status, json={"error": code, "message": message, **extra})

    def _via(self, req: httpx.Request) -> str:
        return "usb" if req.url.host.startswith("10.12.194.") else "tailscale"

    def _authed(self, req: httpx.Request) -> bool:
        h = req.headers.get("authorization", "")
        if not h.startswith("Bearer ") or not self.token_sha:
            return False
        return hashlib.sha256(h[7:].encode()).hexdigest() == self.token_sha

    def _serve(self) -> bool:
        """What the Pi does for POST /v1/tailscale/serve; False if Serve is off on the tailnet."""
        if self.serve_blocked:
            self.serve_error = "serve_not_enabled_on_tailnet"
            return False
        self.serve_enabled, self.serve_error = True, None
        return True

    def ts_status(self) -> dict:
        running = self.ts_state == "Running"
        return {
            "installed": True, "version": "1.94.2", "backend_state": self.ts_state,
            "auth_url": self.ts_auth_url,
            "tailnet": self.tailnet if running else None,
            "tailnet_name": self.tailnet_name if running else None,
            "ip": self.ts_ip if running else None,
            "dns_name": f"{self.hostname}.{self.tailnet}" if running else None,
            "serve_enabled": self.serve_enabled,
            "serve_url": f"https://{self.hostname}.{self.tailnet}" if self.serve_enabled
            else None,
            "serve_error": self.serve_error,
            **({"admin_url": SERVE_ADMIN_URL} if self.serve_error else {}),
        }

    @staticmethod
    def _priority_ok(p: Any) -> bool:
        return isinstance(p, int) and not isinstance(p, bool) and -100 <= p <= 100

    def saved_list(self) -> list[dict]:
        """GET /v1/wifi/saved as the agent sorts it (priority desc, then SSID)."""
        nets = [{"ssid": s, "priority": self.saved_meta.get(s, {}).get("priority", 0),
                 "hidden": self.saved_meta.get(s, {}).get("hidden", False),
                 "in_use": self.wifi_state == "connected" and self.wifi_ssid == s}
                for s in self.saved]
        return sorted(nets, key=lambda n: (-n["priority"], n["ssid"].lower(), n["ssid"]))

    def _saved_endpoint(self, method: str, path: str, body: dict) -> httpx.Response:
        if method == "GET":
            return httpx.Response(200, json={"networks": self.saved_list()})
        if method == "POST":
            ssid, pw = body.get("ssid") or "", body.get("password")
            prio = body.get("priority", 0)
            if not ssid or (pw and not 8 <= len(pw) <= 63) or not self._priority_ok(prio):
                return self._err(400, "bad_request", "bad ssid, password or priority")
            if self.wifi_state == "connecting":
                return self._err(409, "busy", "wifi-connect is already in progress")
            if ssid not in self.saved:
                self.saved.append(ssid)
            self.saved_meta[ssid] = {"priority": prio, "hidden": bool(body.get("hidden")),
                                     "password": pw}
            return httpx.Response(201, json={"ssid": ssid})
        from urllib.parse import unquote
        ssid = unquote(path.rsplit("/", 1)[1])
        if not self._priority_ok(body.get("priority")):
            return self._err(400, "bad_request", "priority")
        if self.wifi_state == "connecting":
            return self._err(409, "busy")
        if ssid not in self.saved:
            return self._err(404, "not_found", f"No saved network called {ssid!r}")
        self.saved_meta.setdefault(ssid, {"hidden": False, "password": None})
        self.saved_meta[ssid]["priority"] = body["priority"]
        return httpx.Response(200, json={"ssid": ssid, "priority": body["priority"]})

    def handle(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        if self.down:
            raise httpx.ConnectError("connection refused", request=req)
        path, method = req.url.path, req.method
        body: dict = {}
        if req.headers.get("content-type", "").startswith("application/json") and req.content:
            body = json.loads(req.content)
        via = self._via(req)
        if (method, path) == ("GET", "/v1/info"):
            return httpx.Response(200, json={
                "agent_version": self.agent_version, "image_version": "2026.10.19-1",
                "hostname": self.hostname, "model": "Raspberry Pi Zero W Rev 1.1",
                "serial": self.serial, "claimed": self.claimed, "via": via,
                "time": "2026-10-19T11:05:00Z", "clock_synced": self.clock_synced})
        if (method, path) == ("POST", "/v1/claim"):
            if via != "usb":
                return self._err(403, "usb_only")
            if self.claimed:
                return self._err(409, "already_claimed")
            if len(body.get("password", "")) < 8:
                return self._err(400, "weak_password", "too short")
            if len(body.get("recovery_password", "")) < 20 or not body.get("api_token"):
                return self._err(400, "bad_request")
            self.claimed = True
            self.owner_password = body["password"]
            self.recovery_password = body["recovery_password"]
            self.ssh_pubkey = body["ssh_pubkey"]
            self.timezone = body.get("timezone")
            self.token_sha = hashlib.sha256(body["api_token"].encode()).hexdigest()
            return httpx.Response(200, json={"claimed": True})
        if (method, path) == ("POST", "/v1/recover"):
            if via != "usb":
                return self._err(403, "usb_only")
            if not self.claimed:
                return self._err(409, "not_claimed")
            if body.get("recovery_password") != self.recovery_password:
                return self._err(403, "bad_recovery")
            self.owner_password = body["new_password"]
            if body.get("new_api_token"):
                self.token_sha = hashlib.sha256(body["new_api_token"].encode()).hexdigest()
            if body.get("new_ssh_pubkey"):
                self.ssh_pubkey = body["new_ssh_pubkey"]
            return httpx.Response(200, json={})
        # everything else needs auth
        if not self.claimed:
            return self._err(409, "not_claimed")
        if not self._authed(req):
            return self._err(401, "unauthorized")
        if (method, path) == ("POST", "/v1/password"):
            if len(body.get("new_password", "")) < 8:
                return self._err(400, "weak_password")
            self.owner_password = body["new_password"]
            return httpx.Response(200, json={})
        if (method, path) == ("GET", "/v1/wifi/scan"):
            return httpx.Response(200, json={"networks": self.wifi_networks})
        if (method, path) == ("POST", "/v1/wifi/connect"):
            if "priority" in body and not self._priority_ok(body["priority"]):
                return self._err(400, "bad_request", "priority")
            self._wifi_target = (body["ssid"], body.get("password", ""))
            self.wifi_state, self._wifi_polls, self.wifi_error = "connecting", 0, None
            if "priority" in body:
                self.saved_meta.setdefault(body["ssid"], {"hidden": False, "password": None})
                self.saved_meta[body["ssid"]]["priority"] = body["priority"]
            return httpx.Response(202, json={"job": "wifi-connect"})
        if path == "/v1/wifi/saved" or (method == "PATCH" and path.startswith("/v1/wifi/saved/")):
            if not self.saved_supported:  # what a 2.0.0 agent answers
                return self._err(405 if method == "PATCH" else 404,
                                 "method_not_allowed" if method == "PATCH" else "not_found")
            # raw path: an SSID may contain an encoded "/"
            return self._saved_endpoint(method, req.url.raw_path.decode().split("?")[0], body)
        if (method, path) == ("GET", "/v1/wifi/status"):
            if self.wifi_state == "connecting" and self._wifi_target:
                self._wifi_polls += 1
                if self._wifi_polls >= self.wifi_polls_to_connect:
                    ssid, pw = self._wifi_target
                    known = {n["ssid"] for n in self.wifi_networks}
                    if ssid not in known:
                        self.wifi_state, self.wifi_error = "failed", "not_found"
                    elif self.wifi_passwords.get(ssid) != pw:
                        self.wifi_state, self.wifi_error = "failed", "wrong_password"
                    else:
                        self.wifi_state, self.wifi_ssid = "connected", ssid
                        if ssid not in self.saved:
                            self.saved.append(ssid)
            connected = self.wifi_state == "connected"
            return httpx.Response(200, json={
                "state": self.wifi_state, "ssid": self.wifi_ssid if connected else None,
                "ip": "192.168.7.9" if connected else None, "signal": 70 if connected else None,
                "internet": connected and self.wifi_internet, "last_error": self.wifi_error,
                "saved": self.saved})
        if method == "DELETE" and path.startswith("/v1/wifi/saved/"):
            from urllib.parse import unquote
            ssid = unquote(path.rsplit("/", 1)[1])
            if self.wifi_state == "connecting":
                return self._err(409, "busy")
            if ssid not in self.saved:
                return self._err(404, "not_found")
            self.saved.remove(ssid)
            self.saved_meta.pop(ssid, None)
            if self.wifi_ssid == ssid:
                self.wifi_state, self.wifi_ssid = "disconnected", None
            return httpx.Response(200, json={"deleted": ssid})
        if (method, path) == ("GET", "/v1/tailscale/status"):
            if self.ts_up_called and self.ts_state == "NeedsLogin" and not self.ts_auth_url:
                self._ts_polls += 1
                if self._ts_polls >= self.ts_polls_to_url:
                    self.ts_auth_url = "https://login.tailscale.com/a/abc123def"
            return httpx.Response(200, json=self.ts_status())
        if (method, path) == ("POST", "/v1/tailscale/up"):
            if not (self.wifi_state == "connected" and self.wifi_internet):
                return self._err(409, "no_internet")
            self.ts_up_called = True
            return httpx.Response(202, json={})
        if (method, path) == ("POST", "/v1/tailscale/serve"):
            if self.serve_timeouts > 0:
                self.serve_timeouts -= 1
                if self.serve_late:
                    self._serve()
                raise httpx.ReadTimeout("timed out", request=req)
            if not self._serve():
                return self._err(409, "serve_not_enabled_on_tailnet", "enable serve",
                                 admin_url=SERVE_ADMIN_URL)
            return httpx.Response(200, json={"status": self.ts_status()})
        if (method, path) == ("POST", "/v1/tailscale/logout"):
            self.ts_state, self.ts_auth_url, self.ts_up_called = "NeedsLogin", None, False
            self._ts_polls = 0
            return httpx.Response(200, json={})
        if (method, path) == ("GET", "/v1/sensors/live"):
            return httpx.Response(200, json={
                "temperature": {"present": self.temp_present,
                                "celsius": 21.4 if self.temp_present else None,
                                "sensor_id": "28-000000000001" if self.temp_present else None,
                                "read_at": "2026-10-19T11:04:30Z"},
                "rain": {"tips_today": self.tips_today,
                         "mm_today": round(self.tips_today * 0.2794, 2),
                         "last_tip_at": self.last_tip_at, "mm_per_tip": 0.2794},
                "logger": {"alive": self.logger_alive, "last_heartbeat": "2026-10-19T11:00:00Z",
                           "stale_seconds": 12}})
        if (method, path) == ("GET", "/v1/diagnostics"):
            return httpx.Response(200, json={
                "collected_at": "2026-10-19T11:05:00Z",
                "system": {"uptime_s": 1234, "kernel": "6.12.0", "temp_c": 41.2,
                           "throttled": "0x0", "undervoltage_seen": False},
                "services": [{"name": "weatherpi-logger", "active": "active", "enabled": True,
                              "restarts": 0}],
                "journal": {"weatherpi-logger": "Oct 19 started\nOct 19 tip recorded"},
                "network": {"wifi": {"ssid": "ExampleWiFi", "psk": "should-not-leak"}},
                "hardware": {"w1_devices": [], "gpio17_level": 1},
                "config": {"station": {"name": "WeatherPi"}}})
        if (method, path) == ("GET", "/v1/backup"):
            return httpx.Response(200, content=self.backup_bytes, headers={
                "content-type": "application/gzip",
                "content-disposition": 'attachment; filename="weatherpi-backup-weatherpi-'
                                       '20261019T110500Z.tar.gz"'})
        if (method, path) == ("POST", "/v1/restore"):
            content = req.read()
            if b'name="backup"' not in content:
                return self._err(400, "bad_backup")
            self.restored = True
            return httpx.Response(200, json={"rows": {"temperature_readings": 3,
                                                      "rain_events": 2}})
        if (method, path) == ("POST", "/v1/system/time"):
            self.time_set_to = body.get("utc")
            self.clock_synced = True
            return httpx.Response(200, json={})
        if (method, path) in (("POST", "/v1/system/reboot"), ("POST", "/v1/system/shutdown")):
            return httpx.Response(202, json={})
        if path == "/v1/settings" and self.has_settings:
            if method == "PUT":
                value = body.get("record_interval_s")
                if set(body) != {"record_interval_s"} or value not in SETTINGS_CHOICES:
                    return self._err(400, "bad_request", "record_interval_s")
                self.record_interval_s = value
            return httpx.Response(200, json={"record_interval_s": self.record_interval_s,
                                             "record_interval_choices": list(SETTINGS_CHOICES)})
        if path == "/v1/system/maintenance" and self.has_maintenance:
            if method == "POST":
                if not isinstance(body.get("enabled"), bool):
                    return self._err(400, "bad_request", "enabled")
                if body["enabled"] != self.maintenance_on:
                    self.maintenance_on = body["enabled"]
                    return httpx.Response(202, json={**self._maintenance(), "rebooting": True})
            return httpx.Response(200, json=self._maintenance())
        return self._err(404, "not_found", path)

    def _maintenance(self) -> dict:
        return {"overlay_active": self.overlay_active,
                "overlay_on_next_boot": not self.maintenance_on,
                "maintenance": self.maintenance_on}


# --------------------------------------------------------------------------- runner


@dataclass
class FakeRunner:
    """Answers commands by regex on the joined argv. First match wins."""

    rules: list[tuple[str, CommandResult | Callable[[Sequence[str]], CommandResult]]] = field(
        default_factory=list)
    calls: list[list[str]] = field(default_factory=list)

    def on(self, pattern: str, stdout: str = "", returncode: int = 0,
           stderr: str = "") -> FakeRunner:
        self.rules.append((pattern, CommandResult(returncode, stdout, stderr)))
        return self

    def on_call(self, pattern: str,
                fn: Callable[[Sequence[str]], CommandResult]) -> FakeRunner:
        self.rules.append((pattern, fn))
        return self

    def __call__(self, args: Sequence[str], timeout: float = 0) -> CommandResult:
        argv = list(args)
        self.calls.append(argv)
        joined = " ".join(argv)
        for pattern, result in self.rules:
            if re.search(pattern, joined, re.DOTALL):
                return result(argv) if callable(result) else result
        return CommandResult(NOT_FOUND, "", f"no fake for: {joined[:80]}")

    def ran(self, pattern: str) -> bool:
        return any(re.search(pattern, " ".join(c), re.DOTALL) for c in self.calls)


class FakeClock:
    """Monotonic clock whose ``sleep`` just advances time."""

    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s


# --------------------------------------------------------------------------- tailscale JSON


def host_status_json(state: str = "Running", suffix: str = "example.ts.net",
                     tailnet_name: str = "owner@example.com",
                     peers: list[dict] | None = None, auth_url: str = "") -> str:
    running = state == "Running"
    if peers is None:
        peers = [{"ID": "n2", "HostName": "weatherpi", "DNSName": f"weatherpi.{suffix}.",
                  "TailscaleIPs": ["100.94.2.31", "fd7a:115c:a1e0::1"], "OS": "linux",
                  "Online": True, "LastSeen": "2026-10-19T11:00:00Z"}]
    return json.dumps({
        "Version": "1.94.2-t0a29cf18b-g3f044c9f6",
        "BackendState": state,
        "AuthURL": auth_url,
        "TailscaleIPs": ["100.64.0.5"] if running else None,
        "Self": {"ID": "n1", "HostName": "DESKTOP-TEST", "DNSName":
                 f"desktop-test.{suffix}." if running else "", "OS": "windows",
                 "TailscaleIPs": ["100.64.0.5", "fd7a:115c:a1e0::5"] if running else None,
                 "Online": running},
        "Health": [] if running else ["Tailscale is starting. Please wait."],
        "MagicDNSSuffix": suffix if running else "",
        "CurrentTailnet": {"Name": tailnet_name, "MagicDNSSuffix": suffix,
                           "MagicDNSEnabled": True} if running else None,
        "Peer": {f"nodekey:{p['ID']}": p for p in peers} if running else None,
        "User": None,
    })


#: real output captured from this dev PC's Tailscale 1.94.2 while it was starting
#: (anonymised hostname)
HOST_STATUS_NOSTATE = json.dumps({
    "Version": "1.94.2-t0a29cf18b-g3f044c9f6", "TUN": True, "BackendState": "NoState",
    "HaveNodeKey": True, "AuthURL": "", "TailscaleIPs": None,
    "Self": {"ID": "", "PublicKey": "nodekey:" + "0" * 64, "HostName": "DESKTOP-TEST",
             "DNSName": "", "OS": "windows", "UserID": 0, "TailscaleIPs": None, "Addrs": [],
             "Online": False},
    "Health": ["Tailscale is starting. Please wait."], "MagicDNSSuffix": "",
    "CurrentTailnet": None, "CertDomains": None, "Peer": None, "User": None,
    "ClientVersion": None,
})
