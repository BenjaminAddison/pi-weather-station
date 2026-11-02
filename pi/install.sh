#!/bin/bash
# WeatherPi installer: deploys this pi/ directory onto a Raspberry Pi OS root filesystem.
#
#   install.sh [--root DIR] [--no-enable] [--force-config] [--skip-users]
#
#   --root DIR      target root filesystem (default /). The image build runs this inside
#                   the chroot with the default root; on a live Pi it redeploys in place.
#   --no-enable     don't enable the units (systemctl enable)
#   --force-config  overwrite an existing /etc/weatherpi/config.yaml
#   --skip-users    don't create the weatherpi user/group and don't chown anything; for
#                   test installs into a scratch dir on a PC (WEATHERPI_INSTALL_FAKE=1
#                   does the same)
#
# Idempotent, no network, no apt (the image build installs packages).
# What goes where: docs/pi-software.md. Everything under pi/etc/ mirrors /etc/.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT=/
ENABLE=1
FORCE_CONFIG=0
SKIP_USERS=0
[ "${WEATHERPI_INSTALL_FAKE:-0}" = 1 ] && SKIP_USERS=1

UNITS=(
    weatherpi-firstboot.service
    weatherpi-logger.service
    weatherpi-web.service
    weatherpi-agent.service
    weatherpi-watchdog.service
    weatherpi-backup.timer
    nftables.service
)

log() { echo "install: $*"; }
die() { echo "install: ERROR: $*" >&2; exit 1; }
usage() { sed -n '2,15p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

while [ $# -gt 0 ]; do
    case "$1" in
        --root) [ $# -ge 2 ] || die "--root needs a directory"; ROOT="$2"; shift 2 ;;
        --root=*) ROOT="${1#--root=}"; shift ;;
        --no-enable) ENABLE=0; shift ;;
        --force-config) FORCE_CONFIG=1; shift ;;
        --skip-users) SKIP_USERS=1; shift ;;
        -h | --help) usage; exit 0 ;;
        *) usage >&2; die "unknown argument: $1" ;;
    esac
done

[ -d "$ROOT" ] || die "root '$ROOT' is not a directory"
ROOT="$(cd "$ROOT" && pwd)"
ROOT="${ROOT%/}" # "" means the real /
[ -f "$SRC/weatherpi/__init__.py" ] || die "$SRC doesn't look like the repo's pi/ directory"
if [ "$SKIP_USERS" -eq 0 ] && [ "$(id -u)" -ne 0 ]; then
    die "run as root (or use --skip-users for a test install)"
fi

log "source $SRC -> root ${ROOT:-/}"
[ "$SKIP_USERS" -eq 1 ] && log "--skip-users: no user creation, no chown"

T() { printf '%s%s' "$ROOT" "$1"; } # path inside the target root
own() { [ "$SKIP_USERS" -eq 1 ] || chown "$@"; }
mkd() { # mkd MODE OWNER:GROUP DIR
    if [ "$SKIP_USERS" -eq 1 ]; then
        # Test installs (e.g. Git Bash on NTFS) may not support every mode.
        mkdir -p "$3"
        chmod "$1" "$3" 2>/dev/null || log "  (couldn't chmod $1 $3, ignored in test mode)"
    else
        install -d -m "$1" "$3"
        chown "$2" "$3"
    fi
}
put() { # put MODE SRC DEST  (root-owned file, parent dirs created 0755)
    install -D -m "$1" "$2" "$3"
    own root:root "$3"
    log "  $3"
}

# --- 1. System user/group ---------------------------------------------------------
if [ "$SKIP_USERS" -eq 0 ]; then
    R=()
    [ -n "$ROOT" ] && R=(--root "$ROOT")
    has_entry() { grep -q "^$2:" "$(T "/etc/$1")" 2>/dev/null; }
    if ! has_entry group weatherpi; then
        log "creating group weatherpi"
        groupadd "${R[@]}" --system weatherpi
    fi
    if ! has_entry passwd weatherpi; then
        log "creating system user weatherpi"
        useradd "${R[@]}" --system --gid weatherpi --no-create-home \
            --home-dir /var/lib/weatherpi --shell /usr/sbin/nologin \
            --comment "WeatherPi services" weatherpi
    fi
    if has_entry group gpio; then
        usermod "${R[@]}" -aG gpio weatherpi
        log "weatherpi is in group gpio"
    else
        log "WARNING: no gpio group in the target; the logger may not reach GPIO"
    fi
fi

# --- 2. Python package -> /opt/weatherpi/weatherpi (replaced as a whole) ----------
log "code -> $(T /opt/weatherpi/weatherpi)"
mkd 0755 root:root "$(T /opt/weatherpi)"
stage="$(T /opt/weatherpi/.weatherpi.new)"
rm -rf "$stage"
cp -R "$SRC/weatherpi" "$stage"
find "$stage" -type d \( -name __pycache__ -o -name .pytest_cache \) -prune -exec rm -rf {} +
find "$stage" -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete
chmod -R u=rwX,go=rX "$stage"
own -R root:root "$stage"
rm -rf "$(T /opt/weatherpi/weatherpi)"
mv "$stage" "$(T /opt/weatherpi/weatherpi)"

# --- 3. Scripts --------------------------------------------------------------------
log "scripts:"
for f in "$SRC"/scripts/*.sh; do
    put 0755 "$f" "$(T /opt/weatherpi/scripts)/$(basename "$f")"
done
put 0755 "$SRC/firstboot.sh" "$(T /opt/weatherpi/firstboot.sh)"
# called by name by the agent (read-only root, D14)
put 0755 "$SRC/scripts/weatherpi-commit-etc.sh" "$(T /usr/local/sbin/weatherpi-commit-etc)"

# --- 4. Config (kept if present: it may hold the owner's settings) -----------------
mkd 0755 root:root "$(T /etc/weatherpi)"
cfg="$(T /etc/weatherpi/config.yaml)"
if [ ! -e "$cfg" ] || [ "$FORCE_CONFIG" -eq 1 ]; then
    log "config:"
    put 0644 "$SRC/config.yaml" "$cfg"
elif cmp -s "$SRC/config.yaml" "$cfg"; then
    log "config: $cfg already up to date"
else
    log "config: keeping existing $cfg (differs from the repo; --force-config replaces it)"
fi

# --- 5. systemd units and drop-ins -------------------------------------------------
log "systemd units:"
unit_dir="$(T /etc/systemd/system)"
for f in "$SRC"/systemd/*.service "$SRC"/systemd/*.timer; do
    put 0644 "$f" "$unit_dir/$(basename "$f")"
done
for d in "$SRC"/systemd/*.d; do
    [ -d "$d" ] || continue
    for f in "$d"/*.conf; do
        put 0644 "$f" "$unit_dir/$(basename "$d")/$(basename "$f")"
    done
done

# --- 6. Firewall: our table in /etc/nftables.d, /etc/nftables.conf only includes ---
# Debian's default /etc/nftables.conf starts with "flush ruleset", which would wipe
# Tailscale's and NetworkManager's tables on reload; it's replaced by an include-only
# stub (pi/etc/nftables.conf, step 7) and the original kept once as .orig.
log "firewall:"
put 0644 "$SRC/nftables.conf" "$(T /etc/nftables.d/weatherpi.nft)"
nft_main="$(T /etc/nftables.conf)"
if [ -f "$nft_main" ] && ! grep -q WeatherPi "$nft_main" && [ ! -e "$nft_main.orig" ]; then
    cp -p "$nft_main" "$nft_main.orig"
    log "  saved original as $nft_main.orig"
fi

# --- 7. /etc drop-ins (pi/etc/X -> /etc/X) -----------------------------------------
# Not touched here: the agent's /etc/ssh/sshd_config.d/10-weatherpi-password.conf.
log "/etc files:"
while IFS= read -r -d '' f; do
    rel="${f#"$SRC/etc/"}"
    case "$rel" in
        nftables.conf) mode=0755 ;;
        sudoers.d/*) mode=0440 ;;
        *) mode=0644 ;;
    esac
    put "$mode" "$f" "$(T "/etc/$rel")"
done < <(find "$SRC/etc" -type f -print0 | sort -z)

# --- 8. Data dirs (firstboot re-checks these on the device) ------------------------
log "data dirs"
mkd 0755 root:root "$(T /var/lib/weatherpi)"
mkd 0750 weatherpi:weatherpi "$(T /var/lib/weatherpi/db)"
mkd 0700 root:root "$(T /var/lib/weatherpi/agent)"
mkd 0750 root:root "$(T /var/backups/weatherpi)"

# --- 9. Enable units (offline-safe: works in a chroot without a running systemd) ---
live=0
if [ -z "$ROOT" ] && [ -d /run/systemd/system ] &&
    ! systemd-detect-virt --quiet --chroot 2>/dev/null; then
    live=1
fi

if [ "$ENABLE" -eq 1 ]; then
    command -v systemctl >/dev/null 2>&1 || die "systemctl not found (use --no-enable)"
    log "enabling: ${UNITS[*]}"
    if [ -n "$ROOT" ]; then
        systemctl --root="$ROOT" enable "${UNITS[@]}"
    else
        systemctl enable "${UNITS[@]}"
    fi
fi

if [ "$live" -eq 1 ]; then
    log "live system: reloading systemd and restarting WeatherPi services"
    systemctl daemon-reload
    if [ "$ENABLE" -eq 1 ]; then
        systemctl reload-or-restart nftables.service
        systemctl restart weatherpi-logger.service weatherpi-web.service \
            weatherpi-agent.service weatherpi-watchdog.service
        systemctl start weatherpi-backup.timer
    else
        systemctl try-restart weatherpi-logger.service weatherpi-web.service \
            weatherpi-agent.service weatherpi-watchdog.service
    fi
fi

log "done"
