"""Download, verify, list and restore station backups (``GET /v1/backup``, ``/v1/restore``).

A backup is ``weatherpi-backup-<host>-<stamp>.tar.gz`` containing ``weatherpi.db``,
``config.yaml`` and ``manifest.json`` (``format`` 1, ``created_at``, ``hostname``,
``image_version``, ``agent_version``, ``rows``).
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import tarfile
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .agent_client import AgentClient, ProgressCallback, RestoreResult, parse_time

BACKUP_GLOB = "weatherpi-backup-*.tar.gz"
MANIFEST_FORMAT = 1
REQUIRED_MEMBERS = ("weatherpi.db", "manifest.json")
MAX_UNPACKED_BYTES = 2 * 1024**3  # refuse absurd archives
_TABLE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


@dataclass
class BackupCheck:
    ok: bool
    path: Path
    manifest: dict[str, Any] = field(default_factory=dict)
    actual_rows: dict[str, int] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        """One line for the owner, e.g. for the restore confirmation."""
        m = self.manifest
        rows = m.get("rows") or {}
        when = m.get("created_at") or "unknown date"
        parts = [f"{m.get('hostname') or 'WeatherPi'}, made {when}"]
        if "temperature_readings" in rows:
            parts.append(f"{rows['temperature_readings']} temperature readings")
        if "rain_events" in rows:
            parts.append(f"{rows['rain_events']} rain tips")
        return ", ".join(parts)


def default_backup_name(hostname: str = "weatherpi", when: datetime | None = None) -> str:
    stamp = (when or datetime.now(UTC)).astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    host = re.sub(r"[^A-Za-z0-9-]", "", hostname) or "weatherpi"
    return f"weatherpi-backup-{host}-{stamp}.tar.gz"


def _safe_extract(tar: tarfile.TarFile, dest: Path) -> None:
    """Extract only regular files with plain relative names (no links, devices, ``..``)."""
    total = 0
    dest = dest.resolve()
    for m in tar.getmembers():
        name = m.name.replace("\\", "/")
        if m.isdir():
            continue
        if not m.isfile():
            raise ValueError(f"unexpected entry type in backup: {m.name}")
        if name.startswith("/") or ".." in name.split("/") or ":" in name:
            raise ValueError(f"unsafe path in backup: {m.name}")
        target = (dest / name).resolve()
        if dest not in target.parents:
            raise ValueError(f"unsafe path in backup: {m.name}")
        total += m.size
        if total > MAX_UNPACKED_BYTES:
            raise ValueError("backup is unreasonably large")
    tar.extractall(dest, filter="data")


def _find_member(root: Path, name: str) -> Path | None:
    direct = root / name
    if direct.is_file():
        return direct
    matches = sorted(root.rglob(name))
    return matches[0] if matches else None


def verify_backup(path: Path | str) -> BackupCheck:
    """Unpack to a temp folder and check manifest, DB integrity and row counts."""
    path = Path(path)
    check = BackupCheck(False, path)
    tmp = Path(tempfile.mkdtemp(prefix="weatherpi-verify-"))
    try:
        try:
            with tarfile.open(path, "r:gz") as tar:
                _safe_extract(tar, tmp)
        except (tarfile.TarError, OSError, EOFError, ValueError) as exc:
            check.problems.append(f"can't open the backup file: {exc}")
            return check
        manifest_path = _find_member(tmp, "manifest.json")
        db_path = _find_member(tmp, "weatherpi.db")
        if manifest_path is None:
            check.problems.append("manifest.json is missing")
        else:
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (ValueError, OSError) as exc:
                check.problems.append(f"manifest.json is unreadable: {exc}")
            else:
                if isinstance(manifest, dict):
                    check.manifest = manifest
                else:
                    check.problems.append("manifest.json is not a JSON object")
        if check.manifest and check.manifest.get("format") != MANIFEST_FORMAT:
            check.problems.append(
                f"unsupported backup format {check.manifest.get('format')!r} "
                f"(expected {MANIFEST_FORMAT})")
        if db_path is None:
            check.problems.append("weatherpi.db is missing")
        else:
            _check_db(db_path, check)
        check.ok = not check.problems
        return check
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _check_db(db_path: Path, check: BackupCheck) -> None:
    try:
        con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        check.problems.append(f"database can't be opened: {exc}")
        return
    try:
        try:
            result = [r[0] for r in con.execute("PRAGMA integrity_check").fetchall()]
        except sqlite3.DatabaseError as exc:
            check.problems.append(f"database is damaged: {exc}")
            return
        if result != ["ok"]:
            check.problems.append("database integrity check failed: " + "; ".join(
                str(r) for r in result[:5]))
            return
        expected = check.manifest.get("rows") if check.manifest else None
        if not isinstance(expected, dict):
            if check.manifest:
                check.problems.append("manifest has no row counts")
            return
        for table, count in expected.items():
            if not _TABLE_RE.match(str(table)):
                check.problems.append(f"odd table name in manifest: {table!r}")
                continue
            try:
                (actual,) = con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()
            except sqlite3.Error as exc:
                check.problems.append(f"table {table} missing: {exc}")
                continue
            check.actual_rows[str(table)] = int(actual)
            try:
                if int(count) != int(actual):
                    check.problems.append(
                        f"{table}: manifest says {count} rows, database has {actual}")
            except (TypeError, ValueError):
                check.problems.append(f"{table}: bad row count {count!r} in manifest")
    finally:
        con.close()


def read_manifest(path: Path | str) -> dict[str, Any] | None:
    """Just the manifest, without unpacking the DB (for listings)."""
    try:
        with tarfile.open(path, "r:gz") as tar:
            for m in tar.getmembers():
                if m.isfile() and m.name.replace("\\", "/").rsplit("/", 1)[-1] == "manifest.json":
                    fh = tar.extractfile(m)
                    if fh is None:
                        return None
                    data = json.loads(fh.read(1024 * 1024).decode("utf-8"))
                    return data if isinstance(data, dict) else None
    except (tarfile.TarError, OSError, EOFError, ValueError):
        return None
    return None


@dataclass
class BackupInfo:
    path: Path
    size: int
    created_at: datetime  # from the manifest, else the file's mtime
    manifest: dict[str, Any] | None

    @property
    def hostname(self) -> str | None:
        return (self.manifest or {}).get("hostname")

    @property
    def readable(self) -> bool:
        return self.manifest is not None


def list_backups(folder: Path | str) -> list[BackupInfo]:
    """Backups in ``folder``, newest first."""
    folder = Path(folder)
    if not folder.is_dir():
        return []
    out = []
    for p in folder.glob(BACKUP_GLOB):
        if not p.is_file():
            continue
        st = p.stat()
        manifest = read_manifest(p)
        created = parse_time((manifest or {}).get("created_at")) or datetime.fromtimestamp(
            st.st_mtime, UTC)
        out.append(BackupInfo(p, st.st_size, created, manifest))
    out.sort(key=lambda b: b.created_at, reverse=True)
    return out


def last_backup_time(folder: Path | str) -> datetime | None:
    backups = list_backups(folder)
    return backups[0].created_at if backups else None


def backup_overdue(folder: Path | str, days: int = 7, now: datetime | None = None) -> bool:
    last = last_backup_time(folder)
    if last is None:
        return True
    return ((now or datetime.now(UTC)) - last).total_seconds() > days * 86400


def download_and_verify(
    client: AgentClient,
    folder: Path | str,
    hostname: str = "weatherpi",
    progress: ProgressCallback | None = None,
) -> BackupCheck:
    """Back up now: download (server's file name, else our own) and verify.

    A backup that fails verification is kept but renamed ``*.tar.gz.bad`` so it doesn't
    show up as a good backup in :func:`list_backups`.
    """
    dl = client.download_backup(Path(folder), progress=progress)
    path = dl.path
    if not path.name.startswith("weatherpi-backup-") or not path.name.endswith(".tar.gz"):
        new = path.with_name(default_backup_name(hostname))
        path.replace(new)
        path = new
    check = verify_backup(path)
    if not check.ok:
        bad = path.with_name(path.name + ".bad")
        path.replace(bad)
        check.path = bad
    return check


class RestoreRefused(Exception):
    def __init__(self, check: BackupCheck) -> None:
        super().__init__("; ".join(check.problems) or "backup failed verification")
        self.check = check


def restore_backup(client: AgentClient, path: Path | str) -> RestoreResult:
    """Verify locally first, then ``POST /v1/restore``. Raises RestoreRefused or AgentError."""
    check = verify_backup(path)
    if not check.ok:
        raise RestoreRefused(check)
    return client.restore(Path(path))
