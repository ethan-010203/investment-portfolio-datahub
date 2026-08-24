"""按单个品种获取 TQSDK 具体合约并生成正式连续序列。"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from datetime import date, datetime
from typing import Any

import pandas as pd
from fastapi import HTTPException
from starlette.concurrency import run_in_threadpool

from api.continuous_adjust import ContinuousAdjustmentError, build_pre_adjusted_series
from api.dominant_rule0 import DominantRuleError, build_rule0_mapping
from api.quantity_normalizer import normalize_quantity
from api.soymeal_roll_yield import RollYieldError, build_soymeal_roll_yields

# Temporary free test account requested for this deployment.
TQ_USERNAME = "18064423114"
TQ_PASSWORD = "ctk2000121"
TQ_DAILY_SECONDS = 86_400
TQ_COMPLETE_UNIVERSE_START = date(2020, 9, 15)
TQ_DIRECT_SEMAPHORE = asyncio.Semaphore(1)
LOGGER = logging.getLogger("datahub.tq")

PRODUCTS = {
    "M": {"exchange": "DCE", "product_id": "m"},
    "RM": {"exchange": "CZCE", "product_id": "RM"},
    "Y": {"exchange": "DCE", "product_id": "y"},
    "C": {"exchange": "DCE", "product_id": "c"},
    "JD": {"exchange": "DCE", "product_id": "jd"},
}

# TQ projects future DCE expiry dates before the official holiday calendar is
# fully reflected. These two dates are already frozen by the formal local
# contract metadata; remove an override once TQ publishes the same date.
FORMAL_MATURITY_OVERRIDES = {
    "DCE.m2701": pd.Timestamp("2027-01-15"),
    "DCE.m2705": pd.Timestamp("2027-05-19"),
}


class TqDerivedError(RuntimeError):
    pass


def _parse_date(value: Any, field_name: str) -> date:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value.strip()):
        raise HTTPException(status_code=422, detail=f"{field_name} must use YYYY-MM-DD")
    try:
        return date.fromisoformat(value.strip())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"{field_name} must use YYYY-MM-DD") from exc


def _parse_state(value: Any, product: str, start: date) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise HTTPException(status_code=422, detail="state is required")
    trade_date = _parse_date(value.get("trade_date"), "state.trade_date")
    if trade_date >= start:
        raise HTTPException(
            status_code=422,
            detail="state.trade_date must be before start_date",
        )
    main = value.get("main_contract_code")
    if not isinstance(main, str) or not main.strip():
        raise HTTPException(
            status_code=422,
            detail="state.main_contract_code is required",
        )
    seen = value.get("seen_contracts")
    if not isinstance(seen, list) or any(not isinstance(item, str) or not item.strip() for item in seen):
        raise HTTPException(
            status_code=422,
            detail="state.seen_contracts must be a string array",
        )
    normalized_seen = list(dict.fromkeys(item.strip() for item in seen))
    if main.strip() not in normalized_seen:
        normalized_seen.append(main.strip())
    return {
        "trade_date": trade_date,
        "main_contract_code": main.strip(),
        "seen_contracts": normalized_seen,
    }


def parse_tq_product_request(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="POST body must be a JSON object")
    start = _parse_date(payload.get("start_date"), "start_date")
    end = _parse_date(payload.get("end_date"), "end_date")
    if start > end:
        raise HTTPException(status_code=422, detail="start_date must be <= end_date")
    if (end - start).days > 45:
        raise HTTPException(status_code=422, detail="date range must not exceed 45 days")
    if start < TQ_COMPLETE_UNIVERSE_START:
        raise HTTPException(
            status_code=422,
            detail=f"incremental TQ data starts at {TQ_COMPLETE_UNIVERSE_START.isoformat()}",
        )
    product = payload.get("product")
    if not isinstance(product, str) or product not in PRODUCTS:
        raise HTTPException(
            status_code=422,
            detail=f"product must be one of {list(PRODUCTS)}",
        )
    return {
        "start_date": start,
        "end_date": end,
        "product": product,
        "state": _parse_state(payload.get("state"), product, start),
    }


def _completion_cutoff() -> pd.Timestamp:
    now = pd.Timestamp.now(tz="Asia/Shanghai")
    value = now.normalize() if now.hour >= 16 else now.normalize() - pd.Timedelta(days=1)
    return value.tz_localize(None)


def _expiry_series(info: pd.DataFrame) -> pd.Series:
    required = {"instrument_id", "expire_datetime"}
    missing = required.difference(info.columns)
    if missing:
        raise TqDerivedError(f"TQ contract info missing columns: {sorted(missing)}")
    seconds = pd.to_numeric(info["expire_datetime"], errors="coerce")
    values = (
        pd.to_datetime(seconds, unit="s", utc=True, errors="coerce")
        .dt.tz_convert("Asia/Shanghai")
        .dt.tz_localize(None)
        .dt.normalize()
    )
    result = pd.Series(values.to_numpy(), index=info["instrument_id"].astype(str)).dropna()
    for symbol, maturity in FORMAL_MATURITY_OVERRIDES.items():
        if symbol in result.index:
            result.loc[symbol] = maturity
    return result


def _clean_serial(raw: pd.DataFrame, symbol: str, expiry: pd.Timestamp) -> pd.DataFrame:
    frame = pd.DataFrame(raw).copy(deep=True)
    required = {"datetime", "close", "volume", "close_oi"}
    missing = required.difference(frame.columns)
    if missing:
        raise TqDerivedError(f"TQ kline {symbol} missing columns: {sorted(missing)}")
    epoch = pd.to_numeric(frame["datetime"], errors="coerce")
    frame["trade_date"] = (
        pd.to_datetime(epoch, unit="ns", utc=True, errors="coerce")
        .dt.tz_convert("Asia/Shanghai")
        .dt.tz_localize(None)
        .dt.normalize()
    )
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    frame["volume"] = pd.to_numeric(frame["volume"], errors="coerce")
    frame["open_interest"] = pd.to_numeric(frame["close_oi"], errors="coerce")
    frame = frame.loc[
        frame["trade_date"].notna()
        & frame["trade_date"].le(_completion_cutoff())
        & frame["close"].gt(0.0)
        & frame["volume"].ge(0.0)
        & frame["open_interest"].ge(0.0)
    ].copy()
    frame["contract_code"] = symbol
    frame["expiry_date"] = pd.Timestamp(expiry).normalize()
    for index, row in frame.iterrows():
        frame.at[index, "volume"] = normalize_quantity(row["trade_date"], row["volume"])
        frame.at[index, "open_interest"] = normalize_quantity(
            row["trade_date"], row["open_interest"]
        )
    return (
        frame[["trade_date", "contract_code", "expiry_date", "close", "volume", "open_interest"]]
        .sort_values("trade_date")
        .drop_duplicates("trade_date", keep="last")
    )


def _normalise_settlements(raw: pd.DataFrame) -> pd.DataFrame:
    frame = pd.DataFrame(raw).copy()
    required = {"datetime", "symbol", "settlement"}
    missing = required.difference(frame.columns)
    if missing:
        raise TqDerivedError(f"TQ settlement data missing columns: {sorted(missing)}")
    frame["trade_date"] = pd.to_datetime(
        frame["datetime"].astype(str), format="%Y%m%d", errors="coerce"
    ).dt.normalize()
    frame["contract_code"] = frame["symbol"].astype(str)
    frame["settlement"] = pd.to_numeric(frame["settlement"], errors="coerce")
    return frame.loc[
        frame["trade_date"].notna() & frame["settlement"].gt(0.0),
        ["trade_date", "contract_code", "settlement"],
    ].drop_duplicates(["trade_date", "contract_code"], keep="last")


def _query_settlements(
    api: Any,
    symbols: list[str],
    start_date: date,
    end_date: date,
) -> pd.DataFrame:
    days = max(40, (end_date - start_date).days * 2 + 20)
    try:
        return _normalise_settlements(
            api.query_symbol_settlement(symbols, days=days, start_dt=start_date)
        )
    except Exception:
        parts: list[pd.DataFrame] = []
        for symbol in symbols:
            try:
                item = _normalise_settlements(
                    api.query_symbol_settlement(symbol, days=days, start_dt=start_date)
                )
            except Exception:
                continue
            if not item.empty:
                parts.append(item)
        return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(
            columns=["trade_date", "contract_code", "settlement"]
        )


def _load_product_contracts(
    api: Any,
    product: str,
    fetch_start: date,
    end_date: date,
) -> tuple[pd.DataFrame, list[str]]:
    specification = PRODUCTS[product]
    symbols = sorted(
        api.query_quotes(
            ins_class="FUTURE",
            exchange_id=specification["exchange"],
            product_id=specification["product_id"],
        )
    )
    if not symbols:
        raise TqDerivedError(f"TQ returned no contracts for {product}")
    info = pd.DataFrame(api.query_symbol_info(symbols)).copy()
    expiries = _expiry_series(info)
    lower = pd.Timestamp(fetch_start)
    upper = pd.Timestamp(end_date) + pd.Timedelta(days=730)
    candidates = [
        symbol
        for symbol in symbols
        if symbol in expiries.index and lower <= pd.Timestamp(expiries[symbol]) <= upper
    ]
    if not candidates:
        raise TqDerivedError(f"TQ returned no usable contracts for {product}")
    history_days = max(80, min(180, (end_date - fetch_start).days * 2 + 40))
    serials = {
        symbol: api.get_kline_serial(symbol, TQ_DAILY_SECONDS, data_length=history_days)
        for symbol in candidates
    }
    # get_kline_serial blocks until its historical frame is initialized. A
    # wait_update here would wait for a live quote change while the market is
    # closed and would multiply that delay by five products.
    parts = [
        _clean_serial(pd.DataFrame(serial), symbol, pd.Timestamp(expiries[symbol]))
        for symbol, serial in serials.items()
    ]
    frame = pd.concat([part for part in parts if not part.empty], ignore_index=True)
    frame = frame.loc[
        frame["trade_date"].ge(lower) & frame["trade_date"].le(pd.Timestamp(end_date))
    ].copy()
    if frame.empty:
        raise TqDerivedError(f"TQ returned no completed rows for {product}")
    frame["product"] = product
    return frame, candidates


def _serialize_date(value: Any) -> str:
    return pd.Timestamp(value).strftime("%Y-%m-%d")


def _records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for row in frame.to_dict(orient="records"):
        records.append(
            {
                key: (
                    _serialize_date(value)
                    if isinstance(value, (pd.Timestamp, datetime, date))
                    else (None if pd.isna(value) else value)
                )
                for key, value in row.items()
            }
        )
    return records


def _fetch_product(parameters: dict[str, Any]) -> dict[str, Any]:
    try:
        from tqsdk import TqApi, TqAuth
    except ImportError as exc:
        raise TqDerivedError("tqsdk is not installed in the Vercel runtime") from exc

    api = None
    product = str(parameters["product"])
    started = time.monotonic()
    try:
        LOGGER.info("TQ 品种请求开始 product=%s", product)
        api = TqApi(auth=TqAuth(TQ_USERNAME, TQ_PASSWORD))
        LOGGER.info(
            "TQ 连接建立 product=%s elapsed=%.2fs",
            product,
            time.monotonic() - started,
        )
        start_date = parameters["start_date"]
        end_date = parameters["end_date"]
        state = parameters["state"]
        carry = pd.DataFrame()

        frame, symbols = _load_product_contracts(
            api,
            product,
            state["trade_date"],
            end_date,
        )
        LOGGER.info(
            "TQ 合约日线就绪 product=%s contracts=%d elapsed=%.2fs",
            product,
            len(symbols),
            time.monotonic() - started,
        )
        if product == "M":
            settlements = _query_settlements(
                api,
                symbols,
                state["trade_date"],
                end_date,
            )
            frame = frame.merge(
                settlements,
                on=["trade_date", "contract_code"],
                how="left",
            )
            LOGGER.info(
                "TQ 结算价就绪 product=%s rows=%d elapsed=%.2fs",
                product,
                len(settlements),
                time.monotonic() - started,
            )
        else:
            frame["settlement"] = math.nan

        rule = build_rule0_mapping(
            frame,
            seed_contract=state["main_contract_code"],
            seen_contracts=state["seen_contracts"],
        )
        continuous = build_pre_adjusted_series(frame, rule.mapping)
        derived = continuous.rows.loc[
            (continuous.rows.index >= pd.Timestamp(start_date))
            & (continuous.rows.index <= pd.Timestamp(end_date))
        ].reset_index()
        derived["product"] = product

        rolls = continuous.roll_events.copy()
        if not rolls.empty:
            rolls = rolls.loc[
                rolls["effective_date"].ge(pd.Timestamp(start_date))
            ].copy()
            rolls["product"] = product
        else:
            rolls = pd.DataFrame(
                columns=[
                    "effective_date",
                    "from_contract",
                    "to_contract",
                    "prefix_scale_factor",
                    "product",
                ]
            )

        if product == "M":
            carry = build_soymeal_roll_yields(frame, rule.mapping)
            if not carry.empty:
                carry = carry.loc[
                    (carry.index >= pd.Timestamp(start_date))
                    & (carry.index <= pd.Timestamp(end_date))
                ].reset_index()

        raw = frame.loc[
            frame["trade_date"].ge(pd.Timestamp(start_date)),
            [
                "trade_date",
                "product",
                "contract_code",
                "expiry_date",
                "close",
                "settlement",
                "volume",
                "open_interest",
            ],
        ].sort_values(["trade_date", "product", "contract_code"])
        through = derived["trade_date"].max() if not derived.empty else None
        if product == "M" and through is not None and not carry.empty:
            through = min(pd.Timestamp(through), pd.Timestamp(carry["trade_date"].max()))
        LOGGER.info(
            "TQ 品种派生完成 product=%s raw=%d derived=%d elapsed=%.2fs",
            product,
            len(raw),
            len(derived),
            time.monotonic() - started,
        )
        return {
            "product": product,
            "raw_contract_rows": _records(raw),
            "derived_rows": _records(derived),
            "roll_events": _records(rolls),
            "carry_rows": _records(carry),
            "through_date": _serialize_date(through) if through is not None else None,
        }
    except (TqDerivedError, DominantRuleError, ContinuousAdjustmentError, RollYieldError):
        raise
    except Exception as exc:
        raise TqDerivedError(f"TqSdk request failed: {type(exc).__name__}: {exc}") from exc
    finally:
        if api is not None:
            try:
                api.close()
            except Exception as exc:
                LOGGER.warning(
                    "TQ 连接关闭失败 product=%s error=%s",
                    product,
                    type(exc).__name__,
                )
        LOGGER.info(
            "TQ 品种请求结束 product=%s elapsed=%.2fs",
            product,
            time.monotonic() - started,
        )


async def fetch_tq_product(parameters: dict[str, Any]) -> dict[str, Any]:
    async with TQ_DIRECT_SEMAPHORE:
        return await run_in_threadpool(_fetch_product, parameters)
