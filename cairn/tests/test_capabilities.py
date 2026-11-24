from __future__ import annotations

from cairn import __version__
from cairn.server.db import SCHEMA_VERSION


def test_capabilities_shape(client):
    response = client.get("/capabilities")
    assert response.status_code == 200
    body = response.json()
    assert body["version"] == __version__
    assert body["schema_version"] == SCHEMA_VERSION
    assert body["features"] == [
        "task_runs",
        "evidence",
        "verify",
        "require_evidence",
    ]


def test_capabilities_stays_up_when_other_routes_error(client):
    # The dispatcher uses this endpoint to decide the compat path, so it
    # must exist even on a fresh database with no projects.
    assert client.get("/capabilities").status_code == 200
