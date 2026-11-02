"""Section registry (docs/companion-app.md, "Section contract").

Each section lives in ``ui/sections/<id>.py`` and exposes a ``QWidget`` subclass with
``SECTION_ID``, ``TITLE``, ``finished = Signal(str)``, a ``(ctx, parent=None)`` constructor
and an optional ``on_enter()``. Modules are imported lazily: a section whose module
doesn't exist yet (e.g. ``flasher`` before its branch is merged, ``home`` before its round)
gets a friendly placeholder instead, so adding the file is all a merge needs.
"""

from __future__ import annotations

import importlib
import logging

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QHBoxLayout, QPushButton, QVBoxLayout, QWidget

from ..context import AppContext
from ..style import label, set_role

log = logging.getLogger(__name__)

#: the owner's journey, in order: (section id, title in the step list)
SECTIONS: list[tuple[str, str]] = [
    ("flasher", "Flash SD card"),
    ("tester", "Check the Pi"),
    ("setup", "Set up"),
    ("home", "Home"),
]
SECTION_IDS = [s for s, _ in SECTIONS]
TITLES = dict(SECTIONS)


def load_section_class(section_id: str) -> type[QWidget] | None:
    """The section's widget class, or None if its module isn't there (yet)."""
    if section_id not in SECTION_IDS:
        raise KeyError(section_id)
    name = f"{__name__}.{section_id}"
    try:
        mod = importlib.import_module(name)
    except ModuleNotFoundError as exc:
        if exc.name == name:
            return None
        log.exception("section %s failed to import", section_id)
        return None
    except Exception:
        log.exception("section %s failed to import", section_id)
        return None
    for obj in vars(mod).values():
        if (isinstance(obj, type) and issubclass(obj, QWidget)
                and getattr(obj, "SECTION_ID", None) == section_id):
            return obj
    log.error("module %s has no QWidget with SECTION_ID=%r", name, section_id)
    return None


def create_section(section_id: str, ctx: AppContext, parent: QWidget | None = None) -> QWidget:
    cls = load_section_class(section_id)
    if cls is None:
        return PlaceholderSection(section_id, ctx, parent)
    return cls(ctx, parent)


_PLACEHOLDERS = {
    "flasher": (
        "SD card flashing is coming soon",
        ("Soon you'll be able to write the WeatherPi software to a memory card right here. "
        "Until then, use Raspberry Pi Imager: choose \"Use custom\", pick the WeatherPi "
        ".img.xz file and your SD card, and say No when it offers to change settings."),
        [("My card is flashed: check the Pi", "tester")],
    ),
    "home": (
        "The home screen is coming soon",
        ("Your WeatherPi is set up. A home screen with its status, backups and help will "
        "arrive in a later version. You can open the dashboard in your web browser from "
        "any device signed in to your Tailscale account."),
        [("Check the Pi again", "tester")],
    ),
}


class PlaceholderSection(QWidget):
    """Stands in for a section that isn't built yet."""

    finished = Signal(str)

    def __init__(self, section_id: str, ctx: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Page")
        self.SECTION_ID = section_id
        self.TITLE = TITLES.get(section_id, section_id)
        self.ctx = ctx
        title, text, buttons = _PLACEHOLDERS.get(
            section_id, (f"{self.TITLE} is coming soon", "", []))
        lay = QVBoxLayout(self)
        lay.setContentsMargins(36, 28, 36, 28)
        lay.setSpacing(12)
        lay.addWidget(label(self.TITLE.upper(), "PageStep"))
        lay.addWidget(label(title, "PageTitle"))
        if text:
            lay.addWidget(label(text, "Intro"))
        self.dashboard: QPushButton | None = None
        if section_id == "home":
            url = next((p.serve_url for p in ctx.env.app_data.list_profiles()
                        if p.setup_complete and p.serve_url), None)
            if url:
                self.dashboard = QPushButton("Open the dashboard", self)
                set_role(self.dashboard, "primary")
                self.dashboard.clicked.connect(lambda: _open(url))
                r = QHBoxLayout()
                r.addWidget(self.dashboard)
                r.addStretch(1)
                lay.addLayout(r)
                lay.addWidget(label(url, "Muted"))
        row = QHBoxLayout()
        self.buttons: list[QPushButton] = []
        for text_, target in buttons:
            b = QPushButton(text_, self)
            b.clicked.connect(lambda _=False, t=target: self.finished.emit(t))
            row.addWidget(b)
            self.buttons.append(b)
        row.addStretch(1)
        lay.addLayout(row)
        lay.addStretch(1)

    def on_enter(self) -> None:
        pass


def _open(url: str) -> None:
    from .. import desktop

    desktop.open_url(url)
