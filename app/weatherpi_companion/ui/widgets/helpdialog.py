"""The "Get help" dialog: builds the AI help pack (via the core) in the background, lets
the owner read/edit it, then copy it or save it as a file."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import QStandardPaths, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ... import aihelp
from ...agent_client import AgentError
from ...checks import CheckResult
from ...flows import SetupSession
from ..style import label, set_role
from ..worker import run_task
from .busy import BusyBar


def setup_help_pack(session: SetupSession | None, checks: list[CheckResult],
                    owner_text: str = "") -> str:
    """Build the pack during setup (runs on a worker thread): the page's checks, what the
    Pi says about itself, its diagnostics if we're paired, and the app log."""
    diag: dict[str, Any] | None = None
    err: str | None = None
    station: dict[str, Any] | None = None
    known: list[str] = []
    log_tail = ""
    if session is not None:
        log_tail = session.env.app_data.log_tail()
        if session.info is not None:
            station = dict(session.info.raw or {"serial": session.info.serial})
        if session.secrets is not None:
            known = session.secrets.known_values()
            try:
                diag = session.usb_client().diagnostics()
            except AgentError as exc:
                err = exc.message or exc.code
        else:
            err = "not paired with this computer yet (no diagnostics before the password step)"
    return aihelp.build_help_pack(owner_description=owner_text, diagnostics=diag,
                                  diagnostics_error=err, host_checks=checks, station=station,
                                  app_log_tail=log_tail, known_secrets=known)


class HelpDialog(QDialog):
    def __init__(self, build: Callable[[str], str], problem: str = "",
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Get help from an AI assistant")
        self.resize(640, 560)
        self._build = build
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 16)
        lay.setSpacing(10)
        title = label("Get help", "PageTitle")
        lay.addWidget(title)
        lay.addWidget(label(
            "We'll put together a description of your WeatherPi and this computer that you can "
            "paste into any AI chat (for example ChatGPT, Claude or Gemini). Passwords and keys "
            "are removed first. You can read and change the text before you share it.",
            "Intro"))
        lay.addWidget(label("What's going wrong? (optional)", "SectionHeading"))
        self.problem = QPlainTextEdit(self)
        self.problem.setPlaceholderText("For example: the WiFi step keeps failing")
        self.problem.setPlainText(problem)
        self.problem.setFixedHeight(70)
        self.problem.setTabChangesFocus(True)
        lay.addWidget(self.problem)
        row = QHBoxLayout()
        self.build_button = QPushButton("Create help text", self)
        set_role(self.build_button, "primary")
        self.build_button.clicked.connect(self.build)
        row.addWidget(self.build_button)
        row.addStretch(1)
        lay.addLayout(row)
        self.busy = BusyBar(self)
        lay.addWidget(self.busy)
        self.output = QPlainTextEdit(self)
        self.output.setPlaceholderText("The help text will appear here.")
        self.output.setTabChangesFocus(True)
        lay.addWidget(self.output, 1)
        self.error = label("", "Muted")
        self.error.hide()
        lay.addWidget(self.error)
        bottom = QHBoxLayout()
        self.copy_button = QPushButton("Copy to clipboard", self)
        self.save_button = QPushButton("Save as file...", self)
        close = QPushButton("Close", self)
        for b in (self.copy_button, self.save_button):
            b.setEnabled(False)
        self.copy_button.clicked.connect(self.copy)
        self.save_button.clicked.connect(self.save)
        close.clicked.connect(self.accept)
        bottom.addWidget(self.copy_button)
        bottom.addWidget(self.save_button)
        bottom.addStretch(1)
        bottom.addWidget(close)
        lay.addLayout(bottom)
        self.saved_path: Path | None = None

    def build(self) -> None:
        text = self.problem.toPlainText()
        self.build_button.setEnabled(False)
        self.error.hide()
        self.busy.start("Collecting information (this can take a few seconds)...")
        run_task(lambda _t: self._build(text), self._built, on_error=self._failed, owner=self)

    def _built(self, text: str) -> None:
        self.busy.stop()
        self.build_button.setEnabled(True)
        self.build_button.setText("Create again")
        self.output.setPlainText(text)
        self.copy_button.setEnabled(True)
        self.save_button.setEnabled(True)
        self.copy_button.setFocus(Qt.FocusReason.OtherFocusReason)

    def _failed(self, exc: BaseException) -> None:
        self.busy.stop()
        self.build_button.setEnabled(True)
        self.error.setText(f"Couldn't collect everything: {exc}")
        self.error.show()

    def copy(self) -> None:
        QGuiApplication.clipboard().setText(self.output.toPlainText())
        self.copy_button.setText("Copied")

    def save(self) -> None:
        docs = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.DocumentsLocation) or str(Path.home())
        path, _ = QFileDialog.getSaveFileName(self, "Save help text",
                                              str(Path(docs) / aihelp.pack_filename()),
                                              "Text (*.md *.txt)")
        if path:
            Path(path).write_text(self.output.toPlainText(), encoding="utf-8")
            self.saved_path = Path(path)
            self.save_button.setText("Saved")
