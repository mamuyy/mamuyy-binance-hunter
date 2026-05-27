# Alert Taxonomy Template

Gunakan template ini untuk warning governance-safe dan audit trail operasional.

| alert_code | severity | trigger_condition | operator_action | governance_impact | escalation_rule |
|---|---|---|---|---|---|
| DB_LOCK | LOW/MEDIUM/HIGH | SQLite lock berulang atau recovery gagal | audit concurrent writer, cek WAL/busy_timeout | promotion_hold_until_stable bila berulang | escalate jika >=20/hari atau auto_recovery=false |
| HEARTBEAT_STALE | MEDIUM/HIGH | heartbeat age > stale threshold | cek tmux hunter + jalankan health guardian | monitoring_degraded | escalate jika stale >3x threshold |
| BROADCAST_REJECTION | LOW/MEDIUM | reject/skip ratio tinggi | review allocation tier dan reject reason | routing_quality_watch | escalate jika reject > accept selama 2 report |
| REGIME_STRESS | MEDIUM/HIGH | macro/cross stress regime aktif | tetap konservatif PAPER_ONLY | risk_posture_conservative | escalate jika PANIC atau HIGH_STRESS persisten |
| TELEMETRY_DEGRADED | LOW/MEDIUM | telegram/log pipeline gagal/degraded | verifikasi notifier + persistence path | observability_reduced | escalate jika >5 kejadian/hari |
| GUARDIAN_RECOVERY | MEDIUM/HIGH | guardian HALT/recovery loop | cek dependency guardian dan restart session | runtime_safety_watch | escalate jika HALT tanpa recovery |
| DATA_FRESHNESS | MEDIUM | feed/data staleness terdeteksi | cek freshness upstream + fallback source | decision_confidence_lowered | escalate jika stale lintas multi-cycle |
| RESOURCE_PRESSURE | MEDIUM/HIGH | drawdown/resource pressure melewati batas | kurangi agresivitas & review capacity | promotion_blocker_if_persistent | escalate jika threshold breach persisten |
