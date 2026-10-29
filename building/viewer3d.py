r"""A 3D view of the whole building, in the browser (model3d.html).

    python -m building.viewer3d RUN_FOLDER

Columns, beams, slabs, walls, footings and stairs of every plan, stacked floor
over floor, coloured by status as on the review page (read / low / conflict /
missing / decided). Orbit, zoom, pan; hover for an element's mark, size and
status; layers on and off; one drawing set at a time.

How it is put together - the same rules the Revit build follows:
  * each floor's plan is moved onto the lowest plan of its set: by the column
    layout's shifts where both have one, else by laying its columns on the
    lowest plan's columns (the linker's _shift); a plan that lines up with
    neither is shown where it is drawn, and the page says so;
  * a level's height is its elevation from the level table where the drawings
    give one, else storeys are assumed 3000 mm apart in level order - and the
    page says which;
  * columns and walls rise from the level below to their plan's level, beams
    and slabs hang below it, footings sit at their own founding level; one with
    none read or decided hangs from the lowest level, marked missing.
The assumed storeys are for this preview alone: the Revit build stops for a level
with no elevation rather than assume one.
Two plans of one level (a drawing sent twice) are shown once.

The 3D library (three.js) is loaded from cdnjs, so the page needs the internet
the first time.
"""
from __future__ import annotations

import json
import math
import os
import sys
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from building import Building                               # noqa: E402
from building.sheets.levels import level_rank               # noqa: E402

STOREY_MM = 3000.0       # the preview only, and said on the page: the Revit build never assumes it
SAME_PLACE = 50.0        # mm, as revit\build_model.py: nearer counts as the same column
FC_CARRY_ON = False      # as revit\build_model.py: every plan's FC stands on its own floor (the user's rule)
LOW = 0.6


def _status(e, props) -> str:
    worst = "read"
    rank = {"read": 0, "override": 1, "low": 2, "missing": 3, "conflict": 4}
    for p in props:
        v = e.resolved.get(p)
        k = "missing" if v is None else ("low" if v.status == "read" and v.confidence < LOW else v.status)
        k = k if k in rank else "read"
        if rank[k] > rank[worst]:
            worst = k
    return worst


def _num(e, p, default=None):
    v = e.resolved.get(p)
    if v is not None and v.status in ("read", "override") and isinstance(v.value, (int, float)):
        return float(v.value)
    x = e.get(p)
    return float(x) if isinstance(x, (int, float)) else default


def _footprints(items) -> list:
    """The columns' and walls' footprints among one floor's items, as shapely shapes."""
    from shapely.geometry import LineString, Polygon
    out = []
    for it in items:
        if it["t"] == "column":
            r = math.radians(float(it.get("rot") or 0.0))
            ex, ey, w, d_ = math.cos(r), math.sin(r), it["w"], it["d"]
            out.append(Polygon([(it["x"] + u * ex - v * ey, it["y"] + u * ey + v * ex)
                                for u, v in ((-d_ / 2, -w / 2), (d_ / 2, -w / 2), (d_ / 2, w / 2), (-d_ / 2, w / 2))]))
        elif it["t"] == "wall" and it.get("sec"):
            # a shaped wall: a -> b across it, its profile along it (u); its beams meet its top, so
            # its stretch there (a leaning wall is wider over its storey than at its top)
            (ax, ay), (bx, by) = it["a"], it["b"]
            t = math.hypot(bx - ax, by - ay) or 1.0
            fx, fy = (bx - ax) / t, (by - ay) / t
            u0, u1 = min(p[0] for p in it["sec"]), max(p[0] for p in it["sec"])
            try:
                poly = Polygon(it["sec"]).buffer(0)
                y = poly.bounds[3] - 50.0
                cut = poly.intersection(LineString([(u0 - 1.0, y), (u1 + 1.0, y)]))
                if not cut.is_empty and cut.bounds[2] - cut.bounds[0] > 1.0:
                    u0, u1 = cut.bounds[0], cut.bounds[2]
            except Exception:
                pass
            out.append(Polygon([(ax - fy * u0, ay + fx * u0), (ax - fy * u1, ay + fx * u1),
                                (bx - fy * u1, by + fx * u1), (bx - fy * u0, by + fx * u0)]))
        elif it["t"] == "wall":
            out.append(LineString([it["a"], it["b"]]).buffer(it["w"] / 2, cap_style=2))
    return out


def _off(e, p) -> float:
    """A column's or wall's base / top off its levels (linker._wall_heights), else 0."""
    v = e.resolved.get(p)
    return float(v.value) if v is not None and v.status in ("read", "override") and v.value else 0.0


def _lean(item, ln) -> None:
    """A column or wall leaning as a wall elevation draws it (linker._section_lean): its bottom
    and top moved across by ln's base / top (mm), drawn sheared between them."""
    item["ln"] = [ln["base"][0], ln["base"][1], ln["top"][0], ln["top"][1]]
    item["info"] += f", leaning as {ln['section']}"


def _plan_shifts(b, plans):
    """Offset of each plan onto the first (lowest) one: {sid: (dx, dy) or None}."""
    from building.linker import _shift
    ref_sid, ref = plans[0]
    out = {ref_sid: (0.0, 0.0)}

    def cols_of(sid):
        return [(e.geometry["x"], e.geometry["y"]) for e in b.elements
                if e.sheet == sid and e.type == "column" and e.geometry.get("x") is not None]
    ref_cols = cols_of(ref_sid)
    placed = [ref_sid]
    for sid, s in plans[1:]:
        la, lb = s.extra.get("column_layout_shift"), ref.extra.get("column_layout_shift")
        if la and lb:
            out[sid] = (la[0] - lb[0], la[1] - lb[1])
            placed.append(sid)
            continue
        cols = cols_of(sid)
        got = _shift(cols, ref_cols) if cols and ref_cols else None
        out[sid] = got[0] if got else None
        if out[sid] is not None:
            placed.append(sid)
    # floors that do not line up with the lowest plan are stacked on any floor already
    # placed that they do line up with, the nearest below first
    left = [sid for sid, _ in plans if out.get(sid) is None]
    if left and placed == [ref_sid] and len(plans) > 2:
        # the lowest plan lines up with no other floor (NIFT level 1: a strip of two rows
        # of columns, fitting the floor above in two places) - the floors that do line up
        # with each other are shown, stacked on the lowest of them; the lowest plan is not
        others = [sid for sid, _ in plans[1:] if cols_of(sid)]
        frame = next((a for a in others if any(_shift(cols_of(a), cols_of(o)) for o in others if o != a)), None)
        if frame is not None:
            out = {sid: None for sid, _ in plans}
            out[frame] = (0.0, 0.0)
            placed = [frame]
            left = [sid for sid in others if sid != frame]
    changed = True
    while left and changed:
        changed = False
        for sid in list(left):
            cols = cols_of(sid)
            for other in reversed(placed):
                g = _shift(cols, cols_of(other)) if cols else None
                if g:
                    out[sid] = (g[0][0] + out[other][0], g[0][1] + out[other][1])
                    placed.append(sid)
                    left.remove(sid)
                    changed = True
                    break
    # floors still not placed: by their grid, the way engineers line floors up - the same
    # named grid lines (1, 2 ... A, B ...) on every floor. Pirgal draws each floor at its own
    # place in one model space with only 5 columns, which fit in several places
    sheets = dict(plans)
    left = [sid for sid, _ in plans if out.get(sid) is None]
    changed = True
    while left and changed and placed:
        changed = False
        for sid in list(left):
            for other in reversed(placed):
                g = _grid_shift(sheets[sid], sheets[other])
                if g:
                    out[sid] = (g[0] + out[other][0], g[1] + out[other][1])
                    placed.append(sid)
                    left.remove(sid)
                    changed = True
                    break
    return out


_GRIDS: dict = {}


def _grid_labels_of(s) -> list:
    """The grid labels (1, 2, A, B, 2' ...) drawn on a plan's own drawing, read once."""
    import os
    path = s.file or ""
    if not path or not os.path.exists(path) or not path.lower().endswith(".dxf"):
        return []
    key = (path, os.path.getmtime(path))
    if key not in _GRIDS:
        try:
            _GRIDS[key] = _bubbled_labels(path)
        except Exception:
            _GRIDS[key] = []
    return _GRIDS[key]


def _bubbled_labels(path: str) -> list:
    """Grid labels standing in their bubbles: a short label inside a circle 150-1500 mm
    across the radius, drawn loose or as a bubble block's attribute. Section callouts and
    detail numbers ("3", "A" all over a plan) have no bubble of their own."""
    import ezdxf
    from building.sheets.views import _GRID_TEXT
    from building.sheets.source import clean
    msp = ezdxf.readfile(path).modelspace()
    circles, out = [], []
    for e in msp:
        if e.dxftype() == "CIRCLE" and 150.0 <= float(e.dxf.radius) <= 1500.0:
            circles.append((e.dxf.center[0], e.dxf.center[1], float(e.dxf.radius)))
    for e in msp:
        t = e.dxftype()
        try:
            if t in ("TEXT", "MTEXT"):
                s = clean(e.plain_text() if t == "MTEXT" else e.dxf.text).upper().strip()
                p = e.dxf.align_point if t == "TEXT" and (e.dxf.get("halign", 0) or e.dxf.get("valign", 0)) \
                    and e.dxf.hasattr("align_point") else e.dxf.insert
                if _GRID_TEXT.match(s) and any(abs(p[0] - cx) <= 1.3 * r and abs(p[1] - cy) <= 1.3 * r
                                               for cx, cy, r in circles):
                    out.append((s, p[0], p[1]))
            elif t == "INSERT" and e.attribs:
                blk = e.doc.blocks.get(e.dxf.name)
                if blk is None or not any(x.dxftype() == "CIRCLE" for x in blk):
                    continue
                ip = e.dxf.insert
                for a in e.attribs:
                    s = clean(a.dxf.text or "").upper().strip()
                    if _GRID_TEXT.match(s):
                        out.append((s, ip[0], ip[1]))     # the bubble's place, where its line ends
        except Exception:
            continue
    return out


def _grid_shift(s, ref):
    """(dx, dy) moving plan s onto plan ref by their grid labels, or None.

    A grid's numbered lines and its lettered lines run across each other: the labels of one
    kind stand spread along one direction and fix the shift that way only (Pirgal's number
    bubbles sit 400 mm higher on one floor - their up-down shift means nothing). So each
    direction is voted on by the labels spread along it (100 mm steps): at least 3 labels,
    clearly ahead of any other shift."""
    import re as _re
    one = _re.compile(r"^(?:[A-Z]|\d{1,2})'?$")      # a grid line's name: one letter, or a number
    a, b = _grid_labels_of(s), _grid_labels_of(ref)
    common = {t for t, _x, _y in a if one.match(t)} & {t for t, _x, _y in b}
    a = [p for p in a if p[0] in common]
    b = [p for p in b if p[0] in common]
    if len(a) < 4 or len(b) < 4:
        return None
    from collections import defaultdict
    by_b = defaultdict(list)
    for t, x, y in b:
        by_b[t].append((x, y))

    def row_of(kind):
        """The grid's own labels of one kind: the most different labels standing in one line
        (one x, or one y, within 400 mm) - a bubbled detail callout elsewhere is not one of
        them. Returns (axis they fix: 0 = x for a row along x, 1 = y, labels) or None."""
        pts = [(t, x, y) for t, x, y in a if t[0].isdigit() == kind]
        best = None
        for t0, x0, y0 in pts:
            for ax, c in ((0, y0), (1, x0)):       # a row at one y fixes x; a column at one x fixes y
                line = [p for p in pts if abs((p[2] if ax == 0 else p[1]) - c) <= 400.0]
                n = len({p[0] for p in line})
                if best is None or n > best[0]:
                    best = (n, ax, line)
        if best is None or best[0] < 3:
            return None
        return best[1], best[2]

    out = [None, None]
    for kind in (True, False):                   # numbers, letters
        got = row_of(kind)
        if got is None:
            continue
        ax, line = got
        votes, vals = defaultdict(set), defaultdict(list)
        for t, x, y in line:
            for qx, qy in by_b.get(t, []):
                d = (qx - x, qy - y)[ax]
                votes[round(d / 100.0)].add(t)
                vals[round(d / 100.0)].append(d)
        if not votes:
            continue
        ranked = sorted(votes, key=lambda k: -len(votes[k]))
        best = ranked[0]
        if len(votes[best]) < 3:
            continue
        rival = next((k for k in ranked[1:] if abs(k - best) > 2), None)
        if rival is not None and len(votes[rival]) * 1.5 > len(votes[best]):
            continue
        v = sorted(vals[best])
        out[ax] = v[len(v) // 2]
    if out[0] is None or out[1] is None:
        return None
    return out[0], out[1]


def scene(b: Building, set_key: str, decisions: dict | None = None) -> dict:
    """Everything to draw for one drawing set, in mm."""
    decided = {str(k): float(v) for k, v in ((decisions or {}).get("levels") or {}).items()
               if isinstance(v, (int, float))}
    plans = [(sid, s) for sid, s in b.sheets.items() if s.reader == "plan.legacy" and s.extra.get("set") == set_key
             and level_rank(s.extra.get("level_key") or "") is not None]
    if not plans:
        return {}
    plans.sort(key=lambda p: (level_rank(p[1].extra["level_key"]), p[0]))
    seen, uniq = set(), []
    for sid, s in plans:
        if s.extra["level_key"] not in seen:          # a level drawn twice: shown once
            seen.add(s.extra["level_key"])
            uniq.append((sid, s))
    plans = uniq
    shifts, fd = stack(b, set_key, plans)
    # level heights: the level table where it gives one, else 3 m storeys in order
    table = {e.level: e.final("elevation") for e in b.of("level") if e.extra.get("set") == set_key
             and e.resolved.get("elevation") and e.resolved["elevation"].status in ("read", "override")}
    return _scene(b, set_key, plans, shifts, fd, table, decided, decisions)


def stack(b: Building, set_key: str, plans) -> tuple[dict, dict | None]:
    """How each plan of a set is moved to stand on the others: ({sid: (dx, dy) or None},
    the footings in the frame of a plan that is placed). The 3D view and the export to
    Revit / IFC / Rhino use this one answer, so they always agree."""
    shifts = _plan_shifts(b, plans)
    # footings, from the export's own placement rules
    try:
        from building.export import foundations_for
        fd = foundations_for(b, set_key, unfounded=True)
    except Exception:
        fd = None
    if fd and shifts.get(fd["plan"]) is None:
        # the footings line up with a plan this view does not show: drawn under a floor
        # that is shown instead
        for psid, _ps in plans:
            if shifts.get(psid) is None:
                continue
            try:
                f2 = foundations_for(b, set_key, unfounded=True, plan_sid=psid)
            except Exception:
                f2 = None
            if f2 and f2.get("sheet_shifts"):
                fd = f2
                break
    if fd and fd.get("sheet_shifts") and shifts.get(fd["plan"]) is not None:
        # a plan not lined up with the other floors (NIFT level 1: a strip of two rows of
        # columns, fitting level 2 in two places) placed through the foundation sheet, whose
        # full grid it fits in one place only
        for psid, _ps in plans:
            if shifts.get(psid) is not None:
                continue
            try:
                f1 = foundations_for(b, set_key, unfounded=True, plan_sid=psid)
            except Exception:
                f1 = None
            common = [fs for fs in (f1 or {}).get("sheet_shifts", {}) if fs in fd["sheet_shifts"]]
            if f1 and f1["plan"] == psid and common:
                a, c = f1["sheet_shifts"][common[0]], fd["sheet_shifts"][common[0]]
                ref = shifts[fd["plan"]]
                shifts[psid] = (c[0] - a[0] + ref[0], c[1] - a[1] + ref[1])
    return shifts, fd


def _scene(b, set_key, plans, shifts, fd, table, decided, decisions) -> dict:
    keys = [s.extra["level_key"] for _, s in plans]
    revit_name = {s.extra["level_key"]: s.level for _, s in plans}
    z, assumed = {}, []
    numbered = [int(level_rank(k)) for k in keys if 1 <= level_rank(k) < 100]
    for k in keys:
        if revit_name.get(k) in decided:               # decided on the Export page: as the build
            z[k] = decided[revit_name[k]]
            continue
        if k in table:
            z[k] = float(table[k])
            continue
        r = level_rank(k)
        if r >= 100:                                   # terrace / roof: a storey above the top floor
            z[k] = ((max(numbered) if numbered else 0) + 1 + (r - 100)) * STOREY_MM
        elif r >= 1:
            z[k] = r * STOREY_MM                       # L3 is 9 m up even when no L2 plan was sent
        else:
            z[k] = min(r, 0.0) * STOREY_MM if r > -1 else -STOREY_MM
        assumed.append(k)
    # an assumed height never below the floor under it (Pirgal: L2 read at +10.35 m, L3 given
    # none - 3 m storeys counted from the ground put it at 9 m, under L2)
    # (numbered floors only: their order is certain; a stilt or podium can be above or below
    # the ground floor depending on the site)
    for i, k in enumerate(keys):
        if k in assumed and i > 0 and 1 <= level_rank(k) < 100 and z[k] <= z[keys[i - 1]]:
            z[k] = z[keys[i - 1]] + STOREY_MM
    below, above = {}, {}
    # the level the lowest floor's columns rise from: a decided one below it (Level 0), as the build
    base = max((v for n, v in decided.items() if keys and v < z[keys[0]] and n not in revit_name.values()),
               default=None)
    for i, k in enumerate(keys):
        below[k] = z[keys[i - 1]] if i > 0 else (base if base is not None else z[k] - STOREY_MM)
        above[k] = z[keys[i + 1]] if i + 1 < len(keys) else z[k] + STOREY_MM
    items, notes = [], []
    rising = []                    # floating columns of the floor below, rising to this floor: (x, y)
    for sid, s in plans:
        sh = shifts.get(sid)
        if sh is None:
            # drawn at its own origin it would float far off and hide the rest
            notes.append(f"{s.extra['level_key']}: not shown - its columns do not line up with the other floors' "
                         f"(none in common, or they fit in more than one place)")
            continue
        k = s.extra["level_key"]
        top, low = z[k], below[k]
        below_fc, rising = rising, []
        from building.export import left_out
        n_sheet = len(items)
        for e in b.elements:
            if e.sheet != sid or e.extra.get("split_into") or e.extra.get("merged_into"):
                continue
            if left_out(e):
                continue                    # not built (another block's, a sample beside the plan)
            g = e.geometry
            mark = str(e.final("mark") or e.mark or "")
            if e.type == "column" and g.get("x") is not None:
                w, d = _num(e, "width", 230.0), _num(e, "depth", 450.0)
                # a floating column (FC) stands on its girder at this level and rises to the
                # next, as the Revit build places it - not from the level below
                fl = e.final("kind") == "column_float" or mark.upper().startswith("FC")
                x, y = g["x"] + sh[0], g["y"] + sh[1]
                if FC_CARRY_ON and fl and any(abs(x - bx) + abs(y - by) <= SAME_PLACE for bx, by in below_fc):
                    # the floor below's FC carrying on, already standing up to this floor: one
                    # column, as the Revit build has it (an FC with a column under it continues)
                    continue
                if fl:
                    rising.append((x, y))
                tv = e.resolved.get("top_mm")             # ends under a lowered beam (building section)
                ctop = top + (float(tv.value) if tv is not None and tv.value is not None
                              and tv.status in ("read", "override") else 0.0)
                items.append({"id": e.id, "t": "column", "x": x, "y": y, "w": w, "d": d,
                              "rot": float(g.get("rotation") or 0.0),
                              "z0": top if fl else low + _off(e, "base_mm"), "z1": above[k] if fl else ctop,
                              "s": _status(e, ("mark", "width", "depth")), "m": mark,
                              "info": f"{'floating column, ' if fl else ''}{w:.0f} x {d:.0f}", "lv": k})
                if e.extra.get("diameter") and abs(w - d) <= 1.0:
                    # drawn as a circle: shown round, as the Revit build makes it
                    items[-1]["rd"] = w
                    items[-1]["info"] = f"{'floating column, ' if fl else ''}round, {w:.0f} across"
                if e.extra.get("stops_here"):
                    items[-1]["info"] += ", ends at this floor (column-stop layer)"
                if e.extra.get("lean") and not fl:
                    _lean(items[-1], e.extra["lean"])
            elif e.type == "beam" and e.extra.get("slab_strip") and g.get("start"):
                # the slab a section draws beside its beam, drawn as lines read as a beam: the
                # slab strip it is, as the export builds it (export._strip_slab)
                from building.export import strip_outline
                strip, p = e.extra["slab_strip"], strip_outline(e)
                if p:
                    items.append({"id": e.id, "t": "slab", "p": [[x + sh[0], y + sh[1]] for x, y in p], "z": top,
                                  "th": float(strip["thickness"]), "s": "read" if strip.get("written") else "low",
                                  "m": "", "info": f"slab strip of section {strip['section']}, {strip['width']:.0f} "
                                                   f"wide, {strip['thickness']:.0f} thick", "lv": k})
            elif e.type == "beam" and (e.extra.get("same_beam_as") or e.extra.get("not_a_beam")):
                pass                                # drawn twice, or part of a column: not built, as the export
            elif e.type == "beam" and e.extra.get("section_parts") and g.get("start") and g.get("end"):
                # a pair of lines that is the beam and the slab beside it (linker._section_faces)
                from building.export import beam_line, strip_outline
                sp = e.extra["section_parts"]
                for part in sp["strips"]:
                    p = strip_outline(e, part)
                    if p:
                        items.append({"id": e.id, "t": "slab", "p": [[x + sh[0], y + sh[1]] for x, y in p],
                                      "z": top, "th": float(part["thickness"]),
                                      "s": "read" if part.get("written") else "low", "m": "",
                                      "info": f"slab strip of section {sp['section']}, {part['width']:.0f} wide, "
                                              f"{part['thickness']:.0f} thick", "lv": k})
                line = beam_line(e)
                if line:
                    w, d = _num(e, "width", 230.0), _num(e, "depth", 450.0)
                    items.append({"id": e.id, "t": "beam", "a": [line[0][0] + sh[0], line[0][1] + sh[1]],
                                  "b": [line[1][0] + sh[0], line[1][1] + sh[1]], "w": w, "d": d, "z": top,
                                  "s": _status(e, ("mark", "width", "depth")), "m": mark,
                                  "info": f"{w:.0f} x {d:.0f}, as section {sp['section']}", "lv": k})
            elif e.type in ("beam", "beam_span", "fold") and g.get("start") and g.get("end"):
                hidden = e.type == "fold"           # the concealed / hidden beam at a sunk step
                w, d = _num(e, "width", 230.0), _num(e, "depth", 450.0)
                items.append({"id": e.id, "t": "beam", "a": [g["start"][0] + sh[0], g["start"][1] + sh[1]],
                              "b": [g["end"][0] + sh[0], g["end"][1] + sh[1]], "w": w, "d": d,
                              "z": top - (_num(e, "top_mm", 0.0) if hidden else 0.0),
                              "s": _status(e, ("width", "depth") if hidden else ("mark", "width", "depth")),
                              "m": mark or ("hidden beam" if hidden else ""),
                              "info": f"{'hidden beam ' if hidden else ''}{w:.0f} x {d:.0f}", "lv": k})
                from building.linker import section_profile
                sec = section_profile(e) if not hidden else None
                if sec and not g.get("mid"):
                    items[-1]["sec"] = sec        # built to its drawn section, not as a box
                if g.get("mid"):
                    # a curved beam: its band along the arc, drawn as one curved solid
                    from building.ifc_export import arc_band
                    items[-1]["p"] = [[round(x, 1), round(y, 1)] for x, y in arc_band(
                        items[-1]["a"], (g["mid"][0] + sh[0], g["mid"][1] + sh[1]), items[-1]["b"], w)]
                    items[-1]["mid"] = [g["mid"][0] + sh[0], g["mid"][1] + sh[1]]
                # its height from a building section: off the level, or sloping in pieces
                tv = e.resolved.get("top_mm") if not hidden else None
                if tv is not None and tv.value is not None and tv.status in ("read", "override"):
                    items[-1]["z"] = top + float(tv.value)
                prof = e.extra.get("slope_profile") if not hidden else None
                if prof and not g.get("mid") and not items[-1].get("sec"):
                    it0 = items.pop()
                    (ax, ay), (bx, by) = it0["a"], it0["b"]
                    for (t0, z0), (t1, z1) in zip(prof, prof[1:]):
                        items.append(dict(it0, a=[ax + (bx - ax) * t0, ay + (by - ay) * t0],
                                          b=[ax + (bx - ax) * t1, ay + (by - ay) * t1], z=top + z0, z2=top + z1,
                                          info=it0["info"] + (" sloping" if z0 != z1 else "")))
            elif e.type == "slab" and len(g.get("outline") or []) >= 3:
                th = _num(e, "thickness", 150.0)
                items.append({"id": e.id, "t": "slab", "p": [[p[0] + sh[0], p[1] + sh[1]] for p in g["outline"]], "z": top,
                              "th": th, "s": _status(e, ("thickness",)), "m": mark, "info": f"{th:.0f} thick",
                              "lv": k})
                sl = e.extra.get("slope")
                if sl:                             # sloping (building section): top through both beams
                    items[-1]["sl"] = {"o": [sl["origin"][0] + sh[0], sl["origin"][1] + sh[1]], "d": sl["dir"],
                                       "p0": sl["p0"], "z0": sl["z0"], "p1": sl["p1"], "z1": sl["z1"]}
                    items[-1]["info"] += f", sloping {sl['z0']:+.0f} to {sl['z1']:+.0f}"
            elif e.type == "wall" and e.extra.get("profile_wall") and e.final("role") != "masonry":
                # the shape a wall elevation draws (linker._profile_walls), carried across its thickness
                pw = e.extra["profile_wall"]
                (ox, oy), (ux, uy), t = pw["origin"], pw["dir"], pw["thickness"]
                nx, ny = uy * t / 2, -ux * t / 2                    # across the wall: its profile on the left
                items.append({"id": e.id, "t": "wall", "a": [ox - nx + sh[0], oy - ny + sh[1]],
                              "b": [ox + nx + sh[0], oy + ny + sh[1]], "sec": pw["points"], "z": low,
                              "s": _status(e, ("width",)), "m": mark,
                              "info": f"shaped as {pw['section']}, {t:.0f} thick", "lv": k})
            elif e.type == "wall" and e.extra.get("runs") and e.final("role") != "masonry":
                for r in e.extra["runs"]:
                    items.append({"id": e.id, "t": "wall", "a": [r["start"][0] + sh[0], r["start"][1] + sh[1]],
                                  "b": [r["end"][0] + sh[0], r["end"][1] + sh[1]], "w": r["thickness"],
                                  "z0": low + _off(e, "base_mm"), "z1": top + _off(e, "top_mm"),
                                  "s": _status(e, ("width",)), "m": mark,
                                  "info": f"{e.final('shape') or ''} {r['thickness']:.0f} thick"
                                          + (", ends at this floor (column-stop layer)" if e.extra.get("stops_here") else ""),
                                  "lv": k})
                    if e.extra.get("lean"):
                        _lean(items[-1], e.extra["lean"])
        # the slab strip along a drawn slab edge (linker._edge_strips), as the export
        for gi, g in enumerate(s.extra.get("section_edge_strips") or []):
            items.append({"id": f"{sid}/edge/{gi}", "t": "slab", "p": [[x + sh[0], y + sh[1]] for x, y in g["outline"]],
                          "z": top, "th": float(g["thickness"]), "s": "read" if g.get("written") else "low", "m": "",
                          "info": f"slab strip of section {g['section']} along the drawn slab edge, "
                                  f"{g['width']:.0f} wide, {g['thickness']:.0f} thick", "lv": k})
        # beams where only their slab strips were paired (linker._beam_line_gaps), as the export
        for gi, g in enumerate(s.extra.get("section_gap_beams") or []):
            items.append({"id": f"{sid}/gap/{gi}", "t": "beam", "a": [g["start"][0] + sh[0], g["start"][1] + sh[1]],
                          "b": [g["end"][0] + sh[0], g["end"][1] + sh[1]], "w": float(g["width"]),
                          "d": float(g["depth"]), "z": top, "s": "read", "m": str(g["mark"]),
                          "info": f"{g['width']:.0f} x {g['depth']:.0f}, " + (g.get("why") or "only its slab strips "
                                  f"were paired there (section {g['section']})"), "lv": k})
        # a beam end stopping short of what carries it is carried on onto it, as the export does
        from building.beam_ends import onto_supports
        on = [it for it in items[n_sheet:] if it["t"] == "beam" and it.get("w") and (it.get("mid") or not it.get("p"))]
        for it, new in zip(on, onto_supports([(it["a"], it["b"], it["w"]) + ((it["mid"],) if it.get("mid") else ())
                                              for it in on], _footprints(items[n_sheet:]))):
            for key, nw in zip(("a", "b"), new):
                if nw is not None:
                    it[key] = nw
            if new[2] is not None:                          # a curved beam carried on round its arc
                from building.ifc_export import arc_band
                it["mid"] = new[2]
                it["p"] = [[round(x, 1), round(y, 1)] for x, y in arc_band(it["a"], it["mid"], it["b"], it["w"])]
        # beams turning an L corner are drawn on to fill it, as Revit joins them
        from building.beam_ends import corner_extensions, carried
        straight = [it for it in items[n_sheet:] if it["t"] == "beam" and not it.get("p")
                    and not it.get("sec") and "z2" not in it]
        for it, ext in zip(straight, corner_extensions([(it["a"], it["b"], it["w"]) for it in straight])):
            if ext[0] or ext[1]:
                it["a"], it["b"] = carried(it["a"], it["b"], ext)
        view = s.extra.get("view_sheet")
        # stairs are read, not modelled, unless the project switches them on (user 2026-10-10)
        for e in (b.of("stair") if (decisions or {}).get("model_stairs") else []):
            if e.sheet not in (sid, view):
                continue
            for f in e.geometry.get("flights") or []:
                (a1, a2), (c1, c2) = f["first"], f["last"]
                items.append({"id": e.id, "t": "stair", "p": [[p[0] + sh[0], p[1] + sh[1]] for p in (a1, a2, c2, c1)],
                              "z0": low, "z1": top, "s": "read", "m": "STAIR",
                              "info": f"{f['treads']} treads @ {f['going']}, {f['width']} wide", "lv": k})
    if fd and fd.get("items") and shifts.get(fd["plan"]) is not None:
        ref_shift = shifts.get(fd["plan"]) or (0.0, 0.0)
        lz = z.get(fd["level_key"], 0.0)
        tops = {}
        for it in fd["items"]:
            if it["type"] == "footing":
                if it.get("bottom_mm") is None:
                    # no founding level read or decided: drawn with its top where the lowest
                    # floor's columns start (not up at that floor), marked missing
                    under = below.get(keys[0], below.get(fd["level_key"], lz))
                    base, s, note = under - it["thickness"], "missing", ", founding level not decided"
                else:
                    # its founding level, against the drawings' datum - the same datum as the
                    # levels read or decided; only a preview's assumed level stands in for it
                    base = float(it["bottom_mm"]) + (lz if fd["level_key"] in assumed else 0.0)
                    s, note = "read", f", founded at {it['bottom_mm']:+.0f}"
                t0 = base + it["thickness"]
                tops[(round(it["x"]), round(it["y"]))] = t0
                items.append({"id": it.get("id", ""), "t": "footing", "x": it["x"] + ref_shift[0], "y": it["y"] + ref_shift[1],
                              "w": it["size_x"], "d": it["size_y"], "rot": float(it.get("rotation") or 0.0), "z0": base, "z1": t0, "s": s,
                              "m": it.get("mark", ""), "info": f"{it['size_x']:.0f} x {it['size_y']:.0f} x "
                                                              f"{it['thickness']:.0f}{note}", "lv": "FDN"})
        for it in fd["items"]:
            if it["type"] == "raft" and len(it.get("outline") or []) >= 3:
                # a raft: its outline, from its founding level up by its thickness
                if it.get("bottom_mm") is None:
                    base, s, note = below.get(keys[0], below.get(fd["level_key"], lz)) - it["thickness"], "missing", \
                        ", founding level not decided"
                else:
                    base = float(it["bottom_mm"]) + (lz if fd["level_key"] in assumed else 0.0)
                    s, note = "read", f", founded at {it['bottom_mm']:+.0f}"
                items.append({"id": it.get("id", ""), "t": "footing", "p": [[p[0] + ref_shift[0], p[1] + ref_shift[1]]
                                                                           for p in it["outline"]],
                              "z0": base, "z1": base + it["thickness"], "s": s, "m": it.get("mark", ""),
                              "info": f"raft {it['thickness']:.0f} thick{note}", "lv": "FDN"})
        for it in fd["items"]:
            if it["type"] == "step":
                t0 = tops.get((round(it["x"]), round(it["y"])))
                if t0 is not None:
                    items.append({"id": it.get("id", ""), "t": "footing", "x": it["x"] + ref_shift[0], "y": it["y"] + ref_shift[1],
                                  "w": it["size_x"], "d": it["size_y"], "rot": float(it.get("rotation") or 0.0), "z0": t0,
                                  "z1": t0 + it["thickness"], "s": "read", "m": it.get("mark", ""),
                                  "info": "step", "lv": "FDN"})
    if fd and fd.get("not_placed") and not any(it["type"] == "footing" for it in fd.get("items") or []):
        why = Counter(r.split(": ", 1)[-1] for r in fd["not_placed"]).most_common(1)[0][0]
        notes.append(f"foundations: {len(fd['not_placed'])} footings not shown - {why} "
                     f"(nothing is drawn at an assumed size)")
    from building.sheets.levels import custom_floor

    def shown(k):
        # a floor added on the Floors page is shown by its name, not its key
        return (revit_name.get(k) or k) if custom_floor(k) else k
    if assumed:
        notes.insert(0, "levels " + ", ".join(shown(k) for k in assumed) + " are shown 3 m per storey: the drawings give no elevation")
    return {"set": set_key, "levels": [{"key": k, "z": z[k], **({"name": shown(k)} if custom_floor(k) else {})} for k in keys],
            "items": items, "notes": notes}


PAGE = r"""<!doctype html>
<html><head><meta charset="utf-8"><title>3D view</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root{--ink:#16202B;--paper:#F7F6F2;--line:#DCD9D0;--mute:#4C5A66}
body{margin:0;font:13px "Segoe UI",Helvetica,Arial,sans-serif;color:var(--ink);background:var(--paper);overflow:hidden}
#bar{position:absolute;top:0;left:0;right:0;padding:8px 12px;background:#fff;border-bottom:1px solid var(--line);
 display:flex;gap:14px;align-items:center;flex-wrap:wrap;z-index:2}
#bar b{font-size:15px}#bar label{white-space:nowrap}
#notes{position:absolute;left:12px;bottom:10px;max-width:60%;color:var(--mute);font-size:12px;z-index:2}
#tip{position:absolute;pointer-events:none;background:#fff;border:1px solid var(--line);padding:6px 8px;
 border-radius:4px;display:none;z-index:3;box-shadow:0 2px 8px rgba(0,0,0,.12)}
.dot{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:4px;vertical-align:-1px}
#msg{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);color:var(--mute)}
</style></head><body>
<div id="bar"><b>3D view</b><select id="set"></select><span id="types"></span><span id="legend"></span>
<span style="color:var(--mute)">drag: orbit &middot; right-drag: pan &middot; wheel: zoom</span></div>
<div id="tip"></div><div id="notes"></div><div id="msg">Loading the 3D library...</div>
<script src="https://cdnjs.cloudflare.com/ajax/libs/three.js/0.160.0/three.min.js"></script>
<script>
const DATA = __DATA__;
const COL={read:'#8A9BA8',low:'#E3A64B',conflict:'#C0392B',missing:'#7E57C2',override:'#2E7D5B'};
const TYPES=['footing','column','wall','beam','slab','stair'];
const $=id=>document.getElementById(id);
if(typeof THREE==='undefined'){ $('msg').textContent='The 3D library could not be loaded - this page needs the internet the first time it is opened.'; throw 0; }
$('msg').remove();
const renderer=new THREE.WebGLRenderer({antialias:true});renderer.setPixelRatio(devicePixelRatio);
document.body.appendChild(renderer.domElement);
const scene=new THREE.Scene();scene.background=new THREE.Color(0xF7F6F2);
const camera=new THREE.PerspectiveCamera(45,1,0.1,5000);
scene.add(new THREE.HemisphereLight(0xffffff,0x9aa4ad,1.1));
const sun=new THREE.DirectionalLight(0xffffff,0.9);sun.position.set(1,2,1.5);scene.add(sun);
let group=null,meshes=[],shown=Object.fromEntries(TYPES.map(t=>[t,true]));
function resize(){renderer.setSize(innerWidth,innerHeight);camera.aspect=innerWidth/innerHeight;camera.updateProjectionMatrix()}
addEventListener('resize',resize);resize();
const mats={};function mat(s,t){const k=s+t;if(!mats[k])mats[k]=new THREE.MeshLambertMaterial({color:COL[s]||COL.read,
 transparent:t==='slab'||t==='footing',opacity:t==='slab'?0.55:(t==='footing'?0.85:1)});return mats[k]}
// mm in the drawing -> m in the scene; drawing y -> scene -z, height -> scene y
const M=v=>v/1000;
function box(cx,cy,w,d,rot,z0,z1,it){const g=new THREE.BoxGeometry(M(w),Math.max(M(z1-z0),0.01),M(d));
 const m=new THREE.Mesh(g,mat(it.s,it.t));m.position.set(M(cx),M((z0+z1)/2),-M(cy));m.rotation.y=(rot||0)*Math.PI/180;return m}
function bar(a,b,w,z0,z1,it){const dx=b[0]-a[0],dy=b[1]-a[1],L=Math.hypot(dx,dy);
 const m=box((a[0]+b[0])/2,(a[1]+b[1])/2,L,w,0,z0,z1,it);m.rotation.y=Math.atan2(dy,dx);return m}
// a sloping beam (building section): a box whose top runs from za at a to zb at b
function slant(a,b,w,za,zb,d,it){const dx=b[0]-a[0],dy=b[1]-a[1],L=Math.hypot(dx,dy)||1,nx=-dy/L*w/2,ny=dx/L*w/2;
 const c=[[a[0]-nx,a[1]-ny,za-d],[a[0]+nx,a[1]+ny,za-d],[b[0]+nx,b[1]+ny,zb-d],[b[0]-nx,b[1]-ny,zb-d],
  [a[0]-nx,a[1]-ny,za],[a[0]+nx,a[1]+ny,za],[b[0]+nx,b[1]+ny,zb],[b[0]-nx,b[1]-ny,zb]],v=[];
 c.forEach(p=>v.push(M(p[0]),M(p[2]),-M(p[1])));const g=new THREE.BufferGeometry();
 g.setAttribute('position',new THREE.Float32BufferAttribute(v,3));
 g.setIndex([0,2,1,0,3,2,4,5,6,4,6,7,0,1,5,0,5,4,1,2,6,1,6,5,2,3,7,2,7,6,3,0,4,3,4,7]);g.computeVertexNormals();
 const m=new THREE.Mesh(g,mat(it.s,it.t));m.material.side=THREE.DoubleSide;return m}
// a sloping slab (building section): its top through both beams' heights, its thickness under it
function slopedSlab(pts,sl,top,th,it){const lo=Math.min(sl.p0,sl.p1),hi=Math.max(sl.p0,sl.p1);
 const z=p=>{let q=(p[0]-sl.o[0])*sl.d[0]+(p[1]-sl.o[1])*sl.d[1];q=Math.min(Math.max(q,lo),hi);return top+sl.z0+(q-sl.p0)*(sl.z1-sl.z0)/(sl.p1-sl.p0)};
 const n=pts.length,v=[];pts.forEach(p=>v.push(M(p[0]),M(z(p)),-M(p[1])));pts.forEach(p=>v.push(M(p[0]),M(z(p)-th),-M(p[1])));
 const idx=[];THREE.ShapeUtils.triangulateShape(pts.map(p=>new THREE.Vector2(p[0],p[1])),[]).forEach(t=>{idx.push(t[0],t[1],t[2],t[0]+n,t[2]+n,t[1]+n)});
 for(let i=0;i<n;i++){const j=(i+1)%n;idx.push(i,j,j+n,i,j+n,i+n)}
 const g=new THREE.BufferGeometry();g.setAttribute('position',new THREE.Float32BufferAttribute(v,3));g.setIndex(idx);g.computeVertexNormals();
 const m=new THREE.Mesh(g,mat(it.s,it.t));m.material.side=THREE.DoubleSide;return m}
// a beam built to its drawn section: the profile (u to the left of a->b, v down from the
// top, mm) carried along the beam from a to b
function swept(a,b,sec,z,it){const dx=b[0]-a[0],dy=b[1]-a[1],L=Math.hypot(dx,dy);
 const g=new THREE.ExtrudeGeometry(new THREE.Shape(sec.map(p=>new THREE.Vector2(M(p[0]),M(p[1])))),{depth:Math.max(M(L),0.01),bevelEnabled:false});
 const m=new THREE.Mesh(g,mat(it.s,it.t)),f=new THREE.Vector3(dx,0,-dy).normalize(),up=new THREE.Vector3(0,1,0);
 m.quaternion.setFromRotationMatrix(new THREE.Matrix4().makeBasis(new THREE.Vector3().crossVectors(up,f),up,f));
 m.position.set(M(a[0]),M(z),-M(a[1]));return m}
// a round column: diameter dia, standing from z0 to z1
function cyl(x,y,dia,z0,z1,it){const h=Math.max(M(z1-z0),0.01),m=new THREE.Mesh(new THREE.CylinderGeometry(M(dia/2),M(dia/2),h,32),mat(it.s,it.t));
 m.position.set(M(x),M(z0)+h/2,-M(y));return m}
function prism(pts,z0,z1,it){const sh=new THREE.Shape(pts.map(p=>new THREE.Vector2(M(p[0]),M(p[1]))));
 const g=new THREE.ExtrudeGeometry(sh,{depth:Math.max(M(z1-z0),0.01),bevelEnabled:false});
 const m=new THREE.Mesh(g,mat(it.s,it.t));m.rotation.x=-Math.PI/2;m.position.y=M(z0);return m}
// leaning as a wall elevation draws it: bottom moved by ln[0..1], top by ln[2..3] (mm), sheared between
function leanIt(m,it){m.updateMatrix();const g=m.geometry.clone();g.applyMatrix4(m.matrix);
 const p=g.attributes.position,lo=M(it.z0),H=Math.max(M(it.z1)-lo,1e-6),l=it.ln;
 for(let i=0;i<p.count;i++){const t=Math.min(Math.max((p.getY(i)-lo)/H,0),1);
  p.setX(i,p.getX(i)+M(l[0]+(l[2]-l[0])*t));p.setZ(i,p.getZ(i)-M(l[1]+(l[3]-l[1])*t))}
 p.needsUpdate=true;g.computeVertexNormals();return new THREE.Mesh(g,m.material)}
function domAng(segs){const h={};let tot=0,best=0,bw=0;for(const [a,b] of segs){const dx=b[0]-a[0],dy=b[1]-a[1],L=Math.hypot(dx,dy);if(L<1)continue;
 let t=((Math.atan2(dy,dx)*180/Math.PI)%90+90)%90;if(t>45)t-=90;const k=Math.round(t*2)/2;h[k]=(h[k]||0)+L;tot+=L;if(h[k]>bw){bw=h[k];best=k}}
 return (Math.abs(best)<0.25||bw<0.4*tot)?0:best}
function build(si){if(group)scene.remove(group);group=new THREE.Group();meshes=[];const S=DATA[si];
 for(const it of S.items){let m;
  // a column's rotation is the direction of its long side (depth); a footing's sizes are x and y
  if(it.t==='column')m=it.rd?cyl(it.x,it.y,it.rd,it.z0,it.z1,it):box(it.x,it.y,it.d,it.w,it.rot,it.z0,it.z1,it);
  else if(it.t==='footing')m=it.p?prism(it.p,it.z0,it.z1,it):box(it.x,it.y,it.w,it.d,it.rot,it.z0,it.z1,it);
  else if(it.t==='beam')m=it.sec?swept(it.a,it.b,it.sec,it.z,it):it.p?prism(it.p,it.z-it.d,it.z,it):
   (it.z2!==undefined&&Math.abs(it.z2-it.z)>1)?slant(it.a,it.b,it.w,it.z,it.z2,it.d,it):bar(it.a,it.b,it.w,it.z-it.d,it.z,it);
  else if(it.t==='wall')m=it.sec?swept(it.a,it.b,it.sec,it.z,it):bar(it.a,it.b,it.w,it.z0,it.z1,it);
  else if(it.t==='slab')m=it.sl?slopedSlab(it.p,it.sl,it.z,it.th,it):prism(it.p,it.z-it.th,it.z,it);
  else if(it.t==='stair')m=prism(it.p,it.z0,it.z0+(it.z1-it.z0)/2,it);
  if(!m)continue;if(it.ln)m=leanIt(m,it);m.userData=it;m.visible=shown[it.t];group.add(m);meshes.push(m)}
 // a plan drawn a few degrees off square (a turned UCS) is shown square - display only
 group.rotation.y=-domAng(S.items.filter(it=>it.t==='beam').map(it=>[it.a,it.b]))*Math.PI/180;
 scene.add(group);group.updateMatrixWorld(true);const bb=new THREE.Box3().setFromObject(group),c=bb.getCenter(new THREE.Vector3()),
 r=bb.getSize(new THREE.Vector3()).length()||10;target.copy(c);dist=r*1.1;theta=1.1;phi=0.9;place();
 $('notes').innerHTML=(S.notes||[]).map(n=>'&bull; '+n).join('<br>')+'<br>Levels: '+S.levels.map(l=>(l.name||l.key)+' '+(l.z/1000).toFixed(2)+' m').join(', ')}
// orbit by hand: no extra script to load
const target=new THREE.Vector3();let dist=50,theta=-0.9,phi=0.9;
function place(){camera.position.set(target.x+dist*Math.sin(phi)*Math.cos(theta),target.y+dist*Math.cos(phi),
 target.z+dist*Math.sin(phi)*Math.sin(theta));camera.lookAt(target)}
let drag=null;renderer.domElement.addEventListener('contextmenu',e=>e.preventDefault());
renderer.domElement.addEventListener('pointerdown',e=>{drag={x:e.clientX,y:e.clientY,b:e.button}});
addEventListener('pointerup',()=>drag=null);
renderer.domElement.addEventListener('pointermove',e=>{if(drag){const dx=e.clientX-drag.x,dy=e.clientY-drag.y;drag.x=e.clientX;drag.y=e.clientY;
  if(drag.b===2||e.shiftKey){const k=dist/600,f=new THREE.Vector3();camera.getWorldDirection(f);
   const right=new THREE.Vector3().crossVectors(f,camera.up).normalize(),up=new THREE.Vector3().crossVectors(right,f).normalize();
   target.addScaledVector(right,-dx*k).addScaledVector(up,dy*k)}
  else{theta+=dx*0.005;phi=Math.min(Math.max(phi-dy*0.005,0.05),3.09)}place();$('tip').style.display='none';return}
 hover(e)});
renderer.domElement.addEventListener('wheel',e=>{e.preventDefault();dist*=Math.exp(e.deltaY*0.001);place()},{passive:false});
const ray=new THREE.Raycaster(),mouse=new THREE.Vector2();
function hover(e){mouse.set(e.clientX/innerWidth*2-1,-e.clientY/innerHeight*2+1);ray.setFromCamera(mouse,camera);
 const hit=ray.intersectObjects(meshes.filter(m=>m.visible))[0],tip=$('tip');
 if(!hit){tip.style.display='none';return}const it=hit.object.userData;
 tip.innerHTML=`<b>${it.m||'(no mark)'}</b> &middot; ${it.t} &middot; ${it.lv}<br>${it.info}<br><span class="dot" style="background:${COL[it.s]}"></span>${it.s}`;
 tip.style.left=(e.clientX+14)+'px';tip.style.top=(e.clientY+14)+'px';tip.style.display='block'}
$('set').innerHTML=DATA.map((s,i)=>`<option value="${i}">${s.set}</option>`).join('');$('set').onchange=e=>build(+e.target.value);
$('types').innerHTML=TYPES.map(t=>`<label><input type="checkbox" checked data-t="${t}"> ${t}s</label>`).join(' ');
document.querySelectorAll('#types input').forEach(c=>c.onchange=()=>{shown[c.dataset.t]=c.checked;meshes.forEach(m=>m.visible=shown[m.userData.t])});
$('legend').innerHTML=Object.entries(COL).map(([k,c])=>`<span class="dot" style="background:${c}"></span>${k}`).join(' ');
(function loop(){requestAnimationFrame(loop);renderer.render(scene,camera)})();
build(0);
</script></body></html>"""


def write(b: Building, path: str, decisions: dict | None = None) -> str | None:
    """decisions: the project decisions (overrides.json "project"): floor elevations
    decided on the Export page are used as the Revit build uses them."""
    sets = sorted({s.extra.get("set", "") for s in b.sheets.values() if s.reader == "plan.legacy"})
    data = [sc for sc in (scene(b, k, decisions) for k in sets) if sc and sc.get("items")]
    if not data:
        return None
    with open(path, "w", encoding="utf-8") as f:
        f.write(PAGE.replace("__DATA__", json.dumps(data, separators=(",", ":"))))
    return path


def main(argv=None) -> int:
    run = (argv or sys.argv[1:] or ["."])[0]
    b = Building.load(os.path.join(run, "building.json"))
    p = write(b, os.path.join(run, "model3d.html"))
    print(f"Wrote {p}" if p else "Nothing to show")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
