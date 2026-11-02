"""Fixtures for agent tests: a fresh fake Pi (paths relocated to tmp_path) per test."""

from pathlib import Path

import pytest
from agent_helpers import claim_body
from fastapi.testclient import TestClient

from weatherpi import config, hw, paths
from weatherpi.agent.app import create_app
from weatherpi.agent.fakes import FakeOS


@pytest.fixture
def root(tmp_path, monkeypatch) -> Path:
    """Relocate every weatherpi.paths location under tmp_path for this test."""
    old_root = paths.ROOT
    for name in dir(paths):
        value = getattr(paths, name)
        if isinstance(value, Path) and name != "ROOT":
            try:
                monkeypatch.setattr(paths, name, tmp_path / value.relative_to(old_root))
            except ValueError:
                pass
    monkeypatch.setattr(paths, "ROOT", tmp_path)
    return tmp_path


@pytest.fixture
def cfg(root) -> dict:
    c = config.load_config(root / "no-config.yaml")
    c["database"]["path"] = str(paths.DB_FILE)
    return c


@pytest.fixture(autouse=True)
def _reset_fake_hw():
    yield
    hw.FAKE_TEMP.present = True
    hw.FAKE_TEMP.celsius = 21.5
    hw.FAKE_LED_EVENTS.clear()


@pytest.fixture
def fake() -> FakeOS:
    return FakeOS()


@pytest.fixture
def make_client(fake, cfg):
    def _make(via="usb", base_url="http://testserver") -> TestClient:
        app = create_app(fake, via=via, cfg=cfg, power_delay=0.0)
        return TestClient(app, base_url=base_url)

    return _make


@pytest.fixture
def client(make_client) -> TestClient:
    return make_client()


@pytest.fixture
def claimed(client):
    """(client, auth headers, claim body) for a claimed Pi."""
    body = claim_body()
    r = client.post("/v1/claim", json=body)
    assert r.status_code == 200, r.text
    return client, {"Authorization": f"Bearer {body['api_token']}"}, body

