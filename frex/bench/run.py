#!/usr/bin/env python3
"""Run one benchmark scenario against a live Frex deployment.

Creates the project, runs the engine, and acts as the goal referee:
when a fact claimed by the model matches the scenario's success
criteria, the referee verifies it via the operator verification
endpoint and completes the project. The engine does the work; the
referee only judges the goal. Metrics recorded: time-to-goal, verified
facts, cost, tokens, and false positives (verified facts later
refuted).
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml

REFeree_WORKER = "bench-referee"
POLL_SECONDS = 5


def project_facts(base: str, project_id: str) -> list[dict]:
    detail = requests.get(f"{base}/projects/{project_id}").json()
    return detail.get("facts", [])


def criteria_texts(scenario: dict) -> list[str]:
    return [
        c["contains"].lower()
        for c in scenario.get("success_criteria", [])
        if isinstance(c, dict) and "contains" in c
    ]


def verify_fact(base: str, project_id: str, fact_id: str, note: str) -> bool:
    response = requests.post(
        f"{base}/projects/{project_id}/facts/{fact_id}/verifications",
        json={
            "method": "operator",
            "result": "pass",
            "verifier_model": REFeree_WORKER,
            "expected": "goal criteria",
            "observed": note,
        },
    )
    return response.status_code == 201


def complete_project(base: str, project_id: str, fact_ids: list[str]) -> bool:
    response = requests.post(
        f"{base}/projects/{project_id}/complete",
        json={
            "from": fact_ids,
            "description": "benchmark success criteria met",
            "worker": REFeree_WORKER,
        },
    )
    return response.status_code in (200, 201)


def referee_poll(base: str, project_id: str, scenario: dict) -> bool:
    """Verify criteria-matching hypothesis facts and complete on success.

    Returns True when the project reached the goal.
    """
    facts = project_facts(base, project_id)
    criteria = criteria_texts(scenario)
    if not criteria:
        return False

    changed = False
    verified_ids: list[str] = []
    verified_text: list[str] = []
    for fact in facts:
        description = (fact.get("description") or "").lower()
        if fact.get("status") == "verified":
            verified_ids.append(fact["id"])
            verified_text.append(description)
        elif fact.get("status") == "hypothesis" and any(
            c in description for c in criteria
        ):
            if verify_fact(base, project_id, fact["id"], description[:200]):
                changed = True
                verified_ids.append(fact["id"])
                verified_text.append(description)

    if changed:
        # Re-read: the verification endpoint may have flipped statuses we
        # did not anticipate; judge only from the server's view.
        facts = project_facts(base, project_id)
        verified_ids = [f["id"] for f in facts if f.get("status") == "verified"]
        verified_text = [
            (f.get("description") or "").lower()
            for f in facts
            if f.get("status") == "verified"
        ]

    if verified_text and all(any(c in t for t in verified_text) for c in criteria):
        # `from` must cite real evidence facts: the server rejects the
        # synthetic origin/goal facts as intent sources.
        evidence_ids = [fid for fid in verified_ids if fid not in ("goal", "origin")]
        if evidence_ids:
            return complete_project(base, project_id, evidence_ids)
    return False


def wait_for_completion(
    base: str, project_id: str, deadline_seconds: int, scenario: dict
) -> tuple[str, dict]:
    """Poll until the project completes or the scenario deadline passes."""
    started = time.monotonic()
    meta: dict = {}
    while time.monotonic() - started < deadline_seconds:
        meta = requests.get(f"{base}/projects/{project_id}").json()["project"]
        if meta["status"] == "completed":
            return "completed", meta
        if referee_poll(base, project_id, scenario):
            continue
        time.sleep(POLL_SECONDS)
    meta = requests.get(f"{base}/projects/{project_id}").json()["project"]
    return ("completed" if meta["status"] == "completed" else "timeout"), meta


def collect_metrics(base: str, project_id: str, scenario: dict) -> dict:
    export = requests.get(
        f"{base}/projects/{project_id}/export", params={"format": "yaml"}
    )
    graph = yaml.safe_load(export.text) or {}
    facts = graph.get("facts", []) or []
    verified = [f for f in facts if f.get("status") == "verified"]
    refuted = [f for f in facts if f.get("status") == "refuted"]

    usage = requests.get(f"{base}/projects/{project_id}/usage").json()["totals"]
    runs = requests.get(f"{base}/projects/{project_id}/runs").json()

    goal_text = scenario.get("goal", "").lower()
    verified_descriptions = " ".join(f["description"].lower() for f in verified)
    criteria_met = all(
        c.get("contains", "").lower() in verified_descriptions
        for c in scenario.get("success_criteria", [])
        if isinstance(c, dict) and "contains" in c
    )

    return {
        "facts_total": len(facts),
        "facts_verified": len(verified),
        "facts_refuted": len(refuted),
        # False positive proxy: claims that got verified then refuted
        # would show as refuted after having been verified; refuted count
        # is the measurable part.
        "false_positive_signal": len(refuted),
        "tokens_in": usage.get("tokens_in", 0),
        "tokens_out": usage.get("tokens_out", 0),
        "cost_usd": usage.get("cost_usd", 0),
        "tool_calls": usage.get("runs", 0),
        "run_records": len(runs if isinstance(runs, list) else runs.get("runs", [])),
        "success_criteria_met": criteria_met,
        "goal_in_verified_facts": goal_text in verified_descriptions,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", required=True, type=Path)
    parser.add_argument("--server", default="http://127.0.0.1:8000")
    parser.add_argument("--label", required=True, help="Model/config label for the results file")
    parser.add_argument("--out", type=Path, default=Path("bench/results"))
    args = parser.parse_args()

    scenario = yaml.safe_load(args.scenario.read_text())
    base = args.server.rstrip("/")

    project = requests.post(
        f"{base}/projects",
        json={
            "title": f"bench-{args.label}-{args.scenario.stem}",
            "origin": scenario["origin"],
            "goal": scenario["goal"],
        },
    ).json()["project"]["id"]

    # Kick off exploration: create the initial intent from origin.
    requests.post(
        f"{base}/projects/{project}/intents",
        json={"from": ["origin"], "description": scenario["goal"], "creator": "bench"},
    )

    started = datetime.now(timezone.utc)
    status, _ = wait_for_completion(
        base, project, scenario.get("max_wall_seconds", 1800), scenario
    )
    wall_seconds = (datetime.now(timezone.utc) - started).total_seconds()

    result = {
        "label": args.label,
        "scenario": args.scenario.stem,
        "project_id": project,
        "status": status,
        "wall_seconds": round(wall_seconds, 1),
        "metrics": collect_metrics(base, project, scenario),
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }

    out_dir = args.out / args.label
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"{args.scenario.stem}-{int(time.time())}.json"
    out_file.write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))
    print(f"saved: {out_file}")


if __name__ == "__main__":
    main()
