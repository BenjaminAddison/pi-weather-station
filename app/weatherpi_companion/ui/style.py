"""App-wide look: colours, a small stylesheet, and painted status icons (no image files,
so they stay sharp at any scaling)."""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPalette, QPen, QPixmap
from PySide6.QtWidgets import QApplication, QLabel, QWidget

ACCENT = "#2563eb"
ACCENT_DARK = "#1d4ed8"
ACCENT_SOFT = "#e8efff"
TEXT = "#1f2937"
MUTED = "#6b7280"
BORDER = "#e3e6eb"
BG = "#f5f7fa"
CARD = "#ffffff"
OK = "#16a34a"
WARN = "#d97706"
FAIL = "#dc2626"
SIDEBAR = "#eef2f7"

STYLESHEET = f"""
QWidget {{ color: {TEXT}; }}
QMainWindow, QWidget#Shell {{ background: {BG}; }}
QWidget#Sidebar {{ background: {SIDEBAR}; border-right: 1px solid {BORDER}; }}
QWidget#Content, QStackedWidget#Pages, QWidget#Page {{ background: {CARD}; }}
QScrollArea {{ background: transparent; border: none; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}

QLabel#AppName {{ font-size: 13pt; font-weight: 600; color: {TEXT}; }}
QLabel#PageTitle {{ font-size: 17pt; font-weight: 600; color: {TEXT}; }}
QLabel#PageStep {{ color: {MUTED}; font-size: 9pt; letter-spacing: 0.5px; }}
QLabel#Intro {{ color: #374151; font-size: 10.5pt; }}
QLabel#Muted, QLabel[muted="true"] {{ color: {MUTED}; }}
QLabel#SectionHeading {{ font-weight: 600; font-size: 10.5pt; }}
QLabel#BigNumber {{ font-size: 15pt; font-weight: 600; }}

QPushButton {{
    background: {CARD}; border: 1px solid #cfd5dd; border-radius: 6px;
    padding: 6px 14px; min-height: 20px;
}}
QPushButton:hover {{ background: #f3f5f8; border-color: #b9c1cc; }}
QPushButton:pressed {{ background: #e9edf2; }}
QPushButton:focus {{ border: 2px solid {ACCENT}; padding: 5px 13px; }}
QPushButton:disabled {{ color: #a0a7b1; background: #f4f5f7; border-color: #e1e4e8; }}
QPushButton[role="primary"] {{
    background: {ACCENT}; color: white; border: 1px solid {ACCENT}; font-weight: 600;
}}
QPushButton[role="primary"]:hover {{ background: {ACCENT_DARK}; }}
QPushButton[role="primary"]:focus {{ border: 2px solid #93b4f8; }}
QPushButton[role="primary"]:disabled {{ background: #a9bff3; border-color: #a9bff3;
    color: #eef3ff; }}
QPushButton[role="link"] {{
    background: transparent; border: none; color: {ACCENT}; padding: 2px 4px;
    text-decoration: underline;
}}
QPushButton[role="link"]:focus {{ border: 1px dotted {ACCENT}; padding: 1px 3px; }}
QPushButton[role="nav"] {{
    text-align: left; background: transparent; border: none; border-radius: 8px;
    padding: 9px 14px; margin: 0px 10px; color: #374151;
}}
QPushButton[role="nav"]:hover {{ background: #e3e8ef; }}
QPushButton[role="nav"]:checked {{ background: {CARD}; color: {ACCENT}; font-weight: 600;
    border: 1px solid {BORDER}; }}
QPushButton[role="nav"]:focus {{ border: 2px solid {ACCENT}; padding: 8px 13px; }}
QPushButton[role="big"] {{ text-align: left; padding: 0px; border-radius: 10px; }}
QPushButton[role="big"]:hover {{ background: {ACCENT_SOFT}; border-color: #9db8f5; }}
QPushButton[role="big"]:focus {{ border: 2px solid {ACCENT}; padding: 0px; }}

QLineEdit, QPlainTextEdit, QSpinBox {{
    background: white; border: 1px solid #cfd5dd; border-radius: 6px; padding: 5px 8px;
    selection-background-color: {ACCENT};
}}
QLineEdit:focus, QPlainTextEdit:focus, QSpinBox:focus {{ border: 2px solid {ACCENT};
    padding: 4px 7px; }}
QLineEdit:disabled {{ background: #f4f5f7; color: #8a919b; }}

QListWidget {{
    background: white; border: 1px solid {BORDER}; border-radius: 8px; padding: 4px;
    outline: none;
}}
QListWidget::item {{ border-radius: 6px; padding: 2px; }}
QListWidget::item:selected {{ background: {ACCENT_SOFT}; color: {TEXT}; }}
QListWidget::item:hover:!selected {{ background: #f5f7fa; }}
QListWidget:focus {{ border: 2px solid {ACCENT}; padding: 3px; }}

QFrame#Card {{ background: {CARD}; border: 1px solid {BORDER}; border-radius: 10px; }}
QFrame#CheckRow {{ background: {CARD}; border: 1px solid {BORDER}; border-radius: 8px; }}
QFrame#CheckRow[state="fail"] {{ border-color: #f3c4c4; background: #fff8f8; }}
QFrame#CheckRow[state="warn"] {{ border-color: #f2dcb5; background: #fffbf3; }}
QFrame#Callout {{ background: {ACCENT_SOFT}; border: 1px solid #c9d8fb; border-radius: 8px; }}
QFrame#NavBar {{ background: {CARD}; border-top: 1px solid {BORDER}; }}
QFrame#BusyBar {{ background: #f8fafc; border: 1px solid {BORDER}; border-radius: 8px; }}

QProgressBar {{ background: #e9edf2; border: none; border-radius: 4px; height: 8px;
    text-align: center; }}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 4px; }}

QTabWidget::pane {{ border: 1px solid {BORDER}; border-radius: 8px; top: -1px;
    background: white; }}
QTabBar::tab {{ padding: 7px 16px; border: 1px solid transparent; border-bottom: none;
    color: {MUTED}; }}
QTabBar::tab:selected {{ color: {TEXT}; font-weight: 600; border-color: {BORDER};
    border-top-left-radius: 8px; border-top-right-radius: 8px; background: white; }}
QTabBar::tab:focus {{ color: {ACCENT}; }}

QCheckBox:focus {{ color: {ACCENT}; }}
QToolTip {{ background: #111827; color: white; border: none; padding: 4px 8px; }}
"""


def apply_app_style(app: QApplication) -> None:
    """Fusion + a light palette + the stylesheet, so it looks the same on every PC
    (including Windows set to dark mode, which would otherwise mix in dark widgets)."""
    try:
        app.styleHints().setColorScheme(Qt.ColorScheme.Light)
    except AttributeError:  # Qt < 6.8
        pass
    app.setStyle("Fusion")
    pal = QPalette()
    pal.setColor(QPalette.ColorRole.Window, QColor(BG))
    pal.setColor(QPalette.ColorRole.WindowText, QColor(TEXT))
    pal.setColor(QPalette.ColorRole.Base, QColor("white"))
    pal.setColor(QPalette.ColorRole.AlternateBase, QColor(BG))
    pal.setColor(QPalette.ColorRole.Text, QColor(TEXT))
    pal.setColor(QPalette.ColorRole.Button, QColor(CARD))
    pal.setColor(QPalette.ColorRole.ButtonText, QColor(TEXT))
    pal.setColor(QPalette.ColorRole.Highlight, QColor(ACCENT))
    pal.setColor(QPalette.ColorRole.HighlightedText, QColor("white"))
    pal.setColor(QPalette.ColorRole.Link, QColor(ACCENT))
    pal.setColor(QPalette.ColorRole.PlaceholderText, QColor("#9aa1ab"))
    pal.setColor(QPalette.ColorRole.ToolTipBase, QColor("#111827"))
    pal.setColor(QPalette.ColorRole.ToolTipText, QColor("white"))
    app.setPalette(pal)
    f = app.font()
    if f.pointSizeF() < 10:
        f.setPointSizeF(10)
        app.setFont(f)
    app.setStyleSheet(STYLESHEET)


def set_role(widget: QWidget, role: str) -> QWidget:
    widget.setProperty("role", role)
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    return widget


def repolish(widget: QWidget) -> None:
    widget.style().unpolish(widget)
    widget.style().polish(widget)


def label(text: str = "", name: str | None = None, wrap: bool = True,
          rich: bool = False) -> QLabel:
    lab = QLabel(text)
    if name:
        lab.setObjectName(name)
    lab.setWordWrap(wrap)
    lab.setTextFormat(Qt.TextFormat.RichText if rich else Qt.TextFormat.PlainText)
    if rich:
        lab.setOpenExternalLinks(True)
    lab.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse
                                | (Qt.TextInteractionFlag.LinksAccessibleByMouse if rich
                                   else Qt.TextInteractionFlag.NoTextInteraction))
    return lab


def bold_font(widget: QWidget, size: float | None = None) -> QFont:
    f = QFont(widget.font())
    f.setBold(True)
    if size:
        f.setPointSizeF(size)
    return f


# --------------------------------------------------------------------------- icons


def _canvas(size: int, dpr: float) -> tuple[QPixmap, QPainter]:
    pm = QPixmap(round(size * dpr), round(size * dpr))
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    return pm, p


def status_icon(state: str, size: int = 22, dpr: float = 1.0) -> QPixmap:
    """``ok`` (green tick), ``warn`` (amber !), ``fail`` (red x), ``pending`` (grey ring),
    ``info`` (blue i)."""
    pm, p = _canvas(size, dpr)
    s = float(size)
    colour = {"ok": OK, "warn": WARN, "fail": FAIL, "info": ACCENT}.get(state, "#9ca3af")
    if state == "pending":
        p.setPen(QPen(QColor(colour), s * 0.1))
        p.drawEllipse(QRectF(s * 0.12, s * 0.12, s * 0.76, s * 0.76))
        p.end()
        return pm
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(colour))
    if state == "warn":
        path = QPainterPath()
        path.moveTo(s * 0.5, s * 0.06)
        path.lineTo(s * 0.96, s * 0.9)
        path.lineTo(s * 0.04, s * 0.9)
        path.closeSubpath()
        p.drawPath(path)
    else:
        p.drawEllipse(QRectF(0.5, 0.5, s - 1, s - 1))
    pen = QPen(QColor("white"), s * 0.12, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
               Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    if state == "ok":
        p.drawPolyline([QPointF(s * 0.28, s * 0.52), QPointF(s * 0.44, s * 0.68),
                        QPointF(s * 0.73, s * 0.35)])
    elif state == "fail":
        p.drawLine(QPointF(s * 0.33, s * 0.33), QPointF(s * 0.67, s * 0.67))
        p.drawLine(QPointF(s * 0.67, s * 0.33), QPointF(s * 0.33, s * 0.67))
    elif state == "warn":
        p.drawLine(QPointF(s * 0.5, s * 0.36), QPointF(s * 0.5, s * 0.6))
        p.drawPoint(QPointF(s * 0.5, s * 0.75))
    elif state == "info":
        p.drawLine(QPointF(s * 0.5, s * 0.45), QPointF(s * 0.5, s * 0.72))
        p.drawPoint(QPointF(s * 0.5, s * 0.29))
    p.end()
    return pm


def signal_bars_icon(bars: int, size: int = 20, dpr: float = 1.0) -> QPixmap:
    pm, p = _canvas(size, dpr)
    s = float(size)
    w = s / 5.5
    for i in range(4):
        h = s * (0.3 + 0.22 * i)
        x = i * (w + s * 0.06) + s * 0.04
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(TEXT if i < bars else "#d1d5db"))
        p.drawRoundedRect(QRectF(x, s * 0.95 - h, w, h), 1.2, 1.2)
    p.end()
    return pm


def lock_icon(size: int = 16, dpr: float = 1.0) -> QPixmap:
    pm, p = _canvas(size, dpr)
    s = float(size)
    p.setPen(QPen(QColor(MUTED), s * 0.12))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawArc(QRectF(s * 0.27, s * 0.08, s * 0.46, s * 0.55), 0, 180 * 16)
    p.drawLine(QPointF(s * 0.27, s * 0.35), QPointF(s * 0.27, s * 0.45))
    p.drawLine(QPointF(s * 0.73, s * 0.35), QPointF(s * 0.73, s * 0.45))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(MUTED))
    p.drawRoundedRect(QRectF(s * 0.15, s * 0.43, s * 0.7, s * 0.5), 2, 2)
    p.end()
    return pm
