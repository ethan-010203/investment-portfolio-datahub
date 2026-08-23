from datetime import date

from api.wasde_parser import _next_business_day, _number_value


def test_next_business_day_skips_weekend():
    assert _next_business_day(date(2026, 8, 21)) == date(2026, 8, 24)


def test_number_value_handles_commas_and_numeric_cells():
    assert _number_value("1,234.5") == 1234.5
    assert _number_value(12) == 12.0
