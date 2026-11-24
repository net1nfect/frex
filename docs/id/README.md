# Cairn — Ringkasan (Bahasa Indonesia)

Cairn adalah AI realtime red teaming engine. Dokumen ini ringkasan
berbahasa Indonesia; sumber kebenaran adalah README utama dan
`docs/en/`.

## Status (v0.3)

Yang ada di tree hari ini (masing-masing terverifikasi di repo):

- **Evidence & verifikasi** — fact membawa kind/status/confidence;
  klaim agent mulai sebagai hipotesis dan hanya verifikasi yang
  membaliknya. Lihat `docs/adr/002-evidence-model.md`.
- **Provider layer** — model adalah konfigurasi, bukan image. Satu
  adapter `openai_compat` mencakup vLLM/Ollama/llama.cpp; routing
  berpindah dari model yang mati. Lihat `docs/adr/001-provider-layer.md`.
- **Native agent runtime** — dispatcher menjalankan loop model dengan
  tools container, budget, steering, dan SSE streaming end-to-end.
  Lihat `docs/adr/003-native-runtime.md` dan `docs/id/native-runtime.md`.
- **Lapisan operasi** — scope guard, gerbang approval risiko, ceiling
  budget, resource lock, secret terenkripsi, dan ingestor output tool
  (`cairn ingest`).
- **Benchmark harness** — `bench/` menjalankan skenario lab dan
  membandingkan model pada time-to-goal, fact terverifikasi, dan biaya.

Yang belum: run end-to-end terverifikasi melawan model self-hosted
sungguhan (butuh vLLM/Ollama di loop), E2E container di CI, benchmark
dengan angka sungguhan.

## Menjalankan

```bash
uv run --project cairn cairn serve          # server
uv run --project cairn cairn dispatch --config dispatch.yaml  # dispatcher
```

Verifikasi: `uv run pytest -q` di `cairn/`.
