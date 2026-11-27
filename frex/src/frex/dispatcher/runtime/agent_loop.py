"""Native agent loop: model <-> tools until the model submits a result.

Budgets (max_steps, max_tokens, max_usd, max_wall_seconds) exist so a
confused agent stops burning money instead of looping forever. Steering
hints are pulled between steps so an operator can redirect a running
task without cancelling it.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from frex.dispatcher.providers.base import Completion, Provider, ProviderError
from frex.dispatcher.runtime.cancellation import TaskCancellation
from frex.dispatcher.runtime.tools import TOOLS_SPEC, ToolExecutor, ToolResult

LOG = logging.getLogger(__name__)


@dataclass(slots=True)
class AgentBudget:
    max_steps: int = 30
    max_tokens: int = 200_000
    max_usd: float = 5.0
    max_wall_seconds: int = 1800


@dataclass(slots=True)
class AgentOutcome:
    status: str  # completed | failed | cancelled | budget_exhausted | provider_error
    result: dict[str, Any] | None = None
    error: str | None = None
    model: str | None = None  # provider model id actually used last
    steps: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    tool_calls: int = 0
    wall_ms: int = 0


@dataclass(slots=True)
class AgentContext:
    provider: Provider
    model: str
    executor: ToolExecutor
    cancellation: TaskCancellation | None = None
    budget: AgentBudget = field(default_factory=AgentBudget)
    # Returns unconsumed steering hints (e.g. from the server's hint
    # queue); the loop injects them as user messages between steps.
    hint_fetcher: Callable[[], list[str]] = lambda: []
    # Per-step cost estimator: (tokens_in, tokens_out) -> usd.
    cost_estimator: Callable[[int, int], float] = lambda ti, to: 0.0
    on_event: Callable[[str, dict[str, Any]], None] = lambda kind, data: None
    # Generation parameters from ModelConfig. None means "let the
    # provider decide"; the native task passes the configured values.
    max_tokens: int | None = None
    temperature: float | None = None
    # stream_tokens: stream completions and emit token.delta events as
    # they arrive. The provider adapter must support stream=True.
    stream_tokens: bool = False
    # Mid-run failover: called after a ProviderError; returns
    # (provider, model_id) for the next model in the routing chain, or
    # None when no healthy fallback exists. Without it a provider error
    # ends the loop.
    model_failover: Callable[[], tuple[Provider, str] | None] | None = None
    # Tool spec offered to the model. Defaults to the full TOOLS_SPEC;
    # deployments with a restricted tool surface pass a filtered spec
    # so the model never sees disabled tools.
    tools_spec: list[dict[str, Any]] | None = None
    # --- Context layering -------------------------------------------------
    # Fixed system prompt (role/scope/stop conditions); prepended once.
    system_prompt: str | None = None
    # State-summary layer: returns a condensed fact-graph view,
    # regenerated every state_summary_interval turns (not accumulated).
    state_summary_provider: Callable[[], str] | None = None
    state_summary_interval: int = 5
    # Sliding window: only the last N tool messages stay raw; older ones
    # collapse to one line. Raw output is disposable — the fact graph is
    # the source of truth and get_fact_detail retrieves details.
    recent_window: int = 6
    # --- Reasoning effort mapping -----------------------------------------
    # Maps step number -> provider reasoning effort. Deterministic
    # routing steps get low; triage/severity steps get medium/high.
    # None entries omit the parameter entirely. Providers that do not
    # support reasoning_effort degrade gracefully (TypeError catch).
    effort_schedule: Callable[[int], str | None] | None = None


class AgentLoop:
    def __init__(self, context: AgentContext):
        self.ctx = context

    def _cancelled(self) -> bool:
        return self.ctx.cancellation is not None and self.ctx.cancellation.is_cancelled

    def run(self, task_prompt: str) -> AgentOutcome:
        started = time.perf_counter()
        messages: list[dict[str, Any]] = []
        if self.ctx.system_prompt:
            # Layer 1: fixed, cacheable between turns.
            messages.append({"role": "system", "content": self.ctx.system_prompt})
        messages.append({"role": "user", "content": task_prompt})
        outcome = AgentOutcome(status="failed")

        def finish(status: str, **kwargs: Any) -> AgentOutcome:
            outcome.status = status
            outcome.model = self.ctx.model
            outcome.steps = step_count
            outcome.wall_ms = int((time.perf_counter() - started) * 1000)
            for key, value in kwargs.items():
                setattr(outcome, key, value)
            outcome.tokens_in = tokens_in
            outcome.tokens_out = tokens_out
            outcome.cost_usd = cost
            outcome.tool_calls = tool_calls
            self.ctx.on_event("task.finished", {"status": status})
            return outcome

        tokens_in = tokens_out = cost = tool_calls = step_count = 0
        self.ctx.on_event("task.started", {"model": self.ctx.model})

        for step in range(self.ctx.budget.max_steps):
            step_count = step + 1
            if self._cancelled():
                return finish("cancelled")
            if cost >= self.ctx.budget.max_usd:
                return finish("budget_exhausted", error="cost budget reached")
            if tokens_in + tokens_out >= self.ctx.budget.max_tokens:
                return finish("budget_exhausted", error="token budget reached")

            for hint in self.ctx.hint_fetcher():
                messages.append({"role": "user", "content": f"[steering] {hint}"})
                self.ctx.on_event("steering.received", {"hint": hint})

            # Layer 2: regenerated state summary every N turns — a
            # condensed fact-graph view, not an accumulating dump.
            if (
                self.ctx.state_summary_provider is not None
                and step_count > 1
                and step_count % self.ctx.state_summary_interval == 0
            ):
                messages.append(
                    {
                        "role": "user",
                        "content": "[state] " + self.ctx.state_summary_provider(),
                    }
                )

            # Only send generation params that are configured, so an
            # adapter with the older signature keeps working when the
            # caller did not set them.
            generation: dict[str, Any] = {}
            if self.ctx.max_tokens is not None:
                generation["max_tokens"] = self.ctx.max_tokens
            if self.ctx.temperature is not None:
                generation["temperature"] = self.ctx.temperature
            if self.ctx.effort_schedule is not None:
                effort = self.ctx.effort_schedule(step_count)
                if effort is not None:
                    generation["reasoning_effort"] = effort

            try:
                if self.ctx.stream_tokens:
                    completion: Completion = self.ctx.provider.complete(
                        self.ctx.model,
                        messages,
                        tools=self.ctx.tools_spec or TOOLS_SPEC,
                        stream=True,
                        on_token=lambda delta: self.ctx.on_event(
                            "token.delta", {"text": delta}
                        ),
                        **generation,
                    )
                else:
                    completion = self.ctx.provider.complete(
                        self.ctx.model,
                        messages,
                        tools=self.ctx.tools_spec or TOOLS_SPEC,
                        **generation,
                    )
            except TypeError as exc:
                # Covers adapters that predate stream=; degrade to
                # non-streaming rather than failing the task.
                if "stream" not in str(exc):
                    raise
                self.ctx.stream_tokens = False
                completion = self.ctx.provider.complete(
                    self.ctx.model,
                    messages,
                    tools=self.ctx.tools_spec or TOOLS_SPEC,
                    **generation,
                )
            except ProviderError as exc:
                self.ctx.on_event("error", {"message": str(exc)})
                if self.ctx.model_failover is not None:
                    fallback = self.ctx.model_failover()
                    if fallback is not None:
                        self.ctx.provider, self.ctx.model = fallback
                        self.ctx.on_event(
                            "model.failover", {"model": self.ctx.model}
                        )
                        continue
                return finish("provider_error", error=str(exc))

            tokens_in += completion.usage.tokens_in
            tokens_out += completion.usage.tokens_out
            cost += self.ctx.cost_estimator(
                completion.usage.tokens_in, completion.usage.tokens_out
            )

            if completion.tool_calls:
                # OpenAI wire format: assistant tool calls are
                # {id, type: function, function: {name, arguments}} and
                # every message content is a string. Strict upstreams
                # (including OpenAI-compatible gateways) reject the
                # flat {id, name, arguments} shape and null content.
                messages.append(
                    {
                        "role": "assistant",
                        "content": completion.text or "",
                        "tool_calls": [
                            {
                                "id": c.id,
                                "type": "function",
                                "function": {
                                    "name": c.name,
                                    "arguments": c.arguments,
                                },
                            }
                            for c in completion.tool_calls
                        ],
                    }
                )
                for call in completion.tool_calls:
                    if self._cancelled():
                        return finish("cancelled")
                    self.ctx.on_event(
                        "tool.started", {"tool": call.name, "id": call.id}
                    )
                    result: ToolResult = self.ctx.executor.run(
                        call.name, call.arguments
                    )
                    tool_calls += 1
                    self.ctx.on_event("tool.output", {"tool": call.name, "ok": result.ok})
                    if result.output.get("error") == "scope":
                        # Rejected actions are part of the audit trail:
                        # what was attempted, against what, and when.
                        self.ctx.on_event(
                            "error",
                            {
                                "message": f"scope rejected {call.name}",
                                "tool": call.name,
                                "arguments": call.arguments[:500],
                            },
                        )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "content": json.dumps(result.output, ensure_ascii=False),
                        }
                    )
                    self._window_messages(messages)
                    if result.terminal:
                        return finish(
                            "completed", result=result.output.get("result")
                        )
            elif completion.text is not None:
                # No tool calls: the model answered in prose instead of
                # submitting. One nudge to use submit_result, then stop.
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Respond by calling the submit_result tool with your "
                            "final JSON result. Plain prose is not accepted."
                        ),
                    }
                )
            else:
                return finish("failed", error="model returned empty response")

        return finish("budget_exhausted", error="max_steps reached")

    def _window_messages(self, messages: list[dict[str, Any]]) -> None:
        """Collapse older raw tool outputs to one-line summaries.

        Keeps the last recent_window tool messages verbatim; each older
        one becomes '[summary] tool <name> ok/error' — the fact graph
        holds the substance, get_fact_detail retrieves it on demand.
        """
        window = self.ctx.recent_window
        if window <= 0:
            return
        tool_indices = [
            i for i, m in enumerate(messages)
            if m.get("role") == "tool" and not str(m.get("content", "")).startswith("[summary]")
        ]
        for i in tool_indices[:-window]:
            content = messages[i].get("content", "")
            try:
                output = json.loads(content)
                name = output.get("tool") or output.get("error") or "result"
                ok = "ok" if output.get("ok", True) else "error"
            except ValueError:
                name, ok = "result", "?"
            messages[i]["content"] = f"[summary] tool {name} {ok}"
