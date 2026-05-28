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

## Read-Only Filtering Simulation Policy

Policy ini berlaku **hanya** untuk simulasi Week 2 dan bersifat recommendation-only.

### Policy rules
1. **BLOCK/ALERT** jika `matched_regime == "RISK OFF"`.
2. **BLOCK/ALERT** jika normalized score `< 0.2`.
   - Jika score berada di rentang `0–100`, normalisasi: `score_norm = score / 100`.
   - Jika score sudah di rentang `0–1`, gunakan langsung.
3. **BLOCK/ALERT** jika `holding_candles <= 3`.
4. Baris `FLAT` dikeluarkan dari metrik winrate default, namun tetap dilaporkan sebagai `excluded_flat_count`.

### Why RISK OFF is reduced
Regime **RISK OFF** secara historis cenderung memiliki profil risk/reward yang lebih lemah untuk setup ini, sehingga ditandai untuk reduce exposure dalam simulasi kebijakan (bukan auto-eksekusi).

### Why score < 0.2 is alert-only
Skor model pada fase ini belum diperlakukan sebagai probabilitas terkalibrasi penuh; karena itu threshold `< 0.2` dipakai sebagai sinyal kehati-hatian untuk alert/block simulasi, bukan klaim probabilistik final.

### Why holding_candles <= 3 is weak zone
Holding yang sangat pendek (`<=3`) diperlakukan sebagai weak zone karena noise mikrostruktur dan ketidakstabilan sinyal jangka sangat pendek dapat meningkatkan variasi outcome.

### Execution governance (strict)
- **No execution changes**: tidak ada perubahan broker/order routing/execution engine/strategy logic/live connector.
- **No live trading changes**: tidak ada perubahan perilaku trading utama.
- **No auto-promotion**: hasil simulasi tidak boleh memicu promotion otomatis.
- **PAPER_ONLY remains enforced**: semua hasil hanya diagnosis/log/alert dan recommendation-only.
