"""The Windows RNDIS driver for the Pi's USB gadget (``VID_2E8A&PID_0013``).

Linux has RNDIS/ECM in the kernel, so there the driver is always OK.

Detection uses ``Get-PnpDevice`` (no admin needed):

* gadget present, class ``Net``, status OK            -> installed and working
* gadget present with Problem 28 / 1 / no class        -> plugged in, **driver missing**
* gadget present with another Problem code             -> driver problem (code shown)
* gadget not present but seen before as class ``Net``  -> installed (worked last time)
* otherwise the driver store (``pnputil /enum-drivers``) is searched for a Raspberry Pi
  network driver; if none is found the state is ``unknown`` (we can't tell until the Pi is
  plugged in), which the UI should treat as "offer to install".

``install_bundled_driver`` runs W7's bundled ``rpi-usb-gadget-driver-setup.exe`` with a UAC
prompt (``ShellExecuteEx`` verb ``runas``).
"""

from __future__ import annotations

import enum
import json
import re
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from .runner import Runner, as_list, run_command, run_powershell

GADGET_VID_PID = "VID_2E8A&PID_0013"
#: the Pi's boot ROM: it shows up like this when it can't boot from the SD card
BOOT_ROM_VID_PID = "VID_0A5C&PID_2763"

# Configuration Manager problem codes (CM_PROB_*) we care about
CM_PROB_NONE = 0
CM_PROB_NOT_CONFIGURED = 1
CM_PROB_FAILED_START = 10
CM_PROB_DISABLED = 22
CM_PROB_FAILED_INSTALL = 28
CM_PROB_PHANTOM = 45  # not currently connected
_NO_DRIVER_CODES = {CM_PROB_NOT_CONFIGURED, CM_PROB_FAILED_INSTALL}

_PNP_SCRIPT = (
    "Get-PnpDevice | Where-Object { $_.InstanceId -match "
    "'VID_2E8A&PID_0013|VID_0A5C&PID_2763' } | "
    "Select-Object InstanceId,FriendlyName,Class,Status,"
    "@{n='Problem';e={[int]$_.Problem}},Present | ConvertTo-Json -Compress"
)


@dataclass(frozen=True)
class PnpDevice:
    instance_id: str
    friendly_name: str
    device_class: str
    status: str  # "OK" | "Error" | "Degraded" | "Unknown"
    problem: int
    present: bool

    @property
    def is_gadget(self) -> bool:
        return GADGET_VID_PID in self.instance_id.upper()

    @property
    def is_boot_rom(self) -> bool:
        return BOOT_ROM_VID_PID in self.instance_id.upper()

    @property
    def working_network(self) -> bool:
        return (self.device_class.lower() == "net" and self.status.upper() == "OK"
                and self.problem == CM_PROB_NONE)

    @property
    def missing_driver(self) -> bool:
        return self.problem in _NO_DRIVER_CODES or (
            self.problem != CM_PROB_NONE and self.device_class.lower() in ("", "other")
        )


def parse_pnp_json(text: str) -> list[PnpDevice]:
    text = (text or "").strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except ValueError:
        return []
    out = []
    for d in as_list(data):
        if not isinstance(d, dict):
            continue
        try:
            problem = int(d.get("Problem") or 0)
        except (TypeError, ValueError):
            problem = -1
        out.append(PnpDevice(
            instance_id=str(d.get("InstanceId") or ""),
            friendly_name=str(d.get("FriendlyName") or ""),
            device_class=str(d.get("Class") or ""),
            status=str(d.get("Status") or ""),
            problem=problem,
            present=bool(d.get("Present", False)),
        ))
    return out


def query_pnp_devices(runner: Runner = run_command) -> list[PnpDevice]:
    """Pi gadget + Pi boot-ROM devices Windows knows about (present and remembered)."""
    res = run_powershell(runner, _PNP_SCRIPT, timeout=30)
    return parse_pnp_json(res.stdout)


class DriverState(enum.StrEnum):
    OK = "ok"  # working now, or worked last time it was plugged in
    MISSING = "missing"  # gadget plugged in but no driver bound
    PROBLEM = "problem"  # driver present but the device reports an error
    DISABLED = "disabled"
    NOT_NEEDED = "not_needed"  # Linux / macOS
    UNKNOWN = "unknown"  # never seen the gadget and no driver package found


@dataclass(frozen=True)
class DriverStatus:
    state: DriverState
    device_present: bool
    problem_code: int | None = None
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.state in (DriverState.OK, DriverState.NOT_NEEDED)


_DRIVER_STORE_RE = re.compile(r"raspberry\s*pi|raspberrypi-rndis|rpi[-_ ]?usb", re.IGNORECASE)
#: GUID of the "Net" device class (locale-independent, unlike the "Class Name" label)
NET_CLASS_GUID = "{4d36e972-e325-11ce-bfc1-08002be10318}"


def driver_package_in_store(runner: Runner = run_command) -> bool | None:
    """Is a Raspberry Pi *network* driver package in the Windows driver store?

    The rpi-usb-gadget driver setup installs ``raspberrypi-rndis.inf`` (provider
    "Raspberry Pi Ltd.", class Net), as seen on the dev PC. Requiring the Net class GUID
    skips other Raspberry Pi packages such as ``rpiboot-winusb.inf``. None if pnputil
    can't run.
    """
    res = runner(["pnputil.exe", "/enum-drivers"], 30)
    if not res.ok:
        return None
    blocks = re.split(r"\r?\n\s*\r?\n", res.stdout)
    return any(_DRIVER_STORE_RE.search(b) and NET_CLASS_GUID in b.lower() for b in blocks)


def driver_status(runner: Runner = run_command, platform: str = sys.platform,
                  devices: Sequence[PnpDevice] | None = None) -> DriverStatus:
    if platform != "win32":
        return DriverStatus(DriverState.NOT_NEEDED, False, detail="built into the kernel")
    devs = [d for d in (devices if devices is not None else query_pnp_devices(runner))
            if d.is_gadget]
    present = [d for d in devs if d.present]
    if present:
        if any(d.working_network for d in present):
            return DriverStatus(DriverState.OK, True, CM_PROB_NONE, "gadget working")
        worst = present[0]
        if any(d.missing_driver for d in present):
            worst = next(d for d in present if d.missing_driver)
            return DriverStatus(DriverState.MISSING, True, worst.problem,
                                f"{worst.friendly_name or worst.instance_id}: no driver")
        if worst.problem == CM_PROB_DISABLED:
            return DriverStatus(DriverState.DISABLED, True, worst.problem, "device disabled")
        return DriverStatus(DriverState.PROBLEM, True, worst.problem,
                            f"{worst.friendly_name}: status {worst.status}, "
                            f"problem code {worst.problem}")
    if any(d.device_class.lower() == "net" for d in devs):
        return DriverStatus(DriverState.OK, False, None, "worked last time it was plugged in")
    in_store = driver_package_in_store(runner)
    if in_store:
        return DriverStatus(DriverState.OK, False, None, "driver package installed")
    return DriverStatus(DriverState.UNKNOWN, False, None,
                        "driver not found (can't be sure until the WeatherPi is plugged in)")


# --------------------------------------------------------------------------- install


class InstallError(Exception):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class InstallResult:
    exit_code: int | None  # None when not waited for

    @property
    def ok(self) -> bool:
        return self.exit_code in (None, 0, 3010)  # 3010 = success, reboot required

    @property
    def reboot_required(self) -> bool:
        return self.exit_code == 3010


#: (exe path, parameters, wait) -> exit code or None. Raises InstallError.
ElevatedLauncher = Callable[[str, str, bool], int | None]

ERROR_CANCELLED = 1223


def _shell_execute_runas(exe: str, params: str, wait: bool) -> int | None:  # pragma: no cover
    """Launch ``exe`` elevated via ShellExecuteExW(verb="runas"); optionally wait for it."""
    import ctypes
    from ctypes import wintypes

    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD), ("fMask", ctypes.c_ulong), ("hwnd", wintypes.HWND),
            ("lpVerb", wintypes.LPCWSTR), ("lpFile", wintypes.LPCWSTR),
            ("lpParameters", wintypes.LPCWSTR), ("lpDirectory", wintypes.LPCWSTR),
            ("nShow", ctypes.c_int), ("hInstApp", wintypes.HINSTANCE),
            ("lpIDList", ctypes.c_void_p), ("lpClass", wintypes.LPCWSTR),
            ("hkeyClass", wintypes.HKEY), ("dwHotKey", wintypes.DWORD),
            ("hIconOrMonitor", wintypes.HANDLE), ("hProcess", wintypes.HANDLE),
        ]

    see_mask_nocloseprocess = 0x00000040
    sw_shownormal = 1
    info = SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = see_mask_nocloseprocess
    info.lpVerb = "runas"
    info.lpFile = exe
    info.lpParameters = params or None
    info.lpDirectory = str(Path(exe).parent)
    info.nShow = sw_shownormal
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        err = ctypes.get_last_error()
        if err == ERROR_CANCELLED:
            raise InstallError("cancelled", "The administrator prompt was cancelled")
        raise InstallError("launch_failed", f"ShellExecuteEx failed ({err})")
    if not info.hProcess:
        return None
    try:
        if not wait:
            return None
        kernel32.WaitForSingleObject(info.hProcess, 0xFFFFFFFF)
        code = wintypes.DWORD()
        kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(code))
        return int(code.value)
    finally:
        kernel32.CloseHandle(info.hProcess)


def install_bundled_driver(
    exe_path: str | Path,
    args: Sequence[str] = (),
    *,
    wait: bool = True,
    launcher: ElevatedLauncher | None = None,
    platform: str = sys.platform,
) -> InstallResult:
    """Run the bundled driver setup with a UAC prompt. Blocks until it exits if ``wait``.

    ``exe_path`` is provided by the installer layout (W7). Raises InstallError with code
    ``not_windows``, ``not_found``, ``cancelled`` (owner said no to UAC) or
    ``launch_failed``.
    """
    if platform != "win32":
        raise InstallError("not_windows", "No driver is needed on this system")
    exe = Path(exe_path)
    if not exe.is_file():
        raise InstallError("not_found", f"Driver setup not found at {exe}")
    params = " ".join(_quote_arg(a) for a in args)
    run = launcher or _shell_execute_runas
    return InstallResult(run(str(exe), params, wait))


def _quote_arg(arg: str) -> str:
    if arg and not re.search(r'[\s"]', arg):
        return arg
    return '"' + arg.replace('"', '\\"') + '"'
