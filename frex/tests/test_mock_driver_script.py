from __future__ import annotations

import json
import subprocess
import sys

import pytest

from frex.dispatcher.config import WorkerConfig
from frex.dispatcher.contracts import (
    parse_json_output,
    validate_bootstrap_conclude_payload,
    validate_bootstrap_execute_payload,
    validate_explore_payload,
    validate_reason_payload,
)
from frex.dispatcher.workers.adapters.mock import MockDriver, _SCRIPT


def run_mock(phase: str, prompt: dict, outcomes: dict, rules=None):
    behavior = {
        phase: {
            "delay": {"min": 0, "max": 0},
            "outcomes": outcomes,
            "rules": rules or [],
        }
    }
    argv = [
        sys.executable,
        "-c",
        _SCRIPT,
        json.dumps(behavior),
        json.dumps(prompt),
    ]
    return subprocess.run(argv, capture_output=True, text=True, timeout=15)


def test_healthcheck_ok():
    result = run_mock("healthcheck", {"phase": "healthcheck"}, {"ok": 1.0, "fail": 0.0})
    assert result.returncode == 0
    assert result.stdout == ""


def test_healthcheck_fail():
    result = run_mock("healthcheck", {"phase": "healthcheck"}, {"ok": 0.0, "fail": 1.0})
    assert result.returncode == 1


def test_reason_intent_round_trips_through_contract():
    prompt = {"phase": "reason", "fact_ids": ["f001"], "open_intents": [], "max_intents": 3}
    outcomes = {
        "complete": 0.0,
        "intent": 1.0,
        "noop": 0.0,
        "rejected": 0.0,
        "invalid_json": 0.0,
        "invalid_payload": 0.0,
        "command_fail": 0.0,
    }
    result = run_mock("reason", prompt, outcomes)
    assert result.returncode == 0
    payload = parse_json_output(result.stdout)
    kind, data = validate_reason_payload(payload, open_intents_empty=True, max_intents=3)
    assert kind == "intents"
    assert data[0]["from"] == ["f001"]


def test_reason_complete_round_trips_through_contract():
    prompt = {"phase": "reason", "fact_ids": ["f001"], "open_intents": [], "max_intents": 3}
    outcomes = {
        "complete": 1.0,
        "intent": 0.0,
        "noop": 0.0,
        "rejected": 0.0,
        "invalid_json": 0.0,
        "invalid_payload": 0.0,
        "command_fail": 0.0,
    }
    result = run_mock("reason", prompt, outcomes)
    payload = parse_json_output(result.stdout)
    kind, data = validate_reason_payload(payload, open_intents_empty=True, max_intents=3)
    assert kind == "complete"
    assert data["from"] == ["f001"]


def test_reason_noop_when_open_intents_exist():
    prompt = {"phase": "reason", "fact_ids": ["f001"], "open_intents": ["i001"], "max_intents": 3}
    outcomes = {
        "complete": 0.0,
        "intent": 0.0,
        "noop": 1.0,
        "rejected": 0.0,
        "invalid_json": 0.0,
        "invalid_payload": 0.0,
        "command_fail": 0.0,
    }
    result = run_mock("reason", prompt, outcomes)
    payload = parse_json_output(result.stdout)
    assert validate_reason_payload(payload, open_intents_empty=False, max_intents=3) == (
        "noop",
        None,
    )


def test_reason_rule_forces_outcome():
    prompt = {"phase": "reason", "fact_ids": ["f001", "f002"], "open_intents": [], "max_intents": 3}
    outcomes = {
        "complete": 1.0,
        "intent": 0.0,
        "noop": 0.0,
        "rejected": 0.0,
        "invalid_json": 0.0,
        "invalid_payload": 0.0,
        "command_fail": 0.0,
    }
    rules = [{"force": "noop", "fact_ids_gte": 2}]
    result = run_mock("reason", prompt, outcomes, rules)
    payload = parse_json_output(result.stdout)
    assert validate_reason_payload(payload, open_intents_empty=False, max_intents=3) == (
        "noop",
        None,
    )


def test_reason_exits_when_no_legal_outcome():
    prompt = {"phase": "reason", "fact_ids": [], "open_intents": [], "max_intents": 3}
    outcomes = {
        "complete": 0.0,
        "intent": 0.0,
        "noop": 1.0,
        "rejected": 0.0,
        "invalid_json": 0.0,
        "invalid_payload": 0.0,
        "command_fail": 0.0,
    }
    result = run_mock("reason", prompt, outcomes)
    assert result.returncode == 2
    assert "no legal outcomes" in result.stderr


def test_bootstrap_fact_requires_conclude_fallback():
    outcomes = {
        "complete": 0.0,
        "fact": 1.0,
        "rejected": 0.0,
        "invalid_json": 0.0,
        "invalid_payload": 0.0,
        "command_fail": 0.0,
    }
    result = run_mock("bootstrap", {"phase": "bootstrap"}, outcomes)
    payload = parse_json_output(result.stdout)
    with pytest.raises(ValueError, match="complete is required"):
        validate_bootstrap_execute_payload(payload)


def test_bootstrap_complete_round_trips():
    outcomes = {
        "complete": 1.0,
        "fact": 0.0,
        "rejected": 0.0,
        "invalid_json": 0.0,
        "invalid_payload": 0.0,
        "command_fail": 0.0,
    }
    result = run_mock("bootstrap", {"phase": "bootstrap"}, outcomes)
    payload = parse_json_output(result.stdout)
    kind, data = validate_bootstrap_execute_payload(payload)
    assert kind == "complete"
    assert data["complete_description"] == "mock bootstrap complete from fact"


def test_bootstrap_conclude_round_trips():
    outcomes = {
        "fact": 1.0,
        "rejected": 0.0,
        "invalid_json": 0.0,
        "invalid_payload": 0.0,
        "command_fail": 0.0,
    }
    result = run_mock("bootstrap_conclude", {"phase": "bootstrap_conclude"}, outcomes)
    payload = parse_json_output(result.stdout)
    kind, description = validate_bootstrap_conclude_payload(payload)
    assert kind == "fact"
    assert description == "mock fact for bootstrap_conclude"


def test_explore_fact_round_trips():
    outcomes = {
        "fact": 1.0,
        "rejected": 0.0,
        "invalid_json": 0.0,
        "invalid_payload": 0.0,
        "command_fail": 0.0,
    }
    result = run_mock("explore_execute", {"phase": "explore_execute", "intent_id": "i007"}, outcomes)
    payload = parse_json_output(result.stdout)
    assert validate_explore_payload(payload) == ("fact", "mock fact for i007")


def test_explore_invalid_json_exits_zero():
    outcomes = {
        "fact": 0.0,
        "rejected": 0.0,
        "invalid_json": 1.0,
        "invalid_payload": 0.0,
        "command_fail": 0.0,
    }
    result = run_mock("explore_execute", {"phase": "explore_execute"}, outcomes)
    assert result.returncode == 0
    with pytest.raises(ValueError):
        parse_json_output(result.stdout)


def test_explore_command_fail_exits_one():
    outcomes = {
        "fact": 0.0,
        "rejected": 0.0,
        "invalid_json": 0.0,
        "invalid_payload": 0.0,
        "command_fail": 1.0,
    }
    result = run_mock("explore_execute", {"phase": "explore_execute"}, outcomes)
    assert result.returncode == 1


def test_mock_driver_builds_python_argv():
    worker = WorkerConfig(
        name="mock-1",
        type="mock",
        task_types=["explore"],
        max_running=1,
        priority=0,
    )
    argv = MockDriver().build_healthcheck(worker)
    assert argv[0] == "python3"
    assert argv[1] == "-c"
    assert argv[4] == '{"phase":"healthcheck"}'
