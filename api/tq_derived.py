from __future__ import annotations

import asyncio
import math
import time
from datetime import date, datetime
from typing import Any

import numpy as np
import pandas as pd
from fastapi import HTTPException
from starlette.concurrency import run_in_threadpool


# Temporary free TqSdk test account requested for this deployment.
TQ_USERNAME = "18064423114"
TQ_PASSWORD = "ctk2000121"
TQ_MAIN_SYMBOL = "KQ.m@DCE.m"
TQ_DAILY_SECONDS = 86_400
TQ_DATA_LENGTH = 160
TQ_WAIT_SECONDS = 35
# Keep two independent TQSDK sessions in flight without blocking the event loop.
# The limit avoids creating an unbounded number of authenticated sessions when
# a scheduled retry and a manual request arrive at the same time.
TQ_DIRECT_SEMAPHORE = asyncio.Semaphore(2)


class TqDerivedError(RuntimeError):
    pass


def _parse_date(value: Any, field_name: str) -> date:
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(status_code=422, detail=f"{field_name} is required")
    normalized = value.strip().replace("-", "")
    try:
        return datetime.strptime(normalized, "%Y%m%d").date()
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"{field_name} must use YYYY-MM-DD",
        ) from exc


def parse_tq_derived_request(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="POST body must be a JSON object")

    start = _parse_date(payload.get("start_date"), "start_date")
    end = _parse_date(payload.get("end_date"), "end_date")
    if start > end:
        raise HTTPException(status_code=422, detail="start_date must be <= end_date")
    if (end - start).days > 45:
        raise HTTPException(status_code=422, detail="date range must not exceed 45 days")

    seed = payload.get("seed")
    if not isinstance(seed, dict):
        raise HTTPException(status_code=422, detail="seed is required")
    seed_date = _parse_date(seed.get("trade_date"), "seed.trade_date")
    if seed_date >= start:
        raise HTTPException(status_code=422, detail="seed.trade_date must be before start_date")
    try:
        seed_close = float(seed["close"])
        seed_m888_close = float(seed["m888_close"])
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=422,
            detail="seed.close and seed.m888_close must be numeric",
        ) from exc
    if not math.isfinite(seed_close) or seed_close <= 0:
        raise HTTPException(status_code=422, detail="seed.close must be positive")
    if not math.isfinite(seed_m888_close) or seed_m888_close <= 0:
        raise HTTPException(status_code=422, detail="seed.m888_close must be positive")

    seed_contract = seed.get("main_contract_code")
    if not isinstance(seed_contract, str) or not seed_contract.strip():
        raise HTTPException(status_code=422, detail="seed.main_contract_code is required")

    return {
        "start_date": start,
        "end_date": end,
        "seed": {
            "trade_date": seed_date,
            "close": seed_close,
            "m888_close": seed_m888_close,
            "main_contract_code": seed_contract.strip(),
        },
    }


def _as_local_datetime(values: pd.Series) -> pd.Series:
    numeric = pd.to_numeric(values, errors="coerce")
    return pd.to_datetime(numeric, unit="ns", utc=True).dt.tz_convert("Asia/Shanghai")


def _normalise_mapping(raw: pd.DataFrame) -> pd.DataFrame:
    mapping = pd.DataFrame(raw).copy()
    if "date" not in mapping.columns:
        raise TqDerivedError("Tq main mapping has no date column")
    mapping["date"] = pd.to_datetime(mapping["date"], errors="coerce").dt.normalize()
    contract_column = TQ_MAIN_SYMBOL if TQ_MAIN_SYMBOL in mapping.columns else None
    if contract_column is None:
        candidates = [column for column in mapping.columns if column != "date"]
        if not candidates:
            raise TqDerivedError("Tq main mapping has no contract column")
        contract_column = candidates[0]
    mapping = mapping[["date", contract_column]].rename(
        columns={contract_column: "underlying_symbol"}
    )
    mapping["underlying_symbol"] = mapping["underlying_symbol"].fillna("").astype(str)
    mapping = mapping.dropna(subset=["date"]).sort_values("date")
    return mapping.drop_duplicates("date", keep="last")


def _assign_trading_dates(frame: pd.DataFrame, mapping: pd.DataFrame) -> pd.DataFrame:
    result = pd.DataFrame(frame).copy()
    if "datetime" not in result.columns:
        raise TqDerivedError("Tq kline has no datetime column")
    result["bar_start"] = _as_local_datetime(result["datetime"])
    trade_dates = mapping.loc[
        mapping["underlying_symbol"].ne(""), "date"
    ].drop_duplicates()
    trade_days = trade_dates.to_numpy(dtype="datetime64[D]")
    if not len(trade_days):
        raise TqDerivedError("Tq main mapping has no usable trading dates")

    calendar_days = (
        result["bar_start"].dt.tz_localize(None).dt.normalize().to_numpy(dtype="datetime64[D]")
    )
    starts_at_night = result["bar_start"].dt.hour.to_numpy() >= 18
    positions = np.searchsorted(trade_days, calendar_days, side="left")
    bounded = np.minimum(positions, len(trade_days) - 1)
    exact = (positions < len(trade_days)) & (trade_days[bounded] == calendar_days)
    positions = positions + (starts_at_night & exact)
    positions = np.minimum(positions, len(trade_days) - 1)
    result["date"] = pd.to_datetime(trade_days[positions])
    return result


def _completion_cutoff() -> pd.Timestamp:
    now = pd.Timestamp.now(tz="Asia/Shanghai")
    cutoff = now.normalize() if now.hour >= 16 else now.normalize() - pd.Timedelta(days=1)
    return cutoff.tz_localize(None)


def _clean_kline(
    raw: pd.DataFrame,
    mapping: pd.DataFrame,
    *,
    symbol: str | None = None,
) -> pd.DataFrame:
    frame = pd.DataFrame(raw).copy(deep=True)
    numeric_datetime = pd.to_numeric(frame.get("datetime"), errors="coerce")
    close = pd.to_numeric(frame.get("close"), errors="coerce")
    frame = frame.loc[numeric_datetime.gt(0) & close.gt(0)].copy()
    if frame.empty:
        return frame
    frame = _assign_trading_dates(frame, mapping)
    frame = frame.loc[frame["date"].le(_completion_cutoff())].copy()
    if symbol is not None:
        frame["symbol"] = symbol
    return frame.sort_values("date").drop_duplicates("date", keep="last")


def _fetch_raw(start_date: date, end_date: date, seed: dict[str, Any]) -> dict[str, Any]:
    try:
        from tqsdk import TqApi, TqAuth
    except ImportError as exc:
        raise TqDerivedError("tqsdk is not installed in the Vercel runtime") from exc

    api = None
    try:
        api = TqApi(auth=TqAuth(TQ_USERNAME, TQ_PASSWORD))
        raw_main = api.get_kline_serial(
            TQ_MAIN_SYMBOL,
            TQ_DAILY_SECONDS,
            data_length=TQ_DATA_LENGTH,
        )
        raw_mapping = api.query_his_cont_quotes(TQ_MAIN_SYMBOL, n=TQ_DATA_LENGTH)
        mapping = _normalise_mapping(raw_mapping.copy(deep=True))
        main = _clean_kline(raw_main.copy(deep=True), mapping)
        if main.empty:
            raise TqDerivedError("Tq returned no completed main-continuous bars")
        main = main.merge(mapping, on="date", how="left")
        if "underlying_symbol_y" in main.columns:
            main["underlying_symbol"] = main["underlying_symbol_y"]
            main = main.drop(
                columns=[
                    column
                    for column in ("underlying_symbol_x", "underlying_symbol_y")
                    if column in main.columns
                ]
            )
        main["underlying_symbol"] = main["underlying_symbol"].fillna("").astype(str)
        main = main.loc[
            main["underlying_symbol"].ne("")
            & main["date"].ge(pd.Timestamp(start_date))
            & main["date"].le(pd.Timestamp(end_date))
        ].copy()
        if main.empty:
            return {"rows": [], "through_date": None}
        main = main.sort_values("date").drop_duplicates("date", keep="last")

        symbols = list(dict.fromkeys(main["underlying_symbol"].astype(str)))
        concrete: dict[str, pd.DataFrame] = {}
        for symbol in symbols:
            raw_contract = api.get_kline_serial(
                symbol,
                TQ_DAILY_SECONDS,
                data_length=TQ_DATA_LENGTH,
            )
            cleaned = _clean_kline(raw_contract.copy(deep=True), mapping, symbol=symbol)
            if not cleaned.empty:
                concrete[symbol] = cleaned

        api.wait_update(deadline=time.time() + TQ_WAIT_SECONDS)
        return _build_rows(main, concrete, seed)
    except TqDerivedError:
        raise
    except Exception as exc:
        raise TqDerivedError(f"TqSdk request failed: {type(exc).__name__}") from exc
    finally:
        if api is not None:
            api.close()


def _build_rows(
    main: pd.DataFrame,
    concrete: dict[str, pd.DataFrame],
    seed: dict[str, Any],
) -> dict[str, Any]:
    close_by_key: dict[tuple[pd.Timestamp, str], float] = {}
    for symbol, frame in concrete.items():
        for _, row in frame.iterrows():
            value = float(row["close"])
            if math.isfinite(value) and value > 0:
                close_by_key[(pd.Timestamp(row["date"]), symbol)] = value

    previous_date = pd.Timestamp(seed["trade_date"])
    previous_close = float(seed["close"])
    previous_m888 = float(seed["m888_close"])
    previous_symbol = str(seed["main_contract_code"])
    output: list[dict[str, Any]] = []
    for _, row in main.sort_values("date").iterrows():
        current_date = pd.Timestamp(row["date"])
        current_close = float(row["close"])
        current_symbol = str(row["underlying_symbol"])
        if not math.isfinite(current_close) or current_close <= 0:
            raise TqDerivedError(f"Invalid main close on {current_date.date()}")

        roll_from = None
        roll_to = None
        new_previous = None
        new_current = None
        roll_return = None
        if current_symbol == previous_symbol:
            daily_return = current_close / previous_close - 1.0
        else:
            new_previous = close_by_key.get((previous_date, current_symbol))
            new_current = close_by_key.get((current_date, current_symbol))
            if new_previous is None or new_current is None or new_previous <= 0:
                raise TqDerivedError(
                    f"Missing new-contract close for roll {previous_symbol}->{current_symbol} "
                    f"on {current_date.date()}"
                )
            roll_from = previous_symbol
            roll_to = current_symbol
            roll_return = new_current / new_previous - 1.0
            daily_return = roll_return

        m888_close = previous_m888 * (1.0 + daily_return)
        if not math.isfinite(m888_close) or m888_close <= 0:
            raise TqDerivedError(f"Invalid M888 close on {current_date.date()}")
        output.append(
            {
                "trade_date": current_date.strftime("%Y-%m-%d"),
                "main_contract_code": current_symbol,
                "close": current_close,
                "m888_close": m888_close,
                "roll_from_contract": roll_from,
                "roll_to_contract": roll_to,
                "new_contract_previous_close": new_previous,
                "new_contract_current_close": new_current,
                "roll_return": roll_return,
                "adjustment_factor": m888_close / current_close,
                "data_source": "tqsdk",
            }
        )
        previous_date = current_date
        previous_close = current_close
        previous_m888 = m888_close
        previous_symbol = current_symbol

    return {
        "rows": output,
        "through_date": output[-1]["trade_date"] if output else None,
    }


async def fetch_tq_derived(parameters: dict[str, Any]) -> dict[str, Any]:
    async with TQ_DIRECT_SEMAPHORE:
        return await run_in_threadpool(
            _fetch_raw,
            parameters["start_date"],
            parameters["end_date"],
            parameters["seed"],
        )
