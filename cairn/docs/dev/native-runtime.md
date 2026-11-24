# Native agent runtime

Native runtime menggantikan CLI bridge untuk task `explore`: dispatcher
berbicara langsung dengan API model dan model memakai tools di dalam
container. CLI bridge tetap default.

## Konfigurasi

```yaml
tasks:
  explore:
    timeout: 600
    conclude_timeout: 120
    native: true          # aktifkan agent loop

providers:
  - name: local-vllm
    type: openai_compat
    base_url: http://vllm.internal:8000/v1
    # api_key: ...        # opsional

models:
  - name: fast
    provider: local-vllm
    model: Qwen/Qwen3-32B          # id model native provider
    max_tokens: 4096
    temperature: 0.4
    input_cost_per_mtok: 0.0       # USD per juta token; self-hosted = 0
    output_cost_per_mtok: 0.0

routing:
  explore: [fast]        # chain; model pertama yang healthy menang
```

Tanpa `native: true` (atau tanpa providers/models/routing) perilaku
lama tidak berubah — registry provider dibangun lazily dan bridge
dipakai.

## Alur

1. Scheduler memilih worker, klaim intent, lalu `run_native_explore_task`.
2. `ProviderRegistry.resolve("explore")` memilih model dari routing chain.
3. Healthcheck container tetap dijalankan (tercatat sebagai run).
4. `AgentLoop` berjalan: model memanggil tools
   (`run_command`, `read_file`, `write_file`, `http_request`,
   `submit_result`) yang dieksekusi di container proyek.
5. Steering hint dari `POST /projects/{id}/hint-queue` disuntik antar
   langkah (konsumsi atomik, tidak pernah dua kali).
6. Event (`task.started`, `tool.started`, `tool.output`,
   `steering.received`, `task.finished`, `error`) di-buffer
   `EventPublisher` dan dikirim batch ke `POST /projects/{id}/events`;
   UI berlangganan `/events/stream` (SSE).
7. Satu baris `task_runs` merangkum loop dengan `tokens_in`,
   `tokens_out`, `cost_usd`; conclude mengutip `run_id` tersebut sehingga
   rantai evidence tetap utuh.

## Budget

`AgentBudget` (default): 30 langkah, 200k token, $5, 30 menit wall
clock. `tasks.explore.timeout` menjadi `max_wall_seconds`. Jawaban prosa
(panggilan `submit_result` tidak dilakukan) mendapat satu nudge, lalu
loop berakhir.

## Endpoint terkait

- `POST /projects/{id}/hint-queue` — enqueue steering (ephemeral).
- `GET  /projects/{id}/hint-queue` — konsumsi hint pending.
- `POST /projects/{id}/events` — batch event dari dispatcher.
- `GET  /projects/{id}/events?since=N` — polling dengan cursor.
- `GET  /projects/{id}/events/stream` — SSE dengan heartbeat.
- `GET  /projects/{id}/usage` — rollup token & biaya per task type.

## Batasan MVP

- Interactive sessions (reverse shell, SSH) belum didukung tool.
- `submit_result` wajib: loop tidak menerima jawaban akhir berupa prosa.
- Scope checking pluggable; kebijakan operasi nyata didefinisikan di
  Fase 4.
