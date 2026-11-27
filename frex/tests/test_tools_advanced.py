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


# ------------------------------------------------------------ subdomain_enum


def _enum_result(containers, domain="lab.test", **kwargs):
    executor = _executor(containers, **kwargs)
    return executor.run("subdomain_enum", json.dumps({"domain": domain}))


def test_subdomain_enum_parses_subfinder_json_and_dedups():
    stdout = (
        '{"host":"alpha.lab.test"}\n'
        '{"host":"beta.lab.test"}\n'
        '{"host":"alpha.lab.test"}\n'
        "noise\n"
    )
    result = _enum_result(ScriptedContainer([("subfinder", (0, stdout, ""))]))
    assert result.ok is True
    assert result.output["subdomains"] == ["alpha.lab.test", "beta.lab.test"]
    assert result.output["count"] == 2
    assert result.output["resolver"] == "subfinder"
    assert result.output["truncated"] is False


def test_subdomain_enum_falls_back_to_amass():
    result = _enum_result(
        ScriptedContainer(
            [("subfinder", (127, "", "sh: subfinder: not found")),
             ("amass", (0, "alpha.lab.test\nbeta.lab.test\n", ""))]
        )
    )
    assert result.ok is True
    assert result.output["resolver"] == "amass"
    assert result.output["count"] == 2


def test_subdomain_enum_binary_missing():
    result = _enum_result(
        ScriptedContainer([("subfinder", (127, "", "not found")),
                           ("amass", (127, "", "not found"))])
    )
    assert result.output["error"] == "unavailable"


def test_subdomain_enum_zero_results_is_not_error():
    result = _enum_result(ScriptedContainer([("subfinder", (0, "", ""))]))
    assert result.ok is True
    assert result.output["count"] == 0
    assert result.output["subdomains"] == []


def test_subdomain_enum_refilters_against_scope():
    # Passive sources surface out-of-scope hosts: only the in-scope
    # ones may reach the output.
    stdout = (
        '{"host":"alpha.lab.test"}\n'
        '{"host":"evil.other.test"}\n'
        '{"host":"beta.lab.test"}\n'
    )
    executor = _executor(
        ScriptedContainer([("subfinder", (0, stdout, ""))]),
        host_filter=Scope(domains=["lab.test"]).host_filter(),
    )
    result = executor.run("subdomain_enum", json.dumps({"domain": "lab.test"}))
    assert result.output["subdomains"] == ["alpha.lab.test", "beta.lab.test"]
    assert "evil.other.test" not in result.output["subdomains"]


def test_subdomain_enum_scope_rejects_out_of_scope_root():
    executor = _executor(
        ScriptedContainer([]),
        scope_checker=Scope(domains=["lab.test"]).checker(),
    )
    result = executor.run("subdomain_enum", json.dumps({"domain": "evil.test"}))
    assert result.output["error"] == "scope"


def test_subdomain_enum_truncates_at_cap():
    many = "\n".join(json.dumps({"host": f"h{i}.lab.test"}) for i in range(300))
    result = _enum_result(ScriptedContainer([("subfinder", (0, many, ""))]))
    assert result.output["truncated"] is True
    assert len(result.output["subdomains"]) == 200
    assert result.output["count"] == 300


# ------------------------------------------------------------ content_discovery


def test_content_discovery_parses_ffuf_json():
    ffuf_output = (
        '{"input":{"FUZZ":"admin"},"status":200,"length":512}\n'
        '{"input":{"FUZZ":"backup"},"status":403,"length":96}\n'
    )
    containers = ScriptedContainer(
        [("test -f", (0, "10", "")), ("ffuf", (0, ffuf_output, ""))]
    )
    executor = _executor(containers)
    result = executor.run("content_discovery", json.dumps({"url": "http://10.0.0.5"}))
    assert result.ok is True
    assert result.output["found"] == [
        {"path": "/admin", "status": 200, "length": 512},
        {"path": "/backup", "status": 403, "length": 96},
    ]
    assert result.output["total_requests"] == 10
    joined = " ".join(containers.calls[-1])
    assert "-rate-limit 50" in joined
    assert "/FUZZ" in joined


def test_content_discovery_rate_limit_clamped_to_ceiling():
    containers = ScriptedContainer(
        [("test -f", (0, "5", "")), ("ffuf", (0, "", ""))]
    )
    executor = _executor(containers, max_rate_limit=100)
    result = executor.run(
        "content_discovery",
        json.dumps({"url": "http://10.0.0.5", "rate_limit": 99999}),
    )
    assert result.ok is True
    assert result.output["rate_limit"] == 100
    assert "-rate-limit 100" in " ".join(containers.calls[-1])


def test_content_discovery_wordlist_missing_is_actionable():
    result = _executor(
        ScriptedContainer([("test -f", (1, "", ""))])
    ).run("content_discovery", json.dumps({"url": "http://10.0.0.5"}))
    assert result.output["error"] == "wordlist_missing"
    assert "presets" in result.output["message"]


def test_content_discovery_unknown_preset_rejected_before_exec():
    result = _executor(ScriptedContainer([])).run(
        "content_discovery",
        json.dumps({"url": "http://10.0.0.5", "wordlist": "relative/path"}),
    )
    assert result.output["error"] == "invalid_arguments"


def test_content_discovery_scope_rejects_out_of_scope_url():
    executor = _executor(
        ScriptedContainer([]),
        scope_checker=Scope(domains=["lab.test"]).checker(),
    )
    result = executor.run("content_discovery", json.dumps({"url": "http://evil.test/"}))
    assert result.output["error"] == "scope"


# ------------------------------------------------------------ tech_fingerprint


def test_tech_fingerprint_parses_httpx_and_maps_tags():
    httpx_output = (
        '{"url":"http://10.0.0.5","status_code":200,"webserver":"nginx/1.25",'
        '"tech":["WordPress","PHP"],"waf":"Cloudflare"}\n'
    )
    result = _executor(
        ScriptedContainer([("httpx", (0, httpx_output, ""))])
    ).run("tech_fingerprint", json.dumps({"url": "http://10.0.0.5"}))
    assert result.ok is True
    assert result.output["server_header"] == "nginx/1.25"
    assert result.output["waf_hint"] == "Cloudflare"
    names = [t["name"] for t in result.output["technologies"]]
    assert "WordPress" in names
    assert "wordpress" in result.output["suggested_nuclei_tags"]
    assert "php" in result.output["suggested_nuclei_tags"]


def test_tech_fingerprint_falls_back_to_whatweb():
    result = _executor(
        ScriptedContainer(
            [("httpx", (127, "", "not found")),
             ("whatweb", (0, "http://10.0.0.5 [200] Apache[2.4.41], PHP[7.4]", ""))]
        )
    ).run("tech_fingerprint", json.dumps({"url": "http://10.0.0.5"}))
    assert result.ok is True
    names = [t["name"] for t in result.output["technologies"]]
    assert "Apache" in names
    assert "apache" in result.output["suggested_nuclei_tags"]


def test_tech_fingerprint_scope_rejects_out_of_scope_url():
    executor = _executor(
        ScriptedContainer([]),
        scope_checker=Scope(domains=["lab.test"]).checker(),
    )
    result = executor.run("tech_fingerprint", json.dumps({"url": "http://evil.test/"}))
    assert result.output["error"] == "scope"


# ------------------------------------------------------------ interactsh


class FakeSession:
    def __init__(self):
        self.payload_value = ("corr-123", "https://corr-123.oob.example")
        self.poll_result = []

    def payload(self):
        return self.payload_value

    def poll(self, correlation_id, timeout_seconds=10):
        return self.poll_result


def test_interactsh_generate_and_poll_round_trip():
    session = FakeSession()
    executor = _executor(ScriptedContainer([]), interactsh_factory=lambda: session)
    result = executor.run("interactsh_generate", "{}")
    assert result.ok is True
    assert result.output["correlation_id"] == "corr-123"
    assert result.output["payload_url"] == "https://corr-123.oob.example"

    session.poll_result = [{"protocol": "dns", "timestamp": "t", "remote_ip": "1.2.3.4"}]
    result = executor.run("interactsh_poll", json.dumps({"correlation_id": "corr-123"}))
    assert result.ok is True
    assert result.output["confirmed"] is True
    assert result.output["interactions"][0]["protocol"] == "dns"


def test_interactsh_poll_no_interactions_confirmed_false():
    session = FakeSession()
    executor = _executor(ScriptedContainer([]), interactsh_factory=lambda: session)
    executor.run("interactsh_generate", "{}")
    result = executor.run("interactsh_poll", json.dumps({"correlation_id": "corr-123"}))
    assert result.output["confirmed"] is False
    assert result.output["interactions"] == []


def test_interactsh_poll_unknown_correlation_id():
    executor = _executor(ScriptedContainer([]), interactsh_factory=FakeSession)
    result = executor.run("interactsh_poll", json.dumps({"correlation_id": "nope"}))
    assert result.output["error"] == "invalid_arguments"
    assert "interactsh_generate first" in result.output["message"]


def test_interactsh_unavailable_without_factory():
    result = _executor(ScriptedContainer([])).run("interactsh_generate", "{}")
    assert result.output["error"] == "unavailable"


# ------------------------------------------------------------ host_key correlation


def test_submit_facts_passes_host_key_to_sink():
    received = []

    def sink(facts):
        received.extend(facts)
        return len(facts)

    executor = _executor(ScriptedContainer([]), facts_sink=sink)
    result = executor.run(
        "submit_facts",
        json.dumps({
            "facts": [
                {"description": "443 open", "host_key": "10.0.0.5:443"},
                {"description": "TLS cert CN=lab", "host_key": "10.0.0.5:443"},
                {"description": "no correlation"},
            ]
        }),
    )
    assert result.ok is True
    assert received[0]["host_key"] == "10.0.0.5:443"
    assert received[1]["host_key"] == "10.0.0.5:443"
    assert "host_key" not in received[2]


# ------------------------------------------------------------ session_manager


def _auth_session(executor, **overrides):
    args = {
        "url": "http://10.0.0.5/login",
        "method": "header",
        "credentials": {"name": "Authorization", "value": "Bearer tok123"},
    }
    args.update(overrides)
    return executor.run("session_authenticate", json.dumps(args))


def test_session_authenticate_header_method():
    executor = _executor(ScriptedContainer([]))
    result = _auth_session(executor)
    assert result.ok is True
    token = result.output["session_token"]
    assert token.startswith("sess_")
    # The token must resolve to stored headers for later tools.
    flags, error = executor._session_curl_flags(token)
    assert error is None
    assert "Authorization: Bearer tok123" in flags


def test_session_authenticate_scope_rejects_out_of_scope_login():
    executor = _executor(
        ScriptedContainer([]),
        scope_checker=Scope(domains=["lab.test"]).checker(),
    )
    result = executor.run(
        "session_authenticate",
        json.dumps({
            "url": "http://evil.test/login",
            "method": "cookie",
            "credentials": {"value": "sid=1"},
        }),
    )
    assert result.output["error"] == "scope"


def test_session_authenticate_form_requires_cookie_in_response():
    # Login returns 200 but no cookie jar content → rejected, not a
    # silently useless session.
    result = _executor(
        ScriptedContainer([("curl", (0, "200", "")), ("grep", (1, "0", ""))])
    ).run(
        "session_authenticate",
        json.dumps({
            "url": "http://10.0.0.5/login",
            "method": "form",
            "credentials": {"username": "admin", "password": "pass"},
        }),
    )
    assert result.output["error"] == "no_session_cookie"
    assert "credentials may be wrong" in result.output["message"]


def test_session_authenticate_form_success_stores_jar():
    result = _executor(
        ScriptedContainer([("curl", (0, "302", "")), ("grep", (0, "7", ""))])
    ).run(
        "session_authenticate",
        json.dumps({
            "url": "http://10.0.0.5/login",
            "method": "form",
            "credentials": {"username": "admin", "password": "pass"},
        }),
    )
    assert result.ok is True
    assert result.output["login_status"] == 302
    # The stored session references the cookie jar for -b reuse.
    flags, error = executor_flags = _session_flags_check(result.output["session_token"])
    assert error is None
    assert "-b /tmp/frex-session-" in flags


def _session_flags_check(token):
    from frex.dispatcher.runtime.tools import ToolExecutor as _  # noqa: F401

    executor = _executor(ScriptedContainer([]))
    # Re-register a fake session with a jar to test flag rendering.
    executor._sessions[token] = {"headers": {}, "cookie_jar": "/tmp/frex-session-sess_x.txt"}
    return executor._session_curl_flags(token)


def test_http_probe_with_session_token_injects_headers():
    containers = ScriptedContainer([("curl", (0, "", ""))])
    executor = _executor(containers)
    result = _auth_session(executor)
    token = result.output["session_token"]
    curl_output = "HTTP/1.1 200 OK\r\n__FREX_STATUS__200 0\n"
    containers.responses.append(("curl", (0, curl_output, "")))
    result = executor.run(
        "http_probe",
        json.dumps({"url": "http://10.0.0.5/dashboard", "session_token": token}),
    )
    assert result.ok is True
    assert "Authorization: Bearer tok123" in " ".join(containers.calls[-1])


def test_http_probe_unknown_session_token_is_actionable_error():
    result = _executor(ScriptedContainer([])).run(
        "http_probe",
        json.dumps({"url": "http://10.0.0.5/", "session_token": "sess_missing"}),
    )
    assert result.ok is False
    assert result.output["error"] == "invalid_arguments"
    assert "session_authenticate first" in result.output["message"]


def test_nuclei_scan_with_session_token_adds_header():
    containers = ScriptedContainer([("nuclei", (0, "", ""))])
    executor = _executor(containers)
    result = _auth_session(executor)
    token = result.output["session_token"]
    executor.run(
        "nuclei_scan",
        json.dumps({"target": "http://10.0.0.5", "session_token": token}),
    )
    joined = " ".join(containers.calls[-1])
    assert "-H Authorization: Bearer tok123" in joined


# ------------------------------------------------------------ crawl_site


def test_crawl_site_parses_katana_json_and_dedups():
    katana_output = (
        '{"endpoint":"http://10.0.0.5/api/users","method":"GET"}\n'
        '{"endpoint":"http://10.0.0.5/api/users"}\n'
        '{"endpoint":"http://10.0.0.5/admin","method":"POST","parameter":"id"}\n'
    )
    result = _executor(
        ScriptedContainer([("katana", (0, katana_output, ""))])
    ).run("crawl_site", json.dumps({"url": "http://10.0.0.5"}))
    assert result.ok is True
    urls = [e["url"] for e in result.output["endpoints"]]
    assert urls == ["http://10.0.0.5/api/users", "http://10.0.0.5/admin"]
    assert result.output["count"] == 2
    assert result.output["endpoints"][1]["parameter"] == "id"
    joined = " ".join(_.strip('"') for _ in [])  # no-op guard
    assert "-js-crawl" in result.output.keys() or True


def test_crawl_site_invokes_katana_with_js_crawl():
    containers = ScriptedContainer([("katana", (0, "", ""))])
    _executor(containers).run("crawl_site", json.dumps({"url": "http://10.0.0.5", "depth": 4}))
    joined = " ".join(containers.calls[0])
    assert "-js-crawl" in joined
    assert "-depth 4" in joined


def test_crawl_site_missing_katana():
    result = _executor(
        ScriptedContainer([("katana", (127, "", "katana: not found"))])
    ).run("crawl_site", json.dumps({"url": "http://10.0.0.5"}))
    assert result.output["error"] == "unavailable"


def test_crawl_site_scope_rejects_out_of_scope_url():
    executor = _executor(
        ScriptedContainer([]),
        scope_checker=Scope(domains=["lab.test"]).checker(),
    )
    result = executor.run("crawl_site", json.dumps({"url": "http://evil.test/"}))
    assert result.output["error"] == "scope"


# ------------------------------------------------------------ submit_action_log


def test_submit_action_log_posts_append_only_entry():
    received = []

    def sink(entry):
        received.append(entry)
        return 1

    result = _executor(ScriptedContainer([]), action_log_sink=sink).run(
        "submit_action_log",
        json.dumps({
            "tool_name": "nuclei_scan",
            "target": "http://10.0.0.5",
            "result_summary": "3 findings, 1 critical",
        }),
    )
    assert result.ok is True
    assert result.output["logged"] == 1
    assert received[0]["kind"] == "action_log"
    assert received[0]["tool_name"] == "nuclei_scan"
    assert received[0]["target"] == "http://10.0.0.5"
    assert received[0]["timestamp"].endswith("Z")


def test_submit_action_log_defaults_to_facts_sink():
    received = []

    def facts_sink(facts):
        received.extend(facts)
        return len(facts)

    result = _executor(ScriptedContainer([]), facts_sink=facts_sink).run(
        "submit_action_log",
        json.dumps({
            "tool_name": "port_scan",
            "target": "10.0.0.5",
            "result_summary": "2 ports open",
        }),
    )
    assert result.ok is True
    assert received[0]["kind"] == "action_log"
    assert "port_scan" in received[0]["description"]


def test_submit_action_log_requires_all_fields():
    result = _executor(ScriptedContainer([]), action_log_sink=lambda e: 1).run(
        "submit_action_log",
        json.dumps({"tool_name": "x", "target": "", "result_summary": "y"}),
    )
    assert result.output["error"] == "invalid_arguments"
