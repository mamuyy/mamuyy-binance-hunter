# Phase 2C Calibration Diagnosis (Governance-Safe)

Script: `scripts/phase2c_calibration_diagnosis.py`

## Tujuan
Mendiagnosis mengapa target Phase 2C (`Brier <= 0.24`) belum tercapai dengan pendekatan read-only.

## Prinsip Governance
- **PAPER_ONLY enforced**.
- **Read-only diagnosis** (hanya baca dataset validasi + tulis report).
- **No synthetic fallback** (fail-fast jika data tidak memadai/tidak valid).
- **Tidak mengubah broker/order/execution/strategy/live connector**.
- **Tidak deploy model**.
- **Temporal split fit/eval** untuk calibrator (anti-leakage).
- Hasil selalu **recommendation only**; **tidak auto-promote Phase 2C** walaupun pass.

## Metode
- Baseline Brier (probability mentah).
- Platt scaling.
- Temperature scaling.
- Isotonic regression.
- Per-regime baseline Brier (jika kolom regime tersedia).

## Cara Pakai
```bash
python scripts/phase2c_calibration_diagnosis.py --validation-csv <path_csv_validasi>
```

Output ke `reports/`:
- `phase2c_calibration_diagnosis.json`
- `phase2c_calibration_diagnosis_per_regime.csv` (opsional, jika data regime ada)

## Konten Report
JSON mencakup:
- baseline commit
- dataset window
- sample count (total/fit/eval)
- metrik Brier (baseline + method calibrator)
- Brier gap vs target
- best method
- pass/fail status
- action plan rekomendasi
- flag governance dan anti-leakage warning

## Summary Note Resmi (Update Diagnosis Terbaru)

### Temuan Utama
- Distribusi score menunjukkan bahwa score Hunter **tidak bersifat probabilistik**.
- Actual winrate terlihat relatif **flat (~50%)** di hampir semua bucket score.
- Score lebih dekat ke fungsi **heuristic ranking / confidence tier** daripada calibrated probability.
- Temuan ini menjelaskan mengapa target Phase 2C (`Brier <= 0.24`) sulit dicapai walaupun berbagai metode kalibrasi telah diuji.
- `matched_regime` menunjukkan separation winrate yang lebih bermakna dibandingkan score.
- `holding_candles` juga menunjukkan korelasi edge yang lebih kuat dibanding score confidence.

### Interpretasi Governance-Safe
- Kesimpulan ini bersifat **diagnosis research-only**.
- Temuan ini **tidak mengubah status PAPER_ONLY**.
- Temuan ini **tidak mengakibatkan auto-close Phase 2C**.
- Temuan ini **tidak mengaktifkan live execution**.

### Rekomendasi Arah Riset Berikutnya
- Pivot eksplorasi ke **Regime-Aware Filtering** dan **Regime-Aware Allocation**.
- Pisahkan secara eksplisit peran **ranking score** vs **true probability estimation**.
- Evaluasi apakah Hunter membutuhkan **dedicated probabilistic layer** yang terpisah dari orchestration score.
- Fokus Week 2 pada **regime gating**, **exposure filtering**, dan **adaptive allocation** sebelum mencoba recalibration ulang.
