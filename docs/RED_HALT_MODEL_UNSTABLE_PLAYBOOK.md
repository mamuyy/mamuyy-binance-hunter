# RED HALT Playbook: MODEL UNSTABLE (PAPER_ONLY)

## Scope
Playbook ini khusus mitigasi **risk-safe** saat `risk_manager` memberi status `HALT` karena ML accuracy turun, tanpa menonaktifkan safety guard dan tanpa mengaktifkan broker/exchange execution.

## Triage cepat
1. Cek status risk engine:
   - `python main.py --risk-check`
2. Cek status model lifecycle:
   - `python main.py --model-status`
3. Retrain candidate ter-guard:
   - `python main.py --retrain-model`
4. Cek ulang status model:
   - `python main.py --model-status`

## Decision tree aman
- Jika candidate **accepted** (status production baru, walkforward sehat, PF/DD lolos), lanjut PAPER_ONLY dan monitor 24-72 jam.
- Jika candidate **rejected** dan rollback tersedia, gunakan rollback artifact `model_weights_previous.pkl` sebagai recovery terkontrol (manual promotion sesuai SOP operasional).
- Jika candidate belum stabil namun tidak ada rollback yang lebih baik, perpanjang validation window (tambahkan data terbaru, ulang retrain) sebelum mempertimbangkan tuning tolerance.

## Guardrail penting
- Jangan longgarkan `RISK_ML_ACCURACY_HALT` sebagai langkah pertama.
- Jangan ubah execution logic, scanner logic, broker API, atau database schema.
- Tetap `PAPER_ONLY` sampai metrik stabil.

## Evidence minimum sebelum keluar dari RED HALT
- Accuracy model >= threshold halt aktif.
- Walkforward score tidak dalam zona lemah.
- Profit factor tidak kolaps dibanding baseline production/rollback.
- Drawdown dan consecutive losses tidak men-trigger gate lain.
