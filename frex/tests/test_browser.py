"""Tests for browser_session (lifecycle, scope re-check, tiers)."""
from __future__ import annotations

import json

import pytest

from frex.dispatcher.runtime.browser import (
    BrowserSessionManager,
    FakeBackend,
    SessionNotFound,
)
from frex.dispatcher.runtime.permissions import ALLOW, ASK, PermissionGate
from frex.dispatcher.runtime.scope import Scope
from frex.dispatcher.runtime.tools import ToolExecutor


class ScriptedContainer:
    def build_exec_process(self, container_name, env, command, timeout_seconds=None):
        class P:
            def start(self):
                pass

            def communicate(self, timeout=None):
                class R:
                    returncode = 0
                    stdout = ""
                    stderr = ""

                return R()

        return P()


def _executor(manager, **kwargs):
    return ToolExecutor(
        ScriptedContainer(), "ctr", browser_manager=manager, **kwargs
    )


def _call(executor, **args):
    return executor.run("browser_session", json.dumps(args))


# ------------------------------------------------------------ lifecycle


def test_open_navigate_extract_close_round_trip():
    manager = BrowserSessionManager(backend=FakeBackend())
    executor = _executor(manager)
    opened = _call(executor, action="open_session", url="http://lab.test/")
    assert opened.ok is True
    state_id = opened.output["state_id"]
    assert state_id.startswith("bws_")

    nav = _call(executor, action="navigate", state_id=state_id, url="http://lab.test/admin")
    assert nav.ok is True and nav.output["url"] == "http://lab.test/admin"

    ext = _call(executor, action="extract", state_id=state_id, mode="text")
    assert ext.ok is True
    assert ext.output["text"] == "hello page text"

    links = _call(executor, action="extract", state_id=state_id, mode="links")
    assert links.output["count"] == 2

    shot = _call(executor, action="screenshot", state_id=state_id)
    assert shot.ok is True and shot.output["bytes"] == 1024

    closed = _call(executor, action="close_session", state_id=state_id)
    assert closed.ok is True
    # After close, the state_id is gone — clean error, not a crash.
    again = _call(executor, action="extract", state_id=state_id)
    assert again.output["error"] == "session_not_found"


def test_unknown_state_id_is_clean_error():
    executor = _executor(BrowserSessionManager(backend=FakeBackend()))
    result = _call(executor, action="navigate", state_id="bws_nope", url="http://lab.test/")
    assert result.output["error"] == "session_not_found"
    assert "open_session first" in result.output["message"]


def test_session_limit_enforced():
    manager = BrowserSessionManager(backend=FakeBackend(), max_sessions=1)
    executor = _executor(manager)
    first = _call(executor, action="open_session", url="http://lab.test/")
    assert first.ok is True
    second = _call(executor, action="open_session", url="http://lab.test/")
    assert second.output["error"] == "browser_error"
    assert "close one first" in second.output["message"]


def test_cookies_persist_across_actions_in_backend():
    backend = FakeBackend()
    manager = BrowserSessionManager(backend=backend)
    session = manager.open("http://lab.test/login")
    manager.navigate(session.state_id, "http://lab.test/dashboard")
    assert backend.navigated == ["http://lab.test/dashboard"]
    # Same page object across calls (identity preserved).
    assert manager.get(session.state_id).page is session.page


# ------------------------------------------------------------ scope re-check


def test_navigate_out_of_scope_mid_session_is_rejected():
    manager = BrowserSessionManager(backend=FakeBackend())
    executor = _executor(manager, scope_checker=Scope(domains=["lab.test"]).checker())

    opened = _call(executor, action="open_session", url="http://lab.test/")
    assert opened.ok is True
    state_id = opened.output["state_id"]

    # Mid-session navigation to an out-of-scope host must be rejected —
    # NOT only at open_session.
    result = _call(executor, action="navigate", state_id=state_id, url="http://evil.test/")
    assert result.ok is False
    assert result.output["error"] == "scope"
    # The backend never navigated there.
    assert manager._backend.navigated == []


def test_open_session_out_of_scope_rejected():
    executor = _executor(
        BrowserSessionManager(backend=FakeBackend()),
        scope_checker=Scope(domains=["lab.test"]).checker(),
    )
    result = _call(executor, action="open_session", url="http://evil.test/")
    assert result.output["error"] == "scope"


def test_executor_rejects_browser_session_url_at_entry():
    # The run()-level scope check covers the url argument too (defence
    # in depth before the handler's own re-check).
    executor = _executor(
        BrowserSessionManager(backend=FakeBackend()),
        scope_checker=Scope(domains=["lab.test"]).checker(),
    )
    result = _call(executor, action="open_session", url="http://evil.test/")
    assert result.output["error"] == "scope"


# ------------------------------------------------------------ tiers


def test_click_and_fill_are_tier2():
    closed_gate = PermissionGate(approval_handler=None)
    # Fail-closed: tier 2 without a channel is a DENY, never auto-run.
    assert closed_gate.check("browser_session", "http://lab.test/", action="click").decision == "deny"
    assert closed_gate.check("browser_session", "http://lab.test/", action="fill").decision == "deny"

    gate = PermissionGate(approval_handler=lambda r: None)
    assert gate.check("browser_session", "http://lab.test/", action="click").decision == ASK
    assert gate.check("browser_session", "http://lab.test/", action="fill").decision == ASK
    # Read-only actions stay automatic.
    assert gate.check("browser_session", "http://lab.test/", action="navigate").decision == ALLOW
    assert gate.check("browser_session", "http://lab.test/", action="extract").decision == ALLOW


def test_click_and_fill_actually_work_with_approval():
    manager = BrowserSessionManager(backend=FakeBackend())
    executor = _executor(manager)
    opened = _call(executor, action="open_session", url="http://lab.test/")
    state_id = opened.output["state_id"]
    fill = _call(executor, action="fill", state_id=state_id, selector="#user", value="admin")
    assert fill.ok is True
    click = _call(executor, action="click", state_id=state_id, selector="#submit")
    assert click.ok is True
    assert manager._backend.fills == [("#user", "admin")]
    assert manager._backend.clicks == ["#submit"]


def test_extract_truncates_to_budget():
    backend = FakeBackend()
    backend.text = "A" * 10_000
    executor = _executor(BrowserSessionManager(backend=backend))
    opened = _call(executor, action="open_session", url="http://lab.test/")
    ext = _call(executor, action="extract", state_id=opened.output["state_id"], mode="text")
    assert ext.output["truncated"] is True
    assert len(ext.output["text"]) == 4_000


def test_browser_unavailable_without_manager():
    result = ToolExecutor(ScriptedContainer(), "ctr").run(
        "browser_session", json.dumps({"action": "open_session", "url": "http://x"})
    )
    assert result.output["error"] == "unavailable"
