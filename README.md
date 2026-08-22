# Investment Portfolio DataHub

Vercel + FastAPI transport service for data sources that Supabase Edge Functions
cannot call directly. It relays source data only and does not calculate factors,
select soybean-meal contracts, or produce portfolio weights.

## Production routes

All data routes require the shared token from `api/security.py` in either
`x-datahub-token` or `Authorization: Bearer`.

### ETF daily bars

```text
GET /api/etf/{symbol}?start_date=20260801&end_date=20260821&adjust=
```

The route supports `512890`, `513100`, `513500`, `518880`, and `159985`. It
reads unadjusted daily OHLCV from TongdaXin through `pytdx`.
Multiple TongdaXin servers provide transport redundancy; there is no fallback
to another market-data source.

The response fields match the Turso daily-K tables:

```json
{
  "date": "2026-08-21",
  "open": 1.182,
  "high": 1.184,
  "low": 1.174,
  "close": 1.176,
  "volume": 513705800,
  "total_turnover": 604722688
}
```

TongdaXin volume is reported in lots and converted to shares before returning.
History is paged backward in blocks of 800 rows and filtered to the requested
date range. Supabase requests all five ETFs as one batch so one TCP connection
is reused for the complete update.

### H30269 dividend yield

```text
GET /api/index/H30269/dividend-yield?start_date=20260801&end_date=20260821
```

This route downloads the official CSI Index indicator workbook directly. It
returns only `date` and the free-float market-cap weighted dividend yield
(`D/P2`) as a decimal.

### USDA WASDE

```text
GET /api/usda/wasde?start_date=20260801&end_date=20260821
```

The route downloads official USDA ESMIS workbooks and normalizes only the
soybean demand fields required by the strategy.

### Raw TQSDK soybean-meal relay

```text
POST /api/tq/soymeal/raw
```

The request is forwarded to the Python TQSDK service configured by
`TQSDK_UPSTREAM_URL`. The route does not choose main/sub-main contracts,
calculate M88/M888, splice prices, or calculate roll yield.

## Data fetched directly by Supabase

`003376` accumulated NAV is fetched directly from Tencent Finance. The China
10-year government-bond yield and credit curves are fetched directly from
ChinaBond. They do not pass through this Vercel service.

## Local test

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn api.index:app --reload --port 8000
```

Then request `/api/health` or one of the authenticated production routes.
