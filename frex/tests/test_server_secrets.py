from __future__ import annotations

import pytest


@pytest.fixture
def secret_project(client, monkeypatch):
    monkeypatch.setenv("FREX_SECRET_KEY", "test-key-material")
    return client.post(
        "/projects", json={"title": "lab", "origin": "s", "goal": "g"}
    ).json()["project"]["id"]


def test_secret_create_list_never_returns_value(client, secret_project):
    response = client.post(
        f"/projects/{secret_project}/secrets",
        json={"name": "db-admin", "value": "hunter2"},
    )
    assert response.status_code == 201
    assert response.json()["ref"] == "secret://db-admin"
    assert "hunter2" not in response.text

    listing = client.get(f"/projects/{secret_project}/secrets").json()
    assert listing["secrets"][0]["name"] == "db-admin"
    assert "hunter2" not in str(listing)


def test_secret_requires_reveal_and_auth(client, secret_project, monkeypatch):
    client.post(
        f"/projects/{secret_project}/secrets",
        json={"name": "db-admin", "value": "hunter2"},
    )
    # Without reveal param: refused even with auth on.
    monkeypatch.setenv("FREX_API_TOKEN", "op-token")
    headers = {"Authorization": "Bearer op-token"}
    assert (
        client.get(
            f"/projects/{secret_project}/secrets/db-admin", headers=headers
        ).status_code
        == 403
    )
    # With reveal but no auth middleware enforcement (auth enabled here):
    revealed = client.get(
        f"/projects/{secret_project}/secrets/db-admin",
        params={"reveal": 1},
        headers=headers,
    )
    assert revealed.status_code == 200
    assert revealed.json()["value"] == "hunter2"


def test_secret_reveal_refused_when_auth_disabled(client, secret_project, monkeypatch):
    monkeypatch.delenv("FREX_API_TOKEN", raising=False)
    client.post(
        f"/projects/{secret_project}/secrets",
        json={"name": "k", "value": "v"},
    )
    response = client.get(
        f"/projects/{secret_project}/secrets/k", params={"reveal": 1}
    )
    assert response.status_code == 403


def test_secret_refused_without_key(client, secret_project, monkeypatch):
    monkeypatch.delenv("FREX_SECRET_KEY", raising=False)
    response = client.post(
        f"/projects/{secret_project}/secrets",
        json={"name": "k", "value": "v"},
    )
    assert response.status_code == 503  # refuses rather than storing plaintext
    # And nothing was persisted in plaintext.
    import sqlite3

    assert client.get(f"/projects/{secret_project}/secrets").json()["secrets"] == []


def test_secret_key_mismatch_fails_closed(client, secret_project, monkeypatch):
    client.post(
        f"/projects/{secret_project}/secrets",
        json={"name": "k", "value": "v"},
    )
    monkeypatch.setenv("FREX_SECRET_KEY", "different-key")
    monkeypatch.setenv("FREX_API_TOKEN", "t")
    response = client.get(
        f"/projects/{secret_project}/secrets/k",
        params={"reveal": 1},
        headers={"Authorization": "Bearer t"},
    )
    assert response.status_code == 500
