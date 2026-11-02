"""The WeatherPi SD card image: checksum, size, local files and the GitHub release download.

The build (``image/build.sh``) publishes ``weatherpi-<version>.img.xz`` plus
``weatherpi-<version>.img.xz.sha256`` (``sha256sum`` format: ``<hex>  <name>``). The sha256 is
of the *compressed* file. The raw (decompressed) size comes from the xz index at the end of
the file, so progress and the "card too small" check work before anything is decompressed.

Downloads are cached under ``<home>/images/`` and resume from a ``.part`` file with an HTTP
``Range`` request. Nothing here imports Qt.
"""

from __future__ import annotations

import hashlib
import re
import threading
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from .. import GITHUB_REPO
from .flash_errors import FlashError

LATEST_RELEASE_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
IMAGE_NAME_RE = re.compile(r"^weatherpi-(?P<version>[A-Za-z0-9._+-]+)\.img\.xz$")
SHA_LINE_RE = re.compile(r"^\s*(?P<hex>[0-9a-fA-F]{64})(?:\s+\*?(?P<name>\S.*?))?\s*$")
READ_CHUNK = 1024 * 1024
XZ_HEADER_MAGIC = b"\xfd7zXZ\x00"
XZ_FOOTER_MAGIC = b"YZ"

#: (phase, bytes done, bytes total or None)
ProgressFn = Callable[[str, int, int | None], None]


def _noop(_phase: str, _done: int, _total: int | None) -> None:
    pass


# --------------------------------------------------------------------------- local images


@dataclass(frozen=True)
class LocalImage:
    """An ``.img.xz`` on disk with its expected sha256 (from the sibling ``.sha256``)."""

    path: Path
    sha256: str
    size: int  # compressed bytes
    raw_size: int  # decompressed bytes (from the xz index)

    @property
    def version(self) -> str:
        return image_version(self.path.name)


def image_version(name: str) -> str:
    """``weatherpi-2026.10.19-1.img.xz`` -> ``2026.10.19-1``; other names -> the file name."""
    m = IMAGE_NAME_RE.match(name)
    return m.group("version") if m else name


def parse_sha256_text(text: str, expected_name: str | None = None) -> str:
    """The hex digest from a ``sha256sum`` line. Raises FlashError ``bad_sha_file``.

    When the line names a file and ``expected_name`` is given, they must match (a checksum
    for a different file is worse than none).
    """
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        m = SHA_LINE_RE.match(line)
        if not m:
            break
        name = (m.group("name") or "").strip()
        if expected_name and name and Path(name).name != expected_name:
            raise FlashError("bad_sha_file", f"checksum file is for {name!r}, "
                             f"not {expected_name!r}")
        return m.group("hex").lower()
    raise FlashError("bad_sha_file", "no sha256 found in the checksum file")


def sha_file_for(image: Path) -> Path | None:
    """The checksum next to ``image``: ``<name>.sha256`` (build output) or ``<stem>.sha256``."""
    for cand in (image.with_name(image.name + ".sha256"), image.with_suffix(".sha256")):
        if cand.is_file():
            return cand
    return None


def load_local_image(path: str | Path) -> LocalImage:
    """Check an ``.img.xz`` the owner picked: sibling ``.sha256`` required, xz index readable.

    Doesn't hash the whole file (that happens just before writing, with progress).
    """
    p = Path(path)
    if not p.is_file():
        raise FlashError("image_missing", str(p))
    sha_path = sha_file_for(p)
    if sha_path is None:
        raise FlashError("no_sha_file", f"no {p.name}.sha256 next to the image")
    try:
        sha = parse_sha256_text(sha_path.read_text(encoding="utf-8", errors="replace"), p.name)
    except OSError as exc:
        raise FlashError("no_sha_file", str(exc)) from exc
    raw = xz_uncompressed_size(p)
    if raw is None or raw <= 0:
        raise FlashError("image_bad", f"{p.name} is not a complete .xz file")
    return LocalImage(p, sha, p.stat().st_size, raw)


def cached_images(images_dir: Path) -> list[LocalImage]:
    """Usable images in the cache folder, newest version first (unreadable ones skipped)."""
    if not images_dir.is_dir():
        return []
    out = []
    for p in images_dir.glob("weatherpi-*.img.xz"):
        try:
            out.append(load_local_image(p))
        except FlashError:
            continue
    out.sort(key=lambda i: _version_key(i.version), reverse=True)
    return out


def _version_key(version: str) -> tuple:
    return tuple((0, int(t)) if t.isdigit() else (1, t) for t in re.split(r"[.\-+_]", version))


def sha256_file(path: Path, *, progress: ProgressFn = _noop, phase: str = "verify-image",
                cancel: threading.Event | None = None, chunk: int = READ_CHUNK) -> str:
    total = path.stat().st_size
    h = hashlib.sha256()
    done = 0
    with open(path, "rb") as f:
        while True:
            if cancel is not None and cancel.is_set():
                raise FlashError("cancelled")
            buf = f.read(chunk)
            if not buf:
                break
            h.update(buf)
            done += len(buf)
            progress(phase, done, total)
    return h.hexdigest()


def verify_image(image: LocalImage, *, progress: ProgressFn = _noop,
                 cancel: threading.Event | None = None) -> None:
    """Hash the ``.img.xz`` and compare with its ``.sha256``. Raises ``sha_mismatch``."""
    try:
        got = sha256_file(image.path, progress=progress, cancel=cancel)
    except OSError as exc:
        raise FlashError("image_missing", str(exc)) from exc
    if got != image.sha256.lower():
        raise FlashError("sha_mismatch", f"{image.path.name}: sha256 {got}, "
                         f"expected {image.sha256}")


# --------------------------------------------------------------------------- xz index


def _read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    value = 0
    for i in range(9):
        if pos >= len(buf):
            raise ValueError("truncated varint")
        b = buf[pos]
        pos += 1
        value |= (b & 0x7F) << (7 * i)
        if not b & 0x80:
            if b == 0 and i:
                raise ValueError("non-minimal varint")
            return value, pos
    raise ValueError("varint too long")


def xz_uncompressed_size(path: Path) -> int | None:
    """Total decompressed size from the xz index(es), or None if the file isn't valid xz.

    Walks the streams backwards: footer -> index -> header, skipping stream padding, so
    concatenated multi-stream files (and ``xz -T0`` multi-block files) work. The index CRC32
    is checked.
    """
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            pos = f.tell()
            total = 0
            streams = 0
            while pos > 0:
                while pos >= 4:  # stream padding: zero bytes in multiples of 4
                    f.seek(pos - 4)
                    if f.read(4) != b"\x00\x00\x00\x00":
                        break
                    pos -= 4
                if pos < 24:
                    return None
                f.seek(pos - 12)
                footer = f.read(12)
                if footer[10:12] != XZ_FOOTER_MAGIC:
                    return None
                backward = (int.from_bytes(footer[4:8], "little") + 1) * 4
                index_start = pos - 12 - backward
                if index_start < 12:
                    return None
                f.seek(index_start)
                index = f.read(backward)
                if len(index) != backward or index[0] != 0:
                    return None
                if zlib.crc32(index[:-4]) != int.from_bytes(index[-4:], "little"):
                    return None
                count, i = _read_varint(index, 1)
                blocks = 0
                for _ in range(count):
                    unpadded, i = _read_varint(index, i)
                    uncompressed, i = _read_varint(index, i)
                    blocks += (unpadded + 3) & ~3
                    total += uncompressed
                stream_start = index_start - blocks - 12
                if stream_start < 0:
                    return None
                f.seek(stream_start)
                if f.read(6) != XZ_HEADER_MAGIC:
                    return None
                pos = stream_start
                streams += 1
            return total if streams else None
    except (OSError, ValueError):
        return None


# --------------------------------------------------------------------------- GitHub release


@dataclass(frozen=True)
class ReleaseImage:
    """The image attached to the latest GitHub release."""

    tag: str
    name: str  # weatherpi-<v>.img.xz
    url: str
    size: int
    sha_url: str
    published_at: str = ""

    @property
    def version(self) -> str:
        return image_version(self.name)


def parse_release(data: dict) -> ReleaseImage:
    """Pick the image + checksum assets from a ``releases/latest`` answer.

    Raises ``no_image_published`` when the release has no ``weatherpi-*.img.xz`` with a
    matching ``.sha256`` (newest version wins if there are several).
    """
    assets = {a.get("name"): a for a in data.get("assets") or [] if isinstance(a, dict)}
    images = [a for name, a in assets.items()
              if isinstance(name, str) and IMAGE_NAME_RE.match(name)
              and f"{name}.sha256" in assets]
    if not images:
        raise FlashError("no_image_published",
                         f"release {data.get('tag_name')!r} has no weatherpi-*.img.xz + .sha256")
    best = max(images, key=lambda a: _version_key(image_version(a["name"])))
    sha = assets[f"{best['name']}.sha256"]
    return ReleaseImage(
        tag=str(data.get("tag_name") or ""),
        name=best["name"],
        url=str(best.get("browser_download_url") or ""),
        size=int(best.get("size") or 0),
        sha_url=str(sha.get("browser_download_url") or ""),
        published_at=str(data.get("published_at") or ""),
    )


def _client(client: httpx.Client | None) -> httpx.Client:
    return client or httpx.Client(timeout=httpx.Timeout(30.0, read=60.0),
                                  follow_redirects=True,
                                  headers={"User-Agent": "WeatherPi-Companion"})


def latest_release(client: httpx.Client | None = None,
                   url: str = LATEST_RELEASE_URL) -> ReleaseImage:
    """Ask GitHub for the latest release's image. Raises FlashError (no release / offline)."""
    c = _client(client)
    try:
        r = c.get(url, headers={"Accept": "application/vnd.github+json"})
    except httpx.HTTPError as exc:
        raise FlashError("offline", f"GitHub: {exc}") from exc
    finally:
        if client is None:
            c.close()
    if r.status_code == 404:
        raise FlashError("no_image_published", "no release on GitHub yet")
    if r.status_code != 200:
        raise FlashError("download_failed", f"GitHub API: HTTP {r.status_code}")
    try:
        data = r.json()
    except ValueError as exc:
        raise FlashError("download_failed", "GitHub API: not JSON") from exc
    return parse_release(data if isinstance(data, dict) else {})


def download_release(release: ReleaseImage, images_dir: Path, *,
                     client: httpx.Client | None = None, progress: ProgressFn = _noop,
                     cancel: threading.Event | None = None,
                     chunk: int = READ_CHUNK) -> LocalImage:
    """Download (or resume) the release image into ``images_dir`` and verify it.

    Progress phases: ``download`` then ``verify-image``. The file is written to
    ``<name>.part`` and only renamed once its sha256 matches, so the cache never holds a
    bad image. Older cached images are removed afterwards to save space.
    """
    images_dir.mkdir(parents=True, exist_ok=True)
    final = images_dir / release.name
    part = images_dir / (release.name + ".part")
    sha_path = images_dir / (release.name + ".sha256")
    c = _client(client)
    try:
        try:
            r = c.get(release.sha_url)
        except httpx.HTTPError as exc:
            raise FlashError("download_failed", f"checksum: {exc}") from exc
        if r.status_code != 200:
            raise FlashError("download_failed", f"checksum: HTTP {r.status_code}")
        sha_text = r.text
        expected = parse_sha256_text(sha_text, release.name)

        if final.is_file() and final.stat().st_size == release.size:
            if sha256_file(final, progress=progress, cancel=cancel) == expected:
                sha_path.write_text(sha_text, encoding="utf-8")
                return load_local_image(final)
            final.unlink()

        have = part.stat().st_size if part.is_file() else 0
        if release.size and have > release.size:
            part.unlink()
            have = 0
        if not release.size or have < release.size:
            _fetch(c, release, part, have, progress, cancel, chunk)
    finally:
        if client is None:
            c.close()

    got = sha256_file(part, progress=progress, cancel=cancel)
    if got != expected:
        part.unlink(missing_ok=True)
        raise FlashError("sha_mismatch", f"downloaded {release.name}: sha256 {got}, "
                         f"expected {expected}")
    part.replace(final)
    sha_path.write_text(sha_text, encoding="utf-8")
    for old in images_dir.glob("weatherpi-*.img.xz*"):
        if old.name not in (final.name, sha_path.name):
            old.unlink(missing_ok=True)
    return load_local_image(final)


def _fetch(c: httpx.Client, release: ReleaseImage, part: Path, have: int,
           progress: ProgressFn, cancel: threading.Event | None, chunk: int) -> None:
    headers = {"Range": f"bytes={have}-"} if have else {}
    total = release.size or None
    try:
        with c.stream("GET", release.url, headers=headers) as r:
            if r.status_code == 416:  # nothing left to fetch; the sha check decides
                return
            if r.status_code == 206 and have:
                mode = "ab"
            elif r.status_code == 200:
                mode, have = "wb", 0
            else:
                raise FlashError("download_failed", f"image: HTTP {r.status_code}")
            with open(part, mode) as f:
                progress("download", have, total)
                for buf in r.iter_bytes(chunk):
                    if cancel is not None and cancel.is_set():
                        raise FlashError("cancelled", "download cancelled (kept for resume)")
                    f.write(buf)
                    have += len(buf)
                    progress("download", have, total)
    except httpx.HTTPError as exc:
        raise FlashError("download_failed", f"image: {exc}") from exc
    except OSError as exc:
        raise FlashError("disk_full" if getattr(exc, "errno", None) == 28 else "download_failed",
                         str(exc)) from exc
