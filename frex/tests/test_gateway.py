from __future__ import annotations

import json
import threading
from http.server import HTTPServer

import pytest
import yaml

from frex.server import gateway
from test_providers import _Handler


@pytest.fixture
def provider_url():
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/v1"
    server.shutdown()


@pytest.fixture
def gateway_config(tmp_path, provider_url, monkeypatch):
    path = tmp_path / "gateway.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "providers": [
                    {"name": "p", "type": "openai_compat", "base_url": provider_url}
                ],
                "models": [
                    {"name": "fast", "provider": "p", "model": "qwen-local"},
                    {"name": "qwen-local", "provider": "p", "model": "qwen-local"},
                ],
                "routing": {"explore": ["fast"]},
            }
        )
    )
    monkeypatch.setenv(gateway.GATEWAY_CONFIG_ENV, str(path))
    # Force a reload per test even when the path is reused.
    gateway._state.registry = None
    gateway._state.config_path = None
    yield path
    gateway._state.registry = None
    gateway._state.config_path = None


def test_gateway_disabled_without_env(client, monkeypatch):
    monkeypatch.delenv(gateway.GATEWAY_CONFIG_ENV, raising=False)
    response = client.post(
        "/v1/chat/completions",
        json={"model": "fast", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 501


def test_gateway_models_list(client, gateway_config):
    response = client.get("/v1/models")
    assert response.status_code == 200
    ids = [m["id"] for m in response.json()["data"]]
    assert ids == ["fast", "qwen-local"]


def test_gateway_chat_completions_non_stream(client, gateway_config):
    response = client.post(
        "/v1/chat/completions",
        json={"model": "fast", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["content"] == "pong"
    assert body["usage"]["prompt_tokens"] == 5


def test_gateway_resolves_native_model_id(client, gateway_config):
    # "qwen-local" is not a config name; it resolves via the native id.
    response = client.post(
        "/v1/chat/completions",
        json={"model": "qwen-local", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 200, response.text


def test_gateway_unknown_model_404(client, gateway_config):
    response = client.post(
        "/v1/chat/completions",
        json={"model": "nope", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 404


def test_gateway_provider_error_502(client, tmp_path, monkeypatch):
    path = tmp_path / "gateway-dead.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "providers": [
                    {"name": "p", "type": "openai_compat", "base_url": "http://127.0.0.1:1/v1"}
                ],
                "models": [{"name": "fast", "provider": "p", "model": "m"}],
                "routing": {"explore": ["fast"]},
            }
        )
    )
    monkeypatch.setenv(gateway.GATEWAY_CONFIG_ENV, str(path))
    gateway._state.registry = None
    response = client.post(
        "/v1/chat/completions",
        json={"model": "fast", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 502
    gateway._state.registry = None


def test_gateway_tool_calls_round_trip(client, gateway_config):
    # The test handler echoes a tool call when asked with tools.
    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "fast",
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "run_command",
                        "description": "run",
                        "parameters": {"type": "object"},
                    },
                }
            ],
        },
    )
    assert response.status_code == 200, response.text
    message = response.json()["choices"][0]["message"]
    assert message["tool_calls"][0]["function"]["name"] == "run_command"


def test_gateway_stream_sse(client, gateway_config, monkeypatch):
    # The plain JSON test handler cannot answer a streaming request;
    # fake the wire with a proper SSE body instead.
    sse_body = (
        'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"po"}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"ng"}}]}\n\n'
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}],'
        '"usage":{"prompt_tokens":5,"completion_tokens":1}}\n\n'
        "data: [DONE]\n\n"
    )

    class FakeStreamResponse:
        status_code = 200
        text = sse_body

        def iter_lines(self, **kwargs):
            for line in sse_body.splitlines():
                yield line

        def close(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    import requests as requests_module

    def fake_post(url, json=None, timeout=None, stream=False, **kwargs):
        assert stream is True
        return FakeStreamResponse()

    monkeypatch.setattr(requests_module, "post", fake_post)
    with client.stream(
        "POST",
        "/v1/chat/completions",
        json={
            "model": "fast",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        },
    ) as response:
        assert response.status_code == 200
        chunks = [
            line[len("data: ") :]
            for line in response.iter_lines()
            if line.startswith("data: ")
        ]
    assert chunks[-1] == "[DONE]"
    events = [json.loads(c) for c in chunks[:-1]]
    assert events[0]["choices"][0]["delta"] == {"role": "assistant"}
    contents = [
        e["choices"][0]["delta"].get("content")
        for e in events[1:]
        if "content" in e["choices"][0]["delta"]
    ]
    assert "".join(c for c in contents if c) == "pong"
    final = events[-1]
    assert final["choices"][0]["finish_reason"] == "stop"
    assert final["usage"]["prompt_tokens"] == 5
