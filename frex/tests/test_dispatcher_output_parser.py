from __future__ import annotations

import pytest

from frex.dispatcher.output_parser import extract_json_object


def test_parses_raw_json_object():
    assert extract_json_object('{"description": "port 22"}') == {
        "description": "port 22"
    }


def test_parses_fenced_json_block():
    text = 'Here is the result:\n```json\n{"accepted": true}\n```\nDone.'
    assert extract_json_object(text) == {"accepted": True}


def test_parses_fenced_block_without_language_tag():
    text = "```\n{\"accepted\": false}\n```"
    assert extract_json_object(text) == {"accepted": False}


def test_parses_object_embedded_in_prose():
    text = 'I ran the scan. {"fact": {"description": "ssh open"}} That is all.'
    assert extract_json_object(text) == {"fact": {"description": "ssh open"}}


def test_parses_nested_object():
    text = '{"data": {"intents": [{"from": ["origin"]}]}}'
    assert extract_json_object(text) == {
        "data": {"intents": [{"from": ["origin"]}]}
    }


def test_returns_first_valid_object():
    text = '{"first": 1} {"second": 2}'
    assert extract_json_object(text) == {"first": 1}


def test_raises_when_no_object_present():
    with pytest.raises(ValueError, match="no JSON object found"):
        extract_json_object("nothing to see here")


def test_raises_on_invalid_json_only():
    with pytest.raises(ValueError, match="no JSON object found"):
        extract_json_object("{invalid json")


def test_ignores_non_object_json_values():
    with pytest.raises(ValueError, match="no JSON object found"):
        extract_json_object("[1, 2, 3]")


def test_handles_unicode():
    assert extract_json_object('{"description": "port 22 ouverte"}') == {
        "description": "port 22 ouverte"
    }
