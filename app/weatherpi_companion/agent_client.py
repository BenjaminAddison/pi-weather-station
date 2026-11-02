"""Typed client for the WeatherPi agent API (docs/pi-agent-api.md, v1).

Synchronous and Qt-free; the UI calls it from a worker thread. Every failure is raised
as :class:`AgentError` (or its subclass :class:`AgentUnreachable` for network problems),
so callers only have one exception type to handle.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self
from urllib.parse import quote

import httpx

USB_HOST = "10.12.194.1"
AGENT_PORT = 8765
USB_BASE_URL = f"http://{USB_HOST}:{AGENT_PORT}"
#: major agent_version this app was built against (see "Versioning" in the API doc)
SUPPORTED_AGENT_MAJOR = 2

DEFAULT_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
SCAN_TIMEOUT = httpx.Timeout(40.0, connect=5.0)
#: /v1/tailscale/serve: the agent runs `tailscale status` (15 s) then `tailscale serve`, which
#: blocks for up to 20 s when Serve is off on the tailnet; the Pi Zero's CLI is slow too
SERVE_TIMEOUT = httpx.Timeout(60.0, connect=5.0)
TRANSFER_TIMEOUT = httpx.Timeout(300.0, connect=10.0)

ProgressCallback = Callable[[int, int | None], None]


# --------------------------------------------------------------------------- errors


class AgentError(Exception):
    """A non-2xx answer from the agent, or a failure to talk to it.

    ``code`` is the API error code (``unauthorized``, ``usb_only``, ``weak_password`` ...),
    ``status`` the HTTP status (0 when no answer arrived), ``body`` the decoded JSON body.
    """

    def __init__(
        self, code: str, message: str = "", status: int = 0, body: dict[str, Any] | None = None
    ) -> None:
        super().__init__(f"{code} ({status}): {message}" if message else f"{code} ({status})")
        self.code = code
        self.message = message
        self.status = status
        self.body = body or {}

    @property
    def admin_url(self) -> str | None:
        """Set for ``serve_not_enabled_on_tailnet``."""
        value = self.body.get("admin_url")
        return value if isinstance(value, str) else None


class AgentUnreachable(AgentError):
    """No HTTP answer at all (connection refused, timeout, DNS failure ...)."""


def _error_from_response(resp: httpx.Response) -> AgentError:
    body: dict[str, Any] = {}
    try:
        data = resp.json()
        if isinstance(data, dict):
            body = data
    except ValueError:
        pass
    code = str(body.get("error") or _generic_code(resp.status_code))
    message = str(body.get("message") or resp.reason_phrase or "")
    return AgentError(code, message, resp.status_code, body)


def _generic_code(status: int) -> str:
    return {
        400: "bad_request",
        401: "unauthorized",
        403: "forbidden",
        404: "not_found",
        409: "conflict",
        500: "internal",
    }.get(status, f"http_{status}")


# --------------------------------------------------------------------------- models


def _s(d: dict[str, Any], key: str, default: str | None = None) -> str | None:
    v = d.get(key, default)
    return None if v is None else str(v)


def _i(d: dict[str, Any], key: str, default: int | None = None) -> int | None:
    v = d.get(key, default)
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _f(d: dict[str, Any], key: str) -> float | None:
    v = d.get(key)
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _d(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else {}


def parse_time(value: str | None) -> datetime | None:
    """Parse an ISO 8601 time from the agent (``Z`` suffix allowed)."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def format_time(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class AgentInfo:
    agent_version: str
    image_version: str
    hostname: str
    model: str
    serial: str
    claimed: bool
    via: str
    time: str | None
    clock_synced: bool
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> AgentInfo:
        return cls(
            agent_version=_s(d, "agent_version", "") or "",
            image_version=_s(d, "image_version", "") or "",
            hostname=_s(d, "hostname", "") or "",
            model=_s(d, "model", "") or "",
            serial=_s(d, "serial", "") or "",
            claimed=bool(d.get("claimed", False)),
            via=_s(d, "via", "") or "",
            time=_s(d, "time"),
            clock_synced=bool(d.get("clock_synced", False)),
            raw=d,
        )

    @property
    def agent_major(self) -> int | None:
        m = re.match(r"\s*(\d+)", self.agent_version)
        return int(m.group(1)) if m else None

    @property
    def compatible(self) -> bool:
        return self.agent_major == SUPPORTED_AGENT_MAJOR


@dataclass
class WifiNetwork:
    ssid: str
    signal: int
    security: str
    known: bool
    in_use: bool

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> WifiNetwork:
        return cls(
            ssid=_s(d, "ssid", "") or "",
            signal=_i(d, "signal", 0) or 0,
            security=_s(d, "security", "") or "",
            known=bool(d.get("known", False)),
            in_use=bool(d.get("in_use", False)),
        )

    @property
    def secured(self) -> bool:
        return self.security.strip().lower() not in ("", "none", "open", "--")

    @property
    def bars(self) -> int:
        """0-4 signal bars for the UI."""
        return max(0, min(4, (self.signal + 12) // 25))


@dataclass(frozen=True)
class SavedNetwork:
    """One entry of ``GET /v1/wifi/saved`` (agent 2.1.0). Higher ``priority`` is joined first."""

    ssid: str
    priority: int
    hidden: bool
    in_use: bool

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> SavedNetwork:
        return cls(
            ssid=_s(d, "ssid", "") or "",
            priority=_i(d, "priority", 0) or 0,
            hidden=bool(d.get("hidden", False)),
            in_use=bool(d.get("in_use", False)),
        )


@dataclass
class WifiStatus:
    state: str
    ssid: str | None
    ip: str | None
    signal: int | None
    internet: bool
    last_error: str | None
    saved: list[str]
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> WifiStatus:
        saved = d.get("saved") or []
        return cls(
            state=_s(d, "state", "disconnected") or "disconnected",
            ssid=_s(d, "ssid"),
            ip=_s(d, "ip"),
            signal=_i(d, "signal"),
            internet=bool(d.get("internet", False)),
            last_error=_s(d, "last_error"),
            saved=[str(x) for x in saved] if isinstance(saved, list) else [],
            raw=d,
        )


@dataclass
class TailscaleStatus:
    installed: bool
    version: str | None
    backend_state: str
    auth_url: str | None
    tailnet: str | None
    tailnet_name: str | None
    ip: str | None
    dns_name: str | None
    serve_enabled: bool
    serve_url: str | None
    serve_error: str | None
    admin_url: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> TailscaleStatus:
        dns = _s(d, "dns_name")
        return cls(
            installed=bool(d.get("installed", True)),
            version=_s(d, "version"),
            backend_state=_s(d, "backend_state", "NoState") or "NoState",
            auth_url=_s(d, "auth_url") or None,
            tailnet=(_s(d, "tailnet") or "").rstrip(".") or None,
            tailnet_name=_s(d, "tailnet_name"),
            ip=_s(d, "ip"),
            dns_name=dns.rstrip(".") if dns else None,
            serve_enabled=bool(d.get("serve_enabled", False)),
            serve_url=_s(d, "serve_url"),
            serve_error=_s(d, "serve_error"),
            admin_url=_s(d, "admin_url"),
            raw=d,
        )

    @property
    def running(self) -> bool:
        return self.backend_state == "Running"


@dataclass
class TemperatureLive:
    present: bool
    celsius: float | None
    sensor_id: str | None
    read_at: str | None


@dataclass
class RainLive:
    tips_today: int
    mm_today: float
    last_tip_at: str | None
    mm_per_tip: float


@dataclass
class LoggerLive:
    alive: bool
    last_heartbeat: str | None
    stale_seconds: int | None


@dataclass
class SensorsLive:
    temperature: TemperatureLive
    rain: RainLive
    logger: LoggerLive
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> SensorsLive:
        t, r, lg = _d(d.get("temperature")), _d(d.get("rain")), _d(d.get("logger"))
        return cls(
            temperature=TemperatureLive(
                present=bool(t.get("present", False)),
                celsius=_f(t, "celsius"),
                sensor_id=_s(t, "sensor_id"),
                read_at=_s(t, "read_at"),
            ),
            rain=RainLive(
                tips_today=_i(r, "tips_today", 0) or 0,
                mm_today=_f(r, "mm_today") or 0.0,
                last_tip_at=_s(r, "last_tip_at"),
                mm_per_tip=_f(r, "mm_per_tip") or 0.2794,
            ),
            logger=LoggerLive(
                alive=bool(lg.get("alive", False)),
                last_heartbeat=_s(lg, "last_heartbeat"),
                stale_seconds=_i(lg, "stale_seconds"),
            ),
            raw=d,
        )


@dataclass
class StationSettings:
    """``GET``/``PUT /v1/settings`` (agent 2.2.0, D26)."""

    record_interval_s: int
    record_interval_choices: list[int]

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> StationSettings:
        choices = [c for c in d.get("record_interval_choices") or [] if isinstance(c, int)]
        return cls(record_interval_s=_i(d, "record_interval_s", 300) or 300,
                   record_interval_choices=choices)


@dataclass
class MaintenanceState:
    """``GET /v1/system/maintenance`` (agent 2.2.0, D14)."""

    overlay_active: bool
    overlay_on_next_boot: bool
    maintenance: bool
    rebooting: bool = False

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> MaintenanceState:
        on_next = bool(d.get("overlay_on_next_boot", True))
        return cls(overlay_active=bool(d.get("overlay_active", False)),
                   overlay_on_next_boot=on_next,
                   maintenance=bool(d.get("maintenance", not on_next)),
                   rebooting=bool(d.get("rebooting", False)))


@dataclass
class RestoreResult:
    rows: dict[str, int]


@dataclass
class DownloadedBackup:
    path: Path
    size: int
    server_filename: str | None


# --------------------------------------------------------------------------- client


_FILENAME_RE = re.compile(r"""filename\*?=(?:UTF-8'')?["']?([^"';]+)""", re.IGNORECASE)


def _filename_from_disposition(value: str | None) -> str | None:
    if not value:
        return None
    m = _FILENAME_RE.search(value)
    if not m:
        return None
    name = os.path.basename(m.group(1).strip().replace("\\", "/"))
    return name or None


class AgentClient:
    """Client for one agent base URL.

    ``transport`` lets tests inject an ``httpx.MockTransport``.
    """

    def __init__(
        self,
        base_url: str,
        token: str | None = None,
        *,
        timeout: httpx.Timeout | float = DEFAULT_TIMEOUT,
        transport: httpx.BaseTransport | None = None,
        via: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        #: "usb" or "tailscale" when built by a factory helper (informational)
        self.via = via
        self._http = httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            transport=transport,
            headers={"User-Agent": "weatherpi-companion"},
            trust_env=False,  # never send agent traffic through a system proxy
        )

    # -- lifecycle

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def with_token(self, token: str | None) -> AgentClient:
        """A new client for the same agent with a different token (shares the transport)."""
        clone = AgentClient.__new__(AgentClient)
        clone.base_url = self.base_url
        clone.token = token
        clone.via = self.via
        clone._http = self._http
        return clone

    # -- plumbing

    def _headers(self, auth: bool) -> dict[str, str]:
        if auth and self.token:
            return {"Authorization": f"Bearer {self.token}"}
        return {}

    def _request(
        self,
        method: str,
        path: str,
        *,
        auth: bool = True,
        json: Any = None,
        timeout: httpx.Timeout | float | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        extra: dict[str, Any] = {}
        if timeout is not None:
            extra["timeout"] = timeout
        try:
            resp = self._http.request(
                method, path, headers=self._headers(auth), json=json, **extra, **kwargs
            )
        except httpx.TimeoutException as exc:
            raise AgentUnreachable("timeout", f"The WeatherPi did not answer in time ({exc})")
        except httpx.TransportError as exc:
            raise AgentUnreachable("unreachable", f"Could not reach the WeatherPi ({exc})")
        if resp.status_code >= 400:
            raise _error_from_response(resp)
        return resp

    def _json(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        resp = self._request(method, path, **kwargs)
        if not resp.content:
            return {}
        try:
            data = resp.json()
        except ValueError:
            raise AgentError("bad_response", "The WeatherPi sent a reply that isn't JSON",
                             resp.status_code)
        return data if isinstance(data, dict) else {"value": data}

    # -- info and claim

    def info(self) -> AgentInfo:
        return AgentInfo.from_json(self._json("GET", "/v1/info", auth=False))

    def claim(
        self,
        password: str,
        recovery_password: str,
        ssh_pubkey: str,
        api_token: str,
        owner_name: str | None = None,
        timezone: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "password": password,
            "recovery_password": recovery_password,
            "ssh_pubkey": ssh_pubkey,
            "api_token": api_token,
        }
        if owner_name:
            body["owner_name"] = owner_name
        if timezone:
            body["timezone"] = timezone
        return self._json("POST", "/v1/claim", auth=False, json=body)

    def recover(
        self,
        recovery_password: str,
        new_password: str,
        new_ssh_pubkey: str | None = None,
        new_api_token: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"recovery_password": recovery_password,
                                "new_password": new_password}
        if new_ssh_pubkey:
            body["new_ssh_pubkey"] = new_ssh_pubkey
        if new_api_token:
            body["new_api_token"] = new_api_token
        return self._json("POST", "/v1/recover", auth=False, json=body)

    def change_password(self, new_password: str) -> dict[str, Any]:
        return self._json("POST", "/v1/password", json={"new_password": new_password})

    # -- wifi

    def wifi_scan(self) -> list[WifiNetwork]:
        data = self._json("GET", "/v1/wifi/scan", timeout=SCAN_TIMEOUT)
        nets = data.get("networks") or []
        return [WifiNetwork.from_json(n) for n in nets if isinstance(n, dict)]

    def wifi_connect(self, ssid: str, password: str | None, hidden: bool = False,
                     priority: int | None = None) -> dict[str, Any]:
        """Start joining a network. ``priority`` None: an existing profile keeps its
        priority and a new one gets 0 (``priority`` needs agent 2.1.0)."""
        body: dict[str, Any] = {"ssid": ssid, "password": password or "", "hidden": hidden}
        if priority is not None:
            body["priority"] = priority
        return self._json("POST", "/v1/wifi/connect", json=body)

    def wifi_status(self) -> WifiStatus:
        return WifiStatus.from_json(self._json("GET", "/v1/wifi/status"))

    def wifi_saved(self) -> list[SavedNetwork]:
        """Saved networks, highest priority first (agent 2.1.0)."""
        data = self._json("GET", "/v1/wifi/saved")
        nets = data.get("networks") or []
        return [SavedNetwork.from_json(n) for n in nets if isinstance(n, dict)]

    def wifi_save(self, ssid: str, password: str | None, hidden: bool = False,
                  priority: int = 0) -> dict[str, Any]:
        """Save a network for later without connecting (agent 2.1.0). ``password`` None or
        empty means an open network. Replaces a saved network with the same SSID.
        Errors: ``bad_request`` (bad password or priority), ``busy`` (a connect is running).
        """
        body: dict[str, Any] = {"ssid": ssid, "password": password or None, "hidden": hidden,
                                "priority": priority}
        return self._json("POST", "/v1/wifi/saved", json=body)

    def wifi_set_priority(self, ssid: str, priority: int) -> dict[str, Any]:
        """Change only a saved network's priority (agent 2.1.0). 404 ``not_found``."""
        return self._json("PATCH", f"/v1/wifi/saved/{quote(ssid, safe='')}",
                          json={"priority": priority})

    def wifi_forget(self, ssid: str) -> dict[str, Any]:
        return self._json("DELETE", f"/v1/wifi/saved/{quote(ssid, safe='')}")

    # -- tailscale

    def tailscale_status(self) -> TailscaleStatus:
        return TailscaleStatus.from_json(self._json("GET", "/v1/tailscale/status"))

    def tailscale_up(self, hostname: str = "weatherpi") -> dict[str, Any]:
        return self._json("POST", "/v1/tailscale/up", json={"hostname": hostname})

    def tailscale_serve(self) -> TailscaleStatus:
        """Raises AgentError(code='serve_not_enabled_on_tailnet') with ``admin_url``."""
        data = self._json("POST", "/v1/tailscale/serve", timeout=SERVE_TIMEOUT)
        status = data.get("status") if isinstance(data.get("status"), dict) else data
        return TailscaleStatus.from_json(status)

    def tailscale_logout(self) -> dict[str, Any]:
        return self._json("POST", "/v1/tailscale/logout")

    # -- station data

    def sensors_live(self) -> SensorsLive:
        return SensorsLive.from_json(self._json("GET", "/v1/sensors/live"))

    def diagnostics(self) -> dict[str, Any]:
        return self._json("GET", "/v1/diagnostics", timeout=httpx.Timeout(60.0, connect=5.0))

    # -- backups

    @contextmanager
    def stream_backup(self) -> Iterator[tuple[httpx.Response, Iterator[bytes]]]:
        """Low-level: yields (response, chunk iterator). Prefer :meth:`download_backup`."""
        try:
            with self._http.stream(
                "GET", "/v1/backup", headers=self._headers(True), timeout=TRANSFER_TIMEOUT
            ) as resp:
                if resp.status_code >= 400:
                    resp.read()
                    raise _error_from_response(resp)
                yield resp, resp.iter_bytes(64 * 1024)
        except httpx.TimeoutException as exc:
            raise AgentUnreachable("timeout", f"The backup download stalled ({exc})")
        except httpx.TransportError as exc:
            raise AgentUnreachable("unreachable", f"The backup download failed ({exc})")

    def download_backup(
        self,
        folder: Path,
        filename: str | None = None,
        progress: ProgressCallback | None = None,
    ) -> DownloadedBackup:
        """Stream ``GET /v1/backup`` into ``folder``. Writes ``<name>.part`` then renames.

        The file name comes from ``filename``, else the server's Content-Disposition, else
        ``weatherpi-backup-<stamp>.tar.gz``.
        """
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        with self.stream_backup() as (resp, chunks):
            server_name = _filename_from_disposition(resp.headers.get("content-disposition"))
            name = filename or server_name or (
                f"weatherpi-backup-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.tar.gz"
            )
            total = resp.headers.get("content-length")
            total_n = int(total) if total and total.isdigit() else None
            target = folder / name
            part = target.with_name(target.name + ".part")
            size = 0
            try:
                with open(part, "wb") as fh:
                    for chunk in chunks:
                        fh.write(chunk)
                        size += len(chunk)
                        if progress:
                            progress(size, total_n)
            except BaseException:
                part.unlink(missing_ok=True)
                raise
        if total_n is not None and size != total_n:
            part.unlink(missing_ok=True)
            raise AgentError("incomplete_download",
                             f"Got {size} of {total_n} bytes", resp.status_code)
        os.replace(part, target)
        return DownloadedBackup(target, size, server_name)

    def restore(self, backup_path: Path) -> RestoreResult:
        path = Path(backup_path)
        with open(path, "rb") as fh:
            data = self._json(
                "POST",
                "/v1/restore",
                files={"backup": (path.name, fh, "application/gzip")},
                timeout=TRANSFER_TIMEOUT,
            )
        rows = data.get("rows") or {}
        return RestoreResult({str(k): int(v) for k, v in _d(rows).items()})

    # -- system

    def reboot(self) -> dict[str, Any]:
        return self._json("POST", "/v1/system/reboot")

    def shutdown(self) -> dict[str, Any]:
        return self._json("POST", "/v1/system/shutdown")

    def set_time(self, utc: datetime | None = None) -> dict[str, Any]:
        return self._json("POST", "/v1/system/time",
                          json={"utc": format_time(utc or datetime.now(UTC))})

    def maintenance(self) -> MaintenanceState:
        """Raises AgentError(status=404) on a Pi without the read-only root (before agent 2.2.0)."""
        return MaintenanceState.from_json(self._json("GET", "/v1/system/maintenance"))

    def set_maintenance(self, enabled: bool) -> MaintenanceState:
        """``rebooting`` is True when the Pi restarts to apply it (about a minute)."""
        return MaintenanceState.from_json(
            self._json("POST", "/v1/system/maintenance", json={"enabled": enabled}))

    # -- settings (D26)

    def settings(self) -> StationSettings:
        """Raises AgentError(status=404) on agents before 2.2.0."""
        return StationSettings.from_json(self._json("GET", "/v1/settings"))

    def set_record_interval(self, seconds: int) -> StationSettings:
        return StationSettings.from_json(
            self._json("PUT", "/v1/settings", json={"record_interval_s": seconds}))


# --------------------------------------------------------------------------- factories


def for_usb(token: str | None = None, **kwargs: Any) -> AgentClient:
    """Client for the agent over the USB cable (``http://10.12.194.1:8765``)."""
    return AgentClient(USB_BASE_URL, token, via="usb", **kwargs)


def for_host(host: str, token: str | None = None, *, via: str | None = None,
             **kwargs: Any) -> AgentClient:
    """Client for the agent at an arbitrary IP / hostname (e.g. the ICS fallback address)."""
    host = host.strip().rstrip(".")
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"  # IPv6 literal
    return AgentClient(f"http://{host}:{AGENT_PORT}", token, via=via, **kwargs)


def for_tailscale(host: str, token: str | None = None, **kwargs: Any) -> AgentClient:
    """Client over the tailnet; ``host`` is the MagicDNS name or the 100.x address."""
    return for_host(host, token, via="tailscale", **kwargs)
