# ADR 002: Evidence model — klaim agent dimulai sebagai hipotesis

## Status

Accepted (v0.3)

## Konteks

Fakta yang diklaim agent diperlakukan sama dengan fakta hasil verifikasi
— graf tidak bisa membedakan mana yang terbukti. Untuk red teaming,
klaim salah yang tak ditandai adalah risiko operasional nyata.

## Keputusan

- Facts v2: `kind` (observation/credential/access/vulnerability/...),
  `status` (hypothesis/verified/refuted/disputed/superseded),
  `confidence`, `subject_json`, `created_by_run`.
- Klaim agent lahir `hypothesis` confidence 0.5. Hanya verifikasi
  (task `verify` dengan evidence) yang membalik status: pass → verified,
  fail → refuted (atau disputed bila sudah verified).
- Tabel `evidence` menyimpan bukti (run_id, command, match_rule/value,
  stdout hash); `fact_verifications` mencatat percobaan verifikasi.
- Fakta `origin`/`goal` lahir `verified` confidence 1.0 — benar by
  definition.
- `complete` dapat mensyaratkan evidence kind access/goal
  (`require_evidence`).
- Ingest dari tool output (nmap/nuclei) lahir verified — output tool
  adalah buktinya.

## Konsekuensi

- Export dan UI menunjukkan status/kind per fact; agent yang me-reason
  atas graf tahu mana yang terbukti.
- Setiap run (healthcheck/execute, semua task type) tercatat di
  `task_runs` via RunRecorder, dan conclude mengutip `run_id` dari exec
  penghasil klaim — rantai klaim→run→evidence tertutup.
- Healthcheck run tidak boleh dikutip sebagai evidence.
