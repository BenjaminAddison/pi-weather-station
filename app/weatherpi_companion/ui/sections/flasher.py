"""Flash SD card section: image source -> card picker -> confirm -> progress -> done.

Follows the section contract in docs/companion-app.md. All slow work (GitHub, hashing,
listing disks, the elevated flash) runs on a QThreadPool; the UI thread only updates widgets.
The logic lives in :mod:`weatherpi_companion.host.flash_service` (Qt-free, unit-tested).
"""

from __future__ import annotations

import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QProgressBar,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ...host.flash import (
    PHASE_DOWNLOAD,
    PHASE_VERIFY_CARD,
    PHASE_VERIFY_IMAGE,
    PHASE_WRITE,
    CardScan,
    Disk,
    TimeLeft,
    format_time_left,
)
from ...host.flash_errors import FlashError
from ...host.flash_image import LocalImage, ReleaseImage
from ...host.flash_service import FlashService

PHASE_TEXT = {
    PHASE_VERIFY_IMAGE: "Step 1 of 3: Checking the WeatherPi image",
    PHASE_WRITE: "Step 2 of 3: Writing the SD card",
    PHASE_VERIFY_CARD: "Step 3 of 3: Checking the SD card",
    PHASE_DOWNLOAD: "Downloading WeatherPi",
}
NEXT_SECTION = "tester"

DONE_EJECTED = (
    "The card has been ejected, so it's safe to take it out now.\n\n"
    "1. Take the card out of this PC's card reader.\n"
    "2. Put the card in the Raspberry Pi's card slot.\n"
    "3. Click Continue.")
DONE_NOT_EJECTED = (
    "Safely remove the card, put it in the Pi, then continue.\n\n"
    "1. Take the card out of this PC's card reader (if Windows says \"You need to "
    "format the disk\", click Cancel: that's normal).\n"
    "2. Put the card in the Raspberry Pi's card slot.\n"
    "3. Click Continue.")


# --------------------------------------------------------------------------- worker plumbing


class _Signals(QObject):
    done = Signal(object)
    failed = Signal(object)
    progress = Signal(str, object, object)  # phase, done, total (object: > 2**31 bytes)


class _Task(QRunnable):
    """Runs ``fn(progress)`` on the pool and reports back through Qt signals."""

    def __init__(self, fn: Callable[[Callable[[str, int, int | None], None]], Any]) -> None:
        super().__init__()
        self.fn = fn
        self.signals = _Signals()

    def run(self) -> None:
        try:
            result = self.fn(self.signals.progress.emit)
        except Exception as exc:  # noqa: BLE001 - every failure goes back to the UI
            self.signals.failed.emit(exc)
        else:
            self.signals.done.emit(result)


def _owner_message(exc: BaseException) -> tuple[str, str]:
    if isinstance(exc, FlashError):
        return exc.message_for_owner, exc.detail
    return FlashError("helper_failed").message_for_owner, f"{type(exc).__name__}: {exc}"


def _mb(n: int | None) -> str:
    return "?" if n is None else f"{n / 1e6:,.0f} MB"


# --------------------------------------------------------------------------- small widgets


def _title(text: str) -> QLabel:
    label = QLabel(text)
    font = QFont(label.font())
    font.setPointSizeF(font.pointSizeF() * 1.6)
    font.setBold(True)
    label.setFont(font)
    label.setWordWrap(True)
    return label


def _text(text: str = "", *, bold: bool = False, muted: bool = False) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    if bold:
        f = QFont(label.font())
        f.setBold(True)
        label.setFont(f)
    if muted:
        label.setStyleSheet("color: palette(placeholder-text);")
    return label


def _button(text: str, primary: bool = False) -> QPushButton:
    b = QPushButton(text)
    b.setMinimumHeight(34)
    b.setMinimumWidth(120)
    if primary:
        b.setDefault(True)
        f = QFont(b.font())
        f.setBold(True)
        b.setFont(f)
    return b


class _Page(QWidget):
    """Title, body (stretching), and a row of buttons at the bottom right."""

    def __init__(self, title: str) -> None:
        super().__init__()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(32, 28, 32, 24)
        outer.setSpacing(14)
        self.title = _title(title)
        outer.addWidget(self.title)
        self.body = QVBoxLayout()
        self.body.setSpacing(12)
        outer.addLayout(self.body)
        outer.addStretch(1)
        self.buttons = QHBoxLayout()
        self.buttons.addStretch(1)
        outer.addLayout(self.buttons)

    def add_button(self, button: QPushButton) -> QPushButton:
        self.buttons.addWidget(button)
        return button


class _ErrorBox(QFrame):
    def __init__(self) -> None:
        super().__init__()
        self.setStyleSheet("_ErrorBox { border: 1px solid #c62828; border-radius: 6px; "
                           "background: rgba(198, 40, 40, 0.08); }")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        self.message = _text()
        self.detail = _text(muted=True)
        lay.addWidget(self.message)
        lay.addWidget(self.detail)
        self.hide()

    def show_error(self, exc: BaseException | None) -> None:
        if exc is None:
            self.hide()
            return
        msg, detail = _owner_message(exc)
        self.message.setText(msg)
        self.detail.setText(f"Details: {detail}")
        self.detail.setVisible(bool(detail))
        self.show()


# --------------------------------------------------------------------------- the section


class FlasherSection(QWidget):
    """Writes the WeatherPi image to an SD card (section id ``flasher``)."""

    SECTION_ID = "flasher"
    TITLE = "Flash SD card"
    finished = Signal(str)

    #: how often the card list refreshes while the picker is shown (ms)
    REFRESH_MS = 2000

    PAGE_IMAGE, PAGE_CARD, PAGE_CONFIRM, PAGE_PROGRESS, PAGE_DONE, PAGE_ERROR = range(6)

    def __init__(self, ctx: Any, parent: QWidget | None = None, *,
                 service: FlashService | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.service = service or FlashService(Path(ctx.home))
        self.pool = QThreadPool(self)
        self._tasks: set[_Task] = set()
        self.image: LocalImage | None = None
        self.release: ReleaseImage | None = None
        self.card: Disk | None = None
        self._cards: list[Disk] = []
        self._scanning = False
        self._looked_up = False
        self._cancel: threading.Event | None = None
        self._time_left: TimeLeft | None = None

        self.stack = QStackedWidget()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.stack)
        self._build_image_page()
        self._build_card_page()
        self._build_confirm_page()
        self._build_progress_page()
        self._build_done_page()
        self._build_error_page()

        self.timer = QTimer(self)
        self.timer.setInterval(self.REFRESH_MS)
        self.timer.timeout.connect(self.refresh_cards)
        self.stack.currentChanged.connect(self._page_changed)

    # -- section contract

    def on_enter(self) -> None:
        if not self._looked_up:
            self.lookup_image()
        self._page_changed(self.stack.currentIndex())

    def hideEvent(self, event) -> None:
        self.timer.stop()
        super().hideEvent(event)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._page_changed(self.stack.currentIndex())

    # -- workers

    def _run(self, fn, on_done, on_fail, on_progress=None) -> _Task:
        task = _Task(fn)
        self._tasks.add(task)

        def finish(handler):
            def slot(value):
                self._tasks.discard(task)
                handler(value)
            return slot

        # queued: the signals object lives on the UI thread, so handlers run there
        queued = Qt.ConnectionType.QueuedConnection
        task.signals.done.connect(finish(on_done), queued)
        task.signals.failed.connect(finish(on_fail), queued)
        if on_progress is not None:
            task.signals.progress.connect(on_progress, queued)
        self.pool.start(task)
        return task

    # ===================================================================== page 1: image

    def _build_image_page(self) -> None:
        p = _Page("Flash the SD card")
        p.body.addWidget(_text(
            "The app will put WeatherPi onto a micro SD card for your weather station. "
            "You'll need the card and a card reader in this PC. Everything already on the "
            "card will be erased."))
        self.img_status = _text("Looking for the latest version of WeatherPi…", bold=True)
        p.body.addWidget(self.img_status)
        self.img_bar = QProgressBar()
        self.img_bar.setRange(0, 1000)
        self.img_bar.setTextVisible(False)
        self.img_bar.hide()
        p.body.addWidget(self.img_bar)
        self.img_bar_text = _text(muted=True)
        self.img_bar_text.hide()
        p.body.addWidget(self.img_bar_text)
        self.img_error = _ErrorBox()
        p.body.addWidget(self.img_error)
        self.btn_local = p.add_button(_button("Use a file on this PC…"))
        self.btn_cancel_dl = p.add_button(_button("Cancel"))
        self.btn_cancel_dl.hide()
        self.btn_retry_lookup = p.add_button(_button("Try again"))
        self.btn_retry_lookup.hide()
        self.btn_image_next = p.add_button(_button("Download", primary=True))
        self.btn_image_next.setEnabled(False)
        self.btn_local.clicked.connect(self._choose_file)
        self.btn_cancel_dl.clicked.connect(self._cancel_work)
        self.btn_retry_lookup.clicked.connect(self.lookup_image)
        self.btn_image_next.clicked.connect(self._image_next)
        self.stack.addWidget(p)

    def lookup_image(self) -> None:
        self._looked_up = True
        self.img_error.show_error(None)
        self.btn_retry_lookup.hide()
        self.btn_image_next.show()
        self.btn_image_next.setEnabled(False)
        self.img_status.setText("Looking for the latest version of WeatherPi…")
        svc = self.service

        def work(_progress):
            cached = svc.cached_image()
            try:
                return cached, svc.latest_release(), None
            except FlashError as exc:
                return cached, None, exc

        self._run(work, self._lookup_done, self._lookup_failed)

    def _lookup_done(self, value) -> None:
        cached, release, error = value
        self.release = release
        if cached is not None and (release is None or cached.version == release.version
                                   or cached.path.name == release.name):
            self.image = cached
            newest = "the latest version" if release else "the version saved on this PC"
            self.img_status.setText(f"WeatherPi {cached.version} is ready on this PC "
                                    f"({newest}).")
            self.btn_image_next.setText("Continue")
            self.btn_image_next.setEnabled(True)
            return
        if release is not None:
            self.img_status.setText(f"Latest version: WeatherPi {release.version} "
                                    f"({_mb(release.size)} download).")
            self.btn_image_next.setText("Download")
            self.btn_image_next.setEnabled(True)
            return
        self.img_status.setText("No WeatherPi image is available to download right now.")
        self.img_error.show_error(error)
        self.btn_image_next.hide()
        self.btn_retry_lookup.show()

    def _lookup_failed(self, exc) -> None:
        self.img_status.setText("Couldn't check for WeatherPi images.")
        self.img_error.show_error(exc)
        self.btn_retry_lookup.show()

    def _image_next(self) -> None:
        if self.image is not None:
            self.go_cards()
        elif self.release is not None:
            self.start_download()

    def start_download(self) -> None:
        release = self.release
        if release is None:
            return
        self._cancel = threading.Event()
        cancel = self._cancel
        self._set_downloading(True)
        svc = self.service
        self._run(lambda progress: svc.download(release, progress, cancel),
                  self._download_done, self._download_failed, self._download_progress)

    def _set_downloading(self, busy: bool) -> None:
        self.img_bar.setVisible(busy)
        self.img_bar_text.setVisible(busy)
        self.btn_cancel_dl.setVisible(busy)
        self.btn_image_next.setEnabled(not busy)
        self.btn_local.setEnabled(not busy)
        if busy:
            self.btn_cancel_dl.setEnabled(True)
            self.img_error.show_error(None)
            self.img_bar.setValue(0)
            self.img_bar_text.setText("Starting the download…")

    def _download_progress(self, phase: str, done, total) -> None:
        self.img_bar.setValue(int(1000 * done / total) if total else 0)
        if phase == PHASE_DOWNLOAD:
            self.img_bar_text.setText(f"Downloading: {_mb(done)} of {_mb(total)}")
        else:
            self.img_bar_text.setText("Checking the download…")

    def _download_done(self, image: LocalImage) -> None:
        self._set_downloading(False)
        self.image = image
        self.img_status.setText(f"WeatherPi {image.version} is downloaded and checked.")
        self.btn_image_next.setText("Continue")
        self.go_cards()

    def _download_failed(self, exc) -> None:
        self._set_downloading(False)
        self.img_error.show_error(exc)
        self.btn_image_next.setText("Download again" if self.release else "Download")

    def _choose_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose the WeatherPi image", str(Path.home() / "Downloads"),
            "WeatherPi image (*.img.xz);;All files (*)")
        if path:
            self.use_local_file(path)

    def use_local_file(self, path: str | Path) -> None:
        self.img_error.show_error(None)
        self.btn_image_next.setEnabled(False)
        svc = self.service
        self._run(lambda _p: svc.open_local(path), self._local_ok, self._local_failed)

    def _local_ok(self, image: LocalImage) -> None:
        self.image = image
        self.img_error.show_error(None)
        self.btn_retry_lookup.hide()
        self.btn_image_next.show()
        self.img_status.setText(f"Using {image.path.name} (WeatherPi {image.version}).")
        self.btn_image_next.setText("Continue")
        self.btn_image_next.setEnabled(True)
        self.go_cards()

    def _local_failed(self, exc) -> None:
        self.img_error.show_error(exc)
        self.btn_image_next.setEnabled(self.image is not None or self.release is not None)

    # ===================================================================== page 2: card

    def _build_card_page(self) -> None:
        p = _Page("Choose the SD card")
        self.card_image_line = _text(muted=True)
        p.body.addWidget(self.card_image_line)
        self.card_hint = _text("Insert the SD card into this PC's card reader.", bold=True)
        p.body.addWidget(self.card_hint)
        self.card_list = QListWidget()
        self.card_list.setMinimumHeight(120)
        self.card_list.setAlternatingRowColors(True)
        p.body.addWidget(self.card_list)
        self.card_note = _text(muted=True)
        p.body.addWidget(self.card_note)
        p.body.addWidget(_text("Only SD cards and card readers are shown. This PC's own "
                               "drives are never listed.", muted=True))
        self.btn_card_back = p.add_button(_button("Back"))
        self.btn_card_next = p.add_button(_button("Next", primary=True))
        self.btn_card_next.setEnabled(False)
        self.btn_card_back.clicked.connect(lambda: self.stack.setCurrentIndex(self.PAGE_IMAGE))
        self.btn_card_next.clicked.connect(self.go_confirm)
        self.card_list.itemSelectionChanged.connect(self._card_selected)
        self.card_list.itemDoubleClicked.connect(lambda _i: self.go_confirm())
        self.stack.addWidget(p)

    def go_cards(self) -> None:
        if self.image is not None:
            self.card_image_line.setText(f"Writing WeatherPi {self.image.version}")
        self.stack.setCurrentIndex(self.PAGE_CARD)

    def refresh_cards(self) -> None:
        if self._scanning:
            return
        self._scanning = True
        svc, image = self.service, self.image
        self._run(lambda _p: svc.scan(image), self._scan_done, self._scan_failed)

    def _scan_done(self, scan: CardScan) -> None:
        self._scanning = False
        self._show_cards(scan)

    def _scan_failed(self, exc) -> None:
        self._scanning = False
        self.card_note.setText(_owner_message(exc)[0])

    def _show_cards(self, scan: CardScan) -> None:
        keep = self.card.identity if self.card else None
        if [d.identity for d in scan.cards] != [d.identity for d in self._cards]:
            self._cards = list(scan.cards)
            self.card_list.blockSignals(True)
            self.card_list.clear()
            for d in self._cards:
                item = QListWidgetItem(d.label)
                item.setData(Qt.ItemDataRole.UserRole, d.device)
                self.card_list.addItem(item)
            self.card_list.blockSignals(False)
            idx = next((i for i, d in enumerate(self._cards) if d.identity == keep), None)
            if idx is None and len(self._cards) == 1:
                idx = 0
            if idx is not None:
                self.card_list.setCurrentRow(idx)
            self._card_selected()
        self.card_list.setVisible(bool(self._cards))
        many = len(self._cards) > 1
        if not self._cards:
            self.card_hint.setText("Insert the SD card into this PC's card reader. "
                                   "The app keeps checking.")
        elif many:
            self.card_hint.setText("More than one card was found. Choose the one for "
                                   "your WeatherPi.")
        else:
            self.card_hint.setText("This card was found:")
        notes = []
        if scan.locked:
            notes.append(f"{scan.locked[0].label} is locked. Take it out, slide the small "
                         "switch on its side away from \"Lock\", and put it back.")
        if scan.too_small:
            notes.append(f"{scan.too_small[0].label} is too small for WeatherPi. Use a "
                         "card of 8 GB or more.")
        self.card_note.setText("\n".join(notes))

    def _card_selected(self) -> None:
        row = self.card_list.currentRow()
        selected = self.card_list.selectedItems()
        self.card = self._cards[row] if selected and 0 <= row < len(self._cards) else None
        self.btn_card_next.setEnabled(self.card is not None)

    # ===================================================================== page 3: confirm

    def _build_confirm_page(self) -> None:
        p = _Page("Erase this card?")
        self.confirm_text = _text(bold=True)
        p.body.addWidget(self.confirm_text)
        p.body.addWidget(_text(
            "WeatherPi will be written onto it and then checked. This takes about 10 to 20 "
            "minutes. Keep the card in the reader until the app says it's done."))
        p.body.addWidget(_text(
            "Windows will ask for permission to make changes to this PC (that's needed to "
            "write to the card). Answer Yes.", muted=True))
        self.confirm_box = QCheckBox("I understand that everything on this card will be "
                                     "erased")
        p.body.addWidget(self.confirm_box)
        self.btn_confirm_back = p.add_button(_button("Back"))
        self.btn_flash = p.add_button(_button("Flash", primary=True))
        self.btn_flash.setEnabled(False)
        self.confirm_box.toggled.connect(self.btn_flash.setEnabled)
        self.btn_confirm_back.clicked.connect(self.go_cards)
        self.btn_flash.clicked.connect(self.start_flash)
        self.stack.addWidget(p)

    def go_confirm(self) -> None:
        if self.card is None or self.image is None:
            return
        self.confirm_text.setText(f"Everything on {self.card.label} will be erased.")
        self.confirm_box.setChecked(False)
        self.btn_flash.setEnabled(False)
        self.stack.setCurrentIndex(self.PAGE_CONFIRM)

    # ===================================================================== page 4: progress

    def _build_progress_page(self) -> None:
        p = _Page("Flashing the SD card")
        self.prog_phase = _text("Waiting for permission… Answer Yes in the Windows prompt.",
                                bold=True)
        p.body.addWidget(self.prog_phase)
        self.prog_bar = QProgressBar()
        self.prog_bar.setRange(0, 1000)
        self.prog_bar.setFormat("%p%")
        self.prog_bar.setMinimumHeight(24)
        p.body.addWidget(self.prog_bar)
        self.prog_time = _text(muted=True)
        p.body.addWidget(self.prog_time)
        p.body.addWidget(_text("Don't remove the card while this is running.", muted=True))
        self.btn_cancel_flash = p.add_button(_button("Cancel"))
        self.btn_cancel_flash.clicked.connect(self._cancel_work)
        self.stack.addWidget(p)

    def start_flash(self) -> None:
        if self.card is None or self.image is None or not self.confirm_box.isChecked():
            return
        image, card = self.image, self.card
        self._cancel = threading.Event()
        cancel = self._cancel
        self._time_left = TimeLeft({PHASE_VERIFY_IMAGE: image.size, PHASE_WRITE: image.raw_size,
                                    PHASE_VERIFY_CARD: image.raw_size})
        prompt = ("Answer Yes in the Windows prompt." if sys.platform == "win32"
                  else "Enter your password if asked.")
        self.prog_phase.setText(f"Waiting for permission… {prompt}")
        self.prog_bar.setValue(0)
        self.prog_time.setText("")
        self.btn_cancel_flash.setEnabled(True)
        self.stack.setCurrentIndex(self.PAGE_PROGRESS)
        svc = self.service
        self._run(lambda progress: svc.flash(image, card, progress, cancel),
                  self._flash_done, self._flash_failed, self._flash_progress)

    def _flash_progress(self, phase: str, done, total) -> None:
        self.prog_phase.setText(PHASE_TEXT.get(phase, phase))
        self.prog_bar.setValue(int(1000 * done / total) if total else 0)
        if self._time_left is not None:
            self.prog_time.setText(format_time_left(self._time_left.update(phase, done,
                                                                           total)))

    def _flash_done(self, result) -> None:
        self.done_text.setText(DONE_EJECTED if getattr(result, "ejected", False)
                               else DONE_NOT_EJECTED)
        self.stack.setCurrentIndex(self.PAGE_DONE)

    def _flash_failed(self, exc) -> None:
        self.err_box.show_error(exc)
        self.stack.setCurrentIndex(self.PAGE_ERROR)

    def _cancel_work(self) -> None:
        if self._cancel is not None:
            self._cancel.set()
            self.btn_cancel_flash.setEnabled(False)
            self.btn_cancel_dl.setEnabled(False)
            self.prog_phase.setText("Stopping…")
            QTimer.singleShot(0, lambda: self.btn_cancel_dl.setEnabled(True))

    # ===================================================================== page 5: done

    def _build_done_page(self) -> None:
        p = _Page("The SD card is ready")
        p.body.addWidget(_text("WeatherPi was written to the card and checked.", bold=True))
        self.done_text = _text(DONE_NOT_EJECTED)
        p.body.addWidget(self.done_text)
        self.btn_done = p.add_button(_button("Continue", primary=True))
        self.btn_done.clicked.connect(lambda: self.finished.emit(NEXT_SECTION))
        self.stack.addWidget(p)

    # ===================================================================== error page

    def _build_error_page(self) -> None:
        p = _Page("The card wasn't flashed")
        self.err_box = _ErrorBox()
        p.body.addWidget(self.err_box)
        self.btn_err_again = p.add_button(_button("Try again", primary=True))
        self.btn_err_again.clicked.connect(self.go_cards)
        self.stack.addWidget(p)

    # -- helpers

    def _page_changed(self, index: int) -> None:
        if index == self.PAGE_CARD and self.isVisible():
            self.refresh_cards()
            self.timer.start()
        else:
            self.timer.stop()

    def shutdown(self, timeout_ms: int = 5000) -> None:
        """Stop polling, cancel any job and wait for workers (used by tests / app exit)."""
        self.timer.stop()
        if self._cancel is not None:
            self._cancel.set()
        self.pool.waitForDone(timeout_ms)


# --------------------------------------------------------------------------- standalone look


def _demo() -> int:  # pragma: no cover - manual preview
    """``python -m weatherpi_companion.ui.sections.flasher``: the section on its own, using a
    throwaway app data folder. Flashing still needs a real card and UAC."""
    import tempfile
    from types import SimpleNamespace

    app = QApplication.instance() or QApplication(sys.argv)
    ctx = SimpleNamespace(home=Path(tempfile.mkdtemp(prefix="weatherpi-flasher-")))
    w = FlasherSection(ctx)
    w.setWindowTitle(FlasherSection.TITLE)
    w.resize(760, 520)
    w.finished.connect(lambda nxt: print("finished ->", nxt))
    w.show()
    w.on_enter()
    return app.exec()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_demo())
