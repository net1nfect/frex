"""Tests for the engagement budget (4c) and its loop integration."""
from __future__ import annotations

import json
import time

import pytest

from frex.dispatcher.providers.base import Completion, ToolCall, Usage
from frex.dispatcher.runtime.agent_loop import AgentBudget, AgentContext, AgentLoop
from frex.dispatcher.runtime.engagement import EngagementBudget, EngagementLedger


class ScriptedProvider:
    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def complete(self, model, messages, *, tools=None, **kwargs):
        self.calls += 1
        item = self.script.pop(0)
        return item

    def healthcheck(self, model):
        return True


class NoopExecutor:
    def __init__(self):
        self.calls = 0

    def run(self, name, arguments):
        from frex.dispatcher.runtime.tools import ToolResult

        self.calls += 1
        if name == "submit_result":
            return ToolResult(ok=True, output={"result": {"summary": "ok"}}, terminal=True)
        return ToolResult(ok=True, output={"ok": True})


def _tool_completion(name="http_probe", args=None):
    return Completion(
        text="",
        tool_calls=[ToolCall(id="t1", name=name, arguments=json.dumps(args or {"url": "http://lab.test/"}))],
        usage=Usage(tokens_in=10, tokens_out=5),
    )


def _final_completion():
    return Completion(
        text="done",
        tool_calls=[ToolCall(id="t2", name="submit_result", arguments=json.dumps({"summary": "ok"}))],
        usage=Usage(tokens_in=10, tokens_out=5),
    )


# ---------------------------------------------------------------- ledger


def test_ledger_unlimited_never_stops():
    ledger = EngagementLedger(EngagementBudget())
    ledger.record_tool_call(10_000)
    ledger.record_cost(999.0)
    assert ledger.stop_reason() is None


def test_tool_call_cap():
    ledger = EngagementLedger(EngagementBudget(max_total_tool_calls=2))
    ledger.record_tool_call()
    assert ledger.stop_reason() is None
    ledger.record_tool_call()
    assert ledger.stop_reason() == "tool_calls_exhausted"


def test_cost_cap():
    ledger = EngagementLedger(EngagementBudget(max_estimated_cost_usd=0.5))
    ledger.record_cost(0.4)
    assert ledger.stop_reason() is None
    ledger.record_cost(0.1)
    assert ledger.stop_reason() == "cost_exhausted"


def test_wall_clock_cap():
    ledger = EngagementLedger(
        EngagementBudget(max_wall_clock_minutes=0.01), started_at=time.time() - 2
    )
    assert ledger.stop_reason() == "wall_clock_exhausted"


def test_snapshot_reports_remaining():
    ledger = EngagementLedger(EngagementBudget(max_total_tool_calls=5, max_estimated_cost_usd=1.0))
    ledger.record_tool_call(2)
    ledger.record_cost(0.25)
    snap = ledger.snapshot()
    assert snap["tool_calls"] == 2
    assert snap["remaining"]["tool_calls"] == 3
    assert snap["remaining"]["cost_usd"] == pytest.approx(0.75)
    assert snap["stop_reason"] is None


def test_sub_ledger_charges_parent_and_enforces_both():
    parent = EngagementLedger(EngagementBudget(max_total_tool_calls=1))
    child = parent.derive(max_tool_calls=10)
    child.record_tool_call()
    assert child.stop_reason() == "parent_tool_calls_exhausted"
    assert parent.snapshot()["tool_calls"] == 1


def test_sub_ledger_cannot_exceed_parent_collectively():
    parent = EngagementLedger(EngagementBudget(max_total_tool_calls=3))
    a = parent.derive(max_tool_calls=10)
    b = parent.derive(max_tool_calls=10)
    a.record_tool_call(2)
    b.record_tool_call(2)
    assert b.stop_reason() == "parent_tool_calls_exhausted"


def test_checkpoint_round_trip(tmp_path):
    ledger = EngagementLedger(EngagementBudget(max_total_tool_calls=10))
    ledger.record_tool_call(4)
    ledger.record_cost(0.1)
    path = tmp_path / "engagement.json"
    ledger.checkpoint(path)
    restored = EngagementLedger.restore(path, EngagementBudget(max_total_tool_calls=10))
    assert restored.snapshot()["tool_calls"] == 4
    assert restored.snapshot()["remaining"]["tool_calls"] == 6


# ---------------------------------------------------------------- loop


def _context(provider, executor, engagement, **kwargs):
    events = []
    return AgentContext(
        provider=provider,
        model="m",
        executor=executor,
        budget=AgentBudget(max_steps=10),
        on_event=lambda kind, data: events.append((kind, data)),
        engagement=engagement,
        **kwargs,
    ), events


def test_loop_stops_on_tool_call_cap_without_asking_model():
    provider = ScriptedProvider([_tool_completion() for _ in range(10)])
    executor = NoopExecutor()
    ledger = EngagementLedger(EngagementBudget(max_total_tool_calls=2))
    ctx, events = _context(provider, executor, ledger)
    outcome = AgentLoop(ctx).run("go")
    assert outcome.status == "budget_exhausted"
    assert "tool_calls_exhausted" in outcome.error
    assert executor.calls == 2
    assert provider.calls == 2
    stops = [e for e in events if e[0] == "engagement.exhausted"]
    assert stops and stops[0][1]["reason"] == "tool_calls_exhausted"
    assert stops[0][1]["remaining"]["tool_calls"] == 0


def test_loop_stops_on_cost_cap():
    provider = ScriptedProvider([_tool_completion() for _ in range(10)])
    ledger = EngagementLedger(EngagementBudget(max_estimated_cost_usd=0.5))
    ctx, events = _context(
        provider, NoopExecutor(), ledger, cost_estimator=lambda ti, to: 0.3
    )
    outcome = AgentLoop(ctx).run("go")
    assert outcome.status == "budget_exhausted"
    assert "cost_exhausted" in outcome.error
    assert outcome.cost_usd == pytest.approx(0.6)


def test_loop_reports_unexamined_scope_on_stop():
    provider = ScriptedProvider([_tool_completion()])
    ledger = EngagementLedger(EngagementBudget(max_total_tool_calls=1))
    ctx, events = _context(
        provider,
        NoopExecutor(),
        ledger,
        scope_reporter=lambda: "unexamined: db.lab.test",
    )
    AgentLoop(ctx).run("go")
    stop = [e for e in events if e[0] == "engagement.exhausted"][0][1]
    assert stop["unexamined_scope"] == "unexamined: db.lab.test"


def test_loop_without_engagement_unchanged():
    provider = ScriptedProvider([_final_completion()])
    ctx, _ = _context(provider, NoopExecutor(), None)
    outcome = AgentLoop(ctx).run("go")
    assert outcome.status == "completed"
    assert outcome.engagement is None


def test_loop_completed_outcome_carries_snapshot():
    provider = ScriptedProvider([_tool_completion(), _final_completion()])
    ledger = EngagementLedger(EngagementBudget(max_total_tool_calls=5))
    ctx, _ = _context(provider, NoopExecutor(), ledger)
    outcome = AgentLoop(ctx).run("go")
    assert outcome.status == "completed"
    assert outcome.engagement["tool_calls"] == 2  # probe + submit_result
