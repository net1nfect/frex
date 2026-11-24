#!/usr/bin/env python3
"""Compare benchmark results across model labels.

Reads the JSON files bench/run.py produces and prints a per-scenario
comparison table plus a JSON summary for machine diffing.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def load_results(results_dir: Path) -> list[dict]:
    results = []
    for path in sorted(results_dir.rglob("*.json")):
        try:
            results.append(json.loads(path.read_text()))
        except ValueError:
            print(f"warning: skipping malformed {path}")
    return results


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--results", type=Path, default=Path("bench/results"))
    args = parser.parse_args()

    results = load_results(args.results)
    if not results:
        print("no results found")
        return

    by_scenario: dict[str, list[dict]] = {}
    for r in results:
        by_scenario.setdefault(r["scenario"], []).append(r)

    for scenario, runs in sorted(by_scenario.items()):
        print(f"\n=== {scenario} ===")
        print(
            f"{'label':<24} {'status':<10} {'wall_s':>8} {'verified':>9} "
            f"{'tokens':>10} {'cost_usd':>9} {'FP':>4}"
        )
        for r in sorted(runs, key=lambda x: x["label"]):
            m = r["metrics"]
            print(
                f"{r['label']:<24} {r['status']:<10} {r['wall_seconds']:>8} "
                f"{m['facts_verified']:>9} {m['tokens_in'] + m['tokens_out']:>10} "
                f"{m['cost_usd']:>9.4f} {m['false_positive_signal']:>4}"
            )

    print(json.dumps({"runs": len(results)}, indent=2))


if __name__ == "__main__":
    main()
