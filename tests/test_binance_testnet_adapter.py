import json
from io import BytesIO
from urllib.error import HTTPError

import main
import pytest

from binance_testnet_adapter import (
    BINANCE_TESTNET_CONFIG_VALID,
    BINANCE_TESTNET_DISABLED,
    BINANCE_TESTNET_DRY_RUN_PREVIEW_ONLY,
    BINANCE_TESTNET_LIVE_ENDPOINT_REJECTED,
    BINANCE_TESTNET_ORDER_BLOCKED_BY_GUARD,
    BINANCE_TESTNET_PUBLIC_PING_OK,
    BINANCE_TESTNET_SIGNED_CREDENTIALS_MISSING,
    BINANCE_TESTNET_SIGNED_ENDPOINT_BLOCKED,
    BINANCE_TESTNET_SIGNED_ORDER_ENDPOINT_REJECTED,
    BINANCE_TESTNET_SIGNED_READ_ONLY_DISABLED,
    BINANCE_TESTNET_SIGNED_READ_ONLY_OK,
    BinanceTestnetAdapter,
    UrllibHttpClient,
    classify_binance_signed_error,
    DEFAULT_REST_BASE_URL,
    load_binance_testnet_config,
    mask_secret,
    run_binance_testnet_audit,
    sign_query_string,
    strip_signature_from_url_or_query,
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

class HttpErrorClient:
    def __init__(self, status_code=401, body=b""):
        self.status_code = status_code
        self.body = body
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        raise HTTPError(url, self.status_code, "Unauthorized", {}, BytesIO(self.body))


class ErrorHttpClient:
    def __init__(self, status_code=400, body=None):
        self.status_code = status_code
        self.body = body or {}
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return {"status_code": self.status_code, "body": self.body}


def signed_config(raw_key="raw-api-key", raw_secret="raw-api-secret"):
    config = load_binance_testnet_config(
        env={"BROKER_MODE": "testnet", "BINANCE_TESTNET_API_KEY": raw_key, "BINANCE_TESTNET_API_SECRET": raw_secret},
        dotenv_path="/does/not/exist",
    )
    return __import__("dataclasses").replace(config, signed_read_only_enabled=True)

def test_urllib_http_client_get_passes_provided_headers_to_request(monkeypatch):
    raw_key = "raw-api-key-value"
    captured = {}

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return b'{"ok":true}'

    def fake_request(url, headers=None):
        captured["url"] = url
        captured["headers"] = headers
        return "request-object"

    def fake_urlopen(request, timeout=0):
        captured["request"] = request
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr("binance_testnet_adapter.urllib.request.Request", fake_request)
    monkeypatch.setattr("binance_testnet_adapter.urllib.request.urlopen", fake_urlopen)

    response = UrllibHttpClient().get(
        "https://demo-fapi.binance.com/fapi/v2/account",
        timeout=7,
        headers={"X-MBX-APIKEY": raw_key},
    )

    assert response == {"status_code": 200, "body": {"ok": True}}
    assert captured == {
        "url": "https://demo-fapi.binance.com/fapi/v2/account",
        "headers": {"X-MBX-APIKEY": raw_key},
        "request": "request-object",
        "timeout": 7.0,
    }

def test_signed_read_only_default_urllib_client_sends_api_key_header(monkeypatch):
    raw_key = "raw-api-key-value"
    raw_secret = "raw-api-secret-value"
    captured = {}

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return b"{}"

    def fake_request(url, headers=None):
        captured["url"] = url
        captured["headers"] = headers
        return "request-object"

    def fake_urlopen(request, timeout=0):
        captured["request"] = request
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr("binance_testnet_adapter.urllib.request.Request", fake_request)
    monkeypatch.setattr("binance_testnet_adapter.urllib.request.urlopen", fake_urlopen)

    result = BinanceTestnetAdapter(config=signed_config(raw_key, raw_secret), http_client=UrllibHttpClient()).signed_account_read_only()

    assert result["status"] == BINANCE_TESTNET_SIGNED_READ_ONLY_OK
    assert captured["headers"] == {"X-MBX-APIKEY": raw_key}
    assert "signature=" in captured["url"]
    assert raw_key not in captured["url"]
    assert raw_secret not in captured["url"]

def test_invalid_key_or_permission_error_is_classified():
    assert classify_binance_signed_error(-2015, "Invalid API-key, IP, or permissions for action.") == "INVALID_KEY_OR_PERMISSION"

def test_timestamp_error_is_classified():
    assert classify_binance_signed_error(-1021, "Timestamp for this request is outside of the recvWindow.") == "TIMESTAMP_DRIFT"

def test_signature_error_is_classified():
    assert classify_binance_signed_error(-1022, "Signature for this request is not valid.") == "INVALID_SIGNATURE"

def test_signed_failure_diagnostic_includes_safe_remote_details():
    adapter = BinanceTestnetAdapter(
        config=signed_config(),
        http_client=ErrorHttpClient(401, {"code": -2015, "msg": "Invalid API-key, IP, or permissions for action."}),
    )

    result = adapter.signed_account_read_only()

    diagnostic = result["diagnostic"]
    assert result["status"] == "BINANCE_TESTNET_SIGNED_READ_ONLY_FAILED"
    assert diagnostic["http_status"] == 401
    assert diagnostic["binance_code"] == -2015
    assert diagnostic["binance_msg"] == "Invalid API-key, IP, or permissions for action."
    assert diagnostic["category"] == "INVALID_KEY_OR_PERMISSION"
    assert diagnostic["endpoint_path"] == "/fapi/v2/account"
    assert diagnostic["timestamp_included"] is True
    assert diagnostic["recvWindow_included"] is False
    assert isinstance(diagnostic["local_timestamp"], int)

def test_diagnostic_output_strips_signature_from_query_and_url():
    value = "https://demo-fapi.binance.com/fapi/v2/account?timestamp=1&signature=abcdef&symbol=BTCUSDT"

    sanitized = strip_signature_from_url_or_query(value)

    assert "signature=abcdef" not in sanitized
    assert "signature=%3Credacted%3E" in sanitized or "signature=<redacted>" in sanitized
    assert "timestamp=1" in sanitized

def test_raw_api_key_and_secret_never_appear_in_signed_audit_report(tmp_path):
    raw_key = "raw-api-key-value"
    raw_secret = "raw-api-secret-value"
    dotenv_path = write_dotenv(
        tmp_path,
        f"BROKER_MODE=testnet\nBINANCE_TESTNET_API_KEY={raw_key}\nBINANCE_TESTNET_API_SECRET={raw_secret}\n",
    )
    report_path = tmp_path / "reports" / "binance_testnet_audit.json"

    run_binance_testnet_audit(
        dotenv_path=dotenv_path,
        report_path=str(report_path),
        http_client=ErrorHttpClient(401, {"code": -2015, "msg": "Invalid API-key, IP, or permissions for action."}),
        run_public_checks=True,
        run_signed_read_only=True,
    )

    report_text = report_path.read_text(encoding="utf-8")
    report = json.loads(report_text)
    assert raw_key not in report_text
    assert raw_secret not in report_text
    assert "signature=" not in report_text or "abcdef" not in report_text
    assert report["account_read_diagnostic"]["category"] == "INVALID_KEY_OR_PERMISSION"
    assert report["signed_read_only_error_categories"] == ["INVALID_KEY_OR_PERMISSION"]
    assert "key" in report["signed_read_only_recommendation"]

def test_bad_api_key_format_error_is_classified_and_recommended(tmp_path):
    assert classify_binance_signed_error(-2014, "API-key format invalid.") in {"BAD_API_KEY_FORMAT", "INVALID_API_KEY_FORMAT"}

    raw_key = "raw-api-key-value"
    raw_secret = "raw-api-secret-value"
    dotenv_path = write_dotenv(
        tmp_path,
        f"BROKER_MODE=testnet\nBINANCE_TESTNET_API_KEY={raw_key}\nBINANCE_TESTNET_API_SECRET={raw_secret}\n",
    )
    report_path = tmp_path / "reports" / "binance_testnet_audit.json"

    run_binance_testnet_audit(
        dotenv_path=dotenv_path,
        report_path=str(report_path),
        http_client=ErrorHttpClient(401, {"code": -2014, "msg": "API-key format invalid."}),
        run_public_checks=False,
        run_signed_read_only=True,
    )

    report_text = report_path.read_text(encoding="utf-8")
    report = json.loads(report_text)
    assert raw_key not in report_text
    assert raw_secret not in report_text
    assert "X-MBX-APIKEY" not in report_text
    assert report["account_read_diagnostic"]["category"] in {"BAD_API_KEY_FORMAT", "INVALID_API_KEY_FORMAT"}
    assert report["signed_read_only_recommendation"] == "check_api_key_header_delivery_and_key_product_for_usd_m_futures_testnet"

def test_http_error_body_invalid_key_is_extracted_and_classified():
    adapter = BinanceTestnetAdapter(
        config=signed_config(),
        http_client=HttpErrorClient(401, b'{"code":-2015,"msg":"Invalid API-key, IP, or permissions for action."}'),
    )

    result = adapter.signed_account_read_only()

    diagnostic = result["diagnostic"]
    assert diagnostic["http_status"] == 401
    assert diagnostic["binance_code"] == -2015
    assert diagnostic["binance_msg"] == "Invalid API-key, IP, or permissions for action."
    assert diagnostic["category"] == "INVALID_KEY_OR_PERMISSION"
    assert diagnostic["raw_error_body_present"] is True
    assert diagnostic["http_error_body_parse_status"] == "json"

def test_http_error_body_timestamp_drift_is_extracted_and_classified():
    adapter = BinanceTestnetAdapter(
        config=signed_config(),
        http_client=HttpErrorClient(400, b'{"code":-1021,"msg":"Timestamp for this request is outside of the recvWindow."}'),
    )

    diagnostic = adapter.signed_account_read_only()["diagnostic"]

    assert diagnostic["binance_code"] == -1021
    assert diagnostic["category"] == "TIMESTAMP_DRIFT"

def test_http_error_body_invalid_signature_is_extracted_and_classified():
    adapter = BinanceTestnetAdapter(
        config=signed_config(),
        http_client=HttpErrorClient(400, b'{"code":-1022,"msg":"Signature for this request is not valid."}'),
    )

    diagnostic = adapter.signed_account_read_only()["diagnostic"]

    assert diagnostic["binance_code"] == -1022
    assert diagnostic["category"] == "INVALID_SIGNATURE"
    assert "signature=" not in (diagnostic["sanitized_error_body_preview"] or "")

def test_http_error_401_empty_body_is_unauthorized_no_body():
    adapter = BinanceTestnetAdapter(config=signed_config(), http_client=HttpErrorClient(401, b""))

    diagnostic = adapter.signed_account_read_only()["diagnostic"]

    assert diagnostic["http_status"] == 401
    assert diagnostic["binance_code"] is None
    assert diagnostic["binance_msg"] is None
    assert diagnostic["category"] == "UNAUTHORIZED_NO_BODY"
    assert diagnostic["raw_error_body_present"] is False
    assert diagnostic["http_error_body_parse_status"] == "empty"

def test_http_error_diagnostic_redacts_signature_and_omits_raw_secrets():
    raw_key = "raw-api-key-value"
    raw_secret = "raw-api-secret-value"
    adapter = BinanceTestnetAdapter(
        config=signed_config(raw_key, raw_secret),
        http_client=HttpErrorClient(
            401,
            b'{"code":-1022,"msg":"Signature for this request is not valid: signature=abcdef"}',
        ),
    )

    diagnostic_text = json.dumps(adapter.signed_account_read_only()["diagnostic"])

    assert raw_key not in diagnostic_text
    assert raw_secret not in diagnostic_text
    assert "signature=abcdef" not in diagnostic_text
    assert "signature=%3Credacted%3E" in diagnostic_text or "signature=<redacted>" in diagnostic_text

def test_signed_query_signature_has_expected_hmac_shape():
    params = {"timestamp": 1234567890}
    assert sign_query_string(params, "test-secret") == "43b9e569008cebbd1321239fd7e5e5d8a5d11a6a1d466be9ad1771faea4cb280"

def test_signed_read_only_disabled_by_default(tmp_path):
    dotenv_path = write_dotenv(tmp_path, "BROKER_MODE=testnet\nBINANCE_TESTNET_API_KEY=k\nBINANCE_TESTNET_API_SECRET=s\n")
    report_path = tmp_path / "audit.json"
    result = run_binance_testnet_audit(dotenv_path=dotenv_path, report_path=str(report_path), http_client=RecordingHttpClient())
    assert result.signed_read_only_enabled is False
    assert result.signed_read_only_status == BINANCE_TESTNET_SIGNED_READ_ONLY_DISABLED

def test_signed_read_only_credentials_missing_when_enabled():
    config = load_binance_testnet_config(env={"BROKER_MODE": "testnet"}, dotenv_path="/does/not/exist")
    config = __import__("dataclasses").replace(config, signed_read_only_enabled=True)
    result = BinanceTestnetAdapter(config=config, http_client=RecordingHttpClient()).signed_account_read_only()
    assert result["status"] == BINANCE_TESTNET_SIGNED_CREDENTIALS_MISSING

def test_signed_read_only_blocks_live_endpoint():
    config = load_binance_testnet_config(env={"BROKER_MODE": "testnet", "BINANCE_FUTURES_TESTNET_BASE_URL": "https://fapi.binance.com", "BINANCE_TESTNET_API_KEY": "k", "BINANCE_TESTNET_API_SECRET": "s"}, dotenv_path="/does/not/exist")
    config = __import__("dataclasses").replace(config, signed_read_only_enabled=True)
    result = BinanceTestnetAdapter(config=config, http_client=RecordingHttpClient()).signed_account_read_only()
    assert result["status"] == BINANCE_TESTNET_SIGNED_ENDPOINT_BLOCKED

def test_signed_read_only_rejects_order_endpoint_path():
    config = load_binance_testnet_config(env={"BROKER_MODE": "testnet", "BINANCE_TESTNET_API_KEY": "k", "BINANCE_TESTNET_API_SECRET": "s"}, dotenv_path="/does/not/exist")
    config = __import__("dataclasses").replace(config, signed_read_only_enabled=True)
    result = BinanceTestnetAdapter(config=config, http_client=RecordingHttpClient())._signed_read_only_get("/fapi/v1/order")
    assert result["status"] == BINANCE_TESTNET_SIGNED_ORDER_ENDPOINT_REJECTED

def test_signed_account_balance_reads_use_injected_client_and_header_only():
    raw_key = "raw-api-key"
    raw_secret = "raw-api-secret"
    http_client = RecordingHttpClient()
    config = load_binance_testnet_config(env={"BROKER_MODE": "testnet", "BINANCE_TESTNET_API_KEY": raw_key, "BINANCE_TESTNET_API_SECRET": raw_secret}, dotenv_path="/does/not/exist")
    config = __import__("dataclasses").replace(config, signed_read_only_enabled=True)
    adapter = BinanceTestnetAdapter(config=config, http_client=http_client)
    assert adapter.signed_account_read_only()["status"] == BINANCE_TESTNET_SIGNED_READ_ONLY_OK
    assert adapter.signed_balance_read_only()["status"] == BINANCE_TESTNET_SIGNED_READ_ONLY_OK
    assert len(http_client.calls) == 2
    for url, kwargs in http_client.calls:
        assert raw_key not in url
        assert raw_secret not in url
        assert kwargs["headers"] == {"X-MBX-APIKEY": raw_key}
        assert "signature=" in url

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


@pytest.mark.parametrize(
    "broker_mode",
    [
        "testnet",
        "TESTNET",
        "binance_testnet",
        "BINANCE_TESTNET",
        "BINANCE_FUTURES_TESTNET",
        "BINANCE_FUTURES_TESTNET_ONLY",
        "USD_M_FUTURES_TESTNET",
    ],
)
def test_explicit_testnet_broker_modes_enable_public_audit(broker_mode):
    config = load_binance_testnet_config(
        env={
            "BROKER_MODE": broker_mode,
            "BINANCE_TESTNET_API_KEY": "test-key",
            "BINANCE_TESTNET_API_SECRET": "test-secret",
            "BINANCE_FUTURES_TESTNET_BASE_URL": "https://demo-fapi.binance.com",
            "ALLOW_TESTNET_ORDER": "False",
            "ALLOW_AUTO_TESTNET_ORDER": "False",
        },
        dotenv_path="/does/not/exist",
    )

    validation = validate_binance_testnet_config(config)

    assert config.enabled is True
    assert validation["status"] == BINANCE_TESTNET_CONFIG_VALID
    assert validation["enabled"] is True

def test_allow_testnet_order_false_blocks_orders_without_disabling_public_audit():
    secret = "raw-secret-value"
    config = load_binance_testnet_config(
        env={
            "BROKER_MODE": "BINANCE_FUTURES_TESTNET_ONLY",
            "BINANCE_TESTNET_API_KEY": secret,
            "BINANCE_TESTNET_API_SECRET": secret,
            "BINANCE_FUTURES_TESTNET_BASE_URL": "https://demo-fapi.binance.com",
            "ALLOW_TESTNET_ORDER": "False",
            "ALLOW_AUTO_TESTNET_ORDER": "False",
        },
        dotenv_path="/does/not/exist",
    )
    adapter = BinanceTestnetAdapter(config=config, http_client=RecordingHttpClient())

    validation = validate_binance_testnet_config(config)
    order = adapter.place_testnet_order({"symbol": "BTCUSDT", "side": "BUY"})
    ping = adapter.ping()
    report_text = json.dumps(validation)

    assert validation["status"] == BINANCE_TESTNET_CONFIG_VALID
    assert validation["enabled"] is True
    assert order["status"] == BINANCE_TESTNET_ORDER_BLOCKED_BY_GUARD
    assert ping["status"] == BINANCE_TESTNET_PUBLIC_PING_OK
    assert validation["api_key_masked"] == mask_secret(secret)
    assert secret not in report_text

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
