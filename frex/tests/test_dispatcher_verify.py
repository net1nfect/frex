from __future__ import annotations

import pytest

from frex.dispatcher.config import DispatchConfig, TasksConfig, VerifyTaskConfig, WorkerConfig
from frex.dispatcher.contracts import evaluate_evidence
from frex.dispatcher.runtime.process import ProcessResult
from frex.dispatcher.tasks import verify as verify_module
from frex.dispatcher.tasks.verify import run_verify_task


class FakeApiResult:
    def __init__(self, status_code=201, data=None):
        self.status_code = status_code
        self.data = data

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300


class FakeClient:
    def __init__(self, fact_detail, settings=None):
        self.fact_detail = fact_detail
        self.settings = settings or {"verify_max_attempts": 3}
        self.verifications: list[tuple[str, dict]] = []

    def get_fact(self, project_id, fact_id):
        return self.fact_detail

    def get_settings(self):
        from types import SimpleNamespace

        return SimpleNamespace(**self.settings)

    def create_verification(self, project_id, fact_id, payload):
        self.verifications.append((fact_id, payload))
        return FakeApiResult(201, {"id": "ver_001"})


class FakeContainers:
    def __init__(self):
        self.created = 0
        self.removed = []
        self.ensure_running_calls = 0

    def create_verify_container(self, project_id):
        self.created += 1
        return "verify-ctr"

    def remove_container(self, name):
        self.removed.append(name)

    def ensure_running(self, project_id):
        self.ensure_running_calls += 1
        return "project-ctr"


def make_worker():
    return WorkerConfig(
        name="kali-01",
        type="mock",
        task_types=["verify"],
        max_running=1,
        priority=0,
    )


def make_config(**verify_kwargs):
    # DispatchConfig is a full pydantic model; only the fields the verify
    # task reads are needed, so build it via TasksConfig with the mock
    # defaults the real config loader would produce.
    config = DispatchConfig.model_construct(
        tasks=TasksConfig.model_construct(
            bootstrap=None,
            reason=None,
            explore=None,
            verify=VerifyTaskConfig(**verify_kwargs),
        ),
        runtime=__import__("frex.dispatcher.config", fromlist=["RuntimeConfig"]).RuntimeConfig.model_construct(
            healthcheck_timeout=30,
        ),
    )
    return config


def make_fact_detail(**overrides):
    fact = {
        "id": "f002",
        "description": "port 22 is open",
        "kind": "observation",
        "status": "hypothesis",
        "confidence": 0.9,
    }
    fact.update(overrides.pop("fact", {}))
    detail = {
        "fact": fact,
        "evidence": [
            {
                "id": "ev_001",
                "run_id": "run_001",
                "fact_id": "f002",
                "kind": "observation",
                "command": "nmap -sV 10.0.0.5",
                "exit_code": 0,
                "match_rule": "contains",
                "match_value": "22/tcp",
                "stdout_sha256": None,
            }
        ],
        "verifications": [],
    }
    detail.update(overrides)
    return detail


def patch_runs(monkeypatch, *, replay=None, healthcheck_code=0):
    health = __import__("frex.dispatcher.tasks.common", fromlist=["HealthcheckRun"]).HealthcheckRun(
        result=ProcessResult(returncode=healthcheck_code, stdout="", stderr=""),
        duration_ms=5,
    )
    monkeypatch.setattr(verify_module, "run_healthcheck", lambda *a, **k: health)
    recorded = {}
    if replay is None:
        replay = ProcessResult(returncode=0, stdout="22/tcp open ssh", stderr="")

    def fake_run(self, container_manager, container_name, worker, argv, *, phase, timeout_seconds, cancellation=None, lease=None):
        recorded["argv"] = argv
        recorded["phase"] = phase
        # record() sets this on a successful POST; simulate the server
        # accepting the run row.
        self.last_run_id = "run_001"
        return replay

    monkeypatch.setattr(verify_module.RunRecorder, "run_worker_process", fake_run)
    return recorded


def test_evaluate_evidence_rules():
    assert evaluate_evidence(
        "contains", match_value="22/tcp", exit_code=0, stdout="22/tcp open", stdout_sha256=None, replay_exit_code=0
    )[0] == "pass"
    assert evaluate_evidence(
        "contains", match_value="22/tcp", exit_code=0, stdout="nothing", stdout_sha256=None, replay_exit_code=0
    )[0] == "fail"
    assert evaluate_evidence(
        "regex", match_value=r"\d+ open", exit_code=0, stdout="port 22 open", stdout_sha256=None, replay_exit_code=0
    )[0] == "pass"
    assert evaluate_evidence(
        "regex", match_value="([bad", exit_code=0, stdout="x", stdout_sha256=None, replay_exit_code=0
    ) == ("inconclusive", "invalid_regex")
    assert evaluate_evidence(
        "exit_code", match_value=None, exit_code=0, stdout="", stdout_sha256=None, replay_exit_code=1
    )[0] == "fail"
    assert evaluate_evidence(
        "exit_code", match_value=None, exit_code=0, stdout="", stdout_sha256=None, replay_exit_code=None
    )[0] == "inconclusive"
    import hashlib

    text = "identical output"
    assert evaluate_evidence(
        "exact",
        match_value=None,
        exit_code=0,
        stdout=text,
        stdout_sha256=hashlib.sha256(text.encode()).hexdigest(),
        replay_exit_code=0,
    )[0] == "pass"
    assert evaluate_evidence(
        "manual", match_value=None, exit_code=0, stdout="x", stdout_sha256=None, replay_exit_code=0
    )[0] == "inconclusive"


def test_verify_pass_posts_verification_and_uses_fresh_container(monkeypatch):
    recorded = patch_runs(monkeypatch)
    client = FakeClient(make_fact_detail())
    containers = FakeContainers()
    status = run_verify_task(
        make_config(), client, containers, "proj_001", "f002", make_worker(), None
    )
    assert status == "verified"
    assert containers.created == 1
    assert containers.removed == ["verify-ctr"]
    assert containers.ensure_running_calls == 0
    fact_id, payload = client.verifications[0]
    assert fact_id == "f002"
    assert payload["method"] == "replay"
    assert payload["result"] == "pass"
    assert payload["run_id"] == "run_001"
    assert "22/tcp" in payload["observed"]
    assert recorded["argv"] == ["sh", "-c", "nmap -sV 10.0.0.5"]
    assert recorded["phase"] == "verify"


def test_verify_fail_on_mismatch(monkeypatch):
    patch_runs(monkeypatch, replay=ProcessResult(returncode=0, stdout="all closed", stderr=""))
    client = FakeClient(make_fact_detail())
    status = run_verify_task(
        make_config(), client, FakeContainers(), "proj_001", "f002", make_worker(), None
    )
    assert status == "refuted"
    assert client.verifications[0][1]["result"] == "fail"


def test_verify_timeout_is_inconclusive_not_fail(monkeypatch):
    patch_runs(
        monkeypatch,
        replay=ProcessResult(returncode=124, stdout="", stderr="", timed_out=True),
    )
    client = FakeClient(make_fact_detail())
    status = run_verify_task(
        make_config(), client, FakeContainers(), "proj_001", "f002", make_worker(), None
    )
    assert status == "inconclusive"
    assert client.verifications[0][1]["result"] == "inconclusive"


def test_verify_reuse_mode_uses_project_container(monkeypatch):
    patch_runs(monkeypatch)
    client = FakeClient(make_fact_detail())
    containers = FakeContainers()
    status = run_verify_task(
        make_config(container="reuse"),
        client,
        containers,
        "proj_001",
        "f002",
        make_worker(),
        None,
    )
    assert status == "verified"
    assert containers.created == 0
    assert containers.ensure_running_calls == 1
    assert containers.removed == []


def test_verify_skips_verified_fact(monkeypatch):
    patch_runs(monkeypatch)
    client = FakeClient(make_fact_detail(fact={"status": "verified"}))
    containers = FakeContainers()
    status = run_verify_task(
        make_config(), client, containers, "proj_001", "f002", make_worker(), None
    )
    assert status == "skipped"
    assert client.verifications == []
    assert containers.created == 0


def test_verify_skips_when_max_attempts_reached(monkeypatch):
    patch_runs(monkeypatch)
    detail = make_fact_detail(verifications=[{"id": f"ver_00{i}"} for i in range(3)])
    client = FakeClient(detail, settings={"verify_max_attempts": 3})
    status = run_verify_task(
        make_config(), client, FakeContainers(), "proj_001", "f002", make_worker(), None
    )
    assert status == "skipped"


def test_verify_skips_without_replayable_evidence(monkeypatch):
    patch_runs(monkeypatch)
    detail = make_fact_detail(evidence=[{"id": "ev_001", "command": None, "match_rule": "contains"}])
    client = FakeClient(detail)
    status = run_verify_task(
        make_config(), client, FakeContainers(), "proj_001", "f002", make_worker(), None
    )
    assert status == "skipped"


def test_verify_unhealthy_worker(monkeypatch):
    patch_runs(monkeypatch, healthcheck_code=1)
    client = FakeClient(make_fact_detail())
    containers = FakeContainers()
    status = run_verify_task(
        make_config(), client, containers, "proj_001", "f002", make_worker(), None
    )
    assert status == "unhealthy"
    assert client.verifications == []
    # The fresh container is still cleaned up.
    assert containers.removed == ["verify-ctr"]


def test_verify_submission_failure_returns_failed(monkeypatch):
    patch_runs(monkeypatch)
    client = FakeClient(make_fact_detail())

    def reject(project_id, fact_id, payload):
        return FakeApiResult(409, {"detail": "conflict"})

    client.create_verification = reject
    status = run_verify_task(
        make_config(), client, FakeContainers(), "proj_001", "f002", make_worker(), None
    )
    assert status == "failed"
