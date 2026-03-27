"""
classifier.py — Ultra token-efficient question classifier.

Supports two backends, selected by environment variable:
  AI_BACKEND=mistral     →  Mistral API    (default: mistral-small-latest)
  AI_BACKEND=openrouter  →  OpenRouter API (any model, default: openrouter/auto)
  AI_BACKEND=anthropic   →  Anthropic API  (default: claude-haiku-4-5-20251001)

OpenRouter is cheaper for text-only classification. Anthropic is used as
fallback if OPENROUTER_API_KEY is not set.

Token efficiency techniques:
  - Minimal topic list (abbrev. form, ~40% fewer tokens than full list)
  - Batch 16 questions per call (fewer HTTP round trips)
  - Prefix tokens suppressed: system prompt tells model to start with "["
  - max_tokens capped tightly per batch (n × 28 + 20)
  - Vision only when text length < 60 chars (avoids spurious image calls)
  - In-memory + Redis cache: classified questions never re-sent
"""

import re, json, base64, time, os, hashlib, logging
import requests
from packer import SPEC_TOPICS

log = logging.getLogger("packgen.classifier")

# ── Backend selection ─────────────────────────────────────────────────────────
AI_BACKEND         = os.environ.get("AI_BACKEND", "mistral").lower()
MISTRAL_API_KEY    = os.environ.get("MISTRAL_API_KEY", "")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")

# Mistral
MI_TEXT_MODEL   = os.environ.get("MI_TEXT_MODEL",   "mistral-small-latest")
MI_VISION_MODEL = os.environ.get("MI_VISION_MODEL", "mistral-small-latest")
MI_URL          = "https://api.mistral.ai/v1/chat/completions"

# OpenRouter model defaults
OR_TEXT_MODEL   = os.environ.get("OR_TEXT_MODEL",   "openrouter/auto")
OR_VISION_MODEL = os.environ.get("OR_VISION_MODEL", "openrouter/auto")
OR_CHAT_URL       = "https://openrouter.ai/api/v1/chat/completions"
OR_RESPONSES_URL  = "https://openrouter.ai/api/v1/responses"

# Anthropic
AN_TEXT_MODEL   = os.environ.get("AN_TEXT_MODEL",   "claude-haiku-4-5-20251001")
AN_URL          = "https://api.anthropic.com/v1/messages"

BATCH_SIZE = 16   # questions per API call

# ── In-memory cache: {md5_hash → {topic, subtopic, difficulty}} ───────────────
_CACHE: dict[str, dict] = {}

NOISE = {"PMT","Turn over","BLANK PAGE","Do not write outside the box",
         "Do NOT write on this page","Answer ALL","Write your answers",
         "You must write down","Calculators may be used"}

# ── Ultra-compact topic list (saves ~40 tokens vs full list) ──────────────────
# Format: "TopicCode: subtopic1, subtopic2 ..."
_TOPICS_COMPACT = (
    "Num: integers,decimals,fractions,%,ratio,std-form,bounds,surds,indices,HCF,LCM | "
    "Alg: expand,factorise,linear-eq,quadratic,simultaneous,inequality,sequence,nth-term,"
         "function,graph,completing-sq,discriminant,proof,iteration,alg-fraction | "
    "Geo: angle,polygon,circle-thm,trig,sine-rule,cosine-rule,area,perimeter,volume,"
         "surface-area,transform,vector,locus,coord,Pythagoras,bearing,similar,congruent,"
         "arc,sector,frustum,3D | "
    "Stat: mean,median,mode,range,frequency,histogram,cum-freq,box-plot,scatter,correlation | "
    "Prob: probability,tree-diagram,Venn,conditional,relative-freq | "
    "Calc: differentiate,gradient,tangent,turning-point,dy-dx"
)

# Full topic names for output validation
_VALID_TOPICS = set(SPEC_TOPICS.keys())


def _q_hash(q: dict) -> str:
    return hashlib.md5(f"{q['key']}|{q['q']}|{q['marks']}".encode()).hexdigest()


def _extract_text(item) -> str:
    """Extract cleaned question text, capped at 400 chars."""
    parts = []
    for pno, clip in item.get("qp_parts", []):
        try:
            page = item["qp_doc"][pno]
            blocks = page.get_text("blocks", clip=clip)
            for b in sorted(blocks, key=lambda b: (b[1], b[0])):
                txt = b[4].strip()
                if not txt or any(n in txt for n in NOISE):
                    continue
                if len(txt) < 3 and not any(c.isdigit() for c in txt):
                    continue
                parts.append(txt)
        except Exception:
            pass
    raw = " | ".join(parts)
    raw = re.sub(r"^\d{1,2}\s+", "", raw)
    return raw[:400]


def _render_b64(item, dpi=88) -> str | None:
    try:
        from extractor import render_question_png
        png = render_question_png(item, dpi=dpi)
        return base64.standard_b64encode(png).decode()
    except Exception:
        return None


def _parse_json_array(raw: str) -> list:
    if raw is None:
        return []
    if not isinstance(raw, str):
        raw = str(raw)
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
    m = re.search(r'\[.*\]', raw, re.DOTALL)
    if m:
        try:
            return json.loads(m.group())
        except Exception:
            pass
    try:
        return json.loads(raw)
    except Exception:
        return []


def _parse_json_obj(raw: str) -> dict:
    if raw is None:
        return {}
    if not isinstance(raw, str):
        raw = str(raw)
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
    try:
        return json.loads(raw)
    except Exception:
        return {}


# ── Backend calls ─────────────────────────────────────────────────────────────
def _call_openrouter(messages: list, model: str, max_tokens: int,
                     api_key: str) -> str:
    key = api_key.strip() or OPENROUTER_API_KEY
    if not key:
        raise ValueError("No OPENROUTER_API_KEY set")
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type":  "application/json",
        "HTTP-Referer":  "https://packgen.app",
        "X-Title":       "PackGen",
    }
    chat_payload = {
        "model":      model,
        "messages":   messages,
        "max_tokens": max_tokens,
        "temperature": 0,
    }

    def _extract_chat_text(data: dict) -> str | None:
        msg = ((data.get("choices") or [{}])[0]).get("message", {})
        content = msg.get("content")
        if isinstance(content, str) and content.strip():
            return content
        if isinstance(content, dict) and isinstance(content.get("text"), str):
            txt = content.get("text", "")
            if txt.strip():
                return txt
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    t = item["text"]
                    if t.strip():
                        parts.append(t)
            if parts:
                return "\n".join(parts)
        alt = ((data.get("choices") or [{}])[0]).get("text")
        if isinstance(alt, str) and alt.strip():
            return alt
        return None

    def _call_responses() -> str:
        responses_payload = {
            "model": model,
            "input": messages,
            "max_output_tokens": max_tokens,
            "temperature": 0,
        }
        resp2 = requests.post(
            OR_RESPONSES_URL, headers=headers, json=responses_payload, timeout=90
        )
        if resp2.status_code < 400:
            data = resp2.json()
            if isinstance(data.get("output_text"), str) and data["output_text"].strip():
                return data["output_text"]
            for out in data.get("output", []) or []:
                for c in out.get("content", []) or []:
                    txt = c.get("text")
                    if isinstance(txt, str) and txt.strip():
                        return txt
            raise RuntimeError("OpenRouter responses call succeeded but returned no text content.")
        body2 = (resp2.text or "").strip()
        if len(body2) > 500:
            body2 = body2[:500] + "..."
        raise RuntimeError(
            f"OpenRouter error {resp2.status_code} at {OR_RESPONSES_URL}. "
            f"Model={model}. Response={body2 or '<empty>'}"
        )

    resp = requests.post(OR_CHAT_URL, headers=headers, json=chat_payload, timeout=90)
    if resp.status_code < 400:
        data = resp.json()
        txt = _extract_chat_text(data)
        if txt is not None:
            return txt
        # Some providers return 200 with non-text chat payloads; try responses API.
        return _call_responses()

    # Fallback for providers/accounts that only expose the Responses API route.
    if resp.status_code == 404:
        return _call_responses()

    body = (resp.text or "").strip()
    if len(body) > 500:
        body = body[:500] + "..."
    raise RuntimeError(
        f"OpenRouter error {resp.status_code} at {OR_CHAT_URL}. "
        f"Model={model}. Response={body or '<empty>'}"
    )


def _call_anthropic(messages: list, system: str, max_tokens: int,
                    api_key: str) -> str:
    key = api_key.strip() or ANTHROPIC_API_KEY
    if not key:
        raise ValueError("No ANTHROPIC_API_KEY set")
    payload = {"model": AN_TEXT_MODEL, "max_tokens": max_tokens, "messages": messages}
    if system:
        payload["system"] = system
    resp = requests.post(AN_URL, headers={
        "x-api-key":         key,
        "anthropic-version": "2023-06-01",
        "content-type":      "application/json",
    }, json=payload, timeout=90)
    resp.raise_for_status()
    return resp.json()["content"][0]["text"]


def _call_mistral(messages: list, model: str, max_tokens: int, api_key: str) -> str:
    key = api_key.strip() or MISTRAL_API_KEY
    if not key:
        raise ValueError("No MISTRAL_API_KEY set")

    # Mistral chat expects text content; convert multimodal-style arrays to text.
    norm_messages = []
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content", "")
        if isinstance(content, list):
            txt_parts = []
            for item in content:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    if item["text"].strip():
                        txt_parts.append(item["text"])
            content = "\n".join(txt_parts).strip()
        elif content is None:
            content = ""
        elif not isinstance(content, str):
            content = str(content)
        norm_messages.append({"role": role, "content": content})

    payload = {
        "model": model,
        "messages": norm_messages,
        "temperature": 0,
        "max_tokens": max_tokens,
    }
    resp = requests.post(
        MI_URL,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=90,
    )
    if resp.status_code >= 400:
        body = (resp.text or "").strip()
        if len(body) > 500:
            body = body[:500] + "..."
        raise RuntimeError(
            f"Mistral error {resp.status_code} at {MI_URL}. "
            f"Model={model}. Response={body or '<empty>'}"
        )

    data = resp.json()
    msg = ((data.get("choices") or [{}])[0]).get("message", {})
    content = msg.get("content")
    if isinstance(content, str) and content.strip():
        return content
    if isinstance(content, dict) and isinstance(content.get("text"), str):
        txt = content.get("text", "")
        if txt.strip():
            return txt
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                t = item["text"]
                if t.strip():
                    parts.append(t)
        if parts:
            return "\n".join(parts)
    raise RuntimeError("Mistral call returned no text content.")


def _llm_call(messages: list, system: str, max_tokens: int,
              api_key: str, vision: bool = False) -> str:
    """Route to the configured backend."""
    backend = AI_BACKEND
    # Auto-fall-through: if configured backend key is missing, try other backends.
    has_req_key = bool(api_key.strip())
    if backend == "mistral" and not (has_req_key or MISTRAL_API_KEY):
        backend = "openrouter" if OPENROUTER_API_KEY else "anthropic"
    elif backend == "openrouter" and not (has_req_key or OPENROUTER_API_KEY):
        backend = "mistral" if MISTRAL_API_KEY else "anthropic"
    elif backend == "anthropic" and not (has_req_key or ANTHROPIC_API_KEY):
        backend = "mistral" if MISTRAL_API_KEY else "openrouter"

    if backend == "mistral":
        model = MI_VISION_MODEL if vision else MI_TEXT_MODEL
        full_messages = ([{"role": "system", "content": system}] if system else []) + messages
        return _call_mistral(full_messages, model, max_tokens, api_key)

    if backend == "openrouter":
        model = OR_VISION_MODEL if vision else OR_TEXT_MODEL
        # OpenRouter uses OpenAI message format — system goes as a system message
        full_messages = ([{"role": "system", "content": system}] if system else []) + messages
        return _call_openrouter(full_messages, model, max_tokens, api_key)
    else:
        return _call_anthropic(messages, system, max_tokens, api_key)


# ── Classification prompts (ultra-compact) ────────────────────────────────────
_SYSTEM_BATCH = (
    "Edexcel 4MA1 IGCSE Maths classifier. "
    "Reply ONLY with a compact JSON array. No markdown. No explanation. "
    "Start your response with '['."
)

_SYSTEM_SINGLE = (
    "Edexcel 4MA1 IGCSE Maths classifier. "
    "Reply ONLY with a compact JSON object. No markdown."
)

def _batch_prompt(batch: list) -> str:
    lines = [f"{i+1}[{q['marks']}m]:{_extract_text(q)}" for i, q in enumerate(batch)]
    return (
        f"Topics:{_TOPICS_COMPACT}\n"
        f"Classify {len(batch)} Qs. "
        f'Output exactly {len(batch)} objects:[{{"t":"Topic","s":"subtopic","d":1-5}},...]\n'
        "t=exact topic name (Num/Alg/Geo/Stat/Prob/Calc→full name). "
        "s=specific subtopic. d=1easy…5hard.\n"
        + "\n".join(lines)
    )

def _single_vision_prompt(q: dict, txt: str) -> str:
    return (
        f"Topics:{_TOPICS_COMPACT}\n"
        f"Q{q['q']}({q['marks']}m)"
        + (f" text:{txt}" if txt else "")
        + '\nReturn:{{"t":"Topic","s":"subtopic","d":1-5}}'
    )


def _expand_topic(t: str) -> str:
    """Map short codes and freeform to canonical topic names."""
    if t is None:
        return "Unknown"
    if not isinstance(t, str):
        t = str(t)

    mapping = {
        "num": "Number", "number": "Number",
        "alg": "Algebra", "algebra": "Algebra",
        "geo": "Geometry", "geometry": "Geometry",
        "stat": "Statistics", "statistics": "Statistics",
        "prob": "Probability", "probability": "Probability",
        "calc": "Calculus", "calculus": "Calculus",
    }
    t_clean = re.sub(r"[\n\r\t]+", " ", t).strip()
    if t_clean in _VALID_TOPICS:
        return t_clean

    # Handle outputs like "Num: Fractions" or "Algebra - equations"
    t_prefix = re.split(r"[:\-|/]", t_clean, maxsplit=1)[0].strip().lower()
    if t_prefix in mapping:
        return mapping[t_prefix]

    t_lower = t_clean.lower()
    if t_lower in mapping:
        return mapping[t_lower]

    # Keyword fallbacks for freer model outputs
    keyword_map = {
        "number": "Number",
        "fraction": "Number",
        "decimal": "Number",
        "percentage": "Number",
        "ratio": "Number",
        "indices": "Number",
        "surd": "Number",
        "algebra": "Algebra",
        "equation": "Algebra",
        "inequal": "Algebra",
        "sequence": "Algebra",
        "function": "Algebra",
        "quadratic": "Algebra",
        "geometry": "Geometry",
        "angle": "Geometry",
        "circle": "Geometry",
        "trig": "Geometry",
        "vector": "Geometry",
        "coordinate": "Geometry",
        "volume": "Geometry",
        "statistics": "Statistics",
        "histogram": "Statistics",
        "box plot": "Statistics",
        "scatter": "Statistics",
        "frequency": "Statistics",
        "probability": "Probability",
        "venn": "Probability",
        "tree diagram": "Probability",
        "combined events": "Probability",
        "calculus": "Calculus",
        "differentiat": "Calculus",
        "tangent": "Calculus",
        "turning point": "Calculus",
    }
    for kw, topic in keyword_map.items():
        if kw in t_lower:
            return topic

    return "Unknown"


def _apply(questions: list, results: list):
    for i, q in enumerate(questions):
        r = results[i] if i < len(results) else {}
        if isinstance(r, dict):
            raw_t = r.get("t") or r.get("topic") or "Unknown"
            q["topic"]      = _expand_topic(raw_t)
            q["subtopic"]   = r.get("s") or r.get("subtopic") or "Unknown"
            q["difficulty"] = _safe_int(r.get("d") or r.get("difficulty"), 3)
        else:
            q["topic"] = q["subtopic"] = "Unknown"
            q["difficulty"] = 3


def _has_usable_labels(batch: list) -> bool:
    return any((q.get("topic") or "Unknown") != "Unknown" for q in batch)


def _coerce_result_from_raw(raw: str) -> dict:
    """
    Best-effort extraction when model doesn't return strict JSON.
    """
    if raw is None:
        return {}
    if not isinstance(raw, str):
        raw = str(raw)
    txt = raw.strip()
    if not txt:
        return {}

    topic = _expand_topic(txt)

    # Try extracting a likely difficulty marker (e.g. "difficulty: 3")
    d = 3
    m = re.search(r"(?:difficulty|diff|level)\D*([1-5])", txt, re.I)
    if not m:
        m = re.search(r"\b([1-5])\b", txt)
    if m:
        d = _safe_int(m.group(1), 3)

    # Lightweight subtopic extraction
    first = txt.splitlines()[0][:120]
    first = re.sub(r"[`*#>\[\]\{\}]", "", first).strip()
    if len(first) < 3:
        first = "Unknown"

    return {"t": topic, "s": first, "d": d}


def _classify_batch_single_fallback(batch: list, api_key: str):
    """
    Retry classification one-by-one with strict object output when a batch
    response is unusable.
    """
    for q in batch:
        try:
            txt = _extract_text(q)
            raw = _llm_call(
                [{"role": "user", "content": _single_vision_prompt(q, txt)}],
                system=_SYSTEM_SINGLE,
                max_tokens=60,
                api_key=api_key,
            )
            r = _parse_json_obj(raw)
            if not r:
                arr = _parse_json_array(raw)
                if arr and isinstance(arr[0], dict):
                    r = arr[0]
            if not r:
                r = _coerce_result_from_raw(raw)
            if isinstance(r, dict) and (r.get("t") or r.get("topic")):
                raw_t = r.get("t") or r.get("topic") or "Unknown"
                q["topic"] = _expand_topic(raw_t)
                q["subtopic"] = r.get("s") or r.get("subtopic") or "Unknown"
                q["difficulty"] = _safe_int(r.get("d") or r.get("difficulty"), 3)
            else:
                q.setdefault("topic", "Unknown")
                q.setdefault("subtopic", "Unknown")
                q.setdefault("difficulty", 3)
        except Exception:
            q.setdefault("topic", "Unknown")
            q.setdefault("subtopic", "Unknown")
            q.setdefault("difficulty", 3)


# ── Public API ────────────────────────────────────────────────────────────────
def classify_batch(questions: list, api_key: str = "", spec_text: str = "",
                   progress_cb=None) -> list:
    """
    Classify all questions. Cache-hits are free.
    Returns same list with topic/subtopic/difficulty populated.
    """
    total = len(questions)
    classified = 0
    api_failures = 0
    api_success = 0
    first_api_error = ""

    # Cache pass
    to_classify = []
    for q in questions:
        h = _q_hash(q)
        if h in _CACHE:
            q.update(_CACHE[h])
            classified += 1
        else:
            to_classify.append(q)

    if progress_cb and classified:
        progress_cb(classified, total, f"Loaded {classified} cached…")

    text_qs   = [q for q in to_classify if q.get("q", 0) <= 21]
    vision_qs = [q for q in to_classify if q.get("q", 0) > 21]

    # ── Text batches ──────────────────────────────────────────────────────────
    for i in range(0, len(text_qs), BATCH_SIZE):
        batch = text_qs[i : i + BATCH_SIZE]
        try:
            raw = _llm_call(
                [{"role": "user", "content": _batch_prompt(batch)}],
                system=_SYSTEM_BATCH,
                max_tokens=batch_max_tokens(batch),
                api_key=api_key,
            )
            _apply(batch, _parse_json_array(raw))
            if not _has_usable_labels(batch):
                _classify_batch_single_fallback(batch, api_key)
            _cache_batch(batch)
            if _has_usable_labels(batch):
                api_success += 1
            else:
                api_failures += 1
                if not first_api_error:
                    first_api_error = "Batch parse produced no usable labels."
        except Exception as e:
            log.error("Text batch classify error: %s", e)
            _default_batch(batch)
            api_failures += 1
            if not first_api_error:
                first_api_error = str(e)
        classified += len(batch)
        if progress_cb:
            progress_cb(classified, total, f"Classified {classified}/{total}…")
        time.sleep(0.15)

    # ── Vision-capable (Q22+) ─────────────────────────────────────────────────
    for i in range(0, len(vision_qs), BATCH_SIZE):
        batch = vision_qs[i : i + BATCH_SIZE]
        text_ok   = [q for q in batch if len(_extract_text(q)) >= 60]
        text_poor = [q for q in batch if len(_extract_text(q)) <  60]

        # Text-rich Q22+ — still use batch
        if text_ok:
            try:
                raw = _llm_call(
                    [{"role": "user", "content": _batch_prompt(text_ok)}],
                    system=_SYSTEM_BATCH,
                    max_tokens=batch_max_tokens(text_ok),
                    api_key=api_key,
                )
                _apply(text_ok, _parse_json_array(raw))
                if not _has_usable_labels(text_ok):
                    _classify_batch_single_fallback(text_ok, api_key)
                _cache_batch(text_ok)
                if _has_usable_labels(text_ok):
                    api_success += 1
                else:
                    api_failures += 1
                    if not first_api_error:
                        first_api_error = "Vision-text batch parse produced no usable labels."
            except Exception as e:
                log.error("Vision-text batch error: %s", e)
                _default_batch(text_ok)
                api_failures += 1
                if not first_api_error:
                    first_api_error = str(e)
            time.sleep(0.2)

        # Diagram-heavy — vision single call
        for q in text_poor:
            try:
                txt = _extract_text(q)
                b64 = _render_b64(q)
                if b64:
                    content = [
                        {"type": "image_url",
                         "image_url": {"url": f"data:image/png;base64,{b64}"}},
                        {"type": "text",
                         "text": _single_vision_prompt(q, txt)},
                    ]
                    raw = _llm_call(
                        [{"role": "user", "content": content}],
                        system=_SYSTEM_SINGLE,
                        max_tokens=60,
                        api_key=api_key,
                        vision=True,
                    )
                    r = _parse_json_obj(raw)
                else:
                    # No image — fall back to text-only
                    raw = _llm_call(
                        [{"role": "user", "content": _batch_prompt([q])}],
                        system=_SYSTEM_BATCH,
                        max_tokens=40,
                        api_key=api_key,
                    )
                    results = _parse_json_array(raw)
                    r = results[0] if results else {}
                raw_t = r.get("t") or r.get("topic") or "Unknown"
                q["topic"]      = _expand_topic(raw_t)
                q["subtopic"]   = r.get("s") or r.get("subtopic") or "Unknown"
                q["difficulty"] = _safe_int(r.get("d") or r.get("difficulty"), 3)
                _CACHE[_q_hash(q)] = {"topic": q["topic"], "subtopic": q["subtopic"],
                                       "difficulty": q["difficulty"]}
                api_success += 1
            except Exception as e:
                log.error("Vision single error Q%s: %s", q.get("q"), e)
                q.setdefault("topic", "Unknown")
                q.setdefault("subtopic", "Unknown")
                q.setdefault("difficulty", 3)
                api_failures += 1
                if not first_api_error:
                    first_api_error = str(e)
            time.sleep(0.8)

        classified += len(batch)
        if progress_cb:
            progress_cb(classified, total, f"Classified {classified}/{total}…")

    if to_classify and api_success == 0 and api_failures > 0:
        msg = "All classification API calls failed. Check API key, backend settings, and provider availability."
        if first_api_error:
            msg += f" First error: {first_api_error}"
        raise RuntimeError(msg)

    return questions


# ── Helpers ───────────────────────────────────────────────────────────────────
def batch_max_tokens(batch: list) -> int:
    """Tight upper bound: 28 tokens per result object + small overhead."""
    return len(batch) * 28 + 20


def _cache_batch(batch: list):
    for q in batch:
        _CACHE[_q_hash(q)] = {
            "topic": q.get("topic", "Unknown"),
            "subtopic": q.get("subtopic", "Unknown"),
            "difficulty": q.get("difficulty", 3),
        }


def _default_batch(batch: list):
    for q in batch:
        q.setdefault("topic", "Unknown")
        q.setdefault("subtopic", "Unknown")
        q.setdefault("difficulty", 3)


def _safe_int(val, default):
    try:
        return max(1, min(5, int(val)))
    except Exception:
        return default
