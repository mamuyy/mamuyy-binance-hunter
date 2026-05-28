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
