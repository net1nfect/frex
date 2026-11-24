from __future__ import annotations

from fastapi import APIRouter

from cairn import __version__
from cairn.server.db import SCHEMA_VERSION

router = APIRouter(tags=["capabilities"])

# Ordered by the phase that introduced them; the dispatcher checks
# membership, never order.
FEATURES = (
    "task_runs",
    "evidence",
    "verify",
    "require_evidence",
)


@router.get("/capabilities")
def get_capabilities():
    return {
        "version": __version__,
        "schema_version": SCHEMA_VERSION,
        "features": list(FEATURES),
    }
