# WeatherPi Companion (desktop app)

An installed desktop app for the station's owner. It does two jobs:

1. **Setup wizard** (USB). Runs once per Pi and takes it from freshly flashed to online on Tailscale.
2. **Home screen** (Tailscale). Ongoing monitoring, backups, AI help, and maintenance (D23).

Stack: Python 3.12 + PySide6 (Qt Widgets), PyInstaller onedir, Inno Setup installer (D5). Windows comes first; the code stays portable for Linux (D18).

## App structure (decided 2026-10-28)

**One app, one window, one installer.** The owner sees a single flowing app. During development, each part is a separate **section** that can be built, run and tested on its own, and the app shell joins them up; nothing has to be merged later.

| Section (id) | What it does | Wizard pages |
|---|---|---|
| **Flash SD card** (`flasher`) | Writes the WeatherPi image to an SD card and verifies it (see TODO below) | new page before 1 |
| **Check the Pi** (`tester`) | Software check after flashing: this computer (driver, Tailscale installed), plug in, USB detection (including `boot_rom` = card unreadable), `/v1/info` answers, `claimed`, image version, agent major version. Ends with "Your WeatherPi is working and ready for setup" or a plain-words fix. Can be re-run any time | 1–3 |
| **Set up** (`setup`) | Password, WiFi (**one or more networks**), Tailscale on this PC and on the Pi, final checks over the tailnet, done | 4–9 |
| **Home** (`home`) | Monitoring over Tailscale, backups, AI help, maintenance | home screen |

- **Start-up routing:** no station profile → Welcome with two choices, "Flash a new SD card" (`flasher`) or "My card is already flashed" (`tester`) → `setup` → `home`. A profile with `setup_complete` → `home`. A profile without it → `tester` (it resumes setup as described in the W5 notes).
- **Dependencies** (RNDIS driver, Tailscale) are installed by the **installer**, which already runs as admin. The app only checks for them (in `tester` and on the home screen) and offers the bundled installer if one has gone missing.
- **Dev switch:** `python -m weatherpi_companion --only <section-id>` opens a window with just that section. Combine with `WEATHERPI_COMPANION_HOME` (throwaway app data). It always talks to the real Pi (D27).

### Section contract (`ui/`)

- `ui/app.py`: the shell (`QMainWindow`: a step list on the left showing where the owner is, the current section on the right) plus the start-up routing and `--only`.
- `ui/context.py`: `AppContext`, shared by all sections: `home: Path` (app data folder), `env: HostEnv`, `session: SetupSession | None` (shared by `tester` and `setup`), and `go(section_id: str)`.
- `ui/sections/<id>.py`: one module per section. Each exposes a `QWidget` subclass with class attributes `SECTION_ID` and `TITLE`, a constructor `(ctx: AppContext, parent: QWidget | None = None)`, a signal `finished = Signal(str)` (the next section id, or `""` to let the shell decide) and an optional `on_enter()`.
- `ui/widgets/`: shared pieces, mainly the **check list** (a row per `CheckResult`: status icon, plain-words message, `Action` button, "Get help").
- **No blocking calls on the UI thread.** All `flows`/`host` calls run in a worker (`QThreadPool`), and cancellable waits pass a `threading.Event`.

## Code layout (`app/`)

```
app/
├── pyproject.toml
├── weatherpi_companion/
│   ├── __main__.py         entry point
│   ├── agent_client.py     typed client for docs/pi-agent-api.md (httpx); no Qt imports
│   ├── host/               host-side checks, one module per platform
│   │   ├── driver.py       RNDIS driver present? install it (Windows: pnputil/driver setup exe)
│   │   ├── usb.py          is the Pi on USB? (adapter with 10.12.194.x, ping .1)
│   │   └── tailscale.py    host Tailscale installed/running/logged in, tailnet name, ping peer
│   ├── secrets.py          app data dir, keypair, token, recovery password (DPAPI on Windows)
│   ├── backups.py          download, list and restore backups
│   ├── aihelp.py           build the AI help pack (see below)
│   ├── ui/                 Qt: wizard pages, home screen, dialogs
│   └── resources/          icons, AI help base text
└── tests/                  pytest with an in-memory fake agent (no Qt needed for core tests)
```
Rule: everything outside `ui/` stays Qt-free and unit-tested. `ui/` stays thin.

## Setup wizard

Each page runs its **checks** and only enables **Next** once they all pass. A failed check always shows what to do in plain words, plus a "Get help" button (AI pack).

| # | Page | What happens | Checks |
|---|---|---|---|
| 1 | Welcome | Explains what you'll need: the Pi, a **data** micro-USB cable (it also powers the Pi from this PC; nothing goes in PWR), your WiFi password, about 15 minutes | — |
| 2 | This computer | Host prerequisites | ✓ RNDIS driver installed (offers to install the bundled one; needs admin) · ✓ Tailscale installed on this PC (offers the install) |
| 3 | Plug in | "Plug the cable into the port marked **USB** (not PWR) and into this PC; that also powers the Pi." Shows a picture (one cable; PWR crossed out in red, "Nothing in PWR"). Warns: power from both ports at once can destroy the Pi (the owner lost one that way) | ✓ USB adapter has a 10.12.194.x address · ✓ `GET /v1/info` answers · ✓ `claimed == false` (if it's already claimed and this PC has its keys, skip to step 6; otherwise offer recovery) |
| 4 | Create your password | The owner types a password twice (strength meter, at least 8 characters). The app generates the SSH key, API token and recovery password, then calls `/v1/claim` and pushes the PC's clock (`/v1/system/time`) | ✓ claim 200 · ✓ the token works (`/v1/wifi/status` 200) · ✓ **SSH key login works** over USB |
| 5 | WiFi | Lists `/v1/wifi/scan` results (signal bars, lock icon, "known"), plus an "Other / hidden network" option. Password field, then Connect | ✓ `state == connected` · ✓ `internet == true` (within 60 s). On failure: wrong password, or not found, so move the Pi closer. At least one network must have connected before Next |
| 5b | More WiFi (optional, D28) | **"Will the WeatherPi live somewhere else?"** The saved networks list (`GET /v1/wifi/saved`) with a prominent **Add a WiFi network…** button: add networks that aren't in range now (a garden extender, a holiday home, a phone hotspot) with `POST /v1/wifi/saved`, reorder them (priority), remove them. They can't be checked until the Pi is near them. New details for the network in use go back to page 5 and reconnect. Next says **Skip** until a second network is saved | — (optional) |
| 6 | Tailscale on this PC | Starts and logs in to Tailscale on the host if needed (opens Tailscale's login page) | ✓ host `BackendState == Running` · show which account/tailnet it's on |
| 7 | Tailscale on the WeatherPi | `/v1/tailscale/up`, then opens `auth_url` in the browser: "sign in with the **same account** as on this PC" | ✓ Pi `Running` · ✓ **same tailnet** as the host · ✓ `/v1/tailscale/serve` ok (if `serve_not_enabled_on_tailnet`, show a button for `admin_url` and retry) |
| 8 | Final checks | Everything now runs over **Tailscale**, not USB | ✓ host can ping the Pi's tailnet IP · ✓ agent `/v1/info` over tailnet with `via == "tailscale"` · ✓ dashboard `serve_url` loads (HTTP 200) · ✓ sensors: logger alive; rain test "tip the bucket now" (optional, skippable); temperature shown or "sensor not connected" |
| 9 | Done | "Unplug the USB cable first, then plug the power supply into PWR. Never connect both at once." Shows the dashboard link (copy / open), the recovery file location, and offers a **first backup** | — |

The app saves the Pi's profile (hostname, serial, tailnet IP, DNS name, claimed at) next to its secrets, so the home screen knows which station to talk to. More than one station is possible (a list), but v2.0 supports one.

## Home screen (monitoring over Tailscale)

Once setup is done the window becomes the **home app** (D28): setup's step list is hidden and the home app has its own menu: **Overview** (status card, Open the dashboard), **WiFi networks**, **Measurements** and **Settings** (maintenance mode, **Set up again…**). Set up again asks first, then shows the Welcome page with the step list.

- **WiFi networks (D28):** the same saved-networks list as setup page 5b, over the home connection (usually Tailscale). Adding, reordering and removing spare networks works from anywhere; the network **in use** can't be removed or given new details from here (a mistake would take the Pi offline): the page says to plug in the USB cable and use Set up again.

- **Status card:** online/offline, last heartbeat, temperature, rain today (mm), WiFi signal, uptime, disk, image version. Refreshes every 30 s while it's open.
- **Dashboard tab:** the station's web dashboard (D24) embedded with `QWebEngineView`, loading `serve_url` over Tailscale. It's the same page any device on the tailnet sees.
- **Buttons:** Open dashboard in browser · Back up now · Restore… · Get AI help · Change WiFi · Change password · Reboot / Shut down · Advanced (open an SSH terminal, show the IPs).
- **If it's offline**, the app works out what's wrong (host Tailscale down? Pi not seen in the tailnet? last seen when?) and suggests the next step (power? WiFi changed? plug in USB to fix the WiFi).
- **Tray icon** (P): shows a badge when the station has been offline longer than `stale_threshold`.
- **Measurements card (D26):** "Save a temperature reading every [5 minutes ▾]" with the choices from `GET /v1/settings` (shown as 1, 5, 10, 15, 30, 60 minutes) and a Save button (`PUT /v1/settings`). Help text: more often gives more detail in the charts; less often keeps the database smaller; rain is always counted tip by tip. If the agent answers `404`, the card says the WeatherPi's software is too old and suggests re-flashing with the latest image.
- **Maintenance mode (D14), under Settings:** a switch with a plain explanation ("Lets you update or repair the WeatherPi. It restarts. Switch it back off when you're done: it makes the SD card wear faster and a power cut more risky"). While it's on, the status card shows a yellow reminder.

## Secrets and app data

`%LOCALAPPDATA%\WeatherPi\` on Windows (Program Files isn't writable by the user), `~/.local/share/weatherpi/` on Linux:

```
stations/<serial>/
  profile.json            hostname, serial, tailnet ip/dns, claimed_at
  id_ed25519 (+.pub)      SSH key (private key DPAPI-wrapped on Windows → id_ed25519.dpapi)
  api_token.dpapi
  RECOVERY.txt            recovery password + plain-language instructions (readable on purpose, D6)
backups/                  default backup folder (changeable; suggest Documents\WeatherPi Backups)
logs/companion.log
```
The owner's password is never stored. `RECOVERY.txt` is visible to the owner on purpose ("keep this safe; it lets you reset your WeatherPi password").

## Backups

- **Back up now:** `GET /v1/backup` saves to the backups folder as `weatherpi-backup-<host>-<stamp>.tar.gz`. The app then verifies it (opens the tarball, checks the manifest, and runs SQLite `PRAGMA integrity_check` on the DB).
- **Manual only** (decided 2026-10-19). Reminder: if the last backup is more than 7 days old, the home screen nags.
- **Restore:** pick a file, see the manifest summary, confirm, `POST /v1/restore`.

## AI help pack

"Get AI help" builds a single text block (copy to clipboard, or save as `weatherpi-ai-help-<stamp>.md`) that the owner pastes into any AI chat:

1. **Base instructions** (static, from `resources/ai_base.md`): what the station is, the hardware and pin map, the architecture, the services, the commands that matter, the safe and unsafe actions, and "ask the owner to run commands and paste the output back; never ask for passwords".
2. **Live diagnostics:** `GET /v1/diagnostics` (over Tailscale or USB, whichever works), plus the **host-side** checks (driver, USB adapter, host Tailscale state, last errors from `companion.log`).
3. **What the owner says is wrong** (a text box in the dialog).

Everything is passed through a redaction step (passwords, tokens, PSKs, the recovery password, authkeys) before it's shown. The owner can see and edit the text before copying it.

## Installer (Windows, Inno Setup), `installer/`

- Installs the app to `Program Files\WeatherPi Companion`, with a Start-menu shortcut and a desktop shortcut (a task, ticked by default).
- **Bundled:** `rpi-usb-gadget-driver-setup.exe`, copied to `<app>\drivers\` (where the app looks for it) and run silently if the driver isn't in the driver store.
- **Tailscale** (D25): if it isn't installed, a task (ticked by default) downloads the pinned MSI from tailscale.com, checks its sha256 and installs it silently. If the download or install fails, setup carries on and says the app will help later.
- The installer runs as admin, so the driver install happens here and the app itself never needs admin (if the app does find the driver missing later, it re-launches the bundled setup with UAC). "Open WeatherPi Companion now" at the end starts the app as the normal user, not elevated.
- Uninstall removes the app and the flasher's cached SD images (they can be downloaded again). It then asks "Also delete your WeatherPi keys, recovery password and backups?" (default **No**; a silent uninstall keeps them).
- Unsigned for now (D20), so expect a SmartScreen "More info → Run anyway" prompt.

## Linux (later, D18)

The same code runs on Linux. No driver is needed (RNDIS/ECM is in the kernel), host Tailscale uses `tailscale` CLI, and secrets are stored with file permissions 0600. Packaged as an AppImage or `.deb` (open).

## Facts for host-side detection

- With `rpi-usb-gadget`, the Pi enumerates as **USB `VID_2E8A&PID_0013`**, "Raspberry Pi USB Gadget", manufacturer "Raspberry Pi Ltd." (from the image's `/usr/lib/modprobe.d/g_ether.conf`). It's an RNDIS+ECM `g_ether` device; Windows uses RNDIS and needs the bundled driver.
- The Pi is `10.12.194.1/28` and serves DHCP to the host (the host gets `10.12.194.2–14`). If the host shares its internet over the link (Windows ICS), the Pi's `ics-watch` flips it to a DHCP client. Detection should therefore look for **any** adapter in `10.12.194.0/28` and then probe `10.12.194.1:8765/v1/info`, falling back to the gateway address if ICS is active.
- **ICS fallback:** with Windows ICS the PC itself is the gateway (usually `192.168.137.1`), so detection also probes the gadget adapter's **ARP neighbours** to find the Pi.
- **Driver detection:** the bundled driver installs **`raspberrypi-rndis.inf`** (provider "Raspberry Pi Ltd.", class Net). Don't confuse it with `rpiboot-winusb.inf`, a different Raspberry Pi package. Detection requires the Net class GUID.
- **The Pi stuck in boot ROM:** if Windows shows `VID_0A5C&PID_2763` ("BCM2708 Boot"), the Pi couldn't read its SD card (what happened to v1). `host/usb.py` reports this as `boot_rom`, and the UI should say "the SD card isn't readable: re-seat it or re-flash it".

## Implementation notes (app core, W5, merged 2026-10-19)

- The code lives in `app/weatherpi_companion/`: `agent_client`, `host/{runner,usb,driver,tailscale}`, `secrets`, `backups`, `aihelp` (+ `resources/ai_base.md`), `checks`, `flows` (wizard logic: `SetupSession`), `station` (home screen: `StationMonitor`). The UI (W6) calls these from worker threads; expected failures come back as `CheckResult`s with an `Action` the UI shows as a button.
- **Secrets on disk:** besides `RECOVERY.txt` there's an encrypted copy, `recovery.dpapi`. `profile.json` also holds `ssh_host_key` (trusted on first use), `setup_complete`, `last_backup_at`, `serve_url` and the versions. On Linux the secrets are files with mode 0600.
- **Wizard page 3 resume:** an already-claimed Pi whose keys are on this PC resumes at page 5 if WiFi or internet isn't up, otherwise page 6.
- **Host Tailscale:** `tailscale status` exits 1 when not Running but still prints JSON (it's parsed anyway). Ping uses `--c 1 --until-direct=false` (a relayed reply counts).
- **Host Tailscale stuck at `NoState`** (fixed 2026-11-01): on Windows the tray app (`tailscale-ipn.exe`) starts the backend. If it isn't running (e.g. Tailscale was switched off in the Windows startup apps), `NoState` / "Tailscale is starting" never ends. `check_host_tailscale` then returns `host_tailscale_not_started` (action `open_tailscale_app`, found with `tasklist`), and page 6 starts the tray app once by itself (button "Start Tailscale").
- **Slow Pi vs. lost cable** (fixed 2026-11-01): a request that *times out* over USB means the Pi is connected but busy, so the check says so and offers Try again; only a failed connection says "check the cable". `POST /v1/tailscale/serve` gets a 60 s client timeout, because the agent can take 15 s (`status`) + 20 s (`serve`, which blocks while Serve is off on the tailnet) on a slow Pi Zero CLI. On setup pages 4, 5 and 7, "Check again" after a lost connection re-checks on the page instead of jumping to the (finished) Check the Pi step; on page 7 a publish failure only re-publishes.
- **Publish timeout, then ask** (2026-11-02): if `POST /v1/tailscale/serve` still times out, the app asks the Pi for `GET /v1/tailscale/status` (fast) instead of guessing. `serve_enabled` means it finished: success. `serve_error == serve_not_enabled_on_tailnet` shows Open settings with `admin_url`. The real Pi hit this: Serve was off on the tailnet, `tailscale serve` waited 20 s for approval, and the app's 10 s timeout reported a lost cable.
- **Dev:** set `WEATHERPI_COMPANION_HOME` to use a throwaway app-data folder.
- **New dependency:** `paramiko` (SSH key-login check).

## TODO

Added 2026-10-28. The flasher is merged; the installer and the release workflow are built (W7, 2026-11-01).

### Flash the SD card from the app

A **Flash SD card** screen, so the owner doesn't need Raspberry Pi Imager. It's reachable from the wizard's Welcome page ("Start with a fresh SD card") and from the home screen's Advanced menu.

- **It only writes the image to the SD card.** It writes the WeatherPi `.img.xz` byte for byte and does no OS customisation (no WiFi, user or hostname). That's the same rule as flashing with Imager (see image-build.md), because customisation would override first boot.
- **Card picker:** lists **removable** drives only (SD card readers and USB card readers), with size, model and drive letter. It never lists the system disk, fixed/internal disks, or anything larger than 256 GB. If no card is found, it says "Insert the SD card into this PC's card reader" and keeps checking.
- **Confirm:** "Everything on <model, size, letters> will be erased." The owner has to tick a box before **Flash** is enabled.
- **Write:** dismount the card's volumes, stream-decompress the `.img.xz`, write it to the raw device (`\\.\PhysicalDriveN` on Windows, `/dev/sdX` on Linux), then **read it back and verify** against the image's sha256. Show progress and time left. Writing to a raw disk needs admin, so this step runs in a small elevated helper (UAC prompt); the rest of the app stays non-admin.
- **Image source:** download the latest image from the GitHub release (check its `.sha256`, cache it under app data) or pick a local `.img.xz`. The image is about 600 MB, so it's downloaded rather than bundled in the installer.
- **Done:** the helper **ejects the card** after a good flash (decided 2026-10-28; it also stops Windows offering to "format" the Linux partition), and the page says it's safe to take out. If the eject didn't work, it says "Safely remove the card" instead. Then the wizard goes on to page 2.
- **Code:** platform logic goes in `host/flash.py` (Qt-free, unit-tested with a fake disk list and a file standing in for the device); the screen is `ui/sections/flasher.py` (see the section contract).

### Auto-build releases on GitHub

A GitHub Actions workflow (`.github/workflows/release.yml`) so the owner just downloads **one installer `.exe` from the Releases page**.

- **Trigger:** pushing a version tag (`v*`), plus a manual run. Runner: `windows-latest`.
- **Steps:** run the `app/` tests, then PyInstaller (onedir), download the third-party installers into `installer/vendor/` (pinned versions, sha256-checked; they stay out of git), compile the Inno Setup script, then attach `WeatherPi-Companion-Setup-<version>.exe` and its `.sha256` to the GitHub Release.
- **The installer sets up everything the PC needs:** the Companion app, the **RNDIS driver** (`rpi-usb-gadget-driver-setup.exe`, silent) and **Tailscale** (silent if it isn't already installed). Recorded as D25 (downloaded at install time, not bundled).
- **The Pi image isn't built in CI.** It needs the privileged builder container (D17). `tools/publish-image.ps1 -Tag vX.Y.Z -Image <weatherpi-...img.xz> [-Publish]` checks it against its `.sha256` and attaches both to the release (GitHub CLI, `gh auth login`), so the flasher can find it.
- **Release page text:** `installer/release-notes.md` (owner-facing steps; `{VERSION}` is filled in by the workflow).
- **Making a release:** bump `__version__` in `app/weatherpi_companion/__init__.py`, commit, push a tag `vX.Y.Z` (the workflow tests, builds and creates a **draft** release with the installer), build the image (docs/image-build.md), then run `tools/publish-image.ps1 ... -Publish`. The repo must be **public**: owners and the app's flasher download without signing in.
- **Done (W7, 2026-11-01).** Because the flasher reads `releases/latest`, a tag whose release doesn't exist yet gets a **draft** release; attach the image, then publish. See the W7 notes below.

## UX decisions (2026-10-28)

- **New password for the network the Pi is on:** the app **reconnects** with it straight away (so a typo shows up now), instead of saving it quietly. A wrong password is safe: the Pi keeps the working profile until the new one connects. Other networks in "Saved networks" are just saved.
- **Eject after flashing:** yes (see the flasher TODO).
- **Skipped steps:** "My card is already flashed" shows the flasher step as **Skipped** (grey dash), not a green tick. Steps finished in an earlier session (the app reopened on a station mid-setup) still show as done.
- **"Update the app":** opens the latest GitHub release (`RELEASES_URL` in `weatherpi_companion/__init__.py`, shared with the flasher's image download).

## Implementation notes (W6, app shell + `tester` + `setup`, 2026-10-28)

- **Code:** `ui/app.py` (shell: step list, Welcome page, routing, `--only`, `run()`), `ui/context.py` (`AppContext`), `ui/worker.py` (`run_task`: `QThreadPool` + a `threading.Event` per task; results come back on the UI thread, and only while the owning widget is alive), `ui/style.py` (Fusion + light palette + one stylesheet; status icons are painted, so they're sharp at any scaling), `ui/desktop.py` (open URLs/files; tests replace `opener`), `ui/widgets/` (`checklist`, `busy`, `wizard`, `helpdialog`, `diagram`), `ui/sections/{__init__,tester,setup}.py`.
- **Additions to the section contract** (all optional, nothing existing changed): a section may have `on_leave()` (the shell calls it when switching away; wizards cancel their running waits) and `step_changed = Signal(str)` with a `step_caption` attribute (shown under the current step, e.g. "3 of 4: Plug in"). `AppContext` also has `ensure_session()`, `reset_session()`, `hints` (e.g. `setup_start`, the page number from the table above that `setup` should open on, set by `tester` after a claim/resume/recovery) and `navigator` (set by the shell; `go()` calls it).
- **Registry:** `ui/sections/__init__.py` imports `ui.sections.<id>` lazily and takes the `QWidget` class whose `SECTION_ID` matches. A missing module gets a placeholder ("SD card flashing is coming soon", "The home screen is coming soon"); a module that fails to import is logged and also gets the placeholder. Merging the flasher is just adding `ui/sections/flasher.py`.
- **Routing details:** `setup` opened without a found WeatherPi (e.g. `--only setup`, or from the step list) looks for one for 20 s, then starts on the right page or points to `tester`. A Pi in the boot ROM shows "can't read its SD card: re-seat it... or flash it again" with a button to `flasher`. "Plug in" keeps watching (restarting `wait_for_plug_in` every 180 s) until found or left.
- **Entry point:** `--only <id>` as specified (no sidebar; a section's `finished` only shows a status-bar note). `--flash-helper ...` as the **first** argument goes straight to `host.flash_helper.main(argv)` before argparse and before any Qt import (W6f's elevated helper). `--version` / `--selftest` unchanged; `--selftest` also imports the UI modules.
- **Driver installer:** looked up as `$WEATHERPI_DRIVER_EXE`, else `rpi-usb-gadget-driver-setup.exe` next to the app (or in `vendor/`, `drivers/`, `installer/vendor/`). W7: please put it in one of those places.
- **Saved networks:** the WiFi page's "Saved networks" tab calls `SetupSession.saved_networks()`, `save_network(ssid, password, hidden, priority)`, `set_network_priority(ssid, priority)` and `forget_network(ssid)` through `getattr`. Without them it says "needs a newer app core". It accepts a list of `SavedNetwork`, an object with `.networks`/`.checks`, or a `(checks, networks)` pair. Reordering rewrites the priorities as an evenly spaced list in 0..100; new networks go to the bottom.
- **Wording:** the UI rewrites three core messages: `usb_boot_rom` (the spec's re-seat/re-flash text plus the flasher button), `host_tailscale_running` (the account is already shown in a card) and `pi_tailscale_running` ("Tailscale is on on the WeatherPi" reads oddly).
- **App log:** `run()` logs to `logs/companion.log` (start-up, failed checks with their detail, unexpected errors), which the AI help pack includes. During setup "Get help" builds the pack from the page's checks, `/v1/info`, `/v1/diagnostics` once paired, and the log.
- **Dev switches:** removed 2026-11-02 (D27). `WEATHERPI_AGENT_URL`, `WEATHERPI_FAKE_HOST` and `ui/devfakes.py` were the pretend Pi and pretend PC; the app now always uses the real USB link, driver and Tailscale.
- **Tests:** `app/tests/test_ui_*.py` (pytest-qt, `QT_QPA_PLATFORM=offscreen`). `test_ui_fakes.py` holds the helpers (`make_ctx`, which also blocks the real `tailscale up` and driver installer, `SavedNetworksSession`, `wait_idle`). For screenshots offscreen, set `QT_QPA_FONTDIR=C:\Windows\Fonts`, or text renders as boxes.

## Implementation notes (W7, installer + release workflow, 2026-11-01)

- **Files** (all in `installer/`): `weatherpi-companion.spec` (PyInstaller, onedir, windowed; entry `launcher.py`), `weatherpi-companion.iss` (Inno Setup 6), `build.ps1` (the whole build), `fetch-vendor.ps1` (pinned, sha256-checked driver download into `vendor/`), `check_bundle.py`, `make_icon.py` → `assets/weatherpi.ico`, `licences/` (driver licence, `THIRD-PARTY.txt`). Workflow: `.github/workflows/release.yml`. The spec moved from the planned `app\weatherpi-companion.spec` to `installer\` so all of W7 lives in its own folder.
- **Version:** single source is `__version__` in `app/weatherpi_companion/__init__.py`. The spec stamps it into the exe's version info, Inno Setup reads it back from the exe, and the workflow fails if the tag (`v0.2.0`) doesn't match it. **Bump `__version__` before tagging.**
- **The spec lists every `weatherpi_companion` module** (sections are imported by name) plus `PySide6.QtWebEngineWidgets`, and refuses to build if the package was picked up from another checkout (the shared `.venv` has an editable install of `weatherpi-v2\app`).
- **Size:** Qt is trimmed in the spec (QML plugins, Quick 3D/Charts/Multimedia etc., translations other than English, WebEngine debug resources): 594 → 365 MB on disk, a ~114 MB installer. Most of what's left is Chromium (`Qt6WebEngineCore.dll`, 195 MB). `check_bundle.py` walks every DLL's imports and fails the build if trimming removed one that's still needed (it caught `Qt6QmlMeta` and `Qt6QmlWorkerScript`).
- **Checks in `build.ps1`:** DLL check, then the frozen `--selftest` (imports `QtWebEngineWidgets`, but doesn't open a web view). **When the home screen lands, extend `--selftest` to create a `QWebEngineView`**, so a missing WebEngine resource fails the build rather than the owner's Dashboard tab.
- **Driver detection in the installer** matches `host/driver.py`: a `raspberrypi-rndis.inf` block in `pnputil /enum-drivers` with the Net class GUID. If pnputil can't run, the driver setup runs anyway. The driver setup's `/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /SP-` still needs confirming on a clean VM (M5): no "Windows Security" driver prompt should appear.
- **Tailscale:** version and per-architecture sha256 (amd64/arm64) are `#define`s at the top of the `.iss`; update them from `https://pkgs.tailscale.com/stable/<msi>.sha256`. msiexec exit 3010/1641 counts as success and asks for a restart at the end.
- **Uninstall** works on `{localappdata}\WeatherPi` of the user running the uninstaller. If a standard user's uninstall is elevated with a *different* admin account, that admin's folder is the one checked (the owner's data stays; it's never wrongly deleted).
- **Not yet tested:** a real install/uninstall on a clean Windows VM (M5), including the driver-missing and Tailscale-missing paths (the dev PC has both). The first CI run happens on the first `v*` tag or a manual run.

## Implementation notes (W9, home screen first round, 2026-11-02)

- **Code:** `ui/sections/home.py` (`HomeSection`), backed by `station.StationMonitor` (new: `settings()`, `set_record_interval()`, `maintenance()`, `set_maintenance()`; the getters return `None` on a `404`, i.e. a Pi too old for them) and `agent_client` (`StationSettings`, `MaintenanceState`).
- **Which station:** the first profile with `setup_complete`, else the first one this PC holds keys for (so a setup that was skipped past still gets a home screen). With none, it offers "Check the Pi".
- **Cards:** Status (online/offline, temperature, rain today, last heard from, WiFi, link, versions; refreshes every 30 s while shown; offline shows `diagnose_offline` as a checklist), Measurements (D26), Maintenance mode (D14; asks for confirmation, then checks again 75 s after the Pi restarts; a yellow reminder in the status card while it's on).
- **Not yet:** backups, restore, AI help, change WiFi/password, reboot/shut down, the embedded dashboard tab and the tray icon (later rounds).

## Implementation notes (W10, home app + More WiFi page, 2026-11-02)

- **Shared saved-networks logic:** `flows.SavedNetworks(client, allow_forget_in_use)` holds what `SetupSession` used to do itself (`saved_networks`, `save_network`, `set_network_priority`, `forget_network`, `saved`, `saved_networks_problem`). `SetupSession` delegates to one over USB; `StationMonitor.wifi` is one over the home connection with `allow_forget_in_use=False` (refuses with `wifi_in_use_remote`).
- **`SavedNetworksPanel` has a host** instead of the WiFi page: `saved_backend()`, `run()`, `edit_in_use()`, `remote`, and optional `networks_changed()`. Setup's host is `MoreWifiPage`; the home app's is `WifiNetworksHost`.
- **Setup page numbers** (4..9, used by `tester` via `setup_start`) are an explicit map in `SetupSession.page_for`; the optional page 5b has no number, so nothing jumps to it. The step caption counts it ("3 of 7").
- **Shell:** `MainWindow.show_section` hides the step list for `home` and shows it again for everything else (incl. Welcome). `HomeSection` builds its own sidebar (`#Sidebar`, buttons with `role="nav"`).

