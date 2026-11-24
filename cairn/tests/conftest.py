from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from cairn.server import db


@pytest.fixture
def db_path(tmp_path):
    db.reset_for_tests()
    path = tmp_path / "cairn.db"
    db.configure(path)
    yield path
    db.reset_for_tests()


@pytest.fixture
def client(db_path):
    # Imported lazily so the module-level app is bound to the configured db.
    from cairn.server.app import app

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def project(client):
    response = client.post(
        "/projects",
        json={"title": "lab", "origin": "start here", "goal": "own the box"},
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture
def auth_headers():
    # Authentication is optional in phase 0; the fixture exists so tests can
    # be switched on when the server token is enabled.
    return {}
