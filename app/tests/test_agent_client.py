from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

import httpx
import pytest

from weatherpi_companion import agent_client as ac
from weatherpi_companion.agent_client import AgentClient, AgentError, AgentUnreachable

from .fakes import FakeAgent, make_backup_bytes

TOKEN = "tok-" + "x" * 40


def claimed_agent() -> FakeAgent:
    return FakeAgent(claimed=True, token_sha=hashlib.sha256(TOKEN.encode()).hexdigest(),
                     recovery_password="r" * 25)


def usb(agent: FakeAgent, token: str | None = TOKEN) -> AgentClient:
    return ac.for_usb(token, transport=agent.transport())


def test_factories():
    assert ac.for_usb().base_url == "http://10.12.194.1:8765"
    assert ac.for_usb().via == "usb"
    c = ac.for_tailscale("weatherpi.example.ts.net.")
    assert c.base_url == "http://weatherpi.example.ts.net:8765"
    assert c.via == "tailscale"
    assert ac.for_tailscale("100.94.2.31").base_url == "http://100.94.2.31:8765"
    assert ac.for_tailscale("fd7a::1").base_url == "http://[fd7a::1]:8765"


def test_info_no_auth_header(agent):
    info = usb(agent, token=TOKEN).info()
    assert info.serial == "00000000abcd1234"
    assert info.claimed is False
    assert info.via == "usb"
    assert info.agent_major == 2 and info.compatible
    assert "authorization" not in agent.requests[0].headers


def test_info_incompatible_major():
    info = ac.AgentInfo.from_json({"agent_version": "3.1.0"})
    assert info.agent_major == 3 and not info.compatible
    assert ac.AgentInfo.from_json({}).agent_major is None


def test_claim_sends_body_and_token_hash(agent):
    c = usb(agent, token=None)
    out = c.claim("goodpassword", "r" * 25, "ssh-ed25519 AAAA test", TOKEN, owner_name="Alex",
                  timezone="Africa/Johannesburg")
    assert out == {"claimed": True}
    assert agent.claimed and agent.timezone == "Africa/Johannesburg"
    assert agent.token_sha == hashlib.sha256(TOKEN.encode()).hexdigest()
    body = json.loads(agent.requests[-1].content)
    assert set(body) == {"password", "recovery_password", "ssh_pubkey", "api_token",
                         "owner_name", "timezone"}


def test_claim_errors(agent):
    c = usb(agent, token=None)
    with pytest.raises(AgentError) as ei:
        c.claim("short", "r" * 25, "k", TOKEN)
    assert ei.value.code == "weak_password" and ei.value.status == 400
    c.claim("goodpassword", "r" * 25, "k", TOKEN)
    with pytest.raises(AgentError) as ei:
        c.claim("goodpassword", "r" * 25, "k", TOKEN)
    assert ei.value.code == "already_claimed" and ei.value.status == 409


def test_claim_usb_only_over_tailscale(agent):
    c = ac.for_tailscale("100.94.2.31", transport=agent.transport())
    with pytest.raises(AgentError) as ei:
        c.claim("goodpassword", "r" * 25, "k", TOKEN)
    assert ei.value.code == "usb_only" and ei.value.status == 403


def test_bearer_token_and_unauthorized():
    agent = claimed_agent()
    assert usb(agent).wifi_status().state == "disconnected"
    assert agent.requests[-1].headers["authorization"] == f"Bearer {TOKEN}"
    with pytest.raises(AgentError) as ei:
        usb(agent, token="wrong").wifi_status()
    assert ei.value.code == "unauthorized" and ei.value.status == 401


def test_error_without_json_body():
    def handler(req):
        return httpx.Response(500, text="boom")

    c = AgentClient("http://x:8765", transport=httpx.MockTransport(handler))
    with pytest.raises(AgentError) as ei:
        c.info()
    assert ei.value.code == "internal" and ei.value.status == 500


def test_unreachable_and_timeout():
    def refuse(req):
        raise httpx.ConnectError("refused", request=req)

    def slow(req):
        raise httpx.ReadTimeout("slow", request=req)

    with pytest.raises(AgentUnreachable) as ei:
        AgentClient("http://x:8765", transport=httpx.MockTransport(refuse)).info()
    assert ei.value.code == "unreachable" and ei.value.status == 0
    assert isinstance(ei.value, AgentError)
    with pytest.raises(AgentUnreachable) as ei:
        AgentClient("http://x:8765", transport=httpx.MockTransport(slow)).info()
    assert ei.value.code == "timeout"


def test_wifi_endpoints():
    agent = claimed_agent()
    c = usb(agent)
    nets = c.wifi_scan()
    assert [n.ssid for n in nets] == ["ExampleWiFi", "Neighbour"]
    assert nets[0].secured and nets[0].bars == 3 and nets[1].bars == 1
    assert c.wifi_connect("ExampleWiFi", "goodpass1") == {"job": "wifi-connect"}
    assert c.wifi_status().state == "connecting"
    st = c.wifi_status()
    assert st.state == "connected" and st.internet and st.saved == ["ExampleWiFi"]
    c.wifi_forget("ExampleWiFi")
    assert agent.requests[-1].url.raw_path == b"/v1/wifi/saved/ExampleWiFi"
    with pytest.raises(AgentError) as ei:
        c.wifi_forget("Odd/SSID name")
    assert ei.value.status == 404
    assert agent.requests[-1].url.raw_path == b"/v1/wifi/saved/Odd%2FSSID%20name"


def test_wifi_saved_endpoints():
    agent = claimed_agent()
    c = usb(agent)
    assert c.wifi_saved() == []
    assert c.wifi_save("Example Phone", "goodpass1", priority=5) == {"ssid": "Example Phone"}
    assert json.loads(agent.requests[-1].content) == {
        "ssid": "Example Phone", "password": "goodpass1", "hidden": False, "priority": 5}
    c.wifi_save("Cafe", None, hidden=True)
    assert json.loads(agent.requests[-1].content) == {
        "ssid": "Cafe", "password": None, "hidden": True, "priority": 0}
    c.wifi_save("Library", "")  # empty password = open network, sent as null
    assert json.loads(agent.requests[-1].content)["password"] is None
    nets = c.wifi_saved()
    assert nets[0] == ac.SavedNetwork("Example Phone", 5, False, False)
    assert [n.ssid for n in nets] == ["Example Phone", "Cafe", "Library"]
    assert nets[1].hidden
    with pytest.raises(AttributeError):
        nets[0].priority = 1  # frozen
    agent.saved.append("Odd/SSID name")
    assert c.wifi_set_priority("Odd/SSID name", -3) == {"ssid": "Odd/SSID name",
                                                         "priority": -3}
    req = agent.requests[-1]
    assert req.method == "PATCH" and req.url.raw_path == b"/v1/wifi/saved/Odd%2FSSID%20name"
    assert json.loads(req.content) == {"priority": -3}
    with pytest.raises(AgentError) as ei:
        c.wifi_set_priority("Nope", 1)
    assert ei.value.status == 404 and ei.value.code == "not_found"
    with pytest.raises(AgentError) as ei:
        c.wifi_save("Example Phone", "goodpass1", priority=101)
    assert ei.value.status == 400 and ei.value.code == "bad_request"


def test_wifi_saved_busy_while_connecting():
    agent = claimed_agent()
    c = usb(agent)
    c.wifi_connect("ExampleWiFi", "goodpass1")
    with pytest.raises(AgentError) as ei:
        c.wifi_save("Example Phone", "goodpass1")
    assert ei.value.code == "busy" and ei.value.status == 409


def test_wifi_connect_priority_only_when_given():
    agent = claimed_agent()
    c = usb(agent)
    c.wifi_connect("ExampleWiFi", "goodpass1")
    assert "priority" not in json.loads(agent.requests[-1].content)
    c.wifi_connect("ExampleWiFi", "goodpass1", priority=0)
    assert json.loads(agent.requests[-1].content)["priority"] == 0
    c.wifi_connect("ExampleWiFi", "goodpass1", hidden=True, priority=7)
    assert json.loads(agent.requests[-1].content) == {
        "ssid": "ExampleWiFi", "password": "goodpass1", "hidden": True, "priority": 7}


def test_saved_network_from_json_defaults():
    n = ac.SavedNetwork.from_json({"ssid": "X", "priority": "bad"})
    assert n == ac.SavedNetwork("X", 0, False, False)


def test_wifi_network_open_security():
    n = ac.WifiNetwork.from_json({"ssid": "Cafe", "signal": 100, "security": ""})
    assert not n.secured and n.bars == 4


def test_tailscale_endpoints():
    agent = claimed_agent()
    agent.wifi_state, agent.wifi_ssid = "connected", "ExampleWiFi"
    c = usb(agent)
    st = c.tailscale_status()
    assert st.backend_state == "NeedsLogin" and not st.running and st.auth_url is None
    c.tailscale_up("weatherpi")
    assert json.loads(agent.requests[-1].content) == {"hostname": "weatherpi"}
    assert c.tailscale_status().auth_url.startswith("https://login.tailscale.com/a/")
    agent.complete_tailscale_login()
    st = c.tailscale_status()
    assert st.running and st.dns_name == "weatherpi.example.ts.net" and st.ip == "100.94.2.31"
    st = c.tailscale_serve()
    assert st.serve_enabled and st.serve_url == "https://weatherpi.example.ts.net"
    agent.serve_blocked = True
    with pytest.raises(AgentError) as ei:
        c.tailscale_serve()
    assert ei.value.code == "serve_not_enabled_on_tailnet"
    assert ei.value.admin_url == "https://login.tailscale.com/f/serve?node=n1"
    c.tailscale_logout()
    assert agent.ts_state == "NeedsLogin"


def test_tailscale_up_no_internet():
    agent = claimed_agent()
    with pytest.raises(AgentError) as ei:
        usb(agent).tailscale_up()
    assert ei.value.code == "no_internet"


def test_sensors_and_diagnostics():
    agent = claimed_agent()
    s = usb(agent).sensors_live()
    assert s.temperature.present and s.temperature.celsius == 21.4
    assert s.rain.tips_today == 3 and s.rain.mm_per_tip == 0.2794
    assert s.logger.alive and s.logger.stale_seconds == 12
    empty = ac.SensorsLive.from_json({})
    assert not empty.temperature.present and empty.rain.tips_today == 0
    d = usb(agent).diagnostics()
    assert d["system"]["throttled"] == "0x0"


def test_system_endpoints():
    agent = claimed_agent()
    c = usb(agent)
    c.set_time(datetime(2026, 10, 19, 11, 5, tzinfo=UTC))
    assert agent.time_set_to == "2026-10-19T11:05:00Z"
    c.reboot()
    c.shutdown()
    c.change_password("anotherpass")
    assert agent.owner_password == "anotherpass"
    assert agent.paths()[-3:] == ["POST /v1/system/reboot", "POST /v1/system/shutdown",
                                  "POST /v1/password"]


def test_recover():
    agent = claimed_agent()
    c = usb(agent, token=None)
    with pytest.raises(AgentError) as ei:
        c.recover("wrong" * 5, "newpassword")
    assert ei.value.code == "bad_recovery"
    c.recover("r" * 25, "newpassword", "ssh-ed25519 NEW", "newtoken")
    assert agent.owner_password == "newpassword" and agent.ssh_pubkey == "ssh-ed25519 NEW"
    assert agent.token_sha == hashlib.sha256(b"newtoken").hexdigest()


def test_download_backup_streams_to_file(tmp_path):
    agent = claimed_agent()
    seen = []
    dl = usb(agent).download_backup(tmp_path / "b", progress=lambda n, t: seen.append((n, t)))
    assert dl.path.name == "weatherpi-backup-weatherpi-20261019T110500Z.tar.gz"
    assert dl.path.read_bytes() == agent.backup_bytes
    assert dl.size == len(agent.backup_bytes)
    assert seen and seen[-1][0] == dl.size
    assert not list((tmp_path / "b").glob("*.part"))


def test_download_backup_error_and_no_disposition(tmp_path):
    def handler(req):
        if req.headers.get("authorization") != "Bearer good":
            return httpx.Response(401, json={"error": "unauthorized", "message": "no"})
        return httpx.Response(200, content=b"data")

    t = httpx.MockTransport(handler)
    with pytest.raises(AgentError) as ei:
        AgentClient("http://x:8765", "bad", transport=t).download_backup(tmp_path)
    assert ei.value.code == "unauthorized"
    assert not list(tmp_path.iterdir())
    dl = AgentClient("http://x:8765", "good", transport=t).download_backup(tmp_path)
    assert dl.path.name.startswith("weatherpi-backup-") and dl.path.read_bytes() == b"data"


def test_disposition_path_traversal_is_stripped():
    assert ac._filename_from_disposition('attachment; filename="../../evil.tar.gz"') == (
        "evil.tar.gz")
    assert ac._filename_from_disposition("attachment; filename*=UTF-8''x.tar.gz") == "x.tar.gz"
    assert ac._filename_from_disposition(None) is None


def test_restore_multipart(tmp_path):
    agent = claimed_agent()
    p = tmp_path / "weatherpi-backup-x.tar.gz"
    p.write_bytes(make_backup_bytes())
    res = usb(agent).restore(p)
    assert agent.restored and res.rows == {"temperature_readings": 3, "rain_events": 2}
    req = agent.requests[-1]
    assert req.headers["content-type"].startswith("multipart/form-data")


def test_parse_and_format_time():
    t = ac.parse_time("2026-10-19T11:05:00Z")
    assert t == datetime(2026, 10, 19, 11, 5, tzinfo=UTC)
    assert ac.parse_time("garbage") is None and ac.parse_time(None) is None
    assert ac.format_time(datetime(2026, 10, 19, 11, 5)) == "2026-10-19T11:05:00Z"


def test_serve_waits_longer_than_the_pi_does():
    """The Pi may take 15 s (status) + 20 s (serve blocked) before it answers."""
    agent = claimed_agent()
    agent.ts_state = "Running"
    c = usb(agent)
    c.tailscale_serve()
    assert agent.requests[-1].extensions["timeout"]["read"] >= 45
