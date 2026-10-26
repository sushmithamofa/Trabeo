"""Tiles around flagged points: CAD (beam layers red) + live extractor beams/columns/walls.
python tiles_flags.py DXF CONFIG|- AUDIT_JSON KIND OUT.png [half=1800]"""
import json
import math
import os
import sys

dxf, cfgp, aj, kind, out = sys.argv[1:6]
half = float(sys.argv[6]) if len(sys.argv) > 6 else 1800.0
APP = r"C:\Users\Admin\Documents\Structure CAD to Revit\New version\cad2revit"
sys.path.insert(0, APP)
os.chdir(APP)
import ezdxf  # noqa: E402
import matplotlib  # noqa: E402
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import Polygon as MP  # noqa: E402
from core import blocks, extract  # noqa: E402
from core.reader import LayerMap  # noqa: E402

cfg = json.load(open(cfgp if cfgp != "-" else "config.json", encoding="utf-8"))
flags = json.load(open(aj))[kind][:16]
res = extract.extract(dxf, cfg, log=lambda *a: None)
lmap = LayerMap(cfg["layer_roles"])
ents = blocks.flatten(ezdxf.readfile(dxf).modelspace(), cfg, None, log=lambda *a: None)
segs, texts = [], []
for e in ents:
    t = e.dxftype()
    try:
        if t == "LINE":
            P = [(e.dxf.start.x, e.dxf.start.y), (e.dxf.end.x, e.dxf.end.y)]
        elif t == "LWPOLYLINE":
            P = [tuple(p[:2]) for p in e.get_points()] + ([tuple(e.get_points()[0][:2])] if e.closed else [])
        elif t in ("ARC", "CIRCLE"):
            P = [(p.x, p.y) for p in e.flattening(5)]
        elif t in ("TEXT", "MTEXT"):
            texts.append((e.dxf.insert.x, e.dxf.insert.y, (e.dxf.text if t == "TEXT" else e.plain_text())[:18]))
            continue
        else:
            continue
        segs.append((P, lmap.role_of(e.dxf.layer)))
    except Exception:
        pass
n = len(flags)
cols = 4
rows = max(1, math.ceil(n / cols))
fig, axs = plt.subplots(rows, cols, figsize=(cols * 5, rows * 5), squeeze=False)
for ax in axs.flat:
    ax.axis("off")
for i, f in enumerate(flags):
    ax = axs.flat[i]
    ax.axis("on")
    cx, cy = f[0], f[1]
    box = (cx - half, cy - half, cx + half, cy + half)
    inb = lambda p: box[0] <= p[0] <= box[2] and box[1] <= p[1] <= box[3]   # noqa: E731
    for P, role in segs:
        if any(inb(p) for p in P):
            ax.plot([p[0] for p in P], [p[1] for p in P], lw=0.7 if role in ("beam", "girder") else 0.3,
                    color="#d33" if role in ("beam", "girder") else "#aaa")
    for x, y, s in texts:
        if inb((x, y)):
            ax.text(x, y, s, fontsize=5, color="#666", clip_on=True)
    for o in list(res.columns) + list(res.walls):
        if len(o.outline) >= 3 and any(inb(p) for p in o.outline):
            ax.add_patch(MP([p[:2] for p in o.outline], closed=True, fc="#555", alpha=0.35))
    for b in res.beams:
        P = [b.start, b.mid, b.end] if b.mid else [b.start, b.end]
        if any(inb(p) for p in P) or True:
            ax.plot([p[0] for p in P], [p[1] for p in P], color="#1f6fd0", lw=2.5, alpha=0.6)
    ax.plot([cx], [cy], "o", ms=14, mfc="none", mec="#f0a", mew=2)
    ax.set_title(f"{i + 1}: {f[2:] if len(f) > 2 else ''}", fontsize=8)
    ax.set_xlim(box[0], box[2]); ax.set_ylim(box[1], box[3]); ax.set_aspect("equal")
    ax.tick_params(labelsize=5)
fig.tight_layout()
fig.savefig(out, dpi=70)
print("saved", out, n)
