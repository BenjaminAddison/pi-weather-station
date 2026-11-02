"""``setup`` section: password, WiFi (+ saved networks with and without the W2b core
methods), Tailscale on both devices, final checks, done."""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QMessageBox

from weatherpi_companion.checks import CheckResult
from weatherpi_companion.host import usb as host_usb
from weatherpi_companion.host.tailscale import LoginStart
from weatherpi_companion.ui.sections import setup as setup_mod
from weatherpi_companion.ui.sections.setup import _checks, _networks, new_priorities

from .test_ui_fakes import SavedNetwork, make_ctx, record_opened, wait_idle


@pytest.fixture(autouse=True)
def opened(monkeypatch):
    return record_opened(monkeypatch)


def section(qtbot, ctx, start: int | None = 4):
    sec = setup_mod.SetupSection(ctx)
    qtbot.addWidget(sec)
    finished = []
    sec.finished.connect(finished.append)
    if start is not None:
        ctx.hints["setup_start"] = start
    sec.on_enter()
    wait_idle()
    return sec, finished


def found(s):
    assert s.detect_plug_in().next_step == "claim"


def claimed(s):
    found(s)
    assert s.create_password("correct horse", "correct horse")[0].ok


def online(s, w):
    claimed(s)
    s.connect_wifi("ExampleWiFi", "goodpass1")
    s.wait_for_wifi("ExampleWiFi")


# --------------------------------------------------------------------------- page 4


def test_password_gating_and_claim(qtbot, app_data):
    ctx, s, w = make_ctx(app_data)
    found(s)
    sec, _f = section(qtbot, ctx)
    page = sec.password
    assert sec.wizard.current is page
    assert not page.set_button.isEnabled() and not sec.wizard.next_button.isEnabled()
    page.password.setText("short")
    assert page.meter.text.text() == "Too short"
    assert not page.set_button.isEnabled()
    page.password.setText("correct horse")
    page.confirm.setText("correct hors")
    assert not page.set_button.isEnabled()
    assert "don't match" in page.problem.text()
    page.confirm.setText("correct horse")
    assert page.set_button.isEnabled() and not page.problem.isVisibleTo(page)
    assert page.meter.score >= 2
    page.confirm.returnPressed.emit()  # Enter submits
    wait_idle()
    assert page.checks.codes() == ["claimed", "time_set", "token_ok", "ssh_login"]
    assert w.agent.claimed and w.agent.owner_password == "correct horse"
    assert sec.wizard.next_button.isEnabled()
    assert page.password.text() == "" and not page.password.isEnabled()
    assert page._pw == ""
    assert "RECOVERY.txt" in page.recovery_note.text()


def test_password_ssh_failure_can_be_retried(qtbot, app_data):
    ctx, s, w = make_ctx(app_data)
    w.ssh_results = [CheckResult(False, "ssh_unreachable", "No SSH.", "", "retry")] * 3
    found(s)
    sec, _f = section(qtbot, ctx)
    page = sec.password
    page.password.setText("correct horse")
    page.confirm.setText("correct horse")
    page.submit()
    wait_idle()
    assert page.checks.codes()[-1] == "ssh_unreachable"
    assert not sec.wizard.next_button.isEnabled()
    page.checks.row("ssh_unreachable").action_button.click()  # Try again
    wait_idle()
    assert page.checks.codes()[-1] == "ssh_login"
    assert sec.wizard.next_button.isEnabled()


# --------------------------------------------------------------------------- page 5


def test_wifi_scan_connect_wrong_then_right(qtbot, app_data):
    ctx, s, _w = make_ctx(app_data)
    claimed(s)
    sec, _f = section(qtbot, ctx, start=5)
    page = sec.wifi
    assert sec.wizard.current is page
    assert not sec.wizard.back_button.isVisibleTo(sec)  # can't go back to the password
    assert [n.ssid for n in page.networks] == ["ExampleWiFi", "Neighbour"]
    assert page.list.count() == 3  # + Other / hidden network
    assert not sec.wizard.next_button.isEnabled()
    assert not page.connect_button.isEnabled()
    page.select("ExampleWiFi")
    page.password.setText("wrongpass1")
    assert page.connect_button.isEnabled()
    page.connect_wifi()
    wait_idle()
    assert page.checks.codes() == ["wifi_failed"]
    assert "wasn't accepted" in page.checks.rows[0].message.text()
    assert not sec.wizard.next_button.isEnabled()
    page.password.setText("goodpass1")
    page.password.returnPressed.emit()
    wait_idle()
    assert page.checks.codes() == ["wifi_connected", "wifi_internet"]
    assert sec.wizard.next_button.isEnabled()


def test_wifi_hidden_network(qtbot, app_data):
    ctx, s, w = make_ctx(app_data)
    claimed(s)
    sec, _f = section(qtbot, ctx, start=5)
    page = sec.wifi
    page.list.setCurrentRow(page.list.count() - 1)
    assert page.ssid.isVisibleTo(page) and page.hidden.isChecked()
    page.ssid.setText("HiddenExample")
    page.password.setText("whatever1")
    page.connect_wifi()
    wait_idle()
    connect = [r for r in w.agent.requests if r.url.path == "/v1/wifi/connect"][-1]
    assert b'"hidden":true' in connect.content.replace(b" ", b"")
    assert page.checks.codes() == ["wifi_failed"]  # the fake can't see it: "not found"
    assert "closer" in page.checks.rows[0].message.text()


def test_wifi_already_connected_enables_next(qtbot, app_data):
    ctx, s, w = make_ctx(app_data)
    online(s, w)
    sec, _f = section(qtbot, ctx, start=5)
    assert sec.wifi.checks.codes() == ["wifi_connected", "wifi_internet"]
    assert sec.wizard.next_button.isEnabled()


def test_new_password_for_the_network_in_use_reconnects(qtbot, app_data):
    """Changing the in-use network's password tests it right away instead of saving it
    quietly (a typo would otherwise only show up when the Pi next rejoins)."""
    ctx, s, w = make_ctx(app_data, saved_networks=True)  # ExampleWiFi is in use
    claimed(s)
    sec, _f = section(qtbot, ctx, start=5)
    panel = sec.more_wifi.saved
    sec.wizard.go_to(sec.more_wifi)
    panel.reload()
    wait_idle()
    panel.add_network("ExampleWiFi", "wrongpass1", False)
    wait_idle()
    assert not any(c[0] == "save_network" for c in s.calls)  # not saved quietly
    connects = [r for r in w.agent.requests if r.url.path == "/v1/wifi/connect"]
    assert len(connects) == 1 and b"wrongpass1" in connects[0].content
    assert sec.wizard.current is sec.wifi  # the result shows on the WiFi page
    assert sec.wifi.checks.codes() == ["wifi_failed"]
    # the right password connects; other networks are still just saved
    panel.add_network("ExampleWiFi", "goodpass1", False)
    wait_idle()
    assert sec.wifi.checks.codes() == ["wifi_connected", "wifi_internet"]
    panel.add_network("Holiday house", "holidaypw1", False)
    wait_idle()
    assert ("save_network", "Holiday house", "holidaypw1", False, -1) in s.calls


def test_saved_networks_pi_too_old(qtbot, app_data, monkeypatch):
    """A Pi on agent 2.0.0 has no /v1/wifi/saved: explain it rather than show an empty list."""
    ctx, s, _w = make_ctx(app_data, saved_networks=True)
    claimed(s)

    def too_old():
        s.saved_networks_problem = CheckResult(
            False, "wifi_saved_unsupported", "This WeatherPi's software is too old to keep "
            "several WiFi networks. It needs a newer SD card image.")
        return []

    monkeypatch.setattr(s, "saved_networks", too_old)
    sec, _f = section(qtbot, ctx, start=5)
    panel = sec.more_wifi.saved
    sec.wizard.go_to(sec.more_wifi)
    panel.reload()
    wait_idle()
    assert panel.stack.currentWidget() is panel.unsupported
    assert "newer SD card image" in panel.unsupported.text()
    assert not panel.up_button.isEnabled() and not panel.remove_button.isEnabled()


def test_saved_networks_add_reorder_remove(qtbot, app_data, monkeypatch):
    ctx, s, _w = make_ctx(app_data, saved_networks=True)
    s.saved.append(SavedNetwork("Phone hotspot", -1))
    claimed(s)
    sec, _f = section(qtbot, ctx, start=5)
    panel = sec.more_wifi.saved
    assert panel.supported
    sec.wizard.go_to(sec.more_wifi)
    wait_idle()
    assert [n.ssid for n in panel.networks] == ["ExampleWiFi", "Phone hotspot"]
    assert panel.list.item(0).text().startswith("1.  ExampleWiFi")
    assert "in use now" in panel.list.item(0).text()
    # add a network that isn't in range: goes to the bottom
    panel.add_network("Holiday house", "holidaypw1", True)
    wait_idle()
    assert ("save_network", "Holiday house", "holidaypw1", True, -2) in s.calls
    assert [n.ssid for n in panel.networks] == ["ExampleWiFi", "Phone hotspot",
                                                "Holiday house"]
    # move it up to the top
    panel.list.setCurrentRow(2)
    assert panel.up_button.isEnabled() and not panel.down_button.isEnabled()
    panel.move(-1)
    wait_idle()
    assert panel.list.currentRow() == 1
    panel.move(-1)
    wait_idle()
    assert [n.ssid for n in panel.networks] == ["Holiday house", "ExampleWiFi",
                                                "Phone hotspot"]
    prios = [n.priority for n in panel.networks]
    assert prios == sorted(prios, reverse=True) and all(-100 <= p <= 100 for p in prios)
    # remove one (confirmed)
    monkeypatch.setattr(QMessageBox, "question",
                        lambda *a, **k: QMessageBox.StandardButton.Yes)
    panel.list.setCurrentRow(2)
    panel.remove()
    wait_idle()
    assert ("forget_network", "Phone hotspot") in s.calls
    assert [n.ssid for n in panel.networks] == ["Holiday house", "ExampleWiFi"]
    # declined: nothing happens
    monkeypatch.setattr(QMessageBox, "question",
                        lambda *a, **k: QMessageBox.StandardButton.No)
    panel.list.setCurrentRow(0)
    panel.remove()
    wait_idle()
    assert len(panel.networks) == 2


def test_saved_networks_failure_is_shown(qtbot, app_data):
    ctx, s, _w = make_ctx(app_data, saved_networks=True)
    claimed(s)
    s.save_network = lambda *a, **k: [CheckResult(False, "wifi_save_failed", "Nope.",
                                                  action="retry")]
    sec, _f = section(qtbot, ctx, start=5)
    panel = sec.more_wifi.saved
    panel.reload()
    wait_idle()
    panel.add_network("X", None, False)
    wait_idle()
    assert panel.checks.codes() == ["wifi_save_failed"]


def test_new_priorities_and_result_adapters():
    assert new_priorities(0) == []
    assert new_priorities(1) == [0]
    assert new_priorities(3) == [20, 10, 0]
    many = new_priorities(40)
    assert many[0] <= 100 and many == sorted(many, reverse=True) and len(set(many)) == 40
    nets = [SavedNetwork("A")]
    ok = [CheckResult(True, "x", "ok")]
    assert _networks(nets) == (nets, [])
    assert _networks((ok, nets)) == (nets, ok)
    assert _networks(type("R", (), {"networks": nets, "checks": ok})()) == (nets, ok)
    assert _checks(ok) == ok and _checks(ok[0]) == ok and _checks(None) == []


# --------------------------------------------------------------------------- page 6


def test_host_tailscale_running(qtbot, app_data):
    ctx, s, w = make_ctx(app_data)
    online(s, w)
    sec, _f = section(qtbot, ctx, start=6)
    page = sec.host_ts
    assert page.checks.codes() == ["host_tailscale_running"]
    assert "owner@example.com" in page.account_text.text()
    assert sec.wizard.next_button.isEnabled()
    assert ctx.hints["host_account"] == "owner@example.com"


def test_host_tailscale_sign_in_opens_browser_and_waits(qtbot, app_data, opened):
    ctx, s, w = make_ctx(app_data)
    online(s, w)
    w.host_state["state"] = "NeedsLogin"
    sec, _f = section(qtbot, ctx, start=6)
    page = sec.host_ts
    assert page.checks.codes() == ["host_tailscale_logged_out"]
    assert not sec.wizard.next_button.isEnabled()
    btn = page.checks.rows[0].action_button
    assert btn.text() == "Sign in"

    def login(_ts):
        w.host_state["state"] = "Running"  # the owner signs in in the browser
        return LoginStart(False, "https://login.tailscale.com/a/host")

    s.env.host_login = login
    btn.click()
    wait_idle()
    assert opened == ["https://login.tailscale.com/a/host"]
    assert page.checks.codes() == ["host_tailscale_running"]
    assert sec.wizard.next_button.isEnabled()


def test_host_tailscale_starting_keeps_checking_by_itself(qtbot, app_data):
    ctx, s, w = make_ctx(app_data)
    online(s, w)
    w.host_state["state"] = "Starting"
    orig = s.check_host_tailscale
    calls = {"n": 0}

    def check():
        calls["n"] += 1
        if calls["n"] >= 3:
            w.host_state["state"] = "Running"
        return orig()

    s.check_host_tailscale = check
    sec, _f = section(qtbot, ctx, start=6)
    assert sec.host_ts.checks.codes() == ["host_tailscale_running"]
    assert calls["n"] >= 3 and sec.wizard.next_button.isEnabled()


def test_host_tailscale_not_started_starts_the_tray_app(qtbot, app_data, opened):
    """Tailscale taken out of the Windows startup apps: NoState until the tray app runs."""
    from weatherpi_companion.host import tailscale as host_ts
    from weatherpi_companion.host.runner import CommandResult

    ctx, s, w = make_ctx(app_data)
    online(s, w)
    s.env.platform, s.env.path_exists = "win32", lambda p: p == host_ts.WINDOWS_GUI
    w.host_state["state"] = "NoState"

    def tasklist(_argv):
        if host_ts.WINDOWS_GUI in opened:  # the page started it: the backend comes up
            w.host_state["state"] = "Running"
            return CommandResult(0, '"tailscale-ipn.exe","5120"\n')
        return CommandResult(0, "INFO: No tasks are running.\n")

    w.runner.on_call("tasklist", tasklist)
    sec, _f = section(qtbot, ctx, start=6)
    page = sec.host_ts
    assert opened == [host_ts.WINDOWS_GUI]  # once, by itself
    assert page.checks.codes() == ["host_tailscale_running"]
    assert sec.wizard.next_button.isEnabled()


# --------------------------------------------------------------------------- page 7


def _auto_approve(agent):
    """The owner approves in the browser as soon as the URL has been shown."""
    orig = agent.handle

    def handle(req):
        resp = orig(req)
        if req.url.path == "/v1/tailscale/status" and agent.ts_auth_url:
            agent.complete_tailscale_login()
        return resp

    agent.handle = handle


def test_pi_tailscale_login_same_tailnet_and_serve_retry(qtbot, app_data, opened):
    ctx, s, w = make_ctx(app_data)
    _auto_approve(w.agent)  # before any client exists (clients keep their transport)
    online(s, w)
    w.agent.serve_blocked = True
    sec, _f = section(qtbot, ctx, start=7)
    page = sec.pi_ts
    assert "same account" in page.explain.text()
    assert not sec.wizard.next_button.isEnabled()
    page.start_button.click()
    wait_idle()
    assert opened[0].startswith("https://login.tailscale.com/a/")
    assert page.checks.codes() == ["pi_tailscale_running", "same_tailnet", "serve_not_enabled"]
    assert page.checks.rows[0].message.text() == "The WeatherPi is connected to Tailscale."
    row = page.checks.row("serve_not_enabled")
    assert row.action_button.text() == "Open Tailscale settings"
    row.action_button.click()
    assert opened[-1] == "https://login.tailscale.com/f/serve?node=n1"
    assert page.retry_serve.isVisibleTo(page)
    assert not sec.wizard.next_button.isEnabled()
    w.agent.serve_blocked = False
    page.retry_serve.click()
    wait_idle()
    assert page.checks.codes() == ["pi_tailscale_running", "same_tailnet", "serve_ok"]
    assert sec.wizard.next_button.isEnabled()


def test_slow_publish_try_again_stays_on_the_page(qtbot, app_data):
    """A publish that times out is a slow Pi, not a lost cable: retry here."""
    ctx, s, w = make_ctx(app_data)
    _auto_approve(w.agent)
    online(s, w)
    w.agent.serve_timeouts = 1
    went = []
    ctx.navigator = went.append
    sec, _f = section(qtbot, ctx, start=7)
    page = sec.pi_ts
    page.start_button.click()
    wait_idle()
    assert page.checks.codes() == ["pi_tailscale_running", "same_tailnet", "serve"]
    row = page.checks.row("serve")
    assert "cable" not in row.message.text()
    row.action_button.click()  # Try again
    wait_idle()
    assert page.checks.codes() == ["pi_tailscale_running", "same_tailnet", "serve_ok"]
    assert sec.wizard.next_button.isEnabled() and went == []


def test_pi_tailscale_different_tailnet_offers_sign_in_again(qtbot, app_data):
    ctx, s, w = make_ctx(app_data)
    _auto_approve(w.agent)
    online(s, w)
    w.agent.complete_tailscale_login(tailnet="other.ts.net")
    w.agent.ts_ip = "100.1.1.1"
    sec, _f = section(qtbot, ctx, start=7)
    page = sec.pi_ts
    page.start()
    wait_idle()
    assert page.checks.codes() == ["pi_tailscale_running", "different_tailnet"]
    row = page.checks.row("different_tailnet")
    assert row.action_button.text() == "Sign in again"
    assert not sec.wizard.next_button.isEnabled()
    w.agent.tailnet, w.agent.ts_ip = "example.ts.net", "100.94.2.31"
    row.action_button.click()
    wait_idle()
    assert page.checks.codes()[-2:] == ["same_tailnet", "serve_ok"]
    assert sec.wizard.next_button.isEnabled()


def test_pi_tailscale_without_internet_points_back_to_wifi(qtbot, app_data):
    ctx, s, _w = make_ctx(app_data)
    claimed(s)  # no WiFi
    sec, _f = section(qtbot, ctx, start=7)
    sec.pi_ts.start()
    wait_idle()
    assert sec.pi_ts.checks.codes() == ["pi_no_internet"]
    sec.pi_ts._back_to_wifi(sec.pi_ts.checks.checks[0])
    wait_idle()
    assert sec.wizard.current is sec.wifi


# --------------------------------------------------------------------------- pages 8-9


def _tailscale_done(s, w):
    online(s, w)
    s.start_pi_tailscale()
    s.wait_for_pi_auth_url()
    w.agent.complete_tailscale_login()
    s.wait_for_pi_tailscale()
    s.enable_dashboard_serve()


def test_final_checks_and_optional_rain_test(qtbot, app_data):
    ctx, s, w = make_ctx(app_data)
    # the bucket tips on the 3rd sensor read: final checks, rain-test start, first poll
    orig = w.agent.handle
    seen = {"n": 0}

    def handle(req):
        if req.url.path == "/v1/sensors/live":
            seen["n"] += 1
            if seen["n"] == 3:
                w.agent.tip()
        return orig(req)

    w.agent.handle = handle
    _tailscale_done(s, w)
    w.agent.temp_present = False
    sec, _f = section(qtbot, ctx, start=8)
    page = sec.final
    assert page.checks.codes() == ["tailnet_ping", "agent_over_tailnet", "dashboard_ok",
                                   "logger_alive", "temperature_missing", "rain"]
    assert page.checks.row("temperature_missing").state == "warn"
    assert sec.wizard.next_button.isEnabled()  # a missing sensor doesn't block
    page.rain.start()
    wait_idle()
    assert page.rain.result.codes() == ["rain_tip_seen"]
    page.rain.skip()
    assert page.rain.result.codes() == ["rain_test_skipped"]


def test_final_checks_failures_block_next(qtbot, app_data):
    ctx, s, w = make_ctx(app_data)
    _tailscale_done(s, w)
    w.ping_ok = False
    w.dashboard_status = 502
    sec, _f = section(qtbot, ctx, start=8)
    assert "tailnet_ping_failed" in sec.final.checks.codes()
    assert not sec.wizard.next_button.isEnabled()
    w.ping_ok, w.dashboard_status = True, 200
    sec.final.again.click()
    wait_idle()
    assert sec.wizard.next_button.isEnabled()


def test_done_page_summary_backup_and_finish(qtbot, app_data, opened):
    ctx, s, w = make_ctx(app_data)
    _tailscale_done(s, w)
    sec, finished = section(qtbot, ctx, start=9)
    page = sec.done
    assert page.dash_url.text() == "https://weatherpi.example.ts.net"
    assert "RECOVERY.txt" in page.recovery_path.text()
    assert not sec.wizard.back_button.isVisibleTo(sec)
    assert s.store.load_profile().setup_complete
    page.open_dash.click()
    assert opened == ["https://weatherpi.example.ts.net"]
    page.backup()
    wait_idle()
    assert page.backup_result.codes() == ["backup_ok"]
    assert page.backup_button.text() == "Back up again"
    assert sec.wizard.next_button.text() == "Go to the home screen"
    sec.wizard.next()
    assert finished == ["home"]


# --------------------------------------------------------------------------- section


def test_setup_without_a_pi_found_looks_for_it_then_starts(qtbot, app_data):
    ctx, _s, _w = make_ctx(app_data)
    sec, _f = section(qtbot, ctx, start=None)  # e.g. --only setup
    assert sec.outer.currentWidget() is sec.wizard
    assert sec.wizard.current is sec.password


def test_setup_without_a_pi_points_to_the_tester(qtbot, app_data):
    ctx, _s, _w = make_ctx(app_data, usb_state=host_usb.UsbState.NOT_PLUGGED_IN)
    went = []
    ctx.navigator = went.append
    sec, _f = section(qtbot, ctx, start=None)
    assert sec.outer.currentWidget() is sec.connecting
    assert sec.connecting.checks.codes() == ["usb_not_found"] or \
        sec.connecting.checks.codes() == ["usb_not_plugged_in"]
    sec.connecting.tester_button.click()
    assert went == ["tester"]


def test_whole_setup_wizard_end_to_end(qtbot, app_data):
    ctx, s, w = make_ctx(app_data)
    found(s)
    _auto_approve(w.agent)
    sec, finished = section(qtbot, ctx)
    wiz = sec.wizard
    sec.password.password.setText("correct horse")
    sec.password.confirm.setText("correct horse")
    sec.password.submit()
    wait_idle()
    wiz.next()
    wait_idle()
    sec.wifi.select("ExampleWiFi")
    sec.wifi.password.setText("goodpass1")
    sec.wifi.connect_wifi()
    wait_idle()
    wiz.next()
    wait_idle()
    # optional: other networks for where it will live (D28); nothing added, so "Skip"
    assert wiz.current is sec.more_wifi and wiz.next_button.text() == "&Skip"
    wiz.next()
    wait_idle()
    assert wiz.current is sec.host_ts and wiz.next_button.isEnabled()
    wiz.next()
    wait_idle()
    sec.pi_ts.start()
    wait_idle()
    wiz.next()
    wait_idle()
    assert wiz.current is sec.final and wiz.next_button.isEnabled()
    wiz.next()
    wait_idle()
    assert wiz.current is sec.done
    captions = sec.step_caption
    wiz.next()
    assert finished == ["home"] and captions == "7 of 7: Done"
    assert s.store.load_profile().setup_complete


def test_more_wifi_page_is_optional_and_says_next_once_one_is_added(qtbot, app_data):
    ctx, s, _w = make_ctx(app_data, saved_networks=True)  # ExampleWiFi is in use
    claimed(s)
    sec, _f = section(qtbot, ctx, start=5)
    sec.wizard.go_to(sec.more_wifi)
    wait_idle()
    page = sec.more_wifi
    assert page.can_next() and page.next_label == "&Skip"
    assert "somewhere else" in page.title_label.text()
    assert page.saved.add_button.text() == "Add a WiFi network..."
    page.saved.add_network("Garden extender", "gardenpw1", False)
    wait_idle()
    assert ("save_network", "Garden extender", "gardenpw1", False, -1) in s.calls
    assert page.next_label == "&Next" and sec.wizard.next_button.text() == "&Next"


def test_page_numbers_skip_the_optional_page(qtbot, app_data):
    ctx, s, _w = make_ctx(app_data)
    claimed(s)
    sec, _f = section(qtbot, ctx, start=6)
    assert sec.wizard.current is sec.host_ts
    assert sec.page_for(5) is sec.wifi and sec.page_for(9) is sec.done
