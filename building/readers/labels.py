"""Marks and sizes written as text: plan captions and detail captions.

What a label means depends on the view it is in:

* On a plan, a label names the element it sits on: "B4a(230X600)" beside a
  beam, "C(230X500)" beside a column, "B12" with "230X600" written under it.
  Each is kept as a caption at its position; attaching it to the right element
  is the linker's job, by distance, never by guessing here.
* On a detail or schedule sheet, a label with a size states the size of that
  mark: "GB 1(230x450)" under a section. Each becomes a spec for the mark.

Every caption and spec keeps the text, layer and handle it was read from.
"""
from __future__ import annotations

import math
import re

from ..marks import grouped, labels_in, parse_mark, size_only
from ..model import Building, Element, Evidence, Value, READ
from ..sheets.levels import wing_in

READER = "labels"
PLAN_KINDS = ("framing_plan", "foundation_plan", "column_layout")
SPEC_KINDS = ("beam_details", "column_details", "footing_details", "slab_details",
              "stair_details", "section", "beam_schedule", "column_schedule",
              "slab_schedule", "footing_schedule")
PAIR_REACH = 4.0      # a lone size pairs with a mark within this many text heights
# a size written alone that can be a member's width x depth (not a room, a footing, a sheet)
MEMBER_MIN, MEMBER_MAX, DEPTH_MAX = 100.0, 1500.0, 3000.0
# a thickness written on its own under a slab mark: "140 MM THK.", "(150 THK)", "125MM THICK"
THK_ONLY = re.compile(r"^\(?\s*(\d{2,3})\s*(?:MM)?\s*\.?\s*(?:THK|THICK)\.?\s*\)?$", re.I)
# a plan's general note: "ALL SLABS ARE 125mm THK. UNLESS OTHERWISE SPECIFIED",
# "SLAB THICKNESS 150 MM UNLESS NOTED OTHERWISE"
SLAB_NOTE = re.compile(r"\bALL\s+(?:THE\s+)?SLABS?\s+(?:ARE|IS|SHALL\s+BE|TO\s+BE)?\s*(\d{2,3})\s*(?:MM)?\s*\.?\s*"
                       r"(?:THK|THICK)|\bSLABS?\s+THICKNESS\s*(?:IS|=|:|-)?\s*(\d{2,3})\s*MM", re.I)


def _shown(t) -> str:
    """The text as a person reads it: as written, and in mm where the app rewrote it
    (9"X18" (229X457))."""
    orig = getattr(t, "original", "")
    return f"{orig} ({t.text})" if orig and orig != t.text else t.text


def _ev(sheet: str, t, source: str) -> Evidence:
    return Evidence(reader=READER, sheet=sheet, source=source, layer=t.layer,
                    handle=t.handle, at=[t.x, t.y], text=_shown(t)[:120])


def _props(label, ev: Evidence, family_overrides) -> dict[str, Value]:
    p = {"family": Value(label.mark.family(family_overrides), READ, 1.0, [ev])}
    if label.width:
        p["width"] = Value(label.width, READ, 1.0, [ev])
    if label.depth:
        p["depth"] = Value(label.depth, READ, 1.0, [ev])
    if label.thickness:
        p["thickness"] = Value(label.thickness, READ, 1.0, [ev])
    if len(label.marks) > 1:
        p["shared_with"] = Value([m.key for m in label.marks], READ, 1.0, [ev])
    return p


_TOKEN = re.compile(r"\b[A-Z]{1,3}\s*-?\s*\d{1,4}[A-Z]?(?:\s*[,&]\s*\d{1,4}[A-Z]?)*\b")


def mentions(rec, texts, plan: bool) -> dict[str, list[dict]]:
    """Every place a mark is written other than as a lone tag on a plan: a schedule
    row ("B" "-" "66,83" ... "230" "600"), a list, a note. Kept so the linker can
    tell a size the drawings never give from one a reader failed to read."""
    rows = {}
    for t in texts:
        h = max(t.height or 0.0, 1.0)
        rows.setdefault(round((t.cy if t.cy is not None else t.y) / (0.6 * h)), []).append(t)
    out: dict[str, list[dict]] = {}
    chunks = []
    for line in rows.values():
        # one row of a table: texts in a line with no wide gap (tags across a plan are far apart)
        line.sort(key=lambda t: t.cx if t.cx is not None else t.x)
        cur = [line[0]]
        for t in line[1:]:
            gap = (t.cx if t.cx is not None else t.x) - (cur[-1].cx if cur[-1].cx is not None else cur[-1].x)
            if gap > 12 * max(t.height or 0.0, cur[-1].height or 0.0, 1.0):
                chunks.append(cur)
                cur = []
            cur.append(t)
        chunks.append(cur)
    for line in chunks:
        if plan and len(line) < 3:
            continue                      # a tag (and the size under it) on the plan, not a row
        if not any(re.search(r"\d", t.text) and not _TOKEN.fullmatch(t.text.strip().upper()) for t in line):
            continue                      # marks side by side ("S2 S2 S2"): tags, no size in the row
        joined = " ".join(t.text for t in line)
        for m in _TOKEN.finditer(joined.upper()):
            tok = m.group(0).replace(" ", "")
            for mk in grouped(tok) or [x for x in [parse_mark(tok)] if x]:
                lst = out.setdefault(mk.key, [])
                if len(lst) < 3:
                    lst.append({"text": " ".join(t.text for t in line)[:100], "sheet": rec.id,
                                "at": [line[0].x, line[0].y]})
    return out


def read(b: Building, rec, family_overrides: dict[str, str] | None = None) -> dict[str, int]:
    """Read the labels of one registered view into the building."""
    kind = rec.verdict.kind
    if kind not in PLAN_KINDS + SPEC_KINDS:
        return {}
    sheet = rec.id
    texts = [t for t in rec.texts if t.space == "model"]
    if sheet in b.sheets:
        b.sheets[sheet].extra["mentions"] = mentions(rec, texts, kind in PLAN_KINDS)
    as_spec = kind in SPEC_KINDS
    made = {"captions": 0, "specs": 0, "paired": 0}
    lone_marks, lone_sizes = [], []

    # a note on a plan naming another wing ("WING C REVISED BEAMS" on the B-wing plan):
    # kept with its position; the linker decides which tags it covers
    if not as_spec and rec.wing and rec.id in b.sheets:
        notes = [{"wing": w, "at": [t.cx if t.cx is not None else t.x, t.cy if t.cy is not None else t.y],
                  "text": t.text[:80], "layer": t.layer, "handle": t.handle}
                 for t in texts for w in [wing_in(t.text)]
                 if w and w != rec.wing and not re.search(r"SCHEDULE|\bPLAN\b|DETAIL|SECTION", t.text, re.I)]
        if notes:
            b.sheets[rec.id].extra["wing_notes"] = notes

    lone_thk = []
    # (and a floor's slab reinforcement drawing beside its plan: its notes are the floor's -
    # Pirgal writes "ALL SLABS ARE 100 thk. U.N.O." there)
    if (not as_spec or getattr(rec.view, "twin", "") == "reinforcement") and rec.id in b.sheets:
        # the slab thickness the plan's notes give every slab nothing else sizes
        for t in texts:
            m = SLAB_NOTE.search(t.text)
            if m:
                b.sheets[rec.id].extra["slab_thickness_note"] = {
                    "value": float(m.group(1) or m.group(2)), "text": t.text[:120], "layer": t.layer,
                    "handle": t.handle, "at": [t.x, t.y]}
                made["slab note"] = 1
                break
    for t in texts:
        labels = labels_in(t.text)
        if not labels:
            s = size_only(t.text)
            if s:
                lone_sizes.append((t, s))
            m = THK_ONLY.match(t.text.strip())
            if m and not as_spec:
                lone_thk.append((t, float(m.group(1))))
            continue
        for lb in labels:
            if lb.mark is None:
                continue
            has_size = bool(lb.width or lb.thickness)
            if not has_size and not lb.mark.number:
                continue
            for mk in lb.marks or [lb.mark]:
                if as_spec and not has_size:
                    continue                      # a bare mark on a detail sheet says nothing
                lb1 = type(lb)(mk, lb.width, lb.depth, lb.thickness, lb.text, lb.marks)
                ev = _ev(sheet, t, "detail caption" if as_spec else "plan caption")
                n = len(b.elements)
                el = Element(
                    id=f"{sheet}/{'spec' if as_spec else 'caption'}/{n}",
                    type="spec" if as_spec else "caption", sheet=sheet,
                    level=rec.levels[0] if len(rec.levels) == 1 else "", wing=rec.wing,
                    mark=mk.key, geometry={"at": [t.cx if t.cx is not None else t.x,
                                                  t.cy if t.cy is not None else t.y],
                                           "text_height": t.height, "rotation": t.rotation,
                                           "text_len": len(t.text)},
                    props=_props(lb1, ev, family_overrides),
                    extra={"raw": mk.raw, "also": mk.also,
                           **({"block_outline": t.block_outline}
                              if getattr(t, "block_outline", None) else {})})
                b.add(el)
                made["specs" if as_spec else "captions"] += 1
                if not has_size and not as_spec:
                    lone_marks.append((t, el))

    # a size written on its own beside a mark (Reis Magos: "B12" and, under it, "230X600")
    unpaired = []
    for t, (w, d) in lone_sizes:
        best, best_d = None, None
        for tm, el in lone_marks:
            dist = math.hypot(tm.x - t.x, tm.y - t.y)
            reach = PAIR_REACH * max(t.height, tm.height, 1.0)
            if dist <= reach and (best_d is None or dist < best_d):
                best, best_d = el, dist
        if best is None:
            unpaired.append((t, w, d))
            continue
        if "width" in best.props:
            continue
        ev = _ev(sheet, t, f"size written beside {best.mark}")
        best.props["width"] = Value(w, READ, 0.8, [ev])
        best.props["depth"] = Value(d, READ, 0.8, [ev])
        made["paired"] += 1

    # a thickness written on its own under a slab mark (New block: "NS1" over "140 MM THK.")
    for t, thk in lone_thk:
        best, best_d = None, None
        for tm, el in lone_marks:
            if el.get("family") != "slab":
                continue
            dist = math.hypot(tm.x - t.x, tm.y - t.y)
            reach = PAIR_REACH * max(t.height, tm.height, 1.0)
            if dist <= reach and (best_d is None or dist < best_d):
                best, best_d = el, dist
        if best is None or "thickness" in best.props:
            continue
        best.props["thickness"] = Value(thk, READ, 0.8, [_ev(sheet, t, f"thickness written beside {best.mark}")])
        made["paired"] += 1

    # a size written on a beam with no mark beside it (aldoot: 9"X21" along each beam, B-0
    # on a few): kept as a size caption - the linker gives it to the beam it runs along
    if not as_spec and kind in PLAN_KINDS:
        for t, w, d in unpaired:
            if not (MEMBER_MIN <= w <= MEMBER_MAX and MEMBER_MIN <= d <= DEPTH_MAX):
                continue
            ev = _ev(sheet, t, "size written on the plan, no mark beside it")
            b.add(Element(
                id=f"{sheet}/size_caption/{len(b.elements)}", type="size_caption", sheet=sheet,
                level=rec.levels[0] if len(rec.levels) == 1 else "", wing=rec.wing, mark="",
                geometry={"at": [t.cx if t.cx is not None else t.x, t.cy if t.cy is not None else t.y],
                          "text_height": t.height, "rotation": t.rotation, "text_len": len(t.text)},
                props={"width": Value(w, READ, 0.8, [ev]), "depth": Value(d, READ, 0.8, [ev])},
                extra={"raw": _shown(t)}))
            made["size captions"] = made.get("size captions", 0) + 1
    return {k: v for k, v in made.items() if v}
