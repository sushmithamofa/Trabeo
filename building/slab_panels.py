"""Slab panels from the beam bays, for plans that mark slabs but draw no slab outline.

Many consultants write S1, S2 in the middle of each bay and never draw the slab:
the beams are its edges. The plan reader (the extractor) finds slab panels only
when it knows which layer holds the slab marks; with every mark on one text layer
(the blind "KA" set) it finds none. So, on a plan where the plan reader found no
slab panel but slab marks are written:

* the beams' centrelines, each end carried to the centre of the column it stops
  at (within reach), are joined into a network;
* every closed bay of that network holding a slab mark is a slab panel of that
  mark - the linker then gives it its thickness from the slab schedule;
* a bay with no mark is left alone (a void, a shaft, a stair well).

The panels are marked generated: the plan reader's own output is never changed,
and a plan where it found slabs is never touched.
"""
from __future__ import annotations

import math

from .model import Building, Element, Evidence, Value, READ

READER = "slab_panels"


def _snap(p, cols, reach):
    best = min(cols, key=lambda c: math.dist(p, c), default=None)
    return best if best is not None and math.dist(p, best) <= reach else p


def make(b: Building, stats) -> None:
    try:
        from shapely.geometry import LineString, Point
        from shapely.ops import polygonize, unary_union
    except Exception:
        return
    b.elements = [e for e in b.elements if not (e.type == "slab" and e.extra.get("generated"))]
    readings = {}
    for sid, s in b.sheets.items():
        if s.reader == "plan.legacy":
            readings[sid] = s.extra.get("view_sheet") or ""
    for sid, view in readings.items():
        els = [e for e in b.elements if e.sheet == sid]
        if any(e.type == "slab" for e in els):
            continue
        caps = [c for c in b.of("caption") if c.sheet == view and c.get("family") == "slab" and c.mark]
        if len(caps) < 2:
            continue
        cols = [(e.geometry["x"], e.geometry["y"]) for e in els if e.type == "column" and e.geometry.get("x") is not None]
        lines = []
        for e in els:
            if e.type != "beam" or not e.geometry.get("start") or not e.geometry.get("end") \
                    or e.extra.get("merged_into"):
                continue
            w = float(e.get("width") or 230.0)
            reach = max(600.0, 2 * w)
            a = _snap(tuple(e.geometry["start"]), cols, reach)
            c = _snap(tuple(e.geometry["end"]), cols, reach)
            if math.dist(a, c) > 1.0:
                lines.append(LineString([a, c]))
        if len(lines) < 3:
            continue
        bays = list(polygonize(unary_union(lines)))
        made = 0
        for poly in bays:
            inside = [c for c in caps if poly.contains(Point(*c.geometry["at"]))]
            marks = sorted({c.mark for c in inside})
            if len(marks) != 1 or poly.area < 1.0e6:
                continue                   # unmarked, or two marks in one bay: not guessed
            cap = inside[0]
            ev = Evidence(reader=READER, sheet=sid, source="beam bay with a slab mark", at=list(cap.geometry["at"]),
                          text=f"{marks[0]} in a {poly.area / 1e6:.1f} m2 bay")
            outline = [[round(x, 1), round(y, 1)] for x, y in list(poly.exterior.coords)[:-1]]
            b.add(Element(id=f"{sid}/slab/g{made}", type="slab", sheet=sid, level=b.sheets[sid].level,
                          wing=b.sheets[sid].wing, mark=marks[0], geometry={"outline": outline, "holes": []},
                          props={"mark": Value(marks[0], READ, 0.8, [ev])},
                          extra={"generated": True, "_layer": ""}))
            made += 1
        if made:
            stats["slab panels from beam bays"] += made
