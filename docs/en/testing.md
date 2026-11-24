# Testing

The test suite lives in `cairn/tests` and runs with `pytest`.

## Unit tests

```bash
cd cairn
uv sync
uv run pytest -q -m "not integration"
```

Unit tests never need Docker or a model. They cover:

- server routes (projects, intents, hints, settings, export, reason leases),
- dispatcher contracts and output parsing,
- scheduler decisions with fake project snapshots,
- the mock worker script executed through `subprocess`.

## Integration tests

The container end-to-end test is skipped by default. It needs Docker, the
worker image, and an explicit opt-in:

```bash
docker image inspect ghcr.io/oritera/cairn-worker-container:latest
CAIRN_E2E=1 uv run pytest -q -m integration
```

It starts a temporary server, creates a project, runs one dispatcher
iteration with the mock worker, and asserts the project completes.

## Fixtures

`tests/conftest.py` provides:

- `db_path` — a fresh sqlite file per test, using `db.reset_for_tests()`;
- `client` — a FastAPI `TestClient` bound to that database;
- `project` — a created project with `origin` and `goal` facts;
- `auth_headers` — empty headers until server auth is enabled.

Never share a database between tests. `db.configure()` is one-shot in
production, so tests must call `db.reset_for_tests()` before configuring a
new path.

## Conventions

- One behaviour per test, named after the contract it protects.
- Prefer API calls over direct SQL; use direct SQL only to simulate time
  passing (expired leases) or to assert storage details.
- Tests that need Docker must carry `@pytest.mark.integration`.
