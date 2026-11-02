"""WeatherPi Companion entry point.

Contract shared by W6 (UI) and W7 (packaging); keep these flags working:
  --version   print the app version and exit 0
  --selftest  import every module the frozen app needs (incl. Qt WebEngine), exit 0 on success
  --only ID   (dev) open a window with just one section: flasher, tester, setup or home
  --flash-helper ...  (first argument only) run the elevated SD card flash helper
                      (host/flash_helper.py, W6f) instead of the app; no Qt is imported
"""

import argparse
import importlib
import sys

from weatherpi_companion import __version__

SELFTEST_MODULES = [
    "weatherpi_companion.agent_client",
    "weatherpi_companion.flows",
    "weatherpi_companion.station",
    "weatherpi_companion.secrets",
    "weatherpi_companion.backups",
    "weatherpi_companion.aihelp",
    "weatherpi_companion.host.usb",
    "weatherpi_companion.host.driver",
    "weatherpi_companion.host.tailscale",
    "weatherpi_companion.ui.app",
    "weatherpi_companion.ui.sections.tester",
    "weatherpi_companion.ui.sections.setup",
    "PySide6.QtWidgets",
    "PySide6.QtWebEngineWidgets",
    "paramiko",
    "httpx",
    "cryptography",
]


def selftest() -> int:
    failed = []
    for name in SELFTEST_MODULES:
        try:
            importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001 - report every failure, not just the first
            failed.append(f"{name}: {exc!r}")
    for line in failed:
        print("FAIL", line, file=sys.stderr)
    print("selftest ok" if not failed else f"selftest failed ({len(failed)})")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> None:
    if argv is None:
        argv = sys.argv[1:]
    # the frozen exe re-launches itself elevated for the flash helper: route it before
    # argparse and before anything imports Qt
    if argv and argv[0] == "--flash-helper":
        from weatherpi_companion.host.flash_helper import main as helper_main

        raise SystemExit(helper_main(argv))
    parser = argparse.ArgumentParser(prog="weatherpi-companion")
    parser.add_argument("--version", action="store_true", help="print version and exit")
    parser.add_argument("--selftest", action="store_true", help="check bundled modules and exit")
    parser.add_argument("--only", metavar="SECTION",
                        help="dev: open just one section (flasher, tester, setup, home)")
    args, _qt_args = parser.parse_known_args(argv)
    if args.version:
        print(f"WeatherPi Companion {__version__}")
        raise SystemExit(0)
    if args.selftest:
        raise SystemExit(selftest())
    from weatherpi_companion.ui.app import run

    raise SystemExit(run(_qt_args, only=args.only))


if __name__ == "__main__":
    main()
