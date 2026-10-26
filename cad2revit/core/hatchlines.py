"""Recognise hatching that has been drawn as ordinary lines.

A drafter shades a region - a drop, a sunk panel, a girder zone - by filling it
with closely spaced parallel lines. Often those lines are put on the structural
layer they are shading rather than on a hatch layer, and then every one of them
looks exactly like a beam: a straight line, on the beam layer, the right sort of
length.

On the Wing B first floor, 162 of the 186 lines on `GIRDER BEAM` are at exactly
45 degrees, 258 to 367 mm apart. They are the fill of the girder zones. The
extractor turned 79 of them into girders, complete with widths measured from the
gap between neighbouring hatch lines, and in Revit they arrive as a thicket of
diagonal beams crossing the real structure.

The signature is unmistakable and has nothing to do with the angle being 45: a
run of parallel lines, many of them, evenly spaced, and spaced far too closely to
be structure. Nothing real is eight parallel beams three hundred millimetres
apart. That is what is tested for, so the same check works whatever angle the
drafter hatched at.
"""
from __future__ import annotations

import collections
import math

DEFAULTS = {
    "detect": True,
    "roles": ["beam", "girder", "column", "column_stop", "column_float",
              "wall", "slab"],
    "min_lines": 8,            # fewer parallel lines than this is not a fill
    "max_spacing_mm": 600.0,   # closer than this is a fill, not a frame
    "angle_tolerance_deg": 1.0,
    "regular_fraction": 0.6,   # how many gaps must match the typical one
}


def settings(cfg: dict) -> dict:
    s = dict(DEFAULTS)
    s.update(cfg.get("hatch_lines") or {})
    return s


def _angle_and_offset(a, b) -> tuple[float, float] | None:
    dx, dy = b[0] - a[0], b[1] - a[1]
    L = math.hypot(dx, dy)
    if L < 1e-9:
        return None
    ang = math.degrees(math.atan2(dy, dx)) % 180.0
    r = math.radians(ang)
    # perpendicular distance from the origin to the line through a
    off = -a[0] * math.sin(r) + a[1] * math.cos(r)
    return ang, off


def find_fills(lines, s: dict) -> list[list]:
    """Groups of lines that are a hatch fill rather than structure.

    `lines` is a list of (entity, start, end). Returns lists of entities.
    """
    step = max(s["angle_tolerance_deg"], 0.1)
    by_angle: dict[tuple[str, int], list] = collections.defaultdict(list)
    for e, a, b in lines:
        got = _angle_and_offset(a, b)
        if got is None:
            continue
        ang, off = got
        r = math.radians(ang)
        t0, t1 = sorted((a[0] * math.cos(r) + a[1] * math.sin(r), b[0] * math.cos(r) + b[1] * math.sin(r)))
        by_angle[(str(e.dxf.layer), int(round(ang / step)))].append((off, e, t0, t1))

    fills = []
    for _key, group in by_angle.items():
        if len(group) < s["min_lines"]:
            continue
        group.sort(key=lambda t: t[0])
        for run in _runs(group, s["max_spacing_mm"]):
            if len(run) < s["min_lines"]:
                continue
            gaps = [run[i + 1][0] - run[i][0] for i in range(len(run) - 1)]
            gaps = [g for g in gaps if g > 1.0]          # ignore lines on one line
            if len(gaps) < s["min_lines"] - 1:
                continue
            gaps.sort()
            typical = gaps[len(gaps) // 2]
            if typical <= 0 or typical > s["max_spacing_mm"]:
                continue
            near = sum(1 for g in gaps if abs(g - typical) <= 0.4 * typical)
            if near / float(len(gaps)) < s["regular_fraction"]:
                continue
            fills.append([e for _off, e, _t0, _t1 in run])
    return fills


def _runs(group, max_gap: float) -> list[list]:
    """The lines of one layer and direction, cut into runs lying side by side.

    A fill is one run: line after line, each beside the one before, closer than
    max_gap. Taken over the whole plan instead, the faces of beams 230 apart, pair
    after pair in one row of toilets (New block's SB beams, 16 lines on SB and 14 on
    BEAM-230X450), measured a regular 230 mm spacing and were left out as shading.
    A run ends at a gap no fill has, or where the next line does not run alongside
    the last one at all."""
    runs, cur = [], [group[0]]
    for item in group[1:]:
        prev = cur[-1]
        gap = item[0] - prev[0]
        level = [c for c in cur if prev[0] - c[0] <= 1.0]       # the lines on the last offset
        beside = any(min(item[3], c[3]) - max(item[2], c[2]) > 0.0 for c in level)
        if gap > max_gap or (gap > 1.0 and not beside):
            runs.append(cur)
            cur = []
        cur.append(item)
    runs.append(cur)
    return runs


def drop(entities, cfg: dict, role_of, res=None, log=print) -> list:
    """Remove hatch fills drawn on structural layers."""
    s = settings(cfg)
    if not s["detect"]:
        return list(entities)

    roles = set(s["roles"] or [])
    candidates = []
    for e in entities:
        if e.dxftype() != "LINE":
            continue
        try:
            if role_of(e.dxf.layer) not in roles:
                continue
            candidates.append((e, (float(e.dxf.start[0]), float(e.dxf.start[1])),
                               (float(e.dxf.end[0]), float(e.dxf.end[1]))))
        except Exception:
            continue

    if len(candidates) < s["min_lines"]:
        return list(entities)

    fills = find_fills(candidates, s)
    if not fills:
        return list(entities)

    drop_ids = set()
    per_layer: dict[str, int] = {}
    for group in fills:
        for e in group:
            drop_ids.add(id(e))
            lay = str(e.dxf.layer)
            per_layer[lay] = per_layer.get(lay, 0) + 1

    if res is not None:
        from .model import WARNING
        for lay, n in sorted(per_layer.items(), key=lambda kv: -kv[1]):
            res.add(WARNING, "geometry",
                    f"{n} lines on '{lay}' are a hatch fill drawn as ordinary lines - "
                    "many parallel lines, evenly spaced, far too close together to be "
                    "structure. They have been left out. Left in, each one becomes a "
                    "beam, and the model fills with elements that are really just "
                    "shading. Put hatching on its own layer, or use a real hatch.",
                    layer=lay)
    n = len(drop_ids)
    if n:
        log(f"  Left out {n} hatch lines drawn on structural layers")
    return [e for e in entities if id(e) not in drop_ids]
