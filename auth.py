"""
Supabase authentication + usage tracking for PackGen.
Each user gets an API key stored in their profile.
Usage is tracked per-job (questions classified).
"""
import os, json, functools
from flask import request, jsonify, g
import requests as req

SUPABASE_URL     = os.environ.get("SUPABASE_URL", "")
SUPABASE_ANON    = os.environ.get("SUPABASE_ANON_KEY", "")
SUPABASE_SERVICE = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")

FREE_QUOTA  = int(os.environ.get("FREE_QUOTA_PER_MONTH",  "500"))   # questions
PRO_QUOTA   = int(os.environ.get("PRO_QUOTA_PER_MONTH",  "5000"))


def _sb_headers(use_service=False):
    key = SUPABASE_SERVICE if use_service else SUPABASE_ANON
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }


# ── Token verification ────────────────────────────────────────────────────────
def verify_token(token: str) -> dict | None:
    """Verify a Supabase JWT and return the user dict, or None."""
    if not SUPABASE_URL:
        # Dev mode — no auth
        return {"id": "dev", "email": "dev@local", "role": "free"}
    try:
        r = req.get(
            f"{SUPABASE_URL}/auth/v1/user",
            headers={**_sb_headers(), "Authorization": f"Bearer {token}"},
            timeout=10,
        )
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


def get_user_profile(user_id: str) -> dict:
    """Fetch user profile from packgen_profiles table."""
    if not SUPABASE_URL:
        return {"user_id": "dev", "plan": "free", "usage_this_month": 0, "anthropic_api_key": ""}
    r = req.get(
        f"{SUPABASE_URL}/rest/v1/packgen_profiles",
        headers=_sb_headers(use_service=True),
        params={"user_id": f"eq.{user_id}", "select": "*"},
        timeout=10,
    )
    rows = r.json() if r.status_code == 200 else []
    if rows:
        return rows[0]
    # Auto-create profile
    profile = {"user_id": user_id, "plan": "free", "usage_this_month": 0, "anthropic_api_key": ""}
    req.post(
        f"{SUPABASE_URL}/rest/v1/packgen_profiles",
        headers=_sb_headers(use_service=True),
        json=profile,
        timeout=10,
    )
    return profile


def increment_usage(user_id: str, count: int):
    """Add `count` to the user's monthly usage counter."""
    if not SUPABASE_URL or user_id == "dev":
        return
    profile = get_user_profile(user_id)
    new_usage = profile.get("usage_this_month", 0) + count
    req.patch(
        f"{SUPABASE_URL}/rest/v1/packgen_profiles",
        headers=_sb_headers(use_service=True),
        params={"user_id": f"eq.{user_id}"},
        json={"usage_this_month": new_usage},
        timeout=10,
    )


def check_quota(profile: dict) -> tuple[bool, int, int]:
    """
    Returns (allowed, used, quota).
    allowed=True if user has remaining quota.
    """
    plan  = profile.get("plan", "free")
    quota = PRO_QUOTA if plan == "pro" else FREE_QUOTA
    used  = profile.get("usage_this_month", 0)
    return used < quota, used, quota


# ── Flask decorator ───────────────────────────────────────────────────────────
def require_auth(f):
    """
    Decorator that verifies the Authorization header and sets g.user / g.profile.
    If SUPABASE_URL is not configured, passes through in dev mode.
    """
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        if not SUPABASE_URL:
            # Dev mode — skip auth
            g.user    = {"id": "dev", "email": "dev@local"}
            g.profile = {"user_id": "dev", "plan": "free", "usage_this_month": 0, "anthropic_api_key": ""}
            return f(*args, **kwargs)

        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return jsonify({"error": "Unauthorized"}), 401
        token = auth[7:]
        user  = verify_token(token)
        if not user:
            return jsonify({"error": "Invalid or expired token"}), 401

        g.user    = user
        g.profile = get_user_profile(user["id"])
        return f(*args, **kwargs)
    return wrapper


def get_effective_api_key(profile: dict, request_key: str = "") -> str:
    """
    Return the API key to use for classification.
    Priority: user-supplied in request → user's stored key → env key.
    """
    if request_key.strip():
        return request_key.strip()
    stored = profile.get("anthropic_api_key", "").strip()
    if stored:
        return stored
    return os.environ.get("ANTHROPIC_API_KEY", "")
