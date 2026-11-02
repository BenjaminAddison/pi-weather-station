"""Home-screen helpers: connect to a set-up station (Tailscale first, USB fallback; D23),
fetch its status, explain why it's offline, and build the AI help pack.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import aihelp, backups
from .agent_client import (
    USB_BASE_URL,
    AgentClient,
    AgentError,
    AgentInfo,
    MaintenanceState,
    SensorsLive,
    StationSettings,
    WifiStatus,
    parse_time,
)
from .checks import Action, CheckResult
from .flows import HostEnv, SavedNetworks
from .host import tailscale as host_ts
from .host import usb as host_usb
from .secrets import StationProfile, StationSecrets, StationStore

PROBE_TIMEOUT = 4.0


@dataclass
class Connection:
    client: AgentClient
    via: str  # "tailscale" | "usb"
    info: AgentInfo


@dataclass
class StationStatus:
    online: bool
    via: str | None = None
    info: AgentInfo | None = None
    sensors: SensorsLive | None = None
    wifi: WifiStatus | None = None
    checked_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    error: str | None = None
    #: filled when offline: what's wrong and what to do
    diagnosis: list[CheckResult] = field(default_factory=list)

    @property
    def last_heartbeat(self) -> datetime | None:
        return parse_time(self.sensors.logger.last_heartbeat) if self.sensors else None


def candidate_urls(profile: StationProfile) -> list[tuple[str, str]]:
    """(base_url, via) in the order to try: MagicDNS name, tailnet IP, then USB."""
    out: list[tuple[str, str]] = []
    for host in (profile.dns_name, profile.tailnet_ip):
        if host:
            url = f"http://{host.rstrip('.')}:8765"
            if (url, "tailscale") not in out:
                out.append((url, "tailscale"))
    out.append((USB_BASE_URL, "usb"))
    return out


def connect(
    profile: StationProfile,
    token: str | None,
    client_factory: Callable[[str, str | None, str], AgentClient],
    *,
    allow_usb: bool = True,
) -> Connection | None:
    """First agent that answers ``/v1/info`` with the right serial."""
    for url, via in candidate_urls(profile):
        if via == "usb" and not allow_usb:
            continue
        client = client_factory(url, token, via)
        try:
            info = client.info()
        except AgentError:
            client.close()
            continue
        if info.serial and profile.serial and info.serial != profile.serial:
            client.close()
            continue
        return Connection(client, info.via or via, info)
    return None


def diagnose_offline(
    profile: StationProfile,
    env: HostEnv,
    now: datetime | None = None,
) -> list[CheckResult]:
    """Work out why the station can't be reached and what the owner should do next."""
    now = now or datetime.now(UTC)
    out: list[CheckResult] = []
    ts = env.tailscale
    assert ts is not None
    try:
        usb = env.detect_usb()
    except Exception:  # noqa: BLE001
        usb = None
    plugged = usb is not None and usb.state not in (host_usb.UsbState.NOT_PLUGGED_IN,)
    if not ts.installed():
        out.append(CheckResult(False, "host_tailscale_missing", "Tailscale isn't installed on "
                               "this computer, so the app can't reach your WeatherPi from "
                               "here.", action=Action.INSTALL_TAILSCALE))
    else:
        try:
            st = ts.status()
        except host_ts.TailscaleError as exc:
            st = None
            out.append(CheckResult(False, "host_tailscale_not_running", "Tailscale isn't "
                                   "running on this computer. Start it from the Start menu.",
                                   str(exc), Action.OPEN_TAILSCALE_APP))
        if st is not None and not st.running:
            out.append(CheckResult(False, "host_tailscale_off", "Tailscale is off or signed out "
                                   "on this computer. Switch it on (Tailscale icon near the "
                                   "clock).", st.backend_state, Action.HOST_TAILSCALE_LOGIN))
        elif st is not None:
            peer = None
            for target in (profile.tailnet_ip, profile.dns_name):
                if target:
                    peer = host_ts.find_peer(st, target)
                    if peer:
                        break
            if peer is None:
                out.append(CheckResult(False, "pi_not_in_tailnet", "Your WeatherPi isn't in "
                                       "your Tailscale network any more. It may have been "
                                       "removed, or this computer is signed in to a different "
                                       "account. Plug in the USB cable to set Tailscale up "
                                       "again.", st.tailnet_name or "", Action.PLUG_IN))
            elif not peer.online:
                ago = _ago(peer.last_seen, now)
                out.append(CheckResult(False, "pi_offline", "Your WeatherPi is offline"
                                       + (f" (last seen {ago})" if ago else "")
                                       + ". Check it has power. If your WiFi changed, plug in "
                                       "the USB cable and use Change WiFi.",
                                       peer.last_seen or "", Action.PLUG_IN,
                                       {"last_seen": peer.last_seen}))
            else:
                out.append(CheckResult(False, "agent_not_answering", "Your WeatherPi is online "
                                       "but its service isn't answering. Try again in a "
                                       "minute; if it keeps happening, restart it (unplug "
                                       "its power, wait 10 s, plug back in).", peer.dns_name,
                                       Action.RETRY))
    if plugged and usb is not None:
        if usb.connected:
            out.append(CheckResult(True, "usb_connected", "The WeatherPi is connected by USB "
                                   "cable, so you can fix its WiFi now."))
        else:
            out.append(CheckResult(False, f"usb_{usb.state.value}", "The WeatherPi seems to be "
                                   "plugged in by USB but isn't answering yet.", usb.detail,
                                   Action.WAIT))
    return out


def _ago(iso: str | None, now: datetime) -> str | None:
    t = parse_time(iso)
    if t is None or t.year < 2000:
        return None
    s = int((now - t).total_seconds())
    if s < 120:
        return "just now"
    if s < 7200:
        return f"{s // 60} minutes ago"
    if s < 172800:
        return f"{s // 3600} hours ago"
    return f"{s // 86400} days ago"


class StationMonitor:
    """Keeps a connection to one station for the home screen. Call ``refresh()`` every
    30 s from a worker thread."""

    def __init__(self, store: StationStore, env: HostEnv | None = None) -> None:
        self.store = store
        self.env = env or HostEnv(app_data=store.app)
        self.conn: Connection | None = None
        self._secrets: StationSecrets | None = None
        #: saved WiFi networks (D28); never removes the one in use (it may carry the link)
        self.wifi = SavedNetworks(self._require_client, allow_forget_in_use=False)

    @property
    def profile(self) -> StationProfile:
        return self.store.load_profile() or StationProfile(serial=self.store.serial)

    @property
    def secrets(self) -> StationSecrets | None:
        if self._secrets is None:
            self._secrets = self.store.load_secrets()
        return self._secrets

    def known_secrets(self) -> list[str]:
        return self.secrets.known_values() if self.secrets else []

    def client(self, allow_usb: bool = True) -> AgentClient | None:
        """Current client, connecting (or reconnecting) if needed."""
        if self.conn is None:
            token = self.secrets.api_token if self.secrets else None
            self.conn = connect(self.profile, token, self.env.client_factory,
                                allow_usb=allow_usb)
        return self.conn.client if self.conn else None

    def drop(self) -> None:
        if self.conn is not None:
            self.conn.client.close()
            self.conn = None

    def refresh(self) -> StationStatus:
        for attempt in range(2):
            client = self.client()
            if client is None:
                break
            try:
                info = client.info()
                sensors = client.sensors_live()
                try:
                    wifi = client.wifi_status()
                except AgentError:
                    wifi = None
                via = info.via or (self.conn.via if self.conn else None)
                self._remember(info)
                return StationStatus(True, via, info, sensors, wifi)
            except AgentError as exc:
                self.drop()
                if exc.status == 401:
                    return StationStatus(False, error="unauthorized", diagnosis=[CheckResult(
                        False, "token_rejected", "The WeatherPi doesn't recognise this "
                        "computer any more. Plug in the USB cable and use your recovery file "
                        "to reconnect.", str(exc), Action.RECOVER)])
                if attempt == 1:
                    break
        return StationStatus(False, error="unreachable",
                             diagnosis=diagnose_offline(self.profile, self.env))

    def _remember(self, info: AgentInfo) -> None:
        prof = self.profile
        if (prof.image_version, prof.agent_version) != (info.image_version, info.agent_version):
            self.store.update_profile(image_version=info.image_version,
                                      agent_version=info.agent_version)

    # -- actions the home screen needs (all raise AgentError on failure)

    def backup_now(self, folder: Path | None = None,
                   progress: Callable[[int, int | None], None] | None = None
                   ) -> backups.BackupCheck:
        client = self.client()
        if client is None:
            raise AgentError("unreachable", "The WeatherPi isn't reachable")
        check = backups.download_and_verify(client, folder or self.env.app_data.backups_dir,
                                            self.profile.hostname, progress)
        if check.ok:
            self.store.update_profile(last_backup_at=check.manifest.get("created_at"))
        return check

    def _require_client(self) -> AgentClient:
        client = self.client()
        if client is None:
            raise AgentError("unreachable", "The WeatherPi isn't reachable")
        return client

    def settings(self) -> StationSettings | None:
        """None when the WeatherPi's software is too old for settings (agent < 2.2.0)."""
        try:
            return self._require_client().settings()
        except AgentError as exc:
            if exc.status == 404:
                return None
            raise

    def set_record_interval(self, seconds: int) -> StationSettings:
        return self._require_client().set_record_interval(seconds)

    def maintenance(self) -> MaintenanceState | None:
        """None when the WeatherPi has no read-only root (images before W8)."""
        try:
            return self._require_client().maintenance()
        except AgentError as exc:
            if exc.status == 404:
                return None
            raise

    @property
    def remote(self) -> bool:
        """True unless the current connection is the USB cable (then WiFi isn't the link)."""
        return self.conn is None or self.conn.via != "usb"

    def set_maintenance(self, enabled: bool) -> MaintenanceState:
        state = self._require_client().set_maintenance(enabled)
        if state.rebooting:
            self.drop()  # the Pi restarts; reconnect afterwards
        return state

    def backup_overdue(self, folder: Path | None = None, days: int = 7) -> bool:
        return backups.backup_overdue(folder or self.env.app_data.backups_dir, days)

    def host_checks(self) -> list[CheckResult]:
        """Host-side facts for the AI pack (driver, USB, host Tailscale)."""
        from .flows import SetupSession

        s = SetupSession(self.env)
        out = [s.driver_check()]
        try:
            det = self.env.detect_usb()
            out.append(CheckResult(det.connected, f"usb_{det.state.value}",
                                   "USB cable: " + det.state.value.replace("_", " "),
                                   det.detail, optional=True))
        except Exception as exc:  # noqa: BLE001
            out.append(CheckResult(False, "usb_check_failed", "USB check failed", str(exc),
                                   optional=True))
        out += s.check_host_tailscale()
        s.close()
        return out

    def ai_help_pack(self, owner_description: str = "",
                     host_checks: list[CheckResult] | None = None) -> str:
        diag: dict[str, Any] | None = None
        err = None
        client = self.client()
        if client is not None:
            try:
                diag = client.diagnostics()
            except AgentError as exc:
                err = exc.message or exc.code
        else:
            err = "not reachable over Tailscale or USB"
        prof = self.profile.to_json()
        prof.pop("ssh_host_key", None)
        return aihelp.build_help_pack(
            owner_description=owner_description,
            diagnostics=diag,
            diagnostics_error=err,
            host_checks=host_checks if host_checks is not None else self.host_checks(),
            station=prof,
            app_log_tail=self.env.app_data.log_tail(),
            known_secrets=self.known_secrets(),
        )
