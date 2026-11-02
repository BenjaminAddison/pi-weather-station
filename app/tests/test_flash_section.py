"""The Flash SD card screen (offscreen Qt) with the flash service faked."""

from __future__ import annotations

import os
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("pytestqt")

from PySide6.QtCore import Qt, QThread

from weatherpi_companion.host.flash import CardScan, FlashResult
from weatherpi_companion.host.flash_errors import FlashError
from weatherpi_companion.host.flash_image import LocalImage, ReleaseImage
from weatherpi_companion.ui.sections.flasher import FlasherSection

from .test_flash_fakes import card_disk

IMAGE = LocalImage(Path("C:/cache/weatherpi-2026.10.28-1.img.xz"), "ab" * 32, 636_000_000,
                   3_152_097_280)
RELEASE = ReleaseImage("v2026.10.28-1", "weatherpi-2026.10.28-1.img.xz", "https://dl/x",
                       636_000_000, "https://dl/x.sha256")
CARD = card_disk(r"\\.\PhysicalDrive1")
LOCKED = card_disk(r"\\.\PhysicalDrive2", number=2, letters=("F",), is_read_only=True)


class FakeService:
    def __init__(self) -> None:
        self.cached: LocalImage | None = None
        self.release: ReleaseImage | None = RELEASE
        self.release_error: FlashError | None = None
        self.scans = [CardScan([]), CardScan([CARD], locked=[LOCKED])]
        self.scan_calls = 0
        self.flash_error: FlashError | None = None
        self.ejected = False
        self.flashed: list = []
        self.threads: set = set()
        self.cancelled = threading.Event()

    def _note(self) -> None:
        self.threads.add(QThread.currentThread())

    def cached_image(self):
        self._note()
        return self.cached

    def latest_release(self):
        if self.release_error:
            raise self.release_error
        if self.release is None:
            raise FlashError("no_image_published")
        return self.release

    def download(self, release, progress, cancel):
        self._note()
        progress("download", 300_000_000, release.size)
        progress("download", release.size, release.size)
        progress("verify-image", release.size, release.size)
        return IMAGE

    def open_local(self, path):
        if not str(path).endswith(".img.xz"):
            raise FlashError("image_bad", str(path))
        return IMAGE

    def scan(self, image=None):
        self.scan_calls += 1
        return self.scans[min(self.scan_calls, len(self.scans)) - 1]

    def flash(self, image, disk, progress, cancel):
        self._note()
        self.flashed.append((image, disk))
        progress("verify-image", image.size, image.size)
        progress("write", image.raw_size // 2, image.raw_size)
        if self.flash_error:
            raise self.flash_error
        progress("write", image.raw_size, image.raw_size)
        progress("verify-card", image.raw_size, image.raw_size)
        return FlashResult(image.raw_size, "cd" * 32, 600.0, ejected=self.ejected)


@pytest.fixture
def section(qtbot, tmp_path):
    svc = FakeService()
    w = FlasherSection(SimpleNamespace(home=tmp_path), service=svc)
    w.REFRESH_MS = 50
    w.timer.setInterval(50)
    qtbot.addWidget(w)
    w.resize(760, 520)
    w.show()
    yield w, svc
    w.shutdown()


def test_contract():
    assert FlasherSection.SECTION_ID == "flasher"
    assert FlasherSection.TITLE == "Flash SD card"
    assert hasattr(FlasherSection, "finished") and hasattr(FlasherSection, "on_enter")


def test_download_pick_card_confirm_flash_done(qtbot, section):
    w, svc = section
    w.on_enter()
    qtbot.waitUntil(lambda: w.btn_image_next.isEnabled(), timeout=3000)
    assert "2026.10.28-1" in w.img_status.text() and "636 MB" in w.img_status.text()
    assert w.btn_image_next.text() == "Download"
    qtbot.mouseClick(w.btn_image_next, Qt.MouseButton.LeftButton)

    # download finishes -> card picker; first scan has no card, then the card appears
    qtbot.waitUntil(lambda: w.stack.currentIndex() == w.PAGE_CARD, timeout=3000)
    qtbot.waitUntil(lambda: w.card_list.count() == 1, timeout=3000)
    assert svc.scan_calls >= 2
    assert w.card_list.item(0).text() == CARD.label
    assert "locked" in w.card_note.text()
    assert w.btn_card_next.isEnabled()  # the only card is preselected
    qtbot.mouseClick(w.btn_card_next, Qt.MouseButton.LeftButton)

    assert w.stack.currentIndex() == w.PAGE_CONFIRM
    assert w.confirm_text.text() == f"Everything on {CARD.label} will be erased."
    assert not w.btn_flash.isEnabled()
    w.confirm_box.setChecked(True)
    assert w.btn_flash.isEnabled()
    assert not w.timer.isActive()  # no polling away from the picker

    with qtbot.waitSignal(w.finished, timeout=5000) as sig:
        qtbot.mouseClick(w.btn_flash, Qt.MouseButton.LeftButton)
        qtbot.waitUntil(lambda: w.stack.currentIndex() == w.PAGE_DONE, timeout=3000)
        assert "Safely remove the card, put it in the Pi" in w.done_text.text()
        qtbot.mouseClick(w.btn_done, Qt.MouseButton.LeftButton)
    assert sig.args == ["tester"]
    assert svc.flashed == [(IMAGE, CARD)]
    assert QThread.currentThread() not in svc.threads  # slow work never on the UI thread


def test_done_page_says_when_the_card_was_ejected(qtbot, section):
    w, svc = section
    svc.ejected = True
    w._flash_done(svc.flash(IMAGE, CARD, lambda *a: None, threading.Event()))
    assert w.stack.currentIndex() == w.PAGE_DONE
    assert "card has been ejected" in w.done_text.text()
    assert "format the disk" not in w.done_text.text()


def test_cached_image_skips_download(qtbot, section):
    w, svc = section
    svc.cached = IMAGE
    w.on_enter()
    qtbot.waitUntil(lambda: w.btn_image_next.isEnabled(), timeout=3000)
    assert w.btn_image_next.text() == "Continue" and "ready on this PC" in w.img_status.text()


def test_no_image_published_offers_a_local_file(qtbot, section):
    w, svc = section
    svc.release = None
    w.on_enter()
    qtbot.waitUntil(lambda: w.img_error.isVisible(), timeout=3000)
    assert "no WeatherPi image to download yet" in w.img_error.message.text()
    assert w.btn_image_next.isHidden() and w.btn_local.isEnabled()
    w.use_local_file("C:/Downloads/notes.txt")
    qtbot.waitUntil(lambda: "isn't a complete" in w.img_error.message.text(), timeout=3000)
    w.use_local_file("C:/Downloads/weatherpi-2026.10.28-1.img.xz")
    qtbot.waitUntil(lambda: w.stack.currentIndex() == w.PAGE_CARD, timeout=3000)


def test_offline_shows_retry(qtbot, section):
    w, svc = section
    svc.release_error = FlashError("offline", "no route")
    w.on_enter()
    qtbot.waitUntil(lambda: w.btn_retry_lookup.isVisible(), timeout=3000)
    assert "internet" in w.img_error.message.text()
    svc.release_error = None
    qtbot.mouseClick(w.btn_retry_lookup, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: w.btn_image_next.isEnabled(), timeout=3000)


def test_flash_error_shows_plain_words_and_try_again(qtbot, section):
    w, svc = section
    svc.cached = IMAGE
    svc.scans = [CardScan([CARD])]
    svc.flash_error = FlashError("write_protected", "ERROR_WRITE_PROTECT")
    w.on_enter()
    qtbot.waitUntil(lambda: w.btn_image_next.isEnabled(), timeout=3000)
    w.btn_image_next.click()
    qtbot.waitUntil(lambda: w.btn_card_next.isEnabled(), timeout=3000)
    w.btn_card_next.click()
    w.confirm_box.setChecked(True)
    w.btn_flash.click()
    qtbot.waitUntil(lambda: w.stack.currentIndex() == w.PAGE_ERROR, timeout=3000)
    assert "Lock" in w.err_box.message.text()
    assert "ERROR_WRITE_PROTECT" in w.err_box.detail.text()
    w.btn_err_again.click()
    assert w.stack.currentIndex() == w.PAGE_CARD


def test_flash_button_needs_the_tick_box(qtbot, section):
    w, svc = section
    w.image, w.card = IMAGE, CARD
    w.go_confirm()
    w.start_flash()  # not ticked: nothing happens
    assert w.stack.currentIndex() == w.PAGE_CONFIRM and svc.flashed == []
