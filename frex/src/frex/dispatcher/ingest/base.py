"""Shared plumbing for ingestors: post facts through the conclude API.

Ingested facts are born verified — the tool output is the evidence,
unlike agent claims which start as hypotheses. They attach to an
intent anchored on origin so the graph stays connected.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from frex.dispatcher.protocol.client import FrexClient


@dataclass(slots=True)
class IngestedFact:
    description: str
    kind: str = "observation"
    subject: dict[str, Any] = field(default_factory=dict)
    match_rule: str = "contains"
    match_value: str = ""


def post_facts(
    client: FrexClient,
    project_id: str,
    facts: list[IngestedFact],
) -> tuple[int, int]:
    """Post facts via one ingest intent anchored on origin.

    Returns (posted, failed). A failed conclude is skipped — a partial
    ingest beats none, and the operator can rerun the ingest.
    """
    response = client.create_intent(
        project_id,
        from_ids=["origin"],
        description="ingest: tool output import",
        creator="ingest",
    )
    if not response.ok:
        raise RuntimeError(f"failed to create ingest intent: {response.status_code}")
    intent_id = response.data["id"]

    posted = failed = 0
    for fact in facts:
        result = client.conclude(
            project_id,
            intent_id,
            "ingest",
            fact.description,
            kind=fact.kind,
            subject=fact.subject or None,
            confidence=1.0,
            match_rule=fact.match_rule,
            match_value=fact.match_value or None,
        )
        if result.ok:
            posted += 1
        else:
            failed += 1
    return posted, failed
