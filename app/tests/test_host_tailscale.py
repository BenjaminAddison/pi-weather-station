from __future__ import annotations

import io
import shutil

import pytest

from weatherpi_companion.agent_client import TailscaleStatus
from weatherpi_companion.host import tailscale as ts
from weatherpi_companion.host.runner import CommandResult, run_command

from .fakes import HOST_STATUS_NOSTATE, FakeClock, FakeRunner, host_status_json

CLI = r"C:\Program Files\Tailscale\tailscale.exe"


def make(runner: FakeRunner) -> ts.HostTailscale:
    return ts.HostTailscale(runner, cli=CLI)


def test_find_cli_windows_and_linux():
    assert ts.find_cli(which=lambda n: None, exists=lambda p: p == ts.WINDOWS_CLI,
                       platform="win32") == ts.WINDOWS_CLI
    assert ts.find_cli(which=lambda n: r"D:\x\tailscale.exe" if n == "tailscale.exe" else None,
                       exists=lambda p: False, platform="win32") == r"D:\x\tailscale.exe"
    assert ts.find_cli(which=lambda n: "/usr/bin/tailscale", exists=lambda p: False,
                       platform="linux") == "/usr/bin/tailscale"
    assert ts.find_cli(which=lambda n: None, exists=lambda p: False, platform="linux") is None
    assert ts.gui_path(exists=lambda p: True, platform="win32") == ts.WINDOWS_GUI
    assert ts.gui_path(exists=lambda p: True, platform="linux") is None


def test_not_installed():
    h = ts.HostTailscale(FakeRunner(), locate=lambda: None)
    assert not h.installed() and h.version() is None
    with pytest.raises(ts.TailscaleError) as ei:
        h.status()
    assert ei.value.code == "not_installed"


def test_version():
    r = FakeRunner().on(r"tailscale\.exe version$", "1.94.2\n  tailscale commit: abc\n")
    assert make(r).version() == "1.94.2"
    assert r.calls == [[CLI, "version"]]


def test_status_running_parsed():
    r = FakeRunner().on("status --json", host_status_json(), returncode=0)
    st = make(r).status()
    assert st.running and not st.needs_login
    assert st.backend_state == "Running"
    assert st.self_ipv4 == "100.64.0.5"
    assert st.self_dns_name == "desktop-test.example.ts.net"
    assert st.tailnet_name == "owner@example.com"
    assert st.magic_dns_suffix == "example.ts.net"
    assert len(st.peers) == 1
    p = st.peers[0]
    assert p.dns_name == "weatherpi.example.ts.net" and p.ipv4 == "100.94.2.31" and p.online


def test_status_real_nostate_capture_exit_1():
    # `tailscale status --json` exits 1 when not Running but still prints JSON
    r = FakeRunner().on("status --json", HOST_STATUS_NOSTATE, returncode=1)
    st = make(r).status()
    assert st.backend_state == "NoState" and not st.running
    assert st.peers == [] and st.self_ips == [] and st.tailnet_name is None
    assert st.health == ["Tailscale is starting. Please wait."]


def test_status_needs_login_with_auth_url():
    r = FakeRunner().on("status --json", host_status_json(
        "NeedsLogin", auth_url="https://login.tailscale.com/a/xyz"), returncode=1)
    st = make(r).status()
    assert st.needs_login and st.auth_url == "https://login.tailscale.com/a/xyz"


def test_status_daemon_down_and_garbage():
    r = FakeRunner().on("status --json", "", returncode=1,
                        stderr="failed to connect to local Tailscale service")
    with pytest.raises(ts.TailscaleError) as ei:
        make(r).status()
    assert ei.value.code == "not_running" and "local Tailscale" in ei.value.message
    r = FakeRunner().on("status --json", "not json")
    with pytest.raises(ts.TailscaleError) as ei:
        make(r).status()
    assert ei.value.code == "bad_output"


@pytest.mark.parametrize("out,ok,via,lat", [
    ("pong from weatherpi (100.94.2.31) via 192.168.7.9:41641 in 12ms\n", True, "direct", 12.0),
    (("pong from weatherpi (100.94.2.31) via DERP(jnb) in 80ms\n"
     "direct connection not established\n"), True, "DERP(jnb)", 80.0),
    ("ping \"100.94.2.31\" timed out\nno reply\n", False, None, None),
])
def test_ping_parsing(out, ok, via, lat):
    r = FakeRunner().on(" ping ", out, returncode=0 if ok and via == "direct" else 1)
    res = make(r).ping("100.94.2.31")
    assert (res.ok, res.via, res.latency_ms) == (ok, via, lat)
    argv = r.calls[0]
    assert argv[1:4] == ["ping", "--c", "1"] and argv[-1] == "100.94.2.31"
    assert "--until-direct=false" in argv


def status(**kw) -> ts.HostTailscaleStatus:
    import json
    return ts.HostTailscaleStatus.from_json(json.loads(host_status_json(**kw)))


def test_find_peer():
    st = status()
    assert ts.find_peer(st, "100.94.2.31").hostname == "weatherpi"
    assert ts.find_peer(st, "weatherpi.example.ts.net.").hostname == "weatherpi"
    assert ts.find_peer(st, "WEATHERPI").hostname == "weatherpi"
    assert ts.find_peer(st, "fd7a:115c:a1e0::1") is not None
    assert ts.find_peer(st, "other") is None
    assert ts.find_peer(st, "") is None


def pi(**kw) -> TailscaleStatus:
    base = {"backend_state": "Running", "tailnet": "example.ts.net",
            "tailnet_name": "owner@example.com", "ip": "100.94.2.31",
            "dns_name": "weatherpi.example.ts.net"}
    base.update(kw)
    return TailscaleStatus.from_json(base)


def test_same_tailnet_variants():
    host = status()
    assert ts.same_tailnet(host, pi()).reason == "peer_visible"
    m = ts.same_tailnet(status(peers=[]), pi())
    assert m.same and m.reason == "suffix_match"
    m = ts.same_tailnet(status(peers=[]), pi(tailnet="other.ts.net", ip="100.1.1.1",
                                             dns_name="weatherpi.other.ts.net"))
    assert not m.same and m.reason == "different" and m.pi_tailnet == "other.ts.net"
    m = ts.same_tailnet(status(peers=[], suffix=""), pi(tailnet=None, ip=None, dns_name=None))
    assert m.same and m.reason == "name_match"
    m = ts.same_tailnet(status(peers=[], suffix=""),
                        pi(tailnet=None, tailnet_name=None, ip=None, dns_name=None))
    assert not m.same and m.reason == "unknown"


class FakeProc:
    def __init__(self, lines):
        self.stdout = io.StringIO("".join(lines))
        self.args = None

    def poll(self):
        return None


def test_start_login_already_running():
    r = FakeRunner().on("status --json", host_status_json())
    res = ts.start_login(make(r), launcher=lambda a: pytest.fail("must not launch"))
    assert res.already_running and res.auth_url is None


def test_start_login_existing_auth_url():
    r = FakeRunner().on("status --json", host_status_json(
        "NeedsLogin", auth_url="https://login.tailscale.com/a/pending"), returncode=1)
    res = ts.start_login(make(r), launcher=lambda a: pytest.fail("must not launch"))
    assert res.auth_url == "https://login.tailscale.com/a/pending"


def test_start_login_reads_url_from_status_after_up():
    states = iter([host_status_json("NeedsLogin"), host_status_json("NeedsLogin"),
                   host_status_json("NeedsLogin", auth_url="https://login.tailscale.com/a/new")])
    r = FakeRunner().on_call("status --json", lambda a: CommandResult(1, next(states)))
    launched = []
    clock = FakeClock()
    res = ts.start_login(make(r), launcher=lambda a: launched.append(a) or FakeProc([]),
                         sleep=clock.sleep, clock=clock, up_timeout_s=300)
    assert launched == [[CLI, "up", "--timeout=300s"]]
    assert res.auth_url == "https://login.tailscale.com/a/new" and not res.already_running


def test_start_login_reads_url_from_process_output():
    r = FakeRunner().on("status --json", host_status_json("NeedsLogin"), returncode=1)
    proc = FakeProc(["\nTo authenticate, visit:\n\n\thttps://login.tailscale.com/a/fromout\n"])
    clock = FakeClock()

    def sleep(s):
        import time as _t
        _t.sleep(0.01)  # let the reader thread run
        clock.sleep(s)

    res = ts.start_login(make(r), launcher=lambda a: proc, sleep=sleep, clock=clock)
    assert res.auth_url == "https://login.tailscale.com/a/fromout"


def test_start_login_times_out_without_url():
    r = FakeRunner().on("status --json", host_status_json("NeedsLogin"), returncode=1)
    clock = FakeClock()
    res = ts.start_login(make(r), launcher=lambda a: FakeProc([]), sleep=clock.sleep,
                         clock=clock, wait_s=5)
    assert res.auth_url is None and not res.already_running


# --- optional read-only smoke test against the real CLI (skipped when absent) ----------


@pytest.mark.skipif(not (ts.find_cli() or shutil.which("tailscale")),
                    reason="Tailscale CLI not installed")
def test_smoke_real_tailscale_version_readonly():
    """Runs only `tailscale version` and `tailscale status --json` (both read-only)."""
    h = ts.HostTailscale(run_command)
    v = h.version()
    assert v and v[0].isdigit()
    try:
        st = h.status()
    except ts.TailscaleError:
        return  # daemon not running is fine for a smoke test
    assert st.backend_state


def test_gui_running_from_tasklist():
    running = FakeRunner().on(r"tasklist", '"tailscale-ipn.exe","5120","Console","1","40,112 K"\n')
    assert ts.gui_running(running, platform="win32") is True
    # the "no tasks" text is translated; only the image name matters
    stopped = FakeRunner().on(r"tasklist", "INFO: Es sind keine Tasks aktiv.\n")
    assert ts.gui_running(stopped, platform="win32") is False
    assert ts.gui_running(FakeRunner(), platform="win32") is None  # tasklist failed
    assert ts.gui_running(running, platform="linux") is None
