# Telemetry Audit: Shadow/Portfolio Metric Normalization

Date: 2026-05-20 (UTC)
Scope: `shadow_engine.py`, `dashboard.py`, `portfolio_observer.py`

## Findings Summary

1. **`live_exposure` is currently cumulative when named as live** in `shadow_engine.py` (`exposure.sum() * 100`). This grows monotonically with historical row count and can exceed 100% indefinitely.
2. **`live_pnl` is cumulative history while labeled live** (`equity.iloc[-1]` from full-table `cumsum()`), so it is sensitive to retention horizon and not reflective of current live risk state.
3. **Shadow equity and drawdown curves are whole-history cumulative** (full `pnl.cumsum()` and `equity - equity.cummax()`). This is valid for historical analytics, but should be explicitly separated from rolling/live gauges.
4. **Portfolio exposure fallback can switch semantic basis** from active shadow exposure -> confidence proxy -> historical activity count, causing unstable observability semantics across runs.
5. **Regime/market/symbol exposure aggregation math itself is internally consistent** after normalization, but source-dependent normalization creates cross-source comparability distortion.

## Suspected Problematic Formulas

### 1) Shadow `live_exposure`
Current formula:
- `live_exposure = exposure.sum() * 100`

Why distorted:
- Sums all historical rows in `shadow_trades`, not current active positions.
- Converts a per-trade fraction into an unbounded cumulative number.

Recommended replacement:
- Use **active latest-per-symbol exposure snapshot**:
  - `latest_active = latest row per symbol where lifecycle not closed`
  - `live_exposure = latest_active.exposure.sum() * 100`
- If lifecycle status quality is low, fallback to `latest per symbol` without active filter.

### 2) Shadow `live_pnl`
Current formula:
- `equity = pnl_percent.cumsum()` over entire shadow history.
- `live_pnl = equity[-1]`.

Why distorted:
- A cumulative backfilled metric labeled as "live" conflates current session vs all-time.
- Any table growth, replays, or retention changes alter the value without true current risk change.

Recommended replacement:
- Keep current cumulative output as `cumulative_pnl` (or preserve key for backward compatibility but add a clearly live companion).
- Add `live_pnl_rolling` based on trailing N events or trailing T time window (e.g., 50 trades / 24h):
  - `rolling_pnl = pnl.tail(N).sum()`.

### 3) Shadow equity/drawdown curve basis
Current formula:
- `equity_curve = cumulative pnl`.
- `drawdown_curve = equity - cummax(equity)`.

Why potentially distorted:
- Not wrong mathematically, but **operational telemetry** can appear stale when old peaks dominate drawdown.
- Can under-react to regime shift when operators expect rolling risk curvature.

Recommended replacement:
- Keep existing cumulative curves for historical analysis.
- Add optional rolling overlays:
  - `rolling_equity_N = pnl.tail(N).cumsum()` (rebased at 0)
  - `rolling_drawdown_N = rolling_equity_N - rolling_equity_N.cummax()`

### 4) Portfolio exposure source fallback semantics
Current behavior:
- Prefer `shadow_trades.exposure` (latest by symbol, filtered by lifecycle status), else
- use `signals` score/confidence proxy, else
- use historical outcomes activity count.

Why distorted:
- Exposure means different things across sources (position size vs score proxy vs activity count).
- Heat score and concentration thresholds become source-dependent, reducing institutional comparability.

Recommended replacement:
- Preserve fallback architecture but emit source-tagged metric families:
  - `exposure_semantic = {notional|confidence_proxy|activity_proxy}`
- Apply confidence penalties or downgraded severity for proxy sources.
- Keep concentration formulas, but gate strict alerts when source != `shadow_trades`.

## Risks/Tradeoffs

1. **Backward compatibility risk**
   - Downstream consumers may rely on current `live_pnl/live_exposure` definitions.
2. **Alert calibration drift**
   - Switching to rolling/live variants will shift absolute thresholds.
3. **Interpretability complexity**
   - More metrics (cumulative + rolling) increase operator cognitive load unless clearly labeled.
4. **Lifecycle-data quality dependency**
   - Active-exposure snapshots depend on reliable closure statuses.

## Minimal Patch Proposal (architecture-preserving)

1. In `shadow_engine.run_shadow_live`:
   - Keep existing outputs unchanged for compatibility.
   - Add new keys:
     - `live_exposure_snapshot` (active latest-per-symbol sum)
     - `live_pnl_rolling_50`
     - `live_drawdown_rolling_50`
     - `cumulative_pnl` (alias of old behavior)
2. In dashboard labels:
   - Explicitly label old metrics as cumulative where applicable.
   - Prefer new rolling/snapshot keys for top-line operational cards once available.
3. In `portfolio_observer.observe_portfolio`:
   - Add `exposure_semantic` and `source_confidence` fields.
   - Keep existing concentration math unchanged.

## Which Metrics Should Be Cumulative vs Rolling

Keep **cumulative**:
- Shadow equity curve (all-time)
- Max drawdown (all-time risk history)
- Historical winrate/profit-factor analytics

Use **rolling/live** for operational telemetry:
- `live_pnl` (or new live key)
- `live_exposure`
- current drawdown watch metric (for alerting path)
- portfolio concentration/heat in active-book monitoring panels

## Cosmetic vs Systemic

- **Systemic**:
  - `live_exposure` cumulative summation issue.
  - source-semantic drift in portfolio exposure fallback.
- **Mostly cosmetic/labeling but operationally impactful**:
  - `live_pnl` naming mismatch if kept cumulative.
  - cumulative-only drawdown/equity shown without rolling companion.

Overall assessment: anomalies are **not purely cosmetic**. At least exposure and source semantics are systemic telemetry consistency issues that can affect risk decisions.
