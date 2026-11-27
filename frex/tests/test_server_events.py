from __future__ import annotations

import json

import pytest


@pytest.fixture
def project_id(client):
    return client.post(
        "/projects", json={"title": "lab", "origin": "s", "goal": "g"}
    ).json()["project"]["id"]


def _post(client, project_id, events, task_type="explore"):
    return client.post(f"/projects/{project_id}/events", json={"events": events, "task_type": task_type})


def test_events_post_assigns_monotonic_seq(client, project_id):
    response = _post(
        client,
        project_id,
        [
            {"kind": "task.started", "data": {"model": "qwen"}},
            {"kind": "tool.started", "data": {"tool": "run_command"}},
            {"kind": "task.finished", "data": {"status": "completed"}},
        ],
    )
    assert response.status_code == 201
    seqs = [e["seq"] for e in response.json()["events"]]
    assert seqs == sorted(seqs) and len(set(seqs)) == 3

    second = _post(client, project_id, [{"kind": "error", "data": {"m": "x"}}])
    assert second.json()["events"][0]["seq"] > seqs[-1]


def test_events_reject_unknown_kind(client, project_id):
    response = _post(client, project_id, [{"kind": "vibes.up"}])
    assert response.status_code == 422


def test_events_reject_empty_batch(client, project_id):
    assert _post(client, project_id, []).status_code == 422


def test_events_poll_with_since_cursor(client, project_id):
    _post(
        client,
        project_id,
        [{"kind": "task.started"}, {"kind": "tool.output", "data": {"ok": True}}],
    )
    body = client.get(f"/projects/{project_id}/events", params={"since": 0}).json()
    assert [e["kind"] for e in body["events"]] == ["task.started", "tool.output"]
    assert body["last_seq"] == 2
    assert body["events"][1]["data"] == {"ok": True}

    _post(client, project_id, [{"kind": "task.finished", "data": {"status": "completed"}}])
    after = client.get(f"/projects/{project_id}/events", params={"since": body["last_seq"]}).json()
    assert [e["kind"] for e in after["events"]] == ["task.finished"]

    # Completed projects stop accepting events but keep serving them.
    client.post(
        f"/projects/{project_id}/complete",
        json={"from": ["origin"], "description": "d", "worker": "w"},
    )
    assert _post(client, project_id, [{"kind": "error"}]).status_code == 403
    assert client.get(f"/projects/{project_id}/events").status_code == 200
    assert client.get("/projects/proj_999/events").status_code == 404


def test_events_stream_sse(client, project_id):
    _post(
        client,
        project_id,
        [{"kind": "task.started", "data": {"m": 1}}, {"kind": "error", "data": {"m": 2}}],
    )
    with client.stream(
        "GET", f"/projects/{project_id}/events/stream", params={"max_polls": 1}
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        chunks = []
        for chunk in response.iter_text():
            chunks.append(chunk)
            if len(chunks) >= 2:
                break
    text = "".join(chunks)
    assert "event: task.started" in text
    assert "event: error" in text
    assert json.loads(text.split("data: ")[1].splitlines()[0])["kind"] == "task.started"
