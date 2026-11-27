# Frex — Ringkasan (Bahasa Indonesia)

Frex adalah AI realtime red teaming engine. Dokumen ini ringkasan
berbahasa Indonesia; sumber kebenaran adalah README utama dan
`docs/en/`.

## Status (v0.4)

Yang ada di tree hari ini (masing-masing terverifikasi di repo):

- **Evidence & verifikasi** — fact membawa kind/status/confidence;
  klaim agent mulai sebagai hipotesis dan hanya verifikasi yang
  membaliknya. Lihat `docs/en/adr/002-evidence-model.md`.
- **Provider layer** — model adalah konfigurasi, bukan image. Satu
  adapter `openai_compat` mencakup vLLM/Ollama/llama.cpp; routing
  berpindah dari model yang mati. Lihat `docs/en/adr/001-provider-layer.md`.
- **Native agent runtime** — dispatcher menjalankan loop model dengan
  tools container, budget, steering, dan SSE streaming end-to-end.
  Lihat `docs/en/adr/003-native-runtime.md` dan `docs/id/native-runtime.md`.
- **Lapisan operasi** — scope guard, gerbang approval risiko, ceiling
  budget, resource lock, secret terenkripsi, dan ingestor output tool
  (`frex ingest`).
- **Benchmark harness** — `frex/bench/` menjalankan skenario lab dengan
  goal diverifikasi referee dan membandingkan model pada time-to-goal,
  fact terverifikasi, dan biaya.

Terverifikasi live pada 2026-11-27: native explore lewat gateway
OpenAI-compatible 9router ke model open-weight `oc/hy4-flash-free`,
menjalankan `echo FREX_LIVE_OK` di container, dan menghasilkan fact
`f001` lewat `run_006`. Container E2E dengan image worker upstream juga
lulus lokal (`FREX_E2E=1`). Detail ada di
`docs/id/native-runtime.md#verifikasi-live-2026-11-27`.

Benchmark nyata pada 2026-11-27 (lab DVWA lokal, dua model gratis
open-weight via gateway 9router, masing-masing satu run): keduanya
mencapai command execution `uid=33(www-data)` terverifikasi —
`oc/hy4-flash-free` dalam 130 detik, `sn/glm-5.3-flash:flex` dalam 436
detik. Angka dan catatannya di `frex/bench/README.md`. Yang belum:
vLLM/Ollama self-hosted secara langsung di loop.

## Menjalankan

```bash
uv run --project frex frex serve          # server
uv run --project frex frex dispatch --config dispatch.yaml  # dispatcher
```

Verifikasi: `uv run pytest -q` di `frex/`.
