"""A simple drawn picture of how to plug the WeatherPi in: one data cable from the port
marked USB to this computer, which also powers it. PWR is crossed out: powering the Pi
from both ports at once can destroy it."""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from ..style import ACCENT, FAIL, TEXT

BOARD = QColor("#1f7a4d")
BOARD_EDGE = QColor("#155c39")
PORT = QColor("#c7ccd3")
#: the drawing's own coordinate space, scaled to fit the widget
DESIGN_W, DESIGN_H = 500.0, 240.0


class PlugInDiagram(QWidget):
    """Fixed height; the drawing is scaled to fit and centred."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumWidth(420)
        self.setFixedHeight(210)  # the drawing scales into whatever width it gets
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setAccessibleName("Picture: plug the data cable into the port marked USB and "
                               "connect it to this computer. The cable powers the WeatherPi "
                               "too. Never plug anything into PWR at the same time.")
        self.setToolTip(self.accessibleName().removeprefix("Picture: "))

    def sizeHint(self) -> QSize:
        return QSize(620, 210)

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = float(self.width()), float(self.height())
        # scale the design into the widget, centred
        k = min(w / DESIGN_W, h / DESIGN_H)
        p.translate((w - DESIGN_W * k) / 2, (h - DESIGN_H * k) / 2)
        p.scale(k, k)
        f = QFont(self.font())
        f.setPointSizeF(10.5)
        f.setBold(True)
        small = QFont(self.font())
        small.setPointSizeF(9)

        # the board (Pi Zero: SD card on the left, ports along the bottom edge)
        board = QRectF(200, 30, 240, 105)
        p.setPen(QPen(BOARD_EDGE, 2))
        p.setBrush(BOARD)
        p.drawRoundedRect(board, 12, 12)
        p.setBrush(QColor("#0f3d26"))
        p.setPen(Qt.PenStyle.NoPen)
        for x in (214, 426):  # mounting holes
            for y in (44, 121):
                p.setBrush(QColor("#e9edf2"))
                p.drawEllipse(QPointF(x, y), 5, 5)
        p.setBrush(QColor("#2b2f36"))
        p.drawRoundedRect(QRectF(290, 62, 46, 40), 3, 3)  # chip
        p.setBrush(QColor("#9aa3ad"))
        p.drawRoundedRect(QRectF(188, 62, 16, 40), 2, 2)  # SD card
        p.setPen(QColor("white"))
        p.setFont(small)
        p.drawText(QRectF(200, 34, 240, 20), Qt.AlignmentFlag.AlignCenter, "WeatherPi")

        # ports: mini HDMI, USB (data), PWR
        ports = {"HDMI": 250.0, "USB": 352.0, "PWR": 404.0}
        p.setFont(small)
        for name, x in ports.items():
            p.setPen(QPen(QColor("#8b939e"), 1))
            p.setBrush(PORT)
            pw = 30 if name == "HDMI" else 22
            p.drawRoundedRect(QRectF(x - pw / 2, 127, pw, 12), 2, 2)
            p.setPen(QColor("white") if name != "HDMI" else QColor("#cfe3d8"))
            p.drawText(QRectF(x - 24, 108, 48, 16), Qt.AlignmentFlag.AlignCenter, name)

        # data cable: USB port -> laptop on the left
        cable = QPen(QColor(ACCENT), 5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
        p.setPen(cable)
        p.setBrush(Qt.BrushStyle.NoBrush)
        path = QPainterPath(QPointF(352, 142))
        path.cubicTo(QPointF(352, 200), QPointF(200, 200), QPointF(144, 160))
        p.drawPath(path)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(ACCENT))
        p.drawRoundedRect(QRectF(345, 139, 14, 14), 3, 3)

        # laptop
        p.setPen(QPen(QColor("#4b5563"), 2))
        p.setBrush(QColor("#eef2f7"))
        p.drawRoundedRect(QRectF(30, 92, 100, 64), 5, 5)
        p.setBrush(QColor("#cfd8e3"))
        p.drawRoundedRect(QRectF(38, 99, 84, 50), 2, 2)
        p.setBrush(QColor("#4b5563"))
        p.drawRoundedRect(QRectF(18, 156, 124, 9), 3, 3)
        p.setPen(QColor(TEXT))
        p.setFont(f)
        p.drawText(QRectF(0, 172, 140, 20), Qt.AlignmentFlag.AlignCenter, "This computer")

        # PWR must stay empty: power from both ports at once can destroy the Pi
        x = ports["PWR"]
        p.setPen(QPen(QColor(FAIL), 3.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawLine(QPointF(x - 9, 142), QPointF(x + 9, 160))
        p.drawLine(QPointF(x + 9, 142), QPointF(x - 9, 160))
        p.setFont(f)
        p.setPen(QColor(FAIL))
        p.drawText(QRectF(x - 65, 164, 130, 18), Qt.AlignmentFlag.AlignCenter, "Nothing in PWR")

        # caption
        p.setFont(small)
        p.setPen(QColor(ACCENT))
        p.drawText(QRectF(40, 206, 400, 20), Qt.AlignmentFlag.AlignCenter,
                   "One cable only: this computer to USB. It powers the WeatherPi too.")
        p.end()
