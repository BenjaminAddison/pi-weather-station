"""Device info, clock, power, live sensors, diagnostics (with redaction)."""

import glob
import logging
import os
import platform
import re
import shutil
import socket
import threading
import time
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

import yaml

from weatherpi import config, hw, paths
from weatherpi.agent.errors import ApiError, iso, utc_now
from weatherpi.agent.oscmd import Runner

log = logging.getLogger("weatherpi.agent")

SERVICES = (
    "weatherpi-logger.service",
    "weatherpi-web.service",
    "weatherpi-agent.service",
    "weatherpi-backup.timer",
    "weatherpi-watchdog.service",
    "NetworkManager.service",
    "tailscaled.service",
    "ssh.service",
    "nftables.service",
)
JOURNAL_UNITS = ("weatherpi-logger", "weatherpi-agent", "weatherpi-web", "NetworkManager",
                 "tailscaled")
JOURNAL_LINES = 80

FAKE_MODEL = "Raspberry Pi Zero W Rev 1.1"
FAKE_SERIAL = "00000000fa4e0001"
FAKE_HOSTNAME = "weatherpi"
_STARTED = time.time()


# ---------- redaction ----------

REDACTED = "[REDACTED]"
_SECRET_KEY_RE = re.compile(
    r"(psk|passw|passphrase|secret|token|auth_?key|api_?key|private|recovery)", re.IGNORECASE
)
_TEXT_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"tskey-[A-Za-z0-9_\-]+"), "tskey-" + REDACTED),
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=\-]+"), r"\1 " + REDACTED),
    (re.compile(r"https://login\.tailscale\.com/a/[A-Za-z0-9]+"),
     "https://login.tailscale.com/a/" + REDACTED),
    (re.compile(
        r"(?i)\b((?:wifi-sec\.|802-11-wireless-security\.)?psk|pass(?:word|wd|phrase)?|secret|"
        r"(?:api[_-]?|auth[_-]?)?token|auth[_-]?key|api[_-]?key|recovery[_-]?password)"
        r"(\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|\S+)"
    ), r"\1\2" + REDACTED),
    (re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"), "[email]"),
]


def redact(value: Any, secrets: Iterable[str] = ()) -> Any:
    """Recursively scrub passwords, PSKs, tokens, tailscale auth keys/URLs, emails and the
    literal `secrets` (e.g. values from the agent state dir) from dicts/lists/strings."""
    literal = sorted({s for s in secrets if s and len(s) >= 6}, key=len, reverse=True)

    def text(s: str) -> str:
        for lit in literal:
            s = s.replace(lit, REDACTED)
        for pattern, repl in _TEXT_RULES:
            s = pattern.sub(repl, s)
        return s

    def walk(v: Any, key: str = "") -> Any:
        if isinstance(v, dict):
            return {k: walk(item, str(k)) for k, item in v.items()}
        if isinstance(v, (list, tuple)):
            return [walk(item, key) for item in v]
        if key and _SECRET_KEY_RE.search(key) and v not in (None, "", True, False):
            return REDACTED
        if isinstance(v, str):
            return text(v)
        return v

    return walk(value)


# ---------- helpers ----------

def _read(path, default: str | None = None) -> str | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return default


def config_txt_overlays() -> list[str]:
    text = _read(paths.ROOT / "boot/firmware/config.txt", "") or ""
    out = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if line.startswith("dtoverlay="):
            out.append(line.split("=", 1)[1].strip())
    return out


def w1_devices() -> list[str]:
    if paths.FAKE:
        sid = hw.temp_sensor_id()
        return [sid] if sid else []
    return sorted(
        os.path.basename(p) for p in glob.glob(str(paths.ROOT / "sys/bus/w1/devices/28-*"))
    )


def _disk_usage(path) -> dict:
    try:
        du = shutil.disk_usage(path)
    except OSError:
        return {}
    return {"total_mb": du.total // 2**20, "used_mb": du.used // 2**20,
            "free_mb": du.free // 2**20}


class SystemManager:
    def __init__(self, runner: Runner, cfg: dict, power_delay: float = 2.0):
        self.r = runner
        self.cfg = cfg
        self.power_delay = power_delay
        # a DS18B20 read takes ~0.75 s and shares the bus with the logger; the rain test polls
        # sensors/live every second, so reuse a reading for a few seconds (not in fake mode)
        self.temp_cache_s = 0.0 if paths.FAKE else 5.0
        self._temp: tuple[float, float | None, str | None, datetime] | None = None
        self._temp_lock = threading.Lock()

    def _temperature(self) -> tuple[float | None, str | None, datetime]:
        with self._temp_lock:
            if self._temp and time.monotonic() - self._temp[0] < self.temp_cache_s:
                return self._temp[1], self._temp[2], self._temp[3]
            celsius, sensor_id, at = hw.read_temperature(), hw.temp_sensor_id(), utc_now()
            self._temp = (time.monotonic(), celsius, sensor_id, at)
            return celsius, sensor_id, at

    # ---------- identity ----------

    def model(self) -> str:
        text = _read(paths.ROOT / "proc/device-tree/model")
        if text:
            return text.strip("\x00\n ")
        return FAKE_MODEL if paths.FAKE else "unknown"

    def serial(self) -> str:
        for line in (_read(paths.ROOT / "proc/cpuinfo", "") or "").splitlines():
            if line.lower().startswith("serial"):
                return line.split(":", 1)[1].strip()
        text = _read(paths.ROOT / "sys/firmware/devicetree/base/serial-number")
        if text:
            return text.strip("\x00\n ")
        return FAKE_SERIAL if paths.FAKE else "unknown"

    def hostname(self) -> str:
        return FAKE_HOSTNAME if paths.FAKE else socket.gethostname()

    def clock_synced(self) -> bool:
        res = self.r.run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"], timeout=10)
        return res.ok and res.stdout.strip() == "yes"

    # ---------- clock / power ----------

    def set_time(self, utc: str) -> dict:
        try:
            when = datetime.fromisoformat(utc.strip())
        except (ValueError, AttributeError):
            raise ApiError(400, "bad_request", "utc must be ISO 8601, e.g. 2026-10-19T11:05:00Z") \
                from None
        if when.tzinfo is None:
            raise ApiError(400, "bad_request", "utc needs a timezone (use the Z suffix)")
        if not 2024 <= when.year <= 2100:
            raise ApiError(400, "bad_request", "that date is implausible")
        if self.clock_synced():
            return {"applied": False, "clock_synced": True}
        res = self.r.run(["date", "-u", "-s", f"@{int(when.timestamp())}"], timeout=10)
        if not res.ok:
            raise ApiError(500, "internal", "setting the clock failed")
        self.r.run(["fake-hwclock", "save"], timeout=10)  # Pi has no RTC; keep it over reboots
        return {"applied": True, "clock_synced": False, "time": iso(when)}

    def _later(self, argv: list[str]) -> None:
        def go() -> None:
            self.r.run(argv, timeout=30)

        timer = threading.Timer(self.power_delay, go)
        timer.daemon = True
        timer.start()

    def reboot(self) -> dict:
        self._later(["systemctl", "reboot"])
        return {"rebooting": True}

    def shutdown(self) -> dict:
        self._later(["systemctl", "poweroff"])
        return {"shutting_down": True}

    # ---------- sensors ----------

    def sensors_live(self, read_station) -> dict:
        now = utc_now()
        celsius, sensor_id, read_at = self._temperature()
        temperature = {
            "present": sensor_id is not None,
            "celsius": round(celsius, 2) if celsius is not None else None,
            "sensor_id": sensor_id,
            "read_at": iso(read_at) if celsius is not None else None,
        }
        mm_per_tip = float(self.cfg["rain"]["mm_per_tip"])
        stale_threshold = int(self.cfg["web"]["stale_threshold"])
        local_midnight = now.astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
        today_start = local_midnight.astimezone(UTC)
        tips, last_tip, heartbeat = read_station(today_start, now)
        stale = int((now - heartbeat).total_seconds()) if heartbeat else None
        return {
            "temperature": temperature,
            "rain": {
                "tips_today": tips,
                "mm_today": round(tips * mm_per_tip, 4),
                "last_tip_at": iso(last_tip),
                "mm_per_tip": mm_per_tip,
            },
            "logger": {
                "alive": stale is not None and stale <= stale_threshold,
                "last_heartbeat": iso(heartbeat),
                "stale_seconds": stale,
            },
        }

    # ---------- diagnostics ----------

    def _cmd(self, argv: list[str], timeout: float = 15) -> str:
        res = self.r.run(argv, timeout=timeout)
        if res.missing:
            return f"({argv[0]} not available)"
        return res.stdout.rstrip() if res.ok else (res.output or f"(exit {res.returncode})")

    def _vcgencmd(self, what: str) -> str | None:
        res = self.r.run(["vcgencmd", what], timeout=10)
        if not res.ok:
            return None
        return res.stdout.strip().split("=", 1)[-1]

    def system_info(self) -> dict:
        uptime = _read(paths.ROOT / "proc/uptime")
        uptime_s = int(float(uptime.split()[0])) if uptime else int(time.time() - _STARTED)
        # D14: with the read-only root, "/" is the overlay's RAM layer, not the SD card.
        # "disk" is where the readings live (the data partition), "root_disk" the system.
        data_mounted = os.path.ismount(paths.DATA_MOUNT)
        overlay = os.path.ismount(paths.OVERLAY_LOWER)
        disks = {
            "disk": _disk_usage(paths.DATA_MOUNT if data_mounted else paths.ROOT),
            "root_disk": _disk_usage(paths.OVERLAY_LOWER if overlay else paths.ROOT),
        }
        if overlay:
            disks["ram_layer"] = _disk_usage(paths.ROOT)  # changes to "/" since the last boot
        mem: dict = {}
        for line in (_read(paths.ROOT / "proc/meminfo", "") or "").splitlines():
            key, _, rest = line.partition(":")
            if key in ("MemTotal", "MemAvailable", "SwapTotal", "SwapFree"):
                mem[key.lower() + "_mb"] = int(rest.split()[0]) // 1024
        if not mem and paths.FAKE:
            mem = {"memtotal_mb": 427, "memavailable_mb": 301}
        temp = self._vcgencmd("measure_temp")
        try:
            temp_c = float(temp.rstrip("'C")) if temp else None
        except ValueError:
            temp_c = None
        throttled = self._vcgencmd("get_throttled")
        try:
            bits = int(throttled, 16) if throttled else 0
        except ValueError:
            bits = 0
        return {
            "uptime_s": uptime_s,
            "kernel": "6.12.47+rpt-rpi-v6 (fake)" if paths.FAKE else platform.release(),
            "image_version": paths.image_version(),
            "hostname": self.hostname(),
            "model": self.model(),
            "time": iso(utc_now()),
            "clock_synced": self.clock_synced(),
            **disks,
            "mem": mem,
            "temp_c": temp_c,
            "throttled": throttled,
            "undervoltage_now": bool(bits & 0x1),
            "undervoltage_seen": bool(bits & 0x10000),
        }

    def services(self) -> list[dict]:
        out = []
        for unit in SERVICES:
            res = self.r.run(
                ["systemctl", "show", unit, "-p", "ActiveState", "-p", "UnitFileState",
                 "-p", "NRestarts", "-p", "SubState"],
                timeout=10,
            )
            props = dict(
                line.split("=", 1) for line in res.stdout.splitlines() if "=" in line
            )
            restarts = props.get("NRestarts", "")
            out.append({
                "name": unit.removesuffix(".service"),
                "active": props.get("ActiveState", "unknown"),
                "sub": props.get("SubState"),
                "enabled": props.get("UnitFileState") in ("enabled", "static", "enabled-runtime"),
                "restarts": int(restarts) if restarts.isdigit() else None,
            })
        return out

    def journal(self) -> dict:
        return {
            unit: self._cmd(["journalctl", "-u", unit, "-n", str(JOURNAL_LINES), "--no-pager",
                             "-o", "short-iso"], timeout=20)
            for unit in JOURNAL_UNITS
        }

    def gpio_level(self, pin: int) -> int | None:
        res = self.r.run(["pinctrl", "get", str(pin)], timeout=5)
        if not res.ok:
            return None
        m = re.search(r"\|\s*(hi|lo)\b", res.stdout)
        return {"hi": 1, "lo": 0}[m.group(1)] if m else None

    def config_dump(self) -> dict:
        try:
            with open(paths.CONFIG_FILE, encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
        except (OSError, yaml.YAMLError):
            raw = None
        return {"file": str(paths.CONFIG_FILE), "file_present": raw is not None,
                "effective": config.load_config()}

    def diagnostics(self, wifi_status: dict, tailscale_status: dict, secrets: list[str]) -> dict:
        rain_pin = int(self.cfg["rain"]["gpio_pin"])
        bundle = {
            "collected_at": iso(utc_now()),
            "system": self.system_info(),
            "services": self.services(),
            "journal": self.journal(),
            "network": {
                "interfaces": self._cmd(["ip", "-br", "addr"]),
                "routes": self._cmd(["ip", "route"]),
                "wifi": wifi_status,
                "tailscale": tailscale_status,
                "nftables": self._cmd(["nft", "list", "ruleset"]),
            },
            "hardware": {
                "w1_devices": w1_devices(),
                f"gpio{rain_pin}_level": self.gpio_level(rain_pin),
                "config_txt_overlays": config_txt_overlays(),
            },
            "config": self.config_dump(),
        }
        return redact(bundle, secrets)
