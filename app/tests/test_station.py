from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

import httpx

from weatherpi_companion import station
from weatherpi_companion.agent_client import AgentClient
from weatherpi_companion.checks import Action
from weatherpi_companion.flows import HostEnv
from weatherpi_companion.host import driver as drv
from weatherpi_companion.host import tailscale as ts
from weatherpi_companion.host import usb as host_usb
from weatherpi_companion.host.runner import CommandResult
from weatherpi_companion.secrets import StationProfile, StationSecrets

from .fakes import FakeAgent, FakeRunner, host_status_json

CLI = r"C:\Program Files\Tailscale\tailscale.exe"


def setup_station(app_data, agent: FakeAgent, *, broken=(), host_json=None,
                  usb_state=host_usb.UsbState.NOT_PLUGGED_IN):
    secrets = StationSecrets.generate()
    agent.claimed = True
    agent.token_sha = hashlib.sha256(secrets.api_token.encode()).hexdigest()
    store = app_data.station(agent.serial)
    store.save_secrets(secrets)
    store.save_profile(StationProfile(serial=agent.serial, hostname="weatherpi",
                                      tailnet_ip="100.94.2.31",
                                      dns_name="weatherpi.example.ts.net",
                                      serve_url="https://weatherpi.example.ts.net",
                                      setup_complete=True))
    runner = FakeRunner()
    runner.on_call("status --json", lambda a: CommandResult(0, host_json or host_status_json()))

    def factory(url, token, via):
        host = httpx.URL(url).host
        if host in broken or (via == "usb" and "usb" in broken):
            def refuse(req):
                raise httpx.ConnectError("no route", request=req)
            return AgentClient(url, token, via=via, transport=httpx.MockTransport(refuse))
        return AgentClient(url, token, via=via, transport=agent.transport())

    env = HostEnv(app_data=app_data, runner=runner, platform="win32", client_factory=factory,
                  tailscale=ts.HostTailscale(runner, cli=CLI),
                  usb_detect=lambda: host_usb.UsbDetection(usb_state),
                  driver_status=lambda: drv.DriverStatus(drv.DriverState.OK, False),
                  sleep=lambda s: None)
    return station.StationMonitor(store, env), secrets


def test_candidate_urls_order():
    p = StationProfile(serial="s", tailnet_ip="100.94.2.31", dns_name="weatherpi.example.ts.net.")
    assert station.candidate_urls(p) == [
        ("http://weatherpi.example.ts.net:8765", "tailscale"),
        ("http://100.94.2.31:8765", "tailscale"),
        ("http://10.12.194.1:8765", "usb"),
    ]
    assert station.candidate_urls(StationProfile(serial="s")) == [
        ("http://10.12.194.1:8765", "usb")]


def test_refresh_over_tailscale(app_data):
    agent = FakeAgent()
    mon, _ = setup_station(app_data, agent)
    st = mon.refresh()
    assert st.online and st.via == "tailscale"
    assert st.sensors.rain.tips_today == 3 and st.wifi is not None
    assert st.last_heartbeat == datetime(2026, 10, 19, 11, 0, tzinfo=UTC)
    assert all(r.url.host == "weatherpi.example.ts.net" for r in agent.requests)
    prof = mon.store.load_profile()
    assert prof.agent_version == "2.0.0" and prof.image_version == "2026.10.19-1"


def test_refresh_falls_back_to_ip_then_usb(app_data):
    agent = FakeAgent()
    mon, _ = setup_station(app_data, agent, broken={"weatherpi.example.ts.net"})
    assert mon.refresh().via == "tailscale"
    assert mon.conn.client.base_url == "http://100.94.2.31:8765"
    mon.drop()
    mon2, _ = setup_station(app_data, FakeAgent(),
                            broken={"weatherpi.example.ts.net", "100.94.2.31"})
    st = mon2.refresh()
    assert st.online and st.via == "usb"


def test_connect_rejects_wrong_serial(app_data):
    agent = FakeAgent(serial="ffffffff00000000")
    p = StationProfile(serial="00000000abcd1234", tailnet_ip="100.94.2.31")
    conn = station.connect(p, None, lambda u, t, v: AgentClient(u, t, via=v,
                                                                 transport=agent.transport()))
    assert conn is None


def test_refresh_unauthorized(app_data):
    agent = FakeAgent()
    mon, _ = setup_station(app_data, agent)
    agent.token_sha = "0" * 64
    st = mon.refresh()
    assert not st.online and st.error == "unauthorized"
    assert st.diagnosis[0].action == Action.RECOVER


def test_offline_pi_seen_offline_in_tailnet(app_data):
    agent = FakeAgent()
    peers = [{"ID": "n2", "HostName": "weatherpi", "DNSName": "weatherpi.example.ts.net.",
              "TailscaleIPs": ["100.94.2.31"], "OS": "linux", "Online": False,
              "LastSeen": "2026-10-17T11:00:00Z"}]
    mon, _ = setup_station(app_data, agent, broken={"weatherpi.example.ts.net", "100.94.2.31",
                                                    "usb"},
                           host_json=host_status_json(peers=peers))
    st = mon.refresh()
    assert not st.online and st.error == "unreachable"
    d = st.diagnosis[0]
    assert d.code == "pi_offline" and "last seen" in d.message_for_owner
    assert d.action == Action.PLUG_IN


def test_offline_diagnosis_variants(app_data):
    agent = FakeAgent()
    mon, _ = setup_station(app_data, agent, host_json=host_status_json(peers=[]))
    now = datetime(2026, 10, 19, 12, tzinfo=UTC)
    d = station.diagnose_offline(mon.profile, mon.env, now)
    assert d[0].code == "pi_not_in_tailnet"
    mon.env.tailscale = ts.HostTailscale(mon.env.runner, locate=lambda: None)
    assert station.diagnose_offline(mon.profile, mon.env, now)[0].code == (
        "host_tailscale_missing")
    mon2, _ = setup_station(app_data, agent, host_json=host_status_json("Stopped"))
    assert station.diagnose_offline(mon2.profile, mon2.env, now)[0].code == "host_tailscale_off"
    mon3, _ = setup_station(app_data, agent, usb_state=host_usb.UsbState.NO_AGENT)
    d = station.diagnose_offline(mon3.profile, mon3.env, now)
    assert [x.code for x in d] == ["agent_not_answering", "usb_no_agent"]


def test_ago():
    now = datetime(2026, 10, 19, 12, tzinfo=UTC)
    assert station._ago("2026-10-19T11:59:30Z", now) == "just now"
    assert station._ago("2026-10-19T11:30:00Z", now) == "30 minutes ago"
    assert station._ago("2026-10-19T06:00:00Z", now) == "6 hours ago"
    assert station._ago("2026-10-10T12:00:00Z", now) == "9 days ago"
    assert station._ago("0001-01-01T00:00:00Z", now) is None


def test_backup_now_and_overdue(app_data):
    agent = FakeAgent()
    mon, _ = setup_station(app_data, agent)
    assert mon.backup_overdue()
    check = mon.backup_now()
    assert check.ok and mon.store.load_profile().last_backup_at == "2026-10-19T11:05:00Z"


def test_ai_help_pack_from_monitor(app_data):
    agent = FakeAgent()
    mon, secrets = setup_station(app_data, agent)
    app_data.log_file.write_text(f"connecting with {secrets.api_token}\n")
    text = mon.ai_help_pack("The temperature is stuck")
    assert "The temperature is stuck" in text
    assert "should-not-leak" not in text  # psk inside diagnostics
    assert secrets.api_token not in text and secrets.recovery_password not in text
    assert "## Checks on the owner's computer" in text and "`driver_ok`" in text
    assert "host_tailscale_running" in text
    station_json = text.split("## Station", 1)[1].split("```json\n", 1)[1].split("\n```")[0]
    assert "ssh_host_key" not in json.loads(station_json)


def test_ai_help_pack_offline(app_data):
    agent = FakeAgent()
    mon, _ = setup_station(app_data, agent, broken={"weatherpi.example.ts.net", "100.94.2.31",
                                                    "usb"})
    text = mon.ai_help_pack("offline", host_checks=[])
    assert "couldn't reach the WeatherPi: not reachable" in text


# ---------------------------------------------------------------- agent 2.2.0 (D14, D26)


def test_settings_and_interval(app_data):
    agent = FakeAgent()
    mon, _ = setup_station(app_data, agent)
    st = mon.settings()
    assert st.record_interval_s == 300 and st.record_interval_choices[0] == 60
    assert mon.set_record_interval(1800).record_interval_s == 1800
    assert agent.record_interval_s == 1800
    put = [r for r in agent.requests if r.method == "PUT"][-1]
    assert put.url.path == "/v1/settings" and json.loads(put.content) == {"record_interval_s": 1800}


def test_settings_none_on_old_agent(app_data):
    agent = FakeAgent()
    agent.has_settings = agent.has_maintenance = False
    mon, _ = setup_station(app_data, agent)
    assert mon.settings() is None
    assert mon.maintenance() is None


def test_maintenance_switch_drops_the_connection(app_data):
    agent = FakeAgent()
    mon, _ = setup_station(app_data, agent)
    m = mon.maintenance()
    assert m.overlay_active and not m.maintenance
    mon.refresh()
    assert mon.conn is not None
    m = mon.set_maintenance(True)
    assert m.maintenance and m.rebooting
    assert mon.conn is None  # the Pi restarts: reconnect next time
    # already on: no reboot
    assert not mon.set_maintenance(True).rebooting
