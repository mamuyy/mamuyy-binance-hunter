import os
from dataclasses import dataclass

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:
    def load_dotenv() -> None:
        return None


load_dotenv()


def _get_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _get_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _get_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


@dataclass(frozen=True)
class Config:
    binance_base_url: str = os.getenv("BINANCE_BASE_URL", "https://fapi.binance.com")
    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")
    telegram_enabled_flag: bool = _get_bool("TELEGRAM_ENABLED", False)
    scan_interval_minutes: int = _get_int("SCAN_INTERVAL_MINUTES", 15)
    top_symbols_limit: int = _get_int("TOP_SYMBOLS_LIMIT", 30)
    candle_interval: str = os.getenv("CANDLE_INTERVAL", "15m")
    candle_limit: int = _get_int("CANDLE_LIMIT", 60)
    alert_score_threshold: int = _get_int("ALERT_SCORE_THRESHOLD", 75)
    request_timeout_seconds: int = _get_int("REQUEST_TIMEOUT_SECONDS", 15)
    min_quote_volume: float = _get_float("MIN_QUOTE_VOLUME", 0.0)
    database_url: str = os.getenv("DATABASE_URL", "")
    database_path: str = os.getenv("DATABASE_PATH", "mamuyy_hunter.db")
    database_backup_dir: str = os.getenv("DATABASE_BACKUP_DIR", "db_backups")
    signals_log_path: str = os.getenv("SIGNALS_LOG_PATH", "signals_log.csv")
    regime_history_path: str = os.getenv("REGIME_HISTORY_PATH", "regime_history.csv")
    flow_log_path: str = os.getenv("FLOW_LOG_PATH", "flow_log.csv")
    paper_trades_path: str = os.getenv("PAPER_TRADES_PATH", "paper_trades.csv")
    equity_curve_path: str = os.getenv("EQUITY_CURVE_PATH", "equity_curve.csv")
    performance_report_path: str = os.getenv(
        "PERFORMANCE_REPORT_PATH",
        "performance_report.html",
    )
    model_output_path: str = os.getenv("MODEL_OUTPUT_PATH", "model_output.json")
    walkforward_results_path: str = os.getenv(
        "WALKFORWARD_RESULTS_PATH",
        "walkforward_results.csv",
    )
    chart_output_dir: str = os.getenv("CHART_OUTPUT_DIR", "charts")
    paper_summary_state_path: str = os.getenv(
        "PAPER_SUMMARY_STATE_PATH",
        ".paper_summary_state",
    )
    orchestrator_profile: str = os.getenv("ORCHESTRATOR_PROFILE", "NORMAL")
    orchestrator_keepalive_interval_seconds: int = _get_int("ORCHESTRATOR_KEEPALIVE_INTERVAL_SECONDS", 30)
    orchestrator_keepalive_threshold_seconds: int = _get_int("ORCHESTRATOR_KEEPALIVE_THRESHOLD_SECONDS", 30)
    log_retention_days: int = _get_int("LOG_RETENTION_DAYS", 14)
    db_retention_days: int = _get_int("DB_RETENTION_DAYS", 90)
    max_log_bytes: int = _get_int("MAX_LOG_BYTES", 5_000_000)
    risk_ml_accuracy_halt: float = _get_float("RISK_ML_ACCURACY_HALT", 45.0)
    risk_drawdown_halt: float = _get_float("RISK_DRAWDOWN_HALT", -20.0)
    risk_drawdown_watch: float = _get_float("RISK_DRAWDOWN_WATCH", -10.0)
    risk_stale_minutes: int = _get_int("RISK_STALE_MINUTES", 10)
    risk_max_open_trades: int = _get_int("RISK_MAX_OPEN_TRADES", 10)
    risk_loss_cooldown: int = _get_int("RISK_LOSS_COOLDOWN", 3)
    risk_base_position_multiplier: float = _get_float("RISK_BASE_POSITION_MULTIPLIER", 1.0)
    risk_high_vol_confidence_min: float = _get_float("RISK_HIGH_VOL_CONFIDENCE_MIN", 55.0)
    health_guardian_interval_seconds: int = _get_int("HEALTH_GUARDIAN_INTERVAL_SECONDS", 300)
    health_guardian_stale_minutes: int = _get_int("HEALTH_GUARDIAN_STALE_MINUTES", 10)
    health_guardian_dry_run: bool = _get_bool("HEALTH_GUARDIAN_DRY_RUN", True)
    health_guardian_restart_dashboard: bool = _get_bool("HEALTH_GUARDIAN_RESTART_DASHBOARD", False)
    health_guardian_restart_cooldown_seconds: int = _get_int("HEALTH_GUARDIAN_RESTART_COOLDOWN_SECONDS", 300)
    health_guardian_project_dir: str = os.getenv("HEALTH_GUARDIAN_PROJECT_DIR", "~/mamuyy-binance-hunter")
    health_guardian_hunter_session: str = os.getenv("HEALTH_GUARDIAN_HUNTER_SESSION", "hunter")
    health_guardian_dashboard_session: str = os.getenv("HEALTH_GUARDIAN_DASHBOARD_SESSION", "dashboard")

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_enabled_flag and self.telegram_bot_token and self.telegram_chat_id)


config = Config()
