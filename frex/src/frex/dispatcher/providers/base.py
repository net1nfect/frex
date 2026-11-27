"""Provider abstraction: swap the model without touching the worker image.

A provider turns a list of messages into a Completion. Everything
downstream (agent loop, task runs, cost tracking) speaks in terms of
this interface, never in terms of a vendor SDK.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Protocol

import requests


@dataclass(slots=True)
class ToolCall:
    id: str
    name: str
    arguments: str  # JSON string, parsed by the tool executor


@dataclass(slots=True)
class Usage:
    tokens_in: int = 0
    tokens_out: int = 0


@dataclass(slots=True)
class Completion:
    text: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    finish_reason: str | None = None
    model: str | None = None
    latency_ms: int = 0


@dataclass(slots=True)
class ProviderError(Exception):
    """Raised when a provider call fails after its own retries."""

    message: str
    status_code: int | None = None
    retriable: bool = False

    def __str__(self) -> str:
        return self.message

class KeyPool:
    """Round-robin over one provider's API keys.

    A provider may hold several credentials (rate-limit spreading, per-key
    quotas). `next()` walks the keys in order; a single key behaves
    identically to the old fixed-credential adapters.
    """

    def __init__(self, api_key: str | None = None, api_keys: list[str] | None = None):
        candidates = ([api_key] if api_key else []) + list(api_keys or [])
        self._keys = list(dict.fromkeys(k for k in candidates if k))
        self._index = 0

    def next(self) -> str | None:
        if not self._keys:
            return None
        key = self._keys[self._index % len(self._keys)]
        self._index += 1
        return key

    def __len__(self) -> int:
        return len(self._keys)


class Provider(Protocol):
    def complete(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        timeout_seconds: int = 120,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> Completion: ...

    def healthcheck(self, model: str) -> bool: ...
