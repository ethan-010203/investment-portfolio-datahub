from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.concurrency import run_in_threadpool

from api.csindex import SUPPORTED_INDEXES, fetch_dividend_yield
from api.security import require_request_auth
from api.tdx import (
    ETF_MARKETS,
    TdxDataError,
    fetch_tdx_etf_bars,
    fetch_tdx_etf_batch,
)
from api.tq_proxy import (
    TqConfigurationError,
    TqUpstreamError,
    fetch_tq_raw,
    parse_tq_request,
)
from api.wasde import fetch_wasde_rows


app = FastAPI(
    title="Investment Portfolio DataHub",
    version="0.2.0",
    description="Minimal Vercel relay for TDX, CSIndex, USDA, and TQSDK data.",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


def normalize_date(value: str, field_name: str) -> str:
    normalized = value.strip().replace("-", "")
    if len(normalized) != 8 or not normalized.isdigit():
        raise HTTPException(
            status_code=422,
            detail=f"{field_name} must use YYYYMMDD or YYYY-MM-DD",
        )
    iso_date = f"{normalized[:4]}-{normalized[4:6]}-{normalized[6:]}"
    try:
        date.fromisoformat(iso_date)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"{field_name} must use YYYYMMDD or YYYY-MM-DD",
        ) from exc
    return iso_date


@app.get("/health", include_in_schema=True)
@app.get("/api/health", include_in_schema=False)
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "service": "investment-portfolio-datahub",
        "version": app.version,
        "sources": ["pytdx", "csindex", "usda.esmis", "tqsdk-relay"],
    }


@app.post("/etf/batch", include_in_schema=True)
@app.post("/api/etf/batch", include_in_schema=False)
async def etf_daily_batch(request: Request) -> dict[str, Any]:
    require_request_auth(request)
    try:
        payload = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Request body must be valid JSON") from exc
    raw_requests = payload.get("requests") if isinstance(payload, dict) else None
    if not isinstance(raw_requests, list) or not raw_requests:
        raise HTTPException(status_code=422, detail="requests must be a non-empty array")

    normalized_requests: list[dict[str, str]] = []
    for index, raw in enumerate(raw_requests):
        if not isinstance(raw, dict):
            raise HTTPException(status_code=422, detail=f"requests[{index}] must be an object")
        symbol = str(raw.get("symbol", "")).strip()
        if symbol not in ETF_MARKETS:
            raise HTTPException(status_code=422, detail=f"requests[{index}].symbol is unsupported")
        start_date = normalize_date(str(raw.get("start_date", "")), f"requests[{index}].start_date")
        end_date = normalize_date(str(raw.get("end_date", "")), f"requests[{index}].end_date")
        if start_date > end_date:
            raise HTTPException(
                status_code=422,
                detail=f"requests[{index}].start_date must be <= end_date",
            )
        normalized_requests.append(
            {"symbol": symbol, "start_date": start_date, "end_date": end_date}
        )

    try:
        server, results = await run_in_threadpool(
            fetch_tdx_etf_batch,
            normalized_requests,
        )
    except TdxDataError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"TDX batch request failed: {type(exc).__name__}",
        ) from exc
    return {
        "ok": True,
        "source": "pytdx",
        "server": server,
        "results": [
            {
                "symbol": item["symbol"],
                "count": len(results[item["symbol"]]),
                "data": results[item["symbol"]],
            }
            for item in normalized_requests
        ],
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
    if symbol not in ETF_MARKETS:
        raise HTTPException(
            status_code=400,
            detail=f"Supported ETFs: {sorted(ETF_MARKETS)}",
        )
    if adjust:
        raise HTTPException(status_code=422, detail="TDX ETF history is unadjusted only")

    normalized_start = normalize_date(start_date, "start_date")
    normalized_end = normalize_date(end_date, "end_date")
    if normalized_start > normalized_end:
        raise HTTPException(status_code=422, detail="start_date must be <= end_date")

    try:
        server, rows = await run_in_threadpool(
            fetch_tdx_etf_bars,
            symbol,
            normalized_start,
            normalized_end,
        )
    except TdxDataError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"TDX request failed: {type(exc).__name__}",
        ) from exc

    return {
        "ok": True,
        "source": "pytdx",
        "server": server,
        "symbol": symbol,
        "start_date": normalized_start,
        "end_date": normalized_end,
        "adjust": "",
        "count": len(rows),
        "data": rows,
    }


@app.get("/index/{symbol}/dividend-yield", include_in_schema=True)
@app.get("/api/index/{symbol}/dividend-yield", include_in_schema=False)
async def index_dividend_yield(
    request: Request,
    symbol: str,
    start_date: str = Query("20200101"),
    end_date: str = Query("20500101"),
) -> dict[str, Any]:
    require_request_auth(request)
    if symbol not in SUPPORTED_INDEXES:
        raise HTTPException(
            status_code=400,
            detail=f"Supported indexes: {sorted(SUPPORTED_INDEXES)}",
        )
    normalized_start = normalize_date(start_date, "start_date")
    normalized_end = normalize_date(end_date, "end_date")
    if normalized_start > normalized_end:
        raise HTTPException(status_code=422, detail="start_date must be <= end_date")
    try:
        rows = await run_in_threadpool(
            fetch_dividend_yield,
            symbol,
            normalized_start,
            normalized_end,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"CSIndex request failed: {type(exc).__name__}",
        ) from exc
    return {
        "ok": True,
        "source": "csindex.official.indicator",
        "symbol": symbol,
        "start_date": normalized_start,
        "end_date": normalized_end,
        "count": len(rows),
        "data": rows,
    }


@app.get("/usda/wasde", include_in_schema=True)
@app.get("/api/usda/wasde", include_in_schema=False)
async def usda_wasde(
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
            fetch_wasde_rows,
            normalized_start.replace("-", ""),
            normalized_end.replace("-", ""),
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"USDA WASDE request failed: {type(exc).__name__}",
        ) from exc
    return {
        "ok": True,
        "source": "usda.esmis.wasde",
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
        return await fetch_tq_raw(parameters)
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
