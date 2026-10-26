r"""Score a run against an answer sheet - the blind test for drawings the tool has not seen.

    python tools\scorecard.py RUN_FOLDER ANSWERS.json

ANSWERS.json - what a person reads off the drawings, as much or as little as is
known (a few elements per type is enough to measure):

    {"columns":  [{"mark": "C1", "width": 300, "depth": 600, "level": "L1"}, ...],
     "beams":    [{"mark": "B12", "width": 230, "depth": 600}, ...],
     "slabs":    [{"mark": "S1", "thickness": 150}, ...],
     "footings": [{"mark": "F1", "width": 1800, "length": 1800, "depth": 450}, ...],
     "walls":    [{"mark": "SW1", "width": 230}, ...],
     "levels":   [{"level": "L1", "elevation": 3150}, ...]}

Every answer is one of:

    correct     the tool gives this value, settled (read or decided)
    flagged     the tool gives no settled value - conflict or missing - so a
                person is asked: safe, only slower
    WRONG       the tool gives a different value as settled: a silent error,
                the one result that must not happen
    not found   no element of this mark at all

Sizes are compared within 5 mm: a column's as narrow side x long side (either way
round), a beam's as width x depth in that order. An answer with a level is only
compared with elements on that level.
"""
from __future__ import annotations

import json
import os
import sys
from collections import Counter, defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

TYPES = {"columns": ("column", ("width", "depth")), "beams": ("beam", ("width", "depth")),
         "slabs": ("slab", ("thickness",)), "footings": ("footing", ("width", "length", "depth")),
         "walls": ("wall", ("width",))}
TOL = 5.0


def _key(mark) -> str:
    return "".join(ch for ch in str(mark or "").upper() if ch.isalnum())


def _level_of(b, e) -> str:
    s = b.sheets.get(e.sheet)
    return (s.extra.get("level_key") if s else "") or e.level or ""


def _elements(b, etype):
    els = [e for e in b.elements if e.type == etype or (etype == "beam" and e.type == "beam_span")]
    return [e for e in els if not e.extra.get("split_into") and not e.extra.get("merged_into")]


def judge(b, kind: str, ans: dict) -> tuple[str, str]:
    """(verdict, detail) for one answer."""
    etype, props = TYPES[kind]
    want_mark = _key(ans.get("mark"))
    cands = [e for e in _elements(b, etype) if _key(e.final("mark") or e.mark) == want_mark]
    if ans.get("level"):
        cands = [e for e in cands if _level_of(b, e) == ans["level"]]
    if not cands:
        return "not found", ""
    names = [p for p in props if ans.get(p) is not None]
    verdicts = Counter()
    details = []
    for e in cands:
        got = {}
        settled = True
        for p in names:
            v = e.resolved.get(p)
            if v is None or v.status not in ("read", "override") or v.value is None:
                settled = False
                break
            got[p] = float(v.value)
        if not settled:
            verdicts["flagged"] += 1
            continue
        want = [float(ans[p]) for p in names]
        have = [got[p] for p in names]
        if etype == "column" and len(names) == 2:
            # a column's width and depth depend on which way it is read; a beam's do not
            # (600 x 230 is a wide flat beam, not a 230 x 600 one)
            want, have = sorted(want), sorted(have)
        if all(abs(a - c) <= TOL for a, c in zip(want, have)):
            verdicts["correct"] += 1
        else:
            verdicts["WRONG"] += 1
            details.append(f"{e.id}: {' x '.join(f'{x:.0f}' for x in have)} (should be "
                           f"{' x '.join(f'{x:.0f}' for x in want)})")
    # one wrong element of the mark is a wrong answer: it would be built wrong
    for v in ("WRONG", "flagged", "correct"):
        if verdicts[v]:
            return v, (f"{verdicts[v]} of {len(cands)} elements; " + "; ".join(details[:3])).rstrip("; ")
    return "not found", ""


def score(b, answers: dict) -> dict:
    rows, totals = [], defaultdict(Counter)
    for kind in TYPES:
        for ans in answers.get(kind, []):
            v, d = judge(b, kind, ans)
            rows.append({"type": kind, "mark": ans.get("mark"), "level": ans.get("level", ""), "verdict": v,
                         "detail": d})
            totals[kind][v] += 1
    for ans in answers.get("levels", []):
        el = next((e for e in b.of("level") if e.level == ans.get("level")), None)
        v = el.resolved.get("elevation") if el else None
        if el is None:
            verdict = "not found"
        elif v is None or v.status not in ("read", "override") or v.value is None:
            verdict = "flagged"
        else:
            verdict = "correct" if abs(float(v.value) - float(ans["elevation"])) <= TOL else "WRONG"
        rows.append({"type": "levels", "mark": ans.get("level"), "level": "", "verdict": verdict,
                     "detail": "" if el is None or v is None else f"{v.value}"})
        totals["levels"][verdict] += 1
    allv = Counter()
    for c in totals.values():
        allv.update(c)
    n = sum(allv.values())
    return {"rows": rows, "totals": {k: dict(v) for k, v in totals.items()}, "all": dict(allv),
            "answers": n,
            "correct_pct": round(100.0 * allv["correct"] / n, 1) if n else 0.0,
            "silent_wrong_pct": round(100.0 * allv["WRONG"] / n, 1) if n else 0.0}


def report(s: dict) -> str:
    lines = [f"{s['answers']} answers: {s['all'].get('correct', 0)} correct ({s['correct_pct']}%), "
             f"{s['all'].get('flagged', 0)} flagged for review, {s['all'].get('WRONG', 0)} SILENTLY WRONG "
             f"({s['silent_wrong_pct']}%), {s['all'].get('not found', 0)} not found", ""]
    for kind, c in s["totals"].items():
        lines.append(f"  {kind:9s} " + ", ".join(f"{v} {k}" for k, v in sorted(c.items())))
    bad = [r for r in s["rows"] if r["verdict"] in ("WRONG", "not found")]
    if bad:
        lines += ["", "TO LOOK AT"] + [f"  {r['verdict']:9s} {r['type']} {r['mark']} {r['level']} {r['detail']}"
                                       for r in bad]
    return "\n".join(lines)


def main(argv=None) -> int:
    from building import Building
    argv = argv or sys.argv[1:]
    if len(argv) != 2:
        print(__doc__)
        return 2
    run, answers = argv
    b = Building.load(os.path.join(run, "building.json"))
    with open(answers, encoding="utf-8") as f:
        s = score(b, json.load(f))
    text = report(s)
    print(text)
    with open(os.path.join(run, "scorecard.json"), "w", encoding="utf-8") as f:
        json.dump(s, f, indent=1)
    with open(os.path.join(run, "scorecard.txt"), "w", encoding="utf-8") as f:
        f.write(text)
    return 1 if s["all"].get("WRONG") else 0


if __name__ == "__main__":
    raise SystemExit(main())
