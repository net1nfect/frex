from __future__ import annotations

import pytest

from cairn.server import artifacts


@pytest.fixture(autouse=True)
def artifact_root(tmp_path):
    root = tmp_path / "artifacts"
    artifacts.configure(root)
    yield root
    artifacts.configure(None)


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
        "status": "success",
        "stdout": "port 22 open",
        "stderr": "no route",
    }
    payload.update(overrides)
    return client.post(f"/projects/{project_id}/runs", json=payload)


def test_create_run_writes_output_files(client, project_id):
    run_id = _create_run(client, project_id).json()["id"]
    run_dir = artifacts.run_dir(project_id, run_id)
    assert (run_dir / "stdout.txt").read_text() == "port 22 open"
    assert (run_dir / "stderr.txt").read_text() == "no route"

    detail = client.get(f"/projects/{project_id}/runs/{run_id}").json()
    assert detail["stdout_path"] == str(run_dir / "stdout.txt")
    assert detail["stderr_path"] == str(run_dir / "stderr.txt")


def test_create_run_without_output_writes_nothing(client, project_id):
    run_id = _create_run(client, project_id, stdout=None, stderr=None).json()["id"]
    assert artifacts.run_dir(project_id, run_id).exists() is False


def test_run_dir_rejects_traversal_ids():
    with pytest.raises(ValueError):
        artifacts.run_dir("../evil", "run_001")
    with pytest.raises(ValueError):
        artifacts.run_dir("proj_001", "../../etc")
    with pytest.raises(ValueError):
        artifacts.run_dir("proj/001", "run_001")
    with pytest.raises(ValueError):
        artifacts.run_dir("proj_001", "run_001\n")


def test_run_dir_stays_inside_root(tmp_path):
    artifacts.configure(tmp_path)
    directory = artifacts.run_dir("proj_001", "run_001")
    assert directory.is_relative_to(tmp_path.resolve())


def test_delete_project_removes_artifacts(client, project_id):
    run_id = _create_run(client, project_id).json()["id"]
    assert artifacts.run_dir(project_id, run_id).exists()
    client.delete(f"/projects/{project_id}")
    assert (artifacts.run_dir(project_id, run_id)).exists() is False
    # The whole per-project tree goes away, not just the run directory.
    project_tree = artifacts.run_dir(project_id, run_id).parent
    assert project_tree.exists() is False
