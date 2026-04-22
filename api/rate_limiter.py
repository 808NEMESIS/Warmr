"""
api/rate_limiter.py — shared slowapi Limiter instance.

Lives in its own module so both `api/main.py` (dashboard + admin routes) and
`api/public_api.py` (machine-to-machine routes) can apply `@limiter.limit(...)`
decorators without importing each other — which would be a circular import.

Key selection is in `_rate_limit_key`:
  1. Public API keys (Bearer wrmr_...) → hashed key → per-key quota
  2. Dashboard JWTs              → sub claim → per-client quota
  3. Unauthenticated calls       → remote IP
"""

from __future__ import annotations

import hashlib

from fastapi import Request
from jose import jwt as jose_jwt
from slowapi import Limiter
from slowapi.util import get_remote_address


def _rate_limit_key(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        token = auth[7:]
        if token.startswith("wrmr_"):
            return f"apikey:{hashlib.sha256(token.encode()).hexdigest()[:16]}"
        try:
            payload = jose_jwt.get_unverified_claims(token)
            sub = payload.get("sub")
            if sub:
                return f"client:{sub}"
        except Exception:
            pass
    return f"ip:{get_remote_address(request)}"


limiter = Limiter(key_func=_rate_limit_key, default_limits=["120/minute"])
