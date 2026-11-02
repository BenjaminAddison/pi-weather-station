"""Shared widgets: check list, worker helper (off the UI thread, cancellable), wizard
gating, help dialog."""

from __future__ import annotations

import threading
import time

import pytest
import shiboken6
from PySide6.QtCore import QThread, QTimer
from PySide6.QtWidgets import QApplication, QWidget

from weatherpi_companion.checks import Action, CheckResult
from weatherpi_companion.ui import worker
from weatherpi_companion.ui.widgets.checklist import CheckList, row_state
from weatherpi_companion.ui.widgets.helpdialog import HelpDialog, setup_help_pack
from weatherpi_companion.ui.widgets.wizard import Wizard, WizardPage

from .test_ui_fakes import make_ctx, record_opened, wait_idle


@pytest.fixture(autouse=True)
def opened(monkeypatch):
    return record_opened(monkeypatch)


OK = CheckResult(True, "driver_ok", "The USB driver is installed.")
FAIL = CheckResult(False, "driver_missing", "Install the driver.", "pnp: no driver",
                   Action.INSTALL_DRIVER)
WARN = CheckResult(False, "temperature_missing", "No temperature sensor.", optional=True)
WAIT = CheckResult(False, "usb_no_agent", "Starting up...", action=Action.WAIT)
FIX = CheckResult(False, "password_mismatch", "Passwords don't match.",
                  action=Action.FIX_PASSWORD)


# --------------------------------------------------------------------------- check list


def test_row_states():
    assert [row_state(c) for c in (OK, FAIL, WARN, WAIT)] == ["ok", "fail", "warn", "running"]


def test_check_list_renders_rows_buttons_and_help(qtbot):
    cl = CheckList()
    qtbot.addWidget(cl)
    actions, helps = [], []
    cl.action_requested.connect(actions.append)
    cl.help_requested.connect(helps.append)
    cl.set_checks([OK, FAIL, WARN, WAIT, FIX])
    assert cl.codes() == ["driver_ok", "driver_missing", "temperature_missing",
                          "usb_no_agent", "password_mismatch"]
    ok, fail, warn, wait, fix = cl.rows
    assert ok.action_button is None and ok.help_button is None
    assert fail.action_button.text() == "Install driver"
    assert fail.help_button is not None and warn.help_button is not None
    assert wait.help_button is None and wait.action_button is None  # just a spinner
    assert fix.action_button is None  # the fix is the password field itself
    assert fail.message.text() == "Install the driver."
    # details are hidden until asked for
    assert fail.detail.isHidden()
    fail.details_button.click()
    assert not fail.detail.isHidden() and fail.detail.text() == "pnp: no driver"
    fail.action_button.click()
    fail.help_button.click()
    assert actions == [FAIL] and helps == [FAIL]


def test_check_list_label_overrides_and_replace(qtbot):
    cl = CheckList()
    qtbot.addWidget(cl)
    cl.code_labels["driver_missing"] = "Do it"
    cl.action_labels[Action.RETRY] = None
    retry = CheckResult(False, "x", "Failed.", action=Action.RETRY)
    cl.set_checks([FAIL, retry])
    assert cl.rows[0].action_button.text() == "Do it"
    assert cl.rows[1].action_button is None
    cl.replace_check("x", CheckResult(True, "x", "Fine now."))
    assert cl.rows[1].state == "ok"
    cl.set_running("Working...")
    assert cl.rows[0].state == "running"
    cl.clear()
    assert cl.rows == []


def test_check_list_hides_help_when_disabled(qtbot):
    cl = CheckList()
    qtbot.addWidget(cl)
    cl.show_help = False
    cl.set_checks([FAIL])
    assert cl.rows[0].help_button is None


# --------------------------------------------------------------------------- worker


def test_worker_runs_off_the_ui_thread_and_delivers_on_it(qtbot):
    ui_thread = QThread.currentThread()
    seen = {}

    def fn(_t):
        seen["worker_is_ui"] = QThread.currentThread() == ui_thread
        time.sleep(0.3)  # a blocking core call
        return 42

    results, ticks = [], []
    timer = QTimer()
    timer.timeout.connect(lambda: ticks.append(1))
    timer.start(10)
    task = worker.run_task(fn, lambda v: results.append((v, QThread.currentThread()
                                                         == ui_thread)))
    qtbot.waitUntil(lambda: bool(results), timeout=5000)
    timer.stop()
    assert results == [(42, True)]
    assert seen["worker_is_ui"] is False
    assert len(ticks) >= 5, "the UI thread was blocked while the task ran"
    assert not task.running and task.result == 42


def test_worker_cancel_sets_the_event_for_wait_calls(qtbot):
    started = threading.Event()

    def fn(t):
        started.set()
        # what a flows.wait_for_* does with cancel=...
        return "cancelled" if t.cancel_event.wait(10) else "timeout"

    out = []
    task = worker.run_task(fn, out.append)
    assert started.wait(5)
    t0 = time.monotonic()
    task.cancel()
    qtbot.waitUntil(lambda: bool(out), timeout=5000)
    assert out == ["cancelled"] and time.monotonic() - t0 < 2


def test_worker_progress_and_errors(qtbot):
    progress, errors = [], []

    def fn(t):
        t.report("half", 1.5)
        raise ValueError("boom")

    worker.run_task(fn, on_error=errors.append, on_progress=lambda v, e: progress.append((v, e)))
    qtbot.waitUntil(lambda: bool(errors), timeout=5000)
    assert progress == [("half", 1.5)]
    assert isinstance(errors[0], ValueError)


def test_worker_skips_callbacks_when_owner_is_gone(qtbot):
    owner = QWidget()
    gate = threading.Event()
    out = []
    worker.run_task(lambda _t: gate.wait(5), out.append, owner=owner)
    shiboken6.delete(owner)
    gate.set()
    wait_idle()
    assert out == []


# --------------------------------------------------------------------------- wizard


class _Page(WizardPage):
    TITLE = "Test page"


def test_wizard_next_is_gated_by_complete_and_busy(qtbot, app_data):
    ctx, _s, _w = make_ctx(app_data)
    wiz = Wizard()
    qtbot.addWidget(wiz)
    p1, p2 = wiz.add_page(_Page(ctx)), wiz.add_page(_Page(ctx))
    finished = []
    wiz.finished.connect(lambda: finished.append(1))
    wiz.start()
    assert not wiz.next_button.isEnabled()
    assert p1.step_label.text() == "STEP 1 OF 2"
    p1.set_complete(True)
    assert wiz.next_button.isEnabled()
    gate = threading.Event()
    p1.run(lambda _t: gate.wait(5), busy="Working...")
    assert not wiz.next_button.isEnabled()  # busy blocks Next
    assert p1.busy.active
    gate.set()
    wait_idle()
    assert wiz.next_button.isEnabled() and not p1.busy.active
    wiz.next()
    assert wiz.current is p2 and wiz.back_button.isVisibleTo(wiz)
    wiz.back()
    assert wiz.current is p1
    wiz.next()
    p2.set_complete(True)
    wiz.next()
    assert finished == [1]


def test_dev_skip_moves_on_without_checks_and_is_off_by_default(qtbot, app_data, monkeypatch):
    from weatherpi_companion.ui.widgets.wizard import dev_skip_enabled

    monkeypatch.delenv("WEATHERPI_DEV_SKIP", raising=False)
    assert not dev_skip_enabled()
    monkeypatch.setenv("WEATHERPI_DEV_SKIP", "1")
    assert dev_skip_enabled()
    ctx, _s, _w = make_ctx(app_data)
    wiz = Wizard()
    qtbot.addWidget(wiz)
    p1, p2 = wiz.add_page(_Page(ctx)), wiz.add_page(_Page(ctx))
    finished = []
    wiz.finished.connect(lambda: finished.append(1))
    wiz.start()
    task = p1.run(lambda t: t.cancel_event.wait(10), busy="Waiting...")
    assert not wiz.next_button.isEnabled()  # checks not passed, still busy
    wiz.skip()
    assert task.cancelled and wiz.current is p2
    wait_idle()
    wiz.skip()  # skipping the last page finishes the wizard
    assert finished == [1]


def test_wizard_leaving_a_page_cancels_its_tasks(qtbot, app_data):
    ctx, _s, _w = make_ctx(app_data)
    wiz = Wizard()
    qtbot.addWidget(wiz)
    p1, p2 = wiz.add_page(_Page(ctx)), wiz.add_page(_Page(ctx))
    wiz.start()
    p1.set_complete(True)
    task = p1.run(lambda t: t.cancel_event.wait(10), busy="Waiting...", cancel_text="Stop")
    wiz.next()
    assert wiz.current is p1  # busy: Next does nothing
    wiz.go_to(p2)
    assert task.cancelled
    wait_idle()
    assert wiz.current is p2


def test_unexpected_error_becomes_a_failed_check(qtbot, app_data):
    ctx, _s, _w = make_ctx(app_data)
    p = _Page(ctx)
    qtbot.addWidget(p)

    def boom(_t):
        raise RuntimeError("kaput")

    p.run(boom, busy="Working...")
    wait_idle()
    assert p.checks.codes() == ["unexpected_error"]
    assert "kaput" in p.checks.checks[0].detail


def test_update_app_action_opens_the_releases_page(qtbot, app_data, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    ctx, _s, _w = make_ctx(app_data)
    p = _Page(ctx)
    qtbot.addWidget(p)
    opened = record_opened(monkeypatch)
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
    p.handle_action(CheckResult(False, "agent_version_mismatch", "Needs a newer app.",
                                action=Action.UPDATE_APP))
    assert opened == ["https://github.com/BenjaminAddison/pi-weather-station/releases/latest"]


# --------------------------------------------------------------------------- help


def test_help_pack_from_setup_session_is_redacted(app_data):
    _ctx, s, _w = make_ctx(app_data)
    assert s.detect_plug_in().next_step == "claim"
    s.create_password("correct horse", "correct horse")
    text = setup_help_pack(s, [FAIL], "WiFi keeps failing")
    assert "WiFi keeps failing" in text and "driver_missing" in text
    assert s.secrets.api_token not in text and s.secrets.recovery_password not in text
    assert "should-not-leak" not in text  # PSK in the fake diagnostics


def test_help_dialog_builds_in_the_background(qtbot):
    calls = []

    def build(text):
        calls.append(QThread.currentThread())
        return f"PACK: {text}"

    dlg = HelpDialog(build, "Something broke")
    qtbot.addWidget(dlg)
    assert not dlg.copy_button.isEnabled()
    dlg.build()
    qtbot.waitUntil(lambda: dlg.output.toPlainText() != "", timeout=5000)
    assert dlg.output.toPlainText() == "PACK: Something broke"
    assert calls[0] != QThread.currentThread()
    dlg.copy()
    assert QApplication.clipboard().text() == "PACK: Something broke"
