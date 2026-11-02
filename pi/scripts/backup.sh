#!/bin/bash
# WeatherPi nightly DB backup (run by weatherpi-backup.timer as root).
# Online-safe: sqlite3 ".backup" copies a consistent snapshot while the logger writes.
set -euo pipefail

export PYTHONPATH="${WEATHERPI_CODE_DIR:-/opt/weatherpi}"
PYTHON="${WEATHERPI_PYTHON:-python3}"
get_config() { "$PYTHON" -m weatherpi.config "$1"; }
log() { echo "backup: $*"; }

DB_PATH="$(get_config database.path)"
BACKUP_DIR="$(get_config backup.backup_dir)"
RETENTION_DAYS="$(get_config backup.retention_days)"

case "$RETENTION_DAYS" in
    '' | *[!0-9]*) log "invalid backup.retention_days '$RETENTION_DAYS', using 7"; RETENTION_DAYS=7 ;;
esac

if [ ! -f "$DB_PATH" ]; then
    log "no database at $DB_PATH yet, nothing to back up"
    exit 0
fi

umask 027
mkdir -p "$BACKUP_DIR"
chmod 0750 "$BACKUP_DIR"

dest="$BACKUP_DIR/weatherpi-$(date +%Y%m%d-%H%M%S).db"
tmp="$dest.partial"
rm -f "$tmp"

sqlite3 -cmd ".timeout 10000" "$DB_PATH" ".backup '$tmp'"
check="$(sqlite3 "$tmp" "PRAGMA quick_check;")"
if [ "$check" != "ok" ]; then
    rm -f "$tmp"
    log "backup failed integrity check: $check"
    exit 1
fi
mv -f "$tmp" "$dest"
log "wrote $dest ($(stat -c %s "$dest") bytes)"

# Retention: drop backups older than N days, but never the one just written.
find "$BACKUP_DIR" -maxdepth 1 -type f -name 'weatherpi-*.db' -mtime "+$RETENTION_DAYS" \
    ! -path "$dest" -print -delete | sed 's/^/backup: removed /'
find "$BACKUP_DIR" -maxdepth 1 -type f -name 'weatherpi-*.db.partial' -delete
