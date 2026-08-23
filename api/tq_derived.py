from __future__ import annotations

import asyncio
import math
import re
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
TQ_AUXILIARY_SYMBOLS = {
    "RM888": "KQ.m@CZCE.RM",
    "Y888": "KQ.m@DCE.y",
    "C888": "KQ.m@DCE.c",
    "JD888": "KQ.m@DCE.jd",
}
TQ_DAILY_SECONDS = 86_400
TQ_DATA_LENGTH = 160
TQ_WAIT_SECONDS = 35
TQ_CURVE_START = date(2020, 9, 15)
# Keep two independent TQSDK sessions in flight without blocking the event loop.
# The limit avoids creating an unbounded number of authenticated sessions when
# a scheduled retry and a manual request arrive at the same time.
TQ_DIRECT_SEMAPHORE = asyncio.Semaphore(2)


class TqDerivedError(RuntimeError):
    pass


def _parse_date(value: Any, field_name: str) -> date:
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(status_code=422, detail=f"{field_name} is required")
    normalized = value.strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", normalized):
        raise HTTPException(
            status_code=422,
            detail=f"{field_name} must use YYYY-MM-DD",
        )
    try:
        return datetime.strptime(normalized, "%Y-%m-%d").date()
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
    if start < TQ_CURVE_START:
        raise HTTPException(
            status_code=422,
            detail=(
                "TQ soybean curve data is available from "
                f"{TQ_CURVE_START.isoformat()}"
            ),
        )

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

    series_seeds = payload.get("series_seeds")
    if not isinstance(series_seeds, dict):
        raise HTTPException(status_code=422, detail="series_seeds is required")
    normalized_series_seeds: dict[str, dict[str, Any]] = {}
    for name in TQ_AUXILIARY_SYMBOLS:
        item = series_seeds.get(name)
        if not isinstance(item, dict):
            raise HTTPException(status_code=422, detail=f"series_seeds.{name} is required")
        try:
            value = float(item["value"])
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=422,
                detail=f"series_seeds.{name}.value must be numeric",
            ) from exc
        if not math.isfinite(value) or value <= 0:
            raise HTTPException(
                status_code=422,
                detail=f"series_seeds.{name}.value must be positive",
            )
        normalized_series_seeds[name] = {"value": value}

    return {
        "start_date": start,
        "end_date": end,
        "seed": {
            "trade_date": seed_date,
            "close": seed_close,
            "m888_close": seed_m888_close,
            "main_contract_code": seed_contract.strip(),
        },
        "series_seeds": normalized_series_seeds,
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


def _expiry_dates(info: pd.DataFrame) -> pd.Series:
    required = {"instrument_id", "expire_datetime"}
    missing = required.difference(info.columns)
    if missing:
        raise TqDerivedError(f"Tq contract info missing columns: {sorted(missing)}")
    seconds = pd.to_numeric(info["expire_datetime"], errors="coerce")
    values = pd.to_datetime(seconds, unit="s", utc=True, errors="coerce")
    values = values.dt.tz_convert("Asia/Shanghai").dt.tz_localize(None).dt.normalize()
    result = pd.Series(values.to_numpy(), index=info["instrument_id"].astype(str))
    return result.dropna()


def _normalise_settlement(raw: pd.DataFrame) -> pd.DataFrame:
    frame = pd.DataFrame(raw).copy()
    required = {"datetime", "symbol", "settlement"}
    missing = required.difference(frame.columns)
    if missing:
        raise TqDerivedError(f"Tq settlement data missing columns: {sorted(missing)}")
    frame["date"] = pd.to_datetime(
        frame["datetime"].astype(str), format="%Y%m%d", errors="coerce"
    ).dt.normalize()
    frame["symbol"] = frame["symbol"].astype(str)
    frame["settlement"] = pd.to_numeric(frame["settlement"], errors="coerce")
    frame = frame.loc[
        frame["date"].notna() & frame["settlement"].gt(0)
    ].copy()
    return frame[["date", "symbol", "settlement"]].drop_duplicates(
        ["date", "symbol"], keep="last"
    )


def _load_curve_inputs(
    api: Any,
    mapping: pd.DataFrame,
    start_date: date,
    end_date: date,
) -> tuple[dict[str, pd.DataFrame], dict[str, pd.DataFrame], pd.Series]:
    """订阅当前请求窗口所需的合约 K 线、结算价和到期日。"""
    symbols = sorted(
        api.query_quotes(
            ins_class="FUTURE",
            exchange_id="DCE",
            product_id="m",
        )
    )
    if not symbols:
        raise TqDerivedError("Tq returned no DCE soybean-meal contracts")
    info = pd.DataFrame(api.query_symbol_info(symbols)).copy()
    expiries = _expiry_dates(info)
    start = pd.Timestamp(start_date)
    candidates = [
        symbol for symbol in symbols
        if symbol in expiries.index and pd.Timestamp(expiries[symbol]) >= start
    ]
    if not candidates:
        raise TqDerivedError("Tq returned no usable soybean contracts for the curve")

    serials = {
        symbol: api.get_kline_serial(
            symbol,
            TQ_DAILY_SECONDS,
            data_length=TQ_DATA_LENGTH,
        )
        for symbol in candidates
    }
    api.wait_update(deadline=time.time() + TQ_WAIT_SECONDS)

    concrete: dict[str, pd.DataFrame] = {}
    settlements: dict[str, pd.DataFrame] = {}
    settlement_days = max(30, (end_date - start_date).days * 2 + 10)
    for symbol, serial in serials.items():
        cleaned = _clean_kline(
            pd.DataFrame(serial).copy(deep=True),
            mapping,
            symbol=symbol,
        )
        if not cleaned.empty:
            concrete[symbol] = cleaned

    # A short batch is materially faster than one settlement request per leg.
    try:
        settlement_all = _normalise_settlement(
            api.query_symbol_settlement(
                candidates,
                days=settlement_days,
                start_dt=start_date,
            )
        )
        for symbol, settlement in settlement_all.groupby("symbol"):
            if not settlement.empty:
                settlements[symbol] = settlement.copy()
    except Exception:
        # Keep a narrow fallback for servers that reject list-valued requests.
        for symbol in candidates:
            try:
                settlement = _normalise_settlement(
                    api.query_symbol_settlement(
                        symbol,
                        days=settlement_days,
                        start_dt=start_date,
                    )
                )
            except Exception:
                settlement = pd.DataFrame(columns=["date", "symbol", "settlement"])
            if not settlement.empty:
                settlements[symbol] = settlement
    return concrete, settlements, expiries


def _load_continuous_inputs(
    api: Any,
    continuous_symbol: str,
    seed_date: pd.Timestamp,
) -> tuple[pd.DataFrame, dict[str, pd.DataFrame]]:
    """读取一个连续合约及其主力腿，用于稳定递推 888 序列。"""
    raw_main = api.get_kline_serial(
        continuous_symbol,
        TQ_DAILY_SECONDS,
        data_length=TQ_DATA_LENGTH,
    )
    mapping = _normalise_mapping(
        pd.DataFrame(api.query_his_cont_quotes(continuous_symbol, n=TQ_DATA_LENGTH)).copy(deep=True)
    )
    main = _clean_kline(pd.DataFrame(raw_main).copy(deep=True), mapping)
    if main.empty:
        raise TqDerivedError(f"Tq returned no completed bars for {continuous_symbol}")
    main = main.merge(mapping, on="date", how="left")
    main["underlying_symbol"] = main["underlying_symbol"].fillna("").astype(str)
    main = main.loc[main["underlying_symbol"].ne("")].sort_values("date")
    active = main.loc[main["date"] >= seed_date]
    symbols = list(dict.fromkeys(active["underlying_symbol"].tolist()))
    if not symbols:
        raise TqDerivedError(
            f"Tq returned no active contract legs for {continuous_symbol}"
        )
    serials = {
        symbol: api.get_kline_serial(
            symbol,
            TQ_DAILY_SECONDS,
            data_length=TQ_DATA_LENGTH,
        )
        for symbol in symbols
    }
    concrete: dict[str, pd.DataFrame] = {}
    for symbol, serial in serials.items():
        cleaned = _clean_kline(
            pd.DataFrame(serial).copy(deep=True),
            mapping,
            symbol=symbol,
        )
        if not cleaned.empty:
            concrete[symbol] = cleaned
    return main, concrete


def _build_continuous_series(
    main: pd.DataFrame,
    concrete: dict[str, pd.DataFrame],
    seed_date: pd.Timestamp,
    seed_value: float,
) -> pd.Series:
    """从上次已存的 888 值继续做比例复权，避免窗口重算改变历史尺度。"""
    close_by_key: dict[tuple[pd.Timestamp, str], float] = {}
    for symbol, frame in concrete.items():
        for _, row in frame.iterrows():
            value = float(row["close"])
            if math.isfinite(value) and value > 0:
                close_by_key[(pd.Timestamp(row["date"]), symbol)] = value

    rows = main.loc[main["date"] >= seed_date].sort_values("date")
    if rows.empty:
        return pd.Series(dtype=float)
    first_symbol = str(rows.iloc[0]["underlying_symbol"])
    previous_raw = close_by_key.get((seed_date, first_symbol))
    if previous_raw is None:
        raise TqDerivedError(
            f"Missing seed close for {first_symbol} on {seed_date.date()}"
        )
    previous_date = seed_date
    previous_symbol = first_symbol
    previous_value = float(seed_value)
    result: dict[pd.Timestamp, float] = {seed_date: previous_value}
    for _, row in rows.iterrows():
        current_date = pd.Timestamp(row["date"])
        if current_date <= seed_date:
            continue
        current_symbol = str(row["underlying_symbol"])
        current_raw = close_by_key.get((current_date, current_symbol))
        if current_raw is None or current_raw <= 0:
            raise TqDerivedError(
                f"Missing close for {current_symbol} on {current_date.date()}"
            )
        if current_symbol == previous_symbol:
            daily_return = current_raw / previous_raw - 1.0
        else:
            roll_previous = close_by_key.get((previous_date, current_symbol))
            if roll_previous is None or roll_previous <= 0:
                raise TqDerivedError(
                    f"Missing new-contract close for {previous_symbol}->{current_symbol} "
                    f"on {current_date.date()}"
                )
            daily_return = current_raw / roll_previous - 1.0
        previous_value *= 1.0 + daily_return
        if not math.isfinite(previous_value) or previous_value <= 0:
            raise TqDerivedError(f"Invalid adjusted close on {current_date.date()}")
        result[current_date] = previous_value
        previous_date = current_date
        previous_symbol = current_symbol
        previous_raw = current_raw
    return pd.Series(result, dtype=float).sort_index()


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
        concrete, settlements, expiries = _load_curve_inputs(
            api,
            mapping,
            start_date,
            end_date,
        )
        auxiliary: dict[str, pd.Series] = {}
        seed_date = pd.Timestamp(seed["trade_date"])
        for name, symbol in TQ_AUXILIARY_SYMBOLS.items():
            auxiliary_main, auxiliary_concrete = _load_continuous_inputs(
                api,
                symbol,
                seed_date,
            )
            auxiliary[name] = _build_continuous_series(
                auxiliary_main,
                auxiliary_concrete,
                seed_date,
                float(seed["series_seeds"][name]["value"]),
            )
        return _build_rows(main, concrete, settlements, expiries, auxiliary, seed)
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
    settlements: dict[str, pd.DataFrame],
    expiries: pd.Series,
    auxiliary: dict[str, pd.Series],
    seed: dict[str, Any],
) -> dict[str, Any]:
    close_by_key: dict[tuple[pd.Timestamp, str], float] = {}
    for symbol, frame in concrete.items():
        for _, row in frame.iterrows():
            value = float(row["close"])
            if math.isfinite(value) and value > 0:
                close_by_key[(pd.Timestamp(row["date"]), symbol)] = value

    settlement_by_key: dict[tuple[pd.Timestamp, str], float] = {}
    for symbol, frame in settlements.items():
        for _, row in frame.iterrows():
            value = float(row["settlement"])
            if math.isfinite(value) and value > 0:
                settlement_by_key[(pd.Timestamp(row["date"]), symbol)] = value

    close_oi_by_key: dict[tuple[pd.Timestamp, str], float] = {}
    volume_by_key: dict[tuple[pd.Timestamp, str], float] = {}
    for symbol, frame in concrete.items():
        for _, row in frame.iterrows():
            key = (pd.Timestamp(row["date"]), symbol)
            close_oi = pd.to_numeric(pd.Series([row.get("close_oi")]), errors="coerce").iloc[0]
            volume = pd.to_numeric(pd.Series([row.get("volume")]), errors="coerce").iloc[0]
            close_oi_by_key[key] = float(close_oi) if pd.notna(close_oi) else -1.0
            volume_by_key[key] = float(volume) if pd.notna(volume) else -1.0

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

        volume = pd.to_numeric(pd.Series([row.get("volume")]), errors="coerce").iloc[0]
        open_interest = pd.to_numeric(pd.Series([row.get("close_oi")]), errors="coerce").iloc[0]
        if not pd.notna(volume) or not math.isfinite(float(volume)) or float(volume) < 0:
            raise TqDerivedError(f"Invalid M888 volume on {current_date.date()}")
        if not pd.notna(open_interest) or not math.isfinite(float(open_interest)) or float(open_interest) < 0:
            raise TqDerivedError(f"Invalid M888 open interest on {current_date.date()}")

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
            daily_return = new_current / new_previous - 1.0

        m888_close = previous_m888 * (1.0 + daily_return)
        if not math.isfinite(m888_close) or m888_close <= 0:
            raise TqDerivedError(f"Invalid M888 close on {current_date.date()}")

        if current_symbol not in expiries.index:
            raise TqDerivedError(f"Missing expiry for main contract {current_symbol}")
        valid_contracts = [
            symbol
            for symbol in expiries.index
            if pd.Timestamp(expiries[symbol]) >= current_date
            and (current_date, symbol) in settlement_by_key
        ]
        if current_symbol not in valid_contracts:
            raise TqDerivedError(
                f"Missing main settlement for {current_symbol} on {current_date.date()}"
            )
        later_contracts = [
            symbol
            for symbol in valid_contracts
            if pd.Timestamp(expiries[symbol]) > pd.Timestamp(expiries[current_symbol])
        ]
        if not later_contracts:
            raise TqDerivedError(
                f"Missing later-maturity soybean contract on {current_date.date()}"
            )

        def activity_key(symbol: str) -> tuple[float, float, int]:
            return (
                close_oi_by_key.get((current_date, symbol), -1.0),
                volume_by_key.get((current_date, symbol), -1.0),
                pd.Timestamp(expiries[symbol]).toordinal(),
            )

        sub_symbol = max(later_contracts, key=activity_key)
        near_symbol = min(
            valid_contracts,
            key=lambda symbol: pd.Timestamp(expiries[symbol]),
        )

        def annualized_roll(from_symbol: str, to_symbol: str) -> float:
            from_price = settlement_by_key[(current_date, from_symbol)]
            to_price = settlement_by_key[(current_date, to_symbol)]
            # The near-main series is legitimately zero while both legs are
            # the same contract; there is no maturity interval to annualize.
            if from_symbol == to_symbol:
                return 0.0
            raw_yield = from_price / to_price - 1.0
            maturity_days = abs(
                int(
                    (
                        pd.Timestamp(expiries[from_symbol])
                        - pd.Timestamp(expiries[to_symbol])
                    ).days
                )
            )
            if maturity_days <= 0:
                raise TqDerivedError(
                    f"Invalid expiry interval {from_symbol}->{to_symbol} on {current_date.date()}"
                )
            result = raw_yield * 365.0 / maturity_days
            if not math.isfinite(result):
                raise TqDerivedError(
                    f"Invalid annualized roll yield on {current_date.date()}"
                )
            return result

        main_sub_annualized = annualized_roll(current_symbol, sub_symbol)
        near_main_annualized = annualized_roll(near_symbol, current_symbol)
        auxiliary_values: dict[str, float] = {}
        for name, series in auxiliary.items():
            available = series.loc[:current_date]
            value = available.iloc[-1] if not available.empty else None
            if value is None or not math.isfinite(float(value)) or float(value) <= 0:
                raise TqDerivedError(f"Missing {name} close on {current_date.date()}")
            auxiliary_values[name] = float(value)
        output.append(
            {
                "trade_date": current_date.strftime("%Y-%m-%d"),
                "main_contract_code": current_symbol,
                "close": current_close,
                "m888_close": m888_close,
                "m888_volume": float(volume),
                "m888_open_interest": float(open_interest),
                "rm888_close": auxiliary_values["RM888"],
                "y888_close": auxiliary_values["Y888"],
                "c888_close": auxiliary_values["C888"],
                "jd888_close": auxiliary_values["JD888"],
                "main_sub_annualized_yield": main_sub_annualized,
                "near_main_annualized_yield": near_main_annualized,
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
