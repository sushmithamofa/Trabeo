"""Graphical column schedules: a drawn section per column mark and level range.

Sasan Gir's "COL SCH" sheet is drawn, not written:

    M30                               |   Z1 MAIN LINK  Z2 LINKS  ...
    FF SLAB LVL  TO  TERRACE LVL      |   [section]   [section]   ...    one band per
    (M30)                             |   10-T16+6-T12   8-T20+10-T16    level range
    ... more bands ...
    COLUMN MARKED                     |   C1          C2          ...    the mark row

No size is written as text anywhere. Each size is the drawn section's own
dimensions, read as they are shown: the sections are drawn four times full size
with a dimension scale of 0.25, so a 2400 line reads 600 (details.dimension_reading).

The table is found from its header ("COLUMN MARKED", "COL. MARK", "COLUMN NO.")
and the column marks in line with it. Each mark's cell is the strip of the table
under or over it; each band is a row of the table, cut by its grid lines where the
drawing has them, otherwise around its level label. A size is read only when the
cell holds one outermost section outline and a dimension on each side whose drawn
length matches that outline. A section without dimensions is reported, never sized
by assuming a drawing scale. A mark with no section in a band has no column in that
range (C1 stops at GF): that is recorded, not reported.

Bars (10-T16+6-T12) and links (T8 @ 100, with their zone) in the cell are kept as
written. Shear walls and cores drawn on the same sheet without a mark row are left
for the wall reader (Stage 6).
"""
from __future__ import annotations

import math
import re

from ..marks import grouped, parse_mark
from ..model import Building, Element, Evidence, Issue, Value, READ, WARNING
from ..sheets.levels import level_rank, levels_in
from .details import dimension_reading

READER = "column_schedule"
KINDS = ("column_schedule", "column_details")
HEADER = re.compile(r"^\s*(?:COLUMN|COL\.?)\s*(?:MARK(?:ED|S)?|NO\.?S?|REF\.?|TYPE|ID)\s*\.?\s*$", re.I)
OUTLINE_SKIP = re.compile(r"REBAR|LINK|BAR|DIM|TEXT|TXT|HATCH|GRID|LEADER|DEFPOINT|TITLE|BORDER", re.I)
BARS = re.compile(r"\b\d+\s*-\s*[TY#]\s?\d{1,2}\b", re.I)
LINKS = re.compile(r"\b[TY#]\s?\d{1,2}\s*@\s*\d{2,3}\b", re.I)
ZONE = re.compile(r"\bZ\s?\d\b|MAIN\s+LINK|OTHER\s+LINKS?|LINKS?$", re.I)
GRADE = re.compile(r"\bM\s?(\d{2})\b")
MATCH = 0.03            # a dimension matches an outline side within 3% of its length


def _outlines(ents) -> list[dict]:
    """Closed outlines (any number of sides): their box and side lengths."""
    out = []
    for e in ents:
        if e.dxftype() not in ("LWPOLYLINE", "POLYLINE") or OUTLINE_SKIP.search(e.dxf.layer):
            continue
        try:
            pts = [(p[0], p[1]) for p in e.get_points("xy")] if e.dxftype() == "LWPOLYLINE" else \
                [(v.dxf.location.x, v.dxf.location.y) for v in e.vertices]
        except Exception:
            continue
        closed = getattr(e, "closed", False) or getattr(e, "is_closed", False) or \
            (len(pts) > 3 and math.hypot(pts[0][0] - pts[-1][0], pts[0][1] - pts[-1][1]) < 1.0)
        if len(pts) < 4 or not closed:
            continue
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        ring = pts + [pts[0]]
        sides = [math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(ring, ring[1:])]
        out.append({"box": (min(xs), min(ys), max(xs), max(ys)), "sides": [s for s in sides if s > 1.0],
                    "n": len(pts), "handle": e.dxf.handle, "layer": e.dxf.layer, "pts": pts})
    return out


def _dims(ents) -> list[dict]:
    out = []
    for e in ents:
        if e.dxftype() != "DIMENSION":
            continue
        shown, drawn, note = dimension_reading(e)
        if not shown:
            continue
        # drawn may be 0: Villa 5's aligned dimensions measure nothing in ezdxf
        # but display 230 - checked against the outline at the section's scale
        p = e.dxf.get("text_midpoint", None) or e.dxf.get("defpoint", None)
        a, c = e.dxf.get("defpoint2", None), e.dxf.get("defpoint3", None)
        direction = ""
        if a is not None and c is not None and (e.dimtype & 0x0F) == 0:
            # a rotated (linear) dimension measures along its own angle
            ang = (e.dxf.get("angle", 0.0) or 0.0) % 180.0
            direction = "h" if min(ang, 180 - ang) < 5 else "v" if abs(ang - 90) < 5 else ""
        elif a is not None and c is not None:
            dx, dy = abs(c.x - a.x), abs(c.y - a.y)
            direction = "h" if dx > 3 * dy else "v" if dy > 3 * dx else ""
        if p is None:
            continue
        out.append({"shown": shown, "drawn": drawn, "note": note, "x": p.x, "y": p.y,
                    "dir": direction, "handle": e.dxf.handle})
    return out


def _lines(ents) -> tuple[list, list]:
    """Long straight lines: (horizontal [(y, x0, x1)], vertical [(x, y0, y1)])."""
    hz, vt = [], []
    for e in ents:
        segs = []
        if e.dxftype() == "LINE":
            segs = [((e.dxf.start.x, e.dxf.start.y), (e.dxf.end.x, e.dxf.end.y))]
        elif e.dxftype() == "LWPOLYLINE":
            pts = [(p[0], p[1]) for p in e.get_points("xy")]
            if e.closed and pts:
                pts.append(pts[0])
            segs = list(zip(pts, pts[1:]))
        for (x0, y0), (x1, y1) in segs:
            if abs(y0 - y1) < 1.0 and abs(x1 - x0) > 1.0:
                hz.append((y0, min(x0, x1), max(x0, x1)))
            elif abs(x0 - x1) < 1.0 and abs(y1 - y0) > 1.0:
                vt.append((x0, min(y0, y1), max(y0, y1)))
    return hz, vt


def _merged(vt: list, gap: float) -> list:
    """Vertical pieces on one line joined end to end: Villa 5 draws each cell
    border band by band, so the table's border is several pieces."""
    out: list[list[float]] = []
    for x, a, c in sorted(vt, key=lambda v: (round(v[0]), v[1])):
        if out and abs(out[-1][0] - x) < 1.0 and a <= out[-1][2] + gap:
            out[-1][2] = max(out[-1][2], c)
        else:
            out.append([x, a, c])
    return [tuple(v) for v in out]


def _row_cuts(hz, vt, cells, span, top, bottom, h) -> list[float]:
    """Where the table's rows divide: lines across the whole table (Sasan Gir), and
    the points where the cell borders break, at two or more borders (Villa 5)."""
    cuts = [l[0] for l in hz if bottom - h < l[0] < top + h and l[1] <= span[0] + h and l[2] >= span[1]]
    inner = [y for y in cuts if bottom + h < y < top - h]
    edges = sorted({c[0] for c in cells} | {cells[-1][1]})
    width = min(c[1] - c[0] for c in cells)
    ends: dict[int, set] = {}
    for x, a, c in ([] if inner else vt):
        # a border lies on a cell edge and is long; a section's own sides lie well
        # inside a cell, and the little tables of links inside a cell are short
        if c - a < 10 * h or not any(abs(x - e) <= 0.15 * width for e in edges):
            continue
        for y in (a, c):
            if bottom - h < y < top + h:
                ends.setdefault(round(y / h), set()).add(round(x))
    cuts += [k * h for k, xs in ends.items() if len(xs) >= 2]
    cuts += [top, bottom]
    out: list[float] = []
    for y in sorted(cuts):
        if not out or y - out[-1] > h:
            out.append(y)
    return out


def _marks_in_row(texts, hd) -> list[tuple]:
    """(x, mark, text) for each column mark in line with the header; a cell can
    name several (C2,C5  C9,C11,C12,C13  C14,). x is the text's centre: a long
    left-aligned list starts well left of the section it heads."""
    h = max(hd.height, 1.0)
    out, seen = [], set()
    for t in texts:
        if t is hd or abs(t.y - hd.y) > 1.5 * h or t.x <= hd.x:
            continue
        s = t.text.strip().rstrip(",;&/ ")
        mks = grouped(s) or ([parse_mark(s)] if parse_mark(s) else [])
        mks = [m for m in mks if m.family() in ("column", "wall")]
        if not mks:
            continue
        x = t.cx if getattr(t, "cx", None) is not None else t.x
        if (round(x / h), s) in seen:
            continue
        seen.add((round(x / h), s))
        out.append((x, mks, t))
    return sorted(out, key=lambda m: m[0])


def _cells(xs: list[float]) -> list[tuple[float, float]]:
    """Each mark's strip: halfway to its neighbours."""
    out = []
    for i, x in enumerate(xs):
        left = (xs[i - 1] + x) / 2 if i else x - (xs[1] - x) / 2
        right = (xs[i + 1] + x) / 2 if i + 1 < len(xs) else x + (x - xs[i - 1]) / 2
        out.append((left, right))
    return out


def in_range(level: str, rng) -> bool | None:
    """Is a plan at `level` inside a column range [lo, hi]? None if it cannot say.

    A framing plan at a slab shows the columns standing below that slab - from the
    slab under it up to this one. So the GF plan carries the FOUNDATION TO GF
    columns and the TERRACE plan the FF TO TERRACE ones.
    """
    r, lo, hi = level_rank(level), level_rank(rng[0]), level_rank(rng[1])
    if r is None or lo is None or hi is None:
        return None
    if lo == hi:
        return r == lo
    return lo < r <= hi


def read(b: Building, rec) -> dict[str, int]:
    if rec.doc is None or rec.verdict.kind not in KINDS:
        return {}
    hs = set(rec.view.handles)
    ents = [e for e in rec.doc.modelspace() if e.dxf.handle in hs]
    texts = [t for t in rec.texts if t.space == "model"]
    headers = [t for t in texts if HEADER.match(t.text)]
    if not headers and not any(levels_in(t.text) for t in texts):
        return {}
    outlines, dims = _outlines(ents), _dims(ents)
    hz, vt = _lines(ents)
    made = {"column schedule specs": 0, "column schedule cells without size": 0}
    summary = []
    _wall_tables(b, rec, texts, outlines, dims, hz, vt, headers, made)
    for hd in headers:
        h = max(hd.height, 1.0)
        marks = _marks_in_row(texts, hd)
        if len(marks) < 2:
            continue
        xs = [m[0] for m in marks]
        cells = _cells(xs)
        label_x = (hd.x - 4 * h, cells[0][0])
        span = (hd.x, xs[-1])

        # the table's height: its borders that cross the header row
        borders = [v for v in _merged(vt, h) if hd.x - 8 * h <= v[0] <= cells[-1][1] + 8 * h
                   and v[1] - h <= hd.y <= v[2] + h and v[2] - v[1] > 10 * h]
        if borders:
            top, bottom = max(v[2] for v in borders), min(v[1] for v in borders)
        else:
            top, bottom = _extent_from_labels(texts, headers, hd, label_x)
            if top is None:
                continue
        # rows: from the table's lines, else around each level label
        cuts = _row_cuts(hz, vt, cells, span, top, bottom, h)
        bands = list(zip(cuts, cuts[1:]))
        if len(bands) < 2:
            bands = _bands_from_labels(texts, hd, label_x, top, bottom)

        for y0, y1 in bands:
            if y0 - h <= hd.y <= y1 + h and y1 - y0 < 6 * h:
                continue                                   # the mark row itself
            labels = sorted((t for t in texts if label_x[0] <= t.x <= label_x[1] and y0 <= t.y <= y1),
                            key=lambda t: -t.y)
            label = " ".join(t.text for t in labels)
            lvls = [k for k in levels_in(label) if level_rank(k) is not None]
            if not lvls:
                continue
            rng = (lvls[0], lvls[-1])
            if level_rank(rng[0]) > level_rank(rng[1]):
                rng = (rng[1], rng[0])
            label_ev = Evidence(reader=READER, sheet=rec.id, source="schedule level range",
                                layer=labels[0].layer, handle=labels[0].handle,
                                at=[labels[0].x, labels[0].y], text=label[:80])
            for (_, mks, mt), (cx0, cx1) in zip(marks, cells):
                inside = lambda x, y: cx0 <= x <= cx1 and y0 <= y <= y1  # noqa: E731
                outs = [o for o in outlines if inside((o["box"][0] + o["box"][2]) / 2, (o["box"][1] + o["box"][3]) / 2)
                        and o["box"][2] - o["box"][0] < cx1 - cx0]
                names = "/".join(m.key for m in mks)
                if not outs:
                    summary += [{"mark": m.key, "range": list(rng), "size": None} for m in mks]
                    continue
                o = max(outs, key=lambda o: (o["box"][2] - o["box"][0]) * (o["box"][3] - o["box"][1]))
                got = _size(o, [d for d in dims if inside(d["x"], d["y"])])
                mark_ev = Evidence(reader=READER, sheet=rec.id, source="schedule column mark", layer=mt.layer,
                                   handle=mt.handle, at=[mt.x, mt.y], text=mt.text)
                box_ev = Evidence(reader=READER, sheet=rec.id, source="section outline", layer=o["layer"],
                                  handle=o["handle"], at=[(o["box"][0] + o["box"][2]) / 2,
                                                          (o["box"][1] + o["box"][3]) / 2],
                                  text=f"{o['box'][2] - o['box'][0]:.0f} x {o['box'][3] - o['box'][1]:.0f} drawn")
                if got is None:
                    made["column schedule cells without size"] += 1
                    summary += [{"mark": m.key, "range": list(rng), "size": "drawn, not dimensioned"} for m in mks]
                    b.issues.append(Issue(WARNING, "column schedule",
                                          f"{names} {rng[0]} to {rng[1]}: section drawn but not dimensioned "
                                          f"- size not read", sheet=rec.id, mark=mks[0].key, at=box_ev.at))
                    continue
                (dw, dd), conf, notes = got

                def dim_ev(d):
                    if d.get("measured"):
                        return Evidence(reader=READER, sheet=rec.id, source="section outline, measured",
                                        handle=o["handle"], at=list(box_ev.at), text=f"{d['shown']:.0f}")
                    return Evidence(reader=READER, sheet=rec.id, source="section dimension", handle=d["handle"],
                                    at=[d["x"], d["y"]], text=f"{d['shown']:.0f}")
                evw = [dim_ev(dw), box_ev, mark_ev, label_ev]
                evd = [dim_ev(dd), box_ev, mark_ev, label_ev]
                props = {"family": Value("column", READ, 1.0, [mark_ev]),
                         "width": Value(dw["shown"], READ, dw.get("conf", conf), evw),
                         "depth": Value(dd["shown"], READ, dd.get("conf", conf), evd)}
                g = GRADE.search(label)
                gt = None
                if not g:
                    gt = next((t for t in texts if inside(t.x, t.y) and GRADE.search(t.text)), None)
                    g = GRADE.search(gt.text) if gt else None
                if g:
                    props["grade"] = Value(f"M{g.group(1)}", READ, 0.9, [label_ev] if gt is None else [Evidence(
                        reader=READER, sheet=rec.id, source="grade in cell", layer=gt.layer, handle=gt.handle,
                        at=[gt.x, gt.y], text=gt.text)])
                bars, links = _steel(texts, inside)
                if bars:
                    props["bars"] = Value([t.text for t in bars], READ, 0.8,
                                          [Evidence(reader=READER, sheet=rec.id, source="bar callout",
                                                    layer=t.layer, handle=t.handle, at=[t.x, t.y], text=t.text)
                                           for t in bars])
                if links:
                    props["stirrups"] = Value([{"text": t.text, "zone": z} for t, z in links], READ, 0.8,
                                              [Evidence(reader=READER, sheet=rec.id, source="link callout",
                                                        layer=t.layer, handle=t.handle, at=[t.x, t.y],
                                                        text=t.text) for t, _ in links])
                for mk in mks:
                    b.add(Element(
                        id=f"{rec.id}/spec/{len(b.elements)}", type="spec", sheet=rec.id, level="",
                        wing=rec.wing, mark=mk.key, geometry={"at": list(box_ev.at), "text_height": mt.height},
                        props=dict(props), notes=list(notes),
                        extra={"table": f"column schedule ({rng[0]} to {rng[1]})", "from": "column schedule",
                               "level_range": list(rng), "range_text": label[:80], "part": "",
                               "outline_sides": o["n"], "cell_marks": names}))
                    made["column schedule specs"] += 1
                    summary.append({"mark": mk.key, "range": list(rng),
                                    "size": f"{dw['shown']:.0f} x {dd['shown']:.0f}"})
    if summary and rec.id in b.sheets:
        b.sheets[rec.id].extra["column_schedule"] = summary
    return {k: v for k, v in made.items() if v}


def _wall_tables(b, rec, texts, outlines, dims, hz, vt, headers, made) -> None:
    """Tables of wall and core sections with no mark row (Sasan Gir COL SCH, below
    the column tables): same grid and level bands, but nothing names the walls.

    Each section is read as a size per level range - straight walls as thickness x
    length (2460 x 230, with the boundary elements drawn inside it), a core as its
    outer size and wall thickness - and kept as an unmarked wall spec. Nothing is
    guessed: the linker gives one to a plan wall only when it is the one section of
    that range whose size the wall has.
    """
    from ..walls import shape_of, wall_runs
    lab = [t for t in texts if levels_in(t.text)]
    if not lab:
        return
    h = sorted(t.height for t in lab)[len(lab) // 2] or 200.0
    long_v = [v for v in _merged(vt, h) if v[2] - v[1] > 10 * h]

    def same_span(v, w):
        return abs(v[1] - w[1]) < h and abs(v[2] - w[2]) < h
    for left in long_v:
        right = next((v for v in long_v if 2 * h <= v[0] - left[0] <= 30 * h and same_span(v, left)), None)
        if right is None or any(left[1] - h <= hd.y <= left[2] + h and left[0] - 2 * h <= hd.x <= right[0]
                                for hd in headers):
            continue                                   # not a label column, or a marked table's
        if any(same_span(v, left) and 0 < left[0] - v[0] <= 40 * h for v in long_v):
            continue                                   # a line inside a table, not its left edge
        if not any(left[0] <= t.x <= right[0] and left[1] <= t.y <= left[2] for t in lab):
            continue
        rows = [l for l in hz if left[1] - h < l[0] < left[2] + h and l[1] <= left[0] + h and l[2] > right[0] + 10 * h]
        if not rows:
            continue
        x_end = max(l[2] for l in rows)
        cuts = sorted({round(l[0]) for l in rows} | {round(left[1]), round(left[2])})
        for y0, y1 in zip(cuts, cuts[1:]):
            labels = sorted((t for t in texts if left[0] <= t.x <= right[0] and y0 <= t.y <= y1), key=lambda t: -t.y)
            label = " ".join(t.text for t in labels)
            lvls = [k for k in levels_in(label) if level_rank(k) is not None]
            if len(set(lvls)) < 2:
                continue                               # a band of a wall table names a range: A TO B
            rng = tuple(sorted((lvls[0], lvls[-1]), key=level_rank))
            inband = [o for o in outlines if right[0] <= (o["box"][0] + o["box"][2]) / 2 <= x_end
                      and y0 <= (o["box"][1] + o["box"][3]) / 2 <= y1 and o["box"][2] - o["box"][0] < x_end - right[0]]
            label_ev = Evidence(reader=READER, sheet=rec.id, source="schedule level range", layer=labels[0].layer,
                                handle=labels[0].handle, at=[labels[0].x, labels[0].y], text=label[:80])
            for o in inband:
                inside = [q for q in inband if q is not o and _contains(o["box"], q["box"])]
                if any(q is not o and _contains(q["box"], o["box"]) and q["box"] != o["box"] for q in inband):
                    continue                           # an end zone drawn inside a wall: part of that wall
                x0, ya, x1, yb = o["box"]
                reach = 0.4 * max(x1 - x0, yb - ya)
                near = [d for d in dims if x0 - reach <= d["x"] <= x1 + reach and ya - reach <= d["y"] <= yb + reach]
                got = _size(o, near)
                box_ev = Evidence(reader=READER, sheet=rec.id, source="wall section outline", layer=o["layer"],
                                  handle=o["handle"], at=[(x0 + x1) / 2, (ya + yb) / 2],
                                  text=f"{x1 - x0:.0f} x {yb - ya:.0f} drawn")
                if got is None:
                    made["wall sections without size"] = made.get("wall sections without size", 0) + 1
                    continue
                (dw, dd), conf, notes = got
                scale = dw["shown"] / dw["drawn"] if dw.get("drawn") else 1.0
                a, c = sorted((dw["shown"], dd["shown"]))
                spts = [(p[0] * scale, p[1] * scale) for p in o["pts"]]
                runs = wall_runs(spts)
                xs, ys = [p[0] for p in spts], [p[1] for p in spts]
                shape = shape_of(runs, (min(xs), min(ys), max(xs), max(ys))) if len(runs) >= 2 else "straight"
                core = shape != "straight"
                ev = [Evidence(reader=READER, sheet=rec.id, source="section dimension", handle=d["handle"],
                               at=[d["x"], d["y"]], text=f"{d['shown']:.0f}") for d in (dw, dd)] + [box_ev, label_ev]
                props = {"family": Value("wall", READ, 1.0, [label_ev]),
                         "length": Value(c, READ, conf, ev),
                         "width": Value(a, READ, conf, ev)}
                if core:
                    # an L / U / core: width x length is its outside size, the
                    # thickness that of its legs, as the outline splits into runs
                    ts = sorted(round(t / 5) * 5 for _, _, t in runs)
                    props["thickness"] = Value(ts[len(ts) // 2], READ, 0.7, [box_ev])
                    props["shape"] = Value(shape, READ, 0.7, [box_ev])
                    notes = notes + [f"{shape}-shaped: width x length is its outside size"]
                else:
                    props["thickness"] = Value(a, READ, conf, ev)
                if inside:
                    props["boundary_elements"] = Value(
                        sorted({f"{min(q['box'][2] - q['box'][0], q['box'][3] - q['box'][1]) * scale:.0f} x "
                                f"{max(q['box'][2] - q['box'][0], q['box'][3] - q['box'][1]) * scale:.0f}"
                                for q in inside}), READ, 0.7, [box_ev])
                bars = [t for t in texts if BARS.search(t.text) and x0 - reach <= t.x <= x1 + reach
                        and y0 <= t.y <= ya and not LINKS.search(t.text)]
                if bars:
                    t = max(bars, key=lambda t: t.y)
                    props["bars"] = Value([t.text], READ, 0.8, [Evidence(reader=READER, sheet=rec.id,
                                                                          source="bar callout", layer=t.layer,
                                                                          handle=t.handle, at=[t.x, t.y], text=t.text)])
                b.add(Element(id=f"{rec.id}/spec/{len(b.elements)}", type="spec", sheet=rec.id, level="",
                              wing=rec.wing, mark="", geometry={"at": list(box_ev.at)}, props=props,
                              notes=notes + ["the schedule names no mark for this section"],
                              extra={"table": f"wall schedule ({rng[0]} to {rng[1]}), no mark", "part": "",
                                     "from": "wall schedule", "level_range": list(rng), "unmarked": True,
                                     "kind": shape, "range_text": label[:80]}))
                made["wall schedule sections"] = made.get("wall schedule sections", 0) + 1


def _contains(a, c) -> bool:
    return a[0] <= c[0] + 1 and a[1] <= c[1] + 1 and a[2] >= c[2] - 1 and a[3] >= c[3] - 1


def _size(o: dict, dims: list[dict]):
    """((width dim, depth dim), confidence, notes) for one section, or None.

    A dimension belongs to the section if its drawn length is one of the
    outline's sides or its box (within 3%). Its shown value over its drawn length
    is the section's scale (0.25 at Sasan Gir, 1 at Villa 5). A dimension whose
    length cannot be measured but which displays a value (Villa 5's 230s) belongs
    if that value, at the section's scale, is a side. Width is the horizontal one,
    depth the vertical one.

    On a skewed outline (Sasan Gir C19, C20 in the angled wing) the sloping side
    is dimensioned along its slope: that one is taken in place of a horizontal
    one, at lower confidence. A side with no dimension at all is measured from the
    outline at the section's own scale - only when the other side is dimensioned,
    and at lower confidence still.
    """
    bw, bd = o["box"][2] - o["box"][0], o["box"][3] - o["box"][1]
    lengths = o["sides"] + [bw, bd]

    def fits_len(v):
        return any(abs(v - s) <= MATCH * s for s in lengths)
    fits = [d for d in dims if d["drawn"] and fits_len(d["drawn"])]
    scales = sorted(d["shown"] / d["drawn"] for d in fits)
    scale = scales[len(scales) // 2] if scales else None
    if scale:
        fits += [d for d in dims if not d["drawn"] and fits_len(d["shown"] / scale)]
    notes = []
    hs = [d for d in fits if d["dir"] == "h"]
    vs = [d for d in fits if d["dir"] == "v"]
    conf = 0.85
    if scale and (hs or vs) and not (hs and vs) and not any(d["dir"] == "" for d in dims):
        # one side dimensioned, the other only drawn (a 230 wall-width side often is)
        side, length, name = ("width", bw, "h") if not hs else ("depth", bd, "v")
        got = {"shown": float(round(length * scale / 5.0) * 5.0), "drawn": length, "note": "",
               "measured": True, "conf": 0.7, "dir": name}
        (hs if not hs else vs).append(got)
        notes.append(f"{side} not dimensioned: measured from the outline at the scale of its other dimension")
    if not hs:
        # an aligned dimension along a sloping side; its drawn length may not match
        # if it was stretched after it was drawn (C20) - its shown value is kept
        hs = [d for d in dims if d["dir"] == "" and d not in vs]
        if hs:
            notes.append("width from a dimension along a sloping side")
            conf = 0.6
    if not vs:
        vs = [d for d in dims if d["dir"] == "" and d not in hs]
        if vs:
            notes.append("depth from a dimension along a sloping side")
            conf = 0.6
    if not hs or not vs:
        return None
    dw = min(hs, key=lambda d: abs(d["drawn"] - bw))
    dd = min(vs, key=lambda d: abs(d["drawn"] - bd))
    for d in (dw, dd):
        if d["note"].startswith("displayed ") and "drawn length gives" in d["note"]:
            notes.append(f"dimension {d['note']}")
            conf = min(conf, 0.6)
    if dw["drawn"] and dd["drawn"] and not (dw.get("measured") or dd.get("measured")):
        sw, sd = dw["shown"] / dw["drawn"], dd["shown"] / dd["drawn"]
        if abs(sw - sd) > 0.05 * max(sw, sd):
            notes.append(f"the two dimensions are at different scales ({sw:.3g} and {sd:.3g})")
            conf = min(conf, 0.6)
    if o["n"] > 4:
        notes.append(f"outline has {o['n']} corners, not a plain rectangle")
    return (dw, dd), conf, notes


def _steel(texts, inside) -> tuple[list, list]:
    """Bar callouts and link callouts in a cell; each link with its zone label above it."""
    bars = [t for t in texts if inside(t.x, t.y) and BARS.search(t.text) and not LINKS.search(t.text)
            and not re.search(r"STARTS?|FROM|BEL+OW", t.text, re.I)]
    links = []
    for t in texts:
        if not (inside(t.x, t.y) and LINKS.search(t.text)):
            continue
        above = [z for z in texts if inside(z.x, z.y) and ZONE.search(z.text) and not LINKS.search(z.text)
                 and 0 < z.y - t.y <= 4 * max(t.height, 1.0) and abs(z.x - t.x) <= 8 * max(t.height, 1.0)]
        zone = min(above, key=lambda z: (z.y - t.y) + abs(z.x - t.x)).text if above else ""
        links.append((t, zone))
    bars.sort(key=lambda t: -t.y)
    links.sort(key=lambda tz: tz[0].x)
    return bars, links


def _extent_from_labels(texts, headers, hd, label_x):
    """No grid lines: the table runs from the header to the farthest level label on
    the side where the labels are, stopping at the next header."""
    h = max(hd.height, 1.0)
    lab = [t for t in texts if label_x[0] <= t.x <= label_x[1] and t is not hd and levels_in(t.text)]
    best = None
    for side in (1, -1):
        stop = [side * (o.y - hd.y) for o in headers if o is not hd and side * (o.y - hd.y) > 0]
        limit = min(stop) if stop else math.inf
        mine = [t for t in lab if 0 < side * (t.y - hd.y) < limit]
        if mine:
            near = min(side * (t.y - hd.y) for t in mine)
            if best is None or near < best[0]:
                far = max(side * (t.y - hd.y) for t in mine)
                best = (near, side, far)
    if best is None:
        return None, None
    _, side, far = best
    ends = (hd.y - side * 2 * h, hd.y + side * (far + 6 * h))
    return max(ends), min(ends)


def _bands_from_labels(texts, hd, label_x, top, bottom):
    """No row lines: one band per group of level labels, cut halfway between groups."""
    h = max(hd.height, 1.0)
    lab = sorted((t for t in texts if label_x[0] <= t.x <= label_x[1] and bottom <= t.y <= top
                  and t is not hd), key=lambda t: -t.y)
    groups: list[list] = []
    for t in lab:
        if groups and groups[-1][-1].y - t.y <= 4 * h:
            groups[-1].append(t)
        else:
            groups.append([t])
    groups = [g for g in groups if any(levels_in(t.text) for t in g)]
    if not groups:
        return []
    mids = [(a[-1].y + b[0].y) / 2 for a, b in zip(groups, groups[1:])]
    edges = [top] + mids + [bottom]
    return [(edges[i + 1], edges[i]) for i in range(len(groups))]
