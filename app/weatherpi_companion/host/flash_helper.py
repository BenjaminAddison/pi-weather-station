"""The elevated flash helper and the app side that launches it and listens to it.

Writing a raw disk needs admin; the app doesn't run as admin. So the app:

1. opens a listening TCP socket on ``127.0.0.1`` (random port) and makes a one-time token;
2. launches this module elevated (Windows: ``ShellExecuteExW`` verb ``runas`` = the UAC
   prompt; Linux: ``pkexec``) with a single ``--job`` argument: base64url JSON holding the
   image path, its expected sha256, the target disk's identity, the port and the token;
3. accepts connections until one starts with ``{"type": "hello", "token": <token>}``
   (anything else is dropped), then reads JSON lines:
   ``{"type": "progress", "phase", "done", "total"}`` and finally
   ``{"type": "result", "ok": true, "bytes", "sha256", "seconds", "ejected"}`` or
   ``{"type": "result", "ok": false, "code", "detail"}``.
   The app cancels by sending ``{"type": "cancel"}``; the helper also cancels if the
   connection drops (the app was closed).

The helper never trusts its arguments: it lists the disks itself, re-runs the whole safety
filter on the target (plus the identity check) right before writing, re-reads the image's
``.sha256`` (it must match the job's), and verifies the image before writing.

How the helper is started (see :func:`helper_command`):

* from source: ``<python> -c "<add app dir to sys.path; run main()>" --job <job>``
* frozen (PyInstaller): ``<WeatherPi Companion.exe> --flash-helper --job <job>``; the app's
  ``__main__`` must hand ``--flash-helper`` to :func:`main` before starting Qt.

It's also runnable directly: ``python -m weatherpi_companion.host.flash_helper --job ...``.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import dataclasses
import hmac
import json
import logging
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .flash import (
    CHUNK,
    MAX_CARD_BYTES,
    MIN_CARD_BYTES,
    DeviceOpener,
    Disk,
    DiskIdentity,
    FlashResult,
    Preparer,
    list_disks,
    run_flash,
)
from .flash_errors import FlashError
from .flash_image import LocalImage, ProgressFn, load_local_image

HELPER_FLAG = "--flash-helper"
HELPER_MODULE = "weatherpi_companion.host.flash_helper"
PROGRESS_INTERVAL = 0.2  # seconds between progress messages (phase changes always sent)

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- job


@dataclass(frozen=True)
class FlashJob:
    image: str
    sha256: str
    target: DiskIdentity
    port: int
    token: str
    log_path: str = ""

    def encode(self) -> str:
        data = {"image": self.image, "sha256": self.sha256, "target": self.target.to_dict(),
                "port": self.port, "token": self.token, "log_path": self.log_path}
        raw = json.dumps(data, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    @classmethod
    def decode(cls, text: str) -> FlashJob:
        raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
        d = json.loads(raw)
        return cls(str(d["image"]), str(d["sha256"]).lower(),
                   DiskIdentity.from_dict(d["target"]), int(d["port"]), str(d["token"]),
                   str(d.get("log_path") or ""))


def _app_dir() -> str:
    """The folder holding the ``weatherpi_companion`` package."""
    return str(Path(__file__).resolve().parents[2])


def helper_command(job_arg: str, *, frozen: bool | None = None,
                   executable: str | None = None,
                   app_dir: str | None = None) -> tuple[str, list[str], str]:
    """(program, arguments, working folder) that starts the helper, from source or frozen.

    An elevated process doesn't inherit ``PYTHONPATH`` or the working folder reliably, so
    from source the app folder is put on ``sys.path`` explicitly.
    """
    exe = executable or sys.executable
    if frozen if frozen is not None else bool(getattr(sys, "frozen", False)):
        return exe, [HELPER_FLAG, "--job", job_arg], str(Path(exe).parent)
    where = app_dir or _app_dir()
    code = (f"import sys;sys.path.insert(0,{where!r});"
            f"from {HELPER_MODULE} import main;sys.exit(main())")
    return exe, ["-c", code, "--job", job_arg], where


# --------------------------------------------------------------------------- JSON lines


class LineChannel:
    """JSON objects, one per line, over a socket. Thread-safe ``send``."""

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self._buf = b""
        self._lock = threading.Lock()
        self.closed = False

    def send(self, obj: dict) -> bool:
        data = (json.dumps(obj, separators=(",", ":")) + "\n").encode()
        with self._lock:
            try:
                self.sock.sendall(data)
                return True
            except OSError:
                self.closed = True
                return False

    def recv(self, timeout: float | None) -> dict | None:
        """The next message; None on timeout. Raises EOFError when the peer has gone."""
        while b"\n" not in self._buf:
            self.sock.settimeout(timeout)
            try:
                chunk = self.sock.recv(65536)
            except TimeoutError:
                return None
            except OSError as exc:
                self.closed = True
                raise EOFError(str(exc)) from exc
            if not chunk:
                self.closed = True
                raise EOFError("connection closed")
            self._buf += chunk
            if len(self._buf) > 1 << 20:
                raise EOFError("line too long")
        line, self._buf = self._buf.split(b"\n", 1)
        try:
            obj = json.loads(line)
        except ValueError:
            return {}
        return obj if isinstance(obj, dict) else {}

    def close(self) -> None:
        self.closed = True
        with contextlib.suppress(OSError):
            self.sock.close()


# --------------------------------------------------------------------------- helper side


@dataclass
class HelperDeps:
    """What the helper touches; tests swap in a fake disk list and a temp-file device."""

    list_fn: Callable[[], list[Disk]] = field(default_factory=lambda: list_disks)
    open_device: DeviceOpener | None = None
    prepare: Preparer | None = None
    eject: Callable[[str], bool] | None = None  # device path -> ejected?
    min_bytes: int = MIN_CARD_BYTES
    max_bytes: int = MAX_CARD_BYTES
    protect_paths: Sequence[str] = ()
    chunk: int = CHUNK

    def resolved(self) -> HelperDeps:
        if self.open_device is None or self.prepare is None:  # pragma: no cover - real device
            from . import flash_platform

            # the real eject only comes with the real device: a test that fakes the device
            # but not the eject must never eject a real disk
            return dataclasses.replace(
                self, open_device=self.open_device or flash_platform.open_device,
                prepare=self.prepare or flash_platform.prepare,
                eject=self.eject if self.open_device else (self.eject or flash_platform.eject))
        return self


def serve_job(job: FlashJob, deps: HelperDeps, *, progress: ProgressFn,
              cancel: threading.Event) -> FlashResult:
    """Run one job with the helper's own checks (never trusting the job's word)."""
    image = load_local_image(job.image)
    if image.sha256 != job.sha256.lower():
        raise FlashError("bad_sha_file", "the image's .sha256 changed since it was chosen")
    d = deps.resolved()
    protect = [sys.executable, _app_dir(), *(p for p in [job.log_path] if p),
               *d.protect_paths]
    res = run_flash(image, job.target, list_fn=d.list_fn, open_device=d.open_device,
                    prepare=d.prepare, progress=progress, cancel=cancel,
                    protect_paths=protect, min_bytes=d.min_bytes, max_bytes=d.max_bytes,
                    chunk=d.chunk)
    try:
        ejected = bool(d.eject and d.eject(job.target.device))
    except Exception:
        log.exception("eject failed")
        ejected = False
    log.info("eject after flash: %s", "ok" if ejected else "not done")
    return dataclasses.replace(res, ejected=ejected)


def _throttled(chan: LineChannel, interval: float = PROGRESS_INTERVAL) -> ProgressFn:
    last = {"t": 0.0, "phase": ""}

    def send(phase: str, done: int, total: int | None) -> None:
        now = time.monotonic()
        final = total is not None and done >= total
        if phase != last["phase"] or final or now - last["t"] >= interval:
            last.update(t=now, phase=phase)
            chan.send({"type": "progress", "phase": phase, "done": done, "total": total})

    return send


def run_helper(job: FlashJob, chan: LineChannel, deps: HelperDeps | None = None) -> int:
    """Talk to the app over ``chan`` and run the job. Returns the process exit code."""
    chan.send({"type": "hello", "token": job.token, "pid": os.getpid()})
    cancel = threading.Event()

    def listen() -> None:
        while not cancel.is_set():
            try:
                msg = chan.recv(1.0)
            except EOFError:
                log.warning("app connection closed; cancelling")
                cancel.set()
                return
            if msg and msg.get("type") == "cancel":
                log.info("cancel requested by the app")
                cancel.set()

    threading.Thread(target=listen, name="flash-helper-listen", daemon=True).start()
    try:
        res = serve_job(job, deps or HelperDeps(), progress=_throttled(chan), cancel=cancel)
    except FlashError as exc:
        log.warning("flash failed: %s", exc)
        chan.send({"type": "result", "ok": False, "code": exc.code, "detail": exc.detail})
        return 2
    except Exception as exc:
        log.exception("flash helper crashed")
        chan.send({"type": "result", "ok": False, "code": "helper_failed",
                   "detail": f"{type(exc).__name__}: {exc}"})
        return 3
    log.info("flash ok: %d bytes, sha256 %s, %.1f s", res.bytes_written, res.sha256,
             res.seconds)
    chan.send({"type": "result", "ok": True, "bytes": res.bytes_written, "sha256": res.sha256,
               "seconds": res.seconds, "ejected": res.ejected})
    return 0


def main(argv: Sequence[str] | None = None, deps: HelperDeps | None = None) -> int:
    """Elevated entry point. Exit codes: 0 ok, 2 flash failed, 3 crashed, 4 bad call."""
    parser = argparse.ArgumentParser(prog="weatherpi-flash-helper")
    parser.add_argument(HELPER_FLAG, action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--job", required=True)
    args, _ = parser.parse_known_args(list(argv) if argv is not None else sys.argv[1:])
    try:
        job = FlashJob.decode(args.job)
    except (ValueError, KeyError, TypeError) as exc:
        print(f"bad --job: {exc}", file=sys.stderr)
        return 4
    if job.log_path:
        with contextlib.suppress(OSError):
            Path(job.log_path).parent.mkdir(parents=True, exist_ok=True)
            handler = logging.FileHandler(job.log_path, encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            logging.getLogger().addHandler(handler)
            logging.getLogger().setLevel(logging.INFO)
    log.info("flash helper started (pid %d) for %s", os.getpid(), job.target)
    try:
        sock = socket.create_connection(("127.0.0.1", job.port), timeout=15)
    except OSError as exc:
        log.error("can't reach the app on port %d: %s", job.port, exc)
        return 4
    chan = LineChannel(sock)
    try:
        return run_helper(job, chan, deps)
    finally:
        chan.close()


# --------------------------------------------------------------------------- app side

#: (program, arguments, working folder) -> exit code. Blocks until the helper exits.
#: Raises FlashError("uac_cancelled") when the owner says no.
Launcher = Callable[[str, list[str], str], int | None]

ERROR_CANCELLED = 1223


def _runas_windows(exe: str, args: list[str], cwd: str) -> int | None:  # pragma: no cover
    """ShellExecuteExW(verb="runas") with a hidden window; waits for the helper to exit."""
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
    see_mask_noasync = 0x00000100
    sw_hide = 0
    info = SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = see_mask_nocloseprocess | see_mask_noasync
    info.lpVerb = "runas"
    info.lpFile = exe
    info.lpParameters = subprocess.list2cmdline(args)
    info.lpDirectory = cwd
    info.nShow = sw_hide
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        err = ctypes.get_last_error()
        if err == ERROR_CANCELLED:
            raise FlashError("uac_cancelled", "the administrator prompt was refused")
        raise FlashError("helper_failed", f"ShellExecuteEx failed ({err})")
    if not info.hProcess:
        return None
    try:
        kernel32.WaitForSingleObject(info.hProcess, 0xFFFFFFFF)
        code = wintypes.DWORD()
        kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(code))
        return int(code.value)
    finally:
        kernel32.CloseHandle(info.hProcess)


def _pkexec_linux(exe: str, args: list[str], cwd: str) -> int | None:  # pragma: no cover
    try:
        proc = subprocess.run(["pkexec", exe, *args], cwd=cwd, check=False,
                              stdin=subprocess.DEVNULL)
    except FileNotFoundError as exc:
        raise FlashError("unsupported", "pkexec isn't installed") from exc
    if proc.returncode in (126, 127):  # dismissed / not authorised
        raise FlashError("uac_cancelled", f"pkexec exit {proc.returncode}")
    return proc.returncode


def default_launcher(platform: str = sys.platform) -> Launcher:
    if platform == "win32":
        return _runas_windows
    if platform.startswith("linux"):
        return _pkexec_linux
    raise FlashError("unsupported", platform)


def run_elevated(image: LocalImage, target: DiskIdentity, *,
                 progress: ProgressFn | None = None,
                 cancel: threading.Event | None = None,
                 launcher: Launcher | None = None,
                 log_path: str | Path = "",
                 frozen: bool | None = None,
                 poll: float = 0.25,
                 exit_grace: float = 10.0) -> FlashResult:
    """Launch the helper elevated, relay its progress, return its result (or raise).

    Cancelling before the helper connects just stops waiting (if the owner approves the UAC
    prompt later, the helper finds nobody listening and exits without writing).
    """
    cancel = cancel or threading.Event()
    report = progress or (lambda *_: None)
    launch = launcher or default_launcher()
    token = secrets.token_urlsafe(32)
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        srv.bind(("127.0.0.1", 0))
        srv.listen(4)
        srv.settimeout(poll)
        job = FlashJob(str(image.path), image.sha256, target, srv.getsockname()[1], token,
                       str(log_path))
        exe, args, cwd = helper_command(job.encode(), frozen=frozen)

        outcome: dict[str, object] = {}
        exited = threading.Event()

        def run_launcher() -> None:
            try:
                outcome["code"] = launch(exe, args, cwd)
            except FlashError as exc:
                outcome["error"] = exc
            except Exception as exc:  # noqa: BLE001
                outcome["error"] = FlashError("helper_failed", f"launch: {exc}")
            finally:
                exited.set()

        threading.Thread(target=run_launcher, name="flash-helper-launch", daemon=True).start()
        chan = _accept_helper(srv, token, cancel, exited, outcome, poll)
    finally:
        srv.close()
    try:
        return _relay(chan, report, cancel, exited, outcome, poll, exit_grace)
    finally:
        chan.close()


def _launcher_error(outcome: dict, what: str) -> FlashError:
    err = outcome.get("error")
    if isinstance(err, FlashError):
        return err
    return FlashError("helper_failed", f"{what} (exit code {outcome.get('code')})")


def _accept_helper(srv: socket.socket, token: str, cancel: threading.Event,
                   exited: threading.Event, outcome: dict, poll: float) -> LineChannel:
    while True:
        if cancel.is_set():
            raise FlashError("cancelled", "stopped before the helper started")
        if exited.is_set():
            raise _launcher_error(outcome, "the helper stopped before it connected")
        try:
            conn, _addr = srv.accept()
        except TimeoutError:
            continue
        chan = LineChannel(conn)
        try:
            hello = chan.recv(10.0)
        except EOFError:
            hello = None
        if (hello and hello.get("type") == "hello"
                and hmac.compare_digest(str(hello.get("token", "")), token)):
            return chan
        log.warning("dropped a connection without the right token")
        chan.close()


def _relay(chan: LineChannel, report: ProgressFn, cancel: threading.Event,
           exited: threading.Event, outcome: dict, poll: float,
           exit_grace: float) -> FlashResult:
    cancel_sent = False
    while True:
        if cancel.is_set() and not cancel_sent:
            chan.send({"type": "cancel"})
            cancel_sent = True
        try:
            msg = chan.recv(poll)
        except EOFError:
            exited.wait(exit_grace)
            if cancel_sent:
                raise FlashError("cancelled") from None
            raise _launcher_error(outcome, "the helper stopped without a result") from None
        if not msg:
            continue
        kind = msg.get("type")
        if kind == "progress":
            try:
                total = msg.get("total")
                report(str(msg.get("phase")), int(msg.get("done") or 0),
                       int(total) if total is not None else None)
            except (TypeError, ValueError):
                continue
        elif kind == "result":
            if msg.get("ok"):
                return FlashResult(int(msg.get("bytes") or 0), str(msg.get("sha256") or ""),
                                   float(msg.get("seconds") or 0.0),
                                   bool(msg.get("ejected", False)))
            raise FlashError(str(msg.get("code") or "helper_failed"),
                             str(msg.get("detail") or ""))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
