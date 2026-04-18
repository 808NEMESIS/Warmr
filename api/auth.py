"""
api/auth.py

JWT authentication dependency.
Validates Supabase-issued Bearer tokens on every protected endpoint.

Supabase tokens may use HS256 (older projects) or ES256 (newer projects).
For ES256 we fetch the public key from the Supabase JWKS endpoint and cache it.
The `sub` claim contains the user's UUID, used as `client_id` throughout the app.
"""

import os
from typing import Annotated

import httpx
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import ExpiredSignatureError, JWTError, jwk, jwt
from supabase import create_client

SUPABASE_JWT_SECRET: str = os.getenv("SUPABASE_JWT_SECRET", "")
_SUPABASE_URL: str = os.getenv("SUPABASE_URL", "")
_SUPABASE_KEY: str = os.getenv("SUPABASE_KEY", "")

_bearer = HTTPBearer(auto_error=True)

# Simple in-process JWKS cache: kid -> public key dict
_jwks_cache: dict[str, dict] = {}


def _get_jwks_key(kid: str) -> dict | None:
    """Fetch and cache a JWK by key id from Supabase JWKS endpoint."""
    if kid in _jwks_cache:
        return _jwks_cache[kid]
    if not _SUPABASE_URL:
        return None
    try:
        resp = httpx.get(f"{_SUPABASE_URL}/auth/v1/.well-known/jwks.json", timeout=5)
        resp.raise_for_status()
        for key in resp.json().get("keys", []):
            _jwks_cache[key["kid"]] = key
        return _jwks_cache.get(kid)
    except Exception:
        return None


def _decode_token(token: str) -> dict:
    """Decode and validate a Supabase JWT. Raises HTTP 401 on failure."""
    # Peek at the header to determine algorithm
    try:
        header = jwt.get_unverified_header(token)
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token: {exc}",
        )

    alg = header.get("alg", "HS256")

    try:
        if alg == "HS256":
            if not SUPABASE_JWT_SECRET:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="SUPABASE_JWT_SECRET is not configured on the server.",
                )
            return jwt.decode(
                token,
                SUPABASE_JWT_SECRET,
                algorithms=["HS256"],
                options={"verify_aud": False},
            )
        elif alg in ("ES256", "RS256"):
            kid = header.get("kid", "")
            jwk_key = _get_jwks_key(kid)
            if not jwk_key:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Unable to fetch JWKS public key for token verification.",
                )
            public_key = jwk.construct(jwk_key)
            return jwt.decode(
                token,
                public_key,
                algorithms=[alg],
                options={"verify_aud": False},
            )
        else:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"Unsupported JWT algorithm: {alg}",
            )
    except ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired. Please log in again.",
        )
    except HTTPException:
        raise
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token: {exc}",
        )


async def get_current_client(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(_bearer)],
) -> str:
    """
    FastAPI dependency: validate a Supabase JWT and return the client_id (user UUID).

    Inject with `client_id: str = Depends(get_current_client)` on any protected route.
    Raises HTTP 401 for missing, malformed, or expired tokens.
    """
    payload = _decode_token(credentials.credentials)
    client_id: str | None = payload.get("sub")
    if not client_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token is missing the required 'sub' claim.",
        )
    return client_id


async def require_admin(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(_bearer)],
) -> str:
    """
    FastAPI dependency: validate the JWT and assert the user is an admin.

    Checks the `clients` table for `is_admin = true`.
    Returns the client_id on success.
    Raises HTTP 403 if the user is not an admin.
    """
    payload = _decode_token(credentials.credentials)
    client_id: str | None = payload.get("sub")
    if not client_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token.")

    if not _SUPABASE_URL or not _SUPABASE_KEY:
        raise HTTPException(status_code=500, detail="Supabase not configured.")

    sb = create_client(_SUPABASE_URL, _SUPABASE_KEY)
    resp = sb.table("clients").select("is_admin").eq("id", client_id).limit(1).execute()
    rows = resp.data or []
    if not rows or not rows[0].get("is_admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required.",
        )
    return client_id
