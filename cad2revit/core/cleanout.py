"""Write the cleaned DXF.

Nothing is copied across from the source drawing. Every entity here is rebuilt
from what the extractor understood, which is the point: if a shape is not in this
file, the reader did not understand it, and the audit says why.

The result is flat at Z = 0, on standard layer names, with beams as true
centrelines and every outline closed.
"""
from __future__ import annotations

import ezdxf

from .model import Result

LAYER_COLOURS = {
    "S-COL": 1, "S-COL-STOP": 12, "S-COL-FLOAT": 30, "S-WALL": 2,
    "S-BEAM": 4, "S-GIRDER": 5, "S-SLAB": 3, "S-OPEN": 8, "S-STAIR": 8,
    "S-GRID": 7, "S-REF": 6,
    "S-COL-TAG": 1, "S-WALL-TAG": 2, "S-BEAM-TAG": 4, "S-GIRDER-TAG": 5,
    "S-SLAB-TAG": 3,
}
TAG_HEIGHT = 250.0


def write_clean_dxf(res: Result, out_path: str, cfg: dict, local: bool = True) -> str:
    """local=True shifts everything so the agreed base point becomes 0,0."""
    ox, oy = res.base_point if local else (0.0, 0.0)

    def P(p):
        return (p[0] - ox, p[1] - oy)

    doc = ezdxf.new("R2010", setup=True)
    doc.header["$INSUNITS"] = 4          # millimetres
    doc.header["$MEASUREMENT"] = 1
    doc.header["$LUNITS"] = 2
    msp = doc.modelspace()

    out = cfg["output_layers"]
    wanted = set(LAYER_COLOURS) | set(out.values())
    for name in sorted(wanted):
        for depth in ():
            pass
        if name not in doc.layers:
            doc.layers.add(name, color=LAYER_COLOURS.get(name, 7))

    for z in res.sunk:
        name = f"S-SUNK-{int(z.depth_mm)}" if z.depth_mm else "S-SUNK-UNKNOWN"
        if name not in doc.layers:
            doc.layers.add(name, color=51)

    def poly(pts, layer):
        msp.add_lwpolyline([P(p) for p in pts], close=True, dxfattribs={"layer": layer})

    def text(s, p, layer, h=TAG_HEIGHT):
        if not s:
            return
        t = msp.add_text(s, dxfattribs={"layer": layer, "height": h})
        t.set_placement(P(p))

    # columns ------------------------------------------------------------
    for c in res.columns:
        layer = {"column": out["column"], "column_stop": out["column_stop"],
                 "column_float": out["column_float"]}.get(c.kind, out["column"])
        poly(c.outline, layer)
        text(c.mark, (c.x, c.y), out["column_tag"])

    # walls --------------------------------------------------------------
    for w in res.walls:
        poly(w.outline, out["wall"])
        if w.mark:
            cx = sum(p[0] for p in w.outline) / len(w.outline)
            cy = sum(p[1] for p in w.outline) / len(w.outline)
            text(w.mark, (cx, cy), out["wall_tag"])

    # beams --------------------------------------------------------------
    for b in res.beams:
        layer = out["beam"] if b.kind == "beam" else out["girder"]
        if b.mid:
            from ezdxf.math import ConstructionArc
            s, m, e = P(b.start), P(b.mid), P(b.end)
            # from_3p runs counter-clockwise from its first point; turn round a
            # clockwise curve, or it draws the other part of the circle.
            if (m[0] - s[0]) * (e[1] - m[1]) - (m[1] - s[1]) * (e[0] - m[0]) < 0:
                s, e = e, s
            arc = ConstructionArc.from_3p(s, e, m)
            arc.add_to_layout(msp, dxfattribs={"layer": layer})
        else:
            msp.add_line(P(b.start), P(b.end), dxfattribs={"layer": layer})
        if b.mark:
            mid = b.mid or ((b.start[0] + b.end[0]) / 2.0, (b.start[1] + b.end[1]) / 2.0)
            text(b.mark, mid, out["beam_tag"] if b.kind == "beam" else out["girder_tag"])

    # slabs, sunk, openings, grid ---------------------------------------
    for s in res.slabs:
        poly(s.outline, out["slab"])
        if s.mark:
            cx = sum(p[0] for p in s.outline) / len(s.outline)
            cy = sum(p[1] for p in s.outline) / len(s.outline)
            text(s.mark, (cx, cy), out["slab_tag"], 300.0)

    for t in res.slab_tags:
        if not res.slabs:                    # keep the marks even with no panels
            text(t.mark, (t.x, t.y), out["slab_tag"], 300.0)

    for z in res.sunk:
        name = f"S-SUNK-{int(z.depth_mm)}" if z.depth_mm else "S-SUNK-UNKNOWN"
        poly(z.outline, name)

    for o in res.openings:
        poly(o.outline, out["opening"] if o.kind == "opening" else out["stair"])

    for g in res.grids:
        msp.add_line(P(g.start), P(g.end), dxfattribs={"layer": out["grid"]})

    # base point marker ---------------------------------------------------
    if "S-REF" not in doc.layers:
        doc.layers.add("S-REF", color=6)
    msp.add_line(P((ox - 500, oy)), P((ox + 500, oy)), dxfattribs={"layer": "S-REF"})
    msp.add_line(P((ox, oy - 500)), P((ox, oy + 500)), dxfattribs={"layer": "S-REF"})
    tb = msp.add_text("PROJECT BASE POINT", dxfattribs={"layer": "S-REF", "height": 200})
    tb.set_placement(P((ox + 600, oy + 200)))

    doc.saveas(out_path)
    return out_path
