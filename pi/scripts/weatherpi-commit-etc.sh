#!/bin/bash
# weatherpi-commit-etc FILE...  (installed as /usr/local/sbin/weatherpi-commit-etc)
#
# Read-only root (D14): with the overlay on, a change to /etc lands in RAM and is gone at
# the next reboot. The agent calls this after the rare /etc changes that must persist (the
# owner's password in /etc/shadow, the sshd password drop-in, the timezone). It remounts
# the real root (/media/root-ro) read-write, copies the files across, and remounts it
# read-only. A file that no longer exists is removed from the real root too.
# With the overlay off (first boot, maintenance mode) the change is already permanent, so
# it does nothing.
#
# For tests: WEATHERPI_OVERLAY_LOWER (default /media/root-ro), WEATHERPI_COMMIT_SRC (prefix
# for the live files, default none), WEATHERPI_COMMIT_ASSUME_OVERLAY=1 (skip the overlay
# check and the remounts).
set -euo pipefail

LOWER="${WEATHERPI_OVERLAY_LOWER:-/media/root-ro}"
SRC="${WEATHERPI_COMMIT_SRC:-}"
ASSUME="${WEATHERPI_COMMIT_ASSUME_OVERLAY:-0}"

log() { echo "commit-etc: $*"; }

[ $# -gt 0 ] || { echo "usage: weatherpi-commit-etc FILE..." >&2; exit 2; }
for f in "$@"; do
    case "$f" in
        /etc/*) ;;
        *) echo "commit-etc: refusing $f (only files under /etc)" >&2; exit 2 ;;
    esac
    case "$f" in
        *..*) echo "commit-etc: refusing $f" >&2; exit 2 ;;
    esac
done

if [ "$ASSUME" != 1 ]; then
    if [ "$(findmnt -n -o FSTYPE /)" != overlay ] || ! mountpoint -q "$LOWER"; then
        log "overlay not active: nothing to do"
        exit 0
    fi
    mount -o remount,rw "$LOWER"
    trap 'sync; mount -o remount,ro "$LOWER" || echo "commit-etc: WARNING: $LOWER is still read-write" >&2' EXIT
fi

for f in "$@"; do
    live="$SRC$f"
    dest="$LOWER$f"
    if [ -e "$live" ] || [ -L "$live" ]; then
        mkdir -p "$(dirname "$dest")"
        # copy beside, then rename: a power cut leaves the old or the new file, never half
        cp -a "$live" "$dest.wp-new"
        mv -f "$dest.wp-new" "$dest"
        log "committed $f"
    elif [ -e "$dest" ] || [ -L "$dest" ]; then
        rm -f "$dest"
        log "removed $f"
    fi
done
sync
