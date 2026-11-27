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

from frex.dispatcher.providers.base import Completion, ProviderError, ToolCall, Usage

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
        stream: bool = False,
        on_token=None,
    ) -> Completion:
        payload: dict[str, Any] = {"model": model, "messages": messages}
        if tools:
            payload["tools"] = [
                {"type": "function", "function": tool} for tool in tools
            ]
        if stream:
            return self._complete_streaming(
                model, payload, timeout_seconds, on_token
            )
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

    def _complete_streaming(
        self,
        model: str,
        payload: dict[str, Any],
        timeout_seconds: int,
        on_token,
    ) -> Completion:
        """SSE variant: same wire format, chunked deltas.

        Tool calls arrive as index-keyed argument deltas that must be
        stitched back together; text deltas go to on_token so the event
        stream can show the model thinking in realtime.
        """
        import json as _json

        payload = {**payload, "stream": True}
        url = f"{self.base_url}/chat/completions"
        started = time.perf_counter()
        response = requests.post(
            url,
            json=payload,
            headers=self._headers(),
            timeout=timeout_seconds,
            stream=True,
        )
        if response.status_code != 200:
            raise ProviderError(
                f"provider {self.name} returned {response.status_code}: {response.text[:200]}",
                status_code=response.status_code,
                retriable=response.status_code in RETRIABLE_STATUS,
            )

        text_parts: list[str] = []
        tool_calls: dict[int, dict[str, Any]] = {}
        usage = Usage()
        finish_reason = None
        for line in response.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data: "):
                continue
            data = line[len("data: "):]
            if data.strip() == "[DONE]":
                break
            try:
                chunk = _json.loads(data)
            except ValueError:
                continue
            chunk_usage = chunk.get("usage")
            if chunk_usage:
                usage = Usage(
                    tokens_in=int(chunk_usage.get("prompt_tokens") or 0),
                    tokens_out=int(chunk_usage.get("completion_tokens") or 0),
                )
            choices = chunk.get("choices") or []
            if not choices:
                continue
            choice = choices[0]
            finish_reason = choice.get("finish_reason") or finish_reason
            delta = choice.get("delta") or {}
            content = delta.get("content")
            if content:
                text_parts.append(content)
                if on_token is not None:
                    on_token(content)
            for raw in delta.get("tool_calls") or []:
                index = raw.get("index", 0)
                slot = tool_calls.setdefault(
                    index, {"id": "", "name": "", "arguments": ""}
                )
                if raw.get("id"):
                    slot["id"] = raw["id"]
                function = raw.get("function") or {}
                if function.get("name"):
                    slot["name"] = function["name"]
                if function.get("arguments"):
                    slot["arguments"] += function["arguments"]

        parsed_calls = [
            ToolCall(
                id=slot["id"] or slot["name"] or f"call{i}",
                name=slot["name"],
                arguments=slot["arguments"] or "{}",
            )
            for i, slot in sorted(tool_calls.items())
        ]
        text = "".join(text_parts) if text_parts else None
        return Completion(
            text=text,
            tool_calls=parsed_calls,
            usage=usage,
            finish_reason=finish_reason,
            model=model,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

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
