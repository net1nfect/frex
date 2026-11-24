# Evidence dan verifikasi

Schema v4 memberi setiap fact sebuah `kind`, `status` verifikasi, dan
`confidence`. Aturan yang dilayani seluruh desain: **klaim hanya sebaik
run yang menghasilkannya, dan klaim terverifikasi adalah klaim yang
direproduksi container fresh.**

## Siklus hidup fact

```
hypothesis ──pass──▶ verified
     │                  │
     │                  └──fail──▶ disputed
     └──fail──▶ refuted
any ──supersede──▶ superseded
```

- `origin` dan `goal` lahir `verified` (confidence 1.0): keduanya jangkar
  proyek, benar by definition.
- Fact buatan agent (`conclude`, `reopen`) mulai sebagai
  `observation`/`hypothesis` confidence 0.5 — evidence membuktikan
  command berjalan dan menghasilkan output tertentu, bukan bahwa klaim
  itu benar.
- `fail` me-refute fact yang belum terbukti, tapi hanya *dispute* fact
  yang sudah verified; fact yang refute tetap refuted sebagai kenangan
  kegagalan. `inconclusive` tidak mengubah apa pun. Fact superseded
  menolak verifikasi.

## Merekam evidence

`POST /projects/{id}/intents/{intent_id}/conclude` menerima: deskripsi,
`kind`, `subject`, `confidence`, `match_rule`/`match_value`, dan
`run_id` — id dari `task_runs` yang menghasilkan klaim. Server menolak
`run_id` yang tidak ada di proyek (404), sehingga rantai
klaim → run → evidence tertutup tanpa wiring manual.

## Verifikasi

Task `verify` memutar ulang evidence di container fresh dan membalik
status: `pass` → verified, `fail` → refuted (atau `disputed` bila sudah
verified), `inconclusive` tidak mengubah apa pun. Detail lengkap:
`docs/dev/evidence-verify.md` (bahasa Inggris, sumber kebenaran).
