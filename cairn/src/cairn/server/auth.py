from __future__ import annotations

import logging
import os
import secrets

from fastapi import Request
from fastapi.responses import JSONResponse

LOG = logging.getLogger(__name__)

TOKEN_ENV = "CAIRN_API_TOKEN"

EXEMPT_PATHS = frozenset({"/", "/docs", "/redoc", "/openapi.json"})
EXEMPT_PREFIXES = ("/static/",)


def token_from_env() -> str | None:
    token = os.environ.get(TOKEN_ENV, "").strip()
    return token or None


def auth_enabled() -> bool:
    return token_from_env() is not None


def warn_if_disabled() -> None:
    if not auth_enabled():
        LOG.warning(
            "%s is not set; the API is reachable without authentication", TOKEN_ENV
        )


def _is_exempt(path: str) -> bool:
    return path in EXEMPT_PATHS or path.startswith(EXEMPT_PREFIXES)


async def auth_middleware(request: Request, call_next):
    token = token_from_env()
    if token is None or _is_exempt(request.url.path):
        return await call_next(request)

    scheme, _, value = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not secrets.compare_digest(value, token):
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    return await call_next(request)
