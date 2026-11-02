"""Is the WeatherPi plugged in over USB, and can we reach its agent?

Normal case: the Pi is ``10.12.194.1/28`` and hands this PC a DHCP address in
``10.12.194.2-14``. If the PC shares its internet over the link (Windows ICS) the Pi's
``ics-watch`` turns it into a DHCP *client* instead; the PC adapter then has an ICS
address (usually ``192.168.137.1``) and the Pi's address has to be discovered (adapter
gateway, or the neighbour/ARP table of the gadget adapter).

On Windows we also look at the USB device itself (``VID_2E8A&PID_0013``) to tell
"not plugged in" from "plugged in but no driver". ``VID_0A5C&PID_2763`` ("BCM2708 Boot")
means the Pi is up but can't boot from its SD card.
"""

from __future__ import annotations

import enum
import ipaddress
import json
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..agent_client import USB_HOST, AgentError, AgentInfo, for_host
from .driver import GADGET_VID_PID, DriverState, PnpDevice, driver_status, query_pnp_devices
from .runner import Runner, as_list, run_command, run_powershell

USB_SUBNET = ipaddress.IPv4Network("10.12.194.0/28")
_GADGET_DESC_HINTS = ("raspberry pi usb gadget", "raspberry pi")

_ADAPTERS_SCRIPT = (
    "$a=@(Get-NetAdapter -IncludeHidden | Select-Object Name,InterfaceDescription,ifIndex,"
    "Status,PnPDeviceID);"
    "$ip=@(Get-NetIPAddress -AddressFamily IPv4 | Select-Object InterfaceIndex,IPAddress,"
    "PrefixLength);"
    "$gw=@(Get-NetRoute -AddressFamily IPv4 -DestinationPrefix '0.0.0.0/0' | "
    "Select-Object InterfaceIndex,NextHop);"
    "$nb=@(Get-NetNeighbor -AddressFamily IPv4 | Select-Object InterfaceIndex,IPAddress,"
    "@{n='State';e={[string]$_.State}});"
    "@{adapters=$a;addresses=$ip;routes=$gw;neighbors=$nb} | ConvertTo-Json -Depth 4 -Compress"
)


@dataclass
class HostAdapter:
    name: str
    index: int
    description: str = ""
    status: str = ""
    pnp_id: str = ""
    addresses: list[ipaddress.IPv4Interface] = field(default_factory=list)
    gateway: str | None = None
    neighbors: list[str] = field(default_factory=list)

    @property
    def is_gadget(self) -> bool:
        if GADGET_VID_PID in self.pnp_id.upper():
            return True
        desc = self.description.lower()
        return any(h in desc for h in _GADGET_DESC_HINTS)

    @property
    def usb_address(self) -> ipaddress.IPv4Interface | None:
        """This PC's address in 10.12.194.0/28, if any."""
        return next((a for a in self.addresses if a.ip in USB_SUBNET), None)

    @property
    def usable_addresses(self) -> list[ipaddress.IPv4Interface]:
        return [a for a in self.addresses if not a.ip.is_link_local and not a.ip.is_loopback]


# --------------------------------------------------------------------------- adapters


def _iface(ip: str, prefix: Any) -> ipaddress.IPv4Interface | None:
    try:
        return ipaddress.IPv4Interface(f"{ip}/{int(prefix)}")
    except (ValueError, TypeError):
        return None


def parse_windows_adapters(text: str) -> list[HostAdapter]:
    try:
        data = json.loads(text or "{}")
    except ValueError:
        return []
    if not isinstance(data, dict):
        return []
    adapters: dict[int, HostAdapter] = {}
    for a in as_list(data.get("adapters")):
        if not isinstance(a, dict) or a.get("ifIndex") is None:
            continue
        idx = int(a["ifIndex"])
        adapters[idx] = HostAdapter(
            name=str(a.get("Name") or f"if{idx}"), index=idx,
            description=str(a.get("InterfaceDescription") or ""),
            status=str(a.get("Status") or ""), pnp_id=str(a.get("PnPDeviceID") or ""),
        )

    def get(idx: Any) -> HostAdapter | None:
        try:
            i = int(idx)
        except (TypeError, ValueError):
            return None
        if i not in adapters:
            adapters[i] = HostAdapter(name=f"if{i}", index=i)
        return adapters[i]

    for ip in as_list(data.get("addresses")):
        if isinstance(ip, dict) and (ad := get(ip.get("InterfaceIndex"))):
            iface = _iface(str(ip.get("IPAddress")), ip.get("PrefixLength"))
            if iface:
                ad.addresses.append(iface)
    for r in as_list(data.get("routes")):
        if isinstance(r, dict) and (ad := get(r.get("InterfaceIndex"))):
            hop = str(r.get("NextHop") or "")
            if hop and hop != "0.0.0.0":
                ad.gateway = hop
    for n in as_list(data.get("neighbors")):
        if isinstance(n, dict) and (ad := get(n.get("InterfaceIndex"))):
            state = str(n.get("State") or "")
            if state.lower() in ("unreachable", "incomplete", "permanent"):
                continue
            ad.neighbors.append(str(n.get("IPAddress")))
    return sorted(adapters.values(), key=lambda a: a.index)


def parse_linux_adapters(addr_json: str, route_json: str = "[]",
                         neigh_json: str = "[]") -> list[HostAdapter]:
    def load(t: str) -> list:
        try:
            v = json.loads(t or "[]")
        except ValueError:
            return []
        return v if isinstance(v, list) else []

    adapters: dict[str, HostAdapter] = {}
    for a in load(addr_json):
        if not isinstance(a, dict) or not a.get("ifname"):
            continue
        ad = HostAdapter(name=a["ifname"], index=int(a.get("ifindex") or 0),
                         status=str(a.get("operstate") or ""))
        for info in a.get("addr_info") or []:
            if info.get("family") == "inet":
                iface = _iface(str(info.get("local")), info.get("prefixlen"))
                if iface:
                    ad.addresses.append(iface)
        adapters[ad.name] = ad
    for r in load(route_json):
        if isinstance(r, dict) and r.get("dst") == "default" and r.get("dev") in adapters:
            adapters[r["dev"]].gateway = r.get("gateway")
    for n in load(neigh_json):
        if isinstance(n, dict) and n.get("dev") in adapters:
            states = [s.upper() for s in n.get("state") or []]
            if "FAILED" in states or "INCOMPLETE" in states:
                continue
            adapters[n["dev"]].neighbors.append(str(n.get("dst")))
    return list(adapters.values())


def list_adapters(runner: Runner = run_command, platform: str = sys.platform) -> list[HostAdapter]:
    if platform == "win32":
        return parse_windows_adapters(run_powershell(runner, _ADAPTERS_SCRIPT).stdout)
    addr = runner(["ip", "-j", "-4", "addr", "show"], 10).stdout
    route = runner(["ip", "-j", "-4", "route", "show", "default"], 10).stdout
    neigh = runner(["ip", "-j", "-4", "neigh", "show"], 10).stdout
    return parse_linux_adapters(addr, route, neigh)


def candidate_pi_addresses(adapters: Sequence[HostAdapter]) -> list[str]:
    """Addresses worth probing for the agent, best first."""
    out: list[str] = []

    def add(ip: str | None) -> None:
        if ip and ip not in out:
            out.append(ip)

    for ad in adapters:
        if ad.usb_address:
            add(USB_HOST)
            if ad.gateway and ipaddress.IPv4Address(ad.gateway) in USB_SUBNET:
                add(ad.gateway)
    # ICS mode: gadget adapter has some other (non link-local) address
    for ad in adapters:
        if not ad.is_gadget or ad.usb_address:
            continue
        own = {str(a.ip) for a in ad.addresses}
        nets = [a.network for a in ad.usable_addresses]
        if ad.gateway and ad.gateway not in own:
            add(ad.gateway)
        for n in ad.neighbors:
            try:
                ip = ipaddress.IPv4Address(n)
            except ValueError:
                continue
            if ip.is_multicast or str(ip) in own or ip.is_unspecified:
                continue
            if any(ip in net and ip != net.broadcast_address for net in nets):
                add(str(ip))
    return out


# --------------------------------------------------------------------------- detection


class UsbState(enum.StrEnum):
    NOT_PLUGGED_IN = "not_plugged_in"
    BOOT_ROM = "boot_rom"  # Pi powered but not booting from the SD card
    NO_DRIVER = "no_driver"
    DRIVER_PROBLEM = "driver_problem"
    NO_NETWORK = "no_network"  # gadget there but no usable address yet (still booting?)
    NO_AGENT = "no_agent"  # network OK, agent not answering
    CONNECTED = "connected"


@dataclass
class UsbDetection:
    state: UsbState
    address: str | None = None  # where the agent answered
    info: AgentInfo | None = None
    adapter: HostAdapter | None = None
    devices: list[PnpDevice] = field(default_factory=list)
    tried: list[str] = field(default_factory=list)
    detail: str = ""

    @property
    def connected(self) -> bool:
        return self.state == UsbState.CONNECTED

    @property
    def base_url(self) -> str | None:
        return f"http://{self.address}:8765" if self.address else None


Probe = Callable[[str], AgentInfo | None]


def probe_agent(address: str, timeout: float = 3.0) -> AgentInfo | None:
    """``GET /v1/info`` at ``address``; None if nothing answers."""
    try:
        with for_host(address, via="usb", timeout=timeout) as c:
            return c.info()
    except AgentError:
        return None


def linux_gadget_present(usb_root: Path = Path("/sys/bus/usb/devices")) -> bool | None:
    try:
        for dev in usb_root.iterdir():
            vid, pid = dev / "idVendor", dev / "idProduct"
            if vid.is_file() and pid.is_file() and (
                vid.read_text().strip().lower(), pid.read_text().strip().lower()
            ) == ("2e8a", "0013"):
                return True
    except OSError:
        return None
    return False


def detect(
    runner: Runner = run_command,
    probe: Probe = probe_agent,
    platform: str = sys.platform,
    gadget_present: Callable[[], bool | None] = linux_gadget_present,
) -> UsbDetection:
    """One detection pass. Cheap enough to call every couple of seconds."""
    adapters = list_adapters(runner, platform)
    candidates = candidate_pi_addresses(adapters)
    tried: list[str] = []
    for addr in candidates:
        tried.append(addr)
        info = probe(addr)
        if info is not None:
            adapter = next((a for a in adapters if a.usb_address), None) or next(
                (a for a in adapters if a.is_gadget), None)
            return UsbDetection(UsbState.CONNECTED, addr, info, adapter, tried=tried)

    gadget_adapter = next((a for a in adapters if a.usb_address or a.is_gadget), None)
    if platform == "win32":
        devices = query_pnp_devices(runner)
        present = [d for d in devices if d.present]
        if not any(d.is_gadget for d in present):
            if any(d.is_boot_rom for d in present):
                return UsbDetection(UsbState.BOOT_ROM, devices=devices, tried=tried,
                                    detail="BCM2708 Boot: the Pi can't read its SD card")
            if gadget_adapter is None:
                return UsbDetection(UsbState.NOT_PLUGGED_IN, devices=devices, tried=tried)
        else:
            drv = driver_status(runner, platform, devices)
            if drv.state == DriverState.MISSING:
                return UsbDetection(UsbState.NO_DRIVER, devices=devices, tried=tried,
                                    detail=drv.detail)
            if drv.state in (DriverState.PROBLEM, DriverState.DISABLED):
                return UsbDetection(UsbState.DRIVER_PROBLEM, devices=devices, tried=tried,
                                    detail=drv.detail)
    else:
        devices = []
        if gadget_adapter is None and not gadget_present():
            return UsbDetection(UsbState.NOT_PLUGGED_IN, tried=tried)

    if not candidates:
        return UsbDetection(UsbState.NO_NETWORK, adapter=gadget_adapter, devices=devices,
                            tried=tried, detail="no address on the USB network yet")
    return UsbDetection(UsbState.NO_AGENT, adapter=gadget_adapter, devices=devices,
                        tried=tried, detail="nothing answered on port 8765")
