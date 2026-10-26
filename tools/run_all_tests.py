r"""Run every test suite (and, with --baselines, every baseline) and say plainly what passed.

    python tools\run_all_tests.py              the test suites (about 1 minute)
    python tools\run_all_tests.py --baselines  + the locked baselines (about 10 minutes)

Each suite runs on its own; the table at the end is the answer. Warnings that
libraries print (and PowerShell shows in red) are not failures and are hidden.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SUITES = [
    ("Building model (lossless round trip)", ["building\\selftest.py"], ROOT),
    ("Sheet registry", ["-m", "building.sheets.selftest"], ROOT),
    ("Marks and labels", ["-m", "building.marks_selftest"], ROOT),
    ("Readers (stage 3)", ["-m", "building.readers.selftest"], ROOT),
    ("Linker, Excel, review (stage 4)", ["-m", "building.linker_selftest"], ROOT),
    ("Mark prefixes, near misses", ["-m", "building.prefixes_selftest"], ROOT),
    ("Foundations, column schedules (stage 5)", ["-m", "building.foundations_selftest"], ROOT),
    ("Walls, stairs, ramps (stage 6)", ["-m", "building.stage6_selftest"], ROOT),
    ("Levels (stage 7)", ["-m", "building.stage7_selftest"], ROOT),
    ("Export for Revit (stage 8)", ["-m", "building.stage8_selftest"], ROOT),
    ("App and workflow (stage 9)", ["-m", "building.stage9_selftest"], ROOT),
    ("Live Revit connection (MCP)", ["-m", "building.revit_mcp_selftest"], ROOT),
    ("Units, floors side by side, pre-build check", ["-m", "building.checks_selftest"], ROOT),
    ("IFC export, starting Revit by itself", ["-m", "building.ifc_selftest"], ROOT),
    ("Sign-in (approved users)", ["-m", "building.access_selftest"], ROOT),
    ("Office mode (colleagues' browsers)", ["-m", "building.office_selftest"], ROOT),
    ("Notes for the consultant, revisions", ["-m", "building.notes_selftest"], ROOT),
    ("Architect's model in 3D, section box", ["-m", "building.arch_selftest"], ROOT),
    ("Extractor", ["selftest.py"], os.path.join(ROOT, "cad2revit")),
    ("Revit build script", ["selftest.py"], os.path.join(ROOT, "revit")),
    ("Revit foundations / levels / stairs script", ["selftest_extras.py"], os.path.join(ROOT, "revit")),
    ("Rhino.Inside component", ["selftest_rir.py"], os.path.join(ROOT, "revit")),
]
BASELINES = [
    ("Baseline: extractor (J1153 floors)", ["tools\\baseline.py", "compare"], r"All cases match"),
    ("Baseline: sheet registry", ["tools\\sheets_baseline.py", "compare"], r"all identified as before"),
    ("Baseline: readers", ["tools\\readers_baseline.py", "compare"], r"all read as before"),
    ("Baseline: blind set (unseen style)", ["tools\\blind_baseline.py", "compare"], r"blind set: \{'correct'"),
]


def run(args, cwd):
    env = dict(os.environ, PYTHONWARNINGS="ignore")
    p = subprocess.run([sys.executable, "-W", "ignore"] + args, cwd=cwd, capture_output=True, text=True,
                       env=env, timeout=1800)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def main(argv=None) -> int:
    argv = argv or sys.argv[1:]
    rows, bad = [], 0
    for name, args, cwd in SUITES:
        t = time.time()
        code, out = run(args, cwd)
        m = re.search(r"Ran (\d+) tests?", out)
        n = int(m.group(1)) if m else 0
        sk = re.search(r"skipped=(\d+)", out)
        ok = code == 0 and re.search(r"^OK", out, re.M)
        note = f"{n} tests" + (f", {sk.group(1)} skipped (need a drawing not on this PC)" if sk else "")
        if not ok:
            bad += 1
            fails = re.findall(r"^(?:FAIL|ERROR): (.+)$", out, re.M)
            note += " - " + ("; ".join(fails[:3]) or out.strip().splitlines()[-1][:120])
        rows.append(("PASS" if ok else "FAIL", name, note, time.time() - t))
    if "--baselines" in argv:
        for name, args, want in BASELINES:
            t = time.time()
            code, out = run(args, ROOT)
            ok = code == 0 and re.search(want, out)
            if not ok:
                bad += 1
            last = [line for line in out.splitlines() if line.strip() and "copy process" not in line
                    and "Required line type" not in line]
            rows.append(("PASS" if ok else "FAIL", name, last[-1][:100] if last else "", time.time() - t))
    w = max(len(r[1]) for r in rows)
    print()
    for status, name, note, secs in rows:
        print(f"  {status}  {name.ljust(w)}  {note}  ({secs:.0f}s)")
    total = sum(int(re.match(r"\d+", r[2]).group()) for r in rows if re.match(r"\d+", r[2]))
    print(f"\n  {len(rows) - bad} of {len(rows)} passed, {total} tests in all." + ("" if not bad else f"  {bad} FAILED."))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
