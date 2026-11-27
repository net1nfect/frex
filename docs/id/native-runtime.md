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
4. `AgentLoop` berjalan: model memanggil tools (`run_command`,
   `read_file`, `write_file`, `http_request`, `submit_result`) yang
   dieksekusi di container proyek.
5. Steering hint dari `POST /projects/{id}/hint-queue` disuntik antar
   langkah (konsumsi atomik, tidak pernah dua kali).
6. Event (`task.started`, `tool.started`, `tool.output`,
   `steering.received`, `task.finished`, `error`) di-buffer
   `EventPublisher` dan dikirim batch ke `POST /projects/{id}/events`;
   UI berlangganan `/events/stream` (SSE).
7. Satu baris `task_runs` merangkum loop dengan `tokens_in`,
   `tokens_out`, `cost_usd`; conclude mengutip `run_id` tersebut
   sehingga rantai evidence tetap utuh.

## Budget

`AgentBudget` (default): 30 langkah, 200k token, $5, 30 menit wall
clock. `tasks.explore.timeout` menjadi `max_wall_seconds`. Jawaban prosa
(panggilan `submit_result` tidak dilakukan) mendapat satu nudge, lalu
loop berakhir.

## Operasi (v0.3.3-ops)

- **Scope**: proyek punya `scope` (domains, cidrs, exclusions). Tool
  call di luar scope ditolak kembali ke model, bukan dieksekusi.
  `PUT /projects/{id}/scope`.
- **Risk & approval**: intent dengan `risk_class` high/critical
  menunggu `POST /intents/{id}/approve` sebelum dispatcher menjalankannya;
  `/reject` mencatat penolakan untuk audit.
- **Budget**: `budget` proyek (max_usd, max_tokens, max_tool_calls,
  deadline) menghentikan dispatch baru saat tercapai; task berjalan
  tetap selesai.
- **Resource locks**: `POST /projects/{id}/locks/acquire` — kepemilikan
  eksklusif target berisiko dengan TTL; lock kadaluarsa direklaim otomatis.
- **Secrets**: kredensial tersimpan terenkripsi (FREX_SECRET_KEY);
  fakta mengutip `secret://name`. Nilai hanya lewat
  `GET /secrets/{name}?reveal=1` dengan auth aktif.
- **Ingest**: `frex ingest --type nmap|nuclei --file <output>` mengisi
  graf dari output tool tanpa LLM (fakta lahir terverifikasi).

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
- Container E2E lulus lokal pada 2026-11-27; CI masih opt-in
  (`FREX_E2E=1`) karena image worker berukuran 15 GB.

## Verifikasi live (2026-11-27)

Native explore dijalankan end-to-end lewat gateway OpenAI-compatible
9router dengan model open-weight `oc/hy4-flash-free`, container
`python:3.12-slim`, dan `network_mode: host`. Tidak ada provider fake di
jalur ini.

Hasil proyek `proj_001`, intent `i001`:

- `run_006` (`explore`/`execute`) sukses dalam 2 langkah, 6577 token
  masuk dan 105 token keluar, biaya 0 (model gratis).
- Run ulang dengan `max_tokens`/`temperature` aktif (proyek `proj_002`)
  juga sukses: `run_002`, 2 langkah, 2612 token masuk, 116 token keluar,
  fakta `f001` = `FREX_LIVE_OK_2`.
- Model memanggil `run_command` (`echo FREX_LIVE_OK`) lalu
  `submit_result`; output container menjadi fakta `f001` =
  `FREX_LIVE_OK` dengan `created_by_run=run_006`.
- Intent `i001` dikonklusikan ke `f001`; status fakta tetap
  `hypothesis` sampai ada verifikasi terpisah.
- Urutan event: `task.started` → `tool.started` → `tool.output` →
  `tool.started` → `tool.output` → `task.finished`.

Live run ini menemukan dua bug yang tidak tertangkap unit test dan sudah
diperbaiki: urutan argumen registry/project di scheduler, dan wire format
tool call/result yang ditolak gateway. Container E2E dengan image worker
upstream juga lulus lokal pada 2026-11-27 (`FREX_E2E=1`); CI tetap
opt-in karena image berukuran 15 GB. Yang belum diverifikasi: vLLM atau
Ollama self-hosted secara langsung.
