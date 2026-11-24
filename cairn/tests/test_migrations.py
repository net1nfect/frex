from __future__ import annotations

import sqlite3

import pytest

from cairn.server import db


def _version(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT version FROM schema_version WHERE id = 1").fetchone()["version"]


def test_fresh_database_is_at_latest_version(tmp_path):
    db.reset_for_tests()
    db.configure(tmp_path / "cairn.db")
    try:
        with db.get_conn() as conn:
            assert _version(conn) == db.SCHEMA_VERSION
    finally:
        db.reset_for_tests()


def test_configure_is_idempotent_after_reset(tmp_path):
    path = tmp_path / "cairn.db"
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

    def migrate_to_three(conn):
        calls.append(3)
        conn.execute("CREATE TABLE IF NOT EXISTS migration_probe (id INTEGER)")

    db.reset_for_tests()
    db.configure(tmp_path / "cairn.db")
    try:
        monkeypatch.setattr(db, "SCHEMA_VERSION", 3)
        monkeypatch.setitem(db.MIGRATIONS, 3, migrate_to_three)
        with db.get_conn() as conn:
            db._migrate(conn)
            assert _version(conn) == 3
            assert conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'migration_probe'"
            ).fetchone() is not None
        with db.get_conn() as conn:
            db._migrate(conn)
            assert _version(conn) == 3
        assert calls == [3]
    finally:
        db.reset_for_tests()


def test_add_column_is_idempotent(tmp_path):
    db.reset_for_tests()
    db.configure(tmp_path / "cairn.db")
    try:
        with db.get_conn() as conn:
            db._add_column(conn, "facts", "kind", "TEXT NOT NULL DEFAULT 'observation'")
            db._add_column(conn, "facts", "kind", "TEXT NOT NULL DEFAULT 'observation'")
            assert db._column_exists(conn, "facts", "kind") is True
            assert db._column_exists(conn, "facts", "missing") is False
    finally:
        db.reset_for_tests()


def test_migration_rolls_back_with_the_transaction(tmp_path, monkeypatch):
    def broken_migration(conn):
        conn.execute("CREATE TABLE migration_broken (id INTEGER)")
        raise RuntimeError("boom")

    db.reset_for_tests()
    db.configure(tmp_path / "cairn.db")
    try:
        monkeypatch.setattr(db, "SCHEMA_VERSION", 3)
        monkeypatch.setitem(db.MIGRATIONS, 3, broken_migration)
        with pytest.raises(RuntimeError):
            with db.get_conn() as conn:
                db._migrate(conn)
        with db.get_conn() as conn:
            assert _version(conn) == 2
            assert conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'migration_broken'"
            ).fetchone() is None
    finally:
        db.reset_for_tests()
