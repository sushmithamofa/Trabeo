"""How each line of a plan is drawn: dashed or solid. Drafting says what it means.

On a framing plan a beam's faces under the slab are drawn dashed (hidden): the slab
covers them. A face drawn solid is seen from above - nothing covers it: the slab
ends there (the building's edge, a shaft, a cut-out) or the beam stands above the
slab (an upturned beam). A line inside a beam (Main Block terrace: the slope of
SECTION X-X between a solid outer face and a dashed inner one) is an edge of that
beam, not a third face.

Kept per plan view: the straight lines on beam-like layers (the linker matches them
to the beams' faces), each with its layer and whether it is dashed. A line's style
is its own linetype, else its layer's; a linetype is dashed when its pattern has
gaps - whatever it is called (CONTINOUS, '1' and Continuous are all solid).
"""
from __future__ import annotations

import math
import re

READER = "linework"
PLAN_KINDS = ("framing_plan",)
BEAMISH = re.compile(r"BEAM|BM\b|GIRDER|\bSB\b|^SB|LINTEL", re.I)
MIN_MM = 300.0
WALLISH = re.compile(r"WALL|SHEAR|\bRCC?\s*W", re.I)       # RC walls also close slab bays
MASONRY = re.compile(r"BRICK|BLOCK|MASON|PARTITION|AAC", re.I)


def _dashed_types(doc) -> set:
    out = set()
    for lt in doc.linetypes:
        try:
            if any(t.code == 49 and float(t.value) < 0 for t in lt.pattern_tags.tags):
                out.add(lt.dxf.name.upper())
        except Exception:
            continue
    return out


def read(b, rec) -> int:
    if getattr(getattr(rec, "verdict", None), "kind", "") not in PLAN_KINDS:
        return 0
    doc = getattr(rec, "doc", None)
    db = getattr(doc, "entitydb", None)
    view = getattr(rec, "view", None)
    if db is None or view is None or rec.id not in b.sheets:
        return 0
    dashed = _dashed_types(doc)
    layer_lt = {}
    lines, layers = [], []
    walls = []                                    # RC wall layers' straight lines (linker._marked_bays)
    for h in view.handles:
        e = db.get(h)
        if e is None or e.dxftype() not in ("LINE", "LWPOLYLINE"):
            continue
        lay = e.dxf.layer
        if WALLISH.search(lay) and not MASONRY.search(lay):
            if e.dxftype() == "LINE":
                wp = [(e.dxf.start.x, e.dxf.start.y), (e.dxf.end.x, e.dxf.end.y)]
            else:
                wp = [(p[0], p[1]) for p in e.get_points("xy")]
                if e.closed and wp:
                    wp.append(wp[0])
            walls += [[round(a[0]), round(a[1]), round(c[0]), round(c[1])] for a, c in zip(wp, wp[1:])
                      if math.dist(a, c) >= MIN_MM]
        if not BEAMISH.search(lay):
            continue
        name = (e.dxf.get("linetype", "BYLAYER") or "BYLAYER").upper()
        if name == "BYLAYER":
            if lay not in layer_lt:
                try:
                    layer_lt[lay] = (doc.layers.get(lay).dxf.linetype or "CONTINUOUS").upper()
                except Exception:
                    layer_lt[lay] = "CONTINUOUS"
            name = layer_lt[lay]
        d = 1 if name in dashed else 0
        if e.dxftype() == "LINE":
            segs = [((e.dxf.start.x, e.dxf.start.y), (e.dxf.end.x, e.dxf.end.y))]
        else:
            pts = [(p[0], p[1]) for p in e.get_points("xy")]
            if e.closed:
                pts.append(pts[0])
            segs = list(zip(pts, pts[1:]))
        if lay not in layers:
            layers.append(lay)
        for a, c in segs:
            if math.dist(a, c) >= MIN_MM:
                lines.append([layers.index(lay), round(a[0]), round(a[1]), round(c[0]), round(c[1]), d])
    if lines:
        b.sheets[rec.id].extra["beam_lines"] = {"layers": layers, "lines": lines}
    if walls:
        b.sheets[rec.id].extra["wall_lines"] = walls
    return len(lines)
