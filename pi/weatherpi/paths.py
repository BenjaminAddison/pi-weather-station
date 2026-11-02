"""Filesystem locations on the Pi (docs/pi-software.md). Override the root for tests/dev.

Set WEATHERPI_ROOT to a directory to relocate every path under it (e.g. a pytest tmp dir).
Set WEATHERPI_FAKE=1 to use fake hardware/OS adapters instead of the real ones.
"""

import os
from pathlib import Path

ROOT = Path(os.environ.get("WEATHERPI_ROOT", "/"))
FAKE = os.environ.get("WEATHERPI_FAKE") == "1"

CODE_DIR = ROOT / "opt/weatherpi"
CONFIG_FILE = ROOT / "etc/weatherpi/config.yaml"
IMAGE_VERSION_FILE = ROOT / "etc/weatherpi/image-version"
DATA_DIR = ROOT / "var/lib/weatherpi"
DB_FILE = DATA_DIR / "db/weatherpi.db"
AGENT_STATE_DIR = DATA_DIR / "agent"
BACKUP_DIR = ROOT / "var/backups/weatherpi"
MAIN_HOME = ROOT / "home/main"
RUN_DIR = ROOT / "run/weatherpi"
WIFI_CONNECT_LOCK = RUN_DIR / "wifi-connect.lock"
SSHD_PASSWORD_CONF = ROOT / "etc/ssh/sshd_config.d/10-weatherpi-password.conf"
BOOT_CONFIG_TXT = ROOT / "boot/firmware/config.txt"
# owner-changed settings (PUT /v1/settings, D26), merged over CONFIG_FILE
SETTINGS_FILE = DATA_DIR / "settings.yaml"
# read-only root (D14): the overlay is on when cmdline.txt has `overlayroot=tmpfs`
BOOT_CMDLINE_TXT = ROOT / "boot/firmware/cmdline.txt"
PROC_CMDLINE = ROOT / "proc/cmdline"
OVERLAYROOT_CONF = ROOT / "etc/overlayroot.conf"  # present once the overlayroot package is in
DATA_MOUNT = ROOT / "data"
OVERLAY_LOWER = ROOT / "media/root-ro"  # the real (read-only) root while the overlay is on


def image_version() -> str:
    try:
        return IMAGE_VERSION_FILE.read_text().strip()
    except OSError:
        return "dev"
