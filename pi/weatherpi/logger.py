"""Sensor loop: temperature averages, rain tips and a heartbeat into SQLite.

Ported from v1 ``logger/weatherpi_logger.py``. Run with ``python3 -m weatherpi.logger``.

* Temperature is polled every ``temp.poll_interval`` seconds; every ``temp.write_interval``
  seconds the average of the valid readings since the last write is stored. A missing sensor
  (``None`` readings) is logged once and never crashes the loop.
* Every rain-bucket tip is stored as one ``rain_events`` row (UTC timestamp).
* ``status.last_alive`` is written at startup and every ``logger.heartbeat_interval`` seconds.
* SIGTERM / SIGINT stop the loop cleanly.

Intervals are measured on a monotonic clock, so the wall-clock jump when the Pi (no RTC)
syncs NTP after boot can't stall or burst the writes. Stored timestamps use the wall clock.
"""

import logging
import signal
import sys
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from weatherpi import hw
from weatherpi.config import load_config
from weatherpi.db import Database

log = logging.getLogger("weatherpi.logger")


def _utc_now() -> datetime:
    return datetime.now(UTC)


class Logger:
    """The logger loop. Every external dependency is injectable so tests never really sleep.

    ``clock`` returns the (aware, UTC) wall-clock time used for stored timestamps;
    ``monotonic`` drives the intervals; ``sleep(seconds)`` waits between polls (by default it
    waits on the stop event, so a signal ends the wait immediately).
    """

    def __init__(
        self,
        config: dict,
        db: Database,
        *,
        clock: Callable[[], datetime] = _utc_now,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Any] | None = None,
        read_temperature: Callable[[], float | None] = hw.read_temperature,
        rain_sensor_factory: Callable[..., Any] = hw.RainSensor,
    ):
        self.db = db
        self.poll_interval = float(config["temp"]["poll_interval"])
        self.write_interval = float(config["temp"]["write_interval"])
        self.heartbeat_interval = float(config["logger"]["heartbeat_interval"])
        self.rain_pin = int(config["rain"]["gpio_pin"])
        self.rain_bounce = float(config["rain"]["bounce_time"])

        self._clock = clock
        self._monotonic = monotonic
        self._sleep = sleep
        self._read_temperature = read_temperature
        self._rain_sensor_factory = rain_sensor_factory

        self.rain_sensor: Any = None
        self.temp_buffer: list[float] = []
        self._last_write = 0.0
        self._last_heartbeat = 0.0
        self._sensor_ok: bool | None = None  # None = not read yet; only log on changes
        self._started = False

    # ---------- lifecycle ----------

    def start(self) -> None:
        """Attach the rain sensor and write the startup heartbeat."""
        log.info(
            "Starting logger (rain pin=%s, poll=%ss, write=%ss, heartbeat=%ss, db=%s)",
            self.rain_pin,
            self.poll_interval,
            self.write_interval,
            self.heartbeat_interval,
            self.db.path,
        )
        self.rain_sensor = self._rain_sensor_factory(
            self.rain_pin, self.on_rain_tip, bounce_time=self.rain_bounce
        )
        now = self._monotonic()
        self._last_write = now
        self._write_heartbeat(now)
        self._started = True

    def stop(self) -> None:
        if self.rain_sensor is not None:
            try:
                self.rain_sensor.close()
            except Exception:
                log.exception("Error closing rain sensor")
            self.rain_sensor = None
        self.db.close()
        self._started = False
        log.info("Logger stopped cleanly")

    def run(self, stop_event: threading.Event) -> None:
        """Poll until ``stop_event`` is set, keeping a steady ``poll_interval`` cadence."""
        sleep = self._sleep or (lambda seconds: stop_event.wait(seconds))
        self.start()
        try:
            next_poll = self._monotonic()
            while not stop_event.is_set():
                self.step()
                next_poll += self.poll_interval
                delay = next_poll - self._monotonic()
                if delay < 0:  # fell behind (slow sensor read, suspended): don't burst
                    next_poll = self._monotonic()
                    delay = 0.0
                if not stop_event.is_set():
                    sleep(delay)
        finally:
            self.stop()

    # ---------- one poll ----------

    def step(self) -> None:
        """Read the sensor once, then write the average and the heartbeat if they're due."""
        if not self._started:
            raise RuntimeError("Logger.start() must be called before step()")
        self._poll_temperature()
        now = self._monotonic()
        # Half a poll of slack: a poll landing at 299.998 s still counts as the 300 s one,
        # otherwise timer jitter would stretch every window by a whole poll interval.
        slack = self.poll_interval / 2
        if now - self._last_write >= self.write_interval - slack:
            self._flush_temperature(now)
        if now - self._last_heartbeat >= self.heartbeat_interval - slack:
            self._write_heartbeat(now)

    def _poll_temperature(self) -> None:
        try:
            temp = self._read_temperature()
        except Exception:
            log.exception("Temperature read failed")
            temp = None
        ok = temp is not None
        if ok != self._sensor_ok:
            if ok:
                log.info("Temperature sensor reading OK (%.2f C)", temp)
            else:
                log.warning("Temperature sensor not found or unreadable; skipping readings")
            self._sensor_ok = ok
        if ok:
            self.temp_buffer.append(temp)

    def _flush_temperature(self, now: float) -> None:
        # The window restarts even when there was nothing to write, so each stored value is
        # the average of (at most) one write_interval of readings.
        self._last_write = now
        if not self.temp_buffer:
            return
        avg = sum(self.temp_buffer) / len(self.temp_buffer)
        try:
            self.db.insert_temperature(self._clock(), avg)
        except Exception:
            log.exception("Temperature insert failed (%d readings kept)", len(self.temp_buffer))
            return
        self.temp_buffer.clear()

    def _write_heartbeat(self, now: float) -> None:
        try:
            self.db.update_heartbeat(self._clock())
            self._last_heartbeat = now
        except Exception:
            log.exception("Heartbeat write failed")

    # ---------- rain (called from the gpiozero thread) ----------

    def on_rain_tip(self) -> None:
        try:
            self.db.insert_rain(self._clock())
        except Exception:
            log.exception("Rain insert failed")


def main() -> None:
    logging.basicConfig(
        stream=sys.stdout, level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )
    config = load_config()
    db = Database(config["database"]["path"])
    stop_event = threading.Event()

    def handle_signal(signum: int, _frame: object) -> None:
        log.info("Received signal %s, stopping", signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    Logger(config, db).run(stop_event)


if __name__ == "__main__":
    main()
