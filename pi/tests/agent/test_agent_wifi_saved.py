"""Several saved networks (agent 2.1.0): GET/POST/PATCH /v1/wifi/saved, connect priority."""

import threading

import pytest
from agent_helpers import agent_of

from weatherpi.agent import AGENT_VERSION
from weatherpi.agent.fakes import FAKE_PASSWORD, Result
from weatherpi.agent.wifi import connect_lock_file, parse_props

PHONE = "Example Phone"  # in the fake world but out of range


def _connect(client, h, **body):
    r = client.post("/v1/wifi/connect", headers=h, json=body)
    assert r.status_code == 202, r.text
    agent_of(client).wifi.job.wait(10)
    return client.get("/v1/wifi/status", headers=h).json()


def _save(client, h, **body):
    r = client.post("/v1/wifi/saved", headers=h, json=body)
    assert r.status_code == 201, r.text
    assert r.json() == {"ssid": body["ssid"]}


def _saved(client, h) -> list[dict]:
    r = client.get("/v1/wifi/saved", headers=h)
    assert r.status_code == 200, r.text
    return r.json()["networks"]


def _profiles(fake, ssid):
    return [p for p in fake.profiles.values() if p.ssid == ssid]


def _up_calls(fake) -> int:
    return sum(1 for c in fake.calls if c[:1] == ["nmcli"] and "up" in c)


def test_agent_version_is_2_1_0(client):
    assert AGENT_VERSION == "2.2.0"
    assert client.get("/v1/info").json()["agent_version"] == "2.2.0"


def test_saved_empty_and_needs_token(claimed, client):
    _, h, _ = claimed
    assert _saved(client, h) == []
    for method, path in (("GET", "/v1/wifi/saved"), ("POST", "/v1/wifi/saved"),
                         ("PATCH", "/v1/wifi/saved/ExampleWiFi")):
        r = client.request(method, path, json={"ssid": "x", "priority": 1})
        assert r.status_code == 401 and r.json()["error"] == "unauthorized"


def test_save_out_of_range_network_without_connecting(claimed, fake):
    client, h, _ = claimed
    st = _connect(client, h, ssid="ExampleWiFi", password=FAKE_PASSWORD)
    active_before, ups_before = fake.active_uuid, _up_calls(fake)
    _save(client, h, ssid=PHONE, password=FAKE_PASSWORD, priority=5)
    # the current connection is untouched: same profile up, no `connection up` ran
    assert fake.active_uuid == active_before and _up_calls(fake) == ups_before
    assert not connect_lock_file().exists()
    st = client.get("/v1/wifi/status", headers=h).json()
    assert st["state"] == "connected" and st["ssid"] == "ExampleWiFi"
    assert sorted(st["saved"]) == [PHONE, "ExampleWiFi"]
    assert _saved(client, h) == [
        {"ssid": PHONE, "priority": 5, "hidden": False, "in_use": False},
        {"ssid": "ExampleWiFi", "priority": 0, "hidden": False, "in_use": True},
    ]
    (prof,) = _profiles(fake, PHONE)
    assert prof.name == PHONE and prof.psk == FAKE_PASSWORD and prof.key_mgmt == "wpa-psk"
    assert prof.autoconnect and prof.priority == 5
    # scan marks it known once it's in range
    fake.set_in_range(PHONE, True)
    scan = client.get("/v1/wifi/scan", headers=h).json()["networks"]
    assert next(n for n in scan if n["ssid"] == PHONE)["known"] is True


def test_saved_sorted_by_priority_then_ssid(claimed):
    client, h, _ = claimed
    for ssid, prio in (("bravo", 0), ("Alpha", 0), ("charlie", 10), ("delta", -5),
                       ("Echo", 10)):
        _save(client, h, ssid=ssid, password=FAKE_PASSWORD, priority=prio)
    nets = _saved(client, h)
    assert [(n["ssid"], n["priority"]) for n in nets] == [
        ("charlie", 10), ("Echo", 10), ("Alpha", 0), ("bravo", 0), ("delta", -5)]


def test_save_replaces_same_ssid(claimed, fake):
    client, h, _ = claimed
    _save(client, h, ssid=PHONE, password="first-password", priority=1)
    _save(client, h, ssid=PHONE, password=FAKE_PASSWORD, priority=3, hidden=True)
    (prof,) = _profiles(fake, PHONE)
    assert prof.psk == FAKE_PASSWORD and prof.priority == 3 and prof.hidden
    assert _saved(client, h) == [{"ssid": PHONE, "priority": 3, "hidden": True,
                                  "in_use": False}]


def test_save_replaces_extra_profiles_made_by_hand(claimed, fake):
    client, h, _ = claimed
    for name in ("one", "two"):
        fake.run(["nmcli", "connection", "add", "type", "wifi", "con-name", name,
                  "ssid", PHONE, "connection.autoconnect-priority", "9"])
    assert _saved(client, h) == [{"ssid": PHONE, "priority": 9, "hidden": False,
                                  "in_use": False}]  # merged into one entry
    _save(client, h, ssid=PHONE, password=FAKE_PASSWORD, priority=2)
    (prof,) = _profiles(fake, PHONE)
    assert prof.priority == 2 and prof.name == PHONE


def test_save_in_use_network_updates_in_place(claimed, fake):
    client, h, _ = claimed
    _connect(client, h, ssid="ExampleWiFi", password=FAKE_PASSWORD)
    active, ups = fake.active_uuid, _up_calls(fake)
    _save(client, h, ssid="ExampleWiFi", password="a-new-password", priority=7)
    (prof,) = _profiles(fake, "ExampleWiFi")
    assert prof.uuid == active == fake.active_uuid  # still connected on the same profile
    assert _up_calls(fake) == ups
    assert prof.psk == "a-new-password" and prof.priority == 7 and prof.key_mgmt == "wpa-psk"
    assert _saved(client, h) == [{"ssid": "ExampleWiFi", "priority": 7, "hidden": False,
                                  "in_use": True}]
    assert client.get("/v1/wifi/status", headers=h).json()["state"] == "connected"


def test_save_in_use_keeps_wpa3_key_mgmt(claimed, fake):
    client, h, _ = claimed
    fake.set_in_range("ExampleWPA3", True, security="WPA3")
    _connect(client, h, ssid="ExampleWPA3", password=FAKE_PASSWORD)
    (prof,) = _profiles(fake, "ExampleWPA3")
    assert prof.key_mgmt == "sae"
    _save(client, h, ssid="ExampleWPA3", password="another-password")
    assert prof.key_mgmt == "sae" and prof.psk == "another-password"


def test_save_open_network(claimed, fake):
    client, h, _ = claimed
    _save(client, h, ssid="Library WiFi")  # password missing
    _save(client, h, ssid="Bus WiFi", password=None)  # password null
    _save(client, h, ssid="Train WiFi", password="")  # empty = open
    for ssid in ("Library WiFi", "Bus WiFi", "Train WiFi"):
        (prof,) = _profiles(fake, ssid)
        assert prof.key_mgmt is None and prof.psk is None
    # a password for a network the Pi can see is open is ignored (like wifi/connect)
    client.get("/v1/wifi/scan", headers=h)
    _save(client, h, ssid="Cafe Free WiFi", password="not-needed-here")
    (prof,) = _profiles(fake, "Cafe Free WiFi")
    assert prof.key_mgmt is None and prof.psk is None


def test_save_in_use_as_open_removes_security(claimed, fake):
    client, h, _ = claimed
    _connect(client, h, ssid="ExampleWiFi", password=FAKE_PASSWORD)
    _save(client, h, ssid="ExampleWiFi", password=None)
    (prof,) = _profiles(fake, "ExampleWiFi")
    assert prof.key_mgmt is None and prof.uuid == fake.active_uuid


def test_save_hidden_network(claimed, fake):
    client, h, _ = claimed
    _save(client, h, ssid="HiddenExample", password=FAKE_PASSWORD, hidden=True, priority=-3)
    assert _saved(client, h) == [{"ssid": "HiddenExample", "priority": -3, "hidden": True,
                                  "in_use": False}]


@pytest.mark.parametrize("priority", [101, -101, 1000, "5", 1.5, True, None])
def test_save_priority_validation(claimed, fake, priority):
    client, h, _ = claimed
    r = client.post("/v1/wifi/saved", headers=h,
                    json={"ssid": PHONE, "password": FAKE_PASSWORD, "priority": priority})
    assert r.status_code == 400 and r.json()["error"] == "bad_request", r.text
    assert _profiles(fake, PHONE) == []


@pytest.mark.parametrize("priority", [-100, 100])
def test_save_priority_limits_ok(claimed, priority):
    client, h, _ = claimed
    _save(client, h, ssid=PHONE, password=FAKE_PASSWORD, priority=priority)
    assert _saved(client, h)[0]["priority"] == priority


@pytest.mark.parametrize("body", [
    {"ssid": PHONE, "password": "short"},
    {"ssid": PHONE, "password": "x" * 64},  # 64 chars must be hex
    {"ssid": PHONE, "password": "bad\npassword"},
    {"ssid": ""},
    {"ssid": "x" * 33, "password": FAKE_PASSWORD},
    {"password": FAKE_PASSWORD},
])
def test_save_bad_request(claimed, fake, body):
    client, h, _ = claimed
    r = client.post("/v1/wifi/saved", headers=h, json=body)
    assert r.status_code == 400 and r.json()["error"] == "bad_request", r.text
    assert fake.profiles == {}


def test_save_hex_psk_ok(claimed, fake):
    client, h, _ = claimed
    _save(client, h, ssid=PHONE, password="0123456789abcdef" * 4)
    assert _profiles(fake, PHONE)[0].psk == "0123456789abcdef" * 4


def test_save_failure_keeps_old_profile(claimed, fake):
    client, h, _ = claimed
    _save(client, h, ssid=PHONE, password=FAKE_PASSWORD, priority=4)
    fake._nm_add = lambda args: Result(4, "", "Error: failed to add connection")
    r = client.post("/v1/wifi/saved", headers=h, json={"ssid": PHONE, "password": "other-pass"})
    assert r.status_code == 500 and r.json()["error"] == "internal"
    (prof,) = _profiles(fake, PHONE)
    assert prof.psk == FAKE_PASSWORD and prof.priority == 4


def test_patch_priority(claimed, fake):
    client, h, _ = claimed
    _save(client, h, ssid=PHONE, password=FAKE_PASSWORD, priority=1, hidden=True)
    r = client.patch(f"/v1/wifi/saved/{PHONE}", headers=h, json={"priority": -20})
    assert r.status_code == 200 and r.json() == {"ssid": PHONE, "priority": -20}
    (prof,) = _profiles(fake, PHONE)
    # only the priority changed
    assert prof.priority == -20 and prof.psk == FAKE_PASSWORD and prof.hidden
    assert prof.autoconnect and prof.key_mgmt == "wpa-psk"


def test_patch_404_and_validation(claimed):
    client, h, _ = claimed
    r = client.patch("/v1/wifi/saved/NoSuchNetwork", headers=h, json={"priority": 1})
    assert r.status_code == 404 and r.json()["error"] == "not_found"
    _save(client, h, ssid=PHONE, password=FAKE_PASSWORD)
    for body in ({"priority": 101}, {"priority": "1"}, {}, {"priority": None}):
        r = client.patch(f"/v1/wifi/saved/{PHONE}", headers=h, json=body)
        assert r.status_code == 400 and r.json()["error"] == "bad_request", body


def test_patch_and_delete_ssid_with_slash(claimed, fake):
    client, h, _ = claimed
    odd = "Home 2.4/5G:x"
    _save(client, h, ssid=odd, password=FAKE_PASSWORD)
    assert _saved(client, h)[0]["ssid"] == odd  # nmcli \: escaping undone
    r = client.patch("/v1/wifi/saved/Home%202.4%2F5G%3Ax", headers=h, json={"priority": 3})
    assert r.status_code == 200, r.text
    assert _profiles(fake, odd)[0].priority == 3
    r = client.delete("/v1/wifi/saved/Home%202.4%2F5G%3Ax", headers=h)
    assert r.status_code == 200 and _profiles(fake, odd) == []


def test_gadget_profiles_never_listed(claimed, fake):
    client, h, _ = claimed
    out = fake.run(["nmcli", "-t", "-f", "UUID,TYPE,AUTOCONNECT-PRIORITY,ACTIVE,NAME",
                    "connection", "show"]).stdout
    assert "USB Gadget (shared)" in out and "802-3-ethernet" in out  # the fake has them
    assert _saved(client, h) == []
    _save(client, h, ssid=PHONE, password=FAKE_PASSWORD)
    assert [n["ssid"] for n in _saved(client, h)] == [PHONE]
    assert client.get("/v1/wifi/status", headers=h).json()["saved"] == [PHONE]
    r = client.patch("/v1/wifi/saved/USB Gadget (shared)", headers=h, json={"priority": 1})
    assert r.status_code == 404
    r = client.delete("/v1/wifi/saved/USB Gadget (shared)", headers=h)
    assert r.status_code == 404


def test_busy_while_connecting(claimed, fake):
    client, h, _ = claimed
    _save(client, h, ssid=PHONE, password=FAKE_PASSWORD)
    gate = threading.Event()
    real_up = fake._nm_up

    def slow_up(ref):
        gate.wait(5)
        return real_up(ref)

    fake._nm_up = slow_up
    r = client.post("/v1/wifi/connect", headers=h,
                    json={"ssid": "ExampleWiFi", "password": FAKE_PASSWORD})
    assert r.status_code == 202
    try:
        for method, path, body in (
            ("POST", "/v1/wifi/saved", {"ssid": "Other", "password": FAKE_PASSWORD}),
            ("PATCH", f"/v1/wifi/saved/{PHONE}", {"priority": 9}),
            ("DELETE", f"/v1/wifi/saved/{PHONE}", None),
        ):
            r = client.request(method, path, headers=h, json=body)
            assert r.status_code == 409 and r.json()["error"] == "busy", (method, r.text)
        # listing still works and hides the in-progress temporary profile
        assert [n["ssid"] for n in _saved(client, h)] == [PHONE]
    finally:
        gate.set()
        agent_of(client).wifi.job.wait(10)
    assert _profiles(fake, PHONE)[0].priority == 0
    assert sorted(n["ssid"] for n in _saved(client, h)) == [PHONE, "ExampleWiFi"]


def test_edit_lock_serialises_connect_start(claimed, fake):
    """A connect can't start in the middle of a save: it waits for the save to finish."""
    client, _, _ = claimed
    wifi = agent_of(client).wifi
    entered, release = threading.Event(), threading.Event()
    real_add = fake._nm_add

    def slow_add(args):
        entered.set()
        release.wait(5)
        return real_add(args)

    fake._nm_add = slow_add
    t = threading.Thread(target=wifi.save, args=(PHONE, FAKE_PASSWORD, False, 1))
    t.start()
    assert entered.wait(5)
    started = threading.Event()

    def connect():
        wifi.start_connect("ExampleWiFi", FAKE_PASSWORD, False)
        started.set()

    threading.Thread(target=connect).start()
    assert not started.wait(0.2)  # blocked behind the save
    fake._nm_add = real_add
    release.set()
    t.join(5)
    assert started.wait(5)
    wifi.job.wait(10)
    assert {p.ssid for p in fake.profiles.values()} == {PHONE, "ExampleWiFi"}


def test_connect_priority_sets_and_keeps(claimed, fake):
    client, h, _ = claimed
    # new profile without a priority -> 0
    _connect(client, h, ssid="ExampleWiFi", password=FAKE_PASSWORD)
    assert _profiles(fake, "ExampleWiFi")[0].priority == 0
    # with a priority -> set
    _connect(client, h, ssid="ExampleWiFi", password=FAKE_PASSWORD, priority=12)
    (prof,) = _profiles(fake, "ExampleWiFi")
    assert prof.priority == 12 and prof.autoconnect
    # without -> the existing profile's priority is kept
    _connect(client, h, ssid="ExampleWiFi", password=FAKE_PASSWORD)
    (prof,) = _profiles(fake, "ExampleWiFi")
    assert prof.priority == 12
    assert _saved(client, h) == [{"ssid": "ExampleWiFi", "priority": 12, "hidden": False,
                                  "in_use": True}]
    # a saved (never connected) network keeps its priority when it's joined later
    fake.set_in_range(PHONE, True)
    _save(client, h, ssid=PHONE, password=FAKE_PASSWORD, priority=-7)
    st = _connect(client, h, ssid=PHONE, password=FAKE_PASSWORD)
    assert st["state"] == "connected" and st["ssid"] == PHONE
    (prof,) = _profiles(fake, PHONE)
    assert prof.priority == -7 and prof.name == PHONE


def test_connect_failure_keeps_saved_priority(claimed, fake):
    client, h, _ = claimed
    _connect(client, h, ssid="ExampleWiFi", password=FAKE_PASSWORD, priority=8)
    st = _connect(client, h, ssid="ExampleWiFi", password="wrong-horse", priority=1)
    assert st["last_error"] == "wrong_password"
    (prof,) = _profiles(fake, "ExampleWiFi")
    assert prof.priority == 8


@pytest.mark.parametrize("priority", [101, -101, "3", 2.5])
def test_connect_priority_validation(claimed, priority):
    client, h, _ = claimed
    r = client.post("/v1/wifi/connect", headers=h,
                    json={"ssid": "ExampleWiFi", "password": FAKE_PASSWORD,
                          "priority": priority})
    assert r.status_code == 400 and r.json()["error"] == "bad_request"


def test_connect_priority_null_is_left_out(claimed, fake):
    client, h, _ = claimed
    st = _connect(client, h, ssid="ExampleWiFi", password=FAKE_PASSWORD, priority=None)
    assert st["state"] == "connected" and _profiles(fake, "ExampleWiFi")[0].priority == 0


def test_fake_autoconnect_picks_highest_priority(claimed, fake):
    """The fake NM joins the best saved network in range."""
    client, h, _ = claimed
    client.get("/v1/wifi/scan", headers=h)  # unblocks the radio
    _save(client, h, ssid="ExampleWiFi", password=FAKE_PASSWORD, priority=1)
    _save(client, h, ssid=PHONE, password=FAKE_PASSWORD, priority=5)
    assert fake.autoconnect() == "ExampleWiFi"  # the phone is away
    assert fake.set_in_range("ExampleWiFi", False) is None  # left home, phone still away
    assert fake.set_in_range(PHONE, True) == PHONE
    st = client.get("/v1/wifi/status", headers=h).json()
    assert st["state"] == "connected" and st["ssid"] == PHONE
    in_use = {n["ssid"]: n["in_use"] for n in _saved(client, h)}
    assert in_use == {PHONE: True, "ExampleWiFi": False}


def test_parse_props():
    out = "802-11-wireless.ssid:Home\\:Net\n802-11-wireless.hidden:no\n\n"
    assert parse_props(out) == {"802-11-wireless.ssid": "Home:Net",
                                "802-11-wireless.hidden": "no"}
    assert parse_props("") == {}
