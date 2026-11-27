from fastapi import APIRouter

from frex.server.db import get_conn
from frex.server.models import CreateHintRequest, Hint
from frex.server.services import (
    check_project_hint_writable,
    get_project_or_404,
    next_hint_id,
    utcnow,
)

router = APIRouter(tags=["hints"])


@router.post(
    "/projects/{project_id}/hints",
    response_model=Hint,
    status_code=201,
)
def create_hint(project_id: str, body: CreateHintRequest):
    with get_conn() as conn:
        check_project_hint_writable(conn, project_id)

        now = utcnow()
        hid = next_hint_id(conn, project_id)
        conn.execute(
            "INSERT INTO hints (id, project_id, content, creator, created_at) VALUES (?, ?, ?, ?, ?)",
            (hid, project_id, body.content, body.creator, now),
        )
        return Hint(id=hid, content=body.content, creator=body.creator, created_at=now)


@router.post("/projects/{project_id}/hint-queue", status_code=201)
def enqueue_steering_hint(project_id: str, body: CreateHintRequest):
    with get_conn() as conn:
        check_project_hint_writable(conn, project_id)
        now = utcnow()
        qid = next_hint_id(conn, project_id)
        conn.execute(
            """
            INSERT INTO hint_queue (id, project_id, content, creator, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (qid, project_id, body.content, body.creator, now),
        )
        return {"id": qid, "content": body.content, "creator": body.creator}


@router.get("/projects/{project_id}/hint-queue")
def consume_steering_hints(project_id: str):
    """Return pending steering hints and mark them consumed.

    Consuming on read is the contract the agent loop relies on: it polls
    between steps and must never see the same hint twice. The read and
    the mark happen in one transaction (get_conn's implicit one), so two
    pollers cannot both claim the same hint.
    """
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        rows = conn.execute(
            """
            SELECT id, content, creator, created_at FROM hint_queue
            WHERE project_id = ? AND consumed_at IS NULL
            ORDER BY created_at, id
            """,
            (project_id,),
        ).fetchall()
        now = utcnow()
        for row in rows:
            conn.execute(
                "UPDATE hint_queue SET consumed_at = ? WHERE id = ? AND project_id = ? AND consumed_at IS NULL",
                (now, row["id"], project_id),
            )
        return {
            "hints": [
                {"id": r["id"], "content": r["content"], "creator": r["creator"]}
                for r in rows
            ]
        }
