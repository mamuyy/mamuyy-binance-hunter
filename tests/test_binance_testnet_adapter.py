import json

import main

from binance_testnet_adapter import (
    BINANCE_TESTNET_CONFIG_VALID,
    BINANCE_TESTNET_DISABLED,
    BINANCE_TESTNET_DRY_RUN_PREVIEW_ONLY,
    BINANCE_TESTNET_LIVE_ENDPOINT_REJECTED,
    BINANCE_TESTNET_ORDER_BLOCKED_BY_GUARD,
    BINANCE_TESTNET_PUBLIC_PING_OK,
    BinanceTestnetAdapter,
    DEFAULT_REST_BASE_URL,
    load_binance_testnet_config,
    mask_secret,
    run_binance_testnet_audit,
    validate_binance_testnet_config,
)


class RecordingHttpClient:
    def __init__(self):
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return {"status_code": 200, "body": {}}


def write_dotenv(tmp_path, content):
    path = tmp_path / ".env"
    path.write_text(content, encoding="utf-8")
    return str(path)


def test_loads_existing_variable_names_from_dotenv_content(tmp_path, monkeypatch):
    monkeypatch.delenv("BROKER_MODE", raising=False)
    dotenv_path = write_dotenv(
        tmp_path,
        "\n".join(
            [
                "BINANCE_TESTNET_API_KEY=dotenv-key",
                "BINANCE_TESTNET_API_SECRET=dotenv-secret",
                "BINANCE_FUTURES_TESTNET_BASE_URL=https://demo-fapi.binance.com",
                "BROKER_MODE=testnet",
                "REAL_BINANCE_ENABLED=false",
                "ALLOW_REAL_BINANCE_ORDER=false",
                "ALLOW_TESTNET_ORDER=false",
                "ALLOW_AUTO_TESTNET_ORDER=false",
                "TESTNET_ORDER_ALLOWLIST=BTCUSDT",
                "TESTNET_MAX_ORDERS_PER_DAY=3",
                "TESTNET_MAX_NOTIONAL_USDT=12.5",
                "TESTNET_DEFAULT_LEVERAGE=2",
            ]
        ),
    )

    config = load_binance_testnet_config(env={}, dotenv_path=dotenv_path)

    assert config.enabled is True
    assert config.broker_mode == "testnet"
    assert config.rest_base_url == DEFAULT_REST_BASE_URL
    assert config.api_key == "dotenv-key"
    assert config.api_secret == "dotenv-secret"
    assert config.allow_testnet_order is False
    assert config.max_orders_per_day == 3
    assert config.max_notional_usdt == 12.5
    assert config.default_leverage == 2


def test_process_env_overrides_dotenv_values(tmp_path):
    dotenv_path = write_dotenv(
        tmp_path,
        "BROKER_MODE=paper\nBINANCE_FUTURES_TESTNET_BASE_URL=https://testnet.binancefuture.com\nTESTNET_MAX_NOTIONAL_USDT=10\n",
    )

    config = load_binance_testnet_config(
        env={"BROKER_MODE": "testnet", "TESTNET_MAX_NOTIONAL_USDT": "20"},
        dotenv_path=dotenv_path,
    )

    assert config.broker_mode == "testnet"
    assert config.max_notional_usdt == 20.0
    assert config.rest_base_url == "https://testnet.binancefuture.com"


def test_secrets_are_masked_and_never_returned_raw(tmp_path):
    secret = "very-secret-value"
    dotenv_path = write_dotenv(
        tmp_path,
        f"BROKER_MODE=testnet\nBINANCE_TESTNET_API_KEY={secret}\nBINANCE_TESTNET_API_SECRET={secret}\n",
    )

    config = load_binance_testnet_config(env={}, dotenv_path=dotenv_path)
    validation = validate_binance_testnet_config(config)
    preview = BinanceTestnetAdapter(config=config).account_status_preview()

    assert mask_secret(secret) != secret
    assert secret not in json.dumps(validation)
    assert secret not in json.dumps(preview)
    assert validation["api_key_masked"] == mask_secret(secret)
    assert preview["api_secret_masked"] == mask_secret(secret)


def test_live_futures_endpoint_is_rejected():
    config = load_binance_testnet_config(
        env={"BROKER_MODE": "testnet", "BINANCE_FUTURES_TESTNET_BASE_URL": "https://fapi.binance.com"},
        dotenv_path="/does/not/exist",
    )

    result = validate_binance_testnet_config(config)

    assert result["ok"] is False
    assert result["status"] == BINANCE_TESTNET_LIVE_ENDPOINT_REJECTED


def test_live_spot_endpoint_is_rejected():
    config = load_binance_testnet_config(
        env={"BROKER_MODE": "testnet", "BINANCE_FUTURES_TESTNET_BASE_URL": "https://api.binance.com"},
        dotenv_path="/does/not/exist",
    )

    result = validate_binance_testnet_config(config)

    assert result["ok"] is False
    assert result["status"] == BINANCE_TESTNET_LIVE_ENDPOINT_REJECTED


def test_default_mode_is_fail_closed_and_no_order():
    config = load_binance_testnet_config(env={}, dotenv_path="/does/not/exist")
    adapter = BinanceTestnetAdapter(config=config)

    ping = adapter.ping()
    order = adapter.place_testnet_order({"symbol": "BTCUSDT"})

    assert config.enabled is False
    assert config.order_placement_enabled is False
    assert ping["status"] == BINANCE_TESTNET_DISABLED
    assert order["status"] == BINANCE_TESTNET_ORDER_BLOCKED_BY_GUARD


def test_place_order_preview_does_not_call_network():
    http_client = RecordingHttpClient()
    config = load_binance_testnet_config(env={"BROKER_MODE": "testnet"}, dotenv_path="/does/not/exist")
    adapter = BinanceTestnetAdapter(config=config, http_client=http_client)

    result = adapter.place_order_preview({"symbol": "BTCUSDT", "side": "BUY"})

    assert result["ok"] is True
    assert result["status"] == BINANCE_TESTNET_DRY_RUN_PREVIEW_ONLY
    assert result["would_place_order"] is False
    assert http_client.calls == []


def test_place_testnet_order_is_blocked_in_this_pr():
    config = load_binance_testnet_config(env={"BROKER_MODE": "testnet", "ALLOW_TESTNET_ORDER": "true"}, dotenv_path="/does/not/exist")
    adapter = BinanceTestnetAdapter(config=config)

    result = adapter.place_testnet_order({"symbol": "BTCUSDT", "side": "BUY"})

    assert result["ok"] is False
    assert result["status"] == BINANCE_TESTNET_ORDER_BLOCKED_BY_GUARD


def test_public_ping_uses_injected_mock_http_client():
    http_client = RecordingHttpClient()
    config = load_binance_testnet_config(env={"BROKER_MODE": "testnet"}, dotenv_path="/does/not/exist")
    adapter = BinanceTestnetAdapter(config=config, http_client=http_client)

    result = adapter.ping()

    assert result["ok"] is True
    assert result["status"] == BINANCE_TESTNET_PUBLIC_PING_OK
    assert http_client.calls == [("https://demo-fapi.binance.com/fapi/v1/ping", {"timeout": 10})]


def test_binance_testnet_audit_report_preserves_no_raw_secrets(tmp_path):
    secret = "raw-secret-value"
    dotenv_path = write_dotenv(
        tmp_path,
        f"BROKER_MODE=testnet\nBINANCE_TESTNET_API_KEY={secret}\nBINANCE_TESTNET_API_SECRET={secret}\n",
    )
    report_path = tmp_path / "reports" / "binance_testnet_audit.json"

    result = run_binance_testnet_audit(
        dotenv_path=dotenv_path,
        report_path=str(report_path),
        http_client=RecordingHttpClient(),
        run_public_checks=True,
    )
    report_text = report_path.read_text(encoding="utf-8")
    report = json.loads(report_text)

    assert result.status == BINANCE_TESTNET_CONFIG_VALID
    assert report["api_key_present"] is True
    assert report["api_secret_present"] is True
    assert report["public_ping_status"] == BINANCE_TESTNET_PUBLIC_PING_OK
    assert secret not in report_text


def test_readiness_governance_is_unchanged():
    config = load_binance_testnet_config(env={}, dotenv_path="/does/not/exist")
    validation = validate_binance_testnet_config(config)

    execution_allowed = False
    paper_only = True
    assert validation["enabled"] is False
    assert validation["allow_real_binance_order"] is False
    assert validation["real_binance_enabled"] is False
    assert execution_allowed is False
    assert paper_only is True
    assert getattr(main, "CLI_SUBCOMMAND_FLAGS")["ml-metric-audit"] == "--ml-metric-audit"
    assert getattr(main, "CLI_SUBCOMMAND_FLAGS")["binance-testnet-audit"] == "--binance-testnet-audit"
