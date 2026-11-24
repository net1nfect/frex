"""End-to-end evidence and verification flow without docker.

The server runs for real through the ASGI TestClient; the dispatcher's
verify task talks to it through a thin adapter, so the whole path —
run recorded, conclude with evidence, replay, verification submitted,
fact status flipped, project completed with goal evidence — is
exercised against the actual API and database.
"""

from __future__ import annotations

import pytest

from cairn.dispatcher.config import (
    DispatchConfig,
    RuntimeConfig,
    TasksConfig,
    VerifyTaskConfig,
    WorkerConfig,
)
from cairn.dispatcher.runtime.process import ProcessResult
from cairn.dispatcher.tasks import verify as verify_module
from cairn.dispatcher.tasks.common import HealthcheckRun
from cairn.dispatcher.tasks.verify import run_verify_task


class FakeContainers:
    def ensure_running(self, project_id):
        return "project-ctr"


class ServerAdapter:
    """Adapts the FastAPI TestClient to the calls run_verify_task makes."""

    def __init__(self, client):
        self._client = client

    def get_fact(self, project_id, fact_id):
        response = self._client.get(f"/projects/{project_id}/facts/{fact_id}")
        assert response.status_code == 200
        return response.json()

    def get_settings(self):
        from types import SimpleNamespace

        body = self._client.get("/settings").json()
        return SimpleNamespace(verify_max_attempts=body["verify_max_attempts"])

    def create_run(self, project_id, payload):
        from cairn.dispatcher.protocol.client import ApiResult

        response = self._client.post(f"/projects/{project_id}/runs", json=payload)
        assert response.status_code == 201, response.text
        return ApiResult(status_code=201, data=response.json())

    def create_verification(self, project_id, fact_id, payload):
        response = self._client.post(
            f"/projects/{project_id}/facts/{fact_id}/verifications", json=payload
        )
        from cairn.dispatcher.protocol.client import ApiResult

        return ApiResult(
            status_code=response.status_code,
            data=response.json() if response.status_code < 500 else None,
            text=response.text,
        )


def make_worker():
    return WorkerConfig(
        name="kali-01",
        type="mock",
        task_types=["verify"],
        max_running=1,
        priority=0,
    )


def make_config():
    return DispatchConfig.model_construct(
        tasks=TasksConfig.model_construct(
            bootstrap=None,
            reason=None,
            explore=None,
            verify=VerifyTaskConfig(timeout=60, container="reuse"),
        ),
        runtime=RuntimeConfig.model_construct(healthcheck_timeout=10),
    )


@pytest.fixture
def replay_env(monkeypatch):
    """Fake container execution: the replay reproduces the evidence."""
    health = HealthcheckRun(
        result=ProcessResult(returncode=0, stdout="", stderr=""),
        duration_ms=1,
    )
    monkeypatch.setattr(verify_module, "run_healthcheck", lambda *a, **k: health)

    def fake_run(self, container_manager, container_name, worker, argv, *, phase, timeout_seconds, cancellation=None, lease=None):
        command = argv[2]
        stdout = {
            "nmap -sV 10.0.0.5": "22/tcp open ssh",
            "type C:\\flag.txt": "flag{dc01-pwned}",
        }.get(command, "")
        result = ProcessResult(returncode=0, stdout=stdout, stderr="")
        # Record through the real RunRecorder so the run row (and its id)
        # exists server-side, exactly like production.
        self.record(result, 42, phase)
        return result

    monkeypatch.setattr(verify_module.RunRecorder, "run_worker_process", fake_run)
    monkeypatch.setattr(
        verify_module.RunRecorder, "run_healthcheck", lambda *a, **k: health
    )


def _conclude(client, project_id, intent_id, **overrides):
    payload = {"worker": "alice", "description": "port 22 is open"}
    payload.update(overrides)
    return client.post(
        f"/projects/{project_id}/intents/{intent_id}/conclude", json=payload
    )


def test_full_evidence_verify_complete_flow(client, replay_env):
    # 1. Create project, intent, and a recorded run with output.
    project_id = client.post(
        "/projects", json={"title": "lab", "origin": "start", "goal": "domain admin"}
    ).json()["project"]["id"]
    intent_id = client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["origin"], "description": "scan", "creator": "alice"},
    ).json()["id"]
    run_id = client.post(
        f"/projects/{project_id}/runs",
        json={
            "task_type": "explore",
            "worker": "kali-01",
            "command": "nmap -sV 10.0.0.5",
            "exit_code": 0,
            "status": "success",
            "stdout": "22/tcp open ssh",
        },
    ).json()["id"]

    # 2. Conclude with evidence tied to the run.
    concluded = _conclude(
        client,
        project_id,
        intent_id,
        run_id=run_id,
        kind="observation",
        confidence=0.9,
        match_value="22/tcp",
    ).json()
    fact_id = concluded["fact"]["id"]
    assert concluded["evidence"][0]["run_id"] == run_id
    assert concluded["fact"]["status"] == "hypothesis"

    # 3. The dispatcher verifies: replay matches -> pass -> verified.
    adapter = ServerAdapter(client)
    status = run_verify_task(
        make_config(), adapter, FakeContainers(), project_id, fact_id, make_worker(), None
    )
    assert status == "verified"
    detail = client.get(f"/projects/{project_id}/facts/{fact_id}").json()
    assert detail["fact"]["status"] == "verified"
    assert len(detail["verifications"]) == 1
    assert detail["verifications"][0]["method"] == "replay"
    assert detail["verifications"][0]["result"] == "pass"

    # 4. A mismatching replay refutes a fresh hypothesis.
    client2_project = client.post(
        "/projects", json={"title": "lab2", "origin": "s", "goal": "g"}
    ).json()["project"]["id"]
    intent2 = client.post(
        f"/projects/{client2_project}/intents",
        json={"from": ["origin"], "description": "scan", "creator": "alice"},
    ).json()["id"]
    run2 = client.post(
        f"/projects/{client2_project}/runs",
        json={
            "task_type": "explore",
            "worker": "kali-01",
            "command": "nmap -sV 10.0.0.9",
            "exit_code": 0,
            "status": "success",
            "stdout": "nothing open",
        },
    ).json()["id"]
    fact2 = _conclude(
        client, client2_project, intent2, run_id=run2, match_value="22/tcp"
    ).json()["fact"]["id"]
    status = run_verify_task(
        make_config(), ServerAdapter(client), FakeContainers(), client2_project, fact2, make_worker(), None
    )
    assert status == "refuted"
    detail2 = client.get(f"/projects/{client2_project}/facts/{fact2}").json()
    assert detail2["fact"]["status"] == "refuted"

    # 5. Complete requires verified access evidence; a hypothesis is 409.
    access_intent = client.post(
        f"/projects/{project_id}/intents",
        json={"from": [fact_id], "description": "get shell", "creator": "alice"},
    ).json()["id"]
    access_run = client.post(
        f"/projects/{project_id}/runs",
        json={
            "task_type": "explore",
            "worker": "kali-01",
            "command": "type C:\\flag.txt",
            "exit_code": 0,
            "status": "success",
            "stdout": "flag{dc01-pwned}",
        },
    ).json()["id"]
    access_fact = _conclude(
        client,
        project_id,
        access_intent,
        run_id=access_run,
        kind="access",
        description="Administrator shell on dc01",
        match_value="flag{",
    ).json()["fact"]["id"]

    response = client.post(
        f"/projects/{project_id}/complete",
        json={
            "from": [fact_id],
            "description": "done",
            "worker": "alice",
            "goal_evidence": [access_fact],
        },
    )
    assert response.status_code == 409  # access fact is still hypothesis

    assert (
        run_verify_task(
            make_config(),
            ServerAdapter(client),
            FakeContainers(),
            project_id,
            access_fact,
            make_worker(),
            None,
        )
        == "verified"
    )
    response = client.post(
        f"/projects/{project_id}/complete",
        json={
            "from": [fact_id],
            "description": "done",
            "worker": "alice",
            "goal_evidence": [access_fact],
        },
    )
    assert response.status_code == 200
    assert response.json()["to"] == "goal"
