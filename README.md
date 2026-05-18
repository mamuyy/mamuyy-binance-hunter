# mamuyy-binance-hunter

Crypto scanner read-only untuk Binance USDT Futures. Project ini hanya memakai public market data Binance Futures, tidak memakai Binance API key, tidak melakukan auto buy/sell, dan tidak memiliki fitur withdrawal.

## Fitur V1

- Scan market Binance USDT Futures.
- Ambil top symbol USDT berdasarkan quote volume 24 jam.
- Ambil candle interval 15 menit.
- Hitung volume spike, breakout high 20 candle, liquidity sweep + reclaim low 20 candle, taker buy ratio, funding rate, dan open interest.
- Buat score 0-100.
- Deteksi market regime global sebelum alert dikirim.
- Deteksi advanced flow untuk smart money, squeeze risk, funding anomaly, OI expansion, dan market pressure.
- Kirim alert ke Telegram jika score memenuhi threshold.
- Simpan alert ke `signals_log.csv`.
- Simpan history market regime ke `regime_history.csv`.
- Simpan flow metrics ke `flow_log.csv`.
- Paper trading simulasi untuk menguji signal tanpa order sungguhan.
- Performance analytics untuk mengevaluasi paper trading seperti quant system.
- Machine learning research untuk menganalisis feature yang paling berkontribusi terhadap profitability.
- Walk-forward validation untuk menguji generalisasi model dan risiko overfit.
- Database engine SQLite untuk logging scalable, migration CSV, backup, dan query analytics.
- Live dashboard Streamlit untuk monitoring real-time observability dan analytics.
- Regime-specific model engine untuk memilih model adaptif per market regime.
- Portfolio construction engine untuk simulated portfolio risk, allocation, dan exposure.
- Execution simulation engine untuk simulasi slippage, spread, fee, fill, latency, dan liquidity impact.
- Shadow live engine untuk real-time live simulation tanpa real order placement.
- Orchestration engine untuk scheduler, health management, retry, dan failure isolation.
- Risk manager circuit breaker untuk safety gate, drawdown guard, stale heartbeat guard, dan exposure multiplier.
- Health guardian watchdog untuk memantau heartbeat, SQLite, dan session `tmux` secara ringan.
- Adaptive regime shadow penalty untuk riset dampak SIDEWAYS/RISK OFF tanpa mengubah alert live.
- Mode loop otomatis setiap 15 menit.
- Error handling per symbol agar scanner tetap jalan walaupun ada symbol yang gagal dibaca.

## Endpoint Public Binance Futures

Project ini memakai endpoint public berikut:

- `/fapi/v1/ticker/24hr`
- `/fapi/v1/klines`
- `/fapi/v1/openInterest`
- `/fapi/v1/fundingRate`

Tidak perlu Binance API key untuk V1.

## Scoring

- Volume spike >= 3x: +30
- Volume spike >= 2x: +20
- Breakout high 20 candle: +25
- Liquidity sweep + reclaim low 20 candle: +25
- Taker buy ratio 0.50 sampai 0.68: +15
- Funding rate netral, `abs(funding) < 0.0005`: +15

Alert hanya dikirim jika score >= `ALERT_SCORE_THRESHOLD`, default `75`.

## Market Regime Engine

File `market_regime.py` mendeteksi kondisi market global memakai data public Binance Futures:

- Candle `BTCUSDT` untuk EMA 50, EMA 200, ATR, trend strength, volatility, volume, dump, dan candle vertical.
- Aggregate ticker 24 jam sebagai total market volume proxy.
- BTC quote volume share sebagai dominance proxy sederhana.
- Funding rate `BTCUSDT` sebagai proxy overheating/euphoria.

Regime yang dideteksi:

- `TRENDING BULL`
- `SIDEWAYS / CHOPPY`
- `RISK OFF`
- `PANIC SELLING`
- `EUPHORIA`

Bot menambahkan `regime_name` dan `regime_score` ke setiap signal. Regime juga memfilter score:

- `PANIC SELLING`: score buy dikurangi kuat.
- `SIDEWAYS / CHOPPY`: breakout score dikurangi.
- `TRENDING BULL`: momentum score dinaikkan.
- `RISK OFF`: score dikurangi.
- `EUPHORIA`: score dikurangi agar tidak mengejar candle terlalu panas.

Format Telegram regime:

```text
🌎 MARKET REGIME
Current Mode: TRENDING BULL
Confidence: 82%
```

## Advanced Flow Engine

File `flow_engine.py` mendeteksi smart money activity dan kondisi leverage market crypto memakai data public Binance Futures:

- Funding rate dan funding history.
- Open interest current dan open interest history.
- Taker buy/sell volume dari candle.
- Candle structure, volatility candle, volume spike, dan OI expansion.

Metrics yang dihitung:

- `funding_zscore`
- `oi_expansion_rate`
- `taker_delta`
- `pressure_score`
- `squeeze_probability`

Deteksi flow:

- `LONG SQUEEZE RISK`
- `SHORT SQUEEZE RISK`
- `WHALE ACCUMULATION`
- `WHALE DISTRIBUTION`
- `NEUTRAL FLOW`

Adaptive behavior:

- Jika squeeze risk tinggi dan signal breakout, confidence breakout dikurangi.
- Jika `WHALE ACCUMULATION`, confidence dinaikkan.
- Jika `WHALE DISTRIBUTION`, confidence dikurangi.
- Jika funding terlalu crowded, signal diberi warning dan score disesuaikan.

Format Telegram flow:

```text
🚨 FLOW ALERT

Coin:
Pressure:
Funding:
OI Expansion:
Whale Activity:
Squeeze Risk:
Final Score:
```

## Setup

Pastikan Python 3.10+ sudah tersedia.

```bash
cd mamuyy-binance-hunter
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Isi `.env` dengan token Telegram dan chat id jika ingin mengirim alert:

```env
TELEGRAM_BOT_TOKEN=isi_token_bot_telegram
TELEGRAM_CHAT_ID=isi_chat_id_telegram
```

Token asli jangan disimpan ke repo.

## Menjalankan Sekali

```bash
python main.py --once
```

Jika Telegram belum dikonfigurasi, alert tetap dicetak di terminal dan disimpan ke `signals_log.csv`.

## Menjalankan Mode Loop

```bash
python main.py
```

Default loop berjalan setiap 15 menit. Ubah nilai ini di `.env`:

```env
SCAN_INTERVAL_MINUTES=15
```

## Menjalankan Paper Trading

Paper trading hanya simulasi. Bot tidak membuat order Binance, tidak buy/sell, dan tidak membutuhkan Binance API key.

```bash
python main.py --paper
```

Untuk menjalankan sekali saja dengan paper trading aktif:

```bash
python main.py --once --paper
```

Saat ada signal dengan score >= `ALERT_SCORE_THRESHOLD`, bot membuat simulated trade dengan aturan:

- Entry price = close candle terakhir.
- SL = -2%.
- TP1 = +3%.
- TP2 = +5%.

Semua simulated trade disimpan ke `paper_trades.csv` dengan kolom:

```text
timestamp,symbol,entry,current_price,pnl_percent,status,sl,tp1,tp2,score,regime_name,regime_score
```

Status trade:

- `OPEN`: harga belum kena SL, TP1, atau TP2.
- `TP1 HIT`: harga sudah naik minimal 3%.
- `WIN`: harga sudah naik minimal 5%.
- `LOSS`: harga turun minimal 2%.

Saat mode loop `--paper` berjalan, bot memperbarui trade aktif setiap loop memakai harga public Binance Futures. Bot juga mengirim summary paper trading harian ke Telegram jika Telegram sudah dikonfigurasi.

Format summary:

```text
📊 PAPER TRADING SUMMARY

Total Trade:
Win:
Loss:
Winrate:
Average PnL:
Best Coin:
Worst Coin:
```

## Performance Analytics Report

Generate report dari `paper_trades.csv`:

```bash
python main.py --report
```

Output yang dibuat:

- `performance_report.html`
- `equity_curve.csv`
- `charts/equity_curve.png`
- `charts/win_loss_distribution.png`

Metrics yang dihitung:

- Total Trades
- Winrate
- Loss Rate
- Average PnL
- Profit Factor
- Max Drawdown
- Average Win
- Average Loss
- Risk Reward Ratio
- Consecutive Wins
- Consecutive Losses
- Expectancy
- Sharpe Ratio sederhana
- Monthly Return
- Best Coin
- Worst Coin
- Regime Performance untuk `TRENDING BULL`, `SIDEWAYS / CHOPPY`, dan `PANIC SELLING`

Report HTML berisi summary cards, tables, charts, regime analysis, top performing coins, worst performing coins, dan latest signals.

Jika `paper_trades.csv` kosong, report otomatis memakai table SQLite `historical_outcomes` sebagai fallback backtest labeling. `historical_outcomes` adalah hasil simulasi historis untuk riset, bukan live trade dan bukan order Binance.

Telegram summary:

```text
📊 PERFORMANCE REPORT

Winrate:
Profit Factor:
Max DD:
Best Regime:
Worst Regime:
```

Jika winrate < 40%, max drawdown > 20%, atau profit factor < 1, summary akan menambahkan:

```text
⚠️ STRATEGY UNHEALTHY
```

## Machine Learning Research

Jalankan ML research engine:

```bash
python main.py --ml
```

Data source:

- `paper_trades.csv`
- `signals_log.csv`
- `flow_log.csv`
- fallback SQLite `historical_outcomes` join `signals` dan `flow_logs` jika `paper_trades.csv` kosong

Target klasifikasi:

- `WIN`
- `LOSS`
- `TP1 HIT`
- `TP2 HIT`

Features:

- `score`
- `volume_spike`
- `breakout`
- `liquidity_sweep`
- `funding_zscore`
- `oi_expansion_rate`
- `taker_delta`
- `pressure_score`
- `squeeze_probability`
- `regime_name`
- `regime_score`
- `whale_activity`
- `funding_warning`

Output:

- `model_output.json`
- `charts/feature_importance.png`
- `charts/correlation_heatmap.png`
- `charts/prediction_distribution.png`

ML engine memakai RandomForestClassifier sederhana, bukan deep learning. Jika data belum cukup, engine tetap membuat output placeholder dan tidak crash.

## Automated Model Retraining & Drift Mitigation

Jalankan guarded retraining:

```bash
python main.py --retrain-model
```

File utama:

- `retrain_model.py`
- `model_registry.json`

Output runtime:

- `model_weights_candidate.pkl`
- `model_weights.pkl`
- `model_weights_previous.pkl`
- `logs/retrain_walkforward.csv`

Behavior:

- Candidate model selalu disimpan dulu ke `model_weights_candidate.pkl`.
- Production model hanya diganti jika PF stabil/improve, max drawdown tidak memburuk signifikan, dan walkforward stability acceptable.
- Jika candidate ditolak, production model lama tetap dipakai.
- Jika production diganti, model lama disimpan sebagai rollback di `model_weights_previous.pkl`.
- Registry menyimpan version, train timestamp, PF, DD, walkforward score, dataset row count, warning drift, dan rollback availability.

Drift warning:

- `DRIFT WARNING`: accuracy/walkforward/PF memburuk.
- `MODEL AGING`: production model terlalu lama tidak refresh.
- `RETRAIN RECOMMENDED`: kualitas walkforward lemah atau accuracy terus turun.

Dashboard menampilkan section `ML Lifecycle & Drift Monitor` secara read-only. Engine ini analytics-only dan aman untuk cron bulanan VPS; tidak mengubah scanner, execution, broker integration, atau auto trading.

Telegram summary:

```text
🧠 ML ANALYSIS

Top Features:
1.
2.
3.

Most Profitable Regime:
Worst Regime:

Current Model Accuracy:
```

AI confidence score ditampilkan dalam skala 0-100 dan setup ranking dibagi menjadi:

- `HIGH QUALITY`
- `MEDIUM QUALITY`
- `LOW QUALITY`

## Walk-Forward Validation

Jalankan statistical validation:

```bash
python main.py --walkforward
```

Data source:

- `paper_trades.csv`
- `signals_log.csv`
- fallback SQLite `historical_outcomes` jika `paper_trades.csv` kosong

Fitur:

- Rolling train/test split.
- Train pada historical window.
- Test pada future unseen window.
- Repeat rolling process.
- Out-of-sample testing.
- Regime-specific testing.

Metrics:

- `average_accuracy`
- `average_precision`
- `average_recall`
- `average_profit_factor`
- `average_winrate`
- `regime_specific_accuracy`
- `model_stability_score`
- `overfit_risk_score`

Output:

- `walkforward_results.csv`
- `charts/walkforward_equity_curve.png`
- `charts/rolling_accuracy.png`
- `charts/rolling_winrate.png`

Model health classification:

- `ROBUST`
- `UNSTABLE`
- `OVERFIT RISK`

Jika train accuracy jauh di atas test accuracy, report akan memberi warning overfit. Engine ini hanya validasi statistik, bukan auto trade.

Telegram summary:

```text
🧪 WALK FORWARD REPORT

Model Health:
Overfit Risk:
Rolling Accuracy:
Rolling Winrate:
Best Regime:
Worst Regime:
```

## Historical Backfill Engine

Isi SQLite dengan data historis Binance USDT Futures untuk analytics, ML research, dan walk-forward validation:

```bash
python main.py --backfill --days 7
```

File utama:

- `backfill.py`

Data yang diambil:

- OHLCV klines dari Binance Futures.
- Funding history jika endpoint tersedia.
- Open interest history jika endpoint tersedia.

Backfill memakai symbol USDT teratas berdasarkan quote volume, mengikuti konfigurasi `TOP_SYMBOLS_LIMIT`, `MIN_QUOTE_VOLUME`, dan `CANDLE_INTERVAL`. Engine ini memakai scoring scanner existing untuk membuat historical signal record, lalu menyimpan data ke SQLite tanpa menghapus data live.

Tables yang diisi:

- `historical_klines`
- `historical_funding`
- `historical_open_interest`
- `historical_outcomes`
- `signals`
- `flow_logs`

Proteksi runtime:

- Dedupe berdasarkan `timestamp + symbol` untuk generated signal/flow.
- Dedupe berdasarkan `timestamp + symbol + interval` untuk historical candle.
- Safe rate limiting antar request.
- Funding/OI fallback graceful ke OHLCV-only mode jika endpoint historis tidak lengkap.

Engine ini tidak membuat order, tidak auto buy/sell, dan tidak mengubah live scanner logic.

## Historical Outcome Labeling

Label hasil historical signal memakai data `historical_klines`:

```bash
python main.py --label-outcomes --days 7
```

Logic:

- Entry price memakai close candle pada timestamp signal.
- Simulasi SL `-2%`, TP1 `+3%`, dan TP2 `+5%`.
- Jika TP/SL tidak tersentuh, outcome ditutup pada fixed holding period default 20 candle.
- `pnl_pct`, status, dan `WIN`/`LOSS` disimpan ke SQLite.

Output table:

- `historical_outcomes`

Proteksi:

- Dedupe berdasarkan `symbol + signal_timestamp`.
- Tidak menghapus DB, CSV, atau data live.
- Tidak membuat order Binance dan tidak mengubah strategy live.

## Historical Filter Optimizer

Cari kombinasi filter historis yang meningkatkan profit factor dan menekan drawdown:

```bash
python main.py --optimize-filters
```

Data source:

- SQLite `historical_outcomes`
- Join dengan `signals`
- Join dengan `flow_logs`

Filter yang diuji:

- `min_score`: 70, 75, 80, 85, 90
- `flow_state`
- `whale_activity`
- `squeeze_risk` LOW only
- range `funding_zscore`
- `oi_expansion_rate` positive only
- threshold `taker_delta`
- `regime_name`
- threshold `volume_spike`
- `breakout` true/false
- `liquidity_sweep` true/false

Output:

- `optimizer_results.csv`
- top setups by profit factor
- winrate, trade count, average PnL, max drawdown, expectancy
- recommended conservative setup

Optimizer ini hanya research/backtest filtering. Tidak mengubah live strategy, tidak membuat order, dan tidak menghapus DB.

## Historical Regime Label Fix

Isi label regime historis yang masih kosong atau `UNKNOWN` tanpa menimpa label valid:

```bash
python main.py --fix-regime-labels
```

Command ini membaca `historical_outcomes`, `signals`, dan `flow_logs`, lalu mengisi `signals.regime_name` secara konservatif memakai konteks historis seperti breakout, liquidity sweep, volume spike, squeeze risk, funding z-score, OI expansion, pressure score, dan taker delta.

Label turunan yang mungkin muncul:

- `TRENDING BULL`
- `TRENDING BEAR`
- `SIDEWAYS / CHOPPY`
- `HIGH VOLATILITY`
- `BREAKOUT EXPANSION`
- `MEAN REVERSION`
- `HISTORICAL_DERIVED`

Output command menampilkan `UNKNOWN before`, jumlah label yang diperbaiki, `UNKNOWN after`, dan distribusi regime. Ini hanya memperbaiki metadata riset historis; tidak mengubah live strategy, tidak auto trade, dan tidak menghapus DB.

## Regime-Specific Model Engine

Jalankan analysis:

```bash
python main.py --regime-models
```

File utama:

```text
regime_models.py
```

Model yang tersedia:

- `MomentumModel`: dipakai saat `TRENDING BULL`, fokus breakout, OI expansion, whale accumulation, dan trend continuation.
- `MeanReversionModel`: dipakai saat `SIDEWAYS / CHOPPY`, fokus liquidity sweep, exhaustion, funding extremes, dan failed breakout.
- `PanicRecoveryModel`: dipakai saat `PANIC SELLING` atau `RISK OFF`, fokus capitulation, squeeze probability, dan aggressive reversal.

Runtime signal memakai `model_selector()` untuk memilih model berdasarkan `regime_name`, lalu menambahkan:

- `regime_model`
- `regime_model_adjustment`
- `adaptive_confidence_score`
- `model_confidence`
- `expected_behavior`

Metrics:

- `regime_model_accuracy`
- `regime_model_profitability`
- `regime_model_winrate`

Charts:

- `charts/regime_model_comparison.png`
- `charts/regime_accuracy_heatmap.png`

Telegram:

```text
🧠 REGIME MODEL

Current Regime:
Selected Model:
Model Confidence:
Expected Behavior:
```

Jika data masih sedikit atau regime unknown, engine fallback ke model momentum konservatif dan tetap berjalan tanpa auto trade.

## Portfolio Construction Engine

Jalankan simulasi portfolio:

```bash
python main.py --portfolio
```

File utama:

- `portfolio_engine.py`
- `symbol_tags.json`

Categories:

- `AI`
- `RWA`
- `Meme`
- `Layer1`
- `Perp DEX`
- `Gaming`
- `DeFi`

Fitur:

- Portfolio exposure tracking.
- Sector allocation.
- Correlation analysis.
- Risk budget allocation.
- Max exposure limits.
- Position sizing simulation.
- Regime-based allocation.
- Portfolio health score.

Position sizing memakai:

- confidence score
- regime confidence
- volatility proxy
- drawdown state

Rules:

- High confidence menaikkan recommended size.
- Panic regime mengurangi exposure.
- High correlation/sector crowding mengurangi duplicate exposure.
- High drawdown mengurangi total risk.

Metrics:

- `portfolio_risk_score`
- `diversification_score`
- `concentration_score`
- `correlation_risk`
- `sector_exposure`

Charts:

- `charts/portfolio_allocation.png`
- `charts/sector_exposure.png`
- `charts/correlation_heatmap_portfolio.png`
- `charts/risk_budget_chart.png`

Telegram:

```text
📦 PORTFOLIO ENGINE

Portfolio Health:
Risk Score:
Diversification:
Largest Exposure:
Recommended Allocation:
```

Engine ini simulated portfolio only, tidak auto trade, dan fallback graceful jika DB/data masih sedikit.

## Portfolio Observability Layer

Jalankan observability read-only:

```bash
python main.py --portfolio-observer
```

File utama:

- `portfolio_observer.py`

Data source:

- `signals`
- `shadow_trades`
- `historical_outcomes`
- `risk_events`

Output:

- exposure by symbol
- exposure by regime
- exposure by market type
- concentration risk
- top correlated symbols jika historical outcomes cukup
- portfolio heat score `LOW` / `MEDIUM` / `HIGH`

Dashboard menampilkan section `Portfolio Observability` berisi top exposure symbols, regime exposure, market type exposure, top correlated symbols, dan warning concentration risk. Layer ini analytics-only, tidak mengirim order dan tidak mengubah scanner/execution logic.

## Opportunity Allocation Engine V1

Jalankan allocation research:

```bash
python main.py --allocate
```

File utama:

- `opportunity_allocator.py`

Data source:

- `signals`
- `shadow_trades`
- `historical_outcomes`
- `flow_logs`
- `regime_logs`
- `ml_results`
- `risk_events`
- `logs/adaptive_threshold_comparison.csv` jika tersedia

Output:

- `logs/opportunity_allocation.csv`

Kolom utama:

- `symbol`
- `opportunity_score`
- `risk_score`
- `allocation_tier`: `AVOID` / `WATCH` / `SMALL` / `PRIORITY`
- `reason`
- `suggested_max_weight_pct`

Engine ini analytics-only. Ia hanya memberi ranking dan suggested max weight untuk riset, tanpa auto trade, tanpa broker integration, dan tanpa mengubah scanner/execution logic.

## Execution Simulation Engine

Jalankan simulasi execution:

```bash
python main.py --execution
```

File utama:

- `execution_engine.py`
- `execution_log.csv`

Features:

- Slippage simulation.
- Spread simulation.
- Trading fee simulation.
- Partial fill simulation.
- Latency simulation.
- Liquidity impact estimation.
- Execution quality score.

Execution profile:

- `IDEAL`
- `NORMAL`
- `STRESSED`
- `PANIC`

Rules:

- `PANIC` regime menaikkan slippage.
- Low liquidity menaikkan partial fill risk.
- High volatility menaikkan spread.
- Whale activity menaikkan execution uncertainty.

Metrics:

- `expected_fill_price`
- `slippage_percent`
- `execution_cost`
- `fill_probability`
- `latency_risk`
- `liquidity_risk`
- `execution_quality_score`
- `execution_adjusted_pnl`

Charts:

- `charts/slippage_distribution.png`
- `charts/execution_quality_distribution.png`
- `charts/pnl_before_after_execution.png`

Telegram:

```text
⚡ EXECUTION ENGINE

Execution Profile:
Expected Slippage:
Fill Probability:
Execution Quality:
Adjusted PnL Impact:
```

Engine ini simulated execution only, tidak menempatkan order sungguhan.

## Shadow Live Engine

Jalankan shadow live simulation:

```bash
python main.py --shadow
```

File utama:

- `shadow_engine.py`

Fitur:

- Live market monitoring berbasis signal terbaru di DB.
- Real-time signal generation simulation.
- Real-time execution simulation.
- Live shadow portfolio tracking.
- Live PnL tracking.
- Signal aging dan lifecycle tracking.
- Real-time regime adaptation.
- Live execution quality monitoring.

Database:

- Table `shadow_trades`

Signal lifecycle:

- `signal generated`
- `signal triggered`
- `execution simulated`
- `trade closed`

Live shadow metrics:

- `live_winrate`
- `live_pnl`
- `live_drawdown`
- `live_exposure`
- `execution_drift`
- `prediction_drift`
- `regime_drift`

Charts:

- `charts/shadow_equity_curve.png`
- `charts/live_drawdown_curve.png`
- `charts/execution_drift_chart.png`
- `charts/regime_drift_chart.png`

Telegram:

```text
👻 SHADOW LIVE ENGINE

Live PnL:
Live Winrate:
Execution Drift:
Current Regime:
Shadow Exposure:
```

Shadow health classification:

- `HEALTHY`
- `WARNING`
- `UNSTABLE`

Jika execution drift tinggi, drawdown tinggi, atau prediction drift tinggi, health akan turun ke warning/unstable. Engine ini simulated live only dan tidak membuat real order.

## Orchestration Engine

Jalankan satu siklus orchestration:

```bash
python main.py --orchestrator
```

File utama:

- `orchestrator.py`
- `orchestrator_log.csv`

Engines yang dikoordinasikan:

- scanner
- regime
- flow
- ML
- walkforward
- portfolio
- execution
- shadow

Engine states:

- `RUNNING`
- `IDLE`
- `WARNING`
- `FAILED`
- `RECOVERING`

Scheduler profiles:

- `FAST`: scanner tiap 1 menit, flow tiap 2 menit.
- `NORMAL`: scanner tiap 5 menit, ML tiap 1 jam.
- `SAFE`: scanner tiap 15 menit, ML tiap 6 jam.

Runtime metrics:

- execution time
- failure count
- restart count
- average runtime
- last success timestamp

Orchestrator juga membuat heartbeat log, menghitung `system_health_score`, melakukan auto retry, auto restart simulation, dependency coordination, dan failure isolation. Jika DB lambat, failure tinggi, atau memory tinggi, scheduler akan degrade ke profile yang lebih aman.

Telegram:

```text
🛠 ORCHESTRATOR

System Health:
Running Engines:
Failed Engines:
Recovery Actions:
Scheduler Mode:
```

Ini coordination-only, tidak membuat real order.

## 24/7 VPS Runtime

Untuk Oracle Cloud VPS / Ubuntu 24.04, jalankan di `tmux`:

```bash
tmux new -s mamuyy
source .venv/bin/activate
python main.py --orchestrator
```

Lightweight health monitor:

```bash
python main.py --health
```

Runtime stabilization:

- Orchestrator menulis heartbeat setiap cycle ke table SQLite `runtime_heartbeats` dan tetap menulis fallback CSV ke `orchestrator_log.csv`.
- Setiap engine dijalankan terisolasi; crash satu engine tidak mematikan orchestrator.
- Auto retry dan restart simulation tersedia per engine.
- Binance public request memakai retry ringan untuk gangguan sementara.
- Orchestrator melakukan daily/size-based rotation untuk `orchestrator_log.csv`.
- Chart dan backup lama dibersihkan sesuai `LOG_RETENTION_DAYS`.
- DB temporary records lama dibersihkan sesuai `DB_RETENTION_DAYS`.
- `system_health_score` turun jika DB lambat, failure tinggi, atau memory tinggi.
- Scheduler auto-degrade dari `FAST` ke `NORMAL` atau `SAFE` saat runtime tidak sehat.

Operasional cepat:

```bash
python main.py --health
python main.py --risk-check
python main.py --health-guardian-once
python main.py --heartbeat-test
tail -f orchestrator_log.csv
python main.py --db-check
```

Catatan keamanan VPS:

- Jangan print `.env`.
- Jangan commit token Telegram.
- Engine tetap read-only/simulated; tidak ada auto trading.

## Risk Manager / Circuit Breaker

`risk_manager.py` adalah safety layer read-only untuk persiapan shadow execution. Modul ini tidak mengirim order dan tidak mengubah logic scanner.

Jalankan check manual:

```bash
python main.py --risk-check
```

Output utama:

- `SAFE`, `WATCH`, atau `HALT`.
- `risk_score`.
- `position_multiplier`.
- alasan risk gate aktif.

Gate default:

- HALT jika ML accuracy di bawah `RISK_ML_ACCURACY_HALT`.
- HALT jika drawdown mencapai `RISK_DRAWDOWN_HALT`; WATCH jika melewati `RISK_DRAWDOWN_WATCH`.
- HALT jika heartbeat orchestrator stale lebih dari `RISK_STALE_MINUTES`.
- SIDEWAYS / CHOPPY mengurangi multiplier 70%.
- TRENDING BEAR melakukan risk halt konservatif.
- HIGH VOLATILITY hanya diizinkan jika model confidence cukup.
- Max open/shadow trade dan consecutive loss cooldown dibatasi oleh konfigurasi.

Risk events dicatat ke table SQLite `risk_events`. Dashboard menampilkan bagian `Risk Engine Status` secara read-only tanpa menulis event baru.

## Adaptive Regime Shadow Penalty

`regime_shadow.py` menghitung score bayangan untuk riset, tanpa mengubah `score` asli, threshold alert, paper trading, shadow runtime, atau scanner strategy.

Rules analytics-only:

- `SIDEWAYS / CHOPPY`: `shadow_score = calculated_score * 0.20`
- `RISK OFF`: `shadow_score = calculated_score * 0.50`
- Regime lain: `shadow_score = calculated_score`

Nilai yang disimpan ke `signals`:

- `calculated_score`
- `shadow_score`
- `penalty_applied`

Dashboard menampilkan rata-rata calculated score, rata-rata shadow score, impact persentase, dan symbol yang paling terdampak.

Shadow equity validation:

```bash
python main.py --shadow-analysis
```

Command ini membandingkan:

- Original historical outcomes equity curve.
- Hypothetical shadow curve jika trade dengan `shadow_score < ALERT_SCORE_THRESHOLD` dilewati.
- Static threshold `65`, adaptive regime threshold, dan macro-adaptive emergency defense.

Macro-adaptive adalah simulasi analytics-only. Ia membuat deterministic synthetic `macro_stress_score` dari rolling PnL volatility, drawdown pressure, dan cluster loss historical outcomes. Jika `macro_stress_level = HIGH`, threshold simulasi dinaikkan ke `75` untuk menguji emergency defense mode tanpa mengambil data macro live dan tanpa mengubah runtime scanner.

Output:

- `shadow_equity_curve.csv`
- `shadow_comparison.csv`
- `logs/shadow_threshold_tuning.csv`
- `logs/shadow_threshold_walkforward.csv`
- `logs/adaptive_threshold_comparison.csv`
- `logs/adaptive_walkforward.csv`
- `logs/macro_stress_summary.csv`

Dashboard menampilkan `Shadow Penalty Simulation` dengan original vs shadow equity curve, drawdown reduction, trade reduction, avoided losses, skipped winners, regime impact summary, granular threshold tuning untuk `60` sampai `70`, 70/30 walkforward threshold validation, perbandingan original vs static `65` vs adaptive regime threshold vs macro-adaptive, adaptive walkforward, dan macro stress summary.

## Health Guardian Watchdog

`health_guardian.py` adalah watchdog ringan untuk VPS yang tetap kompatibel dengan `tmux`. Default-nya `DRY_RUN`, jadi aman untuk inspeksi tanpa memaksa restart.

Jalankan sekali:

```bash
python main.py --health-guardian-once
```

Test tulis/baca heartbeat SQLite:

```bash
python main.py --heartbeat-test
```

Yang dicek:

- SQLite health.
- Heartbeat orchestrator di table SQLite `runtime_heartbeats`.
- Fallback heartbeat dari `orchestrator_log.csv`.
- Fallback aktivitas dari `flow_logs` atau `regime_logs` jika heartbeat hilang tetapi engine masih update dalam 10 menit.
- Stale runtime jika heartbeat lebih lama dari `HEALTH_GUARDIAN_STALE_MINUTES`.
- Session `tmux` `hunter`.
- Session `tmux` `dashboard`.

Perilaku:

- Jika `hunter` hilang, guardian mencatat recovery action. Dengan `HEALTH_GUARDIAN_DRY_RUN=true`, guardian hanya menampilkan rencana restart.
- Jika `HEALTH_GUARDIAN_DRY_RUN=false`, guardian bisa membuat ulang session `hunter` dengan `python main.py --orchestrator`.
- Jika `dashboard` hilang dan `HEALTH_GUARDIAN_DRY_RUN=false`, guardian bisa membuat ulang session `dashboard` dengan Streamlit bind ke `127.0.0.1:8501`.
- Saat `HEALTH_GUARDIAN_DRY_RUN=true`, dashboard restart tetap warning/log saja kecuali `HEALTH_GUARDIAN_RESTART_DASHBOARD=true`.
- Restart session yang sama punya cooldown default 5 menit agar tidak restart loop.
- Semua event warning/stale/recovery dicatat ke table `risk_events`.
- Output menampilkan `Heartbeat Source`, misalnya `heartbeat_table`, `fallback_flow_logs`, atau `fallback_regime_logs`.

Contoh konfigurasi aman:

```env
HEALTH_GUARDIAN_DRY_RUN=true
HEALTH_GUARDIAN_STALE_MINUTES=10
HEALTH_GUARDIAN_HUNTER_SESSION=hunter
HEALTH_GUARDIAN_DASHBOARD_SESSION=dashboard
HEALTH_GUARDIAN_RESTART_DASHBOARD=false
HEALTH_GUARDIAN_RESTART_COOLDOWN_SECONDS=300
HEALTH_GUARDIAN_PROJECT_DIR=~/mamuyy-binance-hunter
```

Enable guarded recovery di VPS:

```env
HEALTH_GUARDIAN_DRY_RUN=false
```

Kembalikan ke mode inspeksi aman:

```env
HEALTH_GUARDIAN_DRY_RUN=true
```

Untuk menjalankan berkala di VPS tanpa PM2, pakai cron/systemd timer atau loop shell sederhana yang memanggil command ini tiap 5 menit. Tetap gunakan `tmux`, bukan PM2.

## Database Engine

Default database memakai SQLite bawaan Python, tanpa ORM. Jalankan health check, auto-create table, migration CSV, dan backup:

```bash
python main.py --db-check
```

Default file database:

```text
mamuyy_hunter.db
```

Tables:

- `signals`
- `paper_trades`
- `flow_logs`
- `regime_logs`
- `ml_results`
- `walkforward_results`
- `shadow_trades`
- `historical_klines`
- `historical_funding`
- `historical_open_interest`
- `historical_outcomes`
- `risk_events`
- `runtime_heartbeats`

Yang dilakukan `--db-check`:

- Membuat semua table jika belum ada.
- Membuat index untuk `timestamp` dan `symbol`.
- Migrasi CSV yang sudah ada:
  - `signals_log.csv`
  - `paper_trades.csv`
  - `flow_log.csv`
  - `regime_history.csv`
  - `walkforward_results.csv`
- Membuat backup database ke `db_backups/`.
- Tetap graceful jika CSV kosong atau belum ada data.

Runtime baru juga menulis record penting ke database:

- Signal alert ke `signals`
- Paper trade baru ke `paper_trades`
- Flow metrics ke `flow_logs`
- Market regime ke `regime_logs`
- ML output ke `ml_results`
- Walk-forward folds ke `walkforward_results`

Optional PostgreSQL:

```env
DATABASE_URL=postgresql://user:password@host:5432/dbname
```

PostgreSQL disiapkan sebagai mode optional, tetapi dependency driver tidak dibundel. Default yang siap pakai adalah SQLite.

Query analytics helper tersedia di `database.py`:

- `top_profitable_setup()`
- `best_regime()`
- `best_symbol()`
- `worst_symbol()`
- `feature_profitability()`

## Live Dashboard

Jalankan dashboard:

```bash
streamlit run dashboard.py
```

Dashboard memakai SQLite existing dan tetap jalan walaupun database masih kosong. Auto refresh berjalan setiap 60 detik.

Sections:

- `SYSTEM HEALTH`: scanner status, database status, latest runtime, latest signal timestamp, latest ML run, latest walkforward run, dan total DB rows.
- `MARKET REGIME`: current regime, confidence, dan history chart.
- `LIVE SIGNALS`: latest signals, score, flow state, whale activity, squeeze risk.
- `PAPER TRADING`: open trades, winrate, PnL curve, current drawdown, best/worst trade.
- `FLOW ANALYTICS`: funding anomaly, pressure score, whale accumulation frequency, squeeze probability.
- `ML ANALYTICS`: feature importance, model accuracy, prediction distribution, model health.
- `WALKFORWARD ANALYTICS`: rolling accuracy, rolling winrate, overfit risk, regime performance.
- `DATABASE ANALYTICS`: top symbols, top profitable setup, feature profitability, regime profitability.

Status color:

- `GREEN`: healthy.
- `YELLOW`: warning.
- `RED`: unhealthy.

Alert banner muncul jika:

- DB health check gagal.
- ML accuracy drop.
- Drawdown terlalu tinggi.
- Tidak ada signal terlalu lama.

## Konfigurasi `.env`

```env
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=

BINANCE_BASE_URL=https://fapi.binance.com
SCAN_INTERVAL_MINUTES=15
TOP_SYMBOLS_LIMIT=30
CANDLE_INTERVAL=15m
CANDLE_LIMIT=60
ALERT_SCORE_THRESHOLD=75
REQUEST_TIMEOUT_SECONDS=15
MIN_QUOTE_VOLUME=0
DATABASE_URL=
DATABASE_PATH=mamuyy_hunter.db
DATABASE_BACKUP_DIR=db_backups
SIGNALS_LOG_PATH=signals_log.csv
REGIME_HISTORY_PATH=regime_history.csv
FLOW_LOG_PATH=flow_log.csv
PAPER_TRADES_PATH=paper_trades.csv
EQUITY_CURVE_PATH=equity_curve.csv
PERFORMANCE_REPORT_PATH=performance_report.html
MODEL_OUTPUT_PATH=model_output.json
WALKFORWARD_RESULTS_PATH=walkforward_results.csv
CHART_OUTPUT_DIR=charts
PAPER_SUMMARY_STATE_PATH=.paper_summary_state
ORCHESTRATOR_PROFILE=NORMAL
LOG_RETENTION_DAYS=14
DB_RETENTION_DAYS=90
MAX_LOG_BYTES=5000000
RISK_ML_ACCURACY_HALT=45
RISK_DRAWDOWN_HALT=-20
RISK_DRAWDOWN_WATCH=-10
RISK_STALE_MINUTES=10
RISK_MAX_OPEN_TRADES=10
RISK_LOSS_COOLDOWN=3
RISK_BASE_POSITION_MULTIPLIER=1.0
RISK_HIGH_VOL_CONFIDENCE_MIN=55
HEALTH_GUARDIAN_INTERVAL_SECONDS=300
HEALTH_GUARDIAN_STALE_MINUTES=10
HEALTH_GUARDIAN_DRY_RUN=true
HEALTH_GUARDIAN_RESTART_DASHBOARD=false
HEALTH_GUARDIAN_RESTART_COOLDOWN_SECONDS=300
HEALTH_GUARDIAN_PROJECT_DIR=~/mamuyy-binance-hunter
HEALTH_GUARDIAN_HUNTER_SESSION=hunter
HEALTH_GUARDIAN_DASHBOARD_SESSION=dashboard
```

## Struktur File

```text
main.py
scanner.py
telegram.py
logger.py
config.py
database.py
dashboard.py
tracker.py
market_regime.py
flow_engine.py
analytics.py
report_generator.py
ml_engine.py
retrain_model.py
walkforward.py
backfill.py
outcome_labeler.py
filter_optimizer.py
regime_labeler.py
regime_models.py
regime_shadow.py
shadow_analysis.py
portfolio_engine.py
portfolio_observer.py
opportunity_allocator.py
execution_engine.py
shadow_engine.py
orchestrator.py
risk_manager.py
health_guardian.py
symbol_tags.json
requirements.txt
README.md
.env.example
signals_log.csv
regime_history.csv
flow_log.csv
paper_trades.csv
equity_curve.csv
model_output.json
walkforward_results.csv
mamuyy_hunter.db
execution_log.csv
orchestrator_log.csv
```

## Catatan Keamanan

- Tidak ada fitur auto trade.
- Tidak ada order buy/sell.
- Tidak ada withdrawal.
- Tidak meminta atau memakai Binance API key.
- Paper trading hanya simulasi berdasarkan harga market public.
- Telegram token dibaca dari `.env`, bukan di-hardcode.
