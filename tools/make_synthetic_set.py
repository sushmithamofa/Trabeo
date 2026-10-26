r"""Write a small drawing set in a consultant style the tool was not built on, with its answers.

    python tools\make_synthetic_set.py OUT_FOLDER

Not one of the training conventions is used on purpose:
  * layers STR_GRID, STR_COLM, STR_BEAM, STR_ANNO, STR_TABLE (no BEAM/COLUMN layers as known);
  * zero-padded marks C01, B07, S2;
  * beam captions "B01 230x450" - the size after a space, no brackets;
  * schedules as ruled tables: BEAM MARK | WIDTH | DEPTH, COL. NO. | SIZE, SLAB MARK | THK;
  * a section with level names and "+3.300" values written beside them.

Writes: KA-101 FIRST FLOOR FRAMING PLAN.dxf, KA-501 SCHEDULES.dxf, KA-601 SECTION A-A.dxf and
answers.json (what a person reads off them) for tools\scorecard.py.
"""
from __future__ import annotations

import json
import os
import sys

import ezdxf

XS = [0.0, 4500.0, 9000.0, 13500.0]
YS = [0.0, 5000.0, 10000.0]


def columns():
    out, n = [], 0
    for j, y in enumerate(YS):
        for i, x in enumerate(XS):
            n += 1
            inner = 0 < i < len(XS) - 1 and 0 < j < len(YS) - 1
            w, d = (300.0, 600.0) if inner else (300.0, 450.0)
            out.append({"mark": f"C{n:02d}", "x": x, "y": y, "width": w, "depth": d})
    return out


def beams():
    out, n = [], 0
    for j, y in enumerate(YS):                          # along x
        for i in range(len(XS) - 1):
            n += 1
            w, d = (300.0, 600.0) if j == 1 else (230.0, 450.0)
            out.append({"mark": f"B{n:02d}", "a": (XS[i], y), "b": (XS[i + 1], y), "width": w, "depth": d})
    for i, x in enumerate(XS):                          # along y
        for j in range(len(YS) - 1):
            n += 1
            out.append({"mark": f"B{n:02d}", "a": (x, YS[j]), "b": (x, YS[j + 1]), "width": 230.0, "depth": 500.0})
    return out


def slabs():
    out = []
    for j in range(len(YS) - 1):
        for i in range(len(XS) - 1):
            mark = "S2" if i == 1 else "S1"
            out.append({"mark": mark, "x0": XS[i], "y0": YS[j], "x1": XS[i + 1], "y1": YS[j + 1],
                        "thickness": 150.0 if mark == "S2" else 125.0})
    return out


def new_doc():
    doc = ezdxf.new("R2018", setup=True)
    doc.header["$INSUNITS"] = 4
    for name, col in (("STR_GRID", 8), ("STR_COLM", 1), ("STR_BEAM", 3), ("STR_ANNO", 7), ("STR_TABLE", 7),
                      ("STR_DIM", 2), ("STR_SLAB", 5)):
        doc.layers.add(name, color=col)
    return doc


def text(msp, s, x, y, h=150.0, layer="STR_ANNO", rot=0.0):
    msp.add_text(s, height=h, rotation=rot, dxfattribs={"layer": layer, "insert": (x, y)})


def draw_plan(msp, dx=0.0, title="FIRST FLOOR FRAMING PLAN"):
    """One framing plan into model space, dx to the right (several floors side by side)."""
    cols = columns()
    half = {(c["x"], c["y"]): (c["width"] / 2, c["depth"] / 2) for c in cols}
    for i, x in enumerate(XS):
        msp.add_line((dx + x, -2000), (dx + x, YS[-1] + 2000), dxfattribs={"layer": "STR_GRID"})
        msp.add_circle((dx + x, YS[-1] + 2400), 350, dxfattribs={"layer": "STR_GRID"})
        text(msp, "ABCD"[i], dx + x - 90, YS[-1] + 2320, 200, "STR_GRID")
    for j, y in enumerate(YS):
        msp.add_line((dx - 2000, y), (dx + XS[-1] + 2000, y), dxfattribs={"layer": "STR_GRID"})
        msp.add_circle((dx - 2400, y), 350, dxfattribs={"layer": "STR_GRID"})
        text(msp, str(j + 1), dx - 2480, y - 90, 200, "STR_GRID")
    for c in cols:
        hx, hy = half[(c["x"], c["y"])]
        cx = dx + c["x"]
        msp.add_lwpolyline([(cx - hx, c["y"] - hy), (cx + hx, c["y"] - hy), (cx + hx, c["y"] + hy),
                            (cx - hx, c["y"] + hy)], close=True, dxfattribs={"layer": "STR_COLM"})
        msp.add_hatch(color=1, dxfattribs={"layer": "STR_COLM"}).paths.add_polyline_path(
            [(cx - hx, c["y"] - hy), (cx + hx, c["y"] - hy), (cx + hx, c["y"] + hy),
             (cx - hx, c["y"] + hy)], is_closed=True)
        text(msp, c["mark"], cx + hx + 120, c["y"] + hy + 120, 150)
    for bm in beams():
        (ax, ay), (bx, by) = bm["a"], bm["b"]
        w = bm["width"]
        if ay == by:                                   # along x, face to face of the columns
            x0, x1 = dx + ax + half[bm["a"]][0], dx + bx - half[bm["b"]][0]
            for s in (-1, 1):
                msp.add_line((x0, ay + s * w / 2), (x1, ay + s * w / 2), dxfattribs={"layer": "STR_BEAM"})
            text(msp, f"{bm['mark']} {w:.0f}x{bm['depth']:.0f}", (x0 + x1) / 2 - 600, ay + w / 2 + 120, 150)
        else:
            y0, y1 = ay + half[bm["a"]][1], by - half[bm["b"]][1]
            for s in (-1, 1):
                msp.add_line((dx + ax + s * w / 2, y0), (dx + ax + s * w / 2, y1), dxfattribs={"layer": "STR_BEAM"})
            text(msp, f"{bm['mark']} {w:.0f}x{bm['depth']:.0f}", dx + ax - w / 2 - 120, (y0 + y1) / 2 - 600, 150,
                 rot=90.0)
    for s in slabs():
        text(msp, s["mark"], dx + (s["x0"] + s["x1"]) / 2 - 100, (s["y0"] + s["y1"]) / 2, 200)
    text(msp, title, dx + 2000, -3500, 400)
    text(msp, "ALL DIMENSIONS ARE IN MM. CONCRETE M25, STEEL FE500.", dx + 2000, -4200, 180)


def plan(path):
    doc = new_doc()
    draw_plan(doc.modelspace())
    doc.saveas(path)


def table(msp, x, y, title, head, rows, colw=1600.0, rowh=500.0):
    text(msp, title, x, y + 300, 250, "STR_TABLE")
    n = len(rows) + 1
    for r in range(n + 1):
        msp.add_line((x, y - r * rowh), (x + colw * len(head), y - r * rowh), dxfattribs={"layer": "STR_TABLE"})
    for c in range(len(head) + 1):
        msp.add_line((x + c * colw, y), (x + c * colw, y - n * rowh), dxfattribs={"layer": "STR_TABLE"})
    for r, row in enumerate([head] + rows):
        for c, v in enumerate(row):
            text(msp, str(v), x + c * colw + 150, y - r * rowh - rowh / 2 - 60, 130, "STR_TABLE")


def schedules(path):
    doc = new_doc()
    msp = doc.modelspace()
    table(msp, 0, 0, "BEAM SCHEDULE", ["BEAM MARK", "WIDTH", "DEPTH", "REMARKS"],
          [[b["mark"], f"{b['width']:.0f}", f"{b['depth']:.0f}", ""] for b in beams()])
    cols = {}
    for c in columns():
        cols.setdefault((c["width"], c["depth"]), []).append(c["mark"])
    table(msp, 8000, 0, "COLUMN SCHEDULE", ["COL. NO.", "SIZE"],
          [[m, f"{w:.0f} X {d:.0f}"] for (w, d), ms in sorted(cols.items()) for m in ms], colw=2200)
    table(msp, 14000, 0, "SLAB SCHEDULE", ["SLAB MARK", "THK"], [["S1", "125"], ["S2", "150"]])
    doc.saveas(path)


def section(path):
    doc = new_doc()
    msp = doc.modelspace()
    for name, z in (("GROUND FLOOR LVL", 0.0), ("FIRST FLOOR LVL", 3300.0), ("SECOND FLOOR LVL", 6600.0)):
        msp.add_line((0, z), (14000, z), dxfattribs={"layer": "STR_BEAM"})
        text(msp, name, 14500, z + 60, 180)
        text(msp, "±0.000" if z == 0 else f"+{z / 1000:.3f}", 18500, z + 60, 180)
    for x in XS:
        msp.add_line((x, 0), (x, 6600), dxfattribs={"layer": "STR_COLM"})
    text(msp, "SECTION A-A", 5000, -1500, 400)
    doc.saveas(path)


def answers():
    return {"columns": [{"mark": c["mark"], "width": c["width"], "depth": c["depth"]} for c in columns()],
            "beams": [{"mark": b["mark"], "width": b["width"], "depth": b["depth"]} for b in beams()],
            "slabs": [{"mark": "S1", "thickness": 125}, {"mark": "S2", "thickness": 150}],
            "levels": [{"level": "GF", "elevation": 0}, {"level": "L1", "elevation": 3300},
                       {"level": "L2", "elevation": 6600}]}


def main(out):
    os.makedirs(out, exist_ok=True)
    plan(os.path.join(out, "KA-101 FIRST FLOOR FRAMING PLAN.dxf"))
    schedules(os.path.join(out, "KA-501 SCHEDULES.dxf"))
    section(os.path.join(out, "KA-601 SECTION A-A.dxf"))
    with open(os.path.join(os.path.dirname(os.path.abspath(out)), os.path.basename(out) + "_answers.json"),
              "w", encoding="utf-8") as f:
        json.dump(answers(), f, indent=1)
    print(f"Wrote 3 drawings to {out} and the answers beside the folder")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else os.path.join("samples", "synthetic", "KA_consultant"))
