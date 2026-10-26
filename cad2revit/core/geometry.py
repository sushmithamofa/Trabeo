"""Small geometry helpers. Everything is 2D and in drawing units (millimetres)."""
from __future__ import annotations

import math
from typing import Iterable, Sequence

Point = tuple[float, float]


def dist(a: Point, b: Point) -> float:
    return math.hypot(b[0] - a[0], b[1] - a[1])


def midpoint(a: Point, b: Point) -> Point:
    return ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)


def centroid(pts: Sequence[Point]) -> Point:
    n = len(pts)
    return (sum(p[0] for p in pts) / n, sum(p[1] for p in pts) / n)


def bbox(pts: Iterable[Point]) -> tuple[float, float, float, float]:
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def polygon_area(pts: Sequence[Point]) -> float:
    """Unsigned area by the shoelace formula."""
    a = 0.0
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        a += x1 * y2 - x2 * y1
    return abs(a) / 2.0


def angle_deg(a: Point, b: Point) -> float:
    """Direction of a->b in degrees, 0 to 360."""
    return math.degrees(math.atan2(b[1] - a[1], b[0] - a[0])) % 360.0


def normalise_rect_angle(deg: float) -> float:
    """Rectangle rotation reported in the range 0 to 90 degrees."""
    return deg % 90.0


def point_to_line(p: Point, a: Point, b: Point) -> float:
    """Perpendicular distance to the INFINITE line through a and b."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    L = math.hypot(dx, dy)
    if L == 0:
        return dist(p, a)
    return abs((p[0] - a[0]) * dy - (p[1] - a[1]) * dx) / L


def point_to_segment(p: Point, a: Point, b: Point) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    L2 = dx * dx + dy * dy
    if L2 == 0.0:
        return dist(p, a)
    t = max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / L2))
    return dist(p, (a[0] + t * dx, a[1] + t * dy))


def point_in_polygon(p: Point, poly: Sequence[Point]) -> bool:
    """Ray casting. Points exactly on an edge may return either answer."""
    x, y = p
    inside = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            xin = (x2 - x1) * (y - y1) / (y2 - y1) + x1
            if x < xin:
                inside = not inside
    return inside


def rectangle_of(pts: Sequence[Point], side_tol: float, angle_tol: float):
    """If pts form a rectangle, return (centre, short_side, long_side, rotation_deg).

    Returns None when the shape is not a rectangle within tolerance. The rotation
    is the direction of the LONG side, reported 0 to 180 degrees, which is what a
    Revit column instance needs.
    """
    if len(pts) != 4:
        return None
    sides = [dist(pts[i], pts[(i + 1) % 4]) for i in range(4)]
    if abs(sides[0] - sides[2]) > side_tol or abs(sides[1] - sides[3]) > side_tol:
        return None
    for i in range(4):
        a, b, c = pts[i], pts[(i + 1) % 4], pts[(i + 2) % 4]
        v1 = (a[0] - b[0], a[1] - b[1])
        v2 = (c[0] - b[0], c[1] - b[1])
        n1 = math.hypot(*v1)
        n2 = math.hypot(*v2)
        if n1 == 0 or n2 == 0:
            return None
        cosang = (v1[0] * v2[0] + v1[1] * v2[1]) / (n1 * n2)
        cosang = max(-1.0, min(1.0, cosang))
        if abs(math.degrees(math.acos(cosang)) - 90.0) > angle_tol:
            return None

    short = min(sides[0], sides[1])
    long_ = max(sides[0], sides[1])
    if sides[0] >= sides[1]:
        rot = angle_deg(pts[0], pts[1])
    else:
        rot = angle_deg(pts[1], pts[2])
    return centroid(pts), short, long_, rot % 180.0


def long_axis_of_rect(pts: Sequence[Point]) -> tuple[Point, Point]:
    """Centreline of a rectangle along its long side, as (start, end)."""
    sides = [(dist(pts[i], pts[(i + 1) % 4]), i) for i in range(4)]
    _, i = max(sides)
    a, b = pts[i], pts[(i + 1) % 4]
    c, d = pts[(i + 2) % 4], pts[(i + 3) % 4]
    return midpoint(d, a), midpoint(b, c)


def dedupe_points(pts: Sequence[Point], tol: float) -> list[Point]:
    """Drop consecutive duplicates and a closing point equal to the first."""
    out: list[Point] = []
    for p in pts:
        if not out or dist(out[-1], p) > tol:
            out.append(p)
    if len(out) > 2 and dist(out[0], out[-1]) <= tol:
        out.pop()
    return out


def line_intersection(a1: Point, b1: Point, a2: Point, b2: Point,
                      min_angle_deg: float = 15.0) -> Point | None:
    """Where two INFINITE lines cross, or None if they are near parallel.

    Near-parallel lines are refused because their intersection runs away to
    infinity, and snapping a beam to it would throw the end across the building.
    """
    dx1, dy1 = b1[0] - a1[0], b1[1] - a1[1]
    dx2, dy2 = b2[0] - a2[0], b2[1] - a2[1]
    n1 = math.hypot(dx1, dy1)
    n2 = math.hypot(dx2, dy2)
    if n1 == 0 or n2 == 0:
        return None

    cosang = abs((dx1 * dx2 + dy1 * dy2) / (n1 * n2))
    cosang = min(1.0, cosang)
    if math.degrees(math.acos(cosang)) < min_angle_deg:
        return None

    denom = dx1 * dy2 - dy1 * dx2
    if denom == 0:
        return None
    t = ((a2[0] - a1[0]) * dy2 - (a2[1] - a1[1]) * dx2) / denom
    return (a1[0] + t * dx1, a1[1] + t * dy1)


def segments_equal(s1, s2, tol: float) -> bool:
    a1, b1 = s1
    a2, b2 = s2
    return ((dist(a1, a2) <= tol and dist(b1, b2) <= tol)
            or (dist(a1, b2) <= tol and dist(b1, a2) <= tol))
