from __future__ import annotations

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
