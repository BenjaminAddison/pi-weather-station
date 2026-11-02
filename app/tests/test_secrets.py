from __future__ import annotations

import os
import re
import sys

import pytest
from cryptography.hazmat.primitives.serialization import load_ssh_private_key, load_ssh_public_key

from weatherpi_companion import secrets as sec
from weatherpi_companion.secrets import StationProfile, StationSecrets

SERIAL = "00000000abcd1234"


def test_app_home_override(monkeypatch, tmp_path):
    monkeypatch.setenv(sec.ENV_HOME, str(tmp_path / "x"))
    assert sec.app_home() == tmp_path / "x"


def test_app_home_default(monkeypatch):
    monkeypatch.delenv(sec.ENV_HOME, raising=False)
    home = sec.app_home()
    if sys.platform == "win32":
        assert home.name == "WeatherPi" and "Local" in str(home)
    else:
        assert home.name == "weatherpi"


def test_layout(app_data):
    assert app_data.stations_dir.is_dir() and app_data.backups_dir.is_dir()
    assert app_data.log_file.parent.is_dir()
    assert app_data.station(SERIAL).dir == app_data.stations_dir / SERIAL
    with pytest.raises(ValueError):
        app_data.station("../evil")


def test_generators():
    tok = sec.generate_api_token()
    assert len(tok) >= 43 and re.fullmatch(r"[A-Za-z0-9_-]+", tok)
    rp = sec.generate_recovery_password()
    assert len(rp) >= 20
    assert re.fullmatch(r"[a-z2-9]{5}(-[a-z2-9]{5}){4}", rp)
    assert not set(rp) & set("01oil")
    assert len({sec.generate_recovery_password() for _ in range(20)}) == 20


def test_ssh_keypair_openssh_formats():
    kp = sec.generate_ssh_keypair("weatherpi-companion@TEST")
    assert kp.private_openssh.startswith(b"-----BEGIN OPENSSH PRIVATE KEY-----")
    assert kp.public_openssh.startswith("ssh-ed25519 AAAA")
    assert kp.public_openssh.endswith(" weatherpi-companion@TEST")
    priv = load_ssh_private_key(kp.private_openssh, password=None)
    pub = load_ssh_public_key(kp.public_openssh.encode())
    assert priv.public_key().public_bytes_raw() == pub.public_bytes_raw()
    assert sec.default_key_comment().startswith("weatherpi-companion@")


def test_save_and_load_secrets_roundtrip(app_data):
    store = app_data.station(SERIAL)
    assert store.load_secrets() is None
    s = StationSecrets.generate("c")
    store.save_secrets(s)
    loaded = store.load_secrets()
    assert loaded == s
    names = sorted(p.name for p in store.dir.iterdir())
    if sec.DPAPI_AVAILABLE:
        assert names == ["api_token.dpapi", "id_ed25519.dpapi", "id_ed25519.pub",
                         "recovery.dpapi"]
        # encrypted at rest: the plain values must not be in the files
        assert s.api_token.encode() not in (store.dir / "api_token.dpapi").read_bytes()
        assert b"OPENSSH PRIVATE KEY" not in (store.dir / "id_ed25519.dpapi").read_bytes()
    else:
        assert names == ["api_token", "id_ed25519", "id_ed25519.pub", "recovery"]
        assert os.stat(store.dir / "id_ed25519").st_mode & 0o777 == 0o600


def test_plain_secret_file(tmp_path):
    f = sec.SecretFile(tmp_path, "thing", use_dpapi=False)
    f.write(b"hello")
    assert f.path.name == "thing" and f.read() == b"hello"
    if os.name == "posix":
        assert f.path.stat().st_mode & 0o777 == 0o600


@pytest.mark.skipif(not sec.DPAPI_AVAILABLE, reason="Windows only")
def test_dpapi_roundtrip_and_tamper():
    blob = sec.dpapi_protect(b"secret data")
    assert blob != b"secret data" and sec.dpapi_unprotect(blob) == b"secret data"
    bad = bytearray(blob)
    bad[-5] ^= 0xFF
    with pytest.raises(sec.ProtectionError):
        sec.dpapi_unprotect(bytes(bad))


def test_recovery_file(app_data):
    store = app_data.station(SERIAL)
    rp = sec.generate_recovery_password()
    path = store.write_recovery_file(rp, "weatherpi")
    text = path.read_text(encoding="utf-8")
    assert rp in text and "KEEP THIS FILE SAFE" in text and "Forgot password" in text
    assert SERIAL in text
    assert store.read_recovery_password_from_txt() == rp
    assert sec.read_recovery_password(path.parent / "missing.txt") is None


def test_load_secrets_falls_back_to_recovery_txt(app_data):
    store = app_data.station(SERIAL)
    s = StationSecrets.generate()
    store.save_secrets(s)
    store.write_recovery_file(s.recovery_password)
    store._recovery().path.unlink()
    assert store.load_secrets().recovery_password == s.recovery_password


def test_profile_roundtrip_and_update(app_data):
    store = app_data.station(SERIAL)
    assert store.load_profile() is None
    store.save_profile(StationProfile(serial=store.serial, hostname="weatherpi"))
    p = store.update_profile(tailnet_ip="100.94.2.31", dns_name="weatherpi.example.ts.net")
    assert p.tailnet_ip == "100.94.2.31"
    assert store.load_profile() == p
    with pytest.raises(AttributeError):
        store.update_profile(nonsense=1)
    # unknown keys (e.g. from a newer app version) are ignored
    store.profile_path.write_text('{"serial": "00000000abcd1234", "future": 1}')
    assert store.load_profile().serial == SERIAL
    assert [p.serial for p in app_data.list_profiles()] == [SERIAL]
    assert app_data.list_stations() == [SERIAL]


def test_corrupt_profile_is_none(app_data):
    store = app_data.station(SERIAL)
    store.dir.mkdir(parents=True)
    store.profile_path.write_text("{not json")
    assert store.load_profile() is None


def test_log_tail(app_data):
    assert app_data.log_tail() == ""
    app_data.log_file.write_text("\n".join(f"line {i}" for i in range(100)))
    assert app_data.log_tail(5).splitlines() == [f"line {i}" for i in range(95, 100)]


def test_known_values_and_no_password_field():
    s = StationSecrets.generate()
    vals = s.known_values()
    assert s.api_token in vals and s.recovery_password in vals
    assert not any("password" == f for f in s.__dataclass_fields__)
