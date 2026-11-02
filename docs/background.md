# Background: what happened to v1

Recorded 2026-10-19.

## The failure

- **Symptom:** when the Pi was plugged into the PC, the USB instructions drive never appeared.
- **What Windows saw:** only `BCM2708 Boot` (USB `0A5C:2763`). That's the Pi's boot ROM fallback, which happens when it can't boot from the SD card. Up to 2026-10-06 the Pi had always shown up as the `WEATHERPI` composite gadget (`1D6B:0104`: NCM network plus a "Linux File-Stor Gadget" drive).
- **Root cause:** the SD card (Acer MSC100 32 GB, made by Biwin, U1/V10 entry-level) has a dead **DAT2** data line. A raw read on the PC showed every byte with bits `0x44` cleared. For example, the boot signature `55 AA` read as `11 AA`, and `mkfs.fat` read as `29 2B 22 33 2A 22 21 30`. Only 64 distinct byte values appeared, and there were no `0xFF` bytes anywhere in 16 MB. On SD's 4-bit bus, bits 6 and 2 both travel on DAT2. Reads were identical before and after cleaning the contacts, so it's a hardware fault, not corruption.
- **Data underneath was intact:** with the mask accounted for, the layout was a normal Pi OS image (FAT32 boot at LBA 16384, 512 MB, then ext4).
- **Likely contributors:** a budget card in an always-on Linux role, possibly ESD or physical stress. Logs also show **unclean power cuts**: there are blocks of NUL bytes in `weatherpi-wifi-watch.log` (103) and `.bash_history` (498), which is the signature of power loss mid-write. The card was last removed on 2026-10-06 without a shutdown.

## v1's backups

The v1 code and its data backups are kept privately, outside this repo; v2 doesn't import them.

**Not in any backup:** `/boot/firmware/config.txt`, `cmdline.txt`, NetworkManager connection files, and Tailscale state. v1's `config.txt` needed `dtoverlay=w1-gpio,gpiopin=9` and `dtoverlay=dwc2,dr_mode=peripheral`.

**v1 data recovery (separate task):** merge `db/weatherpi.db` with its `-wal` and `-shm` files using sqlite3, then keep the result privately. Per D11 it isn't shipped in the image.

## Hardware facts (carried into v2)

| Item | Value |
|---|---|
| Board | Raspberry Pi Zero W Rev 1.1, ARMv6 (BCM2835), 512 MB, 2.4 GHz WiFi |
| Ports | `PWR` micro-USB (power only); `USB` micro-USB (data/OTG, used for the gadget) |
| Temperature | DS18B20 1-Wire on **BCM GPIO 9 / physical pin 21**, `dtoverlay=w1-gpio,gpiopin=9`, read from `/sys/bus/w1/devices/28-*/w1_slave` |
| Rain | Misol tipping bucket (reed switch) on **BCM GPIO 17 / physical pin 11**, pull-up, **0.2794 mm per tip** |
| LED | Green ACT LED (`/sys/class/leds/ACT`) |

The temperature sensor wasn't detected on 2026-08-18 (no `28-*` device). It's out of scope for now; the software must handle a missing sensor gracefully.

## v1 lessons learned

1. Use an endurance-rated SD card, and design for unclean power loss (see D14).
2. Don't put plaintext passwords in docs or on the USB drive. v1 had the SSH and sudo password in `handoff.md`, in the USB Tailscale guides, and in bash history.
3. The NCM gadget needed a driver on Windows 10. The stock `rpi-usb-gadget` (RNDIS/ECM `g_ether`) has an official Windows driver installer, which v2 bundles.
4. There is no `pi` user. Never reference `User=pi` or `/home/pi`.
5. The DHCP address changes, so don't rely on it. Use USB (`10.12.194.1`) or Tailscale.
6. The watchdog must never reboot the Pi. Recording has to continue offline.
