from __future__ import annotations

import hashlib
import json
from typing import Literal

from fastapi import APIRouter, HTTPException, Query

from cairn.server.db import get_conn
from cairn.server.models import (
    MAX_RUN_OUTPUT_BYTES,
    CreateRunRequest,
    RunDetail,
    RunSummary,
)
from cairn.server.services import (
    check_project_active,
    get_project_or_404,
    next_run_id,
    utcnow,
)

router = APIRouter(tags=["runs"])


def _summary_from_row(row) -> RunSummary:
    return RunSummary(
        id=row["id"],
        project_id=row["project_id"],
        task_type=row["task_type"],
        worker=row["worker"],
        model=row["model"],
        phase=row["phase"],
        intent_id=row["intent_id"],
        command=row["command"],
        exit_code=row["exit_code"],
        timed_out=bool(row["timed_out"]),
        cancelled=bool(row["cancelled"]),
        stdout_truncated=bool(row["stdout_truncated"]),
        duration_ms=row["duration_ms"],
        tokens_in=row["tokens_in"],
        tokens_out=row["tokens_out"],
        cost_usd=row["cost_usd"],
        status=row["status"],
        client_run_id=row["client_run_id"],
        error=row["error"],
        created_at=row["created_at"],
        finished_at=row["finished_at"],
    )


def _check_output_size(name: str, value: str | None) -> None:
    if value is not None and len(value.encode()) > MAX_RUN_OUTPUT_BYTES:
        raise HTTPException(413, f"{name} exceeds 2 MiB")


@router.post("/projects/{project_id}/runs", response_model=RunSummary, status_code=201)
def create_run(project_id: str, body: CreateRunRequest):
    _check_output_size("stdout", body.stdout)
    _check_output_size("stderr", body.stderr)
    with get_conn() as conn:
        check_project_active(conn, project_id)
        now = utcnow()
        run_id = next_run_id(conn, project_id)
        stdout_sha256 = (
            hashlib.sha256(body.stdout.encode()).hexdigest()
            if body.stdout is not None
            else None
        )
        conn.execute(
            """
            INSERT INTO task_runs (
                id, project_id, task_type, worker, model, phase, intent_id,
                command, exit_code, timed_out, cancelled, stdout, stderr,
                stdout_sha256, duration_ms, tokens_in, tokens_out, cost_usd,
                status, client_run_id, error, created_at, finished_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                project_id,
                body.task_type,
                body.worker,
                body.model,
                body.phase,
                body.intent_id,
                body.command,
                body.exit_code,
                int(body.timed_out),
                int(body.cancelled),
                body.stdout,
                body.stderr,
                stdout_sha256,
                body.duration_ms,
                body.tokens_in,
                body.tokens_out,
                body.cost_usd,
                body.status,
                body.client_run_id,
                body.error,
                now,
                now,
            ),
        )
        row = conn.execute(
            "SELECT * FROM task_runs WHERE id = ? AND project_id = ?",
            (run_id, project_id),
        ).fetchone()
        return _summary_from_row(row)


@router.get("/projects/{project_id}/runs", response_model=list[RunSummary])
def list_runs(
    project_id: str,
    task_type: str | None = None,
    intent_id: str | None = None,
    status: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        clauses = ["project_id = ?"]
        params: list = [project_id]
        for column, value in (
            ("task_type", task_type),
            ("intent_id", intent_id),
            ("status", status),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        params.extend([limit, offset])
        rows = conn.execute(
            f"""
            SELECT * FROM task_runs
            WHERE {" AND ".join(clauses)}
            ORDER BY created_at, id
            LIMIT ? OFFSET ?
            """,
            params,
        ).fetchall()
        return [_summary_from_row(row) for row in rows]


@router.get("/projects/{project_id}/runs/{run_id}", response_model=RunDetail)
def get_run(
    project_id: str,
    run_id: str,
    include: Literal["stdout", "stderr", "both"] | None = None,
):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        row = conn.execute(
            "SELECT * FROM task_runs WHERE id = ? AND project_id = ?",
            (run_id, project_id),
        ).fetchone()
        if row is None:
            raise HTTPException(404, "Run not found")
        detail = RunDetail(**_summary_from_row(row).model_dump())
        detail.stdout_sha256 = row["stdout_sha256"]
        if include in ("stdout", "both"):
            detail.stdout = row["stdout"]
        if include in ("stderr", "both"):
            detail.stderr = row["stderr"]
        return detail
