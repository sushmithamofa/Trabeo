r"""Lock what the sheet registry says about every training drawing.

    python tools\sheets_baseline.py capture
    python tools\sheets_baseline.py compare

For every drawing under samples\training: each view's kind, title, levels,
wing, level conflict and duplicate. `compare` lists any view that is now
identified differently, so an improvement for one consultant cannot quietly
change another's. Locked is not the same as correct: KNOWN_DOUBTS lists the
entries still waiting for a person to confirm.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
OUT = os.path.join(ROOT, "baselines", "sheets_expected.json")
WORK = os.path.join(tempfile.gettempdir(), "c2r_sheets")

# Locked as they are today, but not yet confirmed by a person.
KNOWN_DOUBTS = {
    "PLINTH BEAM LAYOUT.dwg COL LAY#1": "title says GROUND LVL, file says PLINTH: which level?",
    "VILLA5.dwg U1": "raft layout merged with other details: layouts point at empty model space",
    "VILLA5.dwg U5": "179 x 474 m region: scattered leftovers, not one drawing",
    "08_09_2026_1153 - 2ND FLOOR PLAN ( B WING )dwg.dwg M1": "title found is only 'PLAN SHOWING'",
}


def snapshot() -> dict:
    from building.sheets import register
    out = {}
    for f in sorted(glob.glob(os.path.join(ROOT, "samples", "training", "**", "*.dwg"), recursive=True)):
        name = os.path.basename(f)
        for r in register(f, WORK):
            if r.view.source == "stray":
                continue
            out[f"{name} {r.view.id}"] = {
                "source": r.view.source, "kind": r.verdict.kind, "title": r.title,
                "levels": r.levels, "wing": r.wing, "level_conflict": r.level_conflict,
                "same_as": r.view.same_as, "entities": len(r.view.handles)}
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
        print(f"captured {len(now)} views -> {OUT}")
        return 0
    with open(OUT, encoding="utf-8") as f:
        was = json.load(f)
    changed = 0
    for key in sorted(set(was) | set(now)):
        old, new = was.get(key), now.get(key)
        if old == new:
            continue
        changed += 1
        if old is None:
            print(f"  + {key}: {new['kind']} | {new['title'][:50]}")
        elif new is None:
            print(f"  - {key}: was {old['kind']} | {old['title'][:50]}")
        else:
            diffs = [f"{k} {old[k]!r} -> {new[k]!r}" for k in old if old[k] != new.get(k)]
            print(f"  ~ {key}: " + "; ".join(diffs))
    print(f"\n{len(now)} views, " + ("all identified as before." if not changed
                                     else f"{changed} identified differently."))
    if KNOWN_DOUBTS:
        print(f"{len(KNOWN_DOUBTS)} entries are locked but still need a person to confirm "
              "(see KNOWN_DOUBTS).")
    return 1 if changed else 0


if __name__ == "__main__":
    raise SystemExit(main())
