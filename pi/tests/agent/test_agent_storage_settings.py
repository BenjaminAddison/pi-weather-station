"""Agent 2.2.0: maintenance mode for the read-only root (D14) and settings (D26)."""

import time

import yaml

from weatherpi import config, paths

CMDLINE = "console=tty1 root=PARTUUID=4c032286-02 rootfstype=ext4 rootwait"


def _overlay_pi(root, on_next_boot=True, active=True):
    """A Pi with the overlayroot package; the overlay arg per cmdline.txt and /proc."""
    (root / "etc").mkdir(parents=True, exist_ok=True)
    (root / "etc/overlayroot.conf").write_text('overlayroot=""\n')
    (root / "boot/firmware").mkdir(parents=True, exist_ok=True)
    arg = "overlayroot=tmpfs:recurse=0 "
    (root / "boot/firmware/cmdline.txt").write_text((arg if on_next_boot else "") + CMDLINE + "\n")
    (root / "proc").mkdir(parents=True, exist_ok=True)
    (root / "proc/cmdline").write_text((arg if active else "") + CMDLINE + "\n")


def _wait_power(fake, verb="reboot", timeout=2.0):
    end = time.monotonic() + timeout
    while verb not in fake.power and time.monotonic() < end:
        time.sleep(0.01)
    return verb in fake.power


# ---------------------------------------------------------------- maintenance


def test_maintenance_404_without_overlay(claimed):
    client, h, _ = claimed
    r = client.get("/v1/system/maintenance", headers=h)
    assert r.status_code == 404 and r.json()["error"] == "not_found"


def test_maintenance_needs_token(claimed, root):
    client, _, _ = claimed
    _overlay_pi(root)
    assert client.get("/v1/system/maintenance").status_code == 401


def test_maintenance_state(claimed, root):
    client, h, _ = claimed
    _overlay_pi(root)
    assert client.get("/v1/system/maintenance", headers=h).json() == {
        "overlay_active": True, "overlay_on_next_boot": True, "maintenance": False}


def test_maintenance_on_then_off(claimed, root, fake):
    client, h, _ = claimed
    _overlay_pi(root)
    r = client.post("/v1/system/maintenance", headers=h, json={"enabled": True})
    assert r.status_code == 202
    assert r.json()["maintenance"] is True and r.json()["rebooting"] is True
    assert (root / "boot/firmware/cmdline.txt").read_text() == CMDLINE + "\n"
    assert _wait_power(fake)

    # after the reboot: writable root, overlay off
    _overlay_pi(root, on_next_boot=False, active=False)
    fake.power.clear()
    r = client.post("/v1/system/maintenance", headers=h, json={"enabled": False})
    assert r.status_code == 202
    assert (root / "boot/firmware/cmdline.txt").read_text() == (
        "overlayroot=tmpfs:recurse=0 " + CMDLINE + "\n")
    assert _wait_power(fake)


def test_maintenance_same_state_no_reboot(claimed, root, fake):
    client, h, _ = claimed
    _overlay_pi(root)
    r = client.post("/v1/system/maintenance", headers=h, json={"enabled": False})
    assert r.status_code == 200 and r.json()["maintenance"] is False
    time.sleep(0.05)
    assert fake.power == []


def test_maintenance_busy_while_wifi_connects(claimed, root, fake, monkeypatch):
    client, h, _ = claimed
    _overlay_pi(root)
    agent = client.app.state.agent
    monkeypatch.setattr(type(agent.wifi.job), "running", property(lambda self: True))
    r = client.post("/v1/system/maintenance", headers=h, json={"enabled": True})
    assert r.status_code == 409 and r.json()["error"] == "busy"
    assert "overlayroot=tmpfs:recurse=0" in (root / "boot/firmware/cmdline.txt").read_text()


def test_maintenance_body_must_be_bool(claimed, root):
    client, h, _ = claimed
    _overlay_pi(root)
    r = client.post("/v1/system/maintenance", headers=h, json={"enabled": "yes"})
    assert r.status_code == 400


def test_diagnostics_storage_with_overlay(claimed, root):
    client, h, _ = claimed
    _overlay_pi(root)
    st = client.get("/v1/diagnostics", headers=h).json()["storage"]
    assert st == {"overlay_supported": True, "data_mounted": False, "overlay_active": True,
                  "overlay_on_next_boot": True, "maintenance": False}


def test_claim_commits_etc_changes(claimed, fake):
    commits = [c[1:] for c in fake.calls if c[0] == "weatherpi-commit-etc"]
    assert ["/etc/shadow"] in commits
    assert ["/etc/localtime", "/etc/timezone"] in commits
    assert any(c[0].endswith("10-weatherpi-password.conf") for c in commits)


def test_password_change_commits_shadow(claimed, fake):
    client, h, _ = claimed
    fake.calls.clear()
    r = client.post("/v1/password", headers=h, json={"new_password": "another-good-one"})
    assert r.status_code == 200
    assert ["weatherpi-commit-etc", "/etc/shadow"] in fake.calls


# ---------------------------------------------------------------- settings


def test_settings_default(claimed):
    client, h, _ = claimed
    assert client.get("/v1/settings", headers=h).json() == {
        "record_interval_s": 300, "record_interval_choices": [60, 300, 600, 900, 1800, 3600]}


def test_settings_put_saves_and_restarts_logger(claimed, fake):
    client, h, _ = claimed
    fake.calls.clear()
    r = client.put("/v1/settings", headers=h, json={"record_interval_s": 1800})
    assert r.status_code == 200 and r.json()["record_interval_s"] == 1800
    assert yaml.safe_load(paths.SETTINGS_FILE.read_text()) == {"record_interval_s": 1800}
    assert ["systemctl", "restart", "weatherpi-logger"] in fake.calls
    assert client.get("/v1/settings", headers=h).json()["record_interval_s"] == 1800
    # what the logger reads at start
    temp = config.load_config()["temp"]
    assert temp["write_interval"] == 1800 and temp["poll_interval"] == 30


def test_settings_short_interval_polls_faster(claimed):
    client, h, _ = claimed
    client.put("/v1/settings", headers=h, json={"record_interval_s": 60})
    temp = config.load_config()["temp"]
    assert temp["write_interval"] == 60 and temp["poll_interval"] == 30


def test_settings_rejects_other_values_and_keys(claimed, fake):
    client, h, _ = claimed
    for body in ({"record_interval_s": 120}, {"record_interval_s": "300"},
                 {"record_interval_s": 300, "colour": "blue"}, {}):
        r = client.put("/v1/settings", headers=h, json=body)
        assert r.status_code == 400 and r.json()["error"] == "bad_request", body
    assert not paths.SETTINGS_FILE.exists()


def test_settings_need_token(claimed):
    client, _, _ = claimed
    assert client.get("/v1/settings").status_code == 401
    assert client.put("/v1/settings", json={"record_interval_s": 60}).status_code == 401


def test_bad_settings_file_is_ignored(root):
    paths.SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    paths.SETTINGS_FILE.write_text("record_interval_s: 7\nother: 1\n")
    assert config.load_settings() == {}
    paths.SETTINGS_FILE.write_text(": not yaml : [")
    assert config.load_config()["temp"]["write_interval"] == 300


def test_raspi_config_style_overlay_counts_as_on(claimed, root):
    client, h, _ = claimed
    _overlay_pi(root)
    (root / "boot/firmware/cmdline.txt").write_text("overlayroot=tmpfs " + CMDLINE + "\n")
    assert client.get("/v1/system/maintenance", headers=h).json()["maintenance"] is False
    r = client.post("/v1/system/maintenance", headers=h, json={"enabled": True})
    assert r.status_code == 202
    assert (root / "boot/firmware/cmdline.txt").read_text() == CMDLINE + "\n"


# ---------------------------------------------------------------- disk figures (D14)


def _fake_disks(monkeypatch, mounts: dict):
    """os.path.ismount / shutil.disk_usage answering per path: {path: total_mb or None}."""
    import collections
    import os

    from weatherpi.agent import system

    usage = collections.namedtuple("usage", "total used free")
    mounted = {os.path.normpath(str(p)) for p, mb in mounts.items() if mb is not None}

    def ismount(p):
        return os.path.normpath(str(p)) in mounted

    def disk_usage(p):
        mb = mounts.get(p, mounts.get(paths.ROOT))
        return usage(mb * 2**20, 2**20, (mb - 1) * 2**20)

    monkeypatch.setattr(system.os.path, "ismount", ismount)
    monkeypatch.setattr(system.shutil, "disk_usage", disk_usage)


def test_disk_is_the_data_partition_with_read_only_root(claimed, monkeypatch):
    client, h, _ = claimed
    _fake_disks(monkeypatch, {paths.ROOT: 213, paths.DATA_MOUNT: 26000,
                              paths.OVERLAY_LOWER: 2800})
    sysinfo = client.get("/v1/diagnostics", headers=h).json()["system"]
    assert sysinfo["disk"]["total_mb"] == 26000       # where the readings live
    assert sysinfo["root_disk"]["total_mb"] == 2800   # the system partition
    assert sysinfo["ram_layer"]["total_mb"] == 213    # "/" is the overlay's RAM layer


def test_disk_without_data_partition_is_the_root(claimed, monkeypatch):
    client, h, _ = claimed
    _fake_disks(monkeypatch, {paths.ROOT: 2800, paths.DATA_MOUNT: None,
                              paths.OVERLAY_LOWER: None})
    sysinfo = client.get("/v1/diagnostics", headers=h).json()["system"]
    assert sysinfo["disk"]["total_mb"] == 2800 and sysinfo["root_disk"]["total_mb"] == 2800
    assert "ram_layer" not in sysinfo
