from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any, Callable, TypeVar

from fastapi import HTTPException
from pydantic import BaseModel

from cairn.server.services import utcnow

T = TypeVar("T", bound=BaseModel)


def canonical_payload_hash(payload: dict[str, Any]) -> str:
    """Hash the request body so retries are compared semantically.

    ``request_id`` is excluded: it is the lookup key, and clients are not
    required to send byte-identical JSON on retry — only an equivalent
    payload.
    """
    body = {k: v for k, v in payload.items() if k != "request_id"}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def lookup_cached_response(
    conn: sqlite3.Connection, scope: str, request_id: str, payload_hash: str
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT payload_hash, response FROM idempotency_keys WHERE key = ? AND scope = ?",
        (request_id, scope),
    ).fetchone()
    if row is None:
        return None
    if row["payload_hash"] != payload_hash:
        raise HTTPException(
            409,
            "request_id was already used with a different payload",
        )
    return json.loads(row["response"])


def store_response(
    conn: sqlite3.Connection,
    scope: str,
    request_id: str,
    payload_hash: str,
    response: BaseModel,
) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO idempotency_keys (key, scope, payload_hash, response, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            request_id,
            scope,
            payload_hash,
            response.model_dump_json(by_alias=True),
            utcnow(),
        ),
    )


def idempotent(
    conn: sqlite3.Connection,
    scope: str,
    request_id: str | None,
    payload: dict[str, Any],
    response_type: type[T],
    produce: Callable[[], T],
) -> T:
    """Run ``produce`` once per (scope, request_id); replay it on retry.

    ``produce`` runs inside the caller's transaction, so the stored response
    commits atomically with the mutation it describes. A retry therefore
    never re-executes the mutation, and a request_id reused with a different
    payload is rejected instead of silently replaying the wrong response.
    """
    payload_hash = canonical_payload_hash(payload)
    if request_id:
        cached = lookup_cached_response(conn, scope, request_id, payload_hash)
        if cached is not None:
            return response_type.model_validate(cached)
    response = produce()
    if request_id:
        store_response(conn, scope, request_id, payload_hash, response)
    return response
