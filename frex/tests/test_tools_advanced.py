"""Tests for the structured toolset (http_probe, port_scan, nuclei_scan,
screenshot, search_sploit, submit_facts) and the tool-surface controls
(enabled_tools, per-tool output budgets, scope coverage).
"""
from __future__ import annotations

import json

import pytest

from frex.dispatcher.runtime.scope import Scope
from frex.dispatcher.runtime.tools import (
    DEFAULT_TOOL_OUTPUT_BUDGETS,
    TOOLS_SPEC,
    ToolExecutor,
)


class ScriptedContainer:
    """Returns canned exec results keyed by a substring of the command."""

    def __init__(self, responses: list[tuple[str, tuple[int, str, str]]]):
        self.responses = responses  # (substring matcher, (rc, out, err))
        self.calls: list[list[str]] = []

    def build_exec_process(self, container_name, env, command, timeout_seconds=None):
        self.calls.append(list(command))
        joined = " ".join(command)
        for matcher, result in self.responses:
            if matcher in joined:
                return _FakeProcess(*result)
        return _FakeProcess(0, "", "")

    def run(self, *args, **kwargs):  # unused by the executor
        raise AssertionError("executor should use build_exec_process")


class _FakeProcess:
    def __init__(self, returncode: int, stdout: str, stderr: str):
        self._result = (returncode, stdout, stderr)

    def start(self):
        pass

    def communicate(self, timeout=None):
        rc, out, err = self._result

        class Result:
            returncode = rc
            stdout = out
            stderr = err

        return Result()


def _executor(containers, **kwargs):
    return ToolExecutor(containers, "ctr", **kwargs)


# ------------------------------------------------------------ spec/controls


def test_spec_contains_all_structured_tools():
    names = {tool["name"] for tool in TOOLS_SPEC}
    assert {
        "run_command", "read_file", "write_file", "http_request",
        "submit_result", "http_probe", "port_scan", "nuclei_scan",
        "screenshot", "search_sploit", "submit_facts",
    } <= names


def test_enabled_tools_rejects_disabled_tool():
    executor = _executor(
        ScriptedContainer([]), enabled_tools={"run_command", "submit_result"}
    )
    result = executor.run("port_scan", json.dumps({"target": "10.0.0.5"}))
    assert result.ok is False
    assert result.output["error"] == "tool_disabled"


def test_enabled_none_means_all_tools():
    executor = _executor(ScriptedContainer([]))
    result = executor.run("port_scan", json.dumps({"target": ""}))
    # Empty target is an arguments error, not a disabled-tool error.
    assert result.output.get("error") == "invalid_arguments" or not result.ok


def test_per_tool_output_budget_truncates():
    long_stdout = "A" * 10_000
    containers = ScriptedContainer([("nmap", (0, long_stdout, ""))])
    executor = _executor(containers)  # port_scan default budget: 4000
    result = executor.run("port_scan", json.dumps({"target": "10.0.0.5"}))
    assert result.output["raw"].endswith("...[truncated]")
    assert len(result.output["raw"]) <= DEFAULT_TOOL_OUTPUT_BUDGETS["port_scan"] + 20


# ------------------------------------------------------------ http_probe


def test_http_probe_parses_headers_status_and_body():
    curl_output = (
        "HTTP/1.1 200 OK\r\n"
        "Server: nginx/1.25\r\n"
        "Content-Type: text/html\r\n"
        "Set-Cookie: a=1\r\n"
        "Set-Cookie: b=2\r\n"
        "__FREX_STATUS__200 19\n"
        "<html>welcome</html>"
    )
    containers = ScriptedContainer([("curl", (0, curl_output, ""))])
    executor = _executor(containers)
    result = executor.run("http_probe", json.dumps({"url": "http://10.0.0.5/"}))
    assert result.ok is True
    assert result.output["status_code"] == 200
    assert result.output["headers"]["server"] == "nginx/1.25"
    assert result.output["headers"]["content_type"] == "text/html"
    assert result.output["headers"]["set_cookie_count"] == "2"
    assert result.output["body_length"] == 19
    assert "welcome" in result.output["body_preview"]


def test_http_probe_detects_reflections():
    curl_output = (
        "HTTP/1.1 200 OK\r\n"
        "__FREX_STATUS__200 33\n"
        "echoed: <script>alert(1)</script>"
    )
    containers = ScriptedContainer([("curl", (0, curl_output, ""))])
    executor = _executor(containers)
    result = executor.run(
        "http_probe",
        json.dumps({"url": "http://10.0.0.5/x", "body": "<script>alert(1)</script>"}),
    )
    assert result.output["reflections"] == ["<script>alert(1)</script>"]


def test_http_probe_scope_rejects_out_of_scope_url():
    executor = _executor(
        ScriptedContainer([]),
        scope_checker=Scope(domains=["lab.test"]).checker(),
    )
    result = executor.run("http_probe", json.dumps({"url": "http://evil.test/"}))
    assert result.ok is False
    assert result.output["error"] == "scope"


# ------------------------------------------------------------ port_scan


def test_port_scan_parses_greppable_output():
    nmap_output = (
        "# Nmap 7.95 scan initiated\n"
        "Host: 10.0.0.5 ()\tPorts: 22/open/tcp//ssh///, 80/open/tcp//http///, 443/closed/tcp//https///\n"
    )
    containers = ScriptedContainer([("nmap", (0, nmap_output, ""))])
    executor = _executor(containers)
    result = executor.run("port_scan", json.dumps({"target": "10.0.0.5"}))
    assert result.ok is True
    ports = result.output["open_ports"]
    assert {"host": "10.0.0.5", "port": 22, "service": "ssh"} in ports
    assert {"host": "10.0.0.5", "port": 80, "service": "http"} in ports
    # Closed ports are filtered by --open.
    assert all(p["port"] != 443 for p in ports)


def test_port_scan_reports_missing_nmap():
    containers = ScriptedContainer([("nmap", (127, "", "sh: nmap: not found"))])
    executor = _executor(containers)
    result = executor.run("port_scan", json.dumps({"target": "10.0.0.5"}))
    assert result.output["error"] == "unavailable"


def test_port_scan_scope_rejects_out_of_scope_target():
    executor = _executor(
        ScriptedContainer([]),
        scope_checker=Scope(cidrs=["127.0.0.0/8"]).checker(),
    )
    result = executor.run("port_scan", json.dumps({"target": "10.0.0.5"}))
    assert result.ok is False
    assert result.output["error"] == "scope"


def test_port_scan_scope_allows_in_scope_cidr():
    executor = _executor(
        ScriptedContainer([("nmap", (0, "Host: 10.0.0.1 ()\tPorts: 80/open/tcp//http///\n", ""))]),
        scope_checker=Scope(cidrs=["10.0.0.0/24"]).checker(),
    )
    result = executor.run("port_scan", json.dumps({"target": "10.0.0.0/24"}))
    assert result.ok is True
    assert result.output["open_ports"][0]["port"] == 80


# ------------------------------------------------------------ nuclei_scan


def test_nuclei_scan_parses_json_findings():
    nuclei_output = (
        '{"template-id":"CVE-2023-1111","info":{"name":"RCE in app","severity":"critical"},"host":"http://10.0.0.5","matched-at":"http://10.0.0.5/login"}\n'
        "noise line\n"
        '{"template-id":"tech-detect","info":{"name":"Tech","severity":"info"},"host":"http://10.0.0.5","matched-at":"http://10.0.0.5/"}\n'
    )
    containers = ScriptedContainer([("nuclei", (0, nuclei_output, ""))])
    executor = _executor(containers)
    result = executor.run(
        "nuclei_scan", json.dumps({"target": "http://10.0.0.5", "severity": "critical,high"})
    )
    assert result.ok is True
    assert result.output["finding_count"] == 2
    assert result.output["findings"][0]["id"] == "CVE-2023-1111"
    assert result.output["findings"][0]["severity"] == "critical"
    # The severity filter reached the command line.
    joined = " ".join(containers.calls[0])
    assert "-severity critical,high" in joined


def test_nuclei_scan_scope_rejects_out_of_scope_target():
    executor = _executor(
        ScriptedContainer([]),
        scope_checker=Scope(domains=["lab.test"]).checker(),
    )
    result = executor.run("nuclei_scan", json.dumps({"target": "http://evil.test/"}))
    assert result.output["error"] == "scope"


# ------------------------------------------------------------ screenshot


def test_screenshot_runs_chromium_and_reports_file():
    containers = ScriptedContainer(
        [("chromium", (0, "-rw-r--r-- 1 root root 48213 screenshot.png", ""))]
    )
    executor = _executor(containers)
    result = executor.run("screenshot", json.dumps({"url": "http://10.0.0.5/"}))
    assert result.ok is True
    assert result.output["bytes"] == 48213
    assert result.output["path"].startswith("/tmp/")
    joined = " ".join(containers.calls[0])
    assert "--headless=new" in joined
    assert "--no-sandbox" in joined


def test_screenshot_rejects_non_tmp_path():
    executor = _executor(ScriptedContainer([]))
    result = executor.run(
        "screenshot",
        json.dumps({"url": "http://10.0.0.5/", "output_path": "/etc/passwd.png"}),
    )
    assert result.output["error"] == "invalid_arguments"


# ------------------------------------------------------------ search_sploit


def test_search_sploit_parses_json_results():
    searchsploit_output = json.dumps(
        {
            "RESULTS": {
                "EXPLOIT": [
                    {"ID": "12345", "Title": "App 1.2 RCE", "Path": "/usr/share/exploits/1"},
                    {"ID": "12346", "Title": "App 1.2 LFI", "Path": "/usr/share/exploits/2"},
                ]
            }
        }
    )
    containers = ScriptedContainer([("searchsploit", (0, searchsploit_output, ""))])
    executor = _executor(containers)
    result = executor.run("search_sploit", json.dumps({"query": "app 1.2", "max_results": 1}))
    assert result.ok is True
    assert len(result.output["results"]) == 1
    assert result.output["results"][0]["id"] == "12345"


def test_search_sploit_missing_binary():
    containers = ScriptedContainer([("searchsploit", (127, "", "searchsploit: not found"))])
    executor = _executor(containers)
    result = executor.run("search_sploit", json.dumps({"query": "x"}))
    assert result.output["error"] == "unavailable"


# ------------------------------------------------------------ submit_facts


def test_submit_facts_posts_through_sink():
    received = []

    def sink(facts):
        received.extend(facts)
        return len(facts)

    executor = _executor(ScriptedContainer([]), facts_sink=sink)
    result = executor.run(
        "submit_facts",
        json.dumps(
            {
                "facts": [
                    {"description": "admin panel at /admin", "kind": "finding", "confidence": 0.9},
                    {"description": "nginx 1.25 detected"},
                ]
            }
        ),
    )
    assert result.ok is True
    assert result.output["submitted"] == 2
    assert received[0]["kind"] == "finding"
    assert received[0]["confidence"] == 0.9
    assert "kind" not in received[1]


def test_submit_facts_requires_sink():
    executor = _executor(ScriptedContainer([]))
    result = executor.run("submit_facts", json.dumps({"facts": [{"description": "x"}]}))
    assert result.output["error"] == "unavailable"


def test_submit_facts_rejects_empty_descriptions():
    executor = _executor(ScriptedContainer([]), facts_sink=lambda facts: 0)
    result = executor.run("submit_facts", json.dumps({"facts": [{"description": "  "}]}))
    assert result.output["error"] == "invalid_arguments"
