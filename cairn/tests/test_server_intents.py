from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

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


def _create_intent(client, project_id, **overrides):
    payload = {
        "from": ["origin"],
        "description": "scan the host",
        "creator": "alice",
        "worker": None,
    }
    payload.update(overrides)
    return client.post(f"/projects/{project_id}/intents", json=payload)


def _age_heartbeat(project_id: str, intent_id: str, seconds: int = 3600) -> None:
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE intents SET last_heartbeat_at = datetime('now', ?) WHERE project_id = ? AND id = ?",
            (f"-{seconds} seconds", project_id, intent_id),
        )


def test_create_unclaimed_intent(client, project_id):
    response = _create_intent(client, project_id)
    assert response.status_code == 201
    body = response.json()
    assert body["id"] == "i001"
    assert body["from"] == ["origin"]
    assert body["to"] is None
    assert body["worker"] is None
    assert body["last_heartbeat_at"] is None
    assert body["concluded_at"] is None


def test_create_intent_can_be_claimed_by_creator(client, project_id):
    response = _create_intent(client, project_id, worker="alice")
    assert response.status_code == 201
    assert response.json()["worker"] == "alice"
    assert response.json()["last_heartbeat_at"] is not None


def test_create_intent_rejects_foreign_worker(client, project_id):
    response = _create_intent(client, project_id, worker="bob")
    assert response.status_code == 400
    assert response.json()["detail"] == "worker must be null or equal to creator"


def test_create_intent_rejects_goal_source(client, project_id):
    response = _create_intent(client, project_id, **{"from": ["goal"]})
    assert response.status_code == 400
    assert response.json()["detail"] == "goal cannot be used in from"


def test_create_intent_rejects_unknown_fact(client, project_id):
    response = _create_intent(client, project_id, **{"from": ["f999"]})
    assert response.status_code == 404


def test_create_intent_requires_source(client, project_id):
    response = _create_intent(client, project_id, **{"from": []})
    assert response.status_code == 422


def test_create_intent_on_stopped_project(client, project_id):
    client.put(f"/projects/{project_id}/status", json={"status": "stopped"})
    response = _create_intent(client, project_id)
    assert response.status_code == 403


def test_heartbeat_claims_unclaimed_intent(client, project_id):
    intent = _create_intent(client, project_id).json()
    response = client.post(
        f"/projects/{project_id}/intents/{intent['id']}/heartbeat",
        json={"worker": "bob"},
    )
    assert response.status_code == 200
    assert response.json()["worker"] == "bob"


def test_heartbeat_conflict_when_claimed_by_other(client, project_id):
    intent = _create_intent(client, project_id, worker="alice").json()
    response = client.post(
        f"/projects/{project_id}/intents/{intent['id']}/heartbeat",
        json={"worker": "bob"},
    )
    assert response.status_code == 409
    assert "alice" in response.json()["detail"]


def test_heartbeat_succeeds_after_lease_expiry(client, project_id):
    intent = _create_intent(client, project_id, worker="alice").json()
    _age_heartbeat(project_id, intent["id"])
    response = client.post(
        f"/projects/{project_id}/intents/{intent['id']}/heartbeat",
        json={"worker": "bob"},
    )
    assert response.status_code == 200
    assert response.json()["worker"] == "bob"


def test_release_clears_worker(client, project_id):
    intent = _create_intent(client, project_id, worker="alice").json()
    response = client.post(
        f"/projects/{project_id}/intents/{intent['id']}/release",
        json={"worker": "alice"},
    )
    assert response.status_code == 200
    assert response.json()["worker"] is None


def test_release_rejects_other_worker(client, project_id):
    intent = _create_intent(client, project_id, worker="alice").json()
    response = client.post(
        f"/projects/{project_id}/intents/{intent['id']}/release",
        json={"worker": "bob"},
    )
    assert response.status_code == 409


def test_conclude_creates_fact_and_closes_intent(client, project_id):
    intent = _create_intent(client, project_id).json()
    response = client.post(
        f"/projects/{project_id}/intents/{intent['id']}/conclude",
        json={"worker": "alice", "description": "port 22 is open"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["fact"]["id"] == "f001"
    assert body["fact"]["description"] == "port 22 is open"
    assert body["fact"]["kind"] == "observation"
    assert body["fact"]["status"] == "hypothesis"
    assert body["fact"]["confidence"] == 0.5
    assert body["fact"]["created_at"] is not None
    assert body["intent"]["to"] == "f001"
    assert body["intent"]["concluded_at"] is not None

    detail = client.get(f"/projects/{project_id}").json()
    assert "f001" in {fact["id"] for fact in detail["facts"]}


def test_conclude_twice_returns_409(client, project_id):
    intent = _create_intent(client, project_id).json()
    first = client.post(
        f"/projects/{project_id}/intents/{intent['id']}/conclude",
        json={"worker": "alice", "description": "first"},
    )
    assert first.status_code == 200
    second = client.post(
        f"/projects/{project_id}/intents/{intent['id']}/conclude",
        json={"worker": "alice", "description": "second"},
    )
    assert second.status_code == 409
    assert second.json()["detail"] == "Intent already concluded"


def test_conclude_rejects_other_worker(client, project_id):
    intent = _create_intent(client, project_id, worker="alice").json()
    response = client.post(
        f"/projects/{project_id}/intents/{intent['id']}/conclude",
        json={"worker": "bob", "description": "stolen"},
    )
    assert response.status_code == 409


def test_conclude_unknown_intent(client, project_id):
    response = client.post(
        f"/projects/{project_id}/intents/i999/conclude",
        json={"worker": "alice", "description": "x"},
    )
    assert response.status_code == 404


def test_list_shows_unclaimed_intent_count(client, project_id):
    _create_intent(client, project_id)
    summary = client.get("/projects").json()[0]
    assert summary["intent_count"] == 1
    assert summary["unclaimed_intent_count"] == 1
    assert summary["working_intent_count"] == 0


def test_parallel_heartbeats_claim_exactly_once(client, project_id):
    intent = _create_intent(client, project_id).json()
    workers = [f"w{index}" for index in range(8)]
    barrier = threading.Barrier(len(workers))

    def claim(worker):
        barrier.wait(timeout=10)
        response = client.post(
            f"/projects/{project_id}/intents/{intent['id']}/heartbeat",
            json={"worker": worker},
        )
        return worker, response.status_code, response.json()

    with ThreadPoolExecutor(max_workers=len(workers)) as pool:
        results = list(pool.map(claim, workers))

    winners = [worker for worker, status, _ in results if status == 200]
    losers = [status for _, status, _ in results if status != 200]
    assert len(winners) == 1
    assert losers == [409] * (len(workers) - 1)

    detail = client.get(f"/projects/{project_id}").json()
    assert detail["intents"][0]["worker"] == winners[0]


def test_parallel_concludes_create_one_fact(client, project_id):
    intent = _create_intent(client, project_id).json()
    workers = [f"w{index}" for index in range(8)]
    barrier = threading.Barrier(len(workers))

    def conclude(worker):
        barrier.wait(timeout=10)
        response = client.post(
            f"/projects/{project_id}/intents/{intent['id']}/conclude",
            json={"worker": worker, "description": f"fact from {worker}"},
        )
        return response.status_code

    with ThreadPoolExecutor(max_workers=len(workers)) as pool:
        statuses = list(pool.map(conclude, workers))

    assert statuses.count(200) == 1
    assert statuses.count(409) == len(workers) - 1

    detail = client.get(f"/projects/{project_id}").json()
    assert len(detail["facts"]) == 3
    assert [intent["to"] for intent in detail["intents"]] == ["f001"]


def test_high_risk_intent_approval_flow(client):
    pid = client.post("/projects", json={"title": "lab", "origin": "s", "goal": "g"}).json()["project"]["id"]
    intent = client.post(
        f"/projects/{pid}/intents",
        json={"from": ["origin"], "description": "attack", "creator": "alice",
              "risk_class": "critical"},
    ).json()
    assert intent["risk_class"] == "critical"
    assert intent["approved_at"] is None

    # Approving a low-risk intent is a conflict.
    low_id = client.post(
        f"/projects/{pid}/intents",
        json={"from": ["origin"], "description": "scan", "creator": "alice"},
    ).json()["id"]
    assert client.post(f"/projects/{pid}/intents/{low_id}/approve", json={"approver": "bob"}).status_code == 409

    approved = client.post(
        f"/projects/{pid}/intents/{intent['id']}/approve", json={"approver": "bob"}
    ).json()
    assert approved["approved_at"] is not None
    assert approved["approved_by"] == "bob"

    # Reject records the rejection with the rejecting operator.
    other = client.post(
        f"/projects/{pid}/intents",
        json={"from": ["origin"], "description": "spray", "creator": "alice", "risk_class": "high"},
    ).json()["id"]
    rejected = client.post(
        f"/projects/{pid}/intents/{other}/reject", json={"approver": "carol"}
    ).json()
    assert rejected["approved_by"] == "REJECTED:carol"
    assert client.post(
        "/projects/proj_999/intents/x/approve", json={"approver": "x"}
    ).status_code == 404
