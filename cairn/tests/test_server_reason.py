from __future__ import annotations

import pytest

from cairn.server import db


@pytest.fixture
def project_id(client):
    response = client.post(
        "/projects",
        json={"title": "lab", "origin": "start", "goal": "finish"},
    )
    assert response.status_code == 201
    return response.json()["project"]["id"]


def _complete(client, project_id):
    response = client.post(
        f"/projects/{project_id}/complete",
        json={"from": ["origin"], "description": "done", "worker": "alice"},
    )
    assert response.status_code == 200


def _age_reason_lease(project_id: str, seconds: int = 3600) -> None:
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE projects SET reason_last_heartbeat_at = datetime('now', ?) WHERE id = ?",
            (f"-{seconds} seconds", project_id),
        )


def test_claim_reason(client, project_id):
    response = client.post(
        f"/projects/{project_id}/reason/claim",
        json={"worker": "alice", "trigger": "initial"},
    )
    assert response.status_code == 200
    reason = response.json()["reason"]
    assert reason["worker"] == "alice"
    assert reason["trigger"] == "initial"
    assert reason["last_heartbeat_at"] is not None


def test_claim_reason_twice_same_worker_is_idempotent(client, project_id):
    first = client.post(
        f"/projects/{project_id}/reason/claim",
        json={"worker": "alice", "trigger": "initial"},
    )
    second = client.post(
        f"/projects/{project_id}/reason/claim",
        json={"worker": "alice", "trigger": "changed"},
    )
    assert second.status_code == 200
    assert second.json()["reason"]["trigger"] == "initial"
    assert first.json()["reason"]["started_at"] == second.json()["reason"]["started_at"]


def test_claim_reason_conflict(client, project_id):
    client.post(
        f"/projects/{project_id}/reason/claim",
        json={"worker": "alice", "trigger": "initial"},
    )
    response = client.post(
        f"/projects/{project_id}/reason/claim",
        json={"worker": "bob", "trigger": "initial"},
    )
    assert response.status_code == 409
    assert "alice" in response.json()["detail"]


def test_reason_heartbeat(client, project_id):
    client.post(
        f"/projects/{project_id}/reason/claim",
        json={"worker": "alice", "trigger": "initial"},
    )
    response = client.post(
        f"/projects/{project_id}/reason/heartbeat",
        json={"worker": "alice"},
    )
    assert response.status_code == 200
    assert response.json()["reason"]["worker"] == "alice"


def test_reason_heartbeat_without_claim(client, project_id):
    response = client.post(
        f"/projects/{project_id}/reason/heartbeat",
        json={"worker": "alice"},
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "Project reason is not currently claimed"


def test_reason_release(client, project_id):
    client.post(
        f"/projects/{project_id}/reason/claim",
        json={"worker": "alice", "trigger": "initial"},
    )
    response = client.post(
        f"/projects/{project_id}/reason/release",
        json={"worker": "alice"},
    )
    assert response.status_code == 200
    assert response.json()["reason"] is None


def test_reason_lease_expires(client, project_id):
    client.post(
        f"/projects/{project_id}/reason/claim",
        json={"worker": "alice", "trigger": "initial"},
    )
    _age_reason_lease(project_id)
    response = client.post(
        f"/projects/{project_id}/reason/claim",
        json={"worker": "bob", "trigger": "initial"},
    )
    assert response.status_code == 200
    assert response.json()["reason"]["worker"] == "bob"


def test_reopen_requires_completed_project(client, project_id):
    response = client.post(
        f"/projects/{project_id}/reopen",
        json={"description": "new lead", "creator": "alice"},
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "Project is active"


def test_reopen_creates_feedback_fact_and_intent(client, project_id):
    _complete(client, project_id)
    response = client.post(
        f"/projects/{project_id}/reopen",
        json={"description": "new lead", "creator": "alice"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["project"]["status"] == "active"
    assert body["fact"]["id"] == "f001"
    assert body["fact"]["description"] == "new lead"
    assert body["intent"]["description"] == "external_feedback"
    assert body["intent"]["from"] == ["origin"]
    assert body["intent"]["to"] == "f001"

    detail = client.get(f"/projects/{project_id}").json()
    assert "f001" in {fact["id"] for fact in detail["facts"]}
    assert "f001" in {intent["to"] for intent in detail["intents"]}
    assert all(intent["to"] != "goal" for intent in detail["intents"])


def test_reopen_missing_project(client):
    response = client.post(
        "/projects/proj_999/reopen",
        json={"description": "x", "creator": "alice"},
    )
    assert response.status_code == 404
