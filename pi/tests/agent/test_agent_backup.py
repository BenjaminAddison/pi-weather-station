import io
import json
import sqlite3
import tarfile
from datetime import UTC, datetime, timedelta

import pytest

from weatherpi import paths
from weatherpi.db import Database


def _seed(n_temp=5, n_rain=3) -> None:
    db = Database(paths.DB_FILE)
    now = datetime.now(UTC)
    for i in range(n_temp):
        db.insert_temperature(now - timedelta(minutes=5 * i), 20.0 + i)
    for i in range(n_rain):
        db.insert_rain(now - timedelta(minutes=i))
    db.update_heartbeat(now)
    db.close()


def _tar(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _restore(client, h, data: bytes, name="b.tar.gz"):
    return client.post("/v1/restore", headers=h,
                       files={"backup": (name, data, "application/gzip")})


def test_backup_contents(claimed, root):
    client, h, _ = claimed
    _seed()
    paths.CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    paths.CONFIG_FILE.write_bytes(b"station:\n  name: Example\n")
    r = client.get("/v1/backup", headers=h)
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/gzip"
    cd = r.headers["content-disposition"]
    assert 'filename="weatherpi-backup-weatherpi-' in cd and cd.endswith('Z.tar.gz"')
    with tarfile.open(fileobj=io.BytesIO(r.content), mode="r:gz") as tar:
        names = set(tar.getnames())
        assert names == {"weatherpi.db", "config.yaml", "manifest.json"}
        manifest = json.loads(tar.extractfile("manifest.json").read())
        assert tar.extractfile("config.yaml").read() == b"station:\n  name: Example\n"
        db_bytes = tar.extractfile("weatherpi.db").read()
    assert manifest["format"] == 1
    assert manifest["hostname"] == "weatherpi"
    assert manifest["agent_version"] == "2.2.0"
    assert manifest["rows"] == {"temperature_readings": 5, "rain_events": 3}
    assert set(manifest) >= {"created_at", "image_version"}
    copy = root / "copy.db"
    copy.write_bytes(db_bytes)
    conn = sqlite3.connect(copy)
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    conn.close()


def test_backup_without_db(claimed):
    client, h, _ = claimed
    r = client.get("/v1/backup", headers=h)
    assert r.status_code == 200
    with tarfile.open(fileobj=io.BytesIO(r.content), mode="r:gz") as tar:
        manifest = json.loads(tar.extractfile("manifest.json").read())
    assert manifest["rows"] == {"temperature_readings": 0, "rain_events": 0}
    assert not paths.DB_FILE.exists()


def test_backup_restore_roundtrip(claimed, fake):
    client, h, _ = claimed
    _seed(5, 3)
    backup = client.get("/v1/backup", headers=h).content
    _seed(10, 10)  # more data arrives after the backup
    db = Database(paths.DB_FILE)
    assert db.row_counts() == {"temperature_readings": 15, "rain_events": 13}
    db.close()
    r = _restore(client, h, backup)
    assert r.status_code == 200, r.text
    assert r.json()["rows"] == {"temperature_readings": 5, "rain_events": 3}
    db = Database(paths.DB_FILE)
    assert db.row_counts() == {"temperature_readings": 5, "rain_events": 3}
    db.close()
    pre = paths.DB_FILE.with_name("weatherpi.db.pre-restore")
    old = Database(pre)
    assert old.row_counts() == {"temperature_readings": 15, "rain_events": 13}
    old.close()
    stop = fake.calls.index(["systemctl", "stop", "weatherpi-logger.service"])
    start = fake.calls.index(["systemctl", "start", "weatherpi-logger.service"])
    assert stop < start
    assert fake.units["weatherpi-logger"] == "active"
    leftovers = [p.name for p in paths.DB_FILE.parent.iterdir() if p.name.startswith(".restore")]
    assert leftovers == []


def test_restore_onto_empty_pi(claimed):
    client, h, _ = claimed
    _seed(2, 1)
    backup = client.get("/v1/backup", headers=h).content
    paths.DB_FILE.unlink()
    r = _restore(client, h, backup)
    assert r.status_code == 200
    assert Database(paths.DB_FILE).row_counts()["temperature_readings"] == 2


def _valid_db_bytes(root) -> bytes:
    p = root / "valid.db"
    Database(p).close()
    return p.read_bytes()


@pytest.mark.parametrize("kind", ["garbage", "not_gzip_tar", "no_manifest", "bad_manifest",
                                  "wrong_format", "bad_db", "wrong_tables"])
def test_restore_bad_backup(claimed, root, fake, kind):
    client, h, _ = claimed
    _seed(1, 1)
    good_manifest = json.dumps({"format": 1}).encode()
    if kind == "garbage":
        data = b"this is not a backup"
    elif kind == "not_gzip_tar":
        import gzip

        data = gzip.compress(b"plain text, not a tar")
    elif kind == "no_manifest":
        data = _tar({"weatherpi.db": _valid_db_bytes(root)})
    elif kind == "bad_manifest":
        data = _tar({"manifest.json": b"{nope", "weatherpi.db": _valid_db_bytes(root)})
    elif kind == "wrong_format":
        data = _tar({"manifest.json": json.dumps({"format": 99}).encode(),
                     "weatherpi.db": _valid_db_bytes(root)})
    elif kind == "bad_db":
        data = _tar({"manifest.json": good_manifest, "weatherpi.db": b"SQLite format 3\x00junk"})
    else:
        other = root / "other.db"
        conn = sqlite3.connect(other)
        conn.execute("CREATE TABLE something (x)")
        conn.commit()
        conn.close()
        data = _tar({"manifest.json": good_manifest, "weatherpi.db": other.read_bytes()})
    r = _restore(client, h, data)
    assert r.status_code == 400, r.text
    assert r.json()["error"] == "bad_backup"
    # the live DB is untouched and the logger was never stopped
    db = Database(paths.DB_FILE)
    assert db.row_counts()["temperature_readings"] == 1
    db.close()
    assert ["systemctl", "stop", "weatherpi-logger.service"] not in fake.calls


def test_restore_ignores_path_traversal(claimed, root):
    client, h, _ = claimed
    data = _tar({"manifest.json": json.dumps({"format": 1}).encode(),
                 "weatherpi.db": _valid_db_bytes(root),
                 "../../evil.txt": b"x", "/abs-evil.txt": b"x"})
    assert _restore(client, h, data).status_code == 200
    assert not (root / "evil.txt").exists() and not (root.parent / "evil.txt").exists()


def test_restore_needs_file_and_token(claimed):
    client, h, _ = claimed
    r = client.post("/v1/restore", headers=h)
    assert r.status_code == 400 and r.json()["error"] == "bad_request"
    assert _restore(client, {}, b"x").status_code == 401


def test_backup_busy(claimed):
    client, h, _ = claimed
    from agent_helpers import agent_of

    lock = agent_of(client).station.lock
    with lock:
        r = client.get("/v1/backup", headers=h)
    assert r.status_code == 409 and r.json()["error"] == "busy"
