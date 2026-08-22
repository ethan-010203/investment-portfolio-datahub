from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
import asyncio
import inspect
import json
from math import isfinite
import os
import re
from typing import Any
from typing import get_args, get_origin

import akshare as ak
import requests
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.concurrency import run_in_threadpool

from api.tq_proxy import (
    TqConfigurationError,
    TqUpstreamError,
    fetch_tq_raw,
    parse_tq_request,
)
from api.security import require_request_auth


SUPPORTED_ETFS = {
    "159985",
    "512890",
    "513100",
    "513500",
    "518880",
    "511260",
    "511220",
}
EASTMONEY_BOND_RATES_URL = "https://datacenter.eastmoney.com/api/data/get"
EASTMONEY_BOND_RATES_TOKEN = "894050c76af8597a853f5b408b759f5d"
EASTMONEY_BOND_RATE_FIELDS = {
    "date": "SOLAR_DATE",
    "cn2": "EMM00588704",
    "cn5": "EMM00166462",
    "cn10": "EMM00166466",
    "cn30": "EMM00166469",
}
FUNCTION_NAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
BLOCKED_FUNCTIONS = {"set_token", "set_proxy", "set_config", "clear_cache"}


def positive_env_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return max(1, value)


UPSTREAM_CONCURRENCY = positive_env_int("AKSHARE_CONCURRENCY", 2)
UPSTREAM_RETRY_ATTEMPTS = positive_env_int("AKSHARE_RETRY_ATTEMPTS", 3)
UPSTREAM_SEMAPHORE = asyncio.Semaphore(UPSTREAM_CONCURRENCY)

app = FastAPI(
    title="Investment Portfolio DataHub",
    version="0.1.0",
    description="Minimal Vercel proxy for AKShare market data.",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
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


def jsonable(value: Any, depth: int = 0) -> Any:
    if depth > 20:
        raise HTTPException(status_code=502, detail="AKShare response is too deeply nested")
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if isfinite(value) else None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if hasattr(value, "columns") and hasattr(value, "to_dict"):
        return jsonable(value.to_dict(orient="records"), depth + 1)
    if hasattr(value, "to_dict") and not isinstance(value, Mapping):
        return jsonable(value.to_dict(), depth + 1)
    if hasattr(value, "tolist"):
        return jsonable(value.tolist(), depth + 1)
    if isinstance(value, Mapping):
        return {str(key): jsonable(item, depth + 1) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [jsonable(item, depth + 1) for item in value]
    if hasattr(value, "item"):
        return jsonable(value.item(), depth + 1)
    return str(value)


def resolve_akshare_function(function_name: str) -> Any:
    if (
        not FUNCTION_NAME_PATTERN.fullmatch(function_name)
        or function_name.startswith("_")
        or function_name in BLOCKED_FUNCTIONS
    ):
        raise HTTPException(status_code=400, detail="Invalid AKShare function name")

    function = getattr(ak, function_name, None)
    if not callable(function):
        raise HTTPException(status_code=404, detail="AKShare function not found")
    module_name = getattr(function, "__module__", "")
    if module_name and not module_name.startswith("akshare"):
        raise HTTPException(status_code=400, detail="Function is not an AKShare data function")
    return function


def coerce_query_value(value: str, parameter: inspect.Parameter | None) -> Any:
    normalized = value.strip()
    if normalized.lower() == "null":
        return None

    default = inspect.Parameter.empty if parameter is None else parameter.default
    annotation = inspect.Parameter.empty if parameter is None else parameter.annotation
    origin = get_origin(annotation)
    args = get_args(annotation)
    target = annotation if origin is None else origin
    if target is bool or isinstance(default, bool):
        if normalized.lower() in {"true", "1", "yes", "y"}:
            return True
        if normalized.lower() in {"false", "0", "no", "n"}:
            return False
    if target is int or (isinstance(default, int) and not isinstance(default, bool)):
        try:
            return int(normalized)
        except ValueError:
            pass
    if target is float or isinstance(default, float):
        try:
            return float(normalized)
        except ValueError:
            pass
    if target in {list, tuple, dict} or list in args or tuple in args or dict in args:
        try:
            return json.loads(normalized)
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=422,
                detail="Complex query parameters must be valid JSON",
            ) from exc
    return value


def query_parameters(request: Request, function: Any) -> dict[str, Any]:
    try:
        signature = inspect.signature(function)
    except (TypeError, ValueError):
        signature = None
    parameters: dict[str, Any] = {}
    accepts_kwargs = bool(
        signature
        and any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in signature.parameters.values()
        )
    )
    for name in request.query_params.keys():
        parameter = signature.parameters.get(name) if signature else None
        if signature and parameter is None and not accepts_kwargs:
            raise HTTPException(status_code=422, detail=f"Unknown parameter: {name}")
        values = request.query_params.getlist(name)
        if len(values) > 1:
            parameters[name] = [coerce_query_value(item, parameter) for item in values]
        else:
            parameters[name] = coerce_query_value(values[0], parameter)
    return parameters


async def request_parameters(request: Request, function: Any) -> dict[str, Any]:
    parameters = query_parameters(request, function)
    if request.method == "POST":
        try:
            body = await request.json()
        except ValueError:
            body = {}
        if body is None:
            body = {}
        if not isinstance(body, dict):
            raise HTTPException(status_code=422, detail="POST body must be a JSON object")
        body_parameters = body.get("params", body)
        if not isinstance(body_parameters, dict):
            raise HTTPException(status_code=422, detail="params must be a JSON object")
        parameters.update(body_parameters)
    try:
        inspect.signature(function).bind_partial(**parameters)
    except TypeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return parameters


async def call_akshare(function: Any, parameters: dict[str, Any]) -> Any:
    async with UPSTREAM_SEMAPHORE:
        for attempt in range(UPSTREAM_RETRY_ATTEMPTS):
            try:
                return await run_in_threadpool(function, **parameters)
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
                if attempt + 1 >= UPSTREAM_RETRY_ATTEMPTS:
                    raise
                await asyncio.sleep(min(2.0, 0.35 * (2**attempt)))
    raise RuntimeError("AKShare call did not return")


def normalize_rows(
    frame: Any,
    *,
    volume_in_lots: bool,
    volume_round_to: int = 1,
    start_date: str | None = None,
    end_date: str | None = None,
) -> list[dict[str, Any]]:
    aliases = {
        "date": ("日期", "date"),
        "open": ("开盘", "open"),
        "close": ("收盘", "close"),
        "high": ("最高", "high"),
        "low": ("最低", "low"),
        "volume": ("成交量", "volume"),
        "total_turnover": ("成交额", "amount"),
    }
    columns = set(frame.columns)
    field_names = {
        field: next((name for name in names if name in columns), None)
        for field, names in aliases.items()
    }
    missing = {field for field, name in field_names.items() if name is None}
    if missing:
        raise HTTPException(
            status_code=502,
            detail=f"AKShare response is missing columns: {sorted(missing)}",
        )

    rows: list[dict[str, Any]] = []
    for raw in frame.to_dict(orient="records"):
        row_date = json_date(raw[field_names["date"]])
        row_date_key = row_date.replace("-", "")
        if start_date and row_date_key < start_date:
            continue
        if end_date and row_date_key > end_date:
            continue
        volume = json_number(raw[field_names["volume"]], "volume")
        if volume is not None and volume_in_lots:
            volume = int(round(volume * 100))
        if volume is not None and volume_round_to > 1:
            volume = int(round(volume / volume_round_to) * volume_round_to)
        rows.append(
            {
                "date": row_date,
                "open": json_number(raw[field_names["open"]], "open"),
                "high": json_number(raw[field_names["high"]], "high"),
                "low": json_number(raw[field_names["low"]], "low"),
                "close": json_number(raw[field_names["close"]], "close"),
                "volume": volume,
                "total_turnover": json_number(
                    raw[field_names["total_turnover"]], "total_turnover"
                ),
            }
        )
    return rows


async def fetch_normalized_etf(
    symbol: str,
    start_date: str,
    end_date: str,
    adjust: str,
) -> tuple[str, list[dict[str, Any]]]:
    frame = await call_akshare(
        ak.fund_etf_hist_em,
        {
            "symbol": symbol,
            "period": "daily",
            "start_date": start_date,
            "end_date": end_date,
            "adjust": adjust,
        },
    )
    return (
        "akshare.fund_etf_hist_em",
        normalize_rows(
            frame,
            volume_in_lots=True,
            volume_round_to=100,
            start_date=start_date,
            end_date=end_date,
        ),
    )


def fetch_eastmoney_china_yield_curve(
    start_date: str,
    end_date: str,
) -> list[dict[str, Any]]:
    """Fetch the minimum China treasury curve fields used by the strategy."""

    start_api = f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:]}"
    end_api = f"{end_date[:4]}-{end_date[4:6]}-{end_date[6:]}"
    params = {
        "type": "RPTA_WEB_TREASURYYIELD",
        "sty": "ALL",
        "st": "SOLAR_DATE",
        "sr": "-1",
        "token": EASTMONEY_BOND_RATES_TOKEN,
        "ps": 500,
        "startDate": start_api,
        "endDate": end_api,
    }
    max_pages = positive_env_int("EASTMONEY_BOND_MAX_PAGES", 100)
    rows_by_date: dict[str, dict[str, Any]] = {}
    total_pages: int | None = None

    for page in range(1, max_pages + 1):
        page_params = {**params, "p": page, "pageNo": page, "pageNum": page}
        response = requests.get(
            EASTMONEY_BOND_RATES_URL,
            params=page_params,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=30,
        )
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError as exc:
            raise HTTPException(status_code=502, detail="Eastmoney returned invalid JSON") from exc

        result = payload.get("result") if isinstance(payload, Mapping) else None
        if not isinstance(result, Mapping):
            raise HTTPException(status_code=502, detail="Eastmoney returned no yield-curve result")
        try:
            response_pages = int(result["pages"])
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=502, detail="Eastmoney yield-curve pagination is invalid") from exc
        if response_pages < 1 or (total_pages is not None and response_pages != total_pages):
            raise HTTPException(status_code=502, detail="Eastmoney yield-curve pagination is invalid")
        total_pages = response_pages
        if total_pages > max_pages:
            raise HTTPException(status_code=502, detail="Eastmoney yield-curve response is too large")

        page_rows = result.get("data")
        if not isinstance(page_rows, list) or not page_rows:
            raise HTTPException(status_code=502, detail="Eastmoney yield-curve page is empty")

        page_dates: list[str] = []
        for raw in page_rows:
            if not isinstance(raw, Mapping):
                raise HTTPException(status_code=502, detail="Eastmoney yield-curve row is invalid")
            row_date = json_date(raw.get(EASTMONEY_BOND_RATE_FIELDS["date"]))
            try:
                datetime.strptime(row_date, "%Y-%m-%d")
            except ValueError as exc:
                raise HTTPException(status_code=502, detail="Eastmoney yield-curve date is invalid") from exc
            page_dates.append(row_date.replace("-", ""))
            if start_date <= page_dates[-1] <= end_date:
                normalized: dict[str, Any] = {"date": row_date}
                for field in ("cn2", "cn5", "cn10", "cn30"):
                    value = json_number(raw.get(EASTMONEY_BOND_RATE_FIELDS[field]), field)
                    if value is None:
                        raise HTTPException(
                            status_code=502,
                            detail=f"Eastmoney yield-curve field {field} is null",
                        )
                    normalized[field] = value / 100.0
                rows_by_date[row_date] = normalized

        if page >= total_pages or min(page_dates) <= start_date:
            break
    else:
        raise HTTPException(status_code=502, detail="Eastmoney yield-curve pagination did not finish")

    return [rows_by_date[key] for key in sorted(rows_by_date)]


@app.get("/health", include_in_schema=True)
@app.get("/api/health", include_in_schema=False)
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "service": "investment-portfolio-datahub",
        "source": "akshare",
        "akshare_version": ak.__version__,
    }


@app.api_route("/akshare/{function_name}", methods=["GET", "POST"])
@app.api_route(
    "/api/akshare/{function_name}",
    methods=["GET", "POST"],
    include_in_schema=False,
)
async def akshare_proxy(function_name: str, request: Request) -> dict[str, Any]:
    require_request_auth(request)
    function = resolve_akshare_function(function_name)
    parameters = await request_parameters(request, function)
    try:
        result = await call_akshare(function, parameters)
        data = jsonable(result)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"AKShare request failed: {type(exc).__name__}",
        ) from exc
    return {
        "ok": True,
        "source": "akshare",
        "function": function_name,
        "parameter_names": sorted(parameters),
        "data": data,
    }


@app.get("/etf/{symbol}", include_in_schema=True)
@app.get("/api/etf/{symbol}", include_in_schema=False)
async def etf_daily(
    request: Request,
    symbol: str,
    start_date: str = Query("20200101"),
    end_date: str = Query("20500101"),
    adjust: str = Query(""),
) -> dict[str, Any]:
    require_request_auth(request)
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
        source, rows = await fetch_normalized_etf(
            symbol,
            normalized_start,
            normalized_end,
            adjust,
        )
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
        "source": source,
        "symbol": symbol,
        "start_date": normalized_start,
        "end_date": normalized_end,
        "adjust": adjust,
        "count": len(rows),
        "data": rows,
    }


@app.get("/bond/china-yield-curve", include_in_schema=True)
@app.get("/api/bond/china-yield-curve", include_in_schema=False)
async def china_yield_curve(
    request: Request,
    start_date: str = Query("20100101"),
    end_date: str = Query("20500101"),
) -> dict[str, Any]:
    require_request_auth(request)
    normalized_start = normalize_date(start_date, "start_date")
    normalized_end = normalize_date(end_date, "end_date")
    if normalized_start > normalized_end:
        raise HTTPException(status_code=422, detail="start_date must be <= end_date")

    try:
        rows = await run_in_threadpool(
            fetch_eastmoney_china_yield_curve,
            normalized_start,
            normalized_end,
        )
    except HTTPException:
        raise
    except requests.exceptions.RequestException as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Eastmoney request failed: {type(exc).__name__}",
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Eastmoney request failed: {type(exc).__name__}",
        ) from exc

    return {
        "ok": True,
        "source": "eastmoney.RPTA_WEB_TREASURYYIELD",
        "start_date": normalized_start,
        "end_date": normalized_end,
        "count": len(rows),
        "data": rows,
    }


@app.post("/tq/soymeal/raw", include_in_schema=True)
@app.post("/api/tq/soymeal/raw", include_in_schema=False)
async def tq_soymeal_raw(request: Request) -> dict[str, Any]:
    require_request_auth(request)
    try:
        payload = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Request body must be valid JSON") from exc

    parameters = parse_tq_request(payload)
    try:
        data = await fetch_tq_raw(parameters)
    except TqConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except TqUpstreamError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"TQ request failed: {type(exc).__name__}",
        ) from exc

    return data
