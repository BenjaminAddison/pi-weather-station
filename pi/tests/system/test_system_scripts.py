"""Shell scripts: syntax, line endings, exec bits, and the behaviour that matters."""

import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest
from systest_helpers import PI_DIR, run_bash

SCRIPTS = sorted([PI_DIR / "install.sh", PI_DIR / "firstboot.sh", *PI_DIR.glob("scripts/*.sh")])
OWNED_TEXT_FILES = sorted(
    p
    for p in [
        *SCRIPTS,
        PI_DIR / "nftables.conf",
        *PI_DIR.glob("systemd/**/*"),
        *PI_DIR.glob("etc/**/*"),
        *PI_DIR.glob("tests/system/*.py"),
    ]
    if p.is_file()
)


def code_lines(path: Path) -> list[str]:
    """Non-comment, non-blank lines."""
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            out.append(stripped)
    return out


@pytest.mark.parametrize("path", OWNED_TEXT_FILES, ids=lambda p: p.relative_to(PI_DIR).as_posix())
def test_lf_line_endings(path):
    data = path.read_bytes()
    assert b"\r" not in data, "CRLF line endings (would break bash/systemd/nft on the Pi)"
    assert data.endswith(b"\n"), "missing final newline"


@pytest.mark.parametrize("path", SCRIPTS, ids=lambda p: p.relative_to(PI_DIR).as_posix())
def test_shebang(path):
    assert path.read_text(encoding="utf-8").startswith("#!/bin/bash\n")


@pytest.mark.parametrize("path", SCRIPTS, ids=lambda p: p.relative_to(PI_DIR).as_posix())
def test_bash_syntax(bash, path):
    result = run_bash(bash, "-n", path.as_posix())
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("path", SCRIPTS, ids=lambda p: p.relative_to(PI_DIR).as_posix())
def test_executable_in_git(path):
    git = shutil.which("git")
    if not git:
        pytest.skip("git not available")
    rel = path.relative_to(PI_DIR.parent).as_posix()
    result = subprocess.run(
        [git, "ls-files", "-s", "--", rel],
        check=False,
        cwd=PI_DIR.parent,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        pytest.skip("not tracked by git yet")
    assert result.stdout.split()[0] == "100755", f"{rel} should be committed with +x"


def test_watchdog_never_reboots():
    for line in code_lines(PI_DIR / "scripts/wifi_watchdog.sh"):
        code = re.sub(r'"[^"]*"', '""', line)  # ignore log messages
        for word in ("reboot", "shutdown", "poweroff", "systemctl", "halt"):
            assert word not in code, f"watchdog must never {word}: {line}"


def test_firstboot_steps():
    text = "\n".join(code_lines(PI_DIR / "firstboot.sh"))
    assert "GADGET_CONF=/etc/modules-load.d/usb-gadget.conf" in text
    assert 'if [ ! -e "$GADGET_CONF" ]; then' in text
    assert "rpi-usb-gadget on -f" in text
    assert "ssh-keygen -A" in text
    assert "rfkill unblock wifi" in text
    assert 'install -d -m 0750 -o weatherpi -g weatherpi "$DATA_DIR/db"' in text
    assert 'install -d -m 0700 -o root -g root "$DATA_DIR/agent"' in text


def test_firstboot_read_only_root_steps():
    """D14: grow /data, and switch the overlay on only when /data and its binds are mounted."""
    text = "\n".join(code_lines(PI_DIR / "firstboot.sh"))
    assert 'OVERLAY_ARG="overlayroot=tmpfs:recurse=0"' in text  # never plain tmpfs (wipes /data)
    assert 'echo ", +" | sfdisk --no-reread --force -N "$data_num" "$data_disk"' in text
    assert 'resize2fs "$data_dev"' in text
    for target in ("/var/lib/weatherpi", "/var/backups/weatherpi", "/var/lib/tailscale",
                   "/etc/NetworkManager/system-connections", "/var/log/journal",
                   "/var/lib/systemd/timesync", "/home/main/.ssh"):
        assert target in text
    assert 'mountpoint -q "$t" || missing="$missing $t"' in text
    # the overlay goes on only after those checks, and the Pi reboots into it
    assert text.index('missing=""') < text.index('sed -i "1s|^|$OVERLAY_ARG |" "$CMDLINE"')


def _commit(bash, tmp_path, *files, assume=True):
    live, lower = tmp_path / "live", tmp_path / "lower"
    env = {"WEATHERPI_COMMIT_SRC": live.as_posix(), "WEATHERPI_OVERLAY_LOWER": lower.as_posix()}
    if assume:
        env["WEATHERPI_COMMIT_ASSUME_OVERLAY"] = "1"
    return run_bash(bash, (PI_DIR / "scripts/weatherpi-commit-etc.sh").as_posix(), *files,
                    env=env), live, lower


def test_commit_etc_copies_and_removes(bash, tmp_path):
    live, lower = tmp_path / "live", tmp_path / "lower"
    (live / "etc/ssh/sshd_config.d").mkdir(parents=True)
    (live / "etc/shadow").write_text("main:$y$new:20000::::::\n")
    (live / "etc/ssh/sshd_config.d/10-weatherpi-password.conf").write_text("PasswordAuthentication yes\n")
    (lower / "etc").mkdir(parents=True)
    (lower / "etc/shadow").write_text("main:!:19000::::::\n")
    (lower / "etc/timezone").write_text("Etc/UTC\n")  # gone from the live root: removed below
    res, _, _ = _commit(bash, tmp_path, "/etc/shadow",
                        "/etc/ssh/sshd_config.d/10-weatherpi-password.conf", "/etc/timezone")
    assert res.returncode == 0, res.stderr
    assert (lower / "etc/shadow").read_text() == "main:$y$new:20000::::::\n"
    assert (lower / "etc/ssh/sshd_config.d/10-weatherpi-password.conf").is_file()
    assert not (lower / "etc/timezone").exists()
    assert not list(lower.rglob("*.wp-new"))
    assert "committed /etc/shadow" in res.stdout and "removed /etc/timezone" in res.stdout


def test_commit_etc_only_under_etc(bash, tmp_path):
    for bad in ("/var/lib/weatherpi/db", "/etc/../root/x", "etc/shadow"):
        res, _, lower = _commit(bash, tmp_path, bad)
        assert res.returncode == 2, bad
        assert not lower.exists()
    res, _, _ = _commit(bash, tmp_path)
    assert res.returncode == 2 and "usage" in res.stderr


def test_commit_etc_does_nothing_without_overlay(bash, tmp_path):
    res, _, lower = _commit(bash, tmp_path, "/etc/shadow", assume=False)
    assert res.returncode == 0 and "overlay not active" in res.stdout
    assert not lower.exists()


def test_scripts_read_config_through_weatherpi_config():
    for name in ("backup.sh", "wifi_watchdog.sh"):
        text = (PI_DIR / "scripts" / name).read_text()
        assert 'PYTHONPATH="${WEATHERPI_CODE_DIR:-/opt/weatherpi}"' in text
        assert '-m weatherpi.config "$1"' in text
    assert ".backup '$tmp'" in (PI_DIR / "scripts/backup.sh").read_text()


def _backup_env(tmp_path: Path, db: Path, backups: Path) -> dict:
    root = tmp_path / "root"
    (root / "etc/weatherpi").mkdir(parents=True)
    (root / "etc/weatherpi/config.yaml").write_text(
        f'database: {{path: "{db.as_posix()}"}}\n'
        f'backup: {{backup_dir: "{backups.as_posix()}", retention_days: 7}}\n'
    )
    return {
        "WEATHERPI_ROOT": str(root),
        "WEATHERPI_CODE_DIR": str(PI_DIR),
        "WEATHERPI_PYTHON": Path(sys.executable).as_posix(),
    }


def test_backup_without_database_is_a_noop(bash, tmp_path):
    db, backups = tmp_path / "missing.db", tmp_path / "backups"
    result = run_bash(
        bash, (PI_DIR / "scripts/backup.sh").as_posix(), env=_backup_env(tmp_path, db, backups)
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "nothing to back up" in result.stdout
    assert not backups.exists()


@pytest.mark.skipif(shutil.which("sqlite3") is None, reason="sqlite3 CLI not available")
def test_backup_creates_snapshot_and_prunes_old(bash, tmp_path):
    db, backups = tmp_path / "weatherpi.db", tmp_path / "backups"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE rain_events (id INTEGER PRIMARY KEY, timestamp TEXT)")
        conn.execute("INSERT INTO rain_events (timestamp) VALUES ('2026-10-19T00:00:00')")
    backups.mkdir()
    old = backups / "weatherpi-20000101-000000.db"
    old.write_bytes(b"old")
    ten_days_ago = time.time() - 10 * 86400
    os.utime(old, (ten_days_ago, ten_days_ago))

    result = run_bash(
        bash, (PI_DIR / "scripts/backup.sh").as_posix(), env=_backup_env(tmp_path, db, backups)
    )
    assert result.returncode == 0, result.stdout + result.stderr
    made = sorted(backups.glob("weatherpi-*.db"))
    assert len(made) == 1 and made[0] != old
    with sqlite3.connect(made[0]) as conn:
        assert conn.execute("SELECT COUNT(*) FROM rain_events").fetchone()[0] == 1
    assert not list(backups.glob("*.partial"))
