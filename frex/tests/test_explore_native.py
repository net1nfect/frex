from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from frex.dispatcher.config import ModelConfig
from frex.dispatcher.providers.base import Completion, ToolCall, Usage
from frex.dispatcher.runtime.cancellation import TaskCancellation
from frex.dispatcher.runtime.tools import ToolExecutor


class FakeApiResult:
    def __init__(self, status_code=200, data=None):
        self.status_code = status_code
        self.data = data or {}

    @property
    def ok(self):
        return 200 <= self.status_code < 300


class FakeClient:
    def __init__(self):
        self.runs = []
        self.concludes = []
        self.events = []
        self.releases = []
        self.hints = []
        self.next_id = 1

    def _post(self, path, json=None):
        class R(FakeApiResult):
            pass

        if path.endswith("/runs"):
            run_id = f"run_{self.next_id:03d}"
            self.next_id += 1
            self.runs.append(json)
            return FakeApiResult(201, {"id": run_id})
        if "/conclude" in path:
            self.concludes.append(json)
            return FakeApiResult(200, {"fact": {"id": "f001"}})
        if "/events" in path:
            self.events.append(json)
            return FakeApiResult(201, {})
        if "/release" in path or path.endswith("/leases"):
            self.releases.append(path)
            return FakeApiResult(200)
        return FakeApiResult(200)

    # The dispatcher client surface used by the native task.
    def create_run(self, project_id, payload):
        self.runs.append(payload)
        run_id = f"run_{self.next_id:03d}"
        self.next_id += 1
        return FakeApiResult(201, {"id": run_id})

    def post_events(self, project_id, task_type, events):
        self.events.append((project_id, task_type, events))
        return FakeApiResult(201, {})

    def consume_hint_queue(self, project_id):
        return self.hints.pop(0) if self.hints else []

    def conclude(self, project_id, intent_id, worker, description, **kwargs):
        self.concludes.append({"description": description, **kwargs})
        return FakeApiResult(200, {"fact": {"id": "f001"}})

    def release(self, project_id, intent_id, worker):
        self.releases.append((project_id, intent_id, worker))
        return FakeApiResult(200)


class ScriptedProvider:
    def __init__(self, script):
        self.script = list(script)

    def complete(self, model, messages, *, tools=None, timeout_seconds=120, **kwargs):
        item = self.script.pop(0)
        return item

    def healthcheck(self, model):
        return True


class FakeContainers:
    def __init__(self, results=None):
        self.results = list(results or [])
        self.executed = []

    def ensure_running(self, project_id):
        return f"ctr-{project_id}"

    def build_exec_process(self, container_name, env, command, timeout_seconds=None):
        result = self.results.pop(0) if self.results else (0, "", "")

        class P:
            def start(self):
                pass

            def communicate(self, timeout=None):
                return SimpleNamespace(
                    returncode=result[0], stdout=result[1], stderr=result[2],
                    timed_out=False, cancelled=False, cancel_reason=None,
                )

        self.executed.append(command)
        return P()


class FakeRegistry:
    def __init__(self, provider, model):
        self._provider = provider
        self.model_config = ModelConfig(name="fast", provider="local", model=model)

    def get_provider(self, model_name):
        return self._provider

    def resolve(self, task_type):
        return self.model_config


class FakeLease:
    failure = None

    def start(self):
        pass

    def stop(self):
        pass

    def attach_process(self, process):
        pass

    @classmethod
    def for_intent(cls, *a, **k):
        return cls()


def make_project():
    return SimpleNamespace(
        project=SimpleNamespace(id="proj_001"),
        facts=[],
        hints=[],
    )


def make_intent():
    return SimpleNamespace(id="i001", description="scan 10.0.0.5")


def make_worker():
    return SimpleNamespace(name="kali-01", type="mock", env={})


def patch_common(monkeypatch):
    import frex.dispatcher.tasks.common as common
    import frex.dispatcher.tasks.explore_native as native

    monkeypatch.setattr(native, "HeartbeatLease", FakeLease)
    monkeypatch.setattr(common, "HeartbeatLease", FakeLease, raising=False)
    return native


def run_native(monkeypatch, script, containers, client=None):
    native = patch_common(monkeypatch)
    client = client or FakeClient()
    provider = ScriptedProvider(script)
    registry = FakeRegistry(provider, "qwen-local")

    # healthcheck passes
    containers.results.insert(0, (0, "ok", ""))
    outcome_value = native.run_native_explore_task(
        config=SimpleNamespace(
            runtime=SimpleNamespace(interval=5, healthcheck_timeout=5),
            tasks=SimpleNamespace(explore=SimpleNamespace(timeout=60, native=True)),
        ),
        client=client,
        container_manager=containers,
        registry=registry,
        project=make_project(),
        intent=make_intent(),
        worker=make_worker(),
        cancellation=TaskCancellation(),
    )
    return outcome_value, client


def test_native_explore_completes_and_concludes_with_run_id(monkeypatch):
    tool_call = Completion(
        text=None,
        tool_calls=[
            ToolCall(id="c0", name="run_command", arguments=json.dumps({"command": "nmap -sV 10.0.0.5"}))
        ],
        usage=Usage(tokens_in=100, tokens_out=20),
    )
    submit = Completion(
        text=None,
        tool_calls=[
            ToolCall(
                id="c1",
                name="submit_result",
                arguments=json.dumps({"result": {"description": "port 22 open on 10.0.0.5"}}),
            )
        ],
        usage=Usage(tokens_in=150, tokens_out=30),
    )
    containers = FakeContainers([(0, "22/tcp open", "")])
    status, client = run_native(monkeypatch, [tool_call, submit], containers)

    assert status == "success"
    # healthcheck run + agent-loop summary run
    assert len(client.runs) == 2
    loop_run = client.runs[-1]
    assert loop_run["tokens_in"] == 250
    assert loop_run["tokens_out"] == 50
    assert loop_run["phase"] == "execute"
    assert loop_run["model"] == "qwen-local"
    # conclude cites the loop run and carries the submitted description
    assert len(client.concludes) == 1
    assert client.concludes[0]["description"] == "port 22 open on 10.0.0.5"
    assert client.concludes[0]["run_id"] is not None
    # no release on success
    assert client.releases == []


def test_native_explore_budget_exhaustion_fails_and_releases(monkeypatch):
    from frex.dispatcher.providers.base import Completion as C
    from frex.dispatcher.providers.base import Usage as U

    looping = C(
        text=None,
        tool_calls=[ToolCall(id="c0", name="run_command", arguments=json.dumps({"command": "ls"}))],
        usage=U(tokens_in=10, tokens_out=5),
    )
    containers = FakeContainers([(0, "", "")] * 40)
    status, client = run_native(monkeypatch, [looping] * 40, containers)

    assert status == "failed"
    assert client.releases, "budget exhaustion must release the lease"
    assert client.concludes == []


def test_native_explore_cancellation(monkeypatch):
    cancellation = TaskCancellation()
    cancellation.cancel("operator")
    native = patch_common(monkeypatch)
    client = FakeClient()
    status = native.run_native_explore_task(
        config=SimpleNamespace(
            runtime=SimpleNamespace(interval=5, healthcheck_timeout=5),
            tasks=SimpleNamespace(explore=SimpleNamespace(timeout=60, native=True)),
        ),
        client=client,
        container_manager=FakeContainers(),
        registry=FakeRegistry(ScriptedProvider([]), "m"),
        project=make_project(),
        intent=make_intent(),
        worker=make_worker(),
        cancellation=cancellation,
    )
    assert status == "cancelled"
    assert client.releases


def test_native_explore_enforces_scope(monkeypatch):
    from frex.server.models import ScopeConfig

    tool_call = Completion(
        text=None,
        tool_calls=[
            ToolCall(id="c0", name="run_command", arguments=json.dumps({"command": "nmap evil.example.com"}))
        ],
        usage=Usage(tokens_in=10, tokens_out=5),
    )
    submit = Completion(
        text=None,
        tool_calls=[
            ToolCall(id="c1", name="submit_result", arguments=json.dumps({"result": {"description": "blocked"}}))
        ],
        usage=Usage(tokens_in=10, tokens_out=5),
    )
    containers = FakeContainers()  # no exec should run
    native = patch_common(monkeypatch)
    client = FakeClient()

    class RecordingProvider(ScriptedProvider):
        calls = []

        def complete(self, model, messages, **kwargs):
            RecordingProvider.calls.append(list(messages))
            return self.script.pop(0)

    provider = RecordingProvider([tool_call, submit])
    RecordingProvider.calls = []
    registry = FakeRegistry(provider, "m")

    project = make_project()
    project.project.scope = ScopeConfig(domains=["target.example.com"])

    project_id_unused = native.run_native_explore_task(
        config=SimpleNamespace(
            runtime=SimpleNamespace(interval=5, healthcheck_timeout=5),
            tasks=SimpleNamespace(explore=SimpleNamespace(timeout=60, native=True)),
        ),
        client=client,
        container_manager=containers,
        registry=registry,
        project=project,
        intent=make_intent(),
        worker=make_worker(),
        cancellation=TaskCancellation(),
    )
    assert project_id_unused == "success"
    # The out-of-scope command never reached the container — only the
    # healthcheck exec ran.
    assert len(containers.executed) == 1
    assert containers.executed[0][-1] == '{"phase":"healthcheck"}'
    assert not any("nmap" in " ".join(c) for c in containers.executed)
    tool_message = next(
        m for m in RecordingProvider.calls[1] if m.get("role") == "tool"
    )
    assert json.loads(tool_message["content"])["error"] == "scope"
