#!/usr/bin/env python3
"""Run one benchmark scenario against a live Cairn deployment.

Creates the project, waits for completion (goal reached or budget
deadline), then records the metrics that matter: time-to-goal, verified
facts, cost, tokens, and false positives (verified facts later refuted).
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml


def wait_for_completion(
    base: str, project_id: str, deadline_seconds: int
) -> tuple[str, dict]:
    """Poll until the project completes or the scenario deadline passes."""
    started = time.monotonic()
    while time.monotonic() - started < deadline_seconds:
        meta = requests.get(f"{base}/projects/{project_id}").json()["project"]
        if meta["status"] == "completed":
            return "completed", meta
        time.sleep(5)
    return "timeout", meta


def collect_metrics(base: str, project_id: str, scenario: dict) -> dict:
    export = requests.get(
        f"{base}/projects/{project_id}/export", params={"format": "yaml"}
    )
    import yaml as yaml_lib

    graph = yaml_lib.safe_load(export.text)
    facts = graph.get("facts", [])
    verified = [f for f in facts if f.get("status") == "verified"]
    refuted = [f for f in facts if f.get("status") == "refuted"]

    usage = requests.get(f"{base}/projects/{project_id}/usage").json()["totals"]
    runs = requests.get(f"{base}/projects/{project_id}/runs", params={"limit": 2000}).json()

    goal_text = scenario.get("goal", "").lower()
    success_criteria = scenario.get("success_criteria", [])
    verified_descriptions = " ".join(f["description"].lower() for f in verified)
    criteria_met = all(
        c.get("contains", "").lower() in verified_descriptions
        for c in success_criteria
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

    intents = requests.get(f"{base}/projects/{project}").json()
    # Kick off exploration: create the initial intent from origin.
    requests.post(
        f"{base}/projects/{project}/intents",
        json={"from": ["origin"], "description": scenario["goal"], "creator": "bench"},
    )

    started = datetime.now(timezone.utc)
    status, _ = wait_for_completion(
        base, project, scenario.get("max_wall_seconds", 1800)
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
