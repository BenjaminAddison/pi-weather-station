"""API errors -> `{"error": code, "message": text, ...extra}` (pi-agent-api.md)."""

import threading
from collections.abc import Callable
from datetime import UTC, datetime


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str = "", **extra):
        super().__init__(message or code)
        self.status = status
        self.code = code
        self.message = message or code.replace("_", " ")
        self.extra = extra

    def body(self) -> dict:
        return {"error": self.code, "message": self.message, **self.extra}


def busy(what: str) -> ApiError:
    return ApiError(409, "busy", f"{what} is already in progress")


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class Job:
    """A long operation running in a background thread (wifi-connect, tailscale-up)."""

    def __init__(self, name: str):
        self.name = name
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.state = "idle"  # idle | running | done | failed
        self.error: str | None = None
        self.started_at: datetime | None = None
        self.finished_at: datetime | None = None
        self.detail: dict = {}

    @property
    def running(self) -> bool:
        return self.state == "running"

    def start(self, fn: Callable[["Job"], None], **detail) -> None:
        """Run fn(job) in a thread. fn returns normally (done) or raises JobFailed (failed)."""
        with self._lock:
            if self.running:
                raise busy(self.name)
            self.state = "running"
            self.error = None
            self.detail = dict(detail)
            self.started_at = utc_now()
            self.finished_at = None
            self._thread = threading.Thread(
                target=self._run, args=(fn,), name=f"job-{self.name}", daemon=True
            )
            self._thread.start()

    def _run(self, fn: Callable[["Job"], None]) -> None:
        try:
            fn(self)
            state, error = "done", None
        except JobFailed as exc:
            state, error = "failed", exc.code
        except Exception:  # a job must never kill the agent
            import logging

            logging.getLogger("weatherpi.agent").exception("job %s crashed", self.name)
            state, error = "failed", "internal"
        with self._lock:
            self.state, self.error = state, error
            self.finished_at = utc_now()

    def wait(self, timeout: float | None = None) -> None:
        """For tests: block until the job thread finishes."""
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def as_dict(self) -> dict:
        return {
            "job": self.name,
            "state": self.state,
            "error": self.error,
            "started_at": iso(self.started_at),
            "finished_at": iso(self.finished_at),
        }


class JobFailed(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class OpLock:
    """Non-blocking per-operation lock: a second caller gets 409 busy."""

    def __init__(self, name: str):
        self.name = name
        self._lock = threading.Lock()

    def __enter__(self):
        if not self._lock.acquire(blocking=False):
            raise busy(self.name)
        return self

    def __exit__(self, *exc) -> None:
        self._lock.release()
