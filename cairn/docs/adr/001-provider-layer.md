# ADR 001: Provider layer — model sebagai konfigurasi, bukan image

## Status

Accepted (v0.3)

## Konteks

Awalnya worker type dan model adalah satu knob yang sama: ganti model
berarti mengubah entry worker dan rebuild image. Untuk strategi
uncensored (model open-weight self-hosted via vLLM/Ollama) model justru
endpoint yang paling sering berpindah.

## Keputusan

Lapisan provider terpisah: `ProviderConfig` (adapter), `ModelConfig`
(id model native), dan `routing` — chain model per task type, model
pertama yang healthy menang. Satu adapter `openai_compat` mencakup
vLLM, Ollama, llama.cpp, TabbyAPI. Seluruh lapisan opsional; config
lama tanpa providers tetap valid.

## Konsekuensi

- Ganti model = edit YAML, tanpa rebuild.
- Fallback otomatis saat model mati, tanpa stall dispatch.
- Health memoized per tick; `invalidate_health` setelah failure runtime.
- Native agent loop dan CLI bridge bisa hidup berdampingan per
  deployment (`tasks.explore.native`).
