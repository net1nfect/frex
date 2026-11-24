# Cairn — Documentation index (English)

Canonical docs live here; `docs/id/` holds the Indonesian versions.

- `evidence-verify.md` — fact lifecycle, verification semantics, and
  the claim → run → evidence chain.
- `native-runtime.md` — configuration and flow of the native agent
  runtime (providers, routing, budgets, streaming, operations layer).
- `auth.md` — bearer token authentication.
- `idempotency.md` — request idempotency contract.
- `testing.md` — test suite layout.
- `adr/` — architecture decision records:
  - 001 provider layer (models as configuration)
  - 002 evidence model (claims are hypotheses until verified)
  - 003 native runtime (loop in the dispatcher, tools in the container)
