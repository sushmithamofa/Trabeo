"""Same size, same colour: the Revit build's view filters, for the IFC and Rhino files.

The Revit build (revit\\build_model.py, build_extras.py) gives each size its own
colour from BEAM_COLOURS: the beams and slab folds first, in palette order, then the
columns, the sunk slabs and the footings, each taking the next colour no other filter
uses yet. The exports colour their elements by the same labels in the same order,
so one floor's file reads like the Revit model:

    "Beam 230x450"  "Fold 230x300"  "Column 300x600"  "Sunk slab 150 mm"
    "Footing 1800x2100x600"  "Footing step 1200x1500x300"

    plan(labels) -> {label: (r, g, b)}
"""
from __future__ import annotations

import ast
import io
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RED = (255, 0, 0)                           # as revit\build_model.py PROVISIONAL_RGB


def palette() -> list[tuple[int, int, int]]:
    """BEAM_COLOURS from revit\\build_model.py, read from that file - not copied."""
    path = os.path.join(ROOT, "revit", "build_model.py")
    with io.open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    for n in tree.body:
        if isinstance(n, ast.Assign) and any(getattr(t, "id", "") == "BEAM_COLOURS" for t in n.targets):
            return [tuple(c) for c in ast.literal_eval(n.value)]
    return [(0, 130, 200)]


def column_label(width, depth) -> str:
    """A column's size whichever way it faces: 600x300 is the 300x600 column."""
    a, b = sorted((float(width), float(depth)))
    return "Column %dx%d" % (round(a), round(b))


def sunk_label(depth_mm) -> str:
    return "Sunk slab %d mm" % round(float(depth_mm))


def footing_label(kind, size_x, size_y, thickness) -> str:
    """As build_extras.type_name: the Revit footing type, and its filter, is this name."""
    a, b = sorted((float(size_x), float(size_y)))
    return "%s %dx%dx%d" % ("Footing" if kind == "footing" else "Footing step", round(a), round(b), round(thickness))


def _nums(label):
    tail = label.split(" mm")[0].rsplit(" ", 1)[-1]
    try:
        return tuple(int(v) for v in tail.split("x"))
    except ValueError:
        return (0,)


def _order(label):
    """Revit's order: beams, folds, columns, sunk slabs, footings, steps; by size within."""
    groups = ("Beam ", "Fold ", "Column ", "Sunk slab ", "Footing step ", "Footing ")
    g = next((i for i, p in enumerate(groups) if label.startswith(p)), len(groups))
    g = {4: 5, 5: 4}.get(g, g)               # footings before their steps
    n = _nums(label)
    if g < 2:
        n = tuple(reversed(n))               # a beam by depth, then width, as the Revit filters
    return (g, n, label)


def _group(label):
    """The Revit filter call a label belongs to: footings and their steps are one."""
    for p in ("Beam ", "Fold ", "Column ", "Sunk slab ", "Footing"):
        if label.startswith(p):
            return "Beam " if p == "Fold " else p
    return label


def plan(labels) -> dict:
    """{label: colour}: beams and folds take the palette in order, every other group
    the next colour nobody has yet; once those run out, as the Revit build does, one
    another group uses but not yet its own group."""
    pal = palette()
    out = {}
    used = set()
    for lb in sorted(set(labels), key=_order):
        if lb.startswith(("Beam ", "Fold ")):
            c = pal[sum(1 for k in out if k.startswith(("Beam ", "Fold "))) % len(pal)]
        else:
            mine = set(c for k, c in out.items() if _group(k) == _group(lb))
            free = [p for p in pal if p not in used and p not in mine] + \
                [p for p in pal if p in used and p not in mine]
            c = free[0] if free else pal[len(mine) % len(pal)]
        out[lb] = c
        used.add(c)
    return out
