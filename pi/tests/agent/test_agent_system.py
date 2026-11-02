import json
import time
from datetime import UTC, datetime, timedelta

from agent_helpers import RECOVERY

from weatherpi import hw, paths
from weatherpi.agent.system import REDACTED, redact
from weatherpi.db import Database


def _db() -> Database:
    return Database(paths.DB_FILE)


def test_sensors_live_without_db(claimed):
    client, h, _ = claimed
    live = client.get("/v1/sensors/live", headers=h).json()
    assert live["temperature"]["present"] is True
    assert live["temperature"]["celsius"] == 21.5
    assert live["temperature"]["sensor_id"] == "28-00000fake001"
    assert live["rain"] == {"tips_today": 0, "mm_today": 0.0, "last_tip_at": None,
                            "mm_per_tip": 0.2794}
    assert live["logger"] == {"alive": False, "last_heartbeat": None, "stale_seconds": None}
    assert not paths.DB_FILE.exists()  # the agent never creates the logger's DB


def test_sensors_live_with_data(claimed):
    client, h, _ = claimed
    db = _db()
    now = datetime.now(UTC)
    db.insert_rain(now - timedelta(days=3))  # not today
    db.insert_rain(now - timedelta(seconds=5))
    db.insert_rain(now)
    db.update_heartbeat(now - timedelta(seconds=12))
    db.close()
    live = client.get("/v1/sensors/live", headers=h).json()
    assert live["rain"]["tips_today"] == 2
    assert live["rain"]["mm_today"] == round(2 * 0.2794, 4)
    assert live["rain"]["last_tip_at"] == now.strftime("%Y-%m-%dT%H:%M:%SZ")
    assert live["logger"]["alive"] is True
    assert 11 <= live["logger"]["stale_seconds"] <= 15


def test_sensors_live_no_sensor(claimed):
    client, h, _ = claimed
    hw.FAKE_TEMP.present = False
    t = client.get("/v1/sensors/live", headers=h).json()["temperature"]
    assert t == {"present": False, "celsius": None, "sensor_id": None, "read_at": None}


def test_diagnostics_shape_and_redaction(claimed, root):
    client, h, body = claimed
    (root / "boot/firmware").mkdir(parents=True)
    (root / "boot/firmware/config.txt").write_text(
        "[all]\ndtoverlay=w1-gpio,gpiopin=9\n# dtoverlay=disabled\n"
        "dtoverlay=dwc2,dr_mode=peripheral     # same line rpi-usb-gadget writes\n"
    )
    r = client.get("/v1/diagnostics", headers=h)
    assert r.status_code == 200
    d = r.json()
    assert set(d) == {"collected_at", "system", "services", "journal", "network", "hardware",
                      "config", "storage"}
    assert d["storage"] == {"overlay_supported": False, "data_mounted": False}
    assert d["system"]["throttled"] == "0x0" and d["system"]["temp_c"] == 41.2
    assert d["system"]["undervoltage_seen"] is False
    names = {s["name"] for s in d["services"]}
    assert {"weatherpi-logger", "weatherpi-agent", "tailscaled"} <= names
    assert set(d["journal"]) == {"weatherpi-logger", "weatherpi-agent", "weatherpi-web",
                                 "NetworkManager", "tailscaled"}
    assert d["hardware"]["config_txt_overlays"] == ["w1-gpio,gpiopin=9",
                                                    "dwc2,dr_mode=peripheral"]
    assert d["hardware"]["gpio17_level"] == 1
    assert d["hardware"]["w1_devices"] == ["28-00000fake001"]
    assert d["config"]["effective"]["rain"]["mm_per_tip"] == 0.2794
    text = json.dumps(d)
    assert "tskey-auth-kFAKE1CNTRL" not in text and "tskey-[REDACTED]" in text
    assert "fake-psk-value" not in text
    assert body["api_token"] not in text and RECOVERY not in text
    token_digest = (paths.AGENT_STATE_DIR / "token.sha256").read_text().strip()
    assert token_digest not in text


def test_redact_unit():
    secret_hash = "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
    data = {
        "wifi": {"ssid": "ExampleWiFi", "psk": "hunter2-example", "password": "pw-example"},
        "api_token": "abc123token",
        "nested": [{"log": "wifi-sec.psk:   s3cret-example and more"},
                   "authkey=tskey-auth-k123ABC-xyz789 done",
                   'wpa_supplicant psk="quoted secret" ok',
                   "Authorization: Bearer abcdefghijklmnop",
                   f"stored {secret_hash} here",
                   "login at https://login.tailscale.com/a/1a2b3c4d5e please",
                   "owner owner@example.com signed in",
                   "PasswordAuthentication yes",
                   "Failed password for main from 10.12.194.2"],
        "count": 3,
        "ok": True,
        "none": None,
    }
    out = redact(data, [secret_hash])
    assert out["wifi"] == {"ssid": "ExampleWiFi", "psk": REDACTED, "password": REDACTED}
    assert out["api_token"] == REDACTED
    logs = out["nested"]
    assert "s3cret-example" not in logs[0]["log"]
    assert "tskey-auth" not in logs[1] and logs[1].endswith(" done")
    assert "quoted secret" not in logs[2] and logs[2].endswith(" ok")
    assert "abcdefghijklmnop" not in logs[3]
    assert secret_hash not in logs[4]
    assert "1a2b3c4d5e" not in logs[5]
    assert "owner@example.com" not in logs[6]
    assert logs[7] == "PasswordAuthentication yes"  # sshd settings stay readable
    assert logs[8] == "Failed password for main from 10.12.194.2"
    assert out["count"] == 3 and out["ok"] is True and out["none"] is None


def test_time_push(claimed, fake):
    client, h, _ = claimed
    r = client.post("/v1/system/time", headers=h, json={"utc": "2026-10-19T11:05:00Z"})
    assert r.status_code == 200 and r.json()["applied"] is True
    assert fake.clock_set_to == int(datetime(2026, 10, 19, 11, 5, tzinfo=UTC).timestamp())
    fake.clock_synced = True
    r = client.post("/v1/system/time", headers=h, json={"utc": "2026-10-19T11:06:00Z"})
    assert r.json() == {"applied": False, "clock_synced": True}
    assert client.get("/v1/info").json()["clock_synced"] is True


def test_time_bad(claimed):
    client, h, _ = claimed
    for utc in ("yesterday", "2026-10-19T11:05:00", "1999-01-01T00:00:00Z"):
        r = client.post("/v1/system/time", headers=h, json={"utc": utc})
        assert r.status_code == 400 and r.json()["error"] == "bad_request"


def test_reboot_and_shutdown(claimed, fake):
    client, h, _ = claimed
    assert client.post("/v1/system/reboot", headers=h).status_code == 202
    assert client.post("/v1/system/shutdown", headers=h).status_code == 202
    end = time.monotonic() + 3
    while len(fake.power) < 2 and time.monotonic() < end:
        time.sleep(0.02)
    assert sorted(fake.power) == ["poweroff", "reboot"]
    assert client.post("/v1/system/reboot").status_code == 401


def test_unknown_route_is_json(client):
    r = client.get("/v1/nope")
    assert r.status_code == 404 and r.json()["error"] == "not_found"
