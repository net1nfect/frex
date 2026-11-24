"""Tool executor for the native agent runtime.

Every tool validates its arguments, checks scope, runs inside the
project container, and returns a structured result — the agent loop
never touches docker directly. Interactive sessions (reverse shells,
SSH) are deliberately out of scope for the MVP.
"""
from __future__ import annotations

import json
import logging
import shlex
from dataclasses import dataclass
from typing import Any, Callable

from cairn.dispatcher.runtime.containers import ContainerManager

LOG = logging.getLogger(__name__)

MAX_TOOL_OUTPUT_CHARS = 64_000
DEFAULT_COMMAND_TIMEOUT_SECONDS = 120

TOOLS_SPEC: list[dict[str, Any]] = [
    {
        "name": "run_command",
        "description": "Run a shell command inside the project container and return its output.",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to run"},
                "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 3600},
            },
            "required": ["command"],
        },
    },
    {
        "name": "read_file",
        "description": "Read a file inside the project container.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Write (or overwrite) a file inside the project container.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "http_request",
        "description": "Perform an HTTP request from inside the container (not from the dispatcher).",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "method": {"type": "string", "enum": ["GET", "POST", "PUT", "DELETE", "HEAD"]},
                "headers": {"type": "object", "additionalProperties": {"type": "string"}},
                "body": {"type": "string"},
                "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 300},
            },
            "required": ["url"],
        },
    },
    {
        "name": "submit_result",
        "description": "Finish the task with a structured JSON result. Call this when done.",
        "parameters": {
            "type": "object",
            "properties": {
                "result": {"type": "object", "description": "Task-specific result payload"}
            },
            "required": ["result"],
        },
    },
]


@dataclass(slots=True)
class ToolResult:
    ok: bool
    output: dict[str, Any]
    # submit_result terminates the loop; scope violations and unknown
    # tools do not (they feed the error back to the model).
    terminal: bool = False


def _truncate(text: str) -> str:
    if len(text) <= MAX_TOOL_OUTPUT_CHARS:
        return text
    return text[:MAX_TOOL_OUTPUT_CHARS] + "\n...[truncated]"


class ToolExecutor:
    def __init__(
        self,
        container_manager: ContainerManager,
        container_name: str,
        *,
        command_timeout_seconds: int = DEFAULT_COMMAND_TIMEOUT_SECONDS,
        scope_checker: Callable[[str, dict[str, Any]], str | None] | None = None,
    ):
        self._container_manager = container_manager
        self._container_name = container_name
        self._command_timeout = command_timeout_seconds
        # Optional guard: return a rejection message when a call falls
        # outside the operation's scope (e.g. destructive commands).
        self._scope_checker = scope_checker

    def _exec(self, argv: list[str], *, timeout_seconds: int | None) -> tuple[int, str, str]:
        process = self._container_manager.build_exec_process(
            self._container_name, {}, argv, timeout_seconds=timeout_seconds
        )
        process.start()
        result = process.communicate(timeout=timeout_seconds + 30 if timeout_seconds else None)
        return result.returncode, result.stdout, result.stderr

    def _check_scope(self, name: str, args: dict[str, Any]) -> ToolResult | None:
        if self._scope_checker is None:
            return None
        rejection = self._scope_checker(name, args)
        if rejection is not None:
            return ToolResult(ok=False, output={"error": "scope", "message": rejection})
        return None

    def run(self, name: str, arguments: str) -> ToolResult:
        try:
            args = json.loads(arguments) if arguments else {}
        except ValueError:
            return ToolResult(
                ok=False, output={"error": "invalid_arguments", "message": "arguments must be JSON"}
            )
        if not isinstance(args, dict):
            return ToolResult(
                ok=False, output={"error": "invalid_arguments", "message": "arguments must be an object"}
            )
        scope = self._check_scope(name, args)
        if scope is not None:
            return scope
        handler = getattr(self, f"_tool_{name}", None)
        if handler is None:
            return ToolResult(
                ok=False, output={"error": "unknown_tool", "message": f"no such tool: {name}"}
            )
        return handler(args)

    def _tool_run_command(self, args: dict[str, Any]) -> ToolResult:
        command = args.get("command")
        if not isinstance(command, str) or not command.strip():
            return ToolResult(
                ok=False, output={"error": "invalid_arguments", "message": "command is required"}
            )
        timeout = args.get("timeout_seconds", self._command_timeout)
        if not isinstance(timeout, int) or timeout < 1:
            timeout = self._command_timeout
        returncode, stdout, stderr = self._exec(
            ["sh", "-c", command], timeout_seconds=timeout
        )
        return ToolResult(
            ok=returncode == 0,
            output={
                "exit_code": returncode,
                "stdout": _truncate(stdout),
                "stderr": _truncate(stderr),
            },
        )

    def _tool_read_file(self, args: dict[str, Any]) -> ToolResult:
        path = args.get("path")
        if not isinstance(path, str) or not path:
            return ToolResult(
                ok=False, output={"error": "invalid_arguments", "message": "path is required"}
            )
        returncode, stdout, stderr = self._exec(["cat", "--", path], timeout_seconds=30)
        if returncode != 0:
            return ToolResult(
                ok=False,
                output={"error": "read_failed", "message": _truncate(stderr) or f"exit {returncode}"},
            )
        return ToolResult(ok=True, output={"path": path, "content": _truncate(stdout)})

    def _tool_write_file(self, args: dict[str, Any]) -> ToolResult:
        path = args.get("path")
        content = args.get("content")
        if not isinstance(path, str) or not path:
            return ToolResult(
                ok=False, output={"error": "invalid_arguments", "message": "path is required"}
            )
        if not isinstance(content, str):
            return ToolResult(
                ok=False, output={"error": "invalid_arguments", "message": "content must be a string"}
            )
        # Heredoc-free: pass content through stdin-free base64 to survive
        # arbitrary bytes without shell-escaping bugs.
        import base64

        encoded = base64.b64encode(content.encode("utf-8", errors="replace")).decode()
        script = f"printf %s {shlex.quote(encoded)} | base64 -d > {shlex.quote(path)}"
        returncode, _, stderr = self._exec(["sh", "-c", script], timeout_seconds=30)
        if returncode != 0:
            return ToolResult(
                ok=False,
                output={"error": "write_failed", "message": _truncate(stderr) or f"exit {returncode}"},
            )
        return ToolResult(ok=True, output={"path": path, "bytes_written": len(content.encode("utf-8"))})

    def _tool_http_request(self, args: dict[str, Any]) -> ToolResult:
        url = args.get("url")
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            return ToolResult(
                ok=False,
                output={"error": "invalid_arguments", "message": "url must be http(s)"},
            )
        method = args.get("method", "GET")
        if method not in ("GET", "POST", "PUT", "DELETE", "HEAD"):
            return ToolResult(
                ok=False, output={"error": "invalid_arguments", "message": f"bad method {method}"}
            )
        headers = args.get("headers") or {}
        if not isinstance(headers, dict):
            return ToolResult(
                ok=False, output={"error": "invalid_arguments", "message": "headers must be an object"}
            )
        body = args.get("body")
        if body is not None and not isinstance(body, str):
            return ToolResult(
                ok=False, output={"error": "invalid_arguments", "message": "body must be a string"}
            )
        timeout = args.get("timeout_seconds", 30)
        if not isinstance(timeout, int) or timeout < 1:
            timeout = 30

        curl_argv = [
            "curl", "-s", "-S", "-X", method, "--max-time", str(timeout),
            "-o", "-", "-w", "\n%{http_code}", url,
        ]
        for key, value in headers.items():
            curl_argv.extend(["-H", f"{key}: {value}"])
        if body is not None and method in ("POST", "PUT", "DELETE"):
            curl_argv.extend(["--data-binary", body])
        returncode, stdout, stderr = self._exec(curl_argv, timeout_seconds=timeout + 15)
        if returncode != 0:
            return ToolResult(
                ok=False,
                output={"error": "request_failed", "message": _truncate(stderr) or f"curl exit {returncode}"},
            )
        # Split the trailing status code that -w appends.
        payload, _, status = stdout.rpartition("\n")
        try:
            status_code = int(status.strip())
        except ValueError:
            payload, status_code = stdout, 0
        return ToolResult(
            ok=200 <= status_code < 300 if status_code else returncode == 0,
            output={
                "status_code": status_code,
                "body": _truncate(payload),
            },
        )

    def _tool_submit_result(self, args: dict[str, Any]) -> ToolResult:
        result = args.get("result")
        if not isinstance(result, dict):
            return ToolResult(
                ok=False,
                output={"error": "invalid_arguments", "message": "result must be an object"},
            )
        return ToolResult(ok=True, output={"result": result}, terminal=True)
