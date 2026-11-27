"""Skills layer: multi-step playbooks the planner can invoke as one action.

A skill file is markdown with YAML frontmatter:

    ---
    name: web-app-recon
    description: trigger text for the planner
    steps:
      - tool: subdomain_enum
        params: {domain: "{domain}"}
      - tool: nuclei_scan
        params: {target: "{url}", tags: "{suggested_nuclei_tags}"}
        stop_condition: "prev.count == 0"
    ---
    Human-readable body.

NON-NEGOTIABLE: every step runs through the same permission gate and
scope guard as a manual tool call — a skill is never a shortcut around
the guards. A DENY (or a denied ASK) at any step stops the remaining
steps; the skill never continues silently past a refusal.

Substitution: ``{placeholder}`` in params templates resolves from the
initial params plus the outputs of earlier steps (``prev.<key>`` picks
a key from the immediately preceding result). stop_condition supports
``prev.<key> == <literal>`` and ``prev.<key> is empty`` — evaluated in
code, not by the model.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml


@dataclass(slots=True)
class SkillStep:
    tool: str
    params: dict[str, str] = field(default_factory=dict)
    stop_condition: str | None = None


@dataclass(slots=True)
class Skill:
    name: str
    description: str
    steps: list[SkillStep]
    body: str = ""

    def to_spec_entry(self) -> dict[str, Any]:
        return {
            "name": f"skill_{self.name}",
            "description": self.description,
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "domain": {"type": "string"},
                    "target": {"type": "string"},
                },
            },
        }


class SkillError(ValueError):
    pass


def parse_skill(text: str, *, source: str = "<skill>") -> Skill:
    # Tolerate a single leading "---" delimiter line before frontmatter.
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        lines = lines[1:]
    text = "\n".join(lines)
    parts = text.split("\n---", 1)
    if len(parts) != 2:
        raise SkillError(f"{source}: frontmatter block not found")
    header = parts[0].strip()
    body = parts[1].split("---", 1)[-1].strip() if "---" in parts[1] else parts[1].strip()
    try:
        meta = yaml.safe_load(header)
    except yaml.YAMLError as exc:
        raise SkillError(f"{source}: invalid frontmatter: {exc}") from exc
    if not isinstance(meta, dict):
        raise SkillError(f"{source}: frontmatter must be a mapping")
    name = meta.get("name")
    if not name or not isinstance(name, str):
        raise SkillError(f"{source}: skill needs a name")
    raw_steps = meta.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise SkillError(f"{source}: skill needs at least one step")
    steps = []
    for i, raw in enumerate(raw_steps):
        if not isinstance(raw, dict) or not isinstance(raw.get("tool"), str):
            raise SkillError(f"{source}: step {i} needs a tool name")
        params = raw.get("params") or {}
        if not isinstance(params, dict):
            raise SkillError(f"{source}: step {i} params must be a mapping")
        steps.append(
            SkillStep(
                tool=raw["tool"],
                params={str(k): str(v) for k, v in params.items()},
                stop_condition=raw.get("stop_condition"),
            )
        )
    return Skill(
        name=name,
        description=str(meta.get("description", "")),
        steps=steps,
        body=body,
    )


def load_skills(directory: Path) -> dict[str, Skill]:
    skills: dict[str, Skill] = {}
    if not directory.is_dir():
        return skills
    for path in sorted(directory.glob("*.md")):
        skill = parse_skill(path.read_text(), source=path.name)
        skills[skill.name] = skill
    return skills


def _resolve_placeholder(key: str, ctx: dict[str, Any], prev: dict[str, Any]) -> str:
    if key.startswith("prev."):
        value = prev.get(key[4:], "")
    else:
        value = ctx.get(key, "")
    if isinstance(value, list):
        return ",".join(str(v) for v in value)
    return str(value) if value is not None else ""


def _substitute(value: str, ctx: dict[str, Any], prev: dict[str, Any]) -> str:
    return re.sub(r"\{([a-zA-Z0-9_.]+)\}", lambda m: _resolve_placeholder(m.group(1), ctx, prev), value)


def _evaluate_stop(condition: str, prev: dict[str, Any]) -> bool:
    """Supported forms: 'prev.<key> == <literal>' and 'prev.<key> is empty'.

    Evaluated in code with a strict pattern — never eval().
    """
    condition = condition.strip()
    match = re.fullmatch(r"prev\.([A-Za-z0-9_]+)\s*==\s*(.+)", condition)
    if match:
        key, literal = match.groups()
        actual = prev.get(key)
        literal = literal.strip().strip("'\"")
        expected: Any = literal
        if literal.isdigit() and isinstance(actual, int):
            expected = int(literal)
        return actual == expected
    match = re.fullmatch(r"prev\.([A-Za-z0-9_]+)\s+is empty", condition)
    if match:
        value = prev.get(match.group(1))
        return value is None or value == [] or value == "" or value == 0
    raise SkillError(f"unsupported stop_condition: {condition!r}")


@dataclass(slots=True)
class SkillRunResult:
    skill: str
    steps_run: list[dict[str, Any]]
    completed: bool
    stopped_reason: str | None = None
    last_output: dict[str, Any] | None = None


class SkillRunner:
    """Executes skill steps through the gate + executor, per step."""

    def __init__(
        self,
        *,
        skills: dict[str, Skill],
        executor: Any,
        gate: Any | None = None,
    ):
        self._skills = skills
        self._executor = executor
        self._gate = gate

    def available(self) -> list[Skill]:
        return list(self._skills.values())

    def run(self, skill_name: str, params: dict[str, Any]) -> SkillRunResult:
        skill = self._skills.get(skill_name)
        if skill is None:
            raise SkillError(f"unknown skill {skill_name!r}")
        ctx: dict[str, Any] = dict(params)
        prev: dict[str, Any] = {}
        steps_run: list[dict[str, Any]] = []

        for i, step in enumerate(skill.steps):
            # A DENY (or denied ASK) at any step stops the whole skill —
            # never continue silently past a refusal.
            if self._gate is not None:
                target = _substitute(
                    str(step.params.get("url") or step.params.get("target") or step.params.get("domain") or ""),
                    ctx, prev,
                )
                decision = self._gate.check(step.tool, target)
                if decision.decision == "deny":
                    return SkillRunResult(
                        skill=skill_name,
                        steps_run=steps_run,
                        completed=False,
                        stopped_reason=(
                            f"step {i} ({step.tool}) denied by permission gate: {decision.reason}"
                        ),
                        last_output=prev or None,
                    )
                if decision.decision == "ask":
                    approved, approver = self._gate.resolve_ask(
                        step.tool, target, decision.tier, rationale=f"skill {skill_name} step {i}"
                    )
                    if not approved:
                        return SkillRunResult(
                            skill=skill_name,
                            steps_run=steps_run,
                            completed=False,
                            stopped_reason=(
                                f"step {i} ({step.tool}) denied by operator ({approver})"
                            ),
                            last_output=prev or None,
                        )

            call_params = {
                key: _substitute(value, ctx, prev) for key, value in step.params.items()
            }
            result = self._executor.run(step.tool, json.dumps(call_params))
            steps_run.append({"step": i, "tool": step.tool, "ok": result.ok})
            prev = dict(result.output) if isinstance(result.output, dict) else {"output": result.output}
            ctx.update(prev)

            if step.stop_condition and _evaluate_stop(step.stop_condition, prev):
                return SkillRunResult(
                    skill=skill_name,
                    steps_run=steps_run,
                    completed=False,
                    stopped_reason=f"stop_condition met at step {i}: {step.stop_condition}",
                    last_output=prev,
                )

        return SkillRunResult(
            skill=skill_name, steps_run=steps_run, completed=True, last_output=prev or None
        )


import json  # noqa: E402  (used by runner; kept at end to avoid shadowing)
