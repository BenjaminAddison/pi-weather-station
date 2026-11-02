"""The elevated helper and its localhost channel, end to end, with an in-process "launcher"
(no UAC, no real disk: the card is a temp file and the disk list is fake)."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from weatherpi_companion.host import flash_helper as fh
from weatherpi_companion.host.flash import FileDevice
from weatherpi_companion.host.flash_errors import FlashError
from weatherpi_companion.host.flash_helper import FlashJob, HelperDeps, run_elevated

from .test_flash_fakes import card_disk, make_device_file, make_image, raw_image_bytes

CHUNK = 64 * 1024


@pytest.fixture
def raw() -> bytes:
    return raw_image_bytes(6 * CHUNK + 512)


@pytest.fixture
def image(tmp_path, raw):
    return make_image(tmp_path / "img", raw)


@pytest.fixture
def card(tmp_path):
    return card_disk(make_device_file(tmp_path / "card.bin", 10 * CHUNK))


def deps_for(card, device_cls=FileDevice, **kw) -> HelperDeps:
    return HelperDeps(list_fn=lambda: [card], open_device=lambda d: device_cls(d.device),
                      prepare=lambda d: contextlib.nullcontext(), chunk=CHUNK, **kw)


def job_arg(args: list[str]) -> str:
    return args[args.index("--job") + 1]


def in_process_launcher(deps: HelperDeps, seen: list | None = None):
    """Runs the helper's main() in this process instead of elevating."""

    def launch(exe: str, args: list[str], cwd: str) -> int:
        if seen is not None:
            seen.append((exe, args, cwd))
        return fh.main(["--job", job_arg(args)], deps=deps)

    return launch


# --------------------------------------------------------------------------- job + command


def test_job_round_trip(card):
    job = FlashJob("C:\\Users\\Jo O'Neil\\weatherpi-1.img.xz", "AB" * 32, card.identity,
                   51234, "tok", "C:\\logs\\flash.log")
    back = FlashJob.decode(job.encode())
    assert back.target == card.identity and back.sha256 == "ab" * 32
    assert back.image == job.image and back.port == 51234 and back.log_path == job.log_path
    assert "=" not in job.encode() and " " not in job.encode()


def test_helper_command_frozen_and_source():
    exe, args, cwd = fh.helper_command("JOB", frozen=True,
                                       executable="C:\\Program Files\\WPC\\WeatherPi.exe")
    assert exe.endswith("WeatherPi.exe") and args == [fh.HELPER_FLAG, "--job", "JOB"]
    assert cwd == "C:\\Program Files\\WPC"
    exe, args, cwd = fh.helper_command("JOB", frozen=False, executable="py.exe",
                                       app_dir="C:\\a b\\app")
    assert args[0] == "-c" and args[-2:] == ["--job", "JOB"] and cwd == "C:\\a b\\app"
    assert "'C:\\\\a b\\\\app'" in args[1] and fh.HELPER_MODULE in args[1]


def test_source_helper_command_really_starts_the_helper():
    """Runs the real command line (not elevated) with a bad job: it must start, import the
    package from the app folder (no PYTHONPATH) and exit 4 without touching anything."""
    exe, args, _cwd = fh.helper_command("bm90LWpzb24", frozen=False)
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    proc = subprocess.run([exe, *args], cwd=str(Path.home()), env=env, check=False,
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 4, proc.stderr
    assert "bad --job" in proc.stderr


def test_main_accepts_flash_helper_flag_and_rejects_bad_job():
    assert fh.main([fh.HELPER_FLAG, "--job", "!!!"]) == 4


# --------------------------------------------------------------------------- end to end


def test_flash_through_the_helper(image, raw, card):
    events, seen = [], []
    res = run_elevated(image, card.identity, progress=lambda *a: events.append(a),
                       launcher=in_process_launcher(deps_for(card), seen))
    assert res.bytes_written == len(raw) and len(res.sha256) == 64
    assert Path(card.device).read_bytes()[:len(raw)] == raw
    phases = []
    for p, *_ in events:
        if not phases or phases[-1] != p:
            phases.append(p)
    assert phases == ["verify-image", "write", "verify-card"]
    assert events[-1] == ("verify-card", len(raw), len(raw))
    exe, args, _cwd = seen[0]
    assert exe == sys.executable
    job = FlashJob.decode(job_arg(args))
    assert job.target == card.identity and job.sha256 == image.sha256
    assert res.ejected is False  # no eject given: a faked device never gets the real one


def test_card_is_ejected_after_a_good_flash(image, raw, card):
    ejected = []
    deps = deps_for(card, eject=lambda dev: ejected.append(dev) or True)
    res = run_elevated(image, card.identity, progress=lambda *a: None,
                       launcher=in_process_launcher(deps))
    assert res.ejected is True and ejected == [card.device]


def test_failed_eject_still_counts_as_a_good_flash(image, raw, card):
    def boom(_dev):
        raise OSError(5, "eject refused")

    res = run_elevated(image, card.identity, progress=lambda *a: None,
                       launcher=in_process_launcher(deps_for(card, eject=boom)))
    assert res.ejected is False and res.bytes_written == len(raw)


def test_no_eject_when_the_flash_fails(image, card):
    ejected = []

    class Corrupting(FileDevice):
        def read_at(self, offset, size):
            data = bytearray(super().read_at(offset, size))
            if data:
                data[0] ^= 0xFF
            return bytes(data)

    deps = deps_for(card, device_cls=Corrupting, eject=lambda dev: ejected.append(dev) or True)
    with pytest.raises(FlashError):
        run_elevated(image, card.identity, progress=lambda *a: None,
                     launcher=in_process_launcher(deps))
    assert ejected == []


def test_helper_refuses_target_that_fails_the_filter(image, card):
    # the app asked for this disk, but the helper's own listing says it's the system disk
    system_view = card_disk(card.device, is_system=True)
    deps = HelperDeps(list_fn=lambda: [system_view],
                      open_device=lambda d: pytest.fail("must not open"),
                      prepare=lambda d: contextlib.nullcontext(), chunk=CHUNK)
    with pytest.raises(FlashError) as e:
        run_elevated(image, card.identity, launcher=in_process_launcher(deps))
    assert e.value.code == "not_allowed"
    assert Path(card.device).read_bytes()[:4] == b"\xaa" * 4


def test_helper_refuses_unknown_disk(image, card):
    other = card_disk(card.device + ".other")
    deps = HelperDeps(list_fn=lambda: [other], open_device=lambda d: pytest.fail("no"),
                      prepare=lambda d: contextlib.nullcontext())
    with pytest.raises(FlashError) as e:
        run_elevated(image, card.identity, launcher=in_process_launcher(deps))
    assert e.value.code == "card_removed"


def test_helper_rereads_the_sha_file(image, card):
    image.path.with_name(image.path.name + ".sha256").write_text("ef" * 32)
    with pytest.raises(FlashError) as e:
        run_elevated(image, card.identity, launcher=in_process_launcher(deps_for(card)))
    assert e.value.code == "bad_sha_file"


class CorruptReadDevice(FileDevice):
    def read_at(self, offset, size):
        data = bytearray(super().read_at(offset, size))
        if data:
            data[-1] ^= 1
        return bytes(data)


def test_verify_failure_comes_back_as_flash_error(image, card):
    with pytest.raises(FlashError) as e:
        run_elevated(image, card.identity,
                     launcher=in_process_launcher(deps_for(card, CorruptReadDevice)))
    assert e.value.code == "verify_failed"
    assert "read back" in e.value.detail


class SlowDevice(FileDevice):
    def write_at(self, offset, data):
        time.sleep(0.15)
        super().write_at(offset, data)


def test_cancel_reaches_the_helper(image, card):
    cancel = threading.Event()

    def progress(phase, done, total):
        if phase == "write" and done > 0:
            cancel.set()

    with pytest.raises(FlashError) as e:
        run_elevated(image, card.identity, progress=progress, cancel=cancel, poll=0.05,
                     launcher=in_process_launcher(deps_for(card, SlowDevice)))
    assert e.value.code == "cancelled"
    assert Path(card.device).read_bytes()[:CHUNK] == bytes(CHUNK)  # never left half-bootable


def test_uac_refused(image, card):
    def refuse(exe, args, cwd):
        raise FlashError("uac_cancelled", "said no")

    with pytest.raises(FlashError) as e:
        run_elevated(image, card.identity, launcher=refuse)
    assert e.value.code == "uac_cancelled"


def test_helper_exits_without_connecting(image, card):
    with pytest.raises(FlashError) as e:
        run_elevated(image, card.identity, launcher=lambda exe, args, cwd: 1)
    assert e.value.code == "helper_failed" and "exit code 1" in e.value.detail


def test_cancel_while_waiting_for_uac(image, card):
    cancel = threading.Event()
    release = threading.Event()

    def waiting(exe, args, cwd):  # like a UAC prompt nobody answers
        release.wait(5)

    threading.Timer(0.2, cancel.set).start()
    try:
        with pytest.raises(FlashError) as e:
            run_elevated(image, card.identity, cancel=cancel, launcher=waiting, poll=0.05)
    finally:
        release.set()
    assert e.value.code == "cancelled"


def test_impostor_without_token_is_ignored(image, raw, card):
    deps = deps_for(card)

    def launch(exe, args, cwd):
        job = FlashJob.decode(job_arg(args))
        with socket.create_connection(("127.0.0.1", job.port)) as s:
            s.sendall(b'{"type":"hello","token":"guess"}\n'
                      b'{"type":"result","ok":true,"bytes":1,"sha256":"x"}\n')
            time.sleep(0.2)
        return fh.main(["--job", job_arg(args)], deps=deps)

    res = run_elevated(image, card.identity, launcher=launch)
    assert res.bytes_written == len(raw)  # the fake "ok" was never believed


def test_helper_crash_is_reported(image, card):
    def boom():
        raise RuntimeError("kaboom")

    deps = HelperDeps(list_fn=boom, open_device=lambda d: None,
                      prepare=lambda d: contextlib.nullcontext())
    with pytest.raises(FlashError) as e:
        run_elevated(image, card.identity, launcher=in_process_launcher(deps))
    assert e.value.code == "helper_failed" and "kaboom" in e.value.detail


def test_helper_stops_when_the_app_goes_away(image, card):
    """If the app is closed mid-flash the helper cancels instead of carrying on."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    job = FlashJob(str(image.path), image.sha256, card.identity, srv.getsockname()[1], "t")
    result: dict = {}
    t = threading.Thread(target=lambda: result.setdefault(
        "code", fh.main(["--job", job.encode()], deps=deps_for(card, SlowDevice))))
    t.start()
    conn, _ = srv.accept()
    hello = json.loads(conn.makefile().readline())
    assert hello["type"] == "hello" and hello["token"] == "t"
    conn.close()
    srv.close()
    t.join(15)
    assert result["code"] == 2


def test_log_file_written(tmp_path, image, card):
    log = tmp_path / "logs" / "flash-helper.log"
    run_elevated(image, card.identity, launcher=in_process_launcher(deps_for(card)),
                 log_path=log)
    for h in list(logging.getLogger().handlers):
        if isinstance(h, logging.FileHandler) and h.baseFilename == str(log):
            h.close()
            logging.getLogger().removeHandler(h)
    assert "flash ok" in log.read_text(encoding="utf-8")
