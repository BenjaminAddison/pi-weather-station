"""Everything outside ui/ must stay Qt-free (docs/companion-app.md)."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

PKG = Path(__file__).resolve().parents[1] / "weatherpi_companion"
QT = ("PySide6", "PyQt5", "PyQt6", "shiboken6", "qtpy")


def test_no_qt_imports_outside_ui():
    offenders = []
    for py in PKG.rglob("*.py"):
        if "ui" in py.relative_to(PKG).parts:
            continue
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            offenders += [f"{py.name}: {n}" for n in names if n.split(".")[0] in QT]
    assert offenders == []


def test_core_modules_import_without_loading_qt():
    code = (
        "import sys\n"
        "import weatherpi_companion.agent_client, weatherpi_companion.flows, "
        "weatherpi_companion.station, weatherpi_companion.backups, "
        "weatherpi_companion.aihelp, weatherpi_companion.secrets\n"
        f"bad = [m for m in sys.modules if m.split('.')[0] in {QT!r}]\n"
        "assert not bad, bad\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True, cwd=PKG.parent)
