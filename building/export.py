r"""Stage 8: the reviewed building, written out for the Revit build.

    python -m building.export RUN_FOLDER [OUT] [--overrides overrides.json]

RUN_FOLDER holds building.json (from readset). For every framing plan it writes
OUT\<plan>\model.json - the very file the Revit build (revit\build_model.py)
already reads, field for field - but with what the review settled:

* every column, beam and slab gets its resolved mark and size where the sources
  agree (read) or a person decided (override). An element whose size is in
  conflict, assumed, or given by nothing is NOT written: the report lists it under
  "needs a decision" until a person settles it in the review. The drawing's own
  value never stands in for a decision, and model.json says "strict", so the
  Revit build assumes nothing either (no default depth, thickness or height);
* a beam line split into marked spans goes out as those spans, each with its
  own mark and size;
* a column the plan reader took for a wall (Sasan Gir's L-shaped C7, the skewed
  C19/C20) goes out as a column; masonry walls are left out;
* a wall matched to a schedule section goes out as one straight wall of that
  thickness along its outline, instead of the pieces its outline splits into;
* an element excluded in the review is left out.

The model.json is built from the reader's own exact copy of the original
(legacy_plan.to_legacy, proven lossless by the baselines) and changed only where
listed, so nothing else the build relies on moves. Each plan gets an
export_report.txt saying what changed and why, what was kept as drawn, and what
is read but not built yet (footings, stairs, ramps). levels.json holds the level
table (stage 7) for setting up the Revit levels.

The original route - extractor -> model.json -> build - is untouched.
"""
from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from building import Building                               # noqa: E402
from building.model import MISSING, OVERRIDE, READ          # noqa: E402
from building.readers.legacy_plan import SPEC, _counts, to_legacy   # noqa: E402

USABLE = (READ, OVERRIDE)
NOT_BUILT_YET = ("footing", "pedestal", "raft", "stair", "ramp")


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_")[:60]


# sizes: a value that is not a number above 0 is never built (a level or founding level
# may be below 0, so only these)
SIZES = ("width", "depth", "thickness", "diameter", "length", "depth_edge", "going", "riser")


def _positive(v) -> bool:
    try:
        return math.isfinite(float(v)) and float(v) > 0
    except (TypeError, ValueError):
        return False


def _settled(e, prop):
    """The resolved value if it may be built from, else None."""
    v = e.resolved.get(prop)
    if v is None or v.status not in USABLE or v.value in (None, ""):
        return None
    if prop in SIZES and not _positive(v.value):
        return None
    return v.value


def _bad_size(e, prop) -> bool:
    """Read or decided, but not a size anything can be built at (0, -230, 'abc')."""
    v = e.resolved.get(prop)
    return (prop in SIZES and v is not None and v.status in USABLE and v.value not in (None, "")
            and not _positive(v.value))


def _why(e, prop) -> str:
    v = e.resolved.get(prop)
    if v is None:
        return "no source"
    kinds = sorted({ev.source.split(":")[0] for ev in v.evidence if ev.source})
    return (v.status if v.status != READ else "read") + (" (" + ", ".join(kinds) + ")" if kinds else "")


class Report:
    def __init__(self):
        self.changed, self.kept, self.left_out, self.added = [], [], [], []
        self.held = []              # needs a decision: not built until a person settles it
        self.provisional = []       # needs a decision, built meanwhile at a working size (marked in Revit)

    def change(self, what, field, old, new, why):
        if old != new:
            self.changed.append(f"{what}: {field} {old} -> {new}   [{why}]")

    def keep(self, what, field, value, why):
        self.kept.append(f"{what}: {field} kept as drawn ({value}) - {why}")

    def hold(self, what, open_):
        self.held.append(f"{what}: " + "; ".join(open_))


def _patch_size(row, e, rep, what, fields=("width", "depth")) -> list[str]:
    """Each size the review settled goes into the row. Returns the sizes that are not
    settled - sources in conflict, assumed, or given by nothing: such an element is not
    built (the drawing's own value is never used in their place)."""
    open_ = []
    for f in fields:
        v = _settled(e, f)
        if v is None:
            st = e.resolved.get(f)
            if _bad_size(e, f):
                open_.append(f"{f} {st.value} is not a size (must be above 0)")
            elif st is not None and st.status not in USABLE:
                open_.append(f"{f} {_why(e, f)}")
            elif not _positive(row.get(f)):
                open_.append(f"{f} not given")
            continue
        v = float(v)
        rep.change(what, f, row.get(f), v, _why(e, f))
        row[f] = v
    return open_


def _common(values) -> float | None:
    """The value most elements of a kind settled on (the larger on a tie)."""
    vals = [round(float(v)) for v in values if v]
    return float(max(set(vals), key=lambda v: (vals.count(v), v))) if vals else None


class Provisional:
    """Working sizes for elements nobody has settled yet, when the project asks for
    everything to be built (project decision "build_unsettled", on in the app).
    Each such element is written with a "provisional" note saying what is open and
    what was used; the Revit build colours it and puts the note in its Comments.
    Sources in conflict: the strongest one's value. Given by nothing: the size most
    elements of that kind on the same plan settled on - for a beam, those as wide."""

    def __init__(self, b: Building, sid: str, decisions: dict | None = None):
        set_key = (b.sheets[sid].extra.get("set", "") if sid in b.sheets else "")

        def pools(on):
            els = [e for e in b.elements if on(e)]
            return ([(_settled(e, "width"), _settled(e, "depth")) for e in els if e.type in ("beam", "beam_span")],
                    [(_settled(e, "width"), _settled(e, "depth")) for e in els if e.type == "column"],
                    [_settled(e, "thickness") for e in els if e.type == "slab"])
        # this floor first, then the whole project (the same drawing set)
        self.floor = pools(lambda e: e.sheet == sid)
        self.job = pools(lambda e: b.sheets.get(e.sheet) is not None
                         and b.sheets[e.sheet].extra.get("set", "") == set_key)
        # a slab thickness nothing in the drawings gives: one value the person decides
        self.slab_mm = (decisions or {}).get("slab_thickness_mm")

    def _pick(self, kind, f, w):
        i = 0 if f == "width" else 1
        for where, (beams, cols, slabs) in (("on this floor", self.floor), ("in this project", self.job)):
            if kind == "slab":
                v = _common(slabs)
            else:
                pool = beams if kind == "beam" else cols
                v = (_common(p[i] for p in pool if p[0] and abs(float(p[0]) - float(w)) < 1) if w else None) \
                    or _common(p[i] for p in pool)
            if v:
                return v, f"the commonest {kind} {f} {where}"
        if kind == "slab" and self.slab_mm:
            return float(self.slab_mm), "the slab thickness you set for the project"
        return None

    def value(self, e, f, kind: str, row: dict):
        st = e.resolved.get(f)
        if st is not None and st.status not in USABLE and st.value not in (None, "") and _positive(st.value):
            return float(st.value), f"{f} {_why(e, f)} - built at {float(st.value):.0f}, the strongest source"
        got = self._pick(kind, f, row.get("width") if f == "depth" else None)
        why = f"{f} {st.value} is not a size" if _bad_size(e, f) else f"{f} not given"
        return (got[0], f"{why} - built at {got[0]:.0f}, {got[1]}") if got else None

    def fill(self, row, e, kind: str, open_: list[str], fields) -> bool:
        """Put working values in for every open size. False when one has none."""
        notes = []
        for f in fields:
            if not any(o.startswith(f + " ") for o in open_):
                continue
            got = self.value(e, f, kind, row)
            if got is None:
                return False
            row[f] = got[0]
            notes.append(got[1])
        row["provisional"] = "; ".join(notes)
        return True


def left_out(e) -> bool:
    """Not built: decided 'other block', or read as standing clear of the plan (a legend
    sample / detail, linker._off_the_frame) - the rows _in_scope leaves out. The 3D view and
    the review use it so they show what the build makes."""
    v = e.resolved.get("scope")
    return v is not None and v.status in (OVERRIDE, READ) and str(v.value or "").lower().startswith("other")


def _in_scope(row, e, rep, what, prov) -> bool:
    """An element inside an area marked as another block (linker._scope): decided 'other
    block' - left out; decided 'this block' - built; not decided - built marked, or held
    when nothing unsettled may be built. False when the row is not built."""
    v = e.resolved.get("scope")
    if v is None:
        return True
    if v.status == OVERRIDE:
        if str(v.value or "").lower().startswith("other"):
            rep.left_out.append(f"{what}: another block's, decided in the review")
            return False
        return True
    if v.status == READ and str(v.value or "").lower().startswith("other"):
        # linker._off_the_frame: a legend sample / detail beside the plan
        rep.left_out.append(f"{what}: {str(v.value).split(':', 1)[-1].strip()} - not built "
                            "('this block' in the review builds it)")
        return False
    where = next((ev.source.split(":")[0] for ev in v.evidence if "other block" in ev.source), "")
    note = f"{where} - another block's? decide in the review"
    if prov is None:
        rep.hold(what, [note])
        return False
    row["provisional"] = "; ".join(x for x in (row.get("provisional"), note) if x)
    rep.provisional.append(f"{what}: {note}")
    return True


def _section(row, e, rep, what, prov) -> bool:
    """A beam built to a drawn section (linker._beam_sections): its profile goes in the
    row. Which way round not settled - built marked, or held when nothing unsettled may
    be built. False when the row is not built."""
    from building.linker import section_profile
    prof, v = section_profile(e), e.resolved.get("section")
    if prof is None or v is None:
        return True
    name = str(v.value).replace(" mirrored", "")
    row["section"] = {"name": name, "profile": prof}
    if v.status not in USABLE:
        note = (f"section {name}: slab on both sides of the beam or on neither, so which way round is not "
                f"known - built as read; decide 'as read' or 'mirrored' in the review")
        if prov is None:
            rep.hold(what, [note])
            return False
        row["provisional"] = "; ".join(x for x in (row.get("provisional"), note) if x)
        rep.provisional.append(f"{what}: {note}")
    rep.added.append(f"{what}: built to section {name} - its drawn shape, not a rectangle")
    return True


def _heights(row, e, rep, what) -> list:
    """A beam's height from a building section (linker._section_heights): its top off the
    level, or - sloping with a slab - its pieces, each from one height to the next (the
    north edge of Main Block's S6 slopes up to B7, then runs flat). Returns the rows."""
    v = e.resolved.get("top_mm")
    if v is not None and v.status in USABLE and v.value is not None:
        row["top_mm"] = float(v.value)
        rep.change(what, "top_mm", None, row["top_mm"], _why(e, "top_mm"))
    prof = e.extra.get("slope_profile")
    if not prof or len(prof) < 2:
        return [row]
    (ax, ay), (bx, by) = row["start"][:2], row["end"][:2]
    out = []
    for (t0, z0), (t1, z1) in zip(prof, prof[1:]):
        r = dict(row)
        r["start"] = [ax + (bx - ax) * t0, ay + (by - ay) * t0]
        r["end"] = [ax + (bx - ax) * t1, ay + (by - ay) * t1]
        r["top_mm"] = float(z0)
        if abs(z1 - z0) > 1.0:
            r["top_end_mm"] = float(z1)
        out.append(r)
    rep.added.append(f"{what}: slopes with the slab (building section) - {len(out)} piece(s), "
                     + ", ".join(f"{z:+.0f}" for _, z in prof) + " mm")
    return out


def _patch_mark(row, e, rep, what):
    m = _settled(e, "mark")
    if m is None:
        st = e.resolved.get("mark")
        if st is not None and st.status not in USABLE:
            rep.keep(what, "mark", row.get("mark"), _why(e, "mark"))
        return
    if "mark" in row or m:
        rep.change(what, "mark", row.get("mark"), m, _why(e, "mark"))
        row["mark"] = m


def export_plan(b: Building, sid: str, provisional: bool = False,
                decisions: dict | None = None) -> tuple[dict, Report]:
    """provisional: build what is not settled at a working size, marked (see Provisional)."""
    d = to_legacy(b, sid)
    rep = Report()
    prov = Provisional(b, sid, decisions) if provisional else None

    def settle(row, e, what, open_, kind, fields=("width", "depth")) -> bool:
        """True when the row may be built: settled, or given working values."""
        if not open_:
            return True
        if e.extra.get("written_elsewhere"):
            # the drawings do write this mark somewhere: likely a reading fault, say where
            open_ = open_ + [f"the drawings write it in '{e.extra['written_elsewhere'][:60]}' - check that reading"]
        if prov and prov.fill(row, e, kind, open_, fields):
            if e.extra.get("written_elsewhere"):
                row["provisional"] += "; " + open_[-1]
            rep.provisional.append(f"{what}: {row['provisional']}")
            return True
        rep.hold(what, open_)
        return False
    by_type = {spec["type"]: key for key, spec in SPEC.items()}
    els = {key: [e for e in b.of(sheet=sid) if e.type == spec["type"] and not e.extra.get("generated")]
           for key, spec in SPEC.items()}
    spans = {}
    for e in b.of("beam_span"):
        if e.sheet == sid:
            spans.setdefault(e.extra.get("parent"), []).append(e)

    # columns
    cols = []
    for row, e in zip(d.get("columns", []), els["columns"]):
        what = f"column {e.final('mark') or row.get('mark') or '?'} at ({row.get('x', 0):.0f}, {row.get('y', 0):.0f})"
        if e.resolved.get("excluded"):
            rep.left_out.append(f"{what}: excluded in the review")
            continue
        row = dict(row)
        _patch_mark(row, e, rep, what)
        if not settle(row, e, what, _patch_size(row, e, rep, what), "column"):
            continue
        if not _in_scope(row, e, rep, what, prov):
            continue
        tv = e.resolved.get("top_mm")
        if tv is not None and tv.status in USABLE and tv.value is not None:
            row["top_mm"] = float(tv.value)              # ends under a lowered beam (building section)
            rep.added.append(f"{what}: ends {row['top_mm']:+.0f} mm from the level (building section)")
        cols.append(row)

    # beams, and the marked spans of split ones. A beam drawn with three lines (two read
    # as beams side by side) goes out as the joined beams instead (merge_beams.py)
    beams = []
    pairs = [(row, e) for row, e in zip(d.get("beams", []), els["beams"]) if not e.extra.get("merged_into")]
    row_of = {e.id: row for row, e in zip(d.get("beams", []), els["beams"])}
    for e in b.of(sheet=sid):
        if e.type != "beam" or not e.extra.get("merged_from") or e.extra["merged_from"][0] not in row_of:
            continue
        r = dict(row_of[e.extra["merged_from"][0]])
        r.update(start=list(e.geometry["start"]), end=list(e.geometry["end"]), width=e.get("width"),
                 mark=e.mark, depth=e.get("depth") or r.get("depth"))
        r.pop("mid", None)
        r["notes"] = list(r.get("notes") or []) + [f"one beam drawn with {len(e.extra['merged_from'])} lines, joined"]
        pairs.append((r, e))
        rep.added.append(f"beam {e.final('mark') or '?'} at ({r['start'][0]:.0f}, {r['start'][1]:.0f}): "
                         f"{len(e.extra['merged_from'])} lines drawn for one beam -> one {e.get('width'):.0f} wide beam")
    for row, e in pairs:
        what = f"beam {e.final('mark') or row.get('mark') or '?'}"
        if e.resolved.get("excluded"):
            rep.left_out.append(f"{what}: excluded in the review")
            continue
        parts = spans.get(e.id) if e.extra.get("split_into") else None
        if parts:
            for sp in parts:
                r = dict(row)
                r["start"], r["end"] = list(sp.geometry["start"]), list(sp.geometry["end"])
                r.pop("mid", None)
                sw = f"span {sp.final('mark') or '?'} of {what}"
                if sp.resolved.get("excluded"):
                    rep.left_out.append(f"{sw}: excluded in the review")
                    continue
                _patch_mark(r, sp, rep, sw)
                if not settle(r, sp, sw, _patch_size(r, sp, rep, sw), "beam"):
                    continue
                if not _in_scope(r, sp, rep, sw, prov) or not _section(r, sp, rep, sw, prov):
                    continue
                beams += _heights(r, sp, rep, sw)
            rep.added.append(f"{what}: one drawn line carrying {len(parts)} marks -> {len(parts)} beams")
            continue
        row = dict(row)
        _patch_mark(row, e, rep, what)
        if not settle(row, e, what, _patch_size(row, e, rep, what), "beam"):
            continue
        if not _in_scope(row, e, rep, what, prov) or not _section(row, e, rep, what, prov):
            continue
        beams += _heights(row, e, rep, what)

    # slabs
    slabs = []
    for row, e in zip(d.get("slabs", []), els["slabs"]):
        what = f"slab {e.final('mark') or row.get('mark') or '?'}"
        if e.resolved.get("excluded"):
            rep.left_out.append(f"{what}: excluded in the review")
            continue
        row = dict(row)
        _patch_mark(row, e, rep, what)
        if not settle(row, e, what, _patch_size(row, e, rep, what, fields=("thickness",)), "slab", ("thickness",)):
            continue
        m = e.resolved.get("mark")
        if prov is not None and m is not None and m.status not in USABLE:
            # no mark written in this bay: built with the nearest one's, marked until confirmed
            note = f"no slab mark written in this bay - {row.get('mark') or '?'} (the nearest) used"
            row["provisional"] = "; ".join(x for x in (row.get("provisional"), note) if x)
            rep.provisional.append(f"{what}: {note}")
        if not _in_scope(row, e, rep, what, prov):
            continue
        if e.extra.get("slope"):
            # sloping (building section): its top runs from z0 at p0 to z1 at p1 along dir
            row["slope"] = dict(e.extra["slope"])
            rep.added.append(f"{what}: slopes {row['slope']['z0']:+.0f} to {row['slope']['z1']:+.0f} mm "
                             f"(building section)")
        slabs.append(row)
    # slab panels taken from the beam bays (the plan draws no slab outline)
    for e in b.of(sheet=sid):
        if e.type != "slab" or not e.extra.get("generated"):
            continue
        what = f"slab {e.final('mark') or '?'} (from a beam bay)"
        if e.resolved.get("excluded"):
            rep.left_out.append(f"{what}: excluded in the review")
            continue
        t = _settled(e, "thickness")
        row = {"mark": _settled(e, "mark") or e.mark, "outline": e.geometry["outline"], "holes": [],
               "thickness": float(t) if t else None, "thickness_source": _why(e, "thickness"),
               "sunk_mm": 0.0, "source_layer": "", "handle": "",
               "notes": ["slab panel from the beam bay its mark is in (stage 8)"]}
        if not t and not settle(row, e, what, [f"thickness {_why(e, 'thickness')}"], "slab", ("thickness",)):
            continue
        t = row["thickness"]
        if e.extra.get("slope"):
            row["slope"] = dict(e.extra["slope"])
        slabs.append(row)
        rep.added.append(f"{what}: {len(e.geometry['outline'])}-sided panel, {t:.0f} thick")

    # walls: RC walls stay walls; columns drawn as odd shapes become columns
    walls = []
    for row, e in zip(d.get("walls", []), els["walls"]):
        role = e.final("role") or "RC wall"
        what = f"wall {e.final('mark') or row.get('mark') or ''} ({role})".replace("  ", " ")
        if e.resolved.get("excluded"):
            rep.left_out.append(f"{what}: excluded in the review")
            continue
        if role == "masonry":
            rep.left_out.append(f"{what} on layer {e.extra.get('_layer')}: masonry, not structure")
            continue
        open_ = [f"{'thickness' if f == 'width' else f} {_why(e, f)}" for f in ("width", "depth")
                 if e.resolved.get(f) is not None and e.resolved[f].status not in USABLE + (MISSING,)]
        open_ += [f"{'thickness' if f == 'width' else f} {e.resolved[f].value} is not a size (must be above 0)"
                  for f in ("width", "depth") if _bad_size(e, f)]
        if open_:
            rep.hold(what, open_)
            continue
        if role == "column" and e.extra.get("column_like"):
            cl = e.extra["column_like"]
            lo, hi = sorted((float(_settled(e, "width") or cl["width"]), float(_settled(e, "depth") or cl["depth"])))
            xs = [p[0] for p in row.get("outline") or []] or [0.0]
            ys = [p[1] for p in row.get("outline") or []] or [0.0]
            # as drawn: width along x, depth along y, the long side where the outline has it
            w, dp = (hi, lo) if (max(xs) - min(xs)) > (max(ys) - min(ys)) else (lo, hi)
            c = {"mark": _settled(e, "mark") or "", "x": cl["x"], "y": cl["y"], "rotation": 0.0,
                 "width": w, "depth": dp, "kind": "column", "outline": row.get("outline"),
                 "source_layer": row.get("source_layer", ""), "handle": row.get("handle", ""),
                 "notes": ["a column drawn as an odd shape; exported as a column (stage 8)"]}
            if not _in_scope(c, e, rep, what, prov):
                continue
            cols.append(c)
            rep.added.append(f"{what} -> column {c['mark'] or '(no mark)'} {c['width']:.0f} x {c['depth']:.0f} "
                             f"at ({c['x']:.0f}, {c['y']:.0f}) instead of {len(e.extra.get('runs') or [])} wall pieces")
            continue
        row = dict(row)
        sec = e.extra.get("schedule_section")
        t = _settled(e, "width")
        outline = row.get("outline") or []
        if sec and t and len(outline) >= 3 and not row.get("start"):
            xs, ys = [p[0] for p in outline], [p[1] for p in outline]
            x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
            if x1 - x0 >= y1 - y0:
                row["start"], row["end"] = [x0, (y0 + y1) / 2], [x1, (y0 + y1) / 2]
            else:
                row["start"], row["end"] = [(x0 + x1) / 2, y0], [(x0 + x1) / 2, y1]
            row["thickness"] = float(t)
            rep.added.append(f"{what}: one straight wall {math.dist(row['start'], row['end']):.0f} long, "
                             f"{t:.0f} thick, as the schedule section ({'-'.join(sec.get('range') or [])}) "
                             f"- its outline alone split into {len(e.extra.get('runs') or [])} pieces")
        if row.get("start") and not row.get("thickness"):
            rep.hold(what, ["thickness not given"])        # a wall drawn as one line
            continue
        if not _in_scope(row, e, rep, what, prov):
            continue
        walls.append(row)

    # hidden beams (folds): their depth comes from the slabs either side; one sized on
    # an assumed slab thickness waits for that thickness
    folds = []
    sized = [f.get("depth") for f in d.get("folds", []) or []
             if f.get("depth") and not any("assumes" in str(n) for n in f.get("notes") or [])]
    for f in d.get("folds", []) or []:
        if any("assumes" in str(n) for n in f.get("notes") or []):
            what = f"hidden beam at ({f['start'][0]:.0f}, {f['start'][1]:.0f})"
            if prov is None:
                rep.hold(what, ["depth rests on a slab thickness nobody gave"])
                continue
            f = dict(f, provisional=f"depth {f.get('depth') or 0:.0f} rests on a slab thickness nobody gave")
            rep.provisional.append(f"{what}: {f['provisional']}")
        elif prov is not None and f.get("width") and not f.get("depth") and _common(sized):
            # a slab on one side only: its depth has nothing to come from - built at the
            # depth most hidden beams on this floor have, marked (the build skipped it before)
            v = _common(sized)
            what = f"hidden beam at ({f['start'][0]:.0f}, {f['start'][1]:.0f})"
            f = dict(f, depth=v, provisional=f"no slab on one side, so no depth - built at {v:.0f}, "
                                             f"the commonest hidden beam depth on this floor")
            rep.provisional.append(f"{what}: {f['provisional']}")
        folds.append(f)
    if "folds" in d:
        d["folds"] = folds
    d["columns"], d["beams"], d["slabs"], d["walls"] = cols, beams, slabs, walls
    d["counts"] = {k: v for k, v in _counts(d).items() if k in (d.get("counts") or _counts(d))}
    d["export"] = {"from": "building.export (stage 8)", "plan": sid,
                   "changed": len(rep.changed), "kept_as_drawn": len(rep.kept), "left_out": len(rep.left_out),
                   "needs_decision": len(rep.held), "provisional": len(rep.provisional)}
    from building.sheets.levels import custom_floor, key_elevation, level_rank
    lk = (b.sheets[sid].extra.get("level_key") or "") if sid in b.sheets else ""
    if custom_floor(lk) or key_elevation(lk) is not None:
        # where a floor the Revit build cannot tell from its name sits ("Service Floor" added on
        # the Floors page, "EL+6600" named by its height): the build orders the floors by it
        d["export"]["level_rank"] = level_rank(lk)
    # the Revit build places nothing on an assumed size: a beam with no depth, a slab
    # with no thickness, a column with no level to rise from are reported, not guessed
    d["strict"] = True

    # read but not built yet, on this plan's level
    s = b.sheets[sid]
    lv, set_key = s.extra.get("level_key"), s.extra.get("set")
    for e in b.elements:
        if e.type in NOT_BUILT_YET and b.sheets.get(e.sheet) and b.sheets[e.sheet].extra.get("set") == set_key:
            view = s.extra.get("view_sheet")
            if e.sheet in (sid, view) or (e.type in ("footing", "pedestal", "raft") and lv in ("GF", "PLINTH")):
                rep.left_out.append(f"{e.type} {e.final('mark') or e.mark or ''} - read, not built yet")
    return d, rep


def _two(v) -> list[float]:
    """'450+300' -> [450, 300]; 750 -> [750]."""
    if v is None:
        return []
    if isinstance(v, (int, float)):
        return [float(v)]
    return [float(x) for x in re.findall(r"\d+(?:\.\d+)?", str(v))]


def _shift_by_marks(b: Building, fsheet: str, plan_sid: str, tol: float = 300.0):
    """The offset from a foundation sheet to a plan, from column marks written on
    both (Villa 5: no column layout; its raft layout writes C1..C18 beside the
    columns). Every mark pair votes; believed when three or more different marks
    agree within tol, and clearly more than any other offset. ((dx, dy), marks) or None."""
    on_f = [(c.mark, c.geometry["at"]) for c in b.of("caption")
            if c.sheet == fsheet and c.get("family") == "column" and c.geometry.get("at")]
    view = b.sheets[plan_sid].extra.get("view_sheet")
    on_p = [(c.final("mark"), (c.geometry["x"], c.geometry["y"])) for c in b.elements
            if c.sheet == plan_sid and c.type == "column" and c.final("mark") and c.geometry.get("x") is not None]
    if view:
        on_p += [(c.mark, tuple(c.geometry["at"])) for c in b.of("caption")
                 if c.sheet == view and c.get("family") == "column" and c.geometry.get("at")]
    votes: dict[tuple, set] = {}
    for m, (fx, fy) in on_f:
        for pm, (px, py) in on_p:
            if pm == m:
                key = (round((px - fx) / tol), round((py - fy) / tol))
                votes.setdefault(key, set()).add(m)
    if not votes:
        return None
    ranked = sorted(votes.items(), key=lambda kv: -len(kv[1]))
    (kx, ky), marks = ranked[0]
    rival = next((len(v) for k, v in ranked[1:] if abs(k[0] - kx) > 1 or abs(k[1] - ky) > 1), 0)
    if len(marks) < 3 or len(marks) < 2 * rival:
        return None
    # the offset itself: the mean of the pairs that voted for it
    pts = [(px - fx, py - fy) for m, (fx, fy) in on_f for pm, (px, py) in on_p
           if pm == m and (round((px - fx) / tol), round((py - fy) / tol)) == (kx, ky)]
    return (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)), len(marks)


def _fit_by_columns(b: Building, fsheet: str, plan_sid: str, tol: float = 300.0):
    """A foundation sheet drawn at another angle than the plan (J1153 B-block foundation,
    2 degrees): turn and shift found from the columns numbered on both - the column
    drawn inside each footing (readers/foundations column_at) and the plan's column of
    that number. Believed with three or more columns, every one within tol after the
    fit. ((cos, sin, dx, dy), degrees, columns, worst miss in mm) or None."""
    def key(m):
        m = str(m or "").upper().replace("-", "").replace(" ", "")
        if m.startswith("FC"):
            return ""                            # a floating column is not on the foundation sheet
        k = re.fullmatch(r"C?(\d{1,3}[A-Z]?)", m)
        return k.group(1) if k else ""
    on_f, on_p = defaultdict(list), defaultdict(list)
    for e in b.elements:
        if e.sheet == fsheet and e.type == "footing":
            for m, xy in (e.extra.get("column_at") or {}).items():
                if key(m):
                    on_f[key(m)].append(tuple(xy))
        elif e.sheet == plan_sid and e.type == "column" and e.geometry.get("x") is not None:
            k = key(e.final("mark") or e.mark)
            if k:
                on_p[k].append((e.geometry["x"], e.geometry["y"]))
    # each number once on each sheet, or it says nothing
    pairs = [(on_f[k][0], on_p[k][0]) for k in on_f if len(on_f[k]) == 1 and len(on_p.get(k, [])) == 1]
    if len(pairs) < 3:
        return None
    n = len(pairs)
    px, py = sum(p[0] for p, _ in pairs) / n, sum(p[1] for p, _ in pairs) / n
    qx, qy = sum(q[0] for _, q in pairs) / n, sum(q[1] for _, q in pairs) / n
    sxx = sum((p[0] - px) * (q[0] - qx) + (p[1] - py) * (q[1] - qy) for p, q in pairs)
    sxy = sum((p[0] - px) * (q[1] - qy) - (p[1] - py) * (q[0] - qx) for p, q in pairs)
    a = math.atan2(sxy, sxx)
    c, s = math.cos(a), math.sin(a)
    dx, dy = qx - (c * px - s * py), qy - (s * px + c * py)
    miss = max(math.hypot(c * p[0] - s * p[1] + dx - q[0], s * p[0] + c * p[1] + dy - q[1]) for p, q in pairs)
    if miss > tol:
        return None
    return (c, s, dx, dy), math.degrees(a), n, miss


def _shift_by_ends(src, dst, tol: float = 300.0):
    """The offset laying footing centres (src) on a plan's columns (dst) when the plan is
    a regular grid: a shift of one bay fits almost as well, but always leaves a whole
    column line at one end without a footing - the right one reaches both ends. Believed
    when 3/4 of the plan's columns get a footing and it beats every other offset by two
    columns or more (NIFT: 37 of 44 against 33). ((dx, dy), columns hit) or None."""
    if not src or not dst:
        return None
    votes: dict[tuple[int, int], int] = defaultdict(int)
    for p in src:
        for q in dst:
            votes[(round((q[0] - p[0]) / 100.0), round((q[1] - p[1]) / 100.0))] += 1
    tried = []
    for (i, j), _n in sorted(votes.items(), key=lambda kv: -kv[1])[:12]:
        dx, dy = i * 100.0, j * 100.0
        hit = sum(1 for q in dst if any(abs(p[0] + dx - q[0]) <= tol and abs(p[1] + dy - q[1]) <= tol for p in src))
        tried.append(((dx, dy), hit))
    tried.sort(key=lambda t: -t[1])
    best = tried[0]
    rival = next((t[1] for t in tried[1:] if abs(t[0][0] - best[0][0]) > 2 * tol
                  or abs(t[0][1] - best[0][1]) > 2 * tol), 0)
    if best[1] < max(6, 0.75 * len(dst)) or best[1] - rival < 2:
        return None
    (dx, dy) = best[0]
    offs = []
    for q in dst:
        p = min(src, key=lambda p: abs(p[0] + dx - q[0]) + abs(p[1] + dy - q[1]))
        if abs(p[0] + dx - q[0]) <= tol and abs(p[1] + dy - q[1]) <= tol:
            offs.append((q[0] - p[0], q[1] - p[1]))
    mid = (sorted(o[0] for o in offs)[len(offs) // 2], sorted(o[1] for o in offs)[len(offs) // 2])
    return mid, best[1]


def foundations_for(b: Building, set_key: str, unfounded: bool = False, plan_sid: str | None = None) -> dict | None:
    """Footings, their steps and rafts of one drawing set, in the frame of its lowest plan.

    Placed only where the foundation sheet's offset from that plan is known: the
    column layout's shift (the foundation layout and the column layout are one
    drawing), else the shift that lays the footings' centres on the plan's
    columns. Otherwise nothing is placed and the reason is given. Sizes are the
    resolved ones only (read or decided): a footing in conflict is listed, not built.
    So is one with no founding level - unless `unfounded` (the 3D view, which shows
    it hanging from the level, marked missing): then its bottom_mm is None.

    The lowest plan is tried first, as always. Only when no foundation sheet lines up
    with it are the other plans tried, and then the grid's ends (_shift_by_ends) -
    NIFT: the lowest plan is a strip of two rows, the footings a full three-row grid.
    plan_sid: this plan's frame only (the 3D view, when the lowest plan is not shown).
    """
    from building.linker import _shift
    from building.sheets.levels import level_rank
    plans = [(sid, s) for sid, s in b.sheets.items() if s.reader == "plan.legacy" and s.extra.get("set") == set_key
             and level_rank(s.extra.get("level_key") or "") is not None]
    found = [e for e in b.elements if e.type in ("footing", "raft", "pedestal")
             and b.sheets.get(e.sheet) and b.sheets[e.sheet].extra.get("set") == set_key]
    if plan_sid:
        plans = [p for p in plans if p[0] == plan_sid]
    if not plans or not found:
        return None
    plans.sort(key=lambda p: (level_rank(p[1].extra["level_key"]), p[0]))
    fsheets = sorted({e.sheet for e in found})

    def line_up(sid, s, ends=False):
        """{sheet: shift or None}, {sheet: fit}, how - the foundation sheets on this plan."""
        shifts, fits, how_all = {}, {}, ""
        cols = [(e.geometry["x"], e.geometry["y"]) for e in b.elements
                if e.sheet == sid and e.type == "column" and e.geometry.get("x") is not None]
        for fs in fsheets:
            shift = None
            lay = s.extra.get("column_layout_shift")
            centres = [(e.geometry["x"], e.geometry["y"]) for e in found
                       if e.sheet == fs and e.type == "footing" and e.geometry.get("x") is not None]
            if ends:
                got = _shift_by_ends(centres, cols) if cols and centres else None
                if got:
                    shift, how = got[0], (f"footing grid laid on the {s.extra.get('level_key')} plan's columns, "
                                          f"both ends meeting ({got[1]} of {len(cols)} columns)")
            elif lay and any(p.sheet.split(":")[0] == fs.split(":")[0] for p in b.of("column_position")):
                # the plan + shift is the column layout's frame, which the foundation layout shares
                shift, how = (-lay[0], -lay[1]), f"column layout shift {lay[0]:.0f}, {lay[1]:.0f} ({lay[2]} columns)"
            else:
                got = _shift(centres, cols) if cols and centres else None
                if got:
                    (dx, dy), hits = got
                    shift, how = (dx, dy), f"footing centres laid on the plan's columns ({hits} matched)"
                else:
                    got = _shift_by_marks(b, fs, sid)
                    if got:
                        shift, how = got[0], f"column marks written on both sheets ({got[1]} marks agree)"
                    else:
                        fit = _fit_by_columns(b, fs, sid)
                        if fit:
                            # drawn at another angle: turned and shifted onto the plan
                            fits[fs] = fit
                            shift, how = (0.0, 0.0), (f"turned {fit[1]:+.2f} degrees and shifted onto the plan by "
                                                      f"{fit[2]} columns numbered on both (worst {fit[3]:.0f} mm)")
            shifts[fs] = shift
            if shift:
                how_all += f"{fs}: {how}. "
        return shifts, fits, how_all, cols

    sid, s = plans[0]
    shifts, fits, how_all, cols = line_up(sid, s)
    if not any(v is not None for v in shifts.values()):
        # nothing lines up with the lowest plan: the other plans, then the grid's ends
        for ends in (False, True):
            for psid, ps in (plans[1:] if not ends else plans):
                got = line_up(psid, ps, ends)
                if any(v is not None for v in got[0].values()):
                    sid, s = psid, ps
                    shifts, fits, how_all, cols = got
                    break
            else:
                continue
            break
    out = {"plan": sid, "level_name": s.level, "level_key": s.extra.get("level_key"),
           "base_point": list(s.base_point), "items": [], "not_placed": [], "how": how_all,
           # each foundation sheet's offset onto this plan (turned sheets left out)
           "sheet_shifts": {fs: list(v) for fs, v in shifts.items() if v is not None and fs not in fits}}

    def move(sheet, x, y):
        """A point of a foundation sheet in the plan's frame: shifted, or turned and shifted."""
        if sheet in fits:
            (c, s_, dx, dy) = fits[sheet][0]
            return c * x - s_ * y + dx, s_ * x + c * y + dy
        sh = shifts[sheet]
        return x + sh[0], y + sh[1]
    pedestals = []
    for e in found:
        shift = shifts.get(e.sheet)
        mark = e.final("mark") or e.mark or ""
        what = f"{e.type} {mark}".strip()
        if shift is None:
            out["not_placed"].append(f"{what}: its sheet is not lined up with the {s.extra.get('level_key')} plan")
            continue
        if e.type == "pedestal":
            pedestals.append((e, shift, what))
            continue
        outline = e.geometry.get("outline") or []
        # its bottom: the founding level read or decided for it - never assumed
        bottom = _settled(e, "founding_level")
        waive = unfounded and bottom is None         # shown, never built
        if e.type == "raft":
            t = _settled(e, "thickness")
            if not t or len(outline) < 3 or (bottom is None and not waive):
                out["not_placed"].append(f"{what}: no thickness read" if not t else f"{what}: no outline"
                                         if len(outline) < 3 else f"{what}: founding level {_why(e, 'founding_level')}")
                continue
            out["items"].append({"type": "raft", "id": e.id, "mark": mark, "thickness": float(t),
                                 "bottom_mm": None if waive else float(bottom),
                                 "outline": [list(move(e.sheet, p[0], p[1])) for p in outline]})
            continue
        w, ln, d = _settled(e, "width"), _settled(e, "length"), _settled(e, "depth")
        if not (w and ln and d) or e.geometry.get("x") is None or (bottom is None and not waive):
            bad = [f"{p.replace('_', ' ')} {_why(e, p)}" for p in ("width", "length", "depth", "founding_level")
                   if _settled(e, p) is None]
            out["not_placed"].append(f"{what}: " + "; ".join(bad or ["no position"]))
            continue
        xs = [p[0] for p in outline] or [0.0]
        ys = [p[1] for p in outline] or [0.0]
        lo, hi = sorted((float(w), float(ln)))
        sx, sy = (hi, lo) if (max(xs) - min(xs)) > (max(ys) - min(ys)) else (lo, hi)
        # "D (STEP-1+STEP-2)" = 450+300: a 450 slab with a 300 step on it; 750+0: one 750 slab
        depths = _two(e.final("step_depths")) or [float(d)]
        cx, cy = move(e.sheet, e.geometry["x"], e.geometry["y"])
        turn = fits[e.sheet][1] if e.sheet in fits else 0.0
        f = {"type": "footing", "id": e.id, "mark": mark, "x": cx, "y": cy, "size_x": sx, "size_y": sy,
             "thickness": depths[0], "bottom_mm": None if waive else float(bottom),
             "columns": e.get("columns") or [],
             # a footing with no column of the plan above it (a wall footing, a lift pit) is
             # still built, but worth a look
             "under_plan_column": any(abs(x - cx) <= sx / 2 and abs(y - cy) <= sy / 2 for x, y in cols)}
        if e.get("pcc_offset") or e.final("pcc_size"):
            f["pcc"] = e.get("pcc_offset") or e.final("pcc_size")
        if turn:
            f["rotation"] = turn                 # degrees: its sheet is drawn turned against the plan
        out["items"].append(f)
        steps = e.get("steps") or []
        if len(depths) > 1 and depths[1]:
            if steps:
                a, c = sorted(steps[0])
                tx, ty = (c, a) if sx >= sy else (a, c)
                out["items"].append({"type": "step", "id": e.id, "mark": mark, "x": cx, "y": cy, "size_x": float(tx),
                                     "size_y": float(ty), "thickness": depths[1], "on": mark,
                                     **({"rotation": turn} if turn else {})})
            else:
                out["not_placed"].append(f"{what}: step {depths[1]:.0f} deep but its plan size is not drawn")
    # a pedestal stands on the footing it is drawn on, up to the plan's level
    for e, shift, what in pedestals:
        x, y = move(e.sheet, e.geometry["x"], e.geometry["y"])
        w, ln = _settled(e, "width"), _settled(e, "length")
        under = [f for f in out["items"] if f["type"] == "footing"
                 and abs(f["x"] - x) <= f["size_x"] / 2 and abs(f["y"] - y) <= f["size_y"] / 2]
        if not (w and ln):
            out["not_placed"].append(f"{what}: no size read")
        elif not under:
            out["not_placed"].append(f"{what}: not standing on a placed footing (drawn as a detail?)")
        else:
            out["items"].append({"type": "pedestal", "mark": e.final("mark") or e.mark or "", "x": x, "y": y,
                                 "size_x": float(w), "size_y": float(ln), "on": under[0]["mark"],
                                 "on_xy": [under[0]["x"], under[0]["y"]]})
    return out


def stairs_for(b: Building, sid: str) -> dict | None:
    """The stairs drawn on one plan, flight by flight, in the plan's own frame."""
    s = b.sheets[sid]
    view = s.extra.get("view_sheet")
    st = [e for e in b.of("stair") if e.sheet in (sid, view)]
    ramps = [e for e in b.of("ramp") if e.sheet in (sid, view)]
    if not st and not ramps:
        return None
    out = {"plan": sid, "level_name": s.level, "level_key": s.extra.get("level_key"),
           "base_point": list(s.base_point), "stairs": [], "not_built": []}
    for e in st:
        flights = e.geometry.get("flights") or []
        riser = _settled(e, "riser")
        going = _settled(e, "going") or e.get("going")
        width = _settled(e, "width") or e.get("width")
        runs = []
        for f in flights:
            (a, c), (p, q) = f["first"], f["last"]
            m0 = [(a[0] + c[0]) / 2, (a[1] + c[1]) / 2]
            m1 = [(p[0] + q[0]) / 2, (p[1] + q[1]) / 2]
            runs.append({"start": m0, "end": m1, "treads": f["treads"], "width": f["width"], "going": f["going"]})
        if not runs:
            out["not_built"].append(f"stair at ({e.geometry.get('x', 0):.0f}, {e.geometry.get('y', 0):.0f}): no flights")
            continue
        out["stairs"].append({"runs": runs, "riser": riser, "going": going, "width": width,
                              "updn": e.final("direction_text") or [],
                              "landing_levels": e.final("landing_levels") or []})
    for r in ramps:
        out["not_built"].append(f"ramp {r.get('width') or '?'} x {r.get('length') or '?'}: where it starts and "
                                f"ends, and its slope, need a decision - listed only")
    return out


def level_table(b: Building, decisions: dict | None = None) -> list[dict]:
    """levels.json: every level of the drawings with its elevation, read or decided.

    A floor elevation decided for the whole project (decisions "levels", by Revit level
    name) fills a level the drawings give none for; a level decided that the drawings
    do not name at all (the one the lowest floor's columns rise from) is added. A
    level whose elevation is neither read nor decided stays "missing": the Revit
    build stops for it rather than assume a storey height."""
    names = {(s.extra.get("set"), s.extra.get("level_key")): s.level for s in b.sheets.values()
             if s.reader == "plan.legacy"}
    levels = [{"set": e.extra.get("set"), "level": e.level,
               "revit_name": names.get((e.extra.get("set"), e.level)) or e.level,
               "elevation_mm": e.final("elevation"),
               "status": e.resolved["elevation"].status if e.resolved.get("elevation") else "missing",
               "height_below_mm": e.final("height_below"), "height_notes_mm": e.final("height_note")}
              for e in b.of("level")]
    decided = {str(k).strip(): v for k, v in ((decisions or {}).get("levels") or {}).items() if v not in (None, "")}
    for lv in levels:
        v = decided.get((lv["revit_name"] or "").strip())
        if v is not None and lv["status"] not in (OVERRIDE,):
            if lv["status"] == READ and abs(float(lv["elevation_mm"]) - float(v)) > 1.0:
                lv["drawings_say_mm"] = lv["elevation_mm"]      # the decision wins, the reading is kept
            lv["elevation_mm"], lv["status"], lv["source"] = float(v), OVERRIDE, "project decision"
    have = {(lv["revit_name"] or "").strip() for lv in levels}
    for name, v in decided.items():
        if name not in have:
            levels.append({"set": "", "level": "", "revit_name": name, "elevation_mm": float(v),
                           "status": OVERRIDE, "source": "project decision"})
    return levels


def _elevation_of(levels: list[dict], name: str):
    for lv in levels:
        if (lv.get("revit_name") or "").strip().upper() == (name or "").strip().upper() \
                and lv.get("status") in USABLE and lv.get("elevation_mm") is not None:
            return float(lv["elevation_mm"])
    return None


def _one_origin(b: Building, plans, decisions, log) -> dict:
    """Every floor of a drawing set is built from one origin (external review, point I).
    Each plan's own base point is the south-west corner of ITS columns, so a floor with a
    column more or less landed shifted against the others. A decided "origin" [x, y]
    (project decision, drawing units) is used for every floor; else, where a set's floors
    differ, the most south-westerly of them. A set whose floors agree - or a single floor -
    is left exactly as it was. Applied to this export only (the building is not saved)."""
    fixed = (decisions or {}).get("origin")
    sets = defaultdict(list)
    for sid in plans:
        sets[b.sheets[sid].extra.get("set", "")].append(sid)
    used = {}
    for key, sids in sets.items():
        bps = [tuple(b.sheets[s].base_point or (0.0, 0.0)) for s in sids]
        if isinstance(fixed, (list, tuple)) and len(fixed) >= 2:
            o, how = (float(fixed[0]), float(fixed[1])), "decided"
        elif len(set(bps)) > 1:
            o = (min(p[0] for p in bps), min(p[1] for p in bps))
            how = "floors' own base points differ: the most south-westerly, for every floor - decide one if the drawings share a grid point"
            log(f"  {key or 'drawings'}: one origin ({o[0]:.0f}, {o[1]:.0f}) for {len(sids)} floors - their own base points differ")
        else:
            o = None
        if o is not None:
            for s in sids:
                b.sheets[s].base_point = [o[0], o[1]]
            used[key or "-"] = {"origin": [o[0], o[1]], "how": how, "floors": len(sids)}
        moved = _stack_drawn_apart(b, key, sids, log)
        if moved:
            used.setdefault(key or "-", {})["stacked"] = moved
    return used


def _stack_drawn_apart(b: Building, set_key: str, sids, log) -> dict:
    """Floors drawn apart are stacked - five floors side by side in one drawing (Aldott,
    28 m apart), or each floor in its own file at its own place in CAD space (NIFT: one
    file 700 m from the next). One origin would build them where they are drawn,
    scattered. Each such plan is moved by the same answer the 3D view uses
    (viewer3d.stack: the columns, which stand in the same places on every floor, and
    the foundation sheet where a floor's columns alone do not tell). A plan whose
    columns overlap the frame floor's is drawn in place (J1153): untouched, as before;
    one that lines up with nothing stays at the origin - nothing is guessed."""
    from building.sheets.levels import level_rank
    from building.viewer3d import stack
    plans = [(s, b.sheets[s]) for s in sids if level_rank(b.sheets[s].extra.get("level_key") or "") is not None]
    plans.sort(key=lambda p: (level_rank(p[1].extra["level_key"]), p[0]))
    seen, uniq = set(), []
    for sid, s in plans:
        if s.extra["level_key"] not in seen:          # a level drawn twice (wings): left as it is
            seen.add(s.extra["level_key"])
            uniq.append((sid, s))
    if len(uniq) < 2:
        return {}
    shifts, _fd = stack(b, set_key, uniq)
    frame = next((sid for sid, _ in uniq if shifts.get(sid) == (0.0, 0.0)), None)
    if frame is None:
        return {}
    cols = {sid: [(e.geometry["x"], e.geometry["y"]) for e in b.elements if e.sheet == sid
                  and e.type == "column" and e.geometry.get("x") is not None] for sid, _ in uniq}

    def box(pts):
        return (min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts))
    if not cols[frame]:
        return {}
    rb = box(cols[frame])
    moved = {}
    for s, _ps in uniq:
        if s == frame or not cols[s]:
            continue
        sb = box(cols[s])
        if sb[0] <= rb[2] and rb[0] <= sb[2] and sb[1] <= rb[3] and rb[1] <= sb[3]:
            continue                                  # drawn in place over the frame floor
        d = shifts.get(s)
        if d is None:
            log(f"  {s}: drawn apart from {frame}, but nothing tells where it stands on it - "
                "left at the drawing set's origin")
            continue
        # the vote is in 100 mm steps: the middle of the columns' own offsets where they meet
        offs = []
        for p in cols[s]:
            q = min(cols[frame], key=lambda q: abs(p[0] + d[0] - q[0]) + abs(p[1] + d[1] - q[1]))
            if abs(p[0] + d[0] - q[0]) <= 200.0 and abs(p[1] + d[1] - q[1]) <= 200.0:
                offs.append((q[0] - p[0], q[1] - p[1]))
        if len(offs) >= 3:
            d = (sorted(o[0] for o in offs)[len(offs) // 2], sorted(o[1] for o in offs)[len(offs) // 2])
        bp = b.sheets[frame].base_point
        b.sheets[s].base_point = [bp[0] - d[0], bp[1] - d[1]]
        moved[s] = {"onto": frame, "by": [round(d[0], 1), round(d[1], 1)], "how": "columns (as the 3D view)"}
        log(f"  {s}: drawn apart from {frame} - stacked on it, moved {d[0]:.0f}, {d[1]:.0f} mm")
    return moved


def export(run: str, out: str | None = None, overrides: str | None = None, log=print,
           decisions: dict | None = None) -> str:
    """decisions: the project decisions (overrides.json "project"); read from
    `overrides` when that is given."""
    b = Building.load(os.path.join(run, "building.json"))
    if overrides:
        from building import profiles
        from building.linker import link
        ov = profiles.load(overrides)
        link(b, ov)
        decisions = decisions or ov.get("project")
    out = out or os.path.join(run, "revit_build")
    os.makedirs(out, exist_ok=True)
    levels = level_table(b, decisions)
    plans = [sid for sid, s in b.sheets.items() if s.reader == "plan.legacy"]
    summary = []
    # the pre-build check (check.json): ready to build / needs a decision / not supported
    check = {"ready": {}, "needs_decision": [], "not_supported": [], "founding_missing": 0,
             "blocked_drawings": [i.message for i in b.issues if i.severity == "blocker" and i.category == "units"]}

    def ready(kind, n):
        if n:
            check["ready"][kind] = check["ready"].get(kind, 0) + n
    # build what is unsettled too, at a working size and marked - unless the project
    # says to hold it back until decided ("build_unsettled": false)
    provisional = (decisions or {}).get("build_unsettled", True) is not False
    check["origin"] = _one_origin(b, plans, decisions, log)
    for sid in plans:
        d, rep = export_plan(b, sid, provisional, decisions)
        # the build may give a column or wall with no level to reach the storey height,
        # marked like the rest; held back when the project holds the unsettled
        d["provisional_ok"] = provisional
        folder = os.path.join(out, _safe(sid))
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "model.json"), "w", encoding="utf-8") as f:
            json.dump(d, f, indent=1)
        lines = [f"Plan: {sid}", f"Level: {d.get('level_name') or '(from drawing)'}",
                 f"Builds: {len(d['columns'])} columns, {len(d['beams'])} beams, {len(d['slabs'])} slabs, "
                 f"{len(d['walls'])} walls, {len(d.get('folds', []))} folds", ""]
        for title, rows in (("NEEDS A DECISION - BUILT AT A WORKING SIZE, coloured red in Revit with this note "
                             "in its Comments, until it is settled in the review", rep.provisional),
                            ("NEEDS A DECISION - NOT BUILT until it is settled in the review", rep.held),
                            ("CHANGED FROM THE DRAWING'S OWN VALUES", rep.changed),
                            ("ADDED / RESHAPED", rep.added),
                            ("MARK KEPT AS DRAWN (sources disagree - decide in the review)", rep.kept),
                            ("LEFT OUT", rep.left_out)):
            lines += [f"{title}: {len(rows)}"] + [f"  {r}" for r in rows] + [""]
        with open(os.path.join(folder, "export_report.txt"), "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        sd = stairs_for(b, sid)
        sfile = os.path.join(folder, "stairs.json")
        if sd and sd["stairs"] and not (decisions or {}).get("model_stairs"):
            # read, not modelled until the project switches stairs on (user 2026-10-10:
            # "no need staircase structure model now"); an earlier export's file goes too
            check["not_supported"].append({"where": d.get("level_name") or sid, "what":
                f"{len(sd['stairs'])} stair(s) read but not modelled - stairs are switched off for now"})
            sd = dict(sd, stairs=[])
        if os.path.exists(sfile) and not (sd and sd["stairs"]):
            os.remove(sfile)
        if sd and sd["stairs"]:
            with open(sfile, "w", encoding="utf-8") as f:
                json.dump(sd, f, indent=1)
        label = d.get("level_name") or sid
        for k in ("columns", "beams", "slabs", "walls"):
            ready(k, len(d.get(k) or []))
        ready("hidden beams", len(d.get("folds") or []))
        ready("stairs", len((sd or {}).get("stairs") or []))
        check["needs_decision"] += [{"where": label, "what": h} for h in rep.held]
        check["needs_decision"] += [{"where": label, "what": h, "built": True} for h in rep.provisional]
        # slabs no drawing gives a thickness at all: the Export page asks for one value
        check["slab_thickness_missing"] = check.get("slab_thickness_missing", 0) + sum(
            1 for h in rep.held if h.startswith("slab") and "thickness" in h)
        check["not_supported"] += [{"where": label, "what": x} for x in (sd or {}).get("not_built") or []]
        summary.append((sid, d, rep))
        log(f"  {sid}: {len(d['columns'])} columns, {len(d['beams'])} beams, {len(d['slabs'])} slabs, "
            f"{len(d['walls'])} walls | {len(rep.provisional)} built at a working size (marked), "
            f"{len(rep.held)} need a decision (not built), {len(rep.changed)} values "
            f"settled by review, {len(rep.left_out)} left out")
    for set_key in sorted({s.extra.get("set", "") for s in b.sheets.values() if s.extra.get("set")}):
        fd = foundations_for(b, set_key)
        if not fd:
            # footings read but nothing to stand them under: said, never dropped silently
            n = sum(1 for e in b.elements if e.type in ("footing", "raft", "pedestal")
                    and b.sheets.get(e.sheet) and b.sheets[e.sheet].extra.get("set") == set_key)
            if n:
                check["not_supported"].append({"where": "foundations", "what":
                    f"{n} footings/rafts read, but no floor plan is imported with them. Footings are placed under "
                    f"the lowest floor plan (its position and level); import that plan with the foundation drawing."})
                log(f"  {set_key}: {n} footings/rafts read, not placed - no floor plan to place them under")
            continue
        # founding levels are against the drawings' datum: the build places them from
        # the elevation the drawings (or a decision) give the level they hang from
        fd["level_elevation_mm"] = _elevation_of(levels, fd["level_name"] or "")
        with open(os.path.join(out, _safe(fd["plan"]), "foundations.json"), "w", encoding="utf-8") as f:
            json.dump(fd, f, indent=1)
        kinds = {}
        for it in fd["items"]:
            kinds[it["type"]] = kinds.get(it["type"], 0) + 1
            ready(it["type"] + "s", 1)
        unaligned = [x for x in fd["not_placed"] if "is not lined up" in x]
        if unaligned:
            # one plain line, not one per footing
            check["not_supported"].append({"where": "foundations", "what":
                f"{len(unaligned)} footings: the foundation drawing does not line up with the "
                f"{fd.get('level_name') or 'lowest'} plan (no shared column layout, and its columns do not fall on "
                f"the plan's columns - drawn at another angle or another place?), so they are not placed."})
        for x in fd["not_placed"]:
            if "is not lined up" in x:
                continue
            # a value to settle is a decision; a sheet the app cannot line up, or a
            # pedestal drawn only as a detail, is beyond what it can place today
            if any(w in x for w in ("founding level", "conflict", "missing", "no size", "not drawn",
                                    "no thickness", "no source")):
                check["needs_decision"].append({"where": "foundations", "what": x})
            else:
                check["not_supported"].append({"where": "foundations", "what": x})
            check["founding_missing"] += "founding level" in x
        if fd["items"] and fd["level_elevation_mm"] is None:
            check["needs_decision"].append({"where": "foundations", "what": f"level '{fd['level_name']}': no "
                                            f"elevation, so the footings' founding levels cannot be measured from it"})
        log(f"  {set_key}: foundations under {fd['plan']} - " + (", ".join(f"{v} {k}s" for k, v in kinds.items())
                                                                or "nothing placeable")
            + f"; {len(fd['not_placed'])} listed, not placed")
    with open(os.path.join(out, "levels.json"), "w", encoding="utf-8") as f:
        json.dump(levels, f, indent=1)
    with open(os.path.join(out, "check.json"), "w", encoding="utf-8") as f:
        json.dump(check, f, indent=1)
    with open(os.path.join(out, "README.txt"), "w", encoding="utf-8") as f:
        f.write("One folder per plan. In Revit, run revit\\build_model.py and pick that folder's model.json,\n"
                "as with an extractor model.json. Read export_report.txt first: it lists every value the review\n"
                "settled, what was kept as drawn, and what is not built yet.\n"
                "levels.json: elevation of each level where the drawings give one or a person decided one\n"
                "(else 'missing' - the build stops rather than assume a storey height).\n"
                "foundations.json: each footing's bottom (bottom_mm) is its founding level, read or decided;\n"
                "a footing with none is listed under not_placed, never built at an assumed depth.\n")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run")
    ap.add_argument("out", nargs="?")
    ap.add_argument("--overrides")
    a = ap.parse_args(argv)
    out = export(a.run, a.out, a.overrides)
    print(f"Wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
