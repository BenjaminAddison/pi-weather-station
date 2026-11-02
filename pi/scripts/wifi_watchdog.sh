#!/bin/bash
# WeatherPi WiFi recovery monitor (weatherpi-watchdog.service, root).
# Watches connectivity and tries to bring WiFi back if it drops.
# It NEVER reboots the Pi: the station keeps collecting data offline, and a
# reboot cannot fix a signal/range problem anyway.
set -uo pipefail

export PYTHONPATH="${WEATHERPI_CODE_DIR:-/opt/weatherpi}"
PYTHON="${WEATHERPI_PYTHON:-python3}"
get_config() { "$PYTHON" -m weatherpi.config "$1" 2>/dev/null; }

PING_TARGET="$(get_config wifi.ping_target)"
PING_TARGET="${PING_TARGET:-1.1.1.1}"
CHECK_INTERVAL="$(get_config wifi.check_interval)"
case "$CHECK_INTERVAL" in '' | *[!0-9]* | 0) CHECK_INTERVAL=300 ;; esac
CONNECT_LOCK="${WEATHERPI_WIFI_LOCK:-/run/weatherpi/wifi-connect.lock}"   # created by the agent

# stdout goes to the journal, which adds timestamps.
log() { echo "watchdog: $*"; }

wifi_profiles() {
    # UUIDs of saved WiFi profiles (UUIDs avoid nmcli's escaping of ':' in names).
    nmcli -t -f UUID,TYPE connection show 2>/dev/null | awk -F: '$2 == "802-11-wireless" {print $1}'
}

recover_wifi() {
    # 1. Make sure the radio is enabled.
    if ! nmcli radio wifi 2>/dev/null | grep -q enabled; then
        nmcli radio wifi on >/dev/null 2>&1 && log "radio was off, turned it on"
    fi

    # 2. Bring up each saved WiFi connection (best-effort).
    local uuid
    while IFS= read -r uuid; do
        [ -z "$uuid" ] && continue
        if nmcli connection up uuid "$uuid" >/dev/null 2>&1; then
            log "reconnected wifi profile $uuid"
            return 0
        fi
    done < <(wifi_profiles)

    # 3. Fallback: ask NetworkManager to reapply the wifi device.
    if nmcli device reapply wlan0 >/dev/null 2>&1; then
        log "reapplied wlan0"
        return 0
    fi

    return 1
}

log "started (target $PING_TARGET, every ${CHECK_INTERVAL}s)"
fail_count=0
no_profile_logged=0

while true; do
    if ping -c 1 -W 5 "$PING_TARGET" >/dev/null 2>&1; then
        if [ "$fail_count" -gt 0 ]; then
            log "connectivity restored"
            fail_count=0
        fi
        no_profile_logged=0
    elif [ -e "$CONNECT_LOCK" ]; then
        # The agent is applying a new WiFi network right now; don't fight it.
        log "wifi connect in progress (agent), skipping recovery"
    elif [ -z "$(wifi_profiles)" ]; then
        # Not set up yet (no WiFi saved): nothing to recover, say so once.
        if [ "$no_profile_logged" -eq 0 ]; then
            log "no saved WiFi network yet, waiting for setup"
            no_profile_logged=1
        fi
    else
        fail_count=$((fail_count + 1))
        log "ping to $PING_TARGET failed (attempt $fail_count), trying to recover wifi"
        if ! recover_wifi; then
            log "wifi recovery did not help (no reboot - keeping data collection alive)"
        fi
    fi
    sleep "$CHECK_INTERVAL"
done
