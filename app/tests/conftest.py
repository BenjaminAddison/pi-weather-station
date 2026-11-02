from __future__ import annotations

import pytest

from weatherpi_companion.secrets import ENV_HOME, AppData

from .fakes import FakeAgent


@pytest.fixture
def app_data(tmp_path, monkeypatch) -> AppData:
    home = tmp_path / "home"
    monkeypatch.setenv(ENV_HOME, str(home))
    return AppData(home).ensure()


@pytest.fixture
def agent() -> FakeAgent:
    return FakeAgent()
