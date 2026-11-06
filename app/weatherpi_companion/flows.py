"""Setup wizard logic, Qt-free (docs/companion-app.md "Setup wizard").

The UI (W6) creates one :class:`SetupSession` and calls one or more methods per page,
from a worker thread. Methods return lists of :class:`CheckResult` (or a small result
object containing them); ``all_ok(checks)`` says whether Next can be enabled. Nothing here
raises for expected failures: problems come back as failed checks with a plain-language
``message_for_owner`` and, where possible, an ``action`` the UI can offer as a button.

Long waits (``wait_for_*``) take ``timeout``, a ``cancel`` ``threading.Event`` and an
optional ``on_progress(value, elapsed_s)`` callback (called on the worker thread).

Page -> methods:

1 Welcome ........ (none)
2 This computer .. check_this_computer(), install_driver()
3 Plug in ........ detect_plug_in(), wait_for_plug_in(); recover_access() if claimed elsewhere
4 Password ....... password_strength(), validate_password(), create_password(), check_ssh()
5 WiFi ........... scan_wifi(), connect_wifi(), wait_for_wifi(), wifi_checks();
                   saved networks: saved_networks(), save_network(),
                   set_network_priority(), forget_network()
6 Tailscale PC ... check_host_tailscale(), start_host_login(), wait_for_host_tailscale()
7 Tailscale Pi ... start_pi_tailscale(), wait_for_pi_auth_url(), wait_for_pi_tailscale(),
                   enable_dashboard_serve(), pi_tailscale_logout()
8 Final checks ... final_checks(), wait_for_rain_tip()
9 Done ........... finish(), first_backup()
"""

from __future__ import annotations

import contextlib
import io
import os
import re
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from . import backups
from .agent_client import (
    USB_HOST,
    AgentClient,
    AgentError,
    AgentInfo,
    AgentUnreachable,
    SavedNetwork,
    SensorsLive,
    TailscaleStatus,
    WifiNetwork,
    WifiStatus,
    format_time,
)
from .checks import Action, CheckResult, PollResult, all_ok, first_problem, poll
from .host import driver as host_driver
from .host import tailscale as host_ts
from .host import usb as host_usb
from .host.runner import Runner, run_command
from .secrets import AppData, StationProfile, StationSecrets, StationStore

__all__ = [
    "MIN_PASSWORD_LENGTH",
    "Action",
    "CheckResult",
    "HostEnv",
    "PasswordStrength",
    "PlugInResult",
    "SavedNetwork",
    "SetupSession",
    "SetupSummary",
    "WifiScanResult",
    "all_ok",
    "check_dashboard",
    "first_problem",
    "guess_timezone",
    "password_strength",
    "validate_password",
    "verify_ssh_login",
]

MIN_PASSWORD_LENGTH = 8
SSH_USER = "main"
WIFI_TIMEOUT_S = 60
WIFI_PRIORITY_MIN, WIFI_PRIORITY_MAX = -100, 100  # range of SavedNetwork.priority

ClientFactory = Callable[[str, str | None, str], AgentClient]  # (base_url, token, via)
HttpGet = Callable[[str, float], int]  # (url, timeout) -> HTTP status; raises on failure
SshVerifier = Callable[..., CheckResult]
Progress = Callable[[Any, float], None]


# --------------------------------------------------------------------------- environment


def _default_client_factory(base_url: str, token: str | None, via: str) -> AgentClient:
    return AgentClient(base_url, token, via=via)


def _default_http_get(url: str, timeout: float) -> int:
    with httpx.Client(timeout=timeout, follow_redirects=True, trust_env=False) as c:
        return c.get(url).status_code


@dataclass
class HostEnv:
    """Everything the flows touch outside Python. Tests replace these with fakes."""

    app_data: AppData = field(default_factory=AppData)
    runner: Runner = run_command
    platform: str = sys.platform
    client_factory: ClientFactory = _default_client_factory
    http_get: HttpGet = _default_http_get
    ssh_verifier: SshVerifier | None = None  # default: verify_ssh_login (paramiko)
    usb_detect: Callable[[], host_usb.UsbDetection] | None = None
    driver_status: Callable[[], host_driver.DriverStatus] | None = None
    tailscale: host_ts.HostTailscale | None = None
    #: path of W7's bundled rpi-usb-gadget-driver-setup.exe
    driver_exe: Path | None = None
    driver_installer: Callable[[Path], host_driver.InstallResult] | None = None
    #: start a host login; default host_ts.start_login (really runs `tailscale up`)
    host_login: Callable[[host_ts.HostTailscale], host_ts.LoginStart] | None = None
    sleep: Callable[[float], None] | None = None  # None: cancel-aware wait
    path_exists: Callable[[str], bool] = os.path.isfile
    clock: Callable[[], float] = time.monotonic
    now: Callable[[], datetime] = lambda: datetime.now(UTC)

    def __post_init__(self) -> None:
        if self.tailscale is None:
            self.tailscale = host_ts.HostTailscale(self.runner)

    def detect_usb(self) -> host_usb.UsbDetection:
        if self.usb_detect is not None:
            return self.usb_detect()
        return host_usb.detect(self.runner, platform=self.platform)

    def get_driver_status(self) -> host_driver.DriverStatus:
        if self.driver_status is not None:
            return self.driver_status()
        return host_driver.driver_status(self.runner, self.platform)

    def tailscale_gui(self) -> str | None:
        """The Windows Tailscale tray app, if it's installed."""
        return host_ts.gui_path(exists=self.path_exists, platform=self.platform)

    def verify_ssh(self, host: str, private_key: bytes, **kw: Any) -> CheckResult:
        fn = self.ssh_verifier or verify_ssh_login
        return fn(host, private_key, **kw)


# --------------------------------------------------------------------------- results


@dataclass
class PlugInResult:
    checks: list[CheckResult]
    #: "wait" (keep polling) | "claim" (page 4) | "resume" (already ours: go to
    #: ``resume_page``) | "recover" (claimed by someone/another PC: offer recovery)
    next_step: str
    info: AgentInfo | None = None
    resume_page: int | None = None
    usb: host_usb.UsbDetection | None = None


@dataclass
class WifiScanResult:
    checks: list[CheckResult]
    networks: list[WifiNetwork] = field(default_factory=list)


@dataclass
class SetupSummary:
    dashboard_url: str | None
    recovery_file: Path | None
    backups_dir: Path
    profile: StationProfile | None


@dataclass
class PasswordStrength:
    score: int  # 0..4
    label: str  # "Too short" | "Weak" | "OK" | "Good" | "Strong"


# --------------------------------------------------------------------------- helpers


def _ok(code: str, msg: str, detail: str = "", **data: Any) -> CheckResult:
    return CheckResult(True, code, msg, detail, data=data)


def _fail(code: str, msg: str, detail: str = "", action: str | None = None,
          optional: bool = False, **data: Any) -> CheckResult:
    return CheckResult(False, code, msg, detail, action, data, optional)


def _unreachable(code: str, err: Exception, over: str = "cable") -> CheckResult:
    if over == "cable" and getattr(err, "code", None) == "timeout":
        # connected, but the Pi took too long (it's slow, and may still finish the job)
        msg = ("The WeatherPi is taking longer than usual to answer. It may still be busy; "
               "wait a few seconds, then try again.")
        action = Action.RETRY
    elif over == "cable":
        msg = ("We lost contact with the WeatherPi over the USB cable. Check the cable is "
               "still in the port marked USB, then try again.")
        action = Action.PLUG_IN
    else:
        msg = ("We couldn't reach the WeatherPi over Tailscale. Give it a minute and try "
               "again.")
        action = Action.RETRY
    return _fail(code, msg, str(err), action)


def password_strength(password: str) -> PasswordStrength:
    """A simple meter for the UI (not a security boundary; the Pi enforces >= 8 chars)."""
    if len(password) < MIN_PASSWORD_LENGTH:
        return PasswordStrength(0, "Too short")
    classes = sum(bool(re.search(p, password)) for p in (r"[a-z]", r"[A-Z]", r"\d", r"[^\w]"))
    score = 1
    if len(password) >= 10:
        score += 1
    if len(password) >= 14:
        score += 1
    if classes >= 3:
        score += 1
    if len(set(password)) <= 3 or password.lower() in _COMMON:
        score = 1
    return PasswordStrength(min(score, 4), ["Too short", "Weak", "OK", "Good", "Strong"][
        min(score, 4)])


_COMMON = {"password", "password1", "12345678", "123456789", "qwertyuiop", "weatherpi",
           "iloveyou", "11111111", "abcdefgh", "letmein1", "raspberry"}


def validate_password(password: str, confirm: str) -> list[CheckResult]:
    checks = []
    if len(password) < MIN_PASSWORD_LENGTH:
        checks.append(_fail("password_too_short",
                            f"Your password needs at least {MIN_PASSWORD_LENGTH} characters.",
                            action=Action.FIX_PASSWORD))
    elif password.lower() in _COMMON:
        checks.append(_fail("password_too_common",
                            "That password is too easy to guess. Please choose another one.",
                            action=Action.FIX_PASSWORD))
    else:
        checks.append(_ok("password_length", "Password is long enough."))
    if password != confirm:
        checks.append(_fail("password_mismatch", "The two passwords don't match.",
                            action=Action.FIX_PASSWORD))
    elif password.strip() != password:
        checks.append(_fail("password_spaces",
                            "Your password starts or ends with a space. Please remove it.",
                            action=Action.FIX_PASSWORD))
    return checks


# Windows time zone ids -> IANA (common ones; the Pi defaults to UTC otherwise)
_WINDOWS_TZ = {
    "South Africa Standard Time": "Africa/Johannesburg", "UTC": "UTC",
    "GMT Standard Time": "Europe/London", "W. Europe Standard Time": "Europe/Berlin",
    "Romance Standard Time": "Europe/Paris", "Central Europe Standard Time": "Europe/Budapest",
    "Central European Standard Time": "Europe/Warsaw", "E. Europe Standard Time":
    "Europe/Chisinau", "FLE Standard Time": "Europe/Kiev", "GTB Standard Time":
    "Europe/Bucharest", "Russian Standard Time": "Europe/Moscow", "Egypt Standard Time":
    "Africa/Cairo", "E. Africa Standard Time": "Africa/Nairobi", "W. Central Africa Standard "
    "Time": "Africa/Lagos", "Morocco Standard Time": "Africa/Casablanca", "Namibia Standard "
    "Time": "Africa/Windhoek", "Mauritius Standard Time": "Indian/Mauritius",
    "Arabian Standard Time": "Asia/Dubai", "India Standard Time": "Asia/Kolkata",
    "China Standard Time": "Asia/Shanghai", "Tokyo Standard Time": "Asia/Tokyo",
    "Singapore Standard Time": "Asia/Singapore", "AUS Eastern Standard Time":
    "Australia/Sydney", "W. Australia Standard Time": "Australia/Perth",
    "New Zealand Standard Time": "Pacific/Auckland", "Eastern Standard Time":
    "America/New_York", "Central Standard Time": "America/Chicago", "Mountain Standard Time":
    "America/Denver", "Pacific Standard Time": "America/Los_Angeles",
    "E. South America Standard Time": "America/Sao_Paulo", "Israel Standard Time":
    "Asia/Jerusalem", "Turkey Standard Time": "Europe/Istanbul",
}


def guess_timezone(runner: Runner = run_command, platform: str = sys.platform) -> str | None:
    """Best guess at this PC's IANA time zone, or None (the Pi then uses UTC)."""
    tz = os.environ.get("TZ")
    if tz and "/" in tz:
        return tz.lstrip(":")
    if platform == "win32":
        res = runner(["tzutil.exe", "/g"], 5)
        return _WINDOWS_TZ.get(res.stdout.strip()) if res.ok else None
    try:
        link = os.readlink("/etc/localtime")
        m = re.search(r"zoneinfo/(.+)$", link)
        if m:
            return m.group(1)
    except OSError:
        pass
    try:
        return Path("/etc/timezone").read_text().strip() or None
    except OSError:
        return None


def verify_ssh_login(
    host: str,
    private_key: bytes,
    *,
    username: str = SSH_USER,
    port: int = 22,
    timeout: float = 10.0,
    expected_host_key: str | None = None,
    client_factory: Callable[[], Any] | None = None,
) -> CheckResult:
    """Log in with the app's key and run ``true``. ``data["host_key"]`` is the server key
    seen ("ssh-ed25519 AAAA..."), to store in the profile (trust on first use).

    ``client_factory`` returns a paramiko-``SSHClient``-like object (tests inject a fake).
    """
    try:
        import paramiko
    except ImportError as exc:  # pragma: no cover
        return _fail("ssh_unavailable", "SSH support is missing from this app.", str(exc),
                     optional=True)
    try:
        pkey = paramiko.Ed25519Key.from_private_key(io.StringIO(private_key.decode("ascii")))
    except (paramiko.SSHException, ValueError, UnicodeDecodeError) as exc:
        return _fail("ssh_key_unreadable", "The app's SSH key couldn't be read.", str(exc))
    client = client_factory() if client_factory else paramiko.SSHClient()
    seen: dict[str, str] = {}

    class _Remember(paramiko.MissingHostKeyPolicy):
        def missing_host_key(self, _client: Any, _hostname: str, key: Any) -> None:
            seen["key"] = f"{key.get_name()} {key.get_base64()}"

    try:
        client.set_missing_host_key_policy(_Remember())
        client.connect(host, port=port, username=username, pkey=pkey, timeout=timeout,
                       banner_timeout=timeout, auth_timeout=timeout, allow_agent=False,
                       look_for_keys=False)
        transport = client.get_transport()
        if transport is not None and not seen:
            k = transport.get_remote_server_key()
            seen["key"] = f"{k.get_name()} {k.get_base64()}"
        if expected_host_key and seen.get("key") and seen["key"] != expected_host_key.strip():
            return _fail("ssh_host_key_changed",
                         "The WeatherPi's identity has changed since setup. If you re-flashed "
                         "its card that's expected; otherwise ask for help.",
                         f"expected {expected_host_key}, got {seen.get('key')}",
                         host_key=seen.get("key"))
        _stdin, stdout, _stderr = client.exec_command("true", timeout=timeout)
        code = stdout.channel.recv_exit_status()
        if code != 0:
            return _fail("ssh_command_failed", "SSH login worked but a test command failed.",
                         f"exit {code}", optional=True, host_key=seen.get("key"))
        return _ok("ssh_login", "The app can log in to the WeatherPi securely.",
                   host_key=seen.get("key"))
    except paramiko.AuthenticationException as exc:
        return _fail("ssh_auth_failed", "The WeatherPi didn't accept the app's key.",
                     str(exc), Action.RETRY)
    except (paramiko.SSHException, OSError, EOFError) as exc:
        return _fail("ssh_unreachable", "We couldn't open an SSH connection to the WeatherPi.",
                     str(exc), Action.RETRY)
    finally:
        with contextlib.suppress(Exception):
            client.close()


def check_dashboard(http_get: HttpGet, url: str | None, timeout: float = 15.0) -> CheckResult:
    if not url:
        return _fail("dashboard_no_url", "The dashboard address isn't known yet.",
                     action=Action.RETRY)
    try:
        status = http_get(url, timeout)
    except Exception as exc:  # noqa: BLE001 - any network failure is a failed check
        return _fail("dashboard_unreachable",
                     "The dashboard page didn't load yet. The first time, its security "
                     "certificate can take a minute to be issued. Try again shortly.",
                     f"{url}: {exc}", Action.RETRY, url=url)
    if status == 200:
        return _ok("dashboard_ok", "The dashboard loads.", url, url=url)
    return _fail("dashboard_http_error", "The dashboard page didn't load properly.",
                 f"{url}: HTTP {status}", Action.RETRY, url=url)


_S = host_usb.UsbState
_USB_MESSAGES: dict[host_usb.UsbState, tuple[str, str | None]] = {
    _S.NOT_PLUGGED_IN: (
        ("We can't see the WeatherPi yet. Plug the cable into its port marked USB (not PWR) "
        "and into this computer; that also powers it. Some cables only charge; if nothing "
        "happens, try another cable."),
        Action.PLUG_IN,
    ),
    _S.BOOT_ROM: (
        ("The WeatherPi has power but can't start from its memory card. Check the card is "
        "pushed in fully. If it still happens, the card may need re-flashing or replacing."),
        None,
    ),
    _S.NO_DRIVER: (
        ("The WeatherPi is plugged in, but this computer needs its USB driver. Click Install "
        "driver."),
        Action.INSTALL_DRIVER,
    ),
    _S.DRIVER_PROBLEM: (
        ("The WeatherPi is plugged in, but its USB driver has a problem. Click Install driver, "
        "then unplug and re-plug the cable."),
        Action.INSTALL_DRIVER,
    ),
    _S.NO_NETWORK: (
        "The WeatherPi is starting up. This can take a couple of minutes the first time.",
        Action.WAIT,
    ),
    _S.NO_AGENT: (
        ("The WeatherPi is connected and still starting its setup service. This can take "
        "up to a minute; we'll keep checking."),
        Action.WAIT,
    ),
}


# --------------------------------------------------------------------------- session


class SavedNetworks:
    """The Pi's saved WiFi networks (agent 2.1.0): list, add one that isn't in range, change
    the order, remove. Shared by setup (over USB) and the home screen (usually over
    Tailscale, where removing the network in use would cut the link: ``allow_forget_in_use``).
    """

    def __init__(self, client: Callable[[], AgentClient],
                 allow_forget_in_use: bool = True) -> None:
        self.client = client
        self.allow_forget_in_use = allow_forget_in_use
        #: last list from saved_networks(); saved_networks_problem says why it's stale/empty
        self.saved: list[SavedNetwork] = []
        self.saved_networks_problem: CheckResult | None = None


    def _saved_problem(self, code: str, exc: AgentError, failed_msg: str,
                       old_agent_statuses: tuple[int, ...] = (404, 405)) -> CheckResult:
        """Turn an AgentError from a saved-networks call into a plain-words check."""
        if isinstance(exc, AgentUnreachable):
            return _unreachable(code, exc)
        if exc.code == "busy":
            return _fail("wifi_busy", "The WeatherPi is busy joining a WiFi network right now. "
                         "Wait a few seconds and try again.", str(exc), Action.RETRY)
        if exc.code == "bad_request":
            return _fail("wifi_save_rejected", "The WeatherPi didn't accept that network. WiFi "
                         "passwords are 8 to 63 characters long; leave the password empty only "
                         "for an open network.", str(exc), Action.CHOOSE_WIFI)
        if exc.status in old_agent_statuses:
            return _fail("wifi_saved_unsupported", "This WeatherPi's software is too old to "
                         "keep several WiFi networks. It needs a newer SD card image.",
                         str(exc))
        return _fail(code, failed_msg, str(exc), Action.RETRY)

    def saved_networks(self) -> list[SavedNetwork]:
        """The Pi's saved networks, highest priority first (also kept in ``self.saved``).

        Never raises: on failure it returns ``[]`` and ``self.saved_networks_problem`` holds
        a failed check saying why (it's None after a successful call).
        """
        try:
            nets = self.client().wifi_saved()
        except AgentError as exc:
            self.saved_networks_problem = self._saved_problem(
                "wifi_saved_failed", exc, "Couldn't read the WeatherPi's saved WiFi networks. "
                "Try again.")
            return []
        self.saved_networks_problem = None
        self.saved = nets
        return nets

    def _with_list(self, check: CheckResult) -> list[CheckResult]:
        """A success check plus the refreshed list in ``data["networks"]`` (best effort)."""
        nets = self.saved_networks()
        if self.saved_networks_problem is None:
            check.data["networks"] = nets
        return [check]

    @staticmethod
    def _priority_problem(priority: object) -> CheckResult | None:
        if isinstance(priority, bool) or not isinstance(priority, int) \
                or not WIFI_PRIORITY_MIN <= priority <= WIFI_PRIORITY_MAX:
            return _fail("wifi_priority_range", f"The priority must be a whole number from "
                         f"{WIFI_PRIORITY_MIN} to {WIFI_PRIORITY_MAX}.", f"priority={priority!r}",
                         Action.CHOOSE_WIFI)
        return None

    def save_network(self, ssid: str, password: str | None, hidden: bool = False,
                     priority: int = 0) -> list[CheckResult]:
        """Save a network for later (e.g. a phone hotspot) without connecting to it.

        An empty/None ``password`` saves an open network. The password can't be checked
        until the WeatherPi is near that network. On success ``data`` has ``ssid`` and the
        refreshed ``networks`` list.
        """
        ssid = ssid.strip("\r\n")
        if not ssid:
            return [_fail("wifi_no_ssid", "Type the network's name first.",
                          action=Action.CHOOSE_WIFI)]
        if password and not (8 <= len(password) <= 63):
            return [_fail("wifi_password_length", "WiFi passwords are 8 to 63 characters long. "
                          "Please check it.", action=Action.CHOOSE_WIFI)]
        problem = self._priority_problem(priority)
        if problem:
            return [problem]
        try:
            self.client().wifi_save(ssid, password or None, hidden, priority)
        except AgentError as exc:
            return [self._saved_problem("wifi_save_failed", exc,
                                        f"The WeatherPi couldn't save {ssid}. Try again.")]
        return self._with_list(_ok(
            "wifi_saved", f"Saved {ssid}. The WeatherPi will join it by itself when it's in "
            "range. It can't check the password until then.", ssid=ssid))

    def set_network_priority(self, ssid: str, priority: int) -> list[CheckResult]:
        """Change which saved network the WeatherPi prefers (higher joins first)."""
        problem = self._priority_problem(priority)
        if problem:
            return [problem]
        try:
            self.client().wifi_set_priority(ssid, priority)
        except AgentError as exc:
            if exc.code == "not_found" and exc.status == 404:
                self.saved_networks()
                return [_fail("wifi_not_saved", f"{ssid} isn't saved on the WeatherPi any more.",
                              str(exc), Action.RETRY)]
            return [self._saved_problem("wifi_priority_failed", exc,
                                        f"Couldn't change the order for {ssid}. Try again.",
                                        old_agent_statuses=(405,))]
        return self._with_list(_ok("wifi_priority_set", f"Updated the order for {ssid}.",
                                   ssid=ssid, priority=priority))

    def forget_network(self, ssid: str) -> list[CheckResult]:
        """Remove a saved network. Forgetting the one in use takes the WeatherPi off WiFi
        until it finds another saved network (the message says so)."""
        try:
            current = self.client().wifi_saved()
        except AgentError:
            current = self.saved  # best effort; the delete below reports real problems
        was_in_use = any(n.ssid == ssid and n.in_use for n in current)
        if was_in_use and not self.allow_forget_in_use:
            return [_fail("wifi_in_use_remote",
                          f"The WeatherPi is using {ssid} right now, and this computer reaches "
                          "it through that network. To change or remove it, plug the WeatherPi "
                          "into this computer with the USB cable and choose Set up again "
                          "(Settings).", ssid=ssid)]
        try:
            self.client().wifi_forget(ssid)
        except AgentError as exc:
            if exc.code == "not_found" and exc.status == 404:
                return self._with_list(_ok("wifi_forgotten", f"{ssid} wasn't saved.", str(exc),
                                           ssid=ssid))
            return [self._saved_problem("wifi_forget_failed", exc,
                                        f"Couldn't forget {ssid}. Try again.",
                                        old_agent_statuses=())]
        msg = f"Forgot {ssid}."
        if was_in_use:
            msg += (" The WeatherPi was using it, so it's off WiFi until it finds another "
                    "saved network.")
        return self._with_list(_ok("wifi_forgotten", msg, ssid=ssid, was_in_use=was_in_use))


class SetupSession:
    """State carried through the wizard for one Pi."""

    def __init__(self, env: HostEnv | None = None) -> None:
        self.env = env or HostEnv()
        self.usb: host_usb.UsbDetection | None = None
        self.info: AgentInfo | None = None
        self.secrets: StationSecrets | None = None
        self.store: StationStore | None = None
        self.pi_ts: TailscaleStatus | None = None
        self.host_ts: host_ts.HostTailscaleStatus | None = None
        self.wifi: WifiStatus | None = None
        self._saved_nets = SavedNetworks(lambda: self.usb_client())
        self._claimed_here = False
        self._usb_client: AgentClient | None = None
        self._ts_client: AgentClient | None = None

    # ---- plumbing

    @property
    def usb_base_url(self) -> str:
        if self.usb and self.usb.base_url:
            return self.usb.base_url
        return f"http://{USB_HOST}:8765"

    @property
    def usb_address(self) -> str:
        return (self.usb.address if self.usb and self.usb.address else USB_HOST)

    def usb_client(self) -> AgentClient:
        token = self.secrets.api_token if self.secrets else None
        c = self._usb_client
        if c is None or c.base_url != self.usb_base_url.rstrip("/") or c.token != token:
            if c is not None:
                c.close()
            c = self.env.client_factory(self.usb_base_url, token, "usb")
            self._usb_client = c
        return c

    def tailnet_client(self) -> AgentClient | None:
        """Client over Tailscale (MagicDNS name, else 100.x IP), once known."""
        target = None
        if self.pi_ts:
            target = self.pi_ts.dns_name or self.pi_ts.ip
        if not target and self.store:
            prof = self.store.load_profile()
            target = prof and (prof.dns_name or prof.tailnet_ip)
        if not target:
            return None
        url = f"http://{target}:8765"
        token = self.secrets.api_token if self.secrets else None
        c = self._ts_client
        if c is None or c.base_url != url or c.token != token:
            if c is not None:
                c.close()
            c = self.env.client_factory(url, token, "tailscale")
            self._ts_client = c
        return c

    def close(self) -> None:
        for c in (self._usb_client, self._ts_client):
            if c is not None:
                c.close()
        self._usb_client = self._ts_client = None

    def _poll(self, fetch: Callable[[], Any], done: Callable[[Any], bool], timeout: float,
              interval: float, cancel: threading.Event | None,
              on_progress: Progress | None) -> PollResult:
        return poll(fetch, done, timeout=timeout, interval=interval, cancel=cancel,
                    on_progress=on_progress, sleep=self.env.sleep, clock=self.env.clock)

    def _update_profile(self, **changes: Any) -> None:
        if self.store is not None:
            self.store.update_profile(**changes)

    # ---- page 2: this computer

    def check_this_computer(self) -> list[CheckResult]:
        checks = [self.driver_check()]
        ts = self.env.tailscale
        assert ts is not None
        if ts.installed():
            ver = ts.version()
            checks.append(_ok("host_tailscale_installed", "Tailscale is installed on this "
                              "computer.", f"version {ver or 'unknown'}", version=ver))
        else:
            checks.append(_fail("host_tailscale_missing",
                                "Tailscale isn't installed on this computer yet. It's free; "
                                "click Install Tailscale and follow the steps.",
                                action=Action.INSTALL_TAILSCALE,
                                url="https://tailscale.com/download/windows"))
        return checks

    def driver_check(self) -> CheckResult:
        try:
            st = self.env.get_driver_status()
        except Exception as exc:  # noqa: BLE001
            return _fail("driver_unknown", "We couldn't check the USB driver. You can carry "
                         "on; we'll check again when the WeatherPi is plugged in.", str(exc),
                         Action.INSTALL_DRIVER, optional=True)
        S = host_driver.DriverState
        if st.state == S.NOT_NEEDED:
            return _ok("driver_not_needed", "No USB driver is needed on this computer.")
        if st.state == S.OK:
            return _ok("driver_ok", "The USB driver for the WeatherPi is installed.",
                       st.detail)
        if st.state == S.UNKNOWN:
            return _fail("driver_unknown",
                         "We couldn't find the WeatherPi USB driver. If you installed WeatherPi "
                         "Companion with its installer it's probably fine. Otherwise click "
                         "Install driver.", st.detail, Action.INSTALL_DRIVER, optional=True)
        if st.state == S.MISSING:
            return _fail("driver_missing",
                         "This computer needs the WeatherPi USB driver. Click Install driver "
                         "and say Yes when Windows asks for permission.", st.detail,
                         Action.INSTALL_DRIVER)
        return _fail("driver_problem",
                     "The WeatherPi USB driver has a problem. Click Install driver to "
                     "reinstall it, then unplug the WeatherPi and plug it back in.",
                     f"{st.detail} (problem code {st.problem_code})", Action.INSTALL_DRIVER)

    def install_driver(self, exe_path: Path | None = None) -> list[CheckResult]:
        """Run the bundled driver setup (UAC prompt), then re-check."""
        exe = exe_path or self.env.driver_exe
        if exe is None:
            return [_fail("driver_setup_missing", "The driver installer isn't included with "
                          "this copy of the app. Please reinstall WeatherPi Companion.")]
        try:
            if self.env.driver_installer is not None:
                result = self.env.driver_installer(Path(exe))
            else:
                result = host_driver.install_bundled_driver(exe, platform=self.env.platform)
        except host_driver.InstallError as exc:
            if exc.code == "cancelled":
                return [_fail("driver_install_cancelled", "The driver wasn't installed because "
                              "the Windows permission prompt was cancelled. Click Install "
                              "driver to try again.", exc.message, Action.INSTALL_DRIVER)]
            return [_fail("driver_install_failed", "The driver installer couldn't start.",
                          exc.message, Action.INSTALL_DRIVER)]
        if not result.ok:
            return [_fail("driver_install_failed", "The driver installer reported a problem.",
                          f"exit code {result.exit_code}", Action.INSTALL_DRIVER)]
        checks = [self.driver_check()]
        if result.reboot_required:
            checks.append(_fail("reboot_required", "Windows says it needs a restart to finish "
                                "installing the driver.", optional=True))
        if checks[0].code == "driver_unknown":
            # installer reported success; trust it until the Pi is plugged in
            checks[0] = _ok("driver_installed", "The USB driver was installed.")
        return checks

    # ---- page 3: plug in

    def detect_plug_in(self) -> PlugInResult:
        try:
            det = self.env.detect_usb()
        except Exception as exc:  # noqa: BLE001
            return PlugInResult([_fail("usb_check_failed", "We couldn't check the USB "
                                       "connection.", str(exc), Action.RETRY)], "wait")
        self.usb = det
        if det.state != host_usb.UsbState.CONNECTED:
            msg, action = _USB_MESSAGES[det.state]
            return PlugInResult([_fail(f"usb_{det.state.value}", msg, det.detail, action)],
                                "wait", usb=det)
        info = det.info
        assert info is not None
        self.info = info
        adapter = det.adapter
        checks = [
            _ok("usb_adapter", "The USB cable connection is working.",
                f"{adapter.name}: {adapter.usb_address or adapter.addresses}" if adapter
                else "", address=det.address),
            _ok("agent_info", f"Found your WeatherPi ({info.model or info.hostname}).",
                f"serial {info.serial}, image {info.image_version}, agent {info.agent_version}"),
        ]
        if not info.compatible:
            checks.append(_fail("agent_version_mismatch",
                                "This WeatherPi needs a different version of WeatherPi "
                                "Companion. Please update the app.",
                                f"agent {info.agent_version}", Action.UPDATE_APP))
            return PlugInResult(checks, "wait", info, usb=det)
        try:
            self.store = self.env.app_data.station(info.serial)
        except ValueError as exc:
            checks.append(_fail("bad_serial", "The WeatherPi reported an unusual serial number.",
                                str(exc)))
            return PlugInResult(checks, "wait", info, usb=det)
        if not info.claimed:
            checks.append(_ok("not_claimed", "This WeatherPi is new and ready to set up."))
            return PlugInResult(checks, "claim", info, usb=det)
        # already claimed: is it ours?
        secrets = self.store.load_secrets()
        if secrets is not None:
            self.secrets = secrets
            try:
                wifi = self.usb_client().wifi_status()
            except AgentError as exc:
                if exc.status != 401:
                    checks.append(_unreachable("token_check", exc))
                    return PlugInResult(checks, "wait", info, usb=det)
                self.secrets = None
            else:
                self.wifi = wifi
                checks.append(_ok("already_ours", "This WeatherPi is already set up with this "
                                  "computer. We'll pick up where you left off."))
                page = 6 if (wifi.state == "connected" and wifi.internet) else 5
                return PlugInResult(checks, "resume", info, page, usb=det)
        checks.append(_fail("claimed_elsewhere",
                            "This WeatherPi has already been set up, but not from this "
                            "computer. If it's yours, use your recovery file to take it over "
                            "(you'll choose a new password).", action=Action.RECOVER))
        return PlugInResult(checks, "recover", info, usb=det)

    def wait_for_plug_in(self, timeout: float = 180, cancel: threading.Event | None = None,
                         on_progress: Progress | None = None,
                         interval: float = 2.0) -> PlugInResult:
        res = self._poll(self.detect_plug_in, lambda r: r.next_step != "wait", timeout,
                         interval, cancel, on_progress)
        return res.value or PlugInResult(
            [_fail("usb_not_found", "We couldn't find the WeatherPi.", action=Action.PLUG_IN)],
            "wait")

    def recover_access(self, new_password: str, confirm: str,
                       recovery_password: str | None = None,
                       recovery_file: Path | None = None) -> list[CheckResult]:
        """Take over a claimed Pi with its recovery password (from this PC's store, a copied
        RECOVERY.txt, or typed). Sets a new owner password and re-keys this PC."""
        checks = validate_password(new_password, confirm)
        if not all_ok(checks):
            return checks
        if self.info is None or self.store is None:
            return [_fail("not_connected", "Plug in the WeatherPi first.", action=Action.PLUG_IN)]
        rp = recovery_password
        if not rp and recovery_file:
            from .secrets import read_recovery_password
            rp = read_recovery_password(recovery_file)
        if not rp:
            existing = self.store.load_secrets()
            rp = existing.recovery_password if existing else None
        if not rp:
            rp = self.store.read_recovery_password_from_txt()
        if not rp:
            return [_fail("no_recovery_password", "We need the recovery password from your "
                          "RECOVERY.txt file. Choose the file or type the password.",
                          action=Action.RECOVER)]
        rp = rp.strip()
        fresh = StationSecrets.generate()
        new = StationSecrets(fresh.ssh_key, fresh.api_token, rp)
        try:
            with self.env.client_factory(self.usb_base_url, None, "usb") as c:
                c.recover(rp, new_password, new.ssh_key.public_openssh, new.api_token)
        except AgentError as exc:
            if exc.code == "bad_recovery":
                return [_fail("bad_recovery", "That recovery password isn't right. Check it "
                              "against your RECOVERY.txt (after 5 wrong tries you have to wait "
                              "10 minutes).", exc.message, Action.RECOVER)]
            if exc.code == "weak_password":
                return [_fail("weak_password", "The WeatherPi didn't accept that password. "
                              "Please choose a longer one.", exc.message, Action.FIX_PASSWORD)]
            if isinstance(exc, AgentUnreachable):
                return [_unreachable("recover", exc)]
            return [_fail("recover_failed", "Recovery didn't work.", str(exc), Action.RETRY)]
        self.store.save_secrets(new)
        self.secrets = new
        self.store.write_recovery_file(rp, self.info.hostname)
        self._update_profile(hostname=self.info.hostname, model=self.info.model,
                             image_version=self.info.image_version,
                             agent_version=self.info.agent_version)
        return [_ok("recovered", "Done. You have a new password and this computer can manage "
                    "the WeatherPi."), *self._post_claim_checks()]

    # ---- page 4: password + claim

    def create_password(self, password: str, confirm: str, owner_name: str | None = None,
                        timezone: str | None = None) -> list[CheckResult]:
        checks = validate_password(password, confirm)
        if not all_ok(checks):
            return checks
        if self.info is None or self.store is None:
            return [_fail("not_connected", "Plug in the WeatherPi first.", action=Action.PLUG_IN)]
        info = self.info
        if not self._claimed_here:
            # generate (or reuse from an earlier failed attempt) and save BEFORE claiming so
            # a crash after the claim can't lose the keys
            self.secrets = self.store.load_secrets() or StationSecrets.generate()
            self.store.save_secrets(self.secrets)
            tz = timezone if timezone is not None else guess_timezone(self.env.runner,
                                                                     self.env.platform)
            try:
                with self.env.client_factory(self.usb_base_url, None, "usb") as c:
                    c.claim(password, self.secrets.recovery_password,
                            self.secrets.ssh_key.public_openssh, self.secrets.api_token,
                            owner_name=owner_name, timezone=tz)
            except AgentError as exc:
                return [self._claim_error(exc)]
            self._claimed_here = True
            self.store.write_recovery_file(self.secrets.recovery_password, info.hostname)
            # A fresh claim means a fresh card (e.g. re-flashed): forget what this PC remembered
            # about the old one (its SSH identity, Tailscale node, dashboard URL, "set up").
            self._update_profile(hostname=info.hostname, model=info.model,
                                 owner_name=owner_name, claimed_at=format_time(self.env.now()),
                                 image_version=info.image_version,
                                 agent_version=info.agent_version, ssh_host_key=None,
                                 tailnet_ip=None, dns_name=None, tailnet=None, serve_url=None,
                                 setup_complete=False)
        out = [_ok("claimed", "Your password is set.",
                   recovery_file=str(self.store.recovery_txt_path))]
        out += self._post_claim_checks()
        return out

    def _claim_error(self, exc: AgentError) -> CheckResult:
        if exc.code == "weak_password":
            return _fail("weak_password", "The WeatherPi didn't accept that password. Please "
                         "choose a longer one.", exc.message, Action.FIX_PASSWORD)
        if exc.code == "already_claimed":
            return _fail("already_claimed", "This WeatherPi has already been set up. If it's "
                         "yours, use your recovery file.", exc.message, Action.RECOVER)
        if isinstance(exc, AgentUnreachable):
            return _unreachable("claim", exc)
        return _fail("claim_failed", "Setting the password didn't work. Please try again.",
                     str(exc), Action.RETRY)

    def _post_claim_checks(self) -> list[CheckResult]:
        out = []
        client = self.usb_client()
        # push the PC clock if the Pi hasn't synced (no RTC, no internet yet)
        if self.info is not None and not self.info.clock_synced:
            try:
                client.set_time(self.env.now())
                out.append(_ok("time_set", "Set the WeatherPi's clock from this computer."))
            except AgentError as exc:
                out.append(_fail("time_not_set", "Couldn't set the WeatherPi's clock (it will "
                                 "set itself once it's online).", str(exc), optional=True))
        try:
            self.wifi = client.wifi_status()
            out.append(_ok("token_ok", "This computer is now paired with the WeatherPi."))
        except AgentError as exc:
            if isinstance(exc, AgentUnreachable):
                out.append(_unreachable("token_check", exc))
            else:
                out.append(_fail("token_rejected", "The WeatherPi didn't accept this "
                                 "computer's pairing key.", str(exc), Action.RETRY))
        out.append(self.check_ssh())
        return out

    def check_ssh(self, host: str | None = None, attempts: int = 3,
                  delay: float = 3.0) -> CheckResult:
        """SSH key login over USB (retries a little: sshd may still be reloading)."""
        if self.secrets is None:
            return _fail("ssh_no_key", "This computer has no key for the WeatherPi yet.")
        prof = self.store.load_profile() if self.store else None
        expected = prof.ssh_host_key if prof else None
        result: CheckResult | None = None
        for i in range(max(1, attempts)):
            result = self.env.verify_ssh(host or self.usb_address,
                                         self.secrets.ssh_key.private_openssh,
                                         expected_host_key=expected)
            if result.ok or result.code == "ssh_host_key_changed":
                break
            if i + 1 < attempts:
                (self.env.sleep or time.sleep)(delay)
        assert result is not None
        if result.ok and result.data.get("host_key") and not expected:
            self._update_profile(ssh_host_key=result.data["host_key"])
        return result

    # ---- page 5: wifi

    def scan_wifi(self) -> WifiScanResult:
        try:
            nets = self.usb_client().wifi_scan()
        except AgentError as exc:
            if isinstance(exc, AgentUnreachable):
                return WifiScanResult([_unreachable("wifi_scan", exc)])
            return WifiScanResult([_fail("wifi_scan_failed", "Couldn't look for WiFi networks. "
                                         "Try again.", str(exc), Action.RETRY)])
        if not nets:
            return WifiScanResult([_fail("wifi_none_found", "The WeatherPi can't see any WiFi "
                                         "networks. It only works with 2.4 GHz WiFi. Try moving "
                                         "it closer to your router, or use Other network.",
                                         action=Action.MOVE_CLOSER, optional=True)])
        return WifiScanResult([_ok("wifi_scan", f"Found {len(nets)} networks.")], nets)

    def connect_wifi(self, ssid: str, password: str | None, hidden: bool = False,
                     secured: bool = True) -> list[CheckResult]:
        ssid = ssid.strip("\r\n")
        if not ssid:
            return [_fail("wifi_no_ssid", "Choose a network first.", action=Action.CHOOSE_WIFI)]
        if secured and password is not None and not (8 <= len(password) <= 63) and password:
            return [_fail("wifi_password_length", "WiFi passwords are 8 to 63 characters long. "
                          "Please check it.", action=Action.CHOOSE_WIFI)]
        if secured and not password:
            return [_fail("wifi_password_missing", "Type the WiFi password.",
                          action=Action.CHOOSE_WIFI)]
        try:
            self.usb_client().wifi_connect(ssid, password if secured else "", hidden)
        except AgentError as exc:
            if isinstance(exc, AgentUnreachable):
                return [_unreachable("wifi_connect", exc)]
            return [_fail("wifi_connect_failed", "The WeatherPi couldn't start connecting. Try "
                          "again.", str(exc), Action.RETRY)]
        return [_ok("wifi_connecting", f"Connecting to {ssid}...", ssid=ssid)]

    def wait_for_wifi(self, ssid: str | None = None, timeout: float = WIFI_TIMEOUT_S,
                      cancel: threading.Event | None = None,
                      on_progress: Progress | None = None,
                      interval: float = 2.0) -> list[CheckResult]:
        client = self.usb_client()

        def done(st: WifiStatus) -> bool:
            if st.state == "failed":
                return True
            right = ssid is None or st.ssid == ssid
            return st.state == "connected" and right and st.internet

        res = self._poll(client.wifi_status, done, timeout, interval, cancel, on_progress)
        if res.reason == "cancelled":
            return [_fail("cancelled", "Stopped waiting.", action=Action.RETRY)]
        st: WifiStatus | None = res.value
        if st is None:
            err = res.last_error or TimeoutError("no answer")
            return [_unreachable("wifi_status", err)]
        self.wifi = st
        return self.wifi_checks(st, ssid, timed_out=res.reason == "timeout")

    def wifi_checks(self, st: WifiStatus | None = None, ssid: str | None = None,
                    timed_out: bool = False) -> list[CheckResult]:
        if st is None:
            try:
                st = self.usb_client().wifi_status()
            except AgentError as exc:
                return [_unreachable("wifi_status", exc)]
            self.wifi = st
        name = ssid or st.ssid or "the network"
        if st.state == "failed" or (st.state != "connected" and timed_out):
            err = st.last_error or ("timeout" if timed_out else "unknown")
            msg = {
                "wrong_password": f"The password for {name} wasn't accepted. Check it and try "
                                  "again (passwords are case-sensitive).",
                "not_found": f"The WeatherPi can't find {name}. Move it closer to your router, "
                             "and remember it only works with 2.4 GHz WiFi.",
                "timeout": f"Connecting to {name} took too long. Move the WeatherPi closer to "
                           "your router and try again.",
            }.get(err, f"The WeatherPi couldn't connect to {name}.")
            action = Action.MOVE_CLOSER if err in ("not_found", "timeout") else Action.CHOOSE_WIFI
            return [_fail("wifi_failed", msg, f"state={st.state} last_error={st.last_error}",
                          action, error=err)]
        if st.state != "connected":
            return [_fail("wifi_not_connected", f"Still connecting to {name}...",
                          f"state={st.state}", Action.WAIT)]
        checks = [_ok("wifi_connected", f"Connected to {st.ssid}.",
                      f"ip {st.ip}, signal {st.signal}", ssid=st.ssid, signal=st.signal)]
        if st.internet:
            checks.append(_ok("wifi_internet", "The WeatherPi can reach the internet."))
        else:
            checks.append(_fail("wifi_no_internet",
                                f"The WeatherPi joined {st.ssid} but can't reach the internet. "
                                "Check your router is online.", action=Action.RETRY))
        return checks

    # ---- page 5: saved networks (agent 2.1.0), shared with the home screen

    @property
    def saved(self) -> list[SavedNetwork]:
        return self._saved_nets.saved

    @saved.setter
    def saved(self, value: list[SavedNetwork]) -> None:
        self._saved_nets.saved = value

    @property
    def saved_networks_problem(self) -> CheckResult | None:
        return self._saved_nets.saved_networks_problem

    @saved_networks_problem.setter
    def saved_networks_problem(self, value: CheckResult | None) -> None:
        self._saved_nets.saved_networks_problem = value

    def saved_networks(self) -> list[SavedNetwork]:
        """See :meth:`SavedNetworks.saved_networks` (never raises)."""
        return self._saved_nets.saved_networks()

    def save_network(self, ssid: str, password: str | None, hidden: bool = False,
                     priority: int = 0) -> list[CheckResult]:
        return self._saved_nets.save_network(ssid, password, hidden, priority)

    def set_network_priority(self, ssid: str, priority: int) -> list[CheckResult]:
        return self._saved_nets.set_network_priority(ssid, priority)

    def forget_network(self, ssid: str) -> list[CheckResult]:
        return self._saved_nets.forget_network(ssid)

    # ---- page 6: tailscale on this PC

    def check_host_tailscale(self) -> list[CheckResult]:
        ts = self.env.tailscale
        assert ts is not None
        if not ts.installed():
            return [_fail("host_tailscale_missing", "Tailscale isn't installed on this "
                          "computer.", action=Action.INSTALL_TAILSCALE,
                          url="https://tailscale.com/download")]
        try:
            ensured = host_ts.ensure_running(ts)  # switched off: switch it back on
        except host_ts.TailscaleError as exc:
            gui = self.env.tailscale_gui()
            return [_fail("host_tailscale_not_running", "Tailscale isn't running on this "
                          "computer. Start the Tailscale app, then try again.", str(exc),
                          Action.OPEN_TAILSCALE_APP if gui else Action.RETRY, path=gui)]
        st = ensured.status
        self.host_ts = st
        state = st.backend_state
        if st.running:
            who = st.tailnet_name or st.magic_dns_suffix or "your account"
            msg = f"Tailscale is on and signed in as {who}."
            detail = f"tailnet {st.magic_dns_suffix}, ip {st.self_ipv4}"
            if ensured.switched_on:
                msg = f"Tailscale was switched off on this computer, so the app switched it " \
                      f"back on. It's signed in as {who}."
                detail = "was Stopped; ran `tailscale up`; " + detail
            return [_ok("host_tailscale_running", msg, detail,
                        tailnet_name=st.tailnet_name, tailnet=st.magic_dns_suffix,
                        ip=st.self_ipv4, switched_on=ensured.switched_on)]
        if st.needs_login:
            return [_fail("host_tailscale_logged_out", "Sign in to Tailscale on this computer. "
                          "Click Sign in; your web browser will open.", state,
                          Action.HOST_TAILSCALE_LOGIN, auth_url=st.auth_url)]
        if state == "Stopped":  # the app tried `tailscale up` and it didn't come on
            return [_fail("host_tailscale_stopped", "Tailscale is switched off on this "
                          "computer and the app couldn't switch it on. Click Connect (or "
                          "switch it on from the Tailscale icon).",
                          "; ".join(x for x in (state, ensured.output) if x),
                          Action.HOST_TAILSCALE_LOGIN)]
        if state == "NoState":
            # On Windows the tray app starts the backend; if it isn't running (e.g. Tailscale
            # was taken out of the startup apps) NoState never ends by itself.
            gui = self.env.tailscale_gui()
            if gui and host_ts.gui_running(self.env.runner, self.env.platform) is False:
                return [_fail("host_tailscale_not_started", "Tailscale is installed but "
                              "hasn't been started on this computer. Click Start Tailscale.",
                              f"{state}; {'; '.join(st.health)}", Action.OPEN_TAILSCALE_APP,
                              path=gui)]
        return [_fail("host_tailscale_starting", "Tailscale is starting on this computer...",
                      f"{state}; {'; '.join(st.health)}", Action.WAIT)]

    def start_host_login(self) -> CheckResult:
        """Start login/connect; ``data["auth_url"]`` is the page to open in the browser."""
        ts = self.env.tailscale
        assert ts is not None
        try:
            login = (self.env.host_login or host_ts.start_login)(ts)
        except host_ts.TailscaleError as exc:
            gui = self.env.tailscale_gui()
            return _fail("host_login_failed", "Couldn't start Tailscale sign-in. Use the "
                         "Tailscale app to sign in, then click Check again.", str(exc),
                         Action.OPEN_TAILSCALE_APP if gui else Action.RETRY, path=gui)
        if login.already_running:
            return _ok("host_tailscale_running", "Tailscale is on.")
        if login.auth_url:
            return CheckResult(False, "host_login_url", "Your browser will open Tailscale's "
                               "sign-in page. Sign in, then come back here.", login.auth_url,
                               Action.OPEN_URL, {"url": login.auth_url})
        gui = self.env.tailscale_gui()
        return _fail("host_login_no_url", "Sign in using the Tailscale icon near the clock, "
                     "then click Check again.", "", Action.OPEN_TAILSCALE_APP if gui else
                     Action.RETRY, path=gui)

    def wait_for_host_tailscale(self, timeout: float = 300,
                                cancel: threading.Event | None = None,
                                on_progress: Progress | None = None,
                                interval: float = 2.0) -> list[CheckResult]:
        res = self._poll(self.check_host_tailscale, all_ok, timeout, interval, cancel,
                         on_progress)
        return res.value or [_fail("host_tailscale_timeout", "Tailscale didn't come on.",
                                   action=Action.RETRY)]

    # ---- page 7: tailscale on the pi

    def _pi_client(self) -> AgentClient:
        return self.usb_client()

    def start_pi_tailscale(self, hostname: str | None = None) -> list[CheckResult]:
        client = self._pi_client()
        try:
            st = client.tailscale_status()
            self.pi_ts = st
            if st.running:
                return [_ok("pi_tailscale_running", "Tailscale is already on on the WeatherPi.")]
            if not st.installed:
                return [_fail("pi_tailscale_missing", "Tailscale is missing on the WeatherPi. "
                              "The card may need re-flashing.", "installed=false")]
            if st.auth_url:
                return [_ok("pi_tailscale_started", "Tailscale is starting on the WeatherPi.",
                            auth_url=st.auth_url)]
            client.tailscale_up(hostname or (self.info.hostname if self.info else "weatherpi"))
        except AgentError as exc:
            if exc.code == "no_internet":
                return [_fail("pi_no_internet", "The WeatherPi isn't on the internet, so it "
                              "can't start Tailscale. Go back and check the WiFi.",
                              exc.message, Action.CHOOSE_WIFI)]
            if isinstance(exc, AgentUnreachable):
                return [_unreachable("pi_tailscale_up", exc)]
            return [_fail("pi_tailscale_up_failed", "Couldn't start Tailscale on the WeatherPi.",
                          str(exc), Action.RETRY)]
        return [_ok("pi_tailscale_started", "Tailscale is starting on the WeatherPi.")]

    def wait_for_pi_auth_url(self, timeout: float = 60,
                             cancel: threading.Event | None = None,
                             on_progress: Progress | None = None,
                             interval: float = 1.0) -> CheckResult:
        """Wait for the Pi's login URL. ``data["url"]`` is it (open it in the browser)."""
        client = self._pi_client()
        res = self._poll(client.tailscale_status, lambda s: bool(s.auth_url) or s.running,
                         timeout, interval, cancel, on_progress)
        st: TailscaleStatus | None = res.value
        if st is not None:
            self.pi_ts = st
        if st is not None and st.running:
            return _ok("pi_tailscale_running", "Tailscale is already on on the WeatherPi.")
        if st is not None and st.auth_url:
            return CheckResult(False, "pi_login_url",
                               "Your browser will open Tailscale's sign-in page for the "
                               "WeatherPi. Sign in with the SAME account you use on this "
                               "computer, then come back here.", st.auth_url, Action.OPEN_URL,
                               {"url": st.auth_url})
        if res.reason == "cancelled":
            return _fail("cancelled", "Stopped waiting.", action=Action.RETRY)
        return _fail("pi_login_url_timeout", "The WeatherPi didn't get a Tailscale sign-in link. "
                     "Check its WiFi and try again.", str(res.last_error or ""), Action.RETRY)

    def wait_for_pi_tailscale(self, timeout: float = 600,
                              cancel: threading.Event | None = None,
                              on_progress: Progress | None = None,
                              interval: float = 2.0) -> list[CheckResult]:
        """Wait until the Pi is Running, then check it joined the same tailnet as this PC."""
        client = self._pi_client()
        res = self._poll(client.tailscale_status, lambda s: s.running, timeout, interval,
                         cancel, on_progress)
        st: TailscaleStatus | None = res.value
        if st is None:
            return [_unreachable("pi_tailscale_status", res.last_error or TimeoutError())]
        self.pi_ts = st
        if not st.running:
            if res.reason == "cancelled":
                return [_fail("cancelled", "Stopped waiting.", action=Action.RETRY)]
            return [_fail("pi_tailscale_not_running", "The WeatherPi hasn't finished signing in "
                          "to Tailscale. Finish the sign-in in your browser, then try again.",
                          st.backend_state, Action.RETRY, auth_url=st.auth_url)]
        checks = [_ok("pi_tailscale_running", "Tailscale is on on the WeatherPi.",
                      f"{st.dns_name} {st.ip}")]
        checks.append(self._same_tailnet_check(st))
        self._update_profile(tailnet_ip=st.ip, dns_name=st.dns_name, tailnet=st.tailnet,
                             serve_url=st.serve_url)
        return checks

    def _same_tailnet_check(self, pi: TailscaleStatus) -> CheckResult:
        ts = self.env.tailscale
        assert ts is not None
        try:
            self.host_ts = ts.status()
        except host_ts.TailscaleError as exc:
            return _fail("host_tailscale_not_running", "Tailscale isn't running on this "
                         "computer.", str(exc), Action.RETRY)
        match = host_ts.same_tailnet(self.host_ts, pi)
        if match.same:
            return _ok("same_tailnet", "This computer and the WeatherPi are on the same "
                       "Tailscale network.", match.reason)
        if match.reason == "unknown":
            return _fail("tailnet_unknown", "We can't tell yet whether the WeatherPi joined your "
                         "Tailscale network. Wait a moment and try again.", "", Action.RETRY)
        return _fail("different_tailnet",
                     "The WeatherPi signed in to a different Tailscale account than this "
                     "computer. Click Sign in again and use the same account on both.",
                     f"PC: {match.host_tailnet}, WeatherPi: {match.pi_tailnet}",
                     Action.PI_TAILSCALE_RELOGIN)

    def pi_tailscale_logout(self) -> list[CheckResult]:
        try:
            self._pi_client().tailscale_logout()
        except AgentError as exc:
            return [_fail("pi_logout_failed", "Couldn't sign the WeatherPi out of Tailscale.",
                          str(exc), Action.RETRY)]
        self.pi_ts = None
        return [_ok("pi_logged_out", "Signed the WeatherPi out of Tailscale. Now sign in "
                    "again with the same account as this computer.")]

    def enable_dashboard_serve(self) -> list[CheckResult]:
        client = self._pi_client()
        try:
            st = client.tailscale_serve()
        except AgentError as exc:
            if exc.code == "serve_not_enabled_on_tailnet":
                return [_fail("serve_not_enabled",
                              "Your Tailscale account needs one setting switched on to share "
                              "the dashboard. Click Open settings, turn it on, then try again.",
                              exc.message, Action.OPEN_URL, url=exc.admin_url)]
            if isinstance(exc, AgentUnreachable):
                # The Pi may still have finished (or found Serve switched off on the tailnet):
                # its status answers quickly, so ask it rather than guess
                try:
                    st = client.tailscale_status()
                except AgentError:
                    return [_unreachable("serve", exc)]
                if not (st.serve_url or st.serve_enabled
                        or st.serve_error == "serve_not_enabled_on_tailnet"):
                    return [_unreachable("serve", exc)]
            else:
                return [_fail("serve_failed", "Couldn't publish the dashboard.", str(exc),
                              Action.RETRY)]
        if st.serve_url or st.serve_enabled:
            if self.pi_ts:
                self.pi_ts.serve_url = st.serve_url or self.pi_ts.serve_url
                self.pi_ts.serve_enabled = True
            self._update_profile(serve_url=st.serve_url or (self.pi_ts and self.pi_ts.serve_url))
            return [_ok("serve_ok", "The dashboard is published on your Tailscale network.",
                        st.serve_url or "", url=st.serve_url)]
        if st.serve_error == "serve_not_enabled_on_tailnet":
            return [_fail("serve_not_enabled", "Your Tailscale account needs one setting switched "
                          "on to share the dashboard. Click Open settings, turn it on, then try "
                          "again.", "", Action.OPEN_URL, url=st.admin_url)]
        return [_fail("serve_failed", "Couldn't publish the dashboard.", str(st.serve_error),
                      Action.RETRY)]

    # ---- page 8: final checks (over tailscale)

    def final_checks(self, ping_attempts: int = 3) -> list[CheckResult]:
        checks: list[CheckResult] = []
        target_ip = self.pi_ts.ip if self.pi_ts else None
        prof = self.store.load_profile() if self.store else None
        target_ip = target_ip or (prof.tailnet_ip if prof else None)
        if not target_ip:
            return [_fail("no_tailnet_ip", "We don't know the WeatherPi's Tailscale address yet. "
                          "Go back one step.", action=Action.RETRY)]
        ts = self.env.tailscale
        assert ts is not None
        # 0. Tailscale on this PC may have been switched off since page 6: switch it back on
        try:
            if host_ts.ensure_running(ts).switched_on:
                checks.append(_ok("host_tailscale_switched_on", "Tailscale was switched off "
                                  "on this computer, so the app switched it back on.",
                                  "was Stopped; ran `tailscale up`"))
        except host_ts.TailscaleError:
            pass  # the ping below reports it
        # 1. ping
        ping = None
        for i in range(max(1, ping_attempts)):
            try:
                ping = ts.ping(target_ip)
            except host_ts.TailscaleError as exc:
                ping = host_ts.PingResult(False, None, None, str(exc))
            if ping.ok:
                break
            if i + 1 < ping_attempts:
                (self.env.sleep or time.sleep)(2.0)
        assert ping is not None
        if ping.ok:
            checks.append(_ok("tailnet_ping", "This computer can reach the WeatherPi over "
                              "Tailscale.", f"{ping.via} {ping.latency_ms} ms"))
        else:
            checks.append(_fail("tailnet_ping_failed", "This computer can't reach the WeatherPi "
                                "over Tailscale yet.", ping.output, Action.RETRY))
        # 2. agent over the tailnet
        client = self.tailnet_client()
        info_ok = False
        if client is not None:
            try:
                info = client.info()
                if info.via == "tailscale":
                    info_ok = True
                    checks.append(_ok("agent_over_tailnet", "The WeatherPi answers over "
                                      "Tailscale.", client.base_url))
                else:
                    checks.append(_fail("agent_wrong_link", "The WeatherPi answered, but not "
                                        "over Tailscale.", f"via={info.via}", Action.RETRY))
            except AgentError as exc:
                if self.pi_ts and self.pi_ts.dns_name and self.pi_ts.ip and \
                        client.base_url.startswith(f"http://{self.pi_ts.dns_name}"):
                    # MagicDNS may be off on this PC: fall back to the IP
                    alt = self.env.client_factory(f"http://{self.pi_ts.ip}:8765",
                                                  client.token, "tailscale")
                    try:
                        if alt.info().via == "tailscale":
                            info_ok = True
                            client.close()
                            self._ts_client, client = alt, alt
                            checks.append(_ok("agent_over_tailnet", "The WeatherPi answers "
                                              "over Tailscale.", alt.base_url))
                    except AgentError:
                        alt.close()
                if not info_ok:
                    checks.append(_fail("agent_tailnet_unreachable", "The WeatherPi doesn't "
                                        "answer over Tailscale yet.", str(exc), Action.RETRY))
        # 3. dashboard
        serve_url = (self.pi_ts.serve_url if self.pi_ts else None) or (
            prof.serve_url if prof else None)
        checks.append(check_dashboard(self.env.http_get, serve_url))
        # 4. sensors
        if info_ok and client is not None:
            checks += self._sensor_checks(client)
        else:
            checks.append(_fail("sensors_unknown", "Sensor check skipped (WeatherPi not "
                                "reachable over Tailscale).", optional=True))
        return checks

    def _sensor_checks(self, client: AgentClient) -> list[CheckResult]:
        try:
            s: SensorsLive = client.sensors_live()
        except AgentError as exc:
            return [_fail("sensors_failed", "Couldn't read the sensors.", str(exc), Action.RETRY)]
        out = []
        if s.logger.alive:
            out.append(_ok("logger_alive", "The weather recorder is running.",
                           f"heartbeat {s.logger.last_heartbeat}"))
        else:
            out.append(_fail("logger_not_alive", "The weather recorder isn't running. Try "
                             "restarting the WeatherPi.",
                             f"stale {s.logger.stale_seconds}s", Action.REBOOT))
        if s.temperature.present and s.temperature.celsius is not None:
            out.append(_ok("temperature", f"Temperature: {s.temperature.celsius:.1f} C",
                           s.temperature.sensor_id or "", celsius=s.temperature.celsius))
        else:
            out.append(_fail("temperature_missing", "Temperature sensor not connected. Everything "
                             "else works; check the sensor wiring when you can.",
                             optional=True))
        out.append(_ok("rain", f"Rain today: {s.rain.mm_today:.1f} mm "
                       f"({s.rain.tips_today} tips)", tips=s.rain.tips_today,
                       last_tip_at=s.rain.last_tip_at))
        return out

    def wait_for_rain_tip(self, timeout: float = 60, cancel: threading.Event | None = None,
                          on_progress: Progress | None = None,
                          interval: float = 1.0) -> CheckResult:
        """Optional "tip the bucket now" test: watch ``last_tip_at`` change."""
        client = self.tailnet_client() or self.usb_client()
        try:
            before = client.sensors_live()
        except AgentError as exc:
            return _fail("rain_test_failed", "Couldn't read the rain gauge.", str(exc),
                         Action.RETRY, optional=True)
        start_tip, start_count = before.rain.last_tip_at, before.rain.tips_today
        res = self._poll(client.sensors_live,
                         lambda s: (s.rain.last_tip_at != start_tip
                                    or s.rain.tips_today != start_count),
                         timeout, interval, cancel, on_progress)
        if res.done:
            return _ok("rain_tip_seen", "Got it! The rain gauge works.")
        if res.reason == "cancelled":
            return _fail("rain_test_skipped", "Rain gauge test skipped.", optional=True)
        return _fail("rain_tip_not_seen", "We didn't see the bucket tip. Check the rain gauge "
                     "cable, or skip this test.", "", Action.RETRY, optional=True)

    # ---- page 9: done

    def finish(self) -> SetupSummary:
        prof = None
        if self.store is not None:
            changes: dict[str, Any] = {"setup_complete": True}
            if self.pi_ts:
                changes.update(tailnet_ip=self.pi_ts.ip, dns_name=self.pi_ts.dns_name,
                               tailnet=self.pi_ts.tailnet,
                               serve_url=self.pi_ts.serve_url)
                changes = {k: v for k, v in changes.items() if v is not None}
            prof = self.store.update_profile(**changes)
        recovery = self.store.recovery_txt_path if self.store else None
        return SetupSummary(
            dashboard_url=prof.serve_url if prof else None,
            recovery_file=recovery if recovery and recovery.exists() else None,
            backups_dir=self.env.app_data.backups_dir,
            profile=prof,
        )

    def first_backup(self, folder: Path | None = None,
                     progress: Callable[[int, int | None], None] | None = None
                     ) -> tuple[list[CheckResult], backups.BackupCheck | None]:
        client = self.tailnet_client() or self.usb_client()
        folder = folder or self.env.app_data.backups_dir
        try:
            check = backups.download_and_verify(
                client, folder, self.info.hostname if self.info else "weatherpi", progress)
        except (AgentError, OSError) as exc:
            return [_fail("backup_failed", "The backup didn't work. You can try again later "
                          "from the home screen.", str(exc), Action.RETRY)], None
        if not check.ok:
            return [_fail("backup_bad", "The backup was made but didn't pass its check.",
                          "; ".join(check.problems), Action.RETRY)], check
        self._update_profile(last_backup_at=format_time(self.env.now()))
        return [_ok("backup_ok", "Backup saved.", str(check.path), path=str(check.path))], check
