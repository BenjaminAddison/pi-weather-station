"""Disk listing, the safety filter, write + verify on a temp file, and the run_flash order."""

from __future__ import annotations

import contextlib
import errno
import json
import threading

import pytest

from weatherpi_companion.host import flash
from weatherpi_companion.host.flash import (
    CHUNK,
    MAX_CARD_BYTES,
    PHASE_VERIFY_CARD,
    PHASE_VERIFY_IMAGE,
    PHASE_WRITE,
    SECTOR,
    Disk,
    FileDevice,
    Reason,
    TimeLeft,
    candidate_disks,
    format_time_left,
    list_disks,
    parse_lsblk,
    parse_windows_disks,
    reject_reasons,
    revalidate,
    run_flash,
    scan_cards,
    verify_device,
    write_image,
)
from weatherpi_companion.host.flash_errors import MESSAGES, FlashError, from_oserror

from .fakes import FakeRunner
from .test_flash_fakes import (
    ALL_WINDOWS,
    BUILTIN_SD_64G,
    EMMC_INTERNAL,
    EMPTY_READER,
    HYPERV_DISK,
    LOCKED_CARD,
    LSBLK,
    LSBLK_OLD,
    MMC_TYPES,
    NO_LETTER_16G,
    NVME_SYSTEM,
    OFFLINE_CARD,
    RAID_DISK,
    SATA_DATA,
    SD_2TB,
    TINY_CARD,
    UNKNOWN_MEDIA_USB,
    USB_HDD_1TB,
    USB_READER_32G,
    USB_SSD_240,
    VHD_ATTACHED,
    card_disk,
    make_device_file,
    make_image,
    raw_image_bytes,
    win_disk,
    windows_json,
)

SMALL_CHUNK = 64 * 1024


def win(*disks, **kw) -> list[Disk]:
    return parse_windows_disks(windows_json(list(disks), **kw))


def by_number(disks: list[Disk]) -> dict[int, Disk]:
    return {d.number: d for d in disks}


# --------------------------------------------------------------------------- Windows parsing


def test_parse_windows_full_listing():
    disks = by_number(win(*ALL_WINDOWS))
    assert len(disks) == len(ALL_WINDOWS)
    card = disks[1]
    assert card.device == r"\\.\PhysicalDrive1"
    assert card.bus == "usb" and card.removable is True and card.letters == ("E",)
    assert card.serial == "000000000819" and card.size == 31_914_983_424
    assert card.label == "Generic- SD/MMC USB Device (31.9 GB, E:)"
    assert disks[0].is_system and disks[0].is_boot and disks[0].has_pagefile
    assert disks[7].label.endswith("(15.9 GB, no drive letter)")
    assert disks[3].removable is False  # External hard disk media


def test_parse_windows_odd_shapes():
    assert parse_windows_disks("") == []
    assert parse_windows_disks("not json") == []
    assert parse_windows_disks("[]") == []
    # a single disk comes out as an object, not a list; PS 5.1 may wrap arrays in {"value"}
    one = json.dumps({"SystemDrive": "C:", "PageFiles": "C:\\pagefile.sys",
                      "Disks": {**USB_READER_32G, "Letters": {"value": ["e"], "Count": 1}}})
    [d] = parse_windows_disks(one)
    assert d.letters == ("E",) and d.number == 1
    # garbage entries are skipped
    bad = json.dumps({"Disks": [{"Number": "x"}, "nope", {"Size": 5}]})
    assert parse_windows_disks(bad) == []


def test_parse_windows_missing_flags_count_as_dangerous():
    d = dict(USB_READER_32G)
    for k in ("IsSystem", "IsBoot", "IsOffline", "IsReadOnly"):
        d.pop(k)
    [disk] = win(d)
    assert disk.is_system and disk.is_boot and disk.is_offline and disk.is_read_only
    assert reject_reasons(disk)


def test_system_drive_letter_and_pagefile_on_other_disks():
    # Windows installed on D: (SystemDrive), page file on E:, both disks look like cards
    d_card = win_disk(1, "Generic- SD/MMC USB Device", 31_914_983_424, "USB", letters=["D"])
    e_card = win_disk(2, "Generic- SD/MMC USB Device", 31_914_983_424, "USB", letters=["E"])
    disks = by_number(win(d_card, e_card, system_drive="D:", pagefiles=["E:\\pagefile.sys"]))
    assert Reason.SYSTEM in reject_reasons(disks[1])
    assert Reason.PAGEFILE in reject_reasons(disks[2])


def test_list_disks_windows_uses_powershell_and_survives_failure():
    r = FakeRunner().on("Get-Disk", windows_json([NVME_SYSTEM, USB_READER_32G]))
    disks = list_disks(r, "win32")
    assert [d.number for d in disks] == [0, 1]
    assert r.ran("powershell.exe") and r.ran("Win32_PageFileUsage")
    assert list_disks(FakeRunner().on("Get-Disk", "", returncode=1), "win32") == []
    assert list_disks(FakeRunner(), "darwin") == []


# --------------------------------------------------------------------------- safety filter


@pytest.mark.parametrize("fixture,expected", [
    (NVME_SYSTEM, {Reason.BUS, Reason.NOT_REMOVABLE, Reason.SYSTEM, Reason.PAGEFILE,
                   Reason.TOO_BIG}),
    (USB_HDD_1TB, {Reason.NOT_REMOVABLE, Reason.TOO_BIG}),
    (SD_2TB, {Reason.TOO_BIG}),
    (HYPERV_DISK, {Reason.BUS, Reason.NOT_REMOVABLE, Reason.VIRTUAL}),
    (VHD_ATTACHED, {Reason.BUS, Reason.NOT_REMOVABLE, Reason.VIRTUAL}),
    (SATA_DATA, {Reason.BUS, Reason.NOT_REMOVABLE, Reason.TOO_BIG}),
    (LOCKED_CARD, {Reason.READ_ONLY}),
    (OFFLINE_CARD, {Reason.OFFLINE, Reason.STATUS}),
    (EMPTY_READER, {Reason.NO_MEDIA, Reason.STATUS}),
    (USB_SSD_240, {Reason.NOT_REMOVABLE}),
    (TINY_CARD, {Reason.TOO_SMALL}),
    (UNKNOWN_MEDIA_USB, {Reason.NOT_REMOVABLE}),
    (EMMC_INTERNAL, {Reason.NOT_REMOVABLE}),
    (RAID_DISK, {Reason.BUS, Reason.NOT_REMOVABLE}),
])
def test_windows_exclusions(fixture, expected):
    [disk] = win(fixture)
    assert set(reject_reasons(disk)) == expected


@pytest.mark.parametrize("fixture", [USB_READER_32G, BUILTIN_SD_64G, NO_LETTER_16G])
def test_windows_cards_allowed(fixture):
    [disk] = win(fixture)
    assert reject_reasons(disk) == []


def test_candidates_from_the_full_windows_listing_are_only_cards():
    cands = candidate_disks(win(*ALL_WINDOWS))
    assert [d.number for d in cands] == [1, 2, 7]
    scan = scan_cards(win(*ALL_WINDOWS))
    assert [d.number for d in scan.cards] == [1, 2, 7]
    assert [d.number for d in scan.locked] == [9]
    assert [d.number for d in scan.too_small] == [13]


def test_virtual_model_names_excluded_even_on_an_allowed_bus():
    odd = win_disk(1, "Msft Virtual Disk", 31_914_983_424, "USB")
    vm = win_disk(2, "VMware Virtual USB Disk", 31_914_983_424, "USB")
    for d in win(odd, vm):
        assert Reason.VIRTUAL in reject_reasons(d)


def test_size_limits():
    at_limit = card_disk("x", size=MAX_CARD_BYTES)
    over = card_disk("x", size=MAX_CARD_BYTES + 1)
    real_256 = card_disk("x", size=256_087_425_024)  # some "256 GB" cards are > 256e9
    assert reject_reasons(at_limit) == [] and reject_reasons(real_256) == []
    assert reject_reasons(over) == [Reason.TOO_BIG]
    assert reject_reasons(card_disk("x", size=3_904_897_024)) == []  # a "4 GB" card
    assert reject_reasons(card_disk("x", size=3_000_000_000)) == [Reason.TOO_SMALL]


def test_disk_holding_the_image_or_app_is_excluded():
    card = card_disk("x", letters=("E",))
    assert reject_reasons(card, protect_paths=["E:\\images\\weatherpi.img.xz"]) == [
        Reason.APP_FILES]
    assert reject_reasons(card, protect_paths=["e:/x", "C:\\app"]) == [Reason.APP_FILES]
    assert reject_reasons(card, protect_paths=["C:\\Users\\x", ""]) == []
    lin = card_disk("/dev/sdb", letters=(), mountpoints=("/media/owner/bootfs",))
    assert reject_reasons(lin, protect_paths=["/media/owner/bootfs/w.img.xz"]) == [
        Reason.APP_FILES]
    assert reject_reasons(lin, protect_paths=["/media/owner/bootfs2/w.img.xz"]) == []


def test_unknown_removable_is_excluded_on_every_bus():
    for bus in ("usb", "sd", "mmc"):
        assert reject_reasons(card_disk("x", bus=bus, removable=None)) == [
            Reason.NOT_REMOVABLE]
    assert reject_reasons(card_disk("x", bus="sd", removable=True)) == []
    assert Reason.BUS in reject_reasons(card_disk("x", bus="", removable=True))
    assert Reason.NO_DEVICE in reject_reasons(card_disk("", number=None))
    assert Reason.NO_DEVICE in reject_reasons(card_disk(r"\\.\PhysicalDrive1", number=None))


# --------------------------------------------------------------------------- Linux


def test_parse_lsblk_and_filter():
    disks = {d.device: d for d in parse_lsblk(json.dumps(LSBLK), MMC_TYPES.get)}
    assert "/dev/loop0" not in disks and "/dev/mmcblk1boot0" not in disks
    reasons = {dev: set(reject_reasons(d)) for dev, d in disks.items()}
    assert reasons["/dev/sdb"] == set()  # USB card reader, auto-mounted under /media
    assert reasons["/dev/mmcblk0"] == set()  # built-in SD slot, sysfs type SD
    assert reasons["/dev/nvme0n1"] >= {Reason.BUS, Reason.SYSTEM}
    assert reasons["/dev/sda"] >= {Reason.BUS, Reason.SYSTEM}
    assert reasons["/dev/sdc"] >= {Reason.NOT_REMOVABLE, Reason.TOO_BIG}
    assert reasons["/dev/mmcblk1"] >= {Reason.NOT_REMOVABLE, Reason.SYSTEM}  # eMMC, /var
    assert reasons["/dev/mmcblk2"] == {Reason.NOT_REMOVABLE}  # unknown type: when in doubt, no
    assert reasons["/dev/sdd"] >= {Reason.NESTED, Reason.SYSTEM}  # LVM mounted on /data
    assert reasons["/dev/zram0"] >= {Reason.BUS, Reason.PAGEFILE}
    assert reasons["/dev/sde"] >= {Reason.PAGEFILE, Reason.SYSTEM}
    assert reasons["/dev/sdf"] == {Reason.READ_ONLY}
    sdb = disks["/dev/sdb"]
    assert sdb.model == "Generic- SD/MMC" and sdb.volumes == ("/dev/sdb1", "/dev/sdb2")
    assert sdb.label == "Generic- SD/MMC (31.9 GB, /media/owner/bootfs)"
    assert sorted(d.device for d in candidate_disks(disks.values())) == [
        "/dev/mmcblk0", "/dev/sdb"]


def test_parse_lsblk_old_string_values_and_garbage():
    [d] = parse_lsblk(json.dumps(LSBLK_OLD))
    assert d.size == 31_914_983_424 and d.removable is True and not d.is_read_only
    assert reject_reasons(d) == []
    assert parse_lsblk("") == [] and parse_lsblk("{bad") == [] and parse_lsblk("[]") == []


def test_list_disks_linux():
    r = FakeRunner().on("^lsblk", json.dumps(LSBLK))
    disks = list_disks(r, "linux", mmc_type=MMC_TYPES.get)
    assert any(d.device == "/dev/sdb" for d in disks)
    assert r.ran("lsblk -J -b")


# --------------------------------------------------------------------------- revalidate


def test_revalidate():
    disks = win(NVME_SYSTEM, USB_READER_32G)
    target = disks[1].identity
    assert revalidate(target, disks).number == 1
    with pytest.raises(FlashError) as e:
        revalidate(target, win(NVME_SYSTEM))
    assert e.value.code == "card_removed"
    swapped = win(NVME_SYSTEM, {**USB_READER_32G, "SerialNumber": "OTHER"})
    with pytest.raises(FlashError) as e:
        revalidate(target, swapped)
    assert e.value.code == "card_changed"
    resized = win(NVME_SYSTEM, {**USB_READER_32G, "Size": 63_864_569_856})
    with pytest.raises(FlashError) as e:
        revalidate(target, resized)
    assert e.value.code == "card_changed"
    locked = win(NVME_SYSTEM, {**USB_READER_32G, "IsReadOnly": True})
    with pytest.raises(FlashError) as e:
        revalidate(target, locked)
    assert e.value.code == "write_protected"
    # same identity but it now holds the page file -> refused
    with pytest.raises(FlashError) as e:
        revalidate(target, win(NVME_SYSTEM, USB_READER_32G, pagefiles=["E:\\pagefile.sys"]))
    assert e.value.code == "not_allowed"
    # an NVMe disk can never be a target, even if asked for by identity
    with pytest.raises(FlashError) as e:
        revalidate(disks[0].identity, disks)
    assert e.value.code == "not_allowed"


def test_identity_round_trip():
    ident = win(USB_READER_32G)[0].identity
    assert flash.DiskIdentity.from_dict(json.loads(json.dumps(ident.to_dict()))) == ident


# --------------------------------------------------------------------------- write + verify


class RecordingDevice(FileDevice):
    def __init__(self, path, **kw) -> None:
        super().__init__(path, **kw)
        self.writes: list[tuple[int, bytes]] = []

    def write_at(self, offset, data):
        self.writes.append((offset, bytes(data[:16])))
        super().write_at(offset, data)


class CorruptReadDevice(FileDevice):
    def read_at(self, offset, size):
        data = bytearray(super().read_at(offset, size))
        if offset == 0 and len(data) > 100:
            data[100] ^= 0xFF
        return bytes(data)


class ShortReadDevice(FileDevice):
    def read_at(self, offset, size):
        return super().read_at(offset, size)[: size // 2]


class FailingDevice(FileDevice):
    def __init__(self, path, exc) -> None:
        super().__init__(path)
        self.exc = exc

    def write_at(self, offset, data):
        raise self.exc


@pytest.fixture
def raw() -> bytes:
    return raw_image_bytes(5 * SMALL_CHUNK + 1000)  # partial last chunk, not sector-aligned


@pytest.fixture
def image(tmp_path, raw):
    return make_image(tmp_path / "img", raw)


@pytest.fixture
def dev_path(tmp_path):
    return make_device_file(tmp_path / "card.bin", 8 * SMALL_CHUNK)


def test_write_and_verify_round_trip(image, raw, dev_path):
    events = []
    dev = RecordingDevice(dev_path)
    try:
        n, sha = write_image(image, dev, chunk=SMALL_CHUNK,
                             progress=lambda *a: events.append(a))
        verify_device(dev, n, sha, chunk=SMALL_CHUNK, progress=lambda *a: events.append(a))
    finally:
        dev.close()
    assert n == len(raw)
    data = dev_path.read_bytes()
    assert data[:len(raw)] == raw
    pad_end = len(raw) + (-len(raw) % SECTOR)
    assert data[len(raw):pad_end] == bytes(pad_end - len(raw))  # padded with zeros
    assert data[pad_end:] == b"\xaa" * (len(data) - pad_end)  # the rest untouched
    # the first chunk is zeroed first and written last
    assert dev.writes[0] == (0, bytes(16)) and dev.writes[-1] == (0, raw[:16])
    assert all(off % SECTOR == 0 for off, _ in dev.writes)
    phases = [e[0] for e in events]
    assert phases[0] == PHASE_WRITE and phases[-1] == PHASE_VERIFY_CARD
    assert events[-1][1:] == (len(raw), len(raw))
    assert (PHASE_WRITE, len(raw), len(raw)) in events


def test_corrupted_read_back_is_caught(image, dev_path):
    dev = CorruptReadDevice(dev_path)
    try:
        n, sha = write_image(image, dev, chunk=SMALL_CHUNK)
        with pytest.raises(FlashError) as e:
            verify_device(dev, n, sha, chunk=SMALL_CHUNK)
    finally:
        dev.close()
    assert e.value.code == "verify_failed"
    assert "flash_verify_failed" == e.value.check().code


def test_short_read_back_is_caught(image, dev_path):
    dev = ShortReadDevice(dev_path)
    try:
        n, sha = write_image(image, dev, chunk=SMALL_CHUNK)
        with pytest.raises(FlashError) as e:
            verify_device(dev, n, sha, chunk=SMALL_CHUNK)
    finally:
        dev.close()
    assert e.value.code == "verify_failed"


def test_cancel_mid_write_leaves_no_partition_table(image, raw, dev_path):
    cancel = threading.Event()

    def progress(phase, done, total):
        if phase == PHASE_WRITE and done >= 2 * SMALL_CHUNK:
            cancel.set()

    dev = FileDevice(dev_path)
    try:
        with pytest.raises(FlashError) as e:
            write_image(image, dev, chunk=SMALL_CHUNK, progress=progress, cancel=cancel)
    finally:
        dev.close()
    assert e.value.code == "cancelled"
    data = dev_path.read_bytes()
    assert data[:SMALL_CHUNK] == bytes(SMALL_CHUNK)  # wiped, real first chunk never written
    assert data[SMALL_CHUNK:2 * SMALL_CHUNK] == raw[SMALL_CHUNK:2 * SMALL_CHUNK]


def test_too_small_device(image, dev_path):
    dev = FileDevice(dev_path)
    try:
        with pytest.raises(FlashError) as e:
            write_image(image, dev, device_size=image.raw_size - 1, chunk=SMALL_CHUNK)
    finally:
        dev.close()
    assert e.value.code == "too_small"
    assert dev_path.read_bytes()[:4] == b"\xaa" * 4  # nothing written


def test_image_changed_after_verification_is_caught(image, dev_path):
    wrong = type(image)(image.path, "0" * 64, image.size, image.raw_size)
    dev = FileDevice(dev_path)
    try:
        with pytest.raises(FlashError) as e:
            write_image(wrong, dev, chunk=SMALL_CHUNK)
    finally:
        dev.close()
    assert e.value.code == "image_changed"


def test_index_lying_about_size(tmp_path, image, dev_path):
    small = type(image)(image.path, image.sha256, image.size, image.raw_size - SMALL_CHUNK)
    dev = FileDevice(dev_path)
    try:
        with pytest.raises(FlashError) as e:
            write_image(small, dev, chunk=SMALL_CHUNK)
    finally:
        dev.close()
    assert e.value.code == "image_bad"


def test_chunk_must_be_sector_aligned(image, dev_path):
    with pytest.raises(ValueError):
        write_image(image, FileDevice(dev_path), chunk=1000)


class WinErr(OSError):
    def __init__(self, winerror: int) -> None:
        super().__init__(errno.EACCES, "Windows error")
        self.winerror = winerror


@pytest.mark.parametrize("exc,code", [
    (WinErr(19), "write_protected"),
    (WinErr(5), "access_denied"),
    (WinErr(1167), "card_removed"),
    (WinErr(21), "card_removed"),
    (WinErr(32), "card_in_use"),
    (OSError(errno.EROFS, "ro"), "write_protected"),
    (OSError(errno.EIO, "io"), "write_failed"),
    (OSError(123, "no medium"), "card_removed"),
    (OSError(errno.EBUSY, "busy"), "card_in_use"),
    (OSError(9999, "?"), "write_failed"),
])
def test_write_errors_become_plain_words(image, dev_path, exc, code):
    dev = FailingDevice(dev_path, exc)
    try:
        with pytest.raises(FlashError) as e:
            write_image(image, dev, chunk=SMALL_CHUNK)
    finally:
        dev.close()
    assert e.value.code == code
    check = e.value.check()
    assert not check.ok and check.message_for_owner == MESSAGES[code]


def test_from_oserror_default_and_messages():
    assert from_oserror(OSError(9999, "x"), "verify_failed").code == "verify_failed"
    err = FlashError("write_protected")
    assert "Lock" in err.message_for_owner and err.check().action == "retry"
    assert FlashError("weird_code").message_for_owner == FlashError("helper_failed") \
        .message_for_owner


# --------------------------------------------------------------------------- run_flash


class Lister:
    """Returns successive listings (the last one repeats)."""

    def __init__(self, *listings: list[Disk]) -> None:
        self.listings = list(listings)
        self.calls = 0

    def __call__(self) -> list[Disk]:
        self.calls += 1
        return self.listings[min(self.calls, len(self.listings)) - 1]


def test_run_flash_happy_path(image, raw, dev_path):
    card = card_disk(dev_path)
    events, prepared, opened = [], [], []

    @contextlib.contextmanager
    def prepare(disk):
        prepared.append(disk.device)
        yield

    def open_device(disk):
        opened.append(disk.device)
        return FileDevice(disk.device)

    lister = Lister([card])
    res = run_flash(image, card.identity, list_fn=lister, open_device=open_device,
                    prepare=prepare, progress=lambda *a: events.append(a), chunk=SMALL_CHUNK)
    assert res.bytes_written == len(raw)
    assert dev_path.read_bytes()[:len(raw)] == raw
    assert lister.calls == 2 and prepared == opened == [str(dev_path)]
    phases = []
    for p, *_ in events:
        if not phases or phases[-1] != p:
            phases.append(p)
    assert phases == [PHASE_VERIFY_IMAGE, PHASE_WRITE, PHASE_VERIFY_CARD]


def test_run_flash_rechecks_after_image_verification(image, dev_path):
    card = card_disk(dev_path)
    swapped = card_disk(dev_path, serial="SOMETHING-ELSE")
    opened = []
    with pytest.raises(FlashError) as e:
        run_flash(image, card.identity, list_fn=Lister([card], [swapped]),
                  open_device=lambda d: opened.append(d) or FileDevice(d.device),
                  chunk=SMALL_CHUNK)
    assert e.value.code == "card_changed" and opened == []
    assert dev_path.read_bytes()[:4] == b"\xaa" * 4


def test_run_flash_refuses_unsafe_target_without_touching_it(image, dev_path):
    system = card_disk(dev_path, bus="nvme", removable=False, is_system=True)
    with pytest.raises(FlashError) as e:
        run_flash(image, system.identity, list_fn=Lister([system]),
                  open_device=lambda d: pytest.fail("must not open"), chunk=SMALL_CHUNK)
    assert e.value.code == "not_allowed"


def test_run_flash_refuses_card_holding_the_image(tmp_path, raw, dev_path):
    img = make_image(tmp_path / "img", raw)
    letter = str(img.path)[0].upper()
    card = card_disk(dev_path, letters=(letter,))
    with pytest.raises(FlashError) as e:
        run_flash(img, card.identity, list_fn=Lister([card]),
                  open_device=lambda d: pytest.fail("must not open"), chunk=SMALL_CHUNK)
    assert e.value.code == "not_allowed" and "app_files" in e.value.detail


def test_run_flash_sha_mismatch_stops_before_writing(tmp_path, raw, dev_path):
    img = make_image(tmp_path / "img", raw, sha_override="ab" * 32)
    card = card_disk(dev_path)
    with pytest.raises(FlashError) as e:
        run_flash(img, card.identity, list_fn=Lister([card]),
                  open_device=lambda d: pytest.fail("must not open"), chunk=SMALL_CHUNK)
    assert e.value.code == "sha_mismatch"


def test_run_flash_image_bigger_than_card(image, dev_path):
    card = card_disk(dev_path, size=4_000_000_000)
    big = type(image)(image.path, image.sha256, image.size, 4_000_000_001)
    with pytest.raises(FlashError) as e:
        run_flash(big, card.identity, list_fn=Lister([card]),
                  open_device=lambda d: pytest.fail("must not open"), chunk=SMALL_CHUNK)
    assert e.value.code == "too_small"


def test_run_flash_open_denied(image, dev_path):
    card = card_disk(dev_path)

    def denied(_d):
        raise PermissionError(errno.EACCES, "denied")

    with pytest.raises(FlashError) as e:
        run_flash(image, card.identity, list_fn=Lister([card]), open_device=denied,
                  chunk=SMALL_CHUNK)
    assert e.value.code == "access_denied"


def test_run_flash_cancel_during_image_check(image, dev_path):
    card = card_disk(dev_path)
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(FlashError) as e:
        run_flash(image, card.identity, list_fn=Lister([card]), cancel=cancel,
                  open_device=lambda d: pytest.fail("must not open"), chunk=SMALL_CHUNK)
    assert e.value.code == "cancelled"


def test_default_chunk_is_4mib_and_aligned():
    assert CHUNK == 4 * 1024 * 1024 and CHUNK % SECTOR == 0


# --------------------------------------------------------------------------- time left


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def test_time_left_estimates():
    clock = Clock()
    tl = TimeLeft({PHASE_VERIFY_IMAGE: 600, PHASE_WRITE: 3000, PHASE_VERIFY_CARD: 3000},
                  clock=clock)
    assert tl.update(PHASE_VERIFY_IMAGE, 0, 600) is None
    clock.t = 2
    assert tl.update(PHASE_VERIFY_IMAGE, 200, 600) is None  # write rate still unknown
    clock.t = 3
    assert tl.update(PHASE_WRITE, 0, 3000) is None
    clock.t = 13
    left = tl.update(PHASE_WRITE, 100, 3000)  # 10 bytes/s
    assert left == pytest.approx(290 + 300)  # rest of write + read-back at write speed
    clock.t = 14
    tl.update(PHASE_VERIFY_CARD, 0, 3000)
    clock.t = 24
    assert tl.update(PHASE_VERIFY_CARD, 1000, 3000) == pytest.approx(20)
    assert tl.update("download", 5, 10) is None


def test_format_time_left():
    assert "Working out" in format_time_left(None)
    assert format_time_left(20) == "Less than a minute left"
    assert format_time_left(80) == "About 1 minute left"
    assert format_time_left(600) == "About 10 minutes left"
