"""Load /etc/weatherpi/config.yaml (single source of truth for all Pi services), with the
owner's settings from /var/lib/weatherpi/settings.yaml on top (D26).

CLI for shell scripts:  python3 -m weatherpi.config <dotted.key>
"""

import copy
import sys
from pathlib import Path
from typing import Any

import yaml

from weatherpi import paths

# Defaults mirror pi/config.yaml so a missing key never crashes a service.
DEFAULTS: dict = {
    "station": {"name": "WeatherPi", "location": "Garden"},
    "temp": {"gpio_pin": 9, "poll_interval": 30, "write_interval": 300},
    "rain": {"gpio_pin": 17, "mm_per_tip": 0.2794, "bounce_time": 0.5},
    "logger": {"heartbeat_interval": 300},
    "database": {"path": str(paths.DB_FILE)},
    "backup": {"backup_dir": str(paths.BACKUP_DIR), "retention_days": 7},
    "web": {
        "host": "127.0.0.1",
        "port": 8080,
        "refresh_interval": 60,
        "stale_threshold": 600,
        "cache_ttl": 300,
    },
    "agent": {"port": 8765, "interfaces": ["usb0", "tailscale0"]},
    "tailscale": {"serve_port": 8080},
    "wifi": {"ping_target": "1.1.1.1", "check_interval": 300},
}


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)  # never share nested dicts with DEFAULTS
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


#: how often a temperature reading may be saved, in seconds (D26: 1, 5, 10, 15, 30, 60 min)
RECORD_INTERVAL_CHOICES = (60, 300, 600, 900, 1800, 3600)
#: the sensor is read at least this often, and averaged into each saved reading
MAX_POLL_INTERVAL = 30


def load_settings(path: Any | None = None) -> dict:
    """The owner's settings; unknown keys and invalid values are dropped."""
    settings_path = Path(path) if path is not None else paths.SETTINGS_FILE
    try:
        with open(settings_path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        return {}
    out: dict = {}
    interval = raw.get("record_interval_s") if isinstance(raw, dict) else None
    if isinstance(interval, int) and interval in RECORD_INTERVAL_CHOICES:
        out["record_interval_s"] = interval
    return out


def apply_settings(cfg: dict, settings: dict) -> dict:
    interval = settings.get("record_interval_s")
    if interval:
        cfg["temp"]["write_interval"] = interval
        cfg["temp"]["poll_interval"] = min(MAX_POLL_INTERVAL, interval)
    return cfg


def load_config(path: Any | None = None, settings_path: Any | None = None) -> dict:
    config_path = Path(path) if path is not None else paths.CONFIG_FILE
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            loaded = yaml.safe_load(f) or {}
    except FileNotFoundError:
        loaded = {}
    return apply_settings(_merge(DEFAULTS, loaded), load_settings(settings_path))


def get_value(dotted: str, config: dict | None = None) -> Any:
    node: Any = config if config is not None else load_config()
    for part in dotted.split("."):
        node = node[int(part)] if isinstance(node, list) else node[part]
    return node


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python3 -m weatherpi.config <dotted.key>")
    print(get_value(sys.argv[1]))
