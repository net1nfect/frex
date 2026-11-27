"""Permission gate: per-action execution approval, separate from scope.

Two orthogonal questions:
- scope_guard: "may this tool touch this target at all?" (always in code)
- permission_gate: "may this specific action run *now*?" (risk tiers)

Risk tiers (config-driven, not hardcoded — deployments override via
``tool_risk_tiers``):
- tier 0: read-only, auto-run.
- tier 1: auto-run, rate-limited by the tool itself, logged.
- tier 2: approval once per (tool, target) session — the operator may
  pre-approve for the whole session.
- tier 3: approval EVERY time — never cached, even for the same target
  the operator approved before.

Every decision (ALLOW included) goes to the append-only audit sink with
who decided and when. Fail-closed: ASK without an approval handler is a
DENY, never a silent auto-run.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

ALLOW, DENY, ASK = "allow", "deny", "ask"

# Default tiers for the built-in toolset. Deployments override per tool
# via config (tool_risk_tiers); unknown tools default to tier 3 — a new
# tool must be explicitly classified before it auto-runs.
DEFAULT_TOOL_TIERS: dict[str, int] = {
    # tier 0 — read-only
    "http_probe": 0,
    "port_scan": 0,
    "screenshot": 0,
    "subdomain_enum": 0,
    "tech_fingerprint": 0,
    "liveness_check": 0,
    "search_sploit": 0,
    "get_fact_detail": 0,
    # tier 1 — auto + rate-limited + logged
    "content_discovery": 1,
    "crawl_site": 1,
    # tier 2 — session approval
    "service_check": 2,
    # Submitting credentials to a (scope-checked) login endpoint is a
    # state-changing action: approval once per session/target.
    "session_authenticate": 2,
    # tier 3 — explicit approval every time
    "default_creds_check": 3,
    # Generic container tools: the scope guard governs their targets;
    # run_command can mutate the container (not the target), so tier 1.
    "run_command": 1,
    "read_file": 0,
    "write_file": 1,
    "http_request": 1,
}

# Internal bookkeeping tools never hit the gate.
GATE_EXEMPT = {
    "submit_facts",
    "submit_result",
    "submit_action_log",
    "interactsh_generate",
    "interactsh_poll",
    # Meta tool: its STEPS are individually gated inside SkillRunner,
    # which is the same gate a manual call would hit.
    "run_skill",
}


@dataclass(slots=True)
class ApprovalRequest:
    tool_name: str
    target: str
    tier: int
    reason: str


@dataclass(slots=True)
class ApprovalResponse:
    approved: bool
    approver_id: str = "operator"
    rationale: str = ""


@dataclass(slots=True)
class GateDecision:
    decision: str  # ALLOW | DENY | ASK
    tier: int
    reason: str


class PermissionGate:
    def __init__(
        self,
        *,
        tier_overrides: dict[str, int] | None = None,
        # Receives an ApprovalRequest, returns ApprovalResponse. The CLI
        # prompt implementation lives in cli_approval_handler; server
        # deployments inject a channel-backed handler.
        approval_handler: Callable[[ApprovalRequest], ApprovalResponse] | None = None,
        # Append-only audit sink: one dict per decision.
        audit_sink: Callable[[dict[str, Any]], None] | None = None,
        operator_id: str = "agent",
    ):
        self._tiers = dict(DEFAULT_TOOL_TIERS)
        if tier_overrides:
            # Tier can only be raised by config, never lowered below the
            # default for tier-3 tools — a misconfiguration must not
            # silently auto-run exploit-class actions.
            for tool, tier in tier_overrides.items():
                base = DEFAULT_TOOL_TIERS.get(tool, 3)
                self._tiers[tool] = max(tier, 0) if base < 3 else 3
        self._approval_handler = approval_handler
        self._audit_sink = audit_sink or (lambda entry: None)
        self._operator_id = operator_id
        # Session approvals for tier 2: (tool, target) -> approver id.
        self._session_approvals: dict[tuple[str, str], str] = {}
        # Tier 3 is deliberately NOT stored here — no cache, ever.

    def tier_for(self, tool_name: str) -> int:
        if tool_name in GATE_EXEMPT:
            return 0
        return self._tiers.get(tool_name, 3)

    def check(self, tool_name: str, target: str, *, severity: str | None = None) -> GateDecision:
        if tool_name in GATE_EXEMPT:
            return GateDecision(ALLOW, 0, "internal bookkeeping tool")
        tier = self.tier_for(tool_name)
        # nuclei_scan escalates with severity: info/low/medium stays
        # tier 1, high/critical needs session approval.
        if tool_name == "nuclei_scan":
            tier = 1
            if severity and any(s in severity.lower() for s in ("high", "critical")):
                tier = 2

        if tier <= 1:
            decision = GateDecision(ALLOW, tier, "read-only / rate-limited auto-run")
        elif tier == 2:
            key = (tool_name, target)
            if key in self._session_approvals:
                decision = GateDecision(ALLOW, tier, f"pre-approved for session by {self._session_approvals[key]}")
            elif self._approval_handler is not None:
                decision = GateDecision(ASK, tier, "session approval required")
            else:
                decision = GateDecision(DENY, tier, "approval required but no approval channel configured")
        else:
            decision = GateDecision(ASK, tier, "explicit per-action approval required (never cached)")
            if self._approval_handler is None:
                decision = GateDecision(DENY, tier, "tier 3 requires approval; no approval channel configured")
        self._audit(tool_name, target, decision, approver_id=self._operator_id)
        return decision

    def resolve_ask(
        self, tool_name: str, target: str, tier: int, rationale: str = ""
    ) -> tuple[bool, str]:
        """Run the approval flow for an ASK decision.

        Returns (approved, approver_id). Tier 2 approvals are cached for
        the session; tier 3 approvals apply to this call only and are
        never stored.
        """
        if self._approval_handler is None:
            self._audit(tool_name, target, GateDecision(DENY, tier, "no approval channel"), approver_id="none")
            return False, "none"
        request = ApprovalRequest(tool_name=tool_name, target=target, tier=tier, reason=rationale)
        try:
            response = self._approval_handler(request)
        except Exception as exc:  # channel failure fails closed
            self._audit(tool_name, target, GateDecision(DENY, tier, f"approval channel error: {exc}"), approver_id="none")
            return False, "none"
        self._audit(
            tool_name,
            target,
            GateDecision(ALLOW if response.approved else DENY, tier, response.rationale or ("operator approved" if response.approved else "operator denied")),
            approver_id=response.approver_id,
        )
        if response.approved and tier == 2:
            self._session_approvals[(tool_name, target)] = response.approver_id
        return response.approved, response.approver_id

    def _audit(self, tool_name: str, target: str, decision: GateDecision, *, approver_id: str) -> None:
        self._audit_sink(
            {
                "tool": tool_name,
                "target": target,
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "tier": decision.tier,
                "decision": decision.decision,
                "approver_id": approver_id,
                "reason": decision.reason,
            }
        )


def cli_approval_handler(request: ApprovalRequest) -> ApprovalResponse:
    """Fallback approval channel: interactive terminal prompt."""
    print(
        f"\n[permission] tier {request.tier} action requires approval:\n"
        f"  tool:   {request.tool_name}\n"
        f"  target: {request.target}\n"
        f"  reason: {request.reason}"
    )
    answer = input("approve? [y/N]: ").strip().lower()
    return ApprovalResponse(approved=answer == "y", approver_id="cli-operator")
