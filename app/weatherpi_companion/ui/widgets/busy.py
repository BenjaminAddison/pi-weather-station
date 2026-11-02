"""Spinner and busy bar."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QWidget

from ..style import ACCENT


class Spinner(QWidget):
    """A small rotating arc. Animates only while visible and running."""

    def __init__(self, size: int = 20, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._size = size
        self._angle = 0
        self._timer = QTimer(self)
        self._timer.setInterval(40)
        self._timer.timeout.connect(self._tick)
        self.setFixedSize(size, size)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.setAccessibleName("Working")

    def sizeHint(self) -> QSize:
        return QSize(self._size, self._size)

    def start(self) -> None:
        if not self._timer.isActive():
            self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    @property
    def spinning(self) -> bool:
        return self._timer.isActive()

    def showEvent(self, e) -> None:
        self.start()
        super().showEvent(e)

    def hideEvent(self, e) -> None:
        self.stop()
        super().hideEvent(e)

    def _tick(self) -> None:
        self._angle = (self._angle + 12) % 360
        self.update()

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        s = float(min(self.width(), self.height()))
        w = max(2.0, s * 0.12)
        r = QRectF(w / 2 + 1, w / 2 + 1, s - w - 2, s - w - 2)
        p.setPen(QPen(QColor("#dbe3f0"), w))
        p.drawEllipse(r)
        pen = QPen(QColor(ACCENT), w)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.drawArc(r, -self._angle * 16, 100 * 16)
        p.end()


class BusyBar(QFrame):
    """"Looking for your WeatherPi... (12 s)" with a spinner and an optional Cancel."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("BusyBar")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 8, 8, 8)
        lay.setSpacing(10)
        self.spinner = Spinner(18, self)
        self.text = QLabel(self)
        self.text.setWordWrap(True)
        self.cancel_button = QPushButton("Stop", self)
        self.cancel_button.hide()
        lay.addWidget(self.spinner, 0, Qt.AlignmentFlag.AlignVCenter)
        lay.addWidget(self.text, 1)
        lay.addWidget(self.cancel_button, 0)
        self._on_cancel: Callable[[], None] | None = None
        self.cancel_button.clicked.connect(self._cancel)
        self.hide()

    def start(self, text: str, on_cancel: Callable[[], None] | None = None,
              cancel_text: str = "Stop") -> None:
        self.text.setText(text)
        self._on_cancel = on_cancel
        self.cancel_button.setText(cancel_text)
        self.cancel_button.setVisible(on_cancel is not None)
        self.show()
        self.spinner.start()

    def set_text(self, text: str) -> None:
        self.text.setText(text)

    def stop(self) -> None:
        self.spinner.stop()
        self.hide()
        self._on_cancel = None

    @property
    def active(self) -> bool:
        return not self.isHidden()

    def _cancel(self) -> None:
        cb = self._on_cancel
        if cb is not None:
            cb()
