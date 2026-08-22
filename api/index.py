from __future__ import annotations

from datetime import datetime
from math import isfinite
from typing import Any

import akshare as ak
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware


SUPPORTED_ETFS = {"512890"}

app = FastAPI(
    title="Investment Portfolio DataHub",
    version="0.1.0",
    description="Minimal Vercel proxy for AKShare market data.",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


def normalize_date(value: str, field_name: str) -> str:
    normalized = value.strip().replace("-", "")
    try:
        parsed = datetime.strptime(normalized, "%Y%m%d").date()
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"{field_name} must use YYYYMMDD or YYYY-MM-DD",
        ) from exc
    return parsed.strftime("%Y%m%d")


def json_number(value: Any, field_name: str) -> float | int | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=502,
            detail=f"AKShare returned a non-numeric {field_name}",
        ) from exc
    if not isfinite(number):
        return None
    return int(number) if number.is_integer() else number


def json_date(value: Any) -> str:
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d")
    text = str(value).strip()
    if "T" in text:
        text = text.split("T", 1)[0]
    return text[:10]


def normalize_rows(frame: Any) -> list[dict[str, Any]]:
    required_columns = {
        "日期",
        "开盘",
        "收盘",
        "最高",
        "最低",
        "成交量",
        "成交额",
    }
    missing = required_columns.difference(frame.columns)
    if missing:
        raise HTTPException(
            status_code=502,
            detail=f"AKShare response is missing columns: {sorted(missing)}",
        )

    rows: list[dict[str, Any]] = []
    for raw in frame.to_dict(orient="records"):
        rows.append(
            {
                "date": json_date(raw["日期"]),
                "open": json_number(raw["开盘"], "open"),
                "high": json_number(raw["最高"], "high"),
                "low": json_number(raw["最低"], "low"),
                "close": json_number(raw["收盘"], "close"),
                "volume": json_number(raw["成交量"], "volume"),
                "total_turnover": json_number(raw["成交额"], "total_turnover"),
            }
        )
    return rows


@app.get("/health", include_in_schema=True)
@app.get("/api/health", include_in_schema=False)
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "service": "investment-portfolio-datahub",
        "source": "akshare",
        "akshare_version": ak.__version__,
    }


@app.get("/etf/{symbol}", include_in_schema=True)
@app.get("/api/etf/{symbol}", include_in_schema=False)
def etf_daily(
    symbol: str,
    start_date: str = Query("20200101"),
    end_date: str = Query("20500101"),
    adjust: str = Query(""),
) -> dict[str, Any]:
    if symbol not in SUPPORTED_ETFS:
        raise HTTPException(
            status_code=400,
            detail=f"MVP only supports: {sorted(SUPPORTED_ETFS)}",
        )

    normalized_start = normalize_date(start_date, "start_date")
    normalized_end = normalize_date(end_date, "end_date")
    if normalized_start > normalized_end:
        raise HTTPException(status_code=422, detail="start_date must be <= end_date")

    try:
        frame = ak.fund_etf_hist_em(
            symbol=symbol,
            period="daily",
            start_date=normalized_start,
            end_date=normalized_end,
            adjust=adjust,
        )
        rows = normalize_rows(frame)
    except HTTPException:
        raise
    except Exception as exc:
        # Do not expose upstream URLs or credentials in the public response.
        raise HTTPException(
            status_code=502,
            detail=f"AKShare request failed: {type(exc).__name__}",
        ) from exc

    return {
        "ok": True,
        "source": "akshare.fund_etf_hist_em",
        "symbol": symbol,
        "start_date": normalized_start,
        "end_date": normalized_end,
        "adjust": adjust,
        "count": len(rows),
        "data": rows,
    }
