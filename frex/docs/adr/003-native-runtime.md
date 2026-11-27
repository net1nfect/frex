# ADR 003: Native agent runtime — loop di dispatcher, tools di container

## Status

Accepted (v0.3)

## Konteks

CLI bridge menjalankan coding agent di dalam container: satu prompt
masuk, hasil keluar, tanpa streaming, steering, budget, atau visibilitas
selama berjalan. Untuk realtime red teaming ini salah bentuk.

## Keputusan

Agent loop berjalan di dispatcher (`runtime/agent_loop.py`): model
dipanggil langsung via provider layer, tools dieksekusi di container
proyek (`run_command`, `read_file`, `write_file`, `http_request`,
`submit_result`). Loop berakhir hanya lewat `submit_result` (jawaban
prosa dapat satu nudge, lalu berhenti).

- Budget: max_steps, max_tokens, max_usd, max_wall_seconds — loop
  berhenti, bukan terbakar tanpa batas.
- Steering: hint queue dikonsumsi atomik antar langkah.
- Streaming: token delta SSE end-to-end (adapter → loop → events → UI).
- Events: tabel `events` dengan seq monotonik per project sebagai
  cursor; polling dan SSE.
- Scope guard memvalidasi target tool sebelum eksekusi; pelanggaran
  dikembalikan ke model sebagai rejection, bukan dieksekusi.
- CLI bridge tetap default; `tasks.explore.native` mengaktifkan loop
  per deployment.

## Konsekuensi

- Realtime: operator melihat token dan tool call saat terjadi.
- Budget dan scope adalah mekanisme dispatcher, bukan disiplin model.
- Interaktif (reverse shell, SSH session) sengaja di luar MVP.
