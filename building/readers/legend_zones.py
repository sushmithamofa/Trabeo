"""The 'THUS MARKED ...' legend: every row, and every place on the plan drawn like its sample.

A plan's legend says what a hatch means: "*THUS MARKED SLOPING RCC SLAB AS PER ARCH.",
"*THUS MARKED FABRICATION ROOF AS PER ARCH. PROFILE.", "*THUS MARKED RCC COLUMN THROUGH.",
"*THUS MARKED RCC COLUMN ENDS AT THIS LVL." (Sasan Gir Main Block terrace). The sunk rows
("... 150 mm SUNK") are read by the plan reader (legend.find_thus_marked); here every row
is kept, so none is silently ignored, and its hatch is found again on the plan - inside
blocks too (the Main Block's column hatches are in block 'Col_Terrace lvl', the steel
roofs in 'new rf').

    legend_zone  one per hatched area: kind (sloping slab / steel roof / column through /
                 column ends / cut out / other), the row's words, the outline
    legend_row   one per row whose sample is found nowhere on the plan (said, not dropped)
"""
from __future__ import annotations

import math
import re

from ..model import Building, Element, Evidence, Value, READ

READER = "legend_zones"
ROW = re.compile(r"THUS\s+MARKED\s+(.+)", re.I)
KINDS = [("sunk", r"\bSUNK\b|\bDOWN\b"), ("sloping slab", r"SLOP"), ("steel roof", r"FABRICAT|STEEL\s+ROOF|SHEET"),
         ("column ends", r"COLUMN.*\bENDS?\b|\bENDS?\b.*COLUMN"), ("column through", r"COLUMN.*THROUGH"),
         ("cut out", r"CUT\s*-?\s*OUT|OPENING|VOID|DUCT")]


def kind_of(words: str) -> str:
    return next((k for k, rx in KINDS if re.search(rx, words, re.I)), "other")


def _walk(es, depth=0):
    for e in es:
        if e.dxftype() == "INSERT" and depth < 3:
            try:
                yield from _walk(e.virtual_entities(), depth + 1)
            except Exception:
                continue
        else:
            yield e


def _outline(h):
    for path in h.paths:
        pts = [(p[0], p[1]) for p in getattr(path, "vertices", [])] or \
              [(ed.start[0], ed.start[1]) for ed in getattr(path, "edges", []) if hasattr(ed, "start")]
        if len(pts) >= 3:
            return pts
    return None


def _bbox(pts):
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def read(b: Building, rec) -> int:
    if getattr(getattr(rec, "verdict", None), "kind", "") != "framing_plan":
        return 0
    db = getattr(getattr(rec, "doc", None), "entitydb", None)
    view = getattr(rec, "view", None)
    if db is None or view is None:
        return 0
    ents = list(_walk([e for e in (db.get(h) for h in view.handles) if e is not None]))
    rows = []
    for e in ents:
        if e.dxftype() not in ("TEXT", "MTEXT"):
            continue
        try:
            s = e.dxf.text if e.dxftype() == "TEXT" else e.plain_text()
        except Exception:
            continue
        m = ROW.search(" ".join(re.sub(r"%%[UuOo]", "", s or "").split()))
        if m:
            h = float(e.dxf.height if e.dxftype() == "TEXT" else e.dxf.char_height or 100.0)
            rows.append((m.group(1).strip(" .*"), e.dxf.insert.x, e.dxf.insert.y, h))
    if not rows:
        return 0
    hatches = []
    for e in ents:
        if e.dxftype() == "HATCH":
            pts = _outline(e)
            if pts:
                hatches.append((e, pts, _bbox(pts)))
    n = 0
    for words, x, y, h in rows:
        kind = kind_of(words)
        # the sample: a hatched box just left of the row's words, a few text heights big
        # (Main Block: 912 x 466 beside 100-high words) - not a small marker beside the text
        sample = None
        for e, pts, (x0, y0, x1, y1) in hatches:
            cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
            if not (2.0 * h <= x1 - x0 <= 20.0 * h and 1.5 * h <= y1 - y0 <= 10.0 * h):
                continue
            # level with its own words: within about half the box's height of their middle
            dy = abs(cy - (y + h / 2.0))
            if x - 25.0 * h < cx < x and dy < 0.6 * (y1 - y0) and (sample is None or dy < sample[1]):
                sample = (e, dy)
        if sample is None:
            if kind != "sunk":                            # drawn otherwise (the cut-out's X): said, not dropped
                ev = Evidence(reader=READER, sheet=rec.id, source="legend row", text=words[:80])
                b.add(Element(id=f"{rec.id}/legend_row/{len(b.elements)}", type="legend_row", sheet=rec.id,
                              mark=kind, props={"kind": Value(kind, READ, 0.9, [ev])},
                              extra={"words": words, "pattern": ""}))
                n += 1
            continue
        s = sample[0]
        key = (s.dxf.pattern_name.upper(), s.dxf.layer.upper() if kind.startswith("column") else "")
        found = 0
        for e, pts, (x0, y0, x1, y1) in hatches:
            if e is s or (e.dxf.pattern_name.upper(), e.dxf.layer.upper() if kind.startswith("column") else "") != key:
                continue
            if any(abs((x0 + x1) / 2.0 - rx) < 30.0 * rh and abs((y0 + y1) / 2.0 - ry) < 6.0 * rh
                   for _w, rx, ry, rh in rows):
                continue                                  # another row's sample in the legend
            ev = Evidence(reader=READER, sheet=rec.id, source=f"legend: THUS MARKED {words}", layer=e.dxf.layer,
                          handle=e.dxf.handle or "", text=words[:80])
            b.add(Element(id=f"{rec.id}/legend_zone/{len(b.elements)}", type="legend_zone", sheet=rec.id,
                          mark=kind, geometry={"outline": [list(p) for p in pts]},
                          props={"kind": Value(kind, READ, 0.9, [ev])},
                          extra={"words": words, "pattern": s.dxf.pattern_name}))
            found += 1
            n += 1
        if not found and kind != "sunk":
            ev = Evidence(reader=READER, sheet=rec.id, source="legend row", text=words[:80])
            b.add(Element(id=f"{rec.id}/legend_row/{len(b.elements)}", type="legend_row", sheet=rec.id, mark=kind,
                          props={"kind": Value(kind, READ, 0.9, [ev])},
                          extra={"words": words, "pattern": s.dxf.pattern_name}))
            n += 1
    return n
