from __future__ import annotations

from datetime import datetime
from math import isfinite
from typing import Any

from pytdx.hq import TdxHq_API


ETF_MARKETS = {
    "159985": 0,
    "512890": 1,
    "513100": 1,
    "513500": 1,
    "518880": 1,
}

# Alternate servers on the same TongdaXin protocol, not alternate data sources.
TDX_SERVERS = (
    ("shanghai-z80", "180.153.18.172", 80),
    ("beijing-z80", "202.108.253.139", 80),
    ("shanghai-z1", "180.153.18.170", 7709),
    ("hangzhou-j1", "60.191.117.167", 7709),
    ("hangzhou-j2", "115.238.56.198", 7709),
    ("hangzhou-j3", "218.75.126.9", 7709),
    ("hangzhou-j4", "115.238.90.165", 7709),
    ("ningbo-j2", "60.12.136.250", 7709),
    ("guangfa-g1", "59.36.5.11", 7709),
)

DAILY_CATEGORY = 9
PAGE_SIZE = 800
MAX_PAGES = 20


class TdxDataError(RuntimeError):
    pass


def _number(value: Any, label: str, *, positive: bool = False) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise TdxDataError(f"TDX {label} is not numeric") from exc
    if not isfinite(parsed) or (positive and parsed <= 0):
        raise TdxDataError(f"TDX {label} is invalid")
    return parsed


def _normalize_bar(raw: dict[str, Any], symbol: str) -> dict[str, Any]:
    raw_datetime = str(raw.get("datetime", ""))
    try:
        trade_date = datetime.strptime(raw_datetime[:10], "%Y-%m-%d").date().isoformat()
    except ValueError as exc:
        raise TdxDataError(f"TDX {symbol} date is invalid") from exc

    open_price = _number(raw.get("open"), f"{symbol} open", positive=True)
    high = _number(raw.get("high"), f"{symbol} high", positive=True)
    low = _number(raw.get("low"), f"{symbol} low", positive=True)
    close = _number(raw.get("close"), f"{symbol} close", positive=True)
    volume_lots = _number(raw.get("vol"), f"{symbol} volume")
    amount = _number(raw.get("amount"), f"{symbol} amount")
    if high < low or volume_lots < 0 or amount < 0:
        raise TdxDataError(f"TDX {symbol} OHLCV values are invalid")

    return {
        "date": trade_date,
        "open": open_price,
        "high": high,
        "low": low,
        "close": close,
        # TDX reports exchange volume in lots. Turso stores shares.
        "volume": int(round(volume_lots * 100)),
        "total_turnover": int(round(amount)),
    }


def _fetch_symbol(
    api: TdxHq_API,
    market: int,
    symbol: str,
    start_date: str,
    end_date: str,
) -> list[dict[str, Any]]:
    rows_by_date: dict[str, dict[str, Any]] = {}
    received_any = False
    for page in range(MAX_PAGES):
        raw_rows = api.get_security_bars(
            DAILY_CATEGORY,
            market,
            symbol,
            page * PAGE_SIZE,
            PAGE_SIZE,
        ) or []
        if not raw_rows:
            break
        received_any = True
        normalized = [_normalize_bar(dict(raw), symbol) for raw in raw_rows]
        for row in normalized:
            if start_date <= row["date"] <= end_date:
                rows_by_date[row["date"]] = row

        oldest_date = min(row["date"] for row in normalized)
        if oldest_date <= start_date or len(raw_rows) < PAGE_SIZE:
            break
    else:
        raise TdxDataError(f"TDX {symbol} history exceeds {MAX_PAGES * PAGE_SIZE} rows")

    if not received_any:
        raise TdxDataError(f"TDX returned no history for {symbol}")
    return [rows_by_date[key] for key in sorted(rows_by_date)]


def _fetch_batch_from_server(
    host: str,
    port: int,
    requests: list[dict[str, str]],
) -> dict[str, list[dict[str, Any]]]:
    api = TdxHq_API(heartbeat=False, auto_retry=False, raise_exception=True)
    connected = False
    try:
        connected = bool(api.connect(host, port, time_out=4))
        if not connected:
            raise TdxDataError("TDX connection was rejected")
        results: dict[str, list[dict[str, Any]]] = {}
        for item in requests:
            symbol = item["symbol"]
            results[symbol] = _fetch_symbol(
                api,
                ETF_MARKETS[symbol],
                symbol,
                item["start_date"],
                item["end_date"],
            )
        return results
    finally:
        if connected:
            api.disconnect()


def fetch_tdx_etf_batch(
    requests: list[dict[str, str]],
) -> tuple[str, dict[str, list[dict[str, Any]]]]:
    if not requests:
        return "none", {}
    if len(requests) > len(ETF_MARKETS):
        raise TdxDataError("TDX ETF batch contains too many requests")
    symbols = [item.get("symbol", "") for item in requests]
    if len(set(symbols)) != len(symbols):
        raise TdxDataError("TDX ETF batch contains duplicate symbols")
    unknown = [symbol for symbol in symbols if symbol not in ETF_MARKETS]
    if unknown:
        raise TdxDataError(f"TDX ETFs are not registered: {unknown}")

    failures: list[str] = []
    for name, host, port in TDX_SERVERS:
        try:
            return name, _fetch_batch_from_server(host, port, requests)
        except Exception as exc:
            failures.append(f"{name}:{type(exc).__name__}")
    raise TdxDataError(
        f"TDX batch failed on all {len(TDX_SERVERS)} servers ({', '.join(failures)})"
    )


def fetch_tdx_etf_bars(
    symbol: str,
    start_date: str,
    end_date: str,
) -> tuple[str, list[dict[str, Any]]]:
    server, results = fetch_tdx_etf_batch([
        {"symbol": symbol, "start_date": start_date, "end_date": end_date}
    ])
    return server, results[symbol]
