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
"""
from __future__ import annotations

import math

SAME_POINT = 30.0        # mm: two beam ends this close are one joint
SQUARE = 0.5             # |cos| of the angle between them below this: a corner, not a run


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
