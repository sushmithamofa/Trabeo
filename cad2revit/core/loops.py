"""Assemble closed outlines from loose lines.

Some drafters draw a column as a closed polyline. Others draw the same column as
four separate lines. The shape on the sheet is identical and the difference is
invisible to anyone reading the drawing, but an extractor that only understands
polylines finds nothing at all.

That is not hypothetical: on the Wing B first floor the COLUMN layer carries 127
lines and 15 polylines, and only the 15 came through. Thirty-one columns were
simply absent from the model.

So loose lines are stitched back into outlines. Every endpoint is snapped to a
tolerance and used as a graph node, and the closed faces of that graph are walked
out. Face traversal rather than naive cycle-following, because columns often
share a corner: at each node the walk takes the next edge clockwise from the one
it arrived on, which traces the smallest face on the left and so separates two
columns meeting at a point instead of merging them into one ring.

Nothing is invented. A line that closes no face is left exactly where it is, and
an outline only becomes an element if it passes the same checks a drawn polyline
has to pass.
"""
from __future__ import annotations

import math

Point = tuple[float, float]

DEFAULTS = {
    "roles": ["column", "column_stop", "column_float", "wall", "slab",
              "opening", "stair"],
    "snap_mm": 25.0,          # endpoints this close are the same corner
    "max_lines": 4000,        # refuse to try on a hatch-sized pile of lines
    "min_area_mm2": 10000.0,  # 100 x 100: smaller is a drafting scrap
    "max_vertices": 64,
}


def settings(cfg: dict) -> dict:
    s = dict(DEFAULTS)
    s.update((cfg.get("assemble_loops") or {}))
    return s


def _signed_area(pts: list[Point]) -> float:
    a = 0.0
    for i in range(len(pts)):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % len(pts)]
        a += x1 * y2 - x2 * y1
    return a / 2.0


def closed_loops(segments: list[tuple[Point, Point]], snap: float,
                 min_area: float = 0.0, max_vertices: int = 64) -> list[list[Point]]:
    """The closed faces formed by these line segments.

    `segments` is a list of (start, end). The result is a list of outlines, each
    a list of points in order, without a repeated closing point.
    """
    if not segments or snap <= 0:
        return []

    def key(p: Point) -> tuple[int, int]:
        return (int(round(p[0] / snap)), int(round(p[1] / snap)))

    # ---- build the graph, dropping zero-length and repeated edges -------
    pos: dict[tuple[int, int], Point] = {}
    adj: dict[tuple[int, int], set] = {}
    for a, b in segments:
        ka, kb = key(a), key(b)
        if ka == kb:
            continue
        pos.setdefault(ka, a)
        pos.setdefault(kb, b)
        adj.setdefault(ka, set()).add(kb)
        adj.setdefault(kb, set()).add(ka)

    if not adj:
        return []

    # ---- drop dangling ends: they belong to no closed face -------------
    changed = True
    while changed:
        changed = False
        for n in list(adj):
            if len(adj[n]) < 2:
                for m in adj[n]:
                    adj[m].discard(n)
                del adj[n]
                changed = True

    if not adj:
        return []

    # neighbours of each node, in angle order, for the rotation system
    order: dict[tuple[int, int], list] = {}
    for n, nbrs in adj.items():
        x, y = pos[n]
        order[n] = sorted(nbrs, key=lambda m: math.atan2(pos[m][1] - y, pos[m][0] - x))

    def next_edge(u, v):
        """Arriving at v from u, the next edge clockwise: traces the left face."""
        ring = order[v]
        i = ring.index(u)
        return v, ring[i - 1]          # -1 wraps, which is what we want

    faces: list[list[Point]] = []
    seen: set = set()
    for u in adj:
        for v in adj[u]:
            if (u, v) in seen:
                continue
            cycle = []
            a, b = u, v
            while (a, b) not in seen:
                seen.add((a, b))
                cycle.append(a)
                if len(cycle) > max_vertices + 1:
                    cycle = []
                    break
                a, b = next_edge(a, b)
            if len(cycle) < 3:
                continue
            pts = [pos[n] for n in cycle]
            area = _signed_area(pts)
            # One traversal direction gives the inner faces, the other gives the
            # outer boundary of the component. Keeping one sign drops the outer.
            if area <= 0:
                continue
            if area < min_area:
                continue
            faces.append(pts)

    return faces


def from_entities(entities, snap: float, min_area: float, max_vertices: int):
    """Closed outlines assembled from the LINE entities in `entities`."""
    segs = []
    for e in entities:
        if e.dxftype() != "LINE":
            continue
        try:
            a = (float(e.dxf.start[0]), float(e.dxf.start[1]))
            b = (float(e.dxf.end[0]), float(e.dxf.end[1]))
        except Exception:
            continue
        segs.append((a, b))
    return segs, closed_loops(segs, snap, min_area, max_vertices)
