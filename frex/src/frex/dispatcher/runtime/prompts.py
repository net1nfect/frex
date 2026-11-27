"""System prompt construction and state summarization for the agent loop.

Context layering: the system prompt is the fixed layer (role, scope
rules, stop conditions — cacheable between turns), the state summary is
the regenerated layer (condensed fact-graph view, not a dump), and raw
tool output is disposable — older turns collapse to one line each.

Scope enforcement stays in code (the scope guard); the <scope> section
exists so the model does not *plan* out-of-scope work, not as the
enforcement mechanism.
"""
from __future__ import annotations

from typing import Any


def build_system_prompt(
    *,
    role_text: str,
    scope: Any | None,
    stop_conditions: dict[str, int],
) -> str:
    """Render the fixed system prompt with XML-tagged sections."""
    sections = [f"<role>\n{role_text}\n</role>"]

    if scope is not None:
        lines = []
        if scope.domains:
            lines.append("domains: " + ", ".join(scope.domains))
        if scope.cidrs:
            lines.append("cidrs: " + ", ".join(scope.cidrs))
        if scope.exclude_domains:
            lines.append("excluded domains: " + ", ".join(scope.exclude_domains))
        if scope.exclude_cidrs:
            lines.append("excluded cidrs: " + ", ".join(scope.exclude_cidrs))
        if scope.notes:
            lines.append("notes: " + scope.notes)
        scope_body = "\n".join(lines) if lines else "no restrictions configured"
    else:
        scope_body = "no scope configured for this operation"
    sections.append(
        "<scope>\n"
        "Targets outside this scope are rejected by the executor before any\n"
        "tool runs. Do not plan against them; pick a different target.\n"
        + scope_body
        + "\n</scope>"
    )

    conditions = [
        f"- Stop when the turn count reaches {stop_conditions.get('max_steps', 30)}"
        f"- Stop when no new findings appear for {stop_conditions.get('stall_limit', 5)} consecutive turns"
        "- Stop when every host in scope has been probed and findings are recorded"
    ]
    sections.append(
        "<stop_conditions>\n" + "\n".join(conditions) + "\n</stop_conditions>"
    )
    sections.append(
        "<output_format>\n"
        "Call tools with structured arguments. Record concrete discoveries\n"
        "with submit_facts (use host_key to correlate facts about the same\n"
        "service) and end the task with submit_result.\n"
        "</output_format>"
    )
    return "\n\n".join(sections)


def condense_facts(facts: list[dict[str, Any]], *, max_chars: int = 1500) -> str:
    """Condensed fact-graph view for the state-summary layer.

    Top findings first (verified before hypotheses), grouped coverage of
    hosts — never a full dump; the model retrieves details on demand via
    get_fact_detail.
    """
    if not facts:
        return "no facts recorded yet"
    verified = [f for f in facts if f.get("status") == "verified"]
    hypotheses = [f for f in facts if f.get("status") != "verified"]

    hosts: dict[str, int] = {}
    for fact in facts:
        subject = fact.get("subject") or {}
        host_key = subject.get("host_key") if isinstance(subject, dict) else None
        if host_key:
            hosts[host_key] = hosts.get(host_key, 0) + 1

    lines = [f"facts: {len(facts)} total ({len(verified)} verified)"]
    if hosts:
        covered = ", ".join(f"{h} ({c} facts)" for h, c in sorted(hosts.items()))
        lines.append(f"covered services: {covered}")
    for fact in (verified + hypotheses)[:10]:
        marker = "✓" if fact.get("status") == "verified" else "?"
        description = str(fact.get("description", ""))[:120]
        lines.append(f"{marker} [{fact.get('id', '?')}] {description}")
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n...[condensed]"
    return text
