import hashlib
import hmac
import os
import time
import urllib.parse
from typing import Any, Dict, Optional

import requests


DEMO_FUTURES_BASE_URL = "https://demo-fapi.binance.com"
REAL_FUTURES_BASE_URL = "https://fapi.binance.com"
REAL_SPOT_BASE_URL_FRAGMENT = "api.binance.com"
DEFAULT_RECV_WINDOW = 10000


class BinanceFuturesTestnetClientError(RuntimeError):
    pass


def load_dotenv_file(path: str = ".env") -> None:
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as env_file:
        for raw_line in env_file:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if key.startswith("export "):
                key = key[7:].strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def assert_testnet_base_url(base_url: str) -> str:
    normalized = (base_url or "").strip().rstrip("/")
    if normalized == DEMO_FUTURES_BASE_URL:
        return normalized
    if REAL_FUTURES_BASE_URL in normalized:
        raise BinanceFuturesTestnetClientError("Refusing real Binance Futures base URL.")
    if REAL_SPOT_BASE_URL_FRAGMENT in normalized:
        raise BinanceFuturesTestnetClientError("Refusing Binance production API base URL.")
    if normalized != DEMO_FUTURES_BASE_URL:
        raise BinanceFuturesTestnetClientError(
            f"Refusing non-demo Binance Futures base URL: {normalized or '<empty>'}"
        )
    return normalized


class BinanceFuturesTestnetClient:
    def __init__(
        self,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: int = 15,
    ) -> None:
        load_dotenv_file()
        self.base_url = assert_testnet_base_url(
            base_url or os.getenv("BINANCE_FUTURES_TESTNET_BASE_URL", DEMO_FUTURES_BASE_URL)
        )
        self.api_key = api_key if api_key is not None else os.getenv("BINANCE_TESTNET_API_KEY", "")
        self.api_secret = api_secret if api_secret is not None else os.getenv("BINANCE_TESTNET_API_SECRET", "")
        self.timeout = timeout

    def _headers(self, signed: bool = False) -> Dict[str, str]:
        if not signed:
            return {}
        if not self.api_key or not self.api_secret:
            raise BinanceFuturesTestnetClientError("Missing Binance Futures Testnet API credentials.")
        return {"X-MBX-APIKEY": self.api_key}

    def _sign_params(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self.api_secret:
            raise BinanceFuturesTestnetClientError("Missing Binance Futures Testnet API secret.")
        signed_params = dict(params)
        signed_params["timestamp"] = int(time.time() * 1000)
        signed_params["recvWindow"] = DEFAULT_RECV_WINDOW
        query_string = urllib.parse.urlencode(signed_params, doseq=True)
        signature = hmac.new(
            self.api_secret.encode("utf-8"),
            query_string.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        signed_params["signature"] = signature
        return signed_params

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        signed: bool = False,
    ) -> Any:
        request_params = dict(params or {})
        if signed:
            request_params = self._sign_params(request_params)
        url = f"{self.base_url}{path}"
        try:
            response = requests.request(
                method=method,
                url=url,
                params=request_params,
                headers=self._headers(signed=signed),
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise BinanceFuturesTestnetClientError(
                f"Binance Futures Testnet request failed before response: {exc}"
            ) from exc
        try:
            payload = response.json()
        except ValueError:
            payload = {"raw": response.text}
        if response.status_code >= 400:
            raise BinanceFuturesTestnetClientError(
                f"Binance Futures Testnet request failed ({response.status_code}): {payload}"
            )
        return payload

    def get_server_time(self) -> Dict[str, Any]:
        return self._request("GET", "/fapi/v1/time")

    def get_exchange_info(self) -> Dict[str, Any]:
        return self._request("GET", "/fapi/v1/exchangeInfo")

    def get_account(self) -> Dict[str, Any]:
        return self._request("GET", "/fapi/v2/account", signed=True)

    def get_position_risk(self, symbol: Optional[str] = None) -> Any:
        params: Dict[str, Any] = {}
        if symbol:
            params["symbol"] = symbol.upper()
        return self._request("GET", "/fapi/v2/positionRisk", params=params, signed=True)

    def get_mark_price(self, symbol: str) -> Optional[Any]:
        payload = self._request(
            "GET",
            "/fapi/v1/premiumIndex",
            params={"symbol": symbol.upper()},
        )
        if isinstance(payload, dict):
            return payload.get("markPrice")
        return None

    def get_ticker_price(self, symbol: str) -> Optional[Any]:
        payload = self._request(
            "GET",
            "/fapi/v1/ticker/price",
            params={"symbol": symbol.upper()},
        )
        if isinstance(payload, dict):
            return payload.get("price")
        return None

    def place_test_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        quantity: Any,
        price: Optional[Any] = None,
        reduce_only: bool = False,
    ) -> Dict[str, Any]:
        params = self._order_params(symbol, side, order_type, quantity, price, reduce_only)
        return self._request("POST", "/fapi/v1/order/test", params=params, signed=True)

    def place_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        quantity: Any,
        price: Optional[Any] = None,
        reduce_only: bool = False,
    ) -> Dict[str, Any]:
        params = self._order_params(symbol, side, order_type, quantity, price, reduce_only)
        return self._request("POST", "/fapi/v1/order", params=params, signed=True)

    def get_order(self, symbol: str, order_id: Any) -> Dict[str, Any]:
        return self._request(
            "GET",
            "/fapi/v1/order",
            params={"symbol": symbol.upper(), "orderId": order_id},
            signed=True,
        )

    def cancel_order(self, symbol: str, order_id: Any) -> Dict[str, Any]:
        return self._request(
            "DELETE",
            "/fapi/v1/order",
            params={"symbol": symbol.upper(), "orderId": order_id},
            signed=True,
        )

    @staticmethod
    def _order_params(
        symbol: str,
        side: str,
        order_type: str,
        quantity: Any,
        price: Optional[Any],
        reduce_only: bool,
    ) -> Dict[str, Any]:
        normalized_type = order_type.upper()
        params: Dict[str, Any] = {
            "symbol": symbol.upper(),
            "side": side.upper(),
            "type": normalized_type,
            "quantity": quantity,
        }
        if reduce_only:
            params["reduceOnly"] = "true"
        if normalized_type == "LIMIT":
            if price is None:
                raise BinanceFuturesTestnetClientError("LIMIT orders require price.")
            params["price"] = price
            params["timeInForce"] = "GTC"
        elif price is not None:
            params["price"] = price
        return params
