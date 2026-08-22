from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
import asyncio
import inspect
import json
from math import isfinite
import os
import re
import secrets
from typing import Any
from typing import get_args, get_origin

import akshare as ak
import requests
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.concurrency import run_in_threadpool


SUPPORTED_ETFS = {"159985", "512890", "513100", "513500", "518880"}
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


def require_request_auth(request: Request) -> None:
    expected = os.getenv("DATAHUB_API_TOKEN", "").strip()
    if not expected:
        return

    supplied = request.headers.get("x-datahub-token", "").strip()
    authorization = request.headers.get("authorization", "")
    if not supplied and authorization.lower().startswith("bearer "):
        supplied = authorization[7:].strip()
    if not supplied or not secrets.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="Missing or invalid datahub token")


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
        frame = await call_akshare(
            ak.fund_etf_hist_em,
            {
                "symbol": symbol,
                "period": "daily",
                "start_date": normalized_start,
                "end_date": normalized_end,
                "adjust": adjust,
            },
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
