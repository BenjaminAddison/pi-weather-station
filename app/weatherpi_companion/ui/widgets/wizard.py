"""Wizard frame shared by the ``tester`` and ``setup`` sections: pages with a title, an
intro, a body, a busy bar and a check list, plus a Back / Next bar. Next stays disabled
until the page says it's complete (all its checks pass) and nothing is running."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ... import RELEASES_URL
from ...checks import Action, CheckResult
from ...flows import SetupSession
from .. import desktop
from ..context import AppContext
from ..style import label, set_role
from ..worker import Task, run_task
from .busy import BusyBar
from .checklist import CheckList
from .helpdialog import HelpDialog, setup_help_pack

log = logging.getLogger(__name__)

PAGE_MARGINS = (36, 28, 36, 20)


def dev_skip_enabled() -> bool:
    """``WEATHERPI_DEV_SKIP=1`` (set by start-app.bat) turns on Ctrl+Shift+S to skip a page."""
    return os.environ.get("WEATHERPI_DEV_SKIP") == "1"


class WizardPage(QWidget):
    """One page. Subclasses fill ``self.body`` and override ``on_enter``/``refresh``."""

    state_changed = Signal()

    #: page title shown in the step list caption and at the top of the page
    TITLE = ""
    INTRO = ""
    #: short name for the shell's step list ("Plug in")
    CAPTION = ""

    def __init__(self, ctx: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Page")
        self.ctx = ctx
        self.wizard: Wizard | None = None
        self.complete = False
        self.next_label = "&Next"
        self.can_go_back = True
        #: page-specific fixes: action -> handler(check)
        self.actions: dict[str, Callable[[CheckResult], None]] = {}
        self._tasks: list[Task] = []
        self._busy_tasks: list[Task] = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.scroll = QScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        outer.addWidget(self.scroll)
        inner = QWidget()
        inner.setObjectName("Page")
        self.scroll.setWidget(inner)
        self.content = QVBoxLayout(inner)
        self.content.setContentsMargins(*PAGE_MARGINS)
        self.content.setSpacing(12)
        self.step_label = label("", "PageStep")
        self.title_label = label(self.TITLE, "PageTitle")
        self.title_label.setAccessibleName(self.TITLE)
        self.intro_label = label(self.INTRO, "Intro")
        self.intro_label.setVisible(bool(self.INTRO))
        self.content.addWidget(self.step_label)
        self.content.addWidget(self.title_label)
        self.content.addWidget(self.intro_label)
        self.content.addSpacing(4)
        self.body = QVBoxLayout()
        self.body.setSpacing(12)
        self.content.addLayout(self.body)
        self.busy = BusyBar(inner)
        self.content.addWidget(self.busy)
        self.checks = CheckList(inner)
        self.checks.hide()
        self.checks.action_requested.connect(self.handle_action)
        self.checks.help_requested.connect(self.help)
        self.content.addWidget(self.checks)
        self.after = QVBoxLayout()
        self.after.setSpacing(12)
        self.content.addLayout(self.after)
        self.content.addStretch(1)

    # ---- helpers

    @property
    def session(self) -> SetupSession:
        return self.ctx.ensure_session()

    def set_title(self, title: str, intro: str | None = None) -> None:
        self.title_label.setText(title)
        if intro is not None:
            self.intro_label.setText(intro)
            self.intro_label.setVisible(bool(intro))

    def reveal(self, widget: QWidget) -> None:
        """Scroll so ``widget`` is in view (after the layout has settled)."""
        from PySide6.QtCore import QTimer

        QTimer.singleShot(0, lambda: self.scroll.ensureWidgetVisible(widget, 0, 24))

    def set_complete(self, complete: bool) -> None:
        if complete != self.complete:
            self.complete = complete
        self.state_changed.emit()

    @property
    def busy_now(self) -> bool:
        return any(t.running for t in self._busy_tasks)

    def can_next(self) -> bool:
        return self.complete and not self.busy_now

    def run(self, fn: Callable[[Task], Any], on_done: Callable[[Any], None] | None = None, *,
            busy: str | None = None, cancel_text: str | None = None,
            on_progress: Callable[[Any, float], None] | None = None,
            on_error: Callable[[BaseException], None] | None = None,
            on_cancel: Callable[[], None] | None = None) -> Task:
        """Run ``fn(task)`` in the background. ``busy`` shows the busy bar (and blocks Next)
        until it finishes; ``cancel_text`` adds a button that cancels the task."""
        task = run_task(fn, on_done, on_error=on_error or self._task_failed,
                        on_progress=on_progress, owner=self)
        self._tasks.append(task)
        if busy is not None:
            self._busy_tasks.append(task)

            def cancel() -> None:
                task.cancel()
                if on_cancel:
                    on_cancel()
            self.busy.start(busy, cancel if cancel_text else None, cancel_text or "Stop")
        task.on_finished(lambda: self._task_finished(task))
        self.state_changed.emit()
        return task

    def _task_finished(self, task: Task) -> None:
        if task in self._tasks:
            self._tasks.remove(task)
        if task in self._busy_tasks:
            self._busy_tasks.remove(task)
            if not self.busy_now:
                self.busy.stop()
        self.state_changed.emit()

    def _task_failed(self, exc: BaseException) -> None:
        log.exception("unexpected error on page %s", type(self).__name__, exc_info=exc)
        self.checks.set_checks([CheckResult(
            False, "unexpected_error", "Something unexpected went wrong. Please try again; if "
            "it keeps happening, click Get help.", f"{type(exc).__name__}: {exc}",
            Action.RETRY)])

    def cancel_tasks(self) -> None:
        for t in list(self._tasks):
            t.cancel()
        self._busy_tasks.clear()
        self.busy.stop()
        self.state_changed.emit()

    # ---- actions & help

    def handle_action(self, check: CheckResult) -> None:
        handler = self.actions.get(check.action or "")
        if handler is not None:
            handler(check)
            return
        a = check.action
        if a in (Action.RETRY, Action.WAIT, Action.MOVE_CLOSER, Action.PLUG_IN):
            self.refresh()
        elif a in (Action.OPEN_URL, Action.INSTALL_TAILSCALE):
            desktop.open_url(check.data.get("url") or check.detail or
                             "https://tailscale.com/download")
        elif a == Action.OPEN_TAILSCALE_APP:
            path = check.data.get("path")
            if path:
                desktop.open_path(path)
            else:
                self.refresh()
        elif a == Action.UPDATE_APP:
            QMessageBox.information(
                self, "Update WeatherPi Companion",
                "This WeatherPi needs a newer WeatherPi Companion. The download page opens "
                "next: download the installer (the .exe), run it, then open the app again.")
            desktop.open_url(RELEASES_URL)
        else:
            self.refresh()

    def help(self, check: CheckResult | None = None) -> HelpDialog:
        checks = self.checks.checks
        problem = check.message_for_owner if check is not None else ""
        session = self.ctx.session
        dlg = HelpDialog(lambda text: setup_help_pack(session, checks, text), problem, self)
        dlg.setModal(True)
        dlg.show()
        return dlg

    # ---- lifecycle (override)

    def on_enter(self) -> None:
        """The page became visible."""

    def on_leave(self) -> None:
        self.cancel_tasks()

    def refresh(self) -> None:
        """"Check again"."""

    def before_next(self) -> bool:
        """Return False to stay on the page (e.g. it started something)."""
        return True

    def next_page(self) -> WizardPage | None:
        """Where Next goes (None: the following page)."""
        return None


class Wizard(QWidget):
    """A stack of :class:`WizardPage` with a Back / Next bar."""

    finished = Signal()
    page_changed = Signal(int, str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.pages: list[WizardPage] = []
        self.history: list[int] = []
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.stack = QStackedWidget(self)
        self.stack.setObjectName("Pages")
        lay.addWidget(self.stack, 1)
        bar = QFrame(self)
        bar.setObjectName("NavBar")
        nav = QHBoxLayout(bar)
        nav.setContentsMargins(PAGE_MARGINS[0], 12, PAGE_MARGINS[2], 14)
        self.help_button = QPushButton("Need help?", bar)
        set_role(self.help_button, "link")
        self.help_button.clicked.connect(lambda: self.current.help())
        self.back_button = QPushButton("&Back", bar)
        self.next_button = QPushButton("&Next", bar)
        set_role(self.next_button, "primary")
        self.next_button.setMinimumWidth(110)
        self.back_button.clicked.connect(self.back)
        self.next_button.clicked.connect(self.next)
        nav.addWidget(self.help_button)
        nav.addStretch(1)
        if dev_skip_enabled():  # developers only (start-app.bat); owners never see it
            nav.addWidget(label("Dev: Ctrl+Shift+S skips this page", "Muted"))
            nav.addSpacing(12)
            QShortcut(QKeySequence("Ctrl+Shift+S"), self, self.skip)
        nav.addWidget(self.back_button)
        nav.addWidget(self.next_button)
        lay.addWidget(bar)
        self.nav_bar = bar
        self.started = False

    def add_page(self, page: WizardPage) -> WizardPage:
        page.wizard = self
        self.pages.append(page)
        self.stack.addWidget(page)
        page.state_changed.connect(self._update_nav)
        self._renumber()
        return page

    def _renumber(self) -> None:
        n = len(self.pages)
        for i, p in enumerate(self.pages):
            p.step_label.setText(f"STEP {i + 1} OF {n}")

    @property
    def index(self) -> int:
        return self.stack.currentIndex()

    @property
    def current(self) -> WizardPage:
        return self.pages[self.index]

    def start(self, page: WizardPage | int | None = None) -> None:
        """Show ``page`` (default: the first) and call its ``on_enter``."""
        self.started = True
        self.history.clear()
        idx = page if isinstance(page, int) else (self.pages.index(page) if page else 0)
        self._show(idx)

    def go_to(self, page: WizardPage | int, record: bool = True) -> None:
        idx = page if isinstance(page, int) else self.pages.index(page)
        if record and idx != self.index:
            self.history.append(self.index)
        self._show(idx)

    def _show(self, idx: int) -> None:
        if self.started and 0 <= self.index < len(self.pages) and self.index != idx:
            self.current.on_leave()
        self.stack.setCurrentIndex(idx)
        page = self.pages[idx]
        self._update_nav()
        self.page_changed.emit(idx, page.CAPTION or page.title_label.text())
        page.on_enter()
        self._update_nav()
        page.setFocus(Qt.FocusReason.OtherFocusReason)

    def next(self) -> None:
        page = self.current
        if not page.can_next() or not page.before_next():
            return
        target = page.next_page()
        if target is not None:
            self.go_to(target)
        elif self.index + 1 < len(self.pages):
            self.go_to(self.index + 1)
        else:
            page.on_leave()
            self.finished.emit()

    def skip(self) -> None:
        """Dev only: go on to the next page even though its checks haven't passed."""
        page = self.current
        log.warning("dev skip: leaving page %s without its checks passing", type(page).__name__)
        page.cancel_tasks()
        if self.index + 1 < len(self.pages):
            self.go_to(self.index + 1)
        else:
            page.on_leave()
            self.finished.emit()

    def back(self) -> None:
        if self.history:
            self._show(self.history.pop())

    def leave(self) -> None:
        if self.started and self.pages:
            self.current.on_leave()

    def _update_nav(self) -> None:
        if not self.pages:
            return
        page = self.current
        self.next_button.setText(page.next_label)
        self.next_button.setEnabled(page.can_next())
        self.back_button.setVisible(bool(self.history) and page.can_go_back)


class WizardSection(QWidget):
    """Base for sections that are a :class:`Wizard` (``tester``, ``setup``). Subclasses set
    ``SECTION_ID``/``TITLE``/``NEXT_SECTION`` and add pages in ``build()``."""

    finished = Signal(str)
    #: caption for the shell's step list, e.g. "This computer (2 of 4)"
    step_changed = Signal(str)

    SECTION_ID = ""
    TITLE = ""
    NEXT_SECTION = ""

    def __init__(self, ctx: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Page")
        self.ctx = ctx
        self.step_caption = ""
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.wizard = Wizard(self)
        lay.addWidget(self.wizard)
        self.wizard.page_changed.connect(self._page_changed)
        self.wizard.finished.connect(self._wizard_finished)
        self.build()

    def build(self) -> None:
        raise NotImplementedError

    def _page_changed(self, idx: int, title: str) -> None:
        self.step_caption = f"{idx + 1} of {len(self.wizard.pages)}: {title}"
        self.step_changed.emit(self.step_caption)

    def _wizard_finished(self) -> None:
        self.finished.emit(self.NEXT_SECTION)

    def on_enter(self) -> None:
        if not self.wizard.started:
            self.wizard.start()
        else:
            self.wizard.current.on_enter()

    def on_leave(self) -> None:
        self.wizard.leave()
