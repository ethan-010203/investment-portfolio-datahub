from __future__ import annotations

from datetime import datetime
from math import isfinite
from typing import Any

import requests
import xlrd


SUPPORTED_INDEXES = {"H30269"}
CSINDEX_INDICATOR_URL = (
    "https://oss-ch.csindex.com.cn/static/html/csindex/public/"
    "uploads/file/autofile/indicator/{symbol}indicator.xls"
)


def fetch_dividend_yield(
    symbol: str,
    start_date: str,
    end_date: str,
) -> list[dict[str, Any]]:
    if symbol not in SUPPORTED_INDEXES:
        raise ValueError(f"CSIndex {symbol} is not registered")

    response = requests.get(
        CSINDEX_INDICATOR_URL.format(symbol=symbol),
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=30,
    )
    response.raise_for_status()
    workbook = xlrd.open_workbook(file_contents=response.content)
    sheet = workbook.sheet_by_index(0)
    if sheet.ncols < 10:
        raise ValueError("CSIndex indicator workbook has too few columns")

    rows_by_date: dict[str, dict[str, Any]] = {}
    for row_index in range(1, sheet.nrows):
        row = sheet.row_values(row_index)
        if str(row[1]).strip() != symbol:
            continue
        raw_date = str(row[0]).strip()
        try:
            trade_date = datetime.strptime(raw_date, "%Y%m%d").date().isoformat()
        except ValueError as exc:
            raise ValueError(f"CSIndex {symbol} returned an invalid date") from exc
        try:
            # Column 9 is D/P2, the free-float market-cap weighted dividend yield.
            value = float(row[9]) / 100.0
        except (TypeError, ValueError) as exc:
            raise ValueError(f"CSIndex {symbol} dividend yield is not numeric") from exc
        if not isfinite(value) or value <= 0:
            raise ValueError(f"CSIndex {symbol} dividend yield is invalid")
        if start_date <= trade_date <= end_date:
            rows_by_date[trade_date] = {"date": trade_date, "value": value}
    return [rows_by_date[key] for key in sorted(rows_by_date)]
