from __future__ import annotations

import yaml


def _setup(client):
    response = client.post(
        "/projects",
        json={
            "title": "lab",
            "origin": "start",
            "goal": "finish",
            "hints": [{"content": "try admin", "creator": "alice"}],
        },
    )
    project_id = response.json()["project"]["id"]
    intent = client.post(
        f"/projects/{project_id}/intents",
        json={
            "from": ["origin"],
            "description": "scan",
            "creator": "alice",
            "worker": "alice",
        },
    ).json()
    client.post(
        f"/projects/{project_id}/intents/{intent['id']}/conclude",
        json={"worker": "alice", "description": "port 22 open"},
    )
    return project_id


def test_export_yaml_contains_project_graph(client):
    project_id = _setup(client)
    response = client.get(f"/projects/{project_id}/export")
    assert response.status_code == 200
    data = yaml.safe_load(response.text)

    assert data["project"] == {
        "title": "lab",
        "origin": "start",
        "goal": "finish",
    }
    assert data["hints"][0]["content"] == "try admin"
    fact_ids = {fact["id"] for fact in data["facts"]}
    assert fact_ids == {"origin", "goal", "f001"}
    assert data["intents"][0]["from"] == ["origin"]
    assert data["intents"][0]["to"] == "f001"
    assert data["intents"][0]["description"] == "scan"


def test_export_yaml_omits_hints_when_empty(client):
    response = client.post(
        "/projects",
        json={"title": "lab", "origin": "start", "goal": "finish"},
    )
    project_id = response.json()["project"]["id"]
    data = yaml.safe_load(client.get(f"/projects/{project_id}/export").text)
    assert "hints" not in data


def test_export_timeline_orders_events(client):
    project_id = _setup(client)
    response = client.get(f"/projects/{project_id}/export?format=timeline")
    assert response.status_code == 200
    text = response.text
    assert text.index("PROJECT CREATED") < text.index("HINT by alice")
    assert text.index("HINT by alice") < text.index("INTENT DECLARED i001")
    assert text.index("INTENT DECLARED i001") < text.index("INTENT CONCLUDED i001")


def test_export_timeline_completion_event(client):
    project_id = _setup(client)
    client.post(
        f"/projects/{project_id}/complete",
        json={"from": ["f001"], "description": "done", "worker": "alice"},
    )
    text = client.get(f"/projects/{project_id}/export?format=timeline").text
    assert "PROJECT COMPLETED by alice" in text
    assert "via: i002 from f001" in text


def test_export_rejects_unknown_format(client):
    response = client.post(
        "/projects",
        json={"title": "lab", "origin": "start", "goal": "finish"},
    )
    project_id = response.json()["project"]["id"]
    response = client.get(f"/projects/{project_id}/export?format=json")
    assert response.status_code == 400
    assert response.json()["detail"] == "Supported formats: yaml, timeline"


def test_export_missing_project(client):
    assert client.get("/projects/proj_999/export").status_code == 404
