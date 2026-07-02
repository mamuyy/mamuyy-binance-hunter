import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import cp044_valid_signal_watch as watch
import semi_auto_testnet_bridge as bridge


def write_report(path: Path, *, symbol="BTCUSDT", score=95, direction="LONG", rank="HIGH_QUALITY", generated_at=None):
    payload = {
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
        "signal": {"symbol": symbol, "direction": direction, "score": score, "price": 50000.0, "quantity": 0.00043},
        "overlay": {
            "signal_score": score,
            "portfolio_eligible": True,
            "overlay_decision": "LONG / TESTNET_READY",
            "trade_rank": rank,
            "suggested_risk": "NORMAL",
        },
        "allocation_record": {"portfolio_eligible": True},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return payload


def run_bridge(tmp_path, monkeypatch, report_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BROKER_MODE", "BINANCE_FUTURES_TESTNET_ONLY")
    monkeypatch.setenv("TESTNET_ORDER_ALLOWLIST", "BTCUSDT,ETHUSDT,HYPEUSDT")
    monkeypatch.setenv("REAL_BINANCE_ENABLED", "false")
    monkeypatch.setenv("ALLOW_REAL_BINANCE_ORDER", "false")
    monkeypatch.setenv("ALLOW_AUTO_TESTNET_ORDER", "false")
    monkeypatch.setenv("TESTNET_DAILY_ORDER_LIMIT", "5")
    args = argparse.Namespace(overlay_report_path=str(report_path), symbol="", telegram_preview=False, allow_need_review=False)
    return bridge.run(args)


def test_stale_overlay_blocks_clearly(tmp_path, monkeypatch):
    report = tmp_path / "stale.json"
    write_report(report, generated_at=(datetime.now(timezone.utc) - timedelta(days=20)).isoformat())
    result = run_bridge(tmp_path, monkeypatch, report)
    assert result["status"] == "BLOCKED_STALE_OVERLAY"
    assert result["overlay_freshness_passed"] is False
    assert result["order_attempted"] is False


def test_fresh_invalid_overlay_remains_blocked(tmp_path, monkeypatch):
    report = tmp_path / "invalid.json"
    write_report(report, score=89)
    result = run_bridge(tmp_path, monkeypatch, report)
    assert result["status"] == "BLOCKED"
    assert result["overlay_freshness_passed"] is True
    assert "signal_score is below 90 or unavailable." in result["blocked_reasons"]
    assert result["order_attempted"] is False


def test_valid_allowlisted_candidate_can_reach_would_order(tmp_path, monkeypatch):
    for symbol in ["BTCUSDT", "ETHUSDT", "HYPEUSDT"]:
        report = tmp_path / f"{symbol}.json"
        write_report(report, symbol=symbol)
        result = run_bridge(tmp_path, monkeypatch, report)
        assert result["status"] == "WOULD_ORDER"
        assert result["order_attempted"] is False


def test_non_allowlist_candidate_remains_blocked(tmp_path, monkeypatch):
    report = tmp_path / "xrp.json"
    write_report(report, symbol="XRPUSDT")
    result = run_bridge(tmp_path, monkeypatch, report)
    assert result["status"] == "BLOCKED"
    assert "symbol is not in TESTNET_ORDER_ALLOWLIST." in result["blocked_reasons"]


def test_direction_unknown_remains_blocked(tmp_path, monkeypatch):
    report = tmp_path / "unknown.json"
    write_report(report, direction="UNKNOWN")
    result = run_bridge(tmp_path, monkeypatch, report)
    assert result["status"] == "BLOCKED"
    assert "direction must be BUY/LONG or SELL/SHORT." in result["blocked_reasons"]


def test_valid_signal_watch_ready_for_prepare(tmp_path, monkeypatch):
    report = tmp_path / "ready.json"
    write_report(report)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BROKER_MODE", "BINANCE_FUTURES_TESTNET_ONLY")
    monkeypatch.setenv("TESTNET_ORDER_ALLOWLIST", "BTCUSDT,ETHUSDT,HYPEUSDT")
    monkeypatch.setenv("ALLOW_TESTNET_ORDER", "true")
    monkeypatch.setenv("REAL_BINANCE_ENABLED", "false")
    monkeypatch.setenv("ALLOW_REAL_BINANCE_ORDER", "false")
    monkeypatch.setenv("ALLOW_AUTO_TESTNET_ORDER", "false")
    monkeypatch.setattr(watch.supervisor, "run", lambda *a, **k: {"verdict": "SAFE_IDLE", "phase3_armed": True, "real_binance_enabled": False, "allow_real_binance_order": False, "allow_auto_testnet_order": False, "blocked_reasons": []})
    args = argparse.Namespace(overlay_report_path=str(report), supervisor_result_path=str(tmp_path / "missing.json"), supervisor_mode="status", refresh_supervisor=True, symbol="", output=str(tmp_path / "watch.json"))
    result = watch.evaluate(args)
    assert result["verdict"] == "READY_FOR_PREPARE"
    assert result["bridge_dry_run_verdict"] == "WOULD_ORDER"
    assert result["orders_placed"] is False
    assert result["broker_called"] is False
    assert result["real_trading_locked"] is True
    assert result["auto_execution_locked"] is True
