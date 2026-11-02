# Dev environment

## Machines

| Where | Role | Notes |
|---|---|---|
| Windows PC | Main dev box: all code, app builds, the installer, and running tests | Python 3.12, Git (Git Bash for the shell scripts and tests), Tailscale |
| A Linux machine with Docker | Image builds only: the `weatherpi-builder` container (docs/image-build.md) | Reached over SSH; set `BUILDER_HOST=user@host` for `tools/sync-to-builder.sh` |
| Pi Zero W + second SD card | Hardware tests (M1, M6) | Plug its USB port into the PC |

## Python environments (Windows)

```powershell
cd <your clone of this repo>
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e "app[dev]"        # PySide6, httpx, cryptography, pyinstaller, pytest, ruff
pip install -r pi\requirements-dev.txt   # fastapi, uvicorn, pyyaml, pytest, httpx (mirrors the Pi's apt versions)
```
One venv covers both `pi/` and `app/` on the PC. On the Pi, `pi/` uses system Python and apt packages only.

## Running things locally

| What | Command |
|---|---|
| Pi unit tests | `$env:WEATHERPI_FAKE=1; pytest pi\tests` |
| App tests | `pytest app\tests` |
| Run the app by hand | Double-click `start-app.bat` (repo root). It asks for a part (whole app, or one section via `--only`) and runs it against **the real Pi on the USB cable** with your real app data. There's no pretend Pi (D27) |
| Lint | `ruff check pi app` |
| Installer (all steps) | `powershell -ExecutionPolicy Bypass -File installer\build.ps1` → `dist\WeatherPi-Companion-Setup-<version>.exe` + `.sha256`. Fetches the vendor files, runs PyInstaller, checks the bundle's DLLs and the frozen `--selftest`, then Inno Setup. From a worktree, pass `-Python ..\..\weatherpi-v2\.venv\Scripts\python.exe` |
| App build only | `pyinstaller installer\weatherpi-companion.spec --distpath dist --workpath build` → `dist\WeatherPi Companion\` |
| Installer only | `iscc installer\weatherpi-companion.iss` (Inno Setup 6: `winget install JRSoftware.InnoSetup`; needs the app build first) |

## Image build

See [image-build.md](image-build.md). Sync the repo to the builder and run:
```bash
# from the PC (Git Bash)
BUILDER_HOST=user@builder tools/sync-to-builder.sh   # copies the repo into the container's /work/repo
ssh user@builder 'cd <builder dir> && docker compose up -d && docker exec weatherpi-builder /work/repo/image/build.sh'
```

## Third-party downloads (into `installer/vendor/`, git-ignored)

| File | Source | Purpose |
|---|---|---|
| `rpi-usb-gadget-driver-setup.exe` | github.com/raspberrypi/rpi-usb-gadget releases (pinned + sha256 in `installer/fetch-vendor.ps1`, which downloads it) | Windows RNDIS driver (D4); Apache-2.0, licence shipped in `installer/licences/` |
| *(Tailscale, not stored here)* | pkgs.tailscale.com/stable | The installer downloads the pinned MSI at install time (D25); version and sha256 are in `installer/weatherpi-companion.iss` |

## Pi addresses during development

- USB: `10.12.194.1` (the agent on `:8765`, SSH as `main` after the claim)
- Tailscale: shown by the app after setup
- `weatherpi.local` over WiFi via mDNS (dev convenience only; no inbound services on wlan0, per D23)
