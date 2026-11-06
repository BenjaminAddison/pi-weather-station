from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import paramiko
import pytest

from weatherpi_companion import flows
from weatherpi_companion.agent_client import AgentClient
from weatherpi_companion.checks import Action, CheckResult, all_ok, first_problem, poll
from weatherpi_companion.flows import HostEnv, SetupSession
from weatherpi_companion.host import driver as drv
from weatherpi_companion.host import tailscale as ts
from weatherpi_companion.host import usb as host_usb
from weatherpi_companion.host.runner import CommandResult
from weatherpi_companion.secrets import StationSecrets, generate_ssh_keypair

from .fakes import SERVE_ADMIN_URL, FakeAgent, FakeClock, FakeRunner, host_status_json

CLI = r"C:\Program Files\Tailscale\tailscale.exe"
HOST_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIHostKeyForTests"


@dataclass
class World:
    """Everything a test can poke: the fake Pi, host Tailscale state, recorded calls."""

    agent: FakeAgent
    clock: FakeClock
    runner: FakeRunner
    host_state: dict = field(default_factory=lambda: {"state": "Running",
                                                      "suffix": "example.ts.net"})
    ping_ok: bool = True
    dashboard_status: int | Exception = 200
    ssh_results: list[CheckResult] = field(default_factory=list)
    ssh_calls: list[tuple] = field(default_factory=list)
    driver: drv.DriverStatus = field(default_factory=lambda: drv.DriverStatus(
        drv.DriverState.OK, True))
    usb_state: host_usb.UsbState = host_usb.UsbState.CONNECTED
    dashboard_calls: list[str] = field(default_factory=list)
    broken_hosts: set[str] = field(default_factory=set)


def make_session(app_data, agent: FakeAgent | None = None, **world_kw) -> tuple[SetupSession,
                                                                                World]:
    agent = agent or FakeAgent()
    clock = FakeClock()
    runner = FakeRunner()
    w = World(agent, clock, runner, **world_kw)

    def status_cmd(argv):
        hs = w.host_state
        return CommandResult(0 if hs["state"] == "Running" else 1,
                             host_status_json(hs["state"], suffix=hs["suffix"],
                                              auth_url=hs.get("auth_url", "")))

    runner.on_call(r"status --json", status_cmd)
    runner.on_call(r" ping ", lambda a: CommandResult(
        0, "pong from weatherpi (100.94.2.31) via DERP(jnb) in 80ms\n") if w.ping_ok
        else CommandResult(1, "no reply\n"))
    runner.on(r" version$", "1.94.2\n")
    runner.on(r"tzutil", "South Africa Standard Time\n")

    def client_factory(url: str, token: str | None, via: str) -> AgentClient:
        host = httpx.URL(url).host
        if host in w.broken_hosts:
            def refuse(req):
                raise httpx.ConnectError("dns failure", request=req)
            return AgentClient(url, token, via=via, transport=httpx.MockTransport(refuse))
        return AgentClient(url, token, via=via, transport=agent.transport())

    def usb_detect():
        if w.usb_state != host_usb.UsbState.CONNECTED:
            return host_usb.UsbDetection(w.usb_state, detail="fake")
        info = client_factory("http://10.12.194.1:8765", None, "usb").info()
        return host_usb.UsbDetection(host_usb.UsbState.CONNECTED, "10.12.194.1", info)

    def ssh(host, key, **kw):
        w.ssh_calls.append((host, key, kw))
        if w.ssh_results:
            return w.ssh_results.pop(0)
        return CheckResult(True, "ssh_login", "ok", data={"host_key": HOST_KEY})

    def http_get(url, timeout):
        w.dashboard_calls.append(url)
        if isinstance(w.dashboard_status, Exception):
            raise w.dashboard_status
        return w.dashboard_status

    env = HostEnv(
        app_data=app_data, runner=runner, platform="win32", client_factory=client_factory,
        http_get=http_get, ssh_verifier=ssh, usb_detect=usb_detect,
        driver_status=lambda: w.driver, tailscale=ts.HostTailscale(runner, cli=CLI),
        sleep=clock.sleep, clock=clock,
    )
    return SetupSession(env), w


def codes(checks) -> list[str]:
    return [c.code for c in checks]


def claim(session: SetupSession) -> list[CheckResult]:
    assert session.detect_plug_in().next_step == "claim"
    return session.create_password("correct horse", "correct horse", owner_name="Alex")


def connect_wifi(session: SetupSession) -> list[CheckResult]:
    session.connect_wifi("ExampleWiFi", "goodpass1")
    return session.wait_for_wifi("ExampleWiFi")


# ------------------------------------------------------------------------ happy path


def test_full_wizard_happy_path(app_data):
    s, w = make_session(app_data)
    a = w.agent
    # page 2
    c = s.check_this_computer()
    assert all_ok(c) and codes(c) == ["driver_ok", "host_tailscale_installed"]
    assert c[1].data["version"] == "1.94.2"
    # page 3
    p = s.detect_plug_in()
    assert p.next_step == "claim" and all_ok(p.checks)
    assert codes(p.checks) == ["usb_adapter", "agent_info", "not_claimed"]
    # page 4
    c = s.create_password("correct horse", "correct horse", owner_name="Alex")
    assert all_ok(c), c
    assert codes(c) == ["claimed", "time_set", "token_ok", "ssh_login"]
    assert a.claimed and a.owner_password == "correct horse"
    assert a.timezone == "Africa/Johannesburg"  # from tzutil
    assert a.time_set_to is not None
    store = app_data.station(a.serial)
    secrets = store.load_secrets()
    assert secrets is not None and a.ssh_pubkey == secrets.ssh_key.public_openssh
    assert a.recovery_password == secrets.recovery_password
    assert secrets.recovery_password in store.recovery_txt_path.read_text(encoding="utf-8")
    prof = store.load_profile()
    assert prof.claimed_at and prof.owner_name == "Alex" and prof.ssh_host_key == HOST_KEY
    assert w.ssh_calls[0][0] == "10.12.194.1"
    # the owner's password is not on disk anywhere
    for f in store.dir.iterdir():
        assert b"correct horse" not in f.read_bytes()
    # page 5
    scan = s.scan_wifi()
    assert all_ok(scan.checks) and scan.networks[0].ssid == "ExampleWiFi"
    assert all_ok(s.connect_wifi("ExampleWiFi", "goodpass1"))
    progress = []
    c = s.wait_for_wifi("ExampleWiFi", on_progress=lambda v, t: progress.append(t))
    assert all_ok(c) and codes(c) == ["wifi_connected", "wifi_internet"]
    assert progress
    # page 6
    c = s.check_host_tailscale()
    assert all_ok(c) and c[0].data["tailnet"] == "example.ts.net"
    # page 7
    c = s.start_pi_tailscale()
    assert all_ok(c) and a.ts_up_called
    url = s.wait_for_pi_auth_url()
    assert url.code == "pi_login_url" and url.action == Action.OPEN_URL
    assert url.data["url"].startswith("https://login.tailscale.com/a/")
    a.complete_tailscale_login()
    c = s.wait_for_pi_tailscale()
    assert all_ok(c) and codes(c) == ["pi_tailscale_running", "same_tailnet"]
    c = s.enable_dashboard_serve()
    assert all_ok(c) and c[0].data["url"] == "https://weatherpi.example.ts.net"
    prof = store.load_profile()
    assert prof.tailnet_ip == "100.94.2.31" and prof.dns_name == "weatherpi.example.ts.net"
    assert prof.serve_url == "https://weatherpi.example.ts.net"
    # page 8: over tailscale
    n_before = len(a.requests)
    c = s.final_checks()
    assert all_ok(c), [x for x in c if not x.ok]
    assert codes(c) == ["tailnet_ping", "agent_over_tailnet", "dashboard_ok", "logger_alive",
                        "temperature", "rain"]
    tail_reqs = a.requests[n_before:]
    assert all(r.url.host == "weatherpi.example.ts.net" for r in tail_reqs)
    assert w.dashboard_calls == ["https://weatherpi.example.ts.net"]
    assert w.runner.ran(r"ping --c 1 .* 100\.94\.2\.31")
    rain = s.wait_for_rain_tip(on_progress=lambda v, t: a.tip() if t >= 2 else None)
    assert rain.ok and rain.code == "rain_tip_seen"
    # page 9
    summary = s.finish()
    assert summary.dashboard_url == "https://weatherpi.example.ts.net"
    assert summary.recovery_file == store.recovery_txt_path
    assert summary.profile.setup_complete
    c, check = s.first_backup()
    assert all_ok(c) and check.ok and check.path.parent == app_data.backups_dir
    assert store.load_profile().last_backup_at
    s.close()


# ------------------------------------------------------------------------ page 2


def test_prereqs_driver_missing_and_tailscale_missing(app_data):
    s, w = make_session(app_data, driver=drv.DriverStatus(drv.DriverState.MISSING, True, 28))
    s.env.tailscale = ts.HostTailscale(w.runner, locate=lambda: None)
    c = s.check_this_computer()
    assert not all_ok(c)
    assert [(x.code, x.action) for x in c] == [
        ("driver_missing", Action.INSTALL_DRIVER), ("host_tailscale_missing",
                                                    Action.INSTALL_TAILSCALE)]


def test_prereqs_driver_unknown_does_not_block(app_data):
    s, _ = make_session(app_data, driver=drv.DriverStatus(drv.DriverState.UNKNOWN, False))
    c = s.check_this_computer()
    assert all_ok(c) and c[0].code == "driver_unknown" and c[0].optional
    assert c[0].action == Action.INSTALL_DRIVER


def test_prereqs_driver_problem_and_not_needed(app_data):
    s, w = make_session(app_data, driver=drv.DriverStatus(drv.DriverState.PROBLEM, True, 10))
    assert s.check_this_computer()[0].code == "driver_problem"
    w.driver = drv.DriverStatus(drv.DriverState.NOT_NEEDED, False)
    assert s.check_this_computer()[0].code == "driver_not_needed"


def test_install_driver_paths(app_data, tmp_path):
    s, _w = make_session(app_data, driver=drv.DriverStatus(drv.DriverState.UNKNOWN, False))
    assert s.install_driver()[0].code == "driver_setup_missing"
    exe = tmp_path / "rpi-usb-gadget-driver-setup.exe"
    exe.write_bytes(b"MZ")
    s.env.driver_exe = exe
    calls = []
    s.env.driver_installer = lambda p: calls.append(p) or drv.InstallResult(0)
    c = s.install_driver()
    assert calls == [exe] and all_ok(c) and c[0].code == "driver_installed"
    s.env.driver_installer = lambda p: drv.InstallResult(3010)
    c = s.install_driver()
    assert codes(c) == ["driver_installed", "reboot_required"] and all_ok(c)

    def cancelled(p):
        raise drv.InstallError("cancelled", "no")

    s.env.driver_installer = cancelled
    c = s.install_driver()
    assert c[0].code == "driver_install_cancelled" and c[0].action == Action.INSTALL_DRIVER
    s.env.driver_installer = lambda p: drv.InstallResult(5)
    assert s.install_driver()[0].code == "driver_install_failed"


# ------------------------------------------------------------------------ page 3


@pytest.mark.parametrize("state,action", [
    (host_usb.UsbState.NOT_PLUGGED_IN, Action.PLUG_IN),
    (host_usb.UsbState.NO_DRIVER, Action.INSTALL_DRIVER),
    (host_usb.UsbState.DRIVER_PROBLEM, Action.INSTALL_DRIVER),
    (host_usb.UsbState.NO_NETWORK, Action.WAIT),
    (host_usb.UsbState.NO_AGENT, Action.WAIT),
    (host_usb.UsbState.BOOT_ROM, None),
])
def test_plug_in_not_ready(app_data, state, action):
    s, _ = make_session(app_data, usb_state=state)
    p = s.detect_plug_in()
    assert p.next_step == "wait" and not all_ok(p.checks)
    assert p.checks[0].code == f"usb_{state.value}" and p.checks[0].action == action
    assert p.checks[0].message_for_owner


def test_wait_for_plug_in_polls_until_connected(app_data):
    s, w = make_session(app_data, usb_state=host_usb.UsbState.NOT_PLUGGED_IN)
    seen = []

    def progress(value, elapsed):
        seen.append(elapsed)
        if elapsed >= 4:
            w.usb_state = host_usb.UsbState.CONNECTED

    p = s.wait_for_plug_in(timeout=60, on_progress=progress)
    assert p.next_step == "claim" and len(seen) >= 3


def test_wait_for_plug_in_timeout_and_cancel(app_data):
    s, w = make_session(app_data, usb_state=host_usb.UsbState.NOT_PLUGGED_IN)
    p = s.wait_for_plug_in(timeout=10)
    assert p.next_step == "wait" and p.checks[0].code == "usb_not_plugged_in"
    assert w.clock.t <= 10
    ev = threading.Event()
    ev.set()
    p = s.wait_for_plug_in(timeout=10, cancel=ev)
    assert p.next_step == "wait"


def test_plug_in_incompatible_agent(app_data):
    s, _ = make_session(app_data, FakeAgent(agent_version="3.0.0"))
    p = s.detect_plug_in()
    assert p.next_step == "wait" and p.checks[-1].code == "agent_version_mismatch"
    assert p.checks[-1].action == Action.UPDATE_APP


def test_plug_in_already_ours_resumes(app_data):
    s, w = make_session(app_data)
    claim(s)
    s2, _ = make_session(app_data, w.agent)
    p = s2.detect_plug_in()
    assert p.next_step == "resume" and p.resume_page == 5 and all_ok(p.checks)
    connect_wifi(s2)
    s3, _ = make_session(app_data, w.agent)
    assert s3.detect_plug_in().resume_page == 6


def test_plug_in_claimed_elsewhere_then_recover_with_file(app_data, tmp_path):
    other = FakeAgent()
    s, _ = make_session(app_data, other)
    claim(s)
    recovery_txt = tmp_path / "RECOVERY.txt"
    recovery_txt.write_text(app_data.station(other.serial).recovery_txt_path.read_text(
        encoding="utf-8"), encoding="utf-8")
    # a different PC: fresh app data
    from weatherpi_companion.secrets import AppData
    pc2 = AppData(tmp_path / "pc2").ensure()
    s2, _ = make_session(pc2, other)
    p = s2.detect_plug_in()
    assert p.next_step == "recover" and p.checks[-1].action == Action.RECOVER
    c = s2.recover_access("new password", "new password")
    assert c[0].code == "no_recovery_password"
    c = s2.recover_access("new password", "new password", recovery_password="wrong-" * 4)
    assert c[0].code == "bad_recovery"
    c = s2.recover_access("new password", "new password", recovery_file=recovery_txt)
    assert all_ok(c), c
    assert c[0].code == "recovered" and other.owner_password == "new password"
    new = pc2.station(other.serial).load_secrets()
    assert other.ssh_pubkey == new.ssh_key.public_openssh
    # the new PC's token works now
    s3, _ = make_session(pc2, other)
    assert s3.detect_plug_in().next_step == "resume"


def test_plug_in_stale_token_offers_recovery(app_data):
    s, w = make_session(app_data)
    claim(s)
    w.agent.token_sha = "0" * 64  # Pi re-keyed by someone else
    s2, _ = make_session(app_data, w.agent)
    p = s2.detect_plug_in()
    assert p.next_step == "recover"
    # recovery password from this PC's own store is used automatically
    c = s2.recover_access("new password", "new password")
    assert all_ok(c) and c[0].code == "recovered"


# ------------------------------------------------------------------------ page 4


def test_password_validation():
    assert codes(flows.validate_password("short", "short")) == ["password_too_short"]
    assert codes(flows.validate_password("longenough", "different")) == [
        "password_length", "password_mismatch"]
    assert codes(flows.validate_password("password", "password")) == ["password_too_common"]
    assert codes(flows.validate_password(" longenough", " longenough")) == [
        "password_length", "password_spaces"]
    assert all_ok(flows.validate_password("longenough", "longenough"))


@pytest.mark.parametrize("pw,score", [
    ("short", 0), ("aaaaaaaaaaaa", 1), ("password", 1), ("abcdefgh", 1), ("abcdefghij", 2),
    ("Abcdefgh1!", 3), ("Correct-Horse-Battery-9", 4),
])
def test_password_strength(pw, score):
    assert flows.password_strength(pw).score == score


def test_create_password_validation_does_not_claim(app_data):
    s, w = make_session(app_data)
    s.detect_plug_in()
    c = s.create_password("short", "short")
    assert not all_ok(c) and not w.agent.claimed
    assert not app_data.station(w.agent.serial).has_secrets()


def test_create_password_requires_plug_in(app_data):
    s, _ = make_session(app_data)
    assert s.create_password("longenough", "longenough")[0].code == "not_connected"


def test_create_password_retry_reuses_secrets_and_does_not_reclaim(app_data):
    s, w = make_session(app_data)
    w.ssh_results = [CheckResult(False, "ssh_unreachable", "x", action=Action.RETRY)] * 3
    c = claim(s)
    assert not all_ok(c) and first_problem(c).code == "ssh_unreachable"
    assert len(w.ssh_calls) == 3 and w.clock.sleeps.count(3.0) == 2  # retried
    claims = w.agent.paths().count("POST /v1/claim")
    c = s.create_password("correct horse", "correct horse")
    assert all_ok(c)
    assert w.agent.paths().count("POST /v1/claim") == claims


def test_claim_error_mapping(app_data):
    s, w = make_session(app_data)
    s.detect_plug_in()
    w.agent.claimed = True  # someone else claimed it in between
    c = s.create_password("correct horse", "correct horse")
    assert c[0].code == "already_claimed" and c[0].action == Action.RECOVER
    w.agent.claimed = False
    w.agent.down = True
    c = s.create_password("correct horse", "correct horse")
    assert c[0].code == "claim" and c[0].action == Action.PLUG_IN


def test_clock_not_pushed_when_synced(app_data):
    s, w = make_session(app_data, FakeAgent(clock_synced=True))
    c = claim(s)
    assert "time_set" not in codes(c) and w.agent.time_set_to is None


def test_ssh_host_key_change_is_reported(app_data):
    s, w = make_session(app_data)
    claim(s)
    w.ssh_results = [CheckResult(False, "ssh_host_key_changed", "x")]
    r = s.check_ssh()
    assert r.code == "ssh_host_key_changed"
    assert w.ssh_calls[-1][2]["expected_host_key"] == HOST_KEY



def test_claim_after_reflash_forgets_the_old_card(app_data):
    """Same Pi (serial), new card: this PC still remembers the old card's identity."""
    s, w = make_session(app_data)
    app_data.station(w.agent.serial).update_profile(
        ssh_host_key="ssh-ed25519 OLDCARD", tailnet_ip="100.64.0.9",
        dns_name="weatherpi-1.example.ts.net", tailnet="example.ts.net",
        serve_url="https://weatherpi-1.example.ts.net", setup_complete=True)
    c = claim(s)
    assert all_ok(c), c
    assert w.ssh_calls[-1][2]["expected_host_key"] is None  # nothing to compare against
    prof = app_data.station(w.agent.serial).load_profile()
    assert prof.ssh_host_key == HOST_KEY  # the new card's, pinned again
    assert (prof.tailnet_ip, prof.dns_name, prof.tailnet, prof.serve_url) == (None,) * 4
    assert not prof.setup_complete


# ------------------------------------------------------------------------ page 5


def test_wifi_wrong_password(app_data):
    s, _ = make_session(app_data)
    claim(s)
    s.connect_wifi("ExampleWiFi", "badpassword")
    c = s.wait_for_wifi("ExampleWiFi")
    assert c[0].code == "wifi_failed" and c[0].data["error"] == "wrong_password"
    assert "password" in c[0].message_for_owner and c[0].action == Action.CHOOSE_WIFI


def test_wifi_not_found(app_data):
    s, _ = make_session(app_data)
    claim(s)
    s.connect_wifi("HiddenNet", "whatever123", hidden=True)
    c = s.wait_for_wifi("HiddenNet")
    assert c[0].data["error"] == "not_found" and c[0].action == Action.MOVE_CLOSER


def test_wifi_timeout(app_data):
    s, w = make_session(app_data)
    claim(s)
    w.agent.wifi_polls_to_connect = 10_000
    s.connect_wifi("ExampleWiFi", "goodpass1")
    c = s.wait_for_wifi("ExampleWiFi", timeout=20)
    assert c[0].code == "wifi_failed" and c[0].data["error"] == "timeout"
    assert w.clock.t <= 20


def test_wifi_no_internet(app_data):
    s, w = make_session(app_data)
    claim(s)
    w.agent.wifi_internet = False
    c = connect_wifi(s)
    assert codes(c) == ["wifi_connected", "wifi_no_internet"] and not all_ok(c)


def test_wifi_cancel(app_data):
    s, w = make_session(app_data)
    claim(s)
    w.agent.wifi_polls_to_connect = 10_000
    s.connect_wifi("ExampleWiFi", "goodpass1")
    ev = threading.Event()
    c = s.wait_for_wifi("ExampleWiFi", cancel=ev, on_progress=lambda v, t: ev.set())
    assert c[0].code == "cancelled"


def test_wifi_input_validation(app_data):
    s, _ = make_session(app_data)
    assert s.connect_wifi("", "x")[0].code == "wifi_no_ssid"
    assert s.connect_wifi("Net", "")[0].code == "wifi_password_missing"
    assert s.connect_wifi("Net", "short")[0].code == "wifi_password_length"
    assert s.connect_wifi("Net", "x" * 64)[0].code == "wifi_password_length"


def test_wifi_scan_empty_is_optional(app_data):
    s, w = make_session(app_data)
    claim(s)
    w.agent.wifi_networks = []
    r = s.scan_wifi()
    assert r.networks == [] and r.checks[0].code == "wifi_none_found" and all_ok(r.checks)


def test_wifi_scan_unreachable(app_data):
    s, w = make_session(app_data)
    claim(s)
    w.agent.down = True
    assert s.scan_wifi().checks[0].action == Action.PLUG_IN


# ------------------------------------------------------------------------ saved networks


def test_saved_networks_flow(app_data):
    s, w = make_session(app_data)
    claim(s)
    assert all_ok(connect_wifi(s))
    nets = s.saved_networks()
    assert nets == [flows.SavedNetwork("ExampleWiFi", 0, False, True)]
    assert s.saved == nets and s.saved_networks_problem is None
    # add a hotspot that isn't in range: saved, current WiFi untouched
    c = s.save_network("Example Phone", "goodpass1", priority=5)
    assert all_ok(c) and codes(c) == ["wifi_saved"]
    assert "can't check the password" in c[0].message_for_owner
    assert [n.ssid for n in c[0].data["networks"]] == ["Example Phone", "ExampleWiFi"]
    assert w.agent.wifi_state == "connected" and w.agent.wifi_ssid == "ExampleWiFi"
    # reorder
    c = s.set_network_priority("ExampleWiFi", 10)
    assert all_ok(c) and codes(c) == ["wifi_priority_set"]
    assert [(n.ssid, n.priority) for n in c[0].data["networks"]] == [
        ("ExampleWiFi", 10), ("Example Phone", 5)]
    # forget the one not in use
    c = s.forget_network("Example Phone")
    assert all_ok(c) and codes(c) == ["wifi_forgotten"] and not c[0].data["was_in_use"]
    assert [n.ssid for n in s.saved] == ["ExampleWiFi"]


def test_save_network_open_and_hidden(app_data):
    s, w = make_session(app_data)
    claim(s)
    assert all_ok(s.save_network("Cafe", None))
    assert all_ok(s.save_network("Library", ""))
    assert all_ok(s.save_network("Secret", "goodpass1", hidden=True, priority=-2))
    assert w.agent.saved_meta["Cafe"]["password"] is None
    assert w.agent.saved_meta["Library"]["password"] is None
    assert w.agent.saved_meta["Secret"] == {"priority": -2, "hidden": True,
                                            "password": "goodpass1"}


def test_save_network_input_validation(app_data):
    s, w = make_session(app_data)
    claim(s)
    before = len(w.agent.requests)
    assert s.save_network("", "goodpass1")[0].code == "wifi_no_ssid"
    assert s.save_network("Net", "short")[0].code == "wifi_password_length"
    assert s.save_network("Net", "x" * 64)[0].code == "wifi_password_length"
    for bad in (101, -101, True, 1.5):
        c = s.save_network("Net", "goodpass1", priority=bad)
        assert c[0].code == "wifi_priority_range" and not all_ok(c)
        assert s.set_network_priority("Net", bad)[0].code == "wifi_priority_range"
    assert len(w.agent.requests) == before  # nothing was sent


def test_save_network_rejected_by_pi(app_data):
    s, w = make_session(app_data)
    claim(s)

    def reject(method, path, body):  # the Pi's own checks are the last word
        return w.agent._err(400, "bad_request", "WiFi passwords are 8-63 characters")

    w.agent._saved_endpoint = reject
    c = s.save_network("Net", "goodpass1")
    assert c[0].code == "wifi_save_rejected" and "8 to 63" in c[0].message_for_owner
    assert c[0].action == Action.CHOOSE_WIFI


def test_saved_network_busy_while_connecting(app_data):
    s, w = make_session(app_data)
    claim(s)
    s.save_network("Example Phone", "goodpass1")
    w.agent.wifi_polls_to_connect = 10_000
    s.connect_wifi("ExampleWiFi", "goodpass1")
    for c in (s.save_network("Other", "goodpass1"),
              s.set_network_priority("Example Phone", 3),
              s.forget_network("Example Phone")):
        assert c[0].code == "wifi_busy" and not all_ok(c)
        assert "busy joining a WiFi network" in c[0].message_for_owner
        assert c[0].action == Action.RETRY


def test_set_priority_not_saved(app_data):
    s, _ = make_session(app_data)
    claim(s)
    c = s.set_network_priority("Nope", 1)
    assert c[0].code == "wifi_not_saved" and not all_ok(c)


def test_forget_network_in_use_and_missing(app_data):
    s, _ = make_session(app_data)
    claim(s)
    connect_wifi(s)
    c = s.forget_network("ExampleWiFi")
    assert all_ok(c) and c[0].data["was_in_use"]
    assert "off WiFi" in c[0].message_for_owner
    assert c[0].data["networks"] == []
    c = s.forget_network("ExampleWiFi")  # already gone: fine
    assert all_ok(c) and c[0].code == "wifi_forgotten"


def test_saved_networks_unreachable_and_old_agent(app_data):
    s, w = make_session(app_data)
    claim(s)
    w.agent.saved_supported = False  # a Pi still on agent 2.0.0
    assert s.saved_networks() == []
    assert s.saved_networks_problem.code == "wifi_saved_unsupported"
    assert s.save_network("Net", "goodpass1")[0].code == "wifi_saved_unsupported"
    assert s.set_network_priority("Net", 1)[0].code == "wifi_saved_unsupported"
    w.agent.saved_supported = True
    assert s.saved_networks() == [] and s.saved_networks_problem is None
    w.agent.down = True
    assert s.saved_networks() == []
    assert s.saved_networks_problem.action == Action.PLUG_IN
    assert s.save_network("Net", "goodpass1")[0].action == Action.PLUG_IN
    assert s.forget_network("Net")[0].action == Action.PLUG_IN


# ------------------------------------------------------------------------ page 6


@pytest.mark.parametrize("state,code,action", [
    ("NeedsLogin", "host_tailscale_logged_out", Action.HOST_TAILSCALE_LOGIN),
    ("Stopped", "host_tailscale_stopped", Action.HOST_TAILSCALE_LOGIN),
    ("NoState", "host_tailscale_starting", Action.WAIT),
    ("Starting", "host_tailscale_starting", Action.WAIT),
])
def test_host_tailscale_states(app_data, state, code, action):
    s, w = make_session(app_data)
    w.host_state["state"] = state
    c = s.check_host_tailscale()
    assert c[0].code == code and c[0].action == action


def _up_switches_on(w) -> None:
    def up(_argv):
        w.host_state["state"] = "Running"
        return CommandResult(0, "")

    w.runner.rules.insert(0, (r"tailscale\.exe up$", up))


def test_host_tailscale_switched_off_is_switched_back_on(app_data):
    s, w = make_session(app_data)
    w.host_state["state"] = "Stopped"
    _up_switches_on(w)
    c = s.check_host_tailscale()
    assert all_ok(c) and c[0].code == "host_tailscale_running"
    assert c[0].data["switched_on"] and "switched it back on" in c[0].message_for_owner
    assert [CLI, "up"] in w.runner.calls
    assert s.check_host_tailscale()[0].data["switched_on"] is False  # already on now


def test_host_tailscale_not_switched_on_when_signed_out(app_data):
    s, w = make_session(app_data)
    w.host_state["state"] = "NeedsLogin"
    _up_switches_on(w)
    assert s.check_host_tailscale()[0].code == "host_tailscale_logged_out"
    assert not w.runner.ran(r" up")


def test_host_tailscale_switch_on_fails(app_data):
    s, w = make_session(app_data)
    w.host_state["state"] = "Stopped"
    w.runner.rules.insert(0, (r"tailscale\.exe up$", CommandResult(1, "", "access denied")))
    c = s.check_host_tailscale()
    assert c[0].code == "host_tailscale_stopped" and c[0].action == Action.HOST_TAILSCALE_LOGIN
    assert "access denied" in c[0].detail


def _tray(w, running: bool) -> None:
    out = '"tailscale-ipn.exe","5120"\n' if running else "INFO: No tasks are running.\n"
    w.runner.rules.insert(0, ("tasklist", CommandResult(0, out)))


def test_host_tailscale_nostate_without_tray_app_offers_to_start_it(app_data):
    """Windows: the tray app starts the backend; without it NoState never ends."""
    s, w = make_session(app_data)
    s.env.platform, s.env.path_exists = "win32", lambda p: p == ts.WINDOWS_GUI
    w.host_state["state"] = "NoState"
    _tray(w, running=False)
    c = s.check_host_tailscale()
    assert c[0].code == "host_tailscale_not_started"
    assert c[0].action == Action.OPEN_TAILSCALE_APP and c[0].data["path"] == ts.WINDOWS_GUI


def test_host_tailscale_nostate_with_tray_app_running_waits(app_data):
    s, w = make_session(app_data)
    s.env.platform, s.env.path_exists = "win32", lambda p: True
    w.host_state["state"] = "NoState"
    _tray(w, running=True)
    c = s.check_host_tailscale()
    assert c[0].code == "host_tailscale_starting" and c[0].action == Action.WAIT


def test_host_tailscale_daemon_down(app_data):
    s, w = make_session(app_data)
    w.runner.rules.insert(0, ("status --json", CommandResult(1, "", "failed to connect")))
    c = s.check_host_tailscale()
    assert c[0].code == "host_tailscale_not_running"


def test_start_host_login_variants(app_data):
    s, _w = make_session(app_data)
    s.env.host_login = lambda h: ts.LoginStart(False, "https://login.tailscale.com/a/host1")
    r = s.start_host_login()
    assert r.action == Action.OPEN_URL and r.data["url"].endswith("/a/host1")
    s.env.host_login = lambda h: ts.LoginStart(True, None)
    assert s.start_host_login().ok
    s.env.host_login = lambda h: ts.LoginStart(False, None)
    assert s.start_host_login().code == "host_login_no_url"


def test_wait_for_host_tailscale(app_data):
    s, w = make_session(app_data)
    w.host_state["state"] = "NeedsLogin"

    def progress(v, t):
        if t >= 4:
            w.host_state["state"] = "Running"

    c = s.wait_for_host_tailscale(on_progress=progress)
    assert all_ok(c) and c[0].code == "host_tailscale_running"


# ------------------------------------------------------------------------ page 7


def ready_for_tailscale(app_data, **kw):
    s, w = make_session(app_data, **kw)
    claim(s)
    connect_wifi(s)
    return s, w


def test_pi_tailscale_no_internet(app_data):
    s, _w = make_session(app_data)
    claim(s)
    c = s.start_pi_tailscale()
    assert c[0].code == "pi_no_internet" and c[0].action == Action.CHOOSE_WIFI


def test_pi_tailscale_different_tailnet_then_relogin(app_data):
    s, w = ready_for_tailscale(app_data)
    s.start_pi_tailscale()
    s.wait_for_pi_auth_url()
    w.agent.ts_ip = "100.1.2.3"
    w.agent.complete_tailscale_login(tailnet="someone-else.ts.net")
    c = s.wait_for_pi_tailscale()
    assert codes(c) == ["pi_tailscale_running", "different_tailnet"]
    assert c[1].action == Action.PI_TAILSCALE_RELOGIN and "someone-else" in c[1].detail
    assert all_ok(s.pi_tailscale_logout()) and w.agent.ts_state == "NeedsLogin"


def test_pi_tailscale_already_running(app_data):
    s, w = ready_for_tailscale(app_data)
    w.agent.complete_tailscale_login()
    c = s.start_pi_tailscale()
    assert c[0].code == "pi_tailscale_running" and not w.agent.ts_up_called
    assert s.wait_for_pi_auth_url().code == "pi_tailscale_running"


def test_pi_auth_url_timeout(app_data):
    s, w = ready_for_tailscale(app_data)
    w.agent.ts_polls_to_url = 10_000
    s.start_pi_tailscale()
    r = s.wait_for_pi_auth_url(timeout=5)
    assert r.code == "pi_login_url_timeout"


def test_pi_tailscale_login_not_finished(app_data):
    s, _w = ready_for_tailscale(app_data)
    s.start_pi_tailscale()
    c = s.wait_for_pi_tailscale(timeout=10)
    assert c[0].code == "pi_tailscale_not_running" and c[0].data["auth_url"]


def test_serve_timeout_is_a_slow_pi_not_a_lost_cable(app_data):
    s, w = ready_for_tailscale(app_data)
    w.agent.complete_tailscale_login()
    s.wait_for_pi_tailscale()
    w.agent.serve_timeouts = 1
    c = s.enable_dashboard_serve()
    assert c[0].code == "serve" and c[0].action == Action.RETRY
    assert "cable" not in c[0].message_for_owner
    assert all_ok(s.enable_dashboard_serve())


def test_serve_timeout_but_pi_finished_publishing(app_data):
    s, w = ready_for_tailscale(app_data)
    w.agent.complete_tailscale_login()
    s.wait_for_pi_tailscale()
    w.agent.serve_timeouts, w.agent.serve_late = 1, True
    c = s.enable_dashboard_serve()
    assert all_ok(c) and c[0].code == "serve_ok"
    assert s.store.load_profile().serve_url == "https://weatherpi.example.ts.net"


def test_serve_timeout_while_serve_is_off_on_the_tailnet(app_data):
    # what a real Pi does: `tailscale serve` waits ~20 s for approval, so the request times
    # out; the Pi's status then says Serve is off, which needs the owner, not a new cable
    s, w = ready_for_tailscale(app_data)
    w.agent.complete_tailscale_login()
    s.wait_for_pi_tailscale()
    w.agent.serve_blocked = True
    w.agent.serve_timeouts, w.agent.serve_late = 1, True
    c = s.enable_dashboard_serve()
    assert c[0].code == "serve_not_enabled" and c[0].action == Action.OPEN_URL
    assert c[0].data["url"] == SERVE_ADMIN_URL


def test_serve_not_enabled_gives_admin_url(app_data):
    s, w = ready_for_tailscale(app_data)
    w.agent.complete_tailscale_login()
    s.wait_for_pi_tailscale()
    w.agent.serve_blocked = True
    c = s.enable_dashboard_serve()
    assert c[0].code == "serve_not_enabled" and c[0].action == Action.OPEN_URL
    assert c[0].data["url"] == SERVE_ADMIN_URL
    w.agent.serve_blocked = False
    assert all_ok(s.enable_dashboard_serve())


# ------------------------------------------------------------------------ page 8


def ready_for_final(app_data, **kw):
    s, w = ready_for_tailscale(app_data, **kw)
    w.agent.complete_tailscale_login()
    s.wait_for_pi_tailscale()
    s.enable_dashboard_serve()
    return s, w


def test_final_checks_failures(app_data):
    s, w = ready_for_final(app_data, ping_ok=False, dashboard_status=502)
    w.agent.temp_present = False
    w.agent.logger_alive = False
    c = s.final_checks(ping_attempts=2)
    by = {x.code: x for x in c}
    assert by["tailnet_ping_failed"].blocking
    assert by["dashboard_http_error"].blocking and "502" in by["dashboard_http_error"].detail
    assert by["logger_not_alive"].action == Action.REBOOT
    assert by["temperature_missing"].optional and not by["temperature_missing"].blocking
    assert w.runner.calls.count(w.runner.calls[-1]) >= 1


def test_final_checks_dashboard_exception(app_data):
    s, _w = ready_for_final(app_data, dashboard_status=httpx.ConnectError("ssl"))
    c = s.final_checks()
    assert "dashboard_unreachable" in codes(c)


def test_final_checks_magicdns_fallback_to_ip(app_data):
    s, w = ready_for_final(app_data)
    w.broken_hosts.add("weatherpi.example.ts.net")
    c = s.final_checks()
    assert all_ok(c), c
    ok = next(x for x in c if x.code == "agent_over_tailnet")
    assert ok.detail == "http://100.94.2.31:8765"


def test_final_checks_agent_unreachable_over_tailnet(app_data):
    s, w = ready_for_final(app_data)
    w.broken_hosts |= {"weatherpi.example.ts.net", "100.94.2.31"}
    c = s.final_checks()
    assert "agent_tailnet_unreachable" in codes(c) and "sensors_unknown" in codes(c)


def test_final_checks_switch_host_tailscale_back_on(app_data):
    s, w = ready_for_final(app_data)
    w.host_state["state"] = "Stopped"  # switched off after page 6
    _up_switches_on(w)
    c = s.final_checks()
    assert all_ok(c), c
    assert c[0].code == "host_tailscale_switched_on" and c[0].ok
    assert [CLI, "up"] in w.runner.calls


def test_final_checks_needs_tailnet_ip(app_data):
    s, _ = make_session(app_data)
    assert s.final_checks()[0].code == "no_tailnet_ip"


def test_rain_tip_not_seen_is_optional(app_data):
    s, _ = ready_for_final(app_data)
    r = s.wait_for_rain_tip(timeout=5)
    assert r.code == "rain_tip_not_seen" and r.optional
    ev = threading.Event()
    ev.set()
    assert s.wait_for_rain_tip(timeout=5, cancel=ev).code == "rain_test_skipped"


def test_first_backup_failure(app_data):
    s, w = ready_for_final(app_data)
    w.agent.down = True
    c, check = s.first_backup()
    assert c[0].code == "backup_failed" and check is None


# ------------------------------------------------------------------------ helpers


def test_guess_timezone(monkeypatch):
    monkeypatch.delenv("TZ", raising=False)
    r = FakeRunner().on("tzutil", "South Africa Standard Time\r\n")
    assert flows.guess_timezone(r, "win32") == "Africa/Johannesburg"
    assert flows.guess_timezone(FakeRunner().on("tzutil", "Mars Time"), "win32") is None
    monkeypatch.setenv("TZ", "Europe/London")
    assert flows.guess_timezone(FakeRunner(), "win32") == "Europe/London"


def test_check_dashboard():
    assert flows.check_dashboard(lambda u, t: 200, "https://x").ok
    assert flows.check_dashboard(lambda u, t: 200, None).code == "dashboard_no_url"


def test_poll_helper_swallows_errors_then_succeeds():
    clock = FakeClock()
    calls = iter([RuntimeError("blip"), 1, 2, 3])

    def fetch():
        v = next(calls)
        if isinstance(v, Exception):
            raise v
        return v

    res = poll(fetch, lambda v: v >= 2, timeout=30, interval=1, sleep=clock.sleep, clock=clock)
    assert res.done and res.value == 2 and res.attempts == 3


def test_poll_helper_real_cancel_wait_is_prompt():
    ev = threading.Event()
    threading.Timer(0.05, ev.set).start()
    res = poll(lambda: 0, lambda v: False, timeout=30, interval=10, cancel=ev)
    assert res.reason == "cancelled"


# ------------------------------------------------------------------------ verify_ssh_login


class FakeKey:
    def get_name(self):
        return "ssh-ed25519"

    def get_base64(self):
        return "AAAAC3NzaC1lZDI1NTE5AAAAIHostKeyForTests"


class FakeChannel:
    def __init__(self, code):
        self.code = code

    def recv_exit_status(self):
        return self.code


class FakeStdout:
    def __init__(self, code):
        self.channel = FakeChannel(code)


class FakeSSHClient:
    def __init__(self, fail: Exception | None = None, exit_code: int = 0):
        self.fail, self.exit_code = fail, exit_code
        self.connected_with: dict | None = None
        self.closed = False
        self.commands: list[str] = []

    def set_missing_host_key_policy(self, policy):
        self.policy = policy

    def connect(self, host, **kw):
        if self.fail:
            raise self.fail
        self.connected_with = {"host": host, **kw}
        self.policy.missing_host_key(self, host, FakeKey())

    def get_transport(self):
        return None

    def exec_command(self, cmd, timeout=None):
        self.commands.append(cmd)
        return None, FakeStdout(self.exit_code), None

    def close(self):
        self.closed = True


def test_verify_ssh_login_success():
    kp = generate_ssh_keypair("t")
    fake = FakeSSHClient()
    r = flows.verify_ssh_login("10.12.194.1", kp.private_openssh, client_factory=lambda: fake)
    assert r.ok and r.data["host_key"] == HOST_KEY
    assert fake.connected_with["username"] == "main" and fake.connected_with["host"] == (
        "10.12.194.1")
    assert isinstance(fake.connected_with["pkey"], paramiko.Ed25519Key)
    assert fake.connected_with["look_for_keys"] is False and fake.commands == ["true"]
    assert fake.closed


def test_verify_ssh_login_failures():
    kp = generate_ssh_keypair("t")
    r = flows.verify_ssh_login("h", kp.private_openssh, client_factory=lambda: FakeSSHClient(
        paramiko.AuthenticationException("denied")))
    assert r.code == "ssh_auth_failed"
    r = flows.verify_ssh_login("h", kp.private_openssh, client_factory=lambda: FakeSSHClient(
        OSError("refused")))
    assert r.code == "ssh_unreachable"
    r = flows.verify_ssh_login("h", kp.private_openssh, expected_host_key="ssh-ed25519 OTHER",
                               client_factory=lambda: FakeSSHClient())
    assert r.code == "ssh_host_key_changed"
    r = flows.verify_ssh_login("h", kp.private_openssh,
                               client_factory=lambda: FakeSSHClient(exit_code=1))
    assert r.code == "ssh_command_failed" and r.optional
    r = flows.verify_ssh_login("h", b"not a key", client_factory=lambda: FakeSSHClient())
    assert r.code == "ssh_key_unreadable"


def test_env_defaults_construct(app_data):
    env = HostEnv(app_data=app_data)
    assert isinstance(env.tailscale, ts.HostTailscale)
    assert StationSecrets and Path  # imported for type use
