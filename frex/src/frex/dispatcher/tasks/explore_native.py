"""Native explore task: the agent loop drives the model directly.

This is the runtime the CLI bridge pointed at: instead of shelling out
to a coding agent inside the container, the dispatcher talks to the
model API itself and lets the model use tools in the container. The
CLI bridge remains the default; `tasks.explore.native: true` plus a
providers/models/routing block switches a deployment over.
"""
from __future__ import annotations

import logging
import time

from frex.dispatcher.config import DispatchConfig, WorkerConfig
from frex.dispatcher.protocol.client import FrexClient
from frex.dispatcher.runtime.agent_loop import AgentBudget, AgentContext, AgentLoop
from frex.dispatcher.runtime.containers import ContainerManager
from frex.dispatcher.runtime.cancellation import TaskCancellation
from frex.dispatcher.runtime.heartbeat import HeartbeatLease
from frex.dispatcher.runtime.event_publisher import EventPublisher
from frex.dispatcher.runtime.scope import Scope
from frex.dispatcher.runtime.tools import TOOLS_SPEC, ToolExecutor
from frex.dispatcher.tasks.common import RunRecorder, best_effort_release, cancel_reason
from frex.dispatcher.workers.registry import get_driver
from frex.server.models import Intent

LOG = logging.getLogger(__name__)


def run_native_explore_task(
    config: DispatchConfig,
    client: FrexClient,
    container_manager: ContainerManager,
    registry,  # ProviderRegistry; typed loosely to keep tests light
    project,  # ProjectDetail
    intent: Intent,
    worker: WorkerConfig,
    cancellation: TaskCancellation,
) -> str:
    """Run one explore intent through the agent loop.

    Returns: success | cancelled | failed | unhealthy | budget_exhausted
    """
    driver = get_driver(worker.type)
    explore_config = config.tasks.explore
    project_id = project.project.id
    started = time.perf_counter()

    try:
        model_config = registry.resolve("explore")
    except Exception as exc:
        LOG.warning("native explore has no model project=%s error=%s", project_id, exc)
        return "failed"

    provider = registry.get_provider(model_config.name)
    recorder = RunRecorder(
        client,
        project_id,
        "explore",
        worker,
        model=model_config.model,
        intent_id=intent.id,
    )
    publisher = EventPublisher(client, project_id, "explore")
    lease = HeartbeatLease.for_intent(
        client, project_id, intent.id, worker.name, config.runtime.interval
    )
    lease.start()
    try:
        if cancellation.is_cancelled:
            best_effort_release(client, project_id, intent.id, worker.name)
            return "cancelled"
        container_name = container_manager.ensure_running(project_id)
        healthcheck = recorder.run_healthcheck(
            container_manager,
            container_name,
            worker,
            driver.build_healthcheck(worker),
            timeout_seconds=config.runtime.healthcheck_timeout,
            lease=lease,
            cancellation=cancellation,
        )
        if cancel_reason(healthcheck.result, cancellation) is not None:
            best_effort_release(client, project_id, intent.id, worker.name)
            return "cancelled"
        if lease.failure is not None:
            LOG.warning(
                "heartbeat lost during native explore healthcheck project=%s intent=%s",
                project_id,
                intent.id,
            )
            best_effort_release(client, project_id, intent.id, worker.name)
            return "failed"
        if healthcheck.result.returncode != 0:
            LOG.warning(
                "worker unhealthy during native explore project=%s intent=%s",
                project_id,
                intent.id,
            )
            best_effort_release(client, project_id, intent.id, worker.name)
            return "unhealthy"

        def cost_estimator(tokens_in: int, tokens_out: int) -> float:
            return (
                tokens_in * model_config.input_cost_per_mtok / 1_000_000
                + tokens_out * model_config.output_cost_per_mtok / 1_000_000
            )

        budget = AgentBudget(
            max_wall_seconds=explore_config.timeout,
        )
        # Scope guard: out-of-scope calls are rejected back to the model,
        # not executed. No scope configured means unrestricted.
        scope = Scope.from_config(getattr(project.project, "scope", None))
        enabled = getattr(explore_config, "tools_enabled", None)
        enabled_set = set(enabled) if enabled else None

        def record_agent_facts(facts: list[dict[str, Any]]) -> int:
            """submit_facts sink: post agent findings into the graph the
            same way ingestors do — born verified from tool evidence,
            attached to one intent anchored on origin. A host_key
            correlation key travels in the fact subject so the graph can
            group multiple observations of the same service."""
            from frex.dispatcher.ingest.base import IngestedFact, post_facts

            ingested = [
                IngestedFact(
                    description=str(fact["description"]),
                    kind=str(fact.get("kind", "observation")),
                    match_value=str(fact["description"]),
                    match_rule="contains",
                    subject={"host_key": fact["host_key"]} if fact.get("host_key") else {},
                )
                for fact in facts
            ]
            posted, _failed = post_facts(client, project_id, ingested)
            return posted

        def make_interactsh():
            from frex.dispatcher.runtime.tools import InteractshSession

            return InteractshSession()

        def record_action_log(entry: dict[str, Any]) -> int:
            """submit_action_log sink: append-only audit entries in the
            fact graph (kind=action_log, tool/target/timestamp in the
            subject). Facts are immutable — audit entries are never
            edited, only superseded, which matches write-once intent."""
            from frex.dispatcher.ingest.base import IngestedFact, post_facts

            ingested = IngestedFact(
                description=entry["description"],
                kind="action_log",
                subject={
                    "tool_name": entry["tool_name"],
                    "target": entry["target"],
                    "timestamp": entry["timestamp"],
                },
                match_value=entry["description"],
                match_rule="contains",
            )
            posted, _failed = post_facts(client, project_id, [ingested])
            return posted

        def fact_lookup(fact_id: str) -> dict[str, Any] | None:
            try:
                return client.get_fact(project_id, fact_id)
            except Exception:
                return None

        executor = ToolExecutor(
            container_manager,
            container_name,
            command_timeout_seconds=explore_config.timeout,
            scope_checker=scope.checker() if scope else None,
            enabled_tools=enabled_set,
            facts_sink=record_agent_facts,
            host_filter=scope.host_filter() if scope else None,
            interactsh_factory=make_interactsh,
            action_log_sink=record_action_log,
            facts_lookup=fact_lookup,
        )
        tools_spec = TOOLS_SPEC
        if enabled_set is not None:
            tools_spec = [tool for tool in TOOLS_SPEC if tool["name"] in enabled_set]
        # Context layering: fixed system prompt + regenerated state
        # summary + on-demand fact retrieval. Raw tool output stays in
        # a sliding window; the fact graph is the source of truth.
        from frex.dispatcher.runtime.prompts import (
            build_system_prompt,
            condense_facts,
        )

        system_prompt = build_system_prompt(
            role_text=(
                "You are a recon agent for an authorized security "
                "assessment. Investigate the intent using the tools, "
                "record concrete discoveries, and report when done."
            ),
            scope=scope,
            stop_conditions={"max_steps": budget.max_steps},
        )

        def state_summary() -> str:
            try:
                detail = client.get_project(project_id)
                return condense_facts(
                    [fact.model_dump() for fact in detail.facts]
                )
            except Exception:
                return "state summary unavailable"

        def effort_for_step(step: int) -> str | None:
            # Deterministic routing (first probe picks) is low; triage
            # and severity reasoning get medium. Severity assessment on
            # ambiguous evidence is where high would go — the model
            # escalates by pausing on ambiguous output, not by a higher
            # default.
            if step <= 1:
                return "low"
            return "medium"

        context = AgentContext(
            provider=provider,
            model=model_config.model,
            executor=executor,
            cancellation=cancellation,
            budget=budget,
            hint_fetcher=lambda: client.consume_hint_queue(project_id),
            cost_estimator=cost_estimator,
            on_event=publisher.emit,
            max_tokens=model_config.max_tokens,
            temperature=model_config.temperature,
            stream_tokens=True,
            tools_spec=tools_spec,
            model_failover=registry.make_failover("explore", model_config.name)
            if hasattr(registry, "make_failover")
            else None,
            system_prompt=system_prompt,
            state_summary_provider=state_summary,
            state_summary_interval=5,
            recent_window=6,
            effort_schedule=effort_for_step,
        )
        outcome = AgentLoop(context).run(
            f"Intent: {intent.description}\n\nInvestigate and report via "
            "submit_result. Use the tools to run commands in the container."
        )

        # One run row summarises the whole loop, carrying the token and
        # cost usage so the usage rollup reflects native tasks too.
        usage_run = RunRecorder(
            client,
            project_id,
            "explore",
            worker,
            model=outcome.model or model_config.model,
            intent_id=intent.id,
            command=f"agent-loop({model_config.name})",
        )
        run_id = usage_run.record(
            _outcome_as_result(outcome),
            duration_ms=outcome.wall_ms,
            phase="execute",
            tokens_in=outcome.tokens_in,
            tokens_out=outcome.tokens_out,
            cost_usd=round(outcome.cost_usd, 6),
        )

        if outcome.status == "cancelled":
            best_effort_release(client, project_id, intent.id, worker.name)
            return "cancelled"
        if outcome.status != "completed" or outcome.result is None:
            LOG.warning(
                "native explore failed project=%s intent=%s status=%s error=%s",
                project_id,
                intent.id,
                outcome.status,
                outcome.error,
            )
            best_effort_release(client, project_id, intent.id, worker.name)
            return "failed"

        result_payload = outcome.result
        description = str(
            result_payload.get("description")
            or result_payload.get("summary")
            or json_compact(result_payload)
        )
        response = client.conclude(
            project_id,
            intent.id,
            worker.name,
            description,
            run_id=run_id,
        )
        if not response.ok:
            LOG.warning(
                "native explore conclude failed project=%s intent=%s status=%s",
                project_id,
                intent.id,
                response.status_code,
            )
            best_effort_release(client, project_id, intent.id, worker.name)
            return "failed"
        LOG.info(
            "native explore done project=%s intent=%s model=%s steps=%s tokens_in=%s tokens_out=%s cost_usd=%.4f total_ms=%s",
            project_id,
            intent.id,
            model_config.name,
            outcome.steps,
            outcome.tokens_in,
            outcome.tokens_out,
            outcome.cost_usd,
            int((time.perf_counter() - started) * 1000),
        )
        return "success"
    except Exception:
        LOG.exception(
            "native explore crashed project=%s intent=%s", project_id, intent.id
        )
        best_effort_release(client, project_id, intent.id, worker.name)
        return "failed"
    finally:
        publisher.close()
        lease.stop()


def _outcome_as_result(outcome) -> "object":
    from frex.dispatcher.runtime.process import ProcessResult

    code = 0 if outcome.status == "completed" else 1
    return ProcessResult(
        returncode=code,
        stdout=json_compact(outcome.result) if outcome.result else "",
        stderr=outcome.error or "",
    )


def json_compact(value) -> str:
    import json

    try:
        return json.dumps(value)
    except (TypeError, ValueError):
        return str(value)
