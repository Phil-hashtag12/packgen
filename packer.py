"""
Pack generator with even mark distribution and improved cover page generation.
Cover page: full A4 width, exam-style front page with timing, instructions, mark grid.
Timing: marks × difficulty multiplier for realistic time estimates.
"""
import random
from pathlib import Path
import fitz

TARGET = 100
TMIN   = 92
TMAX   = 108

SPEC_TOPICS = {
    "Number":      ["Integers & Decimals","Fractions","Percentages","Ratio & Proportion","Powers & Roots","Standard Form","Bounds & Accuracy","Surds","Indices"],
    "Algebra":     ["Expressions & Simplification","Expanding & Factorising","Solving Linear Equations","Solving Quadratic Equations","Simultaneous Equations","Inequalities","Sequences","Functions & Graphs","Differentiation","Algebraic Proof","Iteration"],
    "Geometry":    ["Angles & Polygons","Circle Theorems","Trigonometry (Right-angled)","Sine & Cosine Rules","Area & Perimeter","Volume & Surface Area","Transformations","Vectors","Loci & Constructions","Coordinate Geometry","3D Geometry"],
    "Statistics":  ["Averages & Spread","Frequency Tables & Diagrams","Cumulative Frequency","Box Plots","Histograms","Scatter Graphs & Correlation"],
    "Probability": ["Basic Probability","Relative Frequency","Tree Diagrams","Venn Diagrams","Combined Events"],
    "Calculus":    ["Differentiation","Gradient & Tangents","Turning Points"],
}

TOPIC_COLORS = {
    "Number":      (52,  199, 89),
    "Algebra":     (0,   122, 255),
    "Geometry":    (255, 149, 0),
    "Statistics":  (175, 82,  222),
    "Probability": (255, 45,  85),
    "Calculus":    (90,  200, 250),
    "Unknown":     (142, 142, 147),
}

# Time multiplier per difficulty level (marks × multiplier = minutes)
DIFF_TIME = {1: 0.85, 2: 0.95, 3: 1.05, 4: 1.20, 5: 1.40}


def estimate_time(pack: list) -> int:
    """Estimate total time in minutes for a pack."""
    total = 0
    for q in pack:
        d = q.get("difficulty") or 3
        total += q["marks"] * DIFF_TIME.get(d, 1.0)
    return round(total)


def q_id(q):
    return (q["key"], q["q"])


def make_packs(pool, seed=None, topic_filter=None, difficulty_filter=None,
               balance_topics=True, weights=None):
    rng = random.Random(seed)
    candidates = list(pool)

    if topic_filter:
        tf_topics = {t for t, _ in topic_filter}
        candidates = [q for q in candidates if (q.get("topic") or "Unknown") in tf_topics]

    if difficulty_filter:
        lo, hi = difficulty_filter
        candidates = [q for q in candidates if lo <= (q.get("difficulty") or 3) <= hi]

    if not candidates:
        return []

    by_marks = {}
    for q in candidates:
        by_marks.setdefault(q["marks"], []).append(q)
    for lst in by_marks.values():
        rng.shuffle(lst)
        # Spaced repetition: sort high-weakness questions to front within each mark group
        if weights:
            lst.sort(key=lambda q: weights.get(f"{q['key']}|{q['q']}", 1.0), reverse=True)
    sorted_pool = []
    for m in sorted(by_marks.keys(), reverse=True):
        sorted_pool.extend(by_marks[m])

    total_marks = sum(q["marks"] for q in sorted_pool)
    n_packs = max(1, round(total_marks / TARGET))

    slots = [[] for _ in range(n_packs)]
    slot_marks = [0] * n_packs

    for q in sorted_pool:
        eligible = [i for i in range(len(slots)) if slot_marks[i] + q["marks"] <= TMAX]
        if not eligible:
            slots.append([q])
            slot_marks.append(q["marks"])
        else:
            i = min(eligible, key=lambda i: slot_marks[i])
            slots[i].append(q)
            slot_marks[i] += q["marks"]

    final_packs, overflow = [], []
    for slot in slots:
        if not slot:
            continue
        if sum(q["marks"] for q in slot) >= TMIN:
            final_packs.append(slot)
        else:
            overflow.append(slot)

    overflow_qs = [q for s in overflow for q in s]
    if not final_packs and overflow_qs:
        # If total available marks are below the normal minimum threshold,
        # still return a usable pack instead of failing the entire run.
        final_packs = [overflow_qs]
        overflow_qs = []

    for q in overflow_qs:
        placed = False
        for pack in final_packs:
            if sum(x["marks"] for x in pack) + q["marks"] <= TMAX:
                pack.append(q)
                placed = True
                break
        if not placed and final_packs:
            min(final_packs, key=lambda p: sum(x["marks"] for x in p)).append(q)

    for pack in final_packs:
        by_topic = {}
        for q in pack:
            by_topic.setdefault(q.get("topic") or "Unknown", []).append(q)
        for lst in by_topic.values():
            rng.shuffle(lst)
        topics = list(by_topic.keys())
        rng.shuffle(topics)
        interleaved, ptrs = [], {t: 0 for t in topics}
        while any(ptrs[t] < len(by_topic[t]) for t in topics):
            for t in topics:
                if ptrs[t] < len(by_topic[t]):
                    interleaved.append(by_topic[t][ptrs[t]])
                    ptrs[t] += 1
        pack[:] = interleaved

    return final_packs


def build_cover_page(pack_num, total_marks, pack,
                     paper_name="Edexcel International GCSE Mathematics (4MA1)") -> fitz.Document:
    """
    Generate an exam-authentic A4 cover page.
    Full width, proper margins, timing guidance, instructions, mark grid.
    """
    doc = fitz.open()
    W, H = 595, 842   # A4
    page = doc.new_page(width=W, height=H)

    ML = 50   # margin left
    MR = 545  # margin right (W - 50)
    CW = MR - ML  # content width = 495

    # White background
    page.draw_rect(fitz.Rect(0, 0, W, H), color=None, fill=(1, 1, 1))

    # ── Header band ──────────────────────────────────────────────────────────
    page.draw_rect(fitz.Rect(0, 0, W, 8), color=None, fill=(0, 0, 0))

    # Outer border
    page.draw_rect(fitz.Rect(ML - 5, 20, MR + 5, H - 20),
                   color=(0, 0, 0), fill=None, width=1)

    # ── Examination board header ──────────────────────────────────────────────
    y = 40
    page.insert_text((ML, y), "Pearson Edexcel",
                     fontsize=9, color=(0, 0, 0), fontname="Helvetica")
    page.insert_text((ML, y + 13), "International GCSE",
                     fontsize=9, color=(0, 0, 0), fontname="Helvetica")

    # Pack number top-right
    page.insert_text((MR - 80, y), f"Practice Pack",
                     fontsize=9, color=(0.4, 0.4, 0.4), fontname="Helvetica")
    page.insert_text((MR - 40, y + 13), f"{pack_num:02d}",
                     fontsize=22, color=(0, 0, 0), fontname="Helvetica-Bold")

    # ── Title block ───────────────────────────────────────────────────────────
    y = 90
    page.draw_line((ML, y), (MR, y), color=(0, 0, 0), width=0.5)
    y += 18

    page.insert_text((ML, y), "Mathematics",
                     fontsize=22, color=(0, 0, 0), fontname="Helvetica-Bold")
    y += 28
    page.insert_text((ML, y), "4MA1 — Higher Tier",
                     fontsize=13, color=(0, 0, 0), fontname="Helvetica")
    y += 14
    page.insert_text((ML, y), "Practice Paper",
                     fontsize=11, color=(0.4, 0.4, 0.4), fontname="Helvetica")

    y += 20
    page.draw_line((ML, y), (MR, y), color=(0, 0, 0), width=0.5)

    # ── Time and marks info box ───────────────────────────────────────────────
    y += 14
    est_time = estimate_time(pack)
    n_q = len(pack)

    # Two-column info
    col2 = ML + CW // 2
    page.insert_text((ML, y), "Time:",
                     fontsize=10, color=(0, 0, 0), fontname="Helvetica-Bold")
    page.insert_text((ML + 45, y), f"{est_time} minutes (estimated)",
                     fontsize=10, color=(0, 0, 0), fontname="Helvetica")
    page.insert_text((col2, y), "Total marks:",
                     fontsize=10, color=(0, 0, 0), fontname="Helvetica-Bold")
    page.insert_text((col2 + 75, y), str(total_marks),
                     fontsize=10, color=(0, 0, 0), fontname="Helvetica")

    y += 16
    page.insert_text((ML, y), "Questions:",
                     fontsize=10, color=(0, 0, 0), fontname="Helvetica-Bold")
    page.insert_text((ML + 62, y), str(n_q),
                     fontsize=10, color=(0, 0, 0), fontname="Helvetica")
    page.insert_text((col2, y), "Calculator:",
                     fontsize=10, color=(0, 0, 0), fontname="Helvetica-Bold")
    page.insert_text((col2 + 70, y), "Permitted",
                     fontsize=10, color=(0, 0, 0), fontname="Helvetica")

    y += 20
    page.draw_line((ML, y), (MR, y), color=(0.7, 0.7, 0.7), width=0.5)

    # ── Instructions ─────────────────────────────────────────────────────────
    y += 14
    page.insert_text((ML, y), "Instructions",
                     fontsize=11, color=(0, 0, 0), fontname="Helvetica-Bold")
    y += 14

    instructions = [
        "Use black ink or ball-point pen.",
        "Show all working — marks may be awarded for working even if the final answer is wrong.",
        "Answer all questions.",
        "Work at approximately 1 mark per minute.",
        "If your calculator does not have a π button, take π = 3.142.",
        "Diagrams are NOT accurately drawn unless stated.",
    ]
    for inst in instructions:
        page.insert_text((ML, y), "•  " + inst,
                         fontsize=9, color=(0, 0, 0), fontname="Helvetica")
        y += 13

    y += 8
    page.draw_line((ML, y), (MR, y), color=(0.7, 0.7, 0.7), width=0.5)

    # ── Topic breakdown ───────────────────────────────────────────────────────
    y += 14
    page.insert_text((ML, y), "Topic Breakdown",
                     fontsize=11, color=(0, 0, 0), fontname="Helvetica-Bold")
    y += 14

    topic_counts = {}
    topic_marks = {}
    for q in pack:
        t = q.get("topic") or "Unknown"
        topic_counts[t] = topic_counts.get(t, 0) + 1
        topic_marks[t]  = topic_marks.get(t, 0) + q["marks"]

    bar_w = CW - 140
    for topic, count in sorted(topic_counts.items(), key=lambda x: -x[1]):
        color = tuple(c / 255 for c in TOPIC_COLORS.get(topic, (142, 142, 147)))
        marks = topic_marks[topic]
        pct = marks / total_marks

        # Colour swatch
        page.draw_rect(fitz.Rect(ML, y - 7, ML + 8, y + 1), color=None, fill=color)
        # Topic name
        page.insert_text((ML + 14, y), topic,
                         fontsize=9, color=(0, 0, 0), fontname="Helvetica")
        # Bar track
        bar_x = ML + 110
        page.draw_rect(fitz.Rect(bar_x, y - 7, bar_x + bar_w, y - 1),
                       color=None, fill=(0.92, 0.92, 0.92))
        # Bar fill
        if pct > 0:
            page.draw_rect(fitz.Rect(bar_x, y - 7, bar_x + bar_w * pct, y - 1),
                           color=None, fill=color)
        # Stats
        page.insert_text((MR - 55, y), f"{marks}m  {count}q",
                         fontsize=8.5, color=(0.4, 0.4, 0.4), fontname="Helvetica")
        y += 16

    y += 8
    page.draw_line((ML, y), (MR, y), color=(0.7, 0.7, 0.7), width=0.5)

    # ── Mark grid ─────────────────────────────────────────────────────────────
    y += 14
    page.insert_text((ML, y), "Mark Record",
                     fontsize=11, color=(0, 0, 0), fontname="Helvetica-Bold")
    y += 14

    # Grid: 5 cols of questions per row
    GCOLS = 5
    cell_w = (CW) // GCOLS
    cell_h = 28

    for idx, q in enumerate(pack):
        col = idx % GCOLS
        row = idx // GCOLS
        cx = ML + col * cell_w
        cy = y + row * cell_h

        if cy + cell_h > H - 60:
            break

        # Cell border
        page.draw_rect(fitz.Rect(cx, cy, cx + cell_w - 2, cy + cell_h - 2),
                       color=(0.75, 0.75, 0.75), fill=(0.98, 0.98, 0.98), width=0.5)

        # Q number
        page.insert_text((cx + 4, cy + 11),
                         f"Q{idx + 1}",
                         fontsize=7.5, color=(0, 0, 0), fontname="Helvetica-Bold")
        # Max marks
        page.insert_text((cx + 4, cy + 21),
                         f"/{q['marks']}",
                         fontsize=7, color=(0.5, 0.5, 0.5), fontname="Helvetica")

    # ── Footer ────────────────────────────────────────────────────────────────
    page.draw_line((ML, H - 35), (MR, H - 35), color=(0.7, 0.7, 0.7), width=0.5)
    page.insert_text((ML, H - 22),
                     f"PackGen — Practice Pack {pack_num:02d}  ·  {total_marks} marks  ·  For revision use only",
                     fontsize=7.5, color=(0.6, 0.6, 0.6), fontname="Helvetica")

    return doc


def _get_number_span(page, old_str: str, x_limit: float):
    for block in page.get_text("dict").get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                txt = span.get("text", "").strip().rstrip(".")
                bbox = span.get("bbox")
                if not bbox:
                    continue
                x0, y0, x1, y1 = bbox
                if x0 > x_limit:
                    continue
                if txt == old_str:
                    return x0, y0, x1, y1, span.get("size", 12)
    return None


def _renumber_question(doc: fitz.Document, page_start: int, old_q: int, new_q: int):
    if old_q == new_q:
        return
    page = doc[page_start]
    result = _get_number_span(page, str(old_q), x_limit=120)
    if not result:
        return
    x0, y0, x1, y1, fsize = result
    fsize = max(9, min(16, fsize))
    pad = 1
    rect = fitz.Rect(x0 - pad, y0 - pad, x1 + pad, y1 + pad)
    page.draw_rect(rect, color=None, fill=(1, 1, 1))
    page.insert_text(
        (x0, y1 - 1), str(new_q),
        fontsize=fsize, fontname="Helvetica-Bold", color=(0, 0, 0),
    )


def _renumber_ms(doc: fitz.Document, page_start: int, old_q: int, new_q: int):
    if old_q == new_q:
        return
    page = doc[page_start]
    result = _get_number_span(page, str(old_q), x_limit=160)
    if not result:
        return
    x0, y0, x1, y1, fsize = result
    fsize = max(9, min(14, fsize))
    pad = 1
    rect = fitz.Rect(x0 - pad, y0 - pad, x1 + pad, y1 + pad)
    page.draw_rect(rect, color=None, fill=(1, 1, 1))
    page.insert_text(
        (x0, y1 - 1), str(new_q),
        fontsize=fsize, fontname="Helvetica-Bold", color=(0, 0, 0),
    )


def build_pack_pdfs(pack, pack_num, out_folder):
    from extractor import append_parts
    out_folder = Path(out_folder)

    q_out  = fitz.open()
    ms_out = fitz.open()
    total_marks = sum(q["marks"] for q in pack)

    cover = build_cover_page(pack_num, total_marks, pack)
    q_out.insert_pdf(cover)
    cover.close()

    q_page_starts  = []
    ms_page_starts = []

    for item in pack:
        q_start  = q_out.page_count
        ms_start = ms_out.page_count
        append_parts(q_out,  item["qp_doc"], item["qp_parts"])
        append_parts(ms_out, item["ms_doc"], item["ms_parts"])
        q_page_starts.append((q_start, item["q"]))
        ms_page_starts.append((ms_start, item["q"]))

    for new_num, (pg_start, old_q) in enumerate(q_page_starts, 1):
        _renumber_question(q_out, pg_start, old_q, new_num)

    for new_num, (pg_start, old_q) in enumerate(ms_page_starts, 1):
        _renumber_ms(ms_out, pg_start, old_q, new_num)

    q_pdf  = out_folder / f"Pack_{pack_num:02d}_Questions_{total_marks}marks.pdf"
    ms_pdf = out_folder / f"Pack_{pack_num:02d}_MarkScheme_{total_marks}marks.pdf"

    q_out.save(str(q_pdf))
    q_out.close()
    ms_out.save(str(ms_pdf))
    ms_out.close()

    topic_counts = {}
    for q in pack:
        t = q.get("topic") or "Unclassified"
        topic_counts[t] = topic_counts.get(t, 0) + 1

    est_time = estimate_time(pack)
    return q_pdf, ms_pdf, total_marks, topic_counts, est_time
