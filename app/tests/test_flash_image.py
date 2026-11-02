"""Image checks (sha256, xz index), the GitHub release lookup and the resumable download."""

from __future__ import annotations

import hashlib
import lzma
import threading
from pathlib import Path

import httpx
import pytest

from weatherpi_companion.host import flash_image as fi
from weatherpi_companion.host.flash_errors import FlashError

from .test_flash_fakes import make_image, raw_image_bytes

# ..\images next to the repo (the main checkout or a worktree one level further down)
REAL_IMAGE = next((d / "images" / "weatherpi-2026.10.19-1.img.xz"
                   for d in Path(__file__).resolve().parents[3:5]
                   if (d / "images" / "weatherpi-2026.10.19-1.img.xz").is_file()), Path("-"))


# --------------------------------------------------------------------------- sha files


def test_parse_sha256_text_variants():
    h = "6dd7b0bd8ba93104d4729f8c0f7093719d6e57d75a3817166975f716b78b3a32"
    name = "weatherpi-2026.10.19-1.img.xz"
    assert fi.parse_sha256_text(f"{h}  {name}\n", name) == h
    assert fi.parse_sha256_text(f"{h.upper()} *{name}", name) == h
    assert fi.parse_sha256_text(f"\n{h}\n") == h
    assert fi.parse_sha256_text(f"{h}  /work/out/{name}", name) == h
    for bad in ("", "nothing here", "1234  x"):
        with pytest.raises(FlashError) as e:
            fi.parse_sha256_text(bad)
        assert e.value.code == "bad_sha_file"
    with pytest.raises(FlashError) as e:
        fi.parse_sha256_text(f"{h}  other.img.xz", name)
    assert e.value.code == "bad_sha_file"


def test_image_version():
    assert fi.image_version("weatherpi-2026.10.19-1.img.xz") == "2026.10.19-1"
    assert fi.image_version("custom.img.xz") == "custom.img.xz"


# --------------------------------------------------------------------------- xz index


def test_xz_size_single_stream(tmp_path):
    raw = raw_image_bytes(300_000)
    p = tmp_path / "a.img.xz"
    p.write_bytes(lzma.compress(raw, format=lzma.FORMAT_XZ))
    assert fi.xz_uncompressed_size(p) == len(raw)


def test_xz_size_multi_stream_with_padding(tmp_path):
    a, b = raw_image_bytes(100_000, 1), raw_image_bytes(70_001, 2)
    p = tmp_path / "multi.img.xz"
    p.write_bytes(lzma.compress(a, format=lzma.FORMAT_XZ) + bytes(8)
                  + lzma.compress(b, format=lzma.FORMAT_XZ) + bytes(4))
    assert fi.xz_uncompressed_size(p) == len(a) + len(b)
    # without padding Python's lzma reads both streams; with padding it stops early, which
    # write_image catches (decompressed size != index size -> image_bad)
    q = tmp_path / "multi2.img.xz"
    q.write_bytes(lzma.compress(a, format=lzma.FORMAT_XZ)
                  + lzma.compress(b, format=lzma.FORMAT_XZ))
    assert fi.xz_uncompressed_size(q) == len(a) + len(b) == len(lzma.decompress(q.read_bytes()))


def test_xz_size_rejects_bad_files(tmp_path):
    good = lzma.compress(raw_image_bytes(50_000), format=lzma.FORMAT_XZ)
    cases = {
        "empty": b"",
        "text": b"hello world, definitely not xz" * 10,
        "truncated": good[:-20],
        "bad_index_crc": good[:-13] + bytes([good[-13] ^ 0xFF]) + good[-12:],
        "gzip": b"\x1f\x8b" + bytes(100),
    }
    for name, data in cases.items():
        p = tmp_path / f"{name}.xz"
        p.write_bytes(data)
        assert fi.xz_uncompressed_size(p) is None, name
    assert fi.xz_uncompressed_size(tmp_path / "missing.xz") is None


@pytest.mark.skipif(not REAL_IMAGE.is_file(), reason="built image not on this PC")
def test_xz_size_real_multi_block_image():
    # built with xz -T0: 126 blocks; `xz -l` says 3,152,097,280 bytes
    assert fi.xz_uncompressed_size(REAL_IMAGE) == 3_152_097_280


# --------------------------------------------------------------------------- local images


def test_load_local_image(tmp_path):
    raw = raw_image_bytes(100_000)
    img = make_image(tmp_path, raw)
    assert img.raw_size == len(raw) and img.version == "2026.10.28-1"
    assert img.sha256 == hashlib.sha256(img.path.read_bytes()).hexdigest()
    fi.verify_image(img)


def test_load_local_image_needs_sha_file(tmp_path):
    p = make_image(tmp_path, raw_image_bytes(1000), sha_file=False)
    with pytest.raises(FlashError) as e:
        fi.load_local_image(p)
    assert e.value.code == "no_sha_file"
    # <stem>.sha256 (weatherpi-x.img.sha256) is accepted too
    p.with_suffix(".sha256").write_text(hashlib.sha256(p.read_bytes()).hexdigest() + "\n")
    assert fi.load_local_image(p).sha256
    with pytest.raises(FlashError) as e:
        fi.load_local_image(tmp_path / "nope.img.xz")
    assert e.value.code == "image_missing"


def test_load_local_image_rejects_non_xz(tmp_path):
    p = tmp_path / "weatherpi-1.img.xz"
    p.write_bytes(b"not an image" * 100)
    (tmp_path / "weatherpi-1.img.xz.sha256").write_text("ab" * 32)
    with pytest.raises(FlashError) as e:
        fi.load_local_image(p)
    assert e.value.code == "image_bad"


def test_verify_image_mismatch_and_cancel(tmp_path):
    img = make_image(tmp_path, raw_image_bytes(10_000), sha_override="cd" * 32)
    with pytest.raises(FlashError) as e:
        fi.verify_image(img)
    assert e.value.code == "sha_mismatch"
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(FlashError) as e:
        fi.verify_image(img, cancel=cancel)
    assert e.value.code == "cancelled"


def test_cached_images_newest_first(tmp_path):
    for v in ("2026.10.19-1", "2026.10.28-2", "2026.10.28-10"):
        make_image(tmp_path, raw_image_bytes(1000), v)
    (tmp_path / "weatherpi-2027.1.1-1.img.xz").write_bytes(b"no sha, skipped")
    assert [i.version for i in fi.cached_images(tmp_path)] == [
        "2026.10.28-10", "2026.10.28-2", "2026.10.19-1"]
    assert fi.cached_images(tmp_path / "missing") == []


# --------------------------------------------------------------------------- GitHub release


def release_json(names: list[str], tag: str = "v2026.10.28-1") -> dict:
    return {"tag_name": tag, "published_at": "2026-10-28T10:00:00Z", "assets": [
        {"name": n, "size": 1234,
         "browser_download_url": f"https://github.com/x/releases/download/{tag}/{n}"}
        for n in names]}


def test_parse_release_picks_image_and_sha():
    rel = fi.parse_release(release_json([
        "WeatherPi-Companion-Setup-1.0.exe", "WeatherPi-Companion-Setup-1.0.exe.sha256",
        "weatherpi-2026.10.28-1.img.xz", "weatherpi-2026.10.28-1.img.xz.sha256"]))
    assert rel.name == "weatherpi-2026.10.28-1.img.xz" and rel.version == "2026.10.28-1"
    assert rel.sha_url.endswith(".img.xz.sha256") and rel.size == 1234


def test_parse_release_without_image():
    for names in ([], ["WeatherPi-Companion-Setup-1.0.exe"],
                  ["weatherpi-2026.10.28-1.img.xz"]):  # image but no checksum
        with pytest.raises(FlashError) as e:
            fi.parse_release(release_json(names))
        assert e.value.code == "no_image_published"


def api_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


def test_latest_release_ok_404_error_offline():
    data = release_json(["weatherpi-1.img.xz", "weatherpi-1.img.xz.sha256"])
    seen = []

    def ok(req):
        seen.append(req)
        return httpx.Response(200, json=data)

    assert fi.latest_release(api_client(ok)).version == "1"
    assert seen[0].url.path == "/repos/BenjaminAddison/pi-weather-station/releases/latest"
    for status, code in ((404, "no_image_published"), (500, "download_failed")):
        with pytest.raises(FlashError) as e:
            fi.latest_release(api_client(lambda req, s=status: httpx.Response(s)))
        assert e.value.code == code

    def down(req):
        raise httpx.ConnectError("no route", request=req)

    with pytest.raises(FlashError) as e:
        fi.latest_release(api_client(down))
    assert e.value.code == "offline"
    assert e.value.check().action == "retry"


class FakeReleaseServer:
    """Serves one image + checksum; honours Range unless told not to; can drop mid-way."""

    def __init__(self, data: bytes, name: str = "weatherpi-2026.10.28-1.img.xz") -> None:
        self.data = data
        self.name = name
        self.sha = hashlib.sha256(data).hexdigest()
        self.ranges = True
        self.fail_after: int | None = None
        self.requests: list[httpx.Request] = []

    @property
    def release(self) -> fi.ReleaseImage:
        base = "https://github.com/x/releases/download/v1/"
        return fi.ReleaseImage("v1", self.name, base + self.name, len(self.data),
                               base + self.name + ".sha256")

    def handle(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        if req.url.path.endswith(".sha256"):
            return httpx.Response(200, text=f"{self.sha}  {self.name}\n")
        start = 0
        rng = req.headers.get("range")
        if rng and self.ranges:
            start = int(rng.split("=")[1].rstrip("-"))
            if start >= len(self.data):
                return httpx.Response(416)
        body = self.data[start:]
        if self.fail_after is not None:
            body = body[: self.fail_after]
            stream = _BrokenStream(body)
            return httpx.Response(206 if start else 200, stream=stream)
        return httpx.Response(206 if start else 200, content=body)

    def client(self) -> httpx.Client:
        return api_client(self.handle)


class _BrokenStream(httpx.SyncByteStream):
    def __init__(self, data: bytes) -> None:
        self.data = data

    def __iter__(self):
        yield self.data
        raise httpx.ReadError("connection reset")


def test_download_fresh_then_cached(tmp_path):
    data = lzma.compress(raw_image_bytes(200_000), format=lzma.FORMAT_XZ)
    srv = FakeReleaseServer(data)
    events = []
    img = fi.download_release(srv.release, tmp_path, client=srv.client(), chunk=10_000,
                              progress=lambda *a: events.append(a))
    assert img.path == tmp_path / srv.name and img.path.read_bytes() == data
    assert (tmp_path / f"{srv.name}.sha256").is_file()
    assert not (tmp_path / f"{srv.name}.part").exists()
    assert events[-1] == ("verify-image", len(data), len(data))
    assert ("download", len(data), len(data)) in events
    # second time: only the checksum is fetched, the image comes from the cache
    srv.requests.clear()
    again = fi.download_release(srv.release, tmp_path, client=srv.client())
    assert again.path == img.path
    assert [r.url.path.endswith(".sha256") for r in srv.requests] == [True]
    assert fi.cached_images(tmp_path)[0].version == "2026.10.28-1"


def test_download_resumes_after_a_drop(tmp_path):
    data = lzma.compress(raw_image_bytes(200_000), format=lzma.FORMAT_XZ)
    srv = FakeReleaseServer(data)
    srv.fail_after = 50_000
    with pytest.raises(FlashError) as e:
        fi.download_release(srv.release, tmp_path, client=srv.client(), chunk=10_000)
    assert e.value.code == "download_failed"
    part = tmp_path / f"{srv.name}.part"
    assert part.stat().st_size == 50_000
    srv.fail_after = None
    img = fi.download_release(srv.release, tmp_path, client=srv.client())
    assert img.path.read_bytes() == data
    assert srv.requests[-1].headers["range"] == "bytes=50000-"


def test_download_restarts_if_server_ignores_range(tmp_path):
    data = lzma.compress(raw_image_bytes(100_000), format=lzma.FORMAT_XZ)
    srv = FakeReleaseServer(data)
    srv.ranges = False
    (tmp_path / f"{srv.name}.part").write_bytes(b"x" * 1000)  # stale junk
    img = fi.download_release(srv.release, tmp_path, client=srv.client())
    assert img.path.read_bytes() == data


def test_download_sha_mismatch_discards_the_file(tmp_path):
    data = lzma.compress(raw_image_bytes(50_000), format=lzma.FORMAT_XZ)
    srv = FakeReleaseServer(data)
    srv.sha = "00" * 32
    with pytest.raises(FlashError) as e:
        fi.download_release(srv.release, tmp_path, client=srv.client())
    assert e.value.code == "sha_mismatch"
    assert list(tmp_path.iterdir()) == []


def test_download_cancel_keeps_part_for_resume(tmp_path):
    data = lzma.compress(raw_image_bytes(200_000), format=lzma.FORMAT_XZ)
    srv = FakeReleaseServer(data)
    cancel = threading.Event()

    def progress(phase, done, total):
        if phase == "download" and done >= 20_000:
            cancel.set()

    with pytest.raises(FlashError) as e:
        fi.download_release(srv.release, tmp_path, client=srv.client(), chunk=10_000,
                            progress=progress, cancel=cancel)
    assert e.value.code == "cancelled"
    assert (tmp_path / f"{srv.name}.part").is_file()


def test_download_removes_older_images(tmp_path):
    make_image(tmp_path, raw_image_bytes(1000), "2026.10.19-1")
    data = lzma.compress(raw_image_bytes(10_000), format=lzma.FORMAT_XZ)
    srv = FakeReleaseServer(data)
    fi.download_release(srv.release, tmp_path, client=srv.client())
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        srv.name, f"{srv.name}.sha256"]


def test_download_checksum_http_error(tmp_path):
    srv = FakeReleaseServer(b"x")
    client = api_client(lambda req: httpx.Response(404))
    with pytest.raises(FlashError) as e:
        fi.download_release(srv.release, tmp_path, client=client)
    assert e.value.code == "download_failed"
