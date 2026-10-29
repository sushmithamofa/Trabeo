"""Wall elevations: how high each shear wall stands, and on what.

A plan shows where a wall is; an elevation of it shows from which level to which it
runs. Akanistha / Winter Villa "SHEAR WALL ELEVATION (SW9 & SW10)": level lines
LEVEL -01 ... LEVEL 05 with the storey heights dimensioned (2800, 3900, 5850 ...), the
walls' faces drawn on the column layer, SW9 from LEVEL -01 to LEVEL 01, SW10 up to
LEVEL 05, both on a 600 raft whose top is LEVEL -01. "SHEAR WALL ELEVATION (SW15)":
SW15 stands on its raft 1325 above the lowest level line (the site slopes there), and
leans above LEVEL 01. Since 28 Oct titled "SHEAR WALL ELEVATION (SW15 , SA14 , SW13 ,C12 )":
the one wall drawn, labelled by none, is all four (in line, one behind the other).

Read per view whose title names wall marks and says ELEVATION or SECTION, with at
least two level lines:

    levels   [{"name", "y"}]       each level line, bottom up; a line drawn as the
                                   labelled ones (same layer, same start) but with no
                                   label of its own has name ""
    scale    drawing units per mm, when a written storey height confirms it, else None
    walls    [{"marks", "base", "top", "x0", "x1", "shaped_at", "foundation", "outline"}]
             one per wall drawn: its lowest and highest point, its extent across,
             its outline as drawn ([[x, y], ...]: an arch's curve, a lean),
             the storeys over which it changes shape (an arch, a lean: the index of
             the level line at the storey's top), and the slab drawn under its base
             (top, bottom) when there is one

Heights stay in the drawing's units, as drawn; the linker matches the levels to the
floors and turns them into mm. Nothing is added as an element (no element id moves):
it is kept on the view's sheet, extra["wall_elevation"].
"""
from __future__ import annotations

import math
import re

from ..marks import parse_mark
from ..model import Building

READER = "wall_elevations"
TITLE = re.compile(r"\b(?:ELEVATION|SECTION)\b", re.I)
LEVEL = re.compile(r"^\s*(?:LEVEL|LVL)\b\.?\s*(.*)$", re.I)
WALL_LAYER = re.compile(r"COL|WALL|SHEAR|CORE", re.I)
NOT_WALL = re.compile(r"HATCH|TEXT|DIM|GRID|REINF|\bBAR|ANNO|TABLE|COVER|STEEL|LANDSC", re.I)
NUMBER = re.compile(r"^\s*(\d{3,5})\s*$")
MIN_LEVEL_LINE = 3000.0      # drawing units: a level line runs across the drawing
GAP = 1000.0                 # drawing units: a wall broken by a slab carries on above it
STEP = 50.0                  # drawing units between the heights the wall is read at


def _plain(e) -> str:
    t = e.dxftype()
    try:
        s = e.dxf.text if t == "TEXT" else e.plain_text()
    except Exception:
        return ""
    return " ".join(re.sub(r"%%[UuOo]", "", s or "").split())


def _height(e) -> float:
    try:
        return float(e.dxf.height if e.dxftype() == "TEXT" else e.dxf.char_height)
    except Exception:
        return 100.0


def _entities(rec) -> list:
    """The view's model entities, with those of blocks inserted unscaled and unturned."""
    db = getattr(getattr(rec, "doc", None), "entitydb", None)
    view = getattr(rec, "view", None)
    if db is None or view is None:
        return []
    out = []
    for h in view.handles:
        e = db.get(h)
        if e is None:
            continue
        if e.dxftype() == "INSERT":
            try:
                if all(abs(float(e.dxf.get(k, 1.0) or 1.0) - 1.0) < 1e-6 for k in ("xscale", "yscale")) \
                        and abs(float(e.dxf.get("rotation", 0.0) or 0.0)) < 1e-6:
                    out += list(e.virtual_entities())
            except Exception:
                continue
        else:
            out.append(e)
    return out


def _segments(e) -> list:
    """The entity as straight pieces (arcs and bulges flattened)."""
    t = e.dxftype()
    if t == "LINE":
        return [(e.dxf.start.x, e.dxf.start.y, e.dxf.end.x, e.dxf.end.y)]
    if t in ("ARC", "LWPOLYLINE", "POLYLINE", "SPLINE", "ELLIPSE"):
        try:
            from ezdxf import path
            pts = [(v.x, v.y) for v in path.make_path(e).flattening(5.0)]
        except Exception:
            return []
        return [(a[0], a[1], c[0], c[1]) for a, c in zip(pts, pts[1:])]
    return []


def _levels(texts, hlines) -> list:
    """Level lines: a LEVEL text and the horizontal line it labels, then the lines drawn the
    same way (layer, start) with no label - the lowest one is often left unnamed."""
    found = {}
    for t in texts:
        s = _plain(t)
        m = LEVEL.match(s)
        if not m or TITLE.search(s):
            continue
        h = _height(t)
        tx, ty = t.dxf.insert.x, t.dxf.insert.y
        best = None
        for ln in hlines:
            x0, x1, y, _layer = ln
            if not (x0 - 3 * h <= tx <= x1 + 3 * h):
                continue
            dy = ty - y
            if -1.5 * h <= dy <= 4.0 * h and (best is None or abs(dy) < abs(best[0])):
                best = (dy, ln)
        if best is not None:
            found[round(best[1][2], 1)] = (m.group(1).strip(" .:") or s, best[1])     # "-01" keeps its sign
    if len(found) < 2:
        return []
    named = [ln for _n, ln in found.values()]
    for ln in hlines:
        y = round(ln[2], 1)
        if y in found or any(abs(y - k) < 1.0 for k in found):
            continue
        if any(ln[3] == o[3] and abs(ln[0] - o[0]) <= 50.0 and ln[1] - ln[0] >= 0.8 * (o[1] - o[0]) for o in named):
            found[y] = ("", ln)
    return [{"name": n, "y": y} for y, (n, _ln) in sorted(found.items())]


def _scale(levels, numbers) -> float | None:
    """Drawing units per mm: 1 when a storey's drawn height is written as a number in the
    view (a dimension or its text), the scale those numbers give otherwise; None when no
    storey height is written."""
    hs = [b["y"] - a["y"] for a, b in zip(levels, levels[1:])]
    if not numbers:
        return None
    ratios = [h / n for h in hs for n in numbers if n > 0 and 0.05 <= h / n <= 20.0]
    for r in sorted(set(round(x, 4) for x in ratios), key=lambda x: abs(x - 1.0)):
        hits = sum(1 for h in hs if any(abs(h / r - n) <= 5.0 for n in numbers))
        if hits >= max(1, (len(hs) + 1) // 2):
            return r
    return None


def _cuts(segs, y) -> list[float]:
    xs = []
    for x0, y0, x1, y1 in segs:
        lo, hi = min(y0, y1), max(y0, y1)
        if hi - lo < 1e-6 or not (lo <= y < hi):
            continue
        xs.append(x0 + (x1 - x0) * (y - y0) / (y1 - y0))
    xs.sort()
    out = []
    for x in xs:
        if out and abs(x - out[-1]) < 1.0:
            continue
        out.append(x)
    return out


def _walls(segs, lo, hi) -> list:
    """The walls drawn: read across at every STEP of height; where the faces cut pair up,
    the stretches between them are wall. Stretches one above the other (across a slab
    break up to GAP) are one wall."""
    groups = []          # {"rows": [(y, x0, x1)], "x0", "x1"}
    open_ = []
    y = lo + STEP / 2
    while y < hi:
        xs = _cuts(segs, y)
        here = []
        if xs and len(xs) % 2 == 0:
            for a, c in zip(xs[0::2], xs[1::2]):
                if c - a >= 100.0:
                    here.append((a, c))
        nxt = []
        for a, c in here:
            g = None
            for o in open_:
                last = o["rows"][-1]
                if y - last[0] <= GAP and min(c, last[2]) - max(a, last[1]) > 0.3 * min(c - a, last[2] - last[1]):
                    g = o
                    break
            if g is None:
                g = {"rows": []}
                groups.append(g)
            g["rows"].append((y, a, c))
            nxt.append(g)
        open_ = [o for o in open_ if o not in nxt and y - o["rows"][-1][0] <= GAP] + nxt
        y += STEP
    out = []
    for g in groups:
        rows = g["rows"]
        if len(rows) < 3:
            continue
        ys = [r[0] for r in rows]
        # its faces' own ends, between the first (last) height read and the one before (after)
        def ends(row):
            return [yy for s in segs for xx, yy in ((s[0], s[1]), (s[2], s[3])) if row[1] - 10 <= xx <= row[2] + 10]
        low = [yy for yy in ends(rows[0]) if min(ys) - STEP <= yy <= min(ys)]
        high = [yy for yy in ends(rows[-1]) if max(ys) <= yy <= max(ys) + STEP]
        base = min(low) if low else min(ys) - STEP / 2
        top = max(high) if high else max(ys) + STEP / 2
        out.append({"base": round(base, 1), "top": round(top, 1), "outline": _outline(rows, base, top),
                    "x0": round(min(r[1] for r in rows), 1), "x1": round(max(r[2] for r in rows), 1),
                    "foot": [round(rows[0][1], 1), round(rows[0][2], 1)],
                    "rows": [[round(r[0], 1), round(r[1], 1), round(r[2], 1)] for r in rows]})
    return out


def _outline(rows, base, top) -> list:
    """The wall as drawn, one outline: its left faces up, its right faces down, from its base
    to its top (an arch's curve, a lean), simplified to within half a STEP."""
    from shapely.geometry import Polygon
    pts = [(rows[0][1], base)] + [(r[1], r[0]) for r in rows] + [(rows[-1][1], top), (rows[-1][2], top)] \
        + [(r[2], r[0]) for r in reversed(rows)] + [(rows[0][2], base)]
    poly = Polygon(pts).buffer(0)
    if poly.geom_type != "Polygon":
        poly = max(getattr(poly, "geoms", []), key=lambda g: g.area, default=None)
        if poly is None:
            return []
    return [[round(x, 1), round(y, 1)] for x, y in poly.simplify(STEP / 2).exterior.coords[:-1]]


def _foundation(w, hlines) -> dict | None:
    """The slab drawn under a wall's base: a line at its foot spanning it, and the one
    under it as wide (its bottom)."""
    a, c = w["foot"]
    top = [ln for ln in hlines if abs(ln[2] - w["base"]) <= 25.0 and ln[0] <= a + 10 and ln[1] >= c - 10]
    if not top:
        return None
    t = top[0]
    under = [ln for ln in hlines if 100.0 <= t[2] - ln[2] <= 2500.0 and ln[0] <= a + 10 and ln[1] >= c - 10
             and abs(ln[0] - t[0]) <= 0.2 * (t[1] - t[0]) + 50 and abs(ln[1] - t[1]) <= 0.2 * (t[1] - t[0]) + 50]
    if not under:
        return None
    bot = max(under, key=lambda ln: ln[2])
    return {"top": round(t[2], 1), "bottom": round(bot[2], 1), "x0": round(t[0], 1), "x1": round(t[1], 1)}


def read(b: Building, rec) -> int:
    sheet = b.sheets.get(rec.id)
    title = (sheet.title if sheet else "") or getattr(rec, "title", "") or ""
    if not TITLE.search(title) or sheet is None or getattr(rec.verdict, "kind", "") in ("framing_plan", "column_layout",
                                                                                      "foundation_plan"):
        return 0
    words = re.findall(r"[A-Z]+\d+[A-Z]?", title.upper())
    parsed = [m for m in (parse_mark(w) for w in words) if m]
    marks = [m.key for m in parsed if m.family() == "wall"]
    if not marks:
        return 0
    # the title's other columns and walls (C12, a mistyped SA14): they stand as the one wall
    # drawn when it is the only one and nothing labels it
    also = [m.key for m in parsed if m.family() in ("column", "unknown") and m.key not in marks]
    ents = _entities(rec)
    texts = [e for e in ents if e.dxftype() in ("TEXT", "MTEXT")]
    hlines, segs = [], []
    numbers = []
    for e in ents:
        t = e.dxftype()
        if t == "LINE" and abs(e.dxf.start.y - e.dxf.end.y) < 1.0 and abs(e.dxf.start.x - e.dxf.end.x) >= 100.0:
            x0, x1 = sorted((e.dxf.start.x, e.dxf.end.x))
            hlines.append((x0, x1, (e.dxf.start.y + e.dxf.end.y) / 2, e.dxf.layer))
        if WALL_LAYER.search(e.dxf.layer) and not NOT_WALL.search(e.dxf.layer):
            segs += [s for s in _segments(e) if abs(s[3] - s[1]) > 1e-6]
        if t == "DIMENSION":
            try:
                from .details import dimension_reading
                v = dimension_reading(e)[0]
                if v:
                    numbers.append(float(v))
            except Exception:
                pass
        elif t in ("TEXT", "MTEXT"):
            m = NUMBER.match(_plain(e))
            if m:
                numbers.append(float(m.group(1)))
    long_lines = [ln for ln in hlines if ln[1] - ln[0] >= MIN_LEVEL_LINE]
    levels = _levels(texts, long_lines)
    if len(levels) < 2 or not segs:
        return 0
    lo, hi = levels[0]["y"] - 0.5 * (levels[-1]["y"] - levels[0]["y"]), levels[-1]["y"] + 1000.0
    walls = _walls(segs, lo, hi)
    if not walls:
        return 0
    # which wall is which: a label naming one mark points at it (the nearest wall at its
    # height); one wall and one mark in the title need no label
    labels = []
    for t in texts:
        s = _plain(t)
        if t is not None and TITLE.search(s):
            continue
        for w in re.findall(r"[A-Z]+\d+[A-Z]?", s.upper()):
            mk = parse_mark(w)
            if mk and mk.key in marks:
                labels.append((mk.key, t.dxf.insert.x, t.dxf.insert.y, _height(t) * max(len(s), 1) * 0.8))
    for w in walls:
        w["marks"] = []
    # one wall drawn and no label: it is every mark the title names, in line one behind the
    # other (Winter Villa "SHEAR WALL ELEVATION (SW15 , SA14 , SW13 ,C12 )") - each its own entry
    shared = len(walls) == 1 and not labels
    if shared:
        walls[0]["marks"] = list(dict.fromkeys(marks + also))
    elif len(walls) == 1 and len(marks) == 1:
        walls[0]["marks"] = list(marks)
    else:
        for key, x, y, ln in labels:
            def dist(w):
                rows = [r for r in w["rows"] if abs(r[0] - y) <= 2 * STEP] or w["rows"]
                a = min(r[1] for r in rows)
                c = max(r[2] for r in rows)
                ends = (x, x + ln)
                return 0.0 if a <= x <= c else min(abs(p - q) for p in ends for q in (a, c))
            near = sorted(walls, key=dist)
            if near and (len(near) == 1 or dist(near[0]) < 0.5 * dist(near[1])):
                if key not in near[0]["marks"]:
                    near[0]["marks"].append(key)
    named = [dict(walls[0], marks=[k]) for k in walls[0]["marks"]] if shared else \
        [w for w in walls if len(w["marks"]) == 1]
    if not named:
        return 0
    out = []
    for w in named:
        # the storeys over which it changes shape (an arch, a lean): level index of the storey's top
        shaped_at = []
        for j in range(1, len(levels)):
            rows = [r for r in w["rows"] if levels[j - 1]["y"] + 300.0 < r[0] < levels[j]["y"] - 300.0]
            if len(rows) >= 3:
                wd = [r[2] - r[1] for r in rows]
                md = [(r[1] + r[2]) / 2 for r in rows]
                if max(wd) - min(wd) > 0.1 * max(wd) or max(md) - min(md) > 100.0:
                    shaped_at.append(j)
        out.append({"marks": w["marks"], "base": w["base"], "top": w["top"], "x0": w["x0"], "x1": w["x1"],
                    "shaped_at": shaped_at, "foundation": _foundation(w, hlines), "outline": w["outline"]})
    sheet.extra["wall_elevation"] = {"title": title[:80], "levels": levels, "scale": _scale(levels, numbers),
                                     "walls": out}
    return len(out)
