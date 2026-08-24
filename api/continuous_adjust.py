"""Build selected raw and prev-close-ratio pre-adjusted futures series."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import pandas as pd


class ContinuousAdjustmentError(RuntimeError):
    pass


@dataclass(frozen=True)
class ContinuousResult:
    rows: pd.DataFrame
    roll_events: pd.DataFrame


def build_pre_adjusted_series(bars: pd.DataFrame, mapping: pd.Series) -> ContinuousResult:
    """Build latest-anchored ``pre``/``prev_close_ratio`` adjusted closes."""

    frame = pd.DataFrame(bars).copy()
    frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce").dt.normalize()
    frame["contract_code"] = frame["contract_code"].astype(str)
    indexed = frame.set_index(["trade_date", "contract_code"]).sort_index()
    dates = pd.DatetimeIndex(mapping.index).sort_values()
    if dates.empty:
        raise ContinuousAdjustmentError("dominant mapping is empty")
    selected: list[dict[str, Any]] = []
    adjusted_returns = pd.Series(0.0, index=dates, dtype=float)
    rolls: list[dict[str, Any]] = []

    for position, trade_date in enumerate(dates):
        contract = str(mapping.loc[trade_date])
        key = (trade_date, contract)
        if key not in indexed.index:
            raise ContinuousAdjustmentError(
                f"missing {contract} bar on {trade_date:%Y-%m-%d}"
            )
        row = indexed.loc[key]
        close = float(row["close"])
        if not math.isfinite(close) or close <= 0.0:
            raise ContinuousAdjustmentError(
                f"invalid {contract} close on {trade_date:%Y-%m-%d}"
            )
        selected.append(
            {
                "trade_date": trade_date,
                "main_contract": contract,
                "raw_close": close,
                "volume": float(row["volume"]),
                "open_interest": float(row["open_interest"]),
            }
        )
        if position == 0:
            continue
        previous_date = dates[position - 1]
        previous_contract = str(mapping.loc[previous_date])
        comparison_key = (previous_date, contract)
        if comparison_key not in indexed.index:
            raise ContinuousAdjustmentError(
                f"missing new-leg previous close for {contract} on {previous_date:%Y-%m-%d}"
            )
        comparison_close = float(indexed.at[comparison_key, "close"])
        adjusted_returns.loc[trade_date] = close / comparison_close - 1.0
        if contract != previous_contract:
            old_close = float(indexed.at[(previous_date, previous_contract), "close"])
            rolls.append(
                {
                    "effective_date": trade_date,
                    "from_contract": previous_contract,
                    "to_contract": contract,
                    "prefix_scale_factor": comparison_close / old_close,
                    "old_contract_previous_close": old_close,
                    "new_contract_previous_close": comparison_close,
                }
            )

    adjusted = pd.Series(index=dates, dtype=float)
    adjusted.iloc[-1] = float(selected[-1]["raw_close"])
    for position in range(len(dates) - 2, -1, -1):
        growth = 1.0 + float(adjusted_returns.iloc[position + 1])
        if not math.isfinite(growth) or growth <= 0.0:
            raise ContinuousAdjustmentError(
                f"invalid adjusted return after {dates[position]:%Y-%m-%d}"
            )
        adjusted.iloc[position] = adjusted.iloc[position + 1] / growth

    result = pd.DataFrame(selected).set_index("trade_date")
    result["adjusted_close"] = adjusted
    return ContinuousResult(rows=result, roll_events=pd.DataFrame(rolls))
