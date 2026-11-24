# Idempotency semantics

Conclude and complete accept an optional `request_id`. Clients that can
retry (the dispatcher does) should always send one.

## Contract

- **Same `request_id` + equivalent payload** → the stored response is
  replayed with HTTP 200. The mutation is not executed again; no duplicate
  fact or completion intent is created.
- **Same `request_id` + different payload** → `409` with the detail
  `request_id was already used with a different payload`. Change the
  payload means a new request: generate a new id.
- **No `request_id`** → behaviour is unchanged from before: a repeated
  conclude returns `409 Intent already concluded`, a repeated complete
  fails on project status.

Payloads are compared with a canonical hash, so key order and formatting
differences do not matter; the `request_id` field itself is excluded from
the comparison.

## Scoping

Keys are scoped per endpoint, not globally:

- conclude → `conclude:<project_id>:<intent_id>`
- complete → `complete:<project_id>`

The same `request_id` may be used for different intents or a different
project without interference.

## When to generate a new id

Generate a fresh `request_id` whenever the client starts a logically new
operation — a new conclude with different content, or a completion attempt
after a reopen. Reuse an id only for retries of the exact same logical
operation.

## Notes for operators

- The `idempotency_keys` table is created by schema migration v2. Back up
  the database file before upgrading as usual.
- Rows are never replayed across intents; deleting a project removes its
  idempotency rows only if a future migration scopes them by project
  (currently they are global, keyed by scope string).
- Replay happens before the project status check, so retrying the
  completion of a now-completed project still returns the original
  response instead of a 403.
