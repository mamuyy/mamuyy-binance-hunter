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

## Week 2B Robustness & Stability Analysis

Sebelum policy filtering dipertimbangkan untuk tahap lanjutan, perlu dilakukan stress test robustness untuk mengurangi risiko false confidence akibat luck, look-ahead bias, atau overfitting pada satu snapshot dataset.

### Why stress testing is required before promotion
- Peningkatan metrik pada satu simulasi belum cukup menjadi bukti stabilitas lintas waktu.
- Validasi robustness membantu memastikan improvement tidak hanya terkonsentrasi pada satu segmen periode.
- Hasil tahap ini tetap **diagnosis-only** dan **governance evidence**, bukan izin deployment.

### Time-split validation
- Dataset diurutkan berdasarkan `signal_timestamp`.
- Metrik winrate default mengecualikan baris `FLAT`, namun `excluded_flat_count` wajib dilaporkan.
- Dataset dibagi menjadi tiga segmen waktu: `early`, `middle`, `late`.
- Rule policy yang diuji per split (tetap read-only):
  1. block jika `matched_regime == "RISK OFF"`
  2. block jika `score_norm < 0.2`
  3. block jika `holding_candles <= 3`
- Per split dibandingkan:
  - rows before / after / blocked
  - winrate before / after
  - avg PnL before / after
  - total PnL before / after
  - consistency of improvement lintas split

### Sensitivity analysis
- Rule `RISK OFF` dan `score_norm < 0.2` dipertahankan.
- Threshold `holding_candles` diuji pada: `<=1`, `<=2`, `<=3`, `<=4`, `<=5`, `<=7`.
- Untuk setiap threshold dilaporkan:
  - `rows_kept`
  - `rows_blocked`
  - `retention_rate`
  - `winrate_after`
  - `avg_pnl_after`
  - `total_pnl_after`
- Tujuan: mencari sweet spot antara peningkatan kualitas setup dan trade retention.

### Governance conclusion
- Deployment/live execution **tidak diperbolehkan** dari hasil Week 2B saja.
- Output Week 2B bersifat recommendation-only dan digunakan sebagai evidence untuk review governance lanjutan.

## Week 2C Adaptive Regime-Aware Threshold Simulation

Week 2B menunjukkan policy statis dapat meningkatkan winrate dan rata-rata PnL secara agregat, namun stabilitas tidak konsisten terutama di split `late`. Karena itu, policy belum layak dipromosikan dan perlu diuji dengan threshold `holding_candles` yang adaptif per regime.

### Why static filter is not enough
- Rule `holding_candles <= 3` yang seragam lintas regime berpotensi terlalu kasar.
- Karakteristik noise/reversal pada `SIDEWAYS/CHOPPY` dan continuation pada `TRENDING BULL` bisa membutuhkan cutoff berbeda.
- Robustness yang tidak stabil menandakan kebutuhan policy yang lebih kontekstual, bukan satu threshold global.

### Why late split matters
- Split `late` merepresentasikan periode paling mendekati kondisi operasional terbaru pada dataset.
- Jika policy gagal mempertahankan kualitas di `late`, risiko degradasi saat forward period meningkat.
- Evaluasi Week 2C memprioritaskan apakah adaptive threshold memperbaiki stabilitas khususnya di `late`.

### Candidate policies (simulation only)
- **Baseline static**:
  - block `matched_regime == "RISK OFF"`
  - block `score_norm < 0.2`
  - block `holding_candles <= 3`
- **Conservative**:
  - `SIDEWAYS/CHOPPY`: block `holding_candles <= 2`
  - `TRENDING BULL`: block `holding_candles <= 3`
  - `RISK OFF`: block all / alert all
- **Balanced**:
  - `SIDEWAYS/CHOPPY`: block `holding_candles <= 3`
  - `TRENDING BULL`: block `holding_candles <= 2`
  - `RISK OFF`: block all / alert all
- **Trend-Favoring**:
  - `SIDEWAYS/CHOPPY`: block `holding_candles <= 4`
  - `TRENDING BULL`: block `holding_candles <= 2`
  - `RISK OFF`: block all / alert all

### Output and governance status
- Output diagnosis:
  - `reports/adaptive_filtering_results.json`
  - `reports/adaptive_filtering_time_split.csv`
- Hasil hanya untuk evidence governance dan komparasi policy; **no deployment allowed**.
- Tidak ada perubahan engine/eksekusi/live strategy dari simulasi ini.
- Result bersifat recommendation-only dan harus melalui review manual lanjutan.

## Week 2D Market Drift & Regime Transition Analysis

Simulasi adaptive threshold pada Week 2C belum cukup untuk menstabilkan performa pada split `late`. Hipotesis kerja Week 2D adalah adanya **market drift / regime transition** yang mengubah karakter pasar, sehingga degradasi tidak bisa diselesaikan hanya dengan tuning threshold `holding_candles`.

### Tujuan diagnosis drift
- Mendeteksi sinyal awal perubahan regime yang berpotensi merusak winrate dan expectancy.
- Mengidentifikasi titik collapse berbasis rolling winrate dan rolling rata-rata PnL.
- Membandingkan karakteristik before-vs-after collapse untuk evidence transisi pasar.
- Mengkuantifikasi statistik transisi regime (`A -> B`) dan outcome setelah transisi.

### Early warning signal candidates
- Kenaikan proporsi `RISK OFF`.
- Penurunan rolling winrate keseluruhan.
- Penurunan rolling rata-rata PnL keseluruhan.
- Kompresi `holding_candles` (mean/median menurun).
- Pergeseran distribusi `score` (mean/median dan profil sampel).
- Lonjakan frekuensi transisi regime.

### Output diagnosis Week 2D
- `reports/drift_detection_report.json`
- `reports/regime_transition_stats.csv`
- `reports/drift_rolling_metrics.csv` (opsional)

### Governance status (strict)
- Hasil Week 2D adalah **governance evidence only** untuk review manual.
- Hasil tidak boleh dipakai untuk live promotion langsung.
- Tetap berlaku: PAPER_ONLY, read-only, recommendation-only.
- Tidak ada perubahan engine, eksekusi broker/order, mutasi strategi, atau deployment live.

## Week 2D.1 Emergency Brake Simulation

Week 2D mendeteksi indikasi collapse sekitar `2026-05-23T20:59:59.999000+00:00`, termasuk kompresi `holding_candles_mean` dari kisaran ~13.59 ke ~10.30 setelah collapse. Karena adaptive threshold Week 2C belum menstabilkan split `late`, Week 2D.1 menambahkan **simulasi emergency brake** berbasis drift warning.

### Tujuan simulasi
- Menguji secara **paper-only / read-only** apakah temporary stop-trading setelah warning drift dapat menurunkan drawdown proxy atau mengurangi degradasi PnL setelah collapse.
- Tujuan utama **bukan** menaikkan trade count, melainkan menghindari eksposur saat struktur pasar melemah.

### Trigger logic (simulation only)
- Hitung rolling metrics terhadap `signal_timestamp`:
  - `rolling_winrate`
  - `rolling_avg_pnl`
  - `rolling_holding_candles_mean`
- Trigger `VOLATILITY_ALERT / BRAKE_ON` jika salah satu benar:
  - `rolling_winrate < 0.45`
  - `rolling_avg_pnl < 0.0`
  - `rolling_holding_candles_mean < 10`
- Saat brake aktif, block sinyal selama cooldown window (default 100 rows/candles), lalu evaluasi ulang saat cooldown selesai.

### Why holding_candles compression is an early warning candidate
Kompresi durasi holding dapat merefleksikan perubahan mikrostruktur (trend sustain melemah, reversal/noise meningkat), sehingga diperlakukan sebagai kandidat warning dini bersama metrik outcome rolling.

### Output & usage
- Output simulasi:
  - `reports/emergency_brake_simulation.json`
  - `reports/emergency_brake_events.csv`
- Hasil hanya untuk **recommendation-only governance evidence**.
- **No deployment allowed** dari hasil Week 2D.1 saja.
- Tetap berlaku larangan: no live execution, no engine changes, no strategy deployment.

## Week 2E Regime Transition Prediction & Early Warning

Week 2D.1 Emergency Brake Simulation terbukti membantu menahan drawdown secara reaktif. Week 2E menambahkan layer **proaktif**: diagnosis risiko transisi regime sebelum performa benar-benar collapse.

### Tujuan: dari reaktif ke proaktif
- Mengubah sinyal drift yang sebelumnya baru dipakai setelah degradasi terlihat, menjadi sinyal warning dini berbasis pola transisi regime.
- Memberikan evidence apakah warning score sudah meningkat sebelum titik collapse (jika timestamp collapse tersedia dari drift report).
- Tetap bersifat diagnosis-only: tidak ada perubahan logic eksekusi strategi.

### Transition Instability Score (0-100)
Skor ini merangkum ketidakstabilan struktur regime dengan komponen rolling:
1. frekuensi transisi regime,
2. entropy/churn regime,
3. proporsi transisi menuju `RISK OFF`,
4. proporsi regime change vs regime stable.

Semua komponen dinormalisasi ke skala 0-100 agar bisa dibandingkan lintas metrik.

### Early Warning Score (0-100)
Early warning menggabungkan:
- transition instability,
- volatility cluster proxy,
- holding time compression,
- score distribution shift,
- performance decay (winrate + avg PnL).

Kategori warning:
- `0-30`: **STABLE**
- `31-60`: **WATCH**
- `61-80`: **RISK_ELEVATED**
- `81-100`: **BRAKE_CANDIDATE**

### Output Week 2E
- `reports/transition_prediction_report.json`
- `reports/regime_transition_matrix.csv`
- `reports/transition_warning_timeseries.csv`

### Governance usage (strict)
- Output Week 2E adalah **evidence-only governance artifact** untuk manual review.
- Hasil **tidak boleh** dipakai langsung untuk deployment, auto-promotion, atau perubahan position sizing real-time.
- Tetap berlaku: PAPER_ONLY, read-only, recommendation-only, no live execution, no engine changes, no strategy deployment.
