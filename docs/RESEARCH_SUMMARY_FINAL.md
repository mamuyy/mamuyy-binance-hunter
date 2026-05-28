# Phase 2 Research Summary (Final)

## 1) Phase 2C Calibration Diagnosis

Phase 2C established that the Hunter score is non-probabilistic and should not be interpreted as a calibrated probability output.

Because of this, Brier-style calibration is not the correct primary optimization target for MAMUYY Hunter.

## 2) Regime-Aware Filtering

Research findings show that the **RISK OFF** regime underperforms compared to favorable regimes.

The weak performance zones are concentrated in:
- Low score bands.
- Short holding-duration trades.

Applying regime-aware filtering improves winrate and average PnL, but this comes with a reduction in trade count.

## 3) Robustness Finding

A static filter configuration does not remain stable across early/middle/late dataset splits.

Observed degradation in the late split indicates meaningful market drift and reduced parameter robustness over time.

## 4) Drift Detection

Phase 2 analysis detected a collapse timestamp aligned with a structural performance break.

After the collapse point, holding duration compresses, supporting evidence that market behavior and trade dynamics changed.

This supports the conclusion that market structure shifted materially.

## 5) Emergency Brake

Emergency Brake behavior shows improvement in:
- Winrate.
- Average PnL.
- Max drawdown proxy.

Given these outcomes, the Emergency Brake should be treated as a mandatory safety-layer candidate.

## 6) Transition Prediction

The Early Warning Score is currently useful as a monitoring and risk-dashboard signal.

However, it is not yet proven to be predictively reliable before collapse events.

Operationally, it should be used only to trigger manual defensive-mode review, not autonomous strategy transitions.

## 7) Final Governance Conclusion

- **PAPER_ONLY** remains enforced.
- No live execution.
- No broker/order/strategy mutation.
- No auto-promotion.
- Phase 2 research should close as an evidence package.
- Next work should focus on documentation, dashboard visibility, and defensive risk governance, not further threshold optimization.

The system should stop optimizing thresholds blindly. Research focus shifts from alpha calibration to defensive risk management based on regime health, emergency brake behavior, and early warning monitoring.
