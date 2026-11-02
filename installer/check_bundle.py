"""Check the PyInstaller bundle isn't missing a DLL (the spec trims unused Qt modules).

Every .exe/.dll/.pyd in the bundle must find each DLL it imports either in the bundle or in
Windows itself. Delay-loaded imports are optional at run time, so they're not checked.

    python installer/check_bundle.py "dist/WeatherPi Companion"
"""

import os
import sys
from pathlib import Path

import pefile

SYSTEM32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"


def imports_of(path: Path) -> list[str]:
    pe = pefile.PE(str(path), fast_load=True)
    pe.parse_data_directories(
        directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"]])
    names = [e.dll.decode("ascii", "replace") for e in getattr(pe, "DIRECTORY_ENTRY_IMPORT", [])]
    pe.close()
    return names


def is_system(name: str) -> bool:
    low = name.lower()
    return low.startswith(("api-ms-win-", "ext-ms-")) or (SYSTEM32 / name).is_file()


def main(argv: list[str]) -> int:
    root = Path(argv[1] if len(argv) > 1 else "dist/WeatherPi Companion")
    files = [p for p in root.rglob("*") if p.suffix.lower() in {".exe", ".dll", ".pyd"}]
    bundled = {p.name.lower() for p in files}
    missing: dict[str, list[str]] = {}
    for f in files:
        for dll in imports_of(f):
            if dll.lower() not in bundled and not is_system(dll):
                missing.setdefault(dll, []).append(str(f.relative_to(root)))
    for dll, users in sorted(missing.items()):
        print(f"MISSING {dll}  (needed by {', '.join(users[:3])}"
              f"{', ...' if len(users) > 3 else ''})")
    print(f"checked {len(files)} binaries: "
          + ("ok" if not missing else f"{len(missing)} missing DLL(s)"))
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
