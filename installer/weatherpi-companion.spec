# PyInstaller spec for WeatherPi Companion (onedir, windowed). D5.
#
#   pyinstaller installer/weatherpi-companion.spec --distpath dist --workpath build
#   -> dist/WeatherPi Companion/WeatherPi Companion.exe
#
# installer/build.ps1 runs this, then the frozen --selftest, then Inno Setup.
# -*- mode: python -*-

import re
from pathlib import Path

from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo, StringFileInfo, StringStruct, StringTable, VarFileInfo, VarStruct,
    VSVersionInfo,
)

NAME = "WeatherPi Companion"
REPO = Path(SPECPATH).resolve().parent
APP = REPO / "app"
PKG = APP / "weatherpi_companion"
ICON = Path(SPECPATH) / "assets" / "weatherpi.ico"

VERSION = re.search(r'__version__ = "([^"]+)"', (PKG / "__init__.py").read_text()).group(1)

# Sections are imported lazily by name (ui/sections/__init__.py), so list every module.
app_modules = sorted(
    ".".join(p.relative_to(APP).with_suffix("").parts).removesuffix(".__init__")
    for p in PKG.rglob("*.py")
)
# Non-Python files in the package (resources/ai_base.md, ...), kept at their package path.
app_data = [
    (str(p), str(p.parent.relative_to(APP)))
    for p in PKG.rglob("*")
    if p.is_file() and p.suffix not in {".py", ".pyc"} and "__pycache__" not in p.parts
]

a = Analysis(
    [str(Path(SPECPATH) / "launcher.py")],
    pathex=[str(APP)],
    datas=app_data,
    hiddenimports=[
        *app_modules,
        # --selftest imports it by name; the home screen's Dashboard tab needs it (D24)
        "PySide6.QtWebEngineWidgets",
    ],
    excludes=["tkinter", "pytest", "pytestqt", "_pytest"],
    noarchive=False,
)

# The dev venv has an editable install of weatherpi-v2/app; make sure the code we bundle is
# this checkout's (it matters when building from a worktree).
for name, path, _kind in a.pure:
    if name == "weatherpi_companion" or name.startswith("weatherpi_companion."):
        if not Path(path).resolve().is_relative_to(PKG):
            raise SystemExit(f"{name} came from {path}, not {PKG}")

# Trim Qt. QtWebEngine drags in the QML plugin tree (and through it 3D, Charts, Multimedia...),
# every translation and Chromium's debug resources; the app uses Qt Widgets in English only.
# installer/check_bundle.py (run by build.ps1) catches a DLL that was still needed.
from fnmatch import fnmatch  # noqa: E402

TRIM = [
    "PySide6/qml/*",
    "PySide6/plugins/qmltooling/*",
    "PySide6/plugins/position/*",
    "PySide6/plugins/platforminputcontexts/qtvirtualkeyboardplugin.dll",
    "PySide6/resources/*.debug.*",
    "PySide6/Qt63D*", "PySide6/Qt6Quick3D*", "PySide6/Qt6Charts*", "PySide6/Qt6DataVisualization*",
    "PySide6/Qt6Graphs*", "PySide6/Qt6Location*", "PySide6/Qt6Multimedia*",
    "PySide6/Qt6RemoteObjects*", "PySide6/Qt6Scxml*", "PySide6/Qt6Sensors*",
    "PySide6/Qt6TextToSpeech*", "PySide6/Qt6VirtualKeyboard*", "PySide6/Qt6Labs*",
    "PySide6/Qt6QuickControls2*", "PySide6/Qt6QuickDialogs2*", "PySide6/Qt6QuickTest*",
    "PySide6/Qt6Test*", "PySide6/Qt6SpatialAudio*", "PySide6/Qt6WebView*",
    "PySide6/Qt6StateMachine*", "PySide6/Qt6QmlDesignSupport*", "PySide6/Qt6QuickTimeline*",
    "PySide6/Qt6QuickVectorImage*", "PySide6/Qt6QuickParticles*", "PySide6/Qt6QuickEffects*",
    "PySide6/Qt6QuickShapes*", "PySide6/Qt6ShaderTools*", "PySide6/Qt6WebEngineQuick*",
    "PySide6/Qt6PdfQuick*", "PySide6/Qt6SerialPort*", "PySide6/Qt6WebSockets*",
    "PySide6/Qt6QmlLocalStorage*", "PySide6/Qt6QmlXmlListModel*", "PySide6/Qt6QmlNetwork*",
    "PySide6/Qt6PositioningQuick*", "PySide6/Qt6WebChannelQuick*", "PySide6/Qt6ChartsQml*",
    "PySide6/Qt6QuickLayouts*", "PySide6/Qt6QuickTemplates2*", "PySide6/Qt6QuickNativeStyle*",
    "PySide6/Qt6QmlCore*",
]
KEEP_TRANSLATIONS = ["qt_en.qm", "qtbase_en.qm", "qtwebengine_en.qm", "en-US.pak", "en-GB.pak"]


def _keep(dest: str) -> bool:
    d = dest.replace("\\", "/")
    if d.startswith("PySide6/translations/"):
        return d.rsplit("/", 1)[-1] in KEEP_TRANSLATIONS
    return not any(fnmatch(d, pat) for pat in TRIM)


a.binaries = [e for e in a.binaries if _keep(e[0])]
a.datas = [e for e in a.datas if _keep(e[0])]

pyz = PYZ(a.pure)


def _version_tuple(v: str) -> tuple[int, int, int, int]:
    nums = [int(n) for n in re.findall(r"\d+", v)[:4]]
    return tuple(nums + [0] * (4 - len(nums)))


version_info = VSVersionInfo(
    ffi=FixedFileInfo(filevers=_version_tuple(VERSION), prodvers=_version_tuple(VERSION)),
    kids=[
        StringFileInfo([StringTable("040904B0", [
            StringStruct("CompanyName", "WeatherPi"),
            StringStruct("FileDescription", NAME),
            StringStruct("FileVersion", VERSION),
            StringStruct("InternalName", "weatherpi-companion"),
            StringStruct("OriginalFilename", f"{NAME}.exe"),
            StringStruct("ProductName", NAME),
            StringStruct("ProductVersion", VERSION),
        ])]),
        VarFileInfo([VarStruct("Translation", [0x0409, 1200])]),
    ],
)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=NAME,
    icon=str(ICON),
    version=version_info,
    console=False,  # a windowed app: --version/--selftest report through the exit code only
    upx=False,      # UPX breaks Qt DLLs and upsets virus scanners
)

coll = COLLECT(exe, a.binaries, a.datas, name=NAME, upx=False)
