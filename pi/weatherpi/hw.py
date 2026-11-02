"""Hardware adapters (sensors + LED). Real on the Pi; in-memory fakes when WEATHERPI_FAKE=1.

Shared by the logger (W1) and the agent (W2). Keep the public functions stable.
"""

import glob
import os
import threading
import time
from collections.abc import Callable

from weatherpi import paths

W1_GLOB = str(paths.ROOT / "sys/bus/w1/devices/28-*/w1_slave")
LED_DIR = str(paths.ROOT / "sys/class/leds/ACT")


# ---------- temperature (DS18B20 over 1-Wire sysfs) ----------

class _FakeTemp:
    present = True
    celsius: float | None = 21.5
    sensor_id = "28-00000fake001"


FAKE_TEMP = _FakeTemp()


def temp_sensor_id() -> str | None:
    if paths.FAKE:
        return FAKE_TEMP.sensor_id if FAKE_TEMP.present else None
    found = sorted(glob.glob(W1_GLOB))
    return os.path.basename(os.path.dirname(found[0])) if found else None


def read_temperature(retries: int = 3) -> float | None:
    """Return degrees C, or None if no sensor / bad CRC after retries."""
    if paths.FAKE:
        return FAKE_TEMP.celsius if FAKE_TEMP.present else None
    found = sorted(glob.glob(W1_GLOB))
    if not found:
        return None
    for _ in range(retries):
        try:
            with open(found[0], encoding="ascii") as f:
                lines = f.read().splitlines()
            if len(lines) >= 2 and lines[0].strip().endswith("YES") and "t=" in lines[1]:
                return int(lines[1].split("t=")[1]) / 1000.0
        except (OSError, ValueError):
            pass
        time.sleep(0.2)
    return None


# ---------- rain gauge (reed switch, pull-up) ----------

class RainSensor:
    """Calls on_tip() for every bucket tip. Fake mode: call .simulate_tip() yourself."""

    def __init__(self, gpio_pin: int, on_tip: Callable[[], None], bounce_time: float = 0.5):
        self._on_tip = on_tip
        self._button = None
        if not paths.FAKE:
            import gpiozero  # apt: python3-gpiozero (+ python3-lgpio pin factory)

            self._button = gpiozero.Button(gpio_pin, pull_up=True, bounce_time=bounce_time)
            self._button.when_pressed = lambda: self._on_tip()

    def simulate_tip(self) -> None:
        self._on_tip()

    def level(self) -> int | None:
        """1 = open (idle), 0 = closed (magnet at switch); None in fake mode."""
        if self._button is None:
            return None
        return 0 if self._button.is_pressed else 1

    def close(self) -> None:
        if self._button is not None:
            self._button.close()


# ---------- ACT LED ----------

_led_lock = threading.Lock()
FAKE_LED_EVENTS: list[str] = []


def _led_write(name: str, value: str) -> None:
    try:
        with open(os.path.join(LED_DIR, name), "w", encoding="ascii") as f:
            f.write(value)
    except OSError:
        pass


def led_blink(pattern: str) -> None:
    """pattern: 'success' (3 quick) or 'fail' (1 long). Blocks ~1-1.5 s; restores mmc0 trigger."""
    if paths.FAKE:
        FAKE_LED_EVENTS.append(pattern)
        return
    with _led_lock:
        _led_write("trigger", "none")
        steps = [(0.15, 0.15)] * 3 if pattern == "success" else [(1.5, 0.0)]
        for on, off in steps:
            _led_write("brightness", "1")
            time.sleep(on)
            _led_write("brightness", "0")
            time.sleep(off)
        _led_write("trigger", "mmc0")
