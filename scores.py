"""
scores.py — Mark tracking, spaced repetition, and progress analytics.

Data model:
  Scores are stored per (user_id, question_key, q_num).
  Each attempt records: score, max_marks, topic, subtopic, difficulty, timestamp.
  
  Storage:
    - If Supabase configured: persisted to packgen_scores table
    - Always: in-memory dict for the current session (instant reads)
  
  Spaced repetition:
    Questions are weighted for pack generation based on recent performance.
    Weight formula: base_weight × (1 + weakness_factor)
    weakness_factor = max(0, 1 - avg_score_pct) × difficulty_multiplier
    
    A question you scored 0/5 on recently → weight ~2.0× normal
    A question you scored 5/5 on → weight ~1.0× (no boost)
"""

import os, json, time, hashlib, logging
from collections import defaultdict
import requests as req

log = logging.getLogger("packgen.scores")

SUPABASE_URL     = os.environ.get("SUPABASE_URL", "")
SUPABASE_SERVICE = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")

# In-session score cache: {user_id → {q_key → [attempt, ...]}}
_score_cache: dict[str, dict] = {}

RECENCY_WINDOW = 5   # use last N attempts for weakness calculation


def _sb_headers():
    return {
        "apikey":        SUPABASE_SERVICE,
        "Authorization": f"Bearer {SUPABASE_SERVICE}",
        "Content-Type":  "application/json",
        "Prefer":        "return=minimal",
    }


def _q_key(paper_key: str, q_num: int) -> str:
    return f"{paper_key}|{q_num}"


# ── Write ─────────────────────────────────────────────────────────────────────
def record_score(user_id: str, paper_key: str, q_num: int, score: int,
                 max_marks: int, topic: str, subtopic: str, difficulty: int,
                 pack_num: int | None = None):
    """Record a single question attempt."""
    attempt = {
        "user_id":    user_id,
        "paper_key":  paper_key,
        "q_num":      q_num,
        "score":      score,
        "max_marks":  max_marks,
        "topic":      topic,
        "subtopic":   subtopic,
        "difficulty": difficulty,
        "pack_num":   pack_num,
        "ts":         time.time(),
    }

    # In-memory cache
    qk = _q_key(paper_key, q_num)
    _score_cache.setdefault(user_id, {}).setdefault(qk, []).append(attempt)

    # Persist to Supabase
    if SUPABASE_URL and user_id != "dev":
        try:
            req.post(
                f"{SUPABASE_URL}/rest/v1/packgen_scores",
                headers=_sb_headers(),
                json={
                    "user_id":    user_id,
                    "paper_key":  paper_key,
                    "q_num":      q_num,
                    "score":      score,
                    "max_marks":  max_marks,
                    "topic":      topic,
                    "subtopic":   subtopic,
                    "difficulty": difficulty,
                    "pack_num":   pack_num,
                },
                timeout=8,
            )
        except Exception as e:
            log.warning("Score persist error: %s", e)


def record_pack_attempt(user_id: str, jid: str, pack_num: int,
                        scores: list[dict]):
    """
    Record an entire pack attempt.
    scores = [{"key": "...", "q": 18, "score": 3, "marks": 5,
               "topic": "...", "subtopic": "...", "difficulty": 3}, ...]
    """
    for s in scores:
        record_score(
            user_id=user_id,
            paper_key=s["key"],
            q_num=s["q"],
            score=s["score"],
            max_marks=s["marks"],
            topic=s.get("topic", "Unknown"),
            subtopic=s.get("subtopic", "Unknown"),
            difficulty=s.get("difficulty", 3),
            pack_num=pack_num,
        )

    # Also persist a pack-level attempt record
    if SUPABASE_URL and user_id != "dev":
        total_scored = sum(s["score"] for s in scores)
        total_marks  = sum(s["marks"] for s in scores)
        try:
            req.post(
                f"{SUPABASE_URL}/rest/v1/packgen_attempts",
                headers=_sb_headers(),
                json={
                    "user_id":     user_id,
                    "jid":         jid,
                    "pack_num":    pack_num,
                    "total_score": total_scored,
                    "total_marks": total_marks,
                    "pct":         round(total_scored / total_marks * 100) if total_marks else 0,
                    "q_count":     len(scores),
                },
                timeout=8,
            )
        except Exception as e:
            log.warning("Attempt persist error: %s", e)


# ── Read ──────────────────────────────────────────────────────────────────────
def get_user_scores(user_id: str) -> dict:
    """
    Return all score data for a user.
    Tries memory cache first, then Supabase.
    Returns: {q_key: [attempts...]}
    """
    if user_id in _score_cache:
        return _score_cache[user_id]

    if SUPABASE_URL and user_id != "dev":
        try:
            r = req.get(
                f"{SUPABASE_URL}/rest/v1/packgen_scores",
                headers={**_sb_headers(), "Prefer": ""},
                params={
                    "user_id": f"eq.{user_id}",
                    "select":  "paper_key,q_num,score,max_marks,topic,subtopic,difficulty,pack_num,created_at",
                    "order":   "created_at.asc",
                    "limit":   "5000",
                },
                timeout=10,
            )
            rows = r.json() if r.status_code == 200 else []
            cache = {}
            for row in rows:
                qk = _q_key(row["paper_key"], row["q_num"])
                attempt = {
                    "user_id":    user_id,
                    "paper_key":  row["paper_key"],
                    "q_num":      row["q_num"],
                    "score":      row["score"],
                    "max_marks":  row["max_marks"],
                    "topic":      row["topic"],
                    "subtopic":   row["subtopic"],
                    "difficulty": row["difficulty"],
                    "pack_num":   row.get("pack_num"),
                    "ts":         row.get("created_at", 0),
                }
                cache.setdefault(qk, []).append(attempt)
            _score_cache[user_id] = cache
            return cache
        except Exception as e:
            log.error("Score fetch error: %s", e)

    return {}


def get_pack_attempts(user_id: str) -> list:
    """Return list of past pack attempts for the history view."""
    if not SUPABASE_URL or user_id == "dev":
        return []
    try:
        r = req.get(
            f"{SUPABASE_URL}/rest/v1/packgen_attempts",
            headers={**_sb_headers(), "Prefer": ""},
            params={
                "user_id": f"eq.{user_id}",
                "select":  "*",
                "order":   "created_at.desc",
                "limit":   "50",
            },
            timeout=10,
        )
        return r.json() if r.status_code == 200 else []
    except Exception as e:
        log.error("Attempt fetch error: %s", e)
        return []


# ── Analytics ─────────────────────────────────────────────────────────────────
def topic_progress(user_id: str) -> dict:
    """
    Returns per-topic performance breakdown.
    {
      "Algebra": {
        "attempted": 12,
        "avg_pct": 72.5,
        "subtopics": {"Sequences": {"attempted": 4, "avg_pct": 65.0}, ...}
      }, ...
    }
    """
    scores = get_user_scores(user_id)
    topics: dict[str, dict] = {}

    for qk, attempts in scores.items():
        for a in attempts[-RECENCY_WINDOW:]:
            t   = a.get("topic", "Unknown")
            sub = a.get("subtopic", "Unknown")
            pct = (a["score"] / a["max_marks"] * 100) if a["max_marks"] else 0

            if t not in topics:
                topics[t] = {"attempted": 0, "total_pct": 0.0, "subtopics": {}}
            topics[t]["attempted"] += 1
            topics[t]["total_pct"] += pct

            if sub not in topics[t]["subtopics"]:
                topics[t]["subtopics"][sub] = {"attempted": 0, "total_pct": 0.0}
            topics[t]["subtopics"][sub]["attempted"] += 1
            topics[t]["subtopics"][sub]["total_pct"] += pct

    result = {}
    for t, data in topics.items():
        n = data["attempted"]
        result[t] = {
            "attempted": n,
            "avg_pct":   round(data["total_pct"] / n, 1) if n else 0,
            "subtopics": {
                sub: {
                    "attempted": sd["attempted"],
                    "avg_pct":   round(sd["total_pct"] / sd["attempted"], 1),
                }
                for sub, sd in data["subtopics"].items()
            },
        }
    return result


def paper_stats(user_id: str, pool: list) -> list:
    """
    Returns per-source-paper performance.
    [{key, total_questions, attempted, avg_pct, year}, ...]
    """
    scores = get_user_scores(user_id)

    # Group pool by paper key
    paper_questions: dict[str, list] = defaultdict(list)
    for q in pool:
        paper_questions[q["key"]].append(q)

    results = []
    for key, questions in sorted(paper_questions.items()):
        attempted = 0
        total_pct = 0.0
        for q in questions:
            qk = _q_key(q["key"], q["q"])
            if qk in scores and scores[qk]:
                last = scores[qk][-1]
                pct  = (last["score"] / last["max_marks"] * 100) if last["max_marks"] else 0
                total_pct += pct
                attempted += 1
        results.append({
            "key":              key,
            "total_questions":  len(questions),
            "attempted":        attempted,
            "avg_pct":          round(total_pct / attempted, 1) if attempted else None,
        })
    return results


def weakness_weights(user_id: str, pool: list) -> dict[str, float]:
    """
    Returns {q_key: weight} for spaced-repetition pack generation.
    Questions with poor recent scores get higher weights (more likely to appear).
    Never-attempted questions get weight 1.5 (slight boost to ensure coverage).
    """
    scores = get_user_scores(user_id)
    weights = {}

    for q in pool:
        qk = _q_key(q["key"], q["q"])
        attempts = scores.get(qk, [])

        if not attempts:
            # Never attempted — mild boost
            weights[qk] = 1.5
            continue

        recent = attempts[-RECENCY_WINDOW:]
        avg_pct = sum(
            (a["score"] / a["max_marks"] * 100) if a["max_marks"] else 0
            for a in recent
        ) / len(recent)

        diff = q.get("difficulty", 3)
        # weakness_factor: 0 at 100%, 1.0 at 0%, scaled by difficulty
        weakness = max(0.0, (1 - avg_pct / 100)) * (0.5 + diff * 0.1)
        weights[qk] = 1.0 + weakness

    return weights


def performance_heatmap(user_id: str) -> dict:
    """
    Returns {topic: {difficulty: avg_pct}} for the performance heatmap.
    Differs from the regular heatmap (which shows question counts).
    """
    scores = get_user_scores(user_id)
    buckets: dict[str, dict[int, list]] = {}

    for qk, attempts in scores.items():
        for a in attempts[-3:]:   # last 3 attempts per question
            t    = a.get("topic", "Unknown")
            d    = a.get("difficulty", 3)
            pct  = (a["score"] / a["max_marks"] * 100) if a["max_marks"] else 0
            buckets.setdefault(t, {}).setdefault(d, []).append(pct)

    result = {}
    for t, diffs in buckets.items():
        result[t] = {
            d: round(sum(vals) / len(vals), 1)
            for d, vals in diffs.items()
        }
    return result
