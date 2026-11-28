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
import uuid
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
    "subdomain_enum": 6_000,
    "content_discovery": 4_000,
    "tech_fingerprint": 1_500,
    "interactsh_generate": 500,
    "interactsh_poll": 2_000,
    "session_authenticate": 800,
    "crawl_site": 6_000,
    "submit_action_log": 500,
    "get_fact_detail": 2_000,
    "browser_session": 4_000,
}

# Structured-wordlist presets for content_discovery. Deliberately small
# defaults: a huge wordlist burns the operation budget for noise.
WORDLIST_PRESETS = {
    "common": "/usr/share/wordlists/dirb/common.txt",
    "small": "/usr/share/wordlists/dirb/small.txt",
    "big": "/usr/share/wordlists/dirb/big.txt",
}

# Known technologies → nuclei tags, so tech_fingerprint output narrows a
# following nuclei_scan instead of the model scanning 5000 templates.
TECH_TO_NUCLEI_TAGS: dict[str, list[str]] = {
    "wordpress": ["wordpress", "wp-plugin"],
    "joomla": ["joomla"],
    "drupal": ["drupal"],
    "php": ["php"],
    "nginx": ["nginx"],
    "apache": ["apache"],
    "tomcat": ["tomcat"],
    "iis": ["iis"],
    "express": ["express"],
    "django": ["django"],
    "flask": ["flask"],
    "rails": ["rails"],
    "spring": ["spring"],
    "gitlab": ["gitlab"],
    "jenkins": ["jenkins"],
}

SUBDOMAIN_CAP = 200
FINDING_CAP = 100

# Tools that change state on the target are flagged is_mutating and
# default to NOT auto-running (permission tier >= 2). The registry is
# the single source of truth for the gate's tier defaults.
TOOLS_SPEC: list[dict[str, Any]] = [
    {
        "name": "run_command",
        "is_mutating": False,
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
        "is_mutating": False,
        "description": "Read a file inside the project container.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "is_mutating": False,
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
        "is_mutating": False,
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
        "is_mutating": False,
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
        "is_mutating": False,
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
                "session_token": {"type": "string", "description": "From session_authenticate — test the authenticated surface"},
            },
            "required": ["url"],
        },
    },
    {
        "name": "port_scan",
        "is_mutating": False,
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
        "is_mutating": False,
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
                "tags": {"type": "string", "description": "nuclei tag filter (e.g. from tech_fingerprint suggested_nuclei_tags)"},
                "session_token": {"type": "string", "description": "From session_authenticate — many templates need authenticated requests"},
                "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 3600},
            },
            "required": ["target"],
        },
    },
    {
        "name": "screenshot",
        "is_mutating": False,
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
        "is_mutating": False,
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
        "is_mutating": False,
        "description": (
            "Record findings as facts in the project graph (born verified from "
            "tool evidence). Use for concrete discoveries worth keeping; "
            "submit_result still ends the task. Facts about the same service "
            "should share a host_key (host:port) so they correlate instead of "
            "duplicating."
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
                            "host_key": {
                                "type": "string",
                                "description": "Correlation key 'host:port' for facts about the same service",
                            },
                        },
                        "required": ["description"],
                    },
                }
            },
            "required": ["facts"],
        },
    },
    {
        "name": "subdomain_enum",
        "is_mutating": False,
        "description": (
            "Passive subdomain enumeration (subfinder, fallback amass). Returns "
            "deduplicated subdomains already re-filtered against the operation "
            "scope. Passive sources only — no brute-force by default."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "domain": {"type": "string", "description": "Root domain to enumerate"},
                "sources": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional whitelist of passive sources",
                },
                "timeout_seconds": {"type": "integer", "minimum": 5, "maximum": 600},
            },
            "required": ["domain"],
        },
    },
    {
        "name": "content_discovery",
        "is_mutating": False,
        "description": (
            "Directory/path discovery (ffuf, fallback gobuster) with a small "
            "preset wordlist and a polite rate limit. Use after http_probe to "
            "find paths a single request cannot reveal."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "wordlist": {
                    "type": "string",
                    "description": "Preset (common|small|big) or container path (default: common)",
                },
                "extensions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "e.g. ['php', 'bak']",
                },
                "rate_limit": {"type": "integer", "minimum": 1},
                "exclude_status": {
                    "type": "string",
                    "description": "Comma-separated status codes to filter (default: 404)",
                },
                "session_token": {"type": "string", "description": "From session_authenticate"},
                "timeout_seconds": {"type": "integer", "minimum": 10, "maximum": 3600},
            },
            "required": ["url"],
        },
    },
    {
        "name": "tech_fingerprint",
        "is_mutating": False,
        "description": (
            "Identify the technology stack of a URL (httpx -tech-detect, fallback "
            "whatweb). Returns technologies with suggested nuclei tags — use "
            "those tags to narrow a following nuclei_scan."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "session_token": {"type": "string", "description": "From session_authenticate"},
            },
            "required": ["url"],
        },
    },
    {
        "name": "interactsh_generate",
        "is_mutating": False,
        "description": (
            "Register an out-of-band interaction payload: returns a unique "
            "correlation id and payload URL to inject into tests (blind SSRF, "
            "blind XSS, blind RCE). Pair with interactsh_poll."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "interactsh_poll",
        "is_mutating": False,
        "description": (
            "Poll interactions for a correlation id from interactsh_generate. "
            "confirmed=true means the target actually reached the OOB server."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "correlation_id": {"type": "string"},
                "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 120},
            },
            "required": ["correlation_id"],
        },
    },
    {
        "name": "session_authenticate",
        "is_mutating": False,
        "description": (
            "Establish an authenticated session against a login endpoint. "
            "Returns a session_token; pass it to http_probe, content_discovery, "
            "nuclei_scan, tech_fingerprint or crawl_site to test the "
            "authenticated surface instead of the anonymous one. The login "
            "URL must be inside the operation scope."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Login endpoint (in scope)"},
                "method": {
                    "type": "string",
                    "enum": ["form", "header", "cookie"],
                    "description": (
                        "form: POST username/password fields and keep the "
                        "session cookies. header: static header (e.g. "
                        "Authorization). cookie: static Cookie value."
                    ),
                },
                "credentials": {
                    "type": "object",
                    "description": (
                        "form: {username, password, username_field?, password_field?}. "
                        "header: {name, value}. cookie: {value}."
                    ),
                },
                "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 120},
            },
            "required": ["url", "method", "credentials"],
        },
    },
    {
        "name": "crawl_site",
        "is_mutating": False,
        "description": (
            "Crawl a URL with katana including JavaScript endpoint extraction "
            "(-js-crawl): finds real routes and parameters of modern SPAs that "
            "wordlist fuzzing cannot. Feed the results to content_discovery or "
            "nuclei_scan."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "depth": {"type": "integer", "minimum": 1, "maximum": 10},
                "session_token": {
                    "type": "string",
                    "description": "From session_authenticate, to crawl the authenticated surface",
                },
                "timeout_seconds": {"type": "integer", "minimum": 10, "maximum": 1800},
            },
            "required": ["url"],
        },
    },
    {
        "name": "submit_action_log",
        "is_mutating": False,
        "description": (
            "Record an auditable action entry (tool, target, what happened) "
            "into the project graph. Append-only: entries are never edited. "
            "Use for actions an engagement client may ask about later."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "tool_name": {"type": "string"},
                "target": {"type": "string"},
                "result_summary": {"type": "string"},
            },
            "required": ["tool_name", "target", "result_summary"],
        },
    },
    {
        "name": "get_fact_detail",
        "is_mutating": False,
        "description": (
            "Retrieve the full record of one fact by id (listed in the "
            "state summary). Use instead of re-running a tool when you "
            "need older findings — raw tool output is not kept forever."
        ),
        "parameters": {
            "type": "object",
            "properties": {"fact_id": {"type": "string"}},
            "required": ["fact_id"],
        },
    },
]


class InteractshSession:
    """Out-of-band interaction session (interactsh-client wrapper).

    Stateless tools can't hold a correlation id between calls, so the
    executor keeps sessions alive in scratch state: generate registers
    one, poll looks it up. The default implementation shells out to
    interactsh-client; tests inject a fake via the factory.
    """

    def __init__(self, client_path: str = "interactsh-client"):
        import subprocess

        self._proc = subprocess.Popen(
            [client_path, "-n", "-json"],  # -n: no banner, json output
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        self._buffer: dict[str, list[dict[str, Any]]] = {}
        self._payload: tuple[str, str] | None = None

    def payload(self) -> tuple[str, str]:
        """Return (correlation_id, payload_url) for this session."""
        import time

        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            line = self._proc.stdout.readline()  # type: ignore[union-attr]
            if not line:
                time.sleep(0.1)
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            url = record.get("match") or record.get("payload") or ""
            correlation_id = record.get("correlation_id") or ""
            if url and correlation_id:
                self._payload = (correlation_id, url)
                return self._payload
        raise RuntimeError("interactsh-client did not report a payload URL")

    def poll(self, correlation_id: str, *, timeout_seconds: int = 10) -> list[dict[str, Any]]:
        """Drain buffered interactions for this correlation id."""
        import time

        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            line = self._proc.stdout.readline() if self._proc.stdout else ""
            if line:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if record.get("correlation_id") == correlation_id:
                    self._buffer.setdefault(correlation_id, []).append(
                        {
                            "protocol": record.get("protocol", "dns"),
                            "timestamp": record.get("timestamp", ""),
                            "remote_ip": record.get("remote-address", record.get("remote_address", "")),
                        }
                    )
            else:
                time.sleep(0.1)
        return self._buffer.get(correlation_id, [])

    def close(self):
        self._proc.terminate()


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
        # Re-filters host lists (e.g. subdomain_enum results) against the
        # operation scope. Results from passive sources can include hosts
        # outside the assigned scope; they must not reach later tools.
        host_filter: Callable[[list[str]], list[str]] | None = None,
        # Rate-limit ceiling for fuzzing tools: the model can request a
        # rate, never exceed this.
        max_rate_limit: int = 100,
        # interactsh session factory; injectable for tests. None makes
        # the interactsh tools report unavailable.
        interactsh_factory: "Callable[[], InteractshSession] | None" = None,
        # submit_action_log sink: receives one dict per audited action
        # (tool_name, target, result_summary, timestamp). Defaults to
        # routing through the facts sink as kind=action_log.
        action_log_sink: Callable[[dict[str, Any]], int] | None = None,
        # get_fact_detail lookup: fact_id -> full record. None makes
        # the tool report unavailability.
        facts_lookup: Callable[[str], dict[str, Any] | None] | None = None,
        # Skill runner for the run_skill meta tool. None -> unavailable.
        skill_runner: Any | None = None,
        # Browser session manager for browser_session. None -> unavailable.
        browser_manager: Any | None = None,
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
        self._host_filter = host_filter
        self._max_rate_limit = max(1, max_rate_limit)
        self._interactsh_factory = interactsh_factory
        self._interactsh_sessions: dict[str, InteractshSession] = {}
        # Authenticated sessions established by session_authenticate,
        # keyed by session token. Scratch state for the executor's
        # lifetime; other tools accept a token and reuse its headers.
        self._sessions: dict[str, dict[str, Any]] = {}
        # get_fact_detail lookup: fact_id -> full record. None makes
        # the tool report unavailability.
        self._facts_lookup = facts_lookup
        self._skill_runner = skill_runner
        self._browser_manager = browser_manager
        self._action_log_sink = action_log_sink or (
            (lambda entry: facts_sink([entry]) if facts_sink else 0)
        )

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
        session_flags, session_error = self._session_curl_flags(args.get("session_token"))
        if session_error:
            return ToolResult(ok=False, output={"error": "invalid_arguments", "message": session_error})
        script = (
            f"curl -sS -D {shlex.quote(header_file)} -o {shlex.quote(body_file)}"
            + session_flags + " "
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
        tags = args.get("tags")
        if isinstance(tags, str) and tags.strip():
            argv.extend(["-tags", tags.strip()])
        session = (
            self._sessions.get(str(args.get("session_token")))
            if args.get("session_token") else None
        )
        if session:
            for key, value in session["headers"].items():
                argv.extend(["-H", f"{key}: {value}"])
            if session.get("cookie_jar"):
                argv.extend(["-H", f"Cookie: $(cat {session['cookie_jar']})"])
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

    def _tool_subdomain_enum(self, args: dict[str, Any]) -> ToolResult:
        domain = args.get("domain")
        if not isinstance(domain, str) or not domain.strip():
            return ToolResult(
                ok=False, output={"error": "invalid_arguments", "message": "domain is required"}
            )
        domain = domain.strip().lower()
        timeout = args.get("timeout_seconds", 30)
        if not isinstance(timeout, int) or timeout < 5:
            timeout = 30
        sources = args.get("sources")
        sources_arg: list[str] = []
        if isinstance(sources, list) and sources:
            sources_arg = [str(s) for s in sources if isinstance(s, str) and s.strip()]

        # Primary: subfinder (passive, fast). Fallback: amass passive.
        subfinder_argv = ["subfinder", "-d", domain, "-json", "-silent"]
        if sources_arg:
            subfinder_argv.extend(["-sources", ",".join(sources_arg)])
        returncode, stdout, stderr = self._exec(subfinder_argv, timeout_seconds=timeout)
        hosts: list[str] = []
        binary = "subfinder"
        if returncode == 127 or "not found" in stderr:
            binary = "amass"
            amass_argv = ["amass", "enum", "-passive", "-d", domain]
            if sources_arg:
                amass_argv.extend(["-passive-srcs", ",".join(sources_arg)])
            returncode, stdout, stderr = self._exec(amass_argv, timeout_seconds=timeout)
            if returncode == 127 or "not found" in stderr:
                return ToolResult(
                    ok=False,
                    output={"error": "unavailable", "message": "neither subfinder nor amass is installed in this container"},
                )
            if returncode != 0:
                return ToolResult(
                    ok=False,
                    output={"error": "enum_failed", "message": self._output("subdomain_enum", stderr) or f"amass exit {returncode}"},
                )
            hosts = [line.strip().lower() for line in stdout.splitlines() if line.strip() and "." in line]
        else:
            if returncode != 0:
                return ToolResult(
                    ok=False,
                    output={"error": "enum_failed", "message": self._output("subdomain_enum", stderr) or f"subfinder exit {returncode}"},
                )
            for line in stdout.splitlines():
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                host = record.get("host")
                if isinstance(host, str) and host:
                    hosts.append(host.lower())

        # Re-scope: passive sources can surface hosts outside the assigned
        # scope (old acquisitions, related domains). Never hand those to
        # later tools unchecked.
        if self._host_filter is not None:
            hosts = self._host_filter(hosts)
        unique = sorted(set(hosts))
        truncated = len(unique) > SUBDOMAIN_CAP
        return ToolResult(
            ok=True,
            output={
                "domain": domain,
                "subdomains": unique[:SUBDOMAIN_CAP],
                "count": len(unique),
                "truncated": truncated,
                "resolver": binary,
            },
        )

    def _resolve_wordlist(self, wordlist: Any) -> tuple[str | None, str | None]:
        """Return (container_path, error_message)."""
        if not isinstance(wordlist, str) or not wordlist.strip():
            wordlist = "common"
        wordlist = wordlist.strip()
        path = WORDLIST_PRESETS.get(wordlist, wordlist)
        if not path.startswith("/"):
            return None, f"unknown wordlist preset {wordlist!r}; presets: {', '.join(WORDLIST_PRESETS)} or an absolute container path"
        return path, None

    def _tool_content_discovery(self, args: dict[str, Any]) -> ToolResult:
        url = args.get("url")
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            return ToolResult(
                ok=False,
                output={"error": "invalid_arguments", "message": "url must be http(s)"},
            )
        if "FUZZ" not in url:
            url = url.rstrip("/") + "/FUZZ"
        extensions = args.get("extensions") or []
        if not isinstance(extensions, list):
            extensions = []
        extensions = [str(e).lstrip(".").strip() for e in extensions if str(e).strip()]
        exclude_status = args.get("exclude_status", "404")
        if not isinstance(exclude_status, str) or not exclude_status.strip():
            exclude_status = "404"
        timeout = args.get("timeout_seconds", 600)
        if not isinstance(timeout, int) or timeout < 10:
            timeout = 600
        # Rate limit: the model may request one; the config ceiling wins.
        rate = args.get("rate_limit", 50)
        if not isinstance(rate, int) or rate < 1:
            rate = 50
        rate = min(rate, self._max_rate_limit)

        wordlist_path, error = self._resolve_wordlist(args.get("wordlist"))
        if error:
            return ToolResult(ok=False, output={"error": "invalid_arguments", "message": error})

        # Pre-check the wordlist exists and count its words: an actionable
        # error here beats an ffuf stacktrace, and the word count is the
        # honest request total.
        check_rc, check_out, _ = self._exec(
            ["sh", "-c", f"test -f {shlex.quote(wordlist_path)} && wc -l < {shlex.quote(wordlist_path)}"],
            timeout_seconds=10,
        )
        if check_rc != 0:
            return ToolResult(
                ok=False,
                output={
                    "error": "wordlist_missing",
                    "message": f"wordlist not found in container: {wordlist_path} (presets: {', '.join(WORDLIST_PRESETS)})",
                },
            )
        try:
            total_requests = int(check_out.strip() or 0) * (1 + len(extensions))
        except ValueError:
            total_requests = 0

        ffuf_argv = [
            "ffuf", "-u", url, "-w", wordlist_path, "-json", "-s",
            "-rate-limit", str(rate), "-t", str(min(rate, 50)),
            "-fc", exclude_status,
        ]
        for ext in extensions:
            ffuf_argv.extend(["-e", ext])
        fuzz_session = (
            self._sessions.get(str(args.get("session_token")))
            if args.get("session_token") else None
        )
        if fuzz_session:
            for key, value in fuzz_session["headers"].items():
                ffuf_argv.extend(["-H", f"{key}: {value}"])
            if fuzz_session.get("cookie_jar"):
                ffuf_argv.extend(["-H", f"Cookie: $(cat {fuzz_session['cookie_jar']})"])
        returncode, stdout, stderr = self._exec(ffuf_argv, timeout_seconds=timeout)
        if returncode == 127 or "not found" in stderr:
            # Fallback: gobuster.
            base_url = url.replace("FUZZ", "WORD")  # gobuster has no keyword
            gobuster_argv = [
                "gobuster", "dir", "-u", base_url, "-w", wordlist_path,
                "-t", str(min(rate, 50)), "-q", "--no-error",
                "-b", exclude_status,
            ]
            for ext in extensions:
                gobuster_argv.extend(["-x", ext])
            returncode, stdout, stderr = self._exec(gobuster_argv, timeout_seconds=timeout)
            if returncode == 127 or "not found" in stderr:
                return ToolResult(
                    ok=False,
                    output={"error": "unavailable", "message": "neither ffuf nor gobuster is installed in this container"},
                )
            if returncode != 0:
                return ToolResult(
                    ok=False,
                    output={"error": "fuzz_failed", "message": self._output("content_discovery", stderr) or f"gobuster exit {returncode}"},
                )
            found = []
            import re as _re

            for line in stdout.splitlines():
                match = _re.match(r"(/\S*)\s*\(Status:\s*(\d+)\)\s*\[Size:\s*(\d+)\]", line.strip())
                if match:
                    found.append({"path": match.group(1), "status": int(match.group(2)), "length": int(match.group(3))})
        else:
            if returncode != 0 and not stdout.strip():
                return ToolResult(
                    ok=False,
                    output={"error": "fuzz_failed", "message": self._output("content_discovery", stderr) or f"ffuf exit {returncode}"},
                )
            found = []
            for line in stdout.splitlines():
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                inputs = record.get("input") or {}
                fuzz = inputs.get("FUZZ") or ""
                found.append(
                    {
                        "path": "/" + fuzz if not fuzz.startswith("/") else fuzz,
                        "status": record.get("status"),
                        "length": record.get("length"),
                    }
                )
        truncated = len(found) > FINDING_CAP
        return ToolResult(
            ok=True,
            output={
                "url": url,
                "found": found[:FINDING_CAP],
                "total_requests": total_requests,
                "truncated": truncated,
                "rate_limit": rate,
                "wordlist": wordlist_path,
            },
        )

    def _tool_tech_fingerprint(self, args: dict[str, Any]) -> ToolResult:
        url = args.get("url")
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            return ToolResult(
                ok=False,
                output={"error": "invalid_arguments", "message": "url must be http(s)"},
            )
        returncode, stdout, stderr = self._exec(
            ["httpx", "-u", url, "-tech-detect", "-status-code", "-json", "-silent"]
            + (
                [
                    ["-H", f"{k}: {v}"]
                    for k, v in (
                        self._sessions.get(str(args.get("session_token"))) or {}
                    ).get("headers", {}).items()
                ]
                if args.get("session_token") else []
            ),
            timeout_seconds=90,
        )
        technologies: list[dict[str, Any]] = []
        server_header: str | None = None
        waf_hint: str | None = None
        if returncode == 127 or "not found" in stderr:
            # Fallback: whatweb plain output "url [200] Apache[2.4], PHP[7.4]".
            returncode, stdout, stderr = self._exec(
                ["whatweb", "-q", url], timeout_seconds=90
            )
            if returncode == 127 or "not found" in stderr:
                return ToolResult(
                    ok=False,
                    output={"error": "unavailable", "message": "neither httpx nor whatweb is installed in this container"},
                )
            import re as _re

            for name, version in _re.findall(r"([A-Za-z0-9._-]+)(?:\[([^\]]*)\])?", stdout):
                if not name or name == url:
                    continue
                clean_version = version if version and version.replace(".", "").isdigit() else None
                technologies.append({"name": name, "version": clean_version, "category": "detected"})
        else:
            for line in stdout.splitlines():
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                server_header = record.get("webserver") or server_header
                for tech in record.get("tech") or []:
                    technologies.append({"name": str(tech), "version": None, "category": "detected"})
                waf_hint = record.get("waf") or waf_hint
                if technologies:
                    break

        tags: list[str] = []
        for tech in technologies:
            for key, mapped in TECH_TO_NUCLEI_TAGS.items():
                if key in tech["name"].lower():
                    tags.extend(t for t in mapped if t not in tags)
        return ToolResult(
            ok=True,
            output={
                "url": url,
                "technologies": technologies[:20],
                "server_header": server_header,
                "waf_hint": waf_hint,
                "suggested_nuclei_tags": tags,
            },
        )

    def _tool_interactsh_generate(self, args: dict[str, Any]) -> ToolResult:
        del args
        if self._interactsh_factory is None:
            return ToolResult(
                ok=False,
                output={"error": "unavailable", "message": "interactsh is not configured for this deployment"},
            )
        try:
            session = self._interactsh_factory()
        except FileNotFoundError:
            return ToolResult(
                ok=False,
                output={"error": "unavailable", "message": "interactsh-client is not installed in this container"},
            )
        except Exception as exc:  # registration failure
            return ToolResult(
                ok=False,
                output={"error": "interactsh_failed", "message": self._output("interactsh_generate", str(exc))},
            )
        correlation_id, payload_url = session.payload()
        self._interactsh_sessions[correlation_id] = session
        return ToolResult(
            ok=True,
            output={"correlation_id": correlation_id, "payload_url": payload_url},
        )

    def _tool_interactsh_poll(self, args: dict[str, Any]) -> ToolResult:
        correlation_id = args.get("correlation_id")
        if not isinstance(correlation_id, str) or not correlation_id.strip():
            return ToolResult(
                ok=False, output={"error": "invalid_arguments", "message": "correlation_id is required"}
            )
        session = self._interactsh_sessions.get(correlation_id.strip())
        if session is None:
            return ToolResult(
                ok=False,
                output={"error": "invalid_arguments", "message": f"unknown correlation_id {correlation_id!r} — call interactsh_generate first"},
            )
        timeout = args.get("timeout_seconds", 10)
        if not isinstance(timeout, int) or timeout < 1:
            timeout = 10
        interactions = session.poll(correlation_id, timeout_seconds=timeout)
        return ToolResult(
            ok=True,
            output={
                "correlation_id": correlation_id,
                "interactions": interactions,
                "confirmed": bool(interactions),
            },
        )

    def _apply_session(
        self, headers: dict[str, str], session_token: Any
    ) -> tuple[dict[str, str], str | None]:
        """Merge an authenticated session's headers into the request.

        Returns (headers, error). An unknown token is an actionable
        error, not a silent anonymous request — the model must know the
        authenticated surface was NOT tested.
        """
        if session_token is None:
            return headers, None
        session = self._sessions.get(str(session_token))
        if session is None:
            return headers, (
                f"unknown session_token {session_token!r} — call session_authenticate first"
            )
        merged = dict(session["headers"])
        merged.update(headers)
        return merged, None

    def _tool_session_authenticate(self, args: dict[str, Any]) -> ToolResult:
        url = args.get("url")
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            return ToolResult(
                ok=False,
                output={"error": "invalid_arguments", "message": "url must be http(s)"},
            )
        method = args.get("method")
        if method not in ("form", "header", "cookie"):
            return ToolResult(
                ok=False,
                output={"error": "invalid_arguments", "message": "method must be form, header or cookie"},
            )
        credentials = args.get("credentials")
        if not isinstance(credentials, dict):
            return ToolResult(
                ok=False,
                output={"error": "invalid_arguments", "message": "credentials must be an object"},
            )
        timeout = args.get("timeout_seconds", 30)
        if not isinstance(timeout, int) or timeout < 1:
            timeout = 30

        token = "sess_" + uuid.uuid4().hex[:12]
        session: dict[str, Any] = {"headers": {}, "login_url": url}

        if method == "header":
            name = credentials.get("name")
            value = credentials.get("value")
            if not isinstance(name, str) or not isinstance(value, str) or not name:
                return ToolResult(
                    ok=False,
                    output={"error": "invalid_arguments", "message": "header credentials need {name, value}"},
                )
            session["headers"][name] = value
            self._sessions[token] = session
            return ToolResult(
                ok=True,
                output={"session_token": token, "method": "header", "header": name, "login_url": url},
            )

        if method == "cookie":
            value = credentials.get("value")
            if not isinstance(value, str) or not value:
                return ToolResult(
                    ok=False,
                    output={"error": "invalid_arguments", "message": "cookie credentials need {value}"},
                )
            session["headers"]["Cookie"] = value
            self._sessions[token] = session
            return ToolResult(
                ok=True,
                output={"session_token": token, "method": "cookie", "login_url": url},
            )

        # form: real login POST, keep the response cookies in a jar.
        username = credentials.get("username")
        password = credentials.get("password")
        if not isinstance(username, str) or not isinstance(password, str):
            return ToolResult(
                ok=False,
                output={"error": "invalid_arguments", "message": "form credentials need {username, password}"},
            )
        user_field = credentials.get("username_field", "username")
        pass_field = credentials.get("password_field", "password")
        jar = f"/tmp/frex-session-{token}.txt"
        script = (
            f"curl -sS -c {shlex.quote(jar)} -o /dev/null -w '%{{http_code}}' "
            f"--max-time {timeout} "
            f"--data-urlencode {shlex.quote(f'{user_field}={username}')} "
            f"--data-urlencode {shlex.quote(f'{pass_field}={password}')} "
            f"{shlex.quote(url)}"
        )
        returncode, stdout, stderr = self._exec(["sh", "-c", script], timeout_seconds=timeout + 15)
        status_code = 0
        try:
            status_code = int(stdout.strip().split()[-1])
        except (ValueError, IndexError):
            pass
        if returncode != 0:
            return ToolResult(
                ok=False,
                output={"error": "login_failed", "message": self._output("session_authenticate", stderr) or f"curl exit {returncode}"},
            )
        # Confirm the jar actually holds a session cookie; a 200 with no
        # Set-Cookie usually means the login was rejected silently.
        jar_rc, jar_out, _ = self._exec(
            ["sh", "-c", f"grep -c . {shlex.quote(jar)} 2>/dev/null || echo 0"], timeout_seconds=5
        )
        has_cookies = jar_out.strip() not in ("", "0")
        if not has_cookies:
            return ToolResult(
                ok=False,
                output={
                    "error": "no_session_cookie",
                    "message": f"login returned HTTP {status_code} but no session cookie — credentials may be wrong or the form fields don't match",
                },
            )
        session["headers"]["Cookie"] = f"frex-cookie-jar:{jar}"  # marker; curl re-reads the jar
        # Store the jar path so per-tool commands can reference it with -b.
        session["cookie_jar"] = jar
        session["headers"].pop("Cookie")
        self._sessions[token] = session
        return ToolResult(
            ok=True,
            output={
                "session_token": token,
                "method": "form",
                "login_status": status_code,
                "login_url": url,
                "note": "session cookies stored in container; pass session_token to other tools",
            },
        )

    def _session_curl_flags(self, session_token: Any) -> tuple[str, str | None]:
        """Command-line flags (cookie jar or headers) for curl-based tools."""
        if not session_token:
            return "", None
        session = self._sessions.get(str(session_token))
        if session is None:
            return "", (
                f"unknown session_token {session_token!r} — call session_authenticate first"
            )
        jar = session.get("cookie_jar")
        if jar:
            return f" -b {shlex.quote(jar)}", None
        header_flags = " ".join(
            f"-H {shlex.quote(f'{k}: {v}')}" for k, v in session["headers"].items()
        )
        return f" {header_flags}", None


    def _tool_crawl_site(self, args: dict[str, Any]) -> ToolResult:
        url = args.get("url")
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            return ToolResult(
                ok=False,
                output={"error": "invalid_arguments", "message": "url must be http(s)"},
            )
        depth = args.get("depth", 3)
        if not isinstance(depth, int) or not 1 <= depth <= 10:
            depth = 3
        timeout = args.get("timeout_seconds", 600)
        if not isinstance(timeout, int) or timeout < 10:
            timeout = 600
        session_flags, error = self._session_curl_flags(args.get("session_token"))
        if error:
            return ToolResult(ok=False, output={"error": "invalid_arguments", "message": error})
        # -js-crawl extracts endpoints from JS bundles, not just <a href>.
        argv = [
            "katana", "-u", url, "-depth", str(depth), "-json", "-silent",
            "-js-crawl", "-d", str(depth), "-ef", "css,png,gif,jpg,woff,woff2,svg",
        ]
        session = self._sessions.get(str(args.get("session_token"))) if args.get("session_token") else None
        if session:
            for key, value in session["headers"].items():
                argv.extend(["-H", f"{key}: {value}"])
            if session.get("cookie_jar"):
                argv.extend(["-H", f"Cookie: $(cat {session['cookie_jar']})"])
        returncode, stdout, stderr = self._exec(argv, timeout_seconds=timeout)
        if returncode == 127 or "not found" in stderr:
            return ToolResult(
                ok=False,
                output={"error": "unavailable", "message": "katana is not installed in this container"},
            )
        endpoints: list[dict[str, Any]] = []
        seen: set[str] = set()
        for line in stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            endpoint_url = record.get("endpoint") or record.get("url") or ""
            if not endpoint_url or endpoint_url in seen:
                continue
            seen.add(endpoint_url)
            entry: dict[str, Any] = {"url": endpoint_url}
            if record.get("method"):
                entry["method"] = record["method"]
            parameter = record.get("parameter") or record.get("parameter_value")
            if parameter:
                entry["parameter"] = parameter
            endpoints.append(entry)
            if len(endpoints) >= FINDING_CAP:
                break
        truncated = len(seen) > FINDING_CAP
        return ToolResult(
            ok=True,
            output={
                "url": url,
                "endpoints": endpoints,
                "count": len(endpoints),
                "truncated": truncated,
                "depth": depth,
            },
        )

    def _tool_run_skill(self, args: dict[str, Any]) -> ToolResult:
        if self._skill_runner is None:
            return ToolResult(
                ok=False,
                output={"error": "unavailable", "message": "no skills are loaded for this deployment"},
            )
        skill_name = args.pop("skill", None)
        if not isinstance(skill_name, str) or not skill_name.strip():
            return ToolResult(
                ok=False, output={"error": "invalid_arguments", "message": "skill is required"}
            )
        try:
            run_result = self._skill_runner.run(skill_name.strip(), args)
        except Exception as exc:
            return ToolResult(
                ok=False,
                output={"error": "skill_failed", "message": self._output("run_skill", str(exc))},
            )
        return ToolResult(
            ok=run_result.completed,
            output={
                "skill": run_result.skill,
                "steps_run": run_result.steps_run,
                "completed": run_result.completed,
                "stopped_reason": run_result.stopped_reason,
                "last_output": run_result.last_output,
            },
        )

    def _browser_scope_error(self, url: str) -> str | None:
        """Re-check the scope guard on every browser URL — not just once
        at open_session. A session must not drift out of scope mid-flow."""
        if self._scope_checker is None:
            return None
        return self._scope_checker("browser_session", {"url": url})

    def _tool_browser_session(self, args: dict[str, Any]) -> ToolResult:
        from frex.dispatcher.runtime.browser import SessionNotFound

        if self._browser_manager is None:
            return ToolResult(
                ok=False,
                output={"error": "unavailable", "message": "browser automation is not configured for this deployment"},
            )
        action = args.get("action")
        if not isinstance(action, str):
            return ToolResult(
                ok=False, output={"error": "invalid_arguments", "message": "action is required"}
            )
        state_id = args.get("state_id")

        def not_found(exc: Exception) -> ToolResult:
            return ToolResult(
                ok=False,
                output={"error": "session_not_found", "message": self._output("browser_session", str(exc))},
            )

        try:
            if action == "open_session":
                url = args.get("url")
                if not isinstance(url, str) or not url.startswith(("http://", "https://")):
                    return ToolResult(
                        ok=False, output={"error": "invalid_arguments", "message": "url must be http(s)"}
                    )
                rejection = self._browser_scope_error(url)
                if rejection:
                    return ToolResult(ok=False, output={"error": "scope", "message": rejection})
                session = self._browser_manager.open(url)
                return ToolResult(
                    ok=True,
                    output={"state_id": session.state_id, "url": session.url},
                )

            if action == "navigate":
                url = args.get("url")
                if not isinstance(url, str) or not url.startswith(("http://", "https://")):
                    return ToolResult(
                        ok=False, output={"error": "invalid_arguments", "message": "url must be http(s)"}
                    )
                # SPEC: scope is re-checked here, not only at open_session.
                rejection = self._browser_scope_error(url)
                if rejection:
                    return ToolResult(ok=False, output={"error": "scope", "message": rejection})
                result = self._browser_manager.navigate(state_id, url)
                return ToolResult(ok=True, output={"state_id": state_id, **result})

            if action == "extract":
                mode = args.get("mode", "text")
                if mode not in ("text", "links", "dom"):
                    return ToolResult(
                        ok=False, output={"error": "invalid_arguments", "message": "mode must be text, links or dom"}
                    )
                content = self._browser_manager.extract(state_id, mode)
                if isinstance(content, list):
                    joined = json.dumps(content)
                    truncated = len(joined) > self._budgets["browser_session"]
                    return ToolResult(
                        ok=True,
                        output={"mode": mode, "items": content[:200], "count": len(content), "truncated": truncated},
                    )
                text = str(content)
                budget = self._budgets["browser_session"]
                truncated = len(text) > budget
                return ToolResult(
                    ok=True,
                    output={"mode": mode, "text": text[:budget], "truncated": truncated},
                )

            if action == "screenshot":
                path = args.get("output_path") or f"/tmp/frex-browser-{int(time.time())}.png"
                if not isinstance(path, str) or not path.startswith("/tmp/"):
                    return ToolResult(
                        ok=False, output={"error": "invalid_arguments", "message": "output_path must be under /tmp/"}
                    )
                result = self._browser_manager.screenshot(state_id, path)
                return ToolResult(ok=True, output={"state_id": state_id, **result})

            if action == "click":
                selector = args.get("selector")
                if not isinstance(selector, str) or not selector.strip():
                    return ToolResult(
                        ok=False, output={"error": "invalid_arguments", "message": "selector is required"}
                    )
                result = self._browser_manager.click(state_id, selector)
                return ToolResult(ok=True, output={"state_id": state_id, **result})

            if action == "fill":
                selector = args.get("selector")
                value = args.get("value")
                if not isinstance(selector, str) or not selector.strip():
                    return ToolResult(
                        ok=False, output={"error": "invalid_arguments", "message": "selector is required"}
                    )
                if not isinstance(value, str):
                    return ToolResult(
                        ok=False, output={"error": "invalid_arguments", "message": "value must be a string"}
                    )
                result = self._browser_manager.fill(state_id, selector, value)
                return ToolResult(ok=True, output={"state_id": state_id, **result})

            if action == "close_session":
                self._browser_manager.close(state_id)
                return ToolResult(ok=True, output={"state_id": state_id, "closed": True})

            return ToolResult(
                ok=False, output={"error": "invalid_arguments", "message": f"unknown action {action!r}"}
            )
        except SessionNotFound as exc:
            return not_found(exc)
        except Exception as exc:  # backend/browser failure
            return ToolResult(
                ok=False,
                output={"error": "browser_error", "message": self._output("browser_session", str(exc))},
            )

    def _tool_get_fact_detail(self, args: dict[str, Any]) -> ToolResult:
        fact_id = args.get("fact_id")
        if not isinstance(fact_id, str) or not fact_id.strip():
            return ToolResult(
                ok=False, output={"error": "invalid_arguments", "message": "fact_id is required"}
            )
        if self._facts_lookup is None:
            return ToolResult(
                ok=False,
                output={"error": "unavailable", "message": "fact lookup is not configured for this task"},
            )
        record = self._facts_lookup(fact_id.strip())
        if record is None:
            return ToolResult(
                ok=False,
                output={"error": "not_found", "message": f"no fact {fact_id!r} in this project"},
            )
        return ToolResult(ok=True, output={"fact": record})

    def _tool_submit_action_log(self, args: dict[str, Any]) -> ToolResult:
        tool_name = args.get("tool_name")
        target = args.get("target")
        result_summary = args.get("result_summary")
        for key, value in (("tool_name", tool_name), ("target", target), ("result_summary", result_summary)):
            if not isinstance(value, str) or not value.strip():
                return ToolResult(
                    ok=False,
                    output={"error": "invalid_arguments", "message": f"{key} is required"},
                )
        entry = {
            "description": f"action: {tool_name.strip()} on {target.strip()} — {result_summary.strip()}",
            "kind": "action_log",
            "tool_name": tool_name.strip(),
            "target": target.strip(),
            "result_summary": result_summary.strip(),
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        posted = self._action_log_sink(entry)
        return ToolResult(ok=True, output={"logged": posted, "kind": "action_log"})

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
            host_key = fact.get("host_key")
            if isinstance(host_key, str) and ":" in host_key:
                entry["host_key"] = host_key.strip().lower()
            validated.append(entry)
        if self._facts_sink is None:
            return ToolResult(
                ok=False,
                output={"error": "unavailable", "message": "fact recording is not configured for this task"},
            )
        posted = self._facts_sink(validated)
        return ToolResult(ok=True, output={"submitted": posted, "total": len(validated)})
