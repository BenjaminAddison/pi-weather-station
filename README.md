# WeatherPi

A small garden weather station (a Raspberry Pi Zero W with a rain gauge and a temperature sensor), packaged so it can be handed to someone who isn't technical:

- **A ready-to-flash SD card image.** Flash it, plug the Pi into a computer, and it works. Nothing has to be typed on the Pi.
- **WeatherPi Companion**, an installed Windows desktop app. It talks to the Pi over one USB cable and walks the owner through setup (password, WiFi, Tailscale on both devices). Afterwards it shows the station's status, lets the owner change how often readings are saved, makes backups and builds an "ask an AI for help" pack.
- **A web dashboard** on the Pi (current temperature, rain today, charts, CSV export), reachable from any of the owner's devices over [Tailscale](https://tailscale.com).

## About this project

This was built as a **commissioned project**: a weather station made for a client, designed to be set up and looked after by its owner without help. It's shared publicly as a worked example of a Raspberry Pi appliance with a companion desktop app.

Because it was made for one commission, some choices fit that client and may not fit you:

- The hardware is fixed: a Pi Zero W, a DS18B20 temperature sensor on GPIO 9 and a Misol tipping-bucket rain gauge on GPIO 17 (see [docs/background.md](docs/background.md)).
- The image's default WiFi country is `ZA`; build with `--wifi-country XX` for elsewhere ([docs/image-build.md](docs/image-build.md)).
- Remote access assumes the owner has a (free) Tailscale account.
- The Companion app is Windows-only for now, and unsigned, so Windows SmartScreen warns about it.

It isn't a supported product. You're welcome to use and adapt it under the [MIT licence](LICENSE), but issues and pull requests may not get an answer.

## Using it

1. Download `WeatherPi-Companion-Setup-<version>.exe` from the [Releases](../../releases) page and run it. It installs the app, the Pi's USB network driver and Tailscale.
2. In the app, flash an SD card with the WeatherPi image (the app downloads it from the same release), put the card in the Pi, and plug the Pi's **USB** port into the computer with a data cable. That cable also powers it: **don't** plug anything into PWR at the same time.
3. Follow the setup steps. When they're done, unplug the USB cable, then power the Pi from its PWR port.

## Getting help with an AI

- **Owner:** in WeatherPi Companion, click **Need help?** (or **Get help** next to a failed check), then **Create help text** and **Copy to clipboard**. Paste it into an AI chat (ChatGPT, Claude, Gemini, ...) and describe what you see. The text tells the AI about the station and includes its current diagnostics, with passwords and keys removed. Never paste a password into a chat.
- **Developers and coding agents:** point the agent at this repo; [AGENTS.md](AGENTS.md) tells it where things are, including how the help pack is built.

## Building from source

- **Pi software and the app:** Python 3.12+ on Windows, see [docs/dev-environment.md](docs/dev-environment.md). Tests: `pytest pi\tests` (with `WEATHERPI_FAKE=1`) and `pytest app\tests`.
- **SD card image:** built in a privileged Debian (trixie) Docker container on any Linux machine, see [docs/image-build.md](docs/image-build.md).
- **Installer:** `installer\build.ps1` (PyInstaller + Inno Setup). Pushing a `v*` tag builds it on GitHub Actions and attaches it to a draft release; the SD card image is uploaded to the same release by hand.

## Docs

The docs are the design record: each decision is numbered (D1, D2, ...) in [docs/architecture.md](docs/architecture.md).

| Doc | What it covers |
|---|---|
| [docs/background.md](docs/background.md) | Why v2 exists (what went wrong with v1), the hardware, and lessons learned |
| [docs/architecture.md](docs/architecture.md) | Components, how they talk to each other, the security model, and every design decision |
| [docs/pi-agent-api.md](docs/pi-agent-api.md) | **The contract** between the app and the Pi |
| [docs/pi-software.md](docs/pi-software.md) | What runs on the Pi: services, paths, users, config, the read-only root |
| [docs/image-build.md](docs/image-build.md) | How the SD card image is built |
| [docs/companion-app.md](docs/companion-app.md) | The desktop app: screens, host checks, backups, AI help pack, installer |
| [docs/workstreams.md](docs/workstreams.md) | How the work was split up, plus milestones and conventions |
| [docs/dev-environment.md](docs/dev-environment.md) | Toolchains and how to run and test each part |

## Repo layout

```
├── docs/            design docs (start here)
├── pi/              software that runs on the Pi (logger, dashboard, agent, systemd units)
├── image/           SD card image build scripts (run in a privileged Docker container)
├── app/             WeatherPi Companion desktop app (Python + PySide6)
├── installer/       Windows installer (PyInstaller + Inno Setup)
├── tools/           dev helpers (sync the repo to the image builder)
└── .github/         release workflow
```

## Status

| Milestone | State |
|---|---|
| M0: docs and dev environment | done |
| M1: image boots, USB gadget works, Pi claimable | done |
| M2: app connects, claims, sets up WiFi | done |
| M3: Tailscale on both devices | done |
| M4: home screen | first round: status, measurement interval, maintenance mode |
| M5: Windows installer | built; clean-machine test pending |
| M6: acceptance test on real hardware (incl. the read-only root) | in progress |
