"""Building sections: where beams and slabs stand in height - and which ones slope.

A framing plan shows where things are; a section through the building shows how high.
Sasan Gir Main Block terrace, "SECTION: C-C" (drawn inside a block, scaled 1.495):
level lines "Terrace lvl" and "2nd Floor lvl", beam boxes (a rectangle with an X)
marked B19, B7, B20, B21, and the slab between B19 and B7 drawn sloping - B19's top
2270 mm below the terrace, the slab rising from it to B7 at terrace level.

Read per titled section that has at least one level line (a beam's cross-section
detail, SECTION X-X, has none and is beam_sections' work):

    levels   [{"name", "y"}]                  a level text and the line it labels
    beams    [{"mark", "x0", "x1", "top", "bottom"}]   each box, its mark if written
    slopes   [[x0, y0, x1, y1], ...]           inclined lines (a sloping slab or beam)
    scale    drawn units per mm: the block's scale when the section is a block, else 1

All in the drawing's own units, as drawn; the linker matches the section to the plan
by its beam marks and turns heights into mm (checking the scale on the beams' depths).
"""
from __future__ import annotations

import math
import re

from ..marks import parse_mark
from ..model import Building, Element, Evidence, Value, READ
from .beam_sections import TITLE

READER = "building_sections"
LEVEL = re.compile(r"\b(?:LVL|LEVEL)\b\.?", re.I)


def _plain(e):
    t = e.dxftype()
    try:
        s = e.dxf.text if t == "TEXT" else e.plain_text()
    except Exception:
        return ""
    return " ".join(re.sub(r"%%[UuOo]", "", s or "").split())


def _height(e):
    try:
        return float(e.dxf.height if e.dxftype() == "TEXT" else e.dxf.char_height)
    except Exception:
        return 100.0


def _groups(rec):
    """(entities, scale) per block in the view, and the view's own top-level entities."""
    db = getattr(getattr(rec, "doc", None), "entitydb", None)
    view = getattr(rec, "view", None)
    if db is None or view is None:
        return []
    top, out = [], []
    for h in view.handles:
        e = db.get(h)
        if e is None:
            continue
        if e.dxftype() == "INSERT":
            try:
                out.append((list(e.virtual_entities()), abs(float(e.dxf.get("xscale", 1.0) or 1.0))))
            except Exception:
                continue
        else:
            top.append(e)
    return out + [(top, 1.0)]


def _section_of(ents, title, scale):
    texts = [e for e in ents if e.dxftype() in ("TEXT", "MTEXT")]
    lines = []
    for e in ents:
        if e.dxftype() == "LINE":
            lines.append((e.dxf.start.x, e.dxf.start.y, e.dxf.end.x, e.dxf.end.y))
        elif e.dxftype() == "LWPOLYLINE":
            pts = [(p[0], p[1]) for p in e.get_points("xy")]
            if e.closed:
                pts.append(pts[0])
            lines += [(a[0], a[1], c[0], c[1]) for a, c in zip(pts, pts[1:])]
    # levels: a "... lvl" text and the horizontal line it labels (just below it, under or beside it)
    levels = []
    for t in texts:
        s = _plain(t)
        if not LEVEL.search(s) or TITLE.search(s):
            continue
        h = _height(t)
        tx, ty = t.dxf.insert.x, t.dxf.insert.y
        best = None
        for x0, y0, x1, y1 in lines:
            if abs(y0 - y1) > 1.0 or abs(x1 - x0) < 2.0 * h:
                continue
            if not (min(x0, x1) - 3 * h <= tx <= max(x0, x1) + 3 * h):
                continue
            dy = ty - y0
            if -1.5 * h <= dy <= 4.0 * h and (best is None or abs(dy) < abs(best[0])):
                best = (dy, y0)
        if best is not None:
            levels.append({"name": LEVEL.sub("", s).strip(" .-:"), "y": round(best[1], 1)})
    if not levels:
        return None
    # beam boxes: two diagonals crossing as an X (the drafted beam cut), or a closed rectangle
    diag = [(x0, y0, x1, y1) for x0, y0, x1, y1 in lines if abs(x1 - x0) > 1 and abs(y1 - y0) > 1]
    boxes = []
    for i, a in enumerate(diag):
        for b in diag[i + 1:]:
            ba = (min(a[0], a[2]), min(a[1], a[3]), max(a[0], a[2]), max(a[1], a[3]))
            bb = (min(b[0], b[2]), min(b[1], b[3]), max(b[0], b[2]), max(b[1], b[3]))
            if all(abs(p - q) <= 2.0 for p, q in zip(ba, bb)) and \
                    ((a[1] - a[3]) * (a[0] - a[2])) * ((b[1] - b[3]) * (b[0] - b[2])) < 0:
                boxes.append(ba)
    out = []
    for x0, y0, x1, y1 in boxes:
        if any(abs(x0 - o["x0"]) <= 2 and abs(y1 - o["top"]) <= 2 for o in out):
            continue
        w = x1 - x0
        mark = ""
        best = None
        for t in texts:
            s = _plain(t)
            mk = parse_mark(s.replace(" ", ""))
            if not mk or mk.family() not in ("beam", "unknown"):
                continue
            tx, ty = t.dxf.insert.x, t.dxf.insert.y
            d = min(abs(tx - x1), abs(tx - x0))
            if y0 - _height(t) <= ty <= y1 + _height(t) and d <= 4.0 * w and (best is None or d < best):
                best, mark = d, mk.key
        out.append({"mark": mark, "x0": round(x0, 1), "x1": round(x1, 1), "top": round(y1, 1), "bottom": round(y0, 1)})
    # a sloping slab: an inclined line from one beam box to another (the steel roof drawn
    # above, or a bar, does not end on two boxes)
    widest = max((o["x1"] - o["x0"] for o in out), default=0.0)

    def box_at(x, y):
        for i, o in enumerate(out):
            w = o["x1"] - o["x0"]
            if o["x0"] - w <= x <= o["x1"] + w and abs(y - o["top"]) <= 1.5 * w:
                return i
        return None
    slopes = []
    for ln in diag:
        if not (0.03 <= abs((ln[3] - ln[1]) / (ln[2] - ln[0])) <= 3.0) or \
                math.hypot(ln[2] - ln[0], ln[3] - ln[1]) <= 3.0 * max(widest, 1.0):
            continue
        i, j = box_at(ln[0], ln[1]), box_at(ln[2], ln[3])
        if i is not None and j is not None and i != j and \
                abs(out[i]["top"] - out[j]["top"]) > 0.03 * abs(out[i]["x0"] - out[j]["x0"]):
            pair = sorted((i, j))
            if pair not in [s["between"] for s in slopes]:
                slopes.append({"between": pair, "line": [round(float(v), 1) for v in ln]})
    return {"levels": levels, "beams": out, "slopes": slopes, "scale": round(scale, 6)}


def read(b: Building, rec) -> int:
    n = 0
    for ents, scale in _groups(rec):
        for t in [e for e in ents if e.dxftype() in ("TEXT", "MTEXT")]:
            m = TITLE.search(_plain(t))
            if not m:
                continue
            got = _section_of(ents, t, scale)
            if not got or not any(x["mark"] for x in got["beams"]):
                continue
            ev = Evidence(reader=READER, sheet=rec.id, source=f"section {m.group(1)}-{m.group(1)}",
                          layer=t.dxf.layer, text=_plain(t)[:60])
            b.add(Element(id=f"{rec.id}/building_section/{len(b.elements)}", type="building_section",
                          sheet=rec.id, mark=m.group(1).upper(),
                          props={"scale": Value(got["scale"], READ, 0.7, [ev])},
                          extra={k: v for k, v in got.items() if k != "scale"} | {"title": _plain(t)[:60]}))
            n += 1
    return n
