"""``tester`` section, "Check the Pi" (wizard pages 1-3): what you'll need, this
computer, plug in (live USB detection), then "Your WeatherPi is working and ready for
setup". Also handles an already-claimed Pi: resume when this PC has its keys, otherwise
take it over with the recovery password."""

from __future__ import annotations

import dataclasses
from pathlib import Path

from PySide6.QtCore import QStandardPaths, Qt
from PySide6.QtWidgets import (
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...checks import Action, CheckResult, all_ok
from ...flows import PlugInResult, password_strength, validate_password
from ...secrets import read_recovery_password
from ..style import label, set_role, status_icon
from ..widgets.diagram import PlugInDiagram
from ..widgets.wizard import WizardPage, WizardSection

#: our own action for the boot-ROM case: go to the flasher
FLASH_CARD = "flash_card"
PLUG_IN_TIMEOUT_S = 180.0


def _bullets(items: list[tuple[str, str]]) -> QWidget:
    w = QWidget()
    grid = QGridLayout(w)
    grid.setContentsMargins(0, 0, 0, 0)
    grid.setHorizontalSpacing(12)
    grid.setVerticalSpacing(10)
    for i, (head, text) in enumerate(items):
        icon = QLabel()
        icon.setPixmap(status_icon("info", 18, 2.0))
        icon.setFixedSize(20, 20)
        grid.addWidget(icon, i, 0, Qt.AlignmentFlag.AlignTop)
        lab = label(f"<b>{head}</b>" + (f"<br><span style='color:#6b7280'>{text}</span>"
                                        if text else ""), rich=True)
        grid.addWidget(lab, i, 1)
    grid.setColumnStretch(1, 1)
    return w


# --------------------------------------------------------------------------- page 1


class NeedPage(WizardPage):
    TITLE = "What you'll need"
    CAPTION = "What you'll need"
    INTRO = ("We'll check that this computer and your WeatherPi can talk to each other. "
             "Have these ready:")

    def __init__(self, ctx, parent=None) -> None:
        super().__init__(ctx, parent)
        self.body.addWidget(_bullets([
            ("Your WeatherPi", "with its SD card pushed in."),
            ("A micro-USB data cable",
             ("It connects the WeatherPi to this computer and powers it too. Some cables "
             "only charge and can't carry data; if the WeatherPi isn't found, try another "
             "cable.")),
            ("Your WiFi name and password", "for the network the WeatherPi will use."),
            ("About 15 minutes", ""),
        ]))
        self.set_complete(True)

    def on_enter(self) -> None:
        if self.ctx.env.app_data.list_profiles():
            self.set_title("Welcome back", "Let's reconnect to your WeatherPi and carry on "
                           "where you left off. You'll need:")


# --------------------------------------------------------------------------- page 2


class ComputerPage(WizardPage):
    TITLE = "This computer"
    CAPTION = "This computer"
    INTRO = ("This computer needs a small USB driver to talk to the WeatherPi, and the free "
             "Tailscale app to reach it later from anywhere.")

    def __init__(self, ctx, parent=None) -> None:
        super().__init__(ctx, parent)
        self.actions[Action.INSTALL_DRIVER] = lambda _c: self.install_driver()
        self.actions[Action.INSTALL_TAILSCALE] = self._install_tailscale
        row = QHBoxLayout()
        self.again = QPushButton("Check again", self)
        self.again.clicked.connect(self.refresh)
        row.addWidget(self.again)
        row.addStretch(1)
        self.after.addLayout(row)
        self.note = label("", "Muted")
        self.note.hide()
        self.after.addWidget(self.note)

    def on_enter(self) -> None:
        self.refresh()

    def refresh(self) -> None:
        s = self.session
        self.run(lambda _t: s.check_this_computer(), self._show,
                 busy="Checking this computer...")

    def _show(self, checks: list[CheckResult]) -> None:
        self.checks.set_checks(checks)
        self.set_complete(all_ok(checks))

    def install_driver(self) -> None:
        s = self.session

        def work(_t):
            inst = s.install_driver()
            comp = s.check_this_computer()
            if all_ok(inst):
                return comp + [c for c in inst[1:] if not c.ok]
            return inst + comp[1:]

        self.run(work, self._show, busy="Installing the USB driver. If Windows asks for "
                 "permission, click Yes...")

    def _install_tailscale(self, check: CheckResult) -> None:
        from .. import desktop

        desktop.open_url(check.data.get("url") or "https://tailscale.com/download")
        self.note.setText("Your browser opened Tailscale's download page. Install Tailscale, "
                          "then come back and click Check again.")
        self.note.show()


# --------------------------------------------------------------------------- page 3


class RecoveryPanel(QFrame):
    """Take over a WeatherPi that was set up from another computer."""

    def __init__(self, page: PlugInPage) -> None:
        super().__init__(page)
        self.page = page
        self.setObjectName("Card")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(10)
        lay.addWidget(label("Take over this WeatherPi", "SectionHeading"))
        lay.addWidget(label(
            "If it's yours, use the recovery password from its RECOVERY.txt file (made when it "
            "was first set up). You'll choose a new password, and this computer will be able "
            "to manage it.", "Intro"))
        form = QFormLayout()
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(8)
        rec_row = QHBoxLayout()
        self.recovery = QLineEdit(self)
        self.recovery.setPlaceholderText("xxxxx-xxxxx-xxxxx-xxxxx-xxxxx")
        self.recovery.setAccessibleName("Recovery password")
        self.choose = QPushButton("Choose RECOVERY.txt...", self)
        self.choose.clicked.connect(self.choose_file)
        rec_row.addWidget(self.recovery, 1)
        rec_row.addWidget(self.choose)
        form.addRow("Recovery password", rec_row)
        self.password = QLineEdit(self)
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setAccessibleName("New password")
        self.confirm = QLineEdit(self)
        self.confirm.setEchoMode(QLineEdit.EchoMode.Password)
        self.confirm.setAccessibleName("Type the new password again")
        form.addRow("New password", self.password)
        form.addRow("Type it again", self.confirm)
        lay.addLayout(form)
        self.hint = label("Leave the recovery password empty if this computer has the "
                          "RECOVERY.txt file already; we'll find it.", "Muted")
        lay.addWidget(self.hint)
        self.strength = label("", "Muted")
        lay.addWidget(self.strength)
        row = QHBoxLayout()
        self.go = QPushButton("Take over", self)
        set_role(self.go, "primary")
        self.go.clicked.connect(self.submit)
        self.no_file = QPushButton("I don't have the recovery file", self)
        set_role(self.no_file, "link")
        self.no_file.clicked.connect(self._no_file)
        row.addWidget(self.go)
        row.addStretch(1)
        row.addWidget(self.no_file)
        lay.addLayout(row)
        self.no_file_text = label(
            "Without the recovery file, the only way back in is to start fresh: flash the SD "
            "card again (the weather it has recorded will be lost unless you have a backup).",
            "Muted")
        self.no_file_text.hide()
        self.flash = QPushButton("Flash the SD card again", self)
        self.flash.hide()
        self.flash.clicked.connect(lambda: page.ctx.go("flasher"))
        lay.addWidget(self.no_file_text)
        lay.addWidget(self.flash, 0, Qt.AlignmentFlag.AlignLeft)
        for w in (self.password, self.confirm):
            w.textChanged.connect(self._update)
        self.confirm.returnPressed.connect(self.submit)
        self._update()

    def _update(self) -> None:
        pw, again = self.password.text(), self.confirm.text()
        st = password_strength(pw)
        self.strength.setText(f"Password strength: {st.label}" if pw else "")
        self.go.setEnabled(all_ok(validate_password(pw, again)))

    def _no_file(self) -> None:
        self.no_file_text.show()
        self.flash.show()

    def choose_file(self) -> None:
        start = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.DocumentsLocation) or str(Path.home())
        path, _ = QFileDialog.getOpenFileName(self, "Choose RECOVERY.txt", start,
                                              "Recovery file (RECOVERY.txt *.txt)")
        if path:
            self.load_file(Path(path))

    def load_file(self, path: Path) -> None:
        rp = read_recovery_password(path)
        if rp:
            self.recovery.setText(rp)
            self.hint.setText(f"Recovery password read from {path.name}.")
        else:
            self.hint.setText(f"{path.name} doesn't contain a recovery password.")

    def submit(self) -> None:
        if not self.go.isEnabled():
            return
        s = self.page.session
        pw, again, rp = self.password.text(), self.confirm.text(), self.recovery.text().strip()
        self.page.run(lambda _t: s.recover_access(pw, again, recovery_password=rp or None),
                      self.page.recovered, busy="Taking over the WeatherPi...")


class PlugInPage(WizardPage):
    TITLE = "Plug in the WeatherPi"
    CAPTION = "Plug in"
    INTRO = ""

    def __init__(self, ctx, parent=None) -> None:
        super().__init__(ctx, parent)
        self.instructions = label(
            "1.  Plug the data cable into the WeatherPi's port marked USB (the middle one), and "
            "the other end into this computer. That also powers the WeatherPi.\n"
            "2.  Don't plug anything into PWR, and don't use a power supply now. Power from "
            "both ports at once can destroy the WeatherPi.\n"
            "3.  Wait. The first start takes 2 to 3 minutes; we'll find it automatically.",
            "Intro")
        self.body.addWidget(self.instructions)
        self.diagram = PlugInDiagram(self)
        self.body.addWidget(self.diagram)
        self.result: PlugInResult | None = None
        self._watch = None
        self._active = False
        self.checks.transform = self._transform
        self.checks.code_labels["usb_boot_rom"] = "Flash the SD card again"
        self.actions[FLASH_CARD] = lambda _c: self.ctx.go("flasher")
        self.actions[Action.INSTALL_DRIVER] = self._install_driver
        self.actions[Action.RECOVER] = lambda _c: self._show_recovery()
        self.recovery = RecoveryPanel(self)
        self.recovery.hide()
        self.after.addWidget(self.recovery)
        row = QHBoxLayout()
        self.again = QPushButton("Look again", self)
        self.again.clicked.connect(self.refresh)
        self.again.hide()
        row.addWidget(self.again)
        row.addStretch(1)
        self.after.addLayout(row)

    @staticmethod
    def _transform(c: CheckResult) -> CheckResult:
        if c.code == "usb_boot_rom":
            return dataclasses.replace(
                c, message_for_owner="The WeatherPi can't read its SD card. Unplug the cable, "
                "push the card in firmly (re-seat it) and plug the cable back in. If that "
                "doesn't help, the card needs to be flashed again.", action=FLASH_CARD)
        return c

    def on_enter(self) -> None:
        self._active = True
        if self.result is None or self.result.next_step == "wait":
            self.refresh()

    def refresh(self) -> None:
        """Start (or restart) watching for the Pi."""
        if self._watch is not None and self._watch.running:
            return
        self.set_complete(False)
        self.recovery.hide()
        self.again.hide()
        self._show_instructions(True)
        s = self.session
        holder: list = []
        self._watch = self.run(
            lambda t: s.wait_for_plug_in(timeout=PLUG_IN_TIMEOUT_S, cancel=t.cancel_event,
                                         on_progress=t.report),
            lambda res: self._found(res, holder[0]), busy="Looking for your WeatherPi...",
            on_progress=self._progress, cancel_text="Stop looking", on_cancel=self.again.show)
        holder.append(self._watch)

    def _progress(self, value: PlugInResult | None, elapsed: float) -> None:
        secs = int(elapsed)
        self.busy.set_text(f"Looking for your WeatherPi... ({secs // 60}:{secs % 60:02d})")
        if value is not None:
            self.checks.set_checks(value.checks)

    def _found(self, res: PlugInResult, task=None) -> None:
        if not self._active or (task is not None and task.cancelled):
            return
        self.result = res
        self.checks.set_checks(res.checks)
        self._show_instructions(res.next_step == "wait")
        if res.next_step == "wait":
            self._watch = None
            self.refresh()  # timed out: keep looking while the page is open
            return
        if res.next_step == "claim":
            self.ctx.hints["setup_start"] = 4
            self.set_complete(True)
        elif res.next_step == "resume":
            self.ctx.hints["setup_start"] = res.resume_page or 5
            self.set_complete(True)
        elif res.next_step == "recover":
            self._show_recovery()
            self.set_complete(False)

    def _show_instructions(self, show: bool) -> None:
        """The picture is only needed until the WeatherPi is found."""
        self.instructions.setVisible(show)
        self.diagram.setVisible(show)
        self.set_title("Plug in the WeatherPi" if show else "Found your WeatherPi")

    def _show_recovery(self) -> None:
        self.recovery.show()
        self.recovery.recovery.setFocus()
        self.reveal(self.recovery)

    def _install_driver(self, _c: CheckResult) -> None:
        s = self.session
        if self._watch is not None:
            self._watch.cancel()
            self._watch = None
        self.run(lambda _t: s.install_driver(), self._driver_done,
                 busy="Installing the USB driver. If Windows asks for permission, click Yes...")

    def _driver_done(self, checks: list[CheckResult]) -> None:
        self.checks.set_checks(checks)
        if all_ok(checks):
            self._watch = None
            self.refresh()
        else:
            self.again.show()

    def recovered(self, checks: list[CheckResult]) -> None:
        self.checks.set_checks(checks)
        if all_ok(checks):
            s = self.session
            online = s.wifi is not None and s.wifi.state == "connected" and s.wifi.internet
            self.ctx.hints["setup_start"] = 6 if online else 5
            self.ctx.hints["recovered"] = True
            self.recovery.hide()
            self.set_complete(True)

    def on_leave(self) -> None:
        self._active = False
        super().on_leave()
        self._watch = None


# --------------------------------------------------------------------------- ready


class ReadyPage(WizardPage):
    TITLE = "Your WeatherPi is working and ready for setup"
    CAPTION = "Ready"
    INTRO = ""

    def __init__(self, ctx, parent=None) -> None:
        super().__init__(ctx, parent)
        self.next_label = "Continue to setup"
        head = QHBoxLayout()
        icon = QLabel(self)
        icon.setPixmap(status_icon("ok", 40, 2.0))
        icon.setFixedSize(44, 44)
        head.addWidget(icon, 0, Qt.AlignmentFlag.AlignTop)
        self.summary = label("", "Intro")
        head.addWidget(self.summary, 1)
        self.body.addLayout(head)
        card = QFrame(self)
        card.setObjectName("Card")
        self.grid = QFormLayout(card)
        self.grid.setContentsMargins(18, 14, 18, 14)
        self.grid.setHorizontalSpacing(24)
        self.grid.setVerticalSpacing(8)
        self.fields: dict[str, QLabel] = {}
        for key, text in (("model", "Model"), ("serial", "Serial number"),
                          ("image", "Software version"), ("agent", "Setup service")):
            v = label("")
            v.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            self.fields[key] = v
            self.grid.addRow(label(text, "Muted"), v)
        self.body.addWidget(card)
        self.check_again = QPushButton("Check again", self)
        set_role(self.check_again, "link")
        self.check_again.clicked.connect(self._again)
        self.after.addWidget(self.check_again, 0, Qt.AlignmentFlag.AlignLeft)

    def on_enter(self) -> None:
        s = self.session
        info = s.info
        start = self.ctx.hints.get("setup_start", 4)
        if self.ctx.hints.get("recovered"):
            self.set_title("You're back in control of your WeatherPi",
                           "You have a new password and this computer can manage the "
                           "WeatherPi. Next, we'll finish setting it up.")
        elif start > 4:
            self.set_title("Your WeatherPi is working",
                           "It's already set up with this computer, so we'll pick up where "
                           "you left off" + (" (its WiFi already works)." if start >= 6
                                             else "."))
        else:
            self.set_title(self.TITLE, "")
        self.summary.setText("This computer and the WeatherPi are talking to each other over "
                             "the USB cable. Keep it plugged in for the next steps.")
        if info is not None:
            self.fields["model"].setText(info.model or "Raspberry Pi")
            self.fields["serial"].setText(info.serial)
            self.fields["image"].setText(info.image_version or "unknown")
            self.fields["agent"].setText(f"version {info.agent_version}")
        self.set_complete(info is not None)

    def _again(self) -> None:
        if self.wizard is not None:
            prev = next(p for p in self.wizard.pages if isinstance(p, PlugInPage))
            prev.result = None
            self.wizard.go_to(prev)


# --------------------------------------------------------------------------- section


class TesterSection(WizardSection):
    SECTION_ID = "tester"
    TITLE = "Check the Pi"
    NEXT_SECTION = "setup"

    def build(self) -> None:
        self.need = self.wizard.add_page(NeedPage(self.ctx))
        self.computer = self.wizard.add_page(ComputerPage(self.ctx))
        self.plug_in = self.wizard.add_page(PlugInPage(self.ctx))
        self.ready = self.wizard.add_page(ReadyPage(self.ctx))


__all__ = ["TesterSection"]
