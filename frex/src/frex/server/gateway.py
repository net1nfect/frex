"""Frex as a gateway: expose configured providers over OpenAI wire format.

With `FREX_GATEWAY_CONFIG` pointing at a YAML of providers/models (the
same schema as the dispatcher's), other tools can reach every provider
through this server's `/v1/chat/completions` — one endpoint, many
backends, bearer auth shared with the rest of the API.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from frex.dispatcher.config import ModelConfig, ProviderConfig, RouteEntry
from frex.dispatcher.providers.base import Completion, ProviderError
from frex.dispatcher.providers.registry import ProviderRegistry

LOG = logging.getLogger(__name__)

GATEWAY_CONFIG_ENV = "FREX_GATEWAY_CONFIG"

router = APIRouter(tags=["gateway"])

class GatewayChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]] | None = None
    stream: bool = False
    max_tokens: int | None = Field(default=None, gt=0)
    temperature: float | None = Field(default=None, ge=0, le=2)

class _GatewayState:
    registry: ProviderRegistry | None = None
    config_path: Path | None = None

_state = _GatewayState()

def _load_registry() -> ProviderRegistry | None:
    """(Re)load the gateway registry when the config file changed."""
    path_value = os.environ.get(GATEWAY_CONFIG_ENV, "").strip()
    if not path_value:
        return None
    path = Path(path_value)
    if _state.registry is not None and _state.config_path == path:
        return _state.registry
    raw = yaml.safe_load(path.read_text()) or {}
    # The gateway config is a standalone file with only providers/,
    # models/, routing/ — the same shapes as the dispatcher's, but
    # without its required server/runtime sections. Routing entries are
    # normalized so weighted dicts become RouteEntry objects.
    routing: dict[str, list[Any]] = {}
    for task_type, chain in (raw.get("routing") or {}).items():
        entries: list[Any] = []
        for entry in chain or []:
            if isinstance(entry, dict):
                entries.append(RouteEntry(**entry))
            else:
                entries.append(entry)
        routing[task_type] = entries
    registry = ProviderRegistry(
        type(
            "GatewayConfig",
            (),
            {
                "providers": [ProviderConfig(**p) for p in raw.get("providers", [])],
                "models": [ModelConfig(**m) for m in raw.get("models", [])],
                "routing": routing,
            },
        )()
    )
    _state.registry = registry
    _state.config_path = path
    LOG.info(
        "gateway config loaded path=%s providers=%d models=%d",
        path,
        len(raw.get("providers", [])),
        len(raw.get("models", [])),
    )
    return registry

def _require_registry() -> ProviderRegistry:
    registry = _load_registry()
    if registry is None:
        raise HTTPException(
            501,
            detail=f"gateway is not configured; set {GATEWAY_CONFIG_ENV}",
        )
    return registry

def _resolve_model(registry: ProviderRegistry, requested: str) -> ModelConfig:
    # Accept our model config name first, then the provider-native id.
    if requested in registry.model_names():
        return registry.model_config(requested)
    native = registry.find_model_by_native_id(requested)
    if native is not None:
        return native
    raise HTTPException(404, detail=f"unknown model {requested}")

def _openai_response(completion: Completion, model: str) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant"}
    if completion.text is not None:
        message["content"] = completion.text
    if completion.tool_calls:
        message["tool_calls"] = [
            {
                "id": c.id,
                "type": "function",
                "function": {"name": c.name, "arguments": c.arguments},
            }
            for c in completion.tool_calls
        ]
    return {
        "id": f"chatcmpl-frex-{int(time.time()*1000)}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": completion.model or model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": completion.finish_reason or "stop",
            }
        ],
        "usage": {
            "prompt_tokens": completion.usage.tokens_in,
            "completion_tokens": completion.usage.tokens_out,
            "total_tokens": completion.usage.tokens_in + completion.usage.tokens_out,
        },
    }

def _sse(chunk: dict[str, Any]) -> str:
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

def _unwrap_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
    """Client tools arrive in OpenAI wire format ({type, function});
    adapters take the agent-loop shape ({name, description, parameters})
    and do their own wrapping."""
    if not tools:
        return None
    unwrapped = []
    for tool in tools:
        function = tool.get("function") or tool
        if "name" in function:
            unwrapped.append(
                {
                    "name": function["name"],
                    "description": function.get("description", ""),
                    "parameters": function.get("parameters")
                    or {"type": "object", "properties": {}},
                }
            )
    return unwrapped or None

@router.get("/v1/models")
def list_models():
    registry = _require_registry()
    models = [
        {"id": name, "object": "model", "owned_by": "frex"}
        for name in registry.model_names()
    ]
    return {"object": "list", "data": models}

@router.post("/v1/chat/completions")
def chat_completions(body: GatewayChatRequest):
    registry = _require_registry()
    model_config = _resolve_model(registry, body.model)
    provider = registry.get_provider(model_config.name)

    kwargs: dict[str, Any] = {}
    if body.max_tokens is not None:
        kwargs["max_tokens"] = body.max_tokens
    if body.temperature is not None:
        kwargs["temperature"] = body.temperature

    try:
        if body.stream:
            return _streaming_response(
                provider, model_config, body.messages, body.tools, kwargs
            )
        completion = provider.complete(
            model_config.model,
            body.messages,
            tools=_unwrap_tools(body.tools),
            **kwargs,
        )
    except ProviderError as exc:
        raise HTTPException(502, detail=str(exc)) from exc
    except TypeError as exc:
        raise HTTPException(400, detail=f"unsupported request for this provider: {exc}") from exc
    return JSONResponse(_openai_response(completion, model_config.model))

def _streaming_response(
    provider, model_config: ModelConfig, messages, tools, kwargs
):
    """SSE stream: text deltas as they arrive, tool calls in one chunk."""
    deltas: list[str] = []
    result: dict[str, Any] = {}

    def on_token(text: str) -> None:
        deltas.append(text)

    def generate():
        try:
            try:
                completion = provider.complete(
                    model_config.model,
                    messages,
                    tools=_unwrap_tools(tools),
                    stream=True,
                    on_token=on_token,
                    **kwargs,
                )
            except TypeError as exc:
                # Adapters without streaming support degrade quietly.
                if "stream" not in str(exc):
                    raise
                completion = provider.complete(
                    model_config.model, messages, tools=_unwrap_tools(tools), **kwargs
                )
        except ProviderError as exc:
            yield _sse({"error": {"message": str(exc)}})
            yield "data: [DONE]\n\n"
            return

        first = {
            "id": f"chatcmpl-frex-{int(time.time()*1000)}",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model_config.model,
            "choices": [
                {"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}
            ],
        }
        yield _sse(first)
        for text in deltas:
            yield _sse(
                {
                    **first,
                    "choices": [
                        {"index": 0, "delta": {"content": text}, "finish_reason": None}
                    ],
                }
            )
        if completion.tool_calls:
            yield _sse(
                {
                    **first,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": i,
                                        "id": c.id,
                                        "type": "function",
                                        "function": {
                                            "name": c.name,
                                            "arguments": c.arguments,
                                        },
                                    }
                                    for i, c in enumerate(completion.tool_calls)
                                ]
                            },
                            "finish_reason": None,
                        }
                    ],
                }
            )
        yield _sse(
            {
                **first,
                "choices": [
                    {
                        "index": 0,
                        "delta": {},
                        "finish_reason": completion.finish_reason or "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": completion.usage.tokens_in,
                    "completion_tokens": completion.usage.tokens_out,
                    "total_tokens": completion.usage.tokens_in
                    + completion.usage.tokens_out,
                },
            }
        )
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
