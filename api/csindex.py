from __future__ import annotations

import asyncio
from datetime import date, datetime
import math
from typing import Any

import requests
from fastapi import HTTPException
from starlette.concurrency import run_in_threadpool


CSINDEX_HISTORY_URL = "https://www.csindex.com.cn/csindex-home/perf/index-perf"
CSINDEX_HISTORY_TIMEOUT_SECONDS = 25
CSINDEX_HISTORY_MAX_DAYS = 400
CSINDEX_HISTORY_SEMAPHORE = asyncio.Semaphore(2)
SUPPORTED_INDEXES = frozenset({"H30269", "H20269"})


class CsIndexHistoryError(RuntimeError):
    pass


def _parse_date(value: Any, field_name: str) -> date:
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(status_code=422, detail=f"{field_name} is required")
    try:
        return date.fromisoformat(value.strip())
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"{field_name} must use YYYY-MM-DD",
        ) from exc


def parse_history_request(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="Request body must be a JSON object")

    index_code = payload.get("index_code")
    if not isinstance(index_code, str) or index_code.strip().upper() not in SUPPORTED_INDEXES:
        raise HTTPException(
            status_code=422,
            detail={"message": "Unsupported CSIndex code", "allowed": sorted(SUPPORTED_INDEXES)},
        )
    start_date = _parse_date(payload.get("start_date"), "start_date")
    end_date = _parse_date(payload.get("end_date"), "end_date")
    if start_date > end_date:
        raise HTTPException(status_code=422, detail="start_date must be <= end_date")
    if (end_date - start_date).days > CSINDEX_HISTORY_MAX_DAYS:
        raise HTTPException(
            status_code=422,
            detail=f"date range must not exceed {CSINDEX_HISTORY_MAX_DAYS} days",
        )
    return {
        "index_code": index_code.strip().upper(),
        "start_date": start_date,
        "end_date": end_date,
    }


def _parse_trade_date(value: Any, index_code: str) -> str:
    raw = str(value or "").strip()
    if len(raw) != 8 or not raw.isdigit():
        raise CsIndexHistoryError(f"CSIndex {index_code} returned an invalid tradeDate")
    try:
        return date.fromisoformat(
            f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
        ).isoformat()
    except ValueError as exc:
        raise CsIndexHistoryError(f"CSIndex {index_code} returned an invalid tradeDate") from exc


def _fetch_history(parameters: dict[str, Any]) -> list[dict[str, Any]]:
    index_code = parameters["index_code"]
    start_date: date = parameters["start_date"]
    end_date: date = parameters["end_date"]
    try:
        response = requests.get(
            CSINDEX_HISTORY_URL,
            params={
                "indexCode": index_code,
                "startDate": start_date.strftime("%Y%m%d"),
                "endDate": end_date.strftime("%Y%m%d"),
            },
            headers={
                "Accept": "application/json",
                "User-Agent": "investment-portfolio-datahub/1.0",
            },
            timeout=CSINDEX_HISTORY_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        raise CsIndexHistoryError(
            f"CSIndex {index_code} upstream request failed: {type(exc).__name__}"
        ) from exc
    except ValueError as exc:
        raise CsIndexHistoryError(f"CSIndex {index_code} returned invalid JSON") from exc

    if not isinstance(payload, dict):
        raise CsIndexHistoryError(f"CSIndex {index_code} response must be an object")
    if str(payload.get("code", "")) != "200" or payload.get("success") is not True:
        raise CsIndexHistoryError(f"CSIndex {index_code} returned an unsuccessful response")
    raw_rows = payload.get("data")
    if not isinstance(raw_rows, list):
        raise CsIndexHistoryError(f"CSIndex {index_code} response has no data rows")

    rows: dict[str, dict[str, Any]] = {}
    for raw in raw_rows:
        if not isinstance(raw, dict):
            raise CsIndexHistoryError(f"CSIndex {index_code} returned an invalid row")
        if str(raw.get("indexCode", "")).strip() != index_code:
            raise CsIndexHistoryError(f"CSIndex {index_code} returned a different index code")
        trade_date = _parse_trade_date(raw.get("tradeDate"), index_code)
        if not start_date.isoformat() <= trade_date <= end_date.isoformat():
            continue
        try:
            close = float(raw.get("close", "nan"))
        except (TypeError, ValueError):
            close = math.nan
        if not math.isfinite(close) or close <= 0:
            raise CsIndexHistoryError(f"CSIndex {index_code} close is invalid for {trade_date}")
        rows[trade_date] = {"date": trade_date, "close": close}
    return [rows[key] for key in sorted(rows)]


async def fetch_csindex_history(parameters: dict[str, Any]) -> list[dict[str, Any]]:
    async with CSINDEX_HISTORY_SEMAPHORE:
        return await run_in_threadpool(_fetch_history, parameters)
