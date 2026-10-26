r"""CAD to Revit - inside Revit, through Rhino.Inside.Revit (a Grasshopper Python 3 component).

The same jobs as the browser app (run_app.bat), plus building straight into the
open Revit model. Paste this into a Grasshopper "Python 3 Script" component in
Rhino.Inside.Revit and give it these inputs (all optional except project):

    project       text     the job's project folder (the one the app uses)
    open_app      bool     a Button: opens the app (import / analyse / review) in a window
    floors        text     list: the floors to build, as listed in `plans` (empty = all)
    build         bool     a Button: builds the chosen floors, then foundations and stairs
    extras        bool     build foundations, pedestals, levels and stairs too (default True)

Each footing's founding level comes from the drawings or the review (one per footing,
or one for the whole job, set on the app's Export page) - this component assumes none.

outputs:  log (text)   plans (list of the exported floors, bottom up)   run (folder)

Why Grasshopper: Revit accepts changes only from its own API context, and a
Grasshopper component in Rhino.Inside is always in it (a button in a Rhino window
is not). The analysis runs in the PC's ordinary Python (the browser app's server),
so nothing has to be installed into Rhino's Python.

Building runs revit\build_model.py on each floor's model.json, bottom floor first
(each floor's columns rise from the one below), then revit\build_extras.py on the
foundations.json - exactly the scripts that are run by hand otherwise.
"""
import io
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(globals().get("__file__") or os.path.join(
    os.path.expanduser("~"), "Documents", "Structure CAD to Revit", "New version", "revit", "x.py")))
ROOT = os.path.dirname(HERE)

RANK = {"FDN": -10, "B2": -3, "B1": -2, "PLINTH": -0.5, "GF": 0, "STILT": 0, "MEZZ": 0.5,
        "TERRACE": 100, "ROOF": 101, "MUMTY": 102, "OHT": 103}


# ---------------------------------------------------------------- plain logic (tested outside Revit)
def latest_run(project):
    """The newest analysis of the project that has Revit files."""
    runs = os.path.join(project or "", "runs")
    if not os.path.isdir(runs):
        return None
    done = [os.path.join(runs, d) for d in sorted(os.listdir(runs))
            if os.path.isdir(os.path.join(runs, d, "revit_build"))]
    return done[-1] if done else None


def level_rank(key):
    if key in RANK:
        return RANK[key]
    if key and key.endswith("_LINTEL"):
        return level_rank(key[:-len("_LINTEL")]) + 0.4    # inside its storey
    if key and key[0] == "L" and key[1:].isdigit():
        return int(key[1:])
    return 50


def exported_floors(run):
    """[(label, model.json, level name)] bottom floor first: each floor's columns
    rise from the floor below, so the lower one has to be in the model first."""
    out = []
    rb = os.path.join(run or "", "revit_build")
    if not os.path.isdir(rb):
        return out
    for d in sorted(os.listdir(rb)):
        mj = os.path.join(rb, d, "model.json")
        if not os.path.exists(mj):
            continue
        with io.open(mj, encoding="utf-8") as f:
            m = json.load(f)
        if not (m.get("columns") or m.get("beams") or m.get("slabs")):
            continue                                # a schedules sheet: nothing to build
        plan = (m.get("export") or {}).get("plan", d)
        lvl = m.get("level_name") or ""
        label = "%s  (%s)" % (lvl or "?", plan)
        # a floor whose name tells no order (an added "Service Floor", "EL+6600") carries its place
        given = (m.get("export") or {}).get("level_rank")
        out.append((label, mj, lvl, given if isinstance(given, (int, float)) else level_rank(_key_of(lvl, plan))))
    out.sort(key=lambda r: (r[3], r[0]))
    return [(a, b, c) for a, b, c, _ in out]


def _key_of(level_name, plan):
    """A level name ('Level 2', 'GF', 'TERRACE', 'First Floor') to an order key."""
    s = (level_name or "").upper().replace("_", " ")
    if "LINTEL" in s.split():
        # 'Level 0 Lintel', 'GF LINTEL': inside its storey, after the floor itself
        base = _key_of(" ".join(w for w in s.split() if w != "LINTEL"), plan)
        return base + "_LINTEL" if base else ""
    words = {"GROUND": "GF", "FIRST": "L1", "SECOND": "L2", "THIRD": "L3", "FOURTH": "L4", "FIFTH": "L5",
             "SIXTH": "L6", "SEVENTH": "L7", "EIGHTH": "L8", "NINTH": "L9", "TENTH": "L10"}
    for w, k in words.items():
        if s.startswith(w):
            return k
    for k in RANK:
        if k in s.split():
            return k
    digits = "".join(ch for ch in s if ch.isdigit())
    if digits:
        return "L%d" % int(digits) if int(digits) > 0 else "GF"
    return ""


def chosen(floors, wanted):
    """The floors to build: all, or those whose label contains one of `wanted`."""
    wanted = [w for w in (wanted or []) if w]
    if not wanted:
        return floors
    return [f for f in floors if any(w.strip().lower() in f[0].lower() for w in wanted)]


def check_lines(run):
    """The export's pre-build check (revit_build\\check.json) in a few plain lines."""
    p = os.path.join(run or "", "revit_build", "check.json")
    if not os.path.exists(p):
        return []
    with io.open(p, encoding="utf-8") as f:
        c = json.load(f)
    r = c.get("ready") or {}
    out = ["Ready to build: " + (", ".join("%s %s" % (v, k) for k, v in r.items()) or "nothing")]
    nd = c.get("needs_decision") or []
    if nd:
        out.append("Not built until decided in the review: %d (sizes in conflict or not given, footings "
                   "with no founding level) - see the app's Export page." % len(nd))
    return out


def foundations_file(run):
    rb = os.path.join(run or "", "revit_build")
    if not os.path.isdir(rb):
        return None
    for d in sorted(os.listdir(rb)):
        p = os.path.join(rb, d, "foundations.json")
        if os.path.exists(p):
            return p
    return None


# ---------------------------------------------------------------- inside Rhino.Inside.Revit
def _run_script(path, inputs):
    """Run one of the Revit scripts in this process, with its inputs as globals,
    as if it were pasted into this component. Returns its log."""
    g = {"__name__": "__main__", "__file__": path}
    g.update(inputs)
    with io.open(path, encoding="utf-8") as f:
        code = compile(f.read(), path, "exec")
    exec(code, g)
    return g.get("OUT") or ""


def _server_url():
    """The browser app's server: started with the PC's Python if it is not running."""
    import socket
    for port in range(8765, 8815):
        s = socket.socket()
        try:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return "http://127.0.0.1:%d/" % port
        finally:
            s.close()
    py = "python"
    subprocess.Popen([py, os.path.join(ROOT, "webapp.py")], cwd=ROOT, creationflags=0x08000000)
    import time
    for _ in range(40):
        time.sleep(0.25)
        for port in range(8765, 8815):
            s = socket.socket()
            try:
                if s.connect_ex(("127.0.0.1", port)) == 0:
                    return "http://127.0.0.1:%d/" % port
            finally:
                s.close()
    return None


def _show(url):
    """The app in a window inside Rhino; the browser if that is not possible."""
    try:
        import Eto.Forms as ef
        import Eto.Drawing as ed
        import Rhino.UI
        form = ef.Form()
        form.Title = "CAD to Revit"
        form.ClientSize = ed.Size(1400, 900)
        wv = ef.WebView()
        wv.Url = __import__("System").Uri(url)
        form.Content = wv
        form.Owner = Rhino.UI.RhinoEtoApp.MainWindow
        form.Show()
        return "opened in a Rhino window"
    except Exception:
        import webbrowser
        webbrowser.open(url)
        return "opened in the browser"


def component(project, open_app=False, floors=None, build=False, extras=True):
    log = []
    run = latest_run(project)
    if open_app:
        url = _server_url()
        log.append("App: %s - %s" % (url, _show(url)) if url else
                   "Could not start the app: run run_app.bat once, or check that Python is installed.")
    if not project:
        log.append("Give the job's project folder (the one the app uses).")
        return "\n".join(log), [], None
    if not run:
        log.append("No exported analysis in %s yet: import, analyse and export in the app first." % project)
        return "\n".join(log), [], None
    fl = exported_floors(run)
    log.append("Analysis: %s - %d floor(s) to build." % (os.path.basename(run), len(fl)))
    if build:
        log += check_lines(run)
        todo = chosen(fl, floors)
        for label, mj, lvl in todo:
            log.append("\n=== %s" % label)
            out = _run_script(os.path.join(HERE, "build_model.py"),
                              {"model_json": mj, "level_name": lvl, "mode": "replace"})
            log.append(out.strip().splitlines()[-1] if out.strip() else "(no log)")
        fj = foundations_file(run)
        if extras and fj:
            log.append("\n=== foundations, pedestals, levels, stairs")
            out = _run_script(os.path.join(HERE, "build_extras.py"), {"extras_json": fj})
            log.append(out.strip())
        log.append("\nDone. Each floor's build_log.txt is beside its model.json.")
    return "\n".join(log), [f[0] for f in fl], run


if "project" in globals() or "build" in globals():
    log, plans, run = component(globals().get("project"), bool(globals().get("open_app")),
                                globals().get("floors"), bool(globals().get("build")),
                                globals().get("extras", True) is not False)
