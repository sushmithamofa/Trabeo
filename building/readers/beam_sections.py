"""Beam cross-sections that are not plain rectangles, and the beams they belong to.

A detail sheet draws a beam's cross-section under a title, "SECTION :- X-X" (Sasan Gir
Main Block terrace: 750 wide edge beams, full 600 deep over half their width, the other
half sloping down to 100 or 200 at the edge). The plan marks where each applies with
the section's letter at both ends of a cut across the beam (X ... X).

Such details are seldom drawn to scale (there: 375 drawn as 561 and 600 units, 600 as
1050), so the shape is never measured off the drawing: every corner of the drawn
outline is put at the size its dimensions WRITE. A corner no dimension fixes is kept
in proportion and said to be undimensioned. A second outline hung under the section
("to L.L.", lintel level) has no written depth: it is reported, not modelled.

    beam_section  one per section detail: name, width, depth, profile [[u, v], ...]
                  in mm, u from the outline's left edge, v down from its top (<= 0)
    section_cut   one per pair of letters on a plan: name, a, b (drawing units)
"""
from __future__ import annotations

import math
import re

from ..model import Building, Element, Evidence, Value, READ

READER = "beam_sections"
TITLE = re.compile(r"\bSEC(?:TION|T)?\.?\s*[:\-]*\s*([A-Z0-9]{1,2})\s*-\s*\1\s*$", re.I)
NUMBER = re.compile(r"^\s*(\d{2,4})\s*$")
LETTER = re.compile(r"^\s*([A-Z])\s*$")
SECTION_KINDS = ("beam_details", "section", "slab_details")
PLAN_KINDS = ("framing_plan",)


def _entities(rec):
    db = getattr(getattr(rec, "doc", None), "entitydb", None)
    view = getattr(rec, "view", None)
    if db is None or view is None:
        return []
    return [e for e in (db.get(h) for h in view.handles) if e is not None]


def _closed_polys(ents):
    out = []
    for e in ents:
        if e.dxftype() != "LWPOLYLINE":
            continue
        pts = [(p[0], p[1]) for p in e.get_points("xy")]
        if len(pts) > 3 and math.dist(pts[0], pts[-1]) < 1.0:
            pts = pts[:-1]
        if len(pts) >= 3 and (e.closed or len(pts) >= 4):
            out.append((e, pts))
    return out


def _bbox(pts):
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def _area(pts) -> float:
    return abs(sum(pts[i][0] * pts[i - 1][1] - pts[i - 1][0] * pts[i][1] for i in range(len(pts)))) / 2.0


def _spans(texts, lines, box, th):
    """(axis, drawn a, drawn b, written value) for each written dimension near the
    outline: the number, the dimension line it sits on, and the ticks along that line
    that bound the piece the number is written in."""
    x0, y0, x1, y1 = box
    pad = 8.0 * th
    near = [t for t in texts if x0 - pad <= (t.cx or t.x) <= x1 + pad and y0 - pad <= (t.cy or t.y) <= y1 + pad]
    axl = []
    for e in lines:
        a, b = e.dxf.start, e.dxf.end
        if abs(a.y - b.y) < 1.0:
            axl.append(("x", min(a.x, b.x), max(a.x, b.x), a.y))
        elif abs(a.x - b.x) < 1.0:
            axl.append(("y", min(a.y, b.y), max(a.y, b.y), a.x))
    out = []
    for t in near:
        m = NUMBER.match(t.text)
        if not m:
            continue
        rot = (t.rotation or 0.0) % 180.0
        axis = "x" if rot < 30 or rot > 150 else "y"
        c_along, c_across = ((t.cx, t.cy) if axis == "x" else (t.cy, t.cx))
        best = None
        for ax, lo, hi, at in axl:
            if ax != axis or hi - lo < 2.0 * th or not (lo - th <= c_along <= hi + th):
                continue
            d = abs(c_across - at)
            if d <= 3.0 * th and (best is None or d < best[0]):
                best = (d, lo, hi, at)
        if best is None:
            continue
        _, lo, hi, at = best
        # ticks: short lines across the dimension line, between its ends
        ticks = {lo, hi}
        for ax, l2, h2, at2 in axl:
            if ax != axis and h2 - l2 <= 2.0 * th and l2 - 1.0 <= at <= h2 + 1.0 and lo - 1.0 <= at2 <= hi + 1.0:
                ticks.add(at2)
        ticks = sorted(ticks)
        piece = next(((p, q) for p, q in zip(ticks, ticks[1:]) if p <= c_along <= q), (lo, hi))
        out.append((axis, piece[0], piece[1], float(m.group(1)), t))
    return out


def _solve(coords, spans, tol):
    """Written positions for drawn coordinates, from spans (a, b, value). Returns
    {drawn: written} and the drawn coordinates no dimension reaches."""
    def snap(v):
        c = min(coords, key=lambda q: abs(q - v))
        return c if abs(c - v) <= tol else None
    edges = {}
    for a, b, val in spans:
        sa, sb = snap(a), snap(b)
        if sa is None or sb is None or sa == sb:
            continue
        lo, hi = min(sa, sb), max(sa, sb)
        edges.setdefault(lo, []).append((hi, val))
        edges.setdefault(hi, []).append((lo, -val))
    real = {}
    start = min(coords)
    real[start] = 0.0
    todo = [start]
    while todo:
        c = todo.pop()
        for o, val in edges.get(c, []):
            if o not in real:
                real[o] = real[c] + val
                todo.append(o)
    known = sorted(real)
    loose = [c for c in coords if c not in real]
    if len(known) >= 2:
        k = (real[known[-1]] - real[known[0]]) / ((known[-1] - known[0]) or 1.0)
        for c in loose:            # in proportion between its dimensioned neighbours
            lo = max((q for q in known if q < c), default=None)
            hi = min((q for q in known if q > c), default=None)
            if lo is not None and hi is not None:
                real[c] = real[lo] + (real[hi] - real[lo]) * (c - lo) / (hi - lo)
            else:
                ref = lo if lo is not None else hi
                real[c] = real[ref] + (c - ref) * k
    return real, loose


def _cluster(vals, tol):
    out = []
    for v in sorted(vals):
        if out and v - out[-1][-1] <= tol:
            out[-1].append(v)
        else:
            out.append([v])
    return {v: sum(g) / len(g) for g in out for v in g}


def read_sections(b: Building, rec) -> int:
    texts = [t for t in rec.texts if t.space == "model"]
    titles = [(t, TITLE.search(t.text)) for t in texts]
    titles = [(t, m.group(1).upper()) for t, m in titles if m]
    if not titles:
        return 0
    ents = _entities(rec)
    polys = _closed_polys(ents)
    lines = [e for e in ents if e.dxftype() == "LINE"]
    n = 0
    unread = []                         # titles nothing is found under: tried as loose lines below
    for t, name in titles:
        th = max(t.height or 0.0, 1.0)
        tx, ty = (t.cx or t.x), (t.cy or t.y)
        cands = []
        for e, pts in polys:
            x0, y0, x1, y1 = _bbox(pts)
            w, h = x1 - x0, y1 - y0
            if not (0.0 <= y0 - ty <= 15.0 * th and abs((x0 + x1) / 2.0 - tx) <= 15.0 * th
                    and w <= 30.0 * th and h <= 30.0 * th and w > th and h > th):
                continue
            cands.append((_area(pts), e, pts))
        if not cands:
            unread.append((t, name))
            continue
        _, main, pts = max(cands, key=lambda c: c[0])
        box = _bbox(pts)
        tol = max(0.03 * max(box[2] - box[0], box[3] - box[1]), 0.5 * th)
        spans = _spans(texts, lines, box, th)
        xs = _cluster([p[0] for p in pts], tol / 2)
        ys = _cluster([p[1] for p in pts], tol / 2)
        rx, loose_x = _solve(sorted(set(xs.values())), [(a, c, v) for ax, a, c, v, _ in spans if ax == "x"], tol)
        ry, loose_y = _solve(sorted(set(ys.values())), [(a, c, v) for ax, a, c, v, _ in spans if ax == "y"], tol)
        if len(rx) < 2 or len(ry) < 2:
            unread.append((t, name))
            continue
        top = max(ry.values())
        prof = [[round(rx[xs[p[0]]], 1), round(ry[ys[p[1]]] - top, 1)] for p in pts]
        W = max(p[0] for p in prof) - min(p[0] for p in prof)
        D = -min(p[1] for p in prof)
        written = sorted({f"{v:.0f}" for _, _, _, v, _ in spans})
        ev = Evidence(reader=READER, sheet=rec.id, source=f"section {name}-{name}: its outline at the written sizes "
                      f"({', '.join(written)})", layer=main.dxf.layer, handle=main.dxf.handle, text=t.text[:60])
        hung = []
        for e, q in polys:
            if e is main or e.dxf.layer != main.dxf.layer:
                continue
            qx0, qy0, qx1, qy1 = _bbox(q)
            if abs(qy1 - box[1]) <= tol and qx0 >= box[0] - tol and qx1 <= box[2] + tol:
                hung.append(f"a part hangs below it ({'left' if qx0 - box[0] < box[2] - qx1 else 'right'} side), "
                            "its depth not written (to L.L.: lintel level?) - not modelled")
        b.add(Element(id=f"{rec.id}/beam_section/{len(b.elements)}", type="beam_section", sheet=rec.id,
                      level=rec.levels[0] if len(getattr(rec, "levels", []) or []) == 1 else "",
                      wing=getattr(rec, "wing", ""), mark=name,
                      props={"width": Value(round(W, 1), READ, 1.0, [ev]), "depth": Value(round(D, 1), READ, 1.0, [ev])},
                      extra={"profile": prof, "title": t.text[:60],
                             "undimensioned": (["across"] if loose_x else []) + (["up"] if loose_y else []),
                             "hung": hung}))
        n += 1
    if unread:
        n += _sketch_sections(b, rec, texts, unread, ents)
    return n


_TITLED: dict = {}


def _titled(doc) -> set:
    """The section names the whole drawing titles ("SECTION :- X-X" -> X), once per drawing."""
    if id(doc) not in _TITLED:
        names = set()
        for e in doc.modelspace().query("TEXT MTEXT"):
            try:
                s = e.dxf.text if e.dxftype() == "TEXT" else e.plain_text()
            except Exception:
                continue
            m = TITLE.search(re.sub(r"%%[UuOo]", "", s or "").strip())
            if m:
                names.add(m.group(1).upper())
        _TITLED.clear()
        _TITLED[id(doc)] = names
    return _TITLED[id(doc)]


def read_cuts(b: Building, rec) -> int:
    """The letters marking a section on the plan: the same letter at both ends of a short
    cut across a beam (X at 797421,-122274 and 797495,-124012 on the Main Block plan).
    Only letters the drawing titles a section with: room and label letters are not cuts."""
    doc = getattr(rec, "doc", None)
    if doc is None:
        return 0
    named = _titled(doc)
    letters = [t for t in rec.texts if t.space == "model" and LETTER.match(t.text) and "GRID" not in t.layer.upper()
               and t.text.strip().upper() in named]
    by = {}
    for t in letters:
        by.setdefault(t.text.strip().upper(), []).append(t)
    n, used = 0, set()
    for name, ts in by.items():
        for i, p in enumerate(ts):
            if id(p) in used:
                continue
            th = max(p.height or 0.0, 1.0)
            pc = ((p.cx or p.x), (p.cy or p.y))
            best = None
            for q in ts[i + 1:]:
                if id(q) in used:
                    continue
                qc = ((q.cx or q.x), (q.cy or q.y))
                d = math.dist(pc, qc)
                aligned = min(abs(pc[0] - qc[0]), abs(pc[1] - qc[1])) <= 1.0 * th
                if aligned and 2.0 * th <= d <= 30.0 * th and (best is None or d < best[0]):
                    best = (d, q, qc)
            if best is None:
                continue
            used.update((id(p), id(best[1])))
            ev = Evidence(reader=READER, sheet=rec.id, source=f"section {name}-{name} marked on the plan",
                          layer=p.layer, handle=p.handle, text=name)
            b.add(Element(id=f"{rec.id}/section_cut/{len(b.elements)}", type="section_cut", sheet=rec.id,
                          mark=name, geometry={"a": list(pc), "b": list(best[2])},
                          props={"name": Value(name, READ, 0.9, [ev])}))
            n += 1
    return n


# ---- sections drawn as loose lines, dimensioned with DIMENSION entities --------------
#
# NIFT (A1 block) draws its beam sections line by line, the beam hanging under the slab
# with no line across its top ("SECTION A-A": FLOOR BEAM, SLAB R/F.), at four times full
# size with real dimensions (DIMLFAC 0.25) - and writes only the slab: 150 thick, 150
# past the beam. Such a section is read only where the reader above finds none, and is
# added after every drawing is read (add_late), so no other element's id changes.

SKETCH_SKIP = re.compile(r"REINF|BAR|DIM|TEXT|TXT|HATCH|SYM|LEADER|TITLE|\bTIT\b|GRID|CIR|DEFPOINT|LEGEND|"
                         r"BORDER|COL|WALL|STAIR", re.I)
NOT_A_BEAM = re.compile(r"\bWALL\b|RET\.?\s*WALL|\bSTUB\b|\bCOL(?:UMN)?\b|FOOTING|\bRAFT\b|\bPCC\b|PILE", re.I)
_LATE: dict[int, list] = {}


def _segments(ents) -> list[tuple]:
    """Horizontal and vertical pieces of the lines a section is drawn with: (x0, y0, x1, y1, layer)."""
    out = []
    for e in ents:
        if e.dxftype() not in ("LINE", "LWPOLYLINE") or SKETCH_SKIP.search(e.dxf.layer):
            continue
        if e.dxftype() == "LINE":
            segs = [((e.dxf.start.x, e.dxf.start.y), (e.dxf.end.x, e.dxf.end.y))]
        else:
            pts = [(p[0], p[1]) for p in e.get_points("xy")]
            if e.closed and pts:
                pts.append(pts[0])
            segs = list(zip(pts, pts[1:]))
        for (x0, y0), (x1, y1) in segs:
            if (abs(x0 - x1) < 1.0) != (abs(y0 - y1) < 1.0):
                out.append((x0, y0, x1, y1, e.dxf.layer))
    return out


def _dim_spans(ents) -> list[tuple]:
    """(axis, drawn a, drawn b, value shown, entity) for each DIMENSION along x or y."""
    from .details import dimension_reading
    out = []
    for e in ents:
        if e.dxftype() != "DIMENSION":
            continue
        shown, drawn, _ = dimension_reading(e)
        p, q = e.dxf.get("defpoint2", None), e.dxf.get("defpoint3", None)
        if not shown or p is None or q is None:
            continue
        if (e.dimtype & 0x0F) == 0:
            ang = (e.dxf.get("angle", 0.0) or 0.0) % 180.0
            axis = "x" if min(ang, 180 - ang) < 5 else "y" if abs(ang - 90) < 5 else ""
        else:
            dx, dy = abs(q.x - p.x), abs(q.y - p.y)
            axis = "x" if dx > 3 * dy else "y" if dy > 3 * dx else ""
        if axis:
            a, c = (p.x, q.x) if axis == "x" else (p.y, q.y)
            out.append((axis, a, c, float(shown), float(drawn or 0.0), e))
    return out


def _face(segs):
    """The beam's outline: the face standing on the lowest horizontal line, its sides carried
    up to the slab's top where the beam hangs under it (no line across the beam's top).
    Returns (corner points, every vertex on its outline, share of it really drawn) or None."""
    from shapely.geometry import LineString
    from shapely.ops import polygonize, unary_union
    # corners drawn a hair apart (1075844.013 and 1075844.0129) are one corner
    sx = _cluster([v for s in segs for v in (s[0], s[2])], 1.0)
    sy = _cluster([v for s in segs for v in (s[1], s[3])], 1.0)
    segs = [(sx[s[0]], sy[s[1]], sx[s[2]], sy[s[3]], s[4]) for s in segs]
    hz = [s for s in segs if abs(s[1] - s[3]) < 1.0]
    vt = [s for s in segs if abs(s[0] - s[2]) < 1.0]
    if not hz or len(vt) < 2:
        return None
    lines = [LineString([(s[0], s[1]), (s[2], s[3])]) for s in segs]
    drawn = unary_union(lines)
    extra = []
    for x0, y0, x1, y1, _ in vt:
        top = max(y0, y1)
        over = [h[1] for h in hz if h[1] > top + 1.0 and min(h[0], h[2]) - 1.0 <= x0 <= max(h[0], h[2]) + 1.0]
        if over:
            extra.append(LineString([(x0, top), (x0, min(over))]))
    faces = list(polygonize(unary_union(lines + extra)))
    low = min(h[1] for h in hz)
    base = [h for h in hz if abs(h[1] - low) < 1.0]
    on_base = [f for f in faces if abs(f.bounds[1] - low) < 1.0 and f.area > 0
               and any(f.boundary.distance(LineString([(h[0], h[1]), (h[2], h[3])]).interpolate(0.5, normalized=True)) < 1.0
                       for h in base)]
    if not on_base:
        return None
    f = max(on_base, key=lambda g: g.area)
    ring = list(f.exterior.coords)[:-1]
    share = f.exterior.intersection(drawn.buffer(1.0)).length / max(f.exterior.length, 1.0)
    corners = []
    for i, p in enumerate(ring):
        a, c = ring[i - 1], ring[(i + 1) % len(ring)]
        if abs((p[0] - a[0]) * (c[1] - p[1]) - (p[1] - a[1]) * (c[0] - p[0])) > 1e-6 * max(f.length, 1.0) ** 2:
            corners.append(p)
    return corners, ring, share


def _sketch_sections(b: Building, rec, texts, titles, ents) -> int:
    """Titles the outline reader found nothing under, read as loose lines (see above)."""
    segs, dims = _segments(ents), _dim_spans(ents)
    n = 0
    for t, name in titles:
        th = max(t.height or 0.0, 1.0)
        tx, ty = (t.cx or t.x), (t.cy or t.y)

        def near(x, y):
            return abs(x - tx) <= 15.0 * th and 0.0 <= y - ty <= 30.0 * th
        mine = [s for s in segs if near(s[0], s[1]) and near(s[2], s[3])]
        words = " ".join(x.text for x in texts if x is not t and near(x.cx or x.x, x.cy or x.y))
        if not mine or (NOT_A_BEAM.search(words) and "BEAM" not in words.upper()):
            continue
        got = _face(mine)
        if got is None:
            continue
        corners, ring, share = got
        if len(corners) < 4 or share < 0.75 or not any("BEAM" in s[4].upper() for s in mine):
            continue
        x0, y0, x1, y1 = _bbox(ring)
        if not (x1 - x0 > th and y1 - y0 > th and x1 - x0 <= 30.0 * th and y1 - y0 <= 30.0 * th):
            continue
        pad = 8.0 * th
        mine_d = [d for d in dims if all(x0 - pad <= v <= x1 + pad for v in (d[5].dxf.defpoint2.x, d[5].dxf.defpoint3.x))
                  and all(y0 - pad <= v <= y1 + pad for v in (d[5].dxf.defpoint2.y, d[5].dxf.defpoint3.y))]
        scales = sorted(d[3] / d[4] for d in mine_d if d[4] > 0)
        scale = scales[len(scales) // 2] if scales else None
        # the dimensions are snapped to the lines: a 375 ending 200 units off a corner
        # (NIFT 08, B-B, the sunk slab's step) is not that corner's
        tol = max(0.01 * max(x1 - x0, y1 - y0), 5.0)
        xs = _cluster([p[0] for p in ring], tol / 2)
        ys = _cluster([p[1] for p in ring], tol / 2)
        cx, cy = sorted(set(xs.values())), sorted(set(ys.values()))
        loose = []
        solved = []
        for axis, coords in (("x", cx), ("y", cy)):
            real, left = _solve(coords, [(a, c, v) for ax, a, c, v, _, _ in mine_d if ax == axis], tol)
            if len(real) < 2 or any(c not in real for c in coords):
                if not scale:
                    break
                real = {c: (c - coords[0]) * scale for c in coords}      # measured at the dimensions' scale
                left = coords[1:]
            if left:
                loose.append("across" if axis == "x" else "up")
            solved.append(real)
        if len(solved) < 2:
            continue
        rx, ry = solved
        top = max(ry.values())
        prof = [[round(rx[xs[p[0]]] - min(rx.values()), 1), round(ry[ys[p[1]]] - top, 1)] for p in corners]
        W = max(p[0] for p in prof) - min(p[0] for p in prof)
        D = -min(p[1] for p in prof)
        if W <= 0 or D <= 0:
            continue
        full = [p[0] for p in prof if p[1] > -1.0]                     # its top: the full-height part
        flanges, slab_t = _flanges(mine, (x0, y0, x1, y1), mine_d, scale)
        written = sorted({f"{d[3]:.0f}" for d in mine_d})
        ev = Evidence(reader=READER, sheet=rec.id, source=f"section {name}-{name}: drawn with lines"
                      + (f", dimensions {', '.join(written)}" if written else ""),
                      layer=mine[0][4], text=t.text[:60])
        _LATE.setdefault(id(b), []).append(Element(
            id="", type="beam_section", sheet=rec.id,
            level=rec.levels[0] if len(getattr(rec, "levels", []) or []) == 1 else "",
            wing=getattr(rec, "wing", ""), mark=name,
            props={"width": Value(round(W, 1), READ, 1.0, [ev]), "depth": Value(round(D, 1), READ, 1.0, [ev])},
            extra={"profile": prof, "title": t.text[:60], "undimensioned": loose, "hung": [],
                   "drawn_as": "lines", "scale": scale,
                   "full_width": round(max(full) - min(full), 1) if len(full) >= 2 else round(W, 1),
                   "rectangle": len(corners) == 4, "flanges": flanges, "slab_thickness": slab_t}))
        n += 1
    return n


def _flanges(segs, box, dims, scale):
    """The slab drawn beside the beam, each side: how far it runs past the beam's face, and
    whether that end is drawn (a closed edge - NIFT 5th floor A-A: 150 out, 300 in) or left
    open (the slab carrying on). Sizes as written, else at the dimensions' scale.
    ([{"side", "width", "closed", "written"}], {"mm", "written"} or None)"""
    x0, _y0, x1, top = box
    hz = [s for s in segs if abs(s[1] - s[3]) < 1.0]
    vt = [s for s in segs if abs(s[0] - s[2]) < 1.0]
    on_top = [s for s in hz if abs(s[1] - top) < 1.0]
    if not on_top or not scale:
        return [], None

    def written(axis, a, c):
        return next((d[3] for d in dims if d[0] == axis and abs(min(d[1], d[2]) - min(a, c)) <= 5.0
                     and abs(max(d[1], d[2]) - max(a, c)) <= 5.0), None)
    out, soffits = [], []
    for side, face, end in (("left", x0, min(min(s[0], s[2]) for s in on_top)),
                            ("right", x1, max(max(s[0], s[2]) for s in on_top))):
        if abs(end - face) < 1.0:
            continue
        # the slab's underside meeting this face: the highest line under the top reaching it
        under = [s[1] for s in hz if s[1] < top - 1.0 and min(s[0], s[2]) - 1.0 <= face <= max(s[0], s[2]) + 1.0
                 and (min(s[0], s[2]) < face - 1.0 if side == "left" else max(s[0], s[2]) > face + 1.0)]
        if not under:
            continue
        soffit = max(under)
        soffits.append(soffit)
        closed = any(abs(s[0] - end) < 1.0 and min(s[1], s[3]) <= soffit + 1.0 and max(s[1], s[3]) >= top - 1.0
                     for s in vt)
        w = written("x", face, end)
        out.append({"side": side, "width": round(w if w else abs(end - face) * scale, 1), "closed": closed,
                    "written": w is not None})
    if not soffits:
        return out, None
    soffit = max(soffits)
    t = written("y", soffit, top)
    return out, {"mm": round(t if t else (top - soffit) * scale, 1), "written": t is not None}


def add_late(b: Building) -> int:
    """The sections read from loose lines, into the model once every drawing is read."""
    late = _LATE.pop(id(b), [])
    for e in late:
        e.id = f"{e.sheet}/beam_section/L{len(b.elements)}"
        b.add(e)
    return len(late)


# ---- the grids a beam detail spans, and the grid labels on a plan ------------------
#
# NIFT draws each beam as an elevation under a row of grid labels - 2B5 over (A)Y11' to
# (A)Y17 - its caption under it, and marks the beam on the plan only once. Kept here
# (in extras, nothing counted or added), for linker._detail_span_marks.

GRID_LABEL = re.compile(r"^(?:\([A-Z0-9]{1,2}\))?\s*(?:[A-Z]{1,2}\s?\d{1,3}|[A-Z]{1,2}|\d{1,3})'{0,2}$")


def grid_key(s: str) -> str:
    return re.sub(r"\s+", "", (s or "").upper())


def _axis(s: str) -> str:
    """Which family of grid lines a label names: Y11' and Y12 one, X2 another, A / 1 by kind."""
    core = re.sub(r"^\([A-Z0-9]{1,2}\)", "", grid_key(s)).rstrip("'")
    m = re.match(r"^([A-Z]*)", core)
    return m.group(1) if m.group(1) and any(ch.isdigit() for ch in core) else ("A" if core.isalpha() else "9")


def _number(s: str):
    """A grid label's number: (A)Y11' -> 11, X2 -> 2, A -> None."""
    m = re.search(r"(\d+)'*$", re.sub(r"^\([A-Z0-9]{1,2}\)", "", grid_key(s)))
    return int(m.group(1)) if m else None


def _grid_labels(texts) -> list:
    """Texts shaped like grid labels - not the '0' a bar's diameter sign is written as (NIFT)."""
    return [t for t in texts if t.space == "model" and GRID_LABEL.match(t.text.strip()) and (t.height or 0) > 0
            and not re.fullmatch(r"0+", t.text.strip())]


def _detail_grid_rows(b: Building, rec) -> None:
    """Each beam caption of a detail sheet ('2B5 (300X600)'): the grid labels of the row
    above it, in its stretch of that row - spec.extra["grid_span"] = ["(A)Y11'", ...]."""
    specs = [s for s in b.of("spec", sheet=rec.id) if s.get("family") == "beam" and s.geometry.get("at")]
    labels = _grid_labels(rec.texts)
    if not specs or len(labels) < 2:
        return
    named = [t for t in labels if re.search(r"[A-Z]", t.text.upper())] or labels
    hl = sorted(t.height for t in named)[len(named) // 2]
    labels = [t for t in labels if abs(t.height - hl) <= 0.1 * hl]
    rows: list[list] = []
    for t in sorted(labels, key=lambda t: t.y):
        if rows and t.y - rows[-1][-1].y <= 2.0 * hl:
            rows[-1].append(t)
        else:
            rows.append([t])
    groups = []                                     # (row y, [labels]) - one elevation's stretch
    for row in rows:
        row.sort(key=lambda t: t.x)
        gaps = sorted(c.x - a.x for a, c in zip(row, row[1:]))
        step = gaps[len(gaps) // 2] if gaps else 0.0
        cur = [row[0]]
        for a, c in zip(row, row[1:]):
            # a new elevation: another family of grids, a wide gap, a label again (three
            # beams over X1-X3 side by side), or the numbering jumping (X19 then X1)
            na, nc = _number(a.text), _number(c.text)
            jump = na is not None and nc is not None and abs(nc - na) > 3
            if _axis(c.text) != _axis(a.text) or (step and c.x - a.x > 2.5 * step) or jump \
                    or grid_key(c.text) in {grid_key(t.text) for t in cur}:
                groups.append((sum(t.y for t in row) / len(row), cur))
                cur = []
            cur.append(c)
        groups.append((sum(t.y for t in row) / len(row), cur))
    groups = [(y, g) for y, g in groups if len(g) >= 2]
    for s in specs:
        x, y = s.geometry["at"]
        hc = max(s.geometry.get("text_height") or hl, 1.0)
        above = sorted((gy, g) for gy, g in groups if 4.0 * hc < gy - y <= 60.0 * hc
                       and g[0].x - 3.0 * hl <= x <= g[-1].x + 3.0 * hl)
        if above:
            s.extra["grid_span"] = [t.text.strip() for t in above[0][1]]


def _plan_grid_labels(b: Building, rec) -> None:
    """The grid labels written on a plan, with where they are: sheet.extra["grid_labels"]."""
    if rec.id not in b.sheets:
        return
    # at the text's middle - its bubble's - not its start (NIFT: '(A)Y18' starts 500 left of Y18)
    got = [[t.text.strip(), round(t.cx if getattr(t, "cx", None) is not None else t.x, 1),
            round(t.cy if getattr(t, "cy", None) is not None else t.y, 1), round(t.height, 1)]
           for t in _grid_labels(rec.texts)]
    if got:
        b.sheets[rec.id].extra["grid_labels"] = got


def read(b: Building, rec) -> int:
    kind = getattr(getattr(rec, "verdict", None), "kind", "")
    if kind in SECTION_KINDS:
        n = read_sections(b, rec)
        _detail_grid_rows(b, rec)
        return n
    if kind in PLAN_KINDS:
        _plan_grid_labels(b, rec)
        return read_cuts(b, rec)
    return 0
