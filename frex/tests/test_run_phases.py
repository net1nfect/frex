"""Every RunRecorder phase must be one the server accepts.

The dispatcher and server are separate processes with separate schemas;
a typo in a phase literal only shows up as a 422 at runtime, where the
run row is silently lost. Scan the task modules instead.
"""
from __future__ import annotations

import ast
from pathlib import Path

from frex.server.models import RUN_PHASES


TASKS_DIR = Path(__file__).resolve().parent.parent / "src" / "frex" / "dispatcher" / "tasks"


def test_all_task_run_phases_are_valid():
    invalid: list[tuple[str, int, str]] = []
    for path in sorted(TASKS_DIR.glob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for keyword in node.keywords:
                if keyword.arg != "phase":
                    continue
                if isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str):
                    if keyword.value.value not in RUN_PHASES:
                        invalid.append((path.name, keyword.value.lineno, keyword.value.value))
    assert not invalid, f"invalid task_runs phases: {invalid}"
