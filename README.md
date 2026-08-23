# Investment Portfolio DataHub

Vercel + FastAPI relay service for upstream sources that are better handled in
Python. It provides a direct TQSDK soybean-meal endpoint and parses USDA WASDE
workbooks. The soybean production endpoint returns final M88/M888 rows; it does
not write Turso or produce portfolio weights.

## Production routes

All routes require the shared token from `api/security.py` in either
`x-datahub-token` or `Authorization: Bearer`.

### Health

```text
GET /api/health
```

The health response lists the active relay sources: `tqsdk-relay`,
`usda-excel-relay`, and `csindex-indicator-relay`.

### Raw TQSDK soybean-meal relay

```text
POST /api/tq/soymeal/raw
```

The request is forwarded to the Python TQSDK service configured by
`TQSDK_UPSTREAM_URL`. The relay validates dates, requested datasets, and size
limits, then returns the upstream response without calculating factors. Each
request has a 55-second per-attempt timeout and a 90-second total timeout.

### Derived TQSDK soybean-meal rows

```text
POST /api/tq/soymeal/derived
```

This route uses the temporary TQSDK test account in `api/tq_derived.py`. It
requests only the main continuous series and concrete main contracts needed for
the requested range, then returns M88/M888, roll, and adjustment fields. The
caller provides the previous stored row as `seed`; the route does not write
Turso. It uses the same single DataHub token check as the other protected routes.

### USDA WASDE parser relay

```text
POST /api/usda
```

The request accepts `start_date`, `end_date`, and optional `max_rows` (1-5).
It downloads only the oldest missing USDA releases in the requested range and
returns the six fields required by the Turso `L3_USDA WASDE` table. The relay
does not write Turso and does not return raw workbooks.

### CSIndex dividend indicator relay

```text
POST /api/csindex
```

This endpoint parses the official H30269 indicator workbook and returns only
the date and real dividend-yield value required by Turso.
The relay retries each workbook download once before returning an upstream
error.

## Data fetched directly by Supabase

ETF daily bars use Tencent Finance directly from Supabase. Supabase also calls
FRED, CSI Index daily closes, Tencent Finance, and ChinaBond directly. USDA and
CSIndex indicator workbook download and parsing pass through the Python relay.

## Local test

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn api.index:app --reload --port 8000
```

Then request `/api/health` or the authenticated TQ relay route.
