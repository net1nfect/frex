# Native agent runtime

The native runtime replaces the CLI bridge for `explore` tasks: the
dispatcher talks to the model API directly and the model uses tools
inside the container. The CLI bridge remains the default.

## Configuration

```yaml
tasks:
  explore:
    timeout: 600
    conclude_timeout: 120
    native: true          # enable the agent loop

providers:
  - name: local-vllm
    type: openai_compat
    base_url: http://vllm.internal:8000/v1
    # api_key: ...        # optional

models:
  - name: fast
    provider: local-vllm
    model: Qwen/Qwen3-32B          # provider-native model id
    max_tokens: 4096
    temperature: 0.4
    input_cost_per_mtok: 0.0       # USD per million tokens; self-hosted = 0
    output_cost_per_mtok: 0.0

routing:
  explore: [fast]        # chain; first healthy model wins
```

Without `native: true` (or without providers/models/routing) the old
behavior is unchanged — the provider registry is built lazily and the
bridge is used.

## Flow

1. The scheduler picks a worker, claims the intent, then calls
   `run_native_explore_task`.
2. `ProviderRegistry.resolve("explore")` picks a model from the routing
   chain.
3. The container healthcheck still runs (recorded as a run).
4. `AgentLoop` runs: the model calls tools (`run_command`, `read_file`,
   `write_file`, `http_request`, `submit_result`) executed in the
   project container.
5. Steering hints from `POST /projects/{id}/hint-queue` are injected
   between steps (atomic consumption, never twice).
6. Events (`task.started`, `tool.started`, `tool.output`,
   `steering.received`, `task.finished`, `error`) are buffered by
   `EventPublisher` and sent in batches to `POST /projects/{id}/events`;
   the UI subscribes to `/events/stream` (SSE).
7. One `task_runs` row summarizes the loop with `tokens_in`,
   `tokens_out`, `cost_usd`; conclude cites that `run_id` so the
   evidence chain stays intact.

## Budget

`AgentBudget` (default): 30 steps, 200k tokens, $5, 30 minutes wall
clock. `tasks.explore.timeout` becomes `max_wall_seconds`. A prose
answer (no `submit_result` call) gets one nudge, then the loop ends.

## Related endpoints

- `POST /projects/{id}/hint-queue` — enqueue steering (ephemeral).
- `GET  /projects/{id}/hint-queue` — consume pending hints.
- `POST /projects/{id}/events` — batch events from the dispatcher.
- `GET  /projects/{id}/events?since=N` — cursor polling.
- `GET  /projects/{id}/events/stream` — SSE with heartbeat.
- `GET  /projects/{id}/usage` — token and cost rollup per task type.

## MVP limitations

- Interactive sessions (reverse shell, SSH) are not supported by tools
  yet.
- `submit_result` is required: the loop does not accept a prose final
  answer.
- Scope checking is pluggable; real operational policy is defined in
  Phase 4.

## Operations (v0.3.3-ops)

- **Scope**: projects have a `scope` (domains, cidrs, exclusions). Tool
  calls outside scope are rejected back to the model, not executed.
  `PUT /projects/{id}/scope`.
- **Risk & approval**: intents with `risk_class` high/critical wait for
  `POST /intents/{id}/approve` before the dispatcher runs them;
  `/reject` records the rejection for audit.
- **Budget**: project `budget` (max_usd, max_tokens, max_tool_calls,
  deadline) stops new dispatches once reached; running tasks still
  finish.
- **Resource locks**: `POST /projects/{id}/locks/acquire` — exclusive
  ownership of a risky target with a TTL; expired locks are reclaimed
  automatically.
- **Secrets**: credentials are stored encrypted (FREX_SECRET_KEY);
  facts cite `secret://name`. Values are only available via
  `GET /secrets/{name}?reveal=1` with auth enabled.
- **Ingest**: `frex ingest --type nmap|nuclei --file <output>` fills
  the graph from tool output without an LLM (facts are born verified).

## Live verification (2026-11-27)

Native explore ran end-to-end through the 9router OpenAI-compatible
gateway with the open-weight model `oc/hy4-flash-free`, a
`python:3.12-slim` container, and `network_mode: host`. No fake provider
was in this path.

Project `proj_001`, intent `i001`:

- `run_006` (`explore`/`execute`) succeeded in 2 steps, 6577 tokens in
  and 105 out, cost 0 (free model).
- The model called `run_command` (`echo FREX_LIVE_OK`) then
  `submit_result`; the container output became fact `f001` =
  `FREX_LIVE_OK` with `created_by_run=run_006`.
- Intent `i001` concluded to `f001`; the fact status stays `hypothesis`
  until separately verified.
- Event order: `task.started` → `tool.started` → `tool.output` →
  `tool.started` → `tool.output` → `task.finished`.
- A rerun with `max_tokens`/`temperature` active (project `proj_002`)
  also succeeded: `run_002`, 2 steps, 2612 tokens in, 116 out, fact
  `f001` = `FREX_LIVE_OK_2`.

This live run exposed three bugs that unit tests missed, all fixed: the
scheduler's registry/project argument order, invalid `task_runs` phases
(422, so runs were never recorded), and the tool call/result wire format
rejected by the gateway. A follow-up review found that
`max_tokens`/`temperature` from config were never sent; that is fixed
and was verified live via `proj_002`. Container E2E against the upstream
worker image also passed locally on 2026-11-27 (`FREX_E2E=1`); CI stays
opt-in because the image is 15 GB. Not yet verified: self-hosted vLLM or
Ollama directly.
