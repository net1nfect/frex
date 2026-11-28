"""Engagement budget: cost/tool-call/wall-clock caps for one engagement.

AgentBudget caps a single agent loop. An engagement spans many loops
(parent + per-host subagents), so the ledger is shared: every tool call
and every completion charges the same counters. Limits are optional;
``None`` means unlimited.

The ledger is the stop authority. It returns a reason string and the
loop stops — it never trusts the model to decide that the engagement is
over. When a cap trips the outcome carries the remaining budget and the
scope summary so the operator knows what was left unexamined.

Subagents get a derived ledger (``derive``) that enforces both its own
sub-budget and the parent's, and charges the parent as it runs. That is
how fan-out stays inside the engagement cap without a global
coordinator.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class EngagementBudget:
    max_total_tool_calls: int | None = None
    max_wall_clock_minutes: float | None = None
    max_estimated_cost_usd: float | None = None

    def is_unlimited(self) -> bool:
        return (
            self.max_total_tool_calls is None
            and self.max_wall_clock_minutes is None
            and self.max_estimated_cost_usd is None
        )


@dataclass(slots=True)
class _Counters:
    tool_calls: int = 0
    cost_usd: float = 0.0
    started_at: float = field(default_factory=time.time)


class EngagementLedger:
    """Thread-safe counter shared by a parent loop and its subagents."""

    def __init__(
        self,
        budget: EngagementBudget | None = None,
        *,
        parent: "EngagementLedger | None" = None,
        started_at: float | None = None,
        tool_calls: int = 0,
        cost_usd: float = 0.0,
    ):
        self.budget = budget or EngagementBudget()
        self._parent = parent
        self._counters = _Counters(
            tool_calls=tool_calls,
            cost_usd=cost_usd,
            started_at=time.time() if started_at is None else started_at,
        )
        self._lock = threading.Lock()

    # ------------------------------------------------------------ charging

    def record_tool_call(self, count: int = 1) -> None:
        if count <= 0:
            return
        with self._lock:
            self._counters.tool_calls += count
        if self._parent is not None:
            self._parent.record_tool_call(count)

    def record_cost(self, usd: float) -> None:
        if usd <= 0:
            return
        with self._lock:
            self._counters.cost_usd += usd
        if self._parent is not None:
            self._parent.record_cost(usd)

    # ------------------------------------------------------------ limits

    def _own_stop_reason(self) -> str | None:
        b = self.budget
        if b.max_total_tool_calls is not None and self._counters.tool_calls >= b.max_total_tool_calls:
            return "tool_calls_exhausted"
        if b.max_estimated_cost_usd is not None and self._counters.cost_usd >= b.max_estimated_cost_usd:
            return "cost_exhausted"
        if b.max_wall_clock_minutes is not None:
            elapsed_minutes = (time.time() - self._counters.started_at) / 60.0
            if elapsed_minutes >= b.max_wall_clock_minutes:
                return "wall_clock_exhausted"
        return None

    def stop_reason(self) -> str | None:
        """Why the ledger says stop, or None. Parent limit wins first."""
        if self._parent is not None:
            parent_reason = self._parent.stop_reason()
            if parent_reason is not None:
                return f"parent_{parent_reason}"
        return self._own_stop_reason()

    # ------------------------------------------------------------ subagents

    def derive(
        self,
        budget: EngagementBudget | None = None,
        *,
        max_tool_calls: int | None = None,
        max_estimated_cost_usd: float | None = None,
        max_wall_clock_minutes: float | None = None,
    ) -> "EngagementLedger":
        """Create a sub-ledger that charges this one as it runs.

        Per-host sub-budgets are enforced independently; the parent cap
        still applies, so N subagents cannot collectively exceed the
        engagement limit.
        """
        sub_budget = budget or EngagementBudget(
            max_total_tool_calls=max_tool_calls,
            max_estimated_cost_usd=max_estimated_cost_usd,
            max_wall_clock_minutes=max_wall_clock_minutes,
        )
        return EngagementLedger(sub_budget, parent=self)

    # ------------------------------------------------------------ snapshot

    def snapshot(self) -> dict[str, Any]:
        """Serializable cursor — the resume point for this engagement."""
        with self._lock:
            remaining: dict[str, Any] = {}
            b = self.budget
            if b.max_total_tool_calls is not None:
                remaining["tool_calls"] = max(0, b.max_total_tool_calls - self._counters.tool_calls)
            if b.max_estimated_cost_usd is not None:
                remaining["cost_usd"] = round(
                    max(0.0, b.max_estimated_cost_usd - self._counters.cost_usd), 6
                )
            if b.max_wall_clock_minutes is not None:
                elapsed = (time.time() - self._counters.started_at) / 60.0
                remaining["wall_clock_minutes"] = round(
                    max(0.0, b.max_wall_clock_minutes - elapsed), 3
                )
            return {
                "tool_calls": self._counters.tool_calls,
                "cost_usd": round(self._counters.cost_usd, 6),
                "started_at": self._counters.started_at,
                "remaining": remaining,
                "stop_reason": self.stop_reason(),
            }

    def checkpoint(self, path: str | Path) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(json.dumps(self.snapshot(), indent=2))
        tmp.replace(target)

    @classmethod
    def restore(
        cls,
        path: str | Path,
        budget: EngagementBudget | None = None,
    ) -> "EngagementLedger":
        data = json.loads(Path(path).read_text())
        return cls(
            budget,
            started_at=float(data.get("started_at", time.time())),
            tool_calls=int(data.get("tool_calls", 0)),
            cost_usd=float(data.get("cost_usd", 0.0)),
        )
