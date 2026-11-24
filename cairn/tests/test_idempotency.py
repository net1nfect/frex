from __future__ import annotations

import pytest

from cairn.server.idempotency import canonical_payload_hash


@pytest.fixture
def project_id(client):
    response = client.post(
        "/projects",
        json={"title": "lab", "origin": "start", "goal": "finish"},
    )
    assert response.status_code == 201
    return response.json()["project"]["id"]


@pytest.fixture
def intent_id(client, project_id):
    response = client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["origin"], "description": "scan", "creator": "alice"},
    )
    assert response.status_code == 201
    return response.json()["id"]


def _conclude(client, project_id, intent_id, **overrides):
    payload = {"worker": "alice", "description": "port 22 is open"}
    payload.update(overrides)
    return client.post(
        f"/projects/{project_id}/intents/{intent_id}/conclude", json=payload
    )


def _complete(client, project_id, **overrides):
    payload = {"from": ["origin"], "description": "done", "worker": "alice"}
    payload.update(overrides)
    return client.post(f"/projects/{project_id}/complete", json=payload)


# --- hash ---------------------------------------------------------------------


def test_hash_is_key_order_independent():
    assert canonical_payload_hash({"a": 1, "b": 2}) == canonical_payload_hash(
        {"b": 2, "a": 1}
    )


def test_hash_excludes_request_id():
    assert canonical_payload_hash({"worker": "a", "request_id": "x"}) == (
        canonical_payload_hash({"worker": "a"})
    )


def test_hash_distinguishes_payloads():
    assert canonical_payload_hash({"worker": "a"}) != canonical_payload_hash(
        {"worker": "b"}
    )


# --- conclude -----------------------------------------------------------------


def test_conclude_retry_with_same_request_id_does_not_duplicate(client, project_id, intent_id):
    first = _conclude(client, project_id, intent_id, request_id="req-1")
    assert first.status_code == 200

    retry = _conclude(client, project_id, intent_id, request_id="req-1")
    assert retry.status_code == 200
    assert retry.json() == first.json()

    detail = client.get(f"/projects/{project_id}").json()
    assert [f["id"] for f in detail["facts"]] == ["origin", "goal", "f001"]
    assert len(detail["intents"]) == 1


def test_conclude_rejects_reused_request_id_with_different_payload(
    client, project_id, intent_id
):
    assert _conclude(client, project_id, intent_id, request_id="req-1").status_code == 200
    response = _conclude(
        client, project_id, intent_id, request_id="req-1", description="different"
    )
    assert response.status_code == 409
    assert "different payload" in response.json()["detail"]


def test_conclude_without_request_id_still_409s(client, project_id, intent_id):
    assert _conclude(client, project_id, intent_id).status_code == 200
    response = _conclude(client, project_id, intent_id)
    assert response.status_code == 409
    assert response.json()["detail"] == "Intent already concluded"


def test_conclude_request_id_scoped_to_intent(client, project_id):
    first = client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["origin"], "description": "one", "creator": "alice"},
    ).json()
    second = client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["origin"], "description": "two", "creator": "bob"},
    ).json()
    ok1 = _conclude(client, project_id, first["id"], request_id="req-1")
    assert ok1.status_code == 200
    ok2 = _conclude(client, project_id, second["id"], request_id="req-1")
    assert ok2.status_code == 200
    assert ok2.json()["fact"]["id"] != ok1.json()["fact"]["id"]


# --- complete -----------------------------------------------------------------


def test_complete_retry_with_same_request_id_does_not_duplicate(client, project_id):
    first = _complete(client, project_id, request_id="req-done")
    assert first.status_code == 200

    retry = _complete(client, project_id, request_id="req-done")
    assert retry.status_code == 200
    assert retry.json() == first.json()

    detail = client.get(f"/projects/{project_id}").json()
    completion_intents = [i for i in detail["intents"] if i["to"] == "goal"]
    assert len(completion_intents) == 1


def test_complete_rejects_reused_request_id_with_different_payload(
    client, project_id
):
    assert _complete(client, project_id, request_id="req-done").status_code == 200
    response = _complete(
        client, project_id, request_id="req-done", description="revised"
    )
    assert response.status_code == 409


def test_complete_retry_survives_reopen_cycle(client, project_id):
    assert _complete(client, project_id, request_id="req-done").status_code == 200
    reopened = client.post(
        f"/projects/{project_id}/reopen",
        json={"description": "new lead", "creator": "alice"},
    )
    assert reopened.status_code == 200
    # The key still replays the original completion even though the project
    # is active again; a genuinely new completion needs a new request_id.
    retry = _complete(client, project_id, request_id="req-done")
    assert retry.status_code == 200
    assert retry.json()["id"] == "i001"
