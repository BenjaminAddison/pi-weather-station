from datetime import UTC, datetime, timedelta

from weatherpi import config, hw, paths
from weatherpi.db import Database


def test_config_defaults_and_cli_lookup(tmp_path):
    cfg = config.load_config(tmp_path / "missing.yaml")
    assert config.get_value("rain.mm_per_tip", cfg) == 0.2794
    assert config.get_value("agent.interfaces.0", cfg) == "usb0"


def test_config_file_overrides_merge(tmp_path):
    f = tmp_path / "c.yaml"
    f.write_text("rain:\n  gpio_pin: 27\n")
    cfg = config.load_config(f)
    assert cfg["rain"]["gpio_pin"] == 27
    assert cfg["rain"]["mm_per_tip"] == 0.2794


def test_db_roundtrip_and_backup(tmp_path):
    db = Database(tmp_path / "db" / "w.db")
    now = datetime.now(UTC)
    db.insert_temperature(now, 20.0)
    db.insert_rain(now)
    db.update_heartbeat(now)
    assert db.get_heartbeat() == now
    assert len(db.get_temperature_range(now - timedelta(minutes=1), now)) == 1
    assert db.row_counts() == {"temperature_readings": 1, "rain_events": 1}
    db.backup_to(tmp_path / "copy.db")
    assert Database(tmp_path / "copy.db").row_counts()["rain_events"] == 1


def test_fake_hardware():
    assert paths.FAKE
    assert hw.read_temperature() == 21.5
    tips = []
    sensor = hw.RainSensor(17, lambda: tips.append(1))
    sensor.simulate_tip()
    assert tips == [1]
    hw.led_blink("success")
    assert hw.FAKE_LED_EVENTS[-1] == "success"


def test_loaded_config_does_not_alias_defaults(tmp_path):
    cfg = config.load_config(tmp_path / "missing.yaml")
    cfg["web"]["port"] = 1
    assert config.DEFAULTS["web"]["port"] == 8080
