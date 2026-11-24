from __future__ import annotations

import pytest


def _create(client, **overrides):
    payload = {"title": "lab", "origin": "start", "goal": "finish"}
    payload.update(overrides)
    return client.post("/projects", json=payload)


def test_create_project_returns_origin_and_goal(client):
    response = _create(client)
    assert response.status_code == 201
    body = response.json()
    assert body["project"]["id"] == "proj_001"
    assert body["project"]["status"] == "active"
    assert body["project"]["reason"] is None
    assert {fact["id"]: fact["description"] for fact in body["facts"]} == {
        "origin": "start",
        "goal": "finish",
    }
    by_id = {fact["id"]: fact for fact in body["facts"]}
    for anchor in ("origin", "goal"):
        assert by_id[anchor]["kind"] == anchor
        assert by_id[anchor]["status"] == "verified"
        assert by_id[anchor]["confidence"] == 1.0
    assert body["intents"] == []
    assert body["hints"] == []


def test_create_project_with_inline_hints(client):
    response = _create(client, hints=[{"content": "try admin", "creator": "alice"}])
    assert response.status_code == 201
    hints = response.json()["hints"]
    assert len(hints) == 1
    assert hints[0]["id"] == "h001"
    assert hints[0]["content"] == "try admin"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"title": "x", "origin": "y"},
        {"title": "  ", "origin": "y", "goal": "z"},
        {"title": "x", "origin": "", "goal": "z"},
        {"title": "x", "origin": "y", "goal": "\t"},
    ],
)
def test_create_project_rejects_empty_fields(client, payload):
    response = client.post("/projects", json=payload)
    assert response.status_code == 422


def test_list_projects_includes_counts(client):
    _create(client)
    response = client.get("/projects")
    assert response.status_code == 200
    projects = response.json()
    assert len(projects) == 1
    summary = projects[0]
    assert summary["id"] == "proj_001"
    assert summary["fact_count"] == 2
    assert summary["intent_count"] == 0
    assert summary["working_intent_count"] == 0
    assert summary["unclaimed_intent_count"] == 0
    assert summary["hint_count"] == 0


def test_get_project_detail(client):
    _create(client)
    response = client.get("/projects/proj_001")
    assert response.status_code == 200
    body = response.json()
    assert body["project"]["title"] == "lab"
    assert len(body["facts"]) == 2


def test_get_missing_project_returns_404(client):
    response = client.get("/projects/proj_999")
    assert response.status_code == 404
    assert response.json()["detail"] == "Project not found"


def test_delete_project_removes_it(client):
    _create(client)
    response = client.delete("/projects/proj_001")
    assert response.status_code == 204
    assert client.get("/projects/proj_001").status_code == 404
    assert client.get("/projects").json() == []


def test_delete_missing_project_returns_404(client):
    assert client.delete("/projects/proj_999").status_code == 404


def test_second_project_gets_next_id(client):
    _create(client)
    second = _create(client, title="second")
    assert second.status_code == 201
    assert second.json()["project"]["id"] == "proj_002"


def test_project_ids_are_global_not_per_project(client):
    _create(client)
    _create(client, title="second")
    assert [p["id"] for p in client.get("/projects").json()] == ["proj_001", "proj_002"]
