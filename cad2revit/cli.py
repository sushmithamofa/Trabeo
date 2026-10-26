"""Command line version, for batching a whole set of floors.

  python cli.py "path\\to\\plan.dwg"
  python cli.py *.dwg --out "C:\\extracted"
  python cli.py plan.dwg --config my-consultant.json
  python cli.py plan.dwg --inspect          (list layers, change nothing)

Exit code is 1 if any drawing produced a blocker, so it can gate a build step.
"""
from __future__ import annotations

import argparse
import glob
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from core import inspect_layers, pipeline    # noqa: E402
from core.model import BLOCKER               # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Extract structural elements from a CAD plan.")
    ap.add_argument("drawing", nargs="+", help="DWG or DXF file(s); wildcards allowed")
    ap.add_argument("--out", help="output folder (default: beside each drawing)")
    ap.add_argument("--config", help="layer rules JSON (default: config.json)")
    ap.add_argument("--inspect", action="store_true",
                    help="list the layers with a suggested role and exit; extracts nothing")
    ap.add_argument("--level", help="Revit level the floor goes on, e.g. \"Level 2\" or "
                                    "\"Terrace\" (default: read from the drawing's name)")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)

    paths: list[str] = []
    for pat in a.drawing:
        hits = glob.glob(pat)
        paths.extend(hits or [pat])

    log = (lambda *_: None) if a.quiet else print

    if a.inspect:
        import tempfile, shutil, os as _os
        from core import dwgconv
        cfg = pipeline.load_config(a.config)
        for p_ in paths:
            tmp = tempfile.mkdtemp(prefix="c2r_ins_")
            try:
                dxf = dwgconv.to_dxf(p_, _os.path.join(tmp, "inspect.dxf"), log=log)
                rows = inspect_layers.inspect(dxf, cfg)
                print(inspect_layers.format_table(rows, p_))
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
        return 0

    blockers = 0
    for p in paths:
        print(f"\n=== {os.path.basename(p)} ===")
        try:
            res, out_dir, _ = pipeline.run(p, out_dir=a.out, config_path=a.config, log=log,
                                           level_name=a.level or pipeline.guess_level(p))
            n = res.issue_counts().get(BLOCKER, 0)
            blockers += n
            print(f"    -> {out_dir}")
        except Exception as exc:
            print(f"    FAILED: {exc}", file=sys.stderr)
            blockers += 1
    return 1 if blockers else 0


if __name__ == "__main__":
    raise SystemExit(main())
