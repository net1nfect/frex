# Evidence and verification

Schema v4 gives every fact a `kind`, a verification `status`, and
`confidence`. The rule the whole design serves: **a claim is only as good
as the run that produced it, and a verified claim is one a fresh
container reproduced.**

## Fact lifecycle

```
hypothesis ──pass──▶ verified
     │                  │
     │                  └──fail──▶ disputed
     └──fail──▶ refuted
any ──supersede──▶ superseded
```

- `origin` and `goal` are born `verified` (confidence 1.0): they are the
  project's anchors, true by definition.
- Agent-created facts (`conclude`, `reopen`) start as
  `observation`/`hypothesis` with confidence 0.5 — evidence proves the
  command ran and produced certain output, not that the claim is true.
- `fail` refutes an unproven fact but only *disputes* a verified one;
  a refuted fact stays refuted as the memory of the failure.
  `inconclusive` changes nothing. Superseded facts reject verification.

## Recording evidence

`POST /projects/{id}/intents/{intent_id}/conclude` accepts:

| field | notes |
|---|---|
| `run_id` | must belong to the same project (404 otherwise); inserts one evidence row copying the run's command, exit code, and stdout hash |
| `kind` | `observation`, `credential`, `access`, `vulnerability`, `negative` |
| `subject` | free-form JSON (host, account, ...) |
| `confidence` | 0..1 |
| `match_rule` | `exact`, `contains`, `regex`, `exit_code`, `manual`; defaults per kind: `vulnerability` → `regex`, `negative` → `exit_code`, others → `contains` |
| `request_id` | idempotency; the replay returns the stored response including evidence |

Without `run_id` the fact is a bare hypothesis with no evidence.

## Verifying

`POST /projects/{id}/facts/{fact_id}/verifications` with
`method` (`replay`, `cross_model`, `operator`, `ingestor`) and `result`
(`pass`, `fail`, `inconclusive`). Optional `run_id` cites the replay run.
Optional `request_id` makes the submission idempotent.

The status transition re-checks the status it decided from, so an
operator and a dispatcher verifying simultaneously retry briefly and
then 409 instead of applying a stale transition.

The dispatcher runs this automatically: workers that declare `verify`
in `task_types` get replay tasks for hypothesis facts that have
replayable evidence and attempts left (from `verify_max_attempts`).
The replay runs in a **fresh container** from the same image — no
project workspace — so earlier tasks cannot contaminate the verdict
(`tasks.verify.container: reuse` trades that safety for speed).
Every non-verdict is `inconclusive`, never `fail`: a timeout or a
broken worker says nothing about whether the fact is true.

## Completing a project

`POST /projects/{id}/complete` accepts `goal_evidence` fact ids; each
must exist, be `verified`, and carry at least one evidence row of kind
`access` or `goal`. With `require_evidence = 0` (the v0.3 default) the
field is optional for backward compatibility; the setting — exposed on
`GET/PUT /settings` — turns an empty list into a 409.

## Compatibility

`GET /capabilities` reports `{version, schema_version, features}`; the
dispatcher checks feature membership to pick the compatible code path
against older or newer servers.
