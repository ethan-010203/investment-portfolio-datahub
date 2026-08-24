"""RiceQuant-compatible rule=0 dominant-contract selection."""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import pandas as pd

RULE0_THRESHOLD = 1.1
REQUIRED_COLUMNS = {
    "trade_date",
    "contract_code",
    "expiry_date",
    "close",
    "volume",
    "open_interest",
}


class DominantRuleError(RuntimeError):
    pass


@dataclass(frozen=True)
class Rule0Result:
    mapping: pd.Series
    roll_events: pd.DataFrame


def _validated_bars(bars: pd.DataFrame) -> pd.DataFrame:
    frame = pd.DataFrame(bars).copy()
    missing = REQUIRED_COLUMNS.difference(frame.columns)
    if missing:
        raise DominantRuleError(f"contract bars missing columns: {sorted(missing)}")
    frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce").dt.normalize()
    frame["expiry_date"] = pd.to_datetime(frame["expiry_date"], errors="coerce").dt.normalize()
    frame["contract_code"] = frame["contract_code"].fillna("").astype(str).str.strip()
    for column in ("close", "volume", "open_interest"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.loc[
        frame["trade_date"].notna()
        & frame["expiry_date"].notna()
        & frame["contract_code"].ne("")
        & frame["close"].gt(0.0)
        & frame["open_interest"].ge(0.0)
        & frame["volume"].ge(0.0)
    ].copy()
    if frame.duplicated(["trade_date", "contract_code"]).any():
        raise DominantRuleError("contract bars contain duplicate date/contract rows")
    return frame.sort_values(["trade_date", "contract_code"]).reset_index(drop=True)


def _eligible(frame: pd.DataFrame, trade_date: pd.Timestamp) -> pd.DataFrame:
    result = frame.loc[
        frame["expiry_date"].ge(trade_date)
        & frame["close"].gt(0.0)
        & frame["open_interest"].gt(0.0)
    ].copy()
    return result.sort_values(
        ["open_interest", "volume", "expiry_date", "contract_code"],
        ascending=[False, False, True, True],
    ).set_index("contract_code", drop=False)


def build_rule0_mapping(
    bars: pd.DataFrame,
    *,
    seed_contract: str | None = None,
    seen_contracts: Iterable[str] = (),
    threshold: float = RULE0_THRESHOLD,
) -> Rule0Result:
    """Select a dominant contract using close OI and next-day switch timing.

    ``seed_contract`` is the dominant contract on the first bar date. The first
    date must therefore be the stored seed date, not the first requested output
    date. A trigger observed on that seed date is applied on the next date.
    """

    if not math.isfinite(threshold) or threshold <= 1.0:
        raise ValueError("rule=0 threshold must be finite and greater than one")
    frame = _validated_bars(bars)
    dates = pd.DatetimeIndex(frame["trade_date"].drop_duplicates().sort_values())
    if dates.empty:
        raise DominantRuleError("contract bars contain no usable dates")
    by_date = {
        pd.Timestamp(value): group
        for value, group in frame.groupby("trade_date", sort=True)
    }
    first = _eligible(by_date[dates[0]], dates[0])
    requested_seed = str(seed_contract or "").strip()
    current = requested_seed or (str(first.iloc[0]["contract_code"]) if not first.empty else "")
    if not current:
        raise DominantRuleError(f"no eligible contract on {dates[0]:%Y-%m-%d}")

    used = {str(value).strip() for value in seen_contracts if str(value).strip()}
    used.add(current)
    pending: str | None = None
    selected: list[str] = []
    audit: list[dict[str, Any]] = []

    for position, trade_date in enumerate(dates):
        if pending is not None:
            current = pending
            used.add(current)
            pending = None
        eligible = _eligible(by_date[trade_date], trade_date)
        if current not in eligible.index:
            raise DominantRuleError(
                f"dominant contract {current} has no valid bar on {trade_date:%Y-%m-%d}"
            )
        selected.append(current)
        if position + 1 >= len(dates):
            continue
        candidates = eligible.loc[
            eligible["contract_code"].ne(current)
            & ~eligible["contract_code"].isin(used)
        ]
        if candidates.empty:
            continue
        candidate = str(candidates.iloc[0]["contract_code"])
        current_oi = float(eligible.at[current, "open_interest"])
        candidate_oi = float(candidates.iloc[0]["open_interest"])
        if candidate_oi > threshold * current_oi:
            pending = candidate
            audit.append(
                {
                    "trigger_date": trade_date,
                    "effective_date": dates[position + 1],
                    "from_contract": current,
                    "to_contract": candidate,
                    "current_open_interest": current_oi,
                    "candidate_open_interest": candidate_oi,
                    "open_interest_ratio": candidate_oi / current_oi,
                }
            )

    mapping = pd.Series(selected, index=dates, name="main_contract")
    return Rule0Result(mapping=mapping, roll_events=pd.DataFrame(audit))
