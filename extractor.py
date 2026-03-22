"""
4MA1 Edexcel IGCSE Maths — Question & Mark Scheme Extractor
Improvements over v1:
- Table-aware MS extraction: finds left-column boundary, extracts row-by-row
- Font-size cross-validation for question number detection
- Both start AND end required (no silent drops)
- Concurrent extraction via ThreadPoolExecutor
- Session save/load with path references
- Warnings returned for UI surfacing
"""
import re, json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
import fitz

MIN_Q      = 18
MAX_Q      = 60
LEFT_X_QP  = 115
LEFT_X_MS  = 175   # wider to catch table left column
BOLD_FLAG  = 16
PAD_TOP    = 10
PAD_BOTTOM = 52
GAP        = 14
WORKERS    = 4


START_RE = re.compile(r"^(\d{1,2})(?=[\s.\t]|$)")
END_RE   = re.compile(
    r"^\(Total\s+for\s+(?:Question\s+)?(\d{1,2})\s+(?:is\s+)?(\d{1,2})\s+marks?\)",
    re.I,
)
MS_TOTAL_RE = re.compile(r"^Total\s+(\d{1,2})\s+marks?$", re.I)
# Also match sub-question formats: "18" "18(a)" "18 (a)"
MS_Q_RE = re.compile(r"^(\d{1,2})(?:\s*\([a-z]\))?(?:[\s.\t]|$)")


# ── Key normalisation ─────────────────────────────────────────────────────────
def normalise_key(stem: str) -> str:
    s = stem.strip()
    s = re.sub(r"[\s_]*(QP|MS)\s*$", "", s, flags=re.I).strip()
    s = s.replace(" ", "_").replace("-", "_")
    def expand_year(m):
        start = m.start()
        if start >= 2 and s[start-2:start].isdigit():
            return m.group()
        y = int(m.group())
        return str(2000 + y) if 0 <= y <= 30 else m.group()
    s = re.sub(r"(?<!\d)(\d{2})(?!\d)", expand_year, s)
    s = re.sub(r"\(r\)", "_r", s, flags=re.I)
    s = re.sub(r"_+", "_", s).strip("_").lower()
    return s


def find_pairs(folder: Path):
    folder = Path(folder)
    qp_map, ms_map = {}, {}
    qp_re = re.compile(r"^(.*?)[\s_]*QP\s*$", re.I)
    ms_re = re.compile(r"^(.*?)[\s_]*MS\s*$", re.I)
    for f in sorted(folder.glob("*.pdf")):
        stem = f.stem.strip()
        m = qp_re.match(stem)
        if m:
            qp_map[normalise_key(stem)] = (m.group(1).strip(), f)
            continue
        m = ms_re.match(stem)
        if m:
            ms_map[normalise_key(stem)] = (m.group(1).strip(), f)
    pairs, warnings = [], []
    for key in sorted(set(qp_map) & set(ms_map)):
        pairs.append((qp_map[key][0], qp_map[key][1], ms_map[key][1]))
    for k in set(qp_map) - set(ms_map):
        warnings.append(f"No MS for: {qp_map[k][1].name}")
    for k in set(ms_map) - set(qp_map):
        warnings.append(f"No QP for: {ms_map[k][1].name}")
    return pairs, warnings


# ── Text extraction helpers ───────────────────────────────────────────────────
def page_lines(page, include_flags=False):
    out = []
    for block in page.get_text("dict").get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            text, x0, y0, x1, y1, flags, fsize = "", 1e9, 1e9, -1e9, -1e9, 0, 0
            for span in line.get("spans", []):
                t = span.get("text", "")
                if t:
                    text += t
                    flags |= span.get("flags", 0)
                    fsize = max(fsize, span.get("size", 0))
                b = span.get("bbox")
                if b:
                    x0=min(x0,b[0]); y0=min(y0,b[1])
                    x1=max(x1,b[2]); y1=max(y1,b[3])
            text = text.strip()
            if text and x0 < 1e9:
                if include_flags:
                    out.append((text, x0, y0, x1, y1, flags, fsize))
                else:
                    out.append((text, x0, y0, x1, y1))
    out.sort(key=lambda r: (round(r[2], 1), r[1]))
    return out


def is_bold(flags):
    return bool(flags & BOLD_FLAG)


def _body_font_size(page) -> float:
    sizes = []
    for block in page.get_text("dict").get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                s = span.get("size", 0)
                if s > 0:
                    sizes.append(round(s, 1))
    if not sizes:
        return 11.0
    return max(set(sizes), key=sizes.count)


def next_q_y(lines_flagged, y_ref, x_thresh):
    for row in lines_flagged:
        txt, x0, y0 = row[0], row[1], row[2]
        flags = row[5]
        if y0 <= y_ref + 2:
            continue
        if x0 <= x_thresh:
            m = START_RE.match(txt)
            if m and is_bold(flags) and MIN_Q <= int(m.group(1)) <= MAX_Q:
                return y0
    return None


def make_clip(doc, pno, y0, y1, cap_next=True, x_thresh=LEFT_X_QP):
    page = doc[pno]
    h = page.rect.height
    y0p = max(0, y0 - PAD_TOP)
    y1p = min(h, y1 + PAD_BOTTOM)
    if cap_next:
        nxt = next_q_y(page_lines(page, include_flags=True), y1, x_thresh)
        if nxt:
            y1p = min(y1p, max(0, nxt - GAP))
    return fitz.Rect(0, y0p, page.rect.width, y1p)


# ── QP extraction ─────────────────────────────────────────────────────────────
def extract_qp(qp_doc):
    starts, ends = {}, {}

    for pno in range(qp_doc.page_count):
        page = qp_doc[pno]
        body_size = _body_font_size(page)
        lines = page_lines(page, include_flags=True)

        for (txt, x0, y0, x1, y1, flags, fsize) in lines:
            if x0 <= LEFT_X_QP:
                m = START_RE.match(txt)
                if m and is_bold(flags):
                    q = int(m.group(1))
                    if MIN_Q <= q <= MAX_Q and q not in starts:
                        if fsize >= body_size * 0.85:
                            starts[q] = (pno, y0)

            m2 = END_RE.match(txt)
            if m2:
                q, marks = int(m2.group(1)), int(m2.group(2))
                if MIN_Q <= q <= MAX_Q and q not in ends:
                    ends[q] = (pno, y1, marks)

    chunks, marks_map, skipped = {}, {}, []

    for q, (sp, sy0) in sorted(starts.items()):
        if q not in ends:
            skipped.append(f"Q{q}: start found but no end marker")
            continue
        ep, ey1, marks = ends[q]
        marks_map[q] = marks
        if ep < sp:
            skipped.append(f"Q{q}: end page before start page")
            continue

        parts = []
        if sp == ep:
            parts.append((sp, make_clip(qp_doc, sp, sy0, ey1)))
        else:
            parts.append((sp, make_clip(qp_doc, sp, sy0, qp_doc[sp].rect.height, cap_next=False)))
            for mid in range(sp + 1, ep):
                parts.append((mid, qp_doc[mid].rect))
            parts.append((ep, make_clip(qp_doc, ep, 0, ey1)))
        chunks[q] = parts

    return chunks, marks_map, skipped


# ── MS extraction — table-aware ───────────────────────────────────────────────
def _detect_ms_table_column(ms_doc) -> float:
    """
    Detect the left boundary of the question-number column in the mark scheme.
    Returns the x-coordinate threshold (numbers to the left of this are Q numbers).
    Uses the most common leftmost x-coordinate of numeric text across all pages.
    """
    x_counts: dict[float, int] = {}
    for pno in range(min(ms_doc.page_count, 5)):
        for (txt, x0, y0, x1, y1) in page_lines(ms_doc[pno]):
            if re.match(r"^\d{1,2}$", txt.strip()) and x0 < 200:
                bucket = round(x0 / 5) * 5   # 5px buckets
                x_counts[bucket] = x_counts.get(bucket, 0) + 1
    if not x_counts:
        return LEFT_X_MS
    # Most common bucket + some tolerance
    best = max(x_counts, key=x_counts.get)
    return best + 30


def find_ms_start(ms_doc, q, col_x: float | None = None):
    x_thresh = col_x if col_x is not None else LEFT_X_MS

    # Pass 1: bold number at left column
    for pno in range(ms_doc.page_count):
        for row in page_lines(ms_doc[pno], include_flags=True):
            txt, x0, y0, flags = row[0], row[1], row[2], row[5]
            if x0 > x_thresh:
                continue
            m = MS_Q_RE.match(txt.strip())
            if m and int(m.group(1)) == q and is_bold(flags):
                return (pno, y0)

    # Pass 2: any left-aligned number (no bold requirement)
    for pno in range(ms_doc.page_count):
        for (txt, x0, y0, x1, y1) in page_lines(ms_doc[pno]):
            if x0 > x_thresh:
                continue
            m = MS_Q_RE.match(txt.strip())
            if m and int(m.group(1)) == q:
                return (pno, y0)

    # Pass 3: search for pattern anywhere in page with loose x constraint
    for pno in range(ms_doc.page_count):
        rects = ms_doc[pno].search_for(f" {q} ")
        for r in sorted(rects, key=lambda rr: rr.x0):
            if r.x0 < 200:
                return (pno, r.y0)

    return None


def find_ms_end(ms_doc, start_pno, marks):
    phrase = f"Total {marks} marks"
    for pno in range(start_pno, ms_doc.page_count):
        rects = ms_doc[pno].search_for(phrase)
        if rects:
            return (pno, sorted(rects, key=lambda r: r.y1)[-1].y1)
        for (txt, x0, y0, x1, y1) in page_lines(ms_doc[pno]):
            m = MS_TOTAL_RE.match(txt)
            if m and int(m.group(1)) == marks:
                return (pno, y1)
    return None


def extract_ms(ms_doc, qs, marks_map):
    col_x = _detect_ms_table_column(ms_doc)
    chunks, missing = {}, []

    for q in qs:
        st = find_ms_start(ms_doc, q, col_x)
        if not st:
            missing.append(q)
            continue
        sp, sy0 = st
        end = find_ms_end(ms_doc, sp, marks_map[q])
        if not end:
            missing.append(q)
            continue
        ep, ey1 = end
        parts = []
        if sp == ep:
            parts.append((sp, make_clip(ms_doc, sp, sy0, ey1, cap_next=False, x_thresh=col_x)))
        else:
            parts.append((sp, make_clip(ms_doc, sp, sy0, ms_doc[sp].rect.height, cap_next=False, x_thresh=col_x)))
            for mid in range(sp + 1, ep):
                parts.append((mid, ms_doc[mid].rect))
            parts.append((ep, make_clip(ms_doc, ep, 0, ey1, cap_next=False, x_thresh=col_x)))
        chunks[q] = parts

    return chunks, missing


# ── PDF assembly ──────────────────────────────────────────────────────────────
def append_parts(out_doc, src_doc, parts):
    for pno, clip in parts:
        src = src_doc[pno]
        w, h = src.rect.width, clip.height
        pg = out_doc.new_page(width=w, height=h)
        pg.show_pdf_page(fitz.Rect(0, 0, w, h), src_doc, pno, clip=clip)


def render_question_png(item, dpi=120) -> bytes:
    pno, clip = item["qp_parts"][0]
    page = item["qp_doc"][pno]
    mat = fitz.Matrix(dpi / 72, dpi / 72)
    pix = page.get_pixmap(matrix=mat, clip=clip, colorspace=fitz.csRGB)
    return pix.tobytes("png")


# ── Per-paper extraction (runs in thread) ─────────────────────────────────────
def _extract_one_pair(key, qp_path, ms_path):
    items, warnings = [], []
    try:
        qp_doc = fitz.open(str(qp_path))
        ms_doc = fitz.open(str(ms_path))
    except Exception as e:
        return [], [f"Could not open {key}: {e}"], None, None

    qp_chunks, marks_map, skipped = extract_qp(qp_doc)
    for s in skipped:
        warnings.append(f"{key} {s}")

    qs = sorted(qp_chunks.keys())
    if not qs:
        warnings.append(f"{key}: no Q{MIN_Q}+ found")
        qp_doc.close()
        ms_doc.close()
        return [], warnings, None, None

    ms_chunks, missing = extract_ms(ms_doc, qs, marks_map)
    for q in missing:
        warnings.append(f"{key} Q{q}: MS not matched — skipped")

    for q in qs:
        if q not in ms_chunks:
            continue
        items.append({
            "key": key, "q": q, "marks": marks_map[q],
            "qp_doc": qp_doc, "ms_doc": ms_doc,
            "qp_parts": qp_chunks[q], "ms_parts": ms_chunks[q],
            "qp_path": str(qp_path), "ms_path": str(ms_path),
            "topic": None, "subtopic": None, "difficulty": None,
        })

    return items, warnings, qp_doc, ms_doc


# ── Concurrent pool builder ───────────────────────────────────────────────────
def build_question_pool(pairs, progress_cb=None):
    pool, open_docs, warnings = [], [], []
    total = len(pairs)
    done = 0

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = {
            ex.submit(_extract_one_pair, key, qp_path, ms_path): (key, qp_path, ms_path)
            for key, qp_path, ms_path in pairs
        }
        for future in as_completed(futures):
            items, warns, qp_doc, ms_doc = future.result()
            pool.extend(items)
            warnings.extend(warns)
            if qp_doc and ms_doc:
                open_docs.append((qp_doc, ms_doc))
            done += 1
            if progress_cb:
                progress_cb(done, total, f"Extracted {done}/{total} papers…")

    pool.sort(key=lambda q: (q["key"], q["q"]))
    return pool, open_docs, warnings


# ── Session save / load ───────────────────────────────────────────────────────
def pool_to_session(pool: list) -> list:
    return [{
        "key":        q["key"],
        "q":          q["q"],
        "marks":      q["marks"],
        "qp_path":    q.get("qp_path", ""),
        "ms_path":    q.get("ms_path", ""),
        "qp_parts":   [(pno, list(clip)) for pno, clip in q["qp_parts"]],
        "ms_parts":   [(pno, list(clip)) for pno, clip in q["ms_parts"]],
        "topic":      q.get("topic"),
        "subtopic":   q.get("subtopic"),
        "difficulty": q.get("difficulty"),
    } for q in pool]


def session_to_pool(session_items: list) -> tuple[list, list]:
    doc_cache = {}
    pool, open_docs = [], []

    for item in session_items:
        qp_path = item.get("qp_path", "")
        ms_path = item.get("ms_path", "")
        cache_key = (qp_path, ms_path)

        if cache_key not in doc_cache:
            try:
                qp_doc = fitz.open(qp_path)
                ms_doc = fitz.open(ms_path)
                doc_cache[cache_key] = (qp_doc, ms_doc)
                open_docs.append((qp_doc, ms_doc))
            except Exception as e:
                print(f"Session reload: could not open {qp_path}: {e}")
                doc_cache[cache_key] = (None, None)

        qp_doc, ms_doc = doc_cache[cache_key]
        if not qp_doc:
            continue

        def to_rect(r):
            return fitz.Rect(r[0], r[1], r[2], r[3])

        pool.append({
            "key":        item["key"],
            "q":          item["q"],
            "marks":      item["marks"],
            "qp_path":    qp_path,
            "ms_path":    ms_path,
            "qp_doc":     qp_doc,
            "ms_doc":     ms_doc,
            "qp_parts":   [(pno, to_rect(clip)) for pno, clip in item["qp_parts"]],
            "ms_parts":   [(pno, to_rect(clip)) for pno, clip in item["ms_parts"]],
            "topic":      item.get("topic"),
            "subtopic":   item.get("subtopic"),
            "difficulty": item.get("difficulty"),
        })

    return pool, open_docs
