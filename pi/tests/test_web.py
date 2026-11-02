import copy
import csv
import io
import os
import time as time_mod
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from weatherpi import config, hw
from weatherpi.db import Database
from weatherpi.web import app as web_app
from weatherpi.web.app import create_app, local_midnight_utc

NOW = datetime(2026, 10, 19, 15, 30, tzinfo=UTC)
TZ = timezone(timedelta(hours=2))  # "Pi local time" for the tests: 17:30, midnight = 22:00Z


def make_config(**web) -> dict:
    cfg = copy.deepcopy(config.load_config(Path("does-not-exist.yaml")))
    cfg["web"].update({"cache_ttl": 0, **web})
    return cfg


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "db" / "weatherpi.db")
    yield database
    database.close()


@pytest.fixture
def seeded(db):
    # Temperatures every 6 hours over 40 days, last one 30 min ago.
    for i in range(40 * 4):
        db.insert_temperature(NOW - timedelta(minutes=30) - timedelta(hours=6 * i), 10.0 + i % 7)
    # Rain: 3 tips after local midnight (22:00Z yesterday), 2 before it, 1 eight days ago.
    for ts in [
        NOW - timedelta(hours=1),
        NOW - timedelta(hours=2),
        datetime(2026, 10, 18, 22, 0, 1, tzinfo=UTC),
        datetime(2026, 10, 18, 21, 59, 59, tzinfo=UTC),
        datetime(2026, 10, 18, 12, 0, tzinfo=UTC),
        NOW - timedelta(days=8),
    ]:
        db.insert_rain(ts)
    db.update_heartbeat(NOW - timedelta(seconds=90))
    return db


def client_for(db, cfg=None, clock=lambda: NOW, **kw) -> TestClient:
    kw.setdefault("tz", TZ)
    return TestClient(create_app(cfg or make_config(), db, clock=clock, **kw))


# ---------- today / timezone ----------

def test_local_midnight_with_explicit_tz():
    assert local_midnight_utc(NOW, TZ) == datetime(2026, 10, 18, 22, 0, tzinfo=UTC)
    # After 22:00Z it's already "tomorrow" in UTC+2.
    late = datetime(2026, 10, 19, 23, 0, tzinfo=UTC)
    assert local_midnight_utc(late, TZ) == datetime(2026, 10, 19, 22, 0, tzinfo=UTC)
    # Negative offset: 03:00Z is still the previous day in UTC-5.
    west = timezone(timedelta(hours=-5))
    early = datetime(2026, 10, 19, 3, 0, tzinfo=UTC)
    assert local_midnight_utc(early, west) == datetime(2026, 10, 18, 5, 0, tzinfo=UTC)


def test_local_midnight_system_tz_is_local_midnight():
    start = local_midnight_utc(NOW)
    assert start.utcoffset() == timedelta(0)
    local = start.astimezone()
    assert (local.hour, local.minute, local.second) == (0, 0, 0)
    assert local.date() == NOW.astimezone().date()
    assert start <= NOW < start + timedelta(hours=25)


@pytest.mark.skipif(not hasattr(time_mod, "tzset"), reason="needs time.tzset (POSIX)")
def test_local_midnight_system_tz_across_dst(monkeypatch):  # pragma: no cover - POSIX only
    # 2026-10-25 is the EU DST change: midnight is CEST (+2) but noon is CET (+1).
    monkeypatch.setenv("TZ", "Europe/Berlin")
    time_mod.tzset()
    try:
        noon = datetime(2026, 10, 25, 11, 0, tzinfo=UTC)
        assert local_midnight_utc(noon) == datetime(2026, 10, 24, 22, 0, tzinfo=UTC)
    finally:
        monkeypatch.delenv("TZ")
        time_mod.tzset()


# ---------- /api/current ----------

def test_current_counts_rain_since_local_midnight(seeded):
    data = client_for(seeded).get("/api/current").json()
    assert data["rain_today"] == 3
    assert data["rain_mm_today"] == pytest.approx(3 * 0.2794, abs=1e-3)
    assert data["temp_c"] == 10.0  # newest reading (i=0)
    assert data["temp_present"] is True
    assert data["today_start"] == "2026-10-18T22:00:00+00:00"
    assert data["updated"] == NOW.isoformat()


def test_current_uses_configured_mm_per_tip(seeded):
    cfg = make_config()
    cfg["rain"]["mm_per_tip"] = 0.5
    assert client_for(seeded, cfg).get("/api/current").json()["rain_mm_today"] == 1.5


def test_current_empty_db_and_absent_sensor(db, monkeypatch):
    monkeypatch.setattr(hw.FAKE_TEMP, "present", False)
    data = client_for(db).get("/api/current").json()
    assert data == {
        **data,
        "temp_c": None,
        "temp_present": False,
        "rain_today": 0,
        "rain_mm_today": 0.0,
    }


def test_current_is_cached_for_ttl(db):
    now = [NOW]
    client = client_for(db, make_config(cache_ttl=300), clock=lambda: now[0])
    assert client.get("/api/current").json()["rain_today"] == 0
    db.insert_rain(NOW - timedelta(minutes=1))
    now[0] = NOW + timedelta(seconds=299)
    assert client.get("/api/current").json()["rain_today"] == 0  # still cached
    now[0] = NOW + timedelta(seconds=301)
    assert client.get("/api/current").json()["rain_today"] == 1


# ---------- /api/history ----------

@pytest.mark.parametrize(
    ("rng", "n_temps", "n_rains"),
    [("24h", 4, 4), ("7d", 28, 5), ("30d", 120, 6)],
)
def test_history_ranges(seeded, rng, n_temps, n_rains):
    data = client_for(seeded).get(f"/api/history?range={rng}").json()
    assert data["range"] == rng
    assert len(data["temps"]) == n_temps
    assert len(data["rains"]) == n_rains
    stamps = [t["timestamp"] for t in data["temps"]]
    assert stamps == sorted(stamps)
    assert set(data["temps"][0]) == {"timestamp", "temp_c"}
    assert all(datetime.fromisoformat(r).utcoffset() == timedelta(0) for r in data["rains"])


def test_history_unknown_range_falls_back_to_24h(seeded):
    data = client_for(seeded).get("/api/history?range=bogus").json()
    assert data["range"] == "24h"
    assert len(data["temps"]) == 4


# ---------- /health ----------

def test_health_alive_and_stale(seeded):
    data = client_for(seeded).get("/health").json()
    assert data == {
        "status": "ok",
        "logger_alive": True,
        "last_heartbeat": (NOW - timedelta(seconds=90)).isoformat(),
        "stale_seconds": 90,
    }
    later = client_for(seeded, clock=lambda: NOW + timedelta(seconds=600)).get("/health").json()
    assert later["logger_alive"] is False
    assert later["stale_seconds"] == 690


def test_health_without_heartbeat(db):
    data = client_for(db).get("/health").json()
    assert data == {
        "status": "ok", "logger_alive": False, "last_heartbeat": None, "stale_seconds": None,
    }


# ---------- /api/export.csv ----------

def test_export_csv_format(seeded):
    resp = client_for(seeded).get("/api/export.csv?range=24h")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert resp.headers["content-disposition"] == (
        'attachment; filename="weatherpi_24h_20261019-153000.csv"'
    )
    assert resp.text.startswith("timestamp,temp_c,rain_tip\r\n")
    rows = list(csv.reader(io.StringIO(resp.text)))
    assert rows[0] == ["timestamp", "temp_c", "rain_tip"]
    body = rows[1:]
    temps = [r for r in body if r[1] != ""]
    rains = [r for r in body if r[2] != ""]
    assert len(temps) == 4 and all(r[2] == "" for r in temps)
    assert len(rains) == 4 and all(r == [r[0], "", "1"] for r in rains)
    assert [r[0] for r in body] == sorted(r[0] for r in body)
    assert float(temps[-1][1]) == 10.0


def test_export_all_and_unknown_range(seeded):
    client = client_for(seeded)
    rows = list(csv.reader(io.StringIO(client.get("/api/export.csv").text)))
    assert len(rows) == 1 + 160 + 6
    resp = client.get('/api/export.csv?range=x"; evil')
    assert 'filename="weatherpi_all_' in resp.headers["content-disposition"]
    assert len(list(csv.reader(io.StringIO(resp.text)))) == len(rows)


# ---------- / and static ----------

def test_dashboard_renders_with_local_chartjs(db):
    cfg = make_config(refresh_interval=45, stale_threshold=700)
    cfg["station"]["name"] = "Allotment <Station>"
    resp = client_for(db, cfg).get("/")
    assert resp.status_code == 200
    html = resp.text
    assert '<script src="/static/chart.umd.min.js"></script>' in html
    assert "cdn.jsdelivr" not in html and "https://" not in html
    assert "const REFRESH = 45 * 1000;" in html
    assert "const STALE = 700 * 1000;" in html
    assert "Allotment &lt;Station&gt;" in html  # autoescaped
    for endpoint in ("/api/current", "/api/history?range=24h", "/health", "/api/export.csv"):
        assert endpoint in html
    assert "rain_mm_today" in html


def test_static_chartjs_served(db):
    resp = client_for(db).get("/static/chart.umd.min.js")
    assert resp.status_code == 200
    assert "javascript" in resp.headers["content-type"]
    assert resp.text.startswith("/*!\n * Chart.js v4.")
    assert len(resp.content) > 100_000


# ---------- module-level app / entry point ----------

def test_lazy_module_app_uses_weatherpi_root():
    root = Path(os.environ["WEATHERPI_ROOT"])
    application = web_app.app
    assert application is web_app.app
    assert Path(application.state.db.path).is_relative_to(root)
    assert TestClient(application).get("/health").status_code == 200
    with pytest.raises(AttributeError):
        web_app.nope  # noqa: B018


def test_main_runs_uvicorn_with_config(monkeypatch):
    import uvicorn

    from weatherpi.web import __main__ as entry

    calls = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: calls.update(app=app, **kw))
    entry.main()
    assert calls["host"] == "127.0.0.1" and calls["port"] == 8080
    assert calls["app"].title == "WeatherPi"
