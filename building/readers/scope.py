"""Areas of a plan that are not this drawing's structure: another block, existing work.

A consultant often draws the neighbouring block or the existing building on the same
plan, for reference, and marks it - a hatch on a layer named for it (New block: an
ANSI31 hatch on layer 'Main Block' over the lifts, stair and corridor, whose beams
carry the old numbering and whose bays carry no slab marks). Read as structure, it
is modelled quietly with this block. Each such area is kept as a scope zone; the
linker marks what lies inside it for a person to decide - this block or the other -
and nothing is left out or kept without that decision.

Only the layer's name decides what an area is; a plan whose own title names that
block (the Main Block's own drawing) has no such zone.
"""
from __future__ import annotations

import re

from ..model import Building, Element, Evidence, Value, READ

READER = "scope"
OTHER = re.compile(r"\b(?:MAIN|OTHER|ADJ\w*|EXIST\w*|NEIGHBOU?R\w*|OLD)\s*[-_ ]?\s*"
                   r"(?:BLOCK|BLDG|BUILDING|WING|STRUCTURE)\b"
                   r"|\bEXISTING\b|\bN\.?\s?I\.?\s?C\b|\bNOT\s+IN\s+SCOPE\b|\bBY\s+OTHERS\b", re.I)
MIN_AREA_MM2 = 4e6          # smaller than 2 x 2 m: a symbol, not an area of a plan


def _area(pts) -> float:
    return abs(sum(pts[i][0] * pts[i - 1][1] - pts[i - 1][0] * pts[i][1] for i in range(len(pts)))) / 2.0


def _outlines(e) -> list[list[tuple[float, float]]]:
    """The boundary of a hatch (each path) or a closed polyline, as points."""
    t = e.dxftype()
    if t == "LWPOLYLINE":
        pts = [(p[0], p[1]) for p in e.get_points("xy")]
        closed = e.closed or (len(pts) > 3 and abs(pts[0][0] - pts[-1][0]) + abs(pts[0][1] - pts[-1][1]) < 1.0)
        return [pts] if closed and len(pts) >= 3 else []
    if t != "HATCH":
        return []
    out = []
    for path in e.paths:
        if hasattr(path, "vertices"):
            pts = [(v[0], v[1]) for v in path.vertices]
        else:
            pts = [(ed.start[0], ed.start[1]) for ed in getattr(path, "edges", []) if hasattr(ed, "start")]
        if len(pts) >= 3:
            out.append(pts)
    return out


def read(b: Building, rec) -> int:
    db = getattr(getattr(rec, "doc", None), "entitydb", None)
    view = getattr(rec, "view", None)
    if db is None or view is None:
        return 0
    title = (getattr(rec, "title", "") or "").upper()
    n = 0
    for h in view.handles:
        e = db.get(h)
        if e is None or e.dxftype() not in ("HATCH", "LWPOLYLINE"):
            continue
        layer = e.dxf.layer
        m = OTHER.search(layer)
        if not m or re.sub(r"\W+", " ", m.group(0).upper()).strip() in re.sub(r"\W+", " ", title):
            continue
        for pts in _outlines(e):
            if _area(pts) < MIN_AREA_MM2:
                continue
            ev = Evidence(reader=READER, sheet=rec.id, source=f"area on layer '{layer}'", layer=layer,
                          handle=e.dxf.handle, text=layer)
            b.add(Element(id=f"{rec.id}/scope_zone/{len(b.elements)}", type="scope_zone", sheet=rec.id,
                          level=rec.levels[0] if len(getattr(rec, "levels", []) or []) == 1 else "",
                          wing=getattr(rec, "wing", ""), geometry={"outline": [list(p) for p in pts]},
                          props={"kind": Value("other block", READ, 0.8, [ev])}, extra={"layer": layer}))
            n += 1
    return n
