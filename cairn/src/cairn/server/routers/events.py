from __future__ import annotations

import json

import time

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator

from cairn.server.db import get_conn
from cairn.server.services import (
    _next_scoped_id,
    check_project_active,
    get_project_or_404,
    utcnow,
)

router = APIRouter(tags=["events"])

VALID_KINDS = (
    "task.started",
    "token.delta",
    "tool.started",
    "tool.output",
    "steering.received",
    "fact.created",
    "task.finished",
    "error",
)


class CreateEventsRequest(BaseModel):
    task_type: str | None = None
    events: list[dict] = Field(min_length=1, max_length=500)

    @field_validator("events")
    @classmethod
    def validate_events(cls, value: list[dict]) -> list[dict]:
        for event in value:
            if event.get("kind") not in VALID_KINDS:
                raise ValueError(f"unknown event kind: {event.get('kind')!r}")
        return value


def _event_from_row(row) -> dict:
    return {
        "id": row["id"],
        "seq": row["seq"],
        "kind": row["kind"],
        "task_type": row["task_type"],
        "data": json.loads(row["data"]) if row["data"] else None,
        "created_at": row["created_at"],
    }


@router.post("/projects/{project_id}/events", status_code=201)
def create_events(project_id: str, body: CreateEventsRequest):
    with get_conn() as conn:
        check_project_active(conn, project_id)
        now = utcnow()
        created = []
        for event in body.events:
            eid = _next_scoped_id(conn, "event_seq", "e", project_id)
            seq = int(eid[1:])
            conn.execute(
                """
                INSERT INTO events (id, seq, project_id, task_type, kind, data, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    eid,
                    seq,
                    project_id,
                    body.task_type,
                    event["kind"],
                    json.dumps(event.get("data")) if event.get("data") is not None else None,
                    now,
                ),
            )
            created.append({"id": eid, "seq": seq, "kind": event["kind"]})
        return {"events": created}


@router.get("/projects/{project_id}/events")
def list_events(project_id: str, since: int = Query(default=0, ge=0), limit: int = Query(default=500, ge=1, le=2000)):
    """Poll events after a sequence number — the SSE fallback path."""
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        rows = conn.execute(
            """
            SELECT * FROM events WHERE project_id = ? AND seq > ?
            ORDER BY seq LIMIT ?
            """,
            (project_id, since, limit),
        ).fetchall()
        return {
            "events": [_event_from_row(r) for r in rows],
            "last_seq": rows[-1]["seq"] if rows else since,
        }


@router.get("/projects/{project_id}/events/stream")
def stream_events(
    project_id: str,
    since: int = Query(default=0, ge=0),
    # Internal cap on poll cycles: proxies can translate this into a
    # max-duration stream; tests use it to terminate deterministically.
    max_polls: int | None = Query(default=None, gt=0),
):
    """Server-Sent Events stream; falls back to long-poll semantics."""
    with get_conn() as conn:
        get_project_or_404(conn, project_id)

    def generate(start_seq: int, max_polls: int | None):
        cursor = start_seq
        polls = 0
        with get_conn() as conn:
            while max_polls is None or polls < max_polls:
                polls += 1
                rows = conn.execute(
                    "SELECT * FROM events WHERE project_id = ? AND seq > ? ORDER BY seq LIMIT 200",
                    (project_id, cursor),
                ).fetchall()
                for row in rows:
                    cursor = row["seq"]
                    payload = json.dumps(_event_from_row(row))
                    yield f"id: {cursor}\nevent: {row['kind']}\ndata: {payload}\n\n"
                if not rows:
                    # Heartbeat keeps proxies from closing an idle stream.
                    yield ": ping\n\n"
                time.sleep(1.0)

    return StreamingResponse(
        generate(since, max_polls),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
