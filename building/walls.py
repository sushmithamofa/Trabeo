"""Walls and cores: what each plan wall is, before anything is modelled.

The plan reader hands over a wall as its outline. The Revit builder splits that
outline into straight runs when it builds (revit\\build_model.py, wall_runs). The
same function is used here - loaded from that file, not copied - so the review
shows exactly the runs Revit will build, and a wall that will not build is
known before anyone opens Revit.

For every wall:

* runs: [(start, end, thickness)], its thickness (the runs' common one), its
  length and its shape - straight, L, T, U, core (a closed box: a lift or stair
  shaft) or irregular;
* what it really is. Consultants put more than RC walls on a wall or column layer:
  - a compact outline, no side over 1200 mm (Sasan Gir's 450 x 750 L-shaped C7,
    the skewed columns of its angled wing) is a column drawn as an odd shape -
    it is flagged and joins the column marking (linker), but is left as it is;
  - an outline on a brick / masonry / block layer is masonry, not structure;
* a wall with no runs (a curved or odd outline) will not be built: reported.
"""
from __future__ import annotations

import ast
import math
import os
import re

from .model import Building, Evidence, Issue, Value, INFO, READ, WARNING

READER = "walls"
COLUMN_LIKE = 1200.0            # mm: a squat outline no bigger than this both ways is a column
# BK_WALL / BRK: the short forms consultants use for brick walls (Bk_wall_ST)
MASONRY = re.compile(r"BRICK|MASONRY|BLOCK\s*WORK|BLOCKWORK|\bAAC\b|PARTITION|"
                     r"(?<![A-Z])BK[\s_.-]*WALL|(?<![A-Z])BRK(?![A-Z])", re.I)
_RUNS = None


def wall_runs(outline):
    """The Revit builder's own wall_runs, loaded from revit\\build_model.py."""
    global _RUNS
    if _RUNS is None:
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "revit", "build_model.py")
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "wall_runs")
        ns: dict = {}
        exec(compile(ast.Module([fn], []), path, "exec"), ns)
        _RUNS = ns["wall_runs"]
    return _RUNS(outline)


def _box(outline):
    xs, ys = [p[0] for p in outline], [p[1] for p in outline]
    return min(xs), min(ys), max(xs), max(ys)


def shape_of(runs, box) -> str:
    """straight / L / T / U / core / irregular, from how the runs meet."""
    if not runs:
        return "irregular"
    if len(runs) == 1:
        return "straight"
    # each run's direction relative to the first: 0 = parallel, 1 = square to it;
    # walls of an angled wing are fine, a run at any other angle is not
    a0 = math.atan2(runs[0][1][1] - runs[0][0][1], runs[0][1][0] - runs[0][0][0])
    axis = []
    for a, b, _t in runs:
        rel = math.degrees(math.atan2(b[1] - a[1], b[0] - a[0]) - a0) % 180.0
        axis.append(0 if min(rel, 180.0 - rel) <= 3 else 1 if abs(rel - 90.0) <= 3 else None)
    if None in axis:
        return "irregular"
    if len(runs) == 2:
        return "L" if axis[0] != axis[1] else "irregular"
    if len(runs) == 3:
        # U: both parallel runs end on the third; T: the stem stops on the middle of the bar
        if axis.count(0) == 3 or axis.count(1) == 3:
            return "irregular"
        lone = 0 if axis.count(0) == 1 else 1
        o = next(r for r, x in zip(runs, axis) if x == lone)
        par = [r for r, x in zip(runs, axis) if x != lone]
        reach = 1.5 * o[2]

        def end_at(r, q):
            return min(math.dist(r[0], q), math.dist(r[1], q)) <= reach
        # U: the two side runs stand at the two different ends of the third
        if (end_at(par[0], o[0]) and end_at(par[1], o[1])) or (end_at(par[0], o[1]) and end_at(par[1], o[0])):
            return "U"
        return "T"
    w, h = box[2] - box[0], box[3] - box[1]
    if axis.count(0) >= 2 and axis.count(1) >= 2 and min(w, h) > 2 * max(r[2] for r in runs):
        return "core"
    return "irregular"


def analyse(b: Building, stats: dict, walls=None) -> None:
    """Runs, thickness, shape and role of every plan wall (or of the walls given),
    kept in extra and resolved."""
    for e in (b.of("wall") if walls is None else walls):
        e.extra.pop("column_like", None)
        start, end = e.geometry.get("start"), e.geometry.get("end")
        outline = e.geometry.get("outline") or []
        if start and end:
            runs = [(list(start), list(end), e.get("thickness") or 230.0)]
            source = "start and end as read"
        elif len(outline) >= 3:
            runs = [(list(a), list(c), float(t)) for a, c, t in wall_runs(outline)]
            source = "outline split into straight runs (as the Revit build does)"
        else:
            runs, source = [], ""
        ev = Evidence(reader=READER, sheet=e.sheet, source=source or "wall outline", layer=e.extra.get("_layer", ""),
                      handle=e.extra.get("_handle", ""), text=f"{len(runs)} runs")
        box = _box(outline) if len(outline) >= 3 else None
        e.extra["runs"] = [{"start": [round(a[0], 1), round(a[1], 1)], "end": [round(c[0], 1), round(c[1], 1)],
                            "thickness": round(t, 1), "length": round(math.dist(a, c), 1)} for a, c, t in runs]
        layer = e.extra.get("_layer", "") or ""
        bw, bd = (box[2] - box[0], box[3] - box[1]) if box else (0.0, 0.0)
        if MASONRY.search(layer):
            # a short piece of brick wall is still brick: ALCAZAR 1st floor's 100 mm
            # partitions beside doors were read as columns and built as structure
            role, shape = "masonry", shape_of(runs, box) if box else "irregular"
            stats["masonry walls"] += 1
            b.issues.append(Issue(INFO, "wall", f"wall on layer {layer} is masonry, not part of the RC structure"
                                  + ("" if runs else " (and its outline does not split into straight runs)"),
                                  sheet=e.sheet, elements=[e.id], layer=layer, extra={"from_linker": True}))
        elif box and not e.extra.get("was_column") and max(bw, bd) <= COLUMN_LIKE \
                and max(bw, bd) <= 3.0 * max(min(bw, bd), 1.0):
            # squat and small: a column (a 230 x 1025 pier is long and thin - a wall). Not
            # one the linker made a wall by its wall mark, on its true size: on a plan
            # drawn at an angle its square box looks squat (300 x 1000 at 28 deg: 734 x 1024)
            role, shape = "column", "compact"
            e.extra["column_like"] = {"x": (box[0] + box[2]) / 2, "y": (box[1] + box[3]) / 2,
                                      "width": round(min(box[2] - box[0], box[3] - box[1])),
                                      "depth": round(max(box[2] - box[0], box[3] - box[1]))}
            stats["walls that are columns drawn as odd shapes"] += 1
            b.issues.append(Issue(WARNING, "wall", f"a {box[2] - box[0]:.0f} x {box[3] - box[1]:.0f} outline with "
                                  f"{len(outline)} corners on layer {layer} is read as a wall, but it is the size of a "
                                  f"column (an L-shaped or skewed column?). Revit would build it as {len(runs)} short "
                                  f"wall piece(s) - check it", sheet=e.sheet, elements=[e.id], layer=layer,
                                  extra={"from_linker": True}))
        else:
            role = "RC wall"
            shape = shape_of(runs, box) if box else ("straight" if runs else "irregular")
            stats[f"walls: {shape}"] += 1
            if not runs:
                b.issues.append(Issue(WARNING, "wall", f"wall outline with {len(outline)} corners on layer {layer} does "
                                      f"not split into straight runs: it will not be built", sheet=e.sheet,
                                      elements=[e.id], layer=layer, extra={"from_linker": True}))
            elif box and max(t for _, _, t in runs) > min(bw, bd) + 20.0:
                # a run thicker than the outline is wide: its faces were paired wrongly
                # (Sasan Gir's 230 x 1025 pier split into two 457-thick pieces)
                b.issues.append(Issue(WARNING, "wall", f"the {bw:.0f} x {bd:.0f} wall outline on layer {layer} splits "
                                      f"badly: Revit would build {len(runs)} piece(s) up to "
                                      f"{max(t for _, _, t in runs):.0f} thick - check its outline", sheet=e.sheet,
                                      elements=[e.id], layer=layer, extra={"from_linker": True}))
                stats["walls that split badly"] += 1
        e.resolved["role"] = Value(role, READ, 0.8, [ev])
        e.resolved["shape"] = Value(shape, READ, 0.8, [ev])
        if runs:
            ts = sorted(round(t) for _, _, t in runs)
            e.resolved["runs"] = Value(len(runs), READ, 0.9, [ev])
            e.resolved["length"] = Value(round(sum(math.dist(a, c) for a, c, _ in runs)), READ, 0.8, [ev])
            if not e.get("thickness"):
                e.extra["thickness_from_runs"] = ts[len(ts) // 2]
