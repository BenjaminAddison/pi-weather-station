"""Dashboard (D24): the v1 dashboard, ported to the weatherpi package.

Endpoints (unchanged from v1): ``/``, ``/health``, ``/api/current``,
``/api/history?range=24h|7d|30d``, ``/api/export.csv?range=24h|7d|30d|all``.
v2 additions: ``/api/current`` returns ``rain_mm_today`` and ``temp_present``; Chart.js is
served from ``/static`` instead of a CDN.

Build an app with :func:`create_app`. ``weatherpi.web.app:app`` also works (for
``uvicorn weatherpi.web.app:app``); it's created on first access, not at import time.
"""

import csv
import io
import time as _time
from collections.abc import Callable
from datetime import UTC, datetime, time, timedelta, tzinfo
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from weatherpi import __version__, hw
from weatherpi.config import load_config
from weatherpi.db import Database

PACKAGE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = PACKAGE_DIR / "templates"
STATIC_DIR = PACKAGE_DIR / "static"

HISTORY_RANGES = {
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
}
EXPORT_EARLIEST = datetime(2000, 1, 1, tzinfo=UTC)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def local_midnight_utc(now: datetime, tz: tzinfo | None = None) -> datetime:
    """Start of "today" in ``tz`` (default: the system's local timezone), as an aware UTC time.

    The midnight offset is resolved for midnight itself, not copied from ``now``, so the
    boundary is right on DST-change days too.
    """
    if tz is None:
        # The claim (and later admin changes) set the timezone while this service runs;
        # tzset() makes the C library re-read /etc/localtime. Not available on Windows.
        if hasattr(_time, "tzset"):
            _time.tzset()
        local_date = now.astimezone().date()
        # A naive datetime's .astimezone() asks the OS what the local offset was at that time.
        midnight = datetime.combine(local_date, time())
        return midnight.astimezone(UTC)
    local_date = now.astimezone(tz).date()
    return datetime.combine(local_date, time(), tzinfo=tz).astimezone(UTC)


class TTLCache:
    """The v1 in-memory cache: a handful of fixed keys, each kept for ``ttl`` seconds."""

    def __init__(self, ttl: float, clock: Callable[[], datetime] = _utc_now):
        self.ttl = timedelta(seconds=ttl)
        self._clock = clock
        self._entries: dict[str, tuple[datetime, Any]] = {}

    def get(self, key: str) -> Any | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        expiry, value = entry
        if self._clock() < expiry:
            return value
        self._entries.pop(key, None)
        return None

    def set(self, key: str, value: Any) -> None:
        if self.ttl > timedelta(0):
            self._entries[key] = (self._clock() + self.ttl, value)

    def clear(self) -> None:
        self._entries.clear()


def create_app(
    config: dict | None = None,
    db: Database | None = None,
    *,
    clock: Callable[[], datetime] = _utc_now,
    tz: tzinfo | None = None,
    temp_present: Callable[[], bool] | None = None,
) -> FastAPI:
    """Build the dashboard app.

    ``clock`` (aware UTC "now"), ``tz`` (timezone for "today", default system local) and
    ``temp_present`` (is the DS18B20 there?) exist for tests.
    """
    config = config if config is not None else load_config()
    db = db if db is not None else Database(config["database"]["path"])
    web_cfg = config["web"]
    station_name = str(config.get("station", {}).get("name") or "WeatherPi")
    refresh_interval = int(web_cfg["refresh_interval"])
    stale_threshold = int(web_cfg["stale_threshold"])
    mm_per_tip = float(config["rain"]["mm_per_tip"])
    if temp_present is None:
        def temp_present() -> bool:
            return hw.temp_sensor_id() is not None

    cache = TTLCache(float(web_cfg["cache_ttl"]), clock)
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    app = FastAPI(title=station_name, version=__version__, docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.state.db = db
    app.state.config = config
    app.state.cache = cache

    @app.get("/health")
    async def health() -> dict:
        heartbeat = db.get_heartbeat()
        now = clock()
        stale_seconds = None
        logger_alive = False
        if heartbeat:
            if heartbeat.tzinfo is None:  # tolerate naive values (assume UTC)
                heartbeat = heartbeat.replace(tzinfo=UTC)
            stale_seconds = int((now - heartbeat).total_seconds())
            logger_alive = stale_seconds <= stale_threshold
        return {
            "status": "ok",
            "logger_alive": logger_alive,
            "last_heartbeat": heartbeat.isoformat() if heartbeat else None,
            "stale_seconds": stale_seconds,
        }

    @app.get("/api/current")
    async def current() -> dict:
        cached = cache.get("current")
        if cached is not None:
            return cached
        now = clock()
        today_start = local_midnight_utc(now, tz)
        temps = db.get_temperature_range(today_start, now)
        tips = len(db.get_rain_range(today_start, now))
        payload = {
            "temp_c": temps[-1]["temp_c"] if temps else None,
            "temp_present": bool(temp_present()),
            "rain_today": tips,
            "rain_mm_today": round(tips * mm_per_tip, 3),
            "today_start": today_start.isoformat(),
            "updated": now.isoformat(),
        }
        cache.set("current", payload)
        return payload

    @app.get("/api/history")
    async def history(range: str = "24h") -> dict:
        if range not in HISTORY_RANGES:
            range = "24h"
        key = f"history:{range}"
        cached = cache.get(key)
        if cached is not None:
            return cached
        now = clock()
        start = now - HISTORY_RANGES[range]
        payload = {
            "temps": db.get_temperature_range(start, now),
            "rains": [r.isoformat() for r in db.get_rain_range(start, now)],
            "range": range,
        }
        cache.set(key, payload)
        return payload

    @app.get("/api/export.csv")
    async def export_csv(range: str = "all") -> Response:
        now = clock()
        if range in HISTORY_RANGES:
            start = now - HISTORY_RANGES[range]
        else:
            range, start = "all", EXPORT_EARLIEST

        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["timestamp", "temp_c", "rain_tip"])
        for ts, temp_c, rain_tip in db.get_export_range(start, now):
            writer.writerow(
                [ts, "" if temp_c is None else temp_c, "" if rain_tip is None else rain_tip]
            )

        filename = f"weatherpi_{range}_{now:%Y%m%d-%H%M%S}.csv"
        return Response(
            content=buf.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={
                "station_name": station_name,
                "refresh_interval": refresh_interval,
                "stale_threshold": stale_threshold,
            },
        )

    return app


_default_app: FastAPI | None = None


def __getattr__(name: str) -> Any:
    # Lazy module attribute ``app`` (PEP 562): importing this module never opens the DB.
    global _default_app
    if name == "app":
        if _default_app is None:
            _default_app = create_app()
        return _default_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
