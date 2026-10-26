"""Audit every analysed plan view (the app's runs; not whole cached drawings) with a code copy.
python audit_all.py CODE_ROOT OUTDIR"""
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import runall  # noqa: E402

code, outd = sys.argv[1], sys.argv[2]
os.makedirs(outd, exist_ok=True)
plans, seen = [], set()
for proj, plan, src, cfg in runall.plans():
    if proj.endswith("__cache") or proj == "samples" or src in seen:
        continue
    seen.add(src)
    plans.append((proj, plan, src, cfg))


def go(p):
    proj, plan, src, cfg = p
    o = os.path.join(outd, (proj + "__" + plan)[:150] + ".json")
    r = subprocess.run([sys.executable, "-W", "ignore", os.path.join(HERE, "audit_one.py"), code, src, cfg or "-", o],
                       capture_output=True, text=True, timeout=1800)
    if not os.path.exists(o):
        return proj, plan, None, (r.stderr.strip().splitlines() or ["?"])[-1]
    return proj, plan, json.load(open(o)), ""


tot = {}
with ThreadPoolExecutor(4) as ex:
    for proj, plan, rep, err in ex.map(go, plans):
        if rep is None:
            print("ERR", proj, plan, err)
            continue
        line = "  ".join(f"{k}={len(v)}" for k, v in rep.items())
        print(f"{proj} | {plan}: {line}", flush=True)
        for k, v in rep.items():
            tot[k] = tot.get(k, 0) + len(v)
print(len(plans), "plans;", tot)
