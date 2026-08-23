from __future__ import annotations

import asyncio
from datetime import date, datetime
import os
import time
from typing import Any

import requests
from fastapi import HTTPException
from starlette.concurrency import run_in_threadpool

from api.security import SHARED_DATAHUB_TOKEN


ALLOWED_DATASETS = frozenset(
    {"main_continuous", "main_mapping", "contract_info", "contract_bars", "settlements"}
)
DEFAULT_DATASETS = tuple(sorted(ALLOWED_DATASETS))


def positive_env_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return max(1, value)


TQ_RELAY_RETRY_ATTEMPTS = positive_env_int("TQ_RELAY_RETRY_ATTEMPTS", 2)
TQ_RELAY_TIMEOUT_SECONDS = positive_env_int("TQ_RELAY_TIMEOUT_SECONDS", 55)
TQ_RELAY_TOTAL_TIMEOUT_SECONDS = positive_env_int("TQ_RELAY_TOTAL_TIMEOUT_SECONDS", 90)
TQ_RELAY_SEMAPHORE = asyncio.Semaphore(2)


class TqConfigurationError(RuntimeError):
    pass


class TqUpstreamError(RuntimeError):
    pass


def parse_date(value: Any, field_name: str) -> date:
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(status_code=422, detail=f"{field_name} is required")
    normalized = value.strip().replace("-", "")
    try:
        return datetime.strptime(normalized, "%Y%m%d").date()
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"{field_name} must use YYYYMMDD or YYYY-MM-DD",
        ) from exc


def parse_tq_request(payload: Any) -> dict[str, Any]:
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="POST body must be a JSON object")

    start_date = parse_date(payload.get("start_date"), "start_date")
    end_date = parse_date(payload.get("end_date"), "end_date")
    if start_date > end_date:
        raise HTTPException(status_code=422, detail="start_date must be <= end_date")

    max_data_length = positive_env_int("TQ_MAX_DATA_LENGTH", 10_000)
    data_length = payload.get("data_length", 300)
    if isinstance(data_length, bool) or not isinstance(data_length, int):
        raise HTTPException(status_code=422, detail="data_length must be an integer")
    if data_length < 1 or data_length > max_data_length:
        raise HTTPException(
            status_code=422,
            detail=f"data_length must be between 1 and {max_data_length}",
        )

    max_settlement_days = positive_env_int("TQ_MAX_SETTLEMENT_DAYS", 2_000)
    settlement_days = payload.get(
        "settlement_days",
        min(max_settlement_days, max(300, (end_date - start_date).days + 30)),
    )
    if isinstance(settlement_days, bool) or not isinstance(settlement_days, int):
        raise HTTPException(status_code=422, detail="settlement_days must be an integer")
    if settlement_days < 1 or settlement_days > max_settlement_days:
        raise HTTPException(
            status_code=422,
            detail=f"settlement_days must be between 1 and {max_settlement_days}",
        )

    datasets = payload.get("datasets", list(DEFAULT_DATASETS))
    if not isinstance(datasets, list) or not datasets:
        raise HTTPException(status_code=422, detail="datasets must be a non-empty array")
    if any(not isinstance(item, str) for item in datasets):
        raise HTTPException(status_code=422, detail="datasets must contain strings")
    requested = tuple(dict.fromkeys(datasets))
    unknown = sorted(set(requested).difference(ALLOWED_DATASETS))
    if unknown:
        raise HTTPException(
            status_code=422,
            detail={"message": "Unknown TQ dataset", "allowed": sorted(ALLOWED_DATASETS)},
        )

    return {
        "start_date": start_date,
        "end_date": end_date,
        "data_length": data_length,
        "settlement_days": settlement_days,
        "datasets": requested,
    }


def _upstream_payload(parameters: dict[str, Any]) -> dict[str, Any]:
    return {
        **parameters,
        "start_date": parameters["start_date"].isoformat(),
        "end_date": parameters["end_date"].isoformat(),
        "datasets": list(parameters["datasets"]),
    }


def _relay_once(payload: dict[str, Any], timeout_seconds: float) -> dict[str, Any]:
    upstream_url = os.getenv("TQSDK_UPSTREAM_URL", "").strip()
    if not upstream_url:
        raise TqConfigurationError("TQSDK_UPSTREAM_URL is not configured")

    headers = {"content-type": "application/json"}
    headers["x-tq-service-token"] = SHARED_DATAHUB_TOKEN

    try:
        response = requests.post(
            upstream_url,
            json=payload,
            headers=headers,
            timeout=timeout_seconds,
        )
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
        raise TqUpstreamError(type(exc).__name__) from exc

    if response.status_code >= 500:
        raise TqUpstreamError(f"HTTP_{response.status_code}")
    if response.status_code >= 400:
        raise HTTPException(
            status_code=response.status_code,
            detail="TQ upstream rejected the request",
        )
    try:
        result = response.json()
    except ValueError as exc:
        raise TqUpstreamError("invalid_json") from exc
    if not isinstance(result, dict):
        raise TqUpstreamError("response_not_object")
    return result


async def fetch_tq_raw(parameters: dict[str, Any]) -> dict[str, Any]:
    payload = _upstream_payload(parameters)
    async with TQ_RELAY_SEMAPHORE:
        deadline = time.monotonic() + TQ_RELAY_TOTAL_TIMEOUT_SECONDS
        last_error: Exception | None = None
        for attempt in range(TQ_RELAY_RETRY_ATTEMPTS):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                return await run_in_threadpool(
                    _relay_once,
                    payload,
                    min(TQ_RELAY_TIMEOUT_SECONDS, remaining),
                )
            except (TqConfigurationError, HTTPException):
                raise
            except TqUpstreamError as exc:
                last_error = exc
                if attempt + 1 < TQ_RELAY_RETRY_ATTEMPTS:
                    remaining = deadline - time.monotonic()
                    await asyncio.sleep(min(2.0, 0.5 * (2**attempt), max(0.0, remaining)))
        if last_error is None:
            raise TqUpstreamError("TQ relay total timeout exceeded")
        raise TqUpstreamError(
            f"TQ upstream request failed: {type(last_error).__name__}"
        ) from last_error
