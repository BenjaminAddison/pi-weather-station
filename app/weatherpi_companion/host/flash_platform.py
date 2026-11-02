"""Opening the card's raw device and getting its volumes out of the way (elevated helper only).

Windows: the card is ``\\\\.\\PhysicalDriveN``, opened with ``CreateFileW`` (unbuffered,
write-through) so errors keep their Windows codes (``ERROR_WRITE_PROTECT`` etc.; the C
runtime would turn them all into ``EACCES``). Every volume on the card is locked and
dismounted (``FSCTL_LOCK_VOLUME`` / ``FSCTL_DISMOUNT_VOLUME``) and the handles are held until
writing is done, because Windows refuses raw writes to sectors of a mounted volume. Afterwards
``IOCTL_DISK_UPDATE_PROPERTIES`` makes Windows read the new partition table.

Linux: the card is ``/dev/sdX`` / ``/dev/mmcblkN``; its partitions are unmounted with
``umount`` and the device is opened ``O_EXCL`` (fails if anything still has it mounted).
"""

from __future__ import annotations

import contextlib
import mmap
import os
import sys
import time
from collections.abc import Iterator

from .flash import CHUNK, Disk, FileDevice
from .flash_errors import FlashError
from .runner import Runner, run_command

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x1
FILE_SHARE_WRITE = 0x2
OPEN_EXISTING = 3
FILE_FLAG_NO_BUFFERING = 0x20000000
FILE_FLAG_WRITE_THROUGH = 0x80000000
FILE_BEGIN = 0
FSCTL_LOCK_VOLUME = 0x00090018
FSCTL_DISMOUNT_VOLUME = 0x00090020
IOCTL_DISK_UPDATE_PROPERTIES = 0x00070140
IOCTL_DISK_IS_WRITABLE = 0x00070024
IOCTL_STORAGE_EJECT_MEDIA = 0x002D4808


def _kernel32():  # pragma: no cover - Windows only
    import ctypes
    from ctypes import wintypes

    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                              wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    k.CreateFileW.restype = wintypes.HANDLE
    k.WriteFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                            ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    k.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                           ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    k.SetFilePointerEx.argtypes = [wintypes.HANDLE, ctypes.c_longlong,
                                   ctypes.POINTER(ctypes.c_longlong), wintypes.DWORD]
    k.FlushFileBuffers.argtypes = [wintypes.HANDLE]
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    k.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p,
                                  wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                  ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    for fn in (k.WriteFile, k.ReadFile, k.SetFilePointerEx, k.FlushFileBuffers,
               k.CloseHandle, k.DeviceIoControl):
        fn.restype = wintypes.BOOL
    return k


def _win_error():  # pragma: no cover - Windows only
    import ctypes

    return ctypes.WinError(ctypes.get_last_error())


def _win_open(path: str, access: int, flags: int = 0):  # pragma: no cover - Windows only
    import ctypes

    k = _kernel32()
    h = k.CreateFileW(path, access, FILE_SHARE_READ | FILE_SHARE_WRITE, None, OPEN_EXISTING,
                      flags, None)
    if h is None or h == ctypes.c_void_p(-1).value:
        raise _win_error()
    return k, h


def _ioctl(k, h, code: int) -> bool:  # pragma: no cover - Windows only
    from ctypes import byref, wintypes

    n = wintypes.DWORD()
    return bool(k.DeviceIoControl(h, code, None, 0, None, 0, byref(n), None))


class WindowsRawDevice:  # pragma: no cover - exercised on Windows by test_flash_platform
    """``\\\\.\\PhysicalDriveN`` (or any existing file) via unbuffered Win32 I/O.

    Unbuffered I/O needs sector-aligned offsets, sizes and memory; the bounce buffer is an
    anonymous ``mmap``, which is page-aligned.
    """

    def __init__(self, path: str, *, buffer_size: int = CHUNK) -> None:
        import ctypes

        self.path = path
        self._k, self._h = _win_open(path, GENERIC_READ | GENERIC_WRITE,
                                     FILE_FLAG_NO_BUFFERING | FILE_FLAG_WRITE_THROUGH)
        self._mm = mmap.mmap(-1, buffer_size)
        self._cbuf = (ctypes.c_char * buffer_size).from_buffer(self._mm)
        self._size = buffer_size

    def check_writable(self) -> None:
        """Raises OSError(ERROR_WRITE_PROTECT) for a locked card (physical drives only)."""
        if not _ioctl(self._k, self._h, IOCTL_DISK_IS_WRITABLE):
            raise _win_error()

    def _seek(self, offset: int) -> None:
        if not self._k.SetFilePointerEx(self._h, offset, None, FILE_BEGIN):
            raise _win_error()

    def write_at(self, offset: int, data: bytes | memoryview) -> None:
        from ctypes import byref, wintypes

        view = memoryview(data)
        while view:
            n = min(len(view), self._size)
            self._mm[:n] = view[:n]
            self._seek(offset)
            done = wintypes.DWORD()
            if not self._k.WriteFile(self._h, self._cbuf, n, byref(done), None):
                raise _win_error()
            if done.value != n:
                raise OSError(5, f"short write ({done.value} of {n})")
            view = view[n:]
            offset += n

    def read_at(self, offset: int, size: int) -> bytes:
        from ctypes import byref, wintypes

        parts = []
        while size > 0:
            n = min(size, self._size)
            self._seek(offset)
            got = wintypes.DWORD()
            if not self._k.ReadFile(self._h, self._cbuf, n, byref(got), None):
                raise _win_error()
            if got.value == 0:
                break
            parts.append(bytes(self._mm[:got.value]))
            size -= got.value
            offset += got.value
        return b"".join(parts)

    def flush(self) -> None:
        if not self._k.FlushFileBuffers(self._h):
            raise _win_error()

    def update_properties(self) -> None:
        _ioctl(self._k, self._h, IOCTL_DISK_UPDATE_PROPERTIES)

    def close(self) -> None:
        if self._h is not None:
            with contextlib.suppress(Exception):
                self.update_properties()
            self._k.CloseHandle(self._h)
            self._h = None
            del self._cbuf
            self._mm.close()


@contextlib.contextmanager
def windows_locked_volumes(disk: Disk, *, attempts: int = 10,
                           wait: float = 0.5) -> Iterator[None]:  # pragma: no cover - Windows
    """Lock and dismount every volume on the card; keep them locked until the block ends."""
    k = _kernel32()
    handles = []
    try:
        paths = {v.rstrip("\\") for v in disk.volumes}
        paths |= {f"\\\\.\\{letter}:" for letter in disk.letters}
        for path in sorted(paths):
            try:
                _, h = _win_open(path, GENERIC_READ | GENERIC_WRITE)
            except OSError:
                continue  # volume already gone (e.g. card has no file system)
            handles.append(h)
            locked = False
            for _ in range(attempts):
                if _ioctl(k, h, FSCTL_LOCK_VOLUME):
                    locked = True
                    break
                time.sleep(wait)
            _ioctl(k, h, FSCTL_DISMOUNT_VOLUME)
            if not locked and not _ioctl(k, h, FSCTL_LOCK_VOLUME):
                raise FlashError("card_in_use", f"couldn't lock {path}")
        yield
    finally:
        for h in handles:
            k.CloseHandle(h)


@contextlib.contextmanager
def linux_unmounted(disk: Disk, runner: Runner = run_command) -> Iterator[None]:
    """``umount`` every mounted partition of the card (the helper runs as root)."""
    for part in disk.volumes or ():
        res = runner(["umount", part], 30)
        if not res.ok and "not mounted" not in (res.stderr + res.stdout):
            raise FlashError("card_in_use", f"umount {part}: {res.stderr.strip()}")
    try:
        yield
    finally:
        runner(["blockdev", "--rereadpt", disk.device], 30)


def open_device(disk: Disk, platform: str = sys.platform):  # pragma: no cover - real device
    if platform == "win32":
        dev = WindowsRawDevice(disk.device)
        try:
            dev.check_writable()
        except OSError:
            dev.close()
            raise
        return dev
    if platform.startswith("linux"):
        return FileDevice(disk.device, extra_flags=os.O_EXCL)
    raise FlashError("unsupported", platform)


def eject(device: str, platform: str = sys.platform,
          runner: Runner = run_command) -> bool:  # pragma: no cover - real device
    """Eject the card after a good flash (best effort; False if it didn't work).

    Ejecting right away also stops Windows offering to "format" the Pi's Linux partition.
    On Windows this is what Explorer's Eject does: ``IOCTL_STORAGE_EJECT_MEDIA`` on the disk.
    """
    try:
        if platform == "win32":
            k, h = _win_open(device, GENERIC_READ)
            try:
                return _ioctl(k, h, IOCTL_STORAGE_EJECT_MEDIA)
            finally:
                k.CloseHandle(h)
        if platform.startswith("linux"):
            return runner(["eject", device], 30).ok
    except OSError:  # card already gone etc.; the helper logs anything else
        return False
    return False


def prepare(disk: Disk, platform: str = sys.platform):  # pragma: no cover - real device
    if platform == "win32":
        return windows_locked_volumes(disk)
    if platform.startswith("linux"):
        return linux_unmounted(disk)
    raise FlashError("unsupported", platform)
