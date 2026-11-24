from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from cairn.server.db import get_conn
from cairn.server.models import Evidence, Fact, FactVerification
from cairn.server.services import fact_from_row, get_project_or_404

router = APIRouter(tags=["facts"])


def _evidence_from_row(row) -> Evidence:
    return Evidence(
        id=row["id"],
        run_id=row["run_id"],
        fact_id=row["fact_id"],
        kind=row["kind"],
        command=row["command"],
        exit_code=row["exit_code"],
        match_rule=row["match_rule"],
        match_value=row["match_value"],
        stdout_sha256=row["stdout_sha256"],
        excerpt=row["excerpt"],
        captured_at=row["captured_at"],
    )


def _verification_from_row(row) -> FactVerification:
    return FactVerification(
        id=row["id"],
        fact_id=row["fact_id"],
        run_id=row["run_id"],
        method=row["method"],
        result=row["result"],
        verifier_model=row["verifier_model"],
        expected=row["expected"],
        observed=row["observed"],
        details=row["details"],
        created_at=row["created_at"],
    )


def get_fact_or_404(conn, project_id: str, fact_id: str):
    row = conn.execute(
        "SELECT * FROM facts WHERE id = ? AND project_id = ?",
        (fact_id, project_id),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Fact not found")
    return row


@router.get("/projects/{project_id}/facts", response_model=list[Fact])
def list_facts(
    project_id: str,
    status: str | None = None,
    kind: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        clauses = ["project_id = ?"]
        params: list = [project_id]
        for column, value in (("status", status), ("kind", kind)):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        params.extend([limit, offset])
        rows = conn.execute(
            f"""
            SELECT * FROM facts
            WHERE {" AND ".join(clauses)}
            ORDER BY rowid
            LIMIT ? OFFSET ?
            """,
            params,
        ).fetchall()
        return [fact_from_row(row) for row in rows]


@router.get("/projects/{project_id}/facts/{fact_id}")
def get_fact(project_id: str, fact_id: str):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        row = get_fact_or_404(conn, project_id, fact_id)
        evidence = conn.execute(
            "SELECT * FROM evidence WHERE fact_id = ? AND project_id = ? ORDER BY captured_at, id",
            (fact_id, project_id),
        ).fetchall()
        verifications = conn.execute(
            "SELECT * FROM fact_verifications WHERE fact_id = ? AND project_id = ? ORDER BY created_at, id",
            (fact_id, project_id),
        ).fetchall()
        return {
            "fact": fact_from_row(row),
            "evidence": [_evidence_from_row(e) for e in evidence],
            "verifications": [_verification_from_row(v) for v in verifications],
        }
