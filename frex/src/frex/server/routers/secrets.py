from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, field_validator

from frex.server import secrets as secret_store
from frex.server.db import get_conn
from frex.server.services import check_project_active, get_project_or_404, utcnow

router = APIRouter(tags=["secrets"])


class CreateSecretRequest(BaseModel):
    name: str
    value: str

    @field_validator("name", "value")
    @classmethod
    def validate_non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        return value.strip()


def _next_secret_id(conn, project_id: str) -> str:
    from frex.server.services import _next_scoped_id

    return _next_scoped_id(conn, "secret", "sec", project_id)


@router.post("/projects/{project_id}/secrets", status_code=201)
def create_secret(project_id: str, body: CreateSecretRequest):
    """Store a secret encrypted at rest.

    The API never returns the value — only the ref (secret://name) that
    facts may cite.
    """
    with get_conn() as conn:
        check_project_active(conn, project_id)
        try:
            key = secret_store.require_key()
        except secret_store.SecretKeyMissing as exc:
            raise HTTPException(503, str(exc))
        sid = _next_secret_id(conn, project_id)
        conn.execute(
            """
            INSERT INTO secret_refs (project_id, name, ciphertext, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(project_id, name) DO UPDATE SET
                ciphertext = excluded.ciphertext
            """,
            (
                project_id,
                body.name,
                secret_store.encrypt(body.value, key),
                utcnow(),
            ),
        )
        return {"id": sid, "name": body.name, "ref": secret_store.ref(body.name)}


@router.get("/projects/{project_id}/secrets")
def list_secrets(project_id: str):
    """Names and refs only; values are never listed."""
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        rows = conn.execute(
            "SELECT name, created_at FROM secret_refs WHERE project_id = ? ORDER BY name",
            (project_id,),
        ).fetchall()
        return {
            "secrets": [
                {"name": r["name"], "ref": secret_store.ref(r["name"]), "created_at": r["created_at"]}
                for r in rows
            ]
        }


@router.get("/projects/{project_id}/secrets/{name}")
def reveal_secret(project_id: str, name: str, reveal: int = Query(default=0)):
    """Resolve secret://name to its value.

    Requires reveal=1 AND bearer auth: an accidental curl without
    params gets the metadata, not the credential. Auth enforcement
    lives in the global middleware; reveal is the second key.
    """
    from frex.server import auth

    if not reveal:
        raise HTTPException(
            403, "secrets require ?reveal=1 and authenticated access"
        )
    if not auth.auth_enabled():
        raise HTTPException(
            403,
            "revealing secrets requires auth to be enabled (FREX_TOKEN)",
        )
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        row = conn.execute(
            "SELECT ciphertext FROM secret_refs WHERE project_id = ? AND name = ?",
            (project_id, name),
        ).fetchone()
        if row is None:
            raise HTTPException(404, f"secret {name} not found")
        try:
            key = secret_store.require_key()
        except secret_store.SecretKeyMissing as exc:
            raise HTTPException(503, str(exc))
        try:
            value = secret_store.decrypt(row["ciphertext"], key)
        except ValueError:
            raise HTTPException(500, "secret decryption failed — key mismatch?")
        return {"name": name, "value": value}
