from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
import requests

IMAGE = "ghcr.io/oritera/cairn-worker-container:latest"


def _docker_ready() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        subprocess.run(
            ["docker", "info"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
        subprocess.run(
            ["docker", "image", "inspect", IMAGE],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except (subprocess.SubprocessError, OSError):
        return False
    return True


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("FREX_E2E") != "1",
        reason="set FREX_E2E=1 to run the container end-to-end test",
    ),
    pytest.mark.skipif(
        not _docker_ready(),
        reason=f"docker or {IMAGE} is not available",
    ),
]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_for_server(base_url: str, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            response = requests.get(f"{base_url}/projects", timeout=1)
            if response.status_code == 200:
                return
        except requests.RequestException:
            pass
        time.sleep(0.2)
    raise RuntimeError("frex server did not start")


def _write_dispatch_config(path: Path, base_url: str) -> None:
    path.write_text(
        f"""
server: "{base_url}"

runtime:
  interval: 1
  max_workers: 2
  max_running_projects: 1
  max_project_workers: 1
  healthcheck_timeout: 5
  prompt_group: "mock"

tasks:
  bootstrap:
    timeout: 30
    conclude_timeout: 15
  reason:
    timeout: 15
    max_intents: 2
  explore:
    timeout: 15
    conclude_timeout: 15

container:
  image: "{IMAGE}"
  network_mode: "host"
  completed_action: "stop"
  cap_add:
    - "NET_RAW"
    - "NET_ADMIN"

workers:
  - name: "mock-e2e"
    type: "mock"
    task_types: [bootstrap, reason, explore]
    max_running: 1
    priority: 0
""".lstrip(),
        encoding="utf-8",
    )


def test_mock_worker_completes_project_end_to_end(tmp_path):
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    db_path = tmp_path / "frex.db"
    config_path = tmp_path / "dispatch.yaml"
    _write_dispatch_config(config_path, base_url)

    server = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys; from frex.cli import main; sys.exit(main())",
            "serve",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--db-path",
            str(db_path),
            "--log-level",
            "warning",
            "--no-access-log",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    project_id = None
    try:
        _wait_for_server(base_url)
        created = requests.post(
            f"{base_url}/projects",
            json={"title": "e2e", "origin": "start", "goal": "finish"},
            timeout=5,
        )
        created.raise_for_status()
        project_id = created.json()["project"]["id"]

        dispatch = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; from frex.cli import main; sys.exit(main())",
                "dispatch",
                "--config",
                str(config_path),
                "--once",
                "--log-level",
                "warning",
            ],
            capture_output=True,
            text=True,
            timeout=180,
        )
        assert dispatch.returncode == 0, dispatch.stdout + dispatch.stderr

        project = requests.get(f"{base_url}/projects/{project_id}", timeout=5).json()
        assert project["project"]["status"] == "completed"
        assert len(project["facts"]) >= 3
        assert any(intent["to"] == "goal" for intent in project["intents"])
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
        if project_id is not None:
            subprocess.run(
                ["docker", "rm", "-f", f"frex-dispatch-{project_id}"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
