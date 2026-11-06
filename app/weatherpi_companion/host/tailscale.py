"""Tailscale on this computer (the host), driven through the ``tailscale`` CLI.

All functions take an injectable ``runner`` (see :mod:`.runner`), so tests never run the
real CLI.

Logging in a logged-out host
----------------------------
* **Windows:** the Tailscale GUI (``tailscale-ipn.exe``, tray icon) normally handles login;
  :func:`gui_path` finds it so the UI can offer "Open Tailscale". The CLI also works:
* **CLI (all platforms):** :func:`start_login` launches ``tailscale up --timeout=<n>s`` in
  the background (it blocks until the login finishes or the timeout passes), then polls
  ``tailscale status --json`` until ``AuthURL`` appears and returns it, so the app can
  open it in the browser. It also scans the process output for the URL. If the host is
  merely ``Stopped`` (logged in but disconnected) ``tailscale up`` just reconnects and no
  URL is needed. On Linux ``tailscale up`` needs root unless the user is the Tailscale
  *operator* (``sudo tailscale set --operator=$USER``).

Switched off
------------
Tailscale can be switched off on this PC (tray icon "Disconnect", ``tailscale down``) while
it stays signed in: ``BackendState`` is then ``Stopped``. The app's checks switch it back on
by themselves with :func:`ensure_running` (a bare ``tailscale up``), because the WeatherPi is
only reachable over the tailnet (D23).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .runner import CommandResult, Runner, run_command

WINDOWS_CLI = r"C:\Program Files\Tailscale\tailscale.exe"
WINDOWS_GUI = r"C:\Program Files\Tailscale\tailscale-ipn.exe"
LOGIN_URL_RE = re.compile(r"https://login\.tailscale\.com/\S+")


# --------------------------------------------------------------------------- locating


def find_cli(which: Callable[[str], str | None] = shutil.which,
             exists: Callable[[str], bool] = os.path.isfile,
             platform: str = sys.platform) -> str | None:
    """Path to the tailscale CLI, or None if it isn't installed."""
    if platform == "win32":
        candidates = [WINDOWS_CLI]
        pf = os.environ.get("ProgramFiles")
        if pf:
            candidates.append(str(Path(pf) / "Tailscale" / "tailscale.exe"))
        for c in candidates:
            if exists(c):
                return c
        return which("tailscale.exe") or which("tailscale")
    return which("tailscale")


def gui_path(exists: Callable[[str], bool] = os.path.isfile,
             platform: str = sys.platform) -> str | None:
    """The Windows Tailscale tray app, which handles login itself."""
    if platform == "win32" and exists(WINDOWS_GUI):
        return WINDOWS_GUI
    return None


def gui_running(runner: Runner = run_command, platform: str = sys.platform) -> bool | None:
    """Is the Windows tray app running? None if we can't tell (or not Windows).

    On Windows the tray app is what starts the Tailscale backend after a reboot: without it
    ``tailscale status`` stays at ``NoState`` ("Tailscale is starting. Please wait.") for
    good, e.g. when Tailscale was taken out of the Windows startup apps.
    """
    if platform != "win32":
        return None
    res = runner(["tasklist.exe", "/FI", "IMAGENAME eq tailscale-ipn.exe", "/FO", "CSV",
                  "/NH"], 15)
    if not res.ok:
        return None
    # "No tasks are running" is translated, but the image name never is
    return "tailscale-ipn.exe" in res.stdout.lower()


# --------------------------------------------------------------------------- status model


@dataclass
class TailscalePeer:
    id: str
    hostname: str
    dns_name: str  # without trailing dot
    ips: list[str]
    os: str
    online: bool
    last_seen: str | None = None

    @classmethod
    def from_json(cls, key: str, d: dict[str, Any]) -> TailscalePeer:
        return cls(
            id=str(d.get("ID") or key),
            hostname=str(d.get("HostName") or ""),
            dns_name=str(d.get("DNSName") or "").rstrip("."),
            ips=[str(x) for x in (d.get("TailscaleIPs") or [])],
            os=str(d.get("OS") or ""),
            online=bool(d.get("Online", False)),
            last_seen=d.get("LastSeen") or None,
        )

    @property
    def ipv4(self) -> str | None:
        return next((ip for ip in self.ips if ":" not in ip), None)


@dataclass
class HostTailscaleStatus:
    backend_state: str  # NoState | NeedsLogin | NeedsMachineAuth | Starting | Running | Stopped
    auth_url: str | None
    self_ips: list[str]
    self_dns_name: str | None
    self_hostname: str | None
    tailnet_name: str | None  # CurrentTailnet.Name (often the owner's email)
    magic_dns_suffix: str | None
    version: str | None
    health: list[str]
    peers: list[TailscalePeer] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> HostTailscaleStatus:
        self_ = d.get("Self") if isinstance(d.get("Self"), dict) else {}
        tn = d.get("CurrentTailnet") if isinstance(d.get("CurrentTailnet"), dict) else {}
        peers_raw = d.get("Peer") if isinstance(d.get("Peer"), dict) else {}
        suffix = (tn.get("MagicDNSSuffix") or d.get("MagicDNSSuffix") or "").rstrip(".")
        ips = self_.get("TailscaleIPs") or d.get("TailscaleIPs") or []
        return cls(
            backend_state=str(d.get("BackendState") or "NoState"),
            auth_url=d.get("AuthURL") or None,
            self_ips=[str(x) for x in ips],
            self_dns_name=(self_.get("DNSName") or "").rstrip(".") or None,
            self_hostname=self_.get("HostName") or None,
            tailnet_name=tn.get("Name") or None,
            magic_dns_suffix=suffix or None,
            version=d.get("Version") or None,
            health=[str(h) for h in (d.get("Health") or [])],
            peers=[TailscalePeer.from_json(k, v) for k, v in peers_raw.items()
                   if isinstance(v, dict)],
            raw=d,
        )

    @property
    def running(self) -> bool:
        return self.backend_state == "Running"

    @property
    def needs_login(self) -> bool:
        return self.backend_state in ("NeedsLogin", "NeedsMachineAuth")

    @property
    def self_ipv4(self) -> str | None:
        return next((ip for ip in self.self_ips if ":" not in ip), None)


class TailscaleError(Exception):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.message = message


# --------------------------------------------------------------------------- CLI wrappers


class HostTailscale:
    """Wraps the host's tailscale CLI. ``cli`` overrides the located path."""

    def __init__(self, runner: Runner = run_command, cli: str | None = None,
                 locate: Callable[[], str | None] = find_cli) -> None:
        self.runner = runner
        self._cli = cli
        self._locate = locate

    @property
    def cli(self) -> str | None:
        if self._cli is None:
            self._cli = self._locate()
        return self._cli

    def _run(self, *args: str, timeout: float = 15.0) -> CommandResult:
        cli = self.cli
        if not cli:
            raise TailscaleError("not_installed", "Tailscale is not installed on this computer")
        return self.runner([cli, *args], timeout)

    def installed(self) -> bool:
        return self.cli is not None

    def version(self) -> str | None:
        """Short version, e.g. ``1.94.2`` (first line of ``tailscale version``)."""
        if not self.installed():
            return None
        res = self._run("version", timeout=10)
        if not res.ok or not res.stdout.strip():
            return None
        return res.stdout.strip().splitlines()[0].strip()

    def status(self) -> HostTailscaleStatus:
        """Parsed ``tailscale status --json``.

        Raises TailscaleError("not_running") when tailscaled can't be reached.
        Note ``tailscale status`` exits 1 when not Running but ``--json`` still prints.
        """
        import json

        res = self._run("status", "--json", timeout=15)
        text = res.stdout.strip()
        if not text:
            detail = (res.stderr or "").strip()
            raise TailscaleError("not_running", detail or "tailscale status printed nothing")
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise TailscaleError("bad_output", f"could not parse status JSON: {exc}")
        if not isinstance(data, dict):
            raise TailscaleError("bad_output", "status JSON is not an object")
        return HostTailscaleStatus.from_json(data)

    def ping(self, target: str, timeout_s: int = 10) -> PingResult:
        """``tailscale ping --c 1`` one peer. A DERP-relayed pong counts as success."""
        res = self._run("ping", "--c", "1", "--until-direct=false",
                        f"--timeout={timeout_s}s", target, timeout=timeout_s + 10)
        return parse_ping(res)

    def switch_on(self, timeout_s: float = 30.0) -> CommandResult:
        """Switch a signed-in but ``Stopped`` Tailscale back on: a bare ``tailscale up``.

        No flags at all: a bare ``up`` only reconnects, while any flag (even ``--timeout``)
        makes the CLI refuse when settings were changed in the Tailscale app ("requires
        mentioning all non-default flags"). It returns once the backend is Running.
        """
        return self._run("up", timeout=timeout_s)

    def up_args(self, timeout_s: int) -> list[str]:
        cli = self.cli
        if not cli:
            raise TailscaleError("not_installed", "Tailscale is not installed on this computer")
        return [cli, "up", f"--timeout={timeout_s}s"]


@dataclass
class PingResult:
    ok: bool
    via: str | None  # "direct" | "DERP(jnb)" | ...
    latency_ms: float | None
    output: str


_PONG_RE = re.compile(r"pong from \S+ \(([^)]*)\)(?: via (\S+))?(?: in ([\d.]+)ms)?")


def parse_ping(res: CommandResult) -> PingResult:
    out = (res.stdout or "") + (res.stderr or "")
    m = _PONG_RE.search(out)
    if not m:
        return PingResult(False, None, None, out.strip())
    via_raw = m.group(2) or ""
    via = via_raw if via_raw.startswith("DERP") else ("direct" if via_raw else None)
    latency = float(m.group(3)) if m.group(3) else None
    return PingResult(True, via, latency, out.strip())


# --------------------------------------------------------------------------- switched off


@dataclass
class EnsureResult:
    status: HostTailscaleStatus
    switched_on: bool = False  # it was Stopped and the app switched it back on
    output: str = ""  # what ``tailscale up`` printed, when it was run


def ensure_running(ts: HostTailscale,
                   status: HostTailscaleStatus | None = None) -> EnsureResult:
    """Status of Tailscale on this PC, switching it back on first if it was switched off.

    Only ``Stopped`` is touched: signed out (``NeedsLogin``) needs the owner, and ``NoState``
    is the Windows tray app's job. Raises TailscaleError like :meth:`HostTailscale.status`.
    """
    st = status if status is not None else ts.status()
    if st.backend_state != "Stopped":
        return EnsureResult(st)
    res = ts.switch_on()
    out = ((res.stdout or "") + (res.stderr or "")).strip()
    try:
        new = ts.status()
    except TailscaleError:
        return EnsureResult(st, output=out)
    return EnsureResult(new, switched_on=new.backend_state != "Stopped", output=out)


# --------------------------------------------------------------------------- helpers


def _norm(name: str | None) -> str:
    return (name or "").strip().rstrip(".").lower()


def find_peer(status: HostTailscaleStatus, target: str) -> TailscalePeer | None:
    """Find a peer by MagicDNS name (full or short hostname) or Tailscale IP."""
    t = _norm(target)
    if not t:
        return None
    for p in status.peers:
        if t in (ip.lower() for ip in p.ips):
            return p
    for p in status.peers:
        if t == _norm(p.dns_name):
            return p
    for p in status.peers:
        short = _norm(p.dns_name).split(".")[0] if p.dns_name else _norm(p.hostname)
        if t == short:
            return p
    return None


@dataclass
class TailnetMatch:
    same: bool
    reason: str  # "peer_visible" | "suffix_match" | "name_match" | "different" | "unknown"
    host_tailnet: str | None
    pi_tailnet: str | None


def same_tailnet(host: HostTailscaleStatus, pi_status: Any) -> TailnetMatch:
    """Are this PC and the Pi on the same tailnet?

    ``pi_status`` is the agent's :class:`~weatherpi_companion.agent_client.TailscaleStatus`
    (anything with ``tailnet``, ``tailnet_name``, ``ip``, ``dns_name`` attributes).
    Strongest evidence first: the Pi's IP/DNS name is in the host's peer list, then the
    MagicDNS suffix matches, then the tailnet name matches.
    """
    pi_suffix = _norm(getattr(pi_status, "tailnet", None))
    pi_name = _norm(getattr(pi_status, "tailnet_name", None))
    host_suffix = _norm(host.magic_dns_suffix)
    host_name = _norm(host.tailnet_name)
    pi_ip = getattr(pi_status, "ip", None)
    pi_dns = getattr(pi_status, "dns_name", None)
    for target in (pi_ip, pi_dns):
        if target and find_peer(host, target):
            return TailnetMatch(True, "peer_visible", host.magic_dns_suffix, pi_suffix or None)
    if host_suffix and pi_suffix:
        if host_suffix == pi_suffix:
            return TailnetMatch(True, "suffix_match", host.magic_dns_suffix, pi_suffix)
        return TailnetMatch(False, "different", host.magic_dns_suffix, pi_suffix)
    if host_name and pi_name:
        same = host_name == pi_name
        return TailnetMatch(same, "name_match" if same else "different",
                            host.tailnet_name, pi_name)
    return TailnetMatch(False, "unknown", host.magic_dns_suffix, pi_suffix or None)


# --------------------------------------------------------------------------- login


Launcher = Callable[[Sequence[str]], Any]


def _popen(args: Sequence[str]) -> subprocess.Popen:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    return subprocess.Popen(
        list(args), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        text=True, encoding="utf-8", errors="replace", creationflags=flags,
    )


@dataclass
class LoginStart:
    already_running: bool
    auth_url: str | None
    process: Any = None  # the background ``tailscale up`` (may still be waiting)


def start_login(
    ts: HostTailscale,
    *,
    up_timeout_s: int = 600,
    wait_s: float = 20.0,
    poll_s: float = 1.0,
    launcher: Launcher = _popen,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    cancel: threading.Event | None = None,
) -> LoginStart:
    """Start logging this PC in to Tailscale and return the login URL to open.

    Never call this from tests with the default launcher: it really runs ``tailscale up``.
    Returns ``already_running=True`` if no login was needed, or ``auth_url=None`` if the
    URL did not appear within ``wait_s`` (the UI should then point at the Tailscale app).
    """
    st = ts.status()
    if st.running:
        return LoginStart(True, None)
    if st.auth_url:
        return LoginStart(False, st.auth_url)
    proc = launcher(ts.up_args(up_timeout_s))
    found: list[str] = []

    def _reader() -> None:
        out = getattr(proc, "stdout", None)
        if out is None:
            return
        try:
            for line in out:
                m = LOGIN_URL_RE.search(line)
                if m:
                    found.append(m.group(0))
                    return
        except (OSError, ValueError):
            return

    threading.Thread(target=_reader, daemon=True).start()
    deadline = clock() + wait_s
    while clock() < deadline:
        if cancel is not None and cancel.is_set():
            break
        if found:
            return LoginStart(False, found[0], proc)
        try:
            st = ts.status()
        except TailscaleError:
            st = None
        if st is not None:
            if st.running:
                return LoginStart(True, None, proc)
            if st.auth_url:
                return LoginStart(False, st.auth_url, proc)
        sleep(poll_s)
    return LoginStart(False, found[0] if found else None, proc)
