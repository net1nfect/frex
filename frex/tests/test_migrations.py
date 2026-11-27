from __future__ import annotations

import sqlite3

import pytest

from frex.server import db


def _version(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT version FROM schema_version WHERE id = 1").fetchone()["version"]


def test_fresh_database_is_at_latest_version(tmp_path):
    db.reset_for_tests()
    db.configure(tmp_path / "frex.db")
    try:
        with db.get_conn() as conn:
            assert _version(conn) == db.SCHEMA_VERSION
    finally:
        db.reset_for_tests()


def test_configure_is_idempotent_after_reset(tmp_path):
    path = tmp_path / "frex.db"
    db.reset_for_tests()
    db.configure(path)
    with db.get_conn() as conn:
        conn.execute("INSERT INTO counters (name, value) VALUES ('extra', 7)")
    db.reset_for_tests()
    db.configure(path)
    try:
        with db.get_conn() as conn:
            assert _version(conn) == db.SCHEMA_VERSION
            assert conn.execute(
                "SELECT value FROM counters WHERE name = 'extra'"
            ).fetchone()["value"] == 7
    finally:
        db.reset_for_tests()


def test_migration_runner_applies_once(tmp_path, monkeypatch):
    calls: list[int] = []
    next_version = db.SCHEMA_VERSION + 1

    def migrate_probe(conn):
        calls.append(next_version)
        conn.execute("CREATE TABLE IF NOT EXISTS migration_probe (id INTEGER)")

    db.reset_for_tests()
    db.configure(tmp_path / "frex.db")
    try:
        monkeypatch.setattr(db, "SCHEMA_VERSION", next_version)
        monkeypatch.setitem(db.MIGRATIONS, next_version, migrate_probe)
        with db.get_conn() as conn:
            db._migrate(conn)
            assert _version(conn) == next_version
            assert conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'migration_probe'"
            ).fetchone() is not None
        with db.get_conn() as conn:
            db._migrate(conn)
            assert _version(conn) == next_version
        assert calls == [next_version]
    finally:
        db.reset_for_tests()

def test_add_column_is_idempotent(tmp_path):
    db.reset_for_tests()
    db.configure(tmp_path / "frex.db")
    try:
        with db.get_conn() as conn:
            db._add_column(conn, "facts", "kind", "TEXT NOT NULL DEFAULT 'observation'")
            db._add_column(conn, "facts", "kind", "TEXT NOT NULL DEFAULT 'observation'")
            assert db._column_exists(conn, "facts", "kind") is True
            assert db._column_exists(conn, "facts", "missing") is False
    finally:
        db.reset_for_tests()


def test_migration_rolls_back_with_the_transaction(tmp_path, monkeypatch):
    current_version = db.SCHEMA_VERSION
    next_version = current_version + 1

    def broken_migration(conn):
        conn.execute("CREATE TABLE migration_broken (id INTEGER)")
        raise RuntimeError("boom")

    db.reset_for_tests()
    db.configure(tmp_path / "frex.db")
    try:
        monkeypatch.setattr(db, "SCHEMA_VERSION", next_version)
        monkeypatch.setitem(db.MIGRATIONS, next_version, broken_migration)
        with pytest.raises(RuntimeError):
            with db.get_conn() as conn:
                db._migrate(conn)
        with db.get_conn() as conn:
            assert _version(conn) == current_version
            assert conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'migration_broken'"
            ).fetchone() is None
    finally:
        db.reset_for_tests()
