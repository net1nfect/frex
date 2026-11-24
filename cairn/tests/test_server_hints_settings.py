from __future__ import annotations

import pytest


@pytest.fixture
def project_id(client):
    response = client.post(
        "/projects",
        json={"title": "lab", "origin": "start", "goal": "finish"},
    )
    assert response.status_code == 201
    return response.json()["project"]["id"]


def test_add_hint(client, project_id):
    response = client.post(
        f"/projects/{project_id}/hints",
        json={"content": "check /admin", "creator": "alice"},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["id"] == "h001"
    assert body["content"] == "check /admin"
    assert body["creator"] == "alice"


def test_add_hint_rejects_blank(client, project_id):
    response = client.post(
        f"/projects/{project_id}/hints",
        json={"content": "   ", "creator": "alice"},
    )
    assert response.status_code == 422


def test_add_hint_to_completed_project_is_allowed(client, project_id):
    client.post(
        f"/projects/{project_id}/complete",
        json={"from": ["origin"], "description": "done", "worker": "alice"},
    )
    response = client.post(
        f"/projects/{project_id}/hints",
        json={"content": "post-mortem note", "creator": "alice"},
    )
    assert response.status_code == 201


def test_add_hint_missing_project(client):
    response = client.post(
        "/projects/proj_999/hints",
        json={"content": "x", "creator": "alice"},
    )
    assert response.status_code == 404


def test_settings_defaults(client):
    response = client.get("/settings")
    assert response.status_code == 200
    assert response.json() == {
        "intent_timeout": 15,
        "reason_timeout": 15,
        "require_evidence": False,
        "verify_max_attempts": 3,
    }


def test_settings_update(client):
    response = client.put(
        "/settings",
        json={
        "intent_timeout": 30,
        "reason_timeout": 45,
        "require_evidence": True,
        "verify_max_attempts": 5,
    },
    )
    assert response.status_code == 200
    assert response.json() == {
        "intent_timeout": 30,
        "reason_timeout": 45,
        "require_evidence": True,
        "verify_max_attempts": 5,
    }
    assert client.get("/settings").json() == {
        "intent_timeout": 30,
        "reason_timeout": 45,
        "require_evidence": True,
        "verify_max_attempts": 5,
    }


@pytest.mark.parametrize(
    "payload",
    [
        {"intent_timeout": 4, "reason_timeout": 15},
        {"intent_timeout": 15, "reason_timeout": 4},
        {"intent_timeout": 0, "reason_timeout": 15},
    ],
)
def test_settings_reject_timeouts_below_five(client, payload):
    response = client.put("/settings", json=payload)
    assert response.status_code == 422
