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

    _health = _db_health_check(
        database_url=_health_config.database_url or _health_config.database_path,
        migrate_csv=False,
        backup=False,
    )
    _counts: Dict[str, int] = {}
    try:
        with sqlite3.connect(_health_config.database_path) as _connection:
            for _table in ["signals", "paper_trades", "flow_logs", "regime_logs", "ml_results", "walkforward_results", "shadow_trades"]:
                try:
                    _counts[_table] = _connection.execute(f"SELECT COUNT(*) FROM {_table}").fetchone()[0]
                except sqlite3.Error:
                    _counts[_table] = 0
    except sqlite3.Error:
        pass
    _latest_heartbeat = "-"
    _latest_uptime = "0"
    if os.path.exists("orchestrator_log.csv"):
        try:
            with open("orchestrator_log.csv", newline="", encoding="utf-8") as _log_file:
                _rows = [row for row in csv.DictReader(_log_file) if row.get("engine") == "heartbeat"]
                if _rows:
                    _latest_heartbeat = _rows[-1].get("timestamp", "-")
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
    print(f"Latest Uptime Seconds: {_latest_uptime}")
    print(f"Table Counts: {_counts}")
    sys.exit(0)

from config import config
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
            for table in ["signals", "paper_trades", "flow_logs", "regime_logs", "ml_results", "walkforward_results", "shadow_trades"]:
                try:
                    table_counts[table] = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                except sqlite3.Error:
                    table_counts[table] = 0
    except sqlite3.Error:
        pass

    latest_heartbeat = "-"
    if os.path.exists("orchestrator_log.csv"):
        try:
            with open("orchestrator_log.csv", newline="", encoding="utf-8") as log_file:
                rows = [row for row in csv.DictReader(log_file) if row.get("engine") == "heartbeat"]
                if rows:
                    latest_heartbeat = rows[-1].get("timestamp", "-")
        except OSError:
            latest_heartbeat = "-"

    status = {
        "ok": bool(health.get("ok")),
        "database": config.database_path,
        "table_counts": table_counts,
        "latest_heartbeat": latest_heartbeat,
        "uptime_seconds": uptime_seconds(),
    }
    print("RUNTIME HEALTH")
    print(f"OK: {status['ok']}")
    print(f"Database: {status['database']}")
    print(f"Latest Heartbeat: {status['latest_heartbeat']}")
    print(f"Uptime Seconds: {status['uptime_seconds']}")
    print(f"Table Counts: {status['table_counts']}")
    return status


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
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    if args.health:
        run_health()
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
