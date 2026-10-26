"""Sunk areas shaded with hatches that have been exploded into plain lines.

A hatch can be exploded, and then it is no longer a HATCH with a pattern name
and a boundary but thousands of short LINEs. The copy of Wing B 2nd floor the
team works from has every hatch exploded: 3,325 diagonal lines on SUNK where
the GFC original has 3 hatches, and the legend's samples exploded as well. With
only real hatches read, the 250 mm sunk toilet strips (S1A) came out flat.

What is left to go by is how the shading looks, which is also how a reader of
the drawing goes by it:

  lines   one set of parallel lines (the diagonal 250 MM SUNK)
  plus    little '+' marks, a short line across a short line at its middle
          (the 150 MM SUNK dots)
  grid    two sets of lines crossing, but not as '+' marks
  ticks   short marks all one way, standing apart (S7's 150 on this sheet,
          which the legend does not show)

Each caption in the legend ("250MM SUNK") is paired with the lines on its own
row, which gives the look of that depth. Each shaded area of the plan is then
given the depth whose sample looks the same - on the same layer if there is
one, otherwise by look alone, which is reported. Layer names are not used for
the depth: on these sheets '300mm SUNK' carries the 250 sample.

Spacing is not used either. On this sheet the legend is drawn at another scale
from the plan, and the girder sample's spacing (56) is nearer the plan's sunk
lines (48) than the 250 sample's is.
"""
from __future__ import annotations

import collections
import math
import statistics

DEFAULTS = {
    "detect": True,
    "min_lines": 8,              # fewer lines than this is not a shaded area
    "angle_step_deg": 2.0,
    "min_area_mm2": 100000.0,    # a shaded area smaller than 0.1 m2 is slack
    "plus_tolerance_mm": 3.0,    # how near two midpoints are to make a '+'
}


def settings(cfg: dict) -> dict:
    s = dict(DEFAULTS)
    s.update(cfg.get("sunk_lines") or {})
    return s


def segments(entities, layers=None) -> list[tuple[str, tuple, tuple, float]]:
    """(LAYER, start, end, angle 0-180) of every LINE, on `layers` if given."""
    out = []
    for e in entities:
        if e.dxftype() != "LINE":
            continue
        lay = str(e.dxf.layer).upper()
        if layers is not None and lay not in layers:
            continue
        a = (float(e.dxf.start[0]), float(e.dxf.start[1]))
        b = (float(e.dxf.end[0]), float(e.dxf.end[1]))
        if math.dist(a, b) < 1.0:
            continue
        ang = math.degrees(math.atan2(b[1] - a[1], b[0] - a[0])) % 180.0
        out.append((lay, a, b, ang))
    return out


def _angle_gap(a: float, b: float) -> float:
    d = abs(a - b) % 180.0
    return min(d, 180.0 - d)


def look(segs, s: dict) -> tuple[str, float] | None:
    """('lines' | 'plus' | 'grid', main angle) of a set of hatch segments."""
    if not segs:
        return None
    step = s["angle_step_deg"]
    count = collections.Counter(int(round(g[3] / step)) % int(round(180 / step)) for g in segs)
    floor = max(3, 0.15 * len(segs))
    dirs = []
    for k, n in count.most_common():
        if n < floor:
            break
        if all(_angle_gap(k * step, d) > 10.0 for d in dirs):
            dirs.append(k * step)
    if len(dirs) == 1:
        # short marks standing apart (S7's ticks) are not a line hatch, dashed or not
        from shapely import STRtree
        from shapely.geometry import LineString
        geoms = [LineString([g[1], g[2]]) for g in segs]
        _i, dist = STRtree(geoms).query_nearest(geoms, exclusive=True, return_distance=True)
        near = [d for d in dist if d > 1.0]
        length = statistics.median(math.dist(g[1], g[2]) for g in segs)
        if near and length < statistics.median(near):
            return "ticks", dirs[0]
        return "lines", dirs[0]
    if len(dirs) != 2 or abs(_angle_gap(dirs[0], dirs[1]) - 90.0) > 10.0:
        return None
    tol = s["plus_tolerance_mm"]
    mids = collections.defaultdict(set)
    for g in segs:
        k = next(i for i, d in enumerate(dirs) if _angle_gap(g[3], d) <= 10.0) \
            if any(_angle_gap(g[3], d) <= 10.0 for d in dirs) else None
        if k is None:
            continue
        m = ((g[1][0] + g[2][0]) / 2, (g[1][1] + g[2][1]) / 2)
        mids[k].add((round(m[0] / tol), round(m[1] / tol)))
    a, b = mids.get(0, set()), mids.get(1, set())
    near = sum(1 for p in a if any((p[0] + i, p[1] + j) in b
                                   for i in (-1, 0, 1) for j in (-1, 0, 1)))
    return ("plus" if a and near >= 0.6 * len(a) else "grid"), dirs[0]


def areas(segs, s: dict):
    """Shaded areas of one layer: [(shapely Polygon, its segments)].

    The lines are closed up (grown, then shrunk back) by a little more than the
    gap between neighbouring lines, which fills the space between them and
    stops at their ends, where the hatch boundary was.
    """
    from shapely import STRtree
    from shapely.geometry import LineString
    from shapely.ops import unary_union

    step = s["angle_step_deg"]
    groups = collections.defaultdict(list)
    for g in segs:
        groups[int(round(g[3] / step)) % int(round(180 / step))].append(g)
    kept, gaps = [], []
    for grp in groups.values():
        if len(grp) < s["min_lines"]:
            continue
        geoms = [LineString([g[1], g[2]]) for g in grp]
        tree = STRtree(geoms)
        _idx, dist = tree.query_nearest(geoms, exclusive=True, return_distance=True)
        near = [d for d in dist if d > 1.0]
        if near:
            gaps.append(statistics.median(near))
        kept += list(zip(grp, geoms))
    if not kept or not gaps:
        return []
    # 1.5 gaps: the 250 diagonal is dashed, so the gaps along a line count too
    r = min(max(1.5 * max(gaps), 30.0), 1000.0)
    grown = unary_union([geom.buffer(r, quad_segs=2) for _g, geom in kept])
    shaded = grown.buffer(-r, quad_segs=2)
    polys = [p for p in getattr(shaded, "geoms", [shaded])
             if p.geom_type == "Polygon" and p.area >= s["min_area_mm2"]]
    if not polys:
        return []
    tree = STRtree(polys)
    members = collections.defaultdict(list)
    for g, geom in kept:
        hit = tree.query(geom.interpolate(0.5, normalized=True), predicate="intersects")
        if len(hit):
            members[int(hit[0])].append(g)
    return [(p, members[i]) for i, p in enumerate(polys) if len(members[i]) >= s["min_lines"]]
