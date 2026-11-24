from __future__ import annotations

import json

import pytest

from cairn.dispatcher.providers.base import (
    Completion,
    ProviderError,
    ToolCall,
    Usage,
)
from cairn.dispatcher.runtime.agent_loop import AgentBudget, AgentContext, AgentLoop
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.tools import ToolExecutor


class ScriptedProvider:
    """Returns canned completions; records the conversation."""

    def __init__(self, script):
        self.script = list(script)
        self.calls: list[list[dict]] = []

    def complete(self, model, messages, *, tools=None, timeout_seconds=120):
        self.calls.append([dict(m) for m in messages])
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def healthcheck(self, model):
        return True


class FakeContainers:
    """Serves canned exec results for the tool executor."""

    def __init__(self, results=None):
        self.results = list(results or [])
        self.executed = []

    def build_exec_process(self, container_name, env, command, timeout_seconds=None):
        exec_result = self.results.pop(0) if self.results else (0, "", "")

        class P:
            def start(self):
                pass

            def communicate(self, timeout=None):
                return FakeProcessResult(*exec_result)

        self.executed.append(command)
        return P()


class FakeProcessResult:
    def __init__(self, returncode, stdout, stderr):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.timed_out = False
        self.cancelled = False


def make_context(provider, executor, **kwargs):
    return AgentContext(
        provider=provider,
        model="test-model",
        executor=executor,
        **kwargs,
    )


def completion_with_tools(*calls):
    return Completion(
        text=None,
        tool_calls=[ToolCall(id=f"c{i}", name=n, arguments=json.dumps(a)) for i, (n, a) in enumerate(calls)],
        usage=Usage(tokens_in=10, tokens_out=5),
        finish_reason="tool_calls",
    )


def test_loop_runs_tools_until_submit():
    provider = ScriptedProvider(
        [
            completion_with_tools(("run_command", {"command": "nmap -sV"})),
            completion_with_tools(("submit_result", {"result": {"found": "port 22"}})),
        ]
    )
    containers = FakeContainers([(0, "22/tcp open", "")])
    executor = ToolExecutor(containers, "ctr")
    events = []
    outcome = AgentLoop(
        make_context(provider, executor, on_event=lambda k, d: events.append((k, d)))
    ).run("scan the box")

    assert outcome.status == "completed"
    assert outcome.result == {"found": "port 22"}
    assert outcome.steps == 2
    assert outcome.tokens_in == 20
    assert outcome.tool_calls == 2  # run_command + submit_result
    assert containers.executed[0] == ["sh", "-c", "nmap -sV"]
    assert ("task.started", {"model": "test-model"}) == events[0]
    assert events[-1][0] == "task.finished"
    # The tool result fed back into the conversation.
    second_call = provider.calls[1]
    assert any(m.get("role") == "tool" for m in second_call)


def test_loop_rejects_invalid_tool_arguments():
    provider = ScriptedProvider(
        [
            Completion(
                text=None,
                tool_calls=[ToolCall(id="c0", name="run_command", arguments="not json")],
                usage=Usage(tokens_in=1, tokens_out=1),
            ),
            completion_with_tools(("submit_result", {"result": {}})),
        ]
    )
    outcome = AgentLoop(make_context(provider, ToolExecutor(FakeContainers(), "c"))).run("x")
    assert outcome.status == "completed"
    tool_message = next(m for m in provider.calls[1] if m.get("role") == "tool")
    assert tool_message["content"]["error"] == "invalid_arguments"


def test_loop_unknown_tool_feeds_error_back():
    provider = ScriptedProvider(
        [
            completion_with_tools(("rm_rf", {})),
            completion_with_tools(("submit_result", {"result": {"ok": True}})),
        ]
    )
    outcome = AgentLoop(make_context(provider, ToolExecutor(FakeContainers(), "c"))).run("x")
    assert outcome.status == "completed"
    tool_message = next(m for m in provider.calls[1] if m.get("role") == "tool")
    assert tool_message["content"]["error"] == "unknown_tool"


def test_loop_scope_checker_blocks_calls():
    def deny_destructive(name, args):
        if name == "run_command" and "rm" in args.get("command", ""):
            return "destructive commands are out of scope"
        return None

    provider = ScriptedProvider(
        [
            completion_with_tools(("run_command", {"command": "rm -rf /"})),
            completion_with_tools(("submit_result", {"result": {"done": True}})),
        ]
    )
    executor = ToolExecutor(FakeContainers(), "c", scope_checker=deny_destructive)
    outcome = AgentLoop(make_context(provider, executor)).run("x")
    assert outcome.status == "completed"
    tool_message = next(m for m in provider.calls[1] if m.get("role") == "tool")
    assert tool_message["content"]["error"] == "scope"


def test_loop_max_steps_budget():
    provider = ScriptedProvider(
        [completion_with_tools(("run_command", {"command": "ls"}))] * 5
    )
    budget = AgentBudget(max_steps=3)
    outcome = AgentLoop(
        make_context(provider, ToolExecutor(FakeContainers([(0, "", "")] * 5), "c"), budget=budget)
    ).run("x")
    assert outcome.status == "budget_exhausted"
    assert outcome.steps == 3
    assert outcome.error == "max_steps reached"


def test_loop_token_budget():
    provider = ScriptedProvider([completion_with_tools(("run_command", {"command": "ls"}))])
    budget = AgentBudget(max_tokens=5)
    outcome = AgentLoop(
        make_context(provider, ToolExecutor(FakeContainers(), "c"), budget=budget)
    ).run("x")
    assert outcome.status == "budget_exhausted"
    assert outcome.error == "token budget reached"


def test_loop_usd_budget():
    provider = ScriptedProvider([completion_with_tools(("run_command", {"command": "ls"}))])
    budget = AgentBudget(max_usd=0.01)
    outcome = AgentLoop(
        make_context(
            provider,
            ToolExecutor(FakeContainers(), "c"),
            budget=budget,
            cost_estimator=lambda ti, to: 1.0,
        )
    ).run("x")
    assert outcome.status == "budget_exhausted"
    assert outcome.error == "cost budget reached"


def test_loop_provider_error():
    provider = ScriptedProvider([ProviderError("vllm down", retriable=False)])
    outcome = AgentLoop(make_context(provider, ToolExecutor(FakeContainers(), "c"))).run("x")
    assert outcome.status == "provider_error"
    assert "vllm down" in outcome.error


def test_loop_cancellation_between_steps():
    cancellation = TaskCancellation()

    class CancellingProvider(ScriptedProvider):
        def complete(self, model, messages, **kwargs):
            cancellation.cancel("operator")
            return super().complete(model, messages, **kwargs)

    provider = CancellingProvider([completion_with_tools(("run_command", {"command": "ls"}))])
    outcome = AgentLoop(
        make_context(
            provider, ToolExecutor(FakeContainers(), "c"), cancellation=cancellation
        )
    ).run("x")
    assert outcome.status == "cancelled"


def test_loop_injects_steering_hints_between_steps():
    provider = ScriptedProvider(
        [
            completion_with_tools(("run_command", {"command": "ls"})),
            completion_with_tools(("submit_result", {"result": {}})),
        ]
    )
    state = {"hints": ["focus on port 445"]}

    def fetch_hints():
        returned, state["hints"] = state["hints"][:], []
        return returned

    outcome = AgentLoop(
        make_context(
            provider,
            ToolExecutor(FakeContainers([(0, "", "")]), "c"),
            hint_fetcher=fetch_hints,
        )
    ).run("x")
    assert outcome.status == "completed"
    second_call = provider.calls[1]
    assert any(
        m.get("role") == "user" and "[steering] focus on port 445" in m.get("content", "")
        for m in second_call
    )


def test_loop_nudges_prose_response_toward_submit():
    provider = ScriptedProvider(
        [
            Completion(text="I found port 22 open.", usage=Usage(tokens_in=1, tokens_out=1)),
            completion_with_tools(("submit_result", {"result": {"port": 22}})),
        ]
    )
    outcome = AgentLoop(make_context(provider, ToolExecutor(FakeContainers(), "c"))).run("x")
    assert outcome.status == "completed"
    nudge = provider.calls[1][-1]
    assert nudge["role"] == "user"
    assert "submit_result" in nudge["content"]


def test_loop_tool_executor_writes_file():
    provider = ScriptedProvider(
        [
            completion_with_tools(("write_file", {"path": "/tmp/x", "content": "hello"})),
            completion_with_tools(("submit_result", {"result": {}})),
        ]
    )
    containers = FakeContainers([(0, "", "")])
    outcome = AgentLoop(make_context(provider, ToolExecutor(containers, "c"))).run("x")
    assert outcome.status == "completed"
    # write goes through base64 to survive arbitrary bytes
    assert containers.executed[0][0] == "sh"
    assert "base64" in containers.executed[0][2]


def test_event_publisher_batches_and_sends():
    from cairn.dispatcher.runtime.event_publisher import EventPublisher

    class EventClient:
        def __init__(self):
            self.batches = []

        def post_events(self, project_id, task_type, events):
            self.batches.append((project_id, task_type, events))
            from cairn.dispatcher.protocol.client import ApiResult

            return ApiResult(status_code=201, data={})

    client = EventClient()
    publisher = EventPublisher(client, "proj_001", "explore")
    for i in range(5):
        publisher.emit("tool.output", {"i": i})
    publisher.close()
    assert sum(len(b[2]) for b in client.batches) == 5
    assert all(b[0] == "proj_001" and b[1] == "explore" for b in client.batches)


def test_event_publisher_survives_server_error():
    from cairn.dispatcher.runtime.event_publisher import EventPublisher

    class FailingClient:
        def post_events(self, *a):
            from cairn.dispatcher.protocol.client import ApiResult

            return ApiResult(status_code=500)

    publisher = EventPublisher(FailingClient(), "proj_001", "explore")
    publisher.emit("task.started", {})
    publisher.close()  # must not raise


def test_loop_streams_tokens_when_enabled():
    class StreamingProvider:
        def __init__(self):
            self.kwargs = []
            self.calls = 0

        def complete(self, model, messages, *, tools=None, timeout_seconds=120, **kwargs):
            self.kwargs.append(kwargs)
            self.calls += 1
            if self.calls == 1:
                # Streaming path: token deltas already emitted by the
                # adapter via on_token; return the assembled completion.
                return completion_with_tools(
                    ("submit_result", {"result": {"ok": 1}})
                )
            raise AssertionError("loop should not retry after completion")

    provider = StreamingProvider()
    events = []
    outcome = AgentLoop(
        make_context(
            provider,
            ToolExecutor(FakeContainers(), "c"),
            stream_tokens=True,
            on_event=lambda k, d: events.append((k, d)),
        )
    ).run("x")
    assert outcome.status == "completed"
    assert provider.kwargs[0].get("stream") is True
    assert callable(provider.kwargs[0].get("on_token"))
    # on_token emits token.delta events through on_event.
    provider.kwargs[0]["on_token"]("hel")
    provider.kwargs[0]["on_token"]("lo")
    deltas = [d for k, d in events if k == "token.delta"]
    assert deltas == [{"text": "hel"}, {"text": "lo"}]


def test_loop_degrades_gracefully_when_adapter_lacks_stream():
    class LegacyProvider(ScriptedProvider):
        def complete(self, model, messages, *, tools=None, timeout_seconds=120):
            return super().complete(model, messages, tools=tools, timeout_seconds=timeout_seconds)

    provider = LegacyProvider(
        [completion_with_tools(("submit_result", {"result": {"ok": 1}}))]
    )
    outcome = AgentLoop(
        make_context(provider, ToolExecutor(FakeContainers(), "c"), stream_tokens=True)
    ).run("x")
    assert outcome.status == "completed"
