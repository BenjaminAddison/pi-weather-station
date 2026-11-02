"""Fixtures for the W3 system-integration tests."""

import pytest
from systest_helpers import find_bash


@pytest.fixture(scope="session")
def bash() -> str:
    found = find_bash()
    if not found:
        pytest.skip("bash not available")
    return found
