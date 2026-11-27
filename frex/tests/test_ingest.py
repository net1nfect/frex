from __future__ import annotations

import json

import pytest

from frex.dispatcher.ingest import INGESTORS, ingest_nmap, ingest_nuclei
from frex.dispatcher.ingest.base import IngestedFact, post_facts

NMAP_XML = """<?xml version="1.0"?>
<nmaprun>
  <host>
    <address addr="10.0.0.5" addrtype="ipv4"/>
    <hostnames><hostname name="web.lab"/></hostnames>
    <status state="up"/>
    <ports>
      <port protocol="tcp" portid="22">
        <state state="open"/>
        <service name="ssh" product="OpenSSH" version="9.2"/>
      </port>
      <port protocol="tcp" portid="80">
        <state state="closed"/>
        <service name="http"/>
      </port>
      <port protocol="tcp" portid="443">
        <state state="open"/>
        <service name="https"/>
      </port>
    </ports>
  </host>
  <host>
    <address addr="10.0.0.9" addrtype="ipv4"/>
    <status state="down"/>
  </host>
</nmaprun>
"""

NUCLEI_JSONL = "\n".join(
    [
        json.dumps(
            {
                "template-id": "cve-2024-1234",
                "info": {"name": "Auth Bypass", "severity": "critical"},
                "host": "http://10.0.0.5:8080",
                "type": "http",
            }
        ),
        json.dumps(
            {
                "data": {
                    "template-id": "tech-detect",
                    "info": {"name": "nginx detected", "severity": "info"},
                    "host": "http://10.0.0.5",
                }
            }
        ),
        "not json at all",
        "",
    ]
)


def test_nmap_ingests_hosts_and_open_ports_only():
    facts = ingest_nmap(NMAP_XML)
    descriptions = [f.description for f in facts]
    # Host up fact + 2 open ports; closed port and down host excluded.
    assert "host 10.0.0.5 is up (web.lab)" in descriptions
    assert any("tcp/22 open on 10.0.0.5 running ssh (OpenSSH 9.2)" == d for d in descriptions)
    assert any("tcp/443 open on 10.0.0.5 running https" == d for d in descriptions)
    assert not any("80" in d and "closed" not in d for d in descriptions if "tcp/80" in d)
    assert not any("10.0.0.9" in d for d in descriptions)

    port_fact = next(f for f in facts if f.subject.get("port") == 22)
    assert port_fact.kind == "access"
    assert port_fact.subject["hostname"] == "web.lab"
    assert port_fact.match_rule == "regex"


def test_nuclei_ingests_findings_with_severity():
    facts = ingest_nuclei(NUCLEI_JSONL)
    assert len(facts) == 2  # malformed + blank lines skipped
    crit = next(f for f in facts if f.subject["severity"] == "critical")
    assert crit.kind == "vulnerability"
    assert "cve-2024-1234" in crit.description
    assert "Auth Bypass" in crit.description
    info = next(f for f in facts if f.subject["severity"] == "info")
    assert info.subject["template_id"] == "tech-detect"


def test_ingestors_registry():
    assert set(INGESTORS) == {"nmap", "nuclei"}


def test_post_facts_concludes_with_verified_confidence():
    class FakeApiResult:
        def __init__(self, status_code, data=None):
            self.status_code = status_code
            self.data = data or {}

        @property
        def ok(self):
            return 200 <= self.status_code < 300

    class FakeClient:
        def __init__(self):
            self.intents = []
            self.concludes = []

        def create_intent(self, project_id, from_ids, description, creator):
            self.intents.append((project_id, from_ids, description, creator))
            return FakeApiResult(201, {"id": "i001"})

        def conclude(self, project_id, intent_id, worker, description, **kwargs):
            self.concludes.append(
                {"worker": worker, "description": description, **kwargs}
            )
            return FakeApiResult(200, {"fact": {"id": "f001"}})

    client = FakeClient()
    posted, failed = post_facts(
        client,
        "proj_001",
        [ingest_nmap(NMAP_XML)[0], ingest_nmap(NMAP_XML)[1]],
    )
    assert (posted, failed) == (2, 0)
    assert client.intents[0][1] == ["origin"]  # anchored on origin
    assert client.intents[0][2] == "ingest: tool output import"
    assert client.intents[0][3] == "ingest"
    assert client.concludes[0]["worker"] == "ingest"
    assert client.concludes[0]["confidence"] == 1.0
    assert client.concludes[0]["kind"] == "observation"


def test_post_facts_counts_failures():
    class FailResult:
        status_code = 500
        ok = False
        data = {}

    class FlakyClient:
        def __init__(self):
            self.n = 0

        def create_intent(self, *a, **k):
            class R:
                ok = True
                status_code = 201
                data = {"id": "i001"}

            return R()

        def conclude(self, *a, **k):
            self.n += 1
            return FailResult() if self.n == 1 else type("R", (), {"ok": True, "status_code": 200, "data": {}})()

    client = FlakyClient()
    posted, failed = post_facts(
        client, "proj_001", [IngestedFact("a"), IngestedFact("b")]
    )
    assert (posted, failed) == (1, 1)
