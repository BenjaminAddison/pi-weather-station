# Workstreams (for parallel subagents)

Each workstream owns **one top-level folder** and talks to the others only through the docs, mainly [pi-agent-api.md](pi-agent-api.md). An agent must not edit another workstream's folder. If the contract has to change, it proposes the change in `docs/` and flags it in its final report.

| ID | Workstream | Owns | Depends on | Can start |
|---|---|---|---|---|
| W0 | Foundation (done by the coordinator): `paths.py`, `config.py`, `db.py`, `hw.py` (sensor + LED adapters, real and fake), `pi/config.yaml`, `pi/conftest.py` | those files (any change → coordinator) | — | done |
| W1 | Pi core: logger, dashboard (port from v1, D24), tests for config/db/logger/web | `pi/weatherpi/logger.py`, `pi/weatherpi/web/`, `pi/tests/` (except `pi/tests/agent/`) | W0 | done |
| W2 | Pi agent: implements the API, plus fakes and the mock agent | `pi/weatherpi/agent/`, `pi/tests/agent/`, `tools/mock-agent/` | the API doc; W1's `config`/`db` interfaces (stub them if they're not ready) | done |
| W3 | Pi system integration: systemd units, nftables, firstboot, install.sh | `pi/systemd/`, `pi/nftables.conf`, `pi/firstboot.sh`, `pi/scripts/`, `pi/etc/`, `pi/install.sh`, `pi/tests/system/` | pi-software.md | done |
| W4 | Image build | `image/` | W1–W3 for the content; the script skeleton can start now with a placeholder `pi/` | now |
| W5 | App core: agent client, host checks, secrets, backups, AI pack | `app/weatherpi_companion/` except `ui/`, `app/tests/` | the API doc; mock agent from W2 (or its own stub server) | done |
| W6 | App UI: shell, `tester` and `setup` sections now; `home` in a later round (see "App structure" in companion-app.md) | `app/weatherpi_companion/ui/` except `ui/sections/flasher.py`; `__main__.py` (the `--only` switch) | W5 interfaces; W2b client methods for the saved-networks list | in progress (2026-10-28) |
| W6f | Flasher section: SD card detection, write + verify, elevated helper, image download | `app/weatherpi_companion/host/flash*.py`, `app/weatherpi_companion/ui/sections/flasher.py`, `app/tests/test_flash*.py` | the section contract in companion-app.md | in progress (2026-10-28) |
| W2b | Several saved WiFi networks (agent 2.1.0): Pi endpoints, fakes, mock agent, app client methods | `pi/weatherpi/agent/`, `pi/tests/agent/`, `tools/mock-agent/`, `app/weatherpi_companion/agent_client.py` (+ its tests) | pi-agent-api.md "Several saved networks" | in progress (2026-10-28) |
| W8 | Read-only root (D14): data partition, overlay, firstboot, `weatherpi-commit-etc`, maintenance mode; settings endpoint (D26) | `image/`, `pi/` (agent: maintenance + settings endpoints; system: fstab, firstboot, scripts; `config.py` merge of settings.yaml) | pi-software.md "Read-only root", pi-agent-api.md agent 2.2.0 | built 2026-11-02, awaiting hardware test |
| W9 | App home screen, first round: status card, measurements card (D26), maintenance switch (D14) | `app/weatherpi_companion/ui/sections/home.py`, `agent_client.py` settings + maintenance methods (+ tests) | W8's API; tested on the real Pi | built 2026-11-02 (first round), awaiting hardware test |
| W10 | Home app with its own menu (Overview, WiFi networks, Measurements, Settings + Set up again) and the optional More WiFi setup page (D28) | `app/weatherpi_companion/ui/` (home, setup, app shell, style), `flows.SavedNetworks`, `station.py` | W9; agent 2.1.0 saved-networks endpoints | built 2026-11-02, awaiting hardware test |
| W7 | Installer: PyInstaller spec, Inno Setup, bundled driver, Tailscale (downloaded, D25), licence check, release workflow | `installer/`, `.github/workflows/` | W6 producing a PyInstaller build | built (2026-11-01); awaiting a clean-VM test (M5) |

Suggested first wave: **W1, W2, W3, W5** in parallel (they're independent), then W4 (needs W1–W3), then W6 and W7.

## Milestones

| M | Done when |
|---|---|
| M0 | Docs reviewed, repo and toolchains ready, builder container works |
| M1 | Built image boots on real hardware: RNDIS adapter on Windows, `/v1/info` → `claimed:false` (image `2026.10.19-1` built + statically verified; **awaiting hardware test**) |
| M2 | App wizard pages 1–5 work against the real Pi (claim, password, SSH key, WiFi) |
| M3 | Pages 6–8 work: Tailscale on both devices, final checks over the tailnet |
| M4 | Home screen: monitoring over Tailscale, backup and restore, AI help pack |
| M5 | Installer: clean Windows VM → install → full wizard with no manual steps |
| M6 | Acceptance: second SD card, factory-fresh flow, power-pull test (unplug while running, then boot and check the DB is intact) |

## Conventions (all agents)

- **Python 3.12+ syntax**, but `pi/` must run on the Pi's **Python 3.13** using only apt-available libraries (D10). `app/` may use pip dependencies (PySide6, httpx, cryptography, pywin32 or `ctypes` for DPAPI).
- **Tests:** `pytest`. `pi/` tests run with `WEATHERPI_FAKE=1` on Windows and Linux. Network and system calls go through adapters, so tests never shell out to `nmcli` or `tailscale`.
- **Style:** `ruff` (line length 100), type hints on public functions, docstrings only where they add something.
- **Never** hard-code secrets, real WiFi details or personal data. Use example values (`ExampleWiFi`, `example.ts.net`).
- **Paths:** use the constants in `pi/weatherpi/paths.py` (W1 creates it) rather than literal `/var/lib/...` strings scattered around.
- **Commit** small and often on a workstream branch (`w2-agent`, etc.); `main` is merged by the coordinating session.
- **Final report:** what was built, how to run its tests, any deviations from the docs, and open questions.

## Prompt template for a workstream agent

> You are working on WeatherPi v2, workstream **W\<n\>: \<name\>**. The repo is `<path to this repo>`. First read `README.md`, `docs/architecture.md`, `docs/workstreams.md` and \<the relevant docs\>. v1 reference code is in `<path to the v1 archive>` (read-only). Only edit files under \<owned folders\>. Follow the conventions in workstreams.md. Done means \<acceptance criteria\>. Finish with the final report described in workstreams.md.
