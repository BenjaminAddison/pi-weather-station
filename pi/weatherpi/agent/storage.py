"""Read-only root (D14): maintenance mode and committing rare /etc changes.

The root is Raspberry Pi OS's overlay FS: `overlayroot=tmpfs:recurse=0` in cmdline.txt mounts
the real root read-only at /media/root-ro with a RAM layer on top, so changes to / vanish at
reboot. Persistent data lives on /data (bind-mounted, see docs/pi-software.md "Read-only root").
`recurse=0` matters: overlayroot's default also puts a RAM layer over every other fstab mount,
which would make /data and its bind mounts forget everything at reboot.
"""

import logging
import os
from collections.abc import Callable

from weatherpi import paths
from weatherpi.agent.errors import ApiError
from weatherpi.agent.oscmd import Runner

log = logging.getLogger("weatherpi.agent")

OVERLAY_ARG = "overlayroot=tmpfs:recurse=0"
COMMIT_ETC = "weatherpi-commit-etc"


def _is_overlay_arg(arg: str) -> bool:
    # raspi-config writes plain `overlayroot=tmpfs`; count any tmpfs variant as "on"
    return arg.startswith("overlayroot=tmpfs")


def _args(path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8").split()
    except OSError:
        return []


class StorageManager:
    def __init__(self, runner: Runner, reboot: Callable[[], dict],
                 busy: Callable[[], bool] = lambda: False):
        self.r = runner
        self.reboot = reboot
        self.busy = busy

    @staticmethod
    def supported() -> bool:
        return paths.OVERLAYROOT_CONF.exists()

    def state(self) -> dict:
        on_next_boot = any(_is_overlay_arg(a) for a in _args(paths.BOOT_CMDLINE_TXT))
        return {
            "overlay_active": any(_is_overlay_arg(a) for a in _args(paths.PROC_CMDLINE)),
            "overlay_on_next_boot": on_next_boot,
            "maintenance": not on_next_boot,
        }

    def maintenance(self) -> dict:
        if not self.supported():
            raise ApiError(404, "not_found", "This WeatherPi has no read-only root.")
        return self.state()

    def set_maintenance(self, enabled: bool) -> tuple[int, dict]:
        """(status, body): 202 and a reboot when it changes, 200 when it's already so."""
        state = self.maintenance()
        if state["maintenance"] == enabled:
            return 200, state
        if self.busy():
            raise ApiError(409, "busy", "Wait for the WiFi or Tailscale change to finish.")
        self._write_cmdline(overlay=not enabled)
        self.reboot()
        return 202, {**self.state(), "rebooting": True}

    @staticmethod
    def _write_cmdline(overlay: bool) -> None:
        args = [a for a in _args(paths.BOOT_CMDLINE_TXT) if not a.startswith("overlayroot=")]
        if not args:
            raise ApiError(500, "internal", "cmdline.txt is missing or empty")
        if overlay:
            args.insert(0, OVERLAY_ARG)
        # cmdline.txt must stay one line; write in place and fsync (FAT, no atomic rename)
        with open(paths.BOOT_CMDLINE_TXT, "w", encoding="utf-8", newline="\n") as f:
            f.write(" ".join(args) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def commit_etc(self, *files: str) -> bool:
        """Copy changed /etc files to the read-only lower root so they survive a reboot.

        The helper does nothing when the overlay is off; it's missing on images before W8.
        """
        res = self.r.run([COMMIT_ETC, *files], timeout=60)
        if res.missing:
            return True
        if not res.ok:
            log.warning("%s %s failed: %s", COMMIT_ETC, " ".join(files), res.output[-300:])
        return res.ok

    def info(self) -> dict:
        """For /v1/diagnostics."""
        out: dict = {"overlay_supported": self.supported(),
                     "data_mounted": os.path.ismount(paths.DATA_MOUNT)}
        if out["overlay_supported"]:
            out.update(self.state())
        return out
