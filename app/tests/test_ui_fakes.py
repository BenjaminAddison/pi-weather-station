"""Shared helpers for the UI tests (``test_ui_*.py``) and a few tests of the helpers.

* :func:`make_ctx` builds an :class:`AppContext` around W5's fake world
  (``test_flows.make_session``): fake Pi (``FakeAgent``), fake host Tailscale, fake SSH.
* :class:`SavedNetworksSession` is a ``SetupSession`` that also has the saved-networks
  methods W2b is adding (``saved_networks`` etc.), so the panel can be tested before merge.
* :func:`wait_idle` pumps the event loop until no background task is running.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from weatherpi_companion.checks import CheckResult
from weatherpi_companion.flows import SetupSession
from weatherpi_companion.host.driver import InstallResult
from weatherpi_companion.host.tailscale import LoginStart
from weatherpi_companion.ui import desktop, worker
from weatherpi_companion.ui.context import AppContext

from .test_flows import World, make_session


@dataclass
class SavedNetwork:
    """Same shape as ``agent_client.SavedNetwork`` (W2b)."""

    ssid: str
    priority: int = 0
    hidden: bool = False
    in_use: bool = False


class SavedNetworksSession(SetupSession):
    """A SetupSession with the W2b saved-network methods, backed by a list."""

    def __init__(self, base: SetupSession) -> None:  # adopt a configured session
        self.__dict__.update(base.__dict__)
        self.saved: list[SavedNetwork] = [SavedNetwork("ExampleWiFi", 0, False, True)]
        self.calls: list[tuple] = []

    def saved_networks(self) -> list[SavedNetwork]:
        self.calls.append(("saved_networks",))
        return sorted(self.saved, key=lambda n: (-n.priority, n.ssid))

    def save_network(self, ssid: str, password: str | None, hidden: bool = False,
                     priority: int = 0) -> list[CheckResult]:
        self.calls.append(("save_network", ssid, password, hidden, priority))
        self.saved = [n for n in self.saved if n.ssid != ssid]
        self.saved.append(SavedNetwork(ssid, priority, hidden, False))
        return [CheckResult(True, "wifi_saved", f"Saved {ssid}.")]

    def set_network_priority(self, ssid: str, priority: int) -> list[CheckResult]:
        self.calls.append(("set_network_priority", ssid, priority))
        for n in self.saved:
            if n.ssid == ssid:
                n.priority = priority
                return [CheckResult(True, "wifi_priority", "ok")]
        return [CheckResult(False, "wifi_not_saved", f"{ssid} isn't saved.")]

    def forget_network(self, ssid: str) -> list[CheckResult]:
        self.calls.append(("forget_network", ssid))
        self.saved = [n for n in self.saved if n.ssid != ssid]
        return [CheckResult(True, "wifi_forgotten", f"Removed {ssid}.")]


def make_ctx(app_data, *, saved_networks: bool = False, **world_kw
             ) -> tuple[AppContext, SetupSession, World]:
    session, world = make_session(app_data, **world_kw)
    # safety: never reach the real `tailscale up` or the elevated driver installer
    session.env.host_login = lambda _ts: LoginStart(False, "https://login.tailscale.com/a/host")
    session.env.driver_installer = lambda _exe: InstallResult(0)
    if saved_networks:
        session = SavedNetworksSession(session)
    ctx = AppContext(home=app_data.home, env=session.env, session=session)
    ctx.session_factory = lambda _env: session
    return ctx, session, world


def wait_idle(timeout: float = 10.0, qapp=None) -> None:
    """Process events until no background task is running (and their results arrived)."""
    from PySide6.QtWidgets import QApplication

    app = qapp or QApplication.instance()
    end = time.monotonic() + timeout
    idle = 0
    while time.monotonic() < end:
        app.processEvents()
        idle = idle + 1 if not worker.live_tasks() else 0
        if idle >= 3:  # stable: no task started by a just-delivered result
            return
        time.sleep(0.005)
    raise TimeoutError(f"still running: {worker.live_tasks()}")


def record_opened(monkeypatch) -> list[str]:
    """Record URLs/paths the UI tries to open instead of opening them."""
    seen: list[str] = []
    monkeypatch.setattr(desktop, "opener", lambda target: seen.append(target) or True)
    return seen


@pytest.fixture(autouse=True)
def _no_real_opens(monkeypatch):
    record_opened(monkeypatch)


# --------------------------------------------------------------------------- tests


def test_saved_networks_session_has_the_w2b_methods(app_data):
    _ctx, s, _w = make_ctx(app_data, saved_networks=True)
    assert [n.ssid for n in s.saved_networks()] == ["ExampleWiFi"]
    s.save_network("Phone hotspot", "secret123", priority=-1)
    s.set_network_priority("Phone hotspot", 5)
    assert [n.ssid for n in s.saved_networks()] == ["Phone hotspot", "ExampleWiFi"]
    s.forget_network("Phone hotspot")
    assert [n.ssid for n in s.saved_networks()] == ["ExampleWiFi"]
    assert s.info is None and s.env is not None  # still a working SetupSession


def test_plain_session_has_them_since_w2b(app_data):
    _ctx, s, _w = make_ctx(app_data)
    assert all(callable(getattr(s, m)) for m in
               ("saved_networks", "save_network", "set_network_priority", "forget_network"))
