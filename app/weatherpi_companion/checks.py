"""Check results and polling helpers shared by :mod:`.flows` and :mod:`.station`."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any


@dataclass
class CheckResult:
    """One line on a wizard page.

    ``message_for_owner`` is plain language for the owner; ``detail`` is technical (shown
    under "Details" and included in the AI help pack). ``action`` names a fix the UI can
    offer as a button (see ``Action``); ``data`` carries extra values (URLs, networks ...).
    ``optional`` checks don't block Next even when they fail.
    """

    ok: bool
    code: str
    message_for_owner: str
    detail: str = ""
    action: str | None = None
    data: dict[str, Any] = field(default_factory=dict)
    optional: bool = False

    @property
    def blocking(self) -> bool:
        return not self.ok and not self.optional


class Action:
    """Values for :attr:`CheckResult.action`."""

    INSTALL_DRIVER = "install_driver"
    INSTALL_TAILSCALE = "install_tailscale"
    PLUG_IN = "plug_in"  # show the plug-in picture again
    WAIT = "wait"  # just wait / poll again
    RETRY = "retry"
    RECOVER = "recover"  # offer the forgot-password / recovery flow
    FIX_PASSWORD = "fix_password"
    CHOOSE_WIFI = "choose_wifi"
    MOVE_CLOSER = "move_closer"
    HOST_TAILSCALE_LOGIN = "host_tailscale_login"  # call SetupSession.start_host_login()
    OPEN_TAILSCALE_APP = "open_tailscale_app"  # data["path"]
    OPEN_URL = "open_url"  # data["url"]
    PI_TAILSCALE_RELOGIN = "pi_tailscale_relogin"  # SetupSession.pi_tailscale_logout()
    REBOOT = "reboot"
    UPDATE_APP = "update_app"


def all_ok(checks: Iterable[CheckResult]) -> bool:
    """True when nothing blocks Next."""
    return not any(c.blocking for c in checks)


def first_problem(checks: Iterable[CheckResult]) -> CheckResult | None:
    return next((c for c in checks if c.blocking), None)


class Cancelled(Exception):
    pass


@dataclass
class PollResult[T]:
    value: T | None
    reason: str  # "done" | "timeout" | "cancelled"
    attempts: int
    last_error: Exception | None = None

    @property
    def done(self) -> bool:
        return self.reason == "done"


def poll[T](
    fetch: Callable[[], T],
    is_done: Callable[[T], bool],
    *,
    timeout: float,
    interval: float = 2.0,
    cancel: threading.Event | None = None,
    on_progress: Callable[[T | None, float], None] | None = None,
    sleep: Callable[[float], None] | None = None,
    clock: Callable[[], float] = time.monotonic,
    swallow: tuple[type[BaseException], ...] = (Exception,),
) -> PollResult[T]:
    """Call ``fetch`` every ``interval`` s until ``is_done(value)``, timeout, or cancel.

    Exceptions in ``swallow`` are remembered as ``last_error`` and polling continues (the
    link may blip while WiFi or Tailscale changes). ``on_progress(value, elapsed)`` runs
    after every attempt. When no ``sleep`` is given the wait is ``cancel.wait`` so a cancel
    takes effect immediately.
    """
    start = clock()
    attempts = 0
    value: T | None = None
    last_error: Exception | None = None
    if sleep is None:
        ev = cancel or threading.Event()
        sleep = ev.wait
    while True:
        if cancel is not None and cancel.is_set():
            return PollResult(value, "cancelled", attempts, last_error)
        attempts += 1
        try:
            value = fetch()
            last_error = None
            if is_done(value):
                return PollResult(value, "done", attempts)
        except swallow as exc:  # type: ignore[misc]
            last_error = exc  # type: ignore[assignment]
        elapsed = clock() - start
        if on_progress is not None:
            on_progress(value, elapsed)
        if elapsed + interval > timeout:
            return PollResult(value, "timeout", attempts, last_error)
        sleep(interval)
