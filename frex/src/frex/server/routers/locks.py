from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from frex.server.db import get_conn
from frex.server.services import check_project_active, get_project_or_404, utcnow

router = APIRouter(tags=["locks"])


class AcquireLockRequest(BaseModel):
    resource: str
    worker: str
    ttl_seconds: int = Field(default=300, ge=1, le=3600)

    @field_validator("resource", "worker")
    @classmethod
    def validate_non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        return value.strip()


class ReleaseLockRequest(BaseModel):
    resource: str
    worker: str

    @field_validator("resource", "worker")
    @classmethod
    def validate_non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        return value.strip()


@router.post("/projects/{project_id}/locks/acquire", status_code=201)
def acquire_lock(project_id: str, body: AcquireLockRequest):
    """Try to exclusively lock a resource (host, service, credential).

    409 when another worker holds a live lock. Expired locks are
    reclaimed in the same transaction, so a crashed worker's lock
    cannot deadlock the target forever.
    """
    with get_conn() as conn:
        check_project_active(conn, project_id)
        now = utcnow()
        # Reclaim expired locks first.
        conn.execute(
            "DELETE FROM resource_locks WHERE project_id = ? AND expires_at <= ?",
            (project_id, now),
        )
        existing = conn.execute(
            "SELECT worker FROM resource_locks WHERE project_id = ? AND resource = ?",
            (project_id, body.resource),
        ).fetchone()
        if existing is not None:
            raise HTTPException(
                409,
                f"resource {body.resource} locked by {existing['worker']}",
            )
        expires = _expires_at(now, body.ttl_seconds)
        conn.execute(
            """
            INSERT INTO resource_locks (project_id, resource, worker, acquired_at, expires_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (project_id, body.resource, body.worker, now, expires),
        )
        return {
            "resource": body.resource,
            "worker": body.worker,
            "expires_at": expires,
        }


@router.post("/projects/{project_id}/locks/release")
def release_lock(project_id: str, body: ReleaseLockRequest):
    """Release a lock. Only the holder can release it."""
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        row = conn.execute(
            "SELECT worker FROM resource_locks WHERE project_id = ? AND resource = ?",
            (project_id, body.resource),
        ).fetchone()
        if row is None:
            return {"released": False, "reason": "not locked"}
        if row["worker"] != body.worker:
            raise HTTPException(
                403, f"resource {body.resource} is locked by {row['worker']}"
            )
        conn.execute(
            "DELETE FROM resource_locks WHERE project_id = ? AND resource = ?",
            (project_id, body.resource),
        )
        return {"released": True}


@router.get("/projects/{project_id}/locks")
def list_locks(project_id: str):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        now = utcnow()
        rows = conn.execute(
            """
            SELECT resource, worker, acquired_at, expires_at FROM resource_locks
            WHERE project_id = ? AND expires_at > ?
            ORDER BY resource
            """,
            (project_id, now),
        ).fetchall()
        return {
            "locks": [
                {
                    "resource": r["resource"],
                    "worker": r["worker"],
                    "acquired_at": r["acquired_at"],
                    "expires_at": r["expires_at"],
                }
                for r in rows
            ]
        }


def _expires_at(now: str, ttl_seconds: int) -> str:
    from datetime import datetime, timedelta, timezone

    base = datetime.fromisoformat(now.replace("Z", "+00:00"))
    return (base + timedelta(seconds=ttl_seconds)).isoformat()
