# Investment Portfolio DataHub

Vercel + FastAPI proxy for the investment portfolio project. It exposes the
existing AKShare routes and a raw TQSDK soybean-meal relay route. Vercel does
not install or run TQSDK, select contracts, calculate M88/M888, splice prices,
calculate roll yield, or run portfolio factors.

## Raw TQSDK soybean-meal data

The single TQ route receives the query from Supabase, forwards it to a
separate Python service that runs TQSDK, and returns the upstream JSON without
strategy calculations:

```text
POST /api/tq/soymeal/raw
```

Request example:

```json
{
  "start_date": "2026-08-01",
  "end_date": "2026-08-21",
  "data_length": 300,
  "settlement_days": 300,
  "datasets": [
    "main_continuous",
    "main_mapping",
    "contract_info",
    "contract_bars",
    "settlements"
  ]
}
```

Supported datasets are `main_continuous`, `main_mapping`, `contract_info`,
`contract_bars`, and `settlements`. The default is all five. `contract_bars`
and `settlements` are returned grouped by concrete DCE soybean-meal symbol.
Each returned row includes a normalized `trade_date` for Supabase alignment;
raw TQ fields are otherwise preserved. The endpoint does not choose the
main/sub-main contract or calculate any adjusted series.

The separate TQSDK service owns the TQ login and uses the same request body and
response contract. Configure the TQSDK service URL in Vercel:

```text
TQSDK_UPSTREAM_URL
```

The shared 32-character token is defined once in `api/security.py`. Supabase
must send it as either `Authorization: Bearer <token>` or
`x-datahub-token: <token>` for both the AKShare and TQ routes. Vercel sends the
same value to the TQSDK service as `x-tq-service-token`; the TQSDK service must
validate that header with the same token.

`TQ_MAX_DATA_LENGTH`, `TQ_MAX_SETTLEMENT_DAYS`, `TQ_RELAY_TIMEOUT_SECONDS`, and
`TQ_RELAY_RETRY_ATTEMPTS` are optional safeguards. They default to 10,000
K-line rows, 2,000 settlement days, a 55-second upstream timeout, and 2 relay
attempts. Supabase remains responsible for contract selection, M88/M888,
roll-yield, and all strategy calculations.

## Generic AKShare proxy

The proxy supports AKShare data functions without adding one Vercel route per
data source:

```text
GET  /api/akshare/{function_name}?param=value
POST /api/akshare/{function_name}
```

Example:

```http
POST /api/akshare/fund_etf_hist_em
Content-Type: application/json

{
  "symbol": "512890",
  "period": "daily",
  "start_date": "20260801",
  "end_date": "20260822",
  "adjust": ""
}
```

The generic route resolves a callable from the `akshare` module, validates its
Python signature, invokes it in a worker thread, and converts DataFrame,
Series, date, NumPy, and nested values into JSON. It does not use `eval` and
does not write to Turso.

Synchronous AKShare calls are isolated from the FastAPI event loop. The proxy
allows two upstream calls by default and queues additional requests without
blocking the event loop. Connection errors and timeouts are retried with
backoff. Tune `AKSHARE_CONCURRENCY` and `AKSHARE_RETRY_ATTEMPTS` in Vercel if
the upstream provider changes its limits.

The original normalized ETF route remains available for compatibility:

```text
GET /api/health
GET /api/etf/512890?start_date=20260801&end_date=20260822
```

The ETF route returns the fields required by the current Turso daily-K table:

```json
{
  "date": "2026-08-21",
  "open": 1.182,
  "high": 1.184,
  "low": 1.174,
  "close": 1.176,
  "volume": 5137059,
  "total_turnover": 604722662
}
```

For the normalized ETF route, AKShare Eastmoney volume is converted from lots
to shares to match the existing Turso table. If Eastmoney is unavailable, the
route falls back to AKShare `fund_etf_hist_sina`; Sina volume is already in
shares, is rounded to the same 100-share precision, and its historical
`amount` is used as `total_turnover`.

## Local test

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn api.index:app --reload --port 8000
```

Then open:

```text
http://127.0.0.1:8000/health
http://127.0.0.1:8000/etf/512890?start_date=20260801&end_date=20260822
```

## Authentication

The token is intentionally hardcoded in `api/security.py` for the current MVP.
Supabase should store the same value as a secret and forward it on every
request. Rotate the token by changing that one constant and redeploying Vercel.
