"""Stateful browser sessions for the browser_session tool.

Unlike the stateless tools (each call is one request), a browser session
keeps cookies and page state across calls: open_session returns a
state_id, every later action takes it. Expired or unknown ids are a
clean error, never a crash.

Backend: Playwright (optional extra: ``uv sync --extra browser``). The
Chromium binary is reused from the existing cache/image — no second
browser install. Tests inject a fake backend.

Scope enforcement is NOT here: the executor re-checks every navigate()
URL against the scope guard, so a session cannot drift out of scope
mid-flow.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any


class SessionNotFound(Exception):
    """Raised for unknown/expired state_id — the caller maps it to a
    clean session_not_found tool error."""


@dataclass(slots=True)
class BrowserSession:
    state_id: str
    url: str
    created_at: float = field(default_factory=time.time)
    last_used_at: float = field(default_factory=time.time)
    # Backend handle (Playwright page/context, or a fake in tests).
    page: Any = None
    context: Any = None


class FakeBackend:
    """Test backend: no real browser, deterministic page model."""

    def __init__(self):
        self.opened: list[str] = []
        self.navigated: list[str] = []
        self.clicks: list[str] = []
        self.fills: list[tuple[str, str]] = []
        self.closed: list[str] = []
        self.text = "hello page text"

    def open(self, url: str):
        self.opened.append(url)
        return {"url": url}

    def navigate(self, page, url: str):
        self.navigated.append(url)
        page["url"] = url
        return {"url": url}

    def extract(self, page, mode: str):
        if mode == "links":
            return [f"{page['url']}/a", f"{page['url']}/b"]
        if mode == "dom":
            return "<html><body>dom</body></html>"
        return self.text

    def screenshot(self, page, path: str):
        return {"path": path, "bytes": 1024}

    def click(self, page, selector: str):
        self.clicks.append(selector)
        return {"clicked": selector}

    def fill(self, page, selector: str, value: str):
        self.fills.append((selector, value))
        return {"filled": selector}

    def close(self, page):
        self.closed.append(page.get("url", ""))
        return None


class PlaywrightBackend:
    """Real browser backend via Playwright (chromium, headless)."""

    def __init__(self):
        self._playwright = None
        self._browser = None

    def _ensure(self):
        if self._browser is None:
            from playwright.sync_api import sync_playwright  # lazy import

            self._playwright = sync_playwright().start()
            # Reuse an existing Chromium build instead of downloading a
            # second one: env override, then common system paths, then
            # Playwright's own bundled browser.
            import os

            executable = os.environ.get("FREX_CHROMIUM_EXECUTABLE")
            if not executable:
                for candidate in ("/usr/bin/chromium", "/usr/bin/google-chrome"):
                    if os.path.exists(candidate):
                        executable = candidate
                        break
            launch_kwargs: dict[str, Any] = {"headless": True}
            if executable:
                launch_kwargs["executable_path"] = executable
            self._browser = self._playwright.chromium.launch(**launch_kwargs)
        return self._browser

    def open(self, url: str):
        browser = self._ensure()
        context = browser.new_context()
        page = context.new_page()
        page.goto(url, timeout=30_000, wait_until="domcontentloaded")
        return page

    def navigate(self, page, url: str):
        page.goto(url, timeout=30_000, wait_until="domcontentloaded")
        return {"url": page.url}

    def extract(self, page, mode: str):
        if mode == "links":
            return page.eval_on_selector_all(
                "a[href]", "els => els.map(e => e.href)"
            )
        if mode == "dom":
            return page.content()
        return page.inner_text("body")

    def screenshot(self, page, path: str):
        page.screenshot(path=path, full_page=False)
        import os

        return {"path": path, "bytes": os.path.getsize(path)}

    def click(self, page, selector: str):
        page.click(selector, timeout=10_000)
        return {"clicked": selector}

    def fill(self, page, selector: str, value: str):
        page.fill(selector, value, timeout=10_000)
        return {"filled": selector}

    def close(self, page):
        try:
            page.context.close()
        except Exception:
            pass


class BrowserSessionManager:
    def __init__(self, backend: Any | None = None, *, max_sessions: int = 5):
        self._backend = backend if backend is not None else PlaywrightBackend()
        self._sessions: dict[str, BrowserSession] = {}
        self._max = max_sessions

    def open(self, url: str) -> BrowserSession:
        if len(self._sessions) >= self._max:
            raise RuntimeError(
                f"too many open browser sessions ({len(self._sessions)}/{self._max}); close one first"
            )
        page = self._backend.open(url)
        session = BrowserSession(state_id="bws_" + uuid.uuid4().hex[:10], url=url, page=page)
        self._sessions[session.state_id] = session
        return session

    def get(self, state_id: str | None) -> BrowserSession:
        if not state_id or state_id not in self._sessions:
            raise SessionNotFound(
                f"unknown or expired state_id {state_id!r} — open_session first"
            )
        session = self._sessions[state_id]
        session.last_used_at = time.time()
        return session

    def navigate(self, state_id: str, url: str) -> dict[str, Any]:
        session = self.get(state_id)
        result = self._backend.navigate(session.page, url)
        session.url = url
        return result

    def extract(self, state_id: str, mode: str) -> Any:
        return self._backend.extract(self.get(state_id).page, mode)

    def screenshot(self, state_id: str, path: str) -> dict[str, Any]:
        return self._backend.screenshot(self.get(state_id).page, path)

    def click(self, state_id: str, selector: str) -> dict[str, Any]:
        return self._backend.click(self.get(state_id).page, selector)

    def fill(self, state_id: str, selector: str, value: str) -> dict[str, Any]:
        return self._backend.fill(self.get(state_id).page, selector, value)

    def close(self, state_id: str) -> bool:
        session = self._sessions.pop(state_id, None)
        if session is None:
            raise SessionNotFound(f"unknown or expired state_id {state_id!r}")
        self._backend.close(session.page)
        return True
