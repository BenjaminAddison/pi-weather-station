"""The check list: one row per :class:`CheckResult` (status icon, plain-words message,
an ``Action`` button, "Details" and "Get help")."""

from __future__ import annotations

import logging
from collections.abc import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ...checks import Action, CheckResult
from ..style import repolish, set_role, status_icon
from .busy import Spinner

log = logging.getLogger(__name__)

#: default button text per action; ``None`` = no button (the fix is elsewhere on the page)
ACTION_LABELS: dict[str, str | None] = {
    Action.INSTALL_DRIVER: "Install driver",
    Action.INSTALL_TAILSCALE: "Install Tailscale",
    Action.PLUG_IN: "Check again",
    Action.WAIT: None,
    Action.RETRY: "Try again",
    Action.RECOVER: "Use my recovery file",
    Action.FIX_PASSWORD: None,
    Action.CHOOSE_WIFI: None,
    Action.MOVE_CLOSER: "Try again",
    Action.HOST_TAILSCALE_LOGIN: "Sign in",
    Action.OPEN_TAILSCALE_APP: "Open Tailscale",
    Action.OPEN_URL: "Open in browser",
    Action.PI_TAILSCALE_RELOGIN: "Sign in again",
    Action.REBOOT: "Restart the WeatherPi",
    Action.UPDATE_APP: "How to update",
}

#: per check code: a better label for that particular check's button
CODE_LABELS: dict[str, str] = {
    "serve_not_enabled": "Open Tailscale settings",
    "host_login_url": "Open sign-in page",
    "pi_login_url": "Open sign-in page",
    "host_tailscale_stopped": "Connect",
}


def row_state(check: CheckResult) -> str:
    """``ok`` | ``warn`` (optional failure) | ``fail`` | ``running`` (waiting)."""
    if check.ok:
        return "ok"
    if check.action == Action.WAIT:
        return "running"
    return "warn" if check.optional else "fail"


class CheckRow(QFrame):
    action_clicked = Signal(object)
    help_clicked = Signal(object)

    def __init__(self, check: CheckResult, action_label: str | None, show_help: bool,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.check = check
        self.state = row_state(check)
        self.setObjectName("CheckRow")
        self.setProperty("state", self.state)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        outer = QHBoxLayout(self)
        outer.setContentsMargins(12, 7, 10, 7)
        outer.setSpacing(10)

        if self.state == "running":
            self.icon: QWidget = Spinner(20, self)
        else:
            icon = QLabel(self)
            dpr = self.devicePixelRatioF() if self.devicePixelRatioF() else 1.0
            icon.setPixmap(status_icon(self.state, 20, max(dpr, 2.0)))
            icon.setFixedSize(22, 22)
            icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
            icon.setAccessibleName({"ok": "Passed", "warn": "Warning",
                                    "fail": "Problem"}[self.state])
            self.icon = icon
        outer.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignTop)

        text_col = QVBoxLayout()
        text_col.setSpacing(2)
        self.message = QLabel(check.message_for_owner, self)
        self.message.setWordWrap(True)
        self.message.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        text_col.addWidget(self.message)
        self.detail = QLabel(check.detail, self)
        self.detail.setWordWrap(True)
        self.detail.setProperty("muted", True)
        self.detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.detail.hide()
        text_col.addWidget(self.detail)
        outer.addLayout(text_col, 1)

        self.buttons = QHBoxLayout()
        self.buttons.setSpacing(6)
        self.details_button: QPushButton | None = None
        self.help_button: QPushButton | None = None
        self.action_button: QPushButton | None = None
        if check.detail and not check.ok:
            self.details_button = QPushButton("Details", self)
            set_role(self.details_button, "link")
            self.details_button.setCheckable(True)
            self.details_button.toggled.connect(self._toggle_detail)
            self.buttons.addWidget(self.details_button)
        if show_help and not check.ok and self.state != "running":
            self.help_button = QPushButton("Get help", self)
            set_role(self.help_button, "link")
            self.help_button.clicked.connect(lambda: self.help_clicked.emit(self.check))
            self.buttons.addWidget(self.help_button)
        if action_label and not check.ok:
            self.action_button = QPushButton(action_label, self)
            self.action_button.clicked.connect(lambda: self.action_clicked.emit(self.check))
            self.buttons.addWidget(self.action_button)
        outer.addLayout(self.buttons, 0)
        outer.setAlignment(self.buttons, Qt.AlignmentFlag.AlignVCenter)
        repolish(self)

    def _toggle_detail(self, on: bool) -> None:
        self.detail.setVisible(on)
        if self.details_button:
            self.details_button.setText("Hide details" if on else "Details")


class CheckList(QWidget):
    """Shows a list of checks. Connect ``action_requested`` to run the fix; set
    ``help_handler`` to show "Get help" on failed rows."""

    action_requested = Signal(object)
    help_requested = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(5)
        self._checks: list[CheckResult] = []
        self.rows: list[CheckRow] = []
        self.action_labels: dict[str, str | None] = {}
        self.code_labels: dict[str, str | None] = {}
        #: optional: rewrite a check before it's shown (message overrides etc.)
        self.transform: Callable[[CheckResult], CheckResult] | None = None
        self.show_help = True
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)

    # ---- content

    @property
    def checks(self) -> list[CheckResult]:
        return list(self._checks)

    def label_for(self, check: CheckResult) -> str | None:
        if check.code in self.code_labels:
            return self.code_labels[check.code]
        if check.code in CODE_LABELS:
            return CODE_LABELS[check.code]
        if check.action is None:
            return None
        if check.action in self.action_labels:
            return self.action_labels[check.action]
        return ACTION_LABELS.get(check.action, "Fix it")

    def set_checks(self, checks: list[CheckResult]) -> None:
        if self.transform is not None:
            checks = [self.transform(c) for c in checks]
        self.clear()
        self._checks = list(checks)
        for c in checks:
            if not c.ok:
                log.info("check %s: %s (%s)", c.code, c.message_for_owner, c.detail)
            row = CheckRow(c, self.label_for(c), self.show_help, self)
            row.action_clicked.connect(self.action_requested)
            row.help_clicked.connect(self.help_requested)
            self._layout.addWidget(row)
            self.rows.append(row)
        self.setVisible(bool(checks))

    def add_checks(self, checks: list[CheckResult]) -> None:
        self.set_checks(self._checks + list(checks))

    def replace_check(self, code: str, new: CheckResult) -> None:
        """Swap the row with ``code`` (or append) and redraw."""
        out = [new if c.code == code else c for c in self._checks]
        if not any(c.code == code for c in self._checks):
            out.append(new)
        self.set_checks(out)

    def set_running(self, message: str) -> None:
        """One "working on it" row (spinner)."""
        self.set_checks([CheckResult(False, "running", message, action=Action.WAIT)])

    def clear(self) -> None:
        for row in self.rows:
            row.setParent(None)
            row.deleteLater()
        self.rows = []
        self._checks = []

    def row(self, code: str) -> CheckRow | None:
        return next((r for r in self.rows if r.check.code == code), None)

    def codes(self) -> list[str]:
        return [c.code for c in self._checks]
