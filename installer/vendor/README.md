Downloaded third-party installers go here. They're git-ignored, except this file. `installer\fetch-vendor.ps1` downloads them and checks the sha256 (`installer\build.ps1` runs it). See docs/dev-environment.md.

| File | Source | sha256 | Licence | Install silently |
|---|---|---|---|---|
| `rpi-usb-gadget-driver-setup.exe` (v1.0.6, matches `rpi-usb-gadget 1.0.6` in the image) | https://github.com/raspberrypi/rpi-usb-gadget/releases/download/v1.0.6/rpi-usb-gadget-driver-setup.exe | `9edf7f819cb9afa9126cda22b08ee91dff119a4557f590bb4d993daa78132c1e` | Apache-2.0 (repo LICENSE.txt). Redistribution is allowed; the licence text ships in `installer\licences\` | Inno Setup 6.4 installer, signed by "Raspberry Pi Limited": `/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /SP-` (still to verify on a clean VM, M5) |

**Tailscale isn't stored here** (D25). We found no clear right to redistribute Tailscale's Windows client, so the WeatherPi installer downloads the pinned MSI from `https://pkgs.tailscale.com/stable/` at install time and checks its sha256 (version and hashes: top of `installer\weatherpi-companion.iss`). It's installed with `msiexec /i <msi> /qn /norestart`.
