"""What kind of drawing a view is: framing plan, foundation plan, beam details...

Two kinds of evidence, weighed together:

* Words: the view's title, its layout's name, the file name and the other
  prominent text in it. "STR. LAYOUT AT 1st FLOOR LVL" says framing plan;
  "SECTION: 6a-6a" says detail.
* Content: what is actually drawn. Grid bubbles and structural layers make a
  plan; many short "MARK (WxD)" captions and section titles make beam details;
  dense rows of text make a schedule. This is what stops a structural plan
  called "1ST FLOOR PLAN" being taken for an architectural floor plan.

The result is a kind, a confidence (how far ahead of the runner-up it is) and
the reasons, so the review screen can show why and a person can correct it.
Words are consultant-independent English drafting vocabulary; nothing here
names one consultant's layers.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

KINDS = [
    "framing_plan", "foundation_plan", "column_layout", "column_schedule",
    "beam_schedule", "slab_schedule", "footing_schedule", "steel_schedule",
    "beam_details", "column_details", "slab_details", "footing_details",
    "stair_details", "section", "notes", "legend", "architectural",
    "overview", "stray", "unknown",
]
STRUCTURAL_PLANS = ("framing_plan", "foundation_plan", "column_layout")

# (kind, pattern, weight). Patterns are matched against upper-case text.
WORDS: list[tuple[str, re.Pattern, float]] = [(k, re.compile(p), w) for k, p, w in [
    ("framing_plan", r"FRAMING|BEAM\s+LAYOUT|BEAM\s+PLAN|SLAB\s+LAYOUT|STR(?:UCTURAL|U?\.)?\s*LAY\s*-?\s*OUT\b(?!.*(?:FOUNDATION|FOOTING|COL))", 3),
    ("framing_plan", r"\bFLOOR\s+PLAN\b|\bLVL\b|\bSLAB\b", 0.5),
    ("foundation_plan", r"FOUNDATION|FOOTING\s+(?:LAYOUT|PLAN)|\bFDN\b|RAFT\s+(?:LAYOUT|PLAN)|PILE\s+(?:LAYOUT|PLAN)|FOOTING\s*&\s*BASEMENT|DIMENSIONAL\s+PLAN\s+OF\s+RAFT", 3),
    ("column_layout", r"\bCOL(?:UMN)?\.?\s*LAY|COLUMN\s+(?:[\w&/]+\s+){0,2}(?:LAYOUT|PLAN|POSITION)|CENT(?:RE|ER)\s*LINE\s+(?:PLAN|DRAWING)|SETTING\s+OUT|FACE\s+LINE", 3),
    ("column_schedule", r"\bCOL(?:UMN)?\.?\s*SCH|COLUMN\s+SCHEDULE|SCHEDULE\s+OF\s+COLUMNS?", 3),
    ("beam_schedule", r"BEAM\s+SCHEDULE|SCHEDULE\s+OF\s+(?:\w+\s+)*BEAMS|MEMBER\s+SCHEDULE", 3),
    ("slab_schedule", r"SLAB\s+SCHEDULE|SCHEDULE\s+OF\s+SLABS?", 3),
    ("footing_schedule", r"FOOTING\s+SCHEDULE|SCHEDULE\s+OF\s+FOOTINGS?|RAFT\s+SCHEDULE", 3),
    ("steel_schedule", r"STEEL\s+SCHEDULE|BAR\s+BENDING|\bBBS\b", 3),
    ("beam_details", r"BEAM\s+DE(?:TA|AT)ILS?|BEAM\s+SECTIONS?|LINTEL|\bDETAILS?\s+OF\s+BEAMS?", 3),
    ("column_details", r"COLUMN\s+DETAILS?|COLUMN\s+SECTIONS?", 3),
    ("slab_details", r"SLAB\s+DETAILS?|BALCONY\s+(?:SLAB\s+)?DETAIL|CHAJJA|CANOPY\s+DETAIL", 3),
    ("footing_details", r"FOOTING\s+DETAILS?|RAFT\s+DETAILS?|DETAIL\s+OF\s+RAFT|PEDESTAL|PILE\s+CAP", 3),
    ("stair_details", r"STAIR|STAIRCASE|RAMP\s+DETAIL", 3),
    ("section", r"^\s*SECTION\b|\bSECTION\s*[:\-]?\s*[A-Z0-9]{1,3}'?\s*-\s*[A-Z0-9]{1,3}'?|\bELEVATION\b", 2),
    ("notes", r"GENERAL\s+NOTES|^\s*NOTES?\s*[:\-]", 2),
    ("legend", r"\bLEGENDS?\b", 2),
    ("architectural", r"ELECTRICAL|FURNITURE|PLUMBING|DRAINAGE|DOOR\s*&?\s*WINDOW|LANDSCAPE|INTERIOR|HVAC|FIRE\s+FIGHTING", 3),
    ("architectural", r"\bFLOOR\s+PLANS\b|\bARCH(?:ITECTURAL)?\b", 1.5),
]]

SECTION_TITLE = re.compile(r"\bSECTION\s*[:\-]?\s*[A-Z0-9]{1,3}'?\s*-\s*[A-Z0-9]{1,3}'?")
SIZE_CAPTION = re.compile(r"[A-Z]{0,4}\s*-?\s*\d{0,3}[A-Z]?\s*\(?\s*\d{2,4}\s*[X×]\s*\d{2,4}\s*\)?")
GRID_LABEL = re.compile(r"^(?:[A-Z]{1,2}|\d{1,2}|[A-Z]\d?'?)$")
STRUCT_LAYER = re.compile(r"BEAM|COL|SLAB|FOOT|FDN|RAFT|PILE|GIRDER|STR|RCC|PARDI|SHEAR|CORE", re.I)
ARCH_LAYER = re.compile(r"^A[-_]|FURN|DOOR|WINDOW|ELEC|PLUMB|SANIT|LIGHTING|SWITCH|FIXTURE|ARCH", re.I)
REBAR_LAYER = re.compile(r"STEEL|REBAR|REINF|STIRR|STTRUP|\bBARS?\b|LINK", re.I)
# Bar callouts as engineers write them: 3-T16, 2-T16+2-T12, T8-100 c/c, 8T@350, 10#, Ø12
BAR_CALLOUT = re.compile(r"\b\d+\s*-\s*T\s?\d{1,2}\b|\bT\s?\d{1,2}\s*[-@]\s*\d{2,3}\b|"
                         r"\b\d+\s*#\s*\d{0,2}|Ø\s?\d{1,2}\b|\b\d{1,2}\s*T\s*@\s*\d{2,3}", re.I)

# An element mark as drafters write it: B12, B-12, GB 3, B4a, C12, FC1, F7, S4A,
# optionally with its size: B4a(230X600). The family is read from the prefix:
# ending in B is a beam (B, GB, PB, LB, EB, ...), F a footing, C a column, S a slab.
MARK = re.compile(r"^([A-Z]{1,4})\s*-?\s*(\d{1,3})[A-Z]?(?:\s*/\s*[A-Z]{1,3}\d*)?\s*(?:\(\s*\d{2,4}\s*[X×]\s*\d{2,4}\s*\))?$")


def mark_family(prefix: str) -> str:
    p = prefix.upper()
    if p in ("F", "IF", "CF", "PF", "RF", "SF", "FTG", "PC", "P", "PD"):
        return "footing"
    if p.endswith("B") or p in ("BKT", "MLB", "BC", "BP", "BM"):
        return "beam"
    if p in ("C", "FC", "SC", "COL", "SW", "LW"):
        return "column"
    if p in ("S", "TS", "RS"):
        return "slab"
    return "other"


@dataclass
class Verdict:
    kind: str
    confidence: float
    reasons: list[str] = field(default_factory=list)
    scores: dict[str, float] = field(default_factory=dict)
    also: list[str] = field(default_factory=list)


def _words(scores: Counter, reasons: list, text: str, weight: float, where: str):
    t = (text or "").upper()
    if not t:
        return
    for kind, rx, w in WORDS:
        if rx.search(t):
            scores[kind] += w * weight
            reasons.append(f"{where} '{text[:50]}' -> {kind}")


ELEMENT_CAPTION = re.compile(
    r"^(?:\d+\s*MM\s+)?(?:SUNK|DROP|RCC\s+PARDI|SHEAR\s+WALL|RETAINING\s+WALL|GIRDER\s+BEAM|"
    r"FLOATING\s+COLUMN|COLUMN\s+STOP|COLUMN|BEAM|SLAB|CUT\s*-?\s*OUT|OPENING|FOOTING|"
    r"CONCEALED\s+BEAM|HIDDEN\s+BEAM)(?:\s+\w+){0,2}$")


def grid_bubbles(texts, circles) -> int:
    """Circles 150-1500 mm across the radius with a short label inside: grid bubbles.

    Rebar in a section is drawn as circles too, but a few mm across and unlabelled;
    a column mark in a circle is labelled but sits on a plan anyway.
    """
    labels = [(t.x, t.y) for t in texts if GRID_LABEL.match(t.text.upper())]
    n = 0
    for cx, cy, r in circles:
        if 150.0 <= r <= 1500.0 and any(abs(x - cx) <= 1.3 * r and abs(y - cy) <= 1.3 * r
                                        for x, y in labels):
            n += 1
    return n


def tabulated(texts) -> set:
    """Marks standing in a table column: one above the other, at one x, rows evenly
    spaced a few text heights apart, four or more in a run. A plan lines marks up
    along its grid too, but a bay apart - tens of text heights - not a row apart."""
    out = set()
    cols: dict[int, list] = {}
    for t in texts:
        h = max(t.height, 1.0)
        cols.setdefault(round(t.x / (2 * h)), []).append(t)
    for ts in cols.values():
        ts.sort(key=lambda t: -t.y)
        run = [ts[0]] if ts else []
        for a, b in zip(ts, ts[1:]):
            h = max(a.height, 1.0)
            gap = a.y - b.y
            first = run[0].y - run[1].y if len(run) >= 2 else None
            if 0 < gap <= 8 * h and (first is None or abs(gap - first) <= 0.15 * first):
                run.append(b)
                continue
            if len(run) >= 4:
                out.update(id(x) for x in run)
            run = [b]
        if len(run) >= 4:
            out.update(id(x) for x in run)
    return out


# a floor's name standing alone, as an elevation or section labels its levels ("SECOND FLOOR",
# "STILT FLOOR LVL", "TERRACE LEVEL")
FLOOR_LABEL = re.compile(r"^(?:(?:BASEMENT|STILT|PODIUM|PLINTH|GROUND|UPPER|LOWER|MEZZ\w*|TERRACE|FIRST|SECOND|"
                         r"THIRD|FOURTH|FIFTH|SIXTH|SEVENTH|EIGHTH|NINTH|TENTH|\d{1,2}\s*(?:ST|ND|RD|TH)|ROOF)\s*)+"
                         r"(?:FLOOR|FLR)?\.?\s*(?:LEVEL|LVL)?\.?$"
                         # numbered levels as their own labels (Akanistha's shear wall elevations:
                         # LEVEL 00 ... LEVEL 05 up the side)
                         r"|^(?:LEVEL|LVL)\.?\s*[-_]?\s*\d{1,2}$")


def floor_stack(texts) -> tuple[int, float]:
    """How many different floors are named one above the other in one column of labels, and
    how tall that column is (mm): a building elevation or section (Pirgal "ELEVATION ALONG
    GRID H": STILT, GROUND, FIRST, SECOND FLOOR down its left side). A plan names its own
    floor once, in its title."""
    labels = [t for t in texts if FLOOR_LABEL.match(t.text.upper().strip())]
    best, span = 0, 0.0
    for t in labels:
        h = max(t.height, 1.0)
        col = [u for u in labels if abs(u.x - t.x) <= 15 * h]
        n = min(len({u.text.upper().strip() for u in col}), len({round(u.y / (5 * h)) for u in col}))
        if n > best:
            best, span = n, max(u.y for u in col) - min(u.y for u in col)
    return best, span


def features(texts, layers: Counter, circles: list[tuple[float, float, float]]) -> dict[str, float]:
    """Content measures for one view."""
    n_text = len(texts)
    caps = sum(1 for t in texts if
               re.fullmatch(r"[A-Z]{0,4}\s*-?\s*\d{0,3}[A-Z]?\s*\(\s*\d{2,4}\s*[X×]\s*\d{2,4}\s*\)", t.text.upper()))
    sections = sum(1 for t in texts if SECTION_TITLE.search(t.text.upper()))
    legend = sum(1 for t in texts if ELEMENT_CAPTION.match(t.text.upper()))
    total = sum(layers.values()) or 1
    struct = sum(n for l, n in layers.items() if STRUCT_LAYER.search(l)) / total
    arch = sum(n for l, n in layers.items() if ARCH_LAYER.search(l)) / total
    rebar = sum(n for l, n in layers.items() if REBAR_LAYER.search(l)) / total
    bars = sum(1 for t in texts if BAR_CALLOUT.search(t.text))
    fam, tab = Counter(), Counter()
    marked = []
    for t in texts:
        m = MARK.match(t.text.upper())
        if m:
            marked.append((t, mark_family(m.group(1))))
    in_table = tabulated([t for t, _ in marked])
    for t, f in marked:
        (tab if id(t) in in_table else fam)[f] += 1
    return {"texts": n_text, "size_captions": caps, "section_titles": sections,
            "grid_bubbles": grid_bubbles(texts, circles), "element_captions": legend,
            "beam_marks": fam["beam"], "column_marks": fam["column"],
            "footing_marks": fam["footing"], "slab_marks": fam["slab"],
            "table_marks": sum(tab.values()), "table_family": tab.most_common(1)[0][0] if tab else "",
            "bar_callouts": bars, "rebar_share": round(rebar, 2),
            "floor_stack": floor_stack(texts)[0], "floor_stack_mm": round(floor_stack(texts)[1]),
            "struct_share": round(struct, 2), "arch_share": round(arch, 2)}


def is_detail_like(feats: dict) -> bool:
    """Reinforcement drawn and called out everywhere: sections and details.

    Measured on the training sets: detail sheets have 56-75% of their entities on
    reinforcement layers and 100+ bar callouts; plans at most 17% and a few
    dozen callouts (slab steel notes)."""
    return feats["rebar_share"] >= 0.3 or \
        (feats["bar_callouts"] >= 20 and feats["bar_callouts"] >= 0.15 * max(feats["texts"], 1))


def is_plan(feats: dict, size_m: tuple[float, float]) -> bool:
    """A building-sized drawing with a grid, or with element marks spread over
    structural layers. Reis Magos draws no grid at all, so the grid alone is not
    enough; and detail sheets are full of marks too, so without a grid a
    reinforcement-heavy view is never a plan."""
    big = max(size_m) >= 12.0
    if not big:
        return False
    if feats["grid_bubbles"] >= 4:
        return True
    marks = feats["beam_marks"] + feats["column_marks"] + feats["footing_marks"] + feats["slab_marks"]
    return marks >= 20 and feats["struct_share"] >= 0.3 and not is_detail_like(feats)


def classify(title: str, layout: str, filename: str, prominent: list[str],
             feats: dict[str, float], size_m: tuple[float, float]) -> Verdict:
    scores: Counter = Counter()
    reasons: list[str] = []
    # a one-word "title" (FOUNDATION, from a table header) is weak evidence
    _words(scores, reasons, title, 0.5 if len(title.split()) == 1 else 1.0, "title")
    _words(scores, reasons, layout, 1.0, "layout")      # a layout name is a deliberate sheet name
    _words(scores, reasons, filename, 0.4, "file")
    for p in prominent[:6]:
        _words(scores, reasons, p, 0.3, "text")

    w, h = size_m
    big = max(w, h) >= 12.0                       # a building plan, not a detail
    plan = is_plan(feats, size_m)
    if plan:
        for k in STRUCTURAL_PLANS:
            scores[k] += 1.5
        # a 50 m drawing of marked structure is a plan, whatever inset captions say
        for k in ("section", "beam_details", "column_details", "slab_details", "footing_details",
                  "beam_schedule", "slab_schedule", "column_schedule", "footing_schedule"):
            scores[k] -= 2.0
        reasons.append(f"plan: {feats['grid_bubbles']} grid bubbles, {feats['beam_marks']} beam, "
                       f"{feats['column_marks']} column, {feats['footing_marks']} footing marks "
                       f"over {w:.0f} x {h:.0f} m")
        # which plan, from what is marked on it
        if feats["footing_marks"] >= 5:
            scores["foundation_plan"] += 1.5
            reasons.append(f"{feats['footing_marks']} footing marks -> foundation plan")
        if feats["beam_marks"] >= max(10, 2 * feats["footing_marks"]):
            scores["framing_plan"] += 1.5
            reasons.append(f"{feats['beam_marks']} beam marks -> framing plan")
        elif feats["column_marks"] >= 10 and feats["beam_marks"] < 10 and feats["footing_marks"] < 5:
            scores["column_layout"] += 1.5
            reasons.append(f"{feats['column_marks']} column marks, few beams or footings -> column layout")
    says_elevation = any(re.search(r"\bELEVATIONS?\b", (p or "").upper()) for p in [title] + list(prominent[:8]))
    titled_plan = bool(re.search(r"\bPLAN\b|\bLAY\s*-?\s*OUT\b|\bFRAMING\b", (title or "").upper()))
    if feats.get("floor_stack", 0) >= 3 and not titled_plan and \
            (feats.get("floor_stack_mm", 0) >= 0.4 * h * 1000.0 or says_elevation):
        # three or more floors named one above the other, up most of the drawing: an
        # elevation or a building section, however many beam marks it carries (its beams
        # are seen from the side). A small section with its levels beside a plan (NIFT's
        # foundation sheet) does not reach so far up the view
        scores["section"] += 5.0
        for k in STRUCTURAL_PLANS:
            scores[k] -= 2.0
        reasons.append(f"{feats['floor_stack']} floors named one above the other -> elevation / section")
    if feats["struct_share"] >= 0.3 and big:
        for k in STRUCTURAL_PLANS:
            scores[k] += 0.5
        scores["architectural"] -= 2.0
        reasons.append(f"{feats['struct_share']:.0%} of entities on structural layers")
    if feats["arch_share"] >= 0.3:
        scores["architectural"] += 2.0
        reasons.append(f"{feats['arch_share']:.0%} of entities on architectural layers")
    detail = is_detail_like(feats)
    if not plan and (feats["section_titles"] >= 2 or feats["size_captions"] >= 3 or detail):
        # sections, MARK(WxD) captions, reinforcement: a detail sheet; which
        # elements it details follows from the marks on it
        fam = {"beam_details": feats["beam_marks"], "column_details": feats["column_marks"],
               "footing_details": feats["footing_marks"], "slab_details": feats["slab_marks"]}
        kind = max(fam, key=fam.get) if max(fam.values()) > 0 else "beam_details"
        scores[kind] += 1.5 + 0.2 * min(feats["section_titles"], 10) + \
            (1.0 if feats["size_captions"] >= 3 else 0.0) + (1.0 if detail else 0.0)
        scores["section"] -= 1.0                  # many sections make a detail sheet
        for k in STRUCTURAL_PLANS:
            scores[k] -= 1.0 if detail else 0.0
        reasons.append(f"{feats['section_titles']} section titles, {feats['size_captions']} "
                       f"MARK(WxD) captions, {feats['bar_callouts']} bar callouts, "
                       f"{feats['rebar_share']:.0%} on reinforcement layers -> {kind.replace('_', ' ')}")
    if feats.get("table_marks", 0) >= 8 and not plan:
        # marks standing in table columns: a schedule, of what the marks are
        kind = {"beam": "beam_schedule", "column": "column_schedule", "slab": "slab_schedule",
                "footing": "footing_schedule"}.get(feats.get("table_family", ""), "beam_schedule")
        scores[kind] += 2.0
        for k in STRUCTURAL_PLANS:
            scores[k] -= 1.0
        reasons.append(f"{feats['table_marks']} marks in table columns -> {kind.replace('_', ' ')}")
    if feats["element_captions"] >= 4 and \
            feats["element_captions"] >= 0.6 * max(feats["texts"], 1) and not plan:
        scores["legend"] += 4.0
        reasons.append(f"{feats['element_captions']} of {feats['texts']} texts name an element "
                       "type -> legend")

    if not scores or max(scores.values()) <= 0:
        return Verdict("unknown", 0.0, reasons or ["no title words or content to go on"],
                       dict(scores))
    ranked = scores.most_common()
    best, top = ranked[0]
    second = ranked[1][1] if len(ranked) > 1 else 0.0
    conf = round(max(0.0, (top - max(second, 0)) / top), 2) if top > 0 else 0.0
    # one view can hold several things (the Reis Magos schedule block has beam
    # and slab schedules side by side): anything close behind is kept as "also"
    also = [k for k, v in ranked[1:] if v >= 0.6 * top and v > 0]
    return Verdict(best, conf, reasons, {k: round(v, 2) for k, v in ranked}, also)
