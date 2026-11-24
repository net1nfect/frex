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


class Provider(Protocol):
    def complete(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        timeout_seconds: int = 120,
    ) -> Completion: ...

    def healthcheck(self, model: str) -> bool: ...
