"""Tests for the permission gate (risk tiers, approval flow, audit)."""
from __future__ import annotations

import json

import pytest

from frex.dispatcher.runtime.permissions import (
    ALLOW,
    ASK,
    DENY,
    ApprovalResponse,
    PermissionGate,
    cli_approval_handler,
)
from frex.dispatcher.runtime.tools import TOOLS_SPEC


class RecordingSink:
    def __init__(self):
        self.entries = []

    def __call__(self, entry):
        self.entries.append(entry)


def _gate(handler=None, sink=None, overrides=None):
    return PermissionGate(
        tier_overrides=overrides,
        approval_handler=handler,
        audit_sink=sink or RecordingSink(),
    )


def _handler(approves=True, approver="op-1"):
    return lambda request: ApprovalResponse(
        approved=approves, approver_id=approver, rationale="test"
    )


# ------------------------------------------------------------ tiers


def test_tier0_tools_auto_allow():
    gate = _gate()
    for tool in ("http_probe", "port_scan", "screenshot", "subdomain_enum", "tech_fingerprint"):
        assert gate.check(tool, "http://10.0.0.5").decision == ALLOW


def test_tier1_tools_auto_allow_and_logged():
    sink = RecordingSink()
    gate = _gate(sink=sink)
    for tool in ("content_discovery", "crawl_site"):
        assert gate.check(tool, "http://10.0.0.5").decision == ALLOW
    assert len(sink.entries) == 2
    assert sink.entries[0]["decision"] == ALLOW
    assert sink.entries[0]["timestamp"].endswith("Z")


def test_nuclei_scan_tier_depends_on_severity():
    gate = _gate()
    assert gate.check("nuclei_scan", "http://10.0.0.5", severity="info,low,medium").decision == ALLOW
    decision = gate.check("nuclei_scan", "http://10.0.0.5", severity="critical,high")
    assert decision.decision in (ASK, DENY)


def test_tier2_ask_without_handler_is_deny():
    # Fail-closed: no approval channel configured → DENY, never auto-run.
    gate = _gate(handler=None)
    assert gate.check("service_check", "10.0.0.5:6379").decision == DENY


def test_tier2_ask_then_session_cache():
    gate = _gate(handler=_handler())
    decision = gate.check("service_check", "10.0.0.5:6379")
    assert decision.decision == ASK
    approved, approver = gate.resolve_ask("service_check", "10.0.0.5:6379", decision.tier)
    assert approved is True
    assert approver == "op-1"
    # Same tool+target is now pre-approved for the session.
    assert gate.check("service_check", "10.0.0.5:6379").decision == ALLOW
    # A different target still needs approval.
    assert gate.check("service_check", "10.0.0.9:6379").decision == ASK


def test_tier3_never_cached_even_after_approval_same_target():
    gate = _gate(handler=_handler())
    decision = gate.check("default_creds_check", "http://10.0.0.5/login")
    assert decision.decision == ASK
    approved, _ = gate.resolve_ask("default_creds_check", "http://10.0.0.5/login", decision.tier)
    assert approved is True
    # Second call on the SAME target: must ask again — no caching.
    assert gate.check("default_creds_check", "http://10.0.0.5/login").decision == ASK
    # The session-approval cache stays empty for tier 3.
    assert gate._session_approvals == {}


def test_tier3_denial_is_logged_with_approver():
    sink = RecordingSink()
    gate = _gate(handler=_handler(approves=False), sink=sink)
    decision = gate.check("default_creds_check", "http://10.0.0.5/login")
    gate.resolve_ask("default_creds_check", "http://10.0.0.5/login", decision.tier)
    denial = [e for e in sink.entries if e["decision"] == DENY]
    assert denial
    assert denial[-1]["approver_id"] == "op-1"
    assert denial[-1]["tool"] == "default_creds_check"


def test_unknown_tool_defaults_to_tier3():
    gate = _gate()
    assert gate.tier_for("brand_new_tool") == 3
    assert gate.check("brand_new_tool", "x").decision == DENY


def test_config_cannot_lower_tier3():
    gate = _gate(overrides={"default_creds_check": 0})
    assert gate.tier_for("default_creds_check") == 3


def test_config_can_raise_tier():
    gate = _gate(overrides={"http_probe": 2})
    assert gate.check("http_probe", "http://10.0.0.5").decision == DENY  # no channel
    gate2 = _gate(handler=_handler(), overrides={"http_probe": 2})
    assert gate2.check("http_probe", "http://10.0.0.5").decision == ASK


def test_exempt_tools_bypass_gate():
    gate = _gate()
    for tool in ("submit_facts", "submit_result", "submit_action_log"):
        decision = gate.check(tool, "")
        assert decision.decision == ALLOW
        assert decision.tier == 0


def test_audit_captures_allow_decisions_too():
    sink = RecordingSink()
    gate = _gate(sink=sink)
    gate.check("http_probe", "http://10.0.0.5")
    entry = sink.entries[0]
    assert entry["tool"] == "http_probe"
    assert entry["target"] == "http://10.0.0.5"
    assert entry["decision"] == ALLOW
    assert "approver_id" in entry


# ------------------------------------------------------------ registry flags


def test_mutating_tools_are_flagged_in_registry():
    by_name = {tool["name"]: tool for tool in TOOLS_SPEC}
    assert "is_mutating" in by_name["http_probe"]
    assert by_name["http_probe"]["is_mutating"] is False
    # Any tool flagged mutating must default to tier >= 2 so it never
    # auto-runs without approval.
    from frex.dispatcher.runtime.permissions import DEFAULT_TOOL_TIERS

    for tool in TOOLS_SPEC:
        if tool.get("is_mutating"):
            assert DEFAULT_TOOL_TIERS.get(tool["name"], 3) >= 2


# ------------------------------------------------------------ cli handler


def test_cli_approval_handler_approves_on_y(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _: "y")
    response = cli_approval_handler(
        __import__(
            "frex.dispatcher.runtime.permissions", fromlist=["ApprovalRequest"]
        ).ApprovalRequest(tool_name="x", target="y", tier=3, reason="r")
    )
    assert response.approved is True
    assert response.approver_id == "cli-operator"


def test_cli_approval_handler_rejects_default(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _: "")
    import frex.dispatcher.runtime.permissions as perm

    response = perm.cli_approval_handler(
        perm.ApprovalRequest(tool_name="x", target="y", tier=3, reason="r")
    )
    assert response.approved is False


# ------------------------------------------------------------ loop integration


def test_agent_loop_denies_tool_when_gate_denies():
    from frex.dispatcher.runtime.agent_loop import AgentBudget, AgentContext, AgentLoop
    from frex.dispatcher.runtime.tools import ToolExecutor
    from test_agent_loop import (
        FakeContainers,
        ScriptedProvider,
        _submit_completion,
        _tool_completion,
    )

    gate = _gate()  # no handler → tier2/3 deny
    provider = ScriptedProvider(
        [
            _tool_completion("t1", "service_check", {"target": "10.0.0.5:6379"}),
            _submit_completion(),
        ]
    )
    context = AgentContext(
        provider=provider,
        model="m",
        executor=ToolExecutor(FakeContainers(), "ctr"),
        budget=AgentBudget(max_steps=5),
        permission_gate=gate,
    )
    outcome = AgentLoop(context).run("go")
    assert outcome.status == "completed"
    # The denial fed back to the model as a tool message.
    tool_messages = [
        m for m in provider.calls[1] if m.get("role") == "tool"
    ]
    denials = [
        json.loads(m["content"])
        for m in tool_messages
        if "permission_denied" in m.get("content", "")
    ]
    assert denials and denials[0]["error"] == "permission_denied"
