"""Tests for the operator CLI (frex run/status/steer/stop/report/models).

The CLI speaks real HTTP, so these tests run the actual ASGI app on a
real uvicorn server in a background thread — no transport mocks.
"""
from __future__ import annotations

import threading
import time
from http.server import HTTPServer

import pytest
import uvicorn
import yaml
from click.testing import CliRunner

from frex.cli import main
from frex.server import db
from frex.server.app import app


@pytest.fixture
def live_server(tmp_path, monkeypatch):
    """A real uvicorn server bound to an ephemeral port."""
    db.reset_for_tests()
    db.configure(tmp_path / "frex.db")
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    # Wait for the socket.
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    base = f"http://127.0.0.1:{config.port or server.servers[0].sockets[0].getsockname()[1]}"
    yield base
    server.should_exit = True
    thread.join(timeout=5)
    db.reset_for_tests()


@pytest.fixture
def runner():
    return CliRunner()


def _create_project(base, **overrides):
    import requests

    payload = {
        "title": "lab run",
        "origin": "DVWA at 127.0.0.1:8080",
        "goal": "prove code execution",
    }
    payload.update(overrides)
    response = requests.post(f"{base}/projects", json=payload)
    assert response.status_code == 201
    return response.json()["project"]["id"]


def test_run_no_watch_prints_project_id(runner, live_server):
    result = runner.invoke(
        main,
        ["run", "--server", live_server, "--target", "127.0.0.1:8080",
         "--goal", "prove code execution", "--no-watch"],
    )
    assert result.exit_code == 0, result.output
    assert result.output.strip().splitlines()[-1].startswith("proj_")


def test_run_creates_scope_and_budget(runner, live_server):
    import requests

    result = runner.invoke(
        main,
        ["run", "--server", live_server, "--target", "10.0.0.5",
         "--goal", "map the host", "--no-watch",
         "--domain", "example.test", "--cidr", "10.0.0.0/24",
         "--max-usd", "1.5", "--max-tokens", "50000"],
    )
    assert result.exit_code == 0, result.output
    project_id = result.output.strip().splitlines()[-1]
    detail = requests.get(f"{live_server}/projects/{project_id}").json()
    assert detail["project"]["scope"]["domains"] == ["example.test"]
    assert detail["project"]["scope"]["cidrs"] == ["10.0.0.0/24"]
    assert detail["project"]["budget"]["max_usd"] == 1.5
    assert detail["project"]["budget"]["max_tokens"] == 50000


def test_watch_exits_zero_on_completed_live(live_server):
    """Full path: a project completed through the goal-evidence endpoint
    makes the watch loop settle with exit code 0."""
    import requests

    project_id = _create_project(live_server)
    response = requests.post(
        f"{live_server}/projects/{project_id}/complete",
        json={
            "from": ["origin"],
            "description": "command execution proven with id output",
            "worker": "operator",
            "goal_evidence": [],
        },
    )
    assert response.status_code in (200, 201), response.text
    from frex.cli_run import _watch

    assert (
        _watch(live_server, project_id, poll_seconds=0.1, timeout_seconds=10, verbose=False)
        == 0
    )


def test_watch_exit_codes_mapping():
    from frex.cli_run import _watch

    # Pure unit: fake the HTTP layer through monkeypatched module funcs.
    import frex.cli_run as cli_run

    def fake_get(server, path, **kwargs):
        if path.endswith("/events"):
            return {"events": [], "last_seq": 0}
        status = "completed" if fake_get.calls >= 1 else "active"
        fake_get.calls += 1
        return {"project": {"status": status}, "facts": []}
    fake_get.calls = 0

    original = cli_run._get
    cli_run._get = fake_get
    try:
        assert _watch("http://x", "proj_1", poll_seconds=0, timeout_seconds=5, verbose=False) == 0
    finally:
        cli_run._get = original

    def fake_get_stopped(server, path, **kwargs):
        if path.endswith("/events"):
            return {"events": [], "last_seq": 0}
        return {"project": {"status": "stopped"}, "facts": []}
    cli_run._get = fake_get_stopped
    try:
        assert _watch("http://x", "proj_1", poll_seconds=0, timeout_seconds=5, verbose=False) == 1
    finally:
        cli_run._get = original

    def fake_get_timeout(server, path, **kwargs):
        if path.endswith("/events"):
            return {"events": [], "last_seq": 0}
        return {"project": {"status": "active"}, "facts": []}
    cli_run._get = fake_get_timeout
    try:
        assert _watch("http://x", "proj_1", poll_seconds=0, timeout_seconds=0.05, verbose=False) == 2
    finally:
        cli_run._get = original


def test_watch_renders_events_and_finishes(runner, live_server, monkeypatch):
    import requests

    project_id = _create_project(live_server)
    requests.post(
        f"{live_server}/projects/{project_id}/events",
        json={"task_type": "explore",
              "events": [
                  {"kind": "fact.created", "data": {"kind": "finding",
                   "status": "hypothesis", "description": "login page found"}},
                  {"kind": "error", "data": {"message": "boom"}},
              ]},
    )
    # Stop the project so the watch loop settles with exit code 1.
    requests.put(
        f"{live_server}/projects/{project_id}/status", json={"status": "stopped"}
    )

    # Run the watch directly against the live server.
    from frex.cli_run import _watch

    code = _watch(
        live_server, project_id, poll_seconds=0.1, timeout_seconds=10, verbose=False
    )
    assert code == 1


def test_status_lists_projects(runner, live_server):
    _create_project(live_server, title="alpha")
    _create_project(live_server, title="beta")
    result = runner.invoke(main, ["status", "--server", live_server])
    assert result.exit_code == 0, result.output
    assert "alpha" in result.output
    assert "beta" in result.output


def test_steer_queues_hint(runner, live_server):
    import requests

    project_id = _create_project(live_server)
    result = runner.invoke(
        main, ["steer", "--server", live_server, project_id, "check /admin"]
    )
    assert result.exit_code == 0, result.output
    queued = requests.get(f"{live_server}/projects/{project_id}/hint-queue").json()
    assert any(h["content"] == "check /admin" for h in queued["hints"])


def test_stop_stops_project(runner, live_server):
    import requests

    project_id = _create_project(live_server)
    result = runner.invoke(main, ["stop", "--server", live_server, project_id])
    assert result.exit_code == 0, result.output
    detail = requests.get(f"{live_server}/projects/{project_id}").json()
    assert detail["project"]["status"] == "stopped"


def test_report_formats(runner, live_server):
    project_id = _create_project(live_server)
    for fmt in ("yaml", "timeline", "json"):
        result = runner.invoke(
            main, ["report", "--server", live_server, "--format", fmt, project_id]
        )
        assert result.exit_code == 0, result.output
        assert result.output.strip()
    yaml_result = runner.invoke(
        main, ["report", "--server", live_server, "--format", "yaml", project_id]
    )
    parsed = yaml.safe_load(yaml_result.output)
    assert parsed["project"]["title"] == "lab run"
    assert parsed["project"]["goal"] == "prove code execution"
    json_result = runner.invoke(
        main, ["report", "--server", live_server, "--format", "json", project_id]
    )
    assert f'"id": "{project_id}"' in json_result.output


def test_models_reports_gateway_requirement(runner, live_server, monkeypatch):
    import frex.cli_run as cli_run

    def fake_get(server, path, **kwargs):
        import click

        raise click.ClickException("gateway is not configured")
    monkeypatch.setattr(cli_run, "_get", fake_get)
    result = runner.invoke(main, ["models", "--server", live_server])
    assert result.exit_code != 0
    assert "FREX_GATEWAY_CONFIG" in result.output


def test_models_lists_gateway_models(runner, live_server, monkeypatch):
    import frex.cli_run as cli_run

    monkeypatch.setattr(
        cli_run,
        "_get",
        lambda server, path, **k: {"data": [{"id": "fast"}, {"id": "smart"}]},
    )
    result = runner.invoke(main, ["models", "--server", live_server])
    assert result.exit_code == 0
    assert "fast" in result.output
    assert "smart" in result.output


def test_managed_mode_requires_dispatch_config(runner, monkeypatch):
    result = runner.invoke(
        main, ["run", "--target", "x", "--goal", "g", "--no-watch"]
    )
    assert result.exit_code != 0
    assert "--dispatch-config" in result.output


def test_server_unreachable_is_clean_error(runner):
    result = runner.invoke(
        main,
        ["run", "--server", "http://127.0.0.1:1", "--target", "x",
         "--goal", "g", "--no-watch"],
    )
    assert result.exit_code != 0
    assert "cannot reach Frex server" in result.output
