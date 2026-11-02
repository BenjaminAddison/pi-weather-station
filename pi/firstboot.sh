#!/bin/bash
# WeatherPi first-boot setup (weatherpi-firstboot.service, root, once).
# Idempotent: every step checks before acting, so re-running is harmless.
# The image build already does most of this offline; this is the on-hardware safety net.
# Output goes to the journal: journalctl -u weatherpi-firstboot
set -euo pipefail

DATA_DIR=/var/lib/weatherpi
DONE_FILE="$DATA_DIR/firstboot.done"
GADGET_CONF=/etc/modules-load.d/usb-gadget.conf
# read-only root (D14, docs/pi-software.md): the data partition and what's bound from it
DATA_MNT=/data
BIND_TARGETS=(/var/lib/weatherpi /var/backups/weatherpi /var/lib/tailscale
    /etc/NetworkManager/system-connections /var/log/journal /var/lib/systemd/timesync
    /home/main/.ssh)
CMDLINE=/boot/firmware/cmdline.txt
# recurse=0: only / gets the RAM layer; overlayroot's default would cover /data too
OVERLAY_ARG="overlayroot=tmpfs:recurse=0"

log() { echo "firstboot: $*"; }

# 1. USB gadget (D2). The image writes usb-gadget.conf offline; if it's missing,
#    let the stock tool do it on real hardware. Takes effect after a reboot.
need_reboot=0
if [ ! -e "$GADGET_CONF" ]; then
    if command -v rpi-usb-gadget >/dev/null 2>&1; then
        log "USB gadget not configured, running rpi-usb-gadget on -f"
        if rpi-usb-gadget on -f; then
            need_reboot=1
        else
            log "WARNING: rpi-usb-gadget on -f failed (exit $?)"
        fi
    else
        log "WARNING: $GADGET_CONF missing and rpi-usb-gadget not installed"
    fi
else
    log "USB gadget already configured"
fi

# 2. WiFi: Raspberry Pi OS soft-blocks WiFi until a country is set. The image sets
#    cfg80211.ieee80211_regdom on cmdline.txt; unblock so wlan0 is usable now
#    (systemd-rfkill keeps the state across reboots).
if command -v rfkill >/dev/null 2>&1; then
    rfkill unblock wifi && log "rfkill: wifi unblocked" || log "WARNING: rfkill unblock wifi failed"
else
    log "rfkill not installed, skipping wifi unblock"
fi

# 3. SSH host keys (the image ships without them so every Pi gets its own).
if ! compgen -G "/etc/ssh/ssh_host_*_key" >/dev/null; then
    log "generating SSH host keys"
    ssh-keygen -A
else
    log "SSH host keys present"
fi

# 4. Data dirs (docs/pi-software.md): db owned by weatherpi, agent state root-only.
#    The logger creates the database file itself on first start.
install -d -m 0755 -o root -g root "$DATA_DIR"
if getent passwd weatherpi >/dev/null 2>&1; then
    install -d -m 0750 -o weatherpi -g weatherpi "$DATA_DIR/db"
    # Fix ownership if the DB was e.g. restored by root.
    find "$DATA_DIR/db" -maxdepth 1 -name 'weatherpi.db*' ! -user weatherpi \
        -exec chown weatherpi:weatherpi {} + || true
else
    log "WARNING: user weatherpi missing; creating db dir owned by root"
    install -d -m 0750 "$DATA_DIR/db"
fi
install -d -m 0700 -o root -g root "$DATA_DIR/agent"
install -d -m 0750 -o root -g root /var/backups/weatherpi

# 5. Data partition (D14): the image ships it small; grow it to fill the SD card.
if mountpoint -q "$DATA_MNT"; then
    data_dev="$(findmnt -n -o SOURCE "$DATA_MNT")"
    data_disk="/dev/$(lsblk -no pkname "$data_dev")"
    data_num="$(cat "/sys/class/block/$(basename "$data_dev")/partition")"
    if echo ", +" | sfdisk --no-reread --force -N "$data_num" "$data_disk" >/dev/null 2>&1; then
        partx -u -n "$data_num" "$data_disk" || log "WARNING: partx couldn't update the kernel's view"
        resize2fs "$data_dev" && log "grew $data_dev to fill the card" ||
            log "WARNING: resize2fs $data_dev failed"
    else
        log "WARNING: couldn't grow partition $data_num on $data_disk"
    fi
else
    log "no data partition at $DATA_MNT (image before W8?)"
fi

# 6. Read-only root (D14): switch the overlay on for the next boot, but only when /data and
#    every bind mount are really there; otherwise the data would land in RAM and be lost.
if [ -e /etc/overlayroot.conf ] && [ -f "$CMDLINE" ]; then
    missing=""
    mountpoint -q "$DATA_MNT" || missing="$DATA_MNT"
    for t in "${BIND_TARGETS[@]}"; do
        mountpoint -q "$t" || missing="$missing $t"
    done
    if [ -n "$missing" ]; then
        log "WARNING: not switching on the read-only root, not mounted:$missing"
    elif grep -q "overlayroot=" "$CMDLINE"; then
        log "read-only root already configured"
    else
        sed -i "1s|^|$OVERLAY_ARG |" "$CMDLINE"
        log "read-only root switched on (from the next boot)"
        need_reboot=1
    fi
fi

# 7. Done marker (the unit's ConditionPathExists=! skips us from now on).
touch "$DONE_FILE"
log "done"

if [ "$need_reboot" -eq 1 ]; then
    log "rebooting once so the USB gadget / read-only root take effect"
    systemctl --no-block reboot
fi
