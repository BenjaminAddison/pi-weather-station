# Image build

The build produces `weatherpi-<version>.img.xz` plus a `.sha256`. You flash it with Raspberry Pi Imager ("Use custom") **without** Imager's OS customisation (the image is already customised; Imager's settings would override the first-boot config).

## Where it runs

A Docker container, **`weatherpi-builder`**, on any Linux machine. It's privileged and runs as root (it attaches the image as loop devices and registers qemu with the host kernel, D17), so only start it when you're building. A `docker-compose.yml` like this works:

```yaml
services:
  builder:
    image: debian:trixie      # or build: . with image/Dockerfile
    container_name: weatherpi-builder
    privileged: true
    command: sleep infinity
    working_dir: /work
    environment:
      STACK_UID: "1000"      # finished images are chowned to this user/group
      STACK_GID: "1000"
    volumes:
      - ./work:/work
      - /dev:/dev
    restart: "no"
```

```bash
cd <builder dir>
docker compose up -d                                       # start
docker exec weatherpi-builder /work/repo/image/build.sh   # build
docker compose down                                        # stop
```

- `work/` (the bind mount) holds `base/` (the downloaded base image and its sha256), `repo/` (a synced copy of this repo), `out/` (finished images) and `cache/` (apt `.deb` cache, so rebuilds go faster).
- **P: add a Dockerfile** so the build tools are baked in. Right now the tools are apt-installed into a plain `debian:trixie` container and disappear on `docker compose down`. Tools: `xz-utils qemu-user-static binfmt-support parted fdisk e2fsprogs dosfstools mtools rsync file sqlite3 python3 zerofree curl ca-certificates`.
- **ARM emulation:** `/proc/sys/fs/binfmt_misc/qemu-arm` is registered with flags `OCF` → `/usr/bin/qemu-arm-static`. The `F` flag means the chroot doesn't need its own copy of qemu. This changes the host kernel until the host reboots, and `build.sh` re-registers it if it's missing.
- **ARMv6:** build commands run with `QEMU_CPU=arm1176`, so the emulated CPU matches the Pi Zero's. Without it qemu reports `armv7l`, and anything that picks code based on the CPU could install binaries the Pi Zero can't run.

## Base image (pinned in `image/base.env`)

```
BASE_URL=https://downloads.raspberrypi.com/raspios_lite_armhf/images/raspios_lite_armhf-2026-10-06/2026-10-06-raspios-trixie-armhf-lite.img.xz
BASE_SHA256=2c302771f34c67a0b7ae7aa2e0dcdd3eac660504be59193c011ba79d412e1632
```
Layout: p1 FAT32 at sector 16384 (512 MB, `/boot/firmware`), p2 ext4 at 1064960 (2.1 GB). Ships with `rpi-usb-gadget 1.0.6`, cloud-init 25.2, NetworkManager 1.52.1 (`nmcli --offline` works), Python 3.13.5, and a disabled user `pi` (uid 1000, nologin).

## Build steps (`image/build.sh`)

1. **Prepare:** verify the base sha256, then copy to `out/work.img` and grow it by about 1 GB (`truncate` + `parted resizepart 2` + `resize2fs`), leaving room for packages.
2. **Mount:** `losetup -P`, mount p2 on `/mnt/pi` and p1 on `/mnt/pi/boot/firmware`, then bind `/proc /sys /dev /dev/pts` and copy the container's `resolv.conf`.
3. **Packages** (chroot, `QEMU_CPU=arm1176`): `apt-get update`; install `python3-fastapi python3-uvicorn python3-jinja2 python3-python-multipart python3-gpiozero python3-lgpio python3-yaml sqlite3 nftables`; add Tailscale's apt repo (signed keyring) and install `tailscale`; `apt-get clean`.
4. **Users:** rename `pi` to `main` (`usermod -l main -d /home/main -m pi`, `groupmod -n main pi`), set the shell to bash, lock the password (`passwd -l main`), add `main` to `sudo`, remove `010_pi-nopasswd`, and mask `userconfig.service` (Pi OS's first-boot user wizard).
5. **Software:** run **`pi/install.sh`** inside the chroot (W3). It creates the `weatherpi` system user, installs code to `/opt/weatherpi`, config, units, scripts, the nftables ruleset and stub, and the sshd and journald drop-ins, creates the data dirs, and enables firstboot, logger, web, agent, watchdog, the backup timer and nftables. Then write `/etc/weatherpi/image-version`. **`/var/lib/weatherpi/firstboot.done` must not exist in the image.**
6. **Other services:** enable `tailscaled` (step 3), `ssh` (step 9) and `rpi-usb-gadget-ics` (step 7). Everything else is enabled by install.sh.
7. **USB gadget:** replicate `rpi-usb-gadget on` offline: write `g_ether` to `/etc/modules-load.d/usb-gadget.conf`, add the `dwc2,dr_mode=peripheral` overlay, the dnsmasq snippet, and the two NM profiles ("USB Gadget (client)" / "USB Gadget (shared)") via `nmcli --offline` into `/etc/NetworkManager/system-connections/` (0600), and enable `rpi-usb-gadget-ics.service`. `weatherpi-firstboot` also runs `rpi-usb-gadget on -f` on real hardware as a safety net.
8. **Boot config:** append `dtoverlay=w1-gpio,gpiopin=9` under `[all]`, and add `cfg80211.ieee80211_regdom=<CC>` to `cmdline.txt` (default `ZA`, `--wifi-country`), because Pi OS keeps WiFi rfkill-blocked until a country is set (firstboot also runs `rfkill unblock wifi`). (`resize` is removed from `cmdline.txt` for the read-only root; see step 10.)
9. **First-boot identity:** set the hostname to `weatherpi` (`/etc/hostname`, `/etc/hosts`) and enable SSH. Password login stays off via install.sh's `40-weatherpi.conf` until the agent writes `10-weatherpi-password.conf` at the claim (D8). Delete `/etc/ssh/ssh_host_*` (firstboot runs `ssh-keygen -A`), empty `/etc/machine-id`, and disable cloud-init (`/etc/cloud/cloud-init.disabled`) so it can't create users.
10. **Hardening (D14):** `noatime` on `/` in fstab, and disable `bluetooth` and `hciuart`. The journald size cap comes from install.sh.
    **Read-only root (D14, W8):** step 3 installs `overlayroot` and runs `update-initramfs -u -k all` (the package's trigger only rebuilds the newest kernel's, and the Pi Zero boots the v6 one). Step 8 removes `resize` from `cmdline.txt`. `step_data_layout` moves the persistent directories into a staging folder, leaves empty mount points and writes the `/data` and bind-mount lines to fstab. `overlayroot=` is **not** in `cmdline.txt` (firstboot adds it after the first boot). Static checks cover the initramfs (`lsinitramfs .../initramfs | grep overlayroot`), cmdline, fstab, staging and the persistent journal.
11. **Finish:** unmount, run `e2fsck -f`, `zerofree` p2, shrink p2 to its minimum plus about 700 MB of headroom (with W8 it **no longer grows** at first boot), create **p3** (`wpdata`, ext4, 64 MB, 4 MiB aligned; filled from the staging folder; firstboot grows it to fill the card), truncate the image, then `xz -T0 -6`, write the sha256 and `chown ${STACK_UID}:${STACK_GID}`.

The build should be **repeatable from scratch** and must not depend on state left by a previous run. `build.sh --keep-mounted` leaves the image mounted for inspection.

## Tests

- **In the build (static):** chroot checks that `python3 -c "import weatherpi.agent.app"` works, `systemctl is-enabled` reports each unit, `config.txt` has both overlays, and there are no SSH host keys or `pi` user left. Also run the `pi/` unit tests inside the chroot under emulation.
- **Real hardware (M6):** flash the second SD card, boot, and check that Windows shows the RNDIS adapter, `GET http://10.12.194.1:8765/v1/info` returns `claimed:false`, then run the full Companion wizard.
- Full-system QEMU boot (`raspi0`) isn't relied on: its USB gadget emulation isn't usable.

## Copying the image to the PC

`scp user@builder:<builder dir>/work/out/weatherpi-<v>.img.xz* .`, then check it with `sha256sum -c weatherpi-<v>.img.xz.sha256`.
