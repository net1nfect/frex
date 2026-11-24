"""OpenAI-compatible chat-completions adapter.

This is the adapter uncensored self-hosted models speak: vLLM, Ollama
(with its OpenAI shim), llama.cpp server, TabbyAPI, and every minor
cloud that copies the /v1/chat/completions shape. One adapter covers
the whole self-hosted fleet, which is the point of the uncensored
strategy.
"""
from __future__ import annotations

import logging
import time
from typing import Any

import requests

from cairn.dispatcher.providers.base import Completion, ProviderError, ToolCall, Usage

LOG = logging.getLogger(__name__)

RETRIABLE_STATUS = {408, 429, 500, 502, 503, 504}


class OpenAICompatProvider:
    def __init__(
        self,
        name: str,
        base_url: str,
        api_key: str | None = None,
        *,
        max_retries: int = 2,
        retry_backoff_seconds: float = 1.0,
    ):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.max_retries = max_retries
        self.retry_backoff_seconds = retry_backoff_seconds

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _parse_completion(self, body: dict[str, Any], latency_ms: int) -> Completion:
        choices = body.get("choices") or []
        choice = choices[0] if choices else {}
        message = choice.get("message") or {}
        tool_calls = []
        for raw in message.get("tool_calls") or []:
            function = raw.get("function") or {}
            tool_calls.append(
                ToolCall(
                    id=raw.get("id") or function.get("name") or "call",
                    name=function.get("name") or "",
                    arguments=function.get("arguments") or "{}",
                )
            )
        usage = body.get("usage") or {}
        return Completion(
            text=message.get("text") or message.get("content"),
            tool_calls=tool_calls,
            usage=Usage(
                tokens_in=int(usage.get("prompt_tokens") or 0),
                tokens_out=int(usage.get("completion_tokens") or 0),
            ),
            finish_reason=choice.get("finish_reason"),
            model=body.get("model"),
            latency_ms=latency_ms,
        )

    def complete(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        timeout_seconds: int = 120,
    ) -> Completion:
        payload: dict[str, Any] = {"model": model, "messages": messages}
        if tools:
            payload["tools"] = [
                {"type": "function", "function": tool} for tool in tools
            ]
        url = f"{self.base_url}/chat/completions"
        last_error: ProviderError | None = None
        for attempt in range(self.max_retries + 1):
            started = time.perf_counter()
            try:
                response = requests.post(
                    url,
                    json=payload,
                    headers=self._headers(),
                    timeout=timeout_seconds,
                )
            except requests.RequestException as exc:
                last_error = ProviderError(
                    f"provider {self.name} request failed: {exc}", retriable=True
                )
            else:
                latency_ms = int((time.perf_counter() - started) * 1000)
                if response.status_code == 200:
                    return self._parse_completion(response.json(), latency_ms)
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
            )
            # 413-style overloads can still return 200 with no choice;
            # treat an empty response as unhealthy.
            return completion.text is not None or bool(completion.tool_calls)
        except ProviderError:
            return False
