"""Audit one plan for the mistake kinds the user has reported. Writes JSON.
python audit_one.py CODE_ROOT DXF CONFIG|- OUT.json"""
import json
import math
import os
import sys

root, dxf, cfgp, out = sys.argv[1:5]
sys.path.insert(0, os.path.join(root, "cad2revit"))
os.chdir(os.path.join(root, "cad2revit"))
import ezdxf  # noqa: E402
from shapely.geometry import LineString, Point, Polygon  # noqa: E402
from shapely.ops import unary_union  # noqa: E402
from core import blocks, extract  # noqa: E402
from core import geometry as G  # noqa: E402
from core.reader import LayerMap  # noqa: E402

cfg = json.load(open(cfgp if cfgp != "-" else "config.json", encoding="utf-8"))
res = extract.extract(dxf, cfg, log=lambda *a: None)


def circle(a, b, c):
    ax, ay = a[:2]; bx, by = b[:2]; cx, cy = c[:2]
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-9:
        return None
    ux = ((ax * ax + ay * ay) * (by - cy) + (bx * bx + by * by) * (cy - ay) + (cx * cx + cy * cy) * (ay - by)) / d
    uy = ((ax * ax + ay * ay) * (cx - bx) + (bx * bx + by * by) * (ax - cx) + (cx * cx + cy * cy) * (bx - ax)) / d
    return ux, uy


def path(b):
    if not b.mid:
        return [b.start[:2], b.end[:2]]
    c = circle(b.start, b.mid, b.end)
    if c is None:
        return [b.start[:2], b.mid[:2], b.end[:2]]
    r = math.dist(c, b.start)
    a0 = math.atan2(b.start[1] - c[1], b.start[0] - c[0])
    am = math.atan2(b.mid[1] - c[1], b.mid[0] - c[0])
    a1 = math.atan2(b.end[1] - c[1], b.end[0] - c[0])
    sweep = (a1 - a0) % (2 * math.pi)
    if (am - a0) % (2 * math.pi) > sweep:
        sweep -= 2 * math.pi
    return [(c[0] + r * math.cos(a0 + sweep * i / 24), c[1] + r * math.sin(a0 + sweep * i / 24)) for i in range(25)]


beams = [(b, LineString(path(b)), (b.width or 230.0)) for b in res.beams if math.dist(b.start, b.end) > 1]
bands = unary_union([ls.buffer(w / 2 + 40, cap_style=2) for b, ls, w in beams]) if beams else None
sup = [Polygon(o.outline).buffer(0) for o in list(res.columns) + list(res.walls) if len(o.outline) >= 3]
holes = [Polygon(o.outline).buffer(30) for o in res.openings if len(o.outline) >= 3]
supu = unary_union(sup) if sup else None
holeu = unary_union(holes) if holes else None
report = {"uncovered": [], "free_ends": [], "overlap": [], "slab_under_beam": [], "odd_walls": []}

# 1 beam-layer lines with no beam on them
lmap = LayerMap(cfg["layer_roles"])
doc = ezdxf.readfile(dxf)
ents = blocks.flatten(doc.modelspace(), cfg, None, log=lambda *a: None)
segs = []
for e in ents:
    if lmap.role_of(e.dxf.layer) not in ("beam", "girder"):
        continue
    t = e.dxftype()
    try:
        if t == "LINE":
            segs.append(((e.dxf.start.x, e.dxf.start.y), (e.dxf.end.x, e.dxf.end.y)))
        elif t == "LWPOLYLINE":
            P = [tuple(p[:2]) for p in e.get_points()]
            if e.closed:
                P.append(P[0])
            segs += list(zip(P, P[1:]))
    except Exception:
        pass
for a, b in segs:
    L = math.dist(a, b)
    if L < 400:
        continue
    ln = LineString([a, b])
    rest = ln if bands is None else ln.difference(bands)
    if holeu is not None:
        rest = rest.difference(holeu)
    if supu is not None:
        rest = rest.difference(supu.buffer(30))
    parts = [g for g in getattr(rest, "geoms", [rest]) if not g.is_empty and g.length >= 300]
    for g in parts:
        m = g.interpolate(0.5, normalized=True)
        report["uncovered"].append([round(m.x), round(m.y), round(g.length)])

# 2 beam ends resting on nothing
for b, ls, w in beams:
    for p in (b.start, b.end):
        pt = Point(p[:2])
        if any(o is not b and lo.distance(pt) <= wo / 2 + 30 for o, lo, wo in beams):
            continue
        if supu is not None and supu.distance(pt) <= 30:
            continue
        report["free_ends"].append([round(p[0]), round(p[1]), b.mark or "", round(w)])

# 3 beams over each other
for i, (a, la, wa) in enumerate(beams):
    for b, lb, wb in beams[i + 1:]:
        if a.mid or b.mid or extract._angle_between(a, b) > 3:
            continue
        off = G.point_to_line(extract._mid(b), a.start, a.end)
        if off >= (wa + wb) / 2 - 20:
            continue
        shared = la.buffer((wa + wb) / 2, cap_style=2).intersection(lb).length
        if shared > 300:
            m = la.intersection(lb.buffer((wa + wb) / 2, cap_style=2)).centroid
            report["overlap"].append([round(m.x), round(m.y), a.mark or "", b.mark or "", round(shared)])

# 4 slab under a beam
inner = [(b, ls.buffer(max(w / 2 - 25, 20), cap_style=2)) for b, ls, w in beams]
for s in res.slabs:
    try:
        sp = Polygon(s.outline).buffer(0)
    except Exception:
        continue
    for b, band in inner:
        x = sp.intersection(band)
        if x.area > 100000:          # 0.1 m2
            c = x.centroid
            report["slab_under_beam"].append([round(c.x), round(c.y), s.mark or "", b.mark or "", round(x.area / 1e6, 2)])

# 5 column-layer outlines read as walls that are not walls
for w in res.walls:
    if lmap.role_of(w.source_layer or "") not in ("column", "column_stop", "column_float"):
        continue
    try:
        P = Polygon(w.outline)
    except Exception:
        continue
    if len(w.outline) >= 5 and (not P.is_valid or P.area < 0.5 * P.minimum_rotated_rectangle.area):
        c = P.centroid
        report["odd_walls"].append([round(c.x), round(c.y), w.mark or "", len(w.outline)])
json.dump(report, open(out, "w"))
