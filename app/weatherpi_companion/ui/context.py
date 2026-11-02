"""``AppContext``: what every section shares (docs/companion-app.md, "Section contract")."""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..flows import HostEnv, SetupSession


@dataclass
class AppContext:
    """Shared by all sections.

    ``home`` is the app data folder, ``env`` the host environment (fakes in tests),
    ``session`` the :class:`SetupSession` shared by ``tester`` and ``setup`` (created on
    first use with :meth:`ensure_session`), and :meth:`go` switches the shell to another
    section.
    """

    home: Path
    env: HostEnv
    session: SetupSession | None = None
    #: set by the shell; ``go(section_id)`` calls it
    navigator: Callable[[str], None] | None = None
    #: hints passed between sections, e.g. ``{"setup_start": 5}`` from tester to setup
    hints: dict[str, Any] = field(default_factory=dict)
    #: tests can replace how a session is made
    session_factory: Callable[[HostEnv], SetupSession] = SetupSession

    def go(self, section_id: str) -> None:
        if self.navigator is not None:
            self.navigator(section_id)

    def ensure_session(self) -> SetupSession:
        if self.session is None:
            self.session = self.session_factory(self.env)
        return self.session

    def reset_session(self) -> SetupSession:
        """A fresh session (e.g. when re-running the check for another Pi)."""
        if self.session is not None:
            with contextlib.suppress(Exception):
                self.session.close()
        self.session = None
        self.hints.pop("setup_start", None)
        return self.ensure_session()
