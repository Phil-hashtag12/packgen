"""
PackGen Flask backend — v3.

New in v3:
- Mark tracking: POST /api/scores/<jid>/<pack_num>
- Progress analytics: GET /api/progress/<jid>
- Paper stats: GET /api/paper-stats/<jid>
- Pack attempt history: GET /api/attempts
- Spaced repetition: generate weighted by weakness scores
- ZIP download: GET /api/download-zip/<jid>/<pack_num>
- Admin dashboard: GET /admin (requires ADMIN_TOKEN header)
- Missing-key fallback: POST /api/user/recover-key
- Session path validation on load
- OpenRouter + Anthropic classifier backend
"""

import os, json, uuid, threading, base64, logging, time, re, zipfile, io, random
from pathlib import Path, PurePosixPath
from functools import wraps

from flask import (Flask, request, jsonify, send_file,
                   render_template, g, abort)
import secrets as _secrets

# ── Sentry ────────────────────────────────────────────────────────────────────
SENTRY_DSN = os.environ.get("SENTRY_DSN", "")
if SENTRY_DSN:
    import sentry_sdk
    from sentry_sdk.integrations.flask import FlaskIntegration
    sentry_sdk.init(dsn=SENTRY_DSN, integrations=[FlaskIntegration()],
                    traces_sample_rate=0.1)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-8s %(name)s  %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("packgen")

from extractor  import (find_pairs, build_question_pool, render_question_png,
                         pool_to_session, session_to_pool)
from packer     import make_packs, build_pack_pdfs, SPEC_TOPICS, estimate_time
import classifier as _classifier
from classifier import classify_batch
from auth       import verify_token, get_user_profile, increment_usage, check_quota
from api_keys   import (validate_packgen_key, check_key_rate_limit,
                         provision_user_key, rotate_user_key, mask_key)
from store      import (new_job, get_job, update_job, job_exists,
                         set_job_docs, get_live_pool, close_job_docs, delete_job)
from cleanup    import start_cleanup_thread
from scores     import (record_pack_attempt, get_pack_attempts,
                         topic_progress, paper_stats, weakness_weights,
                         performance_heatmap)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 600 * 1024 * 1024
app.config["SECRET_KEY"] = os.environ.get("FLASK_SECRET", _secrets.token_hex(32))

UPLOAD_DIR  = Path(os.environ.get("UPLOAD_DIR",  "uploads"))
OUTPUT_DIR  = Path(os.environ.get("OUTPUT_DIR",  "outputs"))
SESSION_DIR = Path(os.environ.get("SESSION_DIR", "sessions"))
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")

for d in (UPLOAD_DIR, OUTPUT_DIR, SESSION_DIR):
    d.mkdir(exist_ok=True)

start_cleanup_thread()


# ── Security ──────────────────────────────────────────────────────────────────
_SAFE_FILENAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9 _\-()\.]{0,120}\.(pdf|zip)$')

def safe_filename(name: str) -> str:
    name = PurePosixPath(name).name
    name = Path(name).name
    if not _SAFE_FILENAME.match(name):
        raise ValueError(f"Unsafe filename: {name!r}")
    return name


def require_job(f):
    @wraps(f)
    def wrapper(jid, *args, **kwargs):
        if not re.fullmatch(r'[a-f0-9]{8}', jid):
            abort(404)
        if not job_exists(jid):
            return jsonify({"error": "Unknown job"}), 404
        g.jid = jid
        g.job = get_job(jid)
        return f(jid, *args, **kwargs)
    return wrapper


# ── Auth ──────────────────────────────────────────────────────────────────────
def _resolve_caller():
    pg_key = request.headers.get("X-PackGen-Key", "").strip()
    if pg_key:
        profile = validate_packgen_key(pg_key)
        if profile:
            return {"id": profile["user_id"], "via": "pg_key"}, profile
        return None, None
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        user = verify_token(auth[7:])
        if user:
            return user, get_user_profile(user["id"])
    return None, None


def optional_auth(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        g.user = None
        g.profile = None
        g.pg_key = request.headers.get("X-PackGen-Key", "").strip()
        user, profile = _resolve_caller()
        g.user = user
        g.profile = profile
        return f(*args, **kwargs)
    return wrapper


def require_pg_key(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        pg_key = request.headers.get("X-PackGen-Key", "").strip()
        if not pg_key:
            return jsonify({"error": "Missing X-PackGen-Key header", "code": "missing_key"}), 401
        profile = validate_packgen_key(pg_key)
        if not profile:
            return jsonify({"error": "Invalid PackGen API key", "code": "invalid_key"}), 401
        plan = profile.get("plan", "free")
        allowed, count, limit = check_key_rate_limit(pg_key, plan)
        if not allowed:
            return jsonify({"error": "Rate limit exceeded", "limit": limit,
                            "count": count, "retry_after": 3600}), 429
        g.pg_key = pg_key
        g.profile = profile
        g.user = {"id": profile["user_id"]}
        return f(*args, **kwargs)
    return wrapper


def pool_summary(pool):
    return [{"key": q["key"], "q": q["q"], "marks": q["marks"],
             "topic": q.get("topic"), "subtopic": q.get("subtopic"),
             "difficulty": q.get("difficulty")} for q in pool]


@app.before_request
def _t0():
    g.t0 = time.monotonic()

@app.after_request
def _log(resp):
    ms = (time.monotonic() - getattr(g, "t0", time.monotonic())) * 1000
    log.info("%s %s → %d (%.0fms)", request.method, request.path, resp.status_code, ms)
    return resp


# ════════════════════════════════════════════════════════════════
# Routes
# ════════════════════════════════════════════════════════════════

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/health")
def health():
    from store import REDIS_AVAILABLE
    return jsonify({"ok": True, "redis": REDIS_AVAILABLE})

@app.route("/api/config")
def config():
    # NOTE: only expose what the frontend needs for auth — no secrets
    return jsonify({
        "has_api_key":   bool(os.environ.get("MISTRAL_API_KEY", "").strip() or
                              os.environ.get("ANTHROPIC_API_KEY", "").strip() or
                              os.environ.get("OPENROUTER_API_KEY", "").strip()),
        "ai_backend":    os.environ.get("AI_BACKEND", "mistral"),
        "auth_enabled":  bool(os.environ.get("SUPABASE_URL", "")),
        "supabase_url":  os.environ.get("SUPABASE_URL", ""),
        "supabase_anon": os.environ.get("SUPABASE_ANON_KEY", ""),
    })

@app.route("/api/debug/ai")
def debug_ai():
    """
    Safe runtime diagnostics for AI classification config.
    No secrets are returned, only booleans and selected identifiers.
    """
    return jsonify({
        "env_ai_backend": os.environ.get("AI_BACKEND", ""),
        "classifier_ai_backend": _classifier.AI_BACKEND,
        "has_mistral_env_key": bool(os.environ.get("MISTRAL_API_KEY", "").strip()),
        "has_openrouter_env_key": bool(os.environ.get("OPENROUTER_API_KEY", "").strip()),
        "has_anthropic_env_key": bool(os.environ.get("ANTHROPIC_API_KEY", "").strip()),
        "classifier_has_mistral_key": bool((_classifier.MISTRAL_API_KEY or "").strip()),
        "classifier_has_openrouter_key": bool((_classifier.OPENROUTER_API_KEY or "").strip()),
        "classifier_has_anthropic_key": bool((_classifier.ANTHROPIC_API_KEY or "").strip()),
        "mi_text_model": _classifier.MI_TEXT_MODEL,
        "mi_vision_model": _classifier.MI_VISION_MODEL,
        "or_text_model": _classifier.OR_TEXT_MODEL,
        "or_vision_model": _classifier.OR_VISION_MODEL,
    })

@app.route("/api/topics")
def get_topics():
    return jsonify({"topics": SPEC_TOPICS})


# ── User / key management ─────────────────────────────────────────────────────

@app.route("/api/user/provision-key", methods=["POST"])
def provision_key():
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return jsonify({"error": "Unauthorized"}), 401
    user = verify_token(auth[7:])
    if not user:
        return jsonify({"error": "Invalid token"}), 401
    raw_key = provision_user_key(user["id"])
    log.info("Key provisioned for user %s", user["id"][:8])
    return jsonify({"packgen_key": raw_key, "masked": mask_key(raw_key),
                    "note": "Store this key — it will not be shown again."})


@app.route("/api/user/rotate-key", methods=["POST"])
def rotate_key():
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return jsonify({"error": "Unauthorized"}), 401
    user = verify_token(auth[7:])
    if not user:
        return jsonify({"error": "Invalid token"}), 401
    raw_key = rotate_user_key(user["id"])
    return jsonify({"packgen_key": raw_key, "masked": mask_key(raw_key),
                    "note": "Old key has been invalidated."})


@app.route("/api/user/recover-key", methods=["POST"])
def recover_key():
    """
    Called when a signed-in user's pg_key is missing from localStorage
    (e.g. new device, cleared storage). Re-provisions a new key using JWT.
    Same as provision-key but with clearer semantics.
    """
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return jsonify({"error": "Unauthorized"}), 401
    user = verify_token(auth[7:])
    if not user:
        return jsonify({"error": "Invalid token"}), 401
    # rotate gives them a fresh key without needing the old one
    raw_key = rotate_user_key(user["id"])
    log.info("Key recovered for user %s", user["id"][:8])
    return jsonify({"packgen_key": raw_key, "masked": mask_key(raw_key)})


@app.route("/api/user/profile")
@optional_auth
def user_profile():
    if not g.profile:
        return jsonify({"auth_enabled": bool(os.environ.get("SUPABASE_URL", ""))})
    p = g.profile
    from api_keys import RATE_LIMITS
    plan  = p.get("plan", "free")
    limit, _ = RATE_LIMITS.get(plan, RATE_LIMITS["free"])
    return jsonify({
        "plan":                plan,
        "usage_this_month":    p.get("usage_this_month", 0),
        "quota":               500 if plan == "free" else 5000,
        "packgen_key_prefix":  p.get("packgen_key_prefix", ""),
        "rate_limit_per_hour": limit,
        "auth_enabled":        bool(os.environ.get("SUPABASE_URL", "")),
    })


# ── Job lifecycle ─────────────────────────────────────────────────────────────

@app.route("/api/job/new", methods=["POST"])
@optional_auth
def new_job_route():
    jid = new_job()
    (UPLOAD_DIR / jid).mkdir(exist_ok=True)
    (OUTPUT_DIR / jid).mkdir(exist_ok=True)
    if g.user:
        update_job(jid, user_id=g.user["id"])
    return jsonify({"job_id": jid})


@app.route("/api/upload/<jid>", methods=["POST"])
@require_job
def upload_files(jid):
    saved, errors = 0, []
    for f in request.files.getlist("files"):
        try:
            fname = safe_filename(f.filename)
        except ValueError as e:
            errors.append(str(e)); continue
        dest = UPLOAD_DIR / jid / fname
        f.save(str(dest)); dest.touch(); saved += 1
    spec = request.files.get("spec")
    if spec:
        try:
            safe_filename(spec.filename)
            spec.save(str(UPLOAD_DIR / jid / "__spec__.pdf"))
        except ValueError:
            errors.append("Spec filename rejected")
    try:
        pairs, warnings = find_pairs(UPLOAD_DIR / jid)
    except Exception as e:
        log.exception("Job %s: pair detection failed", jid)
        return jsonify({"error": f"Pair detection failed: {e}"}), 500
    return jsonify({"saved": saved, "pairs_found": len(pairs),
                    "pair_names": [p[0] for p in pairs],
                    "warnings": warnings + errors})


@app.route("/api/extract/<jid>", methods=["POST"])
@require_job
def extract(jid):
    def run():
        try:
            update_job(jid, status="extracting", progress=0,
                       message="Finding pairs…", warnings=[])
            pairs, warnings = find_pairs(UPLOAD_DIR / jid)
            if not pairs:
                update_job(jid, status="error",
                           message="No matched QP/MS pairs found."); return
            def prog(i, n, msg):
                update_job(jid, progress=int(i / n * 90), message=msg)
            pool, open_docs, w2 = build_question_pool(pairs, progress_cb=prog)
            set_job_docs(jid, open_docs, pool)
            update_job(jid, status="extracted", progress=100,
                       message=f"Extracted {len(pool)} questions from {len(pairs)} papers.",
                       warnings=warnings + w2, pool=pool, classified=False)
        except Exception as e:
            log.exception("Job %s: extraction failed", jid)
            update_job(jid, status="error", progress=100, message=f"Extraction failed: {e}")
    threading.Thread(target=run, daemon=True, name=f"extract-{jid}").start()
    return jsonify({"started": True})


@app.route("/api/classify/<jid>", methods=["POST"])
@require_job
def classify(jid):
    data = request.json or {}
    job  = g.job

    # Resolve AI key: request body (BYO) → user profile → env (Mistral/OpenRouter/Anthropic)
    ai_key = ""
    if not ai_key:
        ai_key = data.get("api_key", "").strip()
    if job.get("user_id"):
        profile = get_user_profile(job["user_id"])
        if not ai_key:
            ai_key = profile.get("anthropic_api_key", "").strip()
    if not ai_key:
        ai_key = (os.environ.get("MISTRAL_API_KEY", "") or
                  os.environ.get("OPENROUTER_API_KEY", "") or
                  os.environ.get("ANTHROPIC_API_KEY", ""))

    def run():
        try:
            pool        = get_live_pool(jid)
            current_job = get_job(jid)
            usable_labels = sum(1 for q in pool if (q.get("topic") or "Unknown") != "Unknown")
            ai_debug = {
                "backend": _classifier.AI_BACKEND,
                "mi_text_model": _classifier.MI_TEXT_MODEL,
                "mi_vision_model": _classifier.MI_VISION_MODEL,
                "or_text_model": _classifier.OR_TEXT_MODEL,
                "or_vision_model": _classifier.OR_VISION_MODEL,
                "pool_size": len(pool),
                "usable_labels_before": usable_labels,
                "force": bool(data.get("force")),
            }
            update_job(jid, ai_debug=ai_debug)
            if current_job.get("classified") and not data.get("force") and usable_labels > 0:
                update_job(jid, status="classified", progress=100,
                           message=f"Using {len(pool)} cached classifications."); return
            log.info(
                "Job %s: classify start backend=%s model=%s vision_model=%s pool=%d force=%s usable_labels=%d",
                jid,
                _classifier.AI_BACKEND,
                _classifier.OR_TEXT_MODEL,
                _classifier.OR_VISION_MODEL,
                len(pool),
                bool(data.get("force")),
                usable_labels,
            )
            user_id = current_job.get("user_id")
            if user_id and os.environ.get("SUPABASE_URL"):
                profile = get_user_profile(user_id)
                allowed, used, quota = check_quota(profile)
                if not allowed:
                    update_job(jid, status="error",
                               message=f"Quota reached ({used}/{quota}). Upgrade to Pro."); return
            update_job(jid, status="classifying", progress=0, message="Classifying…")
            spec_text = ""
            spec_path = UPLOAD_DIR / jid / "__spec__.pdf"
            if spec_path.exists():
                try:
                    import fitz
                    doc = fitz.open(str(spec_path))
                    spec_text = "\n".join(doc[i].get_text()
                                          for i in range(min(doc.page_count, 20)))
                    doc.close()
                except Exception: pass
            def prog(done, total, msg):
                update_job(jid, progress=int(done / total * 100), message=msg)
            classify_batch(pool, api_key=ai_key, spec_text=spec_text, progress_cb=prog)

            classified_count = sum(1 for q in pool if (q.get("topic") or "Unknown") != "Unknown")
            ai_debug["usable_labels_after"] = classified_count
            ai_debug["unknown_after"] = len(pool) - classified_count
            if pool and classified_count == 0:
                update_job(
                    jid,
                    status="error",
                    progress=100,
                    message="Classification failed: provider returned no usable labels. Check your API key/backend and try again.",
                    ai_debug=ai_debug,
                )
                return

            if user_id:
                increment_usage(user_id, len(pool))
            set_job_docs(jid, [], pool)
            update_job(jid, status="classified", progress=100,
                       message=f"Classified {len(pool)} questions.",
                       pool=pool, classified=True, ai_debug=ai_debug)
            log.info("Job %s: classified %d questions", jid, len(pool))
        except Exception as e:
            log.exception("Job %s: classify failed", jid)
            update_job(
                jid,
                status="error",
                progress=100,
                message=f"Classification failed: {e}",
                ai_debug={"backend": _classifier.AI_BACKEND, "error": str(e)},
            )
    threading.Thread(target=run, daemon=True, name=f"classify-{jid}").start()
    return jsonify({"started": True})


@app.route("/api/pool/<jid>")
@require_job
def get_pool_route(jid):
    pool = get_live_pool(jid)
    return jsonify({"pool": pool_summary(pool),
                    "total_questions": len(pool),
                    "total_marks": sum(q["marks"] for q in pool)})


@app.route("/api/preview/<jid>/<int:q_idx>")
@require_job
def preview(jid, q_idx):
    pool = get_live_pool(jid)
    if q_idx >= len(pool):
        return jsonify({"error": "Out of range"}), 404
    try:
        dpi = min(int(request.args.get("dpi", 120)), 200)
        png = render_question_png(pool[q_idx], dpi=dpi)
        return jsonify({"image": f"data:image/png;base64,{base64.standard_b64encode(png).decode()}"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/heatmap/<jid>")
@require_job
def heatmap(jid):
    pool = get_live_pool(jid)
    data: dict = {}
    for q in pool:
        t = q.get("topic") or "Unknown"
        d = q.get("difficulty") or 0
        data.setdefault(t, {})
        data[t][d] = data[t].get(d, 0) + 1
    # Also add performance heatmap if user is known
    perf = {}
    user_id = g.job.get("user_id") if g.job else None
    if user_id:
        perf = performance_heatmap(user_id)
    return jsonify({"heatmap": data, "performance": perf})


# ── Pack generation with optional spaced-repetition weighting ─────────────────

@app.route("/api/generate/<jid>", methods=["POST"])
@require_job
def generate(jid):
    data = request.json or {}

    # Capture user_id from request context before entering the thread
    _user_id_for_generate = g.job.get("user_id") if g.job else None

    def run():
        update_job(jid, status="generating", progress=0, message="Building packs…")
        pool = get_live_pool(jid)
        if not pool:
            update_job(jid, status="error", message="No questions — extract first."); return
        log.info(
            "Job %s: starting generation with %d questions (%d marks), filters=%s",
            jid,
            len(pool),
            sum(q.get("marks", 0) for q in pool),
            {
                "topic_filter": data.get("topic_filter"),
                "diff_filter": data.get("diff_filter"),
                "balance_topics": data.get("balance_topics", True),
                "spaced_repetition": data.get("spaced_repetition", False),
            },
        )

        # Spaced repetition: apply weakness weights if user has score data
        user_id = _user_id_for_generate
        weights = None
        if user_id and data.get("spaced_repetition", False):
            weights = weakness_weights(user_id, pool)
            log.info("Spaced repetition enabled for job %s", jid)

        packs = make_packs(
            pool,
            topic_filter=data.get("topic_filter"),
            difficulty_filter=tuple(data["diff_filter"]) if data.get("diff_filter") else None,
            balance_topics=data.get("balance_topics", True),
            weights=weights,
        )
        log.info("Job %s: make_packs returned %d pack(s)", jid, len(packs))
        if not packs:
            update_job(
                jid,
                status="error",
                message=f"No packs with current filters. "
                        f"Questions={len(pool)}, Marks={sum(q.get('marks', 0) for q in pool)}.",
            )
            return

        out_dir = OUTPUT_DIR / jid
        out_dir.mkdir(exist_ok=True)
        pack_files = []
        job_warnings = list(get_job(jid).get("warnings", []))

        for i, pack in enumerate(packs, 1):
            update_job(jid, progress=int(i / len(packs) * 100),
                       message=f"Writing pack {i}/{len(packs)}…")
            try:
                qf, mf, total_marks, topic_counts, est_time = build_pack_pdfs(pack, i, out_dir)
                if not qf.exists() or not mf.exists():
                    raise RuntimeError("Generated PDF files were not written to disk")
                pack_files.append({
                    "pack_num": i, "total_marks": total_marks,
                    "question_count": len(pack), "topics": topic_counts,
                    "questions": pool_summary(pack),
                    "q_file": qf.name, "ms_file": mf.name, "est_time": est_time,
                    "attempted": False, "attempt_date": None,
                })
            except Exception as e:
                job_warnings.append(f"Pack {i} error: {e}")
                log.error("Pack %d error: %s", i, e)

        if not pack_files:
            update_job(
                jid,
                status="error",
                progress=100,
                message="Pack generation failed. No output files were created.",
                pack_files=[],
                warnings=job_warnings,
            )
            log.error("Job %s: all pack builds failed (%d attempted)", jid, len(packs))
            return

        failed_count = len(packs) - len(pack_files)
        done_message = f"Generated {len(pack_files)} packs."
        if failed_count > 0:
            done_message += f" ({failed_count} failed - see warnings.)"

        update_job(jid, status="done", progress=100,
                   message=done_message,
                   pack_files=pack_files, warnings=job_warnings)
        log.info("Job %s: generated %d packs", jid, len(pack_files))
    threading.Thread(target=run, daemon=True, name=f"generate-{jid}").start()
    return jsonify({"started": True})


@app.route("/api/custom-pack/<jid>", methods=["POST"])
@require_job
def custom_pack(jid):
    data         = request.json or {}
    prompt       = (data.get("prompt") or "").strip()
    pack_count   = max(1, min(int(data.get("pack_count", 1)), 8))
    target_marks = max(40, min(int(data.get("target_marks", 100)), 200))
    diff_filter  = data.get("difficulty_filter")
    use_sr       = data.get("spaced_repetition", True)
    legacy_topics = data.get("topics", [])

    pool = get_live_pool(jid)
    if not pool:
        return jsonify({"error": "No questions. Extract first."}), 400

    def _pick_difficulty_from_prompt(text: str):
        text = (text or "").lower()
        if "easy" in text:
            return (1, 2)
        if any(k in text for k in ("hard", "harder", "challenging", "difficult")):
            return (4, 5)
        if "medium" in text:
            return (2, 4)
        m = re.search(r"difficulty\s*([1-5])(?:\s*[-to]+\s*([1-5]))?", text)
        if m:
            lo = int(m.group(1))
            hi = int(m.group(2)) if m.group(2) else lo
            return (min(lo, hi), max(lo, hi))
        return None

    def _question_matches_prompt(q: dict, text: str) -> bool:
        if not text:
            return False
        blob = " ".join([
            q.get("topic", ""),
            q.get("subtopic", ""),
            q.get("source_text", "")[:400],
        ]).lower()
        words = [w for w in re.findall(r"[a-z0-9\-\+]+", text.lower()) if len(w) >= 4]
        if not words:
            return False
        hits = sum(1 for w in words if w in blob)
        return hits >= 1

    def _build_pack_from_candidates(cands: list[dict], tmarks: int, qweights: dict[str, float], used: set[tuple[str, int]]):
        scored = []
        for q in cands:
            qk = (q.get("key"), q.get("q"))
            if qk in used:
                continue
            wk = f"{q.get('key')}|{q.get('q')}"
            weight = float(qweights.get(wk, 1.0))
            scored.append((weight, q.get("marks", 0), random.random(), q))
        scored.sort(key=lambda x: (-x[0], -x[1], x[2]))
        chosen = []
        marks = 0
        for _, _, _, q in scored:
            qm = int(q.get("marks", 0))
            if marks + qm <= tmarks + 8:
                chosen.append(q)
                used.add((q.get("key"), q.get("q")))
                marks += qm
            if marks >= tmarks - 8:
                break
        return chosen, marks

    user_id = get_job(jid).get("user_id") if job_exists(jid) else None
    weights = weakness_weights(user_id, pool) if (use_sr and user_id) else {}

    # Build initial candidates from prompt (chat-style), then fallback to legacy topic/subtopic filters.
    candidates = []
    prompt_diff = _pick_difficulty_from_prompt(prompt) if prompt else None
    effective_diff = tuple(diff_filter) if diff_filter else prompt_diff
    wants_weak = any(k in prompt.lower() for k in ["struggle", "weak", "mistake", "wrong", "improve"]) if prompt else False

    if prompt:
        candidates = [q for q in pool if _question_matches_prompt(q, prompt)]
        if not candidates and wants_weak and weights:
            # If no lexical match, use weakest questions across the whole pool.
            candidates = sorted(pool, key=lambda q: weights.get(f"{q['key']}|{q['q']}", 1.0), reverse=True)[:350]

    if not candidates and legacy_topics:
        for sel in legacy_topics:
            t = sel.get("topic", "")
            subs = sel.get("subtopics", [])
            for q in pool:
                if q.get("topic") != t:
                    continue
                if subs and q.get("subtopic") not in subs:
                    continue
                candidates.append(q)

    if not candidates:
        candidates = list(pool)

    # De-duplicate candidate list
    seen = set()
    deduped = []
    for q in candidates:
        qk = (q.get("key"), q.get("q"))
        if qk in seen:
            continue
        seen.add(qk)
        deduped.append(q)
    candidates = deduped

    if not candidates:
        return jsonify({"error": "No questions match your request."}), 400

    if effective_diff:
        lo, hi = effective_diff
        candidates = [q for q in candidates if lo <= (q.get("difficulty") or 3) <= hi]
    if not candidates:
        return jsonify({"error": "No questions match difficulty filter."}), 400

    job = get_job(jid)
    out_dir = OUTPUT_DIR / jid
    out_dir.mkdir(exist_ok=True)
    current_packs = job.get("pack_files", [])
    used_questions = set()
    built_packs = []

    for _ in range(pack_count):
        pack, used_marks = _build_pack_from_candidates(candidates, target_marks, weights, used_questions)
        if not pack:
            break
        pack_num = len(current_packs) + 1
        try:
            qf, mf, total_marks, topic_counts, est_time = build_pack_pdfs(pack, pack_num, out_dir)
        except Exception as e:
            return jsonify({"error": str(e)}), 500

        pack_meta = {
            "pack_num": pack_num,
            "total_marks": total_marks,
            "question_count": len(pack),
            "topics": topic_counts,
            "questions": pool_summary(pack),
            "q_file": qf.name,
            "ms_file": mf.name,
            "est_time": est_time,
            "custom": True,
            "attempted": False,
            "attempt_date": None,
        }
        current_packs.append(pack_meta)
        built_packs.append(pack_meta)

    if not built_packs:
        return jsonify({"error": "Could not build any packs with current request."}), 400

    update_job(jid, pack_files=current_packs)

    # Insights for chatbot-style UI
    weak_breakdown = []
    if user_id:
        try:
            progress = topic_progress(user_id)
            for topic, pdata in progress.items():
                for sub, sdata in (pdata.get("subtopics") or {}).items():
                    weak_breakdown.append({
                        "topic": topic,
                        "subtopic": sub,
                        "avg_pct": sdata.get("avg_pct", 0),
                        "attempted": sdata.get("attempted", 0),
                    })
            weak_breakdown.sort(key=lambda x: (x["avg_pct"], -x["attempted"]))
        except Exception:
            weak_breakdown = []

    return jsonify({
        "pack": built_packs[0],           # backward compatibility
        "packs": built_packs,
        "insights": weak_breakdown[:8],
        "matched_candidates": len(candidates),
        "prompt": prompt,
        "pack_count": len(built_packs),
    })


@app.route("/api/status/<jid>")
@require_job
def status(jid):
    j = get_job(jid)
    if not j:
        return jsonify({"error": "Job state unavailable"}), 404
    return jsonify({
        "status": j["status"], "progress": j["progress"],
        "message": j["message"], "warnings": j.get("warnings", []),
        "pack_files": j.get("pack_files", []),
        "classified": j.get("classified", False),
        "warning_count": len(j.get("warnings", [])),
        "ai_debug": j.get("ai_debug", {}),
    })


@app.route("/api/download/<jid>/<filename>")
@require_job
def download(jid, filename):
    try:
        fname = safe_filename(filename)
    except ValueError:
        abort(400)
    path = OUTPUT_DIR / jid / fname
    if not path.exists():
        return jsonify({"error": "Not found"}), 404
    path.touch()
    return send_file(str(path), as_attachment=True)


@app.route("/api/download-zip/<jid>/<int:pack_num>")
@require_job
def download_zip(jid, pack_num):
    """Bundle question paper + mark scheme into a single ZIP."""
    job        = get_job(jid)
    pack_files = job.get("pack_files", [])
    pack_meta  = next((p for p in pack_files if p["pack_num"] == pack_num), None)
    if not pack_meta:
        return jsonify({"error": "Pack not found"}), 404

    q_path  = OUTPUT_DIR / jid / pack_meta["q_file"]
    ms_path = OUTPUT_DIR / jid / pack_meta["ms_file"]

    if not q_path.exists() or not ms_path.exists():
        return jsonify({"error": "Pack files missing — regenerate"}), 404

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(q_path,  pack_meta["q_file"])
        zf.write(ms_path, pack_meta["ms_file"])
    buf.seek(0)

    zip_name = f"PackGen_Pack_{pack_num:02d}_{pack_meta['total_marks']}marks.zip"
    return send_file(buf, as_attachment=True, download_name=zip_name,
                     mimetype="application/zip")


# ── Mark tracking & analytics ─────────────────────────────────────────────────

@app.route("/api/scores/<jid>/<int:pack_num>", methods=["POST"])
@require_job
def submit_scores(jid, pack_num):
    """
    Submit scores for a completed pack.
    Body: {"scores": [{"key":"...", "q":18, "score":3}, ...]}
    """
    data    = request.json or {}
    scores  = data.get("scores", [])
    job     = get_job(jid)
    user_id = job.get("user_id", "dev")

    # Enrich scores with marks/topic from pool
    pool    = get_live_pool(jid)
    pool_lk = {(q["key"], q["q"]): q for q in pool}

    enriched = []
    for s in scores:
        q = pool_lk.get((s["key"], s["q"]))
        if not q: continue
        enriched.append({
            "key":        s["key"],
            "q":          s["q"],
            "score":      max(0, min(int(s.get("score", 0)), q["marks"])),
            "marks":      q["marks"],
            "topic":      q.get("topic", "Unknown"),
            "subtopic":   q.get("subtopic", "Unknown"),
            "difficulty": q.get("difficulty", 3),
        })

    if enriched:
        record_pack_attempt(user_id, jid, pack_num, enriched)

    # Mark pack as attempted in job state
    pack_files = job.get("pack_files", [])
    for pf in pack_files:
        if pf["pack_num"] == pack_num:
            pf["attempted"]     = True
            pf["attempt_date"]  = time.strftime("%Y-%m-%d")
            total_scored = sum(s["score"] for s in enriched)
            total_marks  = sum(s["marks"]  for s in enriched)
            pf["score_pct"] = round(total_scored / total_marks * 100) if total_marks else 0
            break
    update_job(jid, pack_files=pack_files)

    return jsonify({"recorded": len(enriched)})


@app.route("/api/progress/<jid>")
@require_job
def progress(jid):
    """Topic progress + paper stats for the progress view."""
    job     = get_job(jid)
    user_id = job.get("user_id", "dev")
    pool    = get_live_pool(jid)
    return jsonify({
        "topic_progress": topic_progress(user_id),
        "paper_stats":    paper_stats(user_id, pool),
    })


@app.route("/api/attempts")
@optional_auth
def attempts():
    """Pack attempt history for the current user."""
    user_id = g.user["id"] if g.user else "dev"
    return jsonify({"attempts": get_pack_attempts(user_id)})


# ── Session endpoints ─────────────────────────────────────────────────────────

@app.route("/api/session/save/<jid>", methods=["POST"])
@require_job
def save_session(jid):
    pool = get_live_pool(jid)
    job  = get_job(jid)
    session = {
        "jid": jid, "pool": pool_to_session(pool),
        "warnings": job.get("warnings", []),
        "classified": job.get("classified", False),
        # Keep full pack metadata so LLM context and analytics still work
        # after restoring from a cached session.
        "pack_files": job.get("pack_files", []),
    }
    path = SESSION_DIR / f"{jid}.json"
    path.write_text(json.dumps(session, indent=2))
    return jsonify({"saved": path.name, "questions": len(pool)})


@app.route("/api/session/list")
def list_sessions():
    sessions = []
    for f in sorted(SESSION_DIR.glob("*.json"),
                    key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            d = json.loads(f.read_text())
            sessions.append({
                "jid": d["jid"], "questions": len(d["pool"]),
                "marks": sum(q["marks"] for q in d["pool"]),
                "classified": d.get("classified", any(q.get("topic") for q in d["pool"])),
                "filename": f.name,
            })
        except Exception: pass
    return jsonify({"sessions": sessions})


@app.route("/api/session/load/<filename>", methods=["POST"])
def load_session(filename):
    if not re.fullmatch(r'[a-f0-9]{8}\.json', filename):
        abort(400)
    path = SESSION_DIR / filename
    if not path.exists():
        return jsonify({"error": "Session not found"}), 404
    try:
        data = json.loads(path.read_text())

        # Validate PDF paths before trying to open them
        pool_items = data.get("pool", [])
        missing = []
        for item in pool_items[:3]:   # spot-check first 3
            qp = item.get("qp_path", "")
            ms = item.get("ms_path", "")
            if qp and not Path(qp).exists():
                missing.append(Path(qp).name)
            if ms and not Path(ms).exists():
                missing.append(Path(ms).name)
        if missing:
            return jsonify({
                "error": "PDFs have moved since this session was saved.",
                "missing_files": missing[:5],
                "hint": "Re-upload the original PDF files to restore this session.",
            }), 400

        pool, open_docs = session_to_pool(pool_items)
        if not pool:
            return jsonify({"error": "Could not reload PDFs — files may have moved"}), 400
        jid        = new_job(data["jid"])
        classified = data.get("classified", any(q.get("topic") for q in pool))
        (OUTPUT_DIR / jid).mkdir(exist_ok=True)
        set_job_docs(jid, open_docs, pool)
        update_job(jid, status="classified" if classified else "extracted",
                   pool=pool, classified=classified,
                   pack_files=data.get("pack_files", []),
                   warnings=data.get("warnings", []),
                   message=f"Loaded {len(pool)} questions from session.")
        return jsonify({"job_id": jid, "questions": len(pool),
                        "marks": sum(q["marks"] for q in pool),
                        "classified": classified})
    except Exception as e:
        log.error("Session load: %s", e)
        return jsonify({"error": str(e)}), 500


# ── LLM context ───────────────────────────────────────────────────────────────

@app.route("/api/llm-context/<jid>/<int:pack_num>")
@require_job
def llm_context(jid, pack_num):
    job        = get_job(jid)
    pack_files = job.get("pack_files", [])
    pack_meta  = next((p for p in pack_files if p["pack_num"] == pack_num), None)
    if not pack_meta:
        return jsonify({"error": f"Pack {pack_num} not found."}), 404
    pool = get_live_pool(jid)
    if not pool:
        return jsonify({"error": "Pool is empty"}), 404

    pack_questions = pack_meta.get("questions", [])
    if not pack_questions:
        return jsonify({
            "error": "This pack is missing question mapping. Regenerate packs once, then copy context again."
        }), 400

    pack_ids = {(q["key"], q["q"]) for q in pack_questions}
    order    = {(q["key"], q["q"]): i for i, q in enumerate(pack_questions)}
    pack_qs  = sorted([q for q in pool if (q["key"], q["q"]) in pack_ids],
                      key=lambda q: order.get((q["key"], q["q"]), 999))

    NOISE = {"PMT","Do not write outside the box","Do NOT write on this page",
             "Turn over","BLANK PAGE","Answer ALL","Write your answers"}

    def extract_text(doc, parts):
        lines = []
        for pno, clip in parts:
            try:
                blocks = doc[pno].get_text("blocks", clip=clip)
                for b in sorted(blocks, key=lambda b: (b[1], b[0])):
                    txt = b[4].strip()
                    if txt and not any(n in txt for n in NOISE):
                        lines.append(txt)
            except Exception: pass
        return "\n".join(lines)

    def has_diagram(doc, parts):
        for pno, clip in parts:
            try:
                page = doc[pno]
                if page.get_images(full=True): return True
                if len([p for p in page.get_drawings()
                        if clip.y0 <= p["rect"].y0 <= clip.y1]) > 5: return True
            except Exception: pass
        return False

    diff_labels = {1:"Easy",2:"Low-Medium",3:"Medium",4:"Hard",5:"Very Hard"}
    est = pack_meta.get("est_time", pack_meta["total_marks"])
    md  = [f"""# Edexcel 4MA1 IGCSE Mathematics — Practice Pack {pack_num}
| Field | Value |
|---|---|
| Total marks | {pack_meta['total_marks']} |
| Questions | {pack_meta['question_count']} |
| Time | {est} min |
| Topics | {', '.join(pack_meta['topics'].keys())} |

**Tutor:** Explain step-by-step using Edexcel method. M/A/B marks. Flag common errors.

---
"""]
    for i, q in enumerate(pack_qs, 1):
        qp_text = extract_text(q["qp_doc"], q["qp_parts"])
        ms_text = extract_text(q["ms_doc"], q["ms_parts"])
        diagram = has_diagram(q["qp_doc"], q["qp_parts"])
        md.append(f"""### Q{i} — {q['key']} Q{q['q']} ({q['marks']}m)
{q.get('topic','?')} › {q.get('subtopic','?')} | {diff_labels.get(q.get('difficulty',0),'?')}

**Q:** `{qp_text or "[extraction failed]"}`
{"⚠️ Diagram — see printed paper." if diagram else ""}

**MS:** `{ms_text or "[extraction failed]"}`
---
""")
    markdown = "\n".join(md)
    return jsonify({"markdown": markdown, "char_count": len(markdown)})


# ── Admin dashboard ───────────────────────────────────────────────────────────

@app.route("/admin")
def admin():
    """Simple admin view. Protected by ADMIN_TOKEN env var."""
    token = request.headers.get("X-Admin-Token", "") or request.args.get("token", "")
    if ADMIN_TOKEN and token != ADMIN_TOKEN:
        return jsonify({"error": "Forbidden"}), 403

    from store import REDIS_AVAILABLE, _mem, _doc_cache
    import requests as req2

    # Count users and usage from Supabase if available
    users_count = "N/A"
    total_usage = "N/A"
    SUPABASE_URL     = os.environ.get("SUPABASE_URL", "")
    SUPABASE_SERVICE = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if SUPABASE_URL and SUPABASE_SERVICE:
        try:
            r = req2.get(
                f"{SUPABASE_URL}/rest/v1/packgen_profiles",
                headers={"apikey": SUPABASE_SERVICE,
                         "Authorization": f"Bearer {SUPABASE_SERVICE}",
                         "Prefer": "count=exact"},
                params={"select": "count"},
                timeout=5,
            )
            users_count = r.headers.get("Content-Range", "?").split("/")[-1]
            r2 = req2.get(
                f"{SUPABASE_URL}/rest/v1/packgen_profiles",
                headers={"apikey": SUPABASE_SERVICE,
                         "Authorization": f"Bearer {SUPABASE_SERVICE}"},
                params={"select": "usage_this_month"},
                timeout=5,
            )
            rows = r2.json() if r2.status_code == 200 else []
            total_usage = sum(row.get("usage_this_month", 0) for row in rows)
        except Exception: pass

    active_jobs = sum(1 for jid in list(_doc_cache.keys()))

    return jsonify({
        "redis":       REDIS_AVAILABLE,
        "active_jobs": active_jobs,
        "users":       users_count,
        "total_classifications_this_month": total_usage,
        "ai_backend":  os.environ.get("AI_BACKEND", "mistral"),
        "uptime_pid":  os.getpid(),
    })


# ── Error handlers ────────────────────────────────────────────────────────────
@app.errorhandler(400)
def bad_request(e):  return jsonify({"error": "Bad request"}), 400
@app.errorhandler(404)
def not_found(e):    return jsonify({"error": "Not found"}), 404
@app.errorhandler(413)
def too_large(e):    return jsonify({"error": "File too large (max 600 MB)"}), 413
@app.errorhandler(429)
def rate_limited(e): return jsonify({"error": "Too many requests"}), 429
@app.errorhandler(500)
def server_error(e):
    log.exception("Unhandled: %s", e)
    return jsonify({"error": "Internal server error"}), 500


if __name__ == "__main__":
    app.run(debug=True, port=5050, threaded=True)
