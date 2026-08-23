# Investment Portfolio DataHub

Vercel + FastAPI relay service for upstream sources that are better handled in
Python. It provides a derived TQSDK soybean-meal endpoint and parses USDA WASDE
workbooks. The derived soybean endpoint returns the unified formal soybean
futures input values; it
does not write Turso or produce portfolio weights.

## Production routes

Protected routes require the shared token from `api/security.py` in the
`x-datahub-token` header. Authentication runs once in the outer FastAPI
middleware before the request reaches a route handler.

### Health

```text
GET /api/health
```

The health response lists the active relay sources: `tqsdk-relay`,
`usda-excel-relay`, and `csindex-indicator-relay`.

### Derived TQSDK soybean-meal rows

```text
POST /api/tq/soymeal/derived
```

This route uses the temporary TQSDK test account in `api/tq_derived.py`. It
requests the soybean meal, rapeseed meal, soybean oil, corn, and egg main
continuous series plus their concrete contract legs. It returns one row for
the unified Turso table: M88/M888 prices, M888 volume and open interest, the
RM888/Y888/C888/JD888 adjusted closes, and the two soybean roll yields. M888
and the four auxiliary 888 series are calculated locally from the previous
stored row and the current contract mappings. The caller provides those
previous values as `seed`; the route does not write Turso. It uses the same
outer DataHub token check as the other protected routes.

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

### CSIndex daily history relay

```text
POST /api/csindex/history
```

This endpoint requests the H30269 or H20269 daily close history from the
official CSIndex JSON endpoint and returns only the date and close fields.
Supabase calls this relay instead of accessing CSIndex directly.

## Data fetched directly by Supabase

ETF daily bars use Tencent Finance directly from Supabase. Supabase also calls
FRED, Tencent Finance, and ChinaBond directly. USDA and all CSIndex requests,
including daily closes and indicator workbook parsing, pass through the Python
relay.

## Local test

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn api.index:app --reload --port 8000
```

Then request `/api/health` or an authenticated relay route such as
`/api/tq/soymeal/derived`.
