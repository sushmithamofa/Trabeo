r"""Lock what the Stage 3 readers read from every training drawing.

    python tools\readers_baseline.py capture
    python tools\readers_baseline.py compare

Per drawing: the suggested layer roles, and per view how many captions, specs,
tables and bar callouts were read, and every mark's sizes. `compare` lists any
mark whose size changed and any view whose counts changed - an improvement for
one consultant must not quietly change another's.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import tempfile
from collections import Counter, defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
OUT = os.path.join(ROOT, "baselines", "readers_expected.json")
WORK = os.path.join(tempfile.gettempdir(), "c2r_sheets")


def snapshot() -> dict:
    from building import Building
    from building.profiles.suggest import suggest
    from building.readers import column_schedule, details, elevations, labels, layer_sizes, stairs, tables
    from building.sheets import register
    out = {}
    for f in sorted(glob.glob(os.path.join(ROOT, "samples", "training", "**", "*.dwg"), recursive=True)):
        name = os.path.basename(f)
        recs = register(f, WORK)
        b = Building()
        views = {}
        for r in recs:
            if r.view.source in ("stray", "overview") or r.view.same_as:
                continue
            got = {**labels.read(b, r), **tables.read(b, r), **details.read(b, r),
                   **column_schedule.read(b, r), **stairs.read(b, r), **elevations.read(b, r)}
            got["layer sizes"] = layer_sizes.read(b, r)
            got = {k: v for k, v in got.items() if v}
            if got:
                views[r.view.id] = got
        sizes = defaultdict(set)
        for e in b.elements:
            if e.type == "level_mark":
                sizes[f"levels@{e.sheet.rsplit(':', 1)[-1]}"].add(json.dumps((e.get("kind"), e.get("level"),
                                                                              e.get("elevation"))))
            if e.type == "stair":
                sizes[f"stair@{e.sheet.rsplit(':', 1)[-1]}"].add(json.dumps(
                    (e.get("going"), e.get("width"), e.get("treads"))))
            if e.type in ("spec", "caption"):
                s = (e.get("width"), e.get("depth"), e.get("thickness"))
                rng = e.extra.get("level_range")
                if e.extra.get("from") == "stair notes":
                    s = (e.get("riser"), e.get("tread"), None)
                if e.extra.get("from") == "wall schedule":
                    s = (e.get("width"), e.get("length"), e.get("thickness"))
                if any(s):
                    sizes[f"{e.wing or '-'}:{e.mark}" + (f"@{rng[0]}-{rng[1]}" if rng else "")].add(json.dumps(s))
        p = suggest(recs, name)
        out[name] = {"views": views,
                     "sizes": {k: sorted(v) for k, v in sorted(sizes.items())},
                     "suggested_roles": {r: sorted(ls) for r, ls in sorted(p["layer_roles"].items())}}
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=["capture", "compare"])
    a = ap.parse_args(argv)
    now = snapshot()
    if a.action == "capture":
        with open(OUT, "w", encoding="utf-8") as f:
            json.dump(now, f, indent=1, sort_keys=True)
        n = sum(len(d["sizes"]) for d in now.values())
        print(f"captured {len(now)} drawings, {n} marks with sizes -> {OUT}")
        return 0
    with open(OUT, encoding="utf-8") as f:
        was = json.load(f)
    changed = 0
    for name in sorted(set(was) | set(now)):
        old, new = was.get(name, {}), now.get(name, {})
        lines = []
        for v in sorted(set(old.get("views", {})) | set(new.get("views", {}))):
            if old.get("views", {}).get(v) != new.get("views", {}).get(v):
                lines.append(f"    view {v}: {old.get('views', {}).get(v)} -> {new.get('views', {}).get(v)}")
        for m in sorted(set(old.get("sizes", {})) | set(new.get("sizes", {}))):
            if old.get("sizes", {}).get(m) != new.get("sizes", {}).get(m):
                lines.append(f"    {m}: {old.get('sizes', {}).get(m)} -> {new.get('sizes', {}).get(m)}")
        if old.get("suggested_roles") != new.get("suggested_roles"):
            lines.append(f"    suggested roles: {old.get('suggested_roles')} -> {new.get('suggested_roles')}")
        if lines:
            changed += 1
            print(f"  ~ {name}")
            print("\n".join(lines[:25]) + (f"\n    ... {len(lines) - 25} more" if len(lines) > 25 else ""))
    print(f"\n{len(now)} drawings, " + ("all read as before." if not changed
                                        else f"{changed} read differently."))
    return 1 if changed else 0


if __name__ == "__main__":
    raise SystemExit(main())
