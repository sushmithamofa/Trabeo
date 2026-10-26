r"""The pre-build check: before anything goes to Revit, what is ready to build, what
needs a decision, and what the app does not build yet.

    python -m building.prebuild RUN_FOLDER [--overrides overrides.json]

Reads what the export wrote (revit_build\check.json, levels.json) and adds the floor
levels: every level the build needs - each floor's, and the one the lowest floor's
columns rise from - with its elevation and where it came from (the drawings, or a
decision on the Export page). A level with neither blocks the build; nothing is
assumed for it. Elements needing a decision do not block it: they are simply not
built until decided, and listed so nobody mistakes the model for complete.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def check(run: str, decisions: dict | None = None) -> dict:
    """{ready, needs_decision, not_supported, levels, levels_missing, founding_missing,
    blocked_drawings, can_build, exported}."""
    from building.revit_mcp import level_targets
    rb = os.path.join(run or "", "revit_build")
    out = {"exported": os.path.isdir(rb), "ready": {}, "needs_decision": [], "not_supported": [],
           "founding_missing": 0, "blocked_drawings": [], "levels": [], "levels_missing": []}
    if not out["exported"]:
        return {**out, "can_build": False}
    try:
        with open(os.path.join(rb, "check.json"), encoding="utf-8") as f:
            out.update(json.load(f))
    except (OSError, ValueError):
        out["stale"] = True          # exported before the check existed: export again
    out["levels"] = [{"name": n, "elevation_mm": z, "source": src} for n, z, src in level_targets(run, decisions)]
    out["levels_missing"] = [lv["name"] for lv in out["levels"] if lv["elevation_mm"] is None]
    # into a new file every level needs an elevation; into the user's own model a level
    # they already have counts (revit_mcp.build checks that against Revit)
    out["can_build"] = not out["levels_missing"]
    out["level_hints"] = level_hints(run)
    return out


def level_hints(run: str) -> list[dict]:
    """Levels the drawings write with a value, as written - shown beside the floor levels
    so a person can take one. "CIR 1 LVL B BLOCK +9500" names no floor the app knows,
    so it is offered, never filled in by itself."""
    try:
        with open(os.path.join(run, "building.json"), encoding="utf-8") as f:
            els = json.load(f).get("elements") or []
    except (OSError, ValueError):
        return []
    from building.readers.elevations import NAMEWORD
    seen, out = set(), []
    for e in els:
        p = e.get("props") or {}
        if e.get("type") != "level_mark" or (p.get("kind") or {}).get("value") not in ("floor", "local"):
            continue
        z = (p.get("elevation") or {}).get("value")
        ev = (p.get("elevation") or {}).get("evidence") or [{}]
        text = " ".join((ev[0].get("text") or "").split())
        if z is None or not NAMEWORD.search(text) or (text, z) in seen:
            continue
        seen.add((text, z))
        out.append({"text": text, "elevation_mm": z})
    return sorted(out, key=lambda h: h["elevation_mm"])[:12]


def lines(c: dict) -> list[str]:
    """The check in plain words, for a log or the Rhino.Inside component."""
    r = c.get("ready") or {}
    out = ["READY TO BUILD: " + (", ".join(f"{v} {k}" for k, v in r.items()) or "nothing")]
    nd = c.get("needs_decision") or []
    out.append(f"NEEDS A DECISION (not built until decided in the review): {len(nd)}")
    out += [f"  {x['where']}: {x['what']}" for x in nd[:30]] + ([f"  ... and {len(nd) - 30} more"] if len(nd) > 30 else [])
    ns = c.get("not_supported") or []
    out.append(f"NOT BUILT BY THE APP YET: {len(ns)}")
    out += [f"  {x['where']}: {x['what']}" for x in ns[:15]]
    for d in c.get("blocked_drawings") or []:
        out.append(f"DRAWING NOT READ: {d}")
    if c.get("levels_missing"):
        out.append("LEVELS WITHOUT AN ELEVATION (the build stops for these): " + ", ".join(c["levels_missing"]))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run")
    ap.add_argument("--overrides")
    a = ap.parse_args(argv)
    dec = {}
    if a.overrides:
        with open(a.overrides, encoding="utf-8") as f:
            dec = json.load(f).get("project") or {}
    print("\n".join(lines(check(a.run, dec))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
