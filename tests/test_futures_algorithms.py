from __future__ import annotations

import math

import pandas as pd
import pytest
from fastapi import HTTPException

from api.continuous_adjust import build_pre_adjusted_series
from api.dominant_rule0 import build_rule0_mapping
from api.quantity_normalizer import normalize_quantity
from api.soymeal_roll_yield import build_soymeal_roll_yields
from api.tq_derived import parse_tq_product_request


def bars() -> pd.DataFrame:
    rows = []
    values = {
        "2024-01-02": {"DCE.m2405": (100.0, 100.0), "DCE.m2409": (110.0, 90.0)},
        "2024-01-03": {"DCE.m2405": (101.0, 80.0), "DCE.m2409": (111.0, 100.0)},
        "2024-01-04": {"DCE.m2405": (102.0, 200.0), "DCE.m2409": (112.0, 120.0)},
        "2024-01-05": {"DCE.m2405": (103.0, 210.0), "DCE.m2409": (113.0, 130.0)},
    }
    for date, contracts in values.items():
        for contract, (close, open_interest) in contracts.items():
            rows.append(
                {
                    "trade_date": date,
                    "contract_code": contract,
                    "expiry_date": "2024-05-31" if contract.endswith("2405") else "2024-09-30",
                    "close": close,
                    "settlement": close,
                    "volume": open_interest * 2,
                    "open_interest": open_interest,
                }
            )
    return pd.DataFrame(rows)


def test_rule0_switches_next_day_and_never_switches_back():
    result = build_rule0_mapping(bars())
    assert result.mapping.tolist() == [
        "DCE.m2405",
        "DCE.m2405",
        "DCE.m2409",
        "DCE.m2409",
    ]
    event = result.roll_events.iloc[0]
    assert event["trigger_date"] == pd.Timestamp("2024-01-03")
    assert event["effective_date"] == pd.Timestamp("2024-01-04")


def test_prev_close_ratio_adjustment_uses_new_leg_previous_close():
    mapping = build_rule0_mapping(bars()).mapping
    result = build_pre_adjusted_series(bars(), mapping)
    expected_prefix_factor = 111.0 / 101.0
    assert math.isclose(result.rows.loc["2024-01-03", "adjusted_close"], 111.0)
    assert math.isclose(
        result.rows.loc["2024-01-02", "adjusted_close"],
        100.0 * expected_prefix_factor,
    )
    assert result.rows.loc["2024-01-04", "adjusted_close"] == 112.0
    assert result.roll_events.iloc[0]["prefix_scale_factor"] == pytest.approx(
        expected_prefix_factor
    )


def test_roll_yield_uses_settlement_and_expiry_distance():
    mapping = build_rule0_mapping(bars()).mapping
    result = build_soymeal_roll_yields(bars(), mapping)
    expected = (100.0 / 110.0 - 1.0) * 365.0 / 122.0
    assert result.loc["2024-01-02", "main_sub_annualized_yield"] == pytest.approx(expected)
    assert result.loc["2024-01-02", "near_main_annualized_yield"] == 0.0


def test_quantity_cutover_is_explicit():
    assert normalize_quantity("2019-12-31", 12) == 24.0
    assert normalize_quantity("2020-01-01", 12) == 12.0


def test_incremental_request_requires_one_valid_product_state():
    with pytest.raises(HTTPException) as caught:
        parse_tq_product_request(
            {
                "start_date": "2026-08-01",
                "end_date": "2026-08-02",
                "product": "M",
                "state": {},
            }
        )
    assert caught.value.status_code == 422


def test_incremental_request_parses_one_product_only():
    result = parse_tq_product_request(
        {
            "start_date": "2026-08-01",
            "end_date": "2026-08-02",
            "product": "RM",
            "state": {
                "trade_date": "2026-07-31",
                "main_contract_code": "CZCE.RM609",
                "seen_contracts": ["CZCE.RM605", "CZCE.RM609"],
            },
        }
    )
    assert result["product"] == "RM"
    assert set(result["state"]) == {
        "trade_date",
        "main_contract_code",
        "seen_contracts",
    }
