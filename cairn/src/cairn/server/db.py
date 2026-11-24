from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Generator

DEFAULT_DB = Path.home() / ".local" / "share" / "cairn" / "cairn.db"

SCHEMA_VERSION = 10

_db_path: Path | None = None

SCHEMA = """\
CREATE TABLE IF NOT EXISTS schema_version (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    version INTEGER NOT NULL
);

INSERT OR IGNORE INTO schema_version (id, version) VALUES (1, 1);

CREATE TABLE IF NOT EXISTS settings (
    intent_timeout INTEGER NOT NULL DEFAULT 15,
    reason_timeout INTEGER NOT NULL DEFAULT 15
);

INSERT OR IGNORE INTO settings (rowid, intent_timeout, reason_timeout) VALUES (1, 15, 15);

CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    reason_worker TEXT,
    reason_trigger TEXT,
    reason_started_at TEXT,
    reason_last_heartbeat_at TEXT
);

CREATE TABLE IF NOT EXISTS facts (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    description TEXT NOT NULL,
    PRIMARY KEY (id, project_id)
);

CREATE TABLE IF NOT EXISTS intents (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    to_fact_id TEXT,
    description TEXT NOT NULL,
    creator TEXT NOT NULL,
    worker TEXT,
    last_heartbeat_at TEXT,
    created_at TEXT NOT NULL,
    concluded_at TEXT,
    PRIMARY KEY (id, project_id)
);

CREATE TABLE IF NOT EXISTS intent_sources (
    intent_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    fact_id TEXT NOT NULL,
    PRIMARY KEY (intent_id, project_id, fact_id),
    FOREIGN KEY (intent_id, project_id) REFERENCES intents(id, project_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS hints (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    content TEXT NOT NULL,
    creator TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (id, project_id)
);

CREATE TABLE IF NOT EXISTS counters (
    name TEXT PRIMARY KEY,
    value INTEGER NOT NULL DEFAULT 0
);

INSERT OR IGNORE INTO counters (name, value) VALUES ('project', 0);

CREATE TABLE IF NOT EXISTS scoped_counters (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    value INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (project_id, kind)
);

CREATE TABLE IF NOT EXISTS idempotency_keys (
    key TEXT NOT NULL,
    scope TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    response TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (key, scope)
);

CREATE TABLE IF NOT EXISTS task_runs (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    task_type TEXT NOT NULL,
    worker TEXT NOT NULL,
    model TEXT,
    phase TEXT NOT NULL DEFAULT 'execute',
    intent_id TEXT,
    command TEXT,
    exit_code INTEGER,
    timed_out INTEGER NOT NULL DEFAULT 0,
    cancelled INTEGER NOT NULL DEFAULT 0,
    stdout TEXT,
    stderr TEXT,
    stdout_sha256 TEXT,
    stdout_path TEXT,
    stderr_path TEXT,
    stdout_truncated INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER,
    tokens_in INTEGER,
    tokens_out INTEGER,
    cost_usd REAL,
    status TEXT NOT NULL,
    client_run_id TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    finished_at TEXT,
    PRIMARY KEY (id, project_id)
);

CREATE INDEX IF NOT EXISTS idx_task_runs_project
    ON task_runs(project_id, created_at);
CREATE INDEX IF NOT EXISTS idx_task_runs_intent
    ON task_runs(project_id, intent_id);

CREATE TABLE IF NOT EXISTS evidence (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    fact_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    command TEXT,
    exit_code INTEGER,
    match_rule TEXT NOT NULL DEFAULT 'contains',
    match_value TEXT,
    stdout_sha256 TEXT,
    artifact_path TEXT,
    excerpt TEXT,
    captured_at TEXT NOT NULL,
    PRIMARY KEY (id, project_id),
    FOREIGN KEY (fact_id, project_id)
        REFERENCES facts(id, project_id) ON DELETE CASCADE,
    FOREIGN KEY (run_id, project_id)
        REFERENCES task_runs(id, project_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_evidence_fact
    ON evidence(project_id, fact_id);
CREATE INDEX IF NOT EXISTS idx_evidence_run
    ON evidence(project_id, run_id);

CREATE TABLE IF NOT EXISTS fact_verifications (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    fact_id TEXT NOT NULL,
    run_id TEXT,
    method TEXT NOT NULL,
    result TEXT NOT NULL,
    verifier_model TEXT,
    expected TEXT,
    observed TEXT,
    details TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (id, project_id),
    FOREIGN KEY (fact_id, project_id)
        REFERENCES facts(id, project_id) ON DELETE CASCADE,
    FOREIGN KEY (run_id, project_id)
        REFERENCES task_runs(id, project_id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_verifications_fact
    ON fact_verifications(project_id, fact_id, created_at);
"""


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(row["name"] == column for row in rows)


def _add_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    if not _column_exists(conn, table, column):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


# Each entry migrates the database from version N-1 to N. Migrations must be
# idempotent: a crash between the DDL and the version bump re-runs them.
MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {
    2: lambda conn: conn.execute(
        """
        CREATE TABLE IF NOT EXISTS idempotency_keys (
            key TEXT NOT NULL,
            scope TEXT NOT NULL,
            payload_hash TEXT NOT NULL,
            response TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (key, scope)
        )
        """
    ),
}


def _migrate_v3_create_task_runs(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS task_runs (
            id TEXT NOT NULL,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            task_type TEXT NOT NULL,
            worker TEXT NOT NULL,
            model TEXT,
            phase TEXT NOT NULL DEFAULT 'execute',
            intent_id TEXT,
            command TEXT,
            exit_code INTEGER,
            timed_out INTEGER NOT NULL DEFAULT 0,
            cancelled INTEGER NOT NULL DEFAULT 0,
            stdout TEXT,
            stderr TEXT,
            stdout_sha256 TEXT,
            stdout_path TEXT,
            stderr_path TEXT,
            stdout_truncated INTEGER NOT NULL DEFAULT 0,
            duration_ms INTEGER,
            tokens_in INTEGER,
            tokens_out INTEGER,
            cost_usd REAL,
            status TEXT NOT NULL,
            client_run_id TEXT,
            error TEXT,
            created_at TEXT NOT NULL,
            finished_at TEXT,
            PRIMARY KEY (id, project_id)
        );

        CREATE INDEX IF NOT EXISTS idx_task_runs_project
            ON task_runs(project_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_task_runs_intent
            ON task_runs(project_id, intent_id);
        """
    )


MIGRATIONS[3] = _migrate_v3_create_task_runs


def _migrate_v4_facts_evidence(conn: sqlite3.Connection) -> None:
    # Facts v2: every fact carries a kind, a verification status, and a
    # confidence; new facts start as hypotheses until something verifies
    # them. Existing rows fall back to observation/hypothesis.
    for column, ddl in (
        ("kind", "TEXT NOT NULL DEFAULT 'observation'"),
        ("status", "TEXT NOT NULL DEFAULT 'hypothesis'"),
        ("confidence", "REAL NOT NULL DEFAULT 0.5"),
        ("subject_json", "TEXT"),
        ("supersedes", "TEXT"),
        ("created_by_run", "TEXT"),
        ("created_at", "TEXT"),
    ):
        _add_column(conn, "facts", column, ddl)
    _add_column(conn, "settings", "require_evidence", "INTEGER NOT NULL DEFAULT 0")
    _add_column(conn, "settings", "verify_max_attempts", "INTEGER NOT NULL DEFAULT 3")
    conn.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_facts_project_status
            ON facts(project_id, status);

        CREATE TABLE IF NOT EXISTS evidence (
            id TEXT NOT NULL,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            fact_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            command TEXT,
            exit_code INTEGER,
            match_rule TEXT NOT NULL DEFAULT 'contains',
            match_value TEXT,
            stdout_sha256 TEXT,
            artifact_path TEXT,
            excerpt TEXT,
            captured_at TEXT NOT NULL,
            PRIMARY KEY (id, project_id),
            FOREIGN KEY (fact_id, project_id)
                REFERENCES facts(id, project_id) ON DELETE CASCADE,
            FOREIGN KEY (run_id, project_id)
                REFERENCES task_runs(id, project_id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_evidence_fact
            ON evidence(project_id, fact_id);
        CREATE INDEX IF NOT EXISTS idx_evidence_run
            ON evidence(project_id, run_id);

        CREATE TABLE IF NOT EXISTS fact_verifications (
            id TEXT NOT NULL,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            fact_id TEXT NOT NULL,
            run_id TEXT,
            method TEXT NOT NULL,
            result TEXT NOT NULL,
            verifier_model TEXT,
            expected TEXT,
            observed TEXT,
            details TEXT,
            created_at TEXT NOT NULL,
            PRIMARY KEY (id, project_id),
            FOREIGN KEY (fact_id, project_id)
                REFERENCES facts(id, project_id) ON DELETE CASCADE,
            FOREIGN KEY (run_id, project_id)
                REFERENCES task_runs(id, project_id) ON DELETE SET NULL
        );

        CREATE INDEX IF NOT EXISTS idx_verifications_fact
            ON fact_verifications(project_id, fact_id, created_at);
        """
    )


MIGRATIONS[4] = _migrate_v4_facts_evidence


def _migrate_v5_hint_queue(conn: sqlite3.Connection) -> None:
    # Steering queue for the native runtime: hints land here while a task
    # runs and are consumed (atomically) between agent steps. Kept apart
    # from the persistent hints table — steering is ephemeral.
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS hint_queue (
            id TEXT NOT NULL,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            content TEXT NOT NULL,
            creator TEXT NOT NULL DEFAULT 'operator',
            consumed_at TEXT,
            created_at TEXT NOT NULL,
            PRIMARY KEY (id, project_id)
        );

        CREATE INDEX IF NOT EXISTS idx_hint_queue_pending
            ON hint_queue(project_id, consumed_at);
        """
    )


MIGRATIONS[5] = _migrate_v5_hint_queue


def _migrate_v6_events(conn: sqlite3.Connection) -> None:
    # Realtime stream from the native runtime: token deltas, tool
    # starts/outputs, task lifecycle. Monotonic per-project id (scoped
    # counter) doubles as the SSE/polling cursor.
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS events (
            id TEXT NOT NULL,
            seq INTEGER NOT NULL,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            task_type TEXT,
            kind TEXT NOT NULL,
            data TEXT,
            created_at TEXT NOT NULL,
            PRIMARY KEY (id, project_id)
        );

        CREATE INDEX IF NOT EXISTS idx_events_project_seq
            ON events(project_id, seq);
        """
    )


MIGRATIONS[6] = _migrate_v6_events


def _migrate_v7_scope(conn: sqlite3.Connection) -> None:
    # Operation scope: what the agents are allowed to touch. Stored on
    # the project so every tool call can be checked against it.
    _add_column(conn, "projects", "scope_json", "TEXT")


MIGRATIONS[7] = _migrate_v7_scope


def _migrate_v8_risk(conn: sqlite3.Connection) -> None:
    # Risk classification per intent: high/critical intents wait for
    # explicit operator approval before the dispatcher will run them.
    _add_column(conn, "intents", "risk_class", "TEXT DEFAULT 'low'")
    _add_column(conn, "intents", "approved_at", "TEXT")
    _add_column(conn, "intents", "approved_by", "TEXT")


MIGRATIONS[8] = _migrate_v8_risk


def _migrate_v9_budget(conn: sqlite3.Connection) -> None:
    # Operation budget: hard ceilings the dispatcher enforces before
    # dispatching anything new.
    _add_column(conn, "projects", "budget_json", "TEXT")


MIGRATIONS[9] = _migrate_v9_budget


def _migrate_v10_resource_locks(conn: sqlite3.Connection) -> None:
    # Exclusive access to risky targets: a worker holding the lock owns
    # the resource; others are refused until it expires or is released.
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS resource_locks (
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            resource TEXT NOT NULL,
            worker TEXT NOT NULL,
            acquired_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            PRIMARY KEY (project_id, resource)
        );
        """
    )


MIGRATIONS[10] = _migrate_v10_resource_locks


def _migrate(conn: sqlite3.Connection) -> None:
    row = conn.execute("SELECT version FROM schema_version WHERE id = 1").fetchone()
    current = int(row["version"]) if row is not None else 1
    if current >= SCHEMA_VERSION:
        return
    # Python's sqlite3 legacy mode does not open a transaction for DDL, so a
    # failed migration could leave half-applied tables behind. Start one
    # explicitly; SQLite DDL is transactional once a transaction is active.
    started_here = not conn.in_transaction
    if started_here:
        conn.execute("BEGIN")
    try:
        for version in range(current + 1, SCHEMA_VERSION + 1):
            migration = MIGRATIONS.get(version)
            if migration is not None:
                migration(conn)
            conn.execute(
                "UPDATE schema_version SET version = ? WHERE id = 1", (version,)
            )
    except Exception:
        if started_here:
            conn.rollback()
        raise
    else:
        if started_here:
            conn.commit()


def configure(path: Path) -> None:
    global _db_path
    if _db_path is not None:
        return
    _db_path = path
    _db_path.parent.mkdir(parents=True, exist_ok=True)
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


def reset_for_tests() -> None:
    """Forget the configured database path.

    Production code calls ``configure`` exactly once per process. Tests need
    to point the module at a temporary database per test, so they call this
    between cases. Connections are short-lived and always closed by
    ``get_conn``; there is no global connection to tear down.
    """
    global _db_path
    _db_path = None


@contextmanager
def get_conn() -> Generator[sqlite3.Connection, None, None]:
    assert _db_path is not None
    conn = sqlite3.connect(str(_db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
