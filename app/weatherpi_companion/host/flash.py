"""Write the WeatherPi image to an SD card: find cards, the safety filter, write and verify.

**Safety first.** The owner's hard rule is that this only ever writes to an SD card. Every
disk the OS reports goes through :func:`reject_reasons`; only disks with *no* reasons are
offered (:func:`candidate_disks`), the elevated helper repeats the whole check on a fresh
listing (:func:`revalidate`) and refuses anything that changed or no longer passes. When a
value is missing or odd, the disk is excluded.

Disk listing goes through the injectable ``runner`` (PowerShell ``Get-Disk`` +
``Win32_DiskDrive`` on Windows, ``lsblk -J -b`` on Linux), so tests use canned JSON.

Writing (:func:`write_image` / :func:`verify_device`) works on any :class:`BlockDevice`, so
tests use a temp file. The image is stream-decompressed with :mod:`lzma` (never held in RAM
or a temp file) and written in 4 MiB sector-aligned chunks. The first chunk is zeroed first
and written last, so an interrupted write never leaves a card that looks bootable. After a
flush the card is read back and its sha256 compared with the decompressed stream's.

Nothing here imports Qt.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import lzma
import os
import re
import sys
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Protocol

from .flash_errors import FlashError, from_oserror
from .flash_image import LocalImage, ProgressFn, verify_image
from .runner import Runner, as_list, run_command, run_powershell

CHUNK = 4 * 1024 * 1024
SECTOR = 512
#: smallest disk offered: below any "4 GB" card's real size (the image itself is ~3.2 GB;
#: the exact fit is checked against the image's raw size before writing)
MIN_CARD_BYTES = 3_500_000_000
#: largest disk offered: a "256 GB" card (some report a little over 256e9 bytes, so the
#: limit is 256 GiB). Anything bigger is treated as a hard disk.
MAX_CARD_BYTES = 256 * 1024**3

#: buses a card reader can be on. "usb" also needs the removable-media flag.
ALLOWED_BUSES = frozenset({"sd", "mmc", "usb"})
_VIRTUAL_RE = re.compile(r"virtual|vhd|vmware|vbox|qemu|msft\s+virtual|hyper-?v|iscsi|"
                         r"storage\s*space|ramdisk|imdisk", re.IGNORECASE)
_SAFE_LINUX_MOUNT_PREFIXES = ("/media/", "/run/media/", "/mnt/")

PHASE_VERIFY_IMAGE = "verify-image"
PHASE_WRITE = "write"
PHASE_VERIFY_CARD = "verify-card"
PHASE_DOWNLOAD = "download"


# --------------------------------------------------------------------------- disks


@dataclass(frozen=True)
class Disk:
    """A whole disk as the OS reports it (normalised across Windows and Linux)."""

    device: str  # \\.\PhysicalDrive2 | /dev/sdb
    model: str
    size: int
    bus: str  # lower case: sd, usb, mmc, nvme, sata, scsi, file backed virtual ...
    number: int | None = None  # Windows disk number
    serial: str = ""
    unique_id: str = ""
    removable: bool | None = None  # None = the OS didn't say
    letters: tuple[str, ...] = ()  # Windows drive letters ("E")
    volumes: tuple[str, ...] = ()  # Windows \\?\Volume{..}\ paths | Linux partition devices
    mountpoints: tuple[str, ...] = ()  # Linux mount points of the disk and its partitions
    is_system: bool = False  # holds the OS / boot / EFI (or a non-media Linux mount)
    is_boot: bool = False
    is_offline: bool = False
    is_read_only: bool = False
    has_pagefile: bool = False  # Windows page file or Linux swap
    nested: bool = False  # Linux: LVM / crypt / RAID on top of it
    status: str = ""  # Windows OperationalStatus
    media_type: str = ""  # Windows Win32_DiskDrive.MediaType
    location: str = ""
    pnp_id: str = ""

    @property
    def identity(self) -> DiskIdentity:
        return DiskIdentity(self.device, self.number, self.serial, self.unique_id, self.size,
                            self.model)

    @property
    def size_text(self) -> str:
        return format_size(self.size)

    @property
    def where_text(self) -> str:
        if self.letters:
            return ", ".join(f"{letter}:" for letter in self.letters)
        mounted = [m for m in self.mountpoints if m]
        if mounted:
            return ", ".join(mounted)
        return "no drive letter"

    @property
    def label(self) -> str:
        """``SanDisk SDDR-B531 (31.9 GB, E:)`` for the picker and the confirm screen."""
        return f"{self.model or 'Unknown card'} ({self.size_text}, {self.where_text})"


@dataclass(frozen=True)
class DiskIdentity:
    """What must still match right before writing (the helper gets this as its target)."""

    device: str
    number: int | None
    serial: str
    unique_id: str
    size: int
    model: str = ""

    def matches(self, disk: Disk) -> bool:
        return disk.identity == self

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> DiskIdentity:
        num = d.get("number")
        return cls(str(d["device"]), int(num) if num is not None else None,
                   str(d.get("serial") or ""), str(d.get("unique_id") or ""), int(d["size"]),
                   str(d.get("model") or ""))


def format_size(n: int) -> str:
    """Decimal units, the way cards are sold: 31.9 GB."""
    if n >= 1_000_000_000:
        return f"{n / 1e9:.1f} GB"
    if n >= 1_000_000:
        return f"{n / 1e6:.0f} MB"
    return f"{n} bytes"


# -- Windows

WINDOWS_DISKS_SCRIPT = (
    "$pf=@(Get-CimInstance Win32_PageFileUsage | ForEach-Object { [string]$_.Name });"
    "$wd=@{}; Get-CimInstance Win32_DiskDrive | ForEach-Object { $wd[[string]$_.Index]=$_ };"
    "$disks=@(Get-Disk | ForEach-Object { $d=$_; $w=$wd[[string]$d.Number];"
    "$parts=@(Get-Partition -DiskNumber $d.Number);"
    "[pscustomobject]@{Number=[int]$d.Number;FriendlyName=[string]$d.FriendlyName;"
    "Model=[string]$d.Model;SerialNumber=[string]$d.SerialNumber;"
    "UniqueId=[string]$d.UniqueId;Size=[int64]$d.Size;BusType=[string]$d.BusType;"
    "IsSystem=[bool]$d.IsSystem;IsBoot=[bool]$d.IsBoot;IsOffline=[bool]$d.IsOffline;"
    "IsReadOnly=[bool]$d.IsReadOnly;OperationalStatus=[string]$d.OperationalStatus;"
    "Location=[string]$d.Location;MediaType=[string]$w.MediaType;"
    "PnpDeviceId=[string]$w.PNPDeviceID;"
    "Letters=@($parts | ForEach-Object { [string]$_.DriveLetter } | "
    "Where-Object { $_ -match '^[A-Za-z]$' });"
    "Volumes=@($parts | ForEach-Object { $_.AccessPaths } | "
    "Where-Object { $_ -like '\\\\?\\Volume*' })} });"
    "[pscustomobject]@{SystemDrive=[string]$env:SystemDrive;PageFiles=$pf;Disks=$disks} | "
    "ConvertTo-Json -Depth 4 -Compress"
)


def _ps_list(value: object) -> list:
    """PS 5.1 sometimes serialises arrays as ``{"value": [...], "Count": n}``."""
    if isinstance(value, dict) and "value" in value:
        value = value["value"]
    return as_list(value)


def _bool(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes")


def _windows_removable(media_type: str) -> bool | None:
    mt = media_type.strip().lower()
    if not mt:
        return None
    # anything else ("Fixed hard disk media", "External hard disk media", ...) is not removable
    return mt.startswith("removable")


def parse_windows_disks(text: str) -> list[Disk]:
    """Parse the JSON from :data:`WINDOWS_DISKS_SCRIPT`. Unparseable input -> no disks."""
    try:
        data = json.loads((text or "").strip() or "null")
    except ValueError:
        return []
    if not isinstance(data, dict):
        return []
    system_drive = str(data.get("SystemDrive") or "C:").strip()[:1].upper()
    pagefile_letters = {str(p).strip()[:1].upper() for p in _ps_list(data.get("PageFiles"))
                        if str(p).strip()[1:2] == ":"}
    out = []
    for d in _ps_list(data.get("Disks")):
        if not isinstance(d, dict):
            continue
        try:
            number = int(d["Number"])
            size = int(d.get("Size") or 0)
        except (KeyError, TypeError, ValueError):
            continue
        letters = tuple(sorted({str(x).strip().upper() for x in _ps_list(d.get("Letters"))
                                if re.fullmatch(r"[A-Za-z]", str(x).strip())}))
        media_type = str(d.get("MediaType") or "")
        holds_os = system_drive in letters
        out.append(Disk(
            device=rf"\\.\PhysicalDrive{number}",
            number=number,
            model=str(d.get("FriendlyName") or d.get("Model") or "").strip(),
            size=size,
            bus=str(d.get("BusType") or "").strip().lower(),
            serial=str(d.get("SerialNumber") or "").strip(),
            unique_id=str(d.get("UniqueId") or "").strip(),
            removable=_windows_removable(media_type),
            letters=letters,
            volumes=tuple(str(v) for v in _ps_list(d.get("Volumes")) if v),
            # missing flags count as "yes" for the dangerous ones: when in doubt, exclude
            is_system=_bool(d.get("IsSystem"), True) or holds_os,
            is_boot=_bool(d.get("IsBoot"), True),
            is_offline=_bool(d.get("IsOffline"), True),
            is_read_only=_bool(d.get("IsReadOnly"), True),
            has_pagefile=bool(pagefile_letters & set(letters)),
            status=str(d.get("OperationalStatus") or ""),
            media_type=media_type,
            location=str(d.get("Location") or ""),
            pnp_id=str(d.get("PnpDeviceId") or ""),
        ))
    return out


# -- Linux

LSBLK_ARGS = ["lsblk", "-J", "-b", "-o",
              "NAME,PATH,TYPE,SIZE,MODEL,VENDOR,SERIAL,WWN,TRAN,RM,RO,HOTPLUG,MOUNTPOINT"]


def _flag(value: object) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true")


def _walk(node: dict) -> Iterator[dict]:
    for child in node.get("children") or []:
        if isinstance(child, dict):
            yield child
            yield from _walk(child)


def parse_lsblk(text: str, mmc_type: Callable[[str], str | None] | None = None) -> list[Disk]:
    """Parse ``lsblk -J -b`` output. ``mmc_type(name)`` reads ``/sys/block/<name>/device/type``
    (``SD`` for a card, ``MMC`` for soldered eMMC); without it an mmc disk's removability is
    unknown."""
    try:
        data = json.loads((text or "").strip() or "null")
    except ValueError:
        return []
    if not isinstance(data, dict):
        return []
    out = []
    for d in data.get("blockdevices") or []:
        if not isinstance(d, dict) or d.get("type") != "disk":
            continue
        name = str(d.get("name") or "")
        if not name or re.search(r"(boot\d+|rpmb)$", name):
            continue
        try:
            size = int(d.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        tran = str(d.get("tran") or "").strip().lower()
        bus = "mmc" if name.startswith("mmcblk") else tran
        removable = _flag(d.get("rm"))
        if bus == "mmc":
            kind = (mmc_type(name) if mmc_type else None) or ""
            removable = {"sd": True, "mmc": False}.get(kind.strip().lower())
        children = list(_walk(d))
        mounts = [str(n.get("mountpoint")) for n in [d, *children] if n.get("mountpoint")]
        system = any(m == "[SWAP]" or not m.startswith(_SAFE_LINUX_MOUNT_PREFIXES)
                     for m in mounts)
        model = " ".join(x for x in (str(d.get("vendor") or "").strip(),
                                     str(d.get("model") or "").strip()) if x)
        out.append(Disk(
            device=str(d.get("path") or f"/dev/{name}"),
            model=model,
            size=size,
            bus=bus,
            serial=str(d.get("serial") or "").strip(),
            unique_id=str(d.get("wwn") or "").strip(),
            removable=removable,
            volumes=tuple(str(c.get("path") or f"/dev/{c.get('name')}") for c in children
                          if c.get("type") == "part"),
            mountpoints=tuple(mounts),
            is_system=system,
            is_read_only=bool(_flag(d.get("ro"))),
            has_pagefile="[SWAP]" in mounts,
            nested=any(c.get("type") != "part" for c in children),
        ))
    return out


def _read_mmc_type(name: str) -> str | None:  # pragma: no cover - reads sysfs
    try:
        return Path(f"/sys/block/{name}/device/type").read_text().strip()
    except OSError:
        return None


def list_disks(runner: Runner = run_command, platform: str = sys.platform,
               mmc_type: Callable[[str], str | None] | None = None) -> list[Disk]:
    """Every whole disk the OS reports (unfiltered). Empty list if listing fails."""
    if platform == "win32":
        res = run_powershell(runner, WINDOWS_DISKS_SCRIPT, timeout=45)
        return parse_windows_disks(res.stdout) if res.ok else []
    if platform.startswith("linux"):
        res = runner(LSBLK_ARGS, 20)
        return parse_lsblk(res.stdout, mmc_type or _read_mmc_type) if res.ok else []
    return []


# --------------------------------------------------------------------------- safety filter


class Reason:
    """Why a disk isn't offered (values returned by :func:`reject_reasons`)."""

    NO_DEVICE = "no_device"
    BUS = "bus"  # not SD / MMC / USB (NVMe, SATA, SAS, RAID, SCSI, virtual ...)
    NOT_REMOVABLE = "not_removable"  # USB but not removable media (external HDD/SSD)
    VIRTUAL = "virtual"  # Hyper-V / VHD / VM disk
    SYSTEM = "system"  # system, boot or OS disk, or mounted somewhere that isn't media
    PAGEFILE = "pagefile"  # holds the page file / swap
    NESTED = "nested"  # LVM / crypt / RAID on top
    APP_FILES = "app_files"  # holds the image file or the app itself
    NO_MEDIA = "no_media"  # empty card reader
    TOO_SMALL = "too_small"
    TOO_BIG = "too_big"
    OFFLINE = "offline"
    READ_ONLY = "read_only"
    STATUS = "status"  # OperationalStatus isn't Online


_OK_STATUS = {"online", "ok", ""}


def reject_reasons(disk: Disk, *, protect_paths: Iterable[str | Path] = (),
                   min_bytes: int = MIN_CARD_BYTES,
                   max_bytes: int = MAX_CARD_BYTES) -> list[str]:
    """Every reason not to write to ``disk``. An empty list means it's an SD card we may use.

    ``protect_paths``: files that must not be on the target (the image, the app).
    """
    r: list[str] = []
    if not disk.device or (disk.device.startswith("\\\\.\\") and disk.number is None):
        r.append(Reason.NO_DEVICE)
    if disk.bus not in ALLOWED_BUSES:
        r.append(Reason.BUS)
    if disk.removable is not True:  # fixed, external HDD/SSD, eMMC, or the OS didn't say
        r.append(Reason.NOT_REMOVABLE)
    if any(_VIRTUAL_RE.search(s) for s in (disk.model, disk.pnp_id, disk.location, disk.bus)):
        r.append(Reason.VIRTUAL)
    if disk.is_system or disk.is_boot:
        r.append(Reason.SYSTEM)
    if disk.has_pagefile:
        r.append(Reason.PAGEFILE)
    if disk.nested:
        r.append(Reason.NESTED)
    if _holds_paths(disk, protect_paths):
        r.append(Reason.APP_FILES)
    if disk.size <= 0:
        r.append(Reason.NO_MEDIA)
    elif disk.size < min_bytes:
        r.append(Reason.TOO_SMALL)
    if disk.size > max_bytes:
        r.append(Reason.TOO_BIG)
    if disk.is_offline:
        r.append(Reason.OFFLINE)
    if disk.is_read_only:
        r.append(Reason.READ_ONLY)
    if disk.status.strip().lower() not in _OK_STATUS:
        r.append(Reason.STATUS)
    return r


def _holds_paths(disk: Disk, paths: Iterable[str | Path]) -> bool:
    for p in paths:
        s = str(p)
        if not s:
            continue
        if disk.letters and re.match(r"^[A-Za-z]:", s) and s[0].upper() in disk.letters:
            return True
        if s.startswith("/") and any(
                m.startswith("/") and (s == m or s.startswith(m.rstrip("/") + "/"))
                for m in disk.mountpoints):
            return True
    return False


def candidate_disks(disks: Iterable[Disk], **kw) -> list[Disk]:
    """Only the disks that pass every rule, sorted by device."""
    return sorted((d for d in disks if not reject_reasons(d, **kw)),
                  key=lambda d: (d.number if d.number is not None else 0, d.device))


@dataclass(frozen=True)
class CardScan:
    cards: list[Disk]
    #: removable cards excluded only because they're locked / write-protected (UI hint)
    locked: list[Disk] = field(default_factory=list)
    #: removable cards excluded only because they're too small
    too_small: list[Disk] = field(default_factory=list)


def scan_cards(disks: Iterable[Disk], **kw) -> CardScan:
    """Candidates plus "almost" cards, so the UI can say "your card is locked"."""
    cards, locked, small = [], [], []
    for d in disks:
        reasons = reject_reasons(d, **kw)
        if not reasons:
            cards.append(d)
        elif reasons == [Reason.READ_ONLY]:
            locked.append(d)
        elif reasons == [Reason.TOO_SMALL]:
            small.append(d)
    key = (lambda d: (d.number if d.number is not None else 0, d.device))
    return CardScan(sorted(cards, key=key), locked, small)


def revalidate(target: DiskIdentity, disks: Sequence[Disk], *,
               protect_paths: Iterable[str | Path] = (), min_bytes: int = MIN_CARD_BYTES,
               max_bytes: int = MAX_CARD_BYTES) -> Disk:
    """Find ``target`` in a *fresh* listing and run the safety filter again.

    Raises ``card_removed`` (not there), ``card_changed`` (a different disk has that number
    or path now) or ``not_allowed`` / ``write_protected`` (it no longer passes).
    """
    same_place = [d for d in disks if d.device == target.device]
    if not same_place:
        raise FlashError("card_removed", f"{target.device} is gone")
    disk = same_place[0]
    if len(same_place) != 1 or not target.matches(disk):
        raise FlashError("card_changed", f"expected {target}, found {disk.identity}")
    reasons = reject_reasons(disk, protect_paths=protect_paths, min_bytes=min_bytes,
                             max_bytes=max_bytes)
    if reasons == [Reason.READ_ONLY]:
        raise FlashError("write_protected", f"{disk.device} is read-only")
    if reasons:
        raise FlashError("not_allowed", f"{disk.device}: {', '.join(reasons)}")
    return disk


# --------------------------------------------------------------------------- devices


class BlockDevice(Protocol):
    def write_at(self, offset: int, data: bytes | memoryview) -> None: ...
    def read_at(self, offset: int, size: int) -> bytes: ...
    def flush(self) -> None: ...
    def close(self) -> None: ...


class FileDevice:
    """A file or a Linux block device via ``os.open``; tests use it with a temp file."""

    def __init__(self, path: str | Path, *, extra_flags: int = 0) -> None:
        self.path = str(path)
        flags = os.O_RDWR | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
        self.fd = os.open(self.path, flags | extra_flags)

    def write_at(self, offset: int, data: bytes | memoryview) -> None:
        os.lseek(self.fd, offset, os.SEEK_SET)
        view = memoryview(data)
        while view:
            n = os.write(self.fd, view)
            if n <= 0:
                raise OSError(5, "short write")
            view = view[n:]

    def read_at(self, offset: int, size: int) -> bytes:
        os.lseek(self.fd, offset, os.SEEK_SET)
        parts = []
        while size > 0:
            b = os.read(self.fd, size)
            if not b:
                break
            parts.append(b)
            size -= len(b)
        return b"".join(parts)

    def flush(self) -> None:
        os.fsync(self.fd)

    def drop_caches(self) -> None:
        """Make the read-back come from the card, not the page cache (Linux)."""
        if sys.platform.startswith("linux"):  # pragma: no cover - Linux only
            with contextlib.suppress(OSError):
                import fcntl

                fcntl.ioctl(self.fd, 0x1261)  # BLKFLSBUF
            with contextlib.suppress(OSError, AttributeError):
                os.posix_fadvise(self.fd, 0, 0, os.POSIX_FADV_DONTNEED)

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1


# --------------------------------------------------------------------------- write + verify


@dataclass(frozen=True)
class FlashResult:
    bytes_written: int
    sha256: str  # of the raw (decompressed) image as written and read back
    seconds: float
    ejected: bool = False  # the helper ejected the card afterwards (best effort)


class _HashingReader(io.RawIOBase):
    """Wraps the compressed file: hashes and counts every byte lzma reads."""

    def __init__(self, f: io.BufferedReader) -> None:
        self._f = f
        self.sha = hashlib.sha256()
        self.count = 0

    def readable(self) -> bool:
        return True

    def readinto(self, b) -> int:  # type: ignore[override]
        n = self._f.readinto(b)
        if n:
            self.sha.update(memoryview(b)[:n])
            self.count += n
        return n or 0


def _read_full(stream: io.BufferedIOBase, size: int) -> bytes:
    parts, want = [], size
    while want > 0:
        b = stream.read(want)
        if not b:
            break
        parts.append(b)
        want -= len(b)
    return b"".join(parts)


def _check_cancel(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise FlashError("cancelled")


def write_image(image: LocalImage, device: BlockDevice, *, device_size: int | None = None,
                progress: ProgressFn | None = None, cancel: threading.Event | None = None,
                chunk: int = CHUNK) -> tuple[int, str]:
    """Stream-decompress ``image`` onto ``device``. Returns (raw bytes, raw sha256 hex).

    Also hashes the compressed bytes as they're read and checks them against the image's
    sha256 at the end, so a file swapped after verification is caught (``image_changed``).
    """
    if chunk % SECTOR:
        raise ValueError("chunk must be a multiple of the sector size")
    total = image.raw_size
    if device_size is not None and total > device_size:
        raise FlashError("too_small", f"image {total} bytes, card {device_size} bytes")
    report = progress or (lambda *_: None)
    raw_sha = hashlib.sha256()
    written = 0
    first: bytes | None = None
    try:
        with open(image.path, "rb") as f:
            src = _HashingReader(f)
            with lzma.open(io.BufferedReader(src, CHUNK), "rb") as xz:
                report(PHASE_WRITE, 0, total)
                while True:
                    _check_cancel(cancel)
                    try:
                        buf = _read_full(xz, chunk)
                    except (lzma.LZMAError, EOFError) as exc:
                        raise FlashError("image_bad", f"decompress: {exc}") from exc
                    if not buf:
                        break
                    raw_sha.update(buf)
                    n = len(buf)
                    if written + n > total:
                        raise FlashError("image_bad", "image is bigger than its xz index says")
                    if n % SECTOR:
                        buf += bytes(SECTOR - n % SECTOR)
                    try:
                        if first is None:
                            first = buf
                            # wipe the old partition table first; the real one goes last
                            device.write_at(0, bytes(len(buf)))
                        else:
                            device.write_at(written, buf)
                    except OSError as exc:
                        raise from_oserror(exc) from exc
                    written += n
                    report(PHASE_WRITE, written, total)
            if src.sha.hexdigest() != image.sha256.lower():
                raise FlashError("image_changed", "sha256 of the bytes read while writing "
                                 "doesn't match")
        if written != total:
            raise FlashError("image_bad", f"decompressed {written} bytes, index says {total}")
        _check_cancel(cancel)
        try:
            if first is not None:
                device.write_at(0, first)
            device.flush()
        except OSError as exc:
            raise from_oserror(exc) from exc
    except OSError as exc:  # opening/reading the image file
        raise FlashError("image_missing", str(exc)) from exc
    return written, raw_sha.hexdigest()


def verify_device(device: BlockDevice, length: int, expected_sha256: str, *,
                  progress: ProgressFn | None = None, cancel: threading.Event | None = None,
                  chunk: int = CHUNK) -> None:
    """Read back the first ``length`` bytes and compare their sha256. Raises
    ``verify_failed`` (or ``card_removed`` etc. on read errors)."""
    report = progress or (lambda *_: None)
    sha = hashlib.sha256()
    done = 0
    report(PHASE_VERIFY_CARD, 0, length)
    drop = getattr(device, "drop_caches", None)
    if drop is not None:
        drop()
    while done < length:
        _check_cancel(cancel)
        want = min(chunk, length - done)
        aligned = want + (-want % SECTOR)
        try:
            buf = device.read_at(done, aligned)
        except OSError as exc:
            raise from_oserror(exc, "verify_failed") from exc
        if len(buf) < want:
            raise FlashError("verify_failed", f"card returned {done + len(buf)} of "
                             f"{length} bytes")
        sha.update(memoryview(buf)[:want])
        done += want
        report(PHASE_VERIFY_CARD, done, length)
    got = sha.hexdigest()
    if got != expected_sha256:
        raise FlashError("verify_failed", f"read back sha256 {got}, wrote {expected_sha256}")


# --------------------------------------------------------------------------- orchestration


DeviceOpener = Callable[[Disk], BlockDevice]
#: context manager that dismounts / locks the card's volumes while it's held
Preparer = Callable[[Disk], contextlib.AbstractContextManager]


def run_flash(image: LocalImage, target: DiskIdentity, *,
              list_fn: Callable[[], list[Disk]],
              open_device: DeviceOpener,
              prepare: Preparer | None = None,
              progress: ProgressFn | None = None,
              cancel: threading.Event | None = None,
              protect_paths: Iterable[str | Path] = (),
              min_bytes: int = MIN_CARD_BYTES,
              max_bytes: int = MAX_CARD_BYTES,
              chunk: int = CHUNK,
              clock: Callable[[], float] = time.monotonic) -> FlashResult:
    """The whole job, as the elevated helper runs it.

    1. re-validate the target against a fresh listing (safety filter again)
    2. verify the ``.img.xz`` against its sha256
    3. re-validate again (the image check takes a while) and check the image fits
    4. dismount/lock the card, write, flush
    5. read back and verify
    """
    start = clock()
    protect = [str(image.path), *map(str, protect_paths)]
    kw = {"protect_paths": protect, "min_bytes": min_bytes, "max_bytes": max_bytes}
    revalidate(target, list_fn(), **kw)
    verify_image(image, progress=progress or (lambda *_: None), cancel=cancel)
    _check_cancel(cancel)
    disk = revalidate(target, list_fn(), **kw)
    if image.raw_size > disk.size:
        raise FlashError("too_small", f"image {image.raw_size} bytes, card {disk.size} bytes")
    prep = prepare(disk) if prepare else contextlib.nullcontext()
    with prep:
        try:
            dev = open_device(disk)
        except OSError as exc:
            raise from_oserror(exc, "access_denied") from exc
        try:
            written, sha = write_image(image, dev, device_size=disk.size, progress=progress,
                                       cancel=cancel, chunk=chunk)
            verify_device(dev, written, sha, progress=progress, cancel=cancel, chunk=chunk)
        finally:
            with contextlib.suppress(OSError):
                dev.close()
    return FlashResult(written, sha, clock() - start)


# --------------------------------------------------------------------------- time left


class TimeLeft:
    """Rough "time left" for the whole flash from the rates seen so far.

    Phases run in order verify-image -> write -> verify-card. Before a phase starts its
    rate is guessed: the card read-back at the write rate (cautious), the write unknown.
    """

    ORDER = (PHASE_VERIFY_IMAGE, PHASE_WRITE, PHASE_VERIFY_CARD)

    def __init__(self, sizes: dict[str, int], clock: Callable[[], float] = time.monotonic):
        self.sizes = dict(sizes)
        self.clock = clock
        self._start: dict[str, tuple[float, int]] = {}
        self.rates: dict[str, float] = {}

    def update(self, phase: str, done: int, total: int | None) -> float | None:
        now = self.clock()
        if total:
            self.sizes[phase] = total
        if phase not in self._start:
            self._start[phase] = (now, done)
        t0, d0 = self._start[phase]
        if now - t0 >= 1.0 and done > d0:
            self.rates[phase] = (done - d0) / (now - t0)
        rate = self.rates.get(phase)
        if rate is None:
            return None
        left = (self.sizes.get(phase, done) - done) / rate
        order = self.ORDER if phase in self.ORDER else (phase,)
        for later in order[order.index(phase) + 1:]:
            later_rate = self.rates.get(later) or self.rates.get(PHASE_WRITE)
            size = self.sizes.get(later)
            if not later_rate or size is None:
                return None
            left += size / later_rate
        return max(0.0, left)


def format_time_left(seconds: float | None) -> str:
    if seconds is None:
        return "Working out the time left…"
    if seconds < 60:
        return "Less than a minute left"
    minutes = round(seconds / 60)
    return "About 1 minute left" if minutes <= 1 else f"About {minutes} minutes left"
