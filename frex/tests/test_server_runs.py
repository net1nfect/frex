from __future__ import annotations

import hashlib

import pytest


@pytest.fixture
def project_id(client):
    response = client.post(
        "/projects",
        json={"title": "lab", "origin": "start", "goal": "finish"},
    )
    assert response.status_code == 201
    return response.json()["project"]["id"]


def _create_run(client, project_id, **overrides):
    payload = {
        "task_type": "explore",
        "worker": "kali-01",
        "phase": "execute",
        "intent_id": "i001",
        "command": "nmap -sV 10.0.0.5",
        "exit_code": 0,
        "duration_ms": 12400,
        "status": "success",
        "stdout": "22/tcp open ssh",
        "stderr": "warning: something",
    }
    payload.update(overrides)
    return client.post(f"/projects/{project_id}/runs", json=payload)


def test_create_run_returns_metadata_and_hash(client, project_id):
    response = _create_run(client, project_id)
    assert response.status_code == 201
    body = response.json()
    assert body["id"] == "run_001"
    assert body["project_id"] == project_id
    assert body["task_type"] == "explore"
    assert body["worker"] == "kali-01"
    assert body["status"] == "success"
    assert body["timed_out"] is False
    assert body["stdout_truncated"] is False
    assert "stdout" not in body
    assert "stdout_sha256" not in body


def test_run_ids_are_scoped_per_project(client, project_id):
    assert _create_run(client, project_id).json()["id"] == "run_001"
    second = client.post(
        "/projects",
        json={"title": "b", "origin": "x", "goal": "y"},
    ).json()["project"]["id"]
    assert _create_run(client, second).json()["id"] == "run_001"


def test_create_run_rejects_unknown_task_type_or_status(client, project_id):
    assert _create_run(client, project_id, task_type="nope").status_code == 422
    assert _create_run(client, project_id, status="fine").status_code == 422
    assert _create_run(client, project_id, phase="nope").status_code == 422


def test_create_run_rejects_blank_worker(client, project_id):
    assert _create_run(client, project_id, worker="  ").status_code == 422


def test_create_run_rejects_oversized_output(client, project_id):
    big = "x" * (2 * 1024 * 1024 + 1)
    response = _create_run(client, project_id, stdout=big)
    assert response.status_code == 413


def test_create_run_on_missing_project(client):
    response = _create_run(client, "proj_999")
    assert response.status_code == 404


def test_create_run_on_stopped_project(client, project_id):
    client.put(f"/projects/{project_id}/status", json={"status": "stopped"})
    assert _create_run(client, project_id).status_code == 403


def test_list_runs_filters(client, project_id):
    _create_run(client, project_id)
    _create_run(client, project_id, task_type="healthcheck", phase="healthcheck",
                status="failed", intent_id=None, command=None, stdout=None)
    _create_run(client, project_id, task_type="reason", phase="execute",
                intent_id=None, command=None, stdout=None)

    all_runs = client.get(f"/projects/{project_id}/runs").json()
    assert [run["id"] for run in all_runs] == ["run_001", "run_002", "run_003"]

    only_explore = client.get(
        f"/projects/{project_id}/runs", params={"task_type": "explore"}
    ).json()
    assert [run["id"] for run in only_explore] == ["run_001"]

    only_failed = client.get(
        f"/projects/{project_id}/runs", params={"status": "failed"}
    ).json()
    assert [run["id"] for run in only_failed] == ["run_002"]

    only_reason = client.get(
        f"/projects/{project_id}/runs", params={"intent_id": "i001"}
    ).json()
    assert {run["id"] for run in only_reason} == {"run_001"}


def test_list_runs_pagination(client, project_id):
    for _ in range(3):
        _create_run(client, project_id)
    page = client.get(f"/projects/{project_id}/runs", params={"limit": 2, "offset": 1}).json()
    assert [run["id"] for run in page] == ["run_002", "run_003"]
    assert client.get(
        f"/projects/{project_id}/runs", params={"limit": 500}
    ).status_code == 422


def test_list_runs_missing_project(client):
    assert client.get("/projects/proj_999/runs").status_code == 404


def test_get_run_detail_without_output_by_default(client, project_id):
    created = _create_run(client, project_id).json()
    response = client.get(f"/projects/{project_id}/runs/{created['id']}")
    assert response.status_code == 200
    body = response.json()
    assert body["stdout"] is None
    assert body["stderr"] is None
    assert body["stdout_sha256"] == hashlib.sha256(b"22/tcp open ssh").hexdigest()


def test_get_run_detail_includes_output(client, project_id):
    created = _create_run(client, project_id).json()
    base = f"/projects/{project_id}/runs/{created['id']}"
    stdout_only = client.get(base, params={"include": "stdout"}).json()
    assert stdout_only["stdout"] == "22/tcp open ssh"
    assert stdout_only["stderr"] is None
    both = client.get(base, params={"include": "both"}).json()
    assert both["stdout"] == "22/tcp open ssh"
    assert both["stderr"] == "warning: something"


def test_get_missing_run(client, project_id):
    assert client.get(f"/projects/{project_id}/runs/run_999").status_code == 404


def test_runs_deleted_with_project(client, project_id):
    _create_run(client, project_id)
    client.delete(f"/projects/{project_id}")
    # Recreating the project shows the cascade removed the runs.
    new_id = client.post(
        "/projects", json={"title": "t", "origin": "a", "goal": "b"}
    ).json()["project"]["id"]
    assert client.get(f"/projects/{new_id}/runs").json() == []


def test_usage_endpoint_rolls_up_tokens_and_cost(client, project_id):
    for i, (ti, to, cost) in enumerate([(100, 50, 0.002), (200, 100, 0.004)]):
        response = client.post(
            f"/projects/{project_id}/runs",
            json={
                "task_type": "explore",
                "worker": "kali-01",
                "status": "success",
                "tokens_in": ti,
                "tokens_out": to,
                "cost_usd": cost,
            },
        )
        assert response.status_code == 201
    client.post(
        f"/projects/{project_id}/runs",
        json={"task_type": "verify", "worker": "kali-01", "status": "success"},
    )

    body = client.get(f"/projects/{project_id}/usage").json()
    assert body["by_task_type"]["explore"]["runs"] == 2
    assert body["by_task_type"]["explore"]["tokens_in"] == 300
    assert body["by_task_type"]["explore"]["tokens_out"] == 150
    assert body["by_task_type"]["explore"]["cost_usd"] == 0.006
    assert body["by_task_type"]["verify"]["tokens_in"] == 0
    assert body["totals"]["runs"] == 3
    assert body["totals"]["tokens_in"] == 300
    assert body["totals"]["cost_usd"] == 0.006
    assert client.get("/projects/proj_999/usage").status_code == 404
