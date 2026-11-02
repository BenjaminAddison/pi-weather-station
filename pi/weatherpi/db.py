"""SQLite layer (WAL). Schema unchanged from v1 so v1 backups restore cleanly."""

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS temperature_readings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    temp_c REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS rain_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS status (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_temp_timestamp ON temperature_readings(timestamp);
CREATE INDEX IF NOT EXISTS idx_rain_timestamp ON rain_events(timestamp);
"""

TABLES = ("temperature_readings", "rain_events")


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    @contextmanager
    def _get_conn(self):
        if getattr(self._local, "conn", None) is None:
            self._local.conn = self._connect()
        try:
            yield self._local.conn
        except Exception:
            self._local.conn.close()
            self._local.conn = None
            raise

    def _init_db(self) -> None:
        with self._get_conn() as conn:
            conn.executescript(SCHEMA)
            conn.commit()

    def insert_temperature(self, timestamp: datetime, temp_c: float) -> None:
        with self._get_conn() as conn:
            conn.execute(
                "INSERT INTO temperature_readings (timestamp, temp_c) VALUES (?, ?)",
                (timestamp.isoformat(), temp_c),
            )
            conn.commit()

    def insert_rain(self, timestamp: datetime) -> None:
        with self._get_conn() as conn:
            conn.execute("INSERT INTO rain_events (timestamp) VALUES (?)", (timestamp.isoformat(),))
            conn.commit()

    def update_heartbeat(self, timestamp: datetime) -> None:
        with self._get_conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO status (key, value) VALUES (?, ?)",
                ("last_alive", timestamp.isoformat()),
            )
            conn.commit()

    def get_heartbeat(self) -> datetime | None:
        with self._get_conn() as conn:
            row = conn.execute("SELECT value FROM status WHERE key = ?", ("last_alive",)).fetchone()
            return datetime.fromisoformat(row[0]) if row and row[0] else None

    def get_temperature_range(self, start: datetime, end: datetime) -> list[dict]:
        with self._get_conn() as conn:
            cursor = conn.execute(
                "SELECT timestamp, temp_c FROM temperature_readings "
                "WHERE timestamp >= ? AND timestamp <= ? ORDER BY timestamp",
                (start.isoformat(), end.isoformat()),
            )
            return [{"timestamp": r[0], "temp_c": r[1]} for r in cursor.fetchall()]

    def get_rain_range(self, start: datetime, end: datetime) -> list[datetime]:
        with self._get_conn() as conn:
            cursor = conn.execute(
                "SELECT timestamp FROM rain_events "
                "WHERE timestamp >= ? AND timestamp <= ? ORDER BY timestamp",
                (start.isoformat(), end.isoformat()),
            )
            return [datetime.fromisoformat(r[0]) for r in cursor.fetchall()]

    def get_export_range(self, start: datetime, end: datetime) -> list[tuple]:
        with self._get_conn() as conn:
            cursor = conn.execute(
                "SELECT timestamp, temp_c, NULL FROM temperature_readings "
                "WHERE timestamp >= ? AND timestamp <= ? "
                "UNION ALL "
                "SELECT timestamp, NULL, 1 FROM rain_events "
                "WHERE timestamp >= ? AND timestamp <= ? "
                "ORDER BY timestamp",
                (start.isoformat(), end.isoformat(), start.isoformat(), end.isoformat()),
            )
            return cursor.fetchall()

    def row_counts(self) -> dict[str, int]:
        with self._get_conn() as conn:
            return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}

    def backup_to(self, dest: str | Path) -> None:
        """Consistent copy via the SQLite online backup API (safe while the logger writes)."""
        with self._get_conn() as conn:
            target = sqlite3.connect(str(dest))
            try:
                conn.backup(target)
            finally:
                target.close()

    def close(self) -> None:
        if getattr(self._local, "conn", None) is not None:
            self._local.conn.close()
            self._local.conn = None
