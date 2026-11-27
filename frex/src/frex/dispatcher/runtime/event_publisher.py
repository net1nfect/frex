"""Batches agent-loop events to the server without blocking the loop.

The agent loop emits synchronously; network calls in that path would
stall every step. Events buffer in memory and a background thread
flushes them in batches — losing a buffer on hard crash is acceptable,
losing the task is not.
"""
from __future__ import annotations

import logging
import queue
import threading
from typing import Any

from frex.dispatcher.protocol.client import FrexClient

LOG = logging.getLogger(__name__)

FLUSH_INTERVAL_SECONDS = 1.0
MAX_BATCH = 200


class EventPublisher:
    def __init__(self, client: FrexClient, project_id: str, task_type: str):
        self._client = client
        self._project_id = project_id
        self._task_type = task_type
        self._queue: queue.Queue[dict[str, Any]] = queue.Queue()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def emit(self, kind: str, data: dict[str, Any]) -> None:
        self._queue.put({"kind": kind, "data": data})

    def _flush(self) -> None:
        batch: list[dict[str, Any]] = []
        while True:
            try:
                batch.append(self._queue.get_nowait())
            except queue.Empty:
                break
            if len(batch) >= MAX_BATCH:
                self._send(batch)
                batch = []
        if batch:
            self._send(batch)

    def _send(self, batch: list[dict[str, Any]]) -> None:
        try:
            response = self._client.post_events(self._project_id, self._task_type, batch)
            if not response.ok:
                LOG.warning(
                    "event flush failed project=%s status=%s dropped=%s",
                    self._project_id,
                    response.status_code,
                    len(batch),
                )
        except Exception as exc:
            LOG.warning("event flush error project=%s error=%s dropped=%s", self._project_id, exc, len(batch))

    def _run(self) -> None:
        while not self._stop.is_set():
            self._stop.wait(FLUSH_INTERVAL_SECONDS)
            self._flush()

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)
        self._flush()
