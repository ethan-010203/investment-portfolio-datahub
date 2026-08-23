"""Normalize TQSDK futures activity fields to the formal hand convention."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

import math
import pandas as pd


QUANTITY_CUTOVER = pd.Timestamp("2020-01-01")


def quantity_multiplier(trade_date: Any) -> float:
    """Return the historical TQ-to-formal hand multiplier for one trade date."""

    value = pd.Timestamp(trade_date).normalize()
    return 2.0 if value < QUANTITY_CUTOVER else 1.0


def normalize_quantity(trade_date: date | datetime | str | pd.Timestamp, value: Any) -> float:
    """Normalize volume/open interest and reject unusable market values."""

    result = float(value) * quantity_multiplier(trade_date)
    if not math.isfinite(result) or result < 0.0:
        raise ValueError("futures quantity must be finite and non-negative")
    return result
