import argparse
import csv
import os
import sqlite3
import sys
import time
from typing import List, Dict, Any

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
            for _table in ["signals", "paper_trades", "flow_logs", "regime_logs", "ml_results", "walkforward_results", "shadow_trades", "historical_klines", "historical_funding", "historical_open_interest", "historical_outcomes", "runtime_heartbeats"]:
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
            project_dir=_guardian_config.health_guardian_project_dir or os.getcwd(),
            hunter_session=_guardian_config.health_guardian_hunter_session,
            dashboard_session=_guardian_config.health_guardian_dashboard_session,
            stale_minutes=_guardian_config.health_guardian_stale_minutes,
            interval_seconds=_guardian_config.health_guardian_interval_seconds,
            dry_run=_guardian_config.health_guardian_dry_run,
            restart_dashboard=_guardian_config.health_guardian_restart_dashboard,
        )
    )
    print(_format_health_guardian_result(_guardian_result))
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
from outcome_labeler import label_historical_outcomes
from regime_labeler import fix_historical_regime_labels
from database import (
    backup_database,
    db_health_check,
    insert_flow_log,
    insert_ml_result,
    insert_paper_trade,
    insert_regime_log,
    insert_signal,
    insert_walkforward_rows,
)
from execution_engine import run_execution_simulation
from flow_engine import AdvancedFlowEngine, apply_flow_to_signal, log_flow
from health_guardian import HealthGuardianConfig, check_health_guardian_once, format_health_guardian_result, resolve_runtime_heartbeat
from logger import log_signal
from market_regime import (
    MarketRegimeEngine,
    apply_regime_to_signal,
    log_regime_history,
)
from ml_engine import run_ml_research
from orchestrator import run_orchestrator, uptime_seconds
from portfolio_engine import build_portfolio
from regime_models import analyze_regime_models, apply_regime_model_to_signal
from report_generator import generate_performance_report
from risk_manager import RiskConfig, check_execution_safety
from scanner import BinanceFuturesScanner
from shadow_engine import run_shadow_live
from telegram import (
    format_execution_message,
    format_market_regime_message,
    format_ml_analysis_message,
    format_orchestrator_message,
    format_paper_summary_message,
    format_performance_report_message,
    format_portfolio_message,
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


def run_walkforward() -> Dict[str, Any]:
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


def run_execution() -> Dict[str, Any]:
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
    )
    message = format_orchestrator_message(result)
    print(message)
    send_message_if_enabled(message)
    return result


def run_health() -> Dict[str, Any]:
    health = db_health_check(database_url=database_url(), migrate_csv=False, backup=False)
    table_counts: Dict[str, int] = {}
    try:
        with sqlite3.connect(config.database_path) as connection:
            for table in ["signals", "paper_trades", "flow_logs", "regime_logs", "ml_results", "walkforward_results", "shadow_trades", "historical_klines", "historical_funding", "historical_open_interest", "historical_outcomes", "runtime_heartbeats"]:
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
        project_dir=config.health_guardian_project_dir or os.getcwd(),
        hunter_session=config.health_guardian_hunter_session,
        dashboard_session=config.health_guardian_dashboard_session,
        stale_minutes=config.health_guardian_stale_minutes,
        interval_seconds=config.health_guardian_interval_seconds,
        dry_run=config.health_guardian_dry_run,
        restart_dashboard=config.health_guardian_restart_dashboard,
    )


def run_health_guardian_once() -> Dict[str, Any]:
    result = check_health_guardian_once(health_guardian_config_from_env())
    print(format_health_guardian_result(result))
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
        [apply_regime_model_to_signal(signal) for signal in flow_adjusted_signals],
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
    if args.label_outcomes:
        run_label_outcomes(days=args.days)
    elif args.fix_regime_labels:
        run_fix_regime_labels()
    elif args.optimize_filters:
        run_optimize_filters()
    elif args.backfill:
        run_backfill(days=args.days)
    elif args.health:
        run_health()
    elif args.risk_check:
        run_risk_check()
    elif args.health_guardian_once:
        run_health_guardian_once()
    elif args.orchestrator:
        run_orchestrator_command()
    elif args.shadow:
        run_shadow()
    elif args.execution:
        run_execution()
    elif args.portfolio:
        run_portfolio()
    elif args.regime_models:
        run_regime_models()
    elif args.db_check:
        run_db_check()
    elif args.walkforward:
        run_walkforward()
    elif args.ml:
        run_ml()
    elif args.report:
        run_report()
    elif args.once:
        run_once(paper=args.paper)
    else:
        run_loop(paper=args.paper)
