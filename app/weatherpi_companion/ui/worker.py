"""Run core (``flows``/``host``) calls off the UI thread.

Usage::

    task = run_task(lambda t: session.wait_for_plug_in(cancel=t.cancel_event,
                                                       on_progress=t.report),
                    on_done=self._plugged_in, on_progress=self._still_looking, owner=self)
    ...
    task.cancel()          # sets the threading.Event the wait_* call is watching

``fn`` runs on a :class:`QThreadPool` thread and gets the :class:`Task`, so it can read
``task.cancel_event`` and call ``task.report(value, elapsed)``. ``on_done`` / ``on_error``
/ ``on_progress`` always run on the UI thread, and only while ``owner`` (a QWidget) is
still alive. Exceptions in ``fn`` go to ``on_error`` (or are logged).
"""

from __future__ import annotations

import logging
import threading
import traceback
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot
from shiboken6 import isValid

log = logging.getLogger(__name__)

_POOL: QThreadPool | None = None
_LIVE: set[Task] = set()


def pool() -> QThreadPool:
    global _POOL
    if _POOL is None:
        _POOL = QThreadPool()
        _POOL.setMaxThreadCount(8)
    return _POOL


class Task(QObject):
    """Handle for one background call. Lives on the UI thread; its signals are emitted
    from the worker thread and delivered (queued) to the UI thread."""

    _done = Signal(object)
    _failed = Signal(str, object)
    _progress = Signal(object, float)

    def __init__(self, fn: Callable[[Task], Any], owner: QObject | None = None) -> None:
        super().__init__()
        self.fn = fn
        self.owner = owner
        self.cancel_event = threading.Event()
        self.running = True
        self.result: Any = None
        self.error: BaseException | None = None
        self._on_done: list[Callable[[Any], None]] = []
        self._on_error: list[Callable[[BaseException], None]] = []
        self._on_progress: list[Callable[[Any, float], None]] = []
        self._finished_cbs: list[Callable[[], None]] = []
        self._done.connect(self._deliver_done)
        self._failed.connect(self._deliver_failed)
        self._progress.connect(self._deliver_progress)

    # ---- called from the worker thread

    def report(self, value: Any = None, elapsed: float = 0.0) -> None:
        """Progress callback for the core's ``on_progress(value, elapsed_s)``."""
        if not self.cancel_event.is_set():
            self._safe_emit(self._progress, value, float(elapsed or 0.0))

    def _safe_emit(self, sig: Any, *args: Any) -> None:
        try:
            sig.emit(*args)
        except RuntimeError:  # the Task was deleted under us (app closing)
            pass

    # ---- UI thread

    @property
    def cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def cancel(self) -> None:
        self.cancel_event.set()

    def _owner_alive(self) -> bool:
        return self.owner is None or isValid(self.owner)

    @Slot(object)
    def _deliver_done(self, value: Any) -> None:
        self.running = False
        self.result = value
        _LIVE.discard(self)
        if self._owner_alive():
            for cb in self._on_done:
                cb(value)
            for cb in self._finished_cbs:
                cb()

    @Slot(str, object)
    def _deliver_failed(self, text: str, exc: Any) -> None:
        self.running = False
        self.error = exc
        _LIVE.discard(self)
        if self._owner_alive():
            if self._on_error:
                for cb in self._on_error:
                    cb(exc)
            else:
                log.error("background task failed: %s", text)
            for cb in self._finished_cbs:
                cb()

    @Slot(object, float)
    def _deliver_progress(self, value: Any, elapsed: float) -> None:
        if self.running and not self.cancelled and self._owner_alive():
            for cb in self._on_progress:
                cb(value, elapsed)

    def on_finished(self, cb: Callable[[], None]) -> Task:
        """Runs after done or error (not after an unhandled cancel)."""
        self._finished_cbs.append(cb)
        return self


class _Runnable(QRunnable):
    def __init__(self, task: Task) -> None:
        super().__init__()
        self.task = task
        self.setAutoDelete(True)

    def run(self) -> None:
        t = self.task
        try:
            value = t.fn(t)
        except BaseException as exc:  # noqa: BLE001 - report everything to the UI
            t._safe_emit(t._failed, "".join(traceback.format_exception(exc)), exc)
        else:
            t._safe_emit(t._done, value)


def run_task(
    fn: Callable[[Task], Any],
    on_done: Callable[[Any], None] | None = None,
    *,
    on_error: Callable[[BaseException], None] | None = None,
    on_progress: Callable[[Any, float], None] | None = None,
    owner: QObject | None = None,
) -> Task:
    task = Task(fn, owner)
    if on_done:
        task._on_done.append(on_done)
    if on_error:
        task._on_error.append(on_error)
    if on_progress:
        task._on_progress.append(on_progress)
    _LIVE.add(task)
    pool().start(_Runnable(task))
    return task


def cancel_all() -> None:
    for t in list(_LIVE):
        t.cancel()


def shutdown(timeout_ms: int = 3000) -> bool:
    """Cancel every task and wait for the pool (call when the app quits)."""
    cancel_all()
    return pool().waitForDone(timeout_ms)


def live_tasks() -> list[Task]:
    return list(_LIVE)
