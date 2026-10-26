"""The level table: every level of each drawing set, its elevation and floor height.

Built by the linker from what the readers found (stage 7):

* elevations from floor level markers (sections, elevations, notes on plans):
  every marker naming a level gives that level a candidate; markers that
  disagree are a conflict, never averaged. A finished (FFL) and a structural
  (SFL) level of one floor are both kept, as separate values.
* floor-to-floor heights between consecutive levels whose elevations are read;
* floor height notes ("HEIGHT REVISED TO 3150MM") listed with the level of the
  sheet they are on - shown, never turned into elevations;
* a section's markers checked against the drawing: on one view, levels drawn
  higher must have higher values, all at one scale. A marker off that scale is
  reported.

Levels are those the plans are on plus those the markers name. One "level"
element per (drawing set, level); a level with no elevation from any source is
reported as missing - the Revit build then keeps the levels already in Revit.
"""
from __future__ import annotations

from collections import defaultdict

from .model import Building, Element, Evidence, Issue, Value, CONFLICT, INFO, MISSING, READ, WARNING
from .sheets.levels import key_elevation, level_rank

READER = "levels"


def _set(b: Building, e) -> str:
    s = b.sheets.get(e.sheet)
    return s.extra.get("set", "") if s else ""


def build(b: Building, stats) -> None:
    b.elements = [e for e in b.elements if e.type != "level"]
    marks = [m for m in b.of("level_mark") if m.get("kind") == "floor" and m.get("level")]
    _check_scale(b, marks, stats)

    keys: dict[str, set] = defaultdict(set)
    for sid, s in b.sheets.items():
        if s.reader == "plan.legacy" and s.extra.get("level_key") and level_rank(s.extra["level_key"]) is not None:
            keys[s.extra.get("set", "")].add(s.extra["level_key"])
    for m in marks:
        keys[_set(b, m)].add(m.get("level"))
    notes = defaultdict(list)
    for s in b.of("spec"):
        if s.get("family") == "level" and s.level:
            notes[(_set(b, s), s.level)].append(s)

    for set_key, ks in keys.items():
        rows = []
        for k in sorted(ks, key=level_rank):
            mine = [m for m in marks if _set(b, m) == set_key and m.get("level") == k and not m.extra.get("off_scale")]
            ev_all = [m.props["elevation"].evidence[0] for m in mine]
            by_basis = defaultdict(list)
            for m in mine:
                by_basis[m.get("basis") or ""].append(m)
            el = Element(id=f"levels/{set_key or '-'}/{k}", type="level", sheet="", level=k,
                         extra={"set": set_key, "markers": len(mine)})
            for basis, ms in by_basis.items():
                vals = sorted({m.get("elevation") for m in ms})
                prop = "elevation" if basis in ("", "structural") else f"elevation_{basis}"
                if prop in el.resolved:                # structural and unspecified both given: keep apart
                    prop = "elevation_marked"
                ev = [Evidence(reader=READER, sheet=m.sheet, source=f"level marker: {m.get('elevation'):+.0f}",
                               layer=m.props['elevation'].evidence[0].layer,
                               handle=m.props["elevation"].evidence[0].handle, at=m.props["elevation"].evidence[0].at,
                               text=m.props["elevation"].evidence[0].text) for m in ms]
                if len(vals) == 1:
                    el.resolved[prop] = Value(vals[0], READ, min(1.0, 0.8 + 0.05 * (len(ms) - 1)), ev)
                else:
                    el.resolved[prop] = Value(vals[0], CONFLICT, 0.4, ev)
                    b.issues.append(Issue(WARNING, "level", f"level {k}: markers give {', '.join(f'{v:+.0f}' for v in vals)}"
                                          + (f" ({basis})" if basis else ""), elements=[el.id],
                                          extra={"from_linker": True}))
                    stats["level elevations in conflict"] += 1
            if "elevation" not in el.resolved and key_elevation(k) is not None:
                # a level its plan's title names by value ("FRAMING PLAN @(+)6600 LVL."): that value
                titled = [s for s in b.sheets.values() if s.reader == "plan.legacy"
                          and s.extra.get("set", "") == set_key and s.extra.get("level_key") == k]
                view = b.sheets.get(titled[0].extra.get("view_sheet", "")) if titled else None
                el.resolved["elevation"] = Value(key_elevation(k), READ, 0.8, [Evidence(
                    reader=READER, sheet=view.id if view else "", source="plan title",
                    text=(view.title if view else k))])
            if "elevation" not in el.resolved and not any(p.startswith("elevation") for p in el.resolved):
                el.resolved["elevation"] = Value(None, MISSING, 0.0, [])
            hn = notes.get((set_key, k), [])
            if hn:
                el.resolved["height_note"] = Value(sorted({s.get("storey_height") for s in hn}), READ, 0.6,
                                                   [s.props["storey_height"].evidence[0] for s in hn][:4])
            rows.append(el)
            b.add(el)
            stats["levels"] += 1
            if el.resolved["elevation"].status == MISSING and not any(
                    p.startswith("elevation_") for p in el.resolved):
                stats["levels without elevation"] += 1
        # floor-to-floor: from each level to the next one up, where both are read
        known = [r for r in rows if r.resolved.get("elevation") and r.resolved["elevation"].status == READ]
        for lo, hi in zip(known, known[1:]):
            h = hi.resolved["elevation"].value - lo.resolved["elevation"].value
            hi.resolved["height_below"] = Value(round(h), READ, 0.8,
                                                hi.resolved["elevation"].evidence[:1] + lo.resolved["elevation"].evidence[:1])
            hi.extra["below"] = lo.level
        if rows and not known:
            b.issues.append(Issue(INFO, "level", f"{set_key or 'drawings'}: no floor level is written with its "
                                  f"elevation - the Revit build keeps the levels already in the Revit model",
                                  extra={"from_linker": True}))


def _check_scale(b: Building, marks, stats) -> None:
    """On one view, markers drawn higher must read higher, all at one scale."""
    by_sheet = defaultdict(list)
    for m in marks:
        m.extra.pop("off_scale", None)
        m.extra.pop("on_scale", None)
        if m.geometry.get("y") is not None:
            by_sheet[m.sheet].append(m)
    for sid, ms in by_sheet.items():
        pts = sorted({(round(m.geometry["y"]), m.get("elevation"), m.id) for m in ms})
        uniq = {}
        for y, v, mid in pts:
            uniq.setdefault(v, (y, mid))
        if len(uniq) < 3:
            continue
        items = sorted((v, y, mid) for v, (y, mid) in uniq.items())
        # scale from every pair; the common one is the median
        ks = sorted((y2 - y1) / (v2 - v1) for i, (v1, y1, _) in enumerate(items)
                    for (v2, y2, _) in items[i + 1:] if v2 != v1)
        k = ks[len(ks) // 2]
        if k <= 0:
            continue
        base =sorted(y - k * v for v, y, _ in items)[len(items) // 2]
        for v, y, mid in items:
            if abs((y - base) / k - v) > max(50.0, 0.02 * abs(v)):
                m = next(x for x in ms if x.id == mid)
                m.extra["off_scale"] = True
                b.issues.append(Issue(WARNING, "level", f"level marker {m.extra.get('name_text') or ''} {v:+.0f} is "
                                      f"not where the other markers on this section put it (drawn at "
                                      f"{(y - base) / k:+.0f}) - not used", sheet=sid, elements=[mid],
                                      extra={"from_linker": True}))
                stats["level markers off the section's scale"] += 1
            else:
                next(x for x in ms if x.id == mid).extra["on_scale"] = True
