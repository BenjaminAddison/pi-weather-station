"""Opening things outside the app (browser, files, folders). Tests replace ``opener``."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices


def _default_open(target: str) -> bool:
    if "://" in target:
        return QDesktopServices.openUrl(QUrl(target))
    return QDesktopServices.openUrl(QUrl.fromLocalFile(target))


#: ``opener(url_or_path) -> bool``; replaced in tests so nothing really opens
opener: Callable[[str], bool] = _default_open
opened: list[str] = []


def open_url(url: str | None) -> bool:
    if not url:
        return False
    opened.append(url)
    return opener(url)


def open_path(path: str | Path | None) -> bool:
    if not path:
        return False
    opened.append(str(path))
    return opener(str(path))


def show_in_folder(path: str | Path) -> bool:
    """Open the folder with the file selected where the platform supports it."""
    p = Path(path)
    if opener is not _default_open:
        return open_path(p.parent)
    if sys.platform == "win32" and p.exists():
        opened.append(str(p))
        subprocess.Popen(["explorer.exe", "/select,", str(p)])
        return True
    return open_path(p.parent)
