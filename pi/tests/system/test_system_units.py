"""Static checks on pi/systemd/* (keys, users, ExecStart targets, install wiring)."""

import re
import shlex

import pytest
from systest_helpers import PI_DIR, SYSTEMD_DIR, parse_unit

EXPECTED_UNITS = {
    "weatherpi-logger.service",
    "weatherpi-web.service",
    "weatherpi-agent.service",
    "weatherpi-backup.service",
    "weatherpi-backup.timer",
    "weatherpi-watchdog.service",
    "weatherpi-firstboot.service",
}
PY_MODULES = {"weatherpi.logger", "weatherpi.web", "weatherpi.agent"}
FIRSTBOOT_DONE = "/var/lib/weatherpi/firstboot.done"


def unit(name: str) -> dict[str, dict[str, list[str]]]:
    return parse_unit(SYSTEMD_DIR / name)


def one(sections: dict, section: str, key: str) -> str:
    values = sections.get(section, {}).get(key)
    assert values, f"[{section}] {key}= missing"
    return values[-1]


def words(sections: dict, section: str, key: str) -> set[str]:
    return {w for v in sections.get(section, {}).get(key, []) for w in v.split()}


def env(sections: dict) -> dict[str, str]:
    out = {}
    for value in sections["Service"].get("Environment", []):
        for item in shlex.split(value):
            k, v = item.split("=", 1)
            out[k] = v
    return out


def unit_files() -> list[str]:
    return sorted(p.name for p in SYSTEMD_DIR.iterdir() if p.suffix in (".service", ".timer"))


def test_expected_units_exist():
    assert set(unit_files()) == EXPECTED_UNITS


@pytest.mark.parametrize("name", unit_files())
def test_unit_parses_with_description(name):
    sections = unit(name)
    assert one(sections, "Unit", "Description")
    if name.endswith(".service"):
        assert one(sections, "Service", "ExecStart")


@pytest.mark.parametrize("name", [n for n in unit_files() if n.endswith(".service")])
def test_exec_start_targets_exist(name):
    """python3 -m <known module>, or a script that install.sh ships from pi/."""
    argv = shlex.split(one(unit(name), "Service", "ExecStart"))
    if argv[0] == "/usr/bin/python3":
        assert argv[1] == "-m" and argv[2] in PY_MODULES, argv
        return
    assert argv[0].startswith("/opt/weatherpi/"), argv
    rel = argv[0].removeprefix("/opt/weatherpi/")
    assert (PI_DIR / rel).is_file(), f"{name}: {argv[0]} has no source in pi/{rel}"


def test_logger():
    s = unit("weatherpi-logger.service")
    assert one(s, "Service", "User") == "weatherpi"
    assert one(s, "Service", "Group") == "weatherpi"
    assert "gpio" in words(s, "Service", "SupplementaryGroups")
    e = env(s)
    assert e["PYTHONPATH"] == "/opt/weatherpi"
    assert e["GPIOZERO_PIN_FACTORY"] == "lgpio"
    assert one(s, "Service", "ExecStart") == "/usr/bin/python3 -m weatherpi.logger"
    assert one(s, "Service", "Restart") == "always"
    assert one(s, "Service", "ProtectSystem") == "strict"
    assert "/var/lib/weatherpi/db" in words(s, "Service", "ReadWritePaths")
    # lgpio needs a writable working dir; GPIO devices must stay visible.
    wd = one(s, "Service", "WorkingDirectory")
    assert wd == e["LG_WD"] == "/run/" + one(s, "Service", "RuntimeDirectory")
    for blocked in ("PrivateDevices", "ProtectClock", "DevicePolicy", "DeviceAllow"):
        assert blocked not in s["Service"], f"{blocked} would hide /dev/gpiochip*"
    assert one(s, "Install", "WantedBy") == "multi-user.target"


def test_web():
    s = unit("weatherpi-web.service")
    assert one(s, "Service", "User") == "weatherpi"
    assert "weatherpi-logger.service" in words(s, "Unit", "After")
    assert one(s, "Service", "ExecStart") == "/usr/bin/python3 -m weatherpi.web"
    assert env(s)["PYTHONPATH"] == "/opt/weatherpi"
    assert one(s, "Service", "ProtectSystem") == "strict"
    assert "/var/lib/weatherpi/db" in words(s, "Service", "ReadWritePaths")
    assert one(s, "Install", "WantedBy") == "multi-user.target"


def test_agent_runs_as_root_unsandboxed():
    s = unit("weatherpi-agent.service")
    assert s["Service"].get("User", ["root"])[-1] == "root"
    assert {"network.target", "NetworkManager.service"} <= words(s, "Unit", "After")
    assert one(s, "Service", "ExecStart") == "/usr/bin/python3 -m weatherpi.agent"
    assert one(s, "Service", "Restart") == "always"
    assert env(s)["PYTHONPATH"] == "/opt/weatherpi"
    # It writes /etc (sshd, NM, passwords): no read-only filesystem sandboxing.
    for key in ("ProtectSystem", "ReadOnlyPaths", "ProtectHome", "NoNewPrivileges"):
        assert key not in s["Service"]
    assert one(s, "Install", "WantedBy") == "multi-user.target"


def test_backup_service_and_timer():
    s = unit("weatherpi-backup.service")
    assert one(s, "Service", "Type") == "oneshot"
    assert s["Service"].get("User", ["root"])[-1] == "root"
    assert one(s, "Service", "ExecStart") == "/opt/weatherpi/scripts/backup.sh"
    assert "Install" not in s, "started by the timer only"

    t = unit("weatherpi-backup.timer")
    assert one(t, "Timer", "OnCalendar") == "daily"
    assert one(t, "Timer", "Persistent") == "true"
    assert one(t, "Timer", "Unit") == "weatherpi-backup.service"
    assert one(t, "Install", "WantedBy") == "timers.target"


def test_watchdog():
    s = unit("weatherpi-watchdog.service")
    assert s["Service"].get("User", ["root"])[-1] == "root"
    assert one(s, "Service", "ExecStart") == "/opt/weatherpi/scripts/wifi_watchdog.sh"
    assert one(s, "Service", "Restart") == "always"
    assert one(s, "Install", "WantedBy") == "multi-user.target"


def test_firstboot():
    s = unit("weatherpi-firstboot.service")
    assert one(s, "Unit", "ConditionPathExists") == "!" + FIRSTBOOT_DONE
    assert one(s, "Service", "Type") == "oneshot"
    assert "local-fs.target" in words(s, "Unit", "After")
    before = words(s, "Unit", "Before")
    assert {
        "weatherpi-logger.service",
        "weatherpi-web.service",
        "weatherpi-agent.service",
        "ssh.service",
    } <= before
    assert one(s, "Service", "ExecStart") == "/opt/weatherpi/firstboot.sh"
    assert "ProtectSystem" not in s["Service"]
    assert one(s, "Install", "WantedBy") == "multi-user.target"
    # The script must create exactly the file the condition checks.
    script = (PI_DIR / "firstboot.sh").read_text()
    assert "DATA_DIR=/var/lib/weatherpi\n" in script
    assert 'DONE_FILE="$DATA_DIR/firstboot.done"' in script
    assert 'touch "$DONE_FILE"' in script


def test_nftables_dropin_does_not_flush_ruleset():
    s = parse_unit(SYSTEMD_DIR / "nftables.service.d" / "weatherpi.conf")
    stops = s["Service"]["ExecStop"]
    assert stops[0] == "", "must reset Debian's ExecStop=nft flush ruleset first"
    assert all("flush ruleset" not in v for v in stops)
    assert stops[-1].lstrip("-") == "/usr/sbin/nft delete table inet weatherpi"


def test_install_enables_every_installable_unit():
    text = (PI_DIR / "install.sh").read_text()
    m = re.search(r"^UNITS=\((.*?)^\)", text, re.DOTALL | re.MULTILINE)
    assert m, "UNITS=( ... ) array not found in install.sh"
    listed = set(m.group(1).split())
    installable = {n for n in unit_files() if "Install" in unit(n)}
    assert listed == installable | {"nftables.service"}
