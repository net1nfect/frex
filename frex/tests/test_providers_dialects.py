from __future__ import annotations

import json

import pytest

from frex.dispatcher.providers.anthropic import AnthropicProvider
from frex.dispatcher.providers.base import ProviderError
from frex.dispatcher.providers.gemini import GeminiProvider

OPENAI_HISTORY = [
    {"role": "system", "content": "You are a red team engine."},
    {"role": "user", "content": "scan the host"},
    {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "c1",
                "type": "function",
                "function": {"name": "run_command", "arguments": '{"command": "id"}'},
            }
        ],
    },
    {"role": "tool", "tool_call_id": "c1", "content": '{"stdout": "uid=0"}'},
]


def test_anthropic_translate_messages():
    system, messages = AnthropicProvider.translate_messages(OPENAI_HISTORY)
    assert system == "You are a red team engine."
    assert messages[0] == {
        "role": "user",
        "content": [{"type": "text", "text": "scan the host"}],
    }
    assistant = messages[1]
    assert assistant["role"] == "assistant"
    tool_use = [b for b in assistant["content"] if b["type"] == "tool_use"][0]
    assert tool_use["id"] == "c1"
    assert tool_use["name"] == "run_command"
    assert tool_use["input"] == {"command": "id"}
    tool_result = messages[2]["content"][0]
    assert tool_result["type"] == "tool_result"
    assert tool_result["tool_use_id"] == "c1"
    assert tool_result["content"] == '{"stdout": "uid=0"}'


def test_anthropic_parse_response_with_tool_use():
    body = {
        "model": "claude-sonnet-5",
        "stop_reason": "tool_use",
        "content": [
            {"type": "text", "text": "running id"},
            {
                "type": "tool_use",
                "id": "toolu_1",
                "name": "run_command",
                "input": {"command": "id"},
            },
        ],
        "usage": {"input_tokens": 12, "output_tokens": 34},
    }
    completion = AnthropicProvider._parse_response(body, latency_ms=5)
    assert completion.text == "running id"
    assert completion.tool_calls[0].name == "run_command"
    assert json.loads(completion.tool_calls[0].arguments) == {"command": "id"}
    assert completion.usage.tokens_in == 12
    assert completion.usage.tokens_out == 34
    assert completion.finish_reason == "tool_calls"


def test_anthropic_complete_wire_format(monkeypatch):
    captured = {}

    class Response:
        status_code = 200

        def json(self):
            return {
                "model": "claude-sonnet-5",
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "pong"}],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["body"] = json
        captured["headers"] = headers
        return Response()

    import requests as requests_module

    monkeypatch.setattr(requests_module, "post", fake_post)
    provider = AnthropicProvider("claude", api_key="sk-test")
    completion = provider.complete(
        "claude-sonnet-5",
        OPENAI_HISTORY,
        tools=[{"name": "run_command", "description": "run", "parameters": {"type": "object"}}],
        max_tokens=512,
        temperature=0.2,
    )
    assert completion.text == "pong"
    assert captured["url"].endswith("/v1/messages")
    assert captured["headers"]["x-api-key"] == "sk-test"
    assert captured["headers"]["anthropic-version"] == "2023-06-01"
    assert captured["body"]["max_tokens"] == 512
    assert captured["body"]["system"] == "You are a red team engine."
    assert captured["body"]["tools"][0]["name"] == "run_command"
    assert captured["body"]["tools"][0]["input_schema"] == {"type": "object"}


def test_gemini_translate_messages():
    system, contents = GeminiProvider.translate_messages(OPENAI_HISTORY)
    assert system == "You are a red team engine."
    assert contents[0] == {"role": "user", "parts": [{"text": "scan the host"}]}
    model_turn = contents[1]
    assert model_turn["role"] == "model"
    call = model_turn["parts"][0]["functionCall"]
    assert call["name"] == "run_command"
    assert call["args"] == {"command": "id"}
    response_part = contents[2]["parts"][0]["functionResponse"]
    assert response_part["name"] == "run_command"
    assert response_part["response"] == {"stdout": "uid=0"}


def test_gemini_parse_response_with_function_call():
    body = {
        "candidates": [
            {
                "finishReason": "STOP",
                "content": {
                    "parts": [
                        {"text": "running"},
                        {"functionCall": {"name": "run_command", "args": {"command": "id"}}},
                    ]
                },
            }
        ],
        "usageMetadata": {"promptTokenCount": 9, "candidatesTokenCount": 21},
    }
    completion = GeminiProvider._parse_response(body, latency_ms=5)
    assert completion.text == "running"
    assert completion.tool_calls[0].name == "run_command"
    assert json.loads(completion.tool_calls[0].arguments) == {"command": "id"}
    assert completion.usage.tokens_in == 9
    assert completion.usage.tokens_out == 21
    assert completion.finish_reason == "stop"


def test_gemini_complete_wire_format(monkeypatch):
    captured = {}

    class Response:
        status_code = 200

        def json(self):
            return {
                "candidates": [
                    {"finishReason": "STOP", "content": {"parts": [{"text": "pong"}]}}
                ],
                "usageMetadata": {"promptTokenCount": 2, "candidatesTokenCount": 2},
            }

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["body"] = json
        captured["headers"] = headers
        return Response()

    import requests as requests_module

    monkeypatch.setattr(requests_module, "post", fake_post)
    provider = GeminiProvider("gemini", api_key="g-key")
    completion = provider.complete(
        "gemini-3.8-flash",
        OPENAI_HISTORY,
        tools=[{"name": "run_command", "description": "run", "parameters": {"type": "object"}}],
        max_tokens=256,
        temperature=0.1,
    )
    assert completion.text == "pong"
    assert ":generateContent" in captured["url"]
    assert "gemini-3.8-flash" in captured["url"]
    assert captured["headers"]["x-goog-api-key"] == "g-key"
    assert captured["body"]["generationConfig"] == {"maxOutputTokens": 256, "temperature": 0.1}
    assert captured["body"]["systemInstruction"]["parts"][0]["text"] == "You are a red team engine."
    assert captured["body"]["tools"][0]["functionDeclarations"][0]["name"] == "run_command"


def test_anthropic_healthcheck_false_on_error(monkeypatch):
    provider = AnthropicProvider("claude", api_key="k")

    def fake_post(*a, **k):
        raise ProviderError("down")

    import requests as requests_module

    monkeypatch.setattr(requests_module, "post", fake_post)
    assert provider.healthcheck("claude-sonnet-5") is False


def test_gemini_healthcheck_false_on_error(monkeypatch):
    provider = GeminiProvider("gemini", api_key="k")

    def fake_post(*a, **k):
        raise ProviderError("down")

    import requests as requests_module

    monkeypatch.setattr(requests_module, "post", fake_post)
    assert provider.healthcheck("gemini-3.8-flash") is False
