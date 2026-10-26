"""One beam drawn with more than two lines is one beam, not two.

A beam whose top steps or slopes is drawn with a line along it where the top changes
(Sasan Gir Main Block terrace: the 750 edge beams of SECTION X-X / Y-Y / Z-Z, outer
face solid, slope line and inner face dashed). The plan reader pairs that middle line
with each face and gives two beams side by side, sharing a face - 230 and 520 wide,
each cut into spans at different places, so the edge was built as staggered pieces.

Two parallel beams touching face to face, side by side along most of their length,
are one beam: concrete beams do not stand against each other sharing a face - but
only joined where the drawing says the beam is that wide: a drawn section of the
joined width, its cut letters across the lines (the Main Block corridor's two beams,
paired across the 610 gap between them by the plan reader, are not one beam). They
are joined into one beam across both - outer face to outer face - and cut only where
a column stands under it (a beam spans support to support). The plan reader's own
beams are kept, marked merged_into, and not modelled; the joined beams are generated
and carry everything else (marks, sizes, sections, the review, the export).
"""
from __future__ import annotations

import math

from .model import Building, Element, Evidence, Value, READ

READER = "merge_beams"
TOUCH_MM = 25.0              # faces this close count as one shared face
PARALLEL = math.sin(math.radians(1.0))


def gone(e) -> bool:
    """A beam the model does not hold itself: split into spans, or joined into another."""
    return bool(e.extra.get("split_into") or e.extra.get("merged_into"))


def _frame(e):
    (ax, ay), (bx, by) = e.geometry["start"][:2], e.geometry["end"][:2]
    L = math.hypot(bx - ax, by - ay)
    return (ax, ay), ((bx - ax) / L, (by - ay) / L), L


def _width(e) -> float:
    try:
        return float(e.get("width") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def merge(b: Building, stats) -> None:
    # made again from scratch on every link
    b.elements = [e for e in b.elements if not e.extra.get("merged_from")]
    for e in b.elements:
        e.extra.pop("merged_into", None)
    # only where the drawing says the beam is that wide: a drawn section of that width,
    # its cut letters across the lines (Main Block corridor: two beams 610 apart, wrongly
    # paired across the gap by the plan reader, are not one 1070 beam)
    widths = {}
    for sec in b.of("beam_section"):
        set_ = b.sheets[sec.sheet].extra.get("set", "") if sec.sheet in b.sheets else ""
        try:
            widths[(set_, sec.mark)] = float(sec.get("width") or 0.0)
        except (TypeError, ValueError):
            pass
    cuts = {}
    for c in b.of("section_cut"):
        cuts.setdefault(c.sheet, []).append(c)
    for sid, s in b.sheets.items():
        if s.reader != "plan.legacy":
            continue
        marked = [(c.geometry["a"], c.geometry["b"], widths[(s.extra.get("set", ""), c.mark)])
                  for c in cuts.get(s.extra.get("view_sheet", ""), []) if (s.extra.get("set", ""), c.mark) in widths]
        if not marked:
            continue
        beams = [e for e in b.elements if e.sheet == sid and e.type == "beam" and not e.geometry.get("mid")
                 and e.geometry.get("start") and e.geometry.get("end") and _width(e) > 0
                 and math.dist(e.geometry["start"][:2], e.geometry["end"][:2]) > 100.0]
        if len(beams) < 2:
            continue
        # what a beam is cut at: columns, and columns drawn as odd shapes (read as walls)
        cols = [e for e in b.elements if e.sheet == sid and ((e.type == "column" and e.geometry.get("x") is not None)
                or (e.type == "wall" and len(e.geometry.get("outline") or []) >= 3))]
        parent = list(range(len(beams)))

        def root(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i
        fr = [_frame(e) for e in beams]
        for i in range(len(beams)):
            (ox, oy), (ux, uy), Li = fr[i]
            for j in range(i + 1, len(beams)):
                _, (vx, vy), Lj = fr[j]
                if abs(ux * vy - uy * vx) > PARALLEL:
                    continue
                g = beams[j].geometry
                mx, my = (g["start"][0] + g["end"][0]) / 2.0, (g["start"][1] + g["end"][1]) / 2.0
                s = (mx - ox) * -uy + (my - oy) * ux
                t = sorted(((p[0] - ox) * ux + (p[1] - oy) * uy) for p in (g["start"], g["end"]))
                if abs(s) <= 5.0 and abs(_width(beams[i]) - _width(beams[j])) <= 5.0:
                    # the same line cut into pieces (at columns, or where marks change): one
                    # string - it only joins with another string beside it (checked in _join)
                    if t[0] <= Li + 600.0 and t[1] >= -600.0:
                        parent[root(i)] = root(j)
                    continue
                if abs(abs(s) - (_width(beams[i]) + _width(beams[j])) / 2.0) > TOUCH_MM:
                    continue
                over = min(Li, t[1]) - max(0.0, t[0])
                if over >= max(300.0, 0.3 * min(Li, Lj)):
                    parent[root(i)] = root(j)
        groups = {}
        for i in range(len(beams)):
            groups.setdefault(root(i), []).append(i)
        made = 0
        for idx in sorted(groups.values(), key=lambda g: min(g)):
            if len(idx) < 2:
                continue
            made += _join(b, sid, [beams[i] for i in idx], cols, made, marked)
        if made:
            stats["beams drawn with several lines, joined"] += made


def _crosses(p1, p2, q1, q2) -> bool:
    def side(a, b, c):
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
    return side(p1, p2, q1) * side(p1, p2, q2) < 0 and side(q1, q2, p1) * side(q1, q2, p2) < 0


def _join(b: Building, sid: str, grp, cols, n0: int, marked=()) -> int:
    ref = max(grp, key=lambda e: _frame(e)[2])
    (ox, oy), (ux, uy), _ = _frame(ref)
    nx, ny = -uy, ux
    parts = []
    for e in grp:
        g = e.geometry
        t = sorted(((p[0] - ox) * ux + (p[1] - oy) * uy) for p in (g["start"], g["end"]))
        mx, my = (g["start"][0] + g["end"][0]) / 2.0, (g["start"][1] + g["end"][1]) / 2.0
        parts.append((e, t[0], t[1], (mx - ox) * nx + (my - oy) * ny, _width(e)))
    if len({round(s) for _, _, _, s, _ in parts}) < 2:
        return 0                                   # one line cut into pieces: not this
    # where the lines lie side by side: that stretch is the one beam; a stretch where only
    # one of them goes on, between two side-by-side stretches, is still it (a face line
    # the plan reader used elsewhere); one going on alone at either end is not
    steps = sorted({t for _, t0, t1, _, _ in parts for t in (t0, t1)})
    zones = []
    for p, q in zip(steps, steps[1:]):
        m = (p + q) / 2.0
        if len({round(s) for _, t0, t1, s, _ in parts if t0 <= m <= t1}) >= 2:
            if zones and p - zones[-1][1] <= 1.0:
                zones[-1][1] = q
            else:
                zones.append([p, q])
    if not zones:
        return 0

    def covered(p, q):                             # some line of the group runs all along p..q
        pts = sorted((t0, t1) for _, t0, t1, _, _ in parts if t1 > p and t0 < q)
        at = p
        for t0, t1 in pts:
            if t0 > at + 600.0:
                return False
            at = max(at, t1)
        return at >= q - 600.0
    runs = [zones[0][:]]
    for z in zones[1:]:
        if covered(runs[-1][1], z[0]):
            runs[-1][1] = z[1]
        else:
            runs.append(z[:])
    for r in runs:                                 # a piece's end just short of the stretch (a column face)
        r[0] = min([r[0]] + [t0 for _, t0, _, _, _ in parts if r[0] - 600.0 <= t0 < r[0]])
        r[1] = max([r[1]] + [t1 for _, _, t1, _, _ in parts if r[1] < t1 <= r[1] + 600.0])
    total = sum(r[1] - r[0] for r in runs)
    if total < 1000.0:
        return 0
    # the pieces that ARE the joined beam: largely within a run (40 % - Main Block south
    # edge: a 4.4 m piece 2 m alongside, crossed by the Z-Z cut); the run then covers them
    # whole. The rest stay as drawn (a long line merely passing by is not part of it).
    def along(p):
        return sum(max(0.0, min(p[2], r[1]) - max(p[1], r[0])) for r in runs)
    inside = [p for p in parts if along(p) >= 0.4 * (p[2] - p[1])]
    for r in runs:
        for _, t0, t1, _, _ in inside:
            if min(t1, r[1]) - max(t0, r[0]) > 0:
                r[0], r[1] = min(r[0], t0), max(r[1], t1)
    if len({round(p[3]) for p in inside}) < 2:
        return 0
    # face to face across the whole width, no gap: else it is not one beam
    edges = sorted({(round(s - w / 2.0), round(s + w / 2.0)) for _, _, _, s, w in inside})
    reach = edges[0][1]
    for a, z in edges[1:]:
        if a > reach + TOUCH_MM:
            return 0
        reach = max(reach, z)
    lo = min(s - w / 2.0 for _, _, _, s, w in inside)
    hi = max(s + w / 2.0 for _, _, _, s, w in inside)
    grp = [p[0] for p in inside]
    # a section of this width marked across these lines says they are one beam
    if not any(abs((hi - lo) - W) <= max(60.0, 0.1 * W) and
               any(_crosses(a, c, e.geometry["start"], e.geometry["end"]) for e in grp)
               for a, c, W in marked):
        return 0
    c = (lo + hi) / 2.0
    marks = sorted({e.mark for e in grp if e.mark})
    depths = {e.get("depth") for e in grp if e.get("depth")}
    kinds = {e.get("kind") for e in grp if e.get("kind")}
    made, ids = 0, []
    for t0, t1 in runs:
        cuts = []
        for col in cols:
            if col.type == "column":
                cx, cy = col.geometry["x"], col.geometry["y"]
                half = 0.5 * max(float(col.get("width") or 0.0), float(col.get("depth") or 0.0))
                t = (cx - ox) * ux + (cy - oy) * uy
                s = (cx - ox) * nx + (cy - oy) * ny
                if t0 + 300.0 < t < t1 - 300.0 and lo - half <= s <= hi + half:
                    cuts.append(t)
                continue
            # a column drawn as an odd shape (read as a wall): short along the beam, across its band
            pts = col.geometry["outline"]
            ts = [(p[0] - ox) * ux + (p[1] - oy) * uy for p in pts]
            ss = [(p[0] - ox) * nx + (p[1] - oy) * ny for p in pts]
            if max(ts) - min(ts) <= 1500.0 and min(ss) <= hi and max(ss) >= lo:
                t = (min(ts) + max(ts)) / 2.0
                if t0 + 300.0 < t < t1 - 300.0:
                    cuts.append(t)
        edges = [t0] + sorted(cuts) + [t1]
        for p, q in zip(edges, edges[1:]):
            if q - p < 100.0:
                continue
            eid = f"{sid}/beam/joined{n0 + made}"
            ev = Evidence(reader=READER, sheet=sid, source=f"{len(grp)} lines drawn for one beam, face to face "
                          f"{hi - lo:.0f}", layer=ref.extra.get("_layer", ""), handle=ref.extra.get("_handle", ""))
            props = {"width": Value(round(hi - lo, 1), READ, 1.0, [ev]),
                     "kind": Value(next(iter(kinds)) if len(kinds) == 1 else "beam", READ, 1.0, [ev])}
            if len(depths) == 1:
                props["depth"] = Value(next(iter(depths)), READ, 1.0, [ev])
            b.add(Element(id=eid, type="beam", sheet=sid, level=ref.level, wing=ref.wing,
                          mark=marks[0] if len(marks) == 1 else "",
                          geometry={"start": [ox + ux * p + nx * c, oy + uy * p + ny * c],
                                    "end": [ox + ux * q + nx * c, oy + uy * q + ny * c]},
                          props=props,
                          extra={"generated": True, "merged_from": [e.id for e in grp],
                                 "_layer": ref.extra.get("_layer", ""), "_handle": ref.extra.get("_handle", "")}))
            ids.append(eid)
            made += 1
    for e in grp:
        e.extra["merged_into"] = ids
    return made
