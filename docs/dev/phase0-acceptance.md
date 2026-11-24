# Phase 0 acceptance report

Date: 2026-11-25 · Branch: `feat/v0.3-hardening` · Baseline: cairn 0.2.1 (`86e5a03`)

## Scope delivered

Hardening and instrumentation only — no provider layer, no evidence model:

1. **Test harness** — isolated per-test sqlite databases (`db.reset_for_tests`),
   FastAPI `TestClient` fixtures, and full contract coverage of the existing
   server and dispatcher behaviour before anything changed.
2. **Schema migrations** — `schema_version` table with a transactional,
   idempotent migration runner; v2 `idempotency_keys`, v3 `task_runs`.
3. **Optional bearer auth** — `CAIRN_API_TOKEN` on the server, dispatcher
   client, and browser UI; off by default, warning logged when off.
4. **Atomic claims** — intent heartbeat/conclude and reason lease are single
   conditional `UPDATE`s guarded by `rowcount`; racing workers can no longer
   double-claim or double-conclude.
5. **Idempotent conclude/complete** — optional `request_id` replays the stored
   response atomically committed with the mutation; reused id with a different
   payload is a 409.
6. **Task runs** — `task_runs` table (v3), `POST/GET /projects/{id}/runs`,
   stdout/stderr capped at 2 MiB per field with server-side sha256, artifacts
   written to disk with validated ids, removed with the project.
7. **Dispatcher RunRecorder** — every container exec (execute and healthcheck)
   posts a run record; best-effort, output truncated to the API limit.

## Verification (observed, not assumed)

- `uv run pytest -q -m "not integration"` → **208 passed, 1 skipped** in ~5 s,
  run repeatedly (≥3×) to rule out flakiness in the parallel claim tests.
- Parallel claim tests: 8 threads racing heartbeat/conclude/reason-claim on a
  barrier produce exactly one 200 and seven 409s, every run.
- Migration rollback test: a failing migration leaves the schema and version
  untouched (DDL is wrapped in an explicit transaction because sqlite3's
  legacy mode does not open one for DDL).
- The parallelism and concurrency guarantees are enforced by SQL
  (`UPDATE ... WHERE` + `rowcount`), not by test timing, so the tests verify
  the mechanism even though two requests rarely interleave perfectly.

## Not verified here

- **Container E2E** — `tests/test_e2e_mock.py` is marked `integration` and
  skips unless `CAIRN_E2E=1` and the worker image exists; the image
  (`ghcr.io/oritera/cairn-worker-container:latest`) was not available locally.
- **CI** — `.github/workflows/ci.yml` is in place but has not run on a real
  remote (the repo has no GitHub remote yet).
- **Production-scale auth** — auth is covered by unit tests against the ASGI
  app, not by a long-running deployment behind a proxy.

## Bugs found and fixed during phase 0

- `db.configure()` was one-shot per process, making tests order-dependent —
  fixed with an explicit `reset_for_tests()`.
- Intent claim and conclude were read-then-write; two racing workers could
  both claim, and conclude could insert two facts for one intent.
- Reason lease had the same race on claim/heartbeat/release.
- sqlite3 legacy mode does not open a transaction for DDL, so a failed
  migration could leave half-applied tables — the runner now issues `BEGIN`
  explicitly and rolls back on error.

## Known limitations

- `idempotency_keys` rows are not tied to projects by foreign key; deleting a
  project leaves its keys in place (they are harmless — replay only returns
  stored responses).
- Run artifacts orphan only if the server crashes between the artifact write
  and the commit; there is no sweeper yet.
- `RunRecorder` truncates oversized output rather than marking
  `stdout_truncated`; the flag exists in the schema for a future
  streaming/upload path.

## Next steps (phase 1 preview)

- Evidence-backed facts: `facts.kind/status`, `evidence`,
  `fact_verifications` (see `cairn-v0.3-evidence-verify.md`).
- Native agent runtime with streaming for `explore`/`verify`.
- Wire `RunRecorder` into all dispatcher task paths and surface run ids in
  conclude payloads.
