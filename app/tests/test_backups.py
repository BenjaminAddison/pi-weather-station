from __future__ import annotations

import hashlib
import os
import tarfile
from datetime import UTC, datetime

import pytest

from weatherpi_companion import backups
from weatherpi_companion.agent_client import for_usb

from .fakes import FakeAgent, make_backup_bytes

TOKEN = "t" * 43


def write(tmp_path, name="weatherpi-backup-weatherpi-20261019T110500Z.tar.gz", **kw):
    p = tmp_path / name
    p.write_bytes(make_backup_bytes(**kw))
    return p


def test_verify_good_backup(tmp_path):
    check = backups.verify_backup(write(tmp_path))
    assert check.ok, check.problems
    assert check.manifest["format"] == 1
    assert check.actual_rows == {"temperature_readings": 3, "rain_events": 2}
    assert "3 temperature readings" in check.summary and "2 rain tips" in check.summary


def test_verify_row_count_mismatch(tmp_path):
    check = backups.verify_backup(write(tmp_path, manifest_rows={"temperature_readings": 5,
                                                                  "rain_events": 2}))
    assert not check.ok
    assert any("manifest says 5 rows, database has 3" in p for p in check.problems)


def test_verify_bad_format(tmp_path):
    check = backups.verify_backup(write(tmp_path, manifest={"format": 2, "rows": {}}))
    assert not check.ok and any("unsupported backup format" in p for p in check.problems)


def test_verify_missing_members(tmp_path):
    check = backups.verify_backup(write(tmp_path, omit=["manifest.json"]))
    assert not check.ok and "manifest.json is missing" in check.problems
    check = backups.verify_backup(write(tmp_path, name="b2.tar.gz", omit=["weatherpi.db"]))
    assert not check.ok and "weatherpi.db is missing" in check.problems


def test_verify_corrupt_db(tmp_path):
    check = backups.verify_backup(write(tmp_path, db_bytes=b"SQLite format 3\x00" + b"\x01" *
                                        5000))
    assert not check.ok
    assert any("database" in p for p in check.problems)


def test_verify_bad_table_name_in_manifest(tmp_path):
    check = backups.verify_backup(write(tmp_path, manifest_rows={'x"; DROP TABLE y; --': 1}))
    assert not check.ok and any("odd table name" in p for p in check.problems)


def test_verify_not_a_tarball(tmp_path):
    p = tmp_path / "weatherpi-backup-x.tar.gz"
    p.write_bytes(b"not a tarball")
    check = backups.verify_backup(p)
    assert not check.ok and "can't open" in check.problems[0]


@pytest.mark.parametrize("member", [
    "../escape.txt", "/abs.txt", "sub/../../escape.txt",
])
def test_verify_rejects_path_traversal(tmp_path, member):
    ti = tarfile.TarInfo(member)
    ti.size = 1
    check = backups.verify_backup(write(tmp_path, extra=[ti]))
    assert not check.ok and any("unsafe path" in p for p in check.problems)
    assert not (tmp_path / "escape.txt").exists()


def test_verify_rejects_links(tmp_path):
    ti = tarfile.TarInfo("link")
    ti.type = tarfile.SYMTYPE
    ti.linkname = "/etc/passwd"
    check = backups.verify_backup(write(tmp_path, extra=[ti]))
    assert not check.ok and any("unexpected entry" in p for p in check.problems)


def test_list_backups_newest_first(tmp_path):
    a = write(tmp_path, "weatherpi-backup-weatherpi-a.tar.gz", created_at="2026-10-01T00:00:00Z")
    b = write(tmp_path, "weatherpi-backup-weatherpi-b.tar.gz", created_at="2026-10-19T00:00:00Z")
    junk = tmp_path / "weatherpi-backup-junk.tar.gz"
    junk.write_bytes(b"junk")
    os.utime(junk, (1_000_000_000, 1_000_000_000))  # 2001
    (tmp_path / "other.txt").write_text("x")
    items = backups.list_backups(tmp_path)
    assert [i.path for i in items] == [b, a, junk]
    assert items[0].hostname == "weatherpi" and items[0].readable
    assert not items[2].readable
    assert backups.list_backups(tmp_path / "missing") == []
    assert backups.last_backup_time(tmp_path) == datetime(2026, 10, 19, tzinfo=UTC)


def test_backup_overdue(tmp_path):
    assert backups.backup_overdue(tmp_path)
    write(tmp_path, created_at="2026-10-19T00:00:00Z")
    assert not backups.backup_overdue(tmp_path, now=datetime(2026, 10, 20, tzinfo=UTC))
    assert backups.backup_overdue(tmp_path, now=datetime(2026, 10, 27, tzinfo=UTC))


def claimed_agent(**kw) -> FakeAgent:
    return FakeAgent(claimed=True, token_sha=hashlib.sha256(TOKEN.encode()).hexdigest(), **kw)


def test_download_and_verify(tmp_path):
    agent = claimed_agent()
    check = backups.download_and_verify(for_usb(TOKEN, transport=agent.transport()), tmp_path)
    assert check.ok and check.path.parent == tmp_path
    assert check.path.name == "weatherpi-backup-weatherpi-20261019T110500Z.tar.gz"
    assert backups.list_backups(tmp_path)[0].path == check.path


def test_download_and_verify_marks_bad_backups(tmp_path):
    agent = claimed_agent(backup_bytes=make_backup_bytes(manifest_rows={"rain_events": 99}))
    check = backups.download_and_verify(for_usb(TOKEN, transport=agent.transport()), tmp_path)
    assert not check.ok and check.path.name.endswith(".tar.gz.bad")
    assert backups.list_backups(tmp_path) == []


def test_restore_verifies_first(tmp_path):
    agent = claimed_agent()
    client = for_usb(TOKEN, transport=agent.transport())
    bad = write(tmp_path, "bad.tar.gz", manifest={"format": 9})
    with pytest.raises(backups.RestoreRefused) as ei:
        backups.restore_backup(client, bad)
    assert "unsupported" in str(ei.value)
    assert not agent.restored
    res = backups.restore_backup(client, write(tmp_path))
    assert agent.restored and res.rows["rain_events"] == 2


def test_default_backup_name():
    n = backups.default_backup_name("weather pi!", datetime(2026, 10, 19, 11, 5, tzinfo=UTC))
    assert n == "weatherpi-backup-weatherpi-20261019T110500Z.tar.gz"
