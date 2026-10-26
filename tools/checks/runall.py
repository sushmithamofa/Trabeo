"""Run the plan reader (extractor) on every analysed plan on this PC, with a given copy of
the code, into OUT\<project>\<plan>\model.json - for before/after comparisons.
python runall.py CODE_DIR OUT [--list]"""
import glob
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

ROOTS = [r"C:\Users\Admin\Downloads", r"C:\Users\Admin\Documents", r"C:\Users\Admin\Desktop\Shushmita"]


def plans():
    out = []
    for root in ROOTS:
        for base, dirs, files in os.walk(root):
            if "scratchpad" in base or "\\Temp\\" in base or base.count(os.sep) > 9:
                dirs[:] = []
                continue
            if os.path.basename(base) != "runs":
                continue
            dirs[:] = []
            runs = sorted(d for d in os.listdir(base) if os.path.exists(os.path.join(base, d, "building.json")))
            if not runs:
                continue
            run = os.path.join(base, runs[-1])
            work = os.path.join(run, "_work")
            cfg = os.path.join(work, "config_with_profile.json")
            proj = os.path.basename(os.path.dirname(base))
            whole = [p for p in glob.glob(os.path.join(work, "*.dxf")) if not p.endswith(("_view.dxf", "_floor.dxf"))]
            for d in sorted(os.listdir(run)):
                if not os.path.exists(os.path.join(run, d, "model.json")):
                    continue
                stem = d[:-5] if d.endswith("_plan") else d
                src = next((p for p in (os.path.join(work, stem + "_view.dxf"), os.path.join(work, stem + "_floor.dxf"),
                                        os.path.join(work, d + "_view.dxf")) if os.path.exists(p)), "")
                if not src and len(whole) == 1:
                    src = whole[0]
                if src:
                    out.append((proj + "__" + runs[-1], d, src, cfg if os.path.exists(cfg) else ""))
            # every converted drawing of the project, read whole with the default settings
            for p in sorted(glob.glob(os.path.join(os.path.dirname(base), "dxf_cache", "*.dxf"))):
                if os.path.getsize(p) < 25 * 1048576:
                    out.append((proj + "__cache", os.path.splitext(os.path.basename(p))[0], p, ""))
    for p in sorted(glob.glob(r"C:\Users\Admin\Documents\Structure CAD to Revit\New version\samples\**\*.dxf", recursive=True)):
        if os.path.getsize(p) < 25 * 1048576:
            out.append(("samples", os.path.relpath(p, r"C:\Users\Admin\Documents\Structure CAD to Revit\New version\samples")
                        .replace(os.sep, "__")[:-4], p, ""))
    return out


ONE = r'''
import sys,os
sys.path.insert(0, os.path.join(sys.argv[1], "cad2revit"))
from core import pipeline
pipeline.run(sys.argv[2], out_dir=sys.argv[3], config_path=sys.argv[4] or None, level_name=None, log=lambda *a, **k: None)
'''


def main():
    code, out = sys.argv[1], sys.argv[2]
    ps = plans()
    if "--list" in sys.argv:
        for p in ps:
            print(p[0], "|", p[1])
        print(len(ps), "plans")
        return
    os.makedirs(out, exist_ok=True)
    script = os.path.join(out, "_one.py")
    open(script, "w").write(ONE)

    def go(p):
        proj, plan, src, cfg = p
        d = os.path.join(out, proj, plan)
        os.makedirs(d, exist_ok=True)
        r = subprocess.run([sys.executable, "-W", "ignore", script, code, src, d, cfg], capture_output=True, text=True,
                           timeout=1800)
        ok = os.path.exists(os.path.join(d, "model.json"))
        return f"{'ok ' if ok else 'ERR'} {proj} | {plan}" + ("" if ok else " " + r.stderr.strip().splitlines()[-1][:200])
    with ThreadPoolExecutor(4) as ex:
        for line in ex.map(go, ps):
            print(line, flush=True)


if __name__ == "__main__":
    main()
