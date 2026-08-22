# Investment Portfolio DataHub

Minimal Vercel + FastAPI + AKShare proxy for the investment portfolio project.

## MVP endpoint

The first test only supports ETF `512890` and does not write to Turso:

```text
GET /api/health
GET /api/etf/512890?start_date=20260801&end_date=20260822
```

The ETF endpoint calls `ak.fund_etf_hist_em` and returns the normalized fields
required by the current Turso daily-K table:

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

Vercel detects `api/index.py` as a Python function. This MVP intentionally has
no authentication and no database write; those are added only after the proxy
request path is confirmed.
