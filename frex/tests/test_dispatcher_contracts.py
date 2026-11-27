from __future__ import annotations

import pytest

from frex.dispatcher.contracts import (
    validate_bootstrap_conclude_payload,
    validate_bootstrap_execute_payload,
    validate_explore_payload,
    validate_reason_payload,
)


# --- bootstrap execute -------------------------------------------------------


def test_bootstrap_execute_wrapped_payload():
    kind, data = validate_bootstrap_execute_payload(
        {
            "accepted": True,
            "data": {
                "fact": {"description": " ssh open "},
                "complete": {"description": "root shell"},
            },
        }
    )
    assert kind == "complete"
    assert data == {
        "fact_description": "ssh open",
        "complete_description": "root shell",
    }


def test_bootstrap_execute_unwrapped_payload():
    kind, data = validate_bootstrap_execute_payload(
        {
            "fact": {"description": "ssh open"},
            "complete": {"description": "root shell"},
        }
    )
    assert kind == "complete"
    assert data["fact_description"] == "ssh open"


def test_bootstrap_execute_rejected():
    assert validate_bootstrap_execute_payload(
        {"accepted": False, "reason": "no"}
    ) == ("rejected", None)


@pytest.mark.parametrize(
    "payload",
    [
        {"accepted": True, "data": {"fact": {"description": "x"}}},
        {"accepted": True, "data": {"fact": {"description": "x"}, "complete": {}}},
        {"accepted": True, "data": {"fact": {"description": "  "}, "complete": {"description": "y"}}},
        {"accepted": True, "data": {"fact": {}, "complete": {"description": "y"}}},
        {"accepted": True, "data": {"fact": "not-an-object", "complete": {"description": "y"}}},
        {"accepted": True, "data": "not-an-object"},
        {"unexpected": True},
    ],
)
def test_bootstrap_execute_rejects_invalid_payload(payload):
    with pytest.raises(ValueError):
        validate_bootstrap_execute_payload(payload)


# --- bootstrap conclude ------------------------------------------------------


def test_bootstrap_conclude_fact():
    kind, description = validate_bootstrap_conclude_payload(
        {"accepted": True, "data": {"fact": {"description": "ssh open"}}}
    )
    assert kind == "fact"
    assert description == "ssh open"


def test_bootstrap_conclude_allows_complete_key():
    kind, description = validate_bootstrap_conclude_payload(
        {
            "fact": {"description": "ssh open"},
            "complete": {"description": "ignored"},
        }
    )
    assert kind == "fact"
    assert description == "ssh open"


def test_bootstrap_conclude_rejects_extra_keys():
    with pytest.raises(ValueError, match="unexpected keys"):
        validate_bootstrap_conclude_payload(
            {
                "accepted": True,
                "data": {
                    "fact": {"description": "ssh open"},
                    "extra": 1,
                },
            }
        )


def test_bootstrap_conclude_rejected():
    assert validate_bootstrap_conclude_payload({"accepted": False}) == (
        "rejected",
        None,
    )


# --- explore -----------------------------------------------------------------


def test_explore_wrapped_payload():
    assert validate_explore_payload(
        {"accepted": True, "data": {"description": " port 22 "}}
    ) == ("fact", "port 22")


def test_explore_unwrapped_payload():
    assert validate_explore_payload({"description": "port 22"}) == (
        "fact",
        "port 22",
    )


def test_explore_rejected():
    assert validate_explore_payload({"accepted": False}) == ("rejected", None)


@pytest.mark.parametrize(
    "payload",
    [
        {"accepted": True, "data": {}},
        {"accepted": True, "data": {"description": "  "}},
        {"accepted": True, "data": {"description": 3}},
        {"accepted": True, "data": []},
        {},
    ],
)
def test_explore_rejects_invalid_payload(payload):
    with pytest.raises(ValueError):
        validate_explore_payload(payload)


# --- reason ------------------------------------------------------------------


def test_reason_complete():
    kind, data = validate_reason_payload(
        {
            "accepted": True,
            "data": {"complete": {"from": ["f001"], "description": "done"}},
        },
        open_intents_empty=False,
        max_intents=3,
    )
    assert kind == "complete"
    assert data == {"from": ["f001"], "description": "done"}


def test_reason_intents():
    kind, data = validate_reason_payload(
        {
            "accepted": True,
            "data": {
                "intents": [
                    {"from": ["f001"], "description": "one"},
                    {"from": ["origin"], "description": "two"},
                ]
            },
        },
        open_intents_empty=True,
        max_intents=3,
    )
    assert kind == "intents"
    assert len(data) == 2


def test_reason_singular_intent_backward_compat():
    kind, data = validate_reason_payload(
        {"intent": {"from": ["f001"], "description": "one"}},
        open_intents_empty=True,
        max_intents=3,
    )
    assert kind == "intents"
    assert data == [{"from": ["f001"], "description": "one"}]


def test_reason_truncates_to_max_intents():
    intents = [
        {"from": ["f001"], "description": f"intent {index}"} for index in range(5)
    ]
    kind, data = validate_reason_payload(
        {"accepted": True, "data": {"intents": intents}},
        open_intents_empty=True,
        max_intents=2,
    )
    assert kind == "intents"
    assert data == intents[:2]


def test_reason_noop_when_open_intents_exist():
    assert validate_reason_payload(
        {"accepted": True, "data": {}},
        open_intents_empty=False,
        max_intents=3,
    ) == ("noop", None)


def test_reason_rejected():
    assert validate_reason_payload(
        {"accepted": False, "reason": "no"},
        open_intents_empty=False,
        max_intents=3,
    ) == ("rejected", None)


@pytest.mark.parametrize(
    "payload",
    [
        {"accepted": True, "data": {}},
        {"accepted": True, "data": {"complete": {"from": ["f001"], "description": "x"}, "intents": []}},
        {"accepted": True, "data": {"complete": {"from": ["f001"]}}},
        {"accepted": True, "data": {"intents": [{"from": ["f001"]}]}},
        {"accepted": True, "data": {"intents": []}},
        {"accepted": True, "data": "nope"},
        {},
    ],
)
def test_reason_rejects_invalid_payload(payload):
    with pytest.raises(ValueError):
        validate_reason_payload(
            payload,
            open_intents_empty=True,
            max_intents=3,
        )
