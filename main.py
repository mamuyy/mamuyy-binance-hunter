import argparse
import csv
import os
import sqlite3
import sys
import time
from typing import List, Dict, Any

CLI_SUBCOMMAND_FLAGS = {
    "health": "--health",
    "risk-check": "--risk-check",
    "health-guardian-once": "--health-guardian-once",
    "heartbeat-test": "--heartbeat-test",
    "shadow-analysis": "--shadow-analysis",
    "label-outcomes": "--label-outcomes",
    "backfill": "--backfill",
    "optimize-filters": "--optimize-filters",
    "fix-regime-labels": "--fix-regime-labels",
    "shadow-lifecycle-audit": "--shadow-lifecycle-audit",
    "phase3-readiness": "--phase3-readiness",
    "refresh-governance-reports": "--refresh-governance-reports",
    "phase3-remediation": "--phase3-remediation",
    "paper-trade-diagnostics": "--paper-trade-diagnostics",
    "paper-portfolio": "--paper-portfolio",
}

if len(sys.argv) > 1 and sys.argv[1] in CLI_SUBCOMMAND_FLAGS:
    sys.argv[1] = CLI_SUBCOMMAND_FLAGS[sys.argv[1]]

if "--health" in sys.argv:
    from config import config as _health_config
    from database import db_health_check as _db_health_check
    from health_guardian import resolve_runtime_heartbeat as _resolve_runtime_heartbeat

    _health = _db_health_check(
        database_url=_health_config.database_url or _health_config.database_path,
        migrate_csv=False,
        backup=False,
    )
    _counts: Dict[str, int] = {}
    try:
        with sqlite3.connect(_health_config.database_path) as _connection:
            for _table in ["signals", "paper_trades", "flow_logs", "regime_logs", "ml_results", "walkforward_results", "shadow_trades", "internal_paper_trades", "broadcast_events", "telegram_events", "historical_klines", "historical_funding", "historical_open_interest", "historical_outcomes", "runtime_heartbeats"]:
                try:
                    _counts[_table] = _connection.execute(f"SELECT COUNT(*) FROM {_table}").fetchone()[0]
                except sqlite3.Error:
                    _counts[_table] = 0
    except sqlite3.Error:
        pass
    _heartbeat = _resolve_runtime_heartbeat(
        _health_config.database_path,
        "orchestrator_log.csv",
        _health_config.health_guardian_stale_minutes,
    )
    _latest_heartbeat = _heartbeat.get("timestamp") or "-"
    _heartbeat_source = _heartbeat.get("source") or "-"
    _latest_uptime = "0"
    if _heartbeat.get("message"):
        for _part in str(_heartbeat.get("message", "")).split(";"):
            if _part.startswith("uptime="):
                _latest_uptime = _part.replace("uptime=", "").replace("s", "")
    elif os.path.exists("orchestrator_log.csv"):
        try:
            with open("orchestrator_log.csv", newline="", encoding="utf-8") as _log_file:
                _rows = [row for row in csv.DictReader(_log_file) if row.get("engine") == "heartbeat"]
                if _rows:
                    _message = _rows[-1].get("message", "")
                    for _part in _message.split(";"):
                        if _part.startswith("uptime="):
                            _latest_uptime = _part.replace("uptime=", "").replace("s", "")
        except OSError:
            pass
    print("RUNTIME HEALTH")
    print(f"OK: {bool(_health.get('ok'))}")
    print(f"Database: {_health_config.database_path}")
    print(f"Latest Heartbeat: {_latest_heartbeat}")
    print(f"Heartbeat Source: {_heartbeat_source}")
    print(f"Latest Uptime Seconds: {_latest_uptime}")
    print(f"Table Counts: {_counts}")
    sys.exit(0)

if "--risk-check" in sys.argv:
    from config import config as _risk_config
    from risk_manager import RiskConfig as _RiskConfig
    from risk_manager import check_execution_safety as _check_execution_safety

    _risk_result = _check_execution_safety(
        db_path=_risk_config.database_path,
        orchestrator_log_path="orchestrator_log.csv",
        model_output_path="model_output.json",
        config=_RiskConfig(
            ml_accuracy_halt=_risk_config.risk_ml_accuracy_halt,
            drawdown_halt=_risk_config.risk_drawdown_halt,
            drawdown_watch=_risk_config.risk_drawdown_watch,
            stale_minutes=_risk_config.risk_stale_minutes,
            max_open_trades=_risk_config.risk_max_open_trades,
            loss_cooldown=_risk_config.risk_loss_cooldown,
            base_position_multiplier=_risk_config.risk_base_position_multiplier,
            high_vol_confidence_min=_risk_config.risk_high_vol_confidence_min,
        ),
        log_event=True,
    )
    print("RISK CHECK")
    print(f"Safe: {_risk_result['safe']}")
    print(f"Status: {_risk_result['status']}")
    print(f"Risk Score: {_risk_result['risk_score']}")
    print(f"Position Multiplier: {_risk_result['position_multiplier']}")
    print(f"Reasons: {_risk_result['reasons'] or ['none']}")
    print(f"Metrics: {_risk_result['metrics']}")
    sys.exit(0)

if "--health-guardian-once" in sys.argv:
    from config import config as _guardian_config
    from health_guardian import HealthGuardianConfig as _HealthGuardianConfig
    from health_guardian import check_health_guardian_once as _check_health_guardian_once
    from health_guardian import format_health_guardian_result as _format_health_guardian_result

    _guardian_result = _check_health_guardian_once(
        _HealthGuardianConfig(
            database_path=_guardian_config.database_path,
            orchestrator_log_path="orchestrator_log.csv",
            project_dir=_guardian_config.health_guardian_project_dir,
            hunter_session=_guardian_config.health_guardian_hunter_session,
            dashboard_session=_guardian_config.health_guardian_dashboard_session,
            stale_minutes=_guardian_config.health_guardian_stale_minutes,
            interval_seconds=_guardian_config.health_guardian_interval_seconds,
            dry_run=_guardian_config.health_guardian_dry_run,
            restart_dashboard=_guardian_config.health_guardian_restart_dashboard,
            restart_cooldown_seconds=_guardian_config.health_guardian_restart_cooldown_seconds,
        )
    )
    print(_format_health_guardian_result(_guardian_result))
    sys.exit(0)

if "--heartbeat-test" in sys.argv:
    from datetime import datetime as _datetime
    from datetime import timezone as _timezone
    from config import config as _heartbeat_config
    from database import insert_runtime_heartbeat as _insert_runtime_heartbeat
    from health_guardian import resolve_runtime_heartbeat as _resolve_runtime_heartbeat

    _timestamp = _datetime.now(_timezone.utc).isoformat()
    _insert_runtime_heartbeat(
        {
            "timestamp": _timestamp,
            "source": "heartbeat_test",
            "state": "IDLE",
            "system_health_score": 100,
            "scheduler": "TEST",
            "uptime_seconds": 0,
            "message": "heartbeat_test;uptime=0s",
        },
        _heartbeat_config.database_path,
    )
    _heartbeat = _resolve_runtime_heartbeat(
        _heartbeat_config.database_path,
        "orchestrator_log.csv",
        _heartbeat_config.health_guardian_stale_minutes,
    )
    print("HEARTBEAT TEST")
    print(f"Written Timestamp: {_timestamp}")
    print(f"Read Timestamp: {_heartbeat.get('timestamp') or '-'}")
    print(f"Heartbeat Source: {_heartbeat.get('source') or '-'}")
    print(f"Heartbeat Age Minutes: {_heartbeat.get('age_minutes')}")
    print(f"OK: {_heartbeat.get('source') == 'heartbeat_table' and _heartbeat.get('timestamp') == _timestamp}")
    sys.exit(0)

if "--shadow-analysis" in sys.argv:
    from config import config as _shadow_analysis_config
    from shadow_analysis import format_shadow_analysis_summary as _format_shadow_analysis_summary
    from shadow_analysis import run_shadow_equity_analysis as _run_shadow_equity_analysis

    _shadow_result = _run_shadow_equity_analysis(
        database_path=_shadow_analysis_config.database_path,
        threshold=_shadow_analysis_config.alert_score_threshold,
        equity_output_path="shadow_equity_curve.csv",
        comparison_output_path="shadow_comparison.csv",
        tuning_output_path="logs/shadow_threshold_tuning.csv",
        walkforward_output_path="logs/shadow_threshold_walkforward.csv",
        adaptive_comparison_output_path="logs/adaptive_threshold_comparison.csv",
        adaptive_walkforward_output_path="logs/adaptive_walkforward.csv",
        macro_stress_output_path="logs/macro_stress_summary.csv",
    )
    print(_format_shadow_analysis_summary(_shadow_result))
    sys.exit(0)

if "--shadow-lifecycle-audit" in sys.argv:
    from config import config as _cfg
    from shadow_lifecycle import shadow_lifecycle_audit as _shadow_lifecycle_audit

    _report = _shadow_lifecycle_audit(db_path=_cfg.database_path)
    print("SHADOW LIFECYCLE AUDIT")
    print(f"Active Count: {_report.get('active_count', 0)}")
    print(f"Stale Count: {_report.get('stale_count', 0)}")
    print(f"Expired Count: {_report.get('expired_count', 0)}")
    print(f"Profit Matured Count: {_report.get('profit_matured_count', 0)}")
    print(f"Profit Matured Symbols: {_report.get('profit_matured_symbols', []) or ['none']}")
    print(f"Oldest Age Minutes: {float(_report.get('oldest_shadow_age_minutes', 0.0)):.2f}")
    print(f"Stuck Symbols: {_report.get('stuck_symbols', []) or ['none']}")
    print(f"Total Rows: {_report.get('total_rows', 0)}")
    sys.exit(0)

if "--label-outcomes" in sys.argv:
    from config import config as _label_config
    from outcome_labeler import label_historical_outcomes as _label_historical_outcomes

    _days = 7
    if "--days" in sys.argv:
        try:
            _days = int(sys.argv[sys.argv.index("--days") + 1])
        except (IndexError, ValueError):
            print("Invalid --days value. Use an integer, for example: python main.py --label-outcomes --days 7")
            sys.exit(2)
    _label_historical_outcomes(
        database_url=_label_config.database_url or _label_config.database_path,
        days=_days,
    )
    sys.exit(0)

if "--backfill" in sys.argv:
    from backfill import run_historical_backfill as _run_historical_backfill
    from config import config as _backfill_config

    _days = 7
    if "--days" in sys.argv:
        try:
            _days = int(sys.argv[sys.argv.index("--days") + 1])
        except (IndexError, ValueError):
            print("Invalid --days value. Use an integer, for example: python main.py --backfill --days 7")
            sys.exit(2)
    _run_historical_backfill(
        days=_days,
        database_url=_backfill_config.database_url or _backfill_config.database_path,
        base_url=_backfill_config.binance_base_url,
        interval=_backfill_config.candle_interval,
        top_symbols_limit=_backfill_config.top_symbols_limit,
        min_quote_volume=_backfill_config.min_quote_volume,
        timeout=_backfill_config.request_timeout_seconds,
    )
    sys.exit(0)

if "--optimize-filters" in sys.argv:
    from config import config as _optimizer_config
    from filter_optimizer import run_filter_optimizer as _run_filter_optimizer

    _run_filter_optimizer(database_path=_optimizer_config.database_path)
    sys.exit(0)

if "--fix-regime-labels" in sys.argv:
    from config import config as _regime_fix_config
    from regime_labeler import fix_historical_regime_labels as _fix_historical_regime_labels

    _fix_historical_regime_labels(database_path=_regime_fix_config.database_path)
    sys.exit(0)

from config import config
from anomaly_detector import format_anomaly_scan, run_anomaly_scan
from outcome_labeler import label_historical_outcomes
from regime_labeler import fix_historical_regime_labels
from broadcast_router import broadcast_test, format_broadcast_result
from bridge_tradingview import format_webhook_test, webhook_test_payload
from competition_control import competition_status, format_competition_status
from cross_market_intelligence import format_cross_market_report, run_cross_market_intelligence
from daily_ops_report import format_daily_ops_report, generate_daily_ops_report
from database import (
    backup_database,
    db_health_check,
    insert_flow_log,
    insert_ml_result,
    insert_paper_trade,
    insert_regime_log,
    insert_runtime_heartbeat,
    insert_signal,
    insert_walkforward_rows,
)
from flow_engine import AdvancedFlowEngine, apply_flow_to_signal, log_flow
from health_guardian import HealthGuardianConfig, check_health_guardian_once, format_health_guardian_result, resolve_runtime_heartbeat
from internal_paper_engine import (
    format_paper_diagnostics,
    format_paper_engine_result,
    generate_paper_trade_diagnostics,
    run_internal_paper_engine,
)
from logger import log_signal
from paper_portfolio import format_paper_portfolio_report, generate_paper_portfolio_report
from market_regime import (
    MarketRegimeEngine,
    apply_regime_to_signal,
    log_regime_history,
)
from macro_observer import format_macro_observer, observe_macro
from ml_engine import run_ml_research
from opportunity_allocator import allocate_opportunities, format_allocation_summary
from orchestrator import format_orchestrator_diagnostics, load_orchestrator_diagnostics, run_orchestrator, runtime_keepalive, uptime_seconds
from portfolio_engine import build_portfolio
from portfolio_observer import format_portfolio_observer, observe_portfolio
from portfolio_risk_budget import calculate_portfolio_risk_budget, format_portfolio_risk_budget
from backup_verification import generate_backup_verification, format_backup_verification
from label_quality_audit import generate_label_quality_audit, format_label_quality_audit
from stress_test_simulator import generate_stress_test_report, format_stress_test_report
from phase3_readiness import calculate_phase3_readiness, format_phase3_readiness
from governance_audit import format_governance_audit, run_governance_audit
from governance_report_refresh import format_refresh_diagnostics, refresh_governance_reports
from promotion_scorecard import format_promotion_scorecard, generate_promotion_scorecard
from regime_models import analyze_regime_models, apply_regime_model_to_signal
from regime_shadow import apply_adaptive_regime_shadow_penalty
from report_generator import generate_performance_report
from retrain_model import format_model_status, format_retrain_summary, model_status, retrain_model
from risk_manager import RiskConfig, check_execution_safety
from scanner import BinanceFuturesScanner
from shadow_analysis import format_shadow_analysis_summary, run_shadow_equity_analysis
from shadow_engine import run_shadow_live
from strategy_genome import format_strategy_genome_result, run_strategy_genome, strategy_ranking
from telegram_notifier import format_notification_result, notify_summary, telegram_test
from telegram import (
    format_execution_message,
    format_governance_intelligence_message,
    format_governance_audit_message,
    format_market_regime_message,
    format_ml_analysis_message,
    format_orchestrator_message,
    format_paper_portfolio_message,
    format_paper_summary_message,
    format_performance_report_message,
    format_phase3_readiness_message,
    format_portfolio_message,
    format_promotion_scorecard_message,
    format_regime_model_message,
    format_signal_message,
    format_shadow_message,
    format_walkforward_report_message,
    send_telegram_message,
)
from tracker import (
    build_paper_summary,
    ensure_paper_trades_file,
    mark_daily_summary_sent,
    open_paper_trades,
    should_send_daily_summary,
    update_paper_trades,
)
from walkforward import run_walkforward_validation


def database_url() -> str:
    return config.database_url or config.database_path


def send_message_if_enabled(message: str) -> None:
    if config.telegram_enabled:
        send_telegram_message(
            bot_token=config.telegram_bot_token,
            chat_id=config.telegram_chat_id,
            message=message,
            timeout=config.request_timeout_seconds,
        )
    else:
        print("Telegram belum dikonfigurasi. Pesan hanya dicetak di terminal.")


def send_daily_paper_summary() -> None:
    if not should_send_daily_summary(config.paper_summary_state_path):
        return

    summary = build_paper_summary(config.paper_trades_path)
    message = format_paper_summary_message(summary)
    print(message)
    send_message_if_enabled(message)
    mark_daily_summary_sent(config.paper_summary_state_path)


def run_report() -> Dict[str, Any]:
    metrics = generate_performance_report(
        paper_trades_path=config.paper_trades_path,
        equity_curve_path=config.equity_curve_path,
        output_path=config.performance_report_path,
        chart_dir=config.chart_output_dir,
        database_path=config.database_path,
    )
    message = format_performance_report_message(metrics)
    print(message)
    print(f"Report generated: {config.performance_report_path}")
    print(f"Equity curve generated: {config.equity_curve_path}")
    send_message_if_enabled(message)
    return metrics


def run_ml() -> Dict[str, Any]:
    result = run_ml_research(
        paper_trades_path=config.paper_trades_path,
        signals_log_path=config.signals_log_path,
        flow_log_path=config.flow_log_path,
        output_path=config.model_output_path,
        chart_dir=config.chart_output_dir,
        database_path=config.database_path,
    )
    message = format_ml_analysis_message(result)
    print(message)
    print(f"Model output generated: {config.model_output_path}")
    insert_ml_result(result, database_url=database_url())
    send_message_if_enabled(message)
    return result


def run_retrain_model() -> Dict[str, Any]:
    result = retrain_model(
        database_path=config.database_path,
        paper_trades_path=config.paper_trades_path,
        signals_log_path=config.signals_log_path,
        flow_log_path=config.flow_log_path,
        registry_path="model_registry.json",
        production_model_path="model_weights.pkl",
        candidate_model_path="model_weights_candidate.pkl",
        previous_model_path="model_weights_previous.pkl",
        walkforward_output_path="logs/retrain_walkforward.csv",
        chart_dir=config.chart_output_dir,
    )
    print(format_retrain_summary(result))
    return result


def run_model_status() -> Dict[str, Any]:
    result = model_status("model_registry.json")
    print(format_model_status(result))
    return result


def run_paper_engine() -> Dict[str, Any]:
    result = run_internal_paper_engine(
        db_path=config.database_path,
        allocation_path="logs/opportunity_allocation.csv",
    )
    print(format_paper_engine_result(result))
    return result


def run_paper_trade_diagnostics() -> Dict[str, Any]:
    result = generate_paper_trade_diagnostics(
        db_path=config.database_path,
        output_path="reports/paper_trade_lifecycle.json",
        write_report=True,
    )
    print(format_paper_diagnostics(result))
    return result


def run_paper_portfolio() -> Dict[str, Any]:
    result = generate_paper_portfolio_report(
        db_path=config.database_path,
        output_path="reports/paper_portfolio.json",
        write_report=True,
    )
    print(format_paper_portfolio_report(result))
    return result


def run_webhook_test() -> Dict[str, Any]:
    result = webhook_test_payload("logs/webhook_test_payload.json")
    print(format_webhook_test(result))
    return result


def run_macro_observer() -> Dict[str, Any]:
    result = observe_macro(
        db_path=config.database_path,
        output_path="logs/macro_observer.csv",
    )
    print(format_macro_observer(result))
    return result


def run_cross_market() -> Dict[str, Any]:
    result = run_cross_market_intelligence(
        db_path=config.database_path,
        output_path="logs/cross_market_intelligence.csv",
    )
    print(format_cross_market_report(result))
    return result


def run_strategy_genome_command() -> Dict[str, Any]:
    with runtime_keepalive(
        "strategy_genome",
        db_path=config.database_path,
        interval_seconds=config.orchestrator_keepalive_interval_seconds,
        threshold_seconds=config.orchestrator_keepalive_threshold_seconds,
    ):
        result = run_strategy_genome(db_path=config.database_path)
    print(format_strategy_genome_result(result))
    return result


def run_strategy_ranking() -> Dict[str, Any]:
    result = strategy_ranking()
    print(format_strategy_genome_result(result))
    return result


def run_daily_ops_report() -> Dict[str, Any]:
    result = generate_daily_ops_report(db_path=config.database_path)
    print(format_daily_ops_report(result))
    send_message_if_enabled(format_governance_intelligence_message())
    return result


def run_anomaly_scan_command() -> Dict[str, Any]:
    with runtime_keepalive(
        "anomaly_scan",
        db_path=config.database_path,
        interval_seconds=config.orchestrator_keepalive_interval_seconds,
        threshold_seconds=config.orchestrator_keepalive_threshold_seconds,
    ):
        result = run_anomaly_scan(db_path=config.database_path, notify_critical=False)
    print(format_anomaly_scan(result))
    return result


def run_incident_report_command() -> Dict[str, Any]:
    with runtime_keepalive(
        "incident_report",
        db_path=config.database_path,
        interval_seconds=config.orchestrator_keepalive_interval_seconds,
        threshold_seconds=config.orchestrator_keepalive_threshold_seconds,
    ):
        result = run_anomaly_scan(db_path=config.database_path, notify_critical=True)
    print(format_anomaly_scan(result))
    return result


def run_broadcast_test() -> Dict[str, Any]:
    result = broadcast_test(
        db_path=config.database_path,
        allocation_path="logs/opportunity_allocation.csv",
    )
    print(format_broadcast_result(result))
    return result


def run_competition_status() -> Dict[str, Any]:
    result = competition_status()
    print(format_competition_status(result))
    return result


def run_telegram_test() -> Dict[str, Any]:
    result = telegram_test(config.database_path)
    print(format_notification_result(result))
    return result


def run_notify_summary() -> Dict[str, Any]:
    result = notify_summary(config.database_path)
    print(format_notification_result(result))
    return result


def run_walkforward() -> Dict[str, Any]:
    with runtime_keepalive(
        "walkforward",
        db_path=config.database_path,
        interval_seconds=config.orchestrator_keepalive_interval_seconds,
        threshold_seconds=config.orchestrator_keepalive_threshold_seconds,
    ):
        result = run_walkforward_validation(
            paper_trades_path=config.paper_trades_path,
            signals_log_path=config.signals_log_path,
            output_path=config.walkforward_results_path,
            chart_dir=config.chart_output_dir,
            database_path=config.database_path,
        )
    message = format_walkforward_report_message(result)
    print(message)
    print(f"Walk-forward results generated: {config.walkforward_results_path}")
    if result.get("folds", 0) > 0:
        with open(config.walkforward_results_path, newline="", encoding="utf-8") as csv_file:
            insert_walkforward_rows(csv.DictReader(csv_file), database_url=database_url())
    send_message_if_enabled(message)
    return result


def run_regime_models() -> Dict[str, Any]:
    result = analyze_regime_models(
        paper_trades_path=config.paper_trades_path,
        signals_log_path=config.signals_log_path,
        flow_log_path=config.flow_log_path,
        chart_dir=config.chart_output_dir,
    )
    message = format_regime_model_message(result)
    print(message)
    print(f"Charts: {result.get('charts', {})}")
    send_message_if_enabled(message)
    return result


def run_portfolio() -> Dict[str, Any]:
    with runtime_keepalive(
        "portfolio",
        db_path=config.database_path,
        interval_seconds=config.orchestrator_keepalive_interval_seconds,
        threshold_seconds=config.orchestrator_keepalive_threshold_seconds,
    ):
        result = build_portfolio(
            db_path=config.database_path,
            tags_path="symbol_tags.json",
            chart_dir=config.chart_output_dir,
        )
    message = format_portfolio_message(result)
    print(message)
    print(f"Charts: {result.get('charts', {})}")
    send_message_if_enabled(message)
    return result




def run_backup_verification() -> Dict[str, Any]:
    result = generate_backup_verification(
        db_path=config.database_path,
        backup_dir=config.database_backup_dir,
        output_path="reports/backup_verification.json",
        write_report=True,
    )
    print(format_backup_verification(result))
    print("Report generated: reports/backup_verification.json")
    return result


def run_label_quality_audit() -> Dict[str, Any]:
    result = generate_label_quality_audit(
        db_path=config.database_path,
        output_path="reports/label_quality_audit.json",
        write_report=True,
    )
    print(format_label_quality_audit(result))
    print("Report generated: reports/label_quality_audit.json")
    return result


def run_stress_test_report() -> Dict[str, Any]:
    result = generate_stress_test_report(
        output_path="reports/stress_test_report.json",
        markdown_path="docs/STRESS_TEST_REPORT.md",
        write_report=True,
        write_markdown=True,
    )
    print(format_stress_test_report(result))
    print("Report generated: reports/stress_test_report.json")
    print("Markdown generated: docs/STRESS_TEST_REPORT.md")
    return result


def run_phase3_remediation() -> Dict[str, Any]:
    print("PHASE 3 READINESS REMEDIATION PIPELINE")
    print("Safety: PAPER_ONLY, read-only analytics, no broker routing, no order placement, no auto unlock.")
    results: Dict[str, Any] = {}
    print("1/8 Backup Verification")
    results["backup_verification"] = run_backup_verification()
    print("2/8 Label Quality Audit")
    results["label_quality_audit"] = run_label_quality_audit()
    print("3/8 Stress Test Report")
    results["stress_test_report"] = run_stress_test_report()
    print("4/8 Refresh Governance Reports")
    results["governance_refresh"] = refresh_governance_reports()
    print(format_refresh_diagnostics(results["governance_refresh"]))
    print("5/8 Portfolio Risk Budget")
    results["portfolio_risk_budget"] = run_portfolio_risk_budget()
    print("6/8 Promotion Scorecard")
    results["promotion_scorecard"] = run_promotion_scorecard()
    print("7/8 Governance Audit")
    results["governance_audit"] = run_governance_audit_command()
    print("8/8 Phase 3 Readiness")
    results["phase3_readiness"] = run_phase3_readiness()
    return results


def run_portfolio_risk_budget() -> Dict[str, Any]:
    result = calculate_portfolio_risk_budget(
        db_path=config.database_path,
        output_path="reports/portfolio_risk_budget.json",
        write_report=True,
    )
    print(format_portfolio_risk_budget(result))
    return result


def run_promotion_scorecard() -> Dict[str, Any]:
    result = generate_promotion_scorecard(
        db_path=config.database_path,
        output_path="reports/promotion_scorecard.json",
        write_report=True,
    )
    print(format_promotion_scorecard(result))
    send_message_if_enabled(format_promotion_scorecard_message(result))
    return result


def run_refresh_governance_reports() -> Dict[str, Any]:
    result = refresh_governance_reports()
    print(format_refresh_diagnostics(result))

    print("\nFOLLOW-UP READINESS PIPELINE")
    print("1/4 Portfolio Risk Budget")
    run_portfolio_risk_budget()
    print("2/4 Promotion Scorecard")
    run_promotion_scorecard()
    print("3/4 Governance Audit")
    run_governance_audit_command()
    print("4/4 Phase 3 Readiness")
    run_phase3_readiness()
    return result


def run_governance_audit_command() -> Dict[str, Any]:
    result = run_governance_audit(
        output_path="reports/governance_audit.json",
        write_report=True,
    )
    print(format_governance_audit(result))
    send_message_if_enabled(format_governance_audit_message(result))
    return result


def run_phase3_readiness() -> Dict[str, Any]:
    result = calculate_phase3_readiness(
        db_path=config.database_path,
        paper_trades_path=config.paper_trades_path,
        backup_dir=config.database_backup_dir,
        output_path="reports/phase3_readiness.json",
        write_report=True,
        health_stale_minutes=config.health_guardian_stale_minutes,
    )
    print(format_phase3_readiness(result))
    print("Report generated: reports/phase3_readiness.json")
    print(format_phase3_readiness_message(result))
    return result


def send_phase3_readiness_monitoring_summary() -> None:
    message = format_phase3_readiness_message()
    print(message)
    send_message_if_enabled(message)


def send_paper_portfolio_monitoring_summary(sent_sections: set[str] | None = None) -> None:
    if sent_sections is not None and "paper_portfolio" in sent_sections:
        print("Paper portfolio summary already sent in this cycle; skipping duplicate.")
        return

    report = generate_paper_portfolio_report(
        db_path=config.database_path,
        output_path="reports/paper_portfolio.json",
        write_report=True,
    )
    message = format_paper_portfolio_message(report)
    print(message)
    send_message_if_enabled(message)
    if sent_sections is not None:
        sent_sections.add("paper_portfolio")


def run_portfolio_observer() -> Dict[str, Any]:
    with runtime_keepalive(
        "portfolio_observer",
        db_path=config.database_path,
        interval_seconds=config.orchestrator_keepalive_interval_seconds,
        threshold_seconds=config.orchestrator_keepalive_threshold_seconds,
    ):
        result = observe_portfolio(db_path=config.database_path)
    print(format_portfolio_observer(result))
    return result


def run_allocate() -> Dict[str, Any]:
    result = allocate_opportunities(
        db_path=config.database_path,
        output_path="logs/opportunity_allocation.csv",
        logs_dir="logs",
    )
    print(format_allocation_summary(result))
    return result


def run_execution() -> Dict[str, Any]:
    from execution_engine import run_execution_simulation

    result = run_execution_simulation(
        db_path=config.database_path,
        output_path="execution_log.csv",
        chart_dir=config.chart_output_dir,
    )
    message = format_execution_message(result)
    print(message)
    print(f"Charts: {result.get('charts', {})}")
    send_message_if_enabled(message)
    return result


def run_shadow() -> Dict[str, Any]:
    result = run_shadow_live(
        db_path=config.database_path,
        chart_dir=config.chart_output_dir,
    )
    message = format_shadow_message(result)
    print(message)
    print(f"Charts: {result.get('charts', {})}")
    send_message_if_enabled(message)
    return result


def run_orchestrator_command() -> Dict[str, Any]:
    callbacks = {
        "scanner": lambda: run_once(paper=False),
        "regime": lambda: run_once(paper=False),
        "flow": lambda: run_once(paper=False),
        "ML": run_ml,
        "walkforward": run_walkforward,
        "portfolio": run_portfolio,
        "execution": run_execution,
        "shadow": run_shadow,
        "paper_engine": run_paper_engine,
    }
    result = run_orchestrator(
        callbacks=callbacks,
        profile=config.orchestrator_profile,
        db_path=config.database_path,
        log_path="orchestrator_log.csv",
        cycles=1,
        retries=1,
        retention_days=config.log_retention_days,
        db_retention_days=config.db_retention_days,
        max_log_bytes=config.max_log_bytes,
        keepalive_interval_seconds=config.orchestrator_keepalive_interval_seconds,
        keepalive_threshold_seconds=config.orchestrator_keepalive_threshold_seconds,
    )
    sent_sections: set[str] = set()
    send_phase3_readiness_monitoring_summary()
    send_paper_portfolio_monitoring_summary(sent_sections)
    message = format_orchestrator_message(result)
    print(message)
    send_message_if_enabled(message)
    return result


def run_orchestrator_diagnostics() -> Dict[str, Any]:
    result = load_orchestrator_diagnostics()
    print(format_orchestrator_diagnostics(result))
    return result


def run_health() -> Dict[str, Any]:
    health = db_health_check(database_url=database_url(), migrate_csv=False, backup=False)
    table_counts: Dict[str, int] = {}
    try:
        with sqlite3.connect(config.database_path) as connection:
            for table in ["signals", "paper_trades", "flow_logs", "regime_logs", "ml_results", "walkforward_results", "shadow_trades", "internal_paper_trades", "broadcast_events", "telegram_events", "historical_klines", "historical_funding", "historical_open_interest", "historical_outcomes", "runtime_heartbeats"]:
                try:
                    table_counts[table] = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                except sqlite3.Error:
                    table_counts[table] = 0
    except sqlite3.Error:
        pass

    heartbeat = resolve_runtime_heartbeat(
        config.database_path,
        "orchestrator_log.csv",
        config.health_guardian_stale_minutes,
    )
    latest_heartbeat = heartbeat.get("timestamp") or "-"
    heartbeat_source = heartbeat.get("source") or "-"
    latest_uptime = "0"
    if heartbeat.get("message"):
        for part in str(heartbeat.get("message", "")).split(";"):
            if part.startswith("uptime="):
                latest_uptime = part.replace("uptime=", "").replace("s", "")
                break
    elif os.path.exists("orchestrator_log.csv"):
        try:
            with open("orchestrator_log.csv", newline="", encoding="utf-8") as log_file:
                rows = [row for row in csv.DictReader(log_file) if row.get("engine") == "heartbeat"]
                if rows:
                    latest_uptime = "0"
                    message = rows[-1].get("message", "")
                    for part in message.split(";"):
                        if part.startswith("uptime="):
                            latest_uptime = part.replace("uptime=", "").replace("s", "")
        except OSError:
            latest_heartbeat = "-"
            latest_uptime = "0"
    else:
        latest_uptime = "0"

    status = {
        "ok": bool(health.get("ok")),
        "database": config.database_path,
        "table_counts": table_counts,
        "latest_heartbeat": latest_heartbeat,
        "heartbeat_source": heartbeat_source,
        "uptime_seconds": latest_uptime,
    }
    print("RUNTIME HEALTH")
    print(f"OK: {status['ok']}")
    print(f"Database: {status['database']}")
    print(f"Latest Heartbeat: {status['latest_heartbeat']}")
    print(f"Heartbeat Source: {status['heartbeat_source']}")
    print(f"Uptime Seconds: {status['uptime_seconds']}")
    print(f"Table Counts: {status['table_counts']}")
    return status


def risk_config_from_env() -> RiskConfig:
    return RiskConfig(
        ml_accuracy_halt=config.risk_ml_accuracy_halt,
        drawdown_halt=config.risk_drawdown_halt,
        drawdown_watch=config.risk_drawdown_watch,
        stale_minutes=config.risk_stale_minutes,
        max_open_trades=config.risk_max_open_trades,
        loss_cooldown=config.risk_loss_cooldown,
        base_position_multiplier=config.risk_base_position_multiplier,
        high_vol_confidence_min=config.risk_high_vol_confidence_min,
    )


def run_risk_check() -> Dict[str, Any]:
    result = check_execution_safety(
        db_path=config.database_path,
        orchestrator_log_path="orchestrator_log.csv",
        model_output_path="model_output.json",
        config=risk_config_from_env(),
        log_event=True,
    )
    print("RISK CHECK")
    print(f"Safe: {result['safe']}")
    print(f"Status: {result['status']}")
    print(f"Risk Score: {result['risk_score']}")
    print(f"Position Multiplier: {result['position_multiplier']}")
    print(f"Reasons: {result['reasons'] or ['none']}")
    print(f"Metrics: {result['metrics']}")
    return result


def health_guardian_config_from_env() -> HealthGuardianConfig:
    return HealthGuardianConfig(
        database_path=config.database_path,
        orchestrator_log_path="orchestrator_log.csv",
        project_dir=config.health_guardian_project_dir,
        hunter_session=config.health_guardian_hunter_session,
        dashboard_session=config.health_guardian_dashboard_session,
        stale_minutes=config.health_guardian_stale_minutes,
        interval_seconds=config.health_guardian_interval_seconds,
        dry_run=config.health_guardian_dry_run,
        restart_dashboard=config.health_guardian_restart_dashboard,
        restart_cooldown_seconds=config.health_guardian_restart_cooldown_seconds,
    )


def run_health_guardian_once() -> Dict[str, Any]:
    result = check_health_guardian_once(health_guardian_config_from_env())
    print(format_health_guardian_result(result))
    return result


def run_heartbeat_test() -> Dict[str, Any]:
    from datetime import datetime, timezone

    timestamp = datetime.now(timezone.utc).isoformat()
    insert_runtime_heartbeat(
        {
            "timestamp": timestamp,
            "source": "heartbeat_test",
            "state": "IDLE",
            "system_health_score": 100,
            "scheduler": "TEST",
            "uptime_seconds": 0,
            "message": "heartbeat_test;uptime=0s",
        },
        config.database_path,
    )
    heartbeat = resolve_runtime_heartbeat(
        config.database_path,
        "orchestrator_log.csv",
        config.health_guardian_stale_minutes,
    )
    result = {
        "written_timestamp": timestamp,
        "read_timestamp": heartbeat.get("timestamp") or "-",
        "heartbeat_source": heartbeat.get("source") or "-",
        "heartbeat_age_minutes": heartbeat.get("age_minutes"),
        "ok": heartbeat.get("source") == "heartbeat_table" and heartbeat.get("timestamp") == timestamp,
    }
    print("HEARTBEAT TEST")
    print(f"Written Timestamp: {result['written_timestamp']}")
    print(f"Read Timestamp: {result['read_timestamp']}")
    print(f"Heartbeat Source: {result['heartbeat_source']}")
    print(f"Heartbeat Age Minutes: {result['heartbeat_age_minutes']}")
    print(f"OK: {result['ok']}")
    return result


def run_shadow_analysis() -> Dict[str, Any]:
    result = run_shadow_equity_analysis(
        database_path=config.database_path,
        threshold=config.alert_score_threshold,
        equity_output_path="shadow_equity_curve.csv",
        comparison_output_path="shadow_comparison.csv",
        tuning_output_path="logs/shadow_threshold_tuning.csv",
        walkforward_output_path="logs/shadow_threshold_walkforward.csv",
        adaptive_comparison_output_path="logs/adaptive_threshold_comparison.csv",
        adaptive_walkforward_output_path="logs/adaptive_walkforward.csv",
        macro_stress_output_path="logs/macro_stress_summary.csv",
    )
    print(format_shadow_analysis_summary(result))
    return result


def run_backfill(days: int) -> Dict[str, Any]:
    from backfill import run_historical_backfill

    return run_historical_backfill(
        days=days,
        database_url=database_url(),
        base_url=config.binance_base_url,
        interval=config.candle_interval,
        top_symbols_limit=config.top_symbols_limit,
        min_quote_volume=config.min_quote_volume,
        timeout=config.request_timeout_seconds,
    )


def run_label_outcomes(days: int) -> Dict[str, Any]:
    return label_historical_outcomes(
        database_url=database_url(),
        days=days,
    )


def run_optimize_filters() -> Dict[str, Any]:
    from filter_optimizer import run_filter_optimizer

    return run_filter_optimizer(database_path=config.database_path)


def run_fix_regime_labels() -> Dict[str, Any]:
    return fix_historical_regime_labels(database_path=config.database_path)


def run_db_check() -> Dict[str, Any]:
    health = db_health_check(database_url=database_url(), migrate_csv=True, backup=False)
    backup_path = ""
    if health.get("ok"):
        backup_path = backup_database(config.database_path, config.database_backup_dir)
        health["backup_path"] = backup_path

    print("DATABASE HEALTH CHECK")
    print(f"OK: {health.get('ok')}")
    print(f"Database: {health.get('database')}")
    print(f"Tables: {health.get('tables')}")
    print(f"Migrated: {health.get('migrated')}")
    print(f"Backup: {health.get('backup_path') or '-'}")
    if health.get("errors"):
        print(f"Errors: {health.get('errors')}")
    return health


def run_once(paper: bool = False) -> List[Dict[str, Any]]:
    scanner = BinanceFuturesScanner(
        base_url=config.binance_base_url,
        timeout=config.request_timeout_seconds,
    )

    if paper:
        ensure_paper_trades_file(config.paper_trades_path)
        try:
            prices = scanner.get_usdt_prices()
            update_paper_trades(prices, path=config.paper_trades_path)
        except Exception as exc:
            print(f"Gagal update paper trades: {exc}")

    print("Memulai scan Binance USDT Futures...")
    try:
        regime = MarketRegimeEngine(scanner).detect()
        log_regime_history(regime, path=config.regime_history_path)
        insert_regime_log(regime, database_url=database_url())
        regime_message = format_market_regime_message(regime)
        print(regime_message)
        send_message_if_enabled(regime_message)
    except Exception as exc:
        print(f"Gagal mendeteksi market regime: {exc}")
        regime = {"regime_name": "UNKNOWN", "regime_score": 0}

    signals = scanner.scan_market(
        top_symbols_limit=config.top_symbols_limit,
        min_quote_volume=config.min_quote_volume,
        interval=config.candle_interval,
        candle_limit=config.candle_limit,
    )
    signals = [apply_regime_to_signal(signal, regime) for signal in signals]
    flow_engine = AdvancedFlowEngine(scanner)
    flow_adjusted_signals = []

    for signal in signals:
        try:
            candles = scanner.get_klines(
                signal["symbol"],
                interval=config.candle_interval,
                limit=max(config.candle_limit, 80),
            )
            flow = flow_engine.analyze_symbol(
                symbol=signal["symbol"],
                candles=candles,
                funding_rate=signal.get("funding"),
                open_interest=signal.get("open_interest"),
            )
            adjusted_signal = apply_flow_to_signal(signal, flow)
            flow["flow_adjustment"] = adjusted_signal.get("flow_adjustment", 0)
            flow["final_score"] = adjusted_signal.get("score", signal.get("score"))
            log_flow(flow, path=config.flow_log_path)
            insert_flow_log(flow, database_url=database_url())
            flow_adjusted_signals.append(adjusted_signal)
        except Exception as exc:
            print(f"Gagal analisis flow {signal.get('symbol')}: {exc}")
            flow_adjusted_signals.append(signal)

    signals = sorted(
        [
            apply_adaptive_regime_shadow_penalty(apply_regime_model_to_signal(signal))
            for signal in flow_adjusted_signals
        ],
        key=lambda item: item["score"],
        reverse=True,
    )

    alerts = [
        signal
        for signal in signals
        if signal["score"] >= config.alert_score_threshold
    ]

    print(f"Scan selesai. Symbols valid: {len(signals)} | Alerts: {len(alerts)}")

    for signal in alerts:
        print(
            "Shadow penalty | "
            f"regime={signal.get('regime_name')} | "
            f"calculated_score={signal.get('calculated_score')} | "
            f"shadow_score={signal.get('shadow_score')} | "
            f"penalty_applied={bool(signal.get('penalty_applied'))}"
        )
        log_signal(signal, path=config.signals_log_path)
        insert_signal(signal, database_url=database_url())
        message = format_signal_message(signal)
        print(message)
        print("-" * 40)

        send_message_if_enabled(message)

    if paper:
        created_trades = open_paper_trades(alerts, path=config.paper_trades_path)
        for trade in created_trades:
            insert_paper_trade(trade, database_url=database_url())
        print(f"Paper trades baru: {len(created_trades)}")

    return alerts


def run_loop(paper: bool = False) -> None:
    sleep_seconds = config.scan_interval_minutes * 60
    print(
        "Mode loop aktif. Scanner berjalan setiap "
        f"{config.scan_interval_minutes} menit."
    )
    if paper:
        print("Paper trading aktif. Tidak ada order Binance sungguhan.")

    while True:
        try:
            run_once(paper=paper)
            if paper:
                send_daily_paper_summary()
        except Exception as exc:
            print(f"Error tidak terduga di loop scanner: {exc}")

        print(f"Menunggu {config.scan_interval_minutes} menit...")
        time.sleep(sleep_seconds)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MAMUYY Binance Hunter V1")
    parser.add_argument(
        "command",
        nargs="?",
        choices=sorted(CLI_SUBCOMMAND_FLAGS),
        help="Optional safe subcommand alias for selected legacy flags.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Jalankan scanner sekali saja lalu keluar.",
    )
    parser.add_argument(
        "--paper",
        action="store_true",
        help="Aktifkan paper trading simulasi tanpa auto buy/sell.",
    )
    parser.add_argument(
        "--report",
        action="store_true",
        help="Generate performance_report.html dari paper_trades.csv.",
    )
    parser.add_argument(
        "--ml",
        action="store_true",
        help="Jalankan ML research analysis dari CSV paper/signal/flow.",
    )
    parser.add_argument(
        "--retrain-model",
        action="store_true",
        help="Retrain ML model dengan guarded candidate replacement.",
    )
    parser.add_argument(
        "--model-status",
        action="store_true",
        help="Tampilkan status production/candidate ML model lifecycle.",
    )
    parser.add_argument(
        "--paper-engine",
        action="store_true",
        help="Jalankan internal paper execution simulator tanpa broker.",
    )
    parser.add_argument(
        "--paper-trade-diagnostics",
        action="store_true",
        help="Audit read-only lifecycle signal ke internal paper trades.",
    )
    parser.add_argument(
        "--paper-portfolio",
        action="store_true",
        help="Tampilkan read-only active internal paper portfolio monitor dan tulis reports/paper_portfolio.json.",
    )
    parser.add_argument(
        "--webhook-test",
        action="store_true",
        help="Generate TradingView-compatible webhook payload localhost test.",
    )
    parser.add_argument(
        "--macro-observer",
        action="store_true",
        help="Jalankan real macro observer analytics read-only.",
    )
    parser.add_argument(
        "--cross-market",
        action="store_true",
        help="Jalankan Cross Market Intelligence analytics dan update CSV.",
    )
    parser.add_argument(
        "--cross-market-report",
        action="store_true",
        help="Generate dan tampilkan Cross Market Intelligence report.",
    )
    parser.add_argument(
        "--strategy-genome",
        action="store_true",
        help="Jalankan PAPER_ONLY Strategy Genome Lab evaluation dan mutation.",
    )
    parser.add_argument(
        "--strategy-ranking",
        action="store_true",
        help="Tampilkan ranking Strategy Genome Lab terakhir.",
    )
    parser.add_argument(
        "--daily-ops-report",
        action="store_true",
        help="Generate daily ops report dan Telegram preview/send sesuai config.",
    )
    parser.add_argument(
        "--anomaly-scan",
        action="store_true",
        help="Jalankan Incident & Anomaly Intelligence scan tanpa Telegram critical send.",
    )
    parser.add_argument(
        "--incident-report",
        action="store_true",
        help="Generate incident report dan kirim/preview CRITICAL incidents sesuai Telegram config.",
    )
    parser.add_argument(
        "--broadcast-test",
        action="store_true",
        help="Uji multi-target broadcast router dalam mode paper/simulation only.",
    )
    parser.add_argument(
        "--competition-status",
        action="store_true",
        help="Tampilkan competition profile routing control.",
    )
    parser.add_argument(
        "--telegram-test",
        action="store_true",
        help="Preview/kirim test Telegram notification sesuai TELEGRAM_ENABLED.",
    )
    parser.add_argument(
        "--notify-summary",
        action="store_true",
        help="Preview/kirim ringkasan event penting Hunter ke Telegram.",
    )
    parser.add_argument(
        "--walkforward",
        action="store_true",
        help="Jalankan walk-forward validation untuk model ML.",
    )
    parser.add_argument(
        "--db-check",
        action="store_true",
        help="Create/migrate/check SQLite database dan buat backup.",
    )
    parser.add_argument(
        "--regime-models",
        action="store_true",
        help="Jalankan regime-specific model analysis.",
    )
    parser.add_argument(
        "--portfolio",
        action="store_true",
        help="Jalankan simulated portfolio construction engine.",
    )
    parser.add_argument(
        "--portfolio-observer",
        action="store_true",
        help="Tampilkan portfolio observability analytics read-only.",
    )
    parser.add_argument(
        "--promotion-scorecard",
        action="store_true",
        help="Generate Promotion Scorecard Engine report PAPER_ONLY read-only.",
    )
    parser.add_argument(
        "--portfolio-risk-budget",
        action="store_true",
        help="Generate portfolio risk budget governance report read-only.",
    )
    parser.add_argument(
        "--governance-audit",
        action="store_true",
        help="Generate Governance Audit report PAPER_ONLY read-only.",
    )
    parser.add_argument(
        "--refresh-governance-reports",
        action="store_true",
        help="Refresh stale governance reports, then rerun readiness reports in PAPER_ONLY read-only mode.",
    )
    parser.add_argument(
        "--phase3-readiness",
        action="store_true",
        help="Generate Phase 3 readiness tracker report PAPER_ONLY read-only.",
    )
    parser.add_argument(
        "--phase3-remediation",
        action="store_true",
        help="Run read-only Phase 3 remediation artifacts and readiness pipeline without unlocking Phase 3.",
    )
    parser.add_argument(
        "--allocate",
        action="store_true",
        help="Jalankan Opportunity Allocation Engine analytics-only.",
    )
    parser.add_argument(
        "--execution",
        action="store_true",
        help="Jalankan simulated execution engine.",
    )
    parser.add_argument(
        "--shadow",
        action="store_true",
        help="Jalankan simulated shadow live engine.",
    )
    parser.add_argument(
        "--orchestrator",
        action="store_true",
        help="Jalankan orchestration engine satu siklus.",
    )
    parser.add_argument(
        "--orchestrator-diagnostics",
        action="store_true",
        help="Tampilkan crash diagnostics orchestrator terbaru.",
    )
    parser.add_argument(
        "--health",
        action="store_true",
        help="Tampilkan lightweight runtime health monitor.",
    )
    parser.add_argument(
        "--risk-check",
        action="store_true",
        help="Jalankan risk manager circuit breaker check.",
    )
    parser.add_argument(
        "--health-guardian-once",
        action="store_true",
        help="Jalankan tmux-compatible health guardian sekali dalam mode aman.",
    )
    parser.add_argument(
        "--heartbeat-test",
        action="store_true",
        help="Tulis dan verifikasi satu row runtime heartbeat SQLite.",
    )
    parser.add_argument(
        "--shadow-analysis",
        action="store_true",
        help="Bandingkan original vs adaptive regime shadow penalty equity curve.",
    )
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="Isi SQLite dengan historical Binance Futures data.",
    )
    parser.add_argument(
        "--label-outcomes",
        action="store_true",
        help="Label historical signal outcomes dari historical_klines.",
    )
    parser.add_argument(
        "--optimize-filters",
        action="store_true",
        help="Cari filter historis terbaik dari historical_outcomes.",
    )
    parser.add_argument(
        "--fix-regime-labels",
        action="store_true",
        help="Isi regime label historis yang masih kosong/UNKNOWN.",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=7,
        help="Jumlah hari historical data untuk --backfill.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    if args.label_outcomes or args.command == "label-outcomes":
        run_label_outcomes(days=args.days)
    elif args.fix_regime_labels or args.command == "fix-regime-labels":
        run_fix_regime_labels()
    elif args.optimize_filters or args.command == "optimize-filters":
        run_optimize_filters()
    elif args.backfill or args.command == "backfill":
        run_backfill(days=args.days)
    elif args.health or args.command == "health":
        run_health()
    elif args.risk_check or args.command == "risk-check":
        run_risk_check()
    elif args.health_guardian_once or args.command == "health-guardian-once":
        run_health_guardian_once()
    elif args.heartbeat_test or args.command == "heartbeat-test":
        run_heartbeat_test()
    elif args.shadow_analysis or args.command == "shadow-analysis":
        run_shadow_analysis()
    elif args.orchestrator:
        run_orchestrator_command()
    elif args.orchestrator_diagnostics:
        run_orchestrator_diagnostics()
    elif args.shadow:
        run_shadow()
    elif args.execution:
        run_execution()
    elif args.allocate:
        run_allocate()
    elif args.portfolio_risk_budget:
        run_portfolio_risk_budget()
    elif args.promotion_scorecard:
        run_promotion_scorecard()
    elif args.governance_audit:
        run_governance_audit_command()
    elif args.refresh_governance_reports or args.command == "refresh-governance-reports":
        run_refresh_governance_reports()
    elif args.phase3_remediation or args.command == "phase3-remediation":
        run_phase3_remediation()
    elif args.phase3_readiness or args.command == "phase3-readiness":
        run_phase3_readiness()
    elif args.portfolio_observer:
        run_portfolio_observer()
    elif args.portfolio:
        run_portfolio()
    elif args.regime_models:
        run_regime_models()
    elif args.db_check:
        run_db_check()
    elif args.walkforward:
        run_walkforward()
    elif args.retrain_model:
        run_retrain_model()
    elif args.model_status:
        run_model_status()
    elif args.paper_engine:
        run_paper_engine()
    elif args.paper_trade_diagnostics or args.command == "paper-trade-diagnostics":
        run_paper_trade_diagnostics()
    elif args.paper_portfolio or args.command == "paper-portfolio":
        run_paper_portfolio()
    elif args.webhook_test:
        run_webhook_test()
    elif args.macro_observer:
        run_macro_observer()
    elif args.cross_market or args.cross_market_report:
        run_cross_market()
    elif args.strategy_genome:
        run_strategy_genome_command()
    elif args.strategy_ranking:
        run_strategy_ranking()
    elif args.daily_ops_report:
        run_daily_ops_report()
    elif args.anomaly_scan:
        run_anomaly_scan_command()
    elif args.incident_report:
        run_incident_report_command()
    elif args.broadcast_test:
        run_broadcast_test()
    elif args.competition_status:
        run_competition_status()
    elif args.telegram_test:
        run_telegram_test()
    elif args.notify_summary:
        run_notify_summary()
    elif args.ml:
        run_ml()
    elif args.report:
        run_report()
    elif args.once:
        run_once(paper=args.paper)
    else:
        run_loop(paper=args.paper)
