from __future__ import annotations

from datetime import date, datetime, timedelta
import math
import re
from typing import Any
from urllib.parse import urljoin

import requests
import xlrd


WASDE_BASE_URL = (
    "https://esmis.nal.usda.gov/publication/"
    "world-agricultural-supply-and-demand-estimates"
)
WASDE_USER_AGENT = "investment-portfolio-datahub/1.0"
PAGE_TIMEOUT_SECONDS = 15
WORKBOOK_TIMEOUT_SECONDS = 25
WORKBOOK_ATTEMPTS = 2
MAX_RELEASES_PER_CALL = 5
CSINDEX_INDICATOR_URL = (
    "https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/file/"
    "autofile/indicator/H30269indicator.xls"
)


class WasdeError(RuntimeError):
    pass


def _iso_date(value: Any, field_name: str) -> date:
    if not isinstance(value, str):
        raise WasdeError(f"{field_name} must use YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value.strip())
    except ValueError as exc:
        raise WasdeError(f"{field_name} must use YYYY-MM-DD") from exc
    return parsed


def _next_business_day(value: date) -> date:
    result = value + timedelta(days=1)
    while result.weekday() >= 5:
        result += timedelta(days=1)
    return result


def _release_date(value: str) -> date:
    normalized = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized).date()
    except ValueError as exc:
        raise WasdeError(f"WASDE release date is invalid: {value}") from exc


def _clean_label(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]+", " ", str(value or "").upper()).strip()


def _number_value(value: Any) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    match = re.search(r"[-+]?\d+(?:\.\d+)?", str(value or "").replace(",", ""))
    return float(match.group(0)) if match else math.nan


def _marketing_year(value: Any) -> str:
    match = re.search(r"(20\d{2})\D+(\d{2})", str(value or ""))
    if not match:
        raise WasdeError(f"WASDE marketing year is invalid: {value}")
    return f"{match.group(1)}/{match.group(2)}"


def _fetch_text(url: str) -> str:
    try:
        response = requests.get(
            url,
            headers={"Accept": "text/html", "User-Agent": WASDE_USER_AGENT},
            timeout=PAGE_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        return response.text
    except requests.RequestException as exc:
        raise WasdeError(f"USDA release page request failed: {type(exc).__name__}") from exc


def _fetch_workbook(url: str, source_label: str) -> bytes:
    last_error: requests.RequestException | None = None
    for attempt in range(WORKBOOK_ATTEMPTS):
        try:
            response = requests.get(
                url,
                headers={"Accept": "application/vnd.ms-excel", "User-Agent": WASDE_USER_AGENT},
                timeout=WORKBOOK_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            return response.content
        except requests.RequestException as exc:
            last_error = exc
            if attempt + 1 < WORKBOOK_ATTEMPTS:
                continue
    assert last_error is not None
    raise WasdeError(
        f"{source_label} request failed after {WORKBOOK_ATTEMPTS} attempts: "
        f"{type(last_error).__name__}"
    ) from last_error


def _discover_releases(start: date, end: date) -> dict[date, str]:
    found: dict[date, str] = {}
    minimum_needed = start - timedelta(days=10)
    pattern = re.compile(
        r"<a\s+href=[\"']([^\"']+\.xls)[\"'][^>]*>.*?"
        r"<time\s+datetime=[\"']([^\"']+)[\"']",
        flags=re.IGNORECASE | re.DOTALL,
    )
    for page in range(10):
        url = WASDE_BASE_URL if page == 0 else f"{WASDE_BASE_URL}?page={page}"
        html = _fetch_text(url)
        page_dates: list[date] = []
        for match in pattern.finditer(html):
            available = _next_business_day(_release_date(match.group(2)))
            page_dates.append(available)
            if start <= available <= end:
                found[available] = urljoin(f"{WASDE_BASE_URL}/", match.group(1))
        if page_dates and min(page_dates) < minimum_needed:
            break
    return found


def _remove_empty_columns(rows: list[list[Any]]) -> list[list[Any]]:
    width = max((len(row) for row in rows), default=0)
    used = [
        any(column < len(row) and row[column] not in (None, "") for row in rows)
        for column in range(width)
    ]
    return [
        [row[column] if column < len(row) else None for column, keep in enumerate(used) if keep]
        for row in rows
    ]


def _sheet_rows(book: xlrd.book.Book, name: str) -> list[list[Any]]:
    try:
        sheet = book.sheet_by_name(name)
    except xlrd.biffh.XLRDError as exc:
        raise WasdeError(f"USDA workbook has no {name}") from exc
    return [sheet.row_values(index) for index in range(sheet.nrows)]


def _parse_workbook(content: bytes, available: date) -> dict[str, Any]:
    try:
        book = xlrd.open_workbook(file_contents=content, on_demand=True)
    except (xlrd.biffh.XLRDError, ValueError) as exc:
        raise WasdeError("USDA workbook is not a valid .xls file") from exc

    domestic = _remove_empty_columns(_sheet_rows(book, "Page 15"))
    domestic_labels = [_clean_label(row[0] if row else "") for row in domestic]
    start = next((index for index, label in enumerate(domestic_labels) if label == "SOYBEANS"), -1)
    if start < 0:
        raise WasdeError("WASDE Page 15 has no SOYBEANS section")
    end = next(
        (index for index, label in enumerate(domestic_labels) if index > start and label.startswith("SOYBEAN OIL")),
        len(domestic),
    )
    block = domestic[start:end]
    width = max((len(row) for row in block), default=0)
    numeric_columns = [
        column
        for column in range(1, width)
        if sum(math.isfinite(_number_value(row[column] if column < len(row) else None)) for row in block) >= 3
    ]
    if not numeric_columns:
        raise WasdeError("WASDE Page 15 has no projection column")
    projection_column = numeric_columns[-1]
    block_labels = [_clean_label(row[0] if row else "") for row in block]

    def domestic_value(label: str) -> float:
        matches = [
            row
            for row, row_label in zip(block, block_labels)
            if row_label.startswith(label)
        ]
        if not matches:
            raise WasdeError(f"WASDE Page 15 has no {label} row")
        value = _number_value(matches[-1][projection_column])
        if not math.isfinite(value):
            raise WasdeError(f"WASDE {label} is invalid")
        return value

    world = _remove_empty_columns(_sheet_rows(book, "Page 28"))
    world_labels = [_clean_label(row[0] if row else "") for row in world]
    projected = [
        index for index, label in enumerate(world_labels)
        if re.match(r"^20\d\d \d\d PROJ", label)
    ]
    legacy = [
        index for index, label in enumerate(world_labels)
        if re.match(r"^20\d\d \d\d$", label)
    ]
    world_start = projected[-1] if projected else (legacy[-1] if legacy else -1)
    if world_start < 0:
        raise WasdeError("WASDE Page 28 has no projection section")
    world_block = world[world_start:]
    headers = [_clean_label(value) for value in world_block[0]]

    def column_index(label: str) -> int:
        try:
            return headers.index(label)
        except ValueError as exc:
            raise WasdeError(f"WASDE Page 28 has no {label} column") from exc

    imports_column = column_index("IMPORTS")
    crush_column = column_index("DOMESTIC CRUSH")
    entity = ""
    china: list[Any] | None = None
    for row in world_block[1:]:
        current_entity = _clean_label(row[0] if row else "")
        if current_entity and current_entity != "NAN":
            entity = current_entity
        if entity != "CHINA":
            continue
        imports = _number_value(row[imports_column] if imports_column < len(row) else None)
        crush = _number_value(row[crush_column] if crush_column < len(row) else None)
        if math.isfinite(imports) and math.isfinite(crush):
            china = row
    if china is None:
        raise WasdeError("WASDE Page 28 has no valid China row")

    return {
        "available_date": available.isoformat(),
        "projection_marketing_year": _marketing_year(block[0][projection_column]),
        "us_exports": domestic_value("EXPORTS"),
        "us_crush": domestic_value("CRUSHINGS"),
        "china_imports": _number_value(china[imports_column]),
        "china_crush": _number_value(china[crush_column]),
    }


def fetch_wasde_rows(start_date: str, end_date: str, max_rows: int = 5) -> tuple[list[dict[str, Any]], bool, int]:
    start = _iso_date(start_date, "start_date")
    end = _iso_date(end_date, "end_date")
    if start > end:
        raise WasdeError("start_date must be <= end_date")
    if max_rows < 1 or max_rows > MAX_RELEASES_PER_CALL:
        raise WasdeError(f"max_rows must be between 1 and {MAX_RELEASES_PER_CALL}")

    releases = _discover_releases(start, end)
    ordered = sorted(releases.items(), key=lambda item: item[0])
    selected = ordered[:max_rows]
    rows = [
        _parse_workbook(_fetch_workbook(url, "USDA workbook"), available)
        for available, url in selected
    ]
    return rows, len(ordered) > len(selected), len(ordered)


def fetch_dividend_yield(start_date: str, end_date: str) -> list[dict[str, Any]]:
    start = _iso_date(start_date, "start_date")
    end = _iso_date(end_date, "end_date")
    if start > end:
        raise WasdeError("start_date must be <= end_date")
    content = _fetch_workbook(CSINDEX_INDICATOR_URL, "CSIndex indicator workbook")
    try:
        book = xlrd.open_workbook(file_contents=content, on_demand=True)
        sheet = book.sheet_by_index(0)
    except (IndexError, xlrd.biffh.XLRDError, ValueError) as exc:
        raise WasdeError("CSIndex H30269 indicator workbook is invalid") from exc

    rows: dict[str, dict[str, Any]] = {}
    for index in range(1, sheet.nrows):
        values = sheet.row_values(index)
        if len(values) < 10 or str(values[1]).strip() != "H30269":
            continue
        raw_date = values[0]
        if isinstance(raw_date, (int, float)) and not isinstance(raw_date, bool):
            date_text = str(int(raw_date))
        else:
            date_text = str(raw_date).strip()
        if not re.fullmatch(r"\d{8}", date_text):
            raise WasdeError("CSIndex H30269 indicator date is invalid")
        current = _iso_date(
            f"{date_text[:4]}-{date_text[4:6]}-{date_text[6:8]}",
            "CSIndex H30269 indicator date",
        )
        value = _number_value(values[9]) / 100
        if not math.isfinite(value) or value <= 0:
            raise WasdeError(f"CSIndex H30269 dividend yield is invalid for {current.isoformat()}")
        if start <= current <= end:
            rows[current.isoformat()] = {"date": current.isoformat(), "value": value}
    return [rows[key] for key in sorted(rows)]
