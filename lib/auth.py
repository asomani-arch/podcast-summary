"""Supabase authentication for the FastAPI backend.

Each request carries the user's Supabase access token as a Bearer header. We
verify it by calling Supabase's `GET /auth/v1/user` with the token + the anon
`apikey`. That works for any project signing configuration and needs no JWT
secret (docs/PRD.md §F1). A short in-process TTL cache avoids a round-trip to
Supabase on every request for the same (short-lived) token.

Usage in a route:
    from lib.auth import current_user, User
    @app.get("/api/me")
    def me(user: User = Depends(current_user)):
        return {"id": user.id, "email": user.email}
"""
import os
import time

import requests
from fastapi import Header, HTTPException

_CACHE: dict[str, tuple[float, dict]] = {}
_CACHE_TTL = 60          # seconds a verified user is trusted without re-checking
_CACHE_MAX = 500         # prune threshold
_TIMEOUT = 8


def _supabase_url() -> str:
    url = os.getenv("SUPABASE_URL")
    if not url:
        raise RuntimeError("SUPABASE_URL not set")
    return url.rstrip("/")


def _anon_key() -> str:
    key = os.getenv("SUPABASE_ANON_KEY")
    if not key:
        raise RuntimeError("SUPABASE_ANON_KEY not set")
    return key


def _prune(now: float) -> None:
    if len(_CACHE) <= _CACHE_MAX:
        return
    for k in [k for k, (exp, _) in _CACHE.items() if exp <= now]:
        _CACHE.pop(k, None)


def _verify_token(token: str) -> dict:
    now = time.time()
    cached = _CACHE.get(token)
    if cached and cached[0] > now:
        return cached[1]

    try:
        resp = requests.get(
            f"{_supabase_url()}/auth/v1/user",
            headers={"Authorization": f"Bearer {token}", "apikey": _anon_key()},
            timeout=_TIMEOUT,
        )
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Auth service unreachable: {e}")

    if resp.status_code != 200:
        raise HTTPException(status_code=401, detail="Invalid or expired session.")

    user = resp.json()
    if not user.get("id"):
        raise HTTPException(status_code=401, detail="Invalid session.")

    _prune(now)
    _CACHE[token] = (now + _CACHE_TTL, user)
    return user


class User:
    def __init__(self, raw: dict):
        self.id: str = raw.get("id")
        self.email: str | None = raw.get("email")
        self.raw: dict = raw


def _extract_bearer(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token.")
    token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Missing bearer token.")
    return token


def current_user(authorization: str | None = Header(default=None)) -> User:
    """FastAPI dependency: require a valid Supabase session; returns the User
    or raises 401."""
    token = _extract_bearer(authorization)
    return User(_verify_token(token))


def optional_user(authorization: str | None = Header(default=None)) -> User | None:
    """Like current_user, but returns None when no/invalid auth is present
    (for routes that work signed-out, e.g. public podcast search)."""
    if not authorization:
        return None
    try:
        return current_user(authorization)
    except HTTPException:
        return None
