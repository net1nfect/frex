from __future__ import annotations

from cairn.dispatcher.config import WorkerConfig
from cairn.dispatcher.runtime.process import ProcessResult
from cairn.dispatcher.tasks.common import RunRecorder, run_status


class FakeClient:
    def __init__(self, status_code=201, data=None):
        self.status_code = status_code
        self.data = data if data is not None else {"id": "run_001"}
        self.payloads: list[dict] = []

    def create_run(self, project_id, payload):
        self.payloads.append((project_id, payload))
        from cairn.dispatcher.protocol.client import ApiResult

        return ApiResult(status_code=self.status_code, data=self.data)


def make_worker():
    return WorkerConfig(
        name="kali-01",
        type="mock",
        task_types=["explore"],
        max_running=1,
        priority=0,
    )


def make_recorder(client, **kwargs):
    return RunRecorder(
        client,
        "proj_001",
        kwargs.pop("task_type", "explore"),
        kwargs.pop("worker", make_worker()),
        **kwargs,
    )


def test_run_status_mapping():
    assert run_status(ProcessResult(returncode=0, stdout="", stderr="")) == "success"
    assert run_status(ProcessResult(returncode=1, stdout="", stderr="")) == "failed"
    assert run_status(
        ProcessResult(returncode=124, stdout="", stderr="", timed_out=True)
    ) == "timeout"
    assert run_status(
        ProcessResult(returncode=0, stdout="", stderr="", cancelled=True)
    ) == "cancelled"
    assert run_status(ProcessResult(returncode=137, stdout="", stderr="")) == "timeout"


def test_record_posts_payload_and_returns_run_id():
    client = FakeClient()
    recorder = make_recorder(client, intent_id="i003", command="nmap -sV host")
    run_id = recorder.record(
        ProcessResult(returncode=0, stdout="open", stderr="warn"),
        duration_ms=12400,
        phase="execute",
    )
    assert run_id == "run_001"
    project_id, payload = client.payloads[0]
    assert project_id == "proj_001"
    assert payload["task_type"] == "explore"
    assert payload["worker"] == "kali-01"
    assert payload["phase"] == "execute"
    assert payload["intent_id"] == "i003"
    assert payload["command"] == "nmap -sV host"
    assert payload["exit_code"] == 0
    assert payload["duration_ms"] == 12400
    assert payload["status"] == "success"
    assert payload["stdout"] == "open"
    assert payload["stderr"] == "warn"


def test_record_maps_timeout_and_cancelled():
    client = FakeClient()
    recorder = make_recorder(client)
    recorder.record(
        ProcessResult(returncode=124, stdout="", stderr="", timed_out=True),
        duration_ms=10,
        phase="execute",
    )
    recorder.record(
        ProcessResult(returncode=0, stdout="", stderr="", cancelled=True),
        duration_ms=10,
        phase="execute",
    )
    assert client.payloads[0][1]["status"] == "timeout"
    assert client.payloads[1][1]["status"] == "cancelled"


def test_record_truncates_oversized_output():
    client = FakeClient()
    recorder = make_recorder(client)
    big = "x" * (3 * 1024 * 1024)
    recorder.record(
        ProcessResult(returncode=0, stdout=big, stderr=""),
        duration_ms=1,
        phase="execute",
    )
    stdout = client.payloads[0][1]["stdout"]
    assert len(stdout.encode()) <= 2 * 1024 * 1024


def test_record_omits_empty_output():
    client = FakeClient()
    recorder = make_recorder(client)
    recorder.record(
        ProcessResult(returncode=0, stdout="", stderr=""),
        duration_ms=1,
        phase="execute",
    )
    payload = client.payloads[0][1]
    assert payload["stdout"] is None
    assert payload["stderr"] is None


def test_record_survives_server_error():
    client = FakeClient(status_code=500, data=None)
    recorder = make_recorder(client)
    assert recorder.record(
        ProcessResult(returncode=0, stdout="", stderr=""),
        duration_ms=1,
        phase="execute",
    ) is None


def test_record_handles_missing_run_id():
    client = FakeClient(data={"unexpected": True})
    recorder = make_recorder(client)
    assert recorder.record(
        ProcessResult(returncode=0, stdout="", stderr=""),
        duration_ms=1,
        phase="execute",
    ) is None
