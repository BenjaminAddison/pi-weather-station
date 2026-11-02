import copy
import itertools
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from weatherpi import config, hw
from weatherpi.db import Database
from weatherpi.logger import Logger

T0 = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)


class FakeClock:
    def __init__(self):
        self.mono = 1000.0

    def wall(self) -> datetime:
        return T0 + timedelta(seconds=self.mono - 1000.0)

    def monotonic(self) -> float:
        return self.mono

    def sleep(self, seconds: float) -> None:
        self.mono += seconds


def make_config(poll=30, write=300, heartbeat=300) -> dict:
    cfg = copy.deepcopy(config.load_config(Path("does-not-exist.yaml")))
    cfg["temp"].update(poll_interval=poll, write_interval=write)
    cfg["logger"]["heartbeat_interval"] = heartbeat
    return cfg


def all_temps(db: Database) -> list[dict]:
    return db.get_temperature_range(T0 - timedelta(days=1), T0 + timedelta(days=1))


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "db" / "weatherpi.db")
    yield database
    database.close()


def make_logger(db, clock, readings, **cfg):
    it = iter(readings)
    return Logger(
        make_config(**cfg),
        db,
        clock=clock.wall,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        read_temperature=lambda: next(it),
    )


def test_writes_average_every_write_interval(db):
    clock = FakeClock()
    # 10 polls per 300 s window; poll at t=0..270 (window 1), t=300 triggers the write.
    readings = [20.0, 22.0] * 5 + [30.0] * 10 + [99.0] * 5
    lg = make_logger(db, clock, readings)
    lg.start()
    for _ in range(10):
        lg.step()
        clock.sleep(30)
    assert all_temps(db) == []  # nothing before the first full window

    lg.step()  # t=300: reading 11 (30.0) joins the buffer, then the window is flushed
    temps = all_temps(db)
    assert len(temps) == 1
    assert temps[0]["temp_c"] == pytest.approx((5 * 20 + 5 * 22 + 30) / 11)
    assert datetime.fromisoformat(temps[0]["timestamp"]) == T0 + timedelta(seconds=300)
    assert lg.temp_buffer == []


def test_none_readings_are_skipped_in_average(db):
    clock = FakeClock()
    lg = make_logger(db, clock, [None, 10.0, None, 20.0, None], poll=60, write=240)
    lg.start()
    for _ in range(5):
        lg.step()
        clock.sleep(60)
    temps = all_temps(db)
    assert [t["temp_c"] for t in temps] == [15.0]


def test_sensor_absent_never_writes_or_crashes(db):
    clock = FakeClock()
    lg = make_logger(db, clock, [None] * 50, poll=30, write=60, heartbeat=120)
    lg.start()
    for _ in range(50):
        lg.step()
        clock.sleep(30)
    assert all_temps(db) == []
    assert db.get_heartbeat() is not None  # still alive


def test_sensor_read_exception_is_treated_as_missing(db):
    clock = FakeClock()

    def boom():
        raise OSError("w1 bus gone")

    lg = Logger(
        make_config(write=30), db, clock=clock.wall, monotonic=clock.monotonic,
        sleep=clock.sleep, read_temperature=boom,
    )
    lg.start()
    clock.sleep(30)
    lg.step()
    assert all_temps(db) == []


def test_heartbeat_at_startup_and_every_interval(db):
    clock = FakeClock()
    lg = make_logger(db, clock, [21.0] * 20, poll=30, write=10_000, heartbeat=120)
    lg.start()
    assert db.get_heartbeat() == T0

    for _ in range(3):  # t=0, 30, 60, 90: no new heartbeat yet
        lg.step()
        clock.sleep(30)
    lg.step()
    assert db.get_heartbeat() == T0
    clock.sleep(30)  # t=120
    lg.step()
    assert db.get_heartbeat() == T0 + timedelta(seconds=120)


def test_rain_tip_inserts_utc_event(db):
    clock = FakeClock()
    lg = make_logger(db, clock, [])
    lg.start()
    clock.sleep(42)
    lg.rain_sensor.simulate_tip()
    lg.rain_sensor.simulate_tip()
    rains = db.get_rain_range(T0, T0 + timedelta(minutes=1))
    assert rains == [T0 + timedelta(seconds=42)] * 2
    assert rains[0].tzinfo is not None and rains[0].utcoffset() == timedelta(0)


def test_rain_sensor_gets_config_pin_and_bounce(db):
    seen = {}

    class Sensor:
        def __init__(self, pin, on_tip, bounce_time):
            seen.update(pin=pin, bounce=bounce_time)
            self.closed = False

        def close(self):
            seen["closed"] = True

    cfg = make_config()
    cfg["rain"].update(gpio_pin=27, bounce_time=0.3)
    lg = Logger(cfg, db, read_temperature=lambda: None, rain_sensor_factory=Sensor)
    stop = threading.Event()
    stop.set()
    lg.run(stop)
    assert seen == {"pin": 27, "bounce": 0.3, "closed": True}


def test_run_loop_polls_until_stopped(db):
    clock = FakeClock()
    stop = threading.Event()
    polls = []

    def read():
        polls.append(clock.mono)
        if len(polls) == 12:
            stop.set()
        return 18.0

    lg = Logger(
        make_config(poll=30, write=300), db, clock=clock.wall, monotonic=clock.monotonic,
        sleep=clock.sleep, read_temperature=read,
    )
    lg.run(stop)
    assert len(polls) == 12
    assert [b - a for a, b in itertools.pairwise(polls)] == [30.0] * 11  # steady cadence
    assert [t["temp_c"] for t in all_temps(db)] == [18.0]
    assert lg.rain_sensor is None  # closed on the way out


def test_timer_jitter_does_not_stretch_windows(db):
    """Polls that land a hair early (299.998 s) must still trigger the 300 s write."""
    clock = FakeClock()
    lg = make_logger(db, clock, [20.0] * 40, poll=30, write=300, heartbeat=300)
    lg.start()
    for _ in range(30):
        lg.step()
        clock.sleep(29.9999)
    stamps = [datetime.fromisoformat(t["timestamp"]) for t in all_temps(db)]
    assert len(stamps) == 2  # at ~300 s and ~600 s, not 330 s / 660 s
    assert all(abs((s - T0).total_seconds() - 300 * (i + 1)) < 1 for i, s in enumerate(stamps))
    assert abs((db.get_heartbeat() - T0).total_seconds() - 600) < 1


def test_run_with_real_event_wait_stops_promptly(db):
    lg = Logger(make_config(poll=3600), db, read_temperature=lambda: 20.0)
    stop = threading.Event()
    t = threading.Thread(target=lg.run, args=(stop,))
    started = time.monotonic()
    t.start()
    time.sleep(0.2)
    stop.set()
    t.join(timeout=5)
    assert not t.is_alive()
    assert time.monotonic() - started < 5


def test_insert_failure_keeps_buffer_and_loop_alive(db):
    clock = FakeClock()
    lg = make_logger(db, clock, [20.0] * 10, poll=30, write=60)
    lg.start()

    real_insert = db.insert_temperature
    db.insert_temperature = lambda *a: (_ for _ in ()).throw(RuntimeError("disk full"))
    for _ in range(3):  # t=0,30,60 -> flush at 60 fails
        lg.step()
        clock.sleep(30)
    assert len(lg.temp_buffer) == 3

    db.insert_temperature = real_insert
    for _ in range(2):  # t=90, 120 -> flush at 120 succeeds with all 5 readings
        lg.step()
        clock.sleep(30)
    assert [t["temp_c"] for t in all_temps(db)] == [20.0]
    assert lg.temp_buffer == []


def test_fake_hw_end_to_end_defaults(db):
    """With the real hw adapters in fake mode (WEATHERPI_FAKE=1)."""
    clock = FakeClock()
    lg = Logger(make_config(poll=30, write=60), db, clock=clock.wall,
                monotonic=clock.monotonic, sleep=clock.sleep)
    lg.start()
    for _ in range(3):
        lg.step()
        clock.sleep(30)
    assert [t["temp_c"] for t in all_temps(db)] == [hw.FAKE_TEMP.celsius]
    lg.stop()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal delivery")
def test_module_entry_point_stops_on_sigterm(tmp_path):  # pragma: no cover - Linux CI / Pi
    env = dict(os.environ, WEATHERPI_ROOT=str(tmp_path), WEATHERPI_FAKE="1")
    proc = subprocess.Popen(
        [sys.executable, "-m", "weatherpi.logger"],
        cwd=Path(__file__).parents[1], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    time.sleep(1.5)
    proc.send_signal(signal.SIGTERM)
    out, _ = proc.communicate(timeout=10)
    assert proc.returncode == 0
    assert b"stopped cleanly" in out
