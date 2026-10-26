r"""The reviewable Excel: everything the tool decided, and why.

    python -m building.excel OUT\building.json [review.xlsx]

One row per element (beams split into their marked spans), with each value's
status, confidence and every source that gave it - so a person can see at a
glance what is certain, what disagrees and what is missing, and trace any value
back to the drawing. Also: the issues, what every sheet was taken for, and what
the tool can read versus build.

The "Overrides" sheet is for decisions. Fill a row - an element id or a mark
(MARK, MARK@WING or MARK@WING/LEVEL), a property and its value - and the next run
applies it (python -m building.excel --overrides review.xlsx overrides.json, then
readset --overrides overrides.json). A decision is applied as OVERRIDE and always
wins; the sources it overrides stay listed.
"""
from __future__ import annotations

import json
import math
import os
import sys
from collections import Counter, defaultdict

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from building import Building, SUPPORT, support_of     # noqa: E402
from building.sheets.levels import level_rank           # noqa: E402

FONT = "Arial"
HEAD = PatternFill("solid", fgColor="16202B")
FILL = {
    "read": None,
    "low": PatternFill("solid", fgColor="FDEBD3"),        # read, but not sure
    "default": PatternFill("solid", fgColor="E3EEF9"),
    "missing": PatternFill("solid", fgColor="FFF3B0"),
    "conflict": PatternFill("solid", fgColor="F8C9C4"),
    "override": PatternFill("solid", fgColor="E4DDF4"),
}
THIN = Side(style="thin", color="D5D2CA")
BOX = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
LOW = 0.6


def _status_key(v) -> str:
    if v is None:
        return "missing"
    if v.status == "read" and v.confidence < LOW:
        return "low"
    return v.status


def _sources(v) -> str:
    if v is None:
        return ""
    seen, out = set(), []
    for ev in v.evidence:
        s = ev.source or ev.reader
        if s not in seen:
            seen.add(s)
            out.append(s)
    return "; ".join(out)


def _head(ws, row, cols, widths):
    for i, (h, w) in enumerate(zip(cols, widths), start=1):
        c = ws.cell(row=row, column=i, value=h)
        c.font = Font(name=FONT, size=10, bold=True, color="F7F6F2")
        c.fill = HEAD
        c.alignment = Alignment(wrap_text=True, vertical="center")
        c.border = BOX
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.row_dimensions[row].height = 30
    ws.freeze_panes = ws.cell(row=row + 1, column=4)
    ws.auto_filter.ref = f"A{row}:{get_column_letter(len(cols))}{row}"


def _put(ws, row, col, value, fill=None, bold=False):
    c = ws.cell(row=row, column=col, value=value)
    c.font = Font(name=FONT, size=9, bold=bold)
    c.border = BOX
    if fill is not None:
        c.fill = fill
    return c


def _title(ws, text, note):
    ws.cell(row=1, column=1, value=text).font = Font(name=FONT, size=13, bold=True)
    ws.cell(row=2, column=1, value=note).font = Font(name=FONT, size=9, italic=True, color="5F6C77")


def _level(b, e):
    s = b.sheets.get(e.sheet)
    return (s.extra.get("level_key") if s else "") or e.level


def _value_cols(ws, row, col, e, prop):
    v = e.resolved.get(prop)
    key = _status_key(v)
    val = v.value if v is not None else None
    if isinstance(val, float):
        val = round(val, 1)
    _put(ws, row, col, val, FILL.get(key))
    _put(ws, row, col + 1, {"low": "read (low)"}.get(key, key), FILL.get(key))
    _put(ws, row, col + 2, _sources(v))
    return col + 3


def _length(e) -> float | None:
    g = e.geometry
    if g.get("start") and g.get("end"):
        return round(math.hypot(g["end"][0] - g["start"][0], g["end"][1] - g["start"][1]) / 1000.0, 2)
    return None


def write(b: Building, path: str) -> str:
    wb = Workbook()
    wb.remove(wb.active)
    modelled = [e for e in b.elements if e.type in ("beam", "beam_span", "column", "slab", "wall",
                                                    "footing", "raft", "pedestal", "stair", "ramp")
                and not e.extra.get("split_into") and not e.extra.get("merged_into")]

    # ---- Read Me
    ws = wb.create_sheet("Read Me")
    ws.column_dimensions["A"].width = 22
    ws.column_dimensions["B"].width = 100
    lines = [("REVIEW WORKBOOK", ""), ("Project", b.project or "-"),
             ("Levels", ", ".join(b.levels)), ("", ""),
             ("What it is", "Every element the tool read, with each value's status and every source "
                            "that gave it. Nothing here is a guess: a value no source gives is MISSING."),
             ("", ""), ("COLOURS", "")]
    for r, (a, t) in enumerate(lines, start=1):
        ws.cell(row=r, column=1, value=a).font = Font(name=FONT, size=10, bold=True)
        ws.cell(row=r, column=2, value=t).font = Font(name=FONT, size=10)
    r = len(lines) + 1
    for key, text in (("read", "read from the drawing; the sources agree"),
                      ("low", "read, but only from weak hints that disagree - check"),
                      ("conflict", "sources disagree - the strongest is shown, all are listed; decide"),
                      ("missing", "no source gives it - needs a value"),
                      ("override", "a decision from the Overrides sheet")):
        c = ws.cell(row=r, column=1, value=key)
        c.font = Font(name=FONT, size=10)
        if FILL[key] is not None:
            c.fill = FILL[key]
        ws.cell(row=r, column=2, value=text).font = Font(name=FONT, size=10)
        r += 1
    r += 1
    ws.cell(row=r, column=1, value="SHEETS").font = Font(name=FONT, size=10, bold=True)
    for name, text in (("Summary", "counts per level: how much is read, in conflict, missing"),
                       ("Beams / Columns / Slabs / Walls", "one row per element; beams split into their marked spans"),
                       ("Stairs & ramps", "flights, going, riser and landing levels; ramps (read only, built later)"),
                       ("Levels", "each level's elevation and floor-to-floor height, from sections and notes"),
                       ("Marks", "one row per mark per level: every size it was given"),
                       ("Issues", "conflicts, missing values, ambiguous captions, guessed span cuts"),
                       ("Sheets", "what every view in the drawings was taken for, and why"),
                       ("Support", "what the tool can read, and what it can build in Revit"),
                       ("Overrides", "your decisions - applied on the next run")):
        r += 1
        ws.cell(row=r, column=1, value=name).font = Font(name=FONT, size=10)
        ws.cell(row=r, column=2, value=text).font = Font(name=FONT, size=10)

    # ---- Summary
    ws = wb.create_sheet("Summary")
    _title(ws, "SUMMARY", "Per level and element type: how many of each status, per property.")
    cols = ["Level", "Type", "Elements", "Property", "read", "read (low)", "conflict", "missing", "override"]
    _head(ws, 4, cols, [10, 11, 10, 12, 8, 10, 9, 9, 9])
    row = 5
    groups = defaultdict(list)
    for e in modelled:
        groups[(_level(b, e), e.type)].append(e)
    for (lv, t), es in sorted(groups.items()):
        props = {"slab": ("mark", "thickness"), "wall": ("mark", "width"),
                 "footing": ("mark", "width", "length", "depth"), "raft": ("thickness",),
                 "pedestal": ("mark",), "stair": ("going", "riser", "width"),
                 "ramp": ("width", "length")}.get(t, ("mark", "width", "depth"))
        for p in props:
            c = Counter(_status_key(e.resolved.get(p)) for e in es)
            vals = [lv, t, len(es), p, c["read"], c["low"], c["conflict"], c["missing"], c["override"]]
            for i, v in enumerate(vals, start=1):
                fill = None
                if i == 7 and v:
                    fill = FILL["conflict"]
                elif i == 8 and v:
                    fill = FILL["missing"]
                _put(ws, row, i, v, fill)
            row += 1

    # ---- element sheets
    def element_sheet(name, types, props, extra_cols):
        ws = wb.create_sheet(name)
        _title(ws, name.upper(), "One row per element. Status colours as in Read Me. 'Sources' lists every "
                                 "source and the value it gave.")
        cols = ["Level", "Wing", "Mark", "Mark status", "Mark sources"]
        widths = [8, 6, 9, 10, 30]
        for p in props:
            cols += [p.title(), f"{p.title()} status", f"{p.title()} sources"]
            widths += [9, 10, 42]
        cols += [c for c, _ in extra_cols] + ["Read", "Model", "Element id", "Sheet"]
        widths += [w for _, w in extra_cols] + [10, 10, 34, 34]
        _head(ws, 4, cols, widths)
        row = 5
        rows = sorted((e for e in modelled if e.type in types),
                      key=lambda e: (_level(b, e), str(e.final("mark") or "~"), e.id))
        for e in rows:
            sup = support_of(e.type)
            col = 1
            for v in (_level(b, e), e.wing):
                _put(ws, row, col, v)
                col += 1
            col = _value_cols(ws, row, col, e, "mark")
            for p in props:
                col = _value_cols(ws, row, col, e, p)
            for c, _w in extra_cols:
                _put(ws, row, col, EXTRA[c](e))
                col += 1
            excluded = e.resolved.get("excluded")
            _put(ws, row, col, sup["read"])
            _put(ws, row, col + 1, "excluded" if excluded else sup["model"],
                 FILL["override"] if excluded else None)
            _put(ws, row, col + 2, e.id)
            _put(ws, row, col + 3, e.sheet)
            row += 1
        return ws

    EXTRA = {
        "Varying depth": lambda e: "/".join(f"{x:.0f}" for x in e.resolved["depth_varies"].value)
        if e.resolved.get("depth_varies") else None,
        "Length (m)": _length,
        "Split": lambda e: (f"span of {e.extra.get('parent', '').rsplit('/', 2)[-1]}; cut: "
                            f"{e.extra.get('cut_before') or '-'} | {e.extra.get('cut_after') or '-'}")
        if e.type == "beam_span" else None,
        "Kind": lambda e: e.get("kind"),
        "Sunk (mm)": lambda e: e.get("sunk_mm") or None,
        "X": lambda e: round(e.geometry.get("x"), 0) if e.geometry.get("x") is not None else None,
        "Y": lambda e: round(e.geometry.get("y"), 0) if e.geometry.get("y") is not None else None,
        "PCC ring (mm)": lambda e: e.get("pcc_offset"),
        "Steps": lambda e: ", ".join(f"{a:.0f}x{c:.0f}" for a, c in (e.final("steps") or e.get("steps") or [])) or None,
        "Columns on it": lambda e: ", ".join(e.get("columns") or []) or None,
        "Stack": lambda e: e.extra.get("stack"),
        "Role": lambda e: e.final("role"),
        "Shape": lambda e: e.final("shape"),
        "Runs": lambda e: "; ".join(f"{r['length']:.0f} x {r['thickness']:.0f}" for r in e.extra.get("runs") or [])
        or None,
        "Flights": lambda e: e.final("flights"),
        "Treads": lambda e: " + ".join(str(n) for n in (e.final("treads") or [])) or None,
        "UP/DN": lambda e: " ".join(e.final("direction_text") or []) or None,
        "Landing levels": lambda e: ", ".join(f"{v:+.0f}" for v in (e.final("landing_levels") or [])) or None,
        "Slope": lambda e: e.final("slope"),
    }
    element_sheet("Beams", ("beam", "beam_span"), ("width", "depth"),
                  [("Varying depth", 10), ("Length (m)", 9), ("Kind", 8), ("Split", 40)])
    element_sheet("Columns", ("column",), ("width", "depth"), [("X", 10), ("Y", 10), ("Stack", 12)])
    element_sheet("Slabs", ("slab",), ("thickness",), [("Sunk (mm)", 9)])
    element_sheet("Walls", ("wall",), ("width",), [("Length (m)", 9), ("Role", 9), ("Shape", 9),
                                                   ("Runs", 30)])
    element_sheet("Footings", ("footing",), ("width", "length", "depth"),
                  [("PCC ring (mm)", 9), ("Steps", 16), ("Columns on it", 18), ("Stack", 12)])
    element_sheet("Rafts & pedestals", ("raft", "pedestal"), ("width", "length", "thickness"), [])
    element_sheet("Stairs & ramps", ("stair", "ramp"), ("going", "riser", "width", "length"),
                  [("Flights", 8), ("Treads", 10), ("UP/DN", 8), ("Landing levels", 22), ("Slope", 8)])

    # ---- Levels: the level table per drawing set
    ws = wb.create_sheet("Levels")
    _title(ws, "LEVELS", "Every level of each drawing set: its elevation as written on sections, elevations "
                         "or notes, and the floor-to-floor height to the level below. A level no drawing gives "
                         "an elevation for is MISSING - the Revit build then keeps the Revit levels.")
    _head(ws, 4, ["Set", "Level", "Elevation (mm)", "Status", "Sources", "Finished level (mm)",
                  "Height to level below (mm)", "Below", "Height notes (mm)", "Markers"],
          [18, 9, 13, 10, 60, 14, 14, 8, 16, 8])
    row = 5
    for e in sorted(b.of("level"), key=lambda e: (e.extra.get("set", ""), level_rank(e.level) or 0)):
        v = e.resolved.get("elevation")
        st = _status_key(v)
        vals = [e.extra.get("set", ""), e.level, v.value if v else None, st,
                "; ".join(f"{ev.source} [{ev.text}]" for ev in (v.evidence if v else []))[:400] or None,
                e.final("elevation_finished"), e.final("height_below"), e.extra.get("below"),
                ", ".join(str(x) for x in (e.final("height_note") or [])) or None, e.extra.get("markers")]
        for i, x in enumerate(vals, start=1):
            _put(ws, row, i, x, FILL.get(st) if i == 4 else None)
        row += 1

    # ---- Marks: one row per mark per level, every size given
    ws = wb.create_sheet("Marks")
    _title(ws, "MARKS", "One row per mark and level: how many elements carry it and every size they were "
                        "given. More than one size for one mark is worth a look.")
    _head(ws, 4, ["Level", "Wing", "Mark", "Type", "Elements", "Sizes (width x depth / thickness)",
                  "Statuses"], [8, 6, 10, 10, 9, 40, 30])
    marks = defaultdict(list)
    for e in modelled:
        m = e.final("mark")
        if m:
            marks[(_level(b, e), e.wing, str(m), "beam" if e.type == "beam_span" else e.type)].append(e)
    row = 5
    for (lv, wing, m, t), es in sorted(marks.items()):
        if t == "slab":
            sizes = Counter(f"{e.final('thickness'):.0f}" if e.final("thickness") else "?" for e in es)
            sts = Counter(_status_key(e.resolved.get("thickness")) for e in es)
        else:
            sizes = Counter(f"{e.final('width') or 0:.0f} x {e.final('depth') or 0:.0f}" for e in es)
            sts = Counter(_status_key(e.resolved.get("depth")) for e in es)
        fill = FILL["conflict"] if len(sizes) > 1 else None
        for i, v in enumerate([lv, wing, m, t, len(es), ", ".join(f"{k} ({n})" for k, n in sizes.most_common()),
                               ", ".join(f"{k} {n}" for k, n in sts.most_common())], start=1):
            _put(ws, row, i, v, fill if i == 6 else None)
        row += 1

    # ---- Issues
    ws = wb.create_sheet("Issues")
    _title(ws, "ISSUES", "Everything that needs a person: conflicts, missing values, ambiguous captions, "
                         "guessed cuts - and the extractor's own queries.")
    _head(ws, 4, ["Severity", "Category", "Mark", "Message", "Sheet", "Elements"], [9, 13, 9, 90, 34, 34])
    row = 5
    order = {"blocker": 0, "warning": 1, "info": 2}
    for i in sorted(b.issues, key=lambda i: (order.get(i.severity, 3), i.category or "", i.sheet or "")):
        fill = FILL["conflict"] if i.category == "conflict" else FILL["missing"] if i.category == "missing" else None
        for c, v in enumerate([i.severity, i.category, i.mark, i.message, i.sheet, ", ".join(i.elements[:4])],
                              start=1):
            _put(ws, row, c, v, fill if c == 2 else None)
        row += 1

    # ---- Sheets
    ws = wb.create_sheet("Sheets")
    _title(ws, "SHEETS", "Every view in the drawings and what it was taken for. A wrong kind or level here "
                         "can be corrected in the Overrides sheet.")
    _head(ws, 4, ["File", "Sheet id (for Overrides)", "Kind", "Confidence", "Title", "Levels",
                  "Level conflict", "Wing", "Why"], [30, 34, 16, 10, 40, 12, 12, 7, 80])
    row = 5
    for sid, s in b.sheets.items():
        if s.reader != "sheets.registry" or s.kind == "stray":
            continue
        k = s.extra.get("kind", {})
        lv = s.extra.get("levels", {})
        for c, v in enumerate([os.path.basename(s.file), sid, s.kind, k.get("confidence"),
                               s.title, ",".join(lv.get("candidates") or []),
                               "YES" if lv.get("conflict") else "", s.wing,
                               " | ".join(k.get("reasons", [])[:4])], start=1):
            _put(ws, row, c, v, FILL["conflict"] if c == 7 and v else None)
        row += 1

    # ---- Support
    ws = wb.create_sheet("Support")
    _title(ws, "SUPPORT", "Reading and modelling are separate: an element the tool can read is not "
                          "automatically one it can build in Revit.")
    _head(ws, 4, ["Element type", "Read", "Model in Revit", "Note"], [14, 12, 15, 80])
    for row, (t, (rd, md, note)) in enumerate(SUPPORT.items(), start=5):
        for c, v in enumerate([t, rd, md, note], start=1):
            _put(ws, row, c, v)

    # ---- Overrides (for the person)
    ws = wb.create_sheet("Overrides")
    _title(ws, "OVERRIDES", "Your decisions. Scope: an element id (from the element sheets), or a mark: B63, "
                            "B63@B (wing B), B63@B/L1 (wing B, level L1). Property: mark, width, depth, "
                            "thickness - or 'exclude' with value YES to leave an element out. To correct a "
                            "sheet: its id from the Sheets tab, property kind or level.")
    _head(ws, 4, ["Scope", "Property", "Value", "Note (why)"], [40, 12, 12, 60])
    for r in range(5, 105):
        for c in range(1, 5):
            _put(ws, r, c, None, PatternFill("solid", fgColor="FFF6D6") if c < 4 else None)

    wb.active = 0
    wb.save(path)
    return path


def overrides_from_excel(xlsx: str) -> dict:
    """The Overrides sheet as the overrides the linker applies."""
    ws = load_workbook(xlsx, data_only=True)["Overrides"]
    out: dict = {"elements": {}, "marks": {}, "exclude": [], "sheets": {}}
    for r in range(5, ws.max_row + 1):
        scope, prop, value = (ws.cell(row=r, column=c).value for c in (1, 2, 3))
        if not scope or not prop:
            continue
        scope, prop = str(scope).strip(), str(prop).strip().lower()
        if prop in ("kind", "level"):
            # a sheet correction: scope is the sheet id from the Sheets tab
            out["sheets"].setdefault(scope, {})[prop] = str(value).strip()
            continue
        if prop == "exclude":
            if str(value).strip().upper() in ("YES", "Y", "TRUE", "1"):
                out["exclude"].append(scope)
            continue
        try:
            value = float(value)
        except (TypeError, ValueError):
            value = str(value).strip() if value is not None else None
        target = out["elements"] if "/" in scope and ":" in scope else out["marks"]
        target.setdefault(scope, {})[prop] = value
    return out


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("building", help="building.json, or with --overrides a filled review.xlsx")
    ap.add_argument("out", nargs="?", help="review.xlsx, or with --overrides overrides.json")
    ap.add_argument("--overrides", action="store_true", help="read the Overrides sheet into overrides.json")
    a = ap.parse_args(argv)
    if a.overrides:
        ov = overrides_from_excel(a.building)
        out = a.out or os.path.join(os.path.dirname(a.building), "overrides.json")
        with open(out, "w", encoding="utf-8") as f:
            json.dump(ov, f, indent=1)
        n = len(ov["elements"]) + len(ov["marks"]) + len(ov["exclude"])
        print(f"Wrote {out}: {n} decisions")
        return 0
    b = Building.load(a.building)
    out = a.out or os.path.join(os.path.dirname(a.building), "review.xlsx")
    print(f"Wrote {write(b, out)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
