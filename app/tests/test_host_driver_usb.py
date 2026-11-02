from __future__ import annotations

import ipaddress
import json

import pytest

from weatherpi_companion.agent_client import AgentInfo
from weatherpi_companion.host import driver as drv
from weatherpi_companion.host import usb
from weatherpi_companion.host.runner import as_list, powershell_args

from .fakes import FakeRunner

GADGET_OK = {"InstanceId": "USB\\VID_2E8A&PID_0013\\6&1A2B3C&0&1",
             "FriendlyName": "Raspberry Pi USB Gadget", "Class": "Net", "Status": "OK",
             "Problem": 0, "Present": True}
GADGET_NO_DRIVER = {"InstanceId": "USB\\VID_2E8A&PID_0013\\6&1A2B3C&0&1",
                    "FriendlyName": "RNDIS", "Class": "Other", "Status": "Error",
                    "Problem": 28, "Present": True}
GADGET_GONE = {**GADGET_OK, "Status": "Unknown", "Problem": 45, "Present": False}
BOOT_ROM = {"InstanceId": "USB\\VID_0A5C&PID_2763\\8&1DC14B6F&0&7",
            "FriendlyName": "BCM2708 Boot", "Class": "USBDevice", "Status": "OK",
            "Problem": 0, "Present": True}
# real shape captured on the dev PC (remembered, not present)
REAL_OLD = [{"InstanceId": "USB\\VID_0A5C&PID_2763\\8&1DC14B6F&0&7",
             "FriendlyName": "BCM2708 Boot", "Class": "USBDevice", "Status": "Unknown",
             "Problem": 45, "Present": False}]


def pnp_runner(devices, pnputil: str | None = "Microsoft PnP Utility\n") -> FakeRunner:
    r = FakeRunner()
    r.on("Get-PnpDevice", json.dumps(devices[0] if len(devices) == 1 else devices)
         if devices else "")
    if pnputil is not None:
        r.on("pnputil", pnputil)
    return r


def test_runner_helpers():
    assert as_list(None) == [] and as_list({"a": 1}) == [{"a": 1}] and as_list([1]) == [1]
    args = powershell_args("Get-Thing")
    assert args[0] == "powershell.exe" and "-NonInteractive" in args
    assert args[-1].endswith("Get-Thing") and "UTF8" in args[-1]


def test_parse_pnp_json_single_list_empty_and_bad():
    assert drv.parse_pnp_json("") == []
    assert drv.parse_pnp_json("garbage") == []
    one = drv.parse_pnp_json(json.dumps(GADGET_OK))
    assert len(one) == 1 and one[0].is_gadget and one[0].working_network
    many = drv.parse_pnp_json(json.dumps([GADGET_NO_DRIVER, BOOT_ROM]))
    assert many[0].missing_driver and many[1].is_boot_rom
    assert drv.parse_pnp_json(json.dumps(REAL_OLD))[0].present is False


def test_driver_status_linux_not_needed():
    st = drv.driver_status(FakeRunner(), platform="linux")
    assert st.state == drv.DriverState.NOT_NEEDED and st.ok


@pytest.mark.parametrize("devices,state,present", [
    ([GADGET_OK], drv.DriverState.OK, True),
    ([GADGET_NO_DRIVER], drv.DriverState.MISSING, True),
    ([{**GADGET_OK, "Problem": 22, "Status": "Error"}], drv.DriverState.DISABLED, True),
    ([{**GADGET_OK, "Problem": 10, "Status": "Error"}], drv.DriverState.PROBLEM, True),
    ([GADGET_GONE], drv.DriverState.OK, False),
])
def test_driver_status_windows(devices, state, present):
    st = drv.driver_status(pnp_runner(devices), platform="win32")
    assert st.state == state and st.device_present == present


# real `pnputil /enum-drivers` blocks from the dev PC
RPIBOOT_BLOCK = """Published Name:     oem34.inf
Original Name:      rpiboot-winusb.inf
Provider Name:      Raspberry Pi Ltd
Class Name:         USBDevice
Class GUID:         {88bae032-5a81-49f0-bc3d-a4ff138216d6}
Driver Version:     01/01/2025 2.0.0.0
Signer Name:        Raspberry Pi Limited
"""
RNDIS_BLOCK = """Published Name:     oem44.inf
Original Name:      raspberrypi-rndis.inf
Provider Name:      Raspberry Pi Ltd.
Class Name:         Net
Class GUID:         {4d36e972-e325-11ce-bfc1-08002be10318}
Driver Version:     06/10/2025 1.0.0.0
Signer Name:        Raspberry Pi Limited
"""


def crlf(text: str) -> str:
    return text.replace("\n", "\r\n")


def test_driver_store_ignores_rpiboot_package():
    only_rpiboot = crlf("Microsoft PnP Utility\n\n" + RPIBOOT_BLOCK)
    assert drv.driver_package_in_store(FakeRunner().on("pnputil", only_rpiboot)) is False
    both = crlf("Microsoft PnP Utility\n\n" + RPIBOOT_BLOCK + "\n" + RNDIS_BLOCK)
    assert drv.driver_package_in_store(FakeRunner().on("pnputil", both)) is True


def test_driver_status_from_driver_store():
    store = "Microsoft PnP Utility\n\n" + RNDIS_BLOCK
    st = drv.driver_status(pnp_runner([], pnputil=store), platform="win32")
    assert st.state == drv.DriverState.OK and not st.device_present
    st = drv.driver_status(pnp_runner(REAL_OLD, pnputil="Published Name: oem1.inf\n"),
                           platform="win32")
    assert st.state == drv.DriverState.UNKNOWN and not st.ok


def test_driver_store_pnputil_failure():
    r = FakeRunner().on("pnputil", "", returncode=1)
    assert drv.driver_package_in_store(r) is None


def test_install_bundled_driver(tmp_path):
    exe = tmp_path / "rpi-usb-gadget-driver-setup.exe"
    exe.write_bytes(b"MZ")
    calls = []

    def launcher(path, params, wait):
        calls.append((path, params, wait))
        return 0

    res = drv.install_bundled_driver(exe, ["/S", "two words"], launcher=launcher,
                                     platform="win32")
    assert res.ok and not res.reboot_required
    assert calls == [(str(exe), '/S "two words"', True)]
    assert drv.install_bundled_driver(exe, launcher=lambda *a: 3010,
                                      platform="win32").reboot_required
    assert not drv.install_bundled_driver(exe, launcher=lambda *a: 1, platform="win32").ok


def test_install_bundled_driver_errors(tmp_path):
    with pytest.raises(drv.InstallError) as ei:
        drv.install_bundled_driver(tmp_path / "x.exe", platform="linux")
    assert ei.value.code == "not_windows"
    with pytest.raises(drv.InstallError) as ei:
        drv.install_bundled_driver(tmp_path / "missing.exe", platform="win32")
    assert ei.value.code == "not_found"
    exe = tmp_path / "setup.exe"
    exe.write_bytes(b"MZ")

    def cancel(*a):
        raise drv.InstallError("cancelled")

    with pytest.raises(drv.InstallError) as ei:
        drv.install_bundled_driver(exe, launcher=cancel, platform="win32")
    assert ei.value.code == "cancelled"


# ------------------------------------------------------------------------------- usb


def win_net(adapters, addresses, routes=(), neighbors=()) -> str:
    return json.dumps({"adapters": adapters, "addresses": addresses, "routes": list(routes),
                       "neighbors": list(neighbors)})


GADGET_ADAPTER = {"Name": "Ethernet 5", "InterfaceDescription": "Raspberry Pi USB Gadget",
                  "ifIndex": 31, "Status": "Up",
                  "PnPDeviceID": "USB\\VID_2E8A&PID_0013\\6&1A2B3C&0&1"}
WIFI_ADAPTER = {"Name": "Wi-Fi", "InterfaceDescription": "Intel Wi-Fi 6", "ifIndex": 4,
                "Status": "Up", "PnPDeviceID": "PCI\\VEN_8086"}
NORMAL = win_net(
    [GADGET_ADAPTER, WIFI_ADAPTER],
    [{"InterfaceIndex": 31, "IPAddress": "10.12.194.5", "PrefixLength": 28},
     {"InterfaceIndex": 4, "IPAddress": "192.168.7.20", "PrefixLength": 24}],
    [{"InterfaceIndex": 4, "NextHop": "192.168.7.1"}],
    [{"InterfaceIndex": 31, "IPAddress": "10.12.194.1", "State": "Reachable"}],
)
ICS = win_net(
    [GADGET_ADAPTER, WIFI_ADAPTER],
    [{"InterfaceIndex": 31, "IPAddress": "192.168.137.1", "PrefixLength": 24},
     {"InterfaceIndex": 4, "IPAddress": "192.168.7.20", "PrefixLength": 24}],
    [{"InterfaceIndex": 4, "NextHop": "192.168.7.1"}],
    [{"InterfaceIndex": 31, "IPAddress": "192.168.137.255", "State": "Permanent"},
     {"InterfaceIndex": 31, "IPAddress": "224.0.0.22", "State": "Permanent"},
     {"InterfaceIndex": 31, "IPAddress": "192.168.137.42", "State": "Stale"},
     {"InterfaceIndex": 31, "IPAddress": "192.168.137.255", "State": "Reachable"},
     {"InterfaceIndex": 4, "IPAddress": "192.168.7.1", "State": "Reachable"}],
)


def test_parse_windows_adapters():
    ads = usb.parse_windows_adapters(NORMAL)
    g = next(a for a in ads if a.index == 31)
    assert g.is_gadget and g.usb_address == ipaddress.IPv4Interface("10.12.194.5/28")
    w = next(a for a in ads if a.index == 4)
    assert not w.is_gadget and w.gateway == "192.168.7.1" and w.usb_address is None
    assert usb.parse_windows_adapters("") == [] and usb.parse_windows_adapters("[]") == []


def test_candidates_normal_and_ics():
    assert usb.candidate_pi_addresses(usb.parse_windows_adapters(NORMAL)) == ["10.12.194.1"]
    assert usb.candidate_pi_addresses(usb.parse_windows_adapters(ICS)) == ["192.168.137.42"]


def test_candidates_gateway_in_usb_subnet():
    text = win_net([GADGET_ADAPTER],
                   [{"InterfaceIndex": 31, "IPAddress": "10.12.194.5", "PrefixLength": 28}],
                   [{"InterfaceIndex": 31, "NextHop": "10.12.194.2"}])
    assert usb.candidate_pi_addresses(usb.parse_windows_adapters(text)) == [
        "10.12.194.1", "10.12.194.2"]


def test_parse_linux_adapters():
    addr = json.dumps([
        {"ifindex": 1, "ifname": "lo", "operstate": "UNKNOWN",
         "addr_info": [{"family": "inet", "local": "127.0.0.1", "prefixlen": 8}]},
        {"ifindex": 5, "ifname": "usb0", "operstate": "UP",
         "addr_info": [{"family": "inet", "local": "10.12.194.6", "prefixlen": 28}]},
    ])
    route = json.dumps([{"dst": "default", "gateway": "192.168.7.1", "dev": "wlan0"}])
    neigh = json.dumps([{"dst": "10.12.194.1", "dev": "usb0", "state": ["REACHABLE"]},
                        {"dst": "10.12.194.9", "dev": "usb0", "state": ["FAILED"]}])
    ads = usb.parse_linux_adapters(addr, route, neigh)
    u = next(a for a in ads if a.name == "usb0")
    assert u.usb_address is not None and u.neighbors == ["10.12.194.1"]
    assert usb.candidate_pi_addresses(ads) == ["10.12.194.1"]


def info(**kw) -> AgentInfo:
    return AgentInfo.from_json({"serial": "00000000abcd1234", "agent_version": "2.0.0",
                                "claimed": False, "via": "usb", **kw})


def win_runner(net: str, devices) -> FakeRunner:
    r = FakeRunner()
    r.on("Get-NetAdapter", net)
    r.on("Get-PnpDevice", json.dumps(devices) if devices else "")
    r.on("pnputil", "Microsoft PnP Utility\n")
    return r


def test_detect_connected_windows():
    probed = []
    det = usb.detect(win_runner(NORMAL, [GADGET_OK]),
                     probe=lambda a: probed.append(a) or info(), platform="win32")
    assert det.connected and det.address == "10.12.194.1" and det.info.serial
    assert det.base_url == "http://10.12.194.1:8765"
    assert det.adapter.index == 31 and probed == ["10.12.194.1"]


def test_detect_ics_mode():
    det = usb.detect(win_runner(ICS, [GADGET_OK]),
                     probe=lambda a: info() if a == "192.168.137.42" else None,
                     platform="win32")
    assert det.connected and det.address == "192.168.137.42"


def no_net():
    return win_net([WIFI_ADAPTER],
                   [{"InterfaceIndex": 4, "IPAddress": "192.168.7.20", "PrefixLength": 24}])


@pytest.mark.parametrize("net,devices,state", [
    (None, [], usb.UsbState.NOT_PLUGGED_IN),
    (None, REAL_OLD, usb.UsbState.NOT_PLUGGED_IN),
    (None, [BOOT_ROM], usb.UsbState.BOOT_ROM),
    (None, [GADGET_NO_DRIVER], usb.UsbState.NO_DRIVER),
    (None, [{**GADGET_OK, "Problem": 10, "Status": "Error"}], usb.UsbState.DRIVER_PROBLEM),
    (None, [GADGET_OK], usb.UsbState.NO_NETWORK),
    (NORMAL, [GADGET_OK], usb.UsbState.NO_AGENT),
])
def test_detect_states_windows(net, devices, state):
    det = usb.detect(win_runner(net or no_net(), devices), probe=lambda a: None,
                     platform="win32")
    assert det.state == state


def test_detect_linux_states():
    r = FakeRunner().on("ip -j -4 addr", "[]").on("ip -j -4 route", "[]").on(
        "ip -j -4 neigh", "[]")
    det = usb.detect(r, probe=lambda a: None, platform="linux", gadget_present=lambda: False)
    assert det.state == usb.UsbState.NOT_PLUGGED_IN
    det = usb.detect(r, probe=lambda a: None, platform="linux", gadget_present=lambda: True)
    assert det.state == usb.UsbState.NO_NETWORK
    assert not r.ran("Get-PnpDevice")


def test_linux_gadget_present(tmp_path):
    d = tmp_path / "1-1"
    d.mkdir()
    (d / "idVendor").write_text("2e8a\n")
    (d / "idProduct").write_text("0013\n")
    assert usb.linux_gadget_present(tmp_path) is True
    (d / "idProduct").write_text("0003\n")
    assert usb.linux_gadget_present(tmp_path) is False
    assert usb.linux_gadget_present(tmp_path / "missing") is None


def test_probe_agent_unreachable_returns_none(monkeypatch):
    # 192.0.2.1 is TEST-NET-1; patch the client so nothing is really sent
    from weatherpi_companion.agent_client import AgentUnreachable

    class Boom:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return None

        def info(self):
            raise AgentUnreachable("unreachable")

    monkeypatch.setattr(usb, "for_host", lambda *a, **k: Boom())
    assert usb.probe_agent("192.0.2.1") is None
