"""Operator CLI: frex run / status / steer / stop / report / models.

A thin layer over the HTTP API — everything here calls endpoints that
the UI and the dispatcher already use, so the CLI can never drift from
the protocol. Two modes:

- attach (default when --server is given): talk to a running server.
- managed (no --server): spawn `frex serve` and `frex dispatch` locally,
  run the watch loop, then shut both down.

Exit codes for `run`: 0 goal reached (project completed), 1 stopped or
failed, 2 watch timeout / budget window ended.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import click
import requests

TOKEN_ENV = "FREX_API_TOKEN"
DEFAULT_SERVER = "http://127.0.0.1:8000"

# Events too noisy to print by default in the watch loop.
QUIET_EVENT_KINDS = {"token.delta", "tool.output"}


# ---------------------------------------------------------------- helpers


def _headers() -> dict[str, str]:
    token = os.environ.get(TOKEN_ENV, "").strip()
    return {"Authorization": f"Bearer {token}"} if token else {}


def _request(
    method: str,
    url: str,
    *,
    ok: tuple[int, ...] = (200, 201, 204),
    **kwargs: Any,
) -> requests.Response:
    try:
        response = requests.request(method, url, timeout=30, headers=_headers(), **kwargs)
    except requests.RequestException as exc:
        raise click.ClickException(f"cannot reach Frex server ({url}): {exc}") from exc
    if response.status_code not in ok:
        detail = response.text[:300]
        raise click.ClickException(
            f"{method} {url} -> {response.status_code}: {detail}"
        )
    return response


def _get(server: str, path: str, **kwargs: Any) -> Any:
    return _request("GET", f"{server}{path}", **kwargs).json()


def _render_event(event: dict[str, Any], verbose: bool) -> str | None:
    """One rendered line per event, or None to skip."""
    kind = event.get("kind", "")
    data = event.get("data") or {}
    if kind == "fact.created":
        status = data.get("status", "?")
        fact_kind = data.get("kind", "?")
        color = "green" if status == "verified" else "white"
        return click.style(
            f"  fact [{fact_kind}/{status}] {str(data.get('description', ''))[:120]}",
            fg=color,
        )
    if kind == "tool.started":
        return click.style(f"  · tool {data.get('tool', '?')}", dim=True)
    if kind == "steering.received":
        return click.style(f"  steer: {data.get('hint', '')}", fg="yellow")
    if kind == "task.started":
        return click.style(
            f"  ▶ {data.get('task_type', 'task')} started", dim=True
        )
    if kind == "task.finished":
        return click.style(
            f"  ■ {data.get('task_type', 'task')} finished: {data.get('status', '?')}",
            dim=True,
        )
    if kind == "error":
        return click.style(f"  error: {data.get('message', '')}", fg="red")
    if verbose and kind == "tool.output":
        return click.style(
            f"    | {str(data.get('output', ''))[:160]!r}", dim=True
        )
    if kind in QUIET_EVENT_KINDS:
        return None
    return click.style(f"  {kind}", dim=True)


def _watch(
    server: str,
    project_id: str,
    *,
    poll_seconds: float,
    timeout_seconds: float,
    verbose: bool,
) -> int:
    """Poll events + status until the project settles. Returns exit code."""
    deadline = time.monotonic() + timeout_seconds
    since = 0
    while True:
        feed = _get(server, f"/projects/{project_id}/events", params={"since": since})
        for event in feed.get("events", []):
            since = max(since, int(event.get("seq", since)))
            line = _render_event(event, verbose)
            if line:
                click.echo(line)
        detail = _get(server, f"/projects/{project_id}")
        status = detail["project"]["status"]
        if status == "completed":
            click.echo(click.style(f"goal reached — project {project_id} completed", fg="green"))
            _print_facts(detail)
            return 0
        if status == "stopped":
            click.echo(click.style(f"project {project_id} stopped", fg="red"))
            _print_facts(detail)
            return 1
        if time.monotonic() > deadline:
            click.echo(
                click.style(
                    f"watch timeout after {int(timeout_seconds)}s — project still {status}",
                    fg="yellow",
                )
            )
            return 2
        time.sleep(poll_seconds)


def _print_facts(detail: dict[str, Any]) -> None:
    verified = [f for f in detail.get("facts", []) if f.get("status") == "verified"]
    click.echo(f"\nverified facts ({len(verified)}):")
    for fact in verified:
        click.echo(f"  [{fact.get('kind', '?')}] {fact.get('description', '')[:160]}")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_until_ready(server: str, timeout_seconds: float) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            if requests.get(f"{server}/capabilities", timeout=2).status_code == 200:
                return True
        except requests.RequestException:
            time.sleep(0.2)
    return False


def _build_scope(domains: tuple[str, ...], cidrs: tuple[str, ...], note: str) -> dict[str, Any] | None:
    if not domains and not cidrs and not note:
        return None
    scope: dict[str, Any] = {"notes": note}
    if domains:
        scope["domains"] = list(domains)
    if cidrs:
        scope["cidrs"] = list(cidrs)
    return scope


def _build_budget(max_usd: float | None, max_tokens: int | None, deadline: str | None) -> dict[str, Any] | None:
    if max_usd is None and max_tokens is None and deadline is None:
        return None
    budget: dict[str, Any] = {}
    if max_usd is not None:
        budget["max_usd"] = max_usd
    if max_tokens is not None:
        budget["max_tokens"] = max_tokens
    if deadline is not None:
        budget["deadline"] = deadline
    return budget


# ---------------------------------------------------------------- commands


def register(main_group: click.Group) -> None:
    @main_group.command()
    @click.option("--server", default=None, help="Attach to a running Frex server (default: managed mode)")
    @click.option("--target", required=True, help="Target URL or host — becomes the origin context")
    @click.option("--origin", default=None, help="Full origin context (default: derived from --target)")
    @click.option("--goal", required=True, help="Mission goal the engine must reach with evidence")
    @click.option("--title", default=None, help="Project title (default: derived from target)")
    @click.option("--domain", "domains", multiple=True, help="Scope: allowed domain (repeatable)")
    @click.option("--cidr", "cidrs", multiple=True, help="Scope: allowed CIDR (repeatable)")
    @click.option("--scope-note", default="", help="Free-form scope note")
    @click.option("--max-usd", type=float, default=None, help="Budget ceiling in USD")
    @click.option("--max-tokens", type=int, default=None, help="Budget ceiling in tokens")
    @click.option("--deadline", default=None, help="Operation deadline (ISO timestamp)")
    @click.option("--watch/--no-watch", default=True, show_default=True, help="Follow events until the project settles")
    @click.option("--poll-seconds", type=float, default=2.0, show_default=True)
    @click.option("--timeout", "timeout_seconds", type=float, default=1800.0, show_default=True, help="Watch timeout in seconds")
    @click.option("--verbose", is_flag=True, help="Also print tool output events")
    @click.option("--dispatch-config", type=click.Path(exists=True, dir_okay=False, path_type=Path), default=None, help="Dispatcher config for managed mode (spawn serve+dispatch)")
    def run(
        server: str | None,
        target: str,
        origin: str | None,
        goal: str,
        title: str | None,
        domains: tuple[str, ...],
        cidrs: tuple[str, ...],
        scope_note: str,
        max_usd: float | None,
        max_tokens: int | None,
        deadline: str | None,
        watch: bool,
        poll_seconds: float,
        timeout_seconds: float,
        verbose: bool,
        dispatch_config: Path | None,
    ):
        """Create a mission and (optionally) follow it to the goal."""
        managed = server is None
        processes: list[subprocess.Popen] = []
        if managed:
            if dispatch_config is None:
                raise click.ClickException(
                    "managed mode needs --dispatch-config (dispatcher config); "
                    "or pass --server to attach to a running server"
                )
            port = _free_port()
            server = f"http://127.0.0.1:{port}"
            click.echo(f"managed mode: starting server on {server}")
            processes.append(
                subprocess.Popen(
                    [sys.executable, "-m", "frex", "serve", "--port", str(port)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            )
            processes.append(
                subprocess.Popen(
                    [sys.executable, "-m", "frex", "dispatch", "--config", str(dispatch_config)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            )
        try:
            if managed and not _wait_until_ready(server, timeout_seconds=15):
                raise click.ClickException("managed server did not become ready")

            payload: dict[str, Any] = {
                "title": title or f"run {target}",
                "origin": origin or f"Target {target} is reachable.",
                "goal": goal,
            }
            scope = _build_scope(domains, cidrs, scope_note)
            if scope:
                payload["scope"] = scope
            budget = _build_budget(max_usd, max_tokens, deadline)
            if budget:
                payload["budget"] = budget

            project = _request("POST", f"{server}/projects", json=payload).json()
            project_id = project["project"]["id"]
            click.echo(f"project {project_id} created — origin & goal anchored")

            if not watch:
                click.echo(project_id)
                return
            code = _watch(
                server, project_id,
                poll_seconds=poll_seconds,
                timeout_seconds=timeout_seconds,
                verbose=verbose,
            )
            sys.exit(code)
        finally:
            for process in processes:
                process.terminate()
            for process in processes:
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()

    @main_group.command("status")
    @click.option("--server", default=DEFAULT_SERVER, show_default=True)
    def status(server: str):
        """List projects and their state."""
        projects = _get(server, "/projects")
        if not projects:
            click.echo("no projects")
            return
        for project in projects:
            click.echo(
                f"{project['id']}  {project['status']:<10} {project['title']}"
            )

    @main_group.command()
    @click.option("--server", default=DEFAULT_SERVER, show_default=True)
    @click.argument("project_id")
    @click.argument("hint")
    def steer(server: str, project_id: str, hint: str):
        """Send a steering hint to a running project."""
        _request(
            "POST",
            f"{server}/projects/{project_id}/hint-queue",
            json={"content": hint, "creator": "operator"},
        )
        click.echo(f"hint queued for {project_id}")

    @main_group.command()
    @click.option("--server", default=DEFAULT_SERVER, show_default=True)
    @click.argument("project_id")
    def stop(server: str, project_id: str):
        """Stop a running project."""
        _request(
            "PUT",
            f"{server}/projects/{project_id}/status",
            json={"status": "stopped"},
        )
        click.echo(f"project {project_id} stopped")

    @main_group.command()
    @click.option("--server", default=DEFAULT_SERVER, show_default=True)
    @click.option("--format", "fmt", type=click.Choice(["yaml", "timeline", "json"]), default="yaml", show_default=True)
    @click.argument("project_id")
    def report(server: str, fmt: str, project_id: str):
        """Export a project's fact graph."""
        if fmt == "json":
            detail = _get(server, f"/projects/{project_id}")
            click.echo(json.dumps(detail, indent=2))
            return
        response = _request(
            "GET", f"{server}/projects/{project_id}/export", params={"format": fmt}
        )
        click.echo(response.text)

    @main_group.command("models")
    @click.option("--server", default=DEFAULT_SERVER, show_default=True)
    def models(server: str):
        """List models exposed by the server's gateway."""
        try:
            data = _get(server, "/v1/models")
        except click.ClickException as exc:
            raise click.ClickException(
                f"{exc}\n(the gateway needs FREX_GATEWAY_CONFIG on the server)"
            ) from exc
        for model in data.get("data", []):
            click.echo(model["id"])
