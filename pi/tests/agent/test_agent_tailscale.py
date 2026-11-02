import time

import pytest
from agent_helpers import agent_of

from weatherpi.agent.fakes import AUTH_URL, FAKE_PASSWORD, SERVE_ADMIN_URL


@pytest.fixture
def online(claimed, fake):
    """Claimed and on WiFi (tailscale up needs internet)."""
    client, h, _ = claimed
    client.post("/v1/wifi/connect", headers=h, json={"ssid": "ExampleWiFi",
                                                     "password": FAKE_PASSWORD})
    agent_of(client).wifi.job.wait(10)
    return client, h


def _wait_for(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def _status(client, h):
    r = client.get("/v1/tailscale/status", headers=h)
    assert r.status_code == 200
    return r.json()


def test_status_needs_login(claimed):
    client, h, _ = claimed
    st = _status(client, h)
    assert st["installed"] is True
    assert st["version"] == "1.94.2"
    assert st["backend_state"] == "NeedsLogin"
    assert st["auth_url"] is None
    assert st["ip"] is None and st["serve_enabled"] is False and st["serve_url"] is None


def test_not_installed(claimed, fake):
    client, h, _ = claimed
    fake.tailscale_installed = False
    st = _status(client, h)
    assert st["installed"] is False and st["backend_state"] == "NoState"


def test_up_needs_internet(claimed):
    client, h, _ = claimed
    r = client.post("/v1/tailscale/up", headers=h, json={"hostname": "weatherpi"})
    assert r.status_code == 409 and r.json()["error"] == "no_internet"


def test_up_bad_hostname(online):
    client, h = online
    r = client.post("/v1/tailscale/up", headers=h, json={"hostname": "bad host!"})
    assert r.status_code == 400 and r.json()["error"] == "bad_request"


def test_full_login_and_serve_flow(online, fake):
    client, h = online
    r = client.post("/v1/tailscale/up", headers=h, json={"hostname": "weatherpi"})
    assert r.status_code == 202 and r.json() == {"job": "tailscale-up"}
    assert _wait_for(lambda: _status(client, h)["auth_url"] == AUTH_URL)
    st = _status(client, h)
    assert st["backend_state"] == "NeedsLogin" and st["up"]["state"] == "running"
    assert any(c[:2] == ["tailscale", "up"] and "--hostname=weatherpi" in c
               and "--ssh=false" in c for c in fake.calls)

    r = client.post("/v1/tailscale/up", headers=h, json={"hostname": "weatherpi"})
    assert r.status_code == 409 and r.json()["error"] == "busy"

    assert fake.approve()
    agent_of(client).tailscale.job.wait(5)
    st = _status(client, h)
    assert st["backend_state"] == "Running"
    assert st["auth_url"] is None
    assert st["ip"] == "100.94.2.31"
    assert st["dns_name"] == "weatherpi.example.ts.net"
    assert st["tailnet"] == "example.ts.net"
    assert st["tailnet_name"] == "owner@example.com"
    assert st["serve_enabled"] is False

    r = client.post("/v1/tailscale/serve", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["serve_enabled"] is True
    assert r.json()["serve_url"] == "https://weatherpi.example.ts.net"
    assert ["tailscale", "serve", "--bg", "--yes", "http://127.0.0.1:8080"] in fake.calls

    r = client.post("/v1/tailscale/logout", headers=h)
    assert r.status_code == 200
    st = _status(client, h)
    assert st["backend_state"] == "NeedsLogin" and st["serve_enabled"] is False


def test_auto_approve(online, fake):
    client, h = online
    fake.auto_approve_after = 0.1
    client.post("/v1/tailscale/up", headers=h, json={"hostname": "weatherpi"})
    agent_of(client).tailscale.job.wait(5)
    assert _status(client, h)["backend_state"] == "Running"


def test_serve_requires_running(claimed):
    client, h, _ = claimed
    r = client.post("/v1/tailscale/serve", headers=h)
    assert r.status_code == 409 and r.json()["error"] == "tailscale_not_running"


def test_serve_not_enabled_on_tailnet(claimed, fake):
    client, h, _ = claimed
    fake.set_online()
    fake.ts_serve = {}
    fake.serve_allowed = False
    r = client.post("/v1/tailscale/serve", headers=h)
    assert r.status_code == 409
    assert r.json()["error"] == "serve_not_enabled_on_tailnet"
    assert r.json()["admin_url"] == SERVE_ADMIN_URL
    st = _status(client, h)
    assert st["serve_error"] == "serve_not_enabled_on_tailnet"
    assert st["admin_url"] == SERVE_ADMIN_URL
    fake.serve_allowed = True
    assert client.post("/v1/tailscale/serve", headers=h).json()["serve_error"] is None


def test_up_when_already_running_is_quick(claimed, fake):
    client, h, _ = claimed
    fake.set_online()
    assert client.post("/v1/tailscale/up", headers=h, json={}).status_code == 202
    agent_of(client).tailscale.job.wait(5)
    assert _status(client, h)["up"]["state"] == "done"


def test_tailscale_needs_token(claimed):
    client, _, _ = claimed
    assert client.get("/v1/tailscale/status").status_code == 401
