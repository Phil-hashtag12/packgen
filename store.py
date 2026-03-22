"""
store.py — Redis-backed job store for PackGen.

Jobs are serialised to Redis with a TTL. The fitz (PyMuPDF) document objects
cannot be serialised, so we keep a small in-process cache for the live docs
and store everything else (status, pool metadata, warnings, pack_files) in
Redis.  When a worker picks up a job that isn't in the local doc cache, it
re-opens the PDFs from disk.

Design:
  Redis key  →  "job:{jid}"          (hash, TTL = JOB_TTL seconds)
  Redis key  →  "ratelimit:{key}:{window_start}"  (counter, TTL = window)
"""

import os, json, time, uuid, logging
from pathlib import Path

log = logging.getLogger(__name__)

# ── Redis connection ──────────────────────────────────────────────────────────
try:
    import redis as _redis
    _REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    _r = _redis.from_url(_REDIS_URL, decode_responses=True)
    _r.ping()
    REDIS_AVAILABLE = True
    log.info("Redis connected: %s", _REDIS_URL)
except Exception as e:
    log.warning("Redis unavailable (%s) — falling back to in-memory store", e)
    _r = None
    REDIS_AVAILABLE = False

# In-memory fallback (single-process dev mode)
_mem: dict = {}

# In-process cache for live fitz docs (never serialised)
_doc_cache: dict = {}   # jid → {"open_docs": [...], "pool_docs": [...]}

JOB_TTL    = int(os.environ.get("JOB_TTL_SECONDS",    str(6 * 3600)))   # 6 h
UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR",  "uploads"))
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR",  "outputs"))


# ── Serialisation helpers ─────────────────────────────────────────────────────
def _serialisable_pool(pool: list) -> list:
    """Strip fitz objects from pool so it can be JSON-serialised."""
    out = []
    for q in pool:
        out.append({k: v for k, v in q.items()
                    if k not in ("qp_doc", "ms_doc")})
    return out


def _serialisable_job(job: dict) -> dict:
    """Return a JSON-safe copy of a job dict."""
    j = dict(job)
    j["pool"]      = _serialisable_pool(j.get("pool", []))
    j["open_docs"] = []   # never serialised — re-opened from disk
    return j


# ── Public API ────────────────────────────────────────────────────────────────
def new_job(jid: str | None = None) -> str:
    jid = jid or str(uuid.uuid4())[:8]
    job = {
        "jid":        jid,
        "status":     "idle",
        "progress":   0,
        "message":    "",
        "warnings":   [],
        "pool":       [],
        "open_docs":  [],
        "pack_files": [],
        "classified": False,
        "user_id":    None,
        "created_at": time.time(),
    }
    _set(jid, job)
    return jid


def get_job(jid: str) -> dict | None:
    raw = _get(jid)
    if raw is None:
        return None
    # Re-attach live fitz docs if available in process cache
    pool = raw.get("pool", [])
    if jid in _doc_cache:
        _reattach_docs(pool, _doc_cache[jid])
    raw["pool"] = pool
    return raw


def update_job(jid: str, **kw):
    job = _get(jid)
    if job is None:
        return
    # Handle pool specially — strip docs before storing
    if "pool" in kw:
        pool = kw["pool"]
        # Cache the live docs
        _doc_cache.setdefault(jid, {})
        _cache_docs(jid, pool)
        kw["pool"] = _serialisable_pool(pool)
    job.update(kw)
    _set(jid, job)


def job_exists(jid: str) -> bool:
    if REDIS_AVAILABLE:
        return bool(_r.exists(f"job:{jid}"))
    return jid in _mem


def set_job_docs(jid: str, open_docs: list, pool: list):
    """Store fitz docs in the process cache (not Redis)."""
    _doc_cache[jid] = {"open_docs": open_docs, "pool": pool}


def get_live_pool(jid: str) -> list:
    """Return pool with fitz docs re-attached (for render/classify operations)."""
    job = _get(jid)
    if not job:
        return []
    pool = job.get("pool", [])
    if jid in _doc_cache:
        _reattach_docs(pool, _doc_cache[jid])
    else:
        # Try to re-open from disk
        _reopen_docs_from_disk(jid, pool)
    return pool


def close_job_docs(jid: str):
    """Close and remove fitz docs for a job."""
    if jid not in _doc_cache:
        return
    for qp_doc, ms_doc in _doc_cache[jid].get("open_docs", []):
        try:
            qp_doc.close()
            ms_doc.close()
        except Exception:
            pass
    del _doc_cache[jid]


def delete_job(jid: str):
    close_job_docs(jid)
    if REDIS_AVAILABLE:
        _r.delete(f"job:{jid}")
    else:
        _mem.pop(jid, None)


# ── Rate limiting ─────────────────────────────────────────────────────────────
def check_rate_limit(key: str, limit: int, window_seconds: int) -> tuple[bool, int, int]:
    """
    Sliding-window counter rate limit.
    Returns (allowed, current_count, limit).
    key     — unique identifier (e.g. "packgen_key:pg-abc123")
    limit   — max requests per window
    window  — window size in seconds
    """
    now      = int(time.time())
    bucket   = now // window_seconds
    redis_key = f"rl:{key}:{bucket}"

    if REDIS_AVAILABLE:
        pipe  = _r.pipeline()
        pipe.incr(redis_key)
        pipe.expire(redis_key, window_seconds * 2)
        count, _ = pipe.execute()
    else:
        # In-memory fallback — simple dict
        _mem_rl = _mem.setdefault("__rl__", {})
        old_bucket = now // window_seconds - 1
        _mem_rl.pop(f"{key}:{old_bucket}", None)
        count = _mem_rl.get(f"{key}:{bucket}", 0) + 1
        _mem_rl[f"{key}:{bucket}"] = count

    return count <= limit, count, limit


# ── Internal helpers ──────────────────────────────────────────────────────────
def _get(jid: str) -> dict | None:
    if REDIS_AVAILABLE:
        raw = _r.hgetall(f"job:{jid}")
        if not raw:
            return None
        # Deserialise JSON fields
        for field in ("warnings", "pool", "pack_files", "open_docs"):
            if field in raw:
                try:
                    raw[field] = json.loads(raw[field])
                except Exception:
                    raw[field] = []
        for field in ("progress",):
            if field in raw:
                try:
                    raw[field] = int(raw[field])
                except Exception:
                    pass
        for field in ("classified",):
            if field in raw:
                raw[field] = raw[field] == "True"
        return raw
    return _mem.get(jid)


def _set(jid: str, job: dict):
    if REDIS_AVAILABLE:
        flat = {}
        for k, v in job.items():
            if isinstance(v, (list, dict)):
                flat[k] = json.dumps(v)
            elif isinstance(v, bool):
                flat[k] = str(v)
            elif v is None:
                flat[k] = ""
            else:
                flat[k] = str(v)
        _r.hset(f"job:{jid}", mapping=flat)
        _r.expire(f"job:{jid}", JOB_TTL)
    else:
        _mem[jid] = job


def _cache_docs(jid: str, pool: list):
    """Cache fitz document references from pool items."""
    if jid not in _doc_cache:
        _doc_cache[jid] = {"open_docs": [], "pool": []}
    _doc_cache[jid]["pool"] = pool


def _reattach_docs(pool: list, cache: dict):
    """Re-attach fitz doc references to a deserialised pool list."""
    cached_pool = cache.get("pool", [])
    # Build lookup by (key, q)
    lookup = {(q["key"], q["q"]): q for q in cached_pool if "qp_doc" in q}
    for item in pool:
        ref = lookup.get((item["key"], item["q"]))
        if ref:
            item["qp_doc"]   = ref["qp_doc"]
            item["ms_doc"]   = ref["ms_doc"]
            item["qp_parts"] = ref["qp_parts"]
            item["ms_parts"] = ref["ms_parts"]


def _reopen_docs_from_disk(jid: str, pool: list):
    """Re-open fitz documents from stored paths when process cache is cold."""
    import fitz
    doc_pairs: dict[tuple, tuple] = {}
    for item in pool:
        qp_path = item.get("qp_path", "")
        ms_path = item.get("ms_path", "")
        if not qp_path or not ms_path:
            continue
        key = (qp_path, ms_path)
        if key not in doc_pairs:
            try:
                doc_pairs[key] = (fitz.open(qp_path), fitz.open(ms_path))
            except Exception:
                continue
        qp_doc, ms_doc = doc_pairs[key]
        item["qp_doc"] = qp_doc
        item["ms_doc"] = ms_doc
        # Restore fitz.Rect objects
        if item.get("qp_parts") and isinstance(item["qp_parts"][0][1], list):
            import fitz as fz
            item["qp_parts"] = [(p, fz.Rect(r)) for p, r in item["qp_parts"]]
            item["ms_parts"] = [(p, fz.Rect(r)) for p, r in item["ms_parts"]]

    if doc_pairs:
        _doc_cache[jid] = {
            "open_docs": list(doc_pairs.values()),
            "pool": pool,
        }
