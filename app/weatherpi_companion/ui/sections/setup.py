"""``setup`` section, "Set up" (wizard pages 4-9): password, WiFi (one or more networks),
Tailscale on this PC, Tailscale on the WeatherPi, final checks over the tailnet, done."""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ...agent_client import WifiNetwork, WifiStatus
from ...checks import Action, CheckResult, all_ok
from ...flows import MIN_PASSWORD_LENGTH, PlugInResult, password_strength, validate_password
from .. import desktop
from ..style import FAIL, OK, TEXT, WARN, label, lock_icon, set_role, signal_bars_icon
from ..widgets.busy import BusyBar, Spinner
from ..widgets.checklist import CheckList
from ..widgets.wizard import WizardPage, WizardSection

log = logging.getLogger(__name__)

WIFI_TIMEOUT_S = 60.0
HOST_LOGIN_TIMEOUT_S = 300.0
PI_LOGIN_TIMEOUT_S = 600.0
RAIN_TEST_S = 60.0
#: the core methods for the saved-networks list (added by W2b; may be missing)
SAVED_NETWORK_METHODS = ("saved_networks", "save_network", "set_network_priority",
                         "forget_network")


def _password_field(name: str) -> QLineEdit:
    e = QLineEdit()
    e.setEchoMode(QLineEdit.EchoMode.Password)
    e.setAccessibleName(name)
    e.setMinimumWidth(260)
    return e


def _card(parent: QWidget | None = None) -> tuple[QFrame, QVBoxLayout]:
    f = QFrame(parent)
    f.setObjectName("Card")
    lay = QVBoxLayout(f)
    lay.setContentsMargins(18, 14, 18, 14)
    lay.setSpacing(8)
    return f, lay


def _row(*widgets: QWidget, stretch: bool = True) -> QHBoxLayout:
    r = QHBoxLayout()
    r.setSpacing(8)
    for w in widgets:
        r.addWidget(w)
    if stretch:
        r.addStretch(1)
    return r


# --------------------------------------------------------------------------- page 4


class StrengthMeter(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.bar = QProgressBar(self)
        self.bar.setRange(0, 4)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(8)
        self.bar.setMaximumWidth(260)
        self.text = label("", "Muted")
        self.text.setMinimumWidth(80)
        lay.addWidget(self.bar, 1)
        lay.addWidget(self.text)
        lay.addStretch(1)
        self.score = 0

    def set_password(self, pw: str) -> None:
        st = password_strength(pw)
        self.score = st.score
        self.bar.setValue(st.score)
        self.text.setText(st.label if pw else "")
        colour = {0: FAIL, 1: FAIL, 2: WARN, 3: OK, 4: OK}[st.score]
        self.bar.setStyleSheet(f"QProgressBar::chunk {{ background: {colour}; }}")
        self.bar.setAccessibleDescription(f"Password strength: {st.label}")


class PasswordPage(WizardPage):
    TITLE = "Create your password"
    CAPTION = "Password"
    INTRO = ("This password protects your WeatherPi. You won't need it day to day (this app "
             "pairs itself with the WeatherPi), but write it down somewhere safe.")

    def __init__(self, ctx, parent=None) -> None:
        super().__init__(ctx, parent)
        form = QFormLayout()
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(10)
        self.password = _password_field("Password")
        self.confirm = _password_field("Type the password again")
        self.meter = StrengthMeter(self)
        self.show_pw = QCheckBox("Show password", self)
        self.show_pw.toggled.connect(self._toggle_show)
        form.addRow("Password", self.password)
        form.addRow("", self.meter)
        form.addRow("Type it again", self.confirm)
        form.addRow("", self.show_pw)
        self.body.addLayout(form)
        self.problem = label("", "Muted")
        self.problem.setStyleSheet(f"color: {FAIL};")
        self.body.addWidget(self.problem)
        self.hint = label(f"At least {MIN_PASSWORD_LENGTH} characters. A few unrelated words "
                          "make a strong password that's easy to remember.", "Muted")
        self.body.addWidget(self.hint)
        self.set_button = QPushButton("Set password", self)
        set_role(self.set_button, "primary")
        self.set_button.clicked.connect(self.submit)
        self.body.addLayout(_row(self.set_button))
        self.recovery_note = label("", "Muted", rich=True)
        self.recovery_note.hide()
        self.after.addWidget(self.recovery_note)
        self.password.textChanged.connect(self._changed)
        self.confirm.textChanged.connect(self._changed)
        self.password.returnPressed.connect(self.confirm.setFocus)
        self.confirm.returnPressed.connect(self.submit)
        self.actions[Action.RETRY] = self._retry
        self.actions[Action.RECOVER] = self._recover
        self.actions[Action.PLUG_IN] = self._retry  # "check the cable, then try again"
        self.done = False
        self._pw = ""
        self._changed()

    def _toggle_show(self, on: bool) -> None:
        mode = QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password
        self.password.setEchoMode(mode)
        self.confirm.setEchoMode(mode)

    def _changed(self) -> None:
        pw, again = self.password.text(), self.confirm.text()
        self.meter.set_password(pw)
        problems = [c for c in validate_password(pw, again) if not c.ok]
        shown = [c for c in problems if again or c.code != "password_mismatch"]
        self.problem.setText(shown[0].message_for_owner if shown and pw else "")
        self.problem.setVisible(bool(shown and pw))
        self.set_button.setEnabled(not problems and not self.done and not self.busy_now)

    def on_enter(self) -> None:
        if not self.done:
            self.password.setFocus()

    def submit(self) -> None:
        if not self.set_button.isEnabled():
            return
        s = self.session
        pw, again = self.password.text(), self.confirm.text()
        self._pw = pw
        self.set_button.setEnabled(False)
        self.run(lambda _t: s.create_password(pw, again), self._result,
                 busy="Setting your password and pairing this computer with the WeatherPi...")

    def _result(self, checks: list[CheckResult]) -> None:
        self.checks.set_checks(checks)
        claimed = any(c.code == "claimed" and c.ok for c in checks)
        if claimed:
            self.done = True
            for w in (self.password, self.confirm, self.show_pw):
                w.setEnabled(False)
            self.password.clear()
            self.confirm.clear()
            self.set_button.setText("Password set")
            self.meter.hide()
            self.hint.hide()
            self.problem.hide()
            path = next((c.data.get("recovery_file") for c in checks
                         if c.data.get("recovery_file")), None)
            if path:
                self.recovery_note.setText(
                    "We saved a recovery file that lets you reset this password if you ever "
                    f"forget it:<br><b>{path}</b><br>Keep it safe.")
                self.recovery_note.show()
        self._changed()
        complete = claimed and all_ok(checks)
        if complete:
            self._pw = ""  # never kept longer than needed
        self.set_complete(complete)

    def _retry(self, check: CheckResult) -> None:
        s = self.session
        if not self.done:
            self.submit()
        elif check.code.startswith("ssh"):
            self.run(lambda _t: s.check_ssh(), lambda c: self._replace(check.code, c),
                     busy="Checking the secure login again...")
        else:
            # already claimed: create_password() skips the claim and re-runs the pairing
            # checks (it still validates the password, so we keep it until they pass)
            pw = self._pw
            self.run(lambda _t: s.create_password(pw, pw), self._result,
                     busy="Checking again...")

    def _replace(self, code: str, check: CheckResult) -> None:
        self.checks.replace_check(code, check)
        self.set_complete(all_ok(self.checks.checks))

    def _recover(self, _c: CheckResult) -> None:
        self.ctx.hints["tester_page"] = "plug_in"
        self.ctx.go("tester")


# --------------------------------------------------------------------------- page 5


class NetworkRow(QWidget):
    """One scan result: signal bars, name, tags, lock."""

    def __init__(self, net: WifiNetwork | None, text: str | None = None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 6)
        lay.setSpacing(10)
        bars = QLabel(self)
        bars.setFixedSize(22, 20)
        if net is not None:
            bars.setPixmap(signal_bars_icon(net.bars, 18, 2.0))
            bars.setToolTip(f"Signal {net.signal}%")
        lay.addWidget(bars)
        name = QLabel(net.ssid if net else (text or ""), self)
        if net is None:
            name.setStyleSheet("color: #2563eb;")
        lay.addWidget(name, 1)
        if net is not None:
            if net.in_use:
                lay.addWidget(_tag("Connected", OK))
            elif net.known:
                lay.addWidget(_tag("Saved", "#6b7280"))
            lock = QLabel(self)
            lock.setFixedSize(16, 16)
            if net.secured:
                lock.setPixmap(lock_icon(14, 2.0))
                lock.setToolTip(f"Needs a password ({net.security})")
            lay.addWidget(lock)
        self.setAccessibleName(_net_description(net) if net else (text or ""))


def _tag(text: str, colour: str) -> QLabel:
    t = QLabel(text)
    t.setStyleSheet(f"color: {colour}; border: 1px solid {colour}; border-radius: 8px; "
                    "padding: 0px 7px; font-size: 8.5pt;")
    return t


def _net_description(net: WifiNetwork) -> str:
    bits = [net.ssid, f"signal {net.bars} of 4"]
    bits.append("needs a password" if net.secured else "open")
    if net.in_use:
        bits.append("connected")
    elif net.known:
        bits.append("saved")
    return ", ".join(bits)


class AddNetworkDialog(QDialog):
    """Add a network the WeatherPi should join later (it can't be checked now)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Add a WiFi network")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 16)
        lay.setSpacing(10)
        lay.addWidget(label("Add a WiFi network", "SectionHeading"))
        lay.addWidget(label("The WeatherPi will join this network by itself when it's in "
                            "range. We can't check the password until then, so type it "
                            "carefully.", "Muted"))
        form = QFormLayout()
        self.ssid = QLineEdit(self)
        self.ssid.setAccessibleName("Network name")
        self.password = _password_field("WiFi password")
        self.open_net = QCheckBox("This network has no password", self)
        self.hidden = QCheckBox("It's a hidden network", self)
        form.addRow("Network name", self.ssid)
        form.addRow("Password", self.password)
        form.addRow("", self.open_net)
        form.addRow("", self.hidden)
        lay.addLayout(form)
        self.problem = label("", "Muted")
        self.problem.setStyleSheet(f"color: {FAIL};")
        lay.addWidget(self.problem)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save
                                        | QDialogButtonBox.StandardButton.Cancel, self)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        lay.addWidget(self.buttons)
        self.open_net.toggled.connect(self.password.setDisabled)
        for sig in (self.ssid.textChanged, self.password.textChanged, self.open_net.toggled):
            sig.connect(self._validate)
        self._validate()

    def values(self) -> tuple[str, str | None, bool]:
        pw = None if self.open_net.isChecked() else self.password.text()
        return self.ssid.text().strip("\r\n"), pw, self.hidden.isChecked()

    def _validate(self) -> None:
        ssid, pw, _hidden = self.values()
        msg = ""
        if not ssid.strip():
            msg = " "
        elif pw is not None and not (8 <= len(pw) <= 63 or (len(pw) == 64 and _is_hex(pw))):
            msg = "WiFi passwords are 8 to 63 characters long." if pw else " "
        self.problem.setText(msg.strip())
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setEnabled(not msg)


def _is_hex(s: str) -> bool:
    return all(c in "0123456789abcdefABCDEF" for c in s)


def _networks(result: Any) -> tuple[list[Any], list[CheckResult]]:
    """Accept what ``SetupSession.saved_networks()`` returns: a list of ``SavedNetwork``,
    an object with ``.networks`` (and ``.checks``), or a ``(checks, networks)`` pair."""
    if result is None:
        return [], []
    if hasattr(result, "networks"):
        return list(result.networks or []), list(getattr(result, "checks", []) or [])
    if isinstance(result, tuple) and len(result) == 2:
        a, b = result
        if a and isinstance(a[0], CheckResult):
            return list(b or []), list(a)
        return list(a or []), list(b or [])
    items = list(result)
    checks = [x for x in items if isinstance(x, CheckResult)]
    return [x for x in items if not isinstance(x, CheckResult)], checks


def _checks(result: Any) -> list[CheckResult]:
    if result is None:
        return []
    if isinstance(result, CheckResult):
        return [result]
    if hasattr(result, "checks"):
        return list(result.checks)
    if isinstance(result, tuple):
        return [x for part in result for x in (part if isinstance(part, list) else [part])
                if isinstance(x, CheckResult)]
    return [x for x in result if isinstance(x, CheckResult)]


def new_priorities(count: int) -> list[int]:
    """Priorities for ``count`` networks, top of the list first, within -100..100."""
    if count <= 0:
        return []
    step = max(1, min(10, 100 // max(1, count - 1)))
    return [min(100, (count - 1 - i) * step) for i in range(count)]


class SavedNetworksPanel(QWidget):
    """The "Saved networks" list: add networks not in range, reorder, remove.

    Used by setup (:class:`MoreWifiPage`, over USB) and the home screen (usually over
    Tailscale). The ``host`` widget provides ``saved_backend()`` (an object with
    ``saved_networks``/``save_network``/``set_network_priority``/``forget_network``, like
    ``flows.SavedNetworks``), ``run(fn, done, on_error=...)``, ``edit_in_use(ssid,
    password, hidden)`` for new details of the network in use, and ``remote`` (True when the
    network in use carries the link, so it can't be removed from here).
    """

    def __init__(self, host: QWidget) -> None:
        super().__init__(host)
        self.host = host
        self.networks: list[Any] = []
        self._select_after: int | None = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(10)
        self.stack = QStackedWidget(self)
        lay.addWidget(self.stack)
        # unsupported
        self.unsupported = label("Saving extra networks needs a newer app core. For now the "
                                 "WeatherPi remembers the network you connect it to.", "Muted")
        self.unsupported.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.stack.addWidget(self.unsupported)
        # supported
        box = QWidget(self)
        v = QVBoxLayout(box)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)
        #: hosts whose page already explains this hide it
        self.hint = label("The WeatherPi joins the highest network on this list that's in "
                          "range. Add networks it will need later, like a holiday home or a "
                          "phone hotspot.", "Muted")
        v.addWidget(self.hint)
        mid = QHBoxLayout()
        self.list = QListWidget(box)
        self.list.setAccessibleName("Saved networks, highest priority first")
        self.list.setMinimumHeight(110)
        self.list.currentRowChanged.connect(self._update_buttons)
        mid.addWidget(self.list, 1)
        col = QVBoxLayout()
        col.setSpacing(6)
        self.add_button = QPushButton("Add a WiFi network...", box)
        set_role(self.add_button, "primary")
        self.up_button = QPushButton("Move up", box)
        self.down_button = QPushButton("Move down", box)
        self.remove_button = QPushButton("Remove", box)
        for b in (self.add_button, self.up_button, self.down_button, self.remove_button):
            col.addWidget(b)
        col.addStretch(1)
        mid.addLayout(col)
        v.addLayout(mid)
        self.busy = BusyBar(box)
        v.addWidget(self.busy)
        self.checks = CheckList(box)
        self.checks.show_help = False
        self.checks.hide()
        self.checks.action_requested.connect(lambda _c: self.reload())
        v.addWidget(self.checks)
        self.stack.addWidget(box)
        self.add_button.clicked.connect(self.add)
        self.up_button.clicked.connect(lambda: self.move(-1))
        self.down_button.clicked.connect(lambda: self.move(1))
        self.remove_button.clicked.connect(self.remove)
        self.stack.setCurrentIndex(1 if self.supported else 0)
        self._update_buttons()

    @property
    def supported(self) -> bool:
        b = self.host.saved_backend()
        return all(callable(getattr(b, m, None)) for m in SAVED_NETWORK_METHODS)

    def _call(self, name: str, *args: Any, **kw: Any) -> Any:
        return getattr(self.host.saved_backend(), name)(*args, **kw)

    def _work(self, fn: Callable[[], Any], busy: str) -> None:
        self.busy.start(busy)
        for b in (self.add_button, self.up_button, self.down_button, self.remove_button):
            b.setEnabled(False)
        self.host.run(lambda _t: fn(), self._loaded, on_error=self._failed)

    def reload(self) -> None:
        self.stack.setCurrentIndex(1 if self.supported else 0)
        if not self.supported:
            return
        def load():
            nets, checks = _networks(self._call("saved_networks"))
            # saved_networks() never raises; it says why the list is empty here instead
            problem = getattr(self.host.saved_backend(), "saved_networks_problem", None)
            return (nets, checks + ([problem] if problem is not None else [])), []

        self._work(load, "Reading the saved networks...")

    def _loaded(self, result: tuple[tuple[list[Any], list[CheckResult]], list[CheckResult]]
                ) -> None:
        (nets, list_checks), action_checks = result
        self.busy.stop()
        too_old = next((c for c in action_checks + list_checks
                        if c.code == "wifi_saved_unsupported"), None)
        if too_old is not None:  # the Pi runs agent < 2.1.0: explain instead of an empty list
            self.unsupported.setText(too_old.message_for_owner + " For now the WeatherPi "
                                     "remembers the network you connect it to.")
            self.stack.setCurrentWidget(self.unsupported)
            self.networks = []
            self._update_buttons()
            return
        self.stack.setCurrentIndex(1)
        self.networks = list(nets)
        self.list.clear()
        for i, n in enumerate(self.networks, 1):
            tags = []
            if getattr(n, "in_use", False):
                tags.append("in use now")
            if getattr(n, "hidden", False):
                tags.append("hidden")
            text = f"{i}.  {n.ssid}" + (f"   ({', '.join(tags)})" if tags else "")
            item = QListWidgetItem(text)
            item.setSizeHint(QSize(0, 30))
            item.setData(Qt.ItemDataRole.AccessibleTextRole, text)
            self.list.addItem(item)
        if not self.networks:
            item = QListWidgetItem("No saved networks yet.")
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.list.addItem(item)
        problems = [c for c in action_checks + list_checks if not c.ok]
        self.checks.set_checks(problems)
        if self._select_after is not None and self._select_after < len(self.networks):
            self.list.setCurrentRow(self._select_after)
        self._select_after = None
        self._update_buttons()
        changed = getattr(self.host, "networks_changed", None)
        if callable(changed):
            changed()

    def _failed(self, exc: BaseException) -> None:
        self.busy.stop()
        self.checks.set_checks([CheckResult(False, "saved_failed", "Couldn't update the saved "
                                            "networks.", str(exc), Action.RETRY)])
        self._update_buttons()

    def _update_buttons(self) -> None:
        row = self.list.currentRow()
        n = len(self.networks)
        has = 0 <= row < n
        idle = not self.busy.active
        self.add_button.setEnabled(idle)
        self.up_button.setEnabled(idle and has and row > 0)
        self.down_button.setEnabled(idle and has and row < n - 1)
        self.remove_button.setEnabled(idle and has)

    def add(self) -> None:
        dlg = AddNetworkDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        self.add_network(*dlg.values())

    def add_network(self, ssid: str, password: str | None, hidden: bool) -> None:
        if any(n.ssid == ssid and getattr(n, "in_use", False) for n in self.networks):
            # the network the Pi is on: test the new details now instead of saving quietly
            self.host.edit_in_use(ssid, password, hidden)
            return
        prios = [getattr(n, "priority", 0) for n in self.networks]
        prio = max(-100, min(prios) - 1) if prios else 0

        def work():
            res = _checks(self._call("save_network", ssid, password, hidden=hidden,
                                     priority=prio))
            return _networks(self._call("saved_networks")), res
        self._work(work, f"Saving {ssid}...")

    def move(self, delta: int) -> None:
        row = self.list.currentRow()
        target = row + delta
        if not (0 <= row < len(self.networks) and 0 <= target < len(self.networks)):
            return
        order = list(self.networks)
        order[row], order[target] = order[target], order[row]
        self.reorder(order, select=target)

    def reorder(self, order: list[Any], select: int | None = None) -> None:
        prios = new_priorities(len(order))
        changes = [(n.ssid, p) for n, p in zip(order, prios, strict=True)
                   if getattr(n, "priority", None) != p]

        def work():
            res: list[CheckResult] = []
            for ssid, p in changes:
                res += _checks(self._call("set_network_priority", ssid, p))
            return _networks(self._call("saved_networks")), res
        self._select_after = select
        self._work(work, "Changing the order...")

    def remove(self) -> None:
        row = self.list.currentRow()
        if not 0 <= row < len(self.networks):
            return
        net = self.networks[row]
        if getattr(net, "in_use", False) and getattr(self.host, "remote", False):
            self.checks.set_checks([CheckResult(
                False, "wifi_in_use_remote", f"The WeatherPi is using {net.ssid} right now, "
                "and this computer reaches it through that network, so it can't be removed "
                "from here. Plug the WeatherPi into this computer with the USB cable and "
                "choose Set up again (Settings) to change it.")])
            return
        warn = (" The WeatherPi is using it right now and will switch to another saved "
                "network, or go offline." if getattr(net, "in_use", False) else "")
        if QMessageBox.question(self, "Remove network",
                                f"Remove {net.ssid} from the WeatherPi?{warn}") != \
                QMessageBox.StandardButton.Yes:
            return
        self.forget(net.ssid)

    def forget(self, ssid: str) -> None:
        def work():
            res = _checks(self._call("forget_network", ssid))
            return _networks(self._call("saved_networks")), res
        self._work(work, f"Removing {ssid}...")


class WifiPage(WizardPage):
    TITLE = "Connect to WiFi"
    CAPTION = "WiFi"
    INTRO = ("Choose the WiFi network the WeatherPi should use. It only works with 2.4 GHz "
             "WiFi, so pick that one if your router shows two.")

    def __init__(self, ctx, parent=None) -> None:
        super().__init__(ctx, parent)
        connect = QFrame(self)
        connect.setObjectName("Card")
        self.body.addWidget(connect)
        h = QHBoxLayout(connect)
        h.setContentsMargins(14, 12, 14, 12)
        h.setSpacing(16)
        left = QVBoxLayout()
        left.addWidget(label("Networks the WeatherPi can see", "SectionHeading"))
        self.list = QListWidget(connect)
        self.list.setAccessibleName("WiFi networks")
        self.list.setMinimumHeight(170)
        self.list.currentRowChanged.connect(self._selected)
        left.addWidget(self.list, 1)
        self.scan_button = QPushButton("Look again", connect)
        self.scan_button.clicked.connect(lambda: self.scan())
        left.addLayout(_row(self.scan_button))
        h.addLayout(left, 3)
        right = QVBoxLayout()
        right.setSpacing(8)
        right.addWidget(label("Network", "SectionHeading"))
        self.chosen = label("Choose a network from the list.", "Muted")
        right.addWidget(self.chosen)
        self.ssid = QLineEdit(connect)
        self.ssid.setPlaceholderText("Network name")
        self.ssid.setAccessibleName("Network name")
        self.ssid.hide()
        right.addWidget(self.ssid)
        self.hidden = QCheckBox("It's a hidden network", connect)
        self.hidden.hide()
        right.addWidget(self.hidden)
        self.pw_label = label("Password", "SectionHeading")
        right.addWidget(self.pw_label)
        self.password = _password_field("WiFi password")
        self.password.setMinimumWidth(200)
        right.addWidget(self.password)
        self.show_pw = QCheckBox("Show password", connect)
        self.show_pw.toggled.connect(lambda on: self.password.setEchoMode(
            QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password))
        right.addWidget(self.show_pw)
        self.connect_button = QPushButton("Connect", connect)
        set_role(self.connect_button, "primary")
        self.connect_button.clicked.connect(self.connect_wifi)
        right.addLayout(_row(self.connect_button))
        right.addStretch(1)
        h.addLayout(right, 2)
        self.networks: list[WifiNetwork] = []
        self.secured = True
        self.scanned = False
        self.actions[Action.CHOOSE_WIFI] = lambda _c: self.password.setFocus()
        self.actions[Action.RETRY] = self._retry
        self.actions[Action.MOVE_CLOSER] = self._retry
        self.actions[Action.PLUG_IN] = self._retry  # "check the cable, then try again"
        self.password.returnPressed.connect(self.connect_wifi)
        self.ssid.textChanged.connect(self._update)
        self.password.textChanged.connect(self._update)
        self._update()

    # -- lifecycle

    def on_enter(self) -> None:
        s = self.session
        self.run(lambda _t: s.wifi_checks(), self._status, busy="Checking the WiFi...")
        if not self.scanned:
            self.scan()

    def _status(self, checks: list[CheckResult]) -> None:
        # only show the current status if it's connected; "not connected yet" is the default
        if any(c.code == "wifi_connected" for c in checks):
            self.checks.set_checks(checks)
            self.set_complete(all_ok(checks))

    # -- scan

    def scan(self, quiet: bool = False) -> None:
        """Scan; ``quiet`` (after connecting) doesn't show the busy bar or block Next."""
        s = self.session
        self.scan_button.setEnabled(False)
        self.run(lambda _t: s.scan_wifi(), self._scanned,
                 busy=None if quiet else "Looking for WiFi networks (about 5 seconds)...")

    def _scanned(self, res: Any) -> None:
        self.scanned = True
        self.scan_button.setEnabled(True)
        self.networks = list(res.networks)
        current = self.current_ssid()
        self.list.clear()
        for n in self.networks:
            self._add_item(NetworkRow(n), n.ssid)
        self._add_item(NetworkRow(None, "Other / hidden network..."), None)
        problems = [c for c in res.checks if not c.ok]
        if problems:
            self.checks.set_checks(problems)
        if current:
            for i, n in enumerate(self.networks):
                if n.ssid == current:
                    self.list.setCurrentRow(i)

    def _add_item(self, w: QWidget, ssid: str | None) -> None:
        item = QListWidgetItem(self.list)
        item.setData(Qt.ItemDataRole.UserRole, ssid)
        item.setData(Qt.ItemDataRole.AccessibleTextRole, w.accessibleName())
        item.setSizeHint(QSize(0, max(36, w.sizeHint().height())))
        self.list.setItemWidget(item, w)

    def _selected(self, row: int) -> None:
        other = row == len(self.networks)
        self.ssid.setVisible(other)
        self.hidden.setVisible(other)
        if other:
            self.chosen.setText("Type the network's name exactly as it's written.")
            self.chosen.setStyleSheet("")
            self.secured = True
            self.hidden.setChecked(True)
            self.ssid.setFocus()
        elif 0 <= row < len(self.networks):
            n = self.networks[row]
            self.chosen.setText(n.ssid)
            self.chosen.setStyleSheet(f"color: {TEXT}; font-weight: 600;")
            self.secured = n.secured
            self.hidden.setChecked(False)
        self.password.setEnabled(self.secured)
        self.show_pw.setEnabled(self.secured)
        self.pw_label.setText("Password" if self.secured else "No password needed")
        if self.secured and not other and row >= 0:
            self.password.setFocus()
        self._update()

    def current_ssid(self) -> str | None:
        row = self.list.currentRow()
        if row == len(self.networks):
            return self.ssid.text().strip("\r\n") or None
        if 0 <= row < len(self.networks):
            return self.networks[row].ssid
        return None

    def select(self, ssid: str) -> bool:
        for i, n in enumerate(self.networks):
            if n.ssid == ssid:
                self.list.setCurrentRow(i)
                return True
        return False

    def _update(self) -> None:
        ssid = self.current_ssid()
        pw_ok = not self.secured or len(self.password.text()) >= 8
        self.connect_button.setEnabled(bool(ssid) and pw_ok and not self.busy_now)

    # -- connect

    def connect_wifi(self) -> None:
        if not self.connect_button.isEnabled():
            return
        ssid = self.current_ssid()
        assert ssid
        pw = self.password.text() if self.secured else None
        hidden = self.hidden.isChecked() and self.list.currentRow() == len(self.networks)
        self.connect_to(ssid, pw, hidden=hidden, secured=self.secured)

    def connect_to(self, ssid: str, password: str | None, *, hidden: bool = False,
                   secured: bool = True, busy: str | None = None) -> None:
        """Connect and wait for the result. The Pi keeps the old profile (and priority) until
        the new one has connected, so a wrong password never loses a working network."""
        s = self.session
        self.set_complete(False)
        self.checks.clear()
        self.checks.hide()

        def work(t):
            started = s.connect_wifi(ssid, password, hidden=hidden, secured=secured)
            if not all_ok(started):
                return started
            return s.wait_for_wifi(ssid, timeout=WIFI_TIMEOUT_S, cancel=t.cancel_event,
                                   on_progress=t.report)

        self.connect_button.setEnabled(False)
        self.run(work, self._connected, busy=busy or f"Connecting to {ssid}. This can take up "
                 "to a minute...", cancel_text="Stop", on_progress=self._progress)

    def reconnect_in_use(self, ssid: str, password: str | None, hidden: bool) -> None:
        """New details for the network the Pi is on: reconnect now so a typo shows up here,
        not the next time the Pi rejoins (decided 2026-10-28)."""
        self.connect_to(ssid, password, hidden=hidden, secured=password is not None,
                        busy=f"Reconnecting to {ssid} to check the new password. This can "
                        "take up to a minute...")

    def _progress(self, st: WifiStatus | None, elapsed: float) -> None:
        if st is None:
            return
        what = {"connecting": "Connecting", "connected": "Joined the network; checking the "
                "internet", "disconnected": "Starting"}.get(st.state, st.state.capitalize())
        self.busy.set_text(f"{what}... ({int(elapsed)} s)")

    def _connected(self, checks: list[CheckResult]) -> None:
        self.checks.set_checks(checks)
        ok = all_ok(checks) and any(c.code == "wifi_connected" for c in checks)
        self.set_complete(ok)
        self._update()
        if ok:
            self.password.clear()
            self.scanned = False  # the "Connected" tag has moved
            QTimer.singleShot(0, lambda: self.scan(quiet=True))

    def _retry(self, _c: CheckResult) -> None:
        if self.current_ssid() and self.connect_button.isEnabled():
            self.connect_wifi()
        else:
            self.scan()

    def run(self, *a, **kw):  # keep Connect's enabled state in step with busy
        t = super().run(*a, **kw)
        self._update()
        t.on_finished(self._update)
        return t


# --------------------------------------------------------------------------- page 5b


class MoreWifiPage(WizardPage):
    """Optional (D28): save the networks the WeatherPi will need where it will live, while
    it's still plugged in here. It joins whichever saved network is in range."""

    TITLE = "Will the WeatherPi live somewhere else?"
    CAPTION = "More WiFi"
    INTRO = ("If the WeatherPi will be used away from this WiFi, add those networks now while "
             "it's plugged in here: for example a WiFi extender in the garden, a holiday home "
             "or a phone hotspot. It joins whichever saved network is in range, by itself. "
             "If it stays on this WiFi, skip this step.")
    #: setup is local: the USB cable carries the link, so any network may be changed
    remote = False

    def __init__(self, ctx, parent=None) -> None:
        super().__init__(ctx, parent)
        self.saved = SavedNetworksPanel(self)
        self.saved.hint.hide()  # the intro says it
        self.body.addWidget(self.saved)
        self.next_label = "&Skip"
        self.set_complete(True)

    def on_enter(self) -> None:
        self.saved.reload()
        self.set_complete(True)

    def saved_backend(self) -> Any:
        return self.ctx.ensure_session()

    def edit_in_use(self, ssid: str, password: str | None, hidden: bool) -> None:
        """New details for the network the Pi is on: test them on the WiFi page."""
        if self.wizard is not None:
            wifi = next(p for p in self.wizard.pages if isinstance(p, WifiPage))
            self.wizard.go_to(wifi)
            wifi.reconnect_in_use(ssid, password, hidden)

    def networks_changed(self) -> None:
        """Called by the panel after each load: "Skip" until a second network is saved."""
        extra = sum(1 for n in self.saved.networks if not getattr(n, "in_use", False))
        self.next_label = "&Next" if extra else "&Skip"
        self.set_complete(True)  # also refreshes the Next button's text


# --------------------------------------------------------------------------- page 6


class HostTailscalePage(WizardPage):
    TITLE = "Tailscale on this computer"
    CAPTION = "Tailscale on this PC"
    INTRO = ("Tailscale is a free app that makes a private network between your own devices. "
             "It lets this computer (and your phone, if you like) reach the WeatherPi from "
             "anywhere, safely.")

    def __init__(self, ctx, parent=None) -> None:
        super().__init__(ctx, parent)
        self.account, lay = _card(self)
        self.account_text = label("", rich=True)
        lay.addWidget(self.account_text)
        self.account.hide()
        self.body.addWidget(self.account)
        self.again = QPushButton("Check again", self)
        self.again.clicked.connect(self.refresh)
        self.after.addLayout(_row(self.again))
        self.note = label("", "Muted")
        self.note.hide()
        self.after.addWidget(self.note)
        self.checks.transform = lambda c: (
            dataclasses.replace(c, message_for_owner="Tailscale is on and signed in.")
            if c.code == "host_tailscale_running" and c.data.get("tailnet_name") else c)
        self.actions[Action.HOST_TAILSCALE_LOGIN] = lambda _c: self.login()
        self.actions[Action.OPEN_TAILSCALE_APP] = self._open_app
        self.checks.code_labels["host_tailscale_not_started"] = "Start Tailscale"
        self.actions[Action.INSTALL_TAILSCALE] = self._install
        self.actions[Action.OPEN_URL] = self._open_login

    def on_enter(self) -> None:
        self.refresh()

    def refresh(self) -> None:
        s = self.session
        self._auto_waited = False
        self._auto_started = False
        self.run(lambda _t: s.check_host_tailscale(), self._show,
                 busy="Checking Tailscale on this computer...")

    def _show(self, checks: list[CheckResult]) -> None:
        self.checks.set_checks(checks)
        ok = all_ok(checks)
        self.set_complete(ok)
        here = self.wizard is not None and self.wizard.current is self
        # Tailscale's tray app isn't running: start it (once per visit/Check again)
        not_started = next((c for c in checks if c.code == "host_tailscale_not_started"), None)
        if not_started is not None and here and not getattr(self, "_auto_started", False):
            self._auto_started = True
            QTimer.singleShot(0, lambda: self._open_app(not_started))
            return
        # "Tailscale is starting...": keep checking by itself (once per visit/Check again)
        if (not ok and any(c.action == Action.WAIT for c in checks)
                and not getattr(self, "_auto_waited", False) and here):
            self._auto_waited = True
            QTimer.singleShot(0, lambda: self.wait("Waiting for Tailscale to start on this "
                                                   "computer..."))
        running = next((c for c in checks if c.code == "host_tailscale_running"), None)
        if running is not None:
            who = running.data.get("tailnet_name") or "your account"
            net = running.data.get("tailnet") or ""
            self.account_text.setText(f"Signed in as <b>{who}</b>"
                                      + (f"<br>Your network: {net}" if net else "")
                                      + "<br><span style='color:#6b7280'>The WeatherPi must "
                                      "join this same account in the next step.</span>")
            self.account.show()
            self.ctx.hints["host_account"] = who
        else:
            self.account.hide()

    def login(self) -> None:
        s = self.session
        self.run(lambda _t: s.start_host_login(), self._login_started,
                 busy="Starting Tailscale sign-in...")

    def _login_started(self, check: CheckResult) -> None:
        if check.ok:
            self.refresh()
            return
        url = check.data.get("url")
        if url:
            desktop.open_url(url)
            self.checks.set_checks([check])
            self.wait()
        else:
            self.checks.set_checks([check])

    def _open_login(self, check: CheckResult) -> None:
        desktop.open_url(check.data.get("url") or check.detail)
        self.wait()

    def _open_app(self, check: CheckResult) -> None:
        if check.data.get("path"):
            desktop.open_path(check.data["path"])
        if check.code == "host_tailscale_not_started":
            self.wait("Starting Tailscale on this computer...")
        else:
            self.wait()

    def _install(self, check: CheckResult) -> None:
        desktop.open_url(check.data.get("url") or "https://tailscale.com/download")
        self.note.setText("Your browser opened Tailscale's download page. Install Tailscale "
                          "and sign in, then click Check again.")
        self.note.show()

    def wait(self, text: str = "Waiting for you to sign in to Tailscale...") -> None:
        s = self.session
        if self.busy_now:
            return
        self.run(lambda t: s.wait_for_host_tailscale(timeout=HOST_LOGIN_TIMEOUT_S,
                                                     cancel=t.cancel_event),
                 self._show, busy=text, cancel_text="Stop waiting")


# --------------------------------------------------------------------------- page 7


#: page 7 checks that come from publishing the dashboard (flows.enable_dashboard_serve)
SERVE_CODES = frozenset({"serve", "serve_failed", "serve_not_enabled"})


class PiTailscalePage(WizardPage):
    TITLE = "Tailscale on the WeatherPi"
    CAPTION = "Tailscale on the Pi"
    INTRO = ""

    def __init__(self, ctx, parent=None) -> None:
        super().__init__(ctx, parent)
        self.explain = label("", "Intro", rich=True)
        self.body.addWidget(self.explain)
        self.start_button = QPushButton("Connect the WeatherPi to Tailscale", self)
        set_role(self.start_button, "primary")
        self.start_button.clicked.connect(self.start)
        self.body.addLayout(_row(self.start_button))
        self.link_box, lay = _card(self)
        lay.addWidget(label("If your browser didn't open, use this link:", "Muted"))
        self.link = label("", rich=False)
        self.link.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self.link)
        self.open_link = QPushButton("Open the sign-in page", self)
        self.copy_link = QPushButton("Copy link", self)
        self.open_link.clicked.connect(lambda: desktop.open_url(self.url))
        self.copy_link.clicked.connect(self._copy)
        lay.addLayout(_row(self.open_link, self.copy_link))
        self.link_box.hide()
        self.body.addWidget(self.link_box)
        self.retry_serve = QPushButton("I've turned it on: try again", self)
        self.retry_serve.clicked.connect(lambda: self.serve())
        self.retry_serve.hide()
        self.after.addLayout(_row(self.retry_serve))
        self.url: str | None = None
        self.tailnet_ok = False
        self.checks.transform = lambda c: (
            dataclasses.replace(c, message_for_owner="The WeatherPi is connected to "
                                "Tailscale.") if c.code == "pi_tailscale_running" else c)
        self.actions[Action.OPEN_URL] = self._open_url
        self.actions[Action.PI_TAILSCALE_RELOGIN] = lambda _c: self.relogin()
        self.actions[Action.RETRY] = self._retry
        self.actions[Action.CHOOSE_WIFI] = self._back_to_wifi
        self.actions[Action.PLUG_IN] = self._retry  # "check the cable, then try again"

    def on_enter(self) -> None:
        who = self.ctx.hints.get("host_account")
        account = f" (<b>{who}</b>)" if who else ""
        self.explain.setText(
            "Now the WeatherPi joins your Tailscale network. When you click the button, your "
            "browser opens a Tailscale page: sign in with the <b>same account</b> you use on "
            f"this computer{account}, and approve the WeatherPi.")

    def start(self) -> None:
        s = self.session
        self.retry_serve.hide()
        self.start_button.setEnabled(False)
        self.set_complete(False)

        def work(t):
            started = s.start_pi_tailscale()
            if not all_ok(started):
                return ("failed", started)
            if any(c.code == "pi_tailscale_running" for c in started):
                return ("running", started)
            url = started[0].data.get("auth_url") if started else None
            if url:
                return ("url", url)
            res = s.wait_for_pi_auth_url(cancel=t.cancel_event)
            if res.ok:
                return ("running", [res])
            if res.data.get("url"):
                return ("url", res.data["url"])
            return ("failed", [res])

        self.run(work, self._started, busy="Starting Tailscale on the WeatherPi...",
                 cancel_text="Stop", on_cancel=lambda: self.start_button.setEnabled(True))

    def _started(self, res: tuple[str, Any]) -> None:
        kind, value = res
        if kind == "failed":
            self.checks.set_checks(value)
            self.start_button.setEnabled(True)
            return
        if kind == "url":
            self.url = value
            self.link.setText(value)
            self.link_box.show()
            desktop.open_url(value)
            self.checks.set_checks([])
        self.wait_joined()

    def wait_joined(self) -> None:
        s = self.session
        self.run(lambda t: s.wait_for_pi_tailscale(timeout=PI_LOGIN_TIMEOUT_S,
                                                   cancel=t.cancel_event),
                 self._joined, busy="Waiting for you to sign in. The WeatherPi joins as soon "
                 "as you approve it...", cancel_text="Stop waiting",
                 on_cancel=lambda: self.start_button.setEnabled(True))

    def _joined(self, checks: list[CheckResult]) -> None:
        self.checks.set_checks(checks)
        self.start_button.setEnabled(True)
        self.tailnet_ok = all_ok(checks)
        if self.tailnet_ok:
            self.link_box.hide()
            self.start_button.setEnabled(False)
            self.serve(keep=checks)

    def serve(self, keep: list[CheckResult] | None = None) -> None:
        s = self.session
        base = keep if keep is not None else [c for c in self.checks.checks
                                              if c.code in ("pi_tailscale_running",
                                                            "same_tailnet")]
        self.retry_serve.hide()
        self.run(lambda _t: s.enable_dashboard_serve(), lambda cs: self._served(base, cs),
                 busy="Publishing the dashboard on your Tailscale network...")

    def _served(self, base: list[CheckResult], checks: list[CheckResult]) -> None:
        allc = list(base) + list(checks)
        self.checks.set_checks(allc)
        ok = all_ok(allc)
        self.retry_serve.setVisible(any(c.code == "serve_not_enabled" for c in checks))
        if not ok and not self.retry_serve.isVisible():
            self.start_button.setEnabled(True)
        self.set_complete(ok and self.tailnet_ok)

    def _open_url(self, check: CheckResult) -> None:
        desktop.open_url(check.data.get("url") or check.detail)
        if check.code == "serve_not_enabled":
            self.retry_serve.show()

    def _retry(self, check: CheckResult) -> None:
        # a failed publish only needs the publish again, not a new Tailscale sign-in
        if check.code in SERVE_CODES and self.tailnet_ok:
            self.serve()
        else:
            self.start()

    def relogin(self) -> None:
        s = self.session
        self.run(lambda _t: s.pi_tailscale_logout(), self._logged_out,
                 busy="Signing the WeatherPi out of Tailscale...")

    def _logged_out(self, checks: list[CheckResult]) -> None:
        self.checks.set_checks(checks)
        if all_ok(checks):
            self.url = None
            self.link_box.hide()
            self.start()

    def _back_to_wifi(self, _c: CheckResult) -> None:
        if self.wizard is not None:
            wifi = next(p for p in self.wizard.pages if isinstance(p, WifiPage))
            self.wizard.go_to(wifi)

    def _copy(self) -> None:
        if self.url:
            QGuiApplication.clipboard().setText(self.url)
            self.copy_link.setText("Copied")


# --------------------------------------------------------------------------- page 8


class RainTest(QFrame):
    """Optional "tip the bucket now" test."""

    def __init__(self, page: FinalChecksPage) -> None:
        super().__init__(page)
        self.page = page
        self.setObjectName("Card")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 14, 18, 14)
        lay.setSpacing(8)
        lay.addWidget(label("Optional: test the rain gauge", "SectionHeading"))
        lay.addWidget(label("Click Start, then tip the rain gauge's little bucket once by hand. "
                            "You can skip this.", "Muted"))
        self.start_button = QPushButton("Start the test", self)
        self.skip_button = QPushButton("Skip", self)
        set_role(self.skip_button, "link")
        lay.addLayout(_row(self.start_button, self.skip_button))
        self.busy = BusyBar(self)
        lay.addWidget(self.busy)
        self.result = CheckList(self)
        self.result.show_help = False
        self.result.hide()
        self.result.action_requested.connect(lambda _c: self.start())
        lay.addWidget(self.result)
        self.start_button.clicked.connect(self.start)
        self.skip_button.clicked.connect(self.skip)
        self.task = None

    def start(self) -> None:
        s = self.page.session
        self.result.hide()
        self.start_button.setEnabled(False)
        self.busy.start(f"Tip the bucket now... ({int(RAIN_TEST_S)} s left)", self.skip,
                        "Stop")
        self.page.reveal(self)
        self.task = self.page.run(
            lambda t: s.wait_for_rain_tip(timeout=RAIN_TEST_S, cancel=t.cancel_event,
                                          on_progress=t.report),
            self._done, on_progress=self._progress)

    def _progress(self, _v: Any, elapsed: float) -> None:
        self.busy.set_text(f"Tip the bucket now... ({max(0, int(RAIN_TEST_S - elapsed))} s "
                           "left)")

    def _done(self, check: CheckResult) -> None:
        self.busy.stop()
        self.start_button.setEnabled(True)
        self.start_button.setText("Test again")
        self.result.set_checks([check])

    def skip(self) -> None:
        if self.task is not None and self.task.running:
            self.task.cancel()
        else:
            self.busy.stop()
            self.result.set_checks([CheckResult(False, "rain_test_skipped",
                                                "Rain gauge test skipped.", optional=True)])


class FinalChecksPage(WizardPage):
    TITLE = "Final checks"
    CAPTION = "Final checks"
    INTRO = ("Everything now runs over Tailscale instead of the USB cable, just like it will "
             "from now on.")

    def __init__(self, ctx, parent=None) -> None:
        super().__init__(ctx, parent)
        self.again = QPushButton("Check again", self)
        self.again.clicked.connect(self.refresh)
        self.after.addLayout(_row(self.again))
        self.rain = RainTest(self)
        self.after.addWidget(self.rain)
        self.actions[Action.REBOOT] = self._reboot

    def on_enter(self) -> None:
        if not self.complete:
            self.refresh()

    def refresh(self) -> None:
        s = self.session
        self.again.setEnabled(False)
        self.run(lambda _t: s.final_checks(), self._show,
                 busy="Checking everything over Tailscale (this can take up to a minute)...")

    def _show(self, checks: list[CheckResult]) -> None:
        self.again.setEnabled(True)
        self.checks.set_checks(checks)
        self.set_complete(all_ok(checks))

    def _reboot(self, _c: CheckResult) -> None:
        if QMessageBox.question(self, "Restart the WeatherPi",
                                "Restart the WeatherPi now? It takes about a minute.") != \
                QMessageBox.StandardButton.Yes:
            return
        s = self.session

        def work(_t):
            client = s.tailnet_client() or s.usb_client()
            client.reboot()
            return [CheckResult(False, "rebooting", "The WeatherPi is restarting. Click "
                                "Check again in a minute.", action=Action.RETRY)]
        self.run(work, self.checks.set_checks, busy="Asking the WeatherPi to restart...")


# --------------------------------------------------------------------------- page 9


class DonePage(WizardPage):
    TITLE = "Your WeatherPi is ready!"
    CAPTION = "Done"
    INTRO = ("Unplug the USB cable first, then plug the WeatherPi's power supply into PWR. "
             "Never connect both at once; it can destroy the WeatherPi. From now on it only "
             "needs its power supply and your WiFi.")

    def __init__(self, ctx, parent=None) -> None:
        super().__init__(ctx, parent)
        self.next_label = "Go to the home screen"
        self.can_go_back = False
        self.summary = None
        # dashboard
        dash, lay = _card(self)
        lay.addWidget(label("Your weather dashboard", "SectionHeading"))
        lay.addWidget(label("Open this link on any device signed in to your Tailscale account. "
                            "To see it on your phone, install the Tailscale app there and "
                            "sign in with the same account.", "Muted"))
        self.dash_url = label("")
        self.dash_url.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self.dash_url)
        self.open_dash = QPushButton("Open dashboard", self)
        set_role(self.open_dash, "primary")
        self.copy_dash = QPushButton("Copy link", self)
        self.open_dash.clicked.connect(lambda: desktop.open_url(self._url()))
        self.copy_dash.clicked.connect(self._copy)
        lay.addLayout(_row(self.open_dash, self.copy_dash))
        self.body.addWidget(dash)
        # recovery
        rec, lay = _card(self)
        lay.addWidget(label("Your recovery file", "SectionHeading"))
        lay.addWidget(label("It lets you reset the WeatherPi's password if you forget it. Keep "
                            "a copy somewhere safe, like a USB stick or a printout.", "Muted"))
        self.recovery_path = label("")
        self.recovery_path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self.recovery_path)
        self.show_recovery = QPushButton("Show the file", self)
        self.show_recovery.clicked.connect(self._show_file)
        lay.addLayout(_row(self.show_recovery))
        self.body.addWidget(rec)
        # backup
        bak, lay = _card(self)
        lay.addWidget(label("Make a first backup", "SectionHeading"))
        lay.addWidget(label("Saves a copy of the WeatherPi's recorded weather and settings to "
                            "this computer.", "Muted"))
        self.backup_button = QPushButton("Back up now", self)
        self.backup_button.clicked.connect(self.backup)
        self.backup_bar = QProgressBar(self)
        self.backup_bar.setTextVisible(False)
        self.backup_bar.setFixedHeight(8)
        self.backup_bar.hide()
        lay.addLayout(_row(self.backup_button))
        lay.addWidget(self.backup_bar)
        self.backup_result = CheckList(self)
        self.backup_result.hide()
        self.backup_result.action_requested.connect(lambda _c: self.backup())
        lay.addWidget(self.backup_result)
        self.body.addWidget(bak)

    def on_enter(self) -> None:
        s = self.session
        self.run(lambda _t: s.finish(), self._finished, busy="Saving your WeatherPi's "
                 "details...")

    def _finished(self, summary: Any) -> None:
        self.summary = summary
        url = summary.dashboard_url
        self.dash_url.setText(url or "The dashboard link isn't known yet.")
        self.open_dash.setEnabled(bool(url))
        self.copy_dash.setEnabled(bool(url))
        rec = summary.recovery_file
        self.recovery_path.setText(str(rec) if rec else "No recovery file was found.")
        self.show_recovery.setEnabled(bool(rec))
        self.set_complete(True)

    def _url(self) -> str | None:
        return self.summary.dashboard_url if self.summary else None

    def _copy(self) -> None:
        if self._url():
            QGuiApplication.clipboard().setText(self._url())
            self.copy_dash.setText("Copied")

    def _show_file(self) -> None:
        if self.summary and self.summary.recovery_file:
            desktop.show_in_folder(self.summary.recovery_file)

    def backup(self) -> None:
        s = self.session
        self.backup_button.setEnabled(False)
        self.backup_result.hide()
        self.backup_bar.setRange(0, 0)
        self.backup_bar.show()
        self.run(lambda t: s.first_backup(progress=lambda d, total: t.report((d, total))),
                 self._backed_up, on_progress=self._backup_progress,
                 busy="Making a backup...")

    def _backup_progress(self, value: Any, _elapsed: float) -> None:
        done, total = value
        if total:
            self.backup_bar.setRange(0, 1000)
            self.backup_bar.setValue(int(1000 * done / total))

    def _backed_up(self, res: Any) -> None:
        checks, _check = res
        self.backup_bar.hide()
        self.backup_button.setEnabled(True)
        self.backup_result.set_checks(checks)
        if all_ok(checks):
            self.backup_button.setText("Back up again")


# --------------------------------------------------------------------------- section


class ConnectingView(QWidget):
    """Shown when ``setup`` is opened without a WeatherPi found yet (e.g. ``--only setup``
    or straight from the step list): finds it, then hands over to the wizard."""

    def __init__(self, section: SetupSection) -> None:
        super().__init__(section)
        self.setObjectName("Page")
        self.section = section
        lay = QVBoxLayout(self)
        lay.setContentsMargins(36, 28, 36, 28)
        lay.setSpacing(12)
        lay.addWidget(label("SET UP", "PageStep"))
        lay.addWidget(label("Finding your WeatherPi", "PageTitle"))
        row = QHBoxLayout()
        self.spinner = Spinner(20, self)
        self.text = label("Make sure it's plugged in with the USB cable...", "Intro")
        row.addWidget(self.spinner)
        row.addWidget(self.text, 1)
        lay.addLayout(row)
        self.checks = CheckList(self)
        self.checks.show_help = False
        self.checks.hide()
        self.checks.action_requested.connect(lambda _c: self.find())
        lay.addWidget(self.checks)
        self.tester_button = QPushButton("Check the Pi", self)
        set_role(self.tester_button, "primary")
        self.tester_button.clicked.connect(lambda: section.ctx.go("tester"))
        self.tester_button.hide()
        lay.addLayout(_row(self.tester_button))
        lay.addStretch(1)

    def find(self) -> None:
        s = self.section.ctx.ensure_session()
        self.spinner.show()
        self.tester_button.hide()
        self.checks.hide()
        self.text.setText("Make sure it's plugged in with the USB cable...")
        from ..worker import run_task

        run_task(lambda t: s.wait_for_plug_in(timeout=20, cancel=t.cancel_event),
                 self._found, owner=self)

    def _found(self, res: PlugInResult) -> None:
        self.spinner.hide()
        if res.next_step == "claim":
            self.section.begin(4)
        elif res.next_step == "resume":
            self.section.begin(res.resume_page or 5)
        else:
            self.text.setText("We couldn't find a WeatherPi that's ready to set up. Let's "
                              "check it first.")
            self.checks.set_checks([c for c in res.checks if not c.ok])
            self.tester_button.show()


class SetupSection(WizardSection):
    SECTION_ID = "setup"
    TITLE = "Set up"
    NEXT_SECTION = "home"
    FIRST_PAGE = 4

    def build(self) -> None:
        self.password = self.wizard.add_page(PasswordPage(self.ctx))
        self.wifi = self.wizard.add_page(WifiPage(self.ctx))
        self.more_wifi = self.wizard.add_page(MoreWifiPage(self.ctx))
        self.host_ts = self.wizard.add_page(HostTailscalePage(self.ctx))
        self.pi_ts = self.wizard.add_page(PiTailscalePage(self.ctx))
        self.final = self.wizard.add_page(FinalChecksPage(self.ctx))
        self.done = self.wizard.add_page(DonePage(self.ctx))
        lay = self.layout()
        self.outer = QStackedWidget(self)
        lay.removeWidget(self.wizard)
        self.connecting = ConnectingView(self)
        self.outer.addWidget(self.connecting)
        self.outer.addWidget(self.wizard)
        lay.addWidget(self.outer)

    def page_for(self, number: int) -> WizardPage:
        """Wizard page by its number in the docs' table (4..9; the optional "More WiFi" page
        between 5 and 6 has no number, so other sections never jump to it)."""
        pages = {4: self.password, 5: self.wifi, 6: self.host_ts, 7: self.pi_ts,
                 8: self.final, 9: self.done}
        return pages[max(4, min(9, number))]

    def begin(self, number: int) -> None:
        self.outer.setCurrentWidget(self.wizard)
        self.wizard.start(self.page_for(number))

    def on_enter(self) -> None:
        start = self.ctx.hints.pop("setup_start", None)
        session = self.ctx.session
        if start is not None and session is not None and session.info is not None:
            self.begin(start)
        elif self.wizard.started and session is not None and session.info is not None:
            self.outer.setCurrentWidget(self.wizard)
            self.wizard.current.on_enter()
        else:
            self.outer.setCurrentWidget(self.connecting)
            self.connecting.find()


__all__ = ["SetupSection", "new_priorities"]
