"""Tests for the skills layer (parsing, gated execution, stop conditions)."""
from __future__ import annotations

import json

import pytest

from frex.dispatcher.runtime.permissions import (
    ApprovalResponse,
    PermissionGate,
)
from frex.dispatcher.runtime.skills import (
    SkillError,
    SkillRunner,
    load_skills,
    parse_skill,
)
from frex.dispatcher.runtime.tools import ToolExecutor
from pathlib import Path


class ScriptedContainer:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def build_exec_process(self, container_name, env, command, timeout_seconds=None):
        self.calls.append(list(command))
        rc, out, err = self.responses.pop(0) if self.responses else (0, "", "")

        class P:
            def start(self):
                pass

            def communicate(self, timeout=None):
                class R:
                    returncode = rc
                    stdout = out
                    stderr = err

                return R()

        return P()


def _executor(containers, **kwargs):
    return ToolExecutor(containers, "ctr", **kwargs)


# ------------------------------------------------------------ parsing


def test_parse_skill_full_frontmatter():
    skill = parse_skill(
        """---
name: demo
description: a demo skill
steps:
  - tool: http_probe
    params: {url: "{url}"}
  - tool: nuclei_scan
    params: {target: "{url}"}
    stop_condition: "prev.finding_count == 0"
---
Human body here.
""",
        source="demo.md",
    )
    assert skill.name == "demo"
    assert len(skill.steps) == 2
    assert skill.steps[1].stop_condition == "prev.finding_count == 0"
    assert "Human body" in skill.body


def test_parse_skill_rejects_missing_name():
    with pytest.raises(SkillError, match="name"):
        parse_skill("---\ndescription: x\nsteps:\n  - tool: http_probe\n---\nbody")


def test_parse_skill_rejects_empty_steps():
    with pytest.raises(SkillError, match="step"):
        parse_skill("---\nname: x\ndescription: d\nsteps: []\n---\nbody")


def test_parse_skill_rejects_missing_frontmatter():
    with pytest.raises(SkillError, match="frontmatter"):
        parse_skill("just markdown, no frontmatter")


def test_load_packaged_skills():
    skills = load_skills(Path(__file__).resolve().parents[1] / "src" / "frex" / "skills")
    assert {"web-app-recon", "network-host-triage", "authenticated-app-recon", "finding-confirmation"} <= set(skills)
    web = skills["web-app-recon"]
    assert len(web.steps) == 6
    assert web.steps[0].tool == "subdomain_enum"
    assert web.steps[-1].tool == "nuclei_scan"


# ------------------------------------------------------------ execution


def test_skill_runs_all_steps_with_substitution():
    containers = ScriptedContainer(
        [
            (0, '{"host":"a.lab.test"}', ""),  # subdomain_enum
            (0, "HTTP/1.1 200 OK\r\n__FREX_STATUS__200 5\nhello", ""),  # probe
        ]
    )
    executor = _executor(containers)
    skill = parse_skill(
        """---
name: mini
description: d
steps:
  - tool: subdomain_enum
    params: {domain: "{domain}"}
  - tool: http_probe
    params: {url: "{url}"}
---
body
"""
    )
    runner = SkillRunner(skills={"mini": skill}, executor=executor)
    result = runner.run("mini", {"domain": "lab.test", "url": "http://lab.test"})
    assert result.completed is True
    assert len(result.steps_run) == 2
    # {domain} substituted into the subfinder command.
    assert "lab.test" in " ".join(containers.calls[0])
    assert result.last_output["status_code"] == 200


def test_stop_condition_ends_skill_early():
    containers = ScriptedContainer([(0, "", "")])  # 0 subdomains
    skill = parse_skill(
        """---
name: mini
description: d
steps:
  - tool: subdomain_enum
    params: {domain: "{domain}"}
    stop_condition: "prev.count == 0"
  - tool: http_probe
    params: {url: "{url}"}
---
body
"""
    )
    containers_for_probe = ScriptedContainer([])
    runner = SkillRunner(skills={"mini": skill}, executor=_executor(containers))
    result = runner.run("mini", {"domain": "lab.test", "url": "http://lab.test"})
    assert result.completed is False
    assert "stop_condition" in result.stopped_reason
    assert len(result.steps_run) == 1  # probe never ran


def test_deny_mid_skill_stops_remaining_steps():
    # session_authenticate is tier 2; without an approval channel it is
    # denied → crawl_site and content_discovery must NOT run.
    skill = parse_skill(
        """---
name: auth
description: d
steps:
  - tool: session_authenticate
    params: {url: "{url}", method: "header", credentials: "{credentials}"}
  - tool: crawl_site
    params: {url: "{url}"}
  - tool: content_discovery
    params: {url: "{url}"}
---
body
"""
    )
    containers = ScriptedContainer([])
    gate = PermissionGate(approval_handler=None, audit_sink=lambda e: None)
    runner = SkillRunner(
        skills={"auth": skill},
        executor=_executor(containers),
        gate=gate,
    )
    result = runner.run("auth", {"url": "http://10.0.0.5", "credentials": "x"})
    assert result.completed is False
    assert "denied" in result.stopped_reason
    assert len(result.steps_run) == 0
    assert containers.calls == []  # nothing executed past the denial


def test_denied_ask_mid_skill_stops_remaining_steps():
    skill = parse_skill(
        """---
name: s
description: d
steps:
  - tool: http_probe
    params: {url: "{url}"}
  - tool: service_check
    params: {target: "{target}"}
  - tool: port_scan
    params: {target: "{target}"}
---
body
"""
    )
    containers = ScriptedContainer([(0, "HTTP/1.1 200 OK\r\n__FREX_STATUS__200 0\n", "")])
    gate = PermissionGate(
        approval_handler=lambda req: ApprovalResponse(approved=False, approver_id="op"),
        audit_sink=lambda e: None,
    )
    runner = SkillRunner(skills={"s": skill}, executor=_executor(containers), gate=gate)
    result = runner.run("s", {"url": "http://10.0.0.5", "target": "10.0.0.5"})
    assert result.completed is False
    assert "denied by operator" in result.stopped_reason
    # http_probe ran (tier 0), port_scan (after service_check) never did.
    assert [s["tool"] for s in result.steps_run] == ["http_probe"]


def test_tier2_session_approval_applies_within_skill():
    skill = parse_skill(
        """---
name: s
description: d
steps:
  - tool: service_check
    params: {target: "{target}"}
  - tool: service_check
    params: {target: "{target}"}
---
body
"""
    )
    approvals = []

    def handler(req):
        approvals.append(req.tool_name)
        return ApprovalResponse(approved=True, approver_id="op")

    gate = PermissionGate(approval_handler=handler, audit_sink=lambda e: None)
    runner = SkillRunner(
        skills={"s": skill}, executor=_executor(ScriptedContainer([])), gate=gate
    )
    result = runner.run("s", {"target": "10.0.0.5:6379"})
    assert result.completed is True
    # Asked once for the session; the second identical step reuses it.
    assert approvals == ["service_check"]


def test_run_skill_tool_without_runner_is_unavailable():
    result = _executor(ScriptedContainer([])).run("run_skill", json.dumps({"skill": "x"}))
    assert result.output["error"] == "unavailable"


def test_run_skill_tool_unknown_skill():
    containers = ScriptedContainer([])
    executor = _executor(containers, skill_runner=SkillRunner(skills={}, executor=None))
    result = executor.run("run_skill", json.dumps({"skill": "nope"}))
    assert result.output["error"] == "skill_failed"
    assert "unknown skill" in result.output["message"]


def test_run_skill_tool_end_to_end():
    containers = ScriptedContainer([(0, '{"host":"a.lab.test"}', "")])
    skill = parse_skill(
        """---
name: mini
description: d
steps:
  - tool: subdomain_enum
    params: {domain: "{domain}"}
---
body
"""
    )
    executor = _executor(
        containers, skill_runner=SkillRunner(skills={"mini": skill}, executor=None)
    )
    # The runner's executor is patched in by the wiring; emulate it.
    executor._skill_runner._executor = executor
    result = executor.run("run_skill", json.dumps({"skill": "mini", "domain": "lab.test"}))
    assert result.ok is True
    assert result.output["completed"] is True
    assert result.output["steps_run"][0]["tool"] == "subdomain_enum"
