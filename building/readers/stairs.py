"""Stairs and ramps on plans, and what their notes say (read only; built in stage 8).

A stair flight is drawn the same way by every consultant: its treads, a row of
parallel lines of one length (the flight's width) at an even spacing (the going).
That is what is looked for, on any layer:

* at least MIN_TREADS parallel lines, equal length (within 5%) 600-3000 mm, their
  ends in line, spaced evenly (within 12%) at 180-400 mm;
* and, so that a hatch or a row of bars is not taken for a stair, either a layer
  that says so (STAIR, STEP, TREAD) or a stair word near it (UP, DN, STAIRCASE,
  LANDING) - never on dimension, text, hatch or reinforcement layers.

Flights close to each other (a dog-leg's two flights and the landing between them)
are one stair. Each flight keeps its width, going, number of treads and direction;
the UP / DN beside it is kept as written.

Notes give what a plan cannot (Villa 5: "RISER = 0.160M THREAD = 0.30M",
"MIDLANDING UFL. LVL. (+)1440"): read as a stair spec of the sheet, with its
evidence; the linker gives it to the stairs of the same drawing set.

A ramp is an outline on a RAMP layer or with RAMP written inside it; a slope
written beside it (1:12, 1 IN 10) is kept.
"""
from __future__ import annotations

import math
import re
from collections import defaultdict

from ..model import Building, Element, Evidence, Value, READ

READER = "stairs"
PLAN_KINDS = ("framing_plan", "foundation_plan", "stair_details", "beam_details", "column_details")
MIN_TREADS = 4
WIDTH = (600.0, 3000.0)
GOING = (180.0, 400.0)
LAYER_HINT = re.compile(r"STAIR|STEP|TREAD|FLIGHT", re.I)
SKIP_LAYER = re.compile(r"DIM|TEXT|TXT|HATCH|GRID|LEADER|DEFPOINT|TITLE|BORDER|REBAR|STEEL|BAR|LINK|NUMBER", re.I)
STAIR_WORD = re.compile(r"^\s*(?:UP|DN|DOWN)\s*$|STAIR|LANDING|FLIGHT", re.I)
UPDN = re.compile(r"^\s*(UP|DN|DOWN)\s*$", re.I)
RISER = re.compile(r"\bRISER\s*[=:-]?\s*(\d+(?:\.\d+)?)\s*(MM|M)?\b|\bR\s*=\s*(\d{2,3})\b", re.I)
TREAD = re.compile(r"\b(?:TREAD|THREAD|GOING)\s*[=:-]?\s*(\d+(?:\.\d+)?)\s*(MM|M)?\b|\bT\s*=\s*(\d{3})\b", re.I)
LANDING = re.compile(r"(?:MID\s*-?\s*LANDING|LANDING)[^0-9+\-()]*(?:LVL|LEVEL)?\.?\s*\(?\s*([+\-])?\s*\)?\s*(\d+(?:\.\d+)?)",
                     re.I)
RAMP = re.compile(r"\bRAMP\b", re.I)
FRAME_TEXTS = 20        # an outline labelled RAMP holding more texts than this is a frame, not a ramp
SLOPE = re.compile(r"\b1\s*(?::|IN)\s*(\d{1,2}(?:\.\d)?)\b", re.I)


def _mm(num: str, unit: str | None) -> float:
    v = float(num)
    return v * 1000.0 if (unit or "").upper() == "M" or v < 5 else v


def _segments(ents):
    """Straight pieces: LINEs and two-point polylines, with their layer. A block on
    a stair layer or named like one (Sasan Gir's STAIR block) is opened up."""
    out = []
    for e in ents:
        if SKIP_LAYER.search(e.dxf.layer):
            continue
        parts = [e]
        if e.dxftype() == "INSERT" and (LAYER_HINT.search(e.dxf.layer) or LAYER_HINT.search(e.dxf.name)):
            try:
                parts = list(e.virtual_entities())
            except Exception:
                parts = []
        for x in parts:
            if x.dxftype() == "LINE":
                a, c = x.dxf.start, x.dxf.end
                out.append(((a.x, a.y), (c.x, c.y), e.dxf.layer, e.dxf.handle))
            elif x.dxftype() == "LWPOLYLINE" and not x.closed and len(x) == 2:
                pts = [(p[0], p[1]) for p in x.get_points("xy")]
                out.append((pts[0], pts[1], e.dxf.layer, e.dxf.handle))
    return out


def flights(segs) -> list[dict]:
    """Rows of evenly spaced, equal, parallel lines."""
    buckets = defaultdict(list)
    for a, c, layer, h in segs:
        L = math.dist(a, c)
        if not (WIDTH[0] <= L <= WIDTH[1]):
            continue
        ang = math.degrees(math.atan2(c[1] - a[1], c[0] - a[0])) % 180.0
        buckets[round(ang) % 180].append((a, c, L, layer, h, ang))
    out = []
    for key in list(buckets):
        lines = buckets[key] + buckets.get((key + 1) % 180, [])
        if not lines:
            continue
        ang = math.radians(sum(x[5] for x in lines) / len(lines)) if key not in (0, 179) else math.radians(0.0)
        ux, uy = math.cos(ang), math.sin(ang)           # along a tread
        nx, ny = -uy, ux                                 # across the treads: the walking direction
        rows = []
        for a, c, L, layer, h, _ in lines:
            mx, my = (a[0] + c[0]) / 2, (a[1] + c[1]) / 2
            rows.append((mx * nx + my * ny, mx * ux + my * uy, L, layer, h, a, c))
        # treads of one flight have one length and stand side by side: cluster on
        # both first, so a plan's other lines in between cannot break the row
        clusters: list[list] = []
        for r in sorted(rows, key=lambda r: (r[2], r[1])):
            for cl in clusters:
                if abs(r[2] - cl[0][2]) <= 0.05 * cl[0][2] and abs(r[1] - cl[0][1]) <= 0.1 * cl[0][2]:
                    cl.append(r)
                    break
            else:
                clusters.append([r])
        for cl in clusters:
            if len(cl) >= MIN_TREADS:
                _rows_to_flights(sorted(cl), (ux, uy), (nx, ny), out)
        buckets[(key + 1) % 180] = []                    # merged into this bucket
    # a flight found from two neighbouring angle buckets is one flight
    uniq = []
    for f in out:
        if not any(math.dist(f["centre"], g["centre"]) < 50 and f["treads"] == g["treads"] for g in uniq):
            uniq.append(f)
    return uniq


def _rows_to_flights(rows, u, n, out) -> None:
    """Split one cluster of equal, side-by-side lines into evenly spaced runs."""
    run: list = []

    def close(run):
        if len(run) >= MIN_TREADS:
            gaps = [q[0] - p[0] for p, q in zip(run, run[1:])]
            g = sorted(gaps)[len(gaps) // 2]
            if GOING[0] <= g <= GOING[1] and all(abs(x - g) <= 0.12 * g for x in gaps):
                out.append(_flight(run, g, u, n))

    for r in rows:
        if run:
            gap = r[0] - run[-1][0]
            if gap < 1.0:
                continue                                     # a line drawn twice
            first = run[1][0] - run[0][0] if len(run) >= 2 else None
            if gap <= GOING[1] * 1.15 and (first is None or abs(gap - first) <= 0.12 * first):
                run.append(r)
                continue
            close(run)
            run = []
        run.append(r)
    close(run)


def _flight(run, going, u, n) -> dict:
    first, last = run[0], run[-1]
    width = sorted(r[2] for r in run)[len(run) // 2]
    pts = [p for r in run for p in (r[5], r[6])]
    cx, cy = sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)
    return {"treads": len(run), "going": round(going), "width": round(width), "centre": (cx, cy),
            "along": list(u), "walk": list(n), "length": round(last[0] - first[0]),
            "first": [list(first[5]), list(first[6])], "last": [list(last[5]), list(last[6])],
            "layers": sorted({r[3] for r in run}), "handles": [r[4] for r in run]}


def _corners(f) -> list[list[float]]:
    (a, b), (c, d) = f["first"], f["last"]
    return [a, b, d, c] if math.dist(b, d) < math.dist(b, c) else [a, b, c, d]


def read(b: Building, rec) -> dict[str, int]:
    if rec.doc is None or rec.verdict.kind not in PLAN_KINDS:
        return {}
    hs = set(rec.view.handles)
    ents = [e for e in rec.doc.modelspace() if e.dxf.handle in hs]
    texts = [t for t in rec.texts if t.space == "model"]
    made = {"stairs": 0, "stair flights": 0, "stair notes": 0, "ramps": 0}
    words = [t for t in texts if STAIR_WORD.search(t.text)]

    # flights, believed only with a stair layer or a stair word beside them
    found = []
    for f in flights(_segments(ents)):
        reach = max(3000.0, 1.5 * max(f["width"], f["length"]))
        near = [t for t in words if math.dist((t.x, t.y), f["centre"]) <= reach]
        if any(LAYER_HINT.search(lyr) for lyr in f["layers"]) or near:
            f["words"] = near
            found.append(f)
    # flights near each other are one stair (a dog-leg: two flights and a landing)
    groups: list[list] = []
    for f in found:
        for g in groups:
            if any(math.dist(f["centre"], h["centre"]) <= f["width"] + h["width"] + 1500.0 for h in g):
                g.append(f)
                break
        else:
            groups.append([f])
    for g in groups:
        pts = [p for f in g for p in _corners(f)]
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
        ev = Evidence(reader=READER, sheet=rec.id, source="tread lines", layer=",".join(g[0]["layers"]),
                      handle=g[0]["handles"][0], at=[cx, cy],
                      text=" + ".join(f"{f['treads']} treads @ {f['going']}" for f in g))
        updn = sorted({t.text.strip().upper() for f in g for t in f["words"] if UPDN.match(t.text)})
        props = {"flights": Value(len(g), READ, 0.8, [ev]),
                 "treads": Value([f["treads"] for f in g], READ, 0.8, [ev]),
                 "going": Value(sorted(f["going"] for f in g)[len(g) // 2], READ, 0.7, [ev]),
                 "width": Value(sorted(f["width"] for f in g)[len(g) // 2], READ, 0.7, [ev])}
        if updn:
            props["direction_text"] = Value(updn, READ, 0.7, [ev])
        b.add(Element(id=f"{rec.id}/stair/{len(b.elements)}", type="stair", sheet=rec.id,
                      level=rec.levels[0] if len(rec.levels) == 1 else "", wing=rec.wing,
                      geometry={"outline": [[min(xs), min(ys)], [max(xs), min(ys)], [max(xs), max(ys)],
                                            [min(xs), max(ys)]], "x": cx, "y": cy,
                                "flights": [{k: f[k] for k in ("treads", "going", "width", "first", "last", "walk")}
                                            for f in g]},
                      props=props))
        made["stairs"] += 1
        made["stair flights"] += len(g)

    # notes: riser, tread, landing levels
    riser = tread = None
    landings = []
    for t in texts:
        m = RISER.search(t.text)
        if m and riser is None:
            v = _mm(m.group(1), m.group(2)) if m.group(1) else float(m.group(3))
            if 100 <= v <= 220:
                riser = (v, t)
        m = TREAD.search(t.text)
        if m and tread is None:
            v = _mm(m.group(1), m.group(2)) if m.group(1) else float(m.group(3))
            if 200 <= v <= 400:
                tread = (v, t)
        m = LANDING.search(t.text)
        if m:
            v = float(m.group(2))
            v = v * 1000.0 if "." in m.group(2) and v < 50 else v
            landings.append(((-1 if m.group(1) == "-" else 1) * v, t))
    if riser or tread or landings:
        props = {"family": Value("stair", READ, 1.0, [])}
        for name, got in (("riser", riser), ("tread", tread)):
            if got:
                v, t = got
                props[name] = Value(round(v), READ, 0.9, [Evidence(reader=READER, sheet=rec.id, source="stair note",
                                                                    layer=t.layer, handle=t.handle, at=[t.x, t.y],
                                                                    text=t.text[:80])])
        if landings:
            props["landing_levels"] = Value(sorted({round(v) for v, _ in landings}), READ, 0.8,
                                            [Evidence(reader=READER, sheet=rec.id, source="landing level note",
                                                      layer=t.layer, handle=t.handle, at=[t.x, t.y],
                                                      text=t.text[:80]) for _, t in landings[:8]])
        b.add(Element(id=f"{rec.id}/spec/{len(b.elements)}", type="spec", sheet=rec.id, wing=rec.wing,
                      mark="", props=props, extra={"from": "stair notes", "part": ""}))
        made["stair notes"] += 1

    # ramps
    for e in ents:
        if SKIP_LAYER.search(e.dxf.layer):
            continue
        if e.dxftype() == "INSERT" and (RAMP.search(e.dxf.layer) or RAMP.search(e.dxf.name)):
            # a ramp drawn as a block (Sasan Gir GF): the box round what it draws
            try:
                from ezdxf import bbox
                ext = bbox.extents(e.virtual_entities(), fast=True)
            except Exception:
                continue
            if not ext.has_data:
                continue
            x0, y0, x1, y1 = ext.extmin.x, ext.extmin.y, ext.extmax.x, ext.extmax.y
            pts = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
        elif e.dxftype() == "LWPOLYLINE" and e.closed:
            pts = [(p[0], p[1]) for p in e.get_points("xy")]
        else:
            continue
        if len(pts) < 3:
            continue
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        box = (min(xs), min(ys), max(xs), max(ys))
        if min(box[2] - box[0], box[3] - box[1]) < 800:
            continue
        inside = [t for t in texts if box[0] <= t.x <= box[2] and box[1] <= t.y <= box[3]]
        on_layer = bool(RAMP.search(e.dxf.layer)) or (e.dxftype() == "INSERT" and bool(RAMP.search(e.dxf.name)))
        labelled = [t for t in inside if RAMP.search(t.text)]
        if not (on_layer or labelled):
            continue
        if not on_layer and len(inside) > FRAME_TEXTS:
            # a frame round the sheet or a whole drawing holds all its writing, a RAMP note
            # among it; a ramp's own outline carries a few notes (RAMP, the slope, UP)
            continue
        ev = Evidence(reader=READER, sheet=rec.id, source="ramp layer" if on_layer else "ramp label",
                      layer=e.dxf.layer, handle=e.dxf.handle, at=[(box[0] + box[2]) / 2, (box[1] + box[3]) / 2],
                      text=labelled[0].text if labelled else e.dxf.layer)
        props = {"width": Value(round(min(box[2] - box[0], box[3] - box[1])), READ, 0.6, [ev]),
                 "length": Value(round(max(box[2] - box[0], box[3] - box[1])), READ, 0.6, [ev])}
        slope = next((t for t in texts if SLOPE.search(t.text) and
                      math.dist((t.x, t.y), ev.at) <= max(box[2] - box[0], box[3] - box[1])), None)
        if slope:
            props["slope"] = Value(f"1:{SLOPE.search(slope.text).group(1)}", READ, 0.7, [Evidence(
                reader=READER, sheet=rec.id, source="slope note", layer=slope.layer, handle=slope.handle,
                at=[slope.x, slope.y], text=slope.text[:60])])
        b.add(Element(id=f"{rec.id}/ramp/{len(b.elements)}", type="ramp", sheet=rec.id,
                      level=rec.levels[0] if len(rec.levels) == 1 else "", wing=rec.wing,
                      geometry={"outline": [list(p) for p in pts], "x": ev.at[0], "y": ev.at[1]}, props=props))
        made["ramps"] += 1
    return {k: v for k, v in made.items() if v}
