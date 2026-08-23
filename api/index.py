from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware

from api.security import require_request_auth
from api.tq_proxy import (
    TqConfigurationError,
    TqUpstreamError,
    fetch_tq_raw,
    parse_tq_request,
)
from api.wasde_parser import (
    MAX_RELEASES_PER_CALL,
    WasdeError,
    fetch_dividend_yield,
    fetch_wasde_rows,
)


app = FastAPI(
    title="Investment Portfolio DataHub",
    version="0.3.0",
    description="Minimal Vercel relay for TQSDK soybean-meal data.",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


@app.get("/health", include_in_schema=True)
@app.get("/api/health", include_in_schema=False)
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "service": "investment-portfolio-datahub",
        "version": app.version,
        "sources": ["tqsdk-relay", "usda-excel-relay", "csindex-indicator-relay"],
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
    require_request_auth(request)
    try:
        payload = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Request body must be valid JSON") from exc
    start_date, end_date, max_rows = _parse_wasde_request(payload)
    try:
        rows, has_more, discovered_rows = fetch_wasde_rows(start_date, end_date, max_rows)
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
    require_request_auth(request)
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
        rows = fetch_dividend_yield(start_date, end_date)
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
