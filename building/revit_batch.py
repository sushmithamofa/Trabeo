r"""Create a .rvt with no live connection: Revit started by the app runs the build itself.

Only Revit can write a .rvt, so Revit is started - but nobody has to be at it and the
MCP plug-in plays no part (that is for building into a model the user has open). The
app's own add-in (revit\c2r_batch\C2RBatch.dll, registered in Revit's Addins folder)
does nothing in a normal Revit session. Started with C2R_JOB set, it makes the new
project, its levels and families, runs revit\build_model.py on every floor and
revit\build_extras.py exactly as the live connection does, saves the .rvt and writes
a result file; the app then closes that Revit. A Revit the user has open is never
touched: this is a separate Revit process.

    build_file(run, path, decisions=..., log=...)
"""
from __future__ import annotations

import glob
import io
import json
import os
import re
import subprocess
import tempfile
import time
import uuid

from building import revit_mcp
from building.revit_mcp import RevitError

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DLL = os.path.join(ROOT, "revit", "c2r_batch", "C2RBatch.dll")
PROGRAM_FILES = os.environ.get("ProgramW6432") or os.environ.get("ProgramFiles") or r"C:\Program Files"
ADDINS = os.path.join(os.environ.get("APPDATA", ""), "Autodesk", "Revit", "Addins")
ADDIN_ID = "6f1d2c1e-3b8a-4f0e-9c52-0c2a7d1e5b31"
MANIFEST = """<?xml version="1.0" encoding="utf-8"?>
<RevitAddIns>
  <AddIn Type="Application">
    <Name>CAD to Revit - make a Revit file</Name>
    <Assembly>%s</Assembly>
    <AddInId>%s</AddInId>
    <FullClassName>CadToRevit.Batch</FullClassName>
    <VendorId>CAD2REVIT</VendorId>
    <VendorDescription>CAD to Revit: builds a new .rvt when the app asks; idle otherwise</VendorDescription>
  </AddIn>
</RevitAddIns>
"""


def revits() -> list[tuple[str, str]]:
    """[(version, Revit.exe)] the add-in runs in (Revit 2025 and later, .NET 8), newest first."""
    out = []
    for exe in glob.glob(os.path.join(PROGRAM_FILES, "Autodesk", "Revit 20*", "Revit.exe")):
        m = re.search(r"Revit (20\d\d)", exe)
        if m and m.group(1) >= "2025":
            out.append((m.group(1), exe))
    return sorted(out, reverse=True)


def install(ver: str) -> str:
    """Register the add-in with that Revit (for this Windows user). Returns the manifest."""
    if not os.path.exists(DLL):
        raise RevitError("The app's Revit add-in is missing (revit\\c2r_batch\\C2RBatch.dll). Reinstall the app.")
    d = os.path.join(ADDINS, ver)
    # a copy named by its content: a Revit holding an older copy open never blocks an update
    import hashlib
    with open(DLL, "rb") as f:
        h = hashlib.sha1(f.read()).hexdigest()[:10]
    lib = os.path.join(d, "CADtoRevit")
    os.makedirs(lib, exist_ok=True)
    dll = os.path.join(lib, "C2RBatch_%s.dll" % h)
    if not os.path.exists(dll):
        import shutil
        shutil.copy2(DLL, dll)
    for old in glob.glob(os.path.join(lib, "C2RBatch_*.dll")):
        if old != dll:
            try:
                os.remove(old)
            except OSError:
                pass                          # still loaded in a Revit: removed next time
    path = os.path.join(d, "CADtoRevit.Batch.addin")
    text = MANIFEST % (dll, ADDIN_ID)
    try:
        with io.open(path, encoding="utf-8") as f:
            if f.read() == text:
                return path
    except OSError:
        pass
    with io.open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def job_for(run: str, new_file: str, decisions: dict | None, ver: str, template: str = "") -> dict:
    """What the add-in does: the same levels, families and scripts as revit_mcp.build."""
    rir = revit_mcp._rir()
    fl = rir.exported_floors(run)
    if not fl:
        raise RevitError("This analysis has no floor to build. Export to Revit first.")
    lv = revit_mcp.level_targets(run, decisions)
    missing = revit_mcp.levels_needed(lv, {})
    if missing:
        raise RevitError("These levels need an elevation first: %s. The drawings give none, so nothing was "
                         "built. Enter them on the Export page under 'Floor levels'." % ", ".join(missing))
    ipy = revit_mcp.ironpython(ver)
    if not ipy[0]:
        raise RevitError("The Python that runs inside Revit is missing (revit\\ironpython3 in the app folder). "
                         "Reinstall the app.")
    steps = [{"label": "%d of %d: %s" % (i, len(fl), label), "script": os.path.join(revit_mcp.REVIT_DIR, "build_model.py"),
              "inputs": {"model_json": mj, "level_name": lvl or "", "mode": "replace"}}
             for i, (label, mj, lvl) in enumerate(fl, 1)]
    fj = rir.foundations_file(run)
    if fj:
        steps.append({"label": "foundations, levels and stairs",
                      "script": os.path.join(revit_mcp.REVIT_DIR, "build_extras.py"), "inputs": {"extras_json": fj}})
    return {"out": os.path.abspath(new_file), "template": template or "",
            "levels": [[n, "%.1f" % z, src] for n, z, src in lv if z is not None],
            "column_family": revit_mcp.family_file("column", ver), "beam_family": revit_mcp.family_file("beam", ver),
            # Revit's footing family too, when there are footings to build
            "footing_family": revit_mcp.family_file("footing", ver) if fj else "",
            "ipy": ipy[0], "lib": ipy[1] or "", "steps": steps}


def build_file(run: str, new_file: str, decisions: dict | None = None, log=print, wait_s: float = 3600.0,
               template: str = "") -> str:
    """Make new_file (.rvt) from the exported analysis in a Revit the app starts and closes."""
    inst = revits()
    if not inst:
        raise RevitError("No Revit 2025 or later is installed on this PC. Create an IFC file instead "
                         "(no Revit needed).")
    ver, exe = inst[0]
    job = job_for(run, new_file, decisions, ver, template)
    install(ver)
    os.makedirs(os.path.dirname(job["out"]), exist_ok=True)
    tag = uuid.uuid4().hex[:10]
    tmp = tempfile.gettempdir()
    job["log"] = os.path.join(tmp, "c2r_batch_%s.log" % tag)
    job["result"] = os.path.join(tmp, "c2r_batch_%s.json" % tag)
    job_path = os.path.join(tmp, "c2r_batch_%s_job.json" % tag)
    with io.open(job_path, "w", encoding="utf-8") as f:
        json.dump(job, f, indent=1)
    log("REVIT Starting Revit %s to make the file - no live connection needed, nothing to do in Revit "
        "(a few minutes)." % ver)
    log("REVIT The first time only, Revit asks whether to load the 'CAD to Revit' add-in: choose 'Always Load'.")
    proc = subprocess.Popen([exe, "/nosplash"], cwd=os.path.dirname(exe), env=dict(os.environ, C2R_JOB=job_path))
    seen, t0 = 0, time.time()
    try:
        while True:
            lines = _read_lines(job["log"])
            for line in lines[seen:]:
                log(line)
            seen = len(lines)
            if os.path.exists(job["result"]):
                time.sleep(0.5)
                break
            if proc.poll() is not None:
                raise RevitError("Revit closed before the file was made (exit code %s)." % proc.returncode)
            if time.time() - t0 > wait_s:
                raise RevitError("Revit did not finish within %d minutes." % (wait_s // 60))
            time.sleep(2)
        for line in _read_lines(job["log"])[seen:]:
            log(line)
        with io.open(job["result"], encoding="utf-8") as f:
            res = json.load(f)
    finally:
        _close(proc, log)
        for p in (job_path, job["log"], job["result"]):
            try:
                os.remove(p)
            except OSError:
                pass
    problems = []
    for st in res.get("steps") or []:
        text = st.get("result") or ""
        head, _, body = text.partition("\n")
        if head.strip() == "ERROR":
            log("REVIT PROBLEM %s: %s" % (st.get("label"), body.strip().splitlines()[-1] if body.strip() else "error"))
            for x in body.splitlines():
                log("  " + x)
            problems.append(st.get("label"))
        else:
            problems += revit_mcp._report(body, st.get("label"), log)
    # Revit keeps a backup (name.0001.rvt) when a file is saved twice - this run's only, not wanted
    stem = os.path.splitext(job["out"])[0]
    for b in glob.glob(glob.escape(stem) + ".0*.rvt"):
        try:
            if os.path.getmtime(b) >= t0 - 5:
                os.remove(b)
        except OSError:
            pass
    if not res.get("ok"):
        raise RevitError(res.get("error") or "Revit stopped.")
    inside = res.get("in_file") or {}
    if not any(inside.get(k) for k in ("columns", "beams", "slabs")):
        raise RevitError("Revit saved the file, but with no columns, beams or slabs in it - the build did not "
                         "stand. The details above say why.")
    log("REVIT DONE" if not problems else "REVIT DONE: %d step%s stopped - %s" % (
        len(problems), "" if len(problems) == 1 else "s", problems[0]))
    return job["out"]


def _read_lines(path: str) -> list[str]:
    try:
        with io.open(path, encoding="utf-8", errors="replace") as f:
            return [x.rstrip("\n") for x in f if x.strip()]
    except OSError:
        return []


def _close(proc, log) -> None:
    """Close the Revit this module started; the file is already saved and closed."""
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=60)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    log("  Closed the Revit the app started.")
