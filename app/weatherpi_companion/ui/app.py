"""The app shell: one window with the owner's steps on the left and the current section on
the right, start-up routing, and ``--only <section-id>`` for development
(docs/companion-app.md, "App structure")."""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractButton,
    QApplication,
    QHBoxLayout,
    QMainWindow,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .. import __version__
from ..flows import HostEnv
from ..secrets import AppData, app_home
from . import worker
from .context import AppContext
from .sections import SECTION_IDS, SECTIONS, TITLES, create_section
from .style import ACCENT, MUTED, OK, TEXT, apply_app_style, label, set_role

log = logging.getLogger(__name__)

WELCOME = "welcome"
WINDOW_SIZE = QSize(960, 640)
DRIVER_EXE_NAME = "rpi-usb-gadget-driver-setup.exe"
ENV_DRIVER_EXE = "WEATHERPI_DRIVER_EXE"


# --------------------------------------------------------------------------- environment


def find_driver_exe() -> Path | None:
    """The bundled RNDIS driver setup (W7 ships it next to the app)."""
    override = os.environ.get(ENV_DRIVER_EXE)
    if override:
        return Path(override)
    base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    roots = {base, Path(sys.executable).parent, Path(sys.executable).parent.parent}
    for root in roots:
        for sub in ("", "vendor", "drivers", "installer/vendor"):
            p = root / sub / DRIVER_EXE_NAME
            if p.is_file():
                return p
    return None


def build_env(home: Path | None = None) -> HostEnv:
    """The real host environment."""
    app_data = AppData(home or app_home()).ensure()
    return HostEnv(app_data=app_data, driver_exe=find_driver_exe())


def initial_section(app_data: AppData) -> str:
    """Start-up routing: a finished station -> home; a started one -> tester (it resumes);
    nothing yet -> the Welcome page."""
    profiles = app_data.list_profiles()
    if any(p.setup_complete for p in profiles):
        return "home"
    if profiles:
        return "tester"
    return WELCOME


# --------------------------------------------------------------------------- step list


class StepItem(QAbstractButton):
    """One step in the left-hand list: a numbered circle (a tick when done), the title,
    and a caption under the current step ("This computer - 2 of 4")."""

    def __init__(self, number: int, section_id: str, title: str,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.number = number
        self.section_id = section_id
        self.setText(title)
        self.caption = ""
        self.state = "todo"  # todo | current | done | skipped
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setAccessibleName(f"Step {number}: {title}")

    def set_state(self, state: str, caption: str = "") -> None:
        self.state, self.caption = state, caption
        if state == "skipped" and not caption:
            caption = "Skipped"
            self.caption = caption
        self.setAccessibleDescription({"done": "done", "current": "you are here",
                                       "skipped": "skipped"}.get(state, ""))
        self.updateGeometry()
        self.update()

    def sizeHint(self) -> QSize:
        fm = QFontMetrics(self.font())
        h = fm.height() * 2 + 18
        if self.caption:
            h += fm.height()
        return QSize(200, h)

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = self.rect()
        fm = QFontMetrics(self.font())
        if self.state == "current":
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor("white"))
            p.drawRoundedRect(QRectF(r).adjusted(6, 2, -6, -2), 8, 8)
            p.setBrush(QColor(ACCENT))
            p.drawRoundedRect(QRectF(6, 10, 4, r.height() - 20), 2, 2)
        if self.hasFocus():
            p.setPen(QPen(QColor(ACCENT), 1.5, Qt.PenStyle.DotLine))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(QRectF(r).adjusted(7, 3, -7, -3), 8, 8)
        d = fm.height() * 1.55
        cy = 8 + fm.height() * 0.5 + 8
        circle = QRectF(22, cy - d / 2, d, d)
        if self.state == "done":
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(OK))
            p.drawEllipse(circle)
            pen = QPen(QColor("white"), d * 0.12, Qt.PenStyle.SolidLine,
                       Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
            p.setPen(pen)
            x, y = circle.x(), circle.y()
            p.drawPolyline([QPointF(x + d * 0.28, y + d * 0.52), QPointF(x + d * 0.44, y + d * 0.68),
                            QPointF(x + d * 0.73, y + d * 0.35)])
        elif self.state == "skipped":  # a grey dash: passed over, not done
            p.setPen(QPen(QColor("#b8c0cc"), 1.6))
            p.setBrush(QColor("#eef0f3"))
            p.drawEllipse(circle)
            p.setPen(QPen(QColor(MUTED), d * 0.12, Qt.PenStyle.SolidLine,
                          Qt.PenCapStyle.RoundCap))
            p.drawLine(QPointF(circle.x() + d * 0.32, circle.center().y()),
                       QPointF(circle.x() + d * 0.68, circle.center().y()))
        else:
            current = self.state == "current"
            p.setPen(QPen(QColor(ACCENT if current else "#b8c0cc"), 1.6))
            p.setBrush(QColor(ACCENT) if current else QColor("white"))
            p.drawEllipse(circle)
            p.setPen(QColor("white") if current else QColor(MUTED))
            f = QFont(self.font())
            f.setBold(True)
            f.setPointSizeF(max(7.5, self.font().pointSizeF() * 0.85))
            p.setFont(f)
            p.drawText(circle, Qt.AlignmentFlag.AlignCenter, str(self.number))
        tx = circle.right() + 12
        f = QFont(self.font())
        f.setBold(self.state == "current")
        p.setFont(f)
        p.setPen(QColor(TEXT if self.state in ("current", "done") else "#4b5563"))
        p.drawText(QRectF(tx, cy - fm.height(), r.width() - tx - 8, fm.height() * 2),
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self.text())
        if self.caption:
            small = QFont(self.font())
            small.setPointSizeF(self.font().pointSizeF() * 0.9)
            p.setFont(small)
            p.setPen(QColor(MUTED))
            box = QRectF(tx, cy + fm.height() * 0.6, r.width() - tx - 10, fm.height() * 1.4)
            text = QFontMetrics(small).elidedText(self.caption, Qt.TextElideMode.ElideRight,
                                                  int(box.width()))
            p.drawText(box, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop, text)
        p.end()


class StepList(QWidget):
    step_clicked = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Sidebar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedWidth(232)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 22, 0, 14)
        lay.setSpacing(2)
        head = QVBoxLayout()
        head.setContentsMargins(22, 0, 16, 14)
        head.setSpacing(2)
        head.addWidget(label("WeatherPi Companion", "AppName"))
        head.addWidget(label("Set up your weather station", "Muted"))
        lay.addLayout(head)
        self.items: dict[str, StepItem] = {}
        for i, (sid, title) in enumerate(SECTIONS, 1):
            item = StepItem(i, sid, title, self)
            item.clicked.connect(lambda _=False, s=sid: self.step_clicked.emit(s))
            lay.addWidget(item)
            self.items[sid] = item
        lay.addStretch(1)
        ver = label(f"Version {__version__}", "Muted")
        ver.setContentsMargins(22, 0, 0, 0)
        lay.addWidget(ver)
        self.current: str | None = None
        #: steps the owner chose to skip this run (e.g. "My card is already flashed");
        #: earlier steps not in here count as done (maybe in an earlier session)
        self.skipped: set[str] = set()

    def set_current(self, section_id: str | None, caption: str = "") -> None:
        self.current = section_id
        self.skipped.discard(section_id or "")  # went back to it after all
        idx = SECTION_IDS.index(section_id) if section_id in SECTION_IDS else -1
        for i, sid in enumerate(SECTION_IDS):
            item = self.items[sid]
            if sid == section_id:
                item.set_state("current", caption)
            elif idx >= 0 and i < idx:
                item.set_state("skipped" if sid in self.skipped else "done")
            else:
                item.set_state("todo")

    def set_caption(self, caption: str) -> None:
        if self.current in self.items:
            self.items[self.current].set_state("current", caption)


# --------------------------------------------------------------------------- welcome


class ChoiceButton(QPushButton):
    """A big button with a bold first line and a muted second line."""

    def __init__(self, title: str, text: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        set_role(self, "big")
        self.setAccessibleName(title)
        self.setAccessibleDescription(text)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMaximumWidth(520)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 12, 18, 12)
        lay.setSpacing(2)
        head = label(title, "SectionHeading", wrap=False)
        sub = label(text, "Muted", wrap=False)
        for w in (head, sub):
            w.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            lay.addWidget(w)
        self.setMinimumHeight(lay.sizeHint().height())

    def sizeHint(self) -> QSize:
        return self.layout().sizeHint().expandedTo(QSize(320, 0))


class WelcomePage(QWidget):
    chosen = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Page")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(48, 44, 48, 32)
        lay.setSpacing(14)
        lay.addWidget(label("WELCOME", "PageStep"))
        lay.addWidget(label("Let's get your WeatherPi running", "PageTitle"))
        lay.addWidget(label(
            "This app sets up your WeatherPi weather station and connects it to your own "
            "private network (Tailscale), so you can see its readings from anywhere. It takes "
            "about 15 minutes, and you won't need to type anything on the WeatherPi itself.",
            "Intro"))
        lay.addSpacing(10)
        lay.addWidget(label("Is the WeatherPi's memory card ready?", "SectionHeading"))
        self.flash_button = ChoiceButton(
            "Flash a new SD card", "Write the WeatherPi software to a memory card first.",
            self)
        self.tester_button = ChoiceButton(
            "My card is already flashed", "Go straight to checking the WeatherPi.", self)
        for b in (self.flash_button, self.tester_button):
            lay.addWidget(b)
        self.flash_button.clicked.connect(lambda: self.chosen.emit("flasher"))
        self.tester_button.clicked.connect(lambda: self.chosen.emit("tester"))
        lay.addStretch(1)
        lay.addWidget(label("Not sure? If the card came with your WeatherPi, it's already "
                            "flashed.", "Muted"))


# --------------------------------------------------------------------------- window


class MainWindow(QMainWindow):
    def __init__(self, ctx: AppContext, only: str | None = None,
                 start: str | None = None) -> None:
        super().__init__()
        self.ctx = ctx
        self.only = only
        self.sections: dict[str, QWidget] = {}
        self.current_id: str | None = None
        ctx.navigator = self.show_section
        self.setWindowTitle("WeatherPi Companion" + (f" - {TITLES.get(only, only)} (dev)"
                                                      if only else ""))
        self.resize(WINDOW_SIZE)
        self.setMinimumSize(760, 520)

        shell = QWidget(self)
        shell.setObjectName("Shell")
        row = QHBoxLayout(shell)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        self.steps = StepList(shell)
        self.steps.step_clicked.connect(self.show_section)
        self.steps.setVisible(only is None)
        row.addWidget(self.steps)
        self.stack = QStackedWidget(shell)
        self.stack.setObjectName("Content")
        row.addWidget(self.stack, 1)
        self.setCentralWidget(shell)

        self.welcome: WelcomePage | None = None
        if only is None:
            self.welcome = WelcomePage(self.stack)
            self.welcome.chosen.connect(self._welcome_chosen)
            self.stack.addWidget(self.welcome)
            self.show_section(start or initial_section(ctx.env.app_data))
        else:
            self.show_section(only)

    def section(self, section_id: str) -> QWidget:
        w = self.sections.get(section_id)
        if w is None:
            w = create_section(section_id, self.ctx, self.stack)
            fin = getattr(w, "finished", None)
            if fin is not None:
                fin.connect(lambda nxt, s=section_id: self._finished(s, nxt))
            sc = getattr(w, "step_changed", None)
            if sc is not None:
                sc.connect(lambda cap, s=section_id: self._caption(s, cap))
            self.sections[section_id] = w
            self.stack.addWidget(w)
        return w

    def _welcome_chosen(self, section_id: str) -> None:
        if section_id == "tester":  # "My card is already flashed"
            self.steps.skipped.add("flasher")
        self.show_section(section_id)

    def show_section(self, section_id: str) -> None:
        if section_id == WELCOME and self.welcome is not None:
            self._leave_current()
            self.steps.setVisible(True)
            self.current_id = WELCOME
            self.stack.setCurrentWidget(self.welcome)
            self.steps.set_current(None)
            self.welcome.flash_button.setFocus(Qt.FocusReason.OtherFocusReason)
            return
        if self.only is not None and section_id != self.only:
            self.statusBar().showMessage(
                f"Section '{self.only}' finished; the app would now go to '{section_id}'.")
            return
        if section_id == self.current_id:
            return
        self._leave_current()
        w = self.section(section_id)
        # D28: the home app has its own menu; setup's step list belongs to setup
        self.steps.setVisible(self.only is None and section_id != "home")
        self.current_id = section_id
        self.stack.setCurrentWidget(w)
        self.steps.set_current(section_id, getattr(w, "step_caption", "") or "")
        enter = getattr(w, "on_enter", None)
        if callable(enter):
            enter()

    def _leave_current(self) -> None:
        w = self.sections.get(self.current_id or "")
        leave = getattr(w, "on_leave", None)
        if callable(leave):
            leave()

    def _finished(self, section_id: str, nxt: str) -> None:
        if not nxt:
            i = SECTION_IDS.index(section_id)
            nxt = SECTION_IDS[min(i + 1, len(SECTION_IDS) - 1)]
        self.show_section(nxt)

    def _caption(self, section_id: str, caption: str) -> None:
        if section_id == self.current_id:
            self.steps.set_caption(caption)

    def closeEvent(self, e) -> None:
        self._leave_current()
        worker.shutdown(3000)
        if self.ctx.session is not None:
            self.ctx.session.close()
        super().closeEvent(e)


# --------------------------------------------------------------------------- entry point


def _setup_logging(app_data: AppData) -> None:
    root = logging.getLogger()
    if any(isinstance(h, logging.FileHandler) for h in root.handlers):
        return
    try:
        app_data.logs_dir.mkdir(parents=True, exist_ok=True)
        h = logging.FileHandler(app_data.log_file, encoding="utf-8")
    except OSError:
        return
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(h)
    root.setLevel(logging.INFO)


def make_app(argv: Sequence[str] | None = None) -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication(list(argv) if argv is not None else sys.argv[:1])
    app.setApplicationName("WeatherPi Companion")
    app.setApplicationVersion(__version__)
    app.setOrganizationName("WeatherPi")
    apply_app_style(app)  # type: ignore[arg-type]
    return app  # type: ignore[return-value]


def run(qt_args: Sequence[str] | None = None, only: str | None = None) -> int:
    """Start the app. ``only`` opens a window with just that section (dev)."""
    if only is not None and only not in SECTION_IDS:
        print(f"unknown section {only!r}; choose from {', '.join(SECTION_IDS)}",
              file=sys.stderr)
        return 2
    app = make_app([sys.argv[0], *(qt_args or [])])
    env = build_env()
    _setup_logging(env.app_data)
    log.info("WeatherPi Companion %s starting (only=%s)", __version__, only)
    ctx = AppContext(home=env.app_data.home, env=env)
    win = MainWindow(ctx, only=only)
    win.show()
    return app.exec()
