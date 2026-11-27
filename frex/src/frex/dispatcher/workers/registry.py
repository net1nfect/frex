from __future__ import annotations

from frex.dispatcher.workers.adapters import ClaudeCodeDriver, CodexDriver, MockDriver, PiDriver
from frex.dispatcher.workers.base import WorkerDriver


DRIVERS: dict[str, WorkerDriver] = {
    "claudecode": ClaudeCodeDriver(),
    "codex": CodexDriver(),
    "pi": PiDriver(),
    "mock": MockDriver(),
}


def get_driver(name: str) -> WorkerDriver:
    return DRIVERS[name]
