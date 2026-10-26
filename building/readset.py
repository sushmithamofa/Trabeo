r"""Read a whole drawing set into one building model.

    python -m building.readset OUT DRAWING_OR_FOLDER [...] [--levels "L1=Level 1,GF=Ground"]

1. Every drawing is split into views and each view identified (sheet registry).
   All of them go into building.json as sheets, with the evidence.
0. A drawing not in millimetres is converted first (metres, centimetres), or not
   read at all when its units cannot be known (feet and inches, or none set on a
   drawing that is not millimetre-sized) - said in the log and as a blocker.
2. Every framing plan is read by the plan reader (today's extractor):
   * from a drawing laid out on paper-space layouts, the plan's view alone is
     cut out and read, so the beam details drawn beside it are not taken for beams;
   * from a drawing with no layouts and one floor's plan, the whole file is read
     exactly as before - the extractor finds the beam schedule beside the plan
     itself, and the locked baselines depend on that;
   * from a drawing with no layouts holding several floors' plans side by side,
     each floor is read on its own, with the other floors' plans taken out.
   Each read writes the usual model.json into OUT\<sheet>\, so the Revit build
   and the Excel work on it unchanged.
3. Views of other kinds (details, schedules, foundation plans...) are listed
   with their kind; their readers arrive in later stages.

--levels maps level keys (L1, GF, TERRACE...) onto the level names used in Revit.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from building import Building                          # noqa: E402
from building import prefixes, profiles               # noqa: E402
from building.linker import link                       # noqa: E402
from building import excel, review                     # noqa: E402
from building.profiles.suggest import suggest          # noqa: E402
from building.readers import (beam_sections, building_sections, column_schedule, columns, details,  # noqa: E402
                              elevations, foundations, labels, layer_sizes, legacy_plan, legend_zones, linework,
                              scope, stairs,
                              tables)
from building.sheets import register, to_building      # noqa: E402
from building.sheets.levels import custom_floor        # noqa: E402
from building.sheets.__main__ import drawings          # noqa: E402
from building.model import BLOCKER, INFO, WARNING, Issue     # noqa: E402
from building.sheets.source import clip_out, clip_to, in_mm   # noqa: E402

sys.path.insert(0, os.path.join(ROOT, "cad2revit"))
from core.units import UnitsError                       # noqa: E402

READ_BY_PLAN_READER = ("framing_plan",)


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_")[:60]


def _reused_drawing(recs, path, log, b):
    """A file holding one plan whose title names another floor than its file name:
    J1153 "3ST FLOOR PLAN" is the 1st-floor drawing issued again as the 3rd floor, its
    title ("R.C.C. PLAN @ WING B 1ST FLOOR.") and schedules left as they were. The file
    name is what was issued, so the whole file is that floor - said in the log and in
    Review, where a person can put it back. A sheet with several plans keeps each
    plan's own title. Returns (old, new) level keys, or None."""
    plans = [r for r in recs if r.verdict and r.verdict.kind in READ_BY_PLAN_READER and not r.view.same_as
             and r.view.source not in ("stray", "overview")]
    if len(plans) != 1 or plans[0].view.split_from or not plans[0].level_conflict:
        return None
    p = plans[0]
    by_src = {src: found for src, _, found in p.level_sources}
    title, named = by_src.get("title") or [], by_src.get("file name") or []
    if len(title) != 1 or len(named) != 1 or title == named or p.levels != title:
        return None                       # (a person's own correction of its floor stands)
    old, new = title[0], named[0]
    for r in recs:
        if r.levels == [old]:
            r.levels = [new]
            r.levels_from = f"file name (its title names {old})"
    msg = (f"{os.path.basename(path)}: its file name says {new} but the plan's title says {old} "
           f"('{p.title[:50]}') - a drawing issued again for another floor. Read as {new}, the file "
           f"name. If it really is {old}, correct it under Sheets in Review.")
    log("     " + msg)
    b.issues.append(Issue(WARNING, "sheets", msg, extra={"sheet": p.id}))
    return old, new


def _added_floors(recs, added: list[str], log, renamed: dict | None = None) -> None:
    """Views named by a floor the person added on the Floors page ("Service floor"): that
    floor. The title first (it wins over a floor the app knows - the person said this
    floor exists); the layout or file name only for a view naming no floor at all.
    renamed: the added floors' names now on the Floors page (found by those too)."""
    from building.sheets.levels import custom_floors_in
    if not added:
        return
    for r in recs:
        hit = custom_floors_in(r.title, added, renamed)
        src, text = "title", r.title
        if not hit and not r.levels:
            for src, text in (("layout name", r.view.layout), ("file name", os.path.splitext(os.path.basename(r.file))[0])):
                hit = custom_floors_in(text, added, renamed)
                if hit:
                    break
        if hit:
            r.levels = hit
            r.level_sources.insert(0, (src, (text or "")[:60], hit))
            r.levels_from = f"{src}: {(text or '')[:60]} (a floor added on the Floors page)"
            log(f"     {r.view.id}: '{(text or '')[:50]}' -> {hit[0]} (a floor added on the Floors page)")


def _correct_sheets(recs, corrections: dict, log) -> None:
    """A person's correction of what a view is - its kind or level - applied
    before anything is read, because it decides which reader gets the view."""
    from building.sheets.classify import Verdict
    for r in recs:
        fix = corrections.get(r.id)
        if not fix:
            continue
        if fix.get("kind"):
            r.verdict = Verdict(str(fix["kind"]), 1.0, [f"set by review (was {r.verdict.kind})"])
        if fix.get("level"):
            r.levels = [str(fix["level"])]
            r.level_sources = [("review", "set by review", r.levels)]
        log(f"     {r.view.id}: corrected by review -> {fix}")


def _set_legend(registered, work: str, log=print, read_from: dict | None = None) -> dict:
    """What a set's legend sheets say about sunk hatches, for the plans whose own
    legend does not: a drawing that is a legend and nothing the plan reader reads
    (an office's or consultant's key sheet imported with the plans). `registered`
    is one set's drawings: a legend sheet never reaches another job's plans."""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, os.path.join(here, "cad2revit"))
    from core import legend as cad_legend                        # noqa: E402
    from core.pipeline import load_config                        # noqa: E402
    from building.sheets.source import dxf_for
    read_from = read_from or {}
    cfg = load_config()
    out = {"sheet": "", "samples": [], "lines": []}
    for path, recs in registered:
        kinds = {r.verdict.kind for r in recs if r.view.source != "stray"}
        if "legend" not in kinds or kinds & set(READ_BY_PLAN_READER):
            continue
        try:
            got = cad_legend.samples_from_file(dxf_for(read_from.get(path, path), work), cfg)
        except Exception as exc:
            log(f"     legend sheet {os.path.basename(path)} could not be read: {exc}")
            continue
        if got["samples"] or got["lines"]:
            name = os.path.splitext(os.path.basename(path))[0]
            out["sheet"] = (out["sheet"] + ", " if out["sheet"] else "") + name
            out["samples"] += got["samples"]
            out["lines"] += got["lines"]
            log(f"     legend sheet {name}: " + ", ".join(
                f"{q[3]:.0f} mm = {q[1]}" for q in got["samples"] + got["lines"]))
    return out if out["samples"] or out["lines"] else {}


def _floors_side_by_side(plans) -> dict[str, list]:
    """Framing plans of one model space, grouped by the floor they name, largest first.

    Only plans naming different floors are separate floors: two regions naming the
    same floor (or none) are taken as parts of one plan - a wing drawn apart - and
    stay together, as before. Regions naming no floor join the largest group.
    """
    groups: dict[str, list] = {}
    for r in sorted(plans, key=lambda r: -len(r.view.handles)):
        groups.setdefault(r.levels[0] if r.levels else "", []).append(r)
    named = {k: v for k, v in groups.items() if k}
    if len(named) < 2:
        return {"": list(plans)}
    rest = []
    for r in groups.get("", []):
        # a plan titled as a floor of its own whose floor is not one the app knows ("SERVICE
        # FLOOR LAYOUT PLAN"): another floor, read on its own with no level - never part of
        # the largest floor (its columns would be that floor's)
        if _FLOOR_TITLE.search((r.title or "").upper()):
            named["?" + r.view.id] = [r]
        else:
            rest.append(r)
    if rest:
        biggest = max((k for k in named if not k.startswith("?")), key=lambda k: len(named[k][0].view.handles))
        named[biggest] += rest
    return named


# a title naming a plan of a floor ("SERVICE FLOOR LAYOUT PLAN", "TERRACE LEVEL PLAN")
_FLOOR_TITLE = re.compile(r"\b(?:FLOOR|FLR|LEVEL|LVL|STOREY|STORY|TERRACE|ROOF)\b.*\bPLAN\b")
# the other drawings of a building's plans in one model space: their columns and beams are never
# a floor's own
_OTHER_PLAN_KINDS = ("framing_plan", "column_layout", "foundation_plan")


def _other_plans(recs, floors) -> list:
    """Drawings beside the floors that show the building in plan - a column layout, a
    foundation plan, a titled floor plan the registry left as scraps: taken out of each
    floor's file as the other floors are. Schedules, details and notes stay."""
    read = {r.id for rs in floors.values() for r in rs}
    out = []
    for r in recs:
        if r.id in read or r.view.same_as:
            continue
        if r.verdict.kind in _OTHER_PLAN_KINDS or \
                (r.view.source == "stray" and _FLOOR_TITLE.search((r.title or "").upper())):
            out.append(r)
    return out


def _not_this_sheets_plan(r, path, set_key, registered, set_of) -> str:
    """Why a plan drawn outside every sheet of its drawing is not read as a floor, or "".

    Pirgal draws the whole building in one model space shared by every sheet file: each
    file carries the other sheets' drawings outside its own viewports - the ELEVATIONS file
    holds the second-floor plan at exactly the place S-PR-10's sheet shows it, and was read
    as one more second floor. Such a region is a copy of another sheet's drawing when that
    sheet's viewport frames the same place. And a region named a plan only by its file name
    (no plan title, no grid or marked structure of its own) is a leftover, not a floor. A plan
    drawn outside the sheets for real (J1153 3rd floor C wing) has its own title or content."""
    from building.sheets.views import _same
    # one floor, one plan: the set already shows this floor on a sheet (Pirgal 2020-11-04's
    # third-floor file keeps three more copies of its own plan, and one of the second floor's
    # slab reinforcement plan, outside its sheet - each read as one more floor). J1153's 3rd
    # floor, whose sheet holds only an overview, keeps its plan drawn outside the sheets
    if r.levels:
        for p, recs in registered:
            if set_of(p) != set_key:
                continue
            for o in recs:
                if o is not r and o.view.source == "viewport" and not o.view.same_as \
                        and o.verdict is not None and o.verdict.kind in READ_BY_PLAN_READER \
                        and getattr(o.view, "twin", "") != "reinforcement" \
                        and o.levels[:1] == r.levels[:1] and (o.wing or "") == (r.wing or ""):
                    return (f"{r.levels[0]} is already on a sheet ({os.path.basename(p)}, '{o.title[:30]}'): "
                            "a copy drawn outside the sheets")
    for p, recs in registered:
        if p == path or set_of(p) != set_key:
            continue
        for o in recs:
            if o.view.source != "viewport":
                continue
            whole = getattr(o.view, "whole", None)          # a sheet window cut in two (twin drawings)
            if any(_same(w, r.view.window) for w in [o.view.window] + ([whole.window] if whole is not None else [])):
                return f"the same place as {os.path.basename(p)}'s sheet shows (a copy of that drawing)"
    reasons = (r.verdict.reasons if r.verdict else []) or []
    if not any(x.startswith(("plan:", "title '", "layout '", "split out")) for x in reasons):
        return "named a plan only by its file name"
    if r.title_from == "file name" and r.verdict is not None and r.verdict.confidence <= 0.0:
        return "no title of its own, and its content says plan no more than anything else"
    return ""


def read_set(out: str, paths: list[str], level_names: dict[str, str] | None = None,
             project: str = "", log=print, profiles_in: list[dict] | None = None,
             use_suggested: bool = False, overrides: dict | None = None,
             set_name: str | None = None, prefix_hints: dict | None = None) -> Building:
    level_names = level_names or {}
    profiles_in = [p for p in (profiles_in or []) if p.get("confirmed")]
    work = os.path.join(out, "_work")
    os.makedirs(work, exist_ok=True)
    b = Building(project=project)
    # every drawing registered first, so a legend sheet anywhere in the set is known
    # before the plans are read, whatever order the files come in
    registered = []
    read_from = {}                 # each drawing as read: itself, or its copy in millimetres
    stated = ((overrides or {}).get("project") or {}).get("units") or {}
    for path in drawings(paths):
        name = os.path.basename(path)
        log(f"\n{name}")
        try:
            src, note = in_mm(path, work, stated.get(name))
        except UnitsError as exc:
            log(f"     STOPPED - not read: {exc}")
            b.issues.append(Issue(BLOCKER, "units", f"{name}: {exc}", extra={"file": path}))
            continue
        if note:
            log(f"     units: {note}")
            b.issues.append(Issue(WARNING, "units", f"{name}: {note}", extra={"file": path}))
        read_from[path] = src
        registered.append((path, register(src, work)))

    def set_of(path):
        # one drawing set per folder: marks of one project never meet another's
        # set_name: every drawing of one job, wherever its file is (the app's import)
        return set_name or os.path.basename(os.path.dirname(os.path.abspath(path)))
    legends = {k: _set_legend([r for r in registered if set_of(r[0]) == k], work, log, read_from)
               for k in {set_of(p) for p, _ in registered}}
    for path, recs in registered:
        log(f"\nReading {os.path.basename(path)}:")
        _added_floors(recs, [k for k in level_names if custom_floor(k)], log, level_names)
        _correct_sheets(recs, (overrides or {}).get("sheets") or {}, log)
        moved = _reused_drawing(recs, path, log, b)
        before = set(b.sheets)
        to_building(recs, b)
        set_key = set_of(path)
        set_legend = legends.get(set_key) or {}
        for sid in set(b.sheets) - before:
            b.sheets[sid].extra["set"] = set_key
            if level_names.get(b.sheets[sid].level):
                # the Revit name the person gave this sheet's floor ("FDN" -> "Foundation")
                b.sheets[sid].extra["revit_level"] = level_names[b.sheets[sid].level]

        # confirmed profiles' prefix meanings, as read until now (a suggested profile
        # never carries any); since 2026-10-15 they are also hints, rechecked below
        fam = profiles.families(*profiles_in)

        for r in recs:
            if r.view.source in ("stray", "overview") or r.view.same_as:
                continue
            got = {**labels.read(b, r, fam or None), **tables.read(b, r), **details.read(b, r),
                   **foundations.read(b, r), **columns.read(b, r), **column_schedule.read(b, r),
                   **stairs.read(b, r), **elevations.read(b, r)}
            hints = layer_sizes.read(b, r)
            zones = scope.read(b, r)
            if zones:
                got["areas marked as another block"] = zones
            shaped = beam_sections.read(b, r)
            if shaped:
                got["beam sections drawn" if r.verdict.kind != "framing_plan" else "section cuts marked"] = shaped
            marked = legend_zones.read(b, r)
            if marked:
                got["legend areas and rows"] = marked
            cuts_h = building_sections.read(b, r)
            if cuts_h:
                got["building sections (levels, heights, slopes)"] = cuts_h
            styled = linework.read(b, r)
            if styled:
                got["beam lines with their style (dashed / solid)"] = styled
            if got or hints:
                log(f"     {r.view.id}: " + ", ".join(f"{v} {k}" for k, v in got.items())
                    + (f", {hints} sizes in layer names" if hints else ""))

        # what this set's mark prefixes mean, from its own schedules and mark layers so
        # far (the linker adds where the marks sit on the plan and settles it)
        saved = {**fam, **{k.upper(): v for k, v in (prefix_hints or {}).items()}}
        decided = prefixes.decide_all(prefixes.drawing_evidence(b, set_key).get(set_key, {}), saved,
                                      (overrides or {}).get("prefixes"))
        set_map = prefixes.families_map(decided)
        for sid in set(b.sheets) - before:
            if saved:
                b.sheets[sid].extra["prefix_hints"] = saved
        for p, d in decided.items():
            if d["status"] in ("learned", "drawing", "decided") and d["family"] != d["builtin"]:
                log(f"     marks {p}..: {d['family']} ({prefixes.describe(d)})")

        # this consultant's layers: confirmed profiles first; a suggestion is
        # written for review and only used when asked for explicitly
        suggestion = suggest(recs, os.path.basename(path), set_map or None)
        if suggestion["layer_roles"]:
            sp = profiles.save(suggestion, os.path.join(out, "_profiles", _safe(path) + ".json"))
            log(f"     suggested profile (unconfirmed): {os.path.relpath(sp, out)}")
            for role, layers in suggestion["layer_roles"].items():
                log(f"        {role:10s} <- {', '.join(layers)}")
        active = profiles_in + ([suggestion] if use_suggested and suggestion["layer_roles"] else [])
        extra = {"set_legend": set_legend} if set_legend else None
        cfg_path = profiles.config_file(*active, work_dir=work, extra=extra) if active or extra else None
        if moved:
            # what the readers took from the copied titles (a slab schedule "... 1ST FLOOR")
            # belongs to the floor this file is
            old, new = moved
            for e in b.elements:
                if e.sheet in set(b.sheets) - before and e.level == old:
                    e.level = new
        has_layouts = any(r.view.source == "viewport" for r in recs)
        plans = [r for r in recs if r.verdict.kind in READ_BY_PLAN_READER and not r.view.same_as
                 and r.view.source in ("viewport", "model region", "unplaced region")]
        for r in [r for r in plans if r.view.source == "unplaced region"]:
            why = _not_this_sheets_plan(r, path, set_key, registered, set_of)
            if why:
                plans.remove(r)
                msg = f"{os.path.basename(path)}: {r.view.id} ('{r.title[:40]}') drawn outside the sheets - {why}; not read"
                log("     " + msg)
                b.issues.append(Issue(INFO, "sheets", msg, extra={"sheet": r.id}))
        for r in recs:
            if r.view.source == "stray":
                continue
            mark = "->" if r in plans else "  "
            log(f"  {mark} {r.view.id:18s} {r.verdict.kind:16s} {','.join(r.levels) or '-':10s} {r.title[:50]}")
        strays = sum(1 for r in recs if r.view.source == "stray")
        if strays:
            log(f"     + {strays} stray scraps, listed in building.json, not read")
        if not plans:
            continue
        others = {}                     # plan -> the other floors' plans to take out first
        alone = set()                   # plans read on their own: clipped to their own entities
        split = not has_layouts and any(r.view.split_from for r in plans)
        if split:
            # Model space held several titled drawings side by side (a lintel plan, a
            # roof plan, their sections and schedules) and was split by their titles.
            # Each plan is read alone; a "plan" naming no floor is not read at all.
            named = [r for r in plans if r.levels]
            for r in plans:
                if not r.levels:
                    log(f"     {r.view.id}: looks like a plan but names no floor - not read "
                        "(correct it under Sheets in Review if it is one)")
                    b.issues.append(Issue(WARNING, "sheets",
                                          f"{r.view.id} ({r.title[:60]}) looks like a plan but its title "
                                          "names no floor, so it was not read. If it is a plan, give it its "
                                          "floor under Sheets in Review.", extra={"sheet": r.id}))
            repeats = defaultdict(list)
            for r in named:
                repeats[r.title.upper()].append(r.view.id)
            for title, ids in repeats.items():
                if len(ids) > 1:
                    log(f"     the plan '{title[:50]}' appears {len(ids)} times ({', '.join(ids)}): "
                        "each read on its own - check whether they are copies")
                    b.issues.append(Issue(WARNING, "sheets",
                                          f"The plan '{title[:60]}' appears {len(ids)} times in "
                                          f"{os.path.basename(path)} ({', '.join(ids)}). Each was read on its "
                                          "own. If they are copies of one plan (an old and a new issue), mark "
                                          "the extra ones 'not a plan' under Sheets in Review, or their "
                                          "elements are built more than once.", extra={"sheets": ids}))
            plans = named
            alone = {r.id for r in plans}
        elif not has_layouts:
            floors = _floors_side_by_side(plans)
            if len(floors) > 1:
                # several floors drawn side by side in model space: each read on its own,
                # with the other floors taken out (its schedules and details stay)
                log(f"     model space holds {len(floors)} floors' plans ("
                    + ", ".join(k or "?" for k in floors) + "): each read on its own")
                plans = []
                beside = _other_plans(recs, floors)
                if beside:
                    log("     taken out of every floor too (other drawings of the plan): "
                        + ", ".join(f"{r.view.id} {r.title[:40]}" for r in beside))
                for k, rs in floors.items():
                    plans.append(rs[0])
                    others[rs[0].id] = [h for k2, rs2 in floors.items() if k2 != k for r2 in rs2
                                        for h in r2.view.handles] + [h for r2 in beside for h in r2.view.handles]
            else:
                # one floor: the whole drawing, exactly as the extractor reads it today
                plans = plans[:1]
        for r in plans:
            if len(r.levels) > 1:
                log(f"     {r.view.id}: names {r.levels}; read on {r.levels[0]} - confirm in review")
            key = r.levels[0] if r.levels else ""
            level = level_names.get(key, key) or None
            sid = f"{r.id}/plan"
            target = os.path.join(out, _safe(sid))
            os.makedirs(target, exist_ok=True)
            src = read_from[path]
            if has_layouts or r.id in alone:
                borrow = getattr(r.view, "borrow", None) or []
                if borrow:
                    log(f"     {r.view.id}: {len(borrow)} slab marks taken from the same floor's reinforcement "
                        "drawing beside it (the plan carries none)")
                src = clip_to(src, r.view.handles,
                              os.path.join(work, _safe(r.id) + "_view.dxf"), work,
                              borrow=borrow, shift=getattr(r.view, "borrow_shift", (0.0, 0.0)))
            elif r.id in others:
                src = clip_out(src, others[r.id], os.path.join(work, _safe(r.id) + "_floor.dxf"), work)
            legacy_plan.read(src, target, sid, level, b, r.wing, config_path=cfg_path)
            b.sheets[sid].extra["view_sheet"] = r.id
            b.sheets[sid].extra["level_key"] = key
            b.sheets[sid].extra["set"] = set_key
            b.sheets[sid].extra["profiles"] = [p.get("name", "") + ("" if p.get("confirmed") else " (suggested)")
                                               for p in active]
            els = b.of(sheet=sid)
            counts = {t: sum(1 for e in els if e.type == t) for t in ("beam", "column", "slab", "wall")}
            log(f"     read {r.view.id} -> " + ", ".join(f"{v} {k}s" for k, v in counts.items() if v)
                + f" on level {level or '(from drawing)'}"
                + ("" if has_layouts else " (this plan alone, split by its title)" if r.id in alone
                   else " (this floor alone)" if r.id in others else " (whole file, as before)")
                + (f" [profile: {', '.join(b.sheets[sid].extra['profiles'])}]" if active else ""))
    # two different floors of one drawing set given the same Revit level on the Floors page
    # (ankit's Akanistha: L1 = "Level 3" and L3 = "Level 3") would be built one on the other
    on = {}
    for sid, s in b.sheets.items():
        if s.kind in READ_BY_PLAN_READER and s.level and level_names.get(s.level):
            on.setdefault((s.extra.get("set", ""), level_names[s.level]), set()).add(s.level)
    for (set_key, rl), keys in sorted(on.items()):
        if len(keys) > 1:
            msg = (f"Floors {', '.join(sorted(keys))} are all given the Revit level '{rl}' on the Floors page, so they "
                   f"would be built on top of each other. Give each floor its own level, then Analyse again.")
            log("     WARNING: " + msg)
            b.issues.append(Issue(WARNING, "levels", msg, extra={"set": set_key, "level": rl, "floors": sorted(keys)}))
    stats = link(b, overrides)
    log("\nLinked: " + ", ".join(f"{v} {k}" for k, v in sorted(stats.items())))
    b.save(os.path.join(out, "building.json"))
    excel.write(b, os.path.join(out, "review.xlsx"))
    review.write(b, os.path.join(out, "review.html"), (overrides or {}).get("project"))
    log(f"Review: {os.path.join(out, 'review.html')} and review.xlsx")
    return b


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out")
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--levels", default="", help='"L1=Level 1,GF=Ground Floor"')
    ap.add_argument("--project", default="")
    ap.add_argument("--profile", action="append", default=[],
                    help="a confirmed consultant or project profile (repeatable; later wins)")
    ap.add_argument("--use-suggested", action="store_true",
                    help="also use the unconfirmed suggestions - for testing only")
    ap.add_argument("--overrides", help="project overrides: review decisions (JSON)")
    a = ap.parse_args(argv)
    overrides = profiles.load(a.overrides) if a.overrides else None
    names = dict(p.split("=", 1) for p in a.levels.split(",") if "=" in p)
    loaded = [profiles.load(p) for p in a.profile]
    for p, path in zip(loaded, a.profile):
        if not p.get("confirmed"):
            print(f"Note: {path} is not confirmed (\"confirmed\": false) and will not be used.")
    b = read_set(a.out, a.paths, names, a.project, profiles_in=loaded,
                 use_suggested=a.use_suggested, overrides=overrides)
    kinds: dict[str, int] = {}
    for s in b.sheets.values():
        if s.reader == "sheets.registry":
            kinds[s.kind] = kinds.get(s.kind, 0) + 1
    types: dict[str, int] = {}
    for e in b.elements:
        types[e.type] = types.get(e.type, 0) + 1
    print(f"\nWrote {os.path.join(a.out, 'building.json')}")
    print("  views: " + ", ".join(f"{v} {k}" for k, v in sorted(kinds.items())))
    print("  elements: " + (", ".join(f"{v} {k}" for k, v in sorted(types.items())) or "none"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
