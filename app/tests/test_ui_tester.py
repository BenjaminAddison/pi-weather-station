"""``tester`` section: this computer, plug in (live detection, boot ROM, resume,
recovery), ready summary."""

from __future__ import annotations

import time

import pytest

from weatherpi_companion.host import driver as drv
from weatherpi_companion.host import usb as host_usb
from weatherpi_companion.secrets import StationSecrets
from weatherpi_companion.ui import worker
from weatherpi_companion.ui.sections import tester as tester_mod

from .test_ui_fakes import make_ctx, record_opened, wait_idle


@pytest.fixture(autouse=True)
def opened(monkeypatch):
    return record_opened(monkeypatch)


def section(qtbot, ctx):
    sec = tester_mod.TesterSection(ctx)
    qtbot.addWidget(sec)
    finished = []
    sec.finished.connect(finished.append)
    sec.on_enter()
    wait_idle()
    return sec, finished


def advance(sec):
    sec.wizard.next()
    wait_idle()


def test_happy_path_to_ready(qtbot, app_data):
    ctx, s, _w = make_ctx(app_data)
    sec, finished = section(qtbot, ctx)
    wiz = sec.wizard
    assert wiz.current is sec.need and wiz.next_button.isEnabled()
    advance(sec)
    assert wiz.current is sec.computer
    assert sec.computer.checks.codes() == ["driver_ok", "host_tailscale_installed"]
    assert wiz.next_button.isEnabled()
    advance(sec)
    assert wiz.current is sec.plug_in
    assert sec.plug_in.checks.codes() == ["usb_adapter", "agent_info", "not_claimed"]
    assert wiz.next_button.isEnabled()
    assert ctx.hints["setup_start"] == 4
    assert sec.plug_in.diagram.isHidden()  # found: no need for the picture any more
    advance(sec)
    assert wiz.current is sec.ready
    assert sec.ready.title_label.text() == "Your WeatherPi is working and ready for setup"
    assert sec.ready.fields["serial"].text() == "00000000abcd1234"
    assert sec.ready.fields["image"].text() == "2026.10.19-1"
    assert wiz.next_button.text() == "Continue to setup"
    assert sec.step_caption == "4 of 4: Ready"
    wiz.next()
    assert finished == ["setup"]
    assert ctx.session is s  # shared with setup


def test_computer_problems_block_next_and_offer_fixes(qtbot, app_data, opened):
    ctx, s, w = make_ctx(app_data, driver=drv.DriverStatus(drv.DriverState.MISSING, True, 28))
    s.env.tailscale._cli = None
    s.env.tailscale._locate = lambda: None
    sec, _f = section(qtbot, ctx)
    advance(sec)
    page = sec.computer
    assert page.checks.codes() == ["driver_missing", "host_tailscale_missing"]
    assert not sec.wizard.next_button.isEnabled()
    rows = page.checks.rows
    assert rows[0].action_button.text() == "Install driver"
    assert rows[1].action_button.text() == "Install Tailscale"
    rows[1].action_button.click()
    assert opened == ["https://tailscale.com/download/windows"]
    assert not page.note.isHidden()
    # install the driver (fake installer; the driver then shows as OK)
    s.env.driver_exe = app_data.home / "rpi-usb-gadget-driver-setup.exe"
    installed = []

    def fake_install(exe):
        installed.append(exe)
        w.driver = drv.DriverStatus(drv.DriverState.OK, True)
        return drv.InstallResult(0)

    s.env.driver_installer = fake_install
    page.checks.rows[0].action_button.click()
    wait_idle()
    assert installed and page.checks.codes()[0] == "driver_ok"
    assert not sec.wizard.next_button.isEnabled()  # Tailscale still missing


def test_install_driver_without_bundled_setup_says_so(qtbot, app_data):
    ctx, _s, _w = make_ctx(app_data, driver=drv.DriverStatus(drv.DriverState.MISSING, True))
    sec, _f = section(qtbot, ctx)
    advance(sec)
    sec.computer.install_driver()
    wait_idle()
    assert sec.computer.checks.codes()[0] == "driver_setup_missing"


def _real_time(s):
    s.env.sleep = None
    s.env.clock = time.monotonic


def test_plug_in_keeps_looking_live_and_stops_when_leaving(qtbot, app_data):
    ctx, s, w = make_ctx(app_data, usb_state=host_usb.UsbState.NOT_PLUGGED_IN)
    _real_time(s)
    sec, _f = section(qtbot, ctx)
    sec.wizard.go_to(sec.plug_in)
    page = sec.plug_in
    qtbot.waitUntil(lambda: page.checks.codes() == ["usb_not_plugged_in"], timeout=5000)
    assert page.busy.active and not sec.wizard.next_button.isEnabled()
    assert not page.diagram.isHidden()
    # plug it in: the running wait picks it up by itself
    w.usb_state = host_usb.UsbState.CONNECTED
    qtbot.waitUntil(lambda: page.complete, timeout=8000)
    assert page.checks.codes()[-1] == "not_claimed"
    assert sec.wizard.next_button.isEnabled()


def test_leaving_plug_in_cancels_the_wait(qtbot, app_data):
    ctx, s, _w = make_ctx(app_data, usb_state=host_usb.UsbState.NOT_PLUGGED_IN)
    _real_time(s)
    sec, _f = section(qtbot, ctx)
    sec.wizard.go_to(sec.plug_in)
    qtbot.waitUntil(lambda: bool(worker.live_tasks()), timeout=2000)
    t0 = time.monotonic()
    sec.on_leave()
    wait_idle(5)
    assert time.monotonic() - t0 < 4
    assert not worker.live_tasks()


def test_stop_looking_shows_look_again(qtbot, app_data):
    ctx, s, _w = make_ctx(app_data, usb_state=host_usb.UsbState.NOT_PLUGGED_IN)
    _real_time(s)
    sec, _f = section(qtbot, ctx)
    sec.wizard.go_to(sec.plug_in)
    page = sec.plug_in
    qtbot.waitUntil(lambda: page.busy.active, timeout=2000)
    page.busy.cancel_button.click()
    wait_idle(5)
    assert not page.again.isHidden() and not page.busy.active
    sec.on_leave()


def test_boot_rom_says_reseat_or_reflash_and_offers_the_flasher(qtbot, app_data):
    ctx, s, _w = make_ctx(app_data, usb_state=host_usb.UsbState.BOOT_ROM)
    _real_time(s)
    went = []
    ctx.navigator = went.append
    sec, _f = section(qtbot, ctx)
    sec.wizard.go_to(sec.plug_in)
    page = sec.plug_in
    qtbot.waitUntil(lambda: page.checks.codes() == ["usb_boot_rom"], timeout=5000)
    row = page.checks.rows[0]
    assert "re-seat" in row.message.text() and "flashed again" in row.message.text()
    assert row.action_button.text() == "Flash the SD card again"
    row.action_button.click()
    assert went == ["flasher"]
    sec.on_leave()
    wait_idle(5)


def test_already_ours_resumes(qtbot, app_data):
    ctx, s, _w = make_ctx(app_data)
    # set it up once (as an earlier run of the app would have)
    assert s.detect_plug_in().next_step == "claim"
    s.create_password("correct horse", "correct horse")
    s.connect_wifi("ExampleWiFi", "goodpass1")
    s.wait_for_wifi("ExampleWiFi")
    s._claimed_here = False  # a new app run
    sec, _f = section(qtbot, ctx)
    sec.wizard.go_to(sec.plug_in)
    wait_idle()
    assert sec.plug_in.checks.codes()[-1] == "already_ours"
    assert ctx.hints["setup_start"] == 6
    advance(sec)
    assert sec.ready.title_label.text() == "Your WeatherPi is working"
    assert "WiFi already works" in sec.ready.intro_label.text()


def test_claimed_elsewhere_offers_recovery(qtbot, app_data):
    ctx, _s, w = make_ctx(app_data)
    rp = StationSecrets.generate().recovery_password
    w.agent.claimed = True
    w.agent.recovery_password = rp
    sec, _f = section(qtbot, ctx)
    sec.wizard.go_to(sec.plug_in)
    wait_idle()
    page = sec.plug_in
    assert page.checks.codes()[-1] == "claimed_elsewhere"
    assert not page.recovery.isHidden()
    assert not sec.wizard.next_button.isEnabled()
    panel = page.recovery
    assert not panel.go.isEnabled()
    panel.recovery.setText("wrong-wrong-wrong-wrong-wrong")
    panel.password.setText("new password 1")
    panel.confirm.setText("new password 1")
    assert panel.go.isEnabled()
    panel.submit()
    wait_idle()
    assert page.checks.codes() == ["bad_recovery"]
    assert not sec.wizard.next_button.isEnabled()
    panel.recovery.setText(rp)
    panel.submit()
    wait_idle()
    assert page.checks.codes()[0] == "recovered"
    assert sec.wizard.next_button.isEnabled()
    assert ctx.hints["setup_start"] == 5 and ctx.hints["recovered"]
    advance(sec)
    assert "back in control" in sec.ready.title_label.text()


def test_recovery_file_can_be_loaded(qtbot, app_data, tmp_path):
    ctx, _s, w = make_ctx(app_data)
    w.agent.claimed = True
    sec, _f = section(qtbot, ctx)
    f = tmp_path / "RECOVERY.txt"
    f.write_text("Recovery password:  abcde-fghjk-mnpqr-stuvw-xyz23\n", encoding="utf-8")
    sec.plug_in.recovery.load_file(f)
    assert sec.plug_in.recovery.recovery.text() == "abcde-fghjk-mnpqr-stuvw-xyz23"
