from __future__ import annotations

import pytest

from cairn.server import auth


@pytest.fixture(autouse=True)
def clear_token(monkeypatch):
    monkeypatch.delenv(auth.TOKEN_ENV, raising=False)


def test_api_is_open_without_token(client):
    assert client.get("/projects").status_code == 200


def test_api_requires_token_when_configured(client, monkeypatch):
    monkeypatch.setenv(auth.TOKEN_ENV, "s3cret")
    response = client.get("/projects")
    assert response.status_code == 401
    assert response.json() == {"detail": "Unauthorized"}


def test_api_accepts_correct_bearer_token(client, monkeypatch):
    monkeypatch.setenv(auth.TOKEN_ENV, "s3cret")
    response = client.get(
        "/projects", headers={"Authorization": "Bearer s3cret"}
    )
    assert response.status_code == 200


def test_bearer_scheme_is_case_insensitive(client, monkeypatch):
    monkeypatch.setenv(auth.TOKEN_ENV, "s3cret")
    response = client.get(
        "/projects", headers={"Authorization": "bearer s3cret"}
    )
    assert response.status_code == 200


def test_wrong_token_is_rejected(client, monkeypatch):
    monkeypatch.setenv(auth.TOKEN_ENV, "s3cret")
    response = client.get(
        "/projects", headers={"Authorization": "Bearer wrong"}
    )
    assert response.status_code == 401


@pytest.mark.parametrize(
    "header",
    ["s3cret", "Basic s3cret", "Bearer", "Bearer "],
)
def test_malformed_authorization_header_is_rejected(client, monkeypatch, header):
    monkeypatch.setenv(auth.TOKEN_ENV, "s3cret")
    response = client.get("/projects", headers={"Authorization": header})
    assert response.status_code == 401


def test_token_is_stripped_from_env(client, monkeypatch):
    monkeypatch.setenv(auth.TOKEN_ENV, "  s3cret  ")
    response = client.get(
        "/projects", headers={"Authorization": "Bearer s3cret"}
    )
    assert response.status_code == 200


def test_empty_token_env_disables_auth(client, monkeypatch):
    monkeypatch.setenv(auth.TOKEN_ENV, "   ")
    assert auth.token_from_env() is None
    assert client.get("/projects").status_code == 200


@pytest.mark.parametrize("path", ["/", "/openapi.json", "/docs", "/static/favicon.svg"])
def test_exempt_paths_do_not_require_token(client, monkeypatch, path):
    monkeypatch.setenv(auth.TOKEN_ENV, "s3cret")
    assert client.get(path).status_code == 200
