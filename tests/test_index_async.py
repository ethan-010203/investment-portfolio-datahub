import asyncio
import json
import time

from api import index
from starlette.requests import Request


def request_for(payload: dict[str, object], path: str) -> Request:
    body = json.dumps(payload).encode()
    sent = False

    async def receive() -> dict[str, object]:
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [(b"content-type", b"application/json")],
            "client": ("test", 123),
            "server": ("test", 80),
        },
        receive,
    )


def test_usda_route_offloads_blocking_parser(monkeypatch):
    active = 0
    maximum_active = 0
    ticks = 0

    def blocking_fetch(*args):
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        time.sleep(0.1)
        active -= 1
        return ([{"available_date": "2026-08-21"}], False, 1)

    async def ticker():
        nonlocal ticks
        for _ in range(3):
            await asyncio.sleep(0.03)
            ticks += 1

    async def run():
        result, _ = await asyncio.gather(
            index.usda_wasde_rows(
                request_for(
                    {"start_date": "2026-08-01", "end_date": "2026-08-23"},
                    "/api/usda",
                )
            ),
            ticker(),
        )
        return result

    monkeypatch.setattr(index, "fetch_wasde_rows", blocking_fetch)
    result = asyncio.run(run())

    assert result["ok"] is True
    assert result["returned_rows"] == 1
    assert ticks == 3
    assert maximum_active == 1


def test_dividend_route_offloads_blocking_parser(monkeypatch):
    ticks = 0

    def blocking_fetch(*args):
        time.sleep(0.1)
        return [{"date": "2026-08-21", "value": 0.05}]

    async def ticker():
        nonlocal ticks
        for _ in range(3):
            await asyncio.sleep(0.03)
            ticks += 1

    async def run():
        result, _ = await asyncio.gather(
            index.csindex_dividend_yield(
                request_for(
                    {"start_date": "2026-08-01", "end_date": "2026-08-23"},
                    "/api/csindex",
                )
            ),
            ticker(),
        )
        return result

    monkeypatch.setattr(index, "fetch_dividend_yield", blocking_fetch)
    result = asyncio.run(run())

    assert result["ok"] is True
    assert result["returned_rows"] == 1
    assert ticks == 3
