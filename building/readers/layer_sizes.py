"""A size in a layer's name: an optional hint, never a requirement.

Some consultants put every beam size on its own layer (Sasan Gir: BEAM-230X600,
BEAM-450X750, S-B5-230X600). Where they do, that is good evidence for every
element drawn on the layer; where they do not, this reader simply finds nothing.
Nothing else depends on it - it is one more independent source the linker can
weigh against captions, details and schedules.
"""
from __future__ import annotations

import re

from ..model import Building, Element, Evidence, Value, READ

READER = "layer_sizes"
SIZE_IN_NAME = re.compile(r"(?<!\d)(\d{2,4})\s*[xX×]\s*(\d{2,4})(?!\d)")
FAMILY_WORDS = [("beam", r"BEAM|\bBM\b|GIRDER|(?<![A-Z])B\d"), ("column", r"COL"),
                ("wall", r"WALL|PARDI"), ("footing", r"FOOT|FTG"), ("slab", r"SLAB")]


def read(b: Building, rec) -> int:
    n = 0
    # blocks inserted on a layer: then its name is not only a beam's (New block: the
    # stair block on BEAM-230X1325), and a size taken from it alone needs confirming
    blocks: dict[str, int] = {}
    db = getattr(getattr(rec, "doc", None), "entitydb", None)
    view = getattr(rec, "view", None)
    if db is not None and view is not None:
        for h in view.handles:
            e = db.get(h)
            if e is not None and e.dxftype() == "INSERT":
                blocks[e.dxf.layer] = blocks.get(e.dxf.layer, 0) + 1
    for layer, count in rec.layers.items():
        m = SIZE_IN_NAME.search(layer)
        if not m:
            continue
        up = layer.upper()
        family = next((f for f, rx in FAMILY_WORDS if re.search(rx, up)), "unknown")
        ev = Evidence(reader=READER, sheet=rec.id, source="layer name", layer=layer, text=layer)
        b.add(Element(
            id=f"{rec.id}/layer_size/{len(b.elements)}", type="layer_size", sheet=rec.id,
            level=rec.levels[0] if len(rec.levels) == 1 else "", wing=rec.wing,
            props={"width": Value(float(m.group(1)), READ, 0.9, [ev]),
                   "depth": Value(float(m.group(2)), READ, 0.9, [ev]),
                   "family": Value(family, READ, 0.7, [ev]),
                   "entities": Value(count, READ, 1.0, [ev])},
            extra={"layer": layer, **({"blocks": blocks[layer]} if blocks.get(layer) else {})}))
        n += 1
    return n
