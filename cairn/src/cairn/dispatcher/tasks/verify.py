from __future__ import annotations

import logging
import time

from cairn.dispatcher.config import DispatchConfig, WorkerConfig
from cairn.dispatcher.contracts import TIMEOUT_EXIT_CODES, evaluate_evidence
from cairn.dispatcher.protocol.client import CairnClient
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.containers import ContainerManager
from cairn.dispatcher.tasks.common import (
    RunRecorder,
    cancel_reason,
    did_timeout,
    preview,
    run_healthcheck,
    run_worker_process,
)
from cairn.dispatcher.workers.registry import get_driver

LOG = logging.getLogger(__name__)

# Facts already past these statuses must not be replayed by the dispatcher:
# verified needs no replay, superseded is history.
ELIGIBLE_STATUSES = ("hypothesis", "refuted", "disputed")


def run_verify_task(
    config: DispatchConfig,
    client: CairnClient,
    container_manager: ContainerManager,
    project_id: str,
    fact_id: str,
    worker: WorkerConfig,
    cancellation: TaskCancellation,
) -> str:
    """Replay the evidence of one fact and report the verdict.

    Returns one of: verified, refuted, inconclusive, skipped, unhealthy,
    cancelled, failed.
    """
    driver = get_driver(worker.type)
    verify_config = config.tasks.verify
    started = time.perf_counter()

    try:
        detail = client.get_fact(project_id, fact_id)
    except Exception:
        LOG.exception("verify fetch failed project=%s fact=%s", project_id, fact_id)
        return "failed"

    fact = detail["fact"]
    evidence_rows = detail["evidence"]
    if fact["status"] not in ELIGIBLE_STATUSES:
        LOG.info(
            "verify skipped project=%s fact=%s status=%s",
            project_id,
            fact_id,
            fact["status"],
        )
        return "skipped"
    try:
        settings = client.get_settings()
        max_attempts = settings.verify_max_attempts
    except Exception:
        LOG.exception("verify settings fetch failed project=%s fact=%s", project_id, fact_id)
        return "failed"
    if len(detail["verifications"]) >= max_attempts:
        LOG.info(
            "verify skipped max attempts project=%s fact=%s attempts=%s max=%s",
            project_id,
            fact_id,
            len(detail["verifications"]),
            max_attempts,
        )
        return "skipped"
    evidence = next((e for e in evidence_rows if e.get("command")), None)
    if evidence is None:
        LOG.info(
            "verify skipped no replayable evidence project=%s fact=%s",
            project_id,
            fact_id,
        )
        return "skipped"

    recorder = RunRecorder(
        client,
        project_id,
        "verify",
        worker,
        model=None,
        intent_id=None,
        command=evidence.get("command"),
    )

    fresh_container: str | None = None
    if verify_config.container == "fresh":
        try:
            fresh_container = container_manager.create_verify_container(project_id)
        except Exception as exc:
            LOG.warning(
                "verify container creation failed project=%s fact=%s error=%s",
                project_id,
                fact_id,
                exc,
            )
            return "failed"
        container_name = fresh_container
    else:
        container_name = container_manager.ensure_running(project_id)

    try:
        healthcheck = run_healthcheck(
            container_manager,
            container_name,
            worker,
            driver.build_healthcheck(worker),
            timeout_seconds=config.runtime.healthcheck_timeout,
            cancellation=cancellation,
        )
        if cancel_reason(healthcheck.result, cancellation) is not None:
            return "cancelled"
        if healthcheck.result.returncode != 0:
            LOG.warning(
                "worker unhealthy during verify project=%s fact=%s stderr=%s",
                project_id,
                fact_id,
                preview(healthcheck.result.stderr),
            )
            return "unhealthy"

        argv = ["sh", "-c", evidence["command"]]
        replay_started = time.perf_counter()
        replay = recorder.run_worker_process(
            container_manager,
            container_name,
            worker,
            argv,
            phase="verify",
            timeout_seconds=verify_config.timeout,
            cancellation=cancellation,
        )
        replay_ms = int((time.perf_counter() - replay_started) * 1000)

        if cancel_reason(replay, cancellation) is not None:
            return "cancelled"

        if did_timeout(replay) or replay.returncode in TIMEOUT_EXIT_CODES:
            result, observed = "inconclusive", "replay timed out"
        else:
            result, observed = evaluate_evidence(
                evidence["match_rule"],
                match_value=evidence.get("match_value"),
                exit_code=evidence.get("exit_code"),
                stdout=replay.stdout or "",
                stdout_sha256=evidence.get("stdout_sha256"),
                replay_exit_code=replay.returncode,
            )

        response = client.create_verification(
            project_id,
            fact_id,
            {
                "run_id": recorder.last_run_id,
                "method": "replay",
                "result": result,
                "expected": evidence.get("match_value")
                or (f"exit_code={evidence.get('exit_code')}" if evidence.get("exit_code") is not None else None),
                "observed": observed,
                "details": f"replayed in {verify_config.container} container ({replay_ms}ms)",
            },
        )
        if not response.ok:
            LOG.warning(
                "verification submit failed project=%s fact=%s status=%s",
                project_id,
                fact_id,
                response.status_code,
            )
            return "failed"

        LOG.info(
            "verify done project=%s fact=%s result=%s replay_ms=%s total_ms=%s",
            project_id,
            fact_id,
            result,
            replay_ms,
            int((time.perf_counter() - started) * 1000),
        )
        return {"pass": "verified", "fail": "refuted"}.get(result, "inconclusive")
    except Exception:
        LOG.exception(
            "verify task crashed project=%s fact=%s", project_id, fact_id
        )
        return "failed"
    finally:
        if fresh_container is not None:
            container_manager.remove_container(fresh_container)
