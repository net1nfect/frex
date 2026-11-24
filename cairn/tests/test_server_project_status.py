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


def test_update_title(client, project_id):
    response = client.put(f"/projects/{project_id}/title", json={"title": "renamed"})
    assert response.status_code == 200
    assert response.json()["title"] == "renamed"
    assert client.get(f"/projects/{project_id}").json()["project"]["title"] == "renamed"


def test_update_title_rejects_blank(client, project_id):
    response = client.put(f"/projects/{project_id}/title", json={"title": "   "})
    assert response.status_code == 422


def test_update_title_missing_project(client):
    response = client.put("/projects/proj_999/title", json={"title": "x"})
    assert response.status_code == 404


def test_stop_project_releases_claimed_intents(client, project_id):
    created = client.post(
        f"/projects/{project_id}/intents",
        json={
            "from": ["origin"],
            "description": "scan",
            "creator": "alice",
            "worker": "alice",
        },
    )
    assert created.status_code == 201
    assert created.json()["worker"] == "alice"

    response = client.put(f"/projects/{project_id}/status", json={"status": "stopped"})
    assert response.status_code == 200
    assert response.json()["status"] == "stopped"

    detail = client.get(f"/projects/{project_id}").json()
    assert detail["intents"][0]["worker"] is None


def test_resume_stopped_project(client, project_id):
    client.put(f"/projects/{project_id}/status", json={"status": "stopped"})
    response = client.put(f"/projects/{project_id}/status", json={"status": "active"})
    assert response.status_code == 200
    assert response.json()["status"] == "active"


def test_status_is_idempotent(client, project_id):
    first = client.put(f"/projects/{project_id}/status", json={"status": "stopped"})
    second = client.put(f"/projects/{project_id}/status", json={"status": "stopped"})
    assert first.status_code == second.status_code == 200
    assert second.json()["status"] == "stopped"


def test_completed_project_cannot_change_status(client, project_id):
    complete = client.post(
        f"/projects/{project_id}/complete",
        json={"from": ["origin"], "description": "done", "worker": "alice"},
    )
    assert complete.status_code == 200
    response = client.put(f"/projects/{project_id}/status", json={"status": "active"})
    assert response.status_code == 409
    assert response.json()["detail"] == "Completed projects cannot change status"


def test_status_rejects_unknown_value(client, project_id):
    response = client.put(f"/projects/{project_id}/status", json={"status": "paused"})
    assert response.status_code == 422


def test_status_missing_project(client):
    response = client.put("/projects/proj_999/status", json={"status": "stopped"})
    assert response.status_code == 404
