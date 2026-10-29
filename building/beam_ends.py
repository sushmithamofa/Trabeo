"""Beams meeting at an L corner, drawn as solids, fill the corner.

Two beams that turn a corner end on the same point - where their centrelines meet,
as the drawing's mitred faces put it and as Revit wants them (Revit joins the two
ends and fills the corner itself). Drawn as boxes ending there (review plan, 3D
view, IFC, Rhino) they leave the outer quarter of the corner empty: a notch at
every corner with no column in it (ALCAZAR 1st floor, the user's circles).

corner_extensions says how far each box is carried on past its end so the corner
is solid: half the width of the other beam, only where exactly two beam ends meet,
roughly square to each other, and no other beam runs through the point. The beam's
own start and end are not changed.

onto_supports is the other half: a beam end that stops short of what carries it (a wall's
angled end, a column's corner - Winter Villa's SW19 and SW4, 50 to 260 mm off) is carried on
along its own line onto the column, wall or beam just ahead, to the middle of its bearing -
so it sits on its support, Revit joins it there, and the load has its path. Nothing is
shortened; an end with nothing ahead within REACH stays where it is (a free end).
"""
from __future__ import annotations

import math

SAME_POINT = 30.0        # mm: two beam ends this close are one joint
SQUARE = 0.5             # |cos| of the angle between them below this: a corner, not a run
TOUCH = 50.0             # mm: an end this near a support sits on it
REACH = 600.0            # mm: how far ahead along its line a beam end looks for what carries it


def corner_extensions(beams) -> list[list[float]]:
    """beams: [(start, end, width)], straight. Returns [[carry on at start, at end]] in mm."""
    ends = []
    for i, (a, b, w) in enumerate(beams):
        L = math.dist(a[:2], b[:2])
        if L < 1.0 or not w:
            ends.append(None)
            continue
        ux, uy = (b[0] - a[0]) / L, (b[1] - a[1]) / L
        ends.append(((a[0], a[1]), (-ux, -uy), (b[0], b[1]), (ux, uy), L))
    out = [[0.0, 0.0] for _ in beams]
    for i, ei in enumerate(ends):
        if ei is None:
            continue
        for k in (0, 1):
            p, u = ei[2 * k], ei[2 * k + 1]
            meet = []
            through = False
            for j, ej in enumerate(ends):
                if j == i or ej is None:
                    continue
                if math.dist(p, ej[0]) <= SAME_POINT:
                    meet.append((j, ej[1]))
                elif math.dist(p, ej[2]) <= SAME_POINT:
                    meet.append((j, ej[3]))
                else:
                    # another beam running through the joint: a T or a cross, not an L
                    (ax, ay), (bx, by) = ej[0], ej[2]
                    vx, vy = (bx - ax) / ej[4], (by - ay) / ej[4]
                    t = (p[0] - ax) * vx + (p[1] - ay) * vy
                    off = abs((p[0] - ax) * -vy + (p[1] - ay) * vx)
                    if SAME_POINT < t < ej[4] - SAME_POINT and off <= float(beams[j][2]) / 2.0:
                        through = True
            if through or len(meet) != 1:
                continue
            j, v = meet[0]
            if abs(u[0] * v[0] + u[1] * v[1]) >= SQUARE:
                continue
            out[i][k] = float(beams[j][2]) / 2.0
    return out


def carried(a, b, ext):
    """start and end carried on by ext = [at start, at end]."""
    L = math.dist(a[:2], b[:2])
    if L < 1.0 or not (ext[0] or ext[1]):
        return list(a), list(b)
    ux, uy = (b[0] - a[0]) / L, (b[1] - a[1]) / L
    return ([a[0] - ux * ext[0], a[1] - uy * ext[0]] + list(a[2:]),
            [b[0] + ux * ext[1], b[1] + uy * ext[1]] + list(b[2:]))


def _circle(a, m, b):
    """Centre, radius and direction (+1 anticlockwise from a to b through m) of the arc a-m-b."""
    ax, ay, mx, my, bx, by = a[0], a[1], m[0], m[1], b[0], b[1]
    d = 2.0 * (ax * (my - by) + mx * (by - ay) + bx * (ay - my))
    if abs(d) < 1e-9:
        return None
    ux = ((ax * ax + ay * ay) * (my - by) + (mx * mx + my * my) * (by - ay) + (bx * bx + by * by) * (ay - my)) / d
    uy = ((ax * ax + ay * ay) * (bx - mx) + (mx * mx + my * my) * (ax - bx) + (bx * bx + by * by) * (mx - ax)) / d
    turn = (mx - ax) * (by - ay) - (my - ay) * (bx - ax)          # > 0: m right of the chord a -> b
    return (ux, uy), math.hypot(ax - ux, ay - uy), (1 if turn > 0 else -1)


SQUARE_LINE = 0.9       # |cos| at or above this: a beam in line with the end's direction, not across it


def _cos(bm, dx, dy) -> float:
    """cos of the angle between a beam's chord and the direction (dx, dy)."""
    a, b = bm[0], bm[1]
    L = math.dist(a[:2], b[:2]) or 1.0
    return ((b[0] - a[0]) * dx + (b[1] - a[1]) * dy) / L


def onto_supports(beams, supports, reach: float = REACH) -> list[list]:
    """beams: [(start, end, width)] or [(start, end, width, mid)] for a curved beam; supports:
    shapely footprints of the columns and walls of the floor. Returns [[new start or None, new
    end or None, new mid or None]]: an end that touches no support or other beam is carried on
    - along the beam's line, or round its arc - onto the first one its width meets within
    reach, to the middle of that bearing - a beam it lands on crosses it (one in line with it
    is the next span: the gap between them is a column's, or meant). Worked out from the
    beams as given, so the order does not matter."""
    from shapely.geometry import LineString, Point
    foot = []
    for bm in beams:
        a, b, w = bm[0], bm[1], bm[2]
        mid = bm[3] if len(bm) > 3 else None
        ok = w and math.dist(a[:2], b[:2]) >= 1.0
        line = LineString([a[:2], mid[:2], b[:2]] if mid else [a[:2], b[:2]])
        foot.append(line.buffer(float(w) / 2, cap_style=2) if ok else None)
    out = []
    for i, bm in enumerate(beams):
        a, b, w = bm[0], bm[1], bm[2]
        mid = bm[3] if len(bm) > 3 else None
        res = [None, None, None]
        out.append(res)
        if foot[i] is None:
            continue
        arc = _circle(a, mid, b) if mid else None
        if mid and arc is None:
            mid = None                                   # three points in a row: straight
        if arc:
            (cx, cy), R, sense = arc
            # going round from a to b: the way out at b, and back out at a
            tb = (-(b[1] - cy) * sense / R, (b[0] - cx) * sense / R)
            ta = ((a[1] - cy) * sense / R, -(a[0] - cx) * sense / R)
            ways = ((a, ta[0], ta[1]), (b, tb[0], tb[1]))
        else:
            L = math.dist(a[:2], b[:2])
            ux, uy = (b[0] - a[0]) / L, (b[1] - a[1]) / L
            ways = ((a, -ux, -uy), (b, ux, uy))
        others = list(supports) + [f for j, f in enumerate(foot) if j != i and f is not None]
        moved = False
        for k, (p, dx, dy) in enumerate(ways):
            if any(g.distance(Point(p[:2])) <= TOUCH for g in others):
                continue
            strip = LineString([p[:2], (p[0] + dx * reach, p[1] + dy * reach)]).buffer(float(w) / 2, cap_style=2)
            # what carries it: a column, a wall, or a beam crossing its line - never the next beam
            # in line with it (that gap is a column's, or meant)
            crossing = [f for j, f in enumerate(foot) if j != i and f is not None
                        and abs(_cos(beams[j], dx, dy)) < SQUARE_LINE]
            best = None
            for g in list(supports) + crossing:
                hit = g.intersection(strip)
                if hit.is_empty or hit.area < 0.3 * float(w) * TOUCH:
                    continue                    # a corner grazed, not a bearing
                ts = [(x - p[0]) * dx + (y - p[1]) * dy for part in getattr(hit, "geoms", [hit])
                      if part.geom_type == "Polygon" for x, y in part.exterior.coords]
                if not ts:
                    continue
                t0, t1 = max(0.0, min(ts)), min(reach, max(ts))
                if best is None or t0 < best[0]:
                    best = (t0, t1)
            if best is None:
                continue
            t = (best[0] + best[1]) / 2.0
            if arc:
                # round the arc by that length (within a few mm of the tangent for so short a piece)
                ang = math.atan2(p[1] - cy, p[0] - cx) + (t / R) * sense * (1 if k == 1 else -1)
                res[k] = [cx + R * math.cos(ang), cy + R * math.sin(ang)] + list(p[2:])
            else:
                res[k] = [p[0] + dx * t, p[1] + dy * t] + list(p[2:])
            moved = True
        if arc and moved:
            # the new arc's middle: halfway round from its new start to its new end
            na, nb = res[0] or a, res[1] or b
            a0 = math.atan2(na[1] - cy, na[0] - cx)
            a1 = math.atan2(nb[1] - cy, nb[0] - cx)
            sweep = ((a1 - a0) * sense) % (2 * math.pi)
            am = a0 + sense * sweep / 2.0
            res[2] = [cx + R * math.cos(am), cy + R * math.sin(am)] + list(mid[2:])
    return out
