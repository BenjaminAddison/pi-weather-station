"""FlashService (what the screen calls) and the Windows raw-device class on a temp file."""

from __future__ import annotations

import contextlib
import sys

import httpx
import pytest

from weatherpi_companion.host import flash_helper as fh
from weatherpi_companion.host.flash import FileDevice, list_disks
from weatherpi_companion.host.flash_errors import FlashError
from weatherpi_companion.host.flash_service import FlashService

from .fakes import FakeRunner
from .test_flash_fakes import (
    LOCKED_CARD,
    NVME_SYSTEM,
    TINY_CARD,
    USB_HDD_1TB,
    USB_READER_32G,
    make_device_file,
    make_image,
    raw_image_bytes,
    windows_json,
)

CHUNK = 64 * 1024


def runner_for(*disks) -> FakeRunner:
    return FakeRunner().on("Get-Disk", windows_json(list(disks)))


def test_scan_lists_only_cards(tmp_path):
    svc = FlashService(tmp_path, runner=runner_for(NVME_SYSTEM, USB_READER_32G, USB_HDD_1TB,
                                                   LOCKED_CARD, TINY_CARD), platform="win32")
    scan = svc.scan()
    assert [d.number for d in scan.cards] == [1]
    assert [d.number for d in scan.locked] == [9] and [d.number for d in scan.too_small] == [13]


def test_scan_protects_the_app_data_drive(tmp_path):
    svc = FlashService(tmp_path, runner=runner_for(USB_READER_32G), platform="win32")
    svc.home = svc.home.__class__("E:/WeatherPi")  # app data on the card's drive letter
    assert svc.scan().cards == []


def test_unsupported_platform(tmp_path):
    svc = FlashService(tmp_path, runner=FakeRunner(), platform="darwin")
    assert svc.scan().cards == [] and not svc.supported
    with pytest.raises(FlashError) as e:
        svc.flash(None, None)  # type: ignore[arg-type]
    assert e.value.code == "unsupported"


def test_cached_image_and_open_local(tmp_path):
    svc = FlashService(tmp_path, runner=FakeRunner(), platform="win32")
    assert svc.cached_image() is None
    img = make_image(svc.images_dir, raw_image_bytes(1000))
    assert svc.cached_image() == img
    assert svc.open_local(img.path) == img


def test_latest_release_and_download_via_service(tmp_path):
    import hashlib
    import lzma

    data = lzma.compress(raw_image_bytes(20_000), format=lzma.FORMAT_XZ)
    name = "weatherpi-2026.10.28-1.img.xz"
    sha = hashlib.sha256(data).hexdigest()

    def handle(req: httpx.Request) -> httpx.Response:
        if req.url.host == "api.github.com":
            return httpx.Response(200, json={"tag_name": "v1", "assets": [
                {"name": name, "size": len(data), "browser_download_url": f"https://dl/{name}"},
                {"name": name + ".sha256", "size": 90,
                 "browser_download_url": f"https://dl/{name}.sha256"}]})
        if req.url.path.endswith(".sha256"):
            return httpx.Response(200, text=f"{sha}  {name}\n")
        return httpx.Response(200, content=data)

    client = httpx.Client(transport=httpx.MockTransport(handle))
    svc = FlashService(tmp_path, runner=FakeRunner(), platform="win32", client=client)
    rel = svc.latest_release()
    img = svc.download(rel)
    assert img.path.parent == tmp_path / "images" and img.version == "2026.10.28-1"
    assert svc.cached_image() == img


def test_flash_checks_the_card_then_runs_the_helper(tmp_path):
    raw = raw_image_bytes(3 * CHUNK + 100)
    img = make_image(tmp_path / "img", raw)
    dev = make_device_file(tmp_path / "card.bin", 5 * CHUNK)
    runner = runner_for(NVME_SYSTEM, USB_READER_32G)
    seen = []

    # the helper sees the same fake listing, but its "PhysicalDrive1" is the temp file
    deps = fh.HelperDeps(list_fn=lambda: list_disks(runner, "win32"),
                         open_device=lambda d: FileDevice(dev),
                         prepare=lambda d: contextlib.nullcontext(), chunk=CHUNK)

    def launcher(exe, args, cwd):
        seen.append(args)
        return fh.main(["--job", args[args.index("--job") + 1]], deps=deps)

    svc = FlashService(tmp_path / "home", runner=runner, platform="win32", launcher=launcher)
    card = svc.scan().cards[0]
    res = svc.flash(img, card)
    assert res.bytes_written == len(raw) and dev.read_bytes()[:len(raw)] == raw
    assert len(seen) == 1
    assert svc.helper_log.is_file()
    _close_log_handlers(svc.helper_log)


def test_flash_refuses_before_uac_if_the_card_changed(tmp_path):
    img = make_image(tmp_path / "img", raw_image_bytes(1000))
    first = FlashService(tmp_path, runner=runner_for(USB_READER_32G), platform="win32")
    card = first.scan().cards[0]
    swapped = {**USB_READER_32G, "SerialNumber": "ANOTHER"}
    svc = FlashService(tmp_path, runner=runner_for(swapped), platform="win32",
                       launcher=lambda *a: pytest.fail("no UAC prompt for a changed card"))
    with pytest.raises(FlashError) as e:
        svc.flash(img, card)
    assert e.value.code == "card_changed"


def test_flash_refuses_image_bigger_than_card(tmp_path):
    img = make_image(tmp_path / "img", raw_image_bytes(1000))
    svc = FlashService(tmp_path, runner=runner_for(USB_READER_32G), platform="win32",
                       launcher=lambda *a: pytest.fail("no UAC prompt"))
    card = svc.scan().cards[0]
    big = type(img)(img.path, img.sha256, img.size, card.size + 1)
    with pytest.raises(FlashError) as e:
        svc.flash(big, card)
    assert e.value.code == "too_small"


def _close_log_handlers(path) -> None:
    import logging

    root = logging.getLogger()
    for h in list(root.handlers):
        if isinstance(h, logging.FileHandler) and h.baseFilename == str(path):
            h.close()
            root.removeHandler(h)


# --------------------------------------------------------------------------- Windows device


@pytest.mark.skipif(sys.platform != "win32", reason="Win32 file API")
def test_windows_raw_device_on_a_temp_file(tmp_path):
    """The unbuffered CreateFileW/WriteFile/ReadFile path, on a temp file (never a disk)."""
    from weatherpi_companion.host.flash_platform import WindowsRawDevice

    path = make_device_file(tmp_path / "card.bin", 4 * CHUNK)
    dev = WindowsRawDevice(str(path), buffer_size=CHUNK)
    try:
        payload = raw_image_bytes(CHUNK + 4096)
        payload = payload[: len(payload) - len(payload) % 512]
        dev.write_at(512, payload)  # bigger than the bounce buffer -> two writes
        dev.flush()
        assert dev.read_at(512, len(payload)) == payload
        assert dev.read_at(0, 512) == b"\xaa" * 512
    finally:
        dev.close()
    assert path.read_bytes()[512:512 + len(payload)] == payload


@pytest.mark.skipif(sys.platform != "win32", reason="Win32 file API")
def test_windows_raw_device_errors_keep_their_codes(tmp_path):
    from weatherpi_companion.host.flash_errors import from_oserror
    from weatherpi_companion.host.flash_platform import WindowsRawDevice

    with pytest.raises(OSError) as e:
        WindowsRawDevice(str(tmp_path / "missing.bin"))
    assert e.value.winerror == 2 and from_oserror(e.value).code == "card_removed"
