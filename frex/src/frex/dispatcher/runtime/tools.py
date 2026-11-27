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
import time
from dataclasses import dataclass
from typing import Any, Callable

from frex.dispatcher.runtime.containers import ContainerManager

LOG = logging.getLogger(__name__)

MAX_TOOL_OUTPUT_CHARS = 64_000
DEFAULT_COMMAND_TIMEOUT_SECONDS = 120

# Structured tools produce small JSON on purpose: a compact result
# leaves more of the token budget for actual investigation steps.
DEFAULT_TOOL_OUTPUT_BUDGETS: dict[str, int] = {
    "http_probe": 4_000,
    "port_scan": 4_000,
    "nuclei_scan": 8_000,
    "screenshot": 500,
    "search_sploit": 6_000,
    "submit_facts": 2_000,
}

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
    {
        "name": "http_probe",
        "description": (
            "Structured HTTP probe: status, selected response headers, body size "
            "and a short body preview. Cheaper and cleaner than curl in run_command. "
            "Reports whether any supplied parameter values reflect in the body."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "method": {"type": "string", "enum": ["GET", "POST", "PUT", "DELETE", "HEAD", "OPTIONS"]},
                "headers": {"type": "object", "additionalProperties": {"type": "string"}},
                "body": {"type": "string"},
                "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 300},
                "max_body_chars": {"type": "integer", "minimum": 100, "maximum": 20000},
            },
            "required": ["url"],
        },
    },
    {
        "name": "port_scan",
        "description": (
            "TCP port scan via nmap from inside the container. Returns a compact "
            "list of open ports with service names. Use for targeted checks; "
            "full-range scans waste the operation budget."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "Host or IP to scan"},
                "ports": {
                    "type": "string",
                    "description": "nmap port spec (default: common web/db/remote ports)",
                },
                "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 3600},
            },
            "required": ["target"],
        },
    },
    {
        "name": "nuclei_scan",
        "description": (
            "Run nuclei templates against a target and return matching findings "
            "(id, severity, matched URL). Use a narrow severity/template filter "
            "first; broad scans are slow and noisy."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "target": {"type": "string"},
                "severity": {"type": "string", "description": "e.g. critical,high"},
                "templates": {"type": "string", "description": "nuclei template path or tag list (-t/-tags)"},
                "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 3600},
            },
            "required": ["target"],
        },
    },
    {
        "name": "screenshot",
        "description": (
            "Capture a headless-browser screenshot of a URL into the container "
            "filesystem. Returns the saved path and file size."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "output_path": {
                    "type": "string",
                    "description": "Container path for the PNG (default: /tmp/frex-shot-<ts>.png)",
                },
                "width": {"type": "integer", "minimum": 320, "maximum": 4096},
                "height": {"type": "integer", "minimum": 240, "maximum": 4096},
                "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 120},
            },
            "required": ["url"],
        },
    },
    {
        "name": "search_sploit",
        "description": (
            "Search the local exploit database (searchsploit) and return the "
            "top matching entries with their file paths."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 50},
            },
            "required": ["query"],
        },
    },
    {
        "name": "submit_facts",
        "description": (
            "Record findings as facts in the project graph (born verified from "
            "tool evidence). Use for concrete discoveries worth keeping; "
            "submit_result still ends the task."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "facts": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {
                            "description": {"type": "string"},
                            "kind": {"type": "string"},
                            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                        },
                        "required": ["description"],
                    },
                }
            },
            "required": ["facts"],
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


def _truncate(text: str, budget: int = MAX_TOOL_OUTPUT_CHARS) -> str:
    if len(text) <= budget:
        return text
    return text[:budget] + "\n...[truncated]"


class ToolExecutor:
    def __init__(
        self,
        container_manager: ContainerManager,
        container_name: str,
        *,
        command_timeout_seconds: int = DEFAULT_COMMAND_TIMEOUT_SECONDS,
        scope_checker: Callable[[str, dict[str, Any]], str | None] | None = None,
        # None means all tools available; a set restricts the surface.
        enabled_tools: set[str] | None = None,
        tool_output_budgets: dict[str, int] | None = None,
        # submit_facts handler: receives validated fact dicts, posts them
        # into the project graph. Without a sink the tool reports that
        # recording is unavailable instead of silently dropping findings.
        facts_sink: Callable[[list[dict[str, Any]]], int] | None = None,
    ):
        self._container_manager = container_manager
        self._container_name = container_name
        self._command_timeout = command_timeout_seconds
        # Optional guard: return a rejection message when a call falls
        # outside the operation's scope (e.g. destructive commands).
        self._scope_checker = scope_checker
        self._enabled_tools = enabled_tools
        self._budgets = dict(DEFAULT_TOOL_OUTPUT_BUDGETS)
        if tool_output_budgets:
            self._budgets.update(tool_output_budgets)
        self._facts_sink = facts_sink

    def _output(self, name: str, text: str, budget: int | None = None) -> str:
        return _truncate(text, budget or self._budgets.get(name, MAX_TOOL_OUTPUT_CHARS))

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
        if self._enabled_tools is not None and name not in self._enabled_tools:
            return ToolResult(
                ok=False,
                output={
                    "error": "tool_disabled",
                    "message": f"tool {name} is not enabled for this operation",
                },
            )
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

    # ------------------------------------------------------------------
    # Structured tools: small JSON outputs, executed in the container.

    def _tool_http_probe(self, args: dict[str, Any]) -> ToolResult:
        url = args.get("url")
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            return ToolResult(
                ok=False,
                output={"error": "invalid_arguments", "message": "url must be http(s)"},
            )
        method = args.get("method", "GET")
        if method not in ("GET", "POST", "PUT", "DELETE", "HEAD", "OPTIONS"):
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
        max_body = args.get("max_body_chars", 2000)
        if not isinstance(max_body, int) or max_body < 100:
            max_body = 2000

        # Deterministic output layout: headers file, then a status marker,
        # then the body — parsed with one regex, no ambiguity between
        # header block and body.
        header_file = "/tmp/frex-probe-h"
        body_file = "/tmp/frex-probe-b"
        status_file = "/tmp/frex-probe-s"
        script = (
            f"curl -sS -D {shlex.quote(header_file)} -o {shlex.quote(body_file)} "
            f"-w '%{{http_code}} %{{size_download}}' -X {shlex.quote(method)} "
            f"--max-time {str(timeout)} "
            + " ".join(f"-H {shlex.quote(f'{k}: {v}')}" for k, v in headers.items())
            + (f" --data-binary {shlex.quote(body)}" if body is not None and method in ("POST", "PUT", "DELETE") else "")
            + f" {shlex.quote(url)} > {shlex.quote(status_file)}; rc=$?; "
            f"cat {shlex.quote(header_file)} 2>/dev/null; "
            f"printf '__FREX_STATUS__'; cat {shlex.quote(status_file)} 2>/dev/null; printf '\\n'; "
            f"cat {shlex.quote(body_file)} 2>/dev/null; "
            f"rm -f {shlex.quote(header_file)} {shlex.quote(body_file)} {shlex.quote(status_file)}; exit $rc"
        )
        returncode, stdout, stderr = self._exec(["sh", "-c", script], timeout_seconds=timeout + 15)
        if returncode != 0 and "__FREX_STATUS__" not in stdout:
            return ToolResult(
                ok=False,
                output={"error": "request_failed", "message": self._output("http_probe", stderr) or f"curl exit {returncode}"},
            )

        status_code = 0
        body_size = 0
        header_block, _, response_body = stdout.partition("__FREX_STATUS__")
        marker = response_body.split("\n", 1)[0] if "__FREX_STATUS__" in stdout else ""
        import re

        match = re.match(r"(\d+)\s+(\d+)", marker)
        if match:
            status_code = int(match.group(1))
            body_size = int(match.group(2))
            response_body = response_body.split("\n", 1)[1] if "\n" in response_body else ""

        selected_headers: dict[str, str] = {}
        for line in header_block.splitlines():
            if ":" in line:
                key, _, value = line.partition(":")
                key_lower = key.strip().lower().replace("-", "_")
                if key_lower in ("server", "content_type", "location", "www_authenticate", "x_powered_by"):
                    selected_headers[key_lower] = value.strip()
                elif key_lower == "set_cookie":
                    selected_headers.setdefault("set_cookie_count", "0")
                    selected_headers["set_cookie_count"] = str(int(selected_headers["set_cookie_count"]) + 1)

        # Reflection markers: did any supplied value come back in the body?
        reflections = []
        candidates = [body] if body else []
        candidates.extend(str(v) for v in headers.values())
        for candidate in candidates:
            if len(candidate) > 3 and candidate in response_body and candidate not in reflections:
                reflections.append(candidate[:100])

        return ToolResult(
            ok=200 <= status_code < 400 if status_code else returncode == 0,
            output={
                "status_code": status_code,
                "headers": selected_headers,
                "body_length": body_size or len(response_body),
                "body_preview": self._output("http_probe", response_body.strip(), max_body),
                "reflections": reflections,
            },
        )

    def _tool_port_scan(self, args: dict[str, Any]) -> ToolResult:
        target = args.get("target")
        if not isinstance(target, str) or not target.strip():
            return ToolResult(
                ok=False, output={"error": "invalid_arguments", "message": "target is required"}
            )
        ports = args.get("ports", "21,22,25,53,80,110,111,135,139,443,445,993,995,1433,3306,3389,5432,5900,6379,8000-8100,8443,27017")
        if not isinstance(ports, str):
            ports = "80,443"
        timeout = args.get("timeout_seconds", 300)
        if not isinstance(timeout, int) or timeout < 1:
            timeout = 300
        returncode, stdout, stderr = self._exec(
            [
                "nmap", "-Pn", "-T4", "--open", "-oG", "-",
                "-p", ports, target,
            ],
            timeout_seconds=timeout,
        )
        if returncode == 127 or "not found" in stderr:
            return ToolResult(
                ok=False,
                output={"error": "unavailable", "message": "nmap is not installed in this container"},
            )
        if returncode != 0:
            return ToolResult(
                ok=False,
                output={"error": "scan_failed", "message": self._output("port_scan", stderr) or f"nmap exit {returncode}"},
            )
        open_ports = []
        for line in stdout.splitlines():
            if not line.startswith("Host:"):
                continue
            host_part, _, ports_part = line.partition("Ports:")
            host = host_part.replace("Host:", "").split("(")[0].strip()
            for entry in ports_part.split(","):
                fields = entry.strip().split("/")
                # Greppable format: 80/open/tcp//http///
                if len(fields) >= 5 and fields[1] == "open":
                    open_ports.append(
                        {"host": host, "port": int(fields[0]), "service": fields[4] or fields[2] or "unknown"}
                    )
        return ToolResult(
            ok=True,
            output={"target": target, "open_ports": open_ports, "raw": self._output("port_scan", stdout) if not open_ports else None},
        )

    def _tool_nuclei_scan(self, args: dict[str, Any]) -> ToolResult:
        target = args.get("target")
        if not isinstance(target, str) or not target.strip():
            return ToolResult(
                ok=False, output={"error": "invalid_arguments", "message": "target is required"}
            )
        timeout = args.get("timeout_seconds", 600)
        if not isinstance(timeout, int) or timeout < 1:
            timeout = 600
        argv = ["nuclei", "-u", target, "-json", "-silent", "-nc"]
        severity = args.get("severity")
        if isinstance(severity, str) and severity.strip():
            argv.extend(["-severity", severity.strip()])
        templates = args.get("templates")
        if isinstance(templates, str) and templates.strip():
            argv.extend(["-t", templates.strip()])
        returncode, stdout, stderr = self._exec(argv, timeout_seconds=timeout)
        if returncode == 127 or "not found" in stderr:
            return ToolResult(
                ok=False,
                output={"error": "unavailable", "message": "nuclei is not installed in this container"},
            )
        findings = []
        for line in stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            findings.append(
                {
                    "id": record.get("template-id") or record.get("id"),
                    "name": record.get("info", {}).get("name") if isinstance(record.get("info"), dict) else None,
                    "severity": (record.get("info", {}) or {}).get("severity") if isinstance(record.get("info"), dict) else None,
                    "host": record.get("host"),
                    "matched": record.get("matched-at") or record.get("matched"),
                }
            )
            if len(findings) >= 50:
                break
        return ToolResult(
            ok=True,
            output={
                "target": target,
                "finding_count": len(findings),
                "findings": findings,
                "stderr_excerpt": self._output("nuclei_scan", stderr) if stderr and not findings else None,
            },
        )

    def _tool_screenshot(self, args: dict[str, Any]) -> ToolResult:
        url = args.get("url")
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            return ToolResult(
                ok=False,
                output={"error": "invalid_arguments", "message": "url must be http(s)"},
            )
        output_path = args.get("output_path") or f"/tmp/frex-shot-{int(time.time())}.png"
        if not isinstance(output_path, str) or not output_path.startswith("/tmp/"):
            return ToolResult(
                ok=False,
                output={"error": "invalid_arguments", "message": "output_path must be under /tmp/"},
            )
        width = args.get("width", 1280)
        height = args.get("height", 800)
        if not isinstance(width, int) or not 320 <= width <= 4096:
            width = 1280
        if not isinstance(height, int) or not 240 <= height <= 4096:
            height = 800
        timeout = args.get("timeout_seconds", 60)
        if not isinstance(timeout, int) or timeout < 1:
            timeout = 60
        returncode, stdout, stderr = self._exec(
            [
                "sh", "-c",
                f"chromium --headless=new --disable-gpu --no-sandbox "
                f"--screenshot={shlex.quote(output_path)} "
                f"--window-size={width},{height} "
                f"--virtual-time-budget=5000 {shlex.quote(url)} "
                f"&& ls -l {shlex.quote(output_path)}",
            ],
            timeout_seconds=timeout,
        )
        if returncode == 127 or "not found" in stderr:
            return ToolResult(
                ok=False,
                output={"error": "unavailable", "message": "chromium is not installed in this container"},
            )
        if returncode != 0:
            return ToolResult(
                ok=False,
                output={"error": "screenshot_failed", "message": self._output("screenshot", stderr) or f"exit {returncode}"},
            )
        bytes_written = 0
        for token in stdout.split():
            if token.isdigit():
                bytes_written = int(token)
        return ToolResult(
            ok=True,
            output={"path": output_path, "bytes": bytes_written, "window": f"{width}x{height}"},
        )

    def _tool_search_sploit(self, args: dict[str, Any]) -> ToolResult:
        query = args.get("query")
        if not isinstance(query, str) or not query.strip():
            return ToolResult(
                ok=False, output={"error": "invalid_arguments", "message": "query is required"}
            )
        max_results = args.get("max_results", 10)
        if not isinstance(max_results, int) or not 1 <= max_results <= 50:
            max_results = 10
        returncode, stdout, stderr = self._exec(
            ["searchsploit", "-j", "--color", "-t", query],
            timeout_seconds=120,
        )
        if returncode == 127 or "not found" in stderr:
            return ToolResult(
                ok=False,
                output={"error": "unavailable", "message": "searchsploit is not installed in this container"},
            )
        if returncode != 0:
            return ToolResult(
                ok=False,
                output={"error": "search_failed", "message": self._output("search_sploit", stderr) or f"exit {returncode}"},
            )
        entries = []
        try:
            data = json.loads(stdout)
            for record in (data.get("RESULTS", {}) or {}).get("EXPLOIT", []) or data.get("RESULTS", {}).get("EXPLOITS", []):
                entries.append(
                    {
                        "id": record.get("ID"),
                        "title": record.get("Title"),
                        "path": record.get("Path"),
                    }
                )
                if len(entries) >= max_results:
                    break
        except ValueError:
            entries = []
        if not entries:
            return ToolResult(
                ok=True,
                output={"query": query, "results": [], "raw": self._output("search_sploit", stdout, 2000)},
            )
        return ToolResult(ok=True, output={"query": query, "results": entries})

    def _tool_submit_facts(self, args: dict[str, Any]) -> ToolResult:
        facts = args.get("facts")
        if not isinstance(facts, list) or not facts:
            return ToolResult(
                ok=False,
                output={"error": "invalid_arguments", "message": "facts must be a non-empty array"},
            )
        validated: list[dict[str, Any]] = []
        for fact in facts:
            if not isinstance(fact, dict) or not isinstance(fact.get("description"), str) or not fact["description"].strip():
                return ToolResult(
                    ok=False,
                    output={"error": "invalid_arguments", "message": "each fact needs a non-empty description"},
                )
            entry: dict[str, Any] = {"description": fact["description"]}
            if isinstance(fact.get("kind"), str) and fact["kind"]:
                entry["kind"] = fact["kind"]
            confidence = fact.get("confidence")
            if isinstance(confidence, (int, float)) and 0 <= confidence <= 1:
                entry["confidence"] = float(confidence)
            validated.append(entry)
        if self._facts_sink is None:
            return ToolResult(
                ok=False,
                output={"error": "unavailable", "message": "fact recording is not configured for this task"},
            )
        posted = self._facts_sink(validated)
        return ToolResult(ok=True, output={"submitted": posted, "total": len(validated)})
