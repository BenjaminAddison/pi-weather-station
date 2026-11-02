"""Draw the WeatherPi Companion icon (sun behind a rain cloud) and save it as a .ico.

Run from the repo root:  python installer/make_icon.py
Writes installer/assets/weatherpi.ico (used for the exe, the installer and the shortcuts).
Only needs PySide6. The .ico holds PNG images at the usual Windows sizes.
"""

import struct
import sys
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QGuiApplication, QImage, QLinearGradient, QPainter, QPainterPath

SIZES = [16, 20, 24, 32, 40, 48, 64, 128, 256]
OUT = Path(__file__).resolve().parent / "assets" / "weatherpi.ico"

ACCENT = "#2563eb"  # ui/style.py
ACCENT_DARK = "#1d4ed8"
SUN = "#fbbf24"
DROP = "#bfdbfe"


def draw(size: int) -> QImage:
    img = QImage(size, size, QImage.Format.Format_ARGB32)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.scale(size / 256, size / 256)  # draw on a 256 grid

    # rounded tile
    grad = QLinearGradient(0, 0, 0, 256)
    grad.setColorAt(0, QColor(ACCENT))
    grad.setColorAt(1, QColor(ACCENT_DARK))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(grad)
    p.drawRoundedRect(QRectF(8, 8, 240, 240), 52, 52)

    # sun, upper right, behind the cloud
    p.setBrush(QColor(SUN))
    p.drawEllipse(QPointF(160, 92), 46, 46)

    # cloud
    cloud = QPainterPath()
    cloud.setFillRule(Qt.FillRule.WindingFill)  # union of the overlapping puffs
    cloud.addEllipse(QPointF(92, 132), 38, 38)
    cloud.addEllipse(QPointF(138, 112), 48, 48)
    cloud.addEllipse(QPointF(182, 140), 32, 32)
    cloud.addRoundedRect(QRectF(54, 130, 160, 42), 21, 21)
    p.setBrush(QColor("#ffffff"))
    p.drawPath(cloud.simplified())

    # rain drops (left out at the smallest sizes, where they'd only be noise)
    if size >= 24:
        p.setBrush(QColor(DROP))
        for x, y in ((92, 196), (134, 210), (176, 196)):
            drop = QPainterPath()
            drop.moveTo(x, y - 18)
            drop.cubicTo(x + 4, y - 8, x + 11, y, x + 11, y + 6)
            drop.arcTo(QRectF(x - 11, y - 5, 22, 22), 0, -180)
            drop.cubicTo(x - 11, y, x - 4, y - 8, x, y - 18)
            p.drawPath(drop)
    p.end()
    return img


def png_bytes(img: QImage) -> bytes:
    data = QByteArray()
    buf = QBuffer(data)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    return bytes(data)


def write_ico(path: Path, images: list[tuple[int, bytes]]) -> None:
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries, blobs = b"", b""
    for size, png in images:
        dim = 0 if size >= 256 else size  # 0 means 256 in the ICO directory
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(png), offset)
        blobs += png
        offset += len(png)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(header + entries + blobs)


def main() -> int:
    _app = QGuiApplication(sys.argv)
    write_ico(OUT, [(s, png_bytes(draw(s))) for s in SIZES])
    draw(256).save(str(OUT.with_suffix(".png")))
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
