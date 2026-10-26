r"""Lock the blind-test score: a drawing set in a style the tool was not built on.

    python tools\blind_baseline.py capture
    python tools\blind_baseline.py compare

Regenerates samples\synthetic\KA_consultant (tools\make_synthetic_set.py), runs the
whole workflow on it in a temporary project, and scores the result against its
answers (tools\scorecard.py). `compare` fails if any answer becomes SILENTLY WRONG
or fewer answers are correct than when captured - an improvement for the training
drawings must not cost what was gained on a style the tool has not seen.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
OUT = os.path.join(ROOT, "baselines", "blind_expected.json")
SET = os.path.join(ROOT, "samples", "synthetic", "KA_consultant")


def snapshot() -> dict:
    import make_synthetic_set
    import scorecard
    from building import Building
    from building.workflow import run
    make_synthetic_set.main(SET)
    with open(SET + "_answers.json", encoding="utf-8") as f:
        answers = json.load(f)
    project = tempfile.mkdtemp(prefix="c2r_blind_")
    run_dir = run(SET, project, "L1=Level 1,L2=Level 2", use_suggested=True, log=lambda *a, **k: None)
    s = scorecard.score(Building.load(os.path.join(run_dir, "building.json")), answers)
    return {"all": s["all"], "totals": s["totals"],
            "rows": {f"{r['type']}:{r['mark']}": r["verdict"] for r in s["rows"]}}


def main(argv=None) -> int:
    action = (argv or sys.argv[1:] or ["compare"])[0]
    now = snapshot()
    if action == "capture":
        with open(OUT, "w", encoding="utf-8") as f:
            json.dump(now, f, indent=1, sort_keys=True)
        print(f"captured: {now['all']}")
        return 0
    with open(OUT, encoding="utf-8") as f:
        was = json.load(f)
    bad = []
    if now["all"].get("WRONG"):
        bad.append(f"{now['all']['WRONG']} answers SILENTLY WRONG")
    if now["all"].get("correct", 0) < was["all"].get("correct", 0):
        bad.append(f"correct {was['all'].get('correct', 0)} -> {now['all'].get('correct', 0)}")
    lost = [k for k, v in was["rows"].items() if v == "correct" and now["rows"].get(k) != "correct"]
    gained = [k for k, v in now["rows"].items() if v == "correct" and was["rows"].get(k) != "correct"]
    for k in lost:
        print(f"  - {k}: correct -> {now['rows'].get(k)}")
    for k in gained:
        print(f"  + {k}: {was['rows'].get(k)} -> correct")
    print(f"blind set: {now['all']}" + ("" if not bad else "   PROBLEM: " + "; ".join(bad)))
    return 1 if bad or lost else 0


if __name__ == "__main__":
    raise SystemExit(main())
