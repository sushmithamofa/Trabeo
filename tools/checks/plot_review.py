"""Draw one plan like Review does: beams by status (green read, yellow missing, red conflict),
curves through their midpoint, slabs filled, the CAD (blocks opened) underneath.
usage: plot_review.py DXF BUILDING_JSON PLAN_SHEET_ID OUT.png [xmin,ymin,xmax,ymax] [--ids]"""
import json
import math
import sys

import ezdxf
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Polygon as MP

dxf, bj, sid, out = sys.argv[1:5]
win = [float(v) for v in sys.argv[5].split(",")] if len(sys.argv) > 5 and "," in sys.argv[5] else None
show_ids = "--ids" in sys.argv
B = json.load(open(bj, encoding="utf-8"))
els = [e for e in B["elements"] if e["sheet"] == sid and not (e.get("extra") or {}).get("merged_into")]
COL = {"read": "#2e8b57", "low": "#d98a1c", "conflict": "#c0392b", "missing": "#c9a600", "override": "#7b5fb4"}


def status(e):
    r = e.get("resolved") or {}
    rank = {"read": 0, "override": 1, "low": 2, "missing": 3, "conflict": 4}
    w = "read"
    for v in r.values():
        s = v.get("status", "read")
        if rank.get(s, 0) > rank[w]:
            w = s
    return w


pts = [p for e in els for p in ((e["geometry"].get("outline") or []) +
                                [e["geometry"].get(k) for k in ("start", "end") if e["geometry"].get(k)])]
box = win or [min(p[0] for p in pts) - 1000, min(p[1] for p in pts) - 1000,
              max(p[0] for p in pts) + 1000, max(p[1] for p in pts) + 1000]
inside = lambda x, y: box[0] <= x <= box[2] and box[1] <= y <= box[3]   # noqa: E731
fig, ax = plt.subplots(figsize=(14, 14) if win else (40, 40))


def draw(e, depth=0):
    t = e.dxftype()
    try:
        if t == "INSERT" and depth < 3:
            for v in e.virtual_entities():
                draw(v, depth + 1)
            return
        if t == "LINE":
            P = [e.dxf.start, e.dxf.end]
        elif t == "LWPOLYLINE":
            P = [ezdxf.math.Vec3(p[0], p[1], 0) for p in e.get_points()]
            if e.closed:
                P.append(P[0])
        elif t in ("ARC", "CIRCLE", "SPLINE", "ELLIPSE"):
            P = list(e.flattening(10))
        elif t == "POLYLINE":
            P = list(e.points())
        elif t in ("TEXT", "MTEXT") and win:
            s = e.dxf.text if t == "TEXT" else e.plain_text()
            if inside(e.dxf.insert.x, e.dxf.insert.y):
                ax.text(e.dxf.insert.x, e.dxf.insert.y, s, fontsize=6, color="#777", clip_on=True)
            return
        else:
            return
        if any(inside(p.x, p.y) for p in P):
            lay = e.dxf.layer.upper()
            ax.plot([p.x for p in P], [p.y for p in P], lw=0.5 if "BEAM" in lay else 0.3,
                    color="#d33" if "BEAM" in lay else "#999")
    except Exception:
        pass


for e in ezdxf.readfile(dxf).modelspace():
    draw(e)
for e in els:
    g = e["geometry"]
    c = COL.get(status(e), "k")
    if e["type"] == "slab" and g.get("outline"):
        ax.add_patch(MP(g["outline"], closed=True, fc=c, alpha=0.15, ec=c, lw=0.8))
        if win:
            xs = [p[0] for p in g["outline"]]; ys = [p[1] for p in g["outline"]]
            ax.text(sum(xs) / len(xs), sum(ys) / len(ys), (e.get("mark") or "?") + (f" [{e['id'].split('/')[-1]}]" if show_ids else ""),
                    fontsize=8, color=c, clip_on=True)
    elif e["type"] in ("beam", "beam_span") and g.get("start"):
        P = [g["start"], g["mid"], g["end"]] if g.get("mid") else [g["start"], g["end"]]
        ax.plot([p[0] for p in P], [p[1] for p in P], color=c, lw=3, alpha=0.55)
        if win:
            m = P[len(P) // 2] if g.get("mid") else [(P[0][0] + P[1][0]) / 2, (P[0][1] + P[1][1]) / 2]
            mk = (e.get("resolved") or {}).get("mark", {}).get("value") or e.get("mark") or "?"
            ax.text(m[0], m[1], str(mk) + (f" [{e['id'].split('/')[-1]}]" if show_ids else ""), fontsize=8, color=c, clip_on=True)
    elif e["type"] in ("column", "wall") and g.get("outline"):
        ax.add_patch(MP(g["outline"], closed=True, fc="#555" if e["type"] == "column" else "#2266dd", alpha=0.4))
        if win:
            xs = [p[0] for p in g["outline"]]; ys = [p[1] for p in g["outline"]]
            ax.text(sum(xs) / len(xs), sum(ys) / len(ys), (e.get("mark") or "?") + (f" [{e['id'].split('/')[-1]}]" if show_ids else ""),
                    fontsize=7, color="#000" if e["type"] == "column" else "#13a", clip_on=True)
ax.set_aspect("equal"); ax.set_xlim(box[0], box[2]); ax.set_ylim(box[1], box[3])
fig.savefig(out, dpi=100 if win else 45, bbox_inches=None)
print("saved", out, [round(v) for v in box])
