from __future__ import annotations

from concurrent.futures import Future
from types import SimpleNamespace

import pytest

from cairn.dispatcher.config import WorkerConfig
from cairn.dispatcher.models import ReasonCheckpoint, RunningTask
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.scheduler.loop import DispatcherLoop
from cairn.server.models import Fact, Intent, ProjectDetail, ProjectMeta, ProjectSummary


class ExplodingClient:
    def get_project(self, project_id):  # pragma: no cover - should not be called
        raise AssertionError(f"unexpected get_project({project_id})")

    def export_project(self, project_id):  # pragma: no cover
        raise AssertionError(f"unexpected export_project({project_id})")


def make_worker(name, task_types=("reason", "explore", "bootstrap"), max_running=1, priority=0):
    return WorkerConfig(
        name=name,
        type="mock",
        task_types=list(task_types),
        max_running=max_running,
        priority=priority,
    )


def make_loop(workers=None):
    loop = object.__new__(DispatcherLoop)
    loop.config = SimpleNamespace(
        runtime=SimpleNamespace(
            max_workers=4,
            max_running_projects=2,
            max_project_workers=2,
            interval=3,
            healthcheck_timeout=2,
        ),
        workers=workers or [],
    )
    loop.futures = {}
    loop.worker_unhealthy_until = {}
    loop.worker_rejected_until = {}
    loop.reason_checkpoints = {}
    loop.project_cursor = 0
    loop._log_state = {}
    loop._cleanup_pending = set()
    loop._inactive_cleanup_done = {}
    loop.runtime_project_ids = set()
    loop.client = ExplodingClient()
    loop.container_manager = SimpleNamespace(
        container_name=lambda project_id: f"cairn-dispatch-{project_id}"
    )
    return loop


def make_intent(intent_id="i001", *, from_ids=("origin",), description="scan", creator="alice", worker=None, to=None):
    return Intent(
        id=intent_id,
        **{"from": list(from_ids)},
        to=to,
        description=description,
        creator=creator,
        worker=worker,
        last_heartbeat_at=None,
        created_at="2026-01-01T00:00:00Z",
        concluded_at=None,
    )


def make_project(intents=(), facts=("origin", "goal"), project_id="proj_001"):
    return ProjectDetail(
        project=ProjectMeta(
            id=project_id,
            title="lab",
            status="active",
            created_at="2026-01-01T00:00:00Z",
            reason=None,
        ),
        facts=[Fact(id=fact_id, description=fact_id) for fact_id in facts],
        intents=list(intents),
        hints=[],
    )


def make_summary(project_id="proj_001", status="active"):
    return ProjectSummary(
        id=project_id,
        title="lab",
        status=status,
        created_at="2026-01-01T00:00:00Z",
        reason=None,
        fact_count=2,
        intent_count=0,
        working_intent_count=0,
        unclaimed_intent_count=0,
        hint_count=0,
    )


def make_running(project_id="proj_001", task_type="explore", worker="w1", intent_id="i001"):
    return RunningTask(
        project_id=project_id,
        task_type=task_type,
        worker_name=worker,
        cancellation=TaskCancellation(),
        intent_id=intent_id,
    )


# --- initial project detection ----------------------------------------------


def test_initial_project_with_only_origin_and_goal():
    loop = make_loop()
    assert loop._is_initial_project(make_project()) is True


def test_project_with_extra_fact_is_not_initial():
    loop = make_loop()
    assert loop._is_initial_project(make_project(facts=("origin", "goal", "f001"))) is False


def test_project_with_only_bootstrap_intent_is_initial():
    loop = make_loop()
    intent = make_intent(description="bootstrap", creator="dispatcher.bootstrap")
    assert loop._is_initial_project(make_project(intents=[intent])) is True


def test_project_with_real_intent_is_not_initial():
    loop = make_loop()
    intent = make_intent(description="scan")
    assert loop._is_initial_project(make_project(intents=[intent])) is False


def test_bootstrap_intent_shape():
    loop = make_loop()
    good = make_intent(description="bootstrap", creator="dispatcher.bootstrap")
    assert loop._is_bootstrap_intent(good) is True
    assert loop._is_bootstrap_intent(make_intent(description="bootstrap")) is False
    assert loop._is_bootstrap_intent(make_intent(description="scan", creator="dispatcher.bootstrap")) is False
    assert loop._is_bootstrap_intent(
        make_intent(description="bootstrap", creator="dispatcher.bootstrap", from_ids=("f001",))
    ) is False


def test_get_bootstrap_intent_prefers_unclaimed():
    loop = make_loop()
    claimed = make_intent("i001", description="bootstrap", creator="dispatcher.bootstrap", worker="w1")
    unclaimed = make_intent("i002", description="bootstrap", creator="dispatcher.bootstrap")
    chosen = loop._get_bootstrap_intent(make_project(intents=[claimed, unclaimed]))
    assert chosen is not None
    assert chosen.id == "i002"


def test_get_bootstrap_intent_returns_none_without_one():
    loop = make_loop()
    assert loop._get_bootstrap_intent(make_project(intents=[make_intent()])) is None


# --- reason trigger ----------------------------------------------------------


def test_reason_trigger_initial_without_checkpoint():
    loop = make_loop()
    assert loop._reason_trigger(make_project()) == "initial"


def test_reason_trigger_none_when_unchanged():
    loop = make_loop()
    project = make_project()
    loop.reason_checkpoints[project.project.id] = ReasonCheckpoint(
        fact_count=2, hint_count=0, open_intent_count=0
    )
    assert loop._reason_trigger(project) is None


def test_reason_trigger_on_new_fact():
    loop = make_loop()
    project = make_project(facts=("origin", "goal", "f001"))
    loop.reason_checkpoints[project.project.id] = ReasonCheckpoint(
        fact_count=2, hint_count=0, open_intent_count=0
    )
    assert loop._reason_trigger(project) == "facts:2->3"


def test_reason_trigger_on_open_intents_drained():
    loop = make_loop()
    project = make_project()
    loop.reason_checkpoints[project.project.id] = ReasonCheckpoint(
        fact_count=2, hint_count=0, open_intent_count=1
    )
    assert loop._reason_trigger(project) == "open_intents:1->0"


# --- worker selection --------------------------------------------------------


def test_select_worker_filters_by_task_type():
    loop = make_loop([make_worker("explore-only", task_types=("explore",))])
    selection = loop._select_worker("proj_001", "reason")
    assert selection.worker is None
    assert selection.blocked_task_type == ["explore-only"]


def test_select_worker_respects_max_running():
    worker = make_worker("w1", max_running=1)
    loop = make_loop([worker])
    loop.futures[object()] = make_running(worker="w1")
    selection = loop._select_worker("proj_001", "explore")
    assert selection.worker is None
    assert selection.blocked_busy == ["w1(1/1)"]


def test_select_worker_respects_unhealthy_window():
    loop = make_loop([make_worker("w1")])
    loop.worker_unhealthy_until["w1"] = 10**12
    selection = loop._select_worker("proj_001", "explore")
    assert selection.worker is None
    assert selection.blocked_unhealthy


def test_select_worker_respects_rejected_window():
    loop = make_loop([make_worker("w1")])
    loop.worker_rejected_until[("proj_001", "explore", "w1")] = 10**12
    selection = loop._select_worker("proj_001", "explore")
    assert selection.worker is None
    assert selection.blocked_rejected


def test_select_worker_chooses_available():
    loop = make_loop([make_worker("w1"), make_worker("w2")])
    selection = loop._select_worker("proj_001", "explore")
    assert selection.worker is not None
    assert selection.worker.name in {"w1", "w2"}


# --- dispatch limits ---------------------------------------------------------


def test_dispatch_stops_at_max_workers():
    loop = make_loop()
    for index in range(4):
        loop.futures[object()] = make_running(project_id=f"proj_{index:03d}", intent_id=None)
    loop._dispatch_available([make_summary()])  # ExplodingClient proves no fetch


def test_dispatch_skips_when_no_active_projects():
    loop = make_loop()
    loop._dispatch_available([make_summary(status="stopped")])


def test_try_dispatch_stops_at_max_project_workers():
    loop = make_loop()
    loop.futures[object()] = make_running(intent_id="i001")
    loop.futures[object()] = make_running(intent_id="i002")
    assert loop._try_dispatch_project(make_summary()) is False


def test_try_dispatch_skips_cleanup_pending():
    loop = make_loop()
    loop._cleanup_pending.add("cairn-dispatch-proj_001")
    assert loop._try_dispatch_project(make_summary()) is False


# --- verify dispatch ---------------------------------------------------------

class VerifyStubClient:
    def __init__(self, fact_details, settings, project):
        self.fact_details = fact_details
        self.settings = settings
        self.project = project
        self.submitted = []

    def get_project(self, project_id):
        return self.project

    def get_fact(self, project_id, fact_id):
        return self.fact_details[fact_id]

    def get_settings(self):
        from types import SimpleNamespace

        return SimpleNamespace(verify_max_attempts=self.settings)

    def export_project(self, project_id):
        return "graph: {}"


class _FakeExecutor:
    def submit(self, fn, *args, **kwargs):
        future = Future()
        future.set_result("submitted")
        return future


def _make_verify_loop(client, workers):
    loop = make_loop(workers=workers)
    loop.client = client
    loop.executor = _FakeExecutor()
    return loop


def _fact(fact_id, status="hypothesis", kind="observation"):
    return Fact(
        id=fact_id,
        description=fact_id,
        kind=kind,
        status=status,
        confidence=0.5,
    )


def test_dispatch_verify_submits_replayable_candidate():
    project = make_project(facts=())
    project.facts.extend([_fact("f001"), _fact("f002", status="verified")])
    client = VerifyStubClient(
        {
            "f001": {
                "fact": {"id": "f001", "status": "hypothesis"},
                "evidence": [{"command": "nmap -sV", "match_rule": "contains"}],
                "verifications": [],
            }
        },
        settings=3,
        project=project,
    )
    loop = _make_verify_loop(client, [make_worker("v1", task_types=("verify",))])
    assert loop._dispatch_verify(project) is True
    assert len(loop.futures) == 1
    task = next(iter(loop.futures.values()))
    assert task.task_type == "verify"
    assert task.fact_id == "f001"


def test_dispatch_verify_skips_facts_without_command_evidence():
    project = make_project(facts=())
    project.facts.append(_fact("f001"))
    client = VerifyStubClient(
        {
            "f001": {
                "fact": {"id": "f001", "status": "hypothesis"},
                "evidence": [{"command": None}],
                "verifications": [],
            }
        },
        settings=3,
        project=project,
    )
    loop = _make_verify_loop(client, [make_worker("v1", task_types=("verify",))])
    assert loop._dispatch_verify(project) is False
    assert loop.futures == {}


def test_dispatch_verify_respects_max_attempts():
    project = make_project(facts=())
    project.facts.append(_fact("f001"))
    client = VerifyStubClient(
        {
            "f001": {
                "fact": {"id": "f001", "status": "hypothesis"},
                "evidence": [{"command": "nmap"}],
                "verifications": [{"id": f"ver_00{i}"} for i in range(3)],
            }
        },
        settings=3,
        project=project,
    )
    loop = _make_verify_loop(client, [make_worker("v1", task_types=("verify",))])
    assert loop._dispatch_verify(project) is False


def test_dispatch_verify_needs_worker_with_verify_task_type():
    project = make_project(facts=())
    project.facts.append(_fact("f001"))
    client = VerifyStubClient(
        {
            "f001": {
                "fact": {"id": "f001", "status": "hypothesis"},
                "evidence": [{"command": "nmap"}],
                "verifications": [],
            }
        },
        settings=3,
        project=project,
    )
    loop = _make_verify_loop(client, [make_worker("v1")])  # no verify type
    assert loop._dispatch_verify(project) is False
    assert loop.futures == {}


def test_dispatch_verify_skips_facts_already_running_locally():
    project = make_project(facts=())
    project.facts.append(_fact("f001"))
    client = VerifyStubClient(
        {
            "f001": {
                "fact": {"id": "f001", "status": "hypothesis"},
                "evidence": [{"command": "nmap"}],
                "verifications": [],
            }
        },
        settings=3,
        project=project,
    )
    loop = _make_verify_loop(client, [make_worker("v1", task_types=("verify",))])
    loop.futures[object()] = make_running(task_type="verify", intent_id=None)
    # mark the running task as covering f001
    next(iter(loop.futures.values())).fact_id = "f001"
    assert loop._dispatch_verify(project) is False


def test_try_dispatch_project_prefers_explore_over_verify():
    intent = make_intent()
    project = make_project(intents=(intent,), facts=())
    project.facts.append(_fact("f001"))
    client = VerifyStubClient(
        {
            "f001": {
                "fact": {"id": "f001", "status": "hypothesis"},
                "evidence": [{"command": "nmap"}],
                "verifications": [],
            }
        },
        settings=3,
        project=project,
    )
    loop = _make_verify_loop(
        client,
        [make_worker("v1", task_types=("verify", "explore"))],
    )
    # explore claims via heartbeat; stub it to succeed
    class _Claim:
        ok = True
        status_code = 200

    client.heartbeat = lambda *a, **k: _Claim()
    assert loop._try_dispatch_project(make_summary()) is True
    task = next(iter(loop.futures.values()))
    assert task.task_type == "explore"


def test_scheduler_skips_unapproved_high_risk_intent():
    from cairn.server.models import Intent

    def intent(risk, approved_at=None, approved_by=None):
        return Intent(
            id="iX", **{"from": ["origin"]}, description="d", creator="a",
            created_at="t", risk_class=risk,
            approved_at=approved_at, approved_by=approved_by,
        )

    assert DispatcherLoop._dispatchable_risk(intent("critical"), "p") is False
    assert DispatcherLoop._dispatchable_risk(
        intent("critical", approved_at="t", approved_by="bob"), "p"
    ) is True
    assert DispatcherLoop._dispatchable_risk(
        intent("critical", approved_at="t", approved_by="REJECTED:carol"), "p"
    ) is False
    assert DispatcherLoop._dispatchable_risk(intent("low"), "p") is True
    assert DispatcherLoop._dispatchable_risk(intent("medium"), "p") is True
