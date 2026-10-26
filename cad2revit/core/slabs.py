"""Slab panels, found from the beams around them when none are drawn.

A Revit floor needs a closed outline, and these drawings have none: the SLAB layer
holds a few open fragments at most. What they do have is a slab mark in every
panel (S3, S8A ...) and a slab schedule that gives each mark its thickness. The
panel itself is simply the bay the beams enclose, so that is what is used:

  1. Every beam, girder, column and wall is laid down at its real width, and the
     result is closed across gaps narrower than `slab_gap_close_mm`, where a beam
     stops just short of the one it frames into.
  2. Each hole left in that is a bay. A bay with a slab mark in it becomes a panel
     with that mark and its scheduled thickness.
  3. A mark the slab schedule calls sunk ("400mm SUNK" in its remarks) drops
     the panel by that much.
  4. A bay with no mark is left open. On the Wing B plans those are the lift,
     stair and shaft openings, which is exactly what should be left open. (The
     user's rule, 14 Oct: no slab mark, no slab - nor a shape on the slab layer
     with no mark in it. `slab_fill_unmarked_bays` brings back the old filling.)
  5. A staircase drawn on the stair layer is taken out of the panel it lies in.

The panel runs to the beam faces, not the centrelines. Beam depths here are the
overall depth, top of beam flush with top of slab, so a panel to the faces is the
concrete that is slab and not beam: nothing is counted twice, and in 3D the beams
and slab read as one flat top.

Nothing is invented. A mark outside every closed bay (a cantilever, or a bay a
missing beam has left open) gets no panel and is reported. A bay holding two
different marks is built once, at the thicker of the two, and reported, because a
beam between them is probably missing.
"""
from __future__ import annotations

import math
import re

from . import geometry as G
from .model import BLOCKER, INFO, WARNING, Result, SlabPanel

SOURCE = "derived from beams"
# masonry layer names, as building/walls.py reads them (BK_WALL / BRK: short forms)
MASONRY = re.compile(r"BRICK|MASONRY|BLOCK\s*WORK|BLOCKWORK|\bAAC\b|PARTITION|"
                     r"(?<![A-Z])BK[\s_.-]*WALL|(?<![A-Z])BRK(?![A-Z])", re.I)
STAIR_MIN_LINES = 6       # stair-layer lines inside an unmarked bay that make it a stair well
STAIR_REACH_MM = 450.0    # stair-layer lines closer than this are one staircase (treads up to 450
                          # apart, two flights); 300 left 300 mm treads as stripes (J1153 1st floor)
STAIR_MAX_M2 = 60.0       # a "staircase" bigger than this is something else drawn on the stair layer
STAIR_REMNANT_M2 = 1.5    # a piece left beside a cut stair, smaller and with no mark, is the stair's
DRAWN_MARK_REACH_MM = 100.0   # a mark on a drawn slab outline's line (or just off it) is its mark
STAIR_MARK = re.compile(r"^(?:ST|STR|FL|LND)(?![A-Z])", re.I)   # the stair marks of building/marks.py


def _arc_points(s, m, e, n=24):
    """Points along the arc through s, m, e."""
    (ax, ay), (bx, by), (cx, cy) = s, m, e
    d = 2.0 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-9:
        return [s, e]
    a2, b2, c2 = ax * ax + ay * ay, bx * bx + by * by, cx * cx + cy * cy
    ux = (a2 * (by - cy) + b2 * (cy - ay) + c2 * (ay - by)) / d
    uy = (a2 * (cx - bx) + b2 * (ax - cx) + c2 * (bx - ax)) / d
    r = math.hypot(ax - ux, ay - uy)
    a0 = math.atan2(ay - uy, ax - ux)
    am = math.atan2(by - uy, bx - ux)
    a1 = math.atan2(cy - uy, cx - ux)
    tau = 2.0 * math.pi
    ccw_m, ccw_e = (am - a0) % tau, (a1 - a0) % tau
    sweep = ccw_e if ccw_m < ccw_e else -((a0 - a1) % tau)
    return [(ux + r * math.cos(a0 + sweep * i / n), uy + r * math.sin(a0 + sweep * i / n))
            for i in range(n + 1)]


def _merge_collinear(segs, off_mm=20.0, gap_mm=2500.0, ang_deg=2.0):
    """Join pieces of one straight line. A cut-out's diagonal is often broken in
    the middle to leave room for its label, as on the Wing B lift shafts."""
    segs = [list(s) for s in segs]
    merged = True
    while merged:
        merged = False
        for i in range(len(segs)):
            for j in range(i + 1, len(segs)):
                a, b = segs[i], segs[j]
                la, lb = G.dist(a[0], a[1]), G.dist(b[0], b[1])
                if la < 1 or lb < 1:
                    continue
                d = abs((G.angle_deg(a[0], a[1]) - G.angle_deg(b[0], b[1])) % 180.0)
                if min(d, 180.0 - d) > ang_deg:
                    continue
                if max(G.point_to_line(b[0], a[0], a[1]),
                       G.point_to_line(b[1], a[0], a[1])) > off_mm:
                    continue
                ends = [a[0], a[1], b[0], b[1]]
                far = max(((p, q) for p in ends for q in ends), key=lambda pq: G.dist(*pq))
                if G.dist(*far) > la + lb + gap_mm:
                    continue
                segs[i] = [far[0], far[1], a[2], a[3]]
                del segs[j]
                merged = True
                break
            if merged:
                break
    return [tuple(s) for s in segs]


def _cross_point(a, b):
    """Where segments a and b cross, as fractions along each, or None."""
    (x1, y1), (x2, y2) = a[0], a[1]
    (x3, y3), (x4, y4) = b[0], b[1]
    den = (x2 - x1) * (y4 - y3) - (y2 - y1) * (x4 - x3)
    if abs(den) < 1e-9:
        return None
    t = ((x3 - x1) * (y4 - y3) - (y3 - y1) * (x4 - x3)) / den
    u = ((x3 - x1) * (y2 - y1) - (y3 - y1) * (x2 - x1)) / den
    return t, u


def openings_from_crosses(lines):
    """Cut-outs drawn as an X: two lines crossing near both their middles.

    `lines` is [(start, end, layer, handle)]. Each X gives the four-cornered
    outline through its line ends. A line that is part of no X is left alone.
    """
    segs = _merge_collinear(lines)
    used = set()
    out = []
    for i, a in enumerate(segs):
        if i in used:
            continue
        for j in range(i + 1, len(segs)):
            if j in used:
                continue
            b = segs[j]
            tu = _cross_point(a, b)
            if tu is None or not (0.3 <= tu[0] <= 0.7 and 0.3 <= tu[1] <= 0.7):
                continue
            la, lb = G.dist(a[0], a[1]), G.dist(b[0], b[1])
            if min(la, lb) < 0.7 * max(la, lb):
                continue
            used.update((i, j))
            out.append(([a[0], b[0], a[1], b[1]], a[2], a[3]))
            break
    for k, (pts, layer, handle) in enumerate(out):
        # corners in order round the outline, whichever way the lines were drawn
        cx = sum(p[0] for p in pts) / 4.0
        cy = sum(p[1] for p in pts) / 4.0
        pts.sort(key=lambda p: math.atan2(p[1] - cy, p[0] - cx))
        out[k] = ([(round(p[0], 1), round(p[1], 1)) for p in pts], layer, handle)
    return out


FOLD_MIN_GAP_MM, FOLD_MAX_GAP_MM = 80.0, 450.0
FOLD_REACH_MM = 300.0       # a fold is run on this far to meet the beams when cutting


def folds_from_lines(lines) -> list:
    """Slab folds from the open lines on the slab layer: [(start, end, width, layer, handle)].

    Every Reis Magos plan draws a fold as two parallel lines, 200 mm apart (250 or
    150 on Wing C 2nd floor), overlapping along their length; one is often
    dashed. Each such pair is one fold, on the centreline between them, as long
    as the two overlap. A line with no partner comes back with width None: it
    still divides slabs, but there is nothing to size an element from.
    """
    segs = [(a, b, lay, h) for a, b, lay, h in lines if G.dist(a, b) > 50]
    used, out = set(), []
    for i, (a, b, lay, h) in enumerate(segs):
        if i in used:
            continue
        L = G.dist(a, b)
        ux, uy = (b[0] - a[0]) / L, (b[1] - a[1]) / L
        best = None
        for j, (c, d, _l2, _h2) in enumerate(segs):
            if j == i or j in used:
                continue
            M = G.dist(c, d)
            vx, vy = (d[0] - c[0]) / M, (d[1] - c[1]) / M
            if abs(abs(ux * vx + uy * vy) - 1.0) > 0.002:          # not parallel
                continue
            off = (c[0] - a[0]) * -uy + (c[1] - a[1]) * ux
            if not (FOLD_MIN_GAP_MM <= abs(off) <= FOLD_MAX_GAP_MM):
                continue
            t0 = (c[0] - a[0]) * ux + (c[1] - a[1]) * uy
            t1 = (d[0] - a[0]) * ux + (d[1] - a[1]) * uy
            lo, hi = max(min(t0, t1), 0.0), min(max(t0, t1), L)
            if hi - lo < 0.3 * min(L, M):
                continue
            if best is None or abs(off) < abs(best[0]):
                best = (off, lo, hi, j)
        if best is None:
            continue
        off, lo, hi, j = best
        used.update((i, j))
        mx, my = -uy * off / 2.0, ux * off / 2.0                  # to the midline
        p = (a[0] + ux * lo + mx, a[1] + uy * lo + my)
        q = (a[0] + ux * hi + mx, a[1] + uy * hi + my)
        out.append((p, q, round(abs(off), 1), lay, h))
    from shapely.geometry import LineString
    from shapely.ops import unary_union
    out = _meet_at_corners(out)
    paired = unary_union([LineString([p, q]).buffer(w / 2.0 + 30.0)
                          for p, q, w, _l, _h in out if w])
    for i, (a, b, lay, h) in enumerate(segs):
        if i in used:
            continue
        # the leftover end of the inner line at an L corner, now inside a fold
        if not paired.is_empty and paired.contains(LineString([a, b])):
            continue
        out.append((a, b, None, lay, h))
    return out


def _meet_at_corners(folds, reach_widths: float = 2.0):
    """Run two folds that turn an L corner on to each other.

    The pairs are matched only where both lines overlap, so at an L the inner
    line's last 200-250 mm has no partner and each fold stops short of the other:
    the corner square was left open and the slab beside it zig-zagged into it
    (Wing B 2nd floor, S1A round the lift core). Each is taken to where the two
    centrelines cross, plus half its width, so the corner is filled.
    """
    out = [list(f) for f in folds]
    for i, f in enumerate(out):
        if not f[2]:
            continue
        for g in out[i + 1:]:
            if not g[2]:
                continue
            u = (f[1][0] - f[0][0], f[1][1] - f[0][1])
            v = (g[1][0] - g[0][0], g[1][1] - g[0][1])
            lu, lv = math.hypot(*u), math.hypot(*v)
            if lu < 1 or lv < 1 or abs(u[0] * v[0] + u[1] * v[1]) / (lu * lv) > 0.1:
                continue                                  # not square to each other
            x = G.line_intersection(f[0], f[1], g[0], g[1])
            if x is None:
                continue
            reach = reach_widths * max(f[2], g[2])
            for fold in (f, g):
                if min(G.dist(fold[0], x), G.dist(fold[1], x)) > reach:
                    break
            else:
                for fold in (f, g):
                    k = 0 if G.dist(fold[0], x) < G.dist(fold[1], x) else 1
                    other = fold[1 - k]
                    # to the far face of the other fold
                    t = G.dist(other, x) + (g[2] if fold is f else f[2]) / 2.0
                    if t <= G.dist(other, fold[k]):
                        continue                          # only ever lengthened
                    ux, uy = (x[0] - other[0]), (x[1] - other[1])
                    n = math.hypot(ux, uy) or 1.0
                    fold[k] = (round(other[0] + ux / n * t, 1), round(other[1] + uy / n * t, 1))
    return [tuple(f) for f in out]


def _ring(poly) -> list[G.Point]:
    pts = [(round(x, 1), round(y, 1)) for x, y in poly.exterior.coords]
    return pts[:-1] if len(pts) > 1 and pts[0] == pts[-1] else pts


def derive_panels(res: Result, cfg, log=lambda *_: None) -> None:
    """Fill res.slabs from the beam bays wherever no slab outline is drawn.

    A closed outline on the slab layer is used as drawn, and takes the mark
    inside it. Drawings mix the two: Wing B 1st floor has 72 marks and just two
    outlines, the small S8A slabs beside two ducts. So an outline only covers its
    own mark, and every other mark still gets its bay. An "outline" around marks
    of several different slabs is not a panel - on Wing C 2nd floor it is a 24 x
    12 m rectangle over half the plan - and is left out and reported.
    """
    fill_unmarked = bool(cfg.get("tolerances", {}).get("slab_fill_unmarked_bays", False))
    if not res.slab_tags and (fill_unmarked or not res.slabs):
        return
    try:
        from shapely.geometry import LineString, Point, Polygon
        from shapely.ops import unary_union
    except ImportError:
        res.add(BLOCKER, "slabs",
                "Slab panels are found from the beams around them, which needs the "
                "'shapely' library, and it is not installed. Run:  python -m pip "
                "install shapely  and extract again.")
        return

    tol = cfg.get("tolerances", {})
    gap = float(tol.get("slab_gap_close_mm", 150))
    min_area = float(tol.get("slab_min_area_mm2", 250000))
    default_w = float(cfg.get("defaults", {}).get("beam_width_mm", 230))

    drawn, tags_left = _take_drawn(res, Point, Polygon, keep_unmarked=fill_unmarked)
    if not tags_left:
        return

    parts = []
    for b in res.beams:
        pts = _arc_points(b.start, b.mid, b.end) if b.mid else [b.start, b.end]
        if G.dist(pts[0], pts[-1]) < 1.0 and len(pts) == 2:
            continue
        w = b.width or default_w
        parts.append(LineString(pts).buffer(w / 2.0, cap_style=2, join_style=2))
    # the sides of a cut-out drawn on the beam layer: not beams (extract._drop_opening_outlines),
    # but the bays still stop at them
    for a, b in res.stats.get("_opening_edges") or []:
        if G.dist(a, b) >= 1.0:
            parts.append(LineString([a, b]).buffer(1.0, cap_style=2, join_style=2))
    framed = len(res.beams) >= 2
    masonry = []                    # brick partitions: they trim a rounded closing only (below)
    for el in list(res.columns) + list(res.walls):
        if len(el.outline) < 3:
            continue
        if framed and el in res.walls and MASONRY.search(el.source_layer or ""):
            # a brick partition stands under the slab, it does not cut it: ALCAZAR
            # 1st floor's partitions split bays and left 240 mm strips of slab
            # between a beam and the wall. (A plan with no beams keeps its walls.)
            masonry.append(Polygon(el.outline).buffer(0))
            continue
        p = Polygon(el.outline).buffer(0)
        # A "wall" with a slab mark inside it is an area, not a wall: on Wing C
        # 1st floor two 10-13 m2 outlines on R.C.C. PARDI sat over the S8A slabs
        # and took them out. A real wall never has a slab mark inside it.
        inside = sorted({t.mark for t in tags_left if p.contains(Point(t.x, t.y))})
        if inside and el in res.walls:
            c = p.representative_point()
            res.add(WARNING, "slabs",
                    f"A closed outline on '{el.source_layer}' ({p.area / 1e6:.1f} m2) is "
                    f"read as a wall but holds slab marks {', '.join(inside)}, so it is not "
                    "a wall around them: the slab was built through it. Check whether it "
                    "should be a wall at all.", layer=el.source_layer, x=c.x, y=c.y,
                    handle=el.handle)
            continue
        parts.append(p)
    if not parts:
        return

    frame = unary_union(parts)
    frame = frame.buffer(gap / 2.0, join_style=2).buffer(-gap / 2.0, join_style=2)
    bodies = list(frame.geoms) if hasattr(frame, "geoms") else [frame]
    bays = [Polygon(r) for body in bodies for r in body.interiors]
    bays = [b for b in bays if b.area >= min_area]

    # Slab is everywhere except at a cut-out (the X): a marked area the beams
    # leave open - an edge beam missing, or short of its column - is still slab.
    # The frame is closed across gaps up to 2 x slab_open_gap_mm, and any area
    # that closes up and holds a slab mark becomes a bay. Areas holding no mark
    # are not added this way, so nothing outside the building is filled.
    reach_open = float(tol.get("slab_open_gap_mm", 1500))
    strip_tags = []                 # marks left to their hatch strip (still a neighbour's mark below)
    if reach_open > 0:
        closed = frame.buffer(reach_open, join_style=1).buffer(-reach_open, join_style=1)
        extra = closed.difference(frame)
        if bays:
            extra = extra.difference(unary_union(bays))
        brick = unary_union(masonry) if masonry else None
        # (only marks no closed bay holds count: those are the ones the hatch rule builds)
        strips = _strip_hatches(res, [t for t in tags_left
                                      if not any(b.contains(Point(t.x, t.y)) for b in bays)], Point, Polygon)
        for g in _pieces(extra.buffer(-40, join_style=2).buffer(40, join_style=2), min_area):
            mine = [t for t in tags_left if g.contains(Point(t.x, t.y))]
            if mine and any(all(z.contains(Point(t.x, t.y)) for t in mine) for z in strips):
                # the drawing gives this slab's edge: the sunk hatch its marks sit in (built
                # from the hatch below), not an edge rounded off across the gap
                strip_tags += mine
                continue
            if mine:
                if brick is not None and g.intersects(brick):
                    g = _trim_past_brick(g, brick, frame, tags_left, Point, unary_union, min_area)
                bays.append(g)
                res.add(INFO, "slabs",
                        "A slab mark sits in an area the beams do not fully close (a beam "
                        "missing or stopping short); the slab was built across the gap. "
                        "Check its edge.", x=g.representative_point().x,
                        y=g.representative_point().y)
    if drawn:
        # what a drawn outline covers is that slab's; the rest of the bay is free
        cover = unary_union(drawn)
        rest = []
        for b in bays:
            if b.intersects(cover):
                left = b.difference(cover).buffer(-40, join_style=2).buffer(40, join_style=2)
                rest += [g for g in (getattr(left, "geoms", None) or [left])
                         if not g.is_empty and g.area >= min_area]
            else:
                rest.append(b)
        bays = rest

    in_bay: dict[int, list] = {}
    outside = []
    for t in tags_left:
        pt = Point(t.x, t.y)
        hit = next((i for i, b in enumerate(bays) if b.contains(pt)), None)
        if hit is None:
            outside.append(t)
        else:
            in_bay.setdefault(hit, []).append(t)

    # A mark whose insertion point lands on the beam line, with the text running
    # into the bay beside it (S8 on Wing A 2nd floor, 34 and 87 mm off), is taken
    # for that bay - but only a bay with no mark of its own, or one taken the same
    # way by the same mark. Kept to slab_tag_snap_mm on purpose: tried at 1 m, it
    # put marks from open areas left by a missing beam into the wrong bay, one of
    # them a lift pit. Anything further stays outside and is reported.
    reach = float(tol.get("slab_tag_snap_mm", 100))
    snapped: dict[int, str] = {}
    still = []
    for t in outside:
        pt = Point(t.x, t.y)
        near = [(b.distance(pt), i) for i, b in enumerate(bays)
                if b.distance(pt) <= reach
                and (i not in in_bay or snapped.get(i) == t.mark)]
        if not near:
            still.append(t)
            continue
        i = min(near)[1]
        in_bay.setdefault(i, []).append(t)
        snapped[i] = t.mark
        res.add(WARNING, "slabs",
                f"Slab mark {t.mark} sits on the beam line, {min(near)[0]:.0f} mm outside "
                "the bay beside it, which has no mark of its own, so it was taken for "
                "that bay. Check it is the slab meant.", mark=t.mark, x=t.x, y=t.y, handle=t.handle)
    outside = still

    # A closed bay with no slab mark is slab too: only a drawn cut-out (the X)
    # leaves the floor open, and that is taken out afterwards. It takes the mark
    # and thickness of the nearest slab mark, and says so in its notes.
    # Only a mark that is in a bay itself: a cantilever's mark outside the
    # building is nearer to the edge bays than any real neighbour.
    # (a strip's marks too: they are a slab beside the bay as before, only shaped by the hatch)
    placed = [t for ts in in_bay.values() for t in ts] + strip_tags
    sized_tags = [t for t in placed if t.thickness] or placed
    unmarked = set()
    # ...except a bay with a staircase drawn in it and no mark of its own: the stair
    # well (New block: the bay round the stair and lift was slabbed over as NS3)
    stair_pts = res.stats.pop("_stair_points", None) or []
    # A bay inside a sunk area takes a mark in that same area first: SB beams split
    # one sunk toilet row into small bays, and only the first carries the mark (New
    # block: the next ones took NS5 and NS2 from the rooms beside the toilets).
    zones = []
    for z in res.sunk:
        if len(z.outline) >= 3:
            zp = Polygon(z.outline).buffer(0)
            if zp.area > 0:
                zones.append(zp)
    left_open = 0
    for i, b in enumerate(bays):
        if i in in_bay or not sized_tags:
            continue
        if not fill_unmarked:
            # no slab mark written in the bay: no slab (the user's rule)
            left_open += 1
            c = b.representative_point()
            res.add(INFO, "slabs",
                    f"A bay closed by beams ({b.area / 1e6:.1f} m2) has no slab mark written in it, so no "
                    "slab was built there (no mark, no slab). If it is slab, write its mark in the bay "
                    "on the drawing.", x=c.x, y=c.y)
            continue
        if stair_pts and sum(1 for x, y in stair_pts if b.contains(Point(x, y))) >= STAIR_MIN_LINES:
            c = b.representative_point()
            res.add(INFO, "slabs", "A bay with no slab mark holds the staircase drawn on the stair layer, "
                    "so it is left open as the stair well.", x=c.x, y=c.y)
            continue
        pool = sized_tags
        for z in zones:
            if z.intersection(b).area > 0.5 * b.area:
                same = [t for t in sized_tags if z.buffer(150.0).contains(Point(t.x, t.y))]
                if same:
                    pool = same
                    break
        in_bay[i] = [min(pool, key=lambda t: b.distance(Point(t.x, t.y)))]
        unmarked.add(i)

    # A slab fold divides a bay just as a beam does, though it is not part of the
    # frame: S8A (sunk) and S8 (level) share one bay on Wing B 1st floor with only
    # a fold between them. Each piece takes the mark inside it; a piece with no
    # mark - the sunk corner of the S9 bay - takes the nearest mark in its bay,
    # so it is not left open. The fold's own 200 mm strip is the fold's concrete.
    # A fold is drafted to stop a little short of the beams (S8A/S8 above: ~100 mm
    # each end), which would leave the bay in one piece, so for cutting it is run
    # on up to FOLD_REACH_MM - but only into the bay it lies in.
    # An end is only run on if that reaches the edge of the bay: at an L corner,
    # where the fold meets another fold, running on would notch the slab.
    def strip(f, bay=None):
        a, b = f.start, f.end
        L = G.dist(a, b)
        if L < 1:
            return None
        ux, uy = (b[0] - a[0]) / L, (b[1] - a[1]) / L
        if bay is not None:
            r = FOLD_REACH_MM
            a2 = (a[0] - ux * r, a[1] - uy * r)
            b2 = (b[0] + ux * r, b[1] + uy * r)
            a = a2 if not bay.contains(Point(a2)) else a
            b = b2 if not bay.contains(Point(b2)) else b
        return LineString([a, b]).buffer((f.width or 1.0) / 2.0, cap_style=2, join_style=2)

    # Only a fold with a bay on both sides cuts. One running beside a girder or
    # beam (Wing B 1st floor, two along GB4 by FC5) has no slab on one side, is
    # not built, and cutting along it left a 200 mm gap in the slab.
    def both_sides_in_bays(f):
        L = G.dist(f.start, f.end)
        if L < 1:
            return False
        ux, uy = (f.end[0] - f.start[0]) / L, (f.end[1] - f.start[1]) / L
        out = (f.width or 1.0) / 2.0 + 150.0
        for sign in (1, -1):
            if not any(any(b.contains(Point(f.start[0] + ux * L * t - uy * out * sign,
                                            f.start[1] + uy * L * t + ux * out * sign))
                           for b in bays) for t in (0.5, 0.25, 0.75)):
                return False
        return True

    folds = [(f, strip(f)) for f in res.folds if both_sides_in_bays(f)]
    folds = [(f, s) for f, s in folds if s is not None]
    units = []                                   # (shape, its marks, taken from a neighbour)
    for i, tags in sorted(in_bay.items()):
        bay = bays[i]
        cuts = [strip(f, bay) for f, s in folds if s.intersects(bay)]
        if not cuts:
            units.append((bay, tags, "unmarked" if i in unmarked else False))
            continue
        pieces = _pieces(bay.difference(unary_union(cuts)), SUNK_MIN_AREA_MM2) or [bay]
        for g in pieces:
            own = [t for t in tags if g.contains(Point(t.x, t.y))]
            if own:
                units.append((g, own, False))
            else:
                near = min(tags, key=lambda t: g.distance(Point(t.x, t.y)))
                units.append((g, [near], "unmarked" if i in unmarked else "fold"))

    mixed = 0
    for bay, tags, borrowed in units:
        marks = sorted({t.mark for t in tags})
        sized = [t for t in tags if t.thickness]
        thick = max(sized, key=lambda t: t.thickness) if sized else None
        notes = []
        if len(marks) > 1:
            mixed += 1
            c = bay.representative_point()
            ths = ", ".join(f"{t.mark} {t.thickness:.0f}" if t.thickness else f"{t.mark} ?"
                            for t in sorted(tags, key=lambda t: t.mark))
            notes.append("marks " + " and ".join(marks) + " share this bay")
            res.add(WARNING, "slabs",
                    f"Slab marks {' and '.join(marks)} ({ths} mm) sit in one bay with no "
                    "beam between them, so they were built as one panel"
                    + (f" at {thick.thickness:.0f} mm, the thicker" if thick else "")
                    + ". If a beam divides them on the drawing, it was not extracted.",
                    mark="/".join(marks), x=c.x, y=c.y)
        chosen = thick or tags[0]
        sunk = chosen.sunk_mm or 0.0
        if len({t.sunk_mm or 0.0 for t in tags}) > 1:
            c = bay.representative_point()
            res.add(WARNING, "slabs",
                    f"Slab marks {' and '.join(marks)} share one bay but the schedule sinks "
                    f"them by different amounts. The panel follows {chosen.mark} "
                    f"({sunk:.0f} mm); check whether a beam between them is missing.",
                    mark="/".join(marks), x=c.x, y=c.y)
        if sunk:
            notes.append(f"sunk {sunk:.0f} mm (slab schedule)")
        if borrowed == "fold":
            notes.append(f"part of the {chosen.mark} bay on the far side of a slab fold")
        elif borrowed:
            notes.append(f"no slab mark in this bay: mark and thickness of the nearest, {chosen.mark}")
        shape = bay.simplify(1.0)
        res.slabs.append(SlabPanel(
            mark=chosen.mark, outline=_ring(shape),
            source_layer=SOURCE, handle="", notes=notes,
            thickness=thick.thickness if thick else None,
            thickness_source=thick.thickness_source if thick else "",
            sunk_mm=sunk,
            # a drawn outline standing clear of the beams leaves a hole in its bay
            holes=[[(round(x, 1), round(y, 1)) for x, y in r.coords][:-1]
                   for r in shape.interiors]))

    outside = _cantilevers_from_hatches(res, outside, Point, Polygon, unary_union, min_area)
    _cut_openings(res, Polygon, unary_union, min_area)
    wells = _cut_stairs(res, res.stats.pop("_stair_segments", None) or [], LineString, Point, Polygon,
                        unary_union, min_area)
    _apply_sunk(res, Polygon, unary_union, min_area)
    _clean_for_revit(res, Polygon)
    _drop_stair_scraps(res, wells, Point, Polygon)
    _size_folds(res, Point, Polygon)

    empty = len(unmarked)
    log(f"  Slab panels from the beam bays: {len(res.slabs)} built, "
        + (f"{empty} bays with no mark given the nearest mark's slab" if fill_unmarked
           else f"{left_open} bays with no mark left open")
        + f", {len(outside)} marks outside any bay")
    derived = sum(1 for s in res.slabs if s.source_layer == SOURCE)
    res.add(INFO, "slabs",
            (f"{len(drawn)} slab outlines are drawn; for the other marks " if drawn
             else "The drawing has no slab outlines, so ")
            + f"{derived} panels were taken "
            "from the bays the beams enclose, each with the slab mark inside it and that "
            "mark's scheduled thickness. Panels run to the beam faces. "
            + (f"{empty} closed bays hold no slab mark; slab is everywhere but at a drawn "
               "cut-out (X), so each was given the mark and thickness of the nearest slab "
               "mark. Check them in report.html (notes: 'no slab mark in this bay')."
               if fill_unmarked else
               f"{left_open} closed bays hold no slab mark and were left open: no mark, no slab."))
    unsized = [s for s in res.slabs if not s.thickness]
    if unsized:
        names = sorted({s.mark for s in unsized})
        res.add(WARNING, "slabs",
                f"{len(unsized)} slab panels ({', '.join(names)}) have no thickness in the "
                "slab schedule, and will be built at the Revit script's default.")
    for t in outside:
        res.add(WARNING, "slabs",
                f"Slab {t.mark} is not inside any bay closed by beams, so no panel was "
                "built for it. A cantilever slab, or a bay left open by a missing beam, "
                "has no edge to take; model it by hand.",
                mark=t.mark, x=t.x, y=t.y, handle=t.handle)


def _trim_past_brick(g, brick, frame, tags, Point, unary_union, min_area):
    """A bay closed across an opening (rounded off where no beam closes it): the part a
    brick wall cuts off that reaches the rounded edge and holds no slab mark is outside.

    ALCAZAR 1st floor, below column 57: brick partitions no longer cut bays, so the S2A
    bay leaked through the 250 mm between the column and B86A and was rounded off into
    a quarter circle past the brick wall that closes it. Only such end parts go; a
    partition inside the bay does not split it."""
    edge = g.boundary.difference(frame.buffer(30.0))          # the invented, rounded edge
    if edge.is_empty:
        return g
    drop = []
    for part in _pieces(g.difference(brick), min_area / 10.0):
        if part.buffer(30.0).intersection(edge).length > 300.0 and \
                not any(part.contains(Point(t.x, t.y)) for t in tags):
            drop.append(part)
    if not drop:
        return g
    left = g.difference(unary_union(drop).buffer(5.0, join_style=2))
    keep = [q for q in _pieces(left.buffer(-40, join_style=2).buffer(40, join_style=2), min_area)
            if any(q.contains(Point(t.x, t.y)) for t in tags)]
    return max(keep, key=lambda q: q.area) if keep else g


CANTILEVER_MAX_M2 = 60.0      # a hatch bigger than this is not one cantilever slab


def _cantilevers_from_hatches(res: Result, outside, Point, Polygon, unary_union, min_area):
    """A slab mark outside every beam bay takes its shape from the sunk hatch round it.

    A cantilever slab (chajja) hangs off the edge beam with free edges, so no
    beams close round it and there is no bay to take. Wing B 2nd floor SC1 is
    such a slab, 150 sunk, and MOFA's hatch of it is its outline. The hatch is
    cut back off the beams, columns, walls and slabs already there. Marks with
    no hatch round them stay reported. Returns the marks still not built.
    """
    from shapely.geometry import LineString
    if not outside or not res.sunk:
        return outside
    solid = [LineString([b.start, b.end]).buffer((b.width or 230.0) / 2.0, cap_style=2)
             for b in res.beams if not getattr(b, "mid", None) and G.dist(b.start, b.end) > 1]
    solid += [Polygon(c.outline).buffer(0) for c in res.columns if len(c.outline) >= 3]
    solid += [Polygon(w.outline).buffer(0) for w in res.walls if len(w.outline) >= 3]
    solid += [Polygon(s.outline).buffer(0) for s in res.slabs if len(s.outline) >= 3]
    taken = unary_union(solid) if solid else None
    # a hatch whose depth the legend (or an override) states - or, with no depth, one that
    # holds this slab's mark and no other (the mark says it is slab, as for a strip below:
    # J1153 1st floor 13-11-2025, the S3 and S7 ledges down both sides, on SUNK with no
    # legend, were all left unbuilt) - and none from an attached architectural drawing
    # ('file$0$SUNK'): those are not slab outlines
    zones = [(z, Polygon(z.outline).buffer(0)) for z in res.sunk
             if len(z.outline) >= 3 and "$" not in (z.source_layer or "")]
    zones = [(z, g) for z, g in zones
             if z.depth_mm or len({t.mark for t in res.slab_tags if g.contains(Point(t.x, t.y))}) == 1]
    left, built = [], []
    outside, built_strips, strips = _strips_from_hatches(res, outside, taken, Point, Polygon, min_area)
    if strips and taken is not None:
        taken = unary_union([taken] + [Polygon(s.outline).buffer(0) for s in strips])
    if built_strips:
        res.add(INFO, "slabs",
                f"{len(built_strips)} sunk hatches lie outside every bay closed by beams and hold "
                "several marks of one slab (a strip along the edge, its mark written once per bay); "
                "each was built as that slab, cut back off the beams: " + ", ".join(built_strips)
                + ". Check their edges against the drawing.")
    for t in outside:
        p = Point(t.x, t.y)
        hit = [(z, g) for z, g in zones if g.contains(p) and g.area <= CANTILEVER_MAX_M2 * 1e6]
        if not hit:
            left.append(t)
            continue
        z, g = min(hit, key=lambda zg: zg[1].area)
        shape = g.difference(taken) if taken is not None else g
        pieces = [q for q in _pieces(shape, min_area) if q.contains(p)] or                  [q for q in _pieces(shape, min_area) if q.distance(p) < 300.0]
        if not pieces:
            left.append(t)
            continue
        shape = max(pieces, key=lambda q: q.area).simplify(1.0)
        sunk = t.sunk_mm or (z.depth_mm or 0.0)
        res.slabs.append(SlabPanel(
            mark=t.mark, outline=_ring(shape), source_layer="cantilever (from its hatch)",
            handle=z.handle, thickness=t.thickness, thickness_source=t.thickness_source,
            sunk_mm=sunk,
            notes=["cantilever: no beams round it, shape from the sunk hatch it sits in"]
                  + ([f"sunk {sunk:.0f} mm (slab schedule)"] if t.sunk_mm else [])
                  + (["the plan gives the hatch no depth: built at the level"]
                     if not t.sunk_mm and not z.depth_mm else []),
            holes=[[(round(x, 1), round(y, 1)) for x, y in r.coords][:-1]
                   for r in shape.interiors]))
        if taken is not None:
            taken = taken.union(shape)
        built.append(f"{t.mark} ({shape.area / 1e6:.1f} m2)")
    if built:
        res.add(INFO, "slabs",
                f"{len(built)} slab marks lie outside every bay closed by beams - cantilever "
                "slabs - and were built from the sunk hatch each sits in, cut back off the "
                "beams: " + ", ".join(built) + ". Check their free edges against the drawing.")
    return left


def _strip_hatches(res: Result, tags, Point, Polygon) -> list:
    """The sunk hatches _strips_from_hatches builds as one slab: two or more marks of
    one slab inside, not too large for them.

    Stilt A wing, top right: B89 along the retaining wall is drawn only as short cross
    pieces there, so the S1 strip between the edge beam and the wall is open on one
    side. Closed across that gap, it came out with scalloped (rounded) edges, and cut
    the strip below along the same curve. Its hatch is its drawn shape."""
    out = []
    for z in res.sunk:
        if len(z.outline) < 3 or "$" in (z.source_layer or ""):
            continue
        g = Polygon(z.outline).buffer(0)
        if g.is_empty or g.area <= 0:
            continue
        inside = [t for t in tags if g.contains(Point(t.x, t.y))]
        if len(inside) >= 2 and len({t.mark for t in inside}) == 1 and \
                g.area <= CANTILEVER_MAX_M2 * 1e6 * len(inside):
            out.append(g)
    return out


def _strips_from_hatches(res: Result, outside, taken, Point, Polygon, min_area):
    """Several marks of one slab in one sunk hatch outside every beam bay: the hatch
    is that slab.

    Stilt A wing: the 600 sunk S1 runs round the building between the edge beams
    and the retaining wall, one hatch down each side, its mark written once per bay
    (11 and 10 marks, 46 and 66 m2). Each hatch, cut back off the beams, columns,
    walls and slabs already there, is built as that slab, one panel per piece that
    holds the marks. The legend need not state the hatch's depth: the marks inside
    say it is slab. A hatch holding different marks, or just one, is left to the
    one-mark cantilever rule. Returns (marks still not built, report lines, panels).
    """
    zones = [(z, Polygon(z.outline).buffer(0)) for z in res.sunk
             if len(z.outline) >= 3 and "$" not in (z.source_layer or "")]
    claimed, names, made = set(), [], []
    for z, g in zones:
        if g.is_empty or g.area <= 0:
            continue
        inside = [t for t in outside if id(t) not in claimed and g.contains(Point(t.x, t.y))]
        if len(inside) < 2 or len({t.mark for t in inside}) != 1:
            continue
        if g.area > CANTILEVER_MAX_M2 * 1e6 * len(inside):
            continue
        shape = g.difference(taken) if taken is not None else g
        for q in _pieces(shape, min_area):
            own = [t for t in inside if id(t) not in claimed and q.distance(Point(t.x, t.y)) < 300.0]
            if not own:
                continue
            q = q.simplify(1.0)
            t0 = next((t for t in own if t.thickness), own[0])
            sunk = t0.sunk_mm or (z.depth_mm or 0.0)
            notes = [f"no beams close it: shape from the sunk hatch its {len(own)} marks sit in"]
            if t0.sunk_mm:
                notes.append(f"sunk {sunk:.0f} mm (slab schedule)")
            elif not z.depth_mm:
                notes.append("the plan gives the hatch no depth: built at the level")
            panel = SlabPanel(
                mark=t0.mark, outline=_ring(q), source_layer="slab strip (from its hatch)",
                handle=z.handle, thickness=t0.thickness, thickness_source=t0.thickness_source,
                sunk_mm=sunk, notes=notes,
                holes=[[(round(x, 1), round(y, 1)) for x, y in r.coords][:-1] for r in q.interiors])
            res.slabs.append(panel)
            made.append(panel)
            claimed.update(id(t) for t in own)
            names.append(f"{t0.mark} ({q.area / 1e6:.1f} m2, {len(own)} marks)")
            taken = taken.union(q) if taken is not None else q
    return [t for t in outside if id(t) not in claimed], names, made


def _take_drawn(res: Result, Point, Polygon, keep_unmarked: bool = True):
    """Match the drawn slab outlines to the marks inside them.

    Returns the outlines kept, as polygons, and the marks no outline covers.
    keep_unmarked=False: an outline with no slab mark in it is not a slab (the user's
    rule: no mark, no slab) - J1153 1st floor 13-11-2025 had eleven 0.2 m2 pieces.
    """
    kept, polys, claimed = [], [], set()
    for s in res.slabs:
        p = Polygon(s.outline).buffer(0)
        inside = [t for t in res.slab_tags if p.contains(Point(t.x, t.y))]
        if not inside and not keep_unmarked and \
                not any(p.distance(Point(t.x, t.y)) <= DRAWN_MARK_REACH_MM for t in res.slab_tags):
            c = p.representative_point()
            res.add(INFO, "slabs",
                    f"A closed shape on the slab layer ({p.area / 1e6:.2f} m2) has no slab mark in it, "
                    "so no slab was built from it (no mark, no slab). If it is a slab, write its mark "
                    "inside it on the drawing.", layer=s.source_layer, x=c.x, y=c.y, handle=s.handle)
            continue
        marks = sorted({t.mark for t in inside})
        if len(marks) > 1:
            c = p.representative_point()
            res.add(WARNING, "slabs",
                    f"A closed shape on the slab layer ({p.area / 1e6:.0f} m2) holds "
                    f"{len(marks)} different slab marks ({', '.join(marks[:6])}"
                    f"{' ...' if len(marks) > 6 else ''}), so it is not one slab panel. "
                    "It was left out and those marks were taken from their beam bays.",
                    layer=s.source_layer, x=c.x, y=c.y, handle=s.handle)
            continue
        if inside:
            sized = [t for t in inside if t.thickness]
            t = max(sized, key=lambda t: t.thickness) if sized else inside[0]
            s.mark, s.thickness, s.thickness_source = t.mark, t.thickness, t.thickness_source
            if t.sunk_mm:
                s.sunk_mm = t.sunk_mm
                s.notes.append(f"sunk {t.sunk_mm:.0f} mm (slab schedule)")
            claimed.update(id(t) for t in inside)
        kept.append(s)
        polys.append(p)
    res.slabs[:] = kept
    return polys, [t for t in res.slab_tags if id(t) not in claimed]


def _cut_openings(res: Result, Polygon, unary_union, min_area) -> None:
    """Take the drawn cut-outs out of the panels they fall in.

    A cut-out drawn inside a bay that also holds a slab mark - a duct beside a
    toilet slab, with no beam between - would otherwise be slabbed over. What is
    left of the panel is cleaned of slivers under 80 mm: the X is drawn to the
    beam faces by eye, and a 20 mm strip of slab along a beam is drafting slack.
    """
    holes = [Polygon(o.outline).buffer(0) for o in res.openings
             if o.kind == "opening" and len(o.outline) >= 3]
    holes = [h for h in holes if h.area > 0]
    # a slab mark written inside the X says there is slab: the drawing contradicts
    # itself (an architect's void under a structural slab) - the slab is kept, said
    from shapely.geometry import Point
    marked = [h for h in holes if any(h.contains(Point(t.x, t.y)) for t in res.slab_tags)]
    for h in marked:
        c = h.representative_point()
        res.add(WARNING, "slabs",
                "A cut-out drawn as an X has a slab mark written inside it; the slab was "
                "kept there. Check whether it is open.", x=c.x, y=c.y)
    holes = [h for h in holes if not any(h is m for m in marked)]
    if not holes:
        return
    kept, cut = [], 0
    for s in res.slabs:
        if s.source_layer != SOURCE:
            kept.append(s)
            continue
        p = Polygon(s.outline, s.holes or None)
        hit = [h for h in holes if h.intersection(p).area > 0.1 * h.area]
        if not hit:
            kept.append(s)
            continue
        cut += len(hit)
        left = p.difference(unary_union(hit))
        left = left.buffer(-40, join_style=2).buffer(40, join_style=2)
        pieces = [g for g in (getattr(left, "geoms", None) or [left])
                  if not g.is_empty and g.area >= min_area]
        for g in pieces:
            g = g.simplify(1.0)
            kept.append(SlabPanel(
                mark=s.mark, outline=_ring(g), source_layer=SOURCE,
                handle="", notes=list(s.notes) + ["cut-out taken out"],
                thickness=s.thickness, thickness_source=s.thickness_source,
                sunk_mm=s.sunk_mm,
                holes=[[(round(x, 1), round(y, 1)) for x, y in r.coords][:-1]
                       for r in g.interiors]))
    res.slabs[:] = kept
    if cut:
        res.add(INFO, "slabs",
                f"{cut} cut-outs drawn as an X fall inside a marked slab bay and were "
                "taken out of the slab.")


def _stair_wells(segs, LineString, unary_union) -> list:
    """The staircases drawn on the stair layer, one shape each: its lines (treads,
    stringers, both flights) gathered where they lie within STAIR_REACH_MM of each other,
    and the area they cover - the gaps between them closed, nothing beyond their ends (a
    hull round a stair and its landing line cut a slanted corner off the slab beside it,
    and swallowed the slab mark written on the landing). A handful of lines is not a
    staircase."""
    lines = [LineString([a, b]) for a, b in segs if G.dist(a, b) >= 1.0]
    if len(lines) < STAIR_MIN_LINES:
        return []
    r = STAIR_REACH_MM / 2.0
    blob = unary_union([ln.buffer(r, cap_style=3, join_style=2) for ln in lines])
    wells = []
    for g in getattr(blob, "geoms", None) or [blob]:
        mine = [ln for ln in lines if g.contains(ln)]
        if len(mine) < STAIR_MIN_LINES:
            continue
        shape = g.buffer(-r, join_style=2)
        for piece in getattr(shape, "geoms", None) or [shape]:
            if piece.is_empty or piece.geom_type != "Polygon":
                continue
            piece = type(piece)(piece.exterior)            # the stair's area, no holes
            if 0 < piece.area <= STAIR_MAX_M2 * 1e6:
                wells.append(piece)
    return wells


def _cut_stairs(res: Result, segs, LineString, Point, Polygon, unary_union, min_area) -> None:
    """Take each drawn staircase out of the slab panel it lies in: no slab where the
    stair is (the user's rule). J1153 1st floor 13-11-2025: the duplex stairs at the end
    of each S8 bay and the stair hall by the entrance were slabbed over, because their
    bays carry slab marks. As with a cut-out (X), a slab mark written on the stair keeps
    the slab there, and is reported."""
    wells = _stair_wells(segs, LineString, unary_union)
    if not wells:
        return
    # a stair mark written on the flight (ST - the schedule's STAIRCASE row, the stair's own
    # waist slab: J1153 1st floor) names the stair, not a floor slab over it
    floor_tags = [t for t in res.slab_tags if not STAIR_MARK.match(t.mark or "")]
    marked = [w for w in wells if any(w.contains(Point(t.x, t.y)) for t in floor_tags)]
    for w in marked:
        c = w.representative_point()
        res.add(WARNING, "slabs",
                "A staircase is drawn where a slab mark is written; the slab was kept there. "
                "Check whether the floor is open over the stair.", x=c.x, y=c.y)
    wells = [w for w in wells if not any(w is m for m in marked)]
    kept, cut = [], 0
    for s in res.slabs:
        if s.source_layer != SOURCE:
            kept.append(s)
            continue
        p = Polygon(s.outline, s.holes or None)
        hit = [w for w in wells if w.intersection(p).area > 0.1 * w.area]
        if not hit:
            kept.append(s)
            continue
        cut += len(hit)
        left = p.difference(unary_union(hit))
        left = left.buffer(-40, join_style=2).buffer(40, join_style=2)
        for g in [g for g in (getattr(left, "geoms", None) or [left])
                  if not g.is_empty and g.geom_type == "Polygon" and g.area >= min_area]:
            g = g.simplify(1.0)
            kept.append(SlabPanel(
                mark=s.mark, outline=_ring(g), source_layer=SOURCE,
                handle="", notes=list(s.notes) + ["staircase taken out"],
                thickness=s.thickness, thickness_source=s.thickness_source,
                sunk_mm=s.sunk_mm,
                holes=[[(round(x, 1), round(y, 1)) for x, y in r.coords][:-1]
                       for r in g.interiors]))
    res.slabs[:] = kept
    if cut:
        res.add(INFO, "slabs",
                f"{cut} staircases drawn on the stair layer lie in slab bays and were taken out "
                "of the slab: no slab where a stair is.")
    return wells


def _drop_stair_scraps(res: Result, wells, Point, Polygon) -> None:
    """A small piece of slab with no mark of its own, touching a staircase that was taken
    out, is part of the stair: J1153 1st floor 13-11-2025, the S8 flights turn through
    winders drawn as diagonals, and the 0.6 m2 triangles between them were built as slab.
    Run after the panels are tidied for Revit, which is when a piece joined to its bay at
    one corner comes apart from it."""
    if not wells:
        return
    kept, scraps = [], 0
    for s in res.slabs:
        if s.source_layer == SOURCE and len(s.outline) >= 3:
            p = Polygon(s.outline).buffer(0)
            touching = any(w.distance(p) < 50.0 for w in wells)
            small = p.area < STAIR_REMNANT_M2 * 1e6 and \
                not any(p.contains(Point(t.x, t.y)) for t in res.slab_tags)
            if touching and small:
                scraps += 1
                continue
            if touching and STAIR_MARK.match(s.mark or ""):
                # what is left of a bay the schedule calls the staircase (ST: J1153 1st floor,
                # the wedge between the two entrance flights) is the stair too, not floor slab -
                # unless a floor slab's mark is written in that piece (the S1 landing below the
                # flights shared the ST bay, which took the thicker ST): then it is that slab
                floor = [t for t in res.slab_tags if p.contains(Point(t.x, t.y))
                         and not STAIR_MARK.match(t.mark or "")]
                if not floor:
                    scraps += 1
                    continue
                t = max(floor, key=lambda t: t.thickness or 0.0)
                s.mark, s.thickness, s.thickness_source = t.mark, t.thickness, t.thickness_source
                s.sunk_mm = t.sunk_mm or 0.0
                s.notes = [n for n in s.notes if "share this bay" not in n] + \
                    [f"part of a staircase bay left beside the stair; {t.mark} is written in it"]
        kept.append(s)
    res.slabs[:] = kept
    if scraps:
        res.add(INFO, "slabs",
                f"{scraps} pieces of slab beside a staircase - under {STAIR_REMNANT_M2:.1f} m2 with no "
                "slab mark of their own (winders, the gaps between the stair's lines), or marked as the "
                "staircase itself (ST) - were left open with the stair.")


SUNK_MIN_AREA_MM2 = 100000.0    # a sunk part smaller than 0.1 m2 is drafting slack


def _pieces(geom, min_area):
    return [g for g in (getattr(geom, "geoms", None) or [geom])
            if not g.is_empty and getattr(g, "area", 0) >= min_area
            and g.geom_type == "Polygon"]


def _apply_sunk(res: Result, Polygon, unary_union, min_area) -> None:
    """Drop the parts of each slab that a sunk hatch covers, by the hatch's depth.

    A sunk area is often only part of a bay, with no beam round it: a toilet in
    the corner of a room, edged on the plan by the red fold line (Wing B 1st
    floor, in the S9 bay between B79 and B82). So the slab is split along the
    hatch: the hatched part becomes its own panel at its sunk depth, the rest
    stays at the level, and both keep the slab's mark and thickness. A hatch
    covering the whole bay just drops the whole panel.

    A slab the schedule already calls sunk keeps the schedule's depth - it is
    stated per mark - and a hatch saying otherwise is reported.
    """
    from shapely.geometry import Point as _Point
    known: dict[float, object] = {}
    unknown = []
    areas = []                                   # each hatch on its own, with its depth
    for z in res.sunk:
        if len(z.outline) < 3:
            continue
        zp = Polygon(z.outline).buffer(0)
        if z.depth_mm:
            areas.append((zp, float(z.depth_mm)))
            known[float(z.depth_mm)] = zp if float(z.depth_mm) not in known                 else known[float(z.depth_mm)].union(zp)
        else:
            unknown.append(zp)
    if not known and not unknown:
        return

    kept, split, clash, in_unknown = [], 0, [], 0
    for s in res.slabs:
        p = Polygon(s.outline, s.holes or None).buffer(0)
        if p.area <= 0:
            kept.append(s)
            continue
        if s.sunk_mm:                  # the slab schedule already says how deep
            for d, u in known.items():
                if d != s.sunk_mm and u.intersection(p).area > 0.5 * p.area:
                    clash.append(f"{s.mark} ({s.sunk_mm:.0f} in the schedule, {d:.0f} hatched)")
            kept.append(s)
            continue
        parts, rest = [], p
        for d in sorted(known):
            part = rest.intersection(known[d])
            part = part.buffer(-40, join_style=2).buffer(40, join_style=2)
            if part.is_empty or part.area < SUNK_MIN_AREA_MM2:
                continue
            parts.append((part, d))
            rest = rest.difference(part)
        if not parts:
            if any(u.intersection(p).area >= 0.5 * p.area for u in unknown):
                in_unknown += 1
                s.notes.append("in a sunk zone of unknown depth, built at level")
            kept.append(s)
            continue
        # what is left, less slivers along the fold line thinner than 150 mm.
        # A sliver goes to the sunk part it touches, not away: an exploded hatch
        # stops short of the beam, and dropping the strip left gaps (0.6 m2 round
        # the Wing B 2nd floor lift core).
        clean = rest.buffer(-75, join_style=2).buffer(75, join_style=2)
        for bit in _pieces(rest.difference(clean), 1.0):
            touch = [(bit.buffer(1.0).intersection(part).area, i)
                     for i, (part, _d) in enumerate(parts)]
            best = max(touch)
            if best[0] > 0:
                part, d = parts[best[1]]
                parts[best[1]] = (part.union(bit).buffer(0.5, join_style=2)
                                  .buffer(-0.5, join_style=2).intersection(p), d)
        rest = clean
        rest_pieces = _pieces(rest, min_area)
        if not rest_pieces and len(parts) == 1:
            s.sunk_mm = parts[0][1]
            s.notes.append(f"sunk {parts[0][1]:.0f} mm (legend hatch)")
            kept.append(s)
            continue
        split += 1
        pieces = [(g, 0.0) for g in rest_pieces] +                  [(g, d) for part, d in parts for g in _pieces(part, SUNK_MIN_AREA_MM2)]
        shared = any("share this bay" in n for n in s.notes)
        for g, d in pieces:
            g = g.simplify(1.0)
            mark, thk, tsrc, notes = s.mark, s.thickness, s.thickness_source, list(s.notes)
            if shared:
                # a bay that held several marks, now cut along the hatch: a part with
                # one mark written in it is that mark's slab (New block: NS3 at level
                # beside the NS2 toilet, both in one bay - both were built as NS2)
                own = {t.mark: t for t in res.slab_tags if g.contains(_Point(t.x, t.y))}
                if len(own) == 1:
                    t = next(iter(own.values()))
                    mark = t.mark
                    if t.thickness:
                        thk, tsrc = t.thickness, t.thickness_source
                    notes = [n for n in notes if "share this bay" not in n] + \
                        [f"its own mark {mark}, written in this part of the bay"]
            elif d and not any(g.contains(_Point(t.x, t.y)) for t in res.slab_tags):
                # a sunk part with no mark in it, whose sunk area carries another mark
                # and not the bay's: that area's mark, as for an unmarked bay (New block:
                # the NS2 room's bay runs on into the NS4 toilet row, no beam between)
                area = next((zp for zp, zd in areas
                             if zd == d and zp.intersection(g).area > 0.5 * g.area), None)
                there = [t for t in res.slab_tags
                         if area is not None and area.buffer(150.0).contains(_Point(t.x, t.y))]
                if there and not any(t.mark == s.mark for t in there):
                    t = min(there, key=lambda t: g.distance(_Point(t.x, t.y)))
                    mark = t.mark
                    if t.thickness:
                        thk, tsrc = t.thickness, t.thickness_source
                    notes = notes + [f"no slab mark in this part: the mark written in the same "
                                     f"sunk area, {mark}"]
            kept.append(SlabPanel(
                mark=mark, outline=_ring(g), source_layer=s.source_layer, handle=s.handle,
                notes=notes + ([f"sunk part, {d:.0f} mm (legend hatch)"] if d
                               else ["part at level beside a sunk area"]),
                thickness=thk, thickness_source=tsrc, sunk_mm=d,
                holes=[[(round(x, 1), round(y, 1)) for x, y in r.coords][:-1]
                       for r in g.interiors]))
    res.slabs[:] = kept
    if split:
        res.add(INFO, "slabs",
                f"{split} slabs are only partly sunk: each was split along its sunk hatch "
                "into a sunk part and a part at the level.")
    if clash:
        res.add(WARNING, "slabs",
                "The slab schedule and the sunk hatch disagree for " + ", ".join(clash)
                + ". The schedule's depth was used.")
    if in_unknown:
        res.add(WARNING, "slabs",
                f"{in_unknown} slab panels lie in a sunk zone whose depth is not known, so "
                "they were built at the level. Set the depth in config.json under "
                "'sunk_depth_override' to have them dropped.")


REVIT_TOL_MM = 2.0      # shortest edge kept in a floor boundary
REVIT_GAP_MM = 5.0      # closest two parts of a boundary may come without touching


def _revit_rejects(s, Polygon) -> bool:
    """True for an outline Revit will not take as a floor boundary.

    Splitting a slab along an L-shaped fold can leave an outline that touches
    itself at the fold's corner - Wing B 1st floor's S7 passes through
    (2938051.7, 84466.9) twice - or a hole touching the edge. Both are valid
    shapes, and Revit refuses both: "curve loops intersect with each other".
    """
    from shapely.geometry import LinearRing, LineString
    rings = [s.outline] + list(s.holes or [])
    for r in rings:
        if len(r) < 3 or len({(round(p[0], 1), round(p[1], 1)) for p in r}) < len(r):
            return True                          # a corner visited twice
        if not LinearRing(r).is_simple:
            return True
        n = len(r)
        # Revit works to about 1 mm: an edge shorter than that, or two parts of
        # the outline closer than that without touching (Wing B 2nd floor S1A:
        # a hole 0.04 mm from the edge), is refused just the same.
        for i in range(n):
            if G.dist(r[i], r[(i + 1) % n]) < REVIT_TOL_MM:
                return True
        lens = [G.dist(r[k], r[(k + 1) % n]) for k in range(n)]
        total = sum(lens)
        for i in range(n):
            a = LineString([r[i], r[(i + 1) % n]])
            for j in range(i + 2, n):
                if i == 0 and j == n - 1:
                    continue                     # the edges either side of a corner
                # edges joined by a short jog (S12 on Wing B 2nd floor: 3.5 mm) are
                # simply close; Revit takes them
                between = sum(lens[i + 1:j])
                if min(between, total - between - lens[i] - lens[j]) < 3 * REVIT_GAP_MM:
                    continue
                if a.distance(LineString([r[j], r[(j + 1) % n]])) < REVIT_GAP_MM:
                    return True
    rings_ls = [LinearRing(r) for r in rings]
    for i in range(len(rings_ls)):
        for j in range(i + 1, len(rings_ls)):
            if rings_ls[i].distance(rings_ls[j]) < REVIT_GAP_MM:
                return True
    return False


def _pinch_points(poly):
    """Where an outline touches itself, or a hole touches the outline."""
    pts = []
    for ring in [poly.exterior] + list(poly.interiors):
        seen = {}
        for x, y in list(ring.coords)[:-1]:
            q = (round(x, 1), round(y, 1))
            if q in seen:
                pts.append((x, y))
            seen[q] = 1
    for h in poly.interiors:
        hit = h.intersection(poly.exterior)
        for g in getattr(hit, "geoms", None) or ([hit] if not hit.is_empty else []):
            pts.append((g.coords[0][0], g.coords[0][1]))
    return pts


def _split_at_pinches(poly, rejects, depth=0):
    """Cut a polygon through each pinch point along one of the outline's own
    edge lines there, until every piece is an outline Revit takes.

    Shrinking and regrowing does not work: it closes straight back onto the same
    touching corner (Wing B 2nd floor S1A, where a fold's slot ends on the edge).
    """
    from shapely.geometry import LineString
    from shapely.ops import split
    if depth > 6 or not rejects(poly):
        return [poly]
    x0, y0, x1, y1 = poly.bounds
    span = (x1 - x0) + (y1 - y0) + 1000.0
    for px, py in _pinch_points(poly):
        dirs = []
        for ring in [poly.exterior] + list(poly.interiors):
            c = list(ring.coords)
            for a, b in zip(c, c[1:]):
                if min(G.dist(a, (px, py)), G.dist(b, (px, py))) < 0.5 and G.dist(a, b) > 1:
                    L = G.dist(a, b)
                    dirs.append(((b[0] - a[0]) / L, (b[1] - a[1]) / L))
        for ux, uy in dirs:
            cut = LineString([(px - ux * span, py - uy * span), (px + ux * span, py + uy * span)])
            parts = [g for g in split(poly, cut).geoms if g.area > 1000.0]
            if len(parts) < 2:
                continue
            out = []
            for g in parts:
                out += _split_at_pinches(g, rejects, depth + 1)
            if not any(rejects(g) for g in out):
                return out
    return [poly]


def _tidy(poly, Polygon):
    """The near-misses Revit refuses, taken out before any cutting.

    A sliver hole (under 0.1 m2) is filled; a hairline strip of slab thinner than
    REVIT_GAP_MM - a hole 0.04 mm from the edge leaves one - is opened up, which
    turns that hole into a notch; corners closer than REVIT_TOL_MM are merged.
    """
    holes = [h for h in poly.interiors if Polygon(h).area >= SUNK_MIN_AREA_MM2]
    g = Polygon(poly.exterior, holes)
    r = REVIT_GAP_MM / 2.0
    g = g.buffer(-r, join_style=2).buffer(r, join_style=2)
    out = []
    for q in (getattr(g, "geoms", None) or [g]):
        if q.geom_type == "Polygon" and not q.is_empty and q.area > 1000.0:
            q = q.simplify(REVIT_TOL_MM)
            if q.geom_type == "Polygon" and q.is_valid and q.area > 1000.0:
                out.append(q)
    return out or [poly]


def _snap(g):
    """Onto a 1 mm grid, always as valid shapes: rotating strips back leaves
    corners a fraction of a millimetre apart, which Revit refuses."""
    import shapely
    g = shapely.set_precision(g, 1.0)
    return [q for q in (getattr(g, "geoms", None) or [g])
            if q.geom_type == "Polygon" and not q.is_empty and q.area > 1000.0]


def _slice(poly, rejects):
    """The last resort: strips cut at every corner, merged back where they can.

    Cut across the slab's own direction at each corner's height, every strip
    has corners only on its two cut edges, so it cannot touch itself or hold a
    hole - Revit always takes it. Neighbouring strips are then joined again
    wherever the joined shape still passes, so a slab is not left in slices.
    """
    import math
    from shapely import affinity
    from shapely.geometry import box
    c = list(poly.exterior.coords)
    a, b = max(zip(c, c[1:]), key=lambda e: G.dist(e[0], e[1]))
    ang = math.degrees(math.atan2(b[1] - a[1], b[0] - a[0]))
    o = poly.centroid
    rp = affinity.rotate(poly, -ang, origin=o)
    ys = sorted({round(y, 1) for ring in [rp.exterior] + list(rp.interiors)
                 for _x, y in ring.coords})
    x0, _y0, x1, _y1 = rp.bounds
    strips = []
    for lo, hi in zip(ys, ys[1:]):
        if hi - lo < REVIT_TOL_MM:
            continue
        cut = rp.intersection(box(x0 - 10, lo, x1 + 10, hi))
        for g in (getattr(cut, "geoms", None) or [cut]):
            if g.geom_type == "Polygon" and g.area > 1000.0:
                strips.append(g)
    merged = True
    while merged:
        merged = False
        for i in range(len(strips)):
            for j in range(i + 1, len(strips)):
                if strips[i].distance(strips[j]) > 0.01:
                    continue
                u = strips[i].union(strips[j])
                back = _snap(affinity.rotate(u, ang, origin=o))
                if u.geom_type == "Polygon" and len(back) == 1 and not rejects(back[0]):
                    strips[i] = u
                    del strips[j]
                    merged = True
                    break
            if merged:
                break
    out = []
    for g in strips:
        out += _snap(affinity.rotate(g, ang, origin=o))
    return out or [poly]


def _clean_for_revit(res: Result, Polygon) -> None:
    """Part any outline Revit would reject into clean pieces, same slab.

    Outlines Revit takes are not touched. One it would refuse is cut through the
    corner where it touches itself (see _split_at_pinches).
    """
    def rejects(g):
        tmp = SlabPanel(mark="", outline=_ring(g), source_layer="", handle="",
                        holes=[[(round(x, 1), round(y, 1)) for x, y in r.coords][:-1]
                               for r in g.interiors])
        return _revit_rejects(tmp, Polygon)

    kept, fixed, failed = [], 0, []
    for s in res.slabs:
        if not _revit_rejects(s, Polygon):
            kept.append(s)
            continue
        fixed += 1
        # Two parts meeting only at a corner (stilt plan S4, where two fold slots
        # meet) come out of buffer(0) as two shapes already; each is then checked.
        g = Polygon(s.outline, s.holes or None).buffer(0)
        pieces = []
        for part in (getattr(g, "geoms", None) or [g]):
            if part.geom_type != "Polygon" or part.area <= 1000.0:
                continue
            for q in _tidy(part, Polygon):
                for p in _split_at_pinches(q, rejects):
                    for p2 in _snap(p):
                        pieces += _slice(p2, rejects) if rejects(p2) else [p2]
        if any(rejects(p) for p in pieces):
            failed.append(s.mark)
        for piece in pieces:
            kept.append(SlabPanel(
                mark=s.mark, outline=_ring(piece), source_layer=s.source_layer,
                handle=s.handle, notes=list(s.notes), thickness=s.thickness,
                thickness_source=s.thickness_source, sunk_mm=s.sunk_mm,
                holes=[[(round(x, 1), round(y, 1)) for x, y in r.coords][:-1]
                       for r in piece.interiors]))
    res.slabs[:] = kept
    if fixed:
        res.add(INFO, "slabs",
                f"{fixed} slab outlines touched themselves where a fold turns a corner, "
                "which Revit refuses as a floor boundary. Each was cut through that corner "
                "into clean pieces with the same mark, thickness and level.")
    if failed:
        res.add(WARNING, "slabs",
                f"Slab outlines for {', '.join(sorted(set(failed)))} still touch themselves "
                "after cutting, and Revit may refuse them. Check them in the model.")


FOLD_DEFAULT_SLAB_MM = 150.0     # a slab with no scheduled thickness
FOLD_ASSUMED = "depth assumes a 150 mm lower slab: its thickness is not given"


def _size_folds(res: Result, Point, Polygon) -> None:
    """Give each fold its depth and top from the slabs on either side of it.

    The fold is the concrete that closes the step: it runs from the top of the
    higher slab down to the underside of the lower one. So its top is the higher
    slab's top, and its depth the step plus the lower slab's thickness - a 125
    slab stepping down 100 mm gives a 225 deep fold. Both sides are looked for a
    little way out from the fold's faces, at a quarter, half and three quarters
    along it. A fold with a slab on one side only, or none, is not sized: there
    is no step to read, and it is reported.
    """
    polys = [(Polygon(s.outline, s.holes or None).buffer(0), s) for s in res.slabs]

    def slab_at(pt):
        p = Point(pt)
        return next((s for poly, s in polys if poly.contains(p)), None)

    unsized = 0
    for f in res.folds:
        if not f.width:
            continue
        L = G.dist(f.start, f.end)
        if L < 1:
            continue
        ux, uy = (f.end[0] - f.start[0]) / L, (f.end[1] - f.start[1]) / L
        out = f.width / 2.0 + 150.0
        sides = []
        for sign in (1, -1):
            hit = None
            for t in (0.5, 0.25, 0.75):
                cx, cy = f.start[0] + ux * L * t, f.start[1] + uy * L * t
                hit = slab_at((cx - uy * out * sign, cy + ux * out * sign))
                if hit:
                    break
            sides.append(hit)
        if not all(sides):
            unsized += 1
            f.notes.append("no slab on one side: not sized")
            continue
        tops = [(-(s.sunk_mm or 0.0), s) for s in sides]
        hi, lo = max(tops, key=lambda t: t[0]), min(tops, key=lambda t: t[0])
        step = hi[0] - lo[0]
        f.top_mm = hi[0] + 0.0
        f.depth = round(step + (lo[1].thickness or FOLD_DEFAULT_SLAB_MM), 1)
        f.notes.append(f"between {sides[0].mark} and {sides[1].mark}, step {step:.0f} mm")
        if not lo[1].thickness:
            # said so, so the reviewed export (building.export) can hold it back
            f.notes.append(FOLD_ASSUMED)
    built = [f for f in res.folds if f.depth]
    if built:
        res.add(INFO, "slabs",
                f"{len(built)} slab folds (the paired red lines on the slab layer) are built "
                "as framing, each from the top of the higher slab to the underside of the "
                "lower one.")
    if unsized:
        res.add(WARNING, "slabs",
                f"{unsized} slab folds do not have a slab on both sides, so their depth "
                "cannot be read and they are not built. See the folds in model.json.")


def check_drawn(res: Result, open_fragments: int) -> None:
    """The warnings about the SLAB layer itself, given what was found or derived."""
    derived = any(s.source_layer == SOURCE for s in res.slabs)
    if open_fragments and not derived:
        res.add(BLOCKER, "slabs",
                f"{open_fragments} shapes on the slab layer are open or too short to "
                "be a panel. A Revit floor needs one closed outline per panel.")
    if not res.slabs and res.slab_tags:
        res.add(BLOCKER, "slabs",
                f"There are {len(res.slab_tags)} slab tags but no closed slab outlines, "
                "and no bay closed by beams holds any of them, so no floor can be built.")
