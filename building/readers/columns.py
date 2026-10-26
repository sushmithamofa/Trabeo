"""Column positions from column layouts and foundation plans, with their marks.

Framing plans often show columns without marks (Sasan Gir); the marks are on a
separate column layout. This reader records every marked column position there,
so the linker can give each plan column the mark of the column at the same place.

A column is a closed outline 150-2000 mm a side with a column-family mark inside
or beside it. A small box drawn round a mark text - a tag box (Sasan Gir's
274 x 439 boxes round "C1") - is not a column: an outline whose size is about
that of the text it encloses is skipped.
"""
from __future__ import annotations

import math

from ..marks import labels_in
from ..model import Building, Element, Evidence, Value, READ
from .foundations import SKIP_LAYER, _inside, _rect, _size

READER = "column_positions"
KINDS = ("column_layout", "foundation_plan")
SIDE = (150.0, 2000.0)


def read(b: Building, rec) -> dict[str, int]:
    if rec.doc is None or rec.verdict.kind not in KINDS:
        return {}
    hs = set(rec.view.handles)
    rects = [r for e in rec.doc.modelspace() if e.dxf.handle in hs and not SKIP_LAYER.search(e.dxf.layer)
             and (r := _rect(e)) and SIDE[0] <= min(_size(r)) and max(_size(r)) <= SIDE[1]]
    marks = []
    for t in rec.texts:
        if t.space != "model":
            continue
        for lb in labels_in(t.text):
            if lb.mark and lb.mark.family() in ("column", "wall") and lb.mark.number:
                marks.append((t, lb.mark))
    n = 0
    taken = set()
    for r in rects:
        w, h = _size(r)
        inside = [(t, m) for t, m in marks if _inside((t.x, t.y), r)]
        # a tag box: about the size of the text it holds
        if inside and any(w <= 2.5 * 0.8 * t.height * max(len(t.text), 1) and h <= 4 * t.height
                          for t, _ in inside):
            continue
        if inside:
            t, m = inside[0]
        else:
            near = [(math.hypot(t.x - r[4], t.y - r[5]) - 0.5 * math.hypot(w, h), t, m) for t, m in marks]
            near = [x for x in near if x[0] <= max(600.0, 4 * x[1].height)]
            if not near:
                continue
            _, t, m = min(near, key=lambda x: x[0])
        if (round(r[4]), round(r[5])) in taken:
            continue
        taken.add((round(r[4]), round(r[5])))
        ev = Evidence(reader=READER, sheet=rec.id, source="column layout", layer=t.layer,
                      handle=t.handle, at=[t.x, t.y], text=t.text)
        b.add(Element(id=f"{rec.id}/column_position/{len(b.elements)}", type="column_position",
                      sheet=rec.id, level=rec.levels[0] if len(rec.levels) == 1 else "", wing=rec.wing,
                      mark=m.key, geometry={"x": r[4], "y": r[5], "outline": [list(p) for p in r[8]]},
                      props={"mark": Value(m.key, READ, 1.0, [ev]),
                             "family": Value(m.family(), READ, 1.0, [ev]),
                             "width": Value(round(min(w, h)), READ, 0.8, [ev]),
                             "depth": Value(round(max(w, h)), READ, 0.8, [ev])}))
        n += 1
    # marks drawn in tag boxes beside their columns (most of Sasan Gir's column
    # layout): kept as labelled points - they name a column near them, they do not
    # place one, so they mark column stacks but never align plans
    labels = 0
    used_texts = {e.props["mark"].evidence[0].handle for e in b.of("column_position") if e.sheet == rec.id}
    for t, m in marks:
        if t.handle in used_texts:
            continue
        ev = Evidence(reader=READER, sheet=rec.id, source="column layout label", layer=t.layer,
                      handle=t.handle, at=[t.x, t.y], text=t.text)
        b.add(Element(id=f"{rec.id}/column_position/{len(b.elements)}", type="column_position",
                      sheet=rec.id, level=rec.levels[0] if len(rec.levels) == 1 else "", wing=rec.wing,
                      mark=m.key, geometry={"x": t.cx if t.cx is not None else t.x,
                                            "y": t.cy if t.cy is not None else t.y},
                      props={"mark": Value(m.key, READ, 0.7, [ev]),
                             "family": Value(m.family(), READ, 1.0, [ev])},
                      extra={"label_only": True}))
        labels += 1
    out = {"column positions": n, "column labels": labels}
    return {k: v for k, v in out.items() if v}
