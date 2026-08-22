from __future__ import annotations

import secrets

from fastapi import HTTPException
from starlette.requests import Request


# Shared by Supabase -> Vercel authentication and Vercel -> TQSDK authentication.
SHARED_DATAHUB_TOKEN = "4nn17DhFyICn7ra4dyRvT7T1BtGHBgdQ"


def require_request_auth(request: Request) -> None:
    supplied = request.headers.get("x-datahub-token", "").strip()
    authorization = request.headers.get("authorization", "")
    if not supplied and authorization.lower().startswith("bearer "):
        supplied = authorization[7:].strip()
    if not supplied or not secrets.compare_digest(supplied, SHARED_DATAHUB_TOKEN):
        raise HTTPException(status_code=401, detail="Missing or invalid datahub token")
