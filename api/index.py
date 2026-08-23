from __future__ import annotations

import asyncio
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from api.security import require_request_auth
from api.csindex import (
    CsIndexHistoryError,
    fetch_csindex_history,
    parse_history_request,
)
from api.tq_derived import (
    TqDerivedError,
    fetch_tq_derived,
    parse_tq_derived_request,
)
from api.wasde_parser import (
    MAX_RELEASES_PER_CALL,
    WasdeError,
    fetch_dividend_yield,
    fetch_wasde_rows,
)


WASDE_SEMAPHORE = asyncio.Semaphore(2)
DIVIDEND_YIELD_SEMAPHORE = asyncio.Semaphore(2)


app = FastAPI(
    title="Investment Portfolio DataHub",
    version="0.4.0",
    description=(
        "Vercel relay for concrete-contract TQSDK futures, USDA WASDE, and "
        "CSIndex data used by the investment portfolio pipeline."
    ),
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

PROTECTED_PATHS = frozenset(
    {
        "/tq/soymeal/derived",
        "/api/tq/soymeal/derived",
        "/usda",
        "/api/usda",
        "/csindex",
        "/api/csindex",
        "/csindex/history",
        "/api/csindex/history",
    }
)


@app.middleware("http")
async def authenticate_datahub_request(request: Request, call_next):
    if request.method != "OPTIONS" and request.url.path in PROTECTED_PATHS:
        try:
            require_request_auth(request)
        except HTTPException as exc:
            return JSONResponse(
                status_code=exc.status_code,
                content={"detail": exc.detail},
            )
    return await call_next(request)


@app.get("/health", include_in_schema=True)
@app.get("/api/health", include_in_schema=False)
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "service": "investment-portfolio-datahub",
        "version": app.version,
        "sources": [
            "tqsdk-relay",
            "usda-excel-relay",
            "csindex-history-relay",
            "csindex-indicator-relay",
        ],
    }


@app.post("/tq/soymeal/derived", include_in_schema=True)
@app.post("/api/tq/soymeal/derived", include_in_schema=False)
async def tq_soymeal_derived(request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Request body must be valid JSON") from exc

    parameters = parse_tq_derived_request(payload)
    try:
        result = await fetch_tq_derived(parameters)
    except TqDerivedError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"TQ derived request failed: {type(exc).__name__}",
        ) from exc
    return {
        "ok": True,
        "source": "tqsdk",
        "start_date": parameters["start_date"].isoformat(),
        "end_date": parameters["end_date"].isoformat(),
        **result,
    }


def _parse_wasde_request(payload: Any) -> tuple[str, str, int]:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="Request body must be a JSON object")
    start_date = payload.get("start_date")
    end_date = payload.get("end_date")
    max_rows = payload.get("max_rows", MAX_RELEASES_PER_CALL)
    if not isinstance(start_date, str) or not isinstance(end_date, str):
        raise HTTPException(status_code=422, detail="start_date and end_date are required")
    if isinstance(max_rows, bool) or not isinstance(max_rows, int):
        raise HTTPException(status_code=422, detail="max_rows must be an integer")
    if max_rows < 1 or max_rows > MAX_RELEASES_PER_CALL:
        raise HTTPException(
            status_code=422,
            detail=f"max_rows must be between 1 and {MAX_RELEASES_PER_CALL}",
        )
    return start_date, end_date, max_rows


@app.post("/usda", include_in_schema=True)
@app.post("/api/usda", include_in_schema=False)
async def usda_wasde_rows(request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Request body must be valid JSON") from exc
    start_date, end_date, max_rows = _parse_wasde_request(payload)
    try:
        async with WASDE_SEMAPHORE:
            rows, has_more, discovered_rows = await run_in_threadpool(
                fetch_wasde_rows,
                start_date,
                end_date,
                max_rows,
            )
    except WasdeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"USDA parsing failed: {type(exc).__name__}") from exc
    return {
        "ok": True,
        "source": "usda-esmis",
        "start_date": start_date,
        "end_date": end_date,
        "max_rows": max_rows,
        "discovered_rows": discovered_rows,
        "returned_rows": len(rows),
        "has_more": has_more,
        "rows": rows,
    }


@app.post("/csindex", include_in_schema=True)
@app.post("/api/csindex", include_in_schema=False)
async def csindex_dividend_yield(request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Request body must be valid JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="Request body must be a JSON object")
    start_date = payload.get("start_date")
    end_date = payload.get("end_date")
    if not isinstance(start_date, str) or not isinstance(end_date, str):
        raise HTTPException(status_code=422, detail="start_date and end_date are required")
    try:
        async with DIVIDEND_YIELD_SEMAPHORE:
            rows = await run_in_threadpool(
                fetch_dividend_yield,
                start_date,
                end_date,
            )
    except WasdeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"CSIndex parsing failed: {type(exc).__name__}") from exc
    return {
        "ok": True,
        "source": "csindex",
        "start_date": start_date,
        "end_date": end_date,
        "returned_rows": len(rows),
        "rows": rows,
    }


@app.post("/csindex/history", include_in_schema=True)
@app.post("/api/csindex/history", include_in_schema=False)
async def csindex_history(request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Request body must be valid JSON") from exc
    parameters = parse_history_request(payload)
    try:
        rows = await fetch_csindex_history(parameters)
    except CsIndexHistoryError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"CSIndex history failed: {type(exc).__name__}") from exc
    return {
        "ok": True,
        "source": "csindex-history",
        "index_code": parameters["index_code"],
        "start_date": parameters["start_date"].isoformat(),
        "end_date": parameters["end_date"].isoformat(),
        "returned_rows": len(rows),
        "rows": rows,
    }
