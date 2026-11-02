"""What the Flash SD card screen calls (all blocking: run them in a worker thread)."""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import httpx

from . import flash_image
from .flash import CardScan, Disk, FlashResult, list_disks, revalidate, scan_cards
from .flash_errors import FlashError
from .flash_helper import Launcher, run_elevated
from .flash_image import LocalImage, ProgressFn, ReleaseImage
from .runner import Runner, run_command


class FlashService:
    """Image source, card listing and flashing for one app data folder (``home``)."""

    def __init__(self, home: Path, *, runner: Runner = run_command,
                 platform: str = sys.platform, client: httpx.Client | None = None,
                 launcher: Launcher | None = None, release_url: str | None = None) -> None:
        self.home = Path(home)
        self.runner = runner
        self.platform = platform
        self.client = client
        self.launcher = launcher
        self.release_url = release_url or flash_image.LATEST_RELEASE_URL

    @property
    def images_dir(self) -> Path:
        return self.home / "images"

    @property
    def helper_log(self) -> Path:
        return self.home / "logs" / "flash-helper.log"

    @property
    def supported(self) -> bool:
        return self.platform == "win32" or self.platform.startswith("linux")

    def _protect(self, *extra: str | Path) -> list[str]:
        return [str(self.home), sys.executable, *map(str, extra)]

    # -- image

    def cached_image(self) -> LocalImage | None:
        found = flash_image.cached_images(self.images_dir)
        return found[0] if found else None

    def latest_release(self) -> ReleaseImage:
        return flash_image.latest_release(self.client, self.release_url)

    def download(self, release: ReleaseImage, progress: ProgressFn | None = None,
                 cancel: threading.Event | None = None) -> LocalImage:
        return flash_image.download_release(release, self.images_dir, client=self.client,
                                            progress=progress or (lambda *_: None),
                                            cancel=cancel)

    def open_local(self, path: str | Path) -> LocalImage:
        return flash_image.load_local_image(path)

    # -- cards

    def disks(self) -> list[Disk]:
        return list_disks(self.runner, self.platform)

    def scan(self, image: LocalImage | None = None) -> CardScan:
        extra = [image.path] if image else []
        return scan_cards(self.disks(), protect_paths=self._protect(*extra))

    # -- flash

    def flash(self, image: LocalImage, disk: Disk, progress: ProgressFn | None = None,
              cancel: threading.Event | None = None) -> FlashResult:
        """Check the card once more here, then hand over to the elevated helper (which
        checks everything again itself)."""
        if not self.supported:
            raise FlashError("unsupported", self.platform)
        revalidate(disk.identity, self.disks(), protect_paths=self._protect(image.path))
        if image.raw_size > disk.size:
            raise FlashError("too_small", f"image {image.raw_size} bytes, card {disk.size}")
        self.helper_log.parent.mkdir(parents=True, exist_ok=True)
        return run_elevated(image, disk.identity, progress=progress, cancel=cancel,
                            launcher=self.launcher, log_path=self.helper_log)
