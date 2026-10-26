"""Suggest a consultant profile from a drawing: what each unknown layer holds.

A layer is judged by what is drawn on it, the name only backing that up:

    column      mostly closed rectangles of column size (150-2500 mm a side)
    beam        mostly long straight lines lying in parallel pairs 100-700 mm
                apart (the two faces of a beam), or long single centrelines
                where the name says beam
    beam_tag    mostly text, and most of it reads as beam marks (B12, GB 1,
    column_tag  B4a(230X600)...); likewise column and slab marks
    slab_tag
    grid        circles with a single letter or number inside

Only layers the base config does not already know are looked at, only in the
views the registry called plans, and every suggestion carries its evidence. The
result is an unconfirmed profile: a person confirms it before it is used.
"""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict

from ..marks import labels_in
from . import base_config

PLAN_KINDS = ("framing_plan", "foundation_plan", "column_layout")
NAME_HINTS = [("beam", r"BEAM|\bBM\b|GIRDER"), ("column", r"COL|CLM"), ("wall", r"WALL|PARDI|SHEAR"),
              ("slab", r"SLAB"), ("grid", r"GRID"), ("stair", r"STAIR"), ("opening", r"CUT|OPEN|VOID")]
SKIP_NAME = re.compile(r"DIM|HATCH|TEXT|TXT|LEADER|LEDDER|STEEL|REBAR|REINF|STIRR|STTRUP|"
                       r"TITLE|SHEET|BORDER|DEFPOINT|CLOUD|ARCH|FURN|PDF|SEC_|SCH|"
                       r"COMMENT|MARKUP|MARK-UP|REVISION|\bREV\b|QUER|REDLINE|CHECK|NPLT|NO\s*PLOT|"
                       r"OVERHEAD", re.I)
# Foundations and setting-out lines also come as parallel lines and boxes; their
# shapes must never make them beams or columns. Foundations get their own reader.
NOT_FRAME = re.compile(r"FOUND|FOOT|FTG|RAFT|PCC|P\.C\.C|PILE|CENT(?:RE|ER)\s*LINE|C/L|SETTING", re.I)
MASONRY = re.compile(r"BRICK|MASONRY|BLOCK\s*WORK|PILL?AR|PILLER", re.I)
TAG_NAME = re.compile(r"\bNO\b|NO\.|\bNOS?\b|NUM|TAG|MARK|LABEL|TEXT|TXT", re.I)
# a beam size in the layer name - BEAM-150X450, S-B5-230X600 (a beam mark and its size):
# such a layer is beams however few of them the plan has (New block: 2 lines on BEAM-150X450)
SIZED_BEAM_NAME = re.compile(r"(?:BEAM|\bBM|(?<![A-Z])[A-Z]{0,2}B\d{0,3}[A-Z]?)\s*[-_ ]?\s*\d{2,4}\s*[X×]\s*\d{2,4}", re.I)


def _with_blocks(ents, depth: int = 2):
    """The entities and what the blocks among them hold (a staircase, a set of
    steel columns drawn as one block): the extractor opens blocks, so their layers
    must be judged too."""
    out = []
    for e in ents:
        out.append(e)
        if e.dxftype() == "INSERT" and depth > 0:
            try:
                out += _with_blocks(list(e.virtual_entities()), depth - 1)
            except Exception:
                continue
    return out


def _segments(e):
    t = e.dxftype()
    if t == "LINE":
        s, f = e.dxf.start, e.dxf.end
        yield (s.x, s.y), (f.x, f.y)
    elif t == "LWPOLYLINE":
        pts = [(p[0], p[1]) for p in e.get_points("xy")]
        if e.closed and pts:
            pts = pts + [pts[0]]
        for a, b in zip(pts, pts[1:]):
            yield a, b


def _rectangle(e) -> tuple[float, float] | None:
    if e.dxftype() != "LWPOLYLINE":
        return None
    pts = [(p[0], p[1]) for p in e.get_points("xy")]
    if len(pts) == 5 and math.hypot(pts[0][0] - pts[4][0], pts[0][1] - pts[4][1]) < 1.0:
        pts = pts[:4]
    if len(pts) != 4 or not (e.closed or len(pts) == 4):
        return None
    sides = [math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]) for i in range(4)]
    if abs(sides[0] - sides[2]) > 5 or abs(sides[1] - sides[3]) > 5:
        return None
    a, b = sorted(sides[:2])
    return (a, b) if 150 <= a <= 2500 and b <= 2500 else None


def _paired_share(segs) -> float:
    """Share of long segments with a parallel partner 100-700 mm away: beam faces."""
    long = [(a, b) for a, b in segs if math.hypot(b[0] - a[0], b[1] - a[1]) >= 1000]
    if len(long) < 4:
        return 0.0
    buckets = defaultdict(list)
    for a, b in long:
        ang = round(math.degrees(math.atan2(b[1] - a[1], b[0] - a[0])) % 180 / 2) % 90
        buckets[ang].append((a, b))
    paired = 0
    for (a, b) in long:
        ang = round(math.degrees(math.atan2(b[1] - a[1], b[0] - a[0])) % 180 / 2)
        L = math.hypot(b[0] - a[0], b[1] - a[1])
        ux, uy = (b[0] - a[0]) / L, (b[1] - a[1]) / L
        for k in (ang - 1, ang, ang + 1):
            hit = False
            for (c, d) in buckets.get(k % 90, []):
                if (c, d) == (a, b):
                    continue
                dist = abs((c[0] - a[0]) * -uy + (c[1] - a[1]) * ux)
                if not 100 <= dist <= 700:
                    continue
                t1 = (c[0] - a[0]) * ux + (c[1] - a[1]) * uy
                t2 = (d[0] - a[0]) * ux + (d[1] - a[1]) * uy
                overlap = min(L, max(t1, t2)) - max(0.0, min(t1, t2))
                if overlap >= 0.5 * min(L, abs(t2 - t1)):
                    hit = True
                    break
            if hit:
                paired += 1
                break
    return paired / len(long)


def _x_crosses(segs) -> tuple[int, int]:
    """(cut-outs drawn as an X, long lines) on a layer: two lines crossing near both
    their middles is how a shaft or cut-out is drawn, whatever the layer is called
    (ALCAZAR 1st floor: 'viod')."""
    long = [(a, b) for a, b in segs if math.hypot(b[0] - a[0], b[1] - a[1]) >= 500]
    if len(long) < 4 or len(long) > 400:
        return 0, len(long)
    import os, sys
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), "cad2revit"))
    from core.slabs import openings_from_crosses                         # noqa: E402
    return len(openings_from_crosses([(a, b, "", "") for a, b in long])), len(long)


# a reinforcement-like layer name: skipped by name, unless its lines are beams carrying marks
REBAR_NAME = re.compile(r"STEEL|REBAR|REINF", re.I)


def _marked_single_lines(segs, marks) -> str:
    """Why a layer named like reinforcement holds beams drawn as single lines, or "".

    Pirgal (a steel-framed house) draws each steel beam as one thick line on 'STEEL', with
    its mark (SB5, SB9, GB1 ...) written along it; the layer was skipped by its name and no
    beam was read on it. Reinforcement bars carry bar callouts, not beam marks: a layer
    counts only when beam marks run along its long lines - parallel, within 600 mm, inside
    the line's length - at least 5 of them and for a third of its long lines, and when its
    lines are single (not a beam's two faces, which other layers carry)."""
    long = [(a, b) for a, b in segs if math.hypot(b[0] - a[0], b[1] - a[1]) >= 1000]
    if len(long) < 6 or _paired_share(segs) >= 0.4:
        return ""
    hit_lines, hit_marks = set(), set()
    for i, (a, b) in enumerate(long):
        L = math.hypot(b[0] - a[0], b[1] - a[1])
        ux, uy = (b[0] - a[0]) / L, (b[1] - a[1]) / L
        ang = math.degrees(math.atan2(uy, ux)) % 180
        for k, (x, y, rot) in enumerate(marks):
            t = (x - a[0]) * ux + (y - a[1]) * uy
            d = abs((x - a[0]) * -uy + (y - a[1]) * ux)
            da = abs((rot % 180) - ang)
            if d <= 600 and -200 <= t <= L + 200 and min(da, 180 - da) <= 10:
                hit_lines.add(i)
                hit_marks.add(k)
    if len(hit_marks) >= 5 and len(hit_lines) >= len(long) / 3:
        return (f"{len(long)} long single lines, {len(hit_lines)} with beam marks along them "
                f"({len(hit_marks)} marks): beams drawn as centrelines")
    return ""


def _layer_facts(ents, texts, families: dict[str, str] | None = None) -> dict[str, dict]:
    facts: dict[str, dict] = defaultdict(lambda: {"n": 0, "types": Counter(), "rects": 0, "segs": [],
                                                  "texts": 0, "fam": Counter(), "circles": 0})
    for e in ents:
        f = facts[e.dxf.layer]
        f["n"] += 1
        f["types"][e.dxftype()] += 1
        if e.dxftype() == "INSERT":
            # a mark drawn as a block with its text in an attribute (Pirgal's SLAB_MARK
            # bubbles reading S1, S2 ...): that text is on the insert's layer
            for a in getattr(e, "attribs", []) or []:
                s = (a.dxf.get("text", "") or "").strip()
                if s:
                    f["texts"] += 1
                    for lb in labels_in(s):
                        if lb.mark:
                            f["fam"][lb.mark.family(families)] += 1
        if _rectangle(e):
            f["rects"] += 1
        elif e.dxftype() in ("LINE", "LWPOLYLINE"):
            f["segs"].extend(_segments(e))
        elif e.dxftype() == "CIRCLE":
            f["circles"] += 1
    for t in texts:
        f = facts[t.layer]
        f["texts"] += 1
        for lb in labels_in(t.text):
            if lb.mark:
                f["fam"][lb.mark.family(families)] += 1
    return facts


def suggest(records, name: str = "", families: dict[str, str] | None = None) -> dict:
    """An unconfirmed profile for the layers in these views the base config does not know.

    families: what this drawing set's own mark prefixes mean (building.prefixes), so a
    layer of SL1, SL2 ... marks is found as slab marks where the set's slab schedule
    lists them; without it, the built-in prefix list alone."""
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), "cad2revit"))
    from core.reader import LayerMap                                   # noqa: E402
    known = LayerMap(base_config()["layer_roles"])

    ents, texts = [], []
    for r in records:
        if r.verdict.kind not in PLAN_KINDS or r.doc is None:
            continue
        hs = set(r.view.handles) | set(getattr(r.view, "borrow", None) or [])   # + marks taken from a twin
        ents += [e for e in r.doc.modelspace() if e.dxf.handle in hs]
        texts += [t for t in r.texts if t.space == "model"]
    facts = _layer_facts(_with_blocks(ents), texts, families)
    # layers drawn only inside blocks: judged by name and shape together, never by
    # shape alone (a block's 'Brick Piller' boxes are not RCC columns)
    direct = {e.dxf.layer for e in ents} | {t.layer for t in texts}
    from ..marks import family_of

    roles: dict[str, list[str]] = defaultdict(list)
    evidence: dict[str, str] = {}
    beam_marks = []                 # (x, y, rotation) of beam marks on the plans
    for t in texts:
        if any(lb.mark and lb.mark.family(families) == "beam" for lb in labels_in(t.text)):
            beam_marks.append((t.x, t.y, float(t.rotation or 0.0)))
    for layer, f in sorted(facts.items()):
        sized_beam = bool(SIZED_BEAM_NAME.search(layer)) and not NOT_FRAME.search(layer)
        if known.role_of(layer) == "unmapped" and REBAR_NAME.search(layer) and layer in direct \
                and not NOT_FRAME.search(layer) and not re.search(r"DIM|HATCH|TEXT|TXT|LEADER", layer, re.I):
            why = _marked_single_lines(f["segs"], beam_marks)
            if why:
                roles["beam"].append(layer)
                evidence[layer] = why + "; NOTE name suggests reinforcement - check"
            continue
        if known.role_of(layer) != "unmapped" or SKIP_NAME.search(layer):
            continue
        if f["n"] + f["texts"] < 5 and not sized_beam:
            continue
        name_says = next((r for r, rx in NAME_HINTS if re.search(rx, layer, re.I)), "")
        prefix_name = ""
        if not name_says and re.fullmatch(r"[A-Z]{2,3}", layer.strip().upper()) and \
                family_of(layer.strip().upper()) == "beam":
            # a layer named by the beams' mark prefix: SB, GB, PB (New block's SB beams
            # in the sunk toilets, on layer SB, were not modelled)
            name_says, prefix_name = "beam", layer.strip().upper()
        xs, n_x = _x_crosses(f["segs"]) if not name_says and not sized_beam else (0, 0)
        crosses = xs >= 2 and 2 * xs >= 0.8 * n_x        # nearly every line is half of an X
        if layer not in direct and not (name_says or sized_beam or crosses):
            continue
        geometry = f["n"] - f["texts"]
        role, why = "", ""
        if NOT_FRAME.search(layer):
            continue
        short = sum(1 for a, b in f["segs"] if 100 <= math.hypot(b[0] - a[0], b[1] - a[1]) <= 2500)
        boxes = f["rects"] + short // 4 + f["circles"]
        share = _paired_share(f["segs"]) if f["segs"] else 0.0
        n_long = sum(1 for a, b in f["segs"] if math.hypot(b[0] - a[0], b[1] - a[1]) >= 1000)
        mlines = f["types"]["MLINE"]
        tag_name = bool(TAG_NAME.search(layer))

        def beam_evidence():
            if mlines >= 5:
                return f"{mlines} multilines (a beam's two faces as one object)"
            if share >= 0.4 and n_long >= 6:
                return f"{n_long} long lines, {share:.0%} in parallel pairs 100-700 mm apart"
            if name_says == "beam" and n_long >= 6 and not prefix_name:
                return f"{n_long} long lines; name says beam (single centrelines)"
            if sized_beam and n_long >= 2:
                return f"{n_long} long lines; the name gives a beam size"
            if prefix_name and share >= 0.4 and n_long >= 4:
                return (f"{n_long} long lines, {share:.0%} in parallel pairs; named by the beam "
                        f"mark prefix {prefix_name}")
            return ""

        if f["texts"] >= 5 and f["texts"] >= 0.6 * f["n"]:
            fam, cnt = (f["fam"].most_common(1) or [("", 0)])[0]
            if cnt >= 0.6 * f["texts"] and fam in ("beam", "column", "slab"):
                role = f"{fam}_tag"
                why = f"{f['texts']} texts, {cnt} read as {fam} marks"
        elif tag_name:
            pass                      # a mark/number layer: leaders and boxes, never elements
        elif name_says == "beam" or sized_beam:
            why = beam_evidence()
            role = "beam" if why else ""
        elif name_says == "opening":
            # cut-outs: an outline, or the usual X from corner to corner (S-STRU. CUTOUT)
            n_shapes = sum(f["types"][k] for k in ("LINE", "LWPOLYLINE", "POLYLINE"))
            if n_shapes >= 2:
                role, why = "opening", f"name says cut-out/opening; {n_shapes} lines and outlines"
        elif name_says == "stair":
            n_shapes = sum(f["types"][k] for k in ("LINE", "LWPOLYLINE", "POLYLINE", "ARC"))
            if n_shapes >= 6:
                role, why = "stair", f"name says stair; {n_shapes} lines (flights, treads)"
        elif name_says in ("column", "wall"):
            if f["rects"] >= 3 or boxes >= 4:
                role = name_says
                why = (f"name says {name_says}; {f['rects']} closed rectangles, {short} short "
                       f"edges, {f['circles']} circles (round columns)")
        elif crosses:
            role, why = "opening", f"{xs} cut-outs drawn as an X ({n_x} lines, all in crosses)"
        elif f["rects"] >= 5 and f["rects"] >= 0.25 * geometry:
            role = "column"
            why = f"{f['rects']} column-sized closed rectangles of {geometry} shapes"
        elif (share >= 0.6 and n_long >= 20) or mlines >= 10:
            # nothing in the name: only strong evidence (a markup layer with a
            # dozen paired lines was once taken for beams)
            why = beam_evidence() + "; name says nothing - check"
            role = "beam"
        if not role and name_says == "grid" and f["circles"] >= 4:
            role, why = "grid", f"{f['circles']} circles; name says grid"
        if role in ("column", "beam") and MASONRY.search(layer):
            role = ""                 # brick piers and walls are not RCC members
        if role:
            if name_says and not role.startswith(name_says) and name_says != "grid":
                why += f"; NOTE name suggests {name_says}"
            elif name_says:
                why += f"; name agrees ({name_says})"
            roles[role].append(layer)
            evidence[layer] = why
    return {"name": name, "confirmed": False, "layer_roles": dict(roles),
            "prefix_families": {}, "evidence": evidence}
