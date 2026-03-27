"""
api_keys.py — Per-user PackGen API key management.

Each user gets a unique PackGen key (e.g. "pg-live-abc123xyz...")
generated on account creation.  This key is used to authenticate
API calls (instead of or alongside the Supabase JWT) and is the
unit of rate limiting.

Key format:   pg-live-<32 hex chars>    (production)
              pg-test-<32 hex chars>    (test/dev)

Keys are stored in the packgen_profiles table in Supabase.
A Redis cache is used for fast validation (~1ms vs ~50ms DB round-trip).

Rate limit tiers (per hour, checked in store.check_rate_limit):
  free   → 200 classification requests / hour
  pro    → 2 000 classification requests / hour
  admin  → unlimited
"""

import os, secrets, hashlib, time, logging
from supabase import create_client, Client
from store import check_rate_limit, REDIS_AVAILABLE

log = logging.getLogger(__name__)

SUPABASE_URL     = os.environ.get("SUPABASE_URL", "")
SUPABASE_SERVICE = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
SUPABASE_ANON    = os.environ.get("SUPABASE_ANON_KEY", "")

# Rate limits: (requests, window_seconds)
RATE_LIMITS = {
    "free":  (200,  3600),
    "pro":   (2000, 3600),
    "admin": (999999, 3600),
}

# In-process cache: {hashed_key: profile_dict}  TTL 5 minutes
_key_cache:  dict[str, dict] = {}
_key_cache_ts: dict[str, float] = {}
KEY_CACHE_TTL = 300
_sb_client: Client | None = None


def _get_supabase_client() -> Client | None:
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
        log.exception("Failed to initialize Supabase client in api_keys")
        return None


# ── Key generation ────────────────────────────────────────────────────────────
def generate_packgen_key(env: str = "live") -> str:
    """Generate a new unique PackGen API key."""
    token = secrets.token_hex(24)          # 48 chars of hex = 192 bits
    return f"pg-{env}-{token}"


def hash_key(raw_key: str) -> str:
    """SHA-256 hash of a key — stored in DB, never the plaintext."""
    return hashlib.sha256(raw_key.encode()).hexdigest()


# ── Key creation (called on user signup) ─────────────────────────────────────
def provision_user_key(user_id: str, plan: str = "free") -> str:
    """
    Generate and store a PackGen key for a new user.
    Returns the plaintext key (only time it's returned).
    """
    raw_key    = generate_packgen_key()
    key_hash   = hash_key(raw_key)
    key_prefix = raw_key[:12]   # "pg-live-abcd" — for display

    sb = _get_supabase_client()
    if sb:
        try:
            # Upsert ensures the profile row exists for brand new OAuth users.
            sb.table("packgen_profiles").upsert(
                {
                    "user_id":            user_id,
                    "plan":               plan,
                    "usage_this_month":   0,
                    "packgen_key_hash":   key_hash,
                    "packgen_key_prefix": key_prefix,
                    # Let DB default trigger/time set created_at if present.
                },
                on_conflict="user_id",
            ).execute()
        except Exception as e:
            log.error("Failed to store key for user %s: %s", user_id, e)

    return raw_key


def rotate_user_key(user_id: str) -> str:
    """Rotate a user's PackGen key. Returns the new plaintext key."""
    # Invalidate cache for old key
    _invalidate_user_cache(user_id)
    return provision_user_key(user_id)


# ── Key validation ────────────────────────────────────────────────────────────
def validate_packgen_key(raw_key: str) -> dict | None:
    """
    Validate a PackGen key. Returns the user profile dict or None.
    Uses in-process cache to avoid hitting the DB on every request.
    """
    if not raw_key or not raw_key.startswith("pg-"):
        return None

    key_hash = hash_key(raw_key)

    # Check in-process cache
    cached = _key_cache.get(key_hash)
    if cached is not None:
        age = time.time() - _key_cache_ts.get(key_hash, 0)
        if age < KEY_CACHE_TTL:
            return cached
        # Expired — fall through to DB

    sb = _get_supabase_client()
    if not sb:
        # Dev mode — accept any pg- key
        profile = {
            "user_id":          "dev",
            "plan":             "free",
            "usage_this_month": 0,
            "packgen_key_hash": key_hash,
        }
        _key_cache[key_hash]    = profile
        _key_cache_ts[key_hash] = time.time()
        return profile

    try:
        res = sb.table("packgen_profiles").select("*").eq("packgen_key_hash", key_hash).limit(1).execute()
        rows = (res.data or []) if hasattr(res, "data") else []
        if rows:
            profile = rows[0]
            _key_cache[key_hash]    = profile
            _key_cache_ts[key_hash] = time.time()
            return profile
    except Exception as e:
        log.error("Key validation DB error: %s", e)

    return None


# ── Rate limiting ─────────────────────────────────────────────────────────────
def check_key_rate_limit(raw_key: str, plan: str) -> tuple[bool, int, int]:
    """
    Check rate limit for a PackGen key.
    Returns (allowed, current_count, limit).
    """
    limit, window = RATE_LIMITS.get(plan, RATE_LIMITS["free"])
    rl_key = f"packgen_key:{hash_key(raw_key)[:16]}"
    return check_rate_limit(rl_key, limit, window)


# ── Helpers ───────────────────────────────────────────────────────────────────
def _invalidate_user_cache(user_id: str):
    """Remove all cache entries for a given user_id."""
    to_remove = [
        h for h, p in _key_cache.items()
        if p.get("user_id") == user_id
    ]
    for h in to_remove:
        _key_cache.pop(h, None)
        _key_cache_ts.pop(h, None)


def mask_key(raw_key: str) -> str:
    """Return a safely displayable version: pg-live-abcd...••••"""
    if len(raw_key) < 16:
        return raw_key
    return raw_key[:12] + "•" * 8 + raw_key[-4:]
