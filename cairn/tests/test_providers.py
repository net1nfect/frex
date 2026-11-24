from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from cairn.dispatcher.config import (
    DispatchConfig,
    ModelConfig,
    ProviderConfig,
)
from cairn.dispatcher.providers import (
    Completion,
    NoModelAvailable,
    OpenAICompatProvider,
    ProviderError,
    ProviderRegistry,
)


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # silence test output
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        if self.path.endswith("/chat/completions"):
            if body.get("model") == "dead-model":
                self.send_response(503)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                json.dumps(
                    {
                        "model": body["model"],
                        "choices": [
                            {
                                "finish_reason": "stop",
                                "message": {
                                    "role": "assistant",
                                    "content": "pong",
                                },
                            }
                        ],
                        "usage": {"prompt_tokens": 5, "completion_tokens": 1},
                    }
                ).encode()
            )
        else:
            self.send_response(404)
            self.end_headers()


@pytest.fixture
def provider_server():
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/v1"
    server.shutdown()


def make_registry(provider_url, routing=None):
    config = DispatchConfig.model_construct(
        providers=[ProviderConfig(name="local", type="openai_compat", base_url=provider_url)],
        models=[
            ModelConfig(name="fast", provider="local", model="qwen-local"),
            ModelConfig(name="dead", provider="local", model="dead-model"),
        ],
        routing=routing or {"reason": ["fast"]},
    )
    return ProviderRegistry(config)


def test_registry_resolves_healthy_model(provider_server):
    registry = make_registry(provider_server)
    model = registry.resolve("reason")
    assert model.name == "fast"
    assert model.model == "qwen-local"


def test_registry_falls_through_unhealthy_model(provider_server):
    registry = make_registry(
        provider_server, routing={"reason": ["dead", "fast"]}
    )
    model = registry.resolve("reason")
    assert model.name == "fast"


def test_registry_raises_when_all_unhealthy(provider_server):
    registry = make_registry(provider_server, routing={"reason": ["dead"]})
    with pytest.raises(NoModelAvailable):
        registry.resolve("reason")


def test_registry_rejects_unknown_model_in_routing(provider_server):
    with pytest.raises(ProviderError, match="unknown model"):
        make_registry(provider_server, routing={"reason": ["nope"]})


def test_registry_rejects_unknown_provider_on_model():
    config = DispatchConfig.model_construct(
        providers=[],
        models=[ModelConfig(name="m", provider="ghost", model="x")],
        routing={},
    )
    with pytest.raises(ProviderError, match="unknown provider"):
        ProviderRegistry(config)


def test_dispatch_config_validates_provider_layer():
    base = {
        "server": "http://x",
        "runtime": {"max_workers": 1, "max_running_projects": 1, "max_project_workers": 1, "interval": 3, "healthcheck_timeout": 5, "prompt_group": "default"},
        "tasks": {"bootstrap": {"timeout": 1, "conclude_timeout": 1}, "reason": {"timeout": 1}, "explore": {"timeout": 1, "conclude_timeout": 1}},
        "container": {"image": "img", "network_mode": "bridge", "completed_action": "stop"},
        "workers": [{"name": "w", "type": "mock", "task_types": ["explore"], "max_running": 1, "priority": 0, "env": {}}],
    }
    # Legacy config without providers still valid.
    assert len(DispatchConfig.model_validate(base).models) == 0

    base["providers"] = [{"name": "p", "type": "openai_compat", "base_url": "http://x"}]
    base["models"] = [{"name": "m", "provider": "ghost", "model": "x"}]
    with pytest.raises(ValueError, match="unknown provider"):
        DispatchConfig.model_validate(base)

    base["models"] = [{"name": "m", "provider": "p", "model": "x"}]
    base["routing"] = {"reason": ["other"]}
    with pytest.raises(ValueError, match="unknown models"):
        DispatchConfig.model_validate(base)

    base["routing"] = {"reason": ["m"]}
    config = DispatchConfig.model_validate(base)
    assert config.routing["reason"] == ["m"]


def test_openai_compat_parses_completion_and_tool_calls(provider_server):
    provider = OpenAICompatProvider("local", provider_server)
    completion = provider.complete(
        "qwen-local", [{"role": "user", "content": "ping"}]
    )
    assert completion.text == "pong"
    assert completion.usage.tokens_in == 5
    assert completion.usage.tokens_out == 1
    assert completion.finish_reason == "stop"


def test_openai_compat_non_retriable_error_raises_immediately(monkeypatch):
    provider = OpenAICompatProvider("local", "http://127.0.0.1:1", max_retries=2)
    import requests

    def refuse(*a, **k):
        raise ProviderError("boom", status_code=400, retriable=False)

    monkeypatch.setattr(requests, "post", refuse)
    with pytest.raises(ProviderError):
        provider.complete("m", [{"role": "user", "content": "x"}], timeout_seconds=1)


def test_healthcheck_false_on_provider_error(monkeypatch):
    provider = OpenAICompatProvider("local", "http://127.0.0.1:1")

    def fail(*a, **k):
        raise ProviderError("down")

    monkeypatch.setattr(provider, "complete", fail)
    assert provider.healthcheck("m") is False


def test_healthcheck_true_on_ping(monkeypatch):
    provider = OpenAICompatProvider("local", "http://x")
    monkeypatch.setattr(
        provider,
        "complete",
        lambda *a, **k: Completion(text="pong"),
    )
    assert provider.healthcheck("m") is True


def test_health_cache_invalidation(provider_server):
    registry = make_registry(provider_server)
    assert registry.healthy("fast") is True
    assert registry._health["fast"] is True
    registry.invalidate_health("fast")
    assert "fast" not in registry._health
    registry.invalidate_health()
    assert registry._health == {}
