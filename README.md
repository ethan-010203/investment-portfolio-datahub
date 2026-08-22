# Investment Portfolio DataHub

Vercel + FastAPI + AKShare proxy for the investment portfolio project.

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

Set the Vercel environment variable `DATAHUB_API_TOKEN` before production
use. When it is present, the proxy accepts either
`Authorization: Bearer <token>` or `x-datahub-token: <token>`. Supabase should
store the same token as a secret and forward it on every request. Without the
variable the MVP remains public for smoke testing.
