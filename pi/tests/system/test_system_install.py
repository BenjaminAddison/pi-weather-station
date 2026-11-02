"""Run pi/install.sh into a scratch root (test mode: no users, no chown, no systemctl)."""

import os
import shutil
from pathlib import Path

import pytest
from systest_helpers import PI_DIR, run_bash

DEBIAN_NFT = "#!/usr/sbin/nft -f\n\nflush ruleset\n\ntable inet filter {\n}\n"


@pytest.fixture
def src(tmp_path) -> Path:
    """A copy of pi/ with junk that must not be deployed."""
    dest = tmp_path / "src" / "pi"
    shutil.copytree(
        PI_DIR, dest, ignore=shutil.ignore_patterns("tests", ".pytest_cache", "__pycache__")
    )
    junk = dest / "weatherpi" / "__pycache__"
    junk.mkdir()
    (junk / "config.cpython-313.pyc").write_bytes(b"junk")
    (dest / "weatherpi" / "stray.pyc").write_bytes(b"junk")
    return dest


@pytest.fixture
def root(tmp_path) -> Path:
    r = tmp_path / "root"
    r.mkdir()
    return r


def install(bash, src: Path, root: Path, *extra: str, env: dict | None = None):
    args = [(src / "install.sh").as_posix(), "--root", root.as_posix(), "--no-enable", *extra]
    return run_bash(bash, *args, env=env)


def ok(result):
    assert result.returncode == 0, result.stdout + result.stderr
    return result


def test_tree(bash, src, root):
    out = ok(install(bash, src, root, "--skip-users")).stdout
    assert "install: done" in out

    code = root / "opt/weatherpi/weatherpi"
    for name in ("__init__.py", "config.py", "paths.py", "db.py"):
        assert (code / name).read_bytes() == (src / "weatherpi" / name).read_bytes()
    assert not list(code.rglob("__pycache__")) and not list(code.rglob("*.pyc"))
    assert not (root / "opt/weatherpi/.weatherpi.new").exists()

    assert (root / "opt/weatherpi/firstboot.sh").is_file()
    assert (root / "opt/weatherpi/scripts/backup.sh").is_file()
    assert (root / "opt/weatherpi/scripts/wifi_watchdog.sh").is_file()
    commit = root / "usr/local/sbin/weatherpi-commit-etc"  # the agent runs it by name (D14)
    assert commit.read_bytes() == (src / "scripts/weatherpi-commit-etc.sh").read_bytes()

    assert (root / "etc/weatherpi/config.yaml").read_text() == (src / "config.yaml").read_text()
    units = root / "etc/systemd/system"
    for p in (src / "systemd").glob("*.*"):
        if p.is_file():
            assert (units / p.name).read_text() == p.read_text()
    assert (units / "nftables.service.d/weatherpi.conf").is_file()

    assert (root / "etc/nftables.d/weatherpi.nft").read_text() == (
        src / "nftables.conf"
    ).read_text()
    assert 'include "/etc/nftables.d/*.nft"' in (root / "etc/nftables.conf").read_text()
    assert (root / "etc/ssh/sshd_config.d/40-weatherpi.conf").is_file()
    assert (root / "etc/systemd/journald.conf.d/weatherpi.conf").is_file()

    for d in ("var/lib/weatherpi/db", "var/lib/weatherpi/agent", "var/backups/weatherpi"):
        assert (root / d).is_dir()
    assert not (root / "var/lib/weatherpi/firstboot.done").exists(), "firstboot must still run"
    assert not (root / "etc/systemd/system/multi-user.target.wants").exists()  # --no-enable

    if os.name != "nt":  # NTFS via Git Bash doesn't keep POSIX modes
        assert os.access(root / "opt/weatherpi/scripts/backup.sh", os.X_OK)
        assert (root / "var/lib/weatherpi/agent").stat().st_mode & 0o777 == 0o700


def test_rerun_is_idempotent_and_keeps_local_state(bash, src, root):
    ok(install(bash, src, root, "--skip-users"))
    cfg = root / "etc/weatherpi/config.yaml"
    cfg.write_text(cfg.read_text() + "# owner edit\n")
    agent_sshd = root / "etc/ssh/sshd_config.d/10-weatherpi-password.conf"
    agent_sshd.write_text("PasswordAuthentication yes\n")
    stale = root / "opt/weatherpi/weatherpi/removed_module.py"
    stale.write_text("")

    out = ok(install(bash, src, root, "--skip-users")).stdout
    assert "keeping existing" in out
    assert cfg.read_text().endswith("# owner edit\n")
    assert agent_sshd.read_text() == "PasswordAuthentication yes\n"
    assert not stale.exists(), "code dir is replaced as a whole"

    ok(install(bash, src, root, "--skip-users", "--force-config"))
    assert cfg.read_text() == (src / "config.yaml").read_text()

    out = ok(install(bash, src, root, "--skip-users")).stdout
    assert "already up to date" in out


def test_debian_nftables_conf_is_saved_once(bash, src, root):
    nft = root / "etc/nftables.conf"
    nft.parent.mkdir(parents=True)
    nft.write_text(DEBIAN_NFT)
    ok(install(bash, src, root, "--skip-users"))
    assert (root / "etc/nftables.conf.orig").read_text() == DEBIAN_NFT
    assert nft.read_text() == (src / "etc/nftables.conf").read_text()

    ok(install(bash, src, root, "--skip-users"))
    assert (root / "etc/nftables.conf.orig").read_text() == DEBIAN_NFT


def test_fake_env_equals_skip_users(bash, src, root):
    ok(install(bash, src, root, env={"WEATHERPI_INSTALL_FAKE": "1"}))
    assert (root / "opt/weatherpi/weatherpi/config.py").is_file()


def test_refuses_non_root_without_skip_users(bash, src, root):
    uid = run_bash(bash, "-c", "id -u").stdout.strip()
    if uid == "0":
        pytest.skip("running as root")
    result = install(bash, src, root, env={"WEATHERPI_INSTALL_FAKE": "0"})
    assert result.returncode != 0
    assert "run as root" in result.stderr
    assert not (root / "opt").exists()


def test_bad_arguments(bash, src, root):
    assert install(bash, src, root, "--bogus").returncode != 0
    missing = root / "nope"
    result = run_bash(bash, (src / "install.sh").as_posix(), "--root", missing.as_posix())
    assert result.returncode != 0 and "not a directory" in result.stderr
    result = run_bash(bash, (src / "install.sh").as_posix(), "--help")
    assert result.returncode == 0 and "--no-enable" in result.stdout
