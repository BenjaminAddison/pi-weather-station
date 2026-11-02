import threading

from agent_helpers import agent_of

from weatherpi import hw, paths
from weatherpi.agent.fakes import FAKE_PASSWORD
from weatherpi.agent.wifi import connect_lock_file


def _connect(client, h, **body):
    r = client.post("/v1/wifi/connect", headers=h, json=body)
    assert r.status_code == 202, r.text
    assert r.json() == {"job": "wifi-connect"}
    agent_of(client).wifi.job.wait(10)
    return client.get("/v1/wifi/status", headers=h).json()


def test_scan_sorted_deduped_and_unblocks_radio(claimed, fake):
    client, h, _ = claimed
    assert fake.radio_soft_blocked  # stock Pi OS state
    r = client.get("/v1/wifi/scan", headers=h)
    assert r.status_code == 200
    nets = r.json()["networks"]
    assert not fake.radio_soft_blocked
    assert ["rfkill", "unblock", "wifi"] in fake.calls
    assert ["nmcli", "radio", "wifi", "on"] in fake.calls
    ssids = [n["ssid"] for n in nets]
    assert len(ssids) == len(set(ssids))  # de-duplicated
    assert "" not in ssids and "HiddenExample" not in ssids  # hidden ones aren't listed
    assert "Example:Colon" in ssids  # nmcli's \: escaping undone
    signals = [n["signal"] for n in nets]
    assert signals == sorted(signals, reverse=True)
    top = nets[0]
    assert top == {"ssid": "ExampleWiFi", "signal": 72, "security": "WPA2", "known": False,
                   "in_use": False}
    by = {n["ssid"]: n for n in nets}
    assert by["Cafe Free WiFi"]["security"] == "open"
    assert by["Neighbour-5G"]["security"] == "WPA2/WPA3"


def test_scan_radio_blocked(claimed, fake):
    client, h, _ = claimed
    fake.radio_hard_blocked = True
    assert client.get("/v1/wifi/scan", headers=h).json() == {"networks": []}
    assert client.get("/v1/wifi/status", headers=h).json()["last_error"] == "radio_blocked"
    fake.radio_hard_blocked = False
    assert client.get("/v1/wifi/scan", headers=h).json()["networks"]
    assert client.get("/v1/wifi/status", headers=h).json()["last_error"] is None


def test_status_initially_disconnected(claimed):
    client, h, _ = claimed
    st = client.get("/v1/wifi/status", headers=h).json()
    assert st == {"state": "disconnected", "ssid": None, "ip": None, "signal": None,
                  "internet": False, "last_error": None, "saved": []}


def test_connect_success(claimed, fake):
    client, h, _ = claimed
    st = _connect(client, h, ssid="ExampleWiFi", password=FAKE_PASSWORD)
    assert st["state"] == "connected"
    assert st["ssid"] == "ExampleWiFi"
    assert st["ip"] == "192.168.7.9"
    assert st["internet"] is True
    assert st["saved"] == ["ExampleWiFi"]
    assert st["last_error"] is None
    assert hw.FAKE_LED_EVENTS[-1] == "success"
    assert not connect_lock_file().exists()
    scan = client.get("/v1/wifi/scan", headers=h).json()["networks"]
    top = next(n for n in scan if n["ssid"] == "ExampleWiFi")
    assert top["known"] is True and top["in_use"] is True


def test_connect_lock_file_exists_while_connecting(claimed, fake):
    client, h, _ = claimed
    seen = {}
    real_up = fake._nm_up

    def spy(ref):
        seen["lock"] = connect_lock_file().exists()
        return real_up(ref)

    fake._nm_up = spy
    _connect(client, h, ssid="ExampleWiFi", password=FAKE_PASSWORD)
    assert seen["lock"] is True
    assert connect_lock_file() == paths.ROOT / "run/weatherpi/wifi-connect.lock"
    assert not connect_lock_file().exists()


def test_connect_wrong_password(claimed, fake):
    client, h, _ = claimed
    st = _connect(client, h, ssid="ExampleWiFi", password="wrong-horse")
    assert st["state"] == "failed"
    assert st["last_error"] == "wrong_password"
    assert st["saved"] == []  # the failed profile is cleaned up
    assert hw.FAKE_LED_EVENTS[-1] == "fail"
    assert not connect_lock_file().exists()


def test_connect_not_found(claimed):
    client, h, _ = claimed
    st = _connect(client, h, ssid="NoSuchNetwork", password=FAKE_PASSWORD)
    assert st["state"] == "failed" and st["last_error"] == "not_found"


def test_connect_hidden(claimed):
    client, h, _ = claimed
    st = _connect(client, h, ssid="HiddenExample", password=FAKE_PASSWORD, hidden=True)
    assert st["state"] == "connected" and st["ssid"] == "HiddenExample"


def test_connect_open_network(claimed):
    client, h, _ = claimed
    st = _connect(client, h, ssid="Cafe Free WiFi")
    assert st["state"] == "connected"


def test_connect_replaces_profile_and_keeps_old_on_failure(claimed, fake):
    client, h, _ = claimed
    _connect(client, h, ssid="ExampleWiFi", password=FAKE_PASSWORD)
    _connect(client, h, ssid="ExampleWiFi", password=FAKE_PASSWORD)
    assert [p.ssid for p in fake.profiles.values()] == ["ExampleWiFi"]
    assert [p.name for p in fake.profiles.values()] == ["ExampleWiFi"]
    # a wrong password for a second network leaves the working profile alone
    st = _connect(client, h, ssid="ExampleWiFi-Guest", password="wrong-horse")
    assert st["last_error"] == "wrong_password"
    assert st["saved"] == ["ExampleWiFi"]


def test_connect_bad_request(claimed):
    client, h, _ = claimed
    for body in ({"ssid": ""}, {"ssid": "x" * 33}, {"ssid": "ExampleWiFi", "password": "short"}):
        r = client.post("/v1/wifi/connect", headers=h, json=body)
        assert r.status_code == 400 and r.json()["error"] == "bad_request"


def test_connect_busy(claimed, fake):
    client, h, _ = claimed
    gate = threading.Event()
    real_up = fake._nm_up

    def slow_up(ref):
        gate.wait(5)
        return real_up(ref)

    fake._nm_up = slow_up
    body = {"ssid": "ExampleWiFi", "password": FAKE_PASSWORD}
    assert client.post("/v1/wifi/connect", headers=h, json=body).status_code == 202
    r = client.post("/v1/wifi/connect", headers=h, json=body)
    assert r.status_code == 409 and r.json()["error"] == "busy"
    assert client.get("/v1/wifi/scan", headers=h).json()["error"] == "busy"
    st = client.get("/v1/wifi/status", headers=h).json()
    assert st["state"] == "connecting" and st["ssid"] == "ExampleWiFi"
    gate.set()
    agent_of(client).wifi.job.wait(10)
    assert client.get("/v1/wifi/status", headers=h).json()["state"] == "connected"


def test_delete_saved(claimed):
    client, h, _ = claimed
    _connect(client, h, ssid="ExampleWiFi", password=FAKE_PASSWORD)
    r = client.delete("/v1/wifi/saved/ExampleWiFi", headers=h)
    assert r.status_code == 200
    st = client.get("/v1/wifi/status", headers=h).json()
    assert st["saved"] == [] and st["state"] == "disconnected"
    r = client.delete("/v1/wifi/saved/ExampleWiFi", headers=h)
    assert r.status_code == 404 and r.json()["error"] == "not_found"


def test_wifi_works_over_tailscale(claimed, make_client):
    _, h, _ = claimed
    c = make_client(via="tailscale")
    assert c.get("/v1/wifi/status", headers=h).status_code == 200
