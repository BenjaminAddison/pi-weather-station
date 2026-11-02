# Notes for AI agents

This file is for AI assistants and coding agents (Claude, ChatGPT, Copilot, Codex, ...) working with this repo or helping someone with a WeatherPi. People: see [README.md](README.md).

## Helping an owner whose WeatherPi has a problem

Ask them for a **help pack** from the WeatherPi Companion app. Don't ask for passwords.

1. In the app, click **Need help?** (bottom left during setup) or **Get help** next to a failed check.
2. Optionally type what's going wrong, click **Create help text**, then **Copy to clipboard** (or **Save as file...** for a `.md` file).
3. Paste it (or attach the file) into the chat with you.

The pack is Markdown. It starts with instructions written for you (the source is [`app/weatherpi_companion/resources/ai_base.md`](app/weatherpi_companion/resources/ai_base.md)): what the station is, how it fits together, read-only commands, what's safe and what isn't, and known causes of trouble. After that it has live information from the owner's computer and WeatherPi: the failed checks, `/v1/info`, `/v1/diagnostics` (services, logs, network, Tailscale, storage) and the app's log. Passwords, tokens and keys are replaced with `[REDACTED]` before the owner sees it; that is expected.

Follow the pack's "How to help" section: plain language, one step at a time, ask for command output instead of guessing, and never ask for secrets.

## Working on the code

Read in this order: [README.md](README.md), then [docs/architecture.md](docs/architecture.md) (every design decision is numbered D1, D2, ...), then the doc for the part you're changing. The docs are the source of truth: change them first when a decision changes, then the code. [docs/pi-agent-api.md](docs/pi-agent-api.md) is the contract between the app and the Pi; both sides are built against it.

| Part | Where |
|---|---|
| Pi software (logger, dashboard, agent API, systemd, firstboot) | `pi/` |
| SD card image build (privileged Docker container) | `image/` |
| WeatherPi Companion (Windows, PySide6) | `app/weatherpi_companion/` |
| Installer and release workflow | `installer/`, `.github/workflows/release.yml` |

### The help parts

| What | Where |
|---|---|
| Builds the help pack and redacts secrets | `app/weatherpi_companion/aihelp.py` (`build_help_pack`, `redact`) |
| The instructions at the top of every pack | `app/weatherpi_companion/resources/ai_base.md`: **keep it accurate** when behaviour, paths, services or commands change, because it's what an AI reads first |
| The help window (build, edit, copy, save) | `app/weatherpi_companion/ui/widgets/helpdialog.py`; opened from `ui/widgets/wizard.py` (`WizardPage.help`) |
| What the Pi reports about itself | `GET /v1/diagnostics` in `pi/weatherpi/agent/system.py` (redacted on the Pi too) |
| Offline diagnosis on the home screen | `diagnose_offline()` in `app/weatherpi_companion/station.py` |

When you add a secret anywhere (a new token, key or password field), add its name to `aihelp.SENSITIVE_KEYS` or a pattern in `aihelp.py`, and add a test in `app/tests/test_aihelp.py`.

### Tests and checks

```powershell
ruff check pi app
$env:WEATHERPI_FAKE=1; pytest pi\tests     # Pi software against in-memory fakes
pytest app\tests                           # app (Qt tests run offscreen)
```

There's no pretend Pi to click through (D27): automated tests use in-memory fakes, and manual testing is on a real Pi over the USB cable (`start-app.bat`).

### Conventions

- Python 3.12+; `pi/` must run on the Pi's Python 3.13 with apt packages only (D10).
- `ruff` with line length 100. Plain-language messages for the owner; technical detail goes in `detail`.
- Never commit secrets, real WiFi details or personal data; use example values (`ExampleWiFi`, `example.ts.net`).
