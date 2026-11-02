"""Shared fakes for the flasher tests (no tests in here): disk listings and image builders.

The Windows listings mimic the JSON from ``flash.WINDOWS_DISKS_SCRIPT`` (field names and
value shapes as seen on a real Windows 11 PC); Linux ones mimic ``lsblk -J -b``.
"""

from __future__ import annotations

import hashlib
import json
import lzma
import os
import random
from pathlib import Path

from weatherpi_companion.host.flash import Disk
from weatherpi_companion.host.flash_image import LocalImage, load_local_image

GB = 1_000_000_000


def win_disk(number: int, name: str, size: int, bus: str, media: str = "Removable Media",
             letters=(), volumes=None, **kw) -> dict:
    d = {
        "Number": number, "FriendlyName": name, "Model": name,
        "SerialNumber": kw.pop("serial", f"SER{number:04d}"),
        "UniqueId": kw.pop("unique_id", f"USBSTOR\\DISK&VEN_GENERIC&PROD_{number}"),
        "Size": size, "BusType": bus, "IsSystem": False, "IsBoot": False,
        "IsOffline": False, "IsReadOnly": False, "OperationalStatus": "Online",
        "Location": kw.pop("location", f"Port {number}"), "MediaType": media,
        "PnpDeviceId": kw.pop("pnp", f"USBSTOR\\DISK&VEN_GENERIC\\{number}"),
        "Letters": list(letters),
        "Volumes": volumes if volumes is not None else
        [f"\\\\?\\Volume{{0000000{number}-0000-0000-0000-000000000000}}\\"] * bool(letters),
    }
    d.update(kw)
    return d


NVME_SYSTEM = win_disk(
    0, "HS-SSD-SILK(P) 1024G", 1024209543168, "NVMe", "Fixed hard disk media", ["C"],
    serial="0000_0000_0000_0000_0C82_D502_5270_3280.",
    unique_id="eui.00000000000000000C82D50252703280",
    location="Integrated : Bus 2 : Device 0 : Function 0 : Adapter 1",
    pnp="SCSI\\DISK&VEN_NVME&PROD_HS-SSD-SILK(P)_1\\5&3483398&0&000000",
    IsSystem=True, IsBoot=True,
    volumes=["\\\\?\\Volume{760ead99-d876-4b9e-a528-47b3802bfd22}\\",
             "\\\\?\\Volume{38388475-72ad-4029-80d6-4883ecbe8911}\\"],
)
# an SD card in a USB card reader: reports bus USB, removable media
USB_READER_32G = win_disk(1, "Generic- SD/MMC USB Device", 31_914_983_424, "USB",
                          letters=["E"], serial="000000000819",
                          unique_id="USBSTOR\\DISK&VEN_GENERIC-&PROD_SD/MMC&REV_1.00\\"
                                    "000000000819&0:DESKTOP")
# the laptop's built-in reader (Realtek PCIe) with a 64 GB card
BUILTIN_SD_64G = win_disk(2, "SD SN64G", 63_864_569_856, "SD", letters=["F"],
                          pnp="SD\\DISK&GENERIC&SN64G&0.0\\5&2A1B&0&RTSX")
USB_HDD_1TB = win_disk(3, "WD Elements 25A2 USB Device", 1_000_170_586_112, "USB",
                       "External hard disk media", ["G"])
SD_2TB = win_disk(4, "SDXC Card 2TB", 2_000_398_934_016, "SD", letters=["H"])
HYPERV_DISK = win_disk(5, "Msft Virtual Disk", 64_424_509_440, "SCSI",
                       "Fixed hard disk media", ["I"], pnp="SCSI\\DISK&VEN_MSFT&PROD_VIRTUAL_DISK\\1")
VHD_ATTACHED = win_disk(6, "Msft Virtual Disk", 8_589_934_592, "File Backed Virtual",
                        "Fixed hard disk media", ["J"])
# Windows doesn't give letters to an ext4-only or blank card
NO_LETTER_16G = win_disk(7, "Generic STORAGE DEVICE USB Device", 15_931_539_456, "USB",
                         letters=[], volumes=[])
SATA_DATA = win_disk(8, "ST500DM002-1BD142", 500_107_862_016, "SATA",
                     "Fixed hard disk media", ["D"])
LOCKED_CARD = win_disk(9, "Generic- SD/MMC USB Device", 7_948_206_080, "USB", letters=["K"],
                       IsReadOnly=True)
OFFLINE_CARD = win_disk(10, "Generic- SD/MMC USB Device", 7_948_206_080, "USB", letters=[],
                        IsOffline=True, OperationalStatus="Offline")
EMPTY_READER = win_disk(11, "Generic- MS/MS-Pro USB Device", 0, "USB", letters=[],
                        OperationalStatus="No Media")
USB_SSD_240 = win_disk(12, "Samsung PSSD T7 USB Device", 240_057_409_536, "USB",
                       "External hard disk media", ["L"])
TINY_CARD = win_disk(13, "Generic- SD/MMC USB Device", 2_002_780_160, "USB", letters=["M"])
UNKNOWN_MEDIA_USB = win_disk(14, "Mass Storage Device USB Device", 31_914_983_424, "USB",
                             "", ["N"])
EMMC_INTERNAL = win_disk(15, "SanDisk DA4064", 62_537_072_640, "MMC",
                         "Fixed hard disk media", [])
RAID_DISK = win_disk(16, "Intel Raid 0 Volume", 128_035_676_160, "RAID",
                     "Fixed hard disk media", ["O"])

ALL_WINDOWS = [NVME_SYSTEM, USB_READER_32G, BUILTIN_SD_64G, USB_HDD_1TB, SD_2TB, HYPERV_DISK,
               VHD_ATTACHED, NO_LETTER_16G, SATA_DATA, LOCKED_CARD, OFFLINE_CARD, EMPTY_READER,
               USB_SSD_240, TINY_CARD, UNKNOWN_MEDIA_USB, EMMC_INTERNAL, RAID_DISK]


def windows_json(disks, system_drive="C:", pagefiles=("C:\\pagefile.sys",)) -> str:
    return json.dumps({"SystemDrive": system_drive, "PageFiles": list(pagefiles),
                       "Disks": disks})


def _part(name, size, mount=None, typ="part", children=None):
    d = {"name": name, "path": f"/dev/{name}", "type": typ, "size": size, "model": None,
         "vendor": None, "serial": None, "wwn": None, "tran": None, "rm": False, "ro": False,
         "hotplug": False, "mountpoint": mount}
    if children:
        d["children"] = children
    return d


def _ldisk(name, size, tran, rm, model="", vendor="", serial="", ro=False, children=None,
           mount=None, typ="disk"):
    d = {"name": name, "path": f"/dev/{name}", "type": typ, "size": size, "model": model,
         "vendor": vendor, "serial": serial, "wwn": None, "tran": tran, "rm": rm, "ro": ro,
         "hotplug": rm, "mountpoint": mount}
    if children:
        d["children"] = children
    return d


LSBLK = {"blockdevices": [
    _ldisk("loop0", 4096, None, False, typ="loop", mount="/snap/core/1"),
    _ldisk("nvme0n1", 512_110_190_592, "nvme", False, "Samsung SSD 970", serial="S4EW",
           children=[_part("nvme0n1p1", 536_870_912, "/boot/efi"),
                     _part("nvme0n1p2", 511_000_000_000, "/")]),
    _ldisk("sda", 1_000_204_886_016, "sata", False, "WDC WD10EZEX", serial="WD-1",
           children=[_part("sda1", 1_000_204_000_000, "/home")]),
    _ldisk("sdb", 31_914_983_424, "usb", True, "SD/MMC", "Generic-", "000000000819",
           children=[_part("sdb1", 536_870_912, "/media/owner/bootfs"),
                     _part("sdb2", 2_684_354_560)]),
    _ldisk("sdc", 1_000_170_586_112, "usb", False, "Elements 25A2", "WD", "575833"),
    _ldisk("mmcblk0", 15_931_539_456, None, False, serial="0x1234",
           children=[_part("mmcblk0p1", 15_931_000_000, "/run/media/owner/CARD")]),
    _ldisk("mmcblk1", 62_537_072_640, None, False, serial="0xabcd",
           children=[_part("mmcblk1p1", 62_000_000_000, "/var")]),
    _ldisk("mmcblk2", 31_268_536_320, None, False, serial="0x5555"),
    _ldisk("mmcblk1boot0", 4_194_304, None, False),
    _ldisk("sdd", 63_864_569_856, "usb", True, "Flash Disk", "Generic",
           children=[_part("sdd1", 63_000_000_000, children=[
               _part("vg-root", 63_000_000_000, "/data", typ="lvm")])]),
    _ldisk("zram0", 8_000_000_000, None, False, mount="[SWAP]"),
    _ldisk("sde", 31_914_983_424, "usb", True, "Card Reader", "Generic",
           children=[_part("sde1", 4_000_000_000, "[SWAP]")]),
    _ldisk("sdf", 7_948_206_080, "usb", True, "SD/MMC", "Generic-", "LOCK", ro=True),
]}
# older lsblk (util-linux < 2.33-ish) prints flags and sizes as strings
LSBLK_OLD = {"blockdevices": [
    {"name": "sdb", "path": "/dev/sdb", "type": "disk", "size": "31914983424",
     "model": "SD/MMC          ", "vendor": "Generic-", "serial": "000000000819",
     "wwn": None, "tran": "usb", "rm": "1", "ro": "0", "hotplug": "1", "mountpoint": None},
]}

MMC_TYPES = {"mmcblk0": "SD", "mmcblk1": "MMC"}  # mmcblk2: unreadable sysfs -> unknown


# --------------------------------------------------------------------------- images


def raw_image_bytes(size: int, seed: int = 1) -> bytes:
    """Partly random, partly zero, like a real image (and not a multiple of the sector)."""
    rnd = random.Random(seed)
    half = size // 2
    return rnd.randbytes(half) + bytes(size - half - 7) + b"WPiEND!"


def make_image(folder: Path, raw: bytes, version: str = "2026.10.28-1", *,
               sha_override: str | None = None, sha_file: bool = True) -> LocalImage | Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"weatherpi-{version}.img.xz"
    path.write_bytes(lzma.compress(raw, format=lzma.FORMAT_XZ))
    if not sha_file:
        return path
    digest = sha_override or hashlib.sha256(path.read_bytes()).hexdigest()
    (folder / f"{path.name}.sha256").write_text(f"{digest}  {path.name}\n")
    return load_local_image(path)


def card_disk(device: Path, size: int = 31_914_983_424, **kw) -> Disk:
    """A disk that passes the safety filter, standing in for a card (the device is a file)."""
    base = {"device": str(device), "model": "Generic- SD/MMC USB Device", "size": size,
            "bus": "usb", "number": 1, "serial": "000000000819", "unique_id": "USBSTOR\\X",
            "removable": True, "letters": ("E",), "status": "Online"}
    base.update(kw)
    return Disk(**base)


def make_device_file(path: Path, size: int, fill: int = 0xAA) -> Path:
    """A temp file with "old card contents" in it."""
    with open(path, "wb") as f:
        f.write(bytes([fill]) * size)
    os.utime(path)
    return path
