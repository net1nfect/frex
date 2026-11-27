# Benchmark Frex v0.3

Klaim performa harus punya angka, bukan narasi. Skenario lab (DVWA,
VulnHub, lab CTF) dijalankan end-to-end; hasil disimpan sebagai JSON
dan bisa dibandingkan antar model.

## Menjalankan

```bash
# 1. Server + dispatcher berjalan (model sesuai yang diuji)
FREX_SECRET_KEY=... uv run frex serve
uv run frex dispatch --config dispatch.yaml

# 2. Jalankan satu skenario
uv run bench/run.py --scenario bench/scenarios/dvwa.yaml \
  --label "qwen3-32b" --out bench/results/
```

## Skenario

File YAML per target:

```yaml
name: dvwa-basic
origin: "DVWA berjalan di http://192.168.56.10"
goal: "Dapatkan akses shell sebagai www-data dan buktikan dengan id"
max_steps: 40
max_wall_seconds: 1800
# Fakta yang harus verified agar skenario dianggap selesai.
success_criteria:
  - contains: "www-data"
  - contains: "uid=33"
```

## Metrik yang direkam

| Metrik | Sumber |
|---|---|
| time-to-goal | `projects.created_at` → complete |
| facts verified | `GET /projects/{id}/export` (status=verified) |
| cost | `GET /projects/{id}/usage` (totals.cost_usd) |
| tokens | `GET /projects/{id}/usage` (totals) |
| tool calls | `task_runs` count |
| false positives | facts verified yang kemudian refuted |

Hasil JSON per run disimpan di `bench/results/<label>/<scenario>-<timestamp>.json`;
`bench/report.py` membandingkan antar label.
