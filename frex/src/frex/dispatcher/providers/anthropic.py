"""Anthropic native adapter: Messages API without a translating gateway.

The agent loop speaks OpenAI-style messages (role/content, tool_calls,
role:"tool" results). This adapter translates both directions, so the
loop and the tool executor stay vendor-agnostic.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

import requests

from frex.dispatcher.providers.base import (
    Completion,
    KeyPool,
    ProviderError,
    ToolCall,
    Usage,
)

LOG = logging.getLogger(__name__)

RETRIABLE_STATUS = {408, 429, 500, 502, 503, 504}
DEFAULT_BASE_URL = "https://api.anthropic.com"
ANTHROPIC_VERSION = "2023-06-01"

_STOP_REASON_MAP = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "tool_use": "tool_calls",
}

class AnthropicProvider:
    def __init__(
        self,
        name: str,
        base_url: str = "",
        api_key: str | None = None,
        api_keys: list[str] | None = None,
        *,
        max_retries: int = 2,
        retry_backoff_seconds: float = 1.0,
    ):
        self.name = name
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self._pool = KeyPool(api_key, api_keys)
        self.max_retries = max_retries
        self.retry_backoff_seconds = retry_backoff_seconds

    # ---- translation: OpenAI-style messages -> Anthropic Messages ----

    @staticmethod
    def translate_messages(
        messages: list[dict[str, Any]],
    ) -> tuple[str | None, list[dict[str, Any]]]:
        """Split OpenAI-style history into (system, anthropic_messages)."""
        system_parts: list[str] = []
        out: list[dict[str, Any]] = []
        for msg in messages:
            role = msg.get("role")
            content = msg.get("content")
            if role == "system":
                if isinstance(content, str):
                    system_parts.append(content)
                continue
            if role == "user":
                out.append({"role": "user", "content": [{"type": "text", "text": content or ""}]})
            elif role == "assistant":
                blocks: list[dict[str, Any]] = []
                if content:
                    blocks.append({"type": "text", "text": content})
                for call in msg.get("tool_calls") or []:
                    function = call.get("function") or {}
                    try:
                        parsed = json.loads(function.get("arguments") or "{}")
                    except ValueError:
                        parsed = {}
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": call.get("id") or function.get("name") or "toolu",
                            "name": function.get("name") or "",
                            "input": parsed if isinstance(parsed, dict) else {},
                        }
                    )
                if not blocks:
                    blocks.append({"type": "text", "text": ""})
                out.append({"role": "assistant", "content": blocks})
            elif role == "tool":
                # OpenAI tool result -> Anthropic tool_result inside a
                # user message; consecutive tool results each get their
                # own user message, which the API accepts.
                out.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": msg.get("tool_call_id") or "",
                                "content": content or "",
                            }
                        ],
                    }
                )
        return ("\n".join(system_parts) or None), out

    @staticmethod
    def _translate_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
        if not tools:
            return None
        return [
            {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "input_schema": tool.get("parameters")
                or {"type": "object", "properties": {}},
            }
            for tool in tools
        ]

    # ---- translation: Anthropic response -> Completion ----

    @staticmethod
    def _parse_response(body: dict[str, Any], latency_ms: int) -> Completion:
        blocks = body.get("content") or []
        text_parts = [b.get("text", "") for b in blocks if b.get("type") == "text"]
        tool_calls = [
            ToolCall(
                id=b.get("id") or b.get("name") or "toolu",
                name=b.get("name") or "",
                arguments=json.dumps(b.get("input") or {}),
            )
            for b in blocks
            if b.get("type") == "tool_use"
        ]
        usage = body.get("usage") or {}
        return Completion(
            text="".join(text_parts) or None,
            tool_calls=tool_calls,
            usage=Usage(
                tokens_in=int(usage.get("input_tokens") or 0),
                tokens_out=int(usage.get("output_tokens") or 0),
            ),
            finish_reason=_STOP_REASON_MAP.get(body.get("stop_reason")),
            model=body.get("model"),
            latency_ms=latency_ms,
        )

    # ---- wire ----

    def complete(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        timeout_seconds: int = 120,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> Completion:
        system, anthropic_messages = self.translate_messages(messages)
        payload: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens or 4096,  # required by the API
            "messages": anthropic_messages,
        }
        if system:
            payload["system"] = system
        translated_tools = self._translate_tools(tools)
        if translated_tools:
            payload["tools"] = translated_tools
        if temperature is not None:
            payload["temperature"] = temperature

        url = f"{self.base_url}/v1/messages"
        headers = {
            "Content-Type": "application/json",
            "anthropic-version": ANTHROPIC_VERSION,
        }
        key = self._pool.next()
        if key:
            headers["x-api-key"] = key

        last_error: ProviderError | None = None
        for attempt in range(self.max_retries + 1):
            started = time.perf_counter()
            try:
                response = requests.post(
                    url, json=payload, headers=headers, timeout=timeout_seconds
                )
            except requests.RequestException as exc:
                last_error = ProviderError(
                    f"provider {self.name} request failed: {exc}", retriable=True
                )
            else:
                latency_ms = int((time.perf_counter() - started) * 1000)
                if response.status_code == 200:
                    return self._parse_response(response.json(), latency_ms)
                last_error = ProviderError(
                    f"provider {self.name} returned {response.status_code}: {response.text[:200]}",
                    status_code=response.status_code,
                    retriable=response.status_code in RETRIABLE_STATUS,
                )
            if not last_error.retriable or attempt == self.max_retries:
                raise last_error
            time.sleep(self.retry_backoff_seconds * (2**attempt))
        raise last_error  # pragma: no cover - loop always raises or returns

    def healthcheck(self, model: str) -> bool:
        """A model is healthy if a one-token completion round-trips."""
        try:
            completion = self.complete(
                model,
                [{"role": "user", "content": "ping"}],
                timeout_seconds=15,
                max_tokens=1,
            )
            return completion.text is not None or bool(completion.tool_calls)
        except ProviderError:
            return False
