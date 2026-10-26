r"""Stage 9: the whole workflow, drawings to Revit, in one step.

    python -m building.workflow DRAWINGS_FOLDER [--project PROJECT_FOLDER]
                                [--levels "L1=Level 1,L2=Level 2"] [--use-suggested]

A project folder keeps everything for one job together:

    PROJECT\
        overrides.json        your review decisions - applied to every run
        profiles\*.json       confirmed consultant profiles - applied to every run
        dxf_cache\            converted drawings, reused while the DWG is unchanged
        runs\2026-10-07_1530\ one folder per run:
            building.json  review.html  review.xlsx  revit_build\  SUMMARY.txt  summary.json

Each run:
  1. closes converter windows a stopped run left open (ZWCAD showing one of this
     tool's own temporary copies - never anything else), which otherwise hold up
     every later conversion;
  2. reuses the drawings already converted to DXF (a DWG converts again only
     when it has changed);
  3. reads the set (readset), links it, writes review.html / review.xlsx;
  4. writes the Revit files (export);
  5. writes SUMMARY.txt: what was read, what needs a decision, what to do next.

The desktop window (app.py, run_app.bat) is this, with buttons.
"""
from __future__ import annotations

import argparse
import csv
import datetime
import glob
import io
import json
import os
import shutil
import subprocess
import sys
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


# ---------------------------------------------------------------- converters
def stale_converters(tasklist_csv: str) -> list[int]:
    """Process ids of ZWCAD windows holding one of this tool's temporary copies.

    tasklist_csv: the output of `tasklist /v /fo csv /fi "imagename eq ZWCAD.exe"`.
    Only a window whose title shows a c2r_zw_ temporary drawing is ours; a ZWCAD
    someone has open on a real drawing is never touched.
    """
    out = []
    rows = list(csv.reader(io.StringIO(tasklist_csv)))
    if not rows:
        return out
    head = [h.strip().lower() for h in rows[0]]
    try:
        i_pid, i_title = head.index("pid"), head.index("window title")
    except ValueError:
        return out
    for r in rows[1:]:
        if len(r) > max(i_pid, i_title) and "c2r_zw_" in r[i_title].lower():
            try:
                out.append(int(r[i_pid]))
            except ValueError:
                continue
    return out


def close_stale_converters(log=print) -> int:
    if os.name != "nt":
        return 0
    try:
        got = subprocess.run(["tasklist", "/v", "/fo", "csv", "/fi", "imagename eq ZWCAD.exe"],
                             capture_output=True, text=True, timeout=60).stdout
    except Exception:
        return 0
    pids = stale_converters(got)
    for pid in pids:
        subprocess.run(["taskkill", "/f", "/pid", str(pid)], capture_output=True, timeout=60)
    if pids:
        log(f"Closed {len(pids)} converter window(s) a stopped run had left open.")
    return len(pids)


def seed_cache(cache: str, work: str, drawings: list[str]) -> int:
    """Copy the converted DXF of every unchanged drawing into the run's work folder.

    The reader converts a DWG only when its DXF is missing or older than the DWG,
    so a copied DXF that is newer than its DWG is used as it is.
    """
    os.makedirs(work, exist_ok=True)
    n = 0
    for dwg in drawings:
        if not dwg.lower().endswith(".dwg"):
            continue
        name = os.path.splitext(os.path.basename(dwg))[0] + ".dxf"
        src = os.path.join(cache, name)
        if os.path.exists(src) and os.path.getmtime(src) >= os.path.getmtime(dwg):
            shutil.copy2(src, os.path.join(work, name))
            n += 1
    return n


def keep_converted(work: str, cache: str, drawings: list[str]) -> int:
    """Keep the run's converted DXFs for the next run (whole drawings only)."""
    os.makedirs(cache, exist_ok=True)
    n = 0
    for dwg in drawings:
        name = os.path.splitext(os.path.basename(dwg))[0] + ".dxf"
        src = os.path.join(work, name)
        if dwg.lower().endswith(".dwg") and os.path.exists(src):
            dst = os.path.join(cache, name)
            if not os.path.exists(dst) or os.path.getmtime(dst) < os.path.getmtime(src):
                shutil.copy2(src, dst)
                n += 1
    return n


# ---------------------------------------------------------------- summary
def summarise(b, stats: dict) -> dict:
    """What the run found, in numbers a person can act on."""
    by_type = Counter(e.type for e in b.elements)
    status = Counter()
    for e in b.elements:
        if e.type not in ("beam", "beam_span", "column", "slab", "wall", "footing", "raft", "stair", "level"):
            continue
        if e.type == "wall" and e.final("role") == "masonry":
            continue                # never built: its sizes are nobody's decision
        for p, v in e.resolved.items():
            if p in ("width", "depth", "thickness", "length", "mark", "elevation", "going"):
                status[v.status] += 1
    issues = Counter((i.severity, i.category) for i in b.issues)
    plans = [s for s in b.sheets.values() if s.reader == "plan.legacy"]
    return {
        "plans": len(plans),
        "views": sum(1 for s in b.sheets.values() if s.reader == "sheets.registry"),
        "elements": {k: by_type[k] for k in ("column", "beam", "slab", "wall", "footing", "raft", "pedestal",
                                             "stair", "ramp", "level") if by_type[k]},
        "values": dict(status),
        "issues": {f"{sev}: {cat}": n for (sev, cat), n in issues.most_common()},
        "stats": {k: v for k, v in stats.items() if isinstance(v, int)},
    }


def next_steps(summary: dict) -> list[str]:
    """Plain next steps, most important first."""
    out = []
    v = summary.get("values", {})
    iss = summary.get("issues", {})
    blockers = {k.split(": ", 1)[1]: n for k, n in iss.items() if k.startswith("blocker: ")}
    if blockers:
        out.append(f"{sum(blockers.values())} blockers ({', '.join(f'{n} {k}' for k, n in blockers.items())}): "
                   f"things that cannot be built until the drawing changes or a person decides. See the Issues "
                   f"list in Review ('setting out' means the drawing has no grid).")
    if v.get("conflict"):
        out.append(f"{v['conflict']} values have sources that disagree (red in Review). Click each one and "
                   f"choose the right value, or type it.")
    if v.get("missing"):
        out.append(f"{v['missing']} values no drawing gives (often slab thicknesses or beam depths). Type them "
                   f"in Review.")
    for key, n in iss.items():
        if key.endswith("column mark"):
            out.append(f"{n} column marks from the column layout disagree with the column schedule - check them "
                       f"in Review (Issues).")
        if key == "warning: wall":
            out.append(f"{n} walls need a look (columns drawn as walls, walls Revit would split badly) - "
                       f"see Issues in Review.")
    pre = summary.get("prebuild") or {}
    if pre.get("needs_decision"):
        red = pre.get("built_red", 0)
        held = pre["needs_decision"] - red
        parts = ([f"{red} are built meanwhile at a working size, coloured red in Revit"] if red else []) + \
                ([f"{held} are left out of Revit until decided"] if held else [])
        out.append(f"{pre['needs_decision']} elements need a decision (sizes in conflict or not given, marks not "
                   f"written, areas of another block, footings with no founding level): {'; '.join(parts)}. "
                   f"The Export page lists them.")
    if pre.get("levels_missing"):
        out.append(f"Floor levels with no elevation in the drawings: {', '.join(pre['levels_missing'])}. Enter them "
                   f"on the Export page (Floor levels) - the app does not assume storey heights.")
    if summary.get("suggested_profiles_used"):
        out.append("Some layer names were new, so the app guessed what they hold (suggested profiles). Check "
                   "the elements on them in Review.")
    out.append("When the review is settled, Export to Revit. In Revit the floors are built with "
               "revit\\build_model.py and the foundations with revit\\build_extras.py (or both at once with the "
               "Rhino.Inside component).")
    return out


def write_summary(run_dir: str, summary: dict) -> str:
    steps = next_steps(summary)
    lines = [f"RUN {os.path.basename(run_dir)}", "",
             f"Read: {summary['views']} views, {summary['plans']} plans",
             "Elements: " + ", ".join(f"{v} {k}s" for k, v in summary["elements"].items()),
             "Values: " + ", ".join(f"{v} {k}" for k, v in sorted(summary["values"].items())), "",
             "ISSUES"] + [f"  {n:5d}  {k}" for k, n in summary["issues"].items()] + ["", "NEXT"] + \
            [f"  {i + 1}. {s}" for i, s in enumerate(steps)]
    with open(os.path.join(run_dir, "summary.json"), "w", encoding="utf-8") as f:
        json.dump({**summary, "next": steps}, f, indent=1)
    path = os.path.join(run_dir, "SUMMARY.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return path


def _prebuild(run_dir: str, overrides: dict | None) -> dict:
    """The pre-build check's counts, for the summary."""
    try:
        from building.prebuild import check
        c = check(run_dir, (overrides or {}).get("project"))
    except Exception:
        return {}
    return {"ready": c.get("ready") or {}, "needs_decision": len(c.get("needs_decision") or []),
            "built_red": sum(1 for x in c.get("needs_decision") or [] if x.get("built")),
            "not_supported": len(c.get("not_supported") or []), "levels_missing": c.get("levels_missing") or []}


# ---------------------------------------------------------------- the run
def run(drawings, project: str | None = None, levels: str = "", use_suggested: bool = False,
        log=print, prefix_hints: dict | None = None) -> str:
    """One run of the whole workflow. Returns the run folder.

    drawings: a folder, or a list of drawing files and folders (the app's Import).
    All of them are one job - one project - whatever folder each file is in.
    prefix_hints: what mark prefixes meant in earlier projects ({"SL": "slab"}) -
    hints only, rechecked against these drawings (building.prefixes).
    """
    from building import profiles
    from building.export import export
    from building.readset import read_set
    from building.sheets.__main__ import drawings as list_drawings

    paths = [drawings] if isinstance(drawings, str) else list(drawings)
    files = list(dict.fromkeys(list_drawings(paths)))
    if not files:
        raise RuntimeError("No DWG or DXF drawings were imported.")
    first = paths[0] if os.path.isdir(paths[0]) else os.path.dirname(os.path.abspath(paths[0]))
    project = project or os.path.join(os.path.dirname(os.path.abspath(first)), "_c2r_project")
    os.makedirs(project, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    run_dir = os.path.join(project, "runs", stamp)
    work = os.path.join(run_dir, "_work")
    cache = os.path.join(project, "dxf_cache")
    job = os.path.basename(os.path.normpath(project))
    log(f"Project: {project}")
    log(f"Drawings: {len(files)}")
    for f in files:
        log(f"  {os.path.basename(f)}")

    close_stale_converters(log)
    n = seed_cache(cache, work, files)
    if n:
        log(f"Reusing {n} converted drawing(s) from earlier runs.")

    names = dict(p.split("=", 1) for p in (levels or "").split(",") if "=" in p)
    confirmed = [profiles.load(p) for p in sorted(glob.glob(os.path.join(project, "profiles", "*.json")))]
    confirmed = [p for p in confirmed if p.get("confirmed")]
    ov_path = os.path.join(project, "overrides.json")
    overrides = profiles.load(ov_path) if os.path.exists(ov_path) else None
    if confirmed:
        log(f"Confirmed profiles: {', '.join(p.get('name', '?') for p in confirmed)}")
    if overrides:
        log("Applying your review decisions from overrides.json.")

    b = read_set(run_dir, files, names, job, log=log, profiles_in=confirmed, use_suggested=use_suggested,
                 overrides=overrides, set_name=job, prefix_hints=prefix_hints)
    keep_converted(work, cache, files)
    log("\nWriting the Revit files ...")
    export(run_dir, log=log, decisions=(overrides or {}).get("project"))
    from building import viewer3d
    if viewer3d.write(b, os.path.join(run_dir, "model3d.html"), (overrides or {}).get("project")):
        log("3D view: model3d.html")

    from building.linker import link
    stats = link(b, overrides)          # the counts of this run, for the summary
    summary = summarise(b, stats)
    summary["suggested_profiles_used"] = bool(use_suggested)
    summary["drawings"] = [os.path.basename(f) for f in files]
    summary["prebuild"] = _prebuild(run_dir, overrides)
    path = write_summary(run_dir, summary)
    log(f"\nSummary: {path}")
    return run_dir


def apply_decisions(run_dir: str, overrides_path: str | None = None, log=print) -> str:
    """Step 4 of the app: the review's decisions into the model, and the Revit files out.

    Re-links the analysed run with overrides.json (the file the review page exports)
    - the drawings are not read again - then rewrites the review page, workbook,
    3D view, summary and the Revit files. The decisions file is kept in the project
    folder, so every later analysis applies it too. Returns the Revit files folder.
    """
    from building import Building, excel, profiles, review, viewer3d
    from building.export import export
    from building.linker import link
    project = os.path.dirname(os.path.dirname(os.path.abspath(run_dir)))
    keep = os.path.join(project, "overrides.json")
    if overrides_path and os.path.abspath(overrides_path) != os.path.abspath(keep):
        shutil.copy2(overrides_path, keep)
        log(f"Decisions saved in the project: {keep}")
    overrides = profiles.load(keep) if os.path.exists(keep) else None
    b = Building.load(os.path.join(run_dir, "building.json"))
    stats = link(b, overrides)
    n = stats.get("overrides applied", 0)
    log(f"Applied {n} decision(s)." if overrides else "No decisions to apply - exporting as analysed.")
    b.save(os.path.join(run_dir, "building.json"))
    excel.write(b, os.path.join(run_dir, "review.xlsx"))
    review.write(b, os.path.join(run_dir, "review.html"), (overrides or {}).get("project"))
    viewer3d.write(b, os.path.join(run_dir, "model3d.html"), (overrides or {}).get("project"))
    out = export(run_dir, log=log, decisions=(overrides or {}).get("project"))
    summary = summarise(b, stats)
    summary["prebuild"] = _prebuild(run_dir, overrides)
    write_summary(run_dir, summary)
    log(f"Revit files: {out}")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("drawings")
    ap.add_argument("--project")
    ap.add_argument("--levels", default="")
    ap.add_argument("--use-suggested", action="store_true")
    a = ap.parse_args(argv)
    run_dir = run(a.drawings, a.project, a.levels, a.use_suggested)
    print(open(os.path.join(run_dir, "SUMMARY.txt"), encoding="utf-8").read())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
