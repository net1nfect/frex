"""Google Gemini native adapter: generateContent without a gateway.

Translates the agent loop's OpenAI-style messages to Gemini
contents/parts, including function calling (functionCall /
functionResponse parts).
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
DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"

_FINISH_MAP = {
    "STOP": "stop",
    "MAX_TOKENS": "length",
    "SAFETY": "content_filter",
    "RECITATION": "content_filter",
}

class GeminiProvider:
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

    # ---- translation: OpenAI-style messages -> Gemini contents ----

    @staticmethod
    def translate_messages(
        messages: list[dict[str, Any]],
    ) -> tuple[str | None, list[dict[str, Any]]]:
        """Return (system_instruction, contents)."""
        system_parts: list[str] = []
        contents: list[dict[str, Any]] = []
        # Gemini requires functionResponse parts to name the function;
        # remember the last assistant functionCall per tool_call_id.
        call_names: dict[str, str] = {}
        for msg in messages:
            role = msg.get("role")
            content = msg.get("content")
            if role == "system":
                if isinstance(content, str):
                    system_parts.append(content)
                continue
            if role == "user":
                contents.append({"role": "user", "parts": [{"text": content or ""}]})
            elif role == "assistant":
                parts: list[dict[str, Any]] = []
                if content:
                    parts.append({"text": content})
                for call in msg.get("tool_calls") or []:
                    function = call.get("function") or {}
                    try:
                        args = json.loads(function.get("arguments") or "{}")
                    except ValueError:
                        args = {}
                    if not isinstance(args, dict):
                        args = {}
                    call_names[call.get("id") or ""] = function.get("name") or ""
                    parts.append(
                        {
                            "functionCall": {
                                "name": function.get("name") or "",
                                "args": args,
                            }
                        }
                    )
                if not parts:
                    parts.append({"text": ""})
                contents.append({"role": "model", "parts": parts})
            elif role == "tool":
                tool_call_id = msg.get("tool_call_id") or ""
                name = call_names.get(tool_call_id, tool_call_id)
                try:
                    parsed = json.loads(content) if content else {}
                except ValueError:
                    parsed = {"output": content or ""}
                contents.append(
                    {
                        "role": "user",
                        "parts": [
                            {
                                "functionResponse": {
                                    "name": name,
                                    "response": parsed
                                    if isinstance(parsed, dict)
                                    else {"output": str(parsed)},
                                }
                            }
                        ],
                    }
                )
        return ("\n".join(system_parts) or None), contents

    @staticmethod
    def _translate_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
        if not tools:
            return None
        declarations = [
            {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": tool.get("parameters")
                or {"type": "object", "properties": {}},
            }
            for tool in tools
        ]
        return [{"functionDeclarations": declarations}]

    # ---- translation: Gemini response -> Completion ----

    @staticmethod
    def _parse_response(body: dict[str, Any], latency_ms: int) -> Completion:
        candidates = body.get("candidates") or []
        candidate = candidates[0] if candidates else {}
        parts = (candidate.get("content") or {}).get("parts") or []
        text_parts = [p.get("text", "") for p in parts if "text" in p]
        tool_calls = [
            ToolCall(
                id=f"call-{i}-{call.get('name', '')}",
                name=call.get("name") or "",
                arguments=json.dumps(call.get("args") or {}),
            )
            for i, p in enumerate(parts)
            for call in [p.get("functionCall")]
            if call
        ]
        usage = body.get("usageMetadata") or {}
        return Completion(
            text="".join(text_parts) or None,
            tool_calls=tool_calls,
            usage=Usage(
                tokens_in=int(usage.get("promptTokenCount") or 0),
                tokens_out=int(usage.get("candidatesTokenCount") or 0),
            ),
            finish_reason=_FINISH_MAP.get(candidate.get("finishReason")),
            model=body.get("modelVersion"),
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
        system, contents = self.translate_messages(messages)
        payload: dict[str, Any] = {"contents": contents}
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        translated_tools = self._translate_tools(tools)
        if translated_tools:
            payload["tools"] = translated_tools
        generation_config: dict[str, Any] = {}
        if max_tokens is not None:
            generation_config["maxOutputTokens"] = max_tokens
        if temperature is not None:
            generation_config["temperature"] = temperature
        if generation_config:
            payload["generationConfig"] = generation_config

        url = f"{self.base_url}/models/{model}:generateContent"
        headers = {"Content-Type": "application/json"}
        key = self._pool.next()
        if key:
            headers["x-goog-api-key"] = key

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
        try:
            completion = self.complete(
                model,
                [{"role": "user", "content": "ping"}],
                timeout_seconds=15,
                max_tokens=8,
            )
            return completion.text is not None or bool(completion.tool_calls)
        except ProviderError:
            return False
