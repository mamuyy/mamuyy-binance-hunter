"""Fail-closed Binance USD-M Futures testnet adapter scaffold.

This module reconnects existing Binance futures testnet/demo values from the
process environment or a local .env file without exposing secrets.  It is
scaffold-only: no live orders, no testnet orders, and no signed order endpoint
calls are performed in this PR.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Any, Mapping, Optional, Protocol
from urllib.parse import urlparse
from urllib.request import urlopen


BINANCE_TESTNET_ENV_FOUND = "BINANCE_TESTNET_ENV_FOUND"
BINANCE_TESTNET_DISABLED = "BINANCE_TESTNET_DISABLED"
BINANCE_TESTNET_CONFIG_VALID = "BINANCE_TESTNET_CONFIG_VALID"
BINANCE_TESTNET_CONFIG_INVALID = "BINANCE_TESTNET_CONFIG_INVALID"
BINANCE_TESTNET_PUBLIC_PING_OK = "BINANCE_TESTNET_PUBLIC_PING_OK"
BINANCE_TESTNET_PUBLIC_PING_FAILED = "BINANCE_TESTNET_PUBLIC_PING_FAILED"
BINANCE_TESTNET_CREDENTIALS_PRESENT_MASKED = "BINANCE_TESTNET_CREDENTIALS_PRESENT_MASKED"
BINANCE_TESTNET_ORDER_BLOCKED_BY_GUARD = "BINANCE_TESTNET_ORDER_BLOCKED_BY_GUARD"
BINANCE_TESTNET_LIVE_ENDPOINT_REJECTED = "BINANCE_TESTNET_LIVE_ENDPOINT_REJECTED"
BINANCE_TESTNET_READ_ONLY_ONLY = "BINANCE_TESTNET_READ_ONLY_ONLY"
BINANCE_TESTNET_DRY_RUN_PREVIEW_ONLY = "BINANCE_TESTNET_DRY_RUN_PREVIEW_ONLY"

USD_M_FUTURES_TESTNET = "USD_M_FUTURES_TESTNET"
DEFAULT_REST_BASE_URL = "https://demo-fapi.binance.com"
DEFAULT_API_KEY_ENV = "BINANCE_TESTNET_API_KEY"
DEFAULT_API_SECRET_ENV = "BINANCE_TESTNET_API_SECRET"

_LIVE_REST_HOSTS = {"api.binance.com", "fapi.binance.com"}
_LIVE_WEBSOCKET_HOSTS = {"stream.binance.com", "fstream.binance.com", "fstream-auth.binance.com"}
_ALLOWED_TESTNET_HOSTS = {"demo-fapi.binance.com", "testnet.binancefuture.com"}
_TRUE_VALUES = {"1", "true", "yes", "y", "on"}
_FALSE_VALUES = {"0", "false", "no", "n", "off", ""}


class HttpClient(Protocol):
    """Minimal injectable HTTP client protocol used by this scaffold."""

    def get(self, url: str, **kwargs: Any) -> Any:
        """Fetch a URL and return a client-specific response object."""


class UrllibHttpClient:
    """Tiny stdlib HTTP client for public testnet connectivity checks."""

    def get(self, url: str, **kwargs: Any) -> Any:
        timeout = float(kwargs.get("timeout", 10))
        with urlopen(url, timeout=timeout) as response:  # nosec B310 - URL is validated before use.
            body = response.read().decode("utf-8")
            try:
                parsed_body: Any = json.loads(body) if body else {}
            except json.JSONDecodeError:
                parsed_body = body
            return {"status_code": getattr(response, "status", None), "body": parsed_body}


@dataclass(frozen=True)
class BinanceTestnetConfig:
    enabled: bool = False
    broker_mode: str = ""
    product: str = USD_M_FUTURES_TESTNET
    rest_base_url: str = DEFAULT_REST_BASE_URL
    api_key: str = ""
    api_secret: str = ""
    api_key_env: str = DEFAULT_API_KEY_ENV
    api_secret_env: str = DEFAULT_API_SECRET_ENV
    allow_testnet_order: bool = False
    allow_auto_testnet_order: bool = False
    real_binance_enabled: bool = False
    allow_real_binance_order: bool = False
    order_placement_enabled: bool = False
    auto_order_enabled: bool = False
    signed_read_only_enabled: bool = False
    max_notional_usdt: Optional[float] = 25.0
    max_orders_per_day: Optional[int] = None
    default_leverage: Optional[int] = None
    dry_run: bool = True
    env_found: bool = False


@dataclass(frozen=True)
class BinanceTestnetAuditResult:
    status: str
    enabled: bool
    broker_mode: str
    rest_base_url: str
    api_key_present: bool
    api_secret_present: bool
    api_key_masked: str
    api_secret_masked: str
    allow_testnet_order: bool
    allow_auto_testnet_order: bool
    real_binance_enabled: bool
    allow_real_binance_order: bool
    max_notional_usdt: Optional[float]
    max_orders_per_day: Optional[int]
    default_leverage: Optional[int]
    public_ping_status: str
    exchange_info_status: str
    order_placement_status: str
    findings: list[str]
    recommendation: str

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def mask_secret(value: Any) -> str:
    """Return a non-reversible display mask for a secret-like value."""

    if value is None or str(value) == "":
        return ""
    text = str(value)
    if len(text) <= 8:
        return "***"
    return f"{text[:2]}***{text[-2:]}"


def load_dotenv_values(path: str = ".env") -> dict[str, str]:
    """Load simple KEY=VALUE entries from .env without exporting or logging them."""

    dotenv_path = Path(path)
    if not dotenv_path.exists() or not dotenv_path.is_file():
        return {}

    values: dict[str, str] = {}
    for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def _merged_env(env: Optional[Mapping[str, Any]], dotenv_path: str) -> dict[str, Any]:
    dotenv_values = load_dotenv_values(dotenv_path)
    source = os.environ if env is None else env
    merged: dict[str, Any] = dict(dotenv_values)
    for key, value in source.items():
        if value is not None:
            merged[key] = value
    return merged


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    return default


def _as_optional_float(value: Any, default: Optional[float]) -> Optional[float]:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_optional_int(value: Any, default: Optional[int]) -> Optional[int]:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _is_testnet_broker_mode(value: Any) -> bool:
    return str(value or "").strip().lower() in {"testnet", "binance_testnet", "futures_testnet", "demo"}


def load_binance_testnet_config(env: Optional[Mapping[str, Any]] = None, dotenv_path: str = ".env") -> BinanceTestnetConfig:
    """Load Binance testnet config from process env first, then missing .env values.

    Existing variable names are used; no new credentials are created.  Secret
    values are held only for presence checks / future signed-read-only injection
    and are never included in validation or audit output without masking.
    """

    values = _merged_env(env, dotenv_path)
    broker_mode = str(values.get("BROKER_MODE", ""))
    allow_testnet_order = _as_bool(values.get("ALLOW_TESTNET_ORDER"), False)
    dry_run = not allow_testnet_order
    enabled = _is_testnet_broker_mode(broker_mode) or allow_testnet_order
    order_placement_enabled = bool(allow_testnet_order and not dry_run)
    allow_auto_testnet_order = _as_bool(values.get("ALLOW_AUTO_TESTNET_ORDER"), False)

    return BinanceTestnetConfig(
        enabled=enabled,
        broker_mode=broker_mode,
        rest_base_url=str(values.get("BINANCE_FUTURES_TESTNET_BASE_URL", DEFAULT_REST_BASE_URL)),
        api_key=str(values.get(DEFAULT_API_KEY_ENV, "")),
        api_secret=str(values.get(DEFAULT_API_SECRET_ENV, "")),
        allow_testnet_order=allow_testnet_order,
        allow_auto_testnet_order=allow_auto_testnet_order,
        real_binance_enabled=_as_bool(values.get("REAL_BINANCE_ENABLED"), False),
        allow_real_binance_order=_as_bool(values.get("ALLOW_REAL_BINANCE_ORDER"), False),
        order_placement_enabled=order_placement_enabled,
        auto_order_enabled=bool(allow_auto_testnet_order and order_placement_enabled),
        signed_read_only_enabled=_as_bool(values.get("BINANCE_TESTNET_SIGNED_READ_ONLY_ENABLED"), False),
        max_notional_usdt=_as_optional_float(values.get("TESTNET_MAX_NOTIONAL_USDT"), 25.0),
        max_orders_per_day=_as_optional_int(values.get("TESTNET_MAX_ORDERS_PER_DAY"), None),
        default_leverage=_as_optional_int(values.get("TESTNET_DEFAULT_LEVERAGE"), None),
        dry_run=dry_run,
        env_found=any(key in values for key in (DEFAULT_API_KEY_ENV, DEFAULT_API_SECRET_ENV, "BINANCE_FUTURES_TESTNET_BASE_URL")),
    )


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def _is_live_endpoint(url: str) -> bool:
    host = _host(url)
    return host in _LIVE_REST_HOSTS or host in _LIVE_WEBSOCKET_HOSTS


def _is_allowed_testnet_endpoint(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    scheme = (parsed.scheme or "").lower()
    return scheme in {"https", "http"} and host in _ALLOWED_TESTNET_HOSTS


def validate_binance_testnet_config(config: BinanceTestnetConfig) -> dict[str, Any]:
    """Validate scaffold config and return a masked, non-secret status object."""

    findings: list[str] = []
    status = BINANCE_TESTNET_CONFIG_VALID

    if config.env_found:
        findings.append(BINANCE_TESTNET_ENV_FOUND)
    if not config.enabled:
        status = BINANCE_TESTNET_DISABLED
        findings.append(BINANCE_TESTNET_DISABLED)
    if not config.rest_base_url:
        findings.append("missing_testnet_base_url")
    if _is_live_endpoint(config.rest_base_url) or not _is_allowed_testnet_endpoint(config.rest_base_url):
        status = BINANCE_TESTNET_LIVE_ENDPOINT_REJECTED
        findings.append(BINANCE_TESTNET_LIVE_ENDPOINT_REJECTED)
    if config.real_binance_enabled:
        findings.append("REAL_BINANCE_ENABLED_danger_flag_true")
    if config.allow_real_binance_order:
        findings.append("ALLOW_REAL_BINANCE_ORDER_danger_flag_true")
    if config.allow_auto_testnet_order and not config.order_placement_enabled:
        findings.append("auto_testnet_order_ignored_without_order_guard")
    if config.order_placement_enabled and config.dry_run:
        findings.append("order_placement_requires_dry_run_false")
    if config.order_placement_enabled and not config.allow_testnet_order:
        findings.append("order_placement_requires_ALLOW_TESTNET_ORDER")
    if config.order_placement_enabled and (not config.api_key or not config.api_secret):
        findings.append("order_guard_requires_credentials_present")
    if config.max_notional_usdt is not None and config.max_notional_usdt <= 0:
        findings.append("max_notional_must_be_positive")
    if config.api_key or config.api_secret:
        findings.append(BINANCE_TESTNET_CREDENTIALS_PRESENT_MASKED)

    invalid_findings = {
        "missing_testnet_base_url",
        "REAL_BINANCE_ENABLED_danger_flag_true",
        "ALLOW_REAL_BINANCE_ORDER_danger_flag_true",
        "order_placement_requires_dry_run_false",
        "order_placement_requires_ALLOW_TESTNET_ORDER",
        "order_guard_requires_credentials_present",
        "max_notional_must_be_positive",
    }
    if status not in {BINANCE_TESTNET_LIVE_ENDPOINT_REJECTED, BINANCE_TESTNET_DISABLED} and any(item in invalid_findings for item in findings):
        status = BINANCE_TESTNET_CONFIG_INVALID

    return {
        "ok": status == BINANCE_TESTNET_CONFIG_VALID,
        "status": status,
        "enabled": config.enabled,
        "broker_mode": config.broker_mode,
        "product": config.product,
        "rest_base_url": config.rest_base_url,
        "api_key_present": bool(config.api_key),
        "api_secret_present": bool(config.api_secret),
        "api_key_masked": mask_secret(config.api_key),
        "api_secret_masked": mask_secret(config.api_secret),
        "allow_testnet_order": config.allow_testnet_order,
        "allow_auto_testnet_order": config.allow_auto_testnet_order,
        "real_binance_enabled": config.real_binance_enabled,
        "allow_real_binance_order": config.allow_real_binance_order,
        "order_placement_enabled": config.order_placement_enabled,
        "auto_order_enabled": config.auto_order_enabled,
        "dry_run": config.dry_run,
        "max_notional_usdt": config.max_notional_usdt,
        "max_orders_per_day": config.max_orders_per_day,
        "default_leverage": config.default_leverage,
        "findings": findings,
        "recommendation": "keep_disabled_paper_only" if findings else "public_testnet_connectivity_check_only",
    }


class BinanceTestnetAdapter:
    """Offline-testable Binance futures testnet adapter scaffold."""

    def __init__(self, config: Optional[BinanceTestnetConfig] = None, http_client: Optional[HttpClient] = None) -> None:
        self.config = config or BinanceTestnetConfig()
        self.http_client = http_client

    def _guard(self) -> dict[str, Any]:
        validation = validate_binance_testnet_config(self.config)
        if not self.config.enabled:
            return {"ok": False, "status": BINANCE_TESTNET_DISABLED, "validation": validation}
        if not validation["ok"]:
            return {"ok": False, "status": validation["status"], "validation": validation}
        return {"ok": True, "status": BINANCE_TESTNET_CONFIG_VALID, "validation": validation}

    def _public_get(self, path: str, params: Optional[Mapping[str, Any]] = None) -> Any:
        if self.http_client is None:
            return {"offline": True}
        url = f"{self.config.rest_base_url.rstrip('/')}{path}"
        if params:
            query = "&".join(f"{key}={value}" for key, value in params.items() if value is not None)
            if query:
                url = f"{url}?{query}"
        return self.http_client.get(url, timeout=10)

    def ping(self) -> dict[str, Any]:
        guard = self._guard()
        if not guard["ok"]:
            return guard
        try:
            response = self._public_get("/fapi/v1/ping")
            return {"ok": True, "status": BINANCE_TESTNET_PUBLIC_PING_OK, "response": response}
        except Exception as exc:  # pragma: no cover - exercised through CLI/network conditions.
            return {"ok": False, "status": BINANCE_TESTNET_PUBLIC_PING_FAILED, "error": type(exc).__name__}

    def exchange_info(self, symbol: Optional[str] = None) -> dict[str, Any]:
        guard = self._guard()
        if not guard["ok"]:
            return guard
        try:
            response = self._public_get("/fapi/v1/exchangeInfo", {"symbol": symbol} if symbol else None)
            return {"ok": True, "status": BINANCE_TESTNET_CONFIG_VALID, "response": response}
        except Exception as exc:  # pragma: no cover - exercised through CLI/network conditions.
            return {"ok": False, "status": BINANCE_TESTNET_PUBLIC_PING_FAILED, "error": type(exc).__name__}

    def account_status_preview(self) -> dict[str, Any]:
        validation = validate_binance_testnet_config(self.config)
        return {
            "ok": validation["ok"],
            "status": BINANCE_TESTNET_CREDENTIALS_PRESENT_MASKED if (self.config.api_key or self.config.api_secret) else BINANCE_TESTNET_DISABLED,
            "api_key_present": bool(self.config.api_key),
            "api_secret_present": bool(self.config.api_secret),
            "api_key_masked": mask_secret(self.config.api_key),
            "api_secret_masked": mask_secret(self.config.api_secret),
        }

    def signed_account_read_only(self) -> dict[str, Any]:
        validation = validate_binance_testnet_config(self.config)
        if not validation["ok"] or not self.config.signed_read_only_enabled or self.http_client is None:
            return {"ok": False, "status": BINANCE_TESTNET_READ_ONLY_ONLY, "validation": validation}
        return {
            "ok": False,
            "status": BINANCE_TESTNET_READ_ONLY_ONLY,
            "reason": "signed_account_read_only_requires_future_signature_implementation",
        }

    def place_order_preview(self, order_request: Mapping[str, Any]) -> dict[str, Any]:
        validation = validate_binance_testnet_config(self.config)
        if not validation["ok"]:
            return {"ok": False, "status": validation["status"], "validation": validation}
        return {
            "ok": True,
            "status": BINANCE_TESTNET_DRY_RUN_PREVIEW_ONLY,
            "dry_run": True,
            "would_place_order": False,
            "order_request": dict(order_request),
            "max_notional_usdt": self.config.max_notional_usdt,
        }

    def place_testnet_order(self, order_request: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "ok": False,
            "status": BINANCE_TESTNET_ORDER_BLOCKED_BY_GUARD,
            "reason": "testnet_order_placement_not_implemented_in_this_pr",
            "order_request": dict(order_request),
        }


def run_binance_testnet_audit(
    dotenv_path: str = ".env",
    report_path: str = "reports/binance_testnet_audit.json",
    http_client: Optional[HttpClient] = None,
    run_public_checks: bool = True,
) -> BinanceTestnetAuditResult:
    """Run a safe Binance testnet audit and write a masked JSON report."""

    config = load_binance_testnet_config(dotenv_path=dotenv_path)
    client = http_client if http_client is not None else UrllibHttpClient()
    adapter = BinanceTestnetAdapter(config=config, http_client=client)
    validation = validate_binance_testnet_config(config)

    ping_status = BINANCE_TESTNET_DISABLED
    exchange_status = BINANCE_TESTNET_DISABLED
    if run_public_checks and config.enabled and validation["ok"]:
        ping_status = adapter.ping()["status"]
        exchange_status = adapter.exchange_info()["status"]

    order_status = adapter.place_testnet_order({})["status"]
    result = BinanceTestnetAuditResult(
        status=validation["status"],
        enabled=validation["enabled"],
        broker_mode=validation["broker_mode"],
        rest_base_url=validation["rest_base_url"],
        api_key_present=validation["api_key_present"],
        api_secret_present=validation["api_secret_present"],
        api_key_masked=validation["api_key_masked"],
        api_secret_masked=validation["api_secret_masked"],
        allow_testnet_order=validation["allow_testnet_order"],
        allow_auto_testnet_order=validation["allow_auto_testnet_order"],
        real_binance_enabled=validation["real_binance_enabled"],
        allow_real_binance_order=validation["allow_real_binance_order"],
        max_notional_usdt=validation["max_notional_usdt"],
        max_orders_per_day=validation["max_orders_per_day"],
        default_leverage=validation["default_leverage"],
        public_ping_status=ping_status,
        exchange_info_status=exchange_status,
        order_placement_status=order_status,
        findings=validation["findings"],
        recommendation=validation["recommendation"],
    )

    output_path = Path(report_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result
