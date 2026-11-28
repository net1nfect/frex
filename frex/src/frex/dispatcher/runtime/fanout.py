"""Per-host subagent fan-out with sub-budgets.

One host = one child loop. Each child gets a derived engagement ledger
that charges the parent, so per-host depth is bounded while the global
engagement cap still applies. Children return structured facts; the
fanout posts them to ONE central sink (the fact graph), so parallel
work converges on a single source of truth instead of N transcripts.

The fanout is the conductor, not a model: it decides — in code — when
to stop starting new hosts (parent cap), and it reports the hosts it
never reached. A child exception is isolated to that host.
"""
from __future__ import annotations

import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from frex.dispatcher.runtime.engagement import EngagementBudget, EngagementLedger

LOG = logging.getLogger(__name__)


@dataclass(slots=True)
class HostResult:
    host: str
    status: str  # completed | error | budget_exhausted | skipped
    facts: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None


@dataclass(slots=True)
class FanoutReport:
    status: str  # completed | partial
    per_host: dict[str, str] = field(default_factory=dict)
    facts_posted: int = 0
    remaining_hosts: list[str] = field(default_factory=list)
    stop_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "per_host": dict(self.per_host),
            "facts_posted": self.facts_posted,
            "remaining_hosts": list(self.remaining_hosts),
            "stop_reason": self.stop_reason,
        }


class HostFanout:
    """Runs child_runner(host, sub_ledger) for each host, capped."""

    def __init__(
        self,
        *,
        ledger: EngagementLedger | None = None,
        child_runner: Callable[[str, EngagementLedger], dict[str, Any]],
        facts_sink: Callable[[list[dict[str, Any]]], int] | None = None,
        max_workers: int = 4,
        per_host: EngagementBudget | None = None,
        cancellation: Any | None = None,
    ):
        self._ledger = ledger or EngagementLedger(EngagementBudget())
        self._child_runner = child_runner
        self._facts_sink = facts_sink
        self._max_workers = max(1, max_workers)
        self._per_host = per_host or EngagementBudget()
        self._cancellation = cancellation
        self._sink_lock = threading.Lock()

    def _cancelled(self) -> bool:
        return (
            self._cancellation is not None
            and getattr(self._cancellation, "is_cancelled", False)
        )

    def _run_host(self, host: str) -> HostResult:
        sub = self._ledger.derive(self._per_host)
        try:
            payload = self._child_runner(host, sub) or {}
        except Exception as exc:
            LOG.warning("host child failed host=%s: %s", host, exc)
            return HostResult(host=host, status="error", error=str(exc))
        facts = [f for f in payload.get("facts", []) if isinstance(f, dict)]
        # Every fact is stamped with the host it was observed on, unless
        # the child already said where it belongs.
        for fact in facts:
            fact.setdefault("host_key", host)
        status = str(payload.get("status", "completed"))
        return HostResult(host=host, status=status, facts=facts)

    def _post(self, facts: list[dict[str, Any]]) -> int:
        if not facts or self._facts_sink is None:
            return 0
        try:
            with self._sink_lock:
                posted = self._facts_sink(facts)
            return int(posted or 0)
        except Exception:
            LOG.warning("fanout fact sink failed", exc_info=True)
            return 0

    def run(self, hosts: list[str]) -> FanoutReport:
        report = FanoutReport(status="completed")
        pending = [h for h in hosts if h]
        if not pending:
            return report

        stopped = False
        with ThreadPoolExecutor(max_workers=min(self._max_workers, len(pending))) as pool:
            futures: dict[Any, str] = {}
            submitted: list[str] = []
            for host in pending:
                if self._cancelled():
                    stopped = True
                    report.stop_reason = "cancelled"
                    break
                reason = self._ledger.stop_reason()
                if reason is not None:
                    stopped = True
                    report.stop_reason = reason
                    break
                submitted.append(host)
                futures[pool.submit(self._run_host, host)] = host

            for future in as_completed(futures):
                host = futures[future]
                result = future.result()
                report.per_host[host] = result.status
                if result.status == "error":
                    report.status = "partial"
                report.facts_posted += self._post(result.facts)

        reached = set(submitted)
        report.remaining_hosts = [h for h in pending if h not in reached]
        if stopped:
            report.status = "partial"
        return report

    # ------------------------------------------------------------ resume

    def checkpoint(self, path: str | Path, report: FanoutReport) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "report": report.as_dict(),
            "ledger": self._ledger.snapshot(),
        }
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(target)

    @staticmethod
    def resume_cursor(path: str | Path) -> dict[str, Any]:
        """Returns {done: [...], remaining: [...]} for a resumable run."""
        data = json.loads(Path(path).read_text())
        report = data.get("report", {})
        done = sorted(report.get("per_host", {}).keys())
        return {
            "done": done,
            "remaining": list(report.get("remaining_hosts", [])),
            "ledger": data.get("ledger", {}),
        }
