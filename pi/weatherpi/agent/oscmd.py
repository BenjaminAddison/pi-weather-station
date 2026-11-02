"""The single choke point for OS commands. Never uses a shell.

Everything in the agent that touches the OS (nmcli, tailscale, chpasswd, systemctl, ...) calls
`runner.run(argv, timeout, input)`. On the Pi that is `RealRunner`; with WEATHERPI_FAKE=1 it is
`weatherpi.agent.fakes.FakeOS`, a stateful in-memory imitation of the same commands.
"""

import logging
import os
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

log = logging.getLogger("weatherpi.agent")


@dataclass
class Result:
    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    @property
    def output(self) -> str:
        return (self.stdout + "\n" + self.stderr).strip()

    @property
    def missing(self) -> bool:
        """The command isn't installed."""
        return self.returncode == 127


class Runner(Protocol):
    def run(
        self, argv: Sequence[str], timeout: float = 30, input: str | None = None
    ) -> Result: ...


def _text(value: bytes | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value


class RealRunner:
    """subprocess.run with a fixed C locale (stable, parseable output)."""

    def __init__(self) -> None:
        self._env = dict(os.environ, LC_ALL="C.UTF-8", LANG="C.UTF-8")

    def run(self, argv: Sequence[str], timeout: float = 30, input: str | None = None) -> Result:
        args = [str(a) for a in argv]
        try:
            proc = subprocess.run(
                args,
                input=input,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=self._env,
                check=False,
            )
        except FileNotFoundError:
            return Result(127, "", f"{args[0]}: command not found")
        except subprocess.TimeoutExpired as exc:
            return Result(-1, _text(exc.stdout), _text(exc.stderr), timed_out=True)
        except OSError as exc:
            return Result(126, "", f"{args[0]}: {exc}")
        if proc.returncode != 0:
            log.debug("%s -> %s: %s", args[0], proc.returncode, proc.stderr.strip()[:200])
        return Result(proc.returncode, proc.stdout, proc.stderr)


def default_runner() -> Runner:
    from weatherpi import paths

    if paths.FAKE:
        from weatherpi.agent.fakes import FakeOS

        return FakeOS()
    return RealRunner()
