from fastapi import APIRouter, HTTPException

import json

from cairn.server.db import get_conn
from cairn.server.idempotency import (
    canonical_payload_hash,
    idempotent,
    lookup_cached_response,
)
from cairn.server.models import (
    DEFAULT_MATCH_RULE,
    ConcludeRequest,
    ConcludeResponse,
    CreateIntentRequest,
    Evidence,
    Fact,
    HeartbeatRequest,
    Intent,
)
from cairn.server.services import (
    begin_intent_conclusion,
    check_project_active,
    claim_open_intent,
    get_intent_or_404,
    intent_to_model,
    next_evidence_id,
    next_fact_id,
    next_intent_id,
    utcnow,
    validate_facts_exist,
    validate_intent_creator_worker,
    validate_goal_not_in_sources,
)

router = APIRouter(tags=["intents"])


@router.post(
    "/projects/{project_id}/intents",
    response_model=Intent,
    status_code=201,
)
def create_intent(project_id: str, body: CreateIntentRequest):
    with get_conn() as conn:
        check_project_active(conn, project_id)
        validate_facts_exist(conn, project_id, body.from_)
        validate_goal_not_in_sources(body.from_)
        validate_intent_creator_worker(body.creator, body.worker)

        now = utcnow()
        iid = next_intent_id(conn, project_id)
        claimed = body.worker is not None
        conn.execute(
            "INSERT INTO intents (id, project_id, to_fact_id, description, creator, worker, last_heartbeat_at, created_at, concluded_at) VALUES (?, ?, NULL, ?, ?, ?, ?, ?, NULL)",
            (
                iid,
                project_id,
                body.description,
                body.creator,
                body.worker,
                now if claimed else None,
                now,
            ),
        )
        for fid in body.from_:
            conn.execute(
                "INSERT INTO intent_sources (intent_id, project_id, fact_id) VALUES (?, ?, ?)",
                (iid, project_id, fid),
            )

        return Intent(
            id=iid,
            **{"from": body.from_},
            to=None,
            description=body.description,
            creator=body.creator,
            worker=body.worker,
            last_heartbeat_at=now if claimed else None,
            created_at=now,
            concluded_at=None,
        )


@router.post(
    "/projects/{project_id}/intents/{intent_id}/heartbeat",
    response_model=Intent,
)
def heartbeat(project_id: str, intent_id: str, body: HeartbeatRequest):
    with get_conn() as conn:
        check_project_active(conn, project_id)
        row = claim_open_intent(conn, project_id, intent_id, body.worker)
        return intent_to_model(conn, row, project_id)


@router.post(
    "/projects/{project_id}/intents/{intent_id}/release",
    response_model=Intent,
)
def release(project_id: str, intent_id: str, body: HeartbeatRequest):
    with get_conn() as conn:
        check_project_active(conn, project_id)
        cursor = conn.execute(
            """
            UPDATE intents
            SET worker = NULL
            WHERE id = ? AND project_id = ?
              AND to_fact_id IS NULL
              AND (worker IS NULL OR worker = ?)
            """,
            (intent_id, project_id, body.worker),
        )
        if cursor.rowcount == 0:
            row = get_intent_or_404(conn, project_id, intent_id)
            if row["to_fact_id"] is not None:
                raise HTTPException(409, "Intent already concluded")
            raise HTTPException(
                409, f"Intent is currently claimed by {row['worker']}"
            )
        row = get_intent_or_404(conn, project_id, intent_id)
        return intent_to_model(conn, row, project_id)


@router.post(
    "/projects/{project_id}/intents/{intent_id}/conclude",
    response_model=ConcludeResponse,
)
def conclude(project_id: str, intent_id: str, body: ConcludeRequest):
    with get_conn() as conn:
        scope = f"conclude:{project_id}:{intent_id}"
        payload = body.model_dump(by_alias=True)
        # Replay before the status check: the first attempt may have been the
        # last act of an active project, and a retry must not 403 on it.
        if body.request_id:
            cached = lookup_cached_response(
                conn, scope, body.request_id, canonical_payload_hash(payload)
            )
            if cached is not None:
                return ConcludeResponse.model_validate(cached)
        check_project_active(conn, project_id)

        def produce() -> ConcludeResponse:
            now = utcnow()
            begin_intent_conclusion(conn, project_id, intent_id, body.worker, now=now)

            run = None
            if body.run_id is not None:
                run = conn.execute(
                    "SELECT * FROM task_runs WHERE id = ? AND project_id = ?",
                    (body.run_id, project_id),
                ).fetchone()
                if run is None:
                    raise HTTPException(404, "Run not found")

            kind = body.kind
            match_rule = body.match_rule or DEFAULT_MATCH_RULE[kind]

            fid = next_fact_id(conn, project_id)
            conn.execute(
                "INSERT INTO facts (id, project_id, description, kind, status, confidence, subject_json, created_by_run, created_at) VALUES (?, ?, ?, ?, 'hypothesis', ?, ?, ?, ?)",
                (
                    fid,
                    project_id,
                    body.description,
                    kind,
                    body.confidence,
                    json.dumps(body.subject) if body.subject is not None else None,
                    body.run_id,
                    now,
                ),
            )

            evidence_rows = []
            if run is not None:
                evid = next_evidence_id(conn, project_id)
                conn.execute(
                    """
                    INSERT INTO evidence (
                        id, project_id, fact_id, run_id, kind, command, exit_code,
                        match_rule, match_value, stdout_sha256, captured_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        evid,
                        project_id,
                        fid,
                        body.run_id,
                        kind,
                        run["command"],
                        run["exit_code"],
                        match_rule,
                        body.match_value,
                        run["stdout_sha256"],
                        now,
                    ),
                )
                evidence_rows.append(
                    Evidence(
                        id=evid,
                        run_id=body.run_id,
                        fact_id=fid,
                        kind=kind,
                        command=run["command"],
                        exit_code=run["exit_code"],
                        match_rule=match_rule,
                        match_value=body.match_value,
                        stdout_sha256=run["stdout_sha256"],
                        captured_at=now,
                    )
                )

            conn.execute(
                "UPDATE intents SET to_fact_id = ? WHERE id = ? AND project_id = ?",
                (fid, intent_id, project_id),
            )

            updated = conn.execute(
                "SELECT * FROM intents WHERE id = ? AND project_id = ?",
                (intent_id, project_id),
            ).fetchone()

            return ConcludeResponse(
                fact=Fact(
                    id=fid,
                    description=body.description,
                    kind=kind,
                    status="hypothesis",
                    confidence=body.confidence,
                    subject=body.subject,
                    created_by_run=body.run_id,
                    created_at=now,
                ),
                evidence=evidence_rows,
                intent=intent_to_model(conn, updated, project_id),
            )

        return idempotent(
            conn,
            scope,
            body.request_id,
            payload,
            ConcludeResponse,
            produce,
        )
