#!/bin/bash
# Build the WeatherPi SD card image (docs/image-build.md).
# Runs inside the 'weatherpi-builder' container (privileged, root; docs/image-build.md).
#
#   /work/repo/image/build.sh [--keep-mounted] [--version V] [--wifi-country CC] [--stop-after STEP]
#
# Inputs : /work/repo (this repo, synced by tools/sync-to-builder.sh), image/base.env
# Output : /work/out/weatherpi-<version>.img.xz (+ .sha256), owned by STACK_UID:STACK_GID
#          Partitions: p1 boot, p2 root (read-only after the first boot), p3 data (D14)
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

REPO="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
# shellcheck source=base.env
source "$REPO/image/base.env"

WORK="${WORK:-/work}"
BASE_DIR="$WORK/base"
OUT_DIR="$WORK/out"
APT_CACHE="$WORK/cache/apt"
MNT=/mnt/weatherpi-build
VERSION="$(date -u +%Y.%m.%d-%H%M)"
WIFI_COUNTRY="${WIFI_COUNTRY:-ZA}"
HOSTNAME_PI=weatherpi
GROW_MB=1024          # extra room for packages before the final shrink
HEADROOM_MB=700       # free space left in the shrunk root (it no longer grows: D14, p3 follows it)
DATA_MB=64            # the data partition as shipped; firstboot grows it to fill the card
DATA_LABEL=wpdata
DATA_STAGE=/.wp-data  # inside the image: what goes on the data partition, staged until packing
# Read-only root (D14): <name on /data>:<path it's bind-mounted over> (docs/pi-software.md)
DATA_BINDS=(
    weatherpi:/var/lib/weatherpi
    backups:/var/backups/weatherpi
    tailscale:/var/lib/tailscale
    nm-connections:/etc/NetworkManager/system-connections
    journal:/var/log/journal
    timesync:/var/lib/systemd/timesync
    main-ssh:/home/main/.ssh
)
KEEP_MOUNTED=0
STOP_AFTER=""

while [ $# -gt 0 ]; do
    case "$1" in
        --keep-mounted) KEEP_MOUNTED=1 ;;
        --version) VERSION="$2"; shift ;;
        --wifi-country) WIFI_COUNTRY="$2"; shift ;;
        --stop-after) STOP_AFTER="$2"; shift ;;   # dev: stop (left mounted) after a step
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
    shift
done

IMG="$OUT_DIR/weatherpi-$VERSION.img"
LOOP=""

log() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
die() { printf '\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

# Run a command inside the image as an emulated ARMv6 (Pi Zero) CPU.
in_pi() {
    QEMU_CPU=arm1176 chroot "$MNT" /usr/bin/env -i \
        PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
        HOME=/root LANG=C.UTF-8 DEBIAN_FRONTEND=noninteractive "$@"
}

ensure_tools() {
    local need=()
    for t in xz parted e2fsck resize2fs zerofree rsync curl losetup sfdisk; do
        command -v "$t" >/dev/null || need+=("$t")
    done
    [ -x /usr/bin/qemu-arm-static ] || need+=(qemu-arm-static)
    if [ ${#need[@]} -gt 0 ]; then
        log "Installing build tools (missing: ${need[*]})"
        apt-get update -qq
        apt-get install -y -qq xz-utils qemu-user-static parted fdisk e2fsprogs dosfstools \
            rsync zerofree curl ca-certificates file >/dev/null
    fi
}

ensure_binfmt() {
    [ -f /proc/sys/fs/binfmt_misc/register ] || mount -t binfmt_misc binfmt_misc /proc/sys/fs/binfmt_misc
    if [ ! -e /proc/sys/fs/binfmt_misc/qemu-arm ]; then
        log "Registering qemu-arm binfmt handler (host kernel, until reboot)"
        printf '%s' ':qemu-arm:M::\x7fELF\x01\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x02\x00\x28\x00:\xff\xff\xff\xff\xff\xff\xff\x00\xff\xff\xff\xff\xff\xff\xff\xff\xfe\xff\xff\xff:/usr/bin/qemu-arm-static:FC' \
            > /proc/sys/fs/binfmt_misc/register
    fi
    grep -q enabled /proc/sys/fs/binfmt_misc/qemu-arm || die "qemu-arm binfmt not enabled"
}

fetch_base() {
    mkdir -p "$BASE_DIR"
    local xzf="$BASE_DIR/$BASE_NAME.img.xz"
    if [ ! -f "$xzf" ]; then
        log "Downloading base image"
        curl -fL --retry 3 -o "$xzf.part" "$BASE_URL" && mv "$xzf.part" "$xzf"
    fi
    log "Verifying base image sha256"
    echo "$BASE_SHA256  $xzf" | sha256sum -c - || die "base image checksum mismatch"
}

mount_image() {
    LOOP="$(losetup -fP --show "$IMG")"
    mkdir -p "$MNT"
    mount "${LOOP}p2" "$MNT"
    mount "${LOOP}p1" "$MNT/boot/firmware"
    for d in proc sys dev dev/pts; do mount --bind "/$d" "$MNT/$d"; done
    mkdir -p "$APT_CACHE" "$MNT/var/cache/apt/archives"
    mount --bind "$APT_CACHE" "$MNT/var/cache/apt/archives"
    cp -L /etc/resolv.conf "$MNT/etc/resolv.conf.build"
    mount --bind "$MNT/etc/resolv.conf.build" "$MNT/etc/resolv.conf" 2>/dev/null \
        || cp -L /etc/resolv.conf "$MNT/etc/resolv.conf"
    # Stop package scripts from starting services inside the chroot.
    printf '#!/bin/sh\nexit 101\n' > "$MNT/usr/sbin/policy-rc.d"
    chmod 0755 "$MNT/usr/sbin/policy-rc.d"
}

unmount_image() {
    [ -n "$LOOP" ] || return 0
    rm -f "$MNT/usr/sbin/policy-rc.d"
    umount "$MNT/etc/resolv.conf" 2>/dev/null || true
    rm -f "$MNT/etc/resolv.conf.build"
    for d in var/cache/apt/archives dev/pts dev sys proc boot/firmware; do
        umount "$MNT/$d" 2>/dev/null || umount -l "$MNT/$d" 2>/dev/null || true
    done
    umount "$MNT" 2>/dev/null || umount -l "$MNT" || true
    losetup -d "$LOOP" 2>/dev/null || true
    LOOP=""
}

cleanup() {
    local rc=$?
    if [ "$KEEP_MOUNTED" = 1 ] && [ $rc -eq 0 ]; then
        echo "Left mounted at $MNT (loop $LOOP). When done: umount -R $MNT && losetup -d $LOOP"
        return
    fi
    unmount_image
    [ $rc -eq 0 ] || echo "Build failed (exit $rc); partial image left at $IMG" >&2
}
trap cleanup EXIT

# ---------------------------------------------------------------------------

step_prepare() {
    log "Preparing $IMG"
    mkdir -p "$OUT_DIR"
    xz -dc "$BASE_DIR/$BASE_NAME.img.xz" > "$IMG"
    truncate -s "+${GROW_MB}M" "$IMG"
    parted -s "$IMG" resizepart 2 100%
    local l; l="$(losetup -fP --show "$IMG")"
    e2fsck -pf "${l}p2" || [ $? -le 1 ]
    resize2fs "${l}p2" >/dev/null
    losetup -d "$l"
}

step_packages() {
    log "Installing packages (emulated ARMv6; this is the slow part)"
    in_pi apt-get update -qq
    in_pi apt-get install -y -qq --no-install-recommends \
        python3-fastapi python3-uvicorn python3-jinja2 python3-python-multipart python3-pydantic \
        python3-gpiozero python3-lgpio python3-yaml sqlite3 nftables curl ca-certificates \
        network-manager rfkill iputils-ping iproute2 openssh-server fake-hwclock raspi-utils
    log "Adding Tailscale apt repo (raspbian/trixie) and installing tailscale"
    curl -fsSL https://pkgs.tailscale.com/stable/raspbian/trixie.noarmor.gpg \
        -o "$MNT/usr/share/keyrings/tailscale-archive-keyring.gpg"
    curl -fsSL https://pkgs.tailscale.com/stable/raspbian/trixie.tailscale-keyring.list \
        -o "$MNT/etc/apt/sources.list.d/tailscale.list"
    in_pi apt-get update -qq
    in_pi apt-get install -y -qq tailscale
    in_pi systemctl enable tailscaled.service
    log "Read-only root support (D14): overlayroot + rebuilt initramfs for every kernel"
    in_pi apt-get install -y -qq --no-install-recommends overlayroot
    # the package's trigger only rebuilds the newest kernel's initramfs; the Pi Zero boots v6
    in_pi update-initramfs -u -k all
}

step_users() {
    log "Users: pi -> main (password locked until claim), system user weatherpi"
    if in_pi id pi >/dev/null 2>&1; then
        in_pi usermod -l main -d /home/main -m pi
        in_pi groupmod -n main pi
    fi
    in_pi usermod -s /bin/bash main
    in_pi passwd -l main
    in_pi usermod -aG sudo main
    # Pi OS first-boot user wizard must not run (the agent's claim replaces it).
    in_pi systemctl disable userconfig.service 2>/dev/null || true
    in_pi systemctl mask userconfig.service 2>/dev/null || true
    rm -f "$MNT/etc/sudoers.d/010_pi-nopasswd"
}

step_software() {
    log "Installing WeatherPi software via pi/install.sh"
    rm -rf "$MNT/tmp/weatherpi-src"
    mkdir -p "$MNT/tmp/weatherpi-src"
    rsync -a --exclude __pycache__ --exclude tests --exclude conftest.py \
        "$REPO/pi/" "$MNT/tmp/weatherpi-src/pi/"
    in_pi /bin/bash /tmp/weatherpi-src/pi/install.sh
    rm -rf "$MNT/tmp/weatherpi-src"
    mkdir -p "$MNT/etc/weatherpi"
    echo "$VERSION" > "$MNT/etc/weatherpi/image-version"
}

step_usb_gadget() {
    log "USB gadget (rpi-usb-gadget, offline equivalent of 'rpi-usb-gadget on')"
    echo g_ether > "$MNT/etc/modules-load.d/usb-gadget.conf"
    mkdir -p "$MNT/etc/NetworkManager/dnsmasq-shared.d"
    cat > "$MNT/etc/NetworkManager/dnsmasq-shared.d/90-rpi-usb-gadget-lease.conf" <<'EOF'
# Autogenerated by rpi-usb-gadget – safe to delete
dhcp-authoritative
dhcp-option=51,120
quiet-dhcp
EOF
    local nmdir="$MNT/etc/NetworkManager/system-connections"
    mkdir -p "$nmdir"
    in_pi nmcli --offline connection add type ethernet ifname usb0 con-name "USB Gadget (client)" \
        connection.autoconnect no connection.autoconnect-priority 100 ipv4.method auto \
        ipv4.may-fail yes ipv4.route-metric 100 ipv4.dhcp-timeout 6 ipv6.method disabled \
        > "$nmdir/USB Gadget (client).nmconnection"
    in_pi nmcli --offline connection add type ethernet ifname usb0 con-name "USB Gadget (shared)" \
        connection.autoconnect yes connection.autoconnect-priority 10 ipv4.method shared \
        ipv4.addresses 10.12.194.1/28 ipv6.method disabled \
        > "$nmdir/USB Gadget (shared).nmconnection"
    chmod 0600 "$nmdir"/*.nmconnection
    in_pi systemctl enable rpi-usb-gadget-ics.service
}

step_boot_config() {
    log "Boot config: overlays + WiFi country $WIFI_COUNTRY"
    local cfg="$MNT/boot/firmware/config.txt"
    for line in "dtoverlay=dwc2,dr_mode=peripheral" "dtoverlay=w1-gpio,gpiopin=9"; do
        grep -qxF "$line" "$cfg" || printf '%s\n' "$line" >> "$cfg"   # file ends in [all]
    done
    local cmd="$MNT/boot/firmware/cmdline.txt"
    sed -i 's/ cfg80211.ieee80211_regdom=[A-Z]*//' "$cmd"
    sed -i "1 s/\$/ cfg80211.ieee80211_regdom=$WIFI_COUNTRY/" "$cmd"
    # D14: no first-boot root resize. Pi OS's initramfs would grow p2 to the end of the card,
    # which fails with the data partition after it; firstboot grows p3 instead. (This also
    # skips Pi OS's disk-ID randomisation, which only runs with `resize`.)
    tr ' ' '\n' < "$cmd" | grep -v -x -e resize -e '' | paste -sd' ' > "$cmd.new"
    mv "$cmd.new" "$cmd"
}

step_identity() {
    log "First-boot identity: hostname, SSH, machine-id, cloud-init"
    echo "$HOSTNAME_PI" > "$MNT/etc/hostname"
    sed -i "s/^127\.0\.1\.1.*/127.0.1.1\t$HOSTNAME_PI/" "$MNT/etc/hosts"
    grep -q "^127.0.1.1" "$MNT/etc/hosts" || printf '127.0.1.1\t%s\n' "$HOSTNAME_PI" >> "$MNT/etc/hosts"
    in_pi systemctl enable ssh.service
    rm -f "$MNT"/etc/ssh/ssh_host_*
    : > "$MNT/etc/machine-id"
    rm -f "$MNT/var/lib/dbus/machine-id"
    # Everything cloud-init would do is baked in; disable it so it can't create users.
    touch "$MNT/etc/cloud/cloud-init.disabled"
    in_pi cloud-init clean --logs >/dev/null 2>&1 || true
}

step_hardening() {
    log "Hardening (D14): noatime, disable Bluetooth"
    sed -i -E '/[[:space:]]\/[[:space:]]+ext4/ s/defaults(,noatime)?/defaults,noatime/' "$MNT/etc/fstab"
    in_pi systemctl disable bluetooth.service hciuart.service 2>/dev/null || true
}

step_data_layout() {
    log "Read-only root (D14): data partition layout and fstab"
    install -d -m 0700 "$MNT/var/lib/tailscale"
    in_pi install -d -m 0700 -o main -g main /home/main/.ssh
    install -d -m 0755 "$MNT/data"
    local stage="$MNT$DATA_STAGE" e name target
    rm -rf "$stage"
    mkdir -p "$stage"
    {
        echo "# WeatherPi data partition (D14): everything that must survive a reboot lives"
        echo "# here; the root is read-only (overlayroot) after the first boot."
        printf 'LABEL=%s /data ext4 defaults,noatime,nofail,x-systemd.device-timeout=30s 0 2\n' \
            "$DATA_LABEL"
    } >> "$MNT/etc/fstab"
    for e in "${DATA_BINDS[@]}"; do
        name="${e%%:*}"
        target="${e#*:}"
        [ -d "$MNT$target" ] || die "missing $target in the image"
        # the directory itself (owner, mode, ACLs) and its contents move to the data partition;
        # the empty directory stays in the root as the mount point
        cp -a "$MNT$target" "$stage/$name"
        find "$MNT$target" -mindepth 1 -delete
        printf '/data/%s %s none bind,nofail,x-systemd.requires-mounts-for=/data 0 0\n' \
            "$name" "$target" >> "$MNT/etc/fstab"
    done
}

step_verify() {
    log "Static checks"
    local fail=0
    check() { if "$@"; then echo "  ok   $*"; else echo "  FAIL $*"; fail=1; fi; }
    check in_pi python3 -c "import fastapi, uvicorn, jinja2, python_multipart, gpiozero, yaml"
    # Import checks use a throwaway root so nothing (e.g. a DB) gets created inside the image.
    local imp="env PYTHONPATH=/opt/weatherpi WEATHERPI_ROOT=/tmp/wp-check WEATHERPI_FAKE=1"
    for m in weatherpi.config weatherpi.db weatherpi.hw weatherpi.logger weatherpi.web.app weatherpi.agent.app; do
        # shellcheck disable=SC2086
        check in_pi $imp python3 -c "import $m"
    done
    rm -rf "$MNT/tmp/wp-check"
    for u in weatherpi-logger weatherpi-web weatherpi-agent weatherpi-firstboot weatherpi-backup.timer \
             tailscaled ssh rpi-usb-gadget-ics nftables; do
        check in_pi systemctl is-enabled --quiet "$u"
    done
    check grep -qx "dtoverlay=dwc2,dr_mode=peripheral" "$MNT/boot/firmware/config.txt"
    check grep -qx "dtoverlay=w1-gpio,gpiopin=9" "$MNT/boot/firmware/config.txt"
    check test -z "$(ls "$MNT"/etc/ssh/ssh_host_* 2>/dev/null)"
    check in_pi id main
    check bash -c "! QEMU_CPU=arm1176 chroot '$MNT' id pi >/dev/null 2>&1"
    check in_pi id weatherpi
    check test ! -e "$MNT/var/lib/weatherpi/firstboot.done"
    check test ! -e "$MNT$DATA_STAGE/weatherpi/firstboot.done"
    # read-only root (D14)
    check test -f "$MNT/etc/overlayroot.conf"
    check bash -c "QEMU_CPU=arm1176 chroot '$MNT' lsinitramfs /boot/firmware/initramfs | grep -q 'scripts/init-bottom/overlayroot'"
    check bash -c "! grep -qw resize '$MNT/boot/firmware/cmdline.txt'"
    check bash -c "! grep -q overlayroot= '$MNT/boot/firmware/cmdline.txt'"   # firstboot adds it
    check grep -q "^LABEL=$DATA_LABEL /data ext4 " "$MNT/etc/fstab"
    for e in "${DATA_BINDS[@]}"; do
        check grep -q "^/data/${e%%:*} ${e#*:} none bind," "$MNT/etc/fstab"
        check test -d "$MNT$DATA_STAGE/${e%%:*}"
    done
    check test -x "$MNT/usr/local/sbin/weatherpi-commit-etc"
    check grep -qx "Storage=persistent" "$MNT/etc/systemd/journald.conf.d/weatherpi.conf"
    # Real-tool checks: firewall syntax (container's nft, check-only) and effective sshd config.
    command -v nft >/dev/null || apt-get install -y -qq nftables >/dev/null
    check nft -c -f "$MNT/etc/nftables.d/weatherpi.nft"
    mkdir -p "$MNT/run/sshd"   # sshd -T insists on its privsep dir
    in_pi ssh-keygen -q -t ed25519 -N '' -f /tmp/verify_hostkey
    check bash -c "QEMU_CPU=arm1176 chroot '$MNT' /usr/sbin/sshd -T -h /tmp/verify_hostkey | grep -qx 'passwordauthentication no'"
    check bash -c "QEMU_CPU=arm1176 chroot '$MNT' /usr/sbin/sshd -T -h /tmp/verify_hostkey | grep -qx 'permitrootlogin no'"
    rm -f "$MNT"/tmp/verify_hostkey*
    [ $fail -eq 0 ] || die "static checks failed"
}

step_shrink_and_pack() {
    log "Shrinking and compressing"
    in_pi apt-get clean
    # the data partition's contents come out of the root before it's shrunk
    local data_host="$OUT_DIR/.data-$VERSION"
    rm -rf "$data_host"
    cp -a "$MNT$DATA_STAGE" "$data_host"
    rm -rf "${MNT:?}$DATA_STAGE"
    unmount_image
    local l; l="$(losetup -fP --show "$IMG")"
    e2fsck -pf "${l}p2" || [ $? -le 1 ]
    resize2fs -M "${l}p2" >/dev/null
    local bs blocks size_b
    bs=$(dumpe2fs -h "${l}p2" 2>/dev/null | awk -F: '/^Block size/{gsub(/ /,"",$2);print $2}')
    blocks=$(dumpe2fs -h "${l}p2" 2>/dev/null | awk -F: '/^Block count/{gsub(/ /,"",$2);print $2}')
    size_b=$(( blocks * bs + HEADROOM_MB * 1024 * 1024 ))
    resize2fs "${l}p2" "$(( size_b / 1024 ))K" >/dev/null
    zerofree "${l}p2"
    losetup -d "$l"
    # Shrink partition 2 in place (same start sector and PARTUUID, which cmdline.txt uses).
    local start sectors
    start=$(parted -sm "$IMG" unit s print | awk -F: '$1==2{gsub("s","",$2);print $2}')
    sectors=$(( size_b / 512 ))
    printf ',%s\n' "$sectors" | sfdisk -q --no-reread --no-tell-kernel -N 2 "$IMG"
    # p3: the data partition (D14), 4 MiB aligned after p2, labelled so fstab finds it
    local p3_start p3_sectors
    p3_start=$(( (start + sectors + 8191) / 8192 * 8192 ))
    p3_sectors=$(( DATA_MB * 2048 ))
    truncate -s $(( (p3_start + p3_sectors) * 512 )) "$IMG"
    printf 'start=%s, size=%s, type=83\n' "$p3_start" "$p3_sectors" |
        sfdisk -q --no-reread --no-tell-kernel --append "$IMG"
    l="$(losetup -fP --show "$IMG")"
    mkfs.ext4 -q -L "$DATA_LABEL" "${l}p3"
    mkdir -p /mnt/weatherpi-data
    mount "${l}p3" /mnt/weatherpi-data
    cp -a "$data_host"/. /mnt/weatherpi-data/
    umount /mnt/weatherpi-data
    losetup -d "$l"
    rm -rf "$data_host"
    xz -T0 -6 -f "$IMG"
    (cd "$OUT_DIR" && sha256sum "$(basename "$IMG").xz" > "$(basename "$IMG").xz.sha256")
    chown "${STACK_UID:-0}:${STACK_GID:-0}" "$IMG.xz" "$IMG.xz.sha256" 2>/dev/null || true
}

run_step() {
    "$1"
    if [ "$STOP_AFTER" = "${1#step_}" ]; then
        KEEP_MOUNTED=1
        log "Stopped after ${1#step_} (--stop-after); image left mounted at $MNT"
        exit 0
    fi
}

main() {
    [ "$(id -u)" = 0 ] || die "run as root inside the weatherpi-builder container"
    ensure_tools
    ensure_binfmt
    fetch_base
    step_prepare
    mount_image
    run_step step_packages
    run_step step_users
    run_step step_software
    run_step step_usb_gadget
    run_step step_boot_config
    run_step step_identity
    run_step step_hardening
    run_step step_data_layout
    run_step step_verify
    if [ "$KEEP_MOUNTED" = 1 ]; then
        log "Left mounted for inspection at $MNT (image not packed)"
        return
    fi
    step_shrink_and_pack
    log "Done: $IMG.xz ($(du -h "$IMG.xz" | cut -f1))"
}

main
