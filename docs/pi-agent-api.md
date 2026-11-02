# Pi agent API (v1): the contract

The Companion app (client) and `weatherpi-agent` (server) are both built against this document. **Change this file first**, then both sides. Breaking changes bump the path prefix (`/v2/...`).

- **Base URLs:** `http://10.12.194.1:8765` over USB (**setup**), `http://<tailscale-ip or MagicDNS name>:8765` over the tailnet (**monitoring**, the normal mode, D23).
- **Transport:** plain HTTP. USB is a point-to-point link and the tailnet is encrypted by WireGuard.
- **Format:** JSON, UTF-8. Times are ISO 8601 UTC (`2026-10-19T11:05:00Z`).
- **Auth:** `Authorization: Bearer <token>` on every endpoint marked 🔒. The server compares `sha256(token)` against `/var/lib/weatherpi/agent/token.sha256` in constant time.
- **Interface restrictions:** endpoints marked **USB-only** return `403 {"error":"usb_only"}` unless the request arrived on `usb0` (check the local address = `10.12.194.1`).
- **Errors:** non-2xx responses have the body `{"error": "<code>", "message": "<human text>"}`. Codes are listed for each endpoint, plus generic ones: `unauthorized` (401), `usb_only` (403), `not_claimed` (409), `busy` (409), `internal` (500).
- **Long operations** (`wifi/connect`, `tailscale/up`) return quickly. The client then polls the matching status endpoint.

## Info and claim

### `GET /v1/info` (open)
Lets the app identify the Pi and its state. Contains no secrets.
```json
{
  "agent_version": "2.1.0",
  "image_version": "2026.10.19-1",
  "hostname": "weatherpi",
  "model": "Raspberry Pi Zero W Rev 1.1",
  "serial": "00000000abcd1234",
  "claimed": false,
  "via": "usb",                      // "usb" | "tailscale"
  "time": "2026-10-19T11:05:00Z",
  "clock_synced": false
}
```

### `POST /v1/claim` (USB-only, unclaimed only)
```json
{
  "password": "owner-chosen",          // ≥ 8 chars; becomes main's login + sudo password
  "recovery_password": "app-generated",// ≥ 20 chars
  "ssh_pubkey": "ssh-ed25519 AAAA... weatherpi-companion@HOSTNAME",
  "api_token": "base64url 32 bytes",
  "owner_name": "optional display name",
  "timezone": "Africa/Johannesburg"    // IANA; optional, default UTC
}
```
The server sets the password (`chpasswd`), writes `~main/.ssh/authorized_keys` (0600), stores `sha256(api_token)` and a salted `hashlib.scrypt` hash of the recovery password (n=2**14, r=8, p=1), turns on SSH password auth (D8: writes `/etc/ssh/sshd_config.d/10-weatherpi-password.conf` = `PasswordAuthentication yes`, 0644 root, then `systemctl reload ssh`. sshd is first-match-wins, so the `10-` file overrides install.sh's `40-weatherpi.conf` default of `no`), sets the timezone, and marks the Pi claimed (`/var/lib/weatherpi/agent/claimed`).
→ `200 {"claimed": true}` · `409 already_claimed` · `400 weak_password` · `400 bad_request`

### `POST /v1/recover` (USB-only, claimed)
```json
{ "recovery_password": "...", "new_password": "...", "new_ssh_pubkey": "optional", "new_api_token": "optional" }
```
Resets the owner password and optionally re-keys the app (for example on a new PC that has the recovery file).
→ `200` · `403 bad_recovery` (rate-limited: 5 tries, then a 10 minute lockout)

### `POST /v1/password` 🔒
`{"new_password": "..."}` → `200` · `400 weak_password`

## WiFi

### `GET /v1/wifi/scan` 🔒
Rescans the air (about 5 s), then returns:
```json
{ "networks": [ {"ssid": "ExampleWiFi", "signal": 72, "security": "WPA2", "known": true, "in_use": false} ] }
```
Sorted by signal and de-duplicated by SSID. Hidden networks aren't listed.

### `POST /v1/wifi/connect` 🔒
`{"ssid": "...", "password": "...", "hidden": false}` → `202 {"job": "wifi-connect"}`
Replaces any existing profile with the same SSID, then brings it up. Progress is reported by `GET /v1/wifi/status`. The ACT LED blinks 3× on success and 1 long on failure (D22).

### `GET /v1/wifi/status` 🔒
```json
{
  "state": "connected",          // "disconnected" | "connecting" | "connected" | "failed"
  "ssid": "ExampleWiFi",
  "ip": "192.168.1.50",
  "signal": 70,
  "internet": true,              // ping 1.1.1.1 ok
  "last_error": null,            // e.g. "wrong_password", "not_found", "timeout"
  "saved": ["ExampleWiFi"]
}
```

### `DELETE /v1/wifi/saved/{ssid}` 🔒 → `200` · `404`

### Several saved networks (agent 2.1.0, added 2026-10-28)

The Pi keeps any number of saved networks and joins the best one in range on its own (NetworkManager autoconnect, highest `priority` first). These endpoints manage that list **without** touching the current connection.

#### `GET /v1/wifi/saved` 🔒
```json
{ "networks": [ {"ssid": "ExampleWiFi", "priority": 10, "hidden": false, "in_use": true}, {"ssid": "Phone hotspot", "priority": 0, "hidden": false, "in_use": false} ] }
```
Sorted by priority (highest first), then by SSID. Only WeatherPi-managed WiFi profiles are listed (not the USB gadget profiles).

#### `POST /v1/wifi/saved` 🔒
`{"ssid": "...", "password": "...", "hidden": false, "priority": 0}` → `201 {"ssid": "..."}`
Saves the network (autoconnect on, with that priority) but **doesn't connect to it**, so it works for networks that aren't in range. It replaces a saved profile with the same SSID. The password can't be checked until the Pi is in range. `password` may be `null` or missing for an open network. `priority` is an integer from −100 to 100 (default 0). Errors: `400 bad_request` (bad PSK or priority), `409 busy` (a connect is running).

#### `PATCH /v1/wifi/saved/{ssid}` 🔒
`{"priority": 5}` → `200` · `404`. Changes only the priority.

`POST /v1/wifi/connect` also accepts an optional `"priority"` (same range). If it's left out, an existing profile keeps its priority and a new one gets 0.

## Tailscale

### `GET /v1/tailscale/status` 🔒
```json
{
  "installed": true,
  "version": "1.94.2",
  "backend_state": "NeedsLogin",  // "NoState" | "NeedsLogin" | "Starting" | "Running" | "Stopped"
  "auth_url": null,               // set while NeedsLogin and an up attempt is pending
  "tailnet": "example.ts.net",    // MagicDNS suffix, when Running
  "tailnet_name": "owner@example.com",
  "ip": "100.94.2.31",
  "dns_name": "weatherpi.example.ts.net",
  "serve_enabled": true,
  "serve_url": "https://weatherpi.example.ts.net",
  "serve_error": null             // "serve_not_enabled_on_tailnet" + admin_url when the tailnet blocks Serve
}
```

### `POST /v1/tailscale/up` 🔒
`{"hostname": "weatherpi"}` → `202`. Starts `tailscale up --hostname=... --ssh=false` with no wait. The agent captures the AuthURL, and the client polls status until `auth_url` appears, opens it in the browser, then keeps polling until `Running`.
Errors: `409 no_internet`.

### `POST /v1/tailscale/serve` 🔒
Runs `tailscale serve --bg http://127.0.0.1:8080`. → `200 {status}` · `409 serve_not_enabled_on_tailnet {"admin_url": "https://login.tailscale.com/f/serve?node=..."}`

### `POST /v1/tailscale/logout` 🔒 → `200`

## Station data

### `GET /v1/sensors/live` 🔒
```json
{
  "temperature": {"present": false, "celsius": null, "sensor_id": null, "read_at": null},
  "rain": {"tips_today": 3, "mm_today": 0.84, "last_tip_at": "...", "mm_per_tip": 0.2794},
  "logger": {"alive": true, "last_heartbeat": "...", "stale_seconds": 12}
}
```
For the "tip the bucket" test, the client polls this every second and watches `last_tip_at` change.

### `GET /v1/diagnostics` 🔒
A text-heavy bundle for the AI help pack. **Already redacted:** no passwords, tokens or PSKs.
```json
{
  "collected_at": "...",
  "system": {"uptime_s": 1234, "kernel": "...", "image_version": "...", "disk": {...}, "root_disk": {...}, "mem": {...}, "temp_c": 41.2, "throttled": "0x0", "undervoltage_seen": false},
  "services": [{"name": "weatherpi-logger", "active": "active", "enabled": true, "restarts": 0}],
  "journal": {"weatherpi-logger": "last 80 lines", "weatherpi-agent": "...", "weatherpi-web": "...", "NetworkManager": "...", "tailscaled": "..."},
  "network": {"interfaces": "...", "routes": "...", "wifi": {...}, "tailscale": {...}},
  "hardware": {"w1_devices": [], "gpio17_level": 1, "config_txt_overlays": ["w1-gpio,gpiopin=9", "dwc2,dr_mode=peripheral"]},
  "config": { "...config.yaml contents..." },
  "storage": {"overlay_supported": true, "data_mounted": true, "overlay_active": true, "overlay_on_next_boot": true, "maintenance": false}
}
```
`disk` is the partition holding the readings (the data partition, `/data`, when it's mounted), `root_disk` the system partition, and `ram_layer` (only while the read-only root is on) the overlay's RAM layer, i.e. changes to `/` since the last boot (fixed 2026-11-02: `disk` used to measure `/`, which is that RAM layer). `storage` was added in agent 2.2.0 (D14); without the overlay feature it's only `{"overlay_supported": false, "data_mounted": ...}`.

## Backups

### `GET /v1/backup` 🔒
Streams `application/gzip`: `weatherpi-backup-<hostname>-<UTCstamp>.tar.gz` containing:
- `weatherpi.db` (made with the SQLite online backup API, so it's consistent and not a live-file copy)
- `config.yaml`
- `manifest.json`: `{"format": 1, "created_at", "hostname", "image_version", "agent_version", "rows": {"temperature_readings": n, "rain_events": n}}`

WiFi credentials and secrets are **not** included.

### `POST /v1/restore` 🔒
`multipart/form-data` with a `backup` file. The server validates the manifest, stops the logger, swaps in the DB (keeping the old one as `weatherpi.db.pre-restore`), and restarts the logger. → `200 {"rows": {...}}` · `400 bad_backup`

## System

### `POST /v1/system/reboot` 🔒 → `202`
### `POST /v1/system/shutdown` 🔒 → `202` (the app tells the owner to wait for the LED to stop before unplugging)
### `POST /v1/system/time` 🔒
`{"utc": "2026-10-19T11:05:00Z"}`. The app pushes the PC's clock when the Pi hasn't synced via NTP yet (it has no RTC). → `200`

### Maintenance mode (agent 2.2.0, D14, added 2026-11-02)

The root filesystem is read-only in normal use (changes go to RAM and are lost at reboot). Maintenance mode turns that off so someone can update or debug the Pi; it stays off until it's switched back on.

#### `GET /v1/system/maintenance` 🔒
```json
{ "overlay_active": true, "overlay_on_next_boot": true, "maintenance": false }
```
`overlay_active`: the root is read-only right now. `overlay_on_next_boot`: what the boot config says. `maintenance` is `not overlay_on_next_boot`. A Pi without the overlay feature (images before W8) answers `404`.

#### `POST /v1/system/maintenance` 🔒
`{"enabled": true}` → `202` and the Pi reboots about 2 s later with a writable root. `{"enabled": false}` → `202`, and it reboots read-only again. Asking for the state it's already in → `200` with the GET body, no reboot. Works over USB and Tailscale. Errors: `409 busy` (a WiFi connect, Tailscale login or restore is running).

## Settings (agent 2.2.0, D26, added 2026-11-02)

Owner-changeable settings. They live in `/var/lib/weatherpi/settings.yaml` (data partition) and override `/etc/weatherpi/config.yaml`.

### `GET /v1/settings` 🔒
```json
{ "record_interval_s": 300, "record_interval_choices": [60, 300, 600, 900, 1800, 3600] }
```

### `PUT /v1/settings` 🔒
`{"record_interval_s": 600}` → `200` with the GET body. Saves it, sets `temp.write_interval` to it and `temp.poll_interval` to `min(30, it)`, and restarts `weatherpi-logger` (the readings averaged so far in the current interval are dropped, so at most one interval is lost). Only values from `record_interval_choices` are accepted; anything else, or an unknown key → `400 bad_request`. Older agents (2.0, 2.1) answer `404`; the app then says the WeatherPi's software is too old for this setting.

## Versioning

`agent_version` follows semver. The app reads it from `/v1/info` and warns if the major version doesn't match the one it was built for.

## Implementation notes (agent 2.0.0, W2, merged 2026-10-19)

These are part of the contract. Where they differ from the sections above, these win.

- **Interfaces:** a request arriving on neither USB nor Tailscale gets `403 forbidden_interface`. Requests on usb0's current address (including ICS mode, where the Pi is a DHCP client and not at `10.12.194.1`) count as USB.
- **Auth order:** a 🔒 endpoint on an **unclaimed** Pi returns `409 not_claimed` (not 401).
- **`/v1/recover`:** a wrong password → `403 bad_recovery` with `attempts_left`, `locked`, `retry_after_s`. During the lockout even the right password is refused, and the lockout survives restarts. `new_ssh_pubkey` replaces only the previous **app** key; keys added by hand are kept.
- **Passwords:** owner password 8–128 characters, no line breaks (otherwise `400 weak_password`). WiFi PSK 8–63 characters or 64 hex digits (otherwise `400 bad_request`).
- **Extra errors:** `tailscale/serve` before login → `409 tailscale_not_running`; a bad hostname in `tailscale/up` → `400 bad_request`; scan/connect while a connect is running → `409 busy`; backup/restore while one is running → `409 busy`.
- **Extra response fields:**
  - `tailscale/status`: `last_error`, `up` (the up job's state), `admin_url` (while Serve is blocked)
  - `claim`: `warnings` (non-fatal failures such as the timezone or `ssh_reload_failed`)
  - `restore`: `manifest`
  - `system/time`: `{"applied": bool, "clock_synced": bool}`; nothing is applied if NTP has already synced
  - `wifi/status.last_error`: can also be `radio_blocked` or `failed`
  - `wifi/scan` `security`: `open`, `WPA2`, `WPA2/WPA3` or `enterprise`
- **WiFi connect safety:** the new profile is created under a temporary name and only replaces older profiles once it connects, so a wrong password never deletes a network that was working. While connecting, `/run/weatherpi/wifi-connect.lock` exists and the watchdog leaves WiFi alone.
- **Commands:** `tailscale up --reset --hostname=X --ssh=false --timeout=600s`, and `tailscale serve --bg --yes http://127.0.0.1:<serve_port>`.
- **Diagnostics redaction** also hides email addresses (`[email]`) and Tailscale login URLs. The rain pin key is `gpio<rain.gpio_pin>_level`.
- **Restore** restores the DB only, not `config.yaml`, and chowns it to `weatherpi`.
- **Tests:** the agent runs in-process against `FakeOS` (`WEATHERPI_FAKE=1`); its fake WiFi password is `correct-horse`. The standalone mock agent was removed 2026-11-02 (D27).

## Implementation notes (agent 2.1.0, W2b, 2026-10-28)

Several saved networks. These are part of the contract too, and they win over the "Several saved networks" section where the two differ. The endpoints and shapes are unchanged; the notes below fill in details.

- **Responses:** `POST /v1/wifi/saved` → `201 {"ssid"}`. `PATCH /v1/wifi/saved/{ssid}` → `200 {"ssid", "priority"}`. `DELETE` still answers `200 {"deleted": ssid}`.
- **Busy:** `PATCH` and `DELETE /v1/wifi/saved/{ssid}` also return `409 busy` while a connect is running, the same as `POST`. Edits and the start of a connect are serialised, so an edit never interleaves with a connect. `GET /v1/wifi/saved` always works, and it hides the connect's temporary `weatherpi-new-*` profile.
- **Validation:** `priority` must be a JSON integer from −100 to 100. Strings, floats, booleans and (for `PATCH`) `null` or a missing value all give `400 bad_request`; this applies to `wifi/connect`'s optional `priority` too. Password rules are the same as for connect (8–63 characters or 64 hex digits, no line breaks). An empty string counts as an open network, the same as `null`.
- **What's listed:** every NetworkManager profile of type `802-11-wireless` except the temporary one. The USB gadget profiles are ethernet, so they never appear, and `PATCH`/`DELETE` on their names give `404`. WiFi profiles made by hand over SSH are listed too. Several profiles for one SSID are merged into one entry (highest priority; `hidden`/`in_use` if any of them is). `in_use` is NM's `ACTIVE` flag, which is also true while the profile is activating.
- **SSIDs in the path:** `PATCH` and `DELETE` accept an SSID containing `/` when it's percent-encoded as `%2F` (the route is `{ssid:path}`). Clients should always percent-encode the SSID.
- **Saving the network that's in use** updates its active profile in place (`nmcli connection modify`). That doesn't re-activate it, so the current link stays up and the new password only applies the next time the Pi joins. Saving it with no password removes its security setting. Other profiles for that SSID are deleted.
- **Saving any other network** adds a new profile (`con-name` = SSID, `connection.autoconnect yes`, `connection.autoconnect-priority P`) and then deletes the older profiles for that SSID. If the add fails (`500 internal`), the old profiles are kept.
- **Security of a saved network:** WPA2-PSK (`wpa-psk`) unless NM's last scan (no rescan) saw the network as WPA3-only, in which case it's `sae`. When the in-use profile is updated, its existing `wpa-psk`/`sae` is kept. If the last scan saw the network as open, a password is ignored, as connect does. A **WPA3-only network that's out of range** is saved as WPA2-PSK and won't join, so join those with `wifi/connect` while in range.
- **Autoconnect behaviour (NetworkManager):** a save never runs `connection up`, creates no lock file and doesn't blink the LED. If the Pi has **no** WiFi connection when a network that's in range is saved, NM may join it straight away; that's ordinary autoconnect. NM never leaves a working connection for a higher-priority one. Priority only decides which network it picks when it (re)joins. After several failed attempts (e.g. a wrong saved password), NM pauses autoconnect for that profile for a few minutes.
- **`wifi/connect` + `priority`:** the temporary profile is created with `connection.autoconnect yes` and the priority: the given one, else the highest priority of the existing profiles for that SSID, else 0. If the connect fails, the old profile and its priority are untouched.
- **Profile files** are NetworkManager keyfiles in `/etc/NetworkManager/system-connections/`, written by NM as root 0600. PSKs are stored in the profile (`psk-flags` 0), as for connect.
- **nmcli used:** `nmcli -t -f UUID,TYPE,AUTOCONNECT-PRIORITY,ACTIVE,NAME connection show`; `nmcli -t -m multiline -f 802-11-wireless.ssid,802-11-wireless.hidden connection show uuid U` (also `802-11-wireless-security.key-mgmt` for the in-use profile); `connection add/modify/delete` by UUID.
- **Fake NM (tests):** `FakeOS.set_in_range(ssid, in_range)` moves a network in or out of range and imitates NM autoconnect by priority. `Example Phone` starts out of range.
