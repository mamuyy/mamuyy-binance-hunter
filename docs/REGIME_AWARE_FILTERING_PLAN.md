# Regime-Aware Filtering / Allocation Plan (Week 2)

## Context
Phase 2C diagnosis menunjukkan skor Hunter belum probabilistik stabil; sinyal yang lebih informatif saat ini adalah `matched_regime` dan `holding_candles`.

Dokumen ini menetapkan **diagnosis-only workflow** untuk menyusun rekomendasi filtering/alokasi berbasis regime tanpa perubahan ke eksekusi strategi.

## Scope & Governance
- PAPER_ONLY wajib.
- Read-only diagnosis terhadap dataset historis.
- Tidak ada perubahan broker/order/execution.
- Tidak ada mutasi strategy logic.
- Tidak ada auto-promotion.
- Output murni rekomendasi: reduce / allow / monitor per regime.

## Input
- `data/ml_calibration_matched_20260520.csv`

## Diagnostic Outputs
- `reports/regime_aware_filtering_diagnosis.json`
- `reports/regime_aware_filtering_diagnosis.csv`

## Analisis yang Wajib
1. Winrate per `matched_regime`.
2. PnL per `matched_regime` (total dan rata-rata).
3. Sample count per regime.
4. Performansi berdasarkan `holding_candles`.
5. Interaksi score bucket vs regime.
6. Identifikasi regime untuk:
   - **reduce**: performa lemah/negatif,
   - **allow**: performa kuat/positif,
   - **monitor**: sampel rendah atau sinyal campuran.

## Decision Heuristic (Recommendation-Only)
- `monitor` jika sample regime < `min_samples`.
- `reduce` jika sample memadai namun winrate rendah atau PnL rata-rata negatif.
- `allow` jika sample memadai, winrate kuat, dan PnL rata-rata positif.
- Semua aturan bersifat diagnosis dan bukan auto-action.

## Next Step (manual)
Hasil diagnosis dipakai untuk menyiapkan proposal parameter filtering/alokasi di fase berikutnya dengan review manual dan paper validation tambahan.
