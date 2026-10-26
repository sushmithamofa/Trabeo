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


def read(b: Building, rec) -> int:
    kind = getattr(getattr(rec, "verdict", None), "kind", "")
    if kind in SECTION_KINDS:
        return read_sections(b, rec)
    if kind in PLAN_KINDS:
        return read_cuts(b, rec)
    return 0
