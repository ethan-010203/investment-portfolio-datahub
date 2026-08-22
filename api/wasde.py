from __future__ import annotations

from datetime import date, datetime, timedelta
from io import BytesIO
import re
from typing import Any
from urllib.parse import urljoin

import pandas as pd
import requests


WASDE_BASE_URL = (
    "https://esmis.nal.usda.gov/publication/"
    "world-agricultural-supply-and-demand-estimates"
)
WASDE_USER_AGENT = "investment-portfolio-datahub/1.0"


def _clean_label(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]+", " ", str(value).upper()).strip()


def _number(value: Any) -> float:
    match = re.search(r"[-+]?\d+(?:\.\d+)?", str(value).replace(",", ""))
    if not match:
        return float("nan")
    return float(match.group())


def _marketing_year(value: Any) -> str:
    match = re.search(r"(20\d{2})\D+(\d{2})", str(value))
    if not match:
        raise ValueError(f"WASDE marketing year is invalid: {value}")
    return f"{match.group(1)}/{match.group(2)}"


def _next_business_day(value: date) -> date:
    result = value + timedelta(days=1)
    while result.weekday() >= 5:
        result += timedelta(days=1)
    return result


def _parse_release_datetime(value: str) -> date:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).date()


def _discover_releases(start_date: date, end_date: date) -> dict[date, str]:
    found: dict[date, str] = {}
    minimum_needed = start_date - timedelta(days=10)
    for page in range(10):
        url = WASDE_BASE_URL if page == 0 else f"{WASDE_BASE_URL}?page={page}"
        response = requests.get(
            url,
            headers={"User-Agent": WASDE_USER_AGENT},
            timeout=30,
        )
        response.raise_for_status()
        matches = re.finditer(
            r'<a\s+href="([^"]+\.xls)"[^>]*>.*?'
            r'<time\s+datetime="([^"]+)"',
            response.text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        page_dates: list[date] = []
        for match in matches:
            release = _parse_release_datetime(match.group(2))
            available = _next_business_day(release)
            page_dates.append(available)
            if start_date <= available <= end_date:
                found[available] = urljoin(WASDE_BASE_URL, match.group(1))
        if page_dates and min(page_dates) < minimum_needed:
            break
    return found


def _parse_workbook(content: bytes, available_date: date) -> dict[str, Any]:
    domestic = pd.read_excel(BytesIO(content), sheet_name="Page 15", header=None)
    world = pd.read_excel(BytesIO(content), sheet_name="Page 28", header=None)

    domestic = domestic.dropna(axis=1, how="all").reset_index(drop=True)
    domestic.columns = range(domestic.shape[1])
    domestic_labels = domestic.iloc[:, 0].map(_clean_label)
    starts = domestic_labels[domestic_labels == "SOYBEANS"].index
    if starts.empty:
        raise ValueError("WASDE Page 15 has no SOYBEANS section")
    start = starts[0]
    ends = domestic_labels[
        (domestic_labels.str.startswith("SOYBEAN OIL"))
        & (domestic_labels.index > start)
    ].index
    stop = ends[0] - 1 if len(ends) else len(domestic) - 1
    block = domestic.loc[start:stop]
    numeric_columns = [
        column
        for column in block.columns[1:]
        if block[column].map(lambda value: _number(value) if pd.notna(value) else float("nan")).notna().sum() >= 3
    ]
    if not numeric_columns:
        raise ValueError("WASDE Page 15 has no projection column")
    projection_column = max(numeric_columns)
    block_labels = block.iloc[:, 0].map(_clean_label)

    def domestic_value(label: str) -> float:
        rows = block.loc[block_labels.str.startswith(label)]
        if rows.empty:
            raise ValueError(f"WASDE Page 15 has no {label} row")
        return _number(rows.iloc[-1][projection_column])

    world = world.dropna(axis=1, how="all").reset_index(drop=True)
    world.columns = range(world.shape[1])
    world_labels = world.iloc[:, 0].map(_clean_label)
    projected = world_labels[world_labels.str.match(r"^20\d\d \d\d PROJ")].index
    if len(projected):
        world_start = projected[-1]
    else:
        legacy = world_labels[world_labels.str.match(r"^20\d\d \d\d$")].index
        if legacy.empty:
            raise ValueError("WASDE Page 28 has no projection section")
        world_start = legacy[-1]
    world_block = world.loc[world_start:]
    headers = [_clean_label(value) for value in world_block.iloc[0].tolist()]

    def world_column(label: str) -> int:
        matches = [position for position, header in enumerate(headers) if header == label]
        if not matches:
            raise ValueError(f"WASDE Page 28 has no {label} column")
        return matches[0]

    imports_column = world_column("IMPORTS")
    crush_column = world_column("DOMESTIC CRUSH")
    entities = world_block.iloc[:, 0].replace("NAN", pd.NA).ffill().map(_clean_label)
    china = world_block.loc[entities == "CHINA"]
    valid = china.apply(
        lambda row: (
            pd.notna(row.iloc[imports_column])
            and pd.notna(row.iloc[crush_column])
            and _number(row.iloc[imports_column]) == _number(row.iloc[imports_column])
            and _number(row.iloc[crush_column]) == _number(row.iloc[crush_column])
        ),
        axis=1,
    )
    china = china.loc[valid]
    if china.empty:
        raise ValueError("WASDE Page 28 has no valid China row")
    current = china.iloc[-1]

    return {
        "available_date": available_date.isoformat(),
        "projection_marketing_year": _marketing_year(block.iloc[0, projection_column]),
        "us_exports": domestic_value("EXPORTS"),
        "us_crush": domestic_value("CRUSHINGS"),
        "china_imports": _number(current.iloc[imports_column]),
        "china_crush": _number(current.iloc[crush_column]),
    }


def fetch_wasde_rows(start_date: str, end_date: str) -> list[dict[str, Any]]:
    start = datetime.strptime(start_date, "%Y%m%d").date() if len(start_date) == 8 else date.fromisoformat(start_date)
    end = datetime.strptime(end_date, "%Y%m%d").date() if len(end_date) == 8 else date.fromisoformat(end_date)
    normalized_start = start.isoformat()
    normalized_end = end.isoformat()
    releases = _discover_releases(start, end)
    rows: list[dict[str, Any]] = []
    for available, url in sorted(releases.items()):
        response = requests.get(
            url,
            headers={"User-Agent": WASDE_USER_AGENT},
            timeout=60,
        )
        response.raise_for_status()
        row = _parse_workbook(response.content, available)
        if normalized_start <= row["available_date"] <= normalized_end:
            rows.append(row)
    unique = {row["available_date"]: row for row in rows}
    return [unique[key] for key in sorted(unique)]
