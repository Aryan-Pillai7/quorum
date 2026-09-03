"""Shared test fixtures.

Unit tests perform no I/O: nothing here connects to Postgres or Redis. Creating the
engine does not open a connection, so importing the app is safe with no services up.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client() -> TestClient:
    return TestClient(app, raise_server_exceptions=False)
