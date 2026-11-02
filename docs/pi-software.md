# Pi software

Everything here lives in `pi/` in the repo. The image build installs it to the paths below (D12).

## Layout on the Pi

| Path | Owner | What |
|---|---|---|
| `/opt/weatherpi/` | root, read-only to services | Code: the `weatherpi` Python package (`config`, `db`, `logger`, `web`, `agent`) |
| `/etc/weatherpi/config.yaml` | root, 0644 | Settings (same keys as v1, with new paths). Read-only in normal use |
| `/var/lib/weatherpi/settings.yaml` | root, 0644 | Owner-changed settings (`PUT /v1/settings`, D26), merged **over** config.yaml by `config.load_config()`. Today only `record_interval_s` |
| `/data` | root | The writable data partition (p3, D14). Everything that must survive a reboot lives here; see "Read-only root" |
| `/var/lib/weatherpi/db/weatherpi.db` | weatherpi | SQLite (WAL) |
| `/var/lib/weatherpi/agent/` | root, 0700 | `claimed`, `token.sha256`, `recovery.hash`, `recover.lockout` |
| `/var/backups/weatherpi/` | root, 0750 | Nightly DB backups, 7-day retention |
| `/run/weatherpi/wifi-connect.lock` | root | Exists while the agent applies a WiFi change; the watchdog skips recovery meanwhile |
| `/etc/NetworkManager/system-connections/*.nmconnection` | root, 0600 (written by NM) | Saved WiFi networks (any number, agent 2.1.0), each with `connection.autoconnect-priority`; NM joins the best one in range. The USB gadget's two ethernet profiles live here too and are never listed as WiFi |
| `/etc/weatherpi/image-version` | root | e.g. `2026.10.19-1` |

User **`main`** (uid 1000) is the owner's login: sudo, password locked until the claim. System user **`weatherpi`** (no login) runs the logger and dashboard, in group `gpio`.

## Network roles (D23)

| Interface | What listens | Purpose |
|---|---|---|
| `usb0` (10.12.194.1) | agent :8765, SSH | **Setup only**: claim, recovery, first WiFi and Tailscale login |
| `tailscale0` | agent :8765, SSH, `tailscale serve` → dashboard | **Monitoring**: everything after setup |
| `wlan0` | nothing inbound (firewalled with nftables except established traffic) | Internet uplink only |
| `lo` | dashboard :8080 | The `tailscale serve` backend |

`POST /v1/wifi/connect` also works over Tailscale (for changing networks remotely), but the app warns that a wrong password will drop the Pi offline until it's set up again over USB.

## Python package `weatherpi`

```
pi/weatherpi/
├── __init__.py          __version__
├── config.py            load_config(), get_value(), CLI "python3 -m weatherpi.config <key>"
├── db.py                Database (WAL, synchronous=NORMAL, busy_timeout); tables below
├── logger.py            sensor loop (ported from v1 logger/weatherpi_logger.py)
├── web/                 FastAPI dashboard (ported from v1 web/app.py + templates)
└── agent/               FastAPI agent implementing docs/pi-agent-api.md
    ├── app.py           routes
    ├── auth.py          token, usb-only check, claim state
    ├── wifi.py          nmcli wrappers (scan, connect, saved networks + priorities)
    ├── tailscale.py     tailscale CLI wrappers (status --json, up, serve)
    ├── system.py        diagnostics, reboot, time, LED
    └── backup.py        online backup + restore
```

There are no third-party imports beyond `fastapi`, `uvicorn`, `pydantic`, `jinja2` (dashboard templates), `python-multipart` (restore upload; apt `python3-python-multipart` 0.0.20 — NOT `python3-multipart`, which is a different library), `gpiozero`, `yaml` and the standard library (all apt packages, D10). `pydantic-settings` from v1's requirements isn't used, so it's dropped.

**Hardware and OS abstraction:** hardware and system commands go through small adapters (`read_w1()`, `rain_button()`, `led()`, `run_nmcli()`, `run_tailscale()`). With `WEATHERPI_FAKE=1` they use in-memory fakes, so the logger, dashboard and agent are tested on a PC. These fakes are for automated tests only; manual testing is on the real Pi (D27).

### Database schema (unchanged from v1)
- `temperature_readings(id INTEGER PK, timestamp TEXT, temp_c REAL)`
- `rain_events(id INTEGER PK, timestamp TEXT)`
- `status(key TEXT PK, value TEXT)` holds `last_alive`

### config.yaml (v2 defaults)
```yaml
station: {name: "WeatherPi", location: "Garden"}
temp:    {gpio_pin: 9, poll_interval: 30, write_interval: 300}   # settings.yaml can override (D26)
rain:    {gpio_pin: 17, mm_per_tip: 0.2794, bounce_time: 0.5}
logger:  {heartbeat_interval: 300}
database: {path: "/var/lib/weatherpi/db/weatherpi.db"}
backup:  {backup_dir: "/var/backups/weatherpi", retention_days: 7}
web:     {host: "127.0.0.1", port: 8080, refresh_interval: 60, stale_threshold: 600, cache_ttl: 300}
agent:   {port: 8765, interfaces: ["usb0", "tailscale0"]}
tailscale: {serve_port: 8080}
wifi:    {ping_target: "1.1.1.1", check_interval: 300}
```

## systemd units (`pi/systemd/`)

| Unit | User | Notes |
|---|---|---|
| `weatherpi-logger.service` | weatherpi | `Restart=always`; `GPIOZERO_PIN_FACTORY=lgpio` |
| `weatherpi-web.service` | weatherpi | uvicorn on 127.0.0.1:8080 |
| `weatherpi-agent.service` | root | uvicorn on `0.0.0.0:8765`; nftables only lets in `usb0` and `tailscale0`, and the agent checks the arrival interface too (belt and braces) |
| `weatherpi-backup.service` + `.timer` | root | daily, `Persistent=true` |
| `weatherpi-watchdog.service` | root | v1's `wifi_watchdog.sh`, never reboots |
| `weatherpi-firstboot.service` | root | oneshot, skipped once `/var/lib/weatherpi/firstboot.done` exists: runs `rpi-usb-gadget on -f` only if the image's offline gadget setup is missing (then reboots once), `rfkill unblock wifi`, `ssh-keygen -A` if no host keys, creates the data dirs with the right owners (the logger creates the DB itself) |
| `nftables.service` | root | ruleset from `pi/nftables.conf` (see network roles) |
| `rpi-usb-gadget-ics.service` | root | from the stock package |
| `tailscaled.service` | root | from the Tailscale package |

## Read-only root (D14)

Implemented 2026-11-02 (W8) in `image/build.sh`, `pi/firstboot.sh`, `pi/scripts/weatherpi-commit-etc.sh` and the agent (`storage.py`). The mechanism is **P** until it's proven on the real Pi Zero.

**Partitions:** p1 boot (FAT32, 512 MB, unchanged), p2 root (ext4, fixed size: the built system plus about 700 MB headroom for updates in maintenance mode), **p3 data** (ext4, label `wpdata`, `noatime`, mounted at `/data`). p3 ships small and `weatherpi-firstboot` grows it to fill the card. Pi OS's own first-boot root resize is switched off (the build removes `resize` from `cmdline.txt`), because its initramfs script would try to grow p2 over p3. That also skips Pi OS's first-boot disk-ID randomisation (it only runs with `resize`), so every card keeps the base image's disk ID; nothing depends on it (`/data` is found by its label, `wpdata`).

**Overlay:** Raspberry Pi OS's overlay FS (the `overlayroot` package in the initramfs), switched on by **`overlayroot=tmpfs:recurse=0`** in `cmdline.txt`. `recurse=0` is essential: overlayroot's default also puts a RAM layer over every other fstab mount, so `/data` and its bind mounts would forget everything at reboot. (raspi-config writes plain `overlayroot=tmpfs`; the agent treats any `overlayroot=tmpfs...` as "on" but always writes the `recurse=0` form.) The real root is mounted read-only at `/media/root-ro`; all writes to `/` land in RAM and vanish at reboot. `/boot/firmware` stays writable (it's only written when maintenance mode is switched). The **first boot runs without the overlay**: `weatherpi-firstboot` does everything that sets the Pi's identity once (machine-id, SSH host keys, growing p3, creating the `/data` folders), then adds `overlayroot=tmpfs:recurse=0` and reboots. It only does that if `/data` and every bind mount below are really mounted; otherwise it logs a warning and the Pi stays writable.

**What lives on `/data`** (bind-mounted over the normal paths from `/etc/fstab`, after `/data` is mounted; all entries are `nofail`, so a missing `/data` still boots, and `/v1/diagnostics` shows `data_mounted: false`). The build moves these directories, with their owners and modes, onto p3 and leaves empty mount points in the root:

| `/data/...` | bound to | Why |
|---|---|---|
| `weatherpi/` | `/var/lib/weatherpi` | DB, agent state (claim, token hash, recovery hash), `settings.yaml`, `firstboot.done` |
| `backups/` | `/var/backups/weatherpi` | Nightly DB backups |
| `tailscale/` | `/var/lib/tailscale` | Tailscale's node key and login |
| `nm-connections/` | `/etc/NetworkManager/system-connections` | Saved WiFi networks (NM writes them itself). Firstboot copies the USB gadget profiles in |
| `journal/` | `/var/log/journal` | The system log survives reboots. Pi OS ships `Storage=volatile`; our `journald.conf.d/weatherpi.conf` sets `Storage=persistent`, `SystemMaxUse=16M` |
| `timesync/` | `/var/lib/systemd/timesync` | timesyncd's last-known time: the Pi has no RTC, so the clock starts from here |
| `main-ssh/` | `/home/main/.ssh` | The app's SSH key (`authorized_keys`) |

**Rare `/etc` changes** (the owner's password in `/etc/shadow`, the sshd password drop-in, `/etc/localtime` and `/etc/timezone` from the claim) can't be bind-mounted, because the tools replace those files by renaming them. The agent writes them as now, then runs **`weatherpi-commit-etc <path>...`**: it remounts `/media/root-ro` read-write, copies those files across, `sync`s, and remounts it read-only. This only happens at claim, recover, password change and timezone change. With the overlay off it does nothing.

**Maintenance mode** (`POST /v1/system/maintenance`): removes `overlayroot=tmpfs` from `cmdline.txt` (or adds it back) and reboots. While it's on, the root is an ordinary writable filesystem for `apt upgrade`, redeploying `/opt/weatherpi`, or debugging. `/v1/diagnostics` and the app's home screen show the state, and the home screen reminds the owner while it's on.

**RAM:** the overlay's upper layer is a tmpfs on a 512 MB Pi. Writes outside `/data` are small (`/tmp`, `/run`, apt lists), so this is fine in normal use; anything big is done in maintenance mode.

**Tests:** `pi/tests/system/` checks the fstab entries, the firstboot steps (with fakes) and `weatherpi-commit-etc` against a temp dir. On hardware (M6): `findmnt /` shows `overlay`; a reboot keeps the DB, WiFi, Tailscale and password; and a power pull while logging leaves a working Pi.

## Dropped from v1
`usb-gadget.sh`, `create-wifi-img.sh`, `weatherpi-wifi-watch`, `wifi.img`, `usb-template/`, `serve.sh` (the agent handles serve), `sync-time.sh` (the agent's `/system/time` plus timesyncd), `tailscale-setup.sh` (the app does it), and the venv.

## Boot config added by the image (`/boot/firmware/config.txt`, under `[all]`)
```
dtoverlay=w1-gpio,gpiopin=9
dtoverlay=dwc2,dr_mode=peripheral     # same line rpi-usb-gadget writes
```
