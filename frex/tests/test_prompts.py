from __future__ import annotations

from frex.dispatcher.runtime.prompts import build_system_prompt, condense_facts
from frex.dispatcher.runtime.scope import Scope


def test_system_prompt_contains_xml_sections_and_scope():
    prompt = build_system_prompt(
        role_text="recon agent",
        scope=Scope(domains=["lab.test"], cidrs=["10.0.0.0/24"], exclude_domains=["admin.lab.test"]),
        stop_conditions={"max_steps": 25},
    )
    assert "<role>\nrecon agent\n</role>" in prompt
    assert "<scope>" in prompt
    assert "lab.test" in prompt
    assert "10.0.0.0/24" in prompt
    assert "admin.lab.test" in prompt
    assert "rejected by the executor" in prompt  # enforcement stays in code
    assert "<stop_conditions>" in prompt
    assert "25" in prompt


def test_system_prompt_without_scope():
    prompt = build_system_prompt(role_text="r", scope=None, stop_conditions={})
    assert "no scope configured" in prompt


def test_condense_facts_prioritizes_verified_and_groups_hosts():
    facts = [
        {"id": "f1", "status": "hypothesis", "description": "maybe login bypass"},
        {"id": "f2", "status": "verified", "description": "RCE via upload",
         "subject": {"host_key": "10.0.0.5:443"}},
        {"id": "f3", "status": "verified", "description": "port 22 open",
         "subject": {"host_key": "10.0.0.5:22"}},
    ]
    text = condense_facts(facts)
    assert "3 total (2 verified)" in text
    assert "10.0.0.5:443 (1 facts)" in text
    # Verified facts listed before hypotheses.
    assert text.index("RCE via upload") < text.index("maybe login bypass")
    assert "[f2]" in text  # ids present for get_fact_detail retrieval


def test_condense_facts_empty():
    assert condense_facts([]) == "no facts recorded yet"
