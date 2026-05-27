# Experiment Registry Schema (Lightweight)

Tujuan: memberi metadata minimal agar eksperimen research (Phase2/Phase4 dan turunannya) tidak diulang tanpa sadar, tetap **PAPER_ONLY**, dan mudah diaudit dari Source of Truth.

## Scope yang dipantau
- `docs/`
- `reports/`
- `scripts/`
- `phase2*/`
- `phase4*/`
- `walkforward*/`
- `calibration*/`
- `robustness*/`

## Minimal Metadata Fields

| Field | Type | Required | Deskripsi |
|---|---|---:|---|
| `experiment_id` | string | ✅ | ID unik eksperimen, contoh: `exp-2026-05-27-phase4-threshold-01`. |
| `category` | enum/string | ✅ | Kategori utama: `threshold_tuning`, `calibration_rerun`, `robustness_rerun`, `temporal_diagnosis`, `nonlinear_exploration`, atau kategori lain yang jelas. |
| `hypothesis` | string | ✅ | Hipotesis eksplisit yang ingin diuji. |
| `baseline_commit` | string | ✅ | Commit hash baseline yang jadi acuan eksperimen. |
| `dataset_window` | string | ✅ | Rentang data, contoh: `2024-01-01..2025-12-31`. |
| `target_metric` | string | ✅ | Metrik utama yang dituju, contoh: `sharpe`, `max_drawdown`, `win_rate`. |
| `result_summary` | string | ✅ | Ringkasan hasil singkat (1-3 kalimat). |
| `status` | enum/string | ✅ | `planned`, `running`, `completed`, `rejected`, `superseded`. |
| `supersedes` | string/null | ✅ | `experiment_id` yang disupersede (jika ada), selain itu `null`. |
| `duplicate_of` | string/null | ✅ | `experiment_id` yang dianggap duplikat (jika ada), selain itu `null`. |
| `reproducibility_command` | string | ✅ | Perintah reproduce secara eksplisit (read-only/research-safe bila relevan). |
| `paper_only_confirmed` | boolean | ✅ | Harus `true` untuk menegaskan mode PAPER_ONLY tetap aktif. |
| `governance_notes` | string | ✅ | Catatan governance: alasan rerun, alasan supersede/reject, atau risk note. |

## Template Entry (contoh)

```yaml
experiment_id: exp-2026-05-27-phase4-threshold-01
category: threshold_tuning
hypothesis: "Penyesuaian threshold 0.02 -> 0.018 meningkatkan sharpe tanpa menambah drawdown > 5%."
baseline_commit: abc1234
dataset_window: "2024-01-01..2025-12-31"
target_metric: sharpe
result_summary: "Sharpe naik 0.14, drawdown stabil. Perlu validasi walkforward."
status: completed
supersedes: null
duplicate_of: null
reproducibility_command: "python scripts/find_duplicate_experiments.py --top-k 8"
paper_only_confirmed: true
governance_notes: "Eksperimen baru, tidak overlap langsung dengan exp-2026-05-20-phase4-threshold-02."
```

## Aturan Ringkas
1. Setiap eksperimen baru wajib punya `experiment_id` unik dan metadata lengkap.
2. Jika rerun substansial (hipotesis/data/metric berubah), gunakan entry baru + tautkan `supersedes` bila perlu.
3. Jika substansi sama, tandai `duplicate_of` dan status `rejected` atau `superseded` sesuai keputusan governance.
4. `paper_only_confirmed` wajib eksplisit `true` untuk semua entry dalam workflow ini.
