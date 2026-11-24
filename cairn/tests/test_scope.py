from __future__ import annotations

import pytest

from cairn.dispatcher.runtime.scope import Scope
from cairn.server.models import ScopeConfig


def test_empty_scope_is_unrestricted():
    scope = Scope()
    assert scope.check_host("10.0.0.1") is None
    assert scope.check_host("anything.example.com") is None
    assert scope.check_command("nmap -sV 10.0.0.1") is None


def test_cidr_scope_allows_inside_and_rejects_outside():
    scope = Scope(cidrs=["10.0.0.0/24"])
    assert scope.check_host("10.0.0.5") is None
    assert scope.check_host("10.0.1.5") is not None
    assert scope.check_host("192.168.1.1") is not None
    # CIDR notation inside a command is checked too.
    assert scope.check_command("nmap -sV 10.0.1.0/24") is not None
    assert scope.check_command("nmap -sV 10.0.0.0/24") is None


def test_domain_scope_covers_subdomains():
    scope = Scope(domains=["example.com"])
    assert scope.check_host("example.com") is None
    assert scope.check_host("sub.example.com") is None
    assert scope.check_host("notexample.com") is not None
    assert scope.check_host("evil-example.com") is not None


def test_exclusions_win_over_allowlist():
    scope = Scope(
        domains=["example.com"],
        exclude_domains=["secret.example.com"],
        exclude_cidrs=["10.0.0.1/32"],
    )
    assert scope.check_host("secret.example.com") is not None
    assert scope.check_host("10.0.0.1") is not None
    assert scope.check_host("other.example.com") is None


def test_exclusion_only_scope_allows_the_rest():
    scope = Scope(exclude_domains=["prod.example.com"])
    assert scope.check_host("prod.example.com") is not None
    assert scope.check_host("dev.example.com") is None


def test_check_url_extracts_host():
    scope = Scope(cidrs=["10.0.0.0/8"])
    assert scope.check_url("http://10.1.2.3/admin") is None
    assert scope.check_url("http://example.com/x") is not None
    assert scope.check_url("https://sub.example.com:8443/api") is not None


def test_checker_targets_tools():
    scope = Scope(cidrs=["10.0.0.0/8"])
    checker = scope.checker()
    assert checker("http_request", {"url": "http://10.1.2.3/"}) is None
    assert checker("http_request", {"url": "http://example.com/"}) is not None
    assert checker("run_command", {"command": "nmap 10.0.0.5"}) is None
    assert checker("run_command", {"command": "nmap example.com"}) is not None
    # Non-network tools are not host targets.
    assert checker("read_file", {"path": "/etc/passwd"}) is None
    assert checker("submit_result", {"result": {}}) is None


def test_from_json_roundtrip_and_garbage():
    assert Scope.from_json(None) is None
    assert Scope.from_json("") is None
    assert Scope.from_json("not json") is None
    scope = Scope.from_json('{"domains": ["Example.COM"], "cidrs": ["10.0.0.0/24"]}')
    assert scope is not None
    assert scope.domains == ["example.com"]
    assert scope.check_host("10.0.0.9") is None


def test_scope_config_roundtrip_via_model():
    config = ScopeConfig(domains=["example.com"], exclude_cidrs=["10.9.9.9/32"])
    scope = Scope.from_config(config)
    assert scope.check_host("example.com") is None
    assert scope.check_host("10.9.9.9") is not None
