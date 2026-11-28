"""Tests for per-host fan-out (4d)."""
from __future__ import annotations

import json

from frex.dispatcher.runtime.engagement import EngagementBudget, EngagementLedger
from frex.dispatcher.runtime.fanout import HostFanout


def _runner(host, ledger):
    ledger.record_tool_call()
    return {
        "status": "completed",
        "facts": [{"description": f"{host} runs nginx", "kind": "service"}],
    }


def test_fanout_runs_every_host_and_posts_facts_to_one_sink():
    posted: list[dict] = []

    def sink(facts):
        posted.extend(facts)
        return len(facts)

    fanout = HostFanout(
        child_runner=_runner,
        facts_sink=sink,
        max_workers=4,
    )
    report = fanout.run(["a.test", "b.test", "c.test"])
    assert report.status == "completed"
    assert set(report.per_host) == {"a.test", "b.test", "c.test"}
    assert report.facts_posted == 3
    assert {f["host_key"] for f in posted} == {"a.test", "b.test", "c.test"}


def test_fanout_child_gets_sub_budget_charged_to_parent():
    seen = []

    def runner(host, ledger):
        ledger.record_tool_call(2)
        seen.append((host, ledger.stop_reason()))
        return {"status": "completed", "facts": []}

    parent = EngagementLedger(EngagementBudget(max_total_tool_calls=6))
    report = HostFanout(
        ledger=parent,
        child_runner=runner,
        per_host=EngagementBudget(max_total_tool_calls=2),
    ).run(["a.test", "b.test"])
    assert report.status == "completed"
    assert all(reason == "tool_calls_exhausted" for _, reason in seen)
    assert parent.snapshot()["tool_calls"] == 4


def test_fanout_stops_starting_hosts_when_parent_cap_trips():
    calls = []

    def runner(host, ledger):
        calls.append(host)
        ledger.record_tool_call(3)
        return {"status": "completed", "facts": []}

    parent = EngagementLedger(EngagementBudget(max_total_tool_calls=3))
    report = HostFanout(
        ledger=parent,
        child_runner=runner,
        per_host=EngagementBudget(max_total_tool_calls=10),
        max_workers=1,
    ).run(["a.test", "b.test", "c.test", "d.test"])
    assert report.status == "partial"
    assert report.stop_reason == "tool_calls_exhausted"
    assert len(calls) == 1
    assert report.remaining_hosts == ["b.test", "c.test", "d.test"]


def test_fanout_isolates_child_exception():
    def runner(host, ledger):
        if host == "bad.test":
            raise RuntimeError("browser crashed")
        return {"status": "completed", "facts": []}

    report = HostFanout(child_runner=runner).run(["bad.test", "good.test"])
    assert report.status == "partial"
    assert report.per_host["bad.test"] == "error"
    assert report.per_host["good.test"] == "completed"


def test_fanout_fact_sink_failure_does_not_kill_run():
    def sink(facts):
        raise RuntimeError("graph down")

    report = HostFanout(child_runner=_runner, facts_sink=sink).run(["a.test"])
    assert report.status == "completed"
    assert report.facts_posted == 0


def test_fanout_cancellation_stops_new_hosts():
    class Cancel:
        is_cancelled = False

    cancel = Cancel()

    def runner(host, ledger):
        cancel.is_cancelled = True
        return {"status": "completed", "facts": []}

    report = HostFanout(
        child_runner=runner, cancellation=cancel, max_workers=1
    ).run(["a.test", "b.test"])
    assert report.status == "partial"
    assert report.stop_reason == "cancelled"
    assert report.remaining_hosts == ["b.test"]


def test_checkpoint_and_resume_cursor(tmp_path):
    fanout = HostFanout(child_runner=_runner)
    report = fanout.run(["a.test", "b.test"])
    path = tmp_path / "fanout.json"
    fanout.checkpoint(path, report)
    cursor = HostFanout.resume_cursor(path)
    assert cursor["done"] == ["a.test", "b.test"]
    assert cursor["remaining"] == []
    data = json.loads(path.read_text())
    assert data["ledger"]["tool_calls"] == 2
