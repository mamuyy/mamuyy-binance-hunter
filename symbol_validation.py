import os
from dataclasses import dataclass
from typing import Any

DEFAULT_POLICY_DENYLIST = {
    "XAUUSDT", "XAGUSDT", "SOXLUSDT", "MRVLUSDT", "SNDKUSDT", "MUUSDT", "INTCUSDT", "SKHYNIXUSDT",
}
SUPPORTED_CONTRACT_TYPES = {"PERPETUAL"}

@dataclass(frozen=True)
class SymbolValidationResult:
    symbol: str
    valid: bool
    reason: str | None = None


def policy_denylist() -> set[str]:
    extra = {s.strip().upper() for s in os.getenv("SYMBOL_POLICY_DENYLIST", "").split(",") if s.strip()}
    return DEFAULT_POLICY_DENYLIST | extra


def validate_symbol(symbol: str, exchange_info: dict[str, Any] | None = None) -> SymbolValidationResult:
    sym = (symbol or "").upper().strip()
    if not sym:
        return SymbolValidationResult(sym, False, "SYMBOL_NOT_FOUND")
    if sym in policy_denylist():
        return SymbolValidationResult(sym, False, "POLICY_DENYLIST")
    if exchange_info is None:
        return SymbolValidationResult(sym, True)
    row = None
    for item in exchange_info.get("symbols", []) or []:
        if str(item.get("symbol", "")).upper() == sym:
            row = item
            break
    if row is None:
        return SymbolValidationResult(sym, False, "SYMBOL_NOT_FOUND")
    if str(row.get("status", "")).upper() != "TRADING":
        return SymbolValidationResult(sym, False, "SYMBOL_NOT_TRADING")
    if str(row.get("quoteAsset", "")).upper() != "USDT":
        return SymbolValidationResult(sym, False, "UNSUPPORTED_QUOTE_ASSET")
    if str(row.get("contractType", "")).upper() not in SUPPORTED_CONTRACT_TYPES:
        return SymbolValidationResult(sym, False, "UNSUPPORTED_CONTRACT")
    return SymbolValidationResult(sym, True)
