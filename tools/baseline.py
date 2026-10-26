r"""Lock what the extractor produces today, and check later runs against it.

    python tools\baseline.py capture            run every case, save as the baseline
    python tools\baseline.py compare            run every case, report any difference
    python tools\baseline.py compare --case B_L1

The cases are listed in baselines\cases.json: a drawing and the level it goes on.
A baseline is a snapshot of every element the extractor found, reduced to what
matters for the model (mark, kind, position, size, level, sunk depth) and rounded
to 1 mm, plus the rows the schedule workbook would get. Two runs that would build
the same Revit model compare equal; anything else is listed element by element.

Nothing here changes the extractor. It only runs it and reads what it wrote.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = os.path.join(ROOT, "baselines")
CASES = os.path.join(BASE, "cases.json")
sys.path.insert(0, os.path.join(ROOT, "cad2revit"))
sys.path.insert(0, os.path.join(ROOT, "standard"))


def r(v, nd=0):
    """Round for comparison; None stays None."""
    if v is None or v == "":
        return None
    try:
        return round(float(v), nd)
    except (TypeError, ValueError):
        return v


def pt(p):
    return [r(p[0]), r(p[1])] if p else None


def snapshot(model: dict) -> dict:
    """The parts of model.json that decide what Revit gets, in a stable order."""
    bx, by = (model.get("base_point") or [0.0, 0.0])[:2]

    def loc(p):
        return [r(p[0] - bx), r(p[1] - by)] if p else None

    def centre(outline):
        if not outline:
            return None
        return loc((sum(q[0] for q in outline) / len(outline), sum(q[1] for q in outline) / len(outline)))

    snap = {
        "level_name": model.get("level_name"),
        "counts": model.get("counts"),
        "issue_counts": model.get("issue_counts"),
        "columns": sorted(
            ({"mark": c.get("mark"), "kind": c.get("kind"), "at": loc((c["x"], c["y"])),
              "w": r(c.get("width")), "d": r(c.get("depth")), "rot": r(c.get("rotation"), 1)}
             for c in model.get("columns", [])), key=json.dumps),
        "beams": sorted(
            ({"mark": b.get("mark"), "kind": b.get("kind"),
              "ends": sorted([loc(b["start"]), loc(b["end"])]) if b.get("start") else None,
              "mid": loc(b["mid"]) if b.get("mid") else None,
              "w": r(b.get("width")), "d": r(b.get("depth")), "top": r(b.get("top_mm"))}
             for b in model.get("beams", [])), key=json.dumps),
        "walls": sorted(
            ({"mark": w.get("mark"), "t": r(w.get("thickness")),
              "ends": sorted([loc(w["start"]), loc(w["end"])]) if w.get("start") else None,
              "centre": centre(w.get("outline"))}
             for w in model.get("walls", [])), key=json.dumps),
        "slabs": sorted(
            ({"mark": s.get("mark"), "t": r(s.get("thickness")), "sunk": r(s.get("sunk_mm")),
              "centre": centre(s.get("outline")), "n": len(s.get("outline") or []),
              "holes": len(s.get("holes") or [])}
             for s in model.get("slabs", [])), key=json.dumps),
        "folds": sorted(
            ({"ends": sorted([loc(f["start"]), loc(f["end"])]) if f.get("start") else None,
              "w": r(f.get("width")), "d": r(f.get("depth")), "top": r(f.get("top_mm"))}
             for f in model.get("folds", [])), key=json.dumps),
        "sunk": sorted(
            ({"d": r(z.get("depth_mm")), "centre": centre(z.get("outline"))}
             for z in model.get("sunk", [])), key=json.dumps),
        "openings": sorted(
            ({"kind": o.get("kind"), "centre": centre(o.get("outline"))}
             for o in model.get("openings", [])), key=json.dumps),
        "issues": sorted(Counter(f"{i.get('severity')}|{i.get('category')}"
                                 for i in model.get("issues", [])).items()),
    }
    return snap


def workbook_rows(folder: str) -> dict:
    """What make_schedules would pre-fill from this run, sheet by sheet."""
    import make_schedules
    rows, _meta = make_schedules.read_analysis(folder, Counter())
    return {k: v for k, v in rows.items()}


def run_case(case: dict, out_dir: str) -> dict:
    from core import pipeline
    src = case["dwg"] if os.path.isabs(case["dwg"]) else os.path.join(ROOT, case["dwg"])
    pipeline.run(src, out_dir=out_dir, level_name=case.get("level"), log=lambda *a, **k: None)
    with open(os.path.join(out_dir, "model.json"), encoding="utf-8") as f:
        model = json.load(f)
    return {"model": snapshot(model), "workbook": workbook_rows(out_dir)}


def through_building(out_dir: str) -> list[str]:
    """The run's model.json, through the shared building model and back.

    Empty when nothing was lost; otherwise the differences. This is the Stage 1
    guarantee: the new model carries everything the working extractor produces.
    """
    sys.path.insert(0, ROOT)
    from building.readers import legacy_plan
    with open(os.path.join(out_dir, "model.json"), encoding="utf-8") as f:
        model = json.load(f)
    b = legacy_plan.from_legacy(json.loads(json.dumps(model)), "check")
    back = legacy_plan.to_legacy(b, "check")
    if back == model:
        return []
    return diff("building", snapshot(model), snapshot(back)) or ["  (a field outside the snapshot differs)"]


def diff(name: str, old, new, path="", out=None, limit=40):
    """Every difference between two snapshots, as readable lines."""
    out = [] if out is None else out
    if len(out) >= limit:
        return out
    if isinstance(old, dict) and isinstance(new, dict):
        for k in sorted(set(old) | set(new)):
            diff(name, old.get(k), new.get(k), f"{path}.{k}" if path else k, out, limit)
    elif isinstance(old, list) and isinstance(new, list) and (old or new) and \
            all(isinstance(x, dict) for x in old + new):
        ko = Counter(json.dumps(x, sort_keys=True) for x in old)
        kn = Counter(json.dumps(x, sort_keys=True) for x in new)
        for x in (ko - kn).elements():
            out.append(f"  - {path}: {x}")
        for x in (kn - ko).elements():
            out.append(f"  + {path}: {x}")
    elif old != new:
        out.append(f"  ~ {path}: {json.dumps(old)} -> {json.dumps(new)}")
    return out


def load_cases(only=None):
    with open(CASES, encoding="utf-8") as f:
        cases = json.load(f)["cases"]
    return [c for c in cases if not only or c["name"] in only]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=["capture", "compare"])
    ap.add_argument("--case", nargs="*", help="only these case names")
    ap.add_argument("--keep", action="store_true", help="keep the compare run's output folders")
    a = ap.parse_args(argv)

    failed = 0
    for case in load_cases(a.case):
        name = case["name"]
        target = os.path.join(BASE, name)
        if a.action == "capture":
            run_dir = os.path.join(target, "run")
            shutil.rmtree(run_dir, ignore_errors=True)
            os.makedirs(run_dir)
            snap = run_case(case, run_dir)
            with open(os.path.join(target, "expected.json"), "w", encoding="utf-8") as f:
                json.dump(snap, f, indent=1, sort_keys=True)
            m = snap["model"]["counts"]
            print(f"captured {name}: " + ", ".join(f"{v} {k}" for k, v in (m or {}).items()))
            continue

        with open(os.path.join(target, "expected.json"), encoding="utf-8") as f:
            expected = json.load(f)
        tmp = tempfile.mkdtemp(prefix=f"c2r_cmp_{name}_")
        try:
            got = run_case(case, tmp)
            lost = through_building(tmp)
        finally:
            if not a.keep:
                shutil.rmtree(tmp, ignore_errors=True)
        lines = diff(name, json.loads(json.dumps(expected)), json.loads(json.dumps(got)))
        if lost:
            lines += ["  building model round trip lost data:"] + lost
        if lines:
            failed += 1
            print(f"DIFFERENT  {name}  ({len(lines)}{'+' if len(lines) >= 40 else ''} differences)")
            print("\n".join(lines))
        else:
            print(f"same       {name}")
    if a.action == "compare":
        print("\nAll cases match the baseline." if not failed
              else f"\n{failed} case(s) differ from the baseline.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
