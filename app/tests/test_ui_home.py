"""Home screen (W9): status, measurement interval (D26), maintenance mode (D14)."""

from __future__ import annotations

import pytest

from weatherpi_companion.flows import HostEnv
from weatherpi_companion.ui.context import AppContext
from weatherpi_companion.ui.sections import home as home_mod

from .fakes import FakeAgent
from .test_station import setup_station
from .test_ui_fakes import record_opened, wait_idle


@pytest.fixture
def opened(monkeypatch):
    return record_opened(monkeypatch)


def make_home(qtbot, app_data, agent: FakeAgent, **kw):
    mon, _ = setup_station(app_data, agent, **kw)
    ctx = AppContext(home=app_data.home, env=mon.env)
    sec = home_mod.HomeSection(ctx)
    qtbot.addWidget(sec)
    sec.show()
    sec.on_enter()
    wait_idle()
    return sec


def test_interval_text():
    t = home_mod.interval_text
    assert [t(s) for s in (60, 300, 600, 900, 1800, 3600)] == [
        "minute", "5 minutes", "10 minutes", "15 minutes", "30 minutes", "hour"]
    assert t(90) == "90 seconds"


def test_status_online(qtbot, app_data):
    sec = make_home(qtbot, app_data, FakeAgent())
    assert "online" in sec.status_line.text()
    assert sec.values["link"].text() == "Tailscale"
    assert "tips" in sec.values["rain"].text()
    assert sec.values["temp"].text().endswith("°C")
    assert sec.dashboard_button.isVisible()
    assert sec.problems.isHidden()


def test_change_interval(qtbot, app_data):
    agent = FakeAgent()
    sec = make_home(qtbot, app_data, agent)
    assert sec.interval.isEnabled() and sec.interval.currentData() == 300
    assert not sec.save_button.isEnabled()  # nothing changed yet
    sec.interval.setCurrentIndex(sec.interval.findData(900))
    assert sec.save_button.isEnabled()
    sec.save_button.click()
    wait_idle()
    assert agent.record_interval_s == 900
    assert "every 15 minutes" in sec.measure_note.text()
    assert not sec.save_button.isEnabled()


def test_old_agent_explains_instead_of_offering(qtbot, app_data):
    agent = FakeAgent()
    agent.has_settings = agent.has_maintenance = False
    sec = make_home(qtbot, app_data, agent)
    assert not sec.interval.isEnabled() and not sec.save_button.isEnabled()
    assert "too old" in sec.measure_note.text()
    assert not sec.maint_button.isEnabled()
    assert "doesn't have maintenance mode" in sec.maint_state.text()


def test_maintenance_on_and_reminder(qtbot, app_data):
    agent = FakeAgent()
    sec = make_home(qtbot, app_data, agent)
    assert "off" in sec.maint_state.text() and sec.maint_banner.isHidden()
    asked = []
    sec.confirm = lambda _w, title, _t: asked.append(title) or True
    sec.maint_button.click()
    wait_idle()
    assert asked == ["Turn on maintenance mode?"] and agent.maintenance_on
    assert "restarting" in sec.maint_state.text() and not sec.maint_button.isEnabled()
    # after the restart
    agent.overlay_active = False
    sec.refresh()
    wait_idle()
    assert "ON" in sec.maint_state.text() and not sec.maint_banner.isHidden()
    assert sec.maint_button.text() == "Turn off maintenance mode"


def test_maintenance_cancelled(qtbot, app_data):
    agent = FakeAgent()
    sec = make_home(qtbot, app_data, agent)
    sec.confirm = lambda *_a: False
    sec.maint_button.click()
    wait_idle()
    assert not agent.maintenance_on


def test_offline_shows_diagnosis_and_disables_controls(qtbot, app_data):
    agent = FakeAgent()
    sec = make_home(qtbot, app_data, agent,
                    broken={"weatherpi.example.ts.net", "100.94.2.31", "usb"})
    assert "can't reach" in sec.status_line.text()
    assert not sec.problems.isHidden()
    assert not sec.interval.isEnabled() and not sec.maint_button.isEnabled()


def test_no_station_offers_the_check(qtbot, app_data):
    ctx = AppContext(home=app_data.home, env=HostEnv(app_data=app_data))
    sec = home_mod.HomeSection(ctx)
    qtbot.addWidget(sec)
    got = []
    sec.finished.connect(got.append)
    sec.on_enter()
    buttons = [b for b in sec.findChildren(home_mod.QPushButton)
               if b.text() == "Set up a WeatherPi"]
    buttons[0].click()
    assert got == ["welcome"] and sec.nav.isHidden()


def test_dashboard_button_opens_serve_url(qtbot, app_data, opened):
    sec = make_home(qtbot, app_data, FakeAgent())
    sec.dashboard_button.click()
    assert opened == ["https://weatherpi.example.ts.net"]


# ---------------------------------------------------------------- W10: the home app (D28)


def test_menu_switches_pages(qtbot, app_data):
    sec = make_home(qtbot, app_data, FakeAgent())
    assert list(sec.nav_buttons) == ["overview", "wifi", "measurements", "settings"]
    assert [b.text() for b in sec.nav_buttons.values()] == [
        "Overview", "WiFi networks", "Measurements", "Settings"]
    assert sec.current_page == "overview" and sec.nav_buttons["overview"].isChecked()
    sec.nav_buttons["settings"].click()
    assert sec.pages.currentWidget() is sec.page_widgets["settings"]
    assert sec.nav_buttons["settings"].isChecked() and not sec.nav_buttons["overview"].isChecked()


def test_wifi_networks_page_adds_a_network_over_tailscale(qtbot, app_data):
    agent = FakeAgent()
    sec = make_home(qtbot, app_data, agent)
    sec.nav_buttons["wifi"].click()
    wait_idle()
    panel = sec.wifi_page.panel
    assert panel.supported and sec.wifi_page.remote  # over Tailscale
    before = len(panel.networks)
    panel.add_network("Garden extender", "gardenpw1", False)
    wait_idle()
    assert any(n.ssid == "Garden extender" for n in panel.networks)
    assert len(panel.networks) == before + 1
    saves = [r for r in agent.requests if r.method == "POST" and r.url.path == "/v1/wifi/saved"]
    assert saves and saves[-1].url.host == "weatherpi.example.ts.net"


def test_wifi_network_in_use_cannot_be_removed_or_changed_remotely(qtbot, app_data):
    agent = FakeAgent(wifi_state="connected", wifi_ssid="ExampleWiFi", saved=["ExampleWiFi"])
    sec = make_home(qtbot, app_data, agent)
    sec.nav_buttons["wifi"].click()
    wait_idle()
    panel = sec.wifi_page.panel
    in_use = next((i for i, n in enumerate(panel.networks) if n.in_use), None)
    assert in_use is not None
    panel.list.setCurrentRow(in_use)
    panel.remove()
    wait_idle()
    assert panel.checks.codes() == ["wifi_in_use_remote"]
    assert not [r for r in agent.requests if r.method == "DELETE"]
    panel.add_network(panel.networks[in_use].ssid, "newpass123", False)
    wait_idle()
    assert panel.checks.codes() == ["wifi_in_use_remote"]
    assert not [r for r in agent.requests if r.url.path == "/v1/wifi/connect"]


def test_set_up_again_asks_then_goes_to_welcome(qtbot, app_data):
    sec = make_home(qtbot, app_data, FakeAgent())
    got = []
    sec.finished.connect(got.append)
    sec.confirm = lambda *_a: False
    sec.setup_again_button.click()
    assert got == []
    sec.confirm = lambda *_a: True
    sec.setup_again_button.click()
    assert got == ["welcome"]


def test_shell_hides_setup_steps_in_the_home_app(qtbot, app_data):
    from weatherpi_companion.ui.app import WELCOME, MainWindow

    from .test_ui_fakes import make_ctx

    app_data.station("00000000abcd1234").update_profile(
        setup_complete=True, serve_url="https://weatherpi.example.ts.net")
    ctx, _s, _w = make_ctx(app_data)
    win = MainWindow(ctx)
    qtbot.addWidget(win)
    win.show()
    assert win.current_id == "home" and win.steps.isHidden()
    wait_idle()
    win.show_section(WELCOME)
    assert not win.steps.isHidden()
    win.show_section("tester")
    assert not win.steps.isHidden()
    win.close()
