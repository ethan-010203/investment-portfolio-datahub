# Investment Portfolio DataHub

Vercel + FastAPI relay service for upstream sources that are better handled in
Python. It provides a concrete-contract TQSDK futures endpoint and parses USDA
WASDE workbooks. The futures endpoint returns both the minimal contract source
rows and the unified formal soybean inputs; it does not write Turso or produce
portfolio weights.

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

### TQSDK futures source and derived rows

```text
POST /api/tq/soymeal/derived
```

This route uses the temporary TQSDK test account in `api/tq_derived.py`. It
requests only concrete M/RM/Y/C/JD contracts; TQ automatic-main labels are not
used as formal inputs. All five mappings use RiceQuant-compatible `rule=0`:
another contract must close above 1.1 times the current open interest, the
switch takes effect on the next trading day, and a previous main cannot return.
The 888 prices use `pre`/`prev_close_ratio` adjustment. Before 2020, volume and
open interest are normalized by the frozen two-times convention.

Each request contains exactly one of `M`, `RM`, `Y`, `C`, or `JD`, together
with that product's stored main contract and previously used contracts. The
response contains only that product's concrete rows, derived series, and roll
events; `M` additionally returns the two term-structure values. Supabase calls
the products serially and commits each successful product independently. The
route itself does not access Turso and uses the same outer token check as every
protected route.

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
