"""nuclei JSONL → vulnerability facts."""
from __future__ import annotations

import json

from frex.dispatcher.ingest.base import IngestedFact


def ingest_nuclei(jsonl_data: str) -> list[IngestedFact]:
    facts: list[IngestedFact] = []
    for line in jsonl_data.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue  # skip malformed lines; nuclei JSONL can interleave
        # nuclei emits either a flat result or a wrapped {"data": ...}
        result = event.get("data", event) if isinstance(event, dict) else {}
        if not isinstance(result, dict):
            continue
        # Skip non-result events (info, errors).
        if result.get("type") not in (None, "http", "network", "dns", "file", "ssl"):
            continue
        template_id = result.get("template-id") or result.get("templateID") or ""
        name = result.get("info") or {}
        if isinstance(name, dict):
            severity = (name.get("severity") or "info").lower()
            vuln_name = name.get("name") or template_id
        else:
            severity, vuln_name = "info", str(name) or template_id
        host = result.get("host") or result.get("matched") or ""
        if not template_id and not vuln_name:
            continue
        description = (
            f"nuclei [{severity}] {vuln_name}"
            + (f" ({template_id})" if template_id else "")
            + f" on {host}"
        )
        facts.append(
            IngestedFact(
                description=description,
                kind="vulnerability" if "vulnerability" in str(result.get("type", "")) or severity in ("high", "critical", "medium", "low") else "observation",
                subject={
                    "host": host,
                    "template_id": template_id,
                    "severity": severity,
                    "vuln_name": vuln_name,
                },
                match_value=f"nuclei [{severity}] {template_id}",
            )
        )
    return facts
