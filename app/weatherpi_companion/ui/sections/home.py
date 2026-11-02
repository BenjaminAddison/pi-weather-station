"""Home app (W9 + W10, docs/companion-app.md "Home screen"): once setup is done the window
becomes this, with its own menu (D28): Overview (status), WiFi networks, Measurements (D26)
and Settings (maintenance mode for the read-only root, D14; "Set up again").

Everything goes through :class:`station.StationMonitor` on worker threads; it reaches the Pi
over Tailscale first and falls back to the USB cable.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ... import __version__
from ...agent_client import AgentError, MaintenanceState, StationSettings
from ...checks import CheckResult
from ...secrets import StationProfile
from ...station import StationMonitor, StationStatus, _ago
from .. import desktop
from ..context import AppContext
from ..style import label, set_role
from ..widgets.checklist import CheckList
from ..worker import run_task
from .setup import SavedNetworksPanel

REFRESH_MS = 30_000
#: the shell's Welcome page ("Flash a new SD card" / "My card is already flashed"); ui/app.py
WELCOME = "welcome"
#: after a maintenance-mode switch the Pi restarts; look again after this long
AFTER_REBOOT_MS = 75_000


def interval_text(seconds: int) -> str:
    minutes = seconds // 60
    if seconds % 60 or minutes < 1:
        return f"{seconds} seconds"
    if minutes == 60:
        return "hour"
    return "minute" if minutes == 1 else f"{minutes} minutes"


def _card(parent: QWidget, heading: str) -> tuple[QFrame, QVBoxLayout]:
    f = QFrame(parent)
    f.setObjectName("Card")
    lay = QVBoxLayout(f)
    lay.setContentsMargins(18, 14, 18, 14)
    lay.setSpacing(8)
    lay.addWidget(label(heading, "SectionHeading"))
    return f, lay


def _row(*widgets: QWidget) -> QHBoxLayout:
    r = QHBoxLayout()
    r.setSpacing(8)
    for w in widgets:
        r.addWidget(w)
    r.addStretch(1)
    return r


def _confirm(parent: QWidget, title: str, text: str) -> bool:
    answer = QMessageBox.question(parent, title, text,
                                  QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                  QMessageBox.StandardButton.No)
    return answer == QMessageBox.StandardButton.Yes


@dataclass
class HomeData:
    status: StationStatus
    settings: StationSettings | None = None
    settings_error: str | None = None
    maintenance: MaintenanceState | None = None
    #: the Pi answered, but has no maintenance mode / settings (too old)
    unsupported: set[str] = field(default_factory=set)


def pick_profile(ctx: AppContext) -> StationProfile | None:
    """The station to show: a finished setup first, else one this PC holds the keys for."""
    app_data = ctx.env.app_data
    profiles = app_data.list_profiles()
    done = [p for p in profiles if p.setup_complete]
    if done:
        return done[0]
    return next((p for p in profiles if app_data.station(p.serial).has_secrets), None)


class WifiNetworksHost(QWidget):
    """Hosts :class:`SavedNetworksPanel` on the home screen (D28): talks to the Pi through
    :class:`StationMonitor` (usually over Tailscale), so the network in use can't be changed
    from here; that needs the USB cable and "Set up again"."""

    def __init__(self, section: HomeSection) -> None:
        super().__init__(section)
        self.section = section
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        frame = QFrame(self)
        frame.setObjectName("Card")
        fl = QVBoxLayout(frame)
        fl.setContentsMargins(4, 4, 4, 4)
        self.panel = SavedNetworksPanel(self)
        self.panel.hint.hide()  # the page's intro says it
        fl.addWidget(self.panel)
        lay.addWidget(frame)

    def saved_backend(self) -> Any:
        assert self.section.monitor is not None
        return self.section.monitor.wifi

    @property
    def remote(self) -> bool:
        mon = self.section.monitor
        return mon is None or mon.remote

    def run(self, fn: Callable[[Any], Any], done: Callable[[Any], None],
            on_error: Callable[[BaseException], None] | None = None, **_kw: Any) -> Any:
        return run_task(fn, done, on_error=on_error, owner=self)

    def edit_in_use(self, ssid: str, password: str | None, hidden: bool) -> None:
        self.panel.checks.set_checks([CheckResult(
            False, "wifi_in_use_remote", f"The WeatherPi is using {ssid} right now, so its "
            "details can't be changed from here: a wrong password would take it offline. Plug "
            "the WeatherPi into this computer with the USB cable and choose Set up again "
            "(Settings).")])


class HomeSection(QWidget):
    SECTION_ID = "home"
    TITLE = "Home"
    finished = Signal(str)

    def __init__(self, ctx: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Page")
        self.ctx = ctx
        #: tests replace this (no modal dialogs offscreen)
        self.confirm: Callable[[QWidget, str, str], bool] = _confirm
        self.profile = pick_profile(ctx)
        self.monitor: StationMonitor | None = None
        if self.profile is not None:
            self.monitor = StationMonitor(ctx.env.app_data.station(self.profile.serial), ctx.env)
        self.data: HomeData | None = None
        self._loading = False
        self._saving = False

        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        # the home app's own menu (D28); the shell hides setup's step list while this shows
        self.nav = QWidget(self)
        self.nav.setObjectName("Sidebar")
        self.nav.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.nav.setFixedWidth(232)
        nl = QVBoxLayout(self.nav)
        nl.setContentsMargins(0, 22, 0, 14)
        nl.setSpacing(4)
        head = QVBoxLayout()
        head.setContentsMargins(22, 0, 16, 14)
        head.setSpacing(2)
        head.addWidget(label("WeatherPi Companion", "AppName"))
        head.addWidget(label("Your weather station", "Muted"))
        nl.addLayout(head)
        self.nav_buttons: dict[str, QPushButton] = {}
        self._nav_layout = nl
        self.pages = QStackedWidget(self)
        self.pages.setObjectName("Pages")
        self.page_widgets: dict[str, QWidget] = {}
        outer.addWidget(self.nav)
        outer.addWidget(self.pages, 1)
        self.current_page = "overview"

        # -- Overview
        lay, body = self._page("overview", "Overview", "Your WeatherPi")
        self.title = lay.itemAt(1).widget()
        if self.monitor is None:
            self.nav.hide()
            lay.addWidget(label("There's no WeatherPi set up on this computer yet.", "Intro"))
            start = QPushButton("Set up a WeatherPi", self)
            set_role(start, "primary")
            start.clicked.connect(lambda: self.finished.emit(WELCOME))
            lay.addLayout(_row(start))
            lay.addStretch(1)
            self.timer = None
            return

        self.status_card, sl = _card(body, "Status")
        self.status_line = label("Looking for your WeatherPi...", "Intro")
        sl.addWidget(self.status_line)
        self.maint_banner = label("", "Muted")
        self.maint_banner.setStyleSheet("color: #92400e; background: #fffbeb; "
                                        "border: 1px solid #f2dcb5; border-radius: 6px; "
                                        "padding: 6px 8px;")
        self.maint_banner.hide()
        sl.addWidget(self.maint_banner)
        grid = QGridLayout()
        grid.setHorizontalSpacing(18)
        grid.setVerticalSpacing(4)
        self.values: dict[str, QLabel] = {}
        for i, (key, name) in enumerate([("temp", "Temperature"), ("rain", "Rain today"),
                                         ("seen", "Last heard from"), ("wifi", "WiFi"),
                                         ("link", "Connected over"),
                                         ("software", "Software")]):
            grid.addWidget(label(name, "Muted"), i, 0)
            value = label("–")
            grid.addWidget(value, i, 1)
            self.values[key] = value
        grid.setColumnStretch(1, 1)
        sl.addLayout(grid)
        self.problems = CheckList(body)
        self.problems.hide()
        sl.addWidget(self.problems)
        self.refresh_button = QPushButton("Refresh", body)
        self.refresh_button.clicked.connect(self.refresh)
        self.dashboard_button = QPushButton("Open the dashboard", body)
        set_role(self.dashboard_button, "primary")
        self.dashboard_button.clicked.connect(self._open_dashboard)
        self.dashboard_button.setVisible(bool(self.profile.serve_url))
        sl.addLayout(_row(self.dashboard_button, self.refresh_button))
        lay.addWidget(self.status_card)
        lay.addStretch(1)

        # -- WiFi networks (D28)
        lay, body = self._page(
            "wifi", "WiFi networks", "WiFi networks",
            "The networks the WeatherPi knows. It joins the highest one on the list that's in "
            "range, by itself. Add the networks it will need later, like a WiFi extender in "
            "the garden, a holiday home or a phone hotspot. This works from anywhere.")
        self.wifi_page = WifiNetworksHost(self)
        lay.addWidget(self.wifi_page)
        lay.addStretch(1)

        # -- Measurements (D26)
        lay, body = self._page("measurements", "Measurements", "Measurements")
        self.measure_card, ml = _card(body, "How often to save a reading")
        self.interval = QComboBox(body)
        self.interval.setAccessibleName("How often to save a temperature reading")
        self.interval.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.interval.setMinimumContentsLength(len("30 minutes") + 1)
        self.save_button = QPushButton("Save", body)
        self.save_button.clicked.connect(self.save_interval)
        self.interval.currentIndexChanged.connect(self._interval_changed)
        ml.addLayout(_row(label("Save a temperature reading every", wrap=False),
                          self.interval, self.save_button))
        ml.addWidget(label("More often gives more detail in the charts; less often keeps the "
                           "database smaller. The temperature is checked every 30 seconds "
                           "either way and averaged, and every tip of the rain gauge is "
                           "counted.", "Muted"))
        self.measure_note = label("", "Muted")
        self.measure_note.hide()
        ml.addWidget(self.measure_note)
        lay.addWidget(self.measure_card)
        lay.addStretch(1)

        # -- Settings: maintenance mode (D14), set up again (D28)
        lay, body = self._page("settings", "Settings", "Settings")
        self.maint_card, al = _card(body, "Maintenance mode")
        al.addWidget(label("Normally the WeatherPi's system files are read-only, so a power cut "
                           "can't damage them and the SD card lasts longer. Maintenance mode "
                           "makes them writable so the WeatherPi can be updated or repaired. "
                           "It restarts the WeatherPi (about a minute). Switch it back off "
                           "when you're done.", "Muted"))
        self.maint_state = label("", "Intro")
        al.addWidget(self.maint_state)
        self.maint_button = QPushButton("Turn on maintenance mode", body)
        self.maint_button.clicked.connect(self.toggle_maintenance)
        al.addLayout(_row(self.maint_button))
        lay.addWidget(self.maint_card)
        self.setup_card, ul = _card(body, "Set up again")
        ul.addWidget(label("Go through the setup steps again, with the USB cable: for example "
                           "after re-flashing the SD card, to change the WiFi network the "
                           "WeatherPi is using, or to set up a replacement. The readings stay "
                           "on the WeatherPi unless you flash its card.", "Muted"))
        self.setup_again_button = QPushButton("Set up again...", body)
        self.setup_again_button.clicked.connect(self.setup_again)
        ul.addLayout(_row(self.setup_again_button))
        lay.addWidget(self.setup_card)
        lay.addStretch(1)

        nl.addStretch(1)
        ver = label(f"Version {__version__}", "Muted")
        ver.setContentsMargins(22, 0, 0, 0)
        nl.addWidget(ver)
        self._set_controls_enabled(False)
        self.show_page("overview")

        self.timer = QTimer(self)
        self.timer.setInterval(REFRESH_MS)
        self.timer.timeout.connect(self.refresh)

    # ---------------------------------------------------------------- pages

    def _page(self, key: str, nav_text: str, title: str,
              intro: str = "") -> tuple[QVBoxLayout, QWidget]:
        scroll = QScrollArea(self.pages)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        body = QWidget()
        body.setObjectName("Page")
        lay = QVBoxLayout(body)
        lay.setContentsMargins(36, 28, 36, 28)
        lay.setSpacing(14)
        lay.addWidget(label("YOUR WEATHERPI", "PageStep"))
        lay.addWidget(label(title, "PageTitle"))
        if intro:
            lay.addWidget(label(intro, "Intro"))
        scroll.setWidget(body)
        self.pages.addWidget(scroll)
        self.page_widgets[key] = scroll
        b = QPushButton(nav_text, self.nav)
        set_role(b, "nav")
        b.setCheckable(True)
        b.clicked.connect(lambda _=False, k=key: self.show_page(k))
        self._nav_layout.addWidget(b)
        self.nav_buttons[key] = b
        return lay, body

    def show_page(self, key: str) -> None:
        self.current_page = key
        self.pages.setCurrentWidget(self.page_widgets[key])
        for k, b in self.nav_buttons.items():
            b.setChecked(k == key)
        if key == "wifi" and self.monitor is not None and self.isVisible():
            self.wifi_page.panel.reload()

    def setup_again(self) -> None:
        if not self.confirm(self, "Set up again?",
                            "This opens the setup steps. You'll need the WeatherPi's USB "
                            "cable. Your WeatherPi keeps recording meanwhile."):
            return
        if self.timer is not None:
            self.timer.stop()
        self.ctx.reset_session()
        self.finished.emit(WELCOME)

    # ---------------------------------------------------------------- lifecycle

    def on_enter(self) -> None:
        if self.monitor is None:
            return
        self.refresh()
        if self.timer is not None:
            self.timer.start()
        if self.current_page == "wifi":
            self.wifi_page.panel.reload()

    def hideEvent(self, e) -> None:
        if self.timer is not None:
            self.timer.stop()
        super().hideEvent(e)

    # ---------------------------------------------------------------- loading

    def refresh(self) -> None:
        if self.monitor is None or self._loading:
            return
        self._loading = True
        self.refresh_button.setEnabled(False)
        run_task(lambda _t: self._load(), self._show, on_error=self._load_failed, owner=self)

    def _load(self) -> HomeData:
        mon = self.monitor
        assert mon is not None
        data = HomeData(mon.refresh())
        if not data.status.online:
            return data
        try:
            data.settings = mon.settings()
            if data.settings is None:
                data.unsupported.add("settings")
        except AgentError as exc:
            data.settings_error = str(exc)
        try:
            data.maintenance = mon.maintenance()
            if data.maintenance is None:
                data.unsupported.add("maintenance")
        except AgentError:
            pass
        return data

    def _load_failed(self, exc: BaseException) -> None:
        self._loading = False
        self.refresh_button.setEnabled(True)
        self.status_line.setText(f"Couldn't check the WeatherPi: {exc}")

    def _show(self, data: HomeData) -> None:
        self._loading = False
        self.refresh_button.setEnabled(True)
        self.data = data
        st = data.status
        now = datetime.now(UTC)
        v = self.values
        if st.online:
            via = {"tailscale": "Tailscale", "usb": "the USB cable"}.get(st.via or "", st.via)
            alive = st.sensors is not None and st.sensors.logger.alive
            self.status_line.setText("Your WeatherPi is online and recording." if alive else
                                     "Your WeatherPi is online, but it hasn't recorded "
                                     "anything recently.")
            self.problems.hide()
            s = st.sensors
            if s is not None:
                t = s.temperature
                v["temp"].setText(f"{t.celsius:.1f} °C" if t.present and t.celsius is not None
                                  else "no sensor found")
                v["rain"].setText(f"{s.rain.mm_today:.1f} mm ({s.rain.tips_today} tips)")
                v["seen"].setText(_ago(s.logger.last_heartbeat, now) or "not yet")
            w = st.wifi
            v["wifi"].setText(f"{w.ssid} ({w.signal}%)" if w and w.ssid and w.signal is not None
                              else (w.ssid if w and w.ssid else "not connected"))
            v["link"].setText(via or "–")
            if st.info is not None:
                v["software"].setText(f"image {st.info.image_version or '?'}, "
                                      f"agent {st.info.agent_version or '?'}")
        else:
            self.status_line.setText("We can't reach your WeatherPi right now.")
            self.problems.set_checks(st.diagnosis)
            self.problems.setVisible(bool(st.diagnosis))
            for key in ("link",):
                v[key].setText("–")
        self._show_settings(data)
        self._show_maintenance(data)

    def _show_settings(self, data: HomeData) -> None:
        online = data.status.online
        st = data.settings
        if st is not None:
            self.interval.blockSignals(True)
            self.interval.clear()
            for c in st.record_interval_choices:
                self.interval.addItem(interval_text(c), c)
            idx = self.interval.findData(st.record_interval_s)
            if idx < 0:  # e.g. set by hand in config.yaml
                self.interval.addItem(interval_text(st.record_interval_s), st.record_interval_s)
                idx = self.interval.count() - 1
            self.interval.setCurrentIndex(idx)
            self.interval.blockSignals(False)
            if not self._saving:
                self._note("")
        elif "settings" in data.unsupported:
            self._note("This WeatherPi's software is too old to change this. "
                                      "Flash its SD card with the latest WeatherPi image to get "
                                      "it.")
        elif data.settings_error:
            self._note(f"Couldn't read the setting: {data.settings_error}")
        elif not online:
            self._note("You can change this when the WeatherPi is online.")
        ok = online and st is not None and not self._saving
        self.interval.setEnabled(ok)
        self.save_button.setEnabled(ok and self._interval_dirty())

    def _show_maintenance(self, data: HomeData) -> None:
        m = data.maintenance
        if m is None:
            self.maint_banner.hide()
            self.maint_button.setEnabled(False)
            if "maintenance" in data.unsupported:
                self.maint_state.setText("This WeatherPi's software doesn't have maintenance "
                                         "mode (its system files are always writable).")
            elif not data.status.online:
                self.maint_state.setText("")
            return
        self.maint_button.setEnabled(True)
        if m.maintenance:
            self.maint_state.setText("Maintenance mode is ON: the system files are writable."
                                     if not m.overlay_active else
                                     "Maintenance mode is ON from the next restart.")
            self.maint_button.setText("Turn off maintenance mode")
            self.maint_banner.setText("Maintenance mode is on. Turn it off when you're done "
                                      "(Settings, Maintenance mode).")
            self.maint_banner.show()
        else:
            self.maint_state.setText("Maintenance mode is off: the system files are read-only."
                                     if m.overlay_active else
                                     "Maintenance mode is off from the next restart.")
            self.maint_button.setText("Turn on maintenance mode")
            self.maint_banner.hide()

    def _note(self, text: str) -> None:
        self.measure_note.setText(text)
        self.measure_note.setVisible(bool(text))

    def _set_controls_enabled(self, on: bool) -> None:
        self.interval.setEnabled(on)
        self.save_button.setEnabled(on)
        self.maint_button.setEnabled(on)

    # ---------------------------------------------------------------- measurements

    def _interval_dirty(self) -> bool:
        st = self.data.settings if self.data else None
        return st is not None and self.interval.currentData() != st.record_interval_s

    def _interval_changed(self, _i: int) -> None:
        self.save_button.setEnabled(self.interval.isEnabled() and self._interval_dirty())

    def save_interval(self) -> None:
        seconds = self.interval.currentData()
        if self.monitor is None or seconds is None or self._saving:
            return
        mon = self.monitor
        self._saving = True
        self.save_button.setEnabled(False)
        self.interval.setEnabled(False)
        self._note("Saving...")
        run_task(lambda _t: mon.set_record_interval(int(seconds)), self._saved,
                 on_error=self._save_failed, owner=self)

    def _saved(self, st: StationSettings) -> None:
        self._saving = False
        if self.data is not None:
            self.data.settings = st
        self._note(f"Saved. The WeatherPi now saves a reading every "
                                  f"{interval_text(st.record_interval_s)}.")
        self.interval.setEnabled(True)
        self.save_button.setEnabled(self._interval_dirty())

    def _save_failed(self, exc: BaseException) -> None:
        self._saving = False
        self._note(f"Couldn't save the setting: {exc}")
        self.interval.setEnabled(True)
        self.save_button.setEnabled(self._interval_dirty())

    # ---------------------------------------------------------------- maintenance

    def toggle_maintenance(self) -> None:
        m = self.data.maintenance if self.data else None
        if self.monitor is None or m is None:
            return
        enable = not m.maintenance
        if enable:
            ok = self.confirm(self, "Turn on maintenance mode?",
                              "The WeatherPi will restart with writable system files, so it "
                              "can be updated or repaired. Remember to turn maintenance mode "
                              "off again afterwards.")
        else:
            ok = self.confirm(self, "Turn off maintenance mode?",
                              "The WeatherPi will restart with read-only system files again.")
        if not ok:
            return
        mon = self.monitor
        self.maint_button.setEnabled(False)
        self.maint_state.setText("Asking the WeatherPi...")
        run_task(lambda _t: mon.set_maintenance(enable), self._maintenance_set,
                 on_error=self._maintenance_failed, owner=self)

    def _maintenance_set(self, m: MaintenanceState) -> None:
        if self.data is not None:
            self.data.maintenance = m
        if m.rebooting:
            self.maint_state.setText("The WeatherPi is restarting. This takes about a minute; "
                                     "this page checks again by itself.")
            self.maint_button.setEnabled(False)
            QTimer.singleShot(AFTER_REBOOT_MS, self.refresh)
        elif self.data is not None:
            self._show_maintenance(self.data)

    def _maintenance_failed(self, exc: BaseException) -> None:
        self.maint_button.setEnabled(True)
        self.maint_state.setText(f"Couldn't change maintenance mode: {exc}")

    # ---------------------------------------------------------------- misc

    def _open_dashboard(self) -> None:
        url = self.profile.serve_url if self.profile else None
        if url:
            desktop.open_url(url)

