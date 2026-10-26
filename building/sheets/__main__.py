r"""List what every view in a set of drawings is.

    python -m building.sheets DRAWING_OR_FOLDER [...] [--json registry.json] [--why]

Prints one line per view: where it is, its title, what kind of drawing it was
judged to be (and how sure), its level and wing. --why adds the reasons.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import tempfile

from . import register
from .notes import summary


def drawings(paths: list[str]) -> list[str]:
    out = []
    for p in paths:
        if os.path.isdir(p):
            out += sorted(glob.glob(os.path.join(p, "**", "*.dwg"), recursive=True))
            out += sorted(glob.glob(os.path.join(p, "**", "*.dxf"), recursive=True))
        else:
            out.append(p)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--json", help="write the registry here")
    ap.add_argument("--why", action="store_true", help="show the reasons for each kind")
    ap.add_argument("--work", help="folder for converted DXFs (kept, so re-runs are fast)")
    a = ap.parse_args(argv)

    work = a.work or os.path.join(tempfile.gettempdir(), "c2r_sheets")
    allrows = []
    for path in drawings(a.paths):
        print(f"\n{os.path.basename(path)}")
        try:
            recs = register(path, work)
        except Exception as exc:
            print(f"  could not read: {type(exc).__name__}: {exc}")
            continue
        strays = [r for r in recs if r.view.source == "stray"]
        for r in recs:
            row = r.row()
            allrows.append({**row, "file": path, "reasons": r.verdict.reasons,
                            "notes": summary(r.notes), "title_from": r.title_from})
            if r.view.source == "stray":
                continue
            flags = (f"  = {row['same_as']}" if row["same_as"] else "") + \
                    ("  [LEVEL CONFLICT: " + "; ".join(f"{s} says {','.join(f)}" for s, _, f in r.level_sources)
                     + "]" if row["level_conflict"] else "") + \
                    (f"  (also {row['also']})" if row["also"] else "")
            print(f"  {r.view.id:18s} {row['source'][:8]:8s} {row['size_m']:>9s} m {row['entities']:5d} ents"
                  f"  {row['kind']:16s} {row['confidence']:.2f}  lvl {row['levels'] or '-':10s}"
                  f" wing {row['wing'] or '-':5s} | {row['title'][:55]}{flags}")
            if a.why:
                for reason in r.verdict.reasons[:6]:
                    print(f"        - {reason}")
                notes = summary(r.notes)
                if notes:
                    print(f"        notes: {notes}")
        if strays:
            print(f"  + {len(strays)} stray scraps ({sum(len(r.view.handles) for r in strays)} entities) "
                  "away from every view: listed, not read")
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(allrows, f, indent=1, default=str)
        print(f"\nWrote {a.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
