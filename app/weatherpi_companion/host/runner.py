"""Injectable command runner for host checks.

Every host module takes a ``runner`` argument so tests never execute PowerShell or
``tailscale`` for real. A runner is any callable ``(args, timeout) -> CommandResult``.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0


Runner = Callable[[Sequence[str], float], CommandResult]

#: returncode used when the program could not be started at all
NOT_FOUND = 127
#: returncode used when the program did not finish in time
TIMED_OUT = 124


def _no_window_flags() -> int:
    if sys.platform == "win32":
        return getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return 0


def run_command(args: Sequence[str], timeout: float = 20.0) -> CommandResult:
    """Run a program and capture its output as text. Never raises for normal failures."""
    try:
        proc = subprocess.run(
            list(args),
            capture_output=True,
            check=False,
            timeout=timeout,
            creationflags=_no_window_flags(),
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError as exc:
        return CommandResult(NOT_FOUND, "", str(exc))
    except subprocess.TimeoutExpired:
        return CommandResult(TIMED_OUT, "", f"timed out after {timeout:.0f}s")
    except OSError as exc:
        return CommandResult(NOT_FOUND, "", str(exc))
    return CommandResult(proc.returncode, _decode(proc.stdout), _decode(proc.stderr))


def _decode(data: bytes | None) -> str:
    if not data:
        return ""
    for enc in ("utf-8", "utf-16"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


POWERSHELL_PREFIX = (
    "$ErrorActionPreference='SilentlyContinue';"
    "$ProgressPreference='SilentlyContinue';"
    "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
)


def powershell_args(script: str) -> list[str]:
    """Argument list for running a PowerShell 5.1 script that prints UTF-8."""
    return [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        POWERSHELL_PREFIX + script,
    ]


def run_powershell(runner: Runner, script: str, timeout: float = 30.0) -> CommandResult:
    return runner(powershell_args(script), timeout)


def as_list(value: object) -> list:
    """ConvertTo-Json gives an object for one item, a list for many, nothing for none."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]
