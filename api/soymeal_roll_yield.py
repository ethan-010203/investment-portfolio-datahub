"""Soybean-meal term-structure inputs used by the formal L3 factor."""

from __future__ import annotations

import math
import pandas as pd


class RollYieldError(RuntimeError):
    pass


def _annualized(
    from_contract: str,
    to_contract: str,
    prices: pd.Series,
    expiries: pd.Series,
) -> float:
    if from_contract == to_contract:
        return 0.0
    from_price = float(prices[from_contract])
    to_price = float(prices[to_contract])
    days = abs(int((pd.Timestamp(expiries[from_contract]) - pd.Timestamp(expiries[to_contract])).days))
    if days <= 0 or from_price <= 0.0 or to_price <= 0.0:
        raise RollYieldError(f"invalid curve legs {from_contract}->{to_contract}")
    result = (from_price / to_price - 1.0) * 365.0 / days
    if not math.isfinite(result):
        raise RollYieldError(f"non-finite curve value {from_contract}->{to_contract}")
    return result


def build_soymeal_roll_yields(bars: pd.DataFrame, mapping: pd.Series) -> pd.DataFrame:
    """Build main/sub-main and near/main annualized settlement yields."""

    frame = pd.DataFrame(bars).copy()
    frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce").dt.normalize()
    frame["expiry_date"] = pd.to_datetime(frame["expiry_date"], errors="coerce").dt.normalize()
    frame["settlement"] = pd.to_numeric(frame["settlement"], errors="coerce")
    frame["open_interest"] = pd.to_numeric(frame["open_interest"], errors="coerce")
    frame["volume"] = pd.to_numeric(frame["volume"], errors="coerce")
    rows: list[dict[str, object]] = []
    for trade_date, day in frame.groupby("trade_date", sort=True):
        date = pd.Timestamp(trade_date)
        if date not in mapping.index:
            continue
        main = str(mapping.loc[date])
        valid = day.loc[
            day["expiry_date"].ge(date)
            & day["settlement"].gt(0.0)
            & day["open_interest"].ge(0.0)
            & day["volume"].ge(0.0)
        ].copy()
        if main not in set(valid["contract_code"]):
            continue
        valid = valid.set_index("contract_code", drop=False)
        later = valid.loc[valid["expiry_date"].gt(valid.at[main, "expiry_date"])]
        if later.empty:
            continue
        sub = str(
            later.reset_index(drop=True).sort_values(
                ["open_interest", "volume", "expiry_date", "contract_code"],
                ascending=[False, False, False, True],
            ).iloc[0]["contract_code"]
        )
        near = str(
            valid.reset_index(drop=True)
            .sort_values(["expiry_date", "contract_code"])
            .iloc[0]["contract_code"]
        )
        prices = valid["settlement"]
        expiries = valid["expiry_date"]
        rows.append(
            {
                "trade_date": date,
                "main_sub_annualized_yield": _annualized(main, sub, prices, expiries),
                "near_main_annualized_yield": _annualized(near, main, prices, expiries),
                "sub_contract": sub,
                "near_contract": near,
            }
        )
    return pd.DataFrame(rows).set_index("trade_date") if rows else pd.DataFrame()
