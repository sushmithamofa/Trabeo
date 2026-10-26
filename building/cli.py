r"""Read drawings into one building model.

    python -m building.cli OUT_FOLDER "plan1.dwg=Level 1" "plan2.dwg=Level 2" [--wing B]

For every drawing it runs the current extractor (unchanged) into
OUT_FOLDER\<sheet>\ - the same model.json and files as before, so the existing
Revit build script and Excel generator work on them as they always have - and
then writes OUT_FOLDER\building.json: all sheets, levels and elements in one
model, each value with its evidence and each element with its support status.
"""
from __future__ import annotations

import argparse
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from building import Building                       # noqa: E402
from building.readers import legacy_plan            # noqa: E402


def sheet_id(path: str, n: int) -> str:
    stem = re.sub(r"[^A-Za-z0-9]+", "_", os.path.splitext(os.path.basename(path))[0]).strip("_")
    return f"S{n:02d}_{stem[:40]}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out", help="output folder")
    ap.add_argument("drawings", nargs="+", help='"drawing.dwg=Level name" (level optional)')
    ap.add_argument("--wing", default="")
    ap.add_argument("--project", default="")
    a = ap.parse_args(argv)

    os.makedirs(a.out, exist_ok=True)
    b = Building(project=a.project)
    for n, item in enumerate(a.drawings, 1):
        path, _, level = item.partition("=")
        if not os.path.exists(path):
            print(f"No such drawing: {path}", file=sys.stderr)
            return 1
        sid = sheet_id(path, n)
        print(f"  {sid}  <- {os.path.basename(path)}  ({level or 'level from drawing'})")
        legacy_plan.read(path, os.path.join(a.out, sid), sid, level or None, b, a.wing)

    out = b.save(os.path.join(a.out, "building.json"))
    by_type: dict[str, int] = {}
    for e in b.elements:
        by_type[e.type] = by_type.get(e.type, 0) + 1
    print(f"\nWrote {out}")
    print(f"  {len(b.sheets)} sheets, levels {b.levels}")
    print("  " + ", ".join(f"{v} {k}" for k, v in sorted(by_type.items())))
    print(f"  {len(b.issues)} issues")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
