"""The app shell: start-up routing, section registry/placeholders, --only, step list."""

from __future__ import annotations

import importlib
import sys
import types

import pytest
from PySide6.QtCore import Signal
from PySide6.QtWidgets import QWidget

from weatherpi_companion import __main__ as entry
from weatherpi_companion.ui import app as ui_app
from weatherpi_companion.ui import sections
from weatherpi_companion.ui.app import WELCOME, MainWindow, initial_section
from weatherpi_companion.ui.sections import PlaceholderSection, load_section_class
from weatherpi_companion.ui.sections import tester as tester_mod
from weatherpi_companion.ui.sections.setup import SetupSection

from .test_ui_fakes import make_ctx, record_opened, wait_idle


@pytest.fixture(autouse=True)
def opened(monkeypatch):
    return record_opened(monkeypatch)


@pytest.fixture
def no_flasher(monkeypatch):
    """Make sure the flasher module looks missing (it's built on another branch)."""
    name = "weatherpi_companion.ui.sections.flasher"
    real = importlib.import_module

    def fake_import(mod, *a, **kw):
        if mod == name:
            raise ModuleNotFoundError(f"No module named {mod!r}", name=mod)
        return real(mod, *a, **kw)

    monkeypatch.setattr(sections.importlib, "import_module", fake_import)
    monkeypatch.delitem(sys.modules, name, raising=False)


def window(qtbot, ctx, **kw) -> MainWindow:
    w = MainWindow(ctx, **kw)
    qtbot.addWidget(w)
    return w


# --------------------------------------------------------------------------- routing


def test_routing_without_profile_is_welcome(app_data):
    assert initial_section(app_data) == WELCOME


def test_routing_with_unfinished_profile_is_tester(app_data):
    app_data.station("00000000abcd1234").update_profile(hostname="weatherpi")
    assert initial_section(app_data) == "tester"


def test_routing_with_finished_profile_is_home(app_data):
    app_data.station("00000000abcd1234").update_profile(setup_complete=True)
    assert initial_section(app_data) == "home"


def test_welcome_choices(qtbot, app_data, no_flasher):
    ctx, _s, _w = make_ctx(app_data)
    win = window(qtbot, ctx)
    assert win.current_id == WELCOME
    assert win.stack.currentWidget() is win.welcome
    win.welcome.tester_button.click()
    wait_idle()
    assert win.current_id == "tester"
    assert isinstance(win.stack.currentWidget(), tester_mod.TesterSection)
    assert win.steps.items["tester"].state == "current"
    # "My card is already flashed" skips the flasher: shown as skipped, not ticked
    assert win.steps.items["flasher"].state == "skipped"
    assert win.steps.items["flasher"].caption == "Skipped"
    win.show_section(WELCOME)
    win.welcome.flash_button.click()
    assert win.current_id == "flasher"
    ph = win.stack.currentWidget()
    assert isinstance(ph, PlaceholderSection)
    assert ph.SECTION_ID == "flasher"
    # the placeholder's button goes on to the tester; the flasher was done this time
    ph.buttons[0].click()
    assert win.current_id == "tester"
    assert win.steps.items["flasher"].state == "done"
    win.close()


def test_resumed_setup_shows_earlier_steps_done_not_skipped(qtbot, app_data):
    """Opening the app on an unfinished station: the card was flashed in an earlier session."""
    app_data.station("00000000abcd1234").update_profile(hostname="weatherpi")
    ctx, _s, _w = make_ctx(app_data)
    win = window(qtbot, ctx)
    assert win.current_id == "tester"
    assert win.steps.items["flasher"].state == "done"
    win.close()


def test_start_up_with_finished_profile_shows_home(qtbot, app_data):
    from weatherpi_companion.ui.sections.home import HomeSection

    app_data.station("00000000abcd1234").update_profile(
        setup_complete=True, serve_url="https://weatherpi.example.ts.net")
    ctx, _s, _w = make_ctx(app_data)
    win = window(qtbot, ctx)
    assert win.current_id == "home"
    home = win.stack.currentWidget()
    assert isinstance(home, HomeSection) and not home.dashboard_button.isHidden()
    wait_idle()
    win.close()


def test_start_up_with_unfinished_profile_goes_to_tester(qtbot, app_data):
    app_data.station("00000000abcd1234").update_profile(hostname="weatherpi")
    ctx, _s, _w = make_ctx(app_data)
    win = window(qtbot, ctx)
    assert win.current_id == "tester"
    tester = win.sections["tester"]
    assert tester.wizard.current.title_label.text() == "Welcome back"
    win.close()


# --------------------------------------------------------------------------- registry


def test_missing_flasher_module_gives_placeholder(no_flasher):
    assert load_section_class("flasher") is None
    assert load_section_class("tester") is tester_mod.TesterSection
    assert load_section_class("setup") is SetupSection
    with pytest.raises(KeyError):
        load_section_class("nope")


def test_flasher_module_is_picked_up_when_it_exists(qtbot, app_data, monkeypatch):
    """After the merge the flasher is just a module with a FlasherSection."""

    class FlasherSection(QWidget):
        SECTION_ID = "flasher"
        TITLE = "Flash SD card"
        finished = Signal(str)

        def __init__(self, ctx, parent=None):
            super().__init__(parent)
            self.ctx = ctx
            self.entered = 0

        def on_enter(self):
            self.entered += 1

    mod = types.ModuleType("weatherpi_companion.ui.sections.flasher")
    mod.FlasherSection = FlasherSection
    monkeypatch.setitem(sys.modules, mod.__name__, mod)
    ctx, _s, _w = make_ctx(app_data)
    win = window(qtbot, ctx)
    win.welcome.flash_button.click()
    sec = win.stack.currentWidget()
    assert isinstance(sec, FlasherSection) and sec.entered == 1
    sec.finished.emit("")  # "" = let the shell decide: the next step
    assert win.current_id == "tester"
    win.close()


def test_broken_section_does_not_crash(monkeypatch):
    def boom(_name, *a, **kw):
        raise SyntaxError("bad file")

    monkeypatch.setattr(sections.importlib, "import_module", boom)
    assert load_section_class("home") is None


# --------------------------------------------------------------------------- --only


def test_only_shows_one_section_without_sidebar(qtbot, app_data):
    ctx, _s, _w = make_ctx(app_data)
    win = window(qtbot, ctx, only="tester")
    assert win.welcome is None and not win.steps.isVisibleTo(win)
    assert win.current_id == "tester" and "dev" in win.windowTitle()
    win.sections["tester"].finished.emit("setup")
    assert win.current_id == "tester"  # stays; tells the developer instead
    assert "setup" in win.statusBar().currentMessage()
    win.sections["tester"].on_leave()
    win.close()


def test_main_passes_only_to_run(monkeypatch):
    got = {}

    def fake_run(qt_args=None, only=None):
        got.update(qt_args=qt_args, only=only)
        return 0

    monkeypatch.setattr(ui_app, "run", fake_run)
    with pytest.raises(SystemExit) as e:
        entry.main(["--only", "setup", "-platform", "offscreen"])
    assert e.value.code == 0
    assert got == {"qt_args": ["-platform", "offscreen"], "only": "setup"}


def test_flash_helper_is_routed_before_argparse_and_qt(monkeypatch):
    got = []
    mod = types.ModuleType("weatherpi_companion.host.flash_helper")
    mod.main = lambda argv: got.append(list(argv)) or 7
    monkeypatch.setitem(sys.modules, mod.__name__, mod)

    def no_ui(*_a, **_k):
        raise AssertionError("the UI must not start for --flash-helper")

    monkeypatch.setattr(ui_app, "run", no_ui)
    with pytest.raises(SystemExit) as e:
        entry.main(["--flash-helper", "--device", r"\.\PhysicalDrive9", "--unknown-x"])
    assert e.value.code == 7
    assert got == [["--flash-helper", "--device", r"\.\PhysicalDrive9", "--unknown-x"]]
    # not the first argument: normal parsing (and run) as before
    monkeypatch.setattr(ui_app, "run", lambda qt_args=None, only=None: 0)
    with pytest.raises(SystemExit) as e:
        entry.main(["--only", "tester", "--flash-helper"])
    assert e.value.code == 0 and len(got) == 1


def test_main_version_and_selftest_still_work(capsys):
    with pytest.raises(SystemExit) as e:
        entry.main(["--version"])
    assert e.value.code == 0 and "WeatherPi Companion" in capsys.readouterr().out
    assert "weatherpi_companion.ui.app" in entry.SELFTEST_MODULES


def test_run_rejects_unknown_section(capsys):
    assert ui_app.run([], only="nope") == 2
    assert "unknown section" in capsys.readouterr().err


# --------------------------------------------------------------------------- flow


def test_step_list_click_and_section_finish_routing(qtbot, app_data):
    ctx, _s, _w = make_ctx(app_data)
    win = window(qtbot, ctx, start="tester")
    tester = win.sections["tester"]
    tester.finished.emit("")  # default: the next section
    assert win.current_id == "setup"
    win.steps.items["tester"].click()
    assert win.current_id == "tester"
    assert win.steps.items["tester"].caption.startswith("1 of 4")
    ctx.go("home")
    assert win.current_id == "home"
    win.close()
