"""
Supabase authentication + usage tracking for PackGen.
Each user gets an API key stored in their profile.
Usage is tracked per-job (questions classified).
"""
import os, functools, logging
from flask import request, jsonify, g
from supabase import create_client, Client

SUPABASE_URL     = os.environ.get("SUPABASE_URL", "")
SUPABASE_ANON    = os.environ.get("SUPABASE_ANON_KEY", "")
SUPABASE_SERVICE = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")

FREE_QUOTA  = int(os.environ.get("FREE_QUOTA_PER_MONTH",  "500"))   # questions
PRO_QUOTA   = int(os.environ.get("PRO_QUOTA_PER_MONTH",  "5000"))
log = logging.getLogger(__name__)
_sb_client: Client | None = None


def _get_supabase_client() -> Client | None:
    """
    Return a cached Supabase client.
    Prefer service role for server-side profile reads/writes.
    """
    global _sb_client
    if _sb_client is not None:
        return _sb_client
    if not SUPABASE_URL:
        return None
    api_key = (SUPABASE_SERVICE or SUPABASE_ANON or "").strip()
    if not api_key:
        return None
    try:
        _sb_client = create_client(SUPABASE_URL, api_key)
        return _sb_client
    except Exception:
        log.exception("Failed to initialize Supabase client")
        return None


# ── Token verification ────────────────────────────────────────────────────────
def verify_token(token: str) -> dict | None:
    """Verify a Supabase JWT and return the user dict, or None."""
    if not SUPABASE_URL:
        # Dev mode — no auth
        return {"id": "dev", "email": "dev@local", "role": "free"}
    sb = _get_supabase_client()
    if not sb:
        return None
    try:
        res = sb.auth.get_user(token)
        user_obj = getattr(res, "user", None)
        if user_obj is None and isinstance(res, dict):
            user_obj = res.get("user")
        if user_obj is None:
            return None
        if hasattr(user_obj, "model_dump"):
            return user_obj.model_dump()
        if isinstance(user_obj, dict):
            return user_obj
        return {
            "id": getattr(user_obj, "id", None),
            "email": getattr(user_obj, "email", None),
        }
    except Exception:
        log.exception("Supabase token verification failed")
    return None


def get_user_profile(user_id: str) -> dict:
    """Fetch user profile from packgen_profiles table."""
    if not SUPABASE_URL:
        return {"user_id": "dev", "plan": "free", "usage_this_month": 0, "anthropic_api_key": ""}
    sb = _get_supabase_client()
    if not sb:
        return {"user_id": user_id, "plan": "free", "usage_this_month": 0, "anthropic_api_key": ""}
    rows = []
    try:
        res = sb.table("packgen_profiles").select("*").eq("user_id", user_id).limit(1).execute()
        rows = (res.data or []) if hasattr(res, "data") else []
    except Exception:
        log.exception("Failed to fetch profile for %s", user_id)
    if rows:
        return rows[0]
    # Auto-create profile
    profile = {"user_id": user_id, "plan": "free", "usage_this_month": 0, "anthropic_api_key": ""}
    try:
        sb.table("packgen_profiles").upsert(profile, on_conflict="user_id").execute()
    except Exception:
        log.exception("Failed to auto-create profile for %s", user_id)
    return profile


def increment_usage(user_id: str, count: int):
    """Add `count` to the user's monthly usage counter."""
    if not SUPABASE_URL or user_id == "dev":
        return
    sb = _get_supabase_client()
    if not sb:
        return
    profile = get_user_profile(user_id)
    new_usage = profile.get("usage_this_month", 0) + count
    try:
        sb.table("packgen_profiles").update({"usage_this_month": new_usage}).eq("user_id", user_id).execute()
    except Exception:
        log.exception("Failed to increment usage for %s", user_id)


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
