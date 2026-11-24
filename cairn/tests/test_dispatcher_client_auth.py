from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from cairn.dispatcher.protocol.client import TOKEN_ENV, CairnClient


class RecordingHandler(BaseHTTPRequestHandler):
    seen_authorization: list[str | None] = []

    def do_GET(self):  # noqa: N802 - http.server API
        type(self).seen_authorization.append(self.headers.get("Authorization"))
        body = json.dumps([]).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):  # noqa: A002 - http.server API
        pass


@pytest.fixture
def http_server():
    RecordingHandler.seen_authorization = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), RecordingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_client_sends_token_from_env(http_server, monkeypatch):
    monkeypatch.setenv(TOKEN_ENV, "s3cret")
    client = CairnClient(http_server)
    try:
        assert client.list_projects() == []
    finally:
        client.close()
    assert RecordingHandler.seen_authorization == ["Bearer s3cret"]


def test_client_sends_explicit_token(http_server, monkeypatch):
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    client = CairnClient(http_server, api_token="explicit")
    try:
        client.list_projects()
    finally:
        client.close()
    assert RecordingHandler.seen_authorization == ["Bearer explicit"]


def test_client_sends_no_header_without_token(http_server, monkeypatch):
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    client = CairnClient(http_server)
    try:
        client.list_projects()
    finally:
        client.close()
    assert RecordingHandler.seen_authorization == [None]


def test_blank_env_token_is_ignored(http_server, monkeypatch):
    monkeypatch.setenv(TOKEN_ENV, "   ")
    client = CairnClient(http_server)
    try:
        client.list_projects()
    finally:
        client.close()
    assert RecordingHandler.seen_authorization == [None]
