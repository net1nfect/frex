from __future__ import annotations

import pytest


@pytest.fixture
def project_id(client):
    response = client.post(
        "/projects",
        json={"title": "lab", "origin": "start", "goal": "finish"},
    )
    assert response.status_code == 201
    return response.json()["project"]["id"]


@pytest.fixture
def intent_id(client, project_id):
    response = client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["origin"], "description": "probe", "creator": "alice"},
    )
    assert response.status_code == 201
    return response.json()["id"]


@pytest.fixture
def run_id(client, project_id, intent_id):
    response = client.post(
        f"/projects/{project_id}/runs",
        json={
            "task_type": "explore",
            "worker": "kali-01",
            "command": "nmap -sV 10.0.0.5",
            "exit_code": 0,
            "status": "success",
            "stdout": "22/tcp open ssh",
        },
    )
    assert response.status_code == 201
    return response.json()["id"]


def _conclude(client, project_id, intent_id, **overrides):
    payload = {"worker": "alice", "description": "port 22 is open"}
    payload.update(overrides)
    return client.post(
        f"/projects/{project_id}/intents/{intent_id}/conclude", json=payload
    )


def test_conclude_without_run_stays_hypothesis_without_evidence(client, project_id, intent_id):
    response = _conclude(client, project_id, intent_id)
    assert response.status_code == 200
    body = response.json()
    assert body["fact"]["status"] == "hypothesis"
    assert body["fact"]["kind"] == "observation"
    assert body["evidence"] == []


def test_conclude_with_run_records_evidence(client, project_id, intent_id, run_id):
    response = _conclude(
        client,
        project_id,
        intent_id,
        run_id=run_id,
        kind="observation",
        confidence=0.9,
        match_value="22/tcp",
    )
    assert response.status_code == 200
    body = response.json()
    assert body["fact"]["status"] == "hypothesis"
    assert body["fact"]["created_by_run"] == run_id
    assert len(body["evidence"]) == 1
    evidence = body["evidence"][0]
    assert evidence["id"] == "ev_001"
    assert evidence["run_id"] == run_id
    assert evidence["fact_id"] == body["fact"]["id"]
    assert evidence["command"] == "nmap -sV 10.0.0.5"
    assert evidence["exit_code"] == 0
    assert evidence["match_rule"] == "contains"
    assert evidence["match_value"] == "22/tcp"
    assert evidence["stdout_sha256"] is not None
    assert evidence["captured_at"] is not None


def test_conclude_vulnerability_defaults_to_regex(client, project_id, intent_id, run_id):
    response = _conclude(
        client, project_id, intent_id, run_id=run_id, kind="vulnerability"
    )
    assert response.json()["evidence"][0]["match_rule"] == "regex"


def test_conclude_negative_defaults_to_exit_code(client, project_id, intent_id, run_id):
    response = _conclude(
        client, project_id, intent_id, run_id=run_id, kind="negative"
    )
    assert response.json()["evidence"][0]["match_rule"] == "exit_code"


def test_conclude_subject_is_preserved(client, project_id, intent_id, run_id):
    subject = {"host": "dc01", "account": "svc_sql"}
    response = _conclude(
        client, project_id, intent_id, run_id=run_id, kind="credential", subject=subject
    )
    assert response.json()["fact"]["subject"] == subject

    detail = client.get(f"/projects/{project_id}").json()
    fact = next(f for f in detail["facts"] if f["id"].startswith("f") and f["id"] not in ("origin", "goal"))
    assert fact["subject"] == subject


def test_conclude_unknown_run_is_404(client, project_id, intent_id):
    response = _conclude(client, project_id, intent_id, run_id="run_999")
    assert response.status_code == 404
    assert response.json()["detail"] == "Run not found"


def test_conclude_rejects_run_from_other_project(client, project_id, intent_id):
    other = client.post(
        "/projects", json={"title": "b", "origin": "x", "goal": "y"}
    ).json()["project"]["id"]
    foreign_run = client.post(
        f"/projects/{other}/runs",
        json={"task_type": "explore", "worker": "w", "status": "success"},
    ).json()["id"]
    response = _conclude(client, project_id, intent_id, run_id=foreign_run)
    assert response.status_code == 404


def test_conclude_rejects_invalid_kind_and_confidence(client, project_id, intent_id):
    assert _conclude(client, project_id, intent_id, kind="opinion").status_code == 422
    assert _conclude(client, project_id, intent_id, confidence=1.5).status_code == 422
    assert _conclude(client, project_id, intent_id, confidence=-0.1).status_code == 422
    assert _conclude(
        client, project_id, intent_id, match_rule="sounds-right"
    ).status_code == 422


def test_conclude_idempotent_replay_includes_evidence(client, project_id, intent_id, run_id):
    first = _conclude(client, project_id, intent_id, run_id=run_id, request_id="req-1")
    retry = _conclude(client, project_id, intent_id, run_id=run_id, request_id="req-1")
    assert retry.status_code == 200
    assert retry.json() == first.json()
    assert len(retry.json()["evidence"]) == 1


def test_list_facts_filters_by_status_and_kind(client, project_id, intent_id, run_id):
    _conclude(
        client, project_id, intent_id, run_id=run_id, kind="credential", confidence=0.9
    )
    hypotheses = client.get(
        f"/projects/{project_id}/facts", params={"status": "hypothesis"}
    ).json()
    assert [f["kind"] for f in hypotheses] == ["credential"]
    verified = client.get(
        f"/projects/{project_id}/facts", params={"status": "verified"}
    ).json()
    assert {f["id"] for f in verified} == {"origin", "goal"}
    credentials = client.get(
        f"/projects/{project_id}/facts", params={"kind": "credential"}
    ).json()
    assert len(credentials) == 1
    assert credentials[0]["confidence"] == 0.9
    assert client.get(
        f"/projects/{project_id}/facts", params={"status": "bogus"}
    ).json() == []


def test_list_facts_pagination_and_missing_project(client, project_id):
    assert client.get(
        f"/projects/{project_id}/facts", params={"limit": 2, "offset": 1}
    ).json()[0]["id"] == "goal"
    assert client.get("/projects/proj_999/facts").status_code == 404
    assert client.get(
        f"/projects/{project_id}/facts", params={"limit": 1000}
    ).status_code == 422


def test_get_fact_detail_includes_evidence_and_verifications(
    client, project_id, intent_id, run_id
):
    concluded = _conclude(
        client, project_id, intent_id, run_id=run_id, kind="observation"
    ).json()
    fact_id = concluded["fact"]["id"]

    detail = client.get(f"/projects/{project_id}/facts/{fact_id}").json()
    assert detail["fact"]["id"] == fact_id
    assert [e["run_id"] for e in detail["evidence"]] == [run_id]
    assert detail["verifications"] == []

    assert client.get(f"/projects/{project_id}/facts/f999").status_code == 404
    assert client.get("/projects/proj_999/facts/origin").status_code == 404


def test_origin_fact_has_no_evidence(client, project_id):
    detail = client.get(f"/projects/{project_id}/facts/origin").json()
    assert detail["fact"]["status"] == "verified"
    assert detail["evidence"] == []
    assert detail["verifications"] == []


def _verify(client, project_id, fact_id, **overrides):
    payload = {"method": "replay", "result": "pass"}
    payload.update(overrides)
    return client.post(
        f"/projects/{project_id}/facts/{fact_id}/verifications", json=payload
    )


def _first_agent_fact_id(client, project_id):
    facts = client.get(
        f"/projects/{project_id}/facts", params={"kind": "observation"}
    ).json()
    return facts[0]["id"]


def test_verification_pass_verifies_hypothesis(client, project_id, intent_id, run_id):
    _conclude(client, project_id, intent_id, run_id=run_id)
    fact_id = _first_agent_fact_id(client, project_id)
    response = _verify(
        client,
        project_id,
        fact_id,
        run_id=run_id,
        verifier_model="qwen-local",
        expected="contains:22/tcp",
        observed="22/tcp open ssh in output",
        details="replayed in fresh container",
    )
    assert response.status_code == 201
    body = response.json()
    assert body["fact"]["status"] == "verified"
    assert body["verification"]["id"] == "ver_001"
    assert body["verification"]["fact_id"] == fact_id
    assert body["verification"]["result"] == "pass"

    detail = client.get(f"/projects/{project_id}/facts/{fact_id}").json()
    assert len(detail["verifications"]) == 1
    assert client.get(
        f"/projects/{project_id}/facts", params={"status": "verified"}
    ).json()


def test_verification_fail_refutes_hypothesis(client, project_id, intent_id):
    _conclude(client, project_id, intent_id)
    fact_id = _first_agent_fact_id(client, project_id)
    response = _verify(client, project_id, fact_id, result="fail")
    assert response.status_code == 201
    assert response.json()["fact"]["status"] == "refuted"


def test_verification_inconclusive_keeps_status(client, project_id, intent_id):
    _conclude(client, project_id, intent_id)
    fact_id = _first_agent_fact_id(client, project_id)
    response = _verify(client, project_id, fact_id, result="inconclusive")
    assert response.json()["fact"]["status"] == "hypothesis"


def test_verified_then_fail_becomes_disputed(client, project_id, intent_id, run_id):
    _conclude(client, project_id, intent_id, run_id=run_id)
    fact_id = _first_agent_fact_id(client, project_id)
    assert _verify(client, project_id, fact_id, result="pass").status_code == 201
    response = _verify(client, project_id, fact_id, result="fail")
    assert response.status_code == 201
    assert response.json()["fact"]["status"] == "disputed"


def test_disputed_then_pass_returns_to_verified(client, project_id, intent_id, run_id):
    _conclude(client, project_id, intent_id, run_id=run_id)
    fact_id = _first_agent_fact_id(client, project_id)
    _verify(client, project_id, fact_id, result="pass")
    _verify(client, project_id, fact_id, result="fail")
    response = _verify(client, project_id, fact_id, result="pass")
    assert response.json()["fact"]["status"] == "verified"


def test_refuted_then_pass_recovers(client, project_id, intent_id):
    _conclude(client, project_id, intent_id)
    fact_id = _first_agent_fact_id(client, project_id)
    _verify(client, project_id, fact_id, result="fail")
    response = _verify(client, project_id, fact_id, result="pass")
    assert response.json()["fact"]["status"] == "verified"


def test_verification_rejects_invalid_method_or_result(client, project_id):
    response = _verify(
        client, project_id, "origin", method="vibes", result="pass"
    )
    assert response.status_code == 422
    response = _verify(client, project_id, "origin", result="maybe")
    assert response.status_code == 422


def test_verification_unknown_fact_or_run(client, project_id):
    assert _verify(client, project_id, "f999").status_code == 404
    assert _verify(client, project_id, "origin", run_id="run_999").status_code == 404


def test_verification_idempotent_replay(client, project_id, intent_id, run_id):
    _conclude(client, project_id, intent_id, run_id=run_id)
    fact_id = _first_agent_fact_id(client, project_id)
    first = _verify(client, project_id, fact_id, request_id="req-v1")
    retry = _verify(client, project_id, fact_id, request_id="req-v1")
    assert retry.status_code == 201
    assert retry.json() == first.json()
    detail = client.get(f"/projects/{project_id}/facts/{fact_id}").json()
    assert len(detail["verifications"]) == 1


def _setup_goal_evidence(client, project_id, with_evidence=True):
    """Create a verified fact carrying access evidence, ready to cite."""
    intent = client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["origin"], "description": "get admin", "creator": "alice"},
    ).json()
    run = client.post(
        f"/projects/{project_id}/runs",
        json={
            "task_type": "explore",
            "worker": "kali-01",
            "command": "type C:\\flag.txt",
            "exit_code": 0,
            "status": "success",
            "stdout": "flag{...}",
        },
    ).json()["id"]
    fact_id = _conclude(
        client,
        project_id,
        intent["id"],
        run_id=run,
        kind="access",
        description="Administrator shell on dc01",
    ).json()["fact"]["id"]
    if with_evidence:
        assert _verify(client, project_id, fact_id, result="pass").status_code == 201
    return fact_id


def _complete(client, project_id, **overrides):
    payload = {"from": ["origin"], "description": "done", "worker": "alice"}
    payload.update(overrides)
    return client.post(f"/projects/{project_id}/complete", json=payload)


def test_complete_with_valid_goal_evidence(client, project_id):
    fact_id = _setup_goal_evidence(client, project_id)
    response = _complete(client, project_id, goal_evidence=[fact_id])
    assert response.status_code == 200
    assert response.json()["to"] == "goal"


def test_complete_rejects_unverified_goal_evidence(client, project_id):
    fact_id = _setup_goal_evidence(client, project_id, with_evidence=False)
    response = _complete(client, project_id, goal_evidence=[fact_id])
    assert response.status_code == 409
    assert "not verified" in response.json()["detail"]


def test_complete_rejects_fact_without_access_evidence(client, project_id, intent_id):
    # A verified fact whose evidence is not kind=access/goal does not count.
    run = client.post(
        f"/projects/{project_id}/runs",
        json={"task_type": "explore", "worker": "w", "status": "success"},
    ).json()["id"]
    fact_id = _conclude(
        client, project_id, intent_id, run_id=run, kind="observation"
    ).json()["fact"]["id"]
    _verify(client, project_id, fact_id, result="pass")
    response = _complete(client, project_id, goal_evidence=[fact_id])
    assert response.status_code == 409
    assert "no access or goal evidence" in response.json()["detail"]


def test_complete_rejects_unknown_goal_evidence_fact(client, project_id):
    response = _complete(client, project_id, goal_evidence=["f999"])
    assert response.status_code == 404


def test_complete_without_goal_evidence_still_works_by_default(client, project_id):
    # require_evidence defaults to 0 in v0.3: backward compatible.
    response = _complete(client, project_id)
    assert response.status_code == 200


def test_complete_requires_goal_evidence_when_setting_enabled(client, project_id):
    client.put("/settings", json={"intent_timeout": 15, "reason_timeout": 15, "require_evidence": True, "verify_max_attempts": 3})
    response = _complete(client, project_id)
    assert response.status_code == 409
    assert "required" in response.json()["detail"]
    fact_id = _setup_goal_evidence(client, project_id)
    assert _complete(client, project_id, goal_evidence=[fact_id]).status_code == 200


def test_steering_hint_queue_consume_once(client, project_id):
    client.post(
        f"/projects/{project_id}/hint-queue",
        json={"content": "focus on 445", "creator": "operator"},
    )
    client.post(
        f"/projects/{project_id}/hint-queue",
        json={"content": "avoid destructive", "creator": "operator"},
    )

    first = client.get(f"/projects/{project_id}/hint-queue").json()["hints"]
    assert [h["content"] for h in first] == ["focus on 445", "avoid destructive"]
    # Consumed on read: a second poll gets nothing.
    assert client.get(f"/projects/{project_id}/hint-queue").json()["hints"] == []
    assert client.get("/projects/proj_999/hint-queue").status_code == 404
