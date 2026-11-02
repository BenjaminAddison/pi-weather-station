"""Station DB access for the agent, backup export (tar.gz) and restore.

The agent runs as root but the DB belongs to the `weatherpi` user. It never creates the DB,
keeps no long-lived connections, and hands any -wal/-shm files it may have created back to the
DB's owner, so the logger can always write.
"""

import io
import json
import logging
import os
import shutil
import sqlite3
import tarfile
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from weatherpi import paths
from weatherpi.agent import AGENT_VERSION
from weatherpi.agent.errors import ApiError, OpLock, iso, utc_now
from weatherpi.agent.oscmd import Runner
from weatherpi.db import TABLES, Database

log = logging.getLogger("weatherpi.agent")

FORMAT = 1
MAX_BACKUP_BYTES = 512 * 2**20
MEMBERS = {"weatherpi.db", "config.yaml", "manifest.json"}
REQUIRED_COLUMNS = {
    "temperature_readings": {"id", "timestamp", "temp_c"},
    "rain_events": {"id", "timestamp"},
}
LOGGER_UNIT = "weatherpi-logger.service"
WEB_UNIT = "weatherpi-web.service"
GZIP_MAGIC = b"\x1f\x8b"
SERVICE_USER = "weatherpi"


def _fix_owner(db_path: Path) -> None:
    """Give -wal/-shm (possibly created by root) back to the DB file's owner."""
    if paths.FAKE or not hasattr(os, "chown"):
        return
    try:
        st = db_path.stat()
    except OSError:
        return
    for suffix in ("-wal", "-shm"):
        side = db_path.with_name(db_path.name + suffix)
        try:
            if side.stat().st_uid != st.st_uid:
                os.chown(side, st.st_uid, st.st_gid)
        except OSError:
            pass


def _chown_service(path: Path) -> None:
    """The logger runs as `weatherpi` and can't write root-owned files (real mode only)."""
    if not paths.FAKE and path.exists():
        shutil.chown(path, SERVICE_USER, SERVICE_USER)


class Station:
    """Read access to the station DB plus backup/restore."""

    def __init__(self, runner: Runner, cfg: dict, hostname: str):
        self.r = runner
        self.cfg = cfg
        self.db_path = Path(cfg["database"]["path"])
        self.hostname = hostname
        self.lock = OpLock("backup/restore")
        self._ro_lock = threading.Lock()

    @contextmanager
    def _ro(self):
        if not self.db_path.exists():
            yield None
            return
        conn = sqlite3.connect(self.db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
        try:
            yield conn
        finally:
            conn.close()
            _fix_owner(self.db_path)

    def read_live(self, since: datetime, until: datetime):
        """(rain tips in [since, until], last tip ever, last heartbeat)."""
        with self._ro_lock, self._ro() as conn:
            if conn is None:
                return 0, None, None
            try:
                tips = conn.execute(
                    "SELECT COUNT(*) FROM rain_events WHERE timestamp >= ? AND timestamp <= ?",
                    (since.isoformat(), until.isoformat()),
                ).fetchone()[0]
                last = conn.execute("SELECT MAX(timestamp) FROM rain_events").fetchone()[0]
                hb = conn.execute(
                    "SELECT value FROM status WHERE key = 'last_alive'"
                ).fetchone()
            except sqlite3.Error as exc:
                log.warning("reading the station DB failed: %s", exc)
                return 0, None, None

        def parse(value):
            try:
                return datetime.fromisoformat(value) if value else None
            except ValueError:
                return None

        return int(tips), parse(last), parse(hb[0] if hb else None)

    # ---------- export ----------

    def create_backup(self) -> tuple[Path, str]:
        """Build the tar.gz in a temp dir. Returns (path, download filename); caller deletes."""
        with self.lock:
            work = Path(tempfile.mkdtemp(prefix="weatherpi-backup-"))
            try:
                db_copy = work / "weatherpi.db"
                if self.db_path.exists():
                    src = Database(self.db_path)
                    try:
                        src.backup_to(db_copy)
                    finally:
                        src.close()
                        _fix_owner(self.db_path)
                else:
                    Database(db_copy).close()  # empty DB with the schema
                copy = Database(db_copy)
                try:
                    rows = copy.row_counts()
                finally:
                    copy.close()
                now = utc_now()
                manifest = {
                    "format": FORMAT,
                    "created_at": iso(now),
                    "hostname": self.hostname,
                    "image_version": paths.image_version(),
                    "agent_version": AGENT_VERSION,
                    "rows": rows,
                }
                try:
                    config_bytes = paths.CONFIG_FILE.read_bytes()
                except OSError:
                    config_bytes = b"# no /etc/weatherpi/config.yaml on the Pi (defaults in use)\n"
                name = f"weatherpi-backup-{self.hostname}-{now.strftime('%Y%m%dT%H%M%SZ')}.tar.gz"
                out = work / name
                with tarfile.open(out, "w:gz") as tar:
                    _add_bytes(tar, "manifest.json", json.dumps(manifest, indent=2).encode())
                    tar.add(db_copy, arcname="weatherpi.db")
                    _add_bytes(tar, "config.yaml", config_bytes)
                db_copy.unlink()
                return out, name
            except Exception:
                shutil.rmtree(work, ignore_errors=True)
                raise

    # ---------- restore ----------

    def restore(self, data: bytes) -> dict:
        if len(data) > MAX_BACKUP_BYTES:
            raise ApiError(400, "bad_backup", "That file is too large to be a WeatherPi backup.")
        with self.lock:
            if not self.db_path.parent.exists():
                self.db_path.parent.mkdir(parents=True)
                _chown_service(self.db_path.parent)
            work = Path(tempfile.mkdtemp(prefix=".restore-", dir=self.db_path.parent))
            try:
                manifest, new_db = _unpack(data, work)
                rows = _validate_db(new_db)
                self._swap_in(new_db)
            finally:
                shutil.rmtree(work, ignore_errors=True)
        return {"rows": rows, "manifest": manifest}

    def _swap_in(self, new_db: Path) -> None:
        self.r.run(["systemctl", "stop", LOGGER_UNIT], timeout=60)
        try:
            pre = self.db_path.with_name(self.db_path.name + ".pre-restore")
            if self.db_path.exists():
                old = Database(self.db_path)
                try:
                    pre.unlink(missing_ok=True)
                    old.backup_to(pre)  # includes anything still in the WAL
                finally:
                    old.close()
            for suffix in ("-wal", "-shm"):  # stale WAL files would corrupt the swapped-in DB
                self.db_path.with_name(self.db_path.name + suffix).unlink(missing_ok=True)
            os.replace(new_db, self.db_path)
            _chown_service(self.db_path)
            _chown_service(pre)
        finally:
            self.r.run(["systemctl", "start", LOGGER_UNIT], timeout=60)
            self.r.run(["systemctl", "try-restart", WEB_UNIT], timeout=60)


def _add_bytes(tar: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mtime = int(utc_now().timestamp())
    info.mode = 0o644
    tar.addfile(info, io.BytesIO(data))


def _bad(message: str) -> ApiError:
    return ApiError(400, "bad_backup", message)


def _unpack(data: bytes, work: Path) -> tuple[dict, Path]:
    if data[:2] != GZIP_MAGIC:
        raise _bad("That isn't a WeatherPi backup (not a .tar.gz file).")
    found: dict[str, bytes] = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            for member in tar:
                name = member.name.removeprefix("./")
                if name not in MEMBERS or not member.isfile():
                    continue  # ignore anything unexpected; never extract by path
                if member.size > MAX_BACKUP_BYTES:
                    raise _bad("A file inside the backup is too large.")
                f = tar.extractfile(member)
                if f is not None:
                    found[name] = f.read()
    except (tarfile.TarError, OSError, EOFError):
        raise _bad("The backup file is damaged.") from None
    if "manifest.json" not in found or "weatherpi.db" not in found:
        raise _bad("The backup is missing manifest.json or weatherpi.db.")
    try:
        manifest = json.loads(found["manifest.json"])
    except ValueError:
        raise _bad("The backup's manifest.json is unreadable.") from None
    if not isinstance(manifest, dict):
        raise _bad("The backup's manifest.json is not an object.")
    if manifest.get("format") != FORMAT:
        raise _bad(f"Unsupported backup format {manifest.get('format')!r}.")
    db = work / "weatherpi.db"
    db.write_bytes(found["weatherpi.db"])
    return manifest, db


def _validate_db(path: Path) -> dict:
    try:
        conn = sqlite3.connect(str(path))
        try:
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise _bad("The database in the backup is corrupt.")
            for table, needed in REQUIRED_COLUMNS.items():
                cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
                if not needed <= cols:
                    raise _bad(f"The database in the backup has no usable {table} table.")
            rows = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}
            conn.execute("PRAGMA journal_mode=WAL")
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        raise _bad("The backup doesn't contain a valid SQLite database.") from None
    Database(path).close()  # make sure the status table/indexes exist (schema is idempotent)
    return rows
