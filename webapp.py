r"""CAD to Revit - the app, in the browser, served from this PC only.

    python webapp.py            (run_app.bat does this and opens the browser)

A project per job. Four steps, moved between freely:
  1 Import CAD   Windows' own file / folder pickers; the imported drawings listed
  2 Analyse      floors ticked and named from lists; progress shown stage by stage
  3 Review       plan and 3D; every decision saved into the project as it is made
  4 Export       build in Revit, or a Revit package (.zip) for whoever builds it

Nothing leaves the PC: the server listens on 127.0.0.1 only (the office mode,
run_office.bat, also lets colleagues on the office network use it here through their
browser - see building/office.py). Each project keeps
its drawings list, levels and decisions in its own folder (c2r_project.json,
overrides.json), so a project can be moved or opened on another PC.
"""
from __future__ import annotations

import json
import os
import re
import socket
import sys
import threading
import traceback
import webbrowser
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from app import FLOORS, detect_levels, levels_text, name_scheme, parse_levels, revit_names   # noqa: E402

SETTINGS = os.environ.get("C2R_SETTINGS") or os.path.join(os.path.expanduser("~"), ".c2r_workflow.json")


def _drawing_floors(run_dir: str) -> dict:
    """{floor key: its name as the drawings write it} for the floors the last analysis found
    plans of ("THIRD FLOOR LAYOUT PLAN" -> L3: "Third Floor"): plan titles first, file names next.
    {} before any analysis."""
    from collections import Counter
    from building.sheets.levels import floor_name, level_rank
    bj = os.path.join(run_dir or "", "building.json")
    if not run_dir or not os.path.exists(bj):
        return {}
    try:
        with open(bj, encoding="utf-8") as f:
            sheets = (json.load(f).get("sheets") or {}).values()
    except Exception:
        return {}
    names = {}
    for s in sheets:
        k = s.get("level") or ""
        if s.get("reader") != "sheets.registry" or s.get("kind") not in (
                "framing_plan", "foundation_plan", "column_layout", "slab_plan") or level_rank(k) is None:
            continue
        stem = os.path.splitext(os.path.basename(s.get("file") or ""))[0]
        n = floor_name([s.get("title") or "", stem], k)
        names.setdefault(k, Counter())[n] += 1
    # a floor's commonest wording (wings may write it apart); none read -> "" (its own name stays)
    return {k: next((n for n, _ in c.most_common() if n), "") for k, c in names.items()}


def _floors_with_added(chosen: dict) -> list:
    """The Floors page's list: the floors the app knows, and each floor added for this
    project ("Service floor") just above or below the floor it was added against."""
    from building.sheets.levels import custom_floor, key_elevation, level_rank
    out = [list(f) for f in FLOORS]
    keys = [k for k, _ in FLOORS]
    for k in chosen:
        el = key_elevation(k)
        if el is not None and k not in keys:
            # a floor the drawings name by its height ("FRAMING PLAN @(+)6600 LVL."): in its place
            r = level_rank(k)
            at = next((i for i, f in enumerate(out) if (level_rank(f[0]) or 0) > r), len(out))
            out.insert(at, [k, f"{'-' if el < 0 else '+'}{abs(round(el))} level"])
            continue
        cf = custom_floor(k)
        if not cf or cf[2] not in keys:
            continue
        words, above, base = cf
        name = dict(FLOORS)[base]
        row = [k, f"{words.capitalize()} ({'above' if above else 'below'} {name[0].lower() + name[1:]})"]
        at = next(i for i, f in enumerate(out) if f[0] == base)
        if above:
            while at + 1 < len(out) and custom_floor(out[at + 1][0]):
                at += 1                          # after the floors already added above it
            out.insert(at + 1, row)
        else:
            out.insert(at, row)
    return out
PROJECT_FILE = "c2r_project.json"


class State:
    def __init__(self, settings=None):
        # settings: where the recent projects are kept - None is this PC's own list (SETTINGS);
        # in the office mode each colleague has their own, in their own folder
        self.settings = settings
        g = self._read(settings or SETTINGS)
        self.recent: list[str] = [p for p in g.get("recent", []) if os.path.isdir(p)]
        self.project = ""
        self.files: list[str] = []
        self.levels = ""
        self.suggested = True
        self.log: list[str] = []
        self.running = ""
        self.run_dir = None
        self.package = ""
        self.rvt_out = ""           # where the user chose to save the .rvt
        self.ifc_out = ""           # where the user chose to save the .ifc
        self.rhino_out = ""         # where the user chose to save the .3dm
        self.export_dir = ""        # the folder the user chose for the Revit files
        self.exported_to = ""
        self.runs_after = ""        # set by "Start a new drawing set": analyses up to it are set aside
        self.revisions: list[dict] = []   # "Replace with revision": {"old", "new", "date"}
        self.lock = threading.Lock()
        last = g.get("project") or (self.recent[0] if self.recent else "")
        if last and os.path.isdir(last):
            self.open(last)

    @staticmethod
    def _read(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def open(self, path):
        """Open (or start) a project: its own drawings list and levels come with it."""
        self.project = os.path.abspath(path)
        os.makedirs(self.project, exist_ok=True)
        p = self._read(os.path.join(self.project, PROJECT_FILE))
        self.files = [f for f in p.get("files", []) if os.path.exists(f)]
        self.levels = p.get("levels", "")
        self.suggested = p.get("suggested", True)
        self.export_dir = p.get("export_dir", "")
        self.runs_after = p.get("runs_after", "")
        self.revisions = p.get("revisions", [])
        self.run_dir = self.latest_run()
        self.package = ""
        self.rvt_out = ""
        self.ifc_out = ""
        self.rhino_out = ""
        self.exported_to = ""
        self.log = []
        self.recent = [self.project] + [r for r in self.recent if os.path.abspath(r) != self.project][:9]
        self.save()

    def save(self):
        try:
            if self.project:
                p = {"files": self.files, "levels": self.levels, "suggested": self.suggested,
                     "export_dir": self.export_dir}
                if self.runs_after:
                    p["runs_after"] = self.runs_after
                if self.revisions:
                    p["revisions"] = self.revisions
                with open(os.path.join(self.project, PROJECT_FILE), "w", encoding="utf-8") as f:
                    json.dump(p, f, indent=1)
            with open(self.settings or SETTINGS, "w", encoding="utf-8") as f:
                json.dump({"project": self.project, "recent": self.recent}, f)
        except Exception:
            pass

    def latest_run(self):
        runs = os.path.join(self.project, "runs") if self.project else ""
        if runs and os.path.isdir(runs):
            done = [os.path.join(runs, d) for d in sorted(os.listdir(runs))
                    if os.path.exists(os.path.join(runs, d, "review.html"))
                    and (not self.runs_after or d > self.runs_after)]
            return done[-1] if done else None
        return None

    def rvt_path(self):
        """Where 'Create a Revit file' saves this analysis' .rvt."""
        if not (self.project and self.run_dir):
            return ""
        return os.path.join(self.project, "packages",
                            f"{os.path.basename(self.project)}_{os.path.basename(self.run_dir)}.rvt")

    def rvt_file(self):
        """The .rvt made from this analysis, if there is one: where the user saved it,
        else the default place."""
        for p in (self.rvt_out, self.rvt_path()):
            if p and os.path.exists(p):
                return p
        return ""

    def ifc_path(self):
        """Where 'Create an IFC file' saves this analysis' .ifc."""
        return self.rvt_path()[:-4] + ".ifc" if self.rvt_path() else ""

    def ifc_file(self):
        for p in (self.ifc_out, self.ifc_path()):
            if p and os.path.exists(p):
                return p
        return ""

    def rhino_path(self):
        """Where 'Create a Rhino file' saves this analysis' .3dm."""
        return self.rvt_path()[:-4] + ".3dm" if self.rvt_path() else ""

    def rhino_file(self):
        for p in (self.rhino_out, self.rhino_path()):
            if p and os.path.exists(p):
                return p
        return ""

    def say(self, m):
        with self.lock:
            self.log.append(str(m))

    def view(self, since=0):
        summary = None
        if self.run_dir and os.path.exists(os.path.join(self.run_dir, "summary.json")):
            summary = self._read(os.path.join(self.run_dir, "summary.json")) or None
        ndec = 0
        d = self._read(os.path.join(self.project, "overrides.json")) if self.project else {}
        if d:
            ndec = len(d.get("elements", {})) + len(d.get("marks", {})) + len(d.get("exclude", []))
        with self.lock:
            log = self.log[since:]
        files = []
        stated = (d.get("project") or {}).get("units") or {} if d else {}
        rev_of = {r.get("new"): r.get("old") for r in self.revisions}
        for f in self.files:
            try:
                size = os.path.getsize(f)
            except OSError:
                size = 0
            files.append({"path": f, "name": os.path.basename(f), "folder": os.path.dirname(f),
                          "kind": os.path.splitext(f)[1][1:].upper(), "mb": round(size / 1048576, 1),
                          "units": stated.get(os.path.basename(f), ""),
                          **({"rev_of": os.path.basename(rev_of[f])} if rev_of.get(f) else {})})
        carry = self._read(os.path.join(self.run_dir, "notes_carry.json")) if self.run_dir else {}
        return {"project": self.project, "name": os.path.basename(self.project) if self.project else "",
                "recent": [{"path": r, "name": os.path.basename(r)} for r in self.recent],
                "files": files, "levels": parse_levels(self.levels), "suggested": self.suggested,
                "running": self.running, "log": log, "logn": since + len(log), "run": self.run_dir,
                "summary": summary, "decisions": ndec, "package": self.package,
                "rvt": self.rvt_file(), "ifc": self.ifc_file(), "rhino": self.rhino_file(),
                "exported_to": self.exported_to, "carry": carry or None,
                "built": bool(self.run_dir and os.path.isdir(os.path.join(self.run_dir, "revit_build")))}


HOME = State()          # this PC's own user: the app as run_app.bat runs it


class _Who(threading.local):
    state = None        # the office colleague a request (or its job) is for; None = this PC's own user


_who = _Who()


class _Current:
    """S: the projects of whoever a request is for - this PC's own user (always, unless
    the office mode is on), or the signed-in colleague, each with their own projects."""

    def __getattr__(self, k):
        return getattr(_who.state or HOME, k)

    def __setattr__(self, k, v):
        setattr(_who.state or HOME, k, v)


S = _Current()
HEAVY = threading.Lock()    # one analysis or export at a time, whoever started it


def _dialog(kind):
    """Windows' own picker, on this PC (the page asks the local server to open it)."""
    import tkinter as tk
    from tkinter import filedialog
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        if kind == "files":
            return list(filedialog.askopenfilenames(parent=root, title="Import the job's structural drawings",
                                                    filetypes=[("CAD drawings", "*.dwg *.dxf"), ("All", "*.*")]))
        if kind.startswith("revision:"):
            old = kind.split(":", 1)[1]
            p = filedialog.askopenfilename(parent=root, title=f"The revised drawing replacing {os.path.basename(old)}",
                                           initialdir=os.path.dirname(old) or None,
                                           filetypes=[("CAD drawings", "*.dwg *.dxf"), ("All", "*.*")])
            return [os.path.normpath(p)] if p else []
        if kind in ("save_rvt", "save_zip", "save_ifc", "save_3dm"):
            ext, default, title, types = {
                "save_rvt": (".rvt", S.rvt_out or S.rvt_path(), "Save the Revit file as", [("Revit project", "*.rvt")]),
                "save_ifc": (".ifc", S.ifc_out or S.ifc_path(), "Save the IFC file as", [("IFC model", "*.ifc")]),
                "save_3dm": (".3dm", S.rhino_out or S.rhino_path(), "Save the Rhino file as", [("Rhino model", "*.3dm")]),
                "save_zip": (".zip", S.package or _package_default(), "Save the Revit package as",
                             [("Zip package", "*.zip")]),
            }[kind]
            p = filedialog.asksaveasfilename(
                parent=root, title=title, initialdir=os.path.dirname(default), initialfile=os.path.basename(default),
                defaultextension=ext, filetypes=types)
            return [os.path.normpath(p)] if p else []
        title = {"folder": "Import every drawing in a folder", "new": "Where should the new project be kept?",
                 "open": "Open a project folder", "export": "Where should the Revit files go?"}.get(kind, "Choose a folder")
        start = (S.export_dir or S.project) if kind == "export" else None
        p = filedialog.askdirectory(parent=root, title=title, initialdir=start) if start else \
            filedialog.askdirectory(parent=root, title=title)
        return [os.path.normpath(p)] if p else []
    finally:
        root.destroy()


def _add(paths):
    from building.sheets.__main__ import drawings
    for p in drawings(paths):
        if p.lower().endswith((".dwg", ".dxf")) and p not in S.files:
            S.files.append(p)
    m = parse_levels(S.levels)
    # a floor's name is its Revit level name too: its own name ("1st floor"), or "Level 1" in a
    # project already named that way
    levelled = any(str(v).startswith("Level ") for v in m.values())
    worded = any(v == name_scheme(k, "words") for k, v in m.items() if k == "GF" or re.fullmatch(r"L\d{1,2}", k))
    for k in detect_levels(S.files):
        m.setdefault(k, name_scheme(k, "level") if levelled else name_scheme(k, "words") if worded else _own_name(k))
    S.levels = levels_text(m)
    S.save()


def _own_name(key: str) -> str:
    """A floor's own name, as the Floors page lists it: Foundation, Ground floor, 1st floor,
    Terrace (the usual Revit name where the list's is long: Stilt, Mumty, Basement 2)."""
    special = name_scheme(key, "words") if key in ("FDN", "B2", "B1", "PLINTH", "STILT", "MEZZ", "TERRACE",
                                                    "ROOF", "MUMTY", "OHT") else ""
    return special or dict(FLOORS).get(key) or key


def _job(name, fn):
    who = _who.state

    def go():
        _who.state = who                # the job works on the projects of whoever started it
        if not HEAVY.acquire(blocking=False):
            S.say("Waiting for another person's work on this PC to finish ...")
            HEAVY.acquire()
        try:
            fn()
        except Exception as exc:
            S.say(f"STOPPED: {exc}")
            S.say(traceback.format_exc())
        finally:
            HEAVY.release()
            S.running = ""
    if S.running:
        return False
    S.running = name
    threading.Thread(target=go, daemon=True).start()
    return True


def _analyse():
    from building.workflow import run
    S.package = ""
    S.say("STAGE prepare")
    S.run_dir = run(list(S.files), S.project, S.levels, S.suggested, log=S.say, prefix_hints=_prefix_hints())
    _carry_over()
    S.say("STAGE done")


def _carry_over():
    """After an analysis: decisions and notes follow their elements (a revised drawing's
    elements get new ids), and a revised drawing's changes are listed for Review. Never
    stops the analysis: what it cannot do is said in the log."""
    from building import notes
    try:
        res = notes.after_analysis(S.project, S.run_dir, list(S.files), S.revisions, log=S.say)
    except Exception as exc:
        S.say(f"Decisions and notes were not carried over to this analysis: {exc}")
        return
    for r in S.revisions:
        if r.get("new") in S.files:
            r["analysed"] = True
    S.save()
    with open(os.path.join(S.run_dir, "notes_carry.json"), "w", encoding="utf-8") as f:
        json.dump({k: res[k] for k in ("moved", "notes_moved", "changes")} |
                  {"unmatched": len(res["unmatched"]), "revised": bool(notes.revision_changes(S.run_dir))}, f)
    if res.get("relink"):
        from building.workflow import apply_decisions
        S.say("Applying the moved decisions ...")
        apply_decisions(S.run_dir, None, log=S.say)


def _replace(i, path):
    """A consultant's revised drawing in place of drawing i. The old one is kept on disk as
    the previous revision; the next analysis compares the two and carries the decisions
    and notes over."""
    if not 0 <= i < len(S.files):
        raise RuntimeError("That drawing is no longer in the list.")
    old = S.files[i]
    same = os.path.normcase(os.path.abspath(path)) == os.path.normcase(os.path.abspath(old))
    if same:
        raise RuntimeError("That is the same file. If the revision was saved over it, just press Analyse "
                           "again: the app notices the drawing's new date and compares it with the last analysis.")
    if any(os.path.normcase(os.path.abspath(path)) == os.path.normcase(os.path.abspath(f)) for f in S.files):
        raise RuntimeError(f"{os.path.basename(path)} is already in the list.")
    if not path.lower().endswith((".dwg", ".dxf")):
        raise RuntimeError("Choose a DWG or DXF drawing.")
    import datetime
    S.files[i] = path
    S.revisions.append({"old": old, "new": path, "date": datetime.datetime.now().strftime("%Y-%m-%d %H:%M")})
    S.save()


def _export(dest=None):
    """The review's decisions into the model and the Revit files written; with dest, a
    copy of them (and the review) in the folder the user chose."""
    import shutil
    from building.workflow import apply_decisions
    S.say("Exporting to Revit ...")
    apply_decisions(S.run_dir, None, log=S.say)
    if dest:
        out = os.path.join(dest, f"{os.path.basename(S.project)} - Revit files")
        shutil.copytree(os.path.join(S.run_dir, "revit_build"), os.path.join(out, "revit_build"), dirs_exist_ok=True)
        for f in ("review.html", "review.xlsx", "model3d.html", "SUMMARY.txt"):
            if os.path.exists(os.path.join(S.run_dir, f)):
                shutil.copy2(os.path.join(S.run_dir, f), out)
        S.exported_to = out
        S.say(f"Revit files saved in {out}")
    S.say("Revit files written.")


def _package_default():
    return os.path.join(S.project, "packages",
                        f"{os.path.basename(S.project)}_{os.path.basename(S.run_dir or 'run')}_revit.zip")


def _package(path=None):
    """One .zip for whoever builds the model: the Revit files, the review page, the
    workbook and the summary. Saved where the user chose (else the project's 'packages')."""
    if not S.run_dir or not os.path.isdir(os.path.join(S.run_dir, "revit_build")):
        raise RuntimeError("Export to Revit first.")
    path = path or _package_default()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for base, _dirs, files in os.walk(os.path.join(S.run_dir, "revit_build")):
            for f in files:
                full = os.path.join(base, f)
                z.write(full, os.path.relpath(full, S.run_dir))
        for f in ("review.html", "review.xlsx", "model3d.html", "SUMMARY.txt"):
            p = os.path.join(S.run_dir, f)
            if os.path.exists(p):
                z.write(p, f)
        rvt = S.rvt_file()
        if rvt:
            z.write(rvt, os.path.basename(rvt))
        z.writestr("READ ME.txt",
                   "Revit package from CAD to Revit\n\n"
                   "revit_build\\<floor>\\model.json   run revit\\build_model.py on each, lowest floor first\n"
                   "revit_build\\<floor>\\foundations.json   then revit\\build_extras.py on it\n"
                   "  (or the Rhino.Inside.Revit component, revit\\rir_cad2revit.py, does both)\n"
                   "review.html   every value with its source (open in a browser)\n"
                   "SUMMARY.txt   what was decided and what is still open\n")
    S.package = path
    return path


def _overrides():
    f = os.path.join(S.project, "overrides.json") if S.project else ""
    return State._read(f) if f and os.path.exists(f) else {}


def _save_overrides(d):
    os.makedirs(S.project, exist_ok=True)      # a choice made on Import CAD, before any analysis
    with open(os.path.join(S.project, "overrides.json"), "w", encoding="utf-8") as f:
        json.dump(d, f, indent=1)


def _prefix_hints_path() -> str:
    """What mark prefixes meant in this person's earlier projects: hints for the next
    one, rechecked against its drawings (each office colleague has their own)."""
    home = getattr(S, "office_home", "") or ""
    return os.path.join(home, "prefix_hints.json") if home else \
        os.path.join(os.path.dirname(SETTINGS), ".c2r_prefix_hints.json")


def _prefix_hints() -> dict:
    return State._read(_prefix_hints_path())


def _save_prefix_hints(decided: dict) -> None:
    if not decided:
        return
    try:
        h = _prefix_hints()
        h.update(decided)
        with open(_prefix_hints_path(), "w", encoding="utf-8") as f:
            json.dump(h, f, indent=1)
    except OSError:
        pass                                  # a hint only: never stops the decision being saved


def _project_decisions(body):
    """The Export page's decisions for the whole job: floor elevations (by Revit level
    name) and one founding level for every footing the drawings give none. Saved with
    the review's decisions in overrides.json; an empty field takes a decision back."""
    d = _overrides()
    proj = d.get("project") or {}
    if "levels" in body:
        lv = {}
        for k, v in (body.get("levels") or {}).items():
            if str(v).strip() not in ("", "None"):
                lv[str(k)] = float(v)
        proj["levels"] = lv
    if "level_set" in body:
        # heights given on the Floors step: merged, one level at a time; empty takes one back
        lv = dict(proj.get("levels") or {})
        for k, v in (body.get("level_set") or {}).items():
            if str(v).strip() in ("", "None"):
                lv.pop(str(k), None)
            else:
                lv[str(k)] = float(v)
        proj["levels"] = lv
    if "founding_level_mm" in body:
        v = str(body.get("founding_level_mm") or "").strip()
        if v:
            proj["founding_level_mm"] = float(v)
        else:
            proj.pop("founding_level_mm", None)
    if "slab_thickness_mm" in body:
        v = str(body.get("slab_thickness_mm") or "").strip()
        if v:
            proj["slab_thickness_mm"] = float(v)
        else:
            proj.pop("slab_thickness_mm", None)
    if "units_set" in body:
        # a drawing's units stated on Import CAD, by file name (readset reads them);
        # "auto" or empty takes it back: the app finds the units from the drawing
        un = dict(proj.get("units") or {})
        for k, v in (body.get("units_set") or {}).items():
            v = str(v or "").strip().lower()
            if v in ("", "auto"):
                un.pop(str(k), None)
            elif v in ("mm", "cm", "dm", "m", "in", "ft"):
                un[str(k)] = v
        proj["units"] = un
    if "build_unsettled" in body:
        # what still needs a decision: built at a working size and marked red (the
        # default), or held back until decided
        proj["build_unsettled"] = bool(body.get("build_unsettled"))
    if "origin" in body:
        # one point every floor is built from (export._one_origin); both or nothing
        xy = [str(v or "").strip() for v in (body.get("origin") or [])][:2]
        if len(xy) == 2 and all(xy):
            proj["origin"] = [float(xy[0]), float(xy[1])]
        else:
            proj.pop("origin", None)
    d["project"] = proj
    _save_overrides(d)
    return proj


def _revit(to_file):
    """Build through the live Revit connection: into the open model, or a new .rvt.

    For a new .rvt nobody has to be at Revit: a connected Revit is used if one is
    open; otherwise the app starts Revit itself, builds, saves and closes it again;
    and on a PC with no Revit the model is written as IFC instead."""
    from building import revit_batch, revit_mcp
    try:
        if to_file:
            # a new .rvt never uses the live connection: the app starts a Revit of its own,
            # which builds and saves the file by itself (revit_batch); no Revit -> IFC
            if not revit_batch.revits():
                S.say("REVIT No Revit 2025 or later on this PC - writing an IFC file instead, which "
                      "Revit and other BIM programs open.")
                _ifc()
                S.say("REVIT DONE" if S.ifc_file() else "REVIT STOPPED: the IFC file was not written")
                return
            revit_batch.build_file(S.run_dir, S.rvt_out or S.rvt_path(), decisions=_overrides().get("project"),
                                   log=S.say)
            return
        # into the model open in Revit: the live connection (MCP)
        S.say("REVIT Connecting to Revit ...")
        revit_mcp.build(S.run_dir, log=S.say, decisions=_overrides().get("project"))
    except revit_mcp.RevitError as exc:
        S.say(f"REVIT STOPPED: {exc}")


def _ifc():
    """Write the model as IFC: no Revit needed. The same elements, levels and rules as
    the Revit build; saved where the user chose (else the project's 'packages')."""
    from building import ifc_export
    path = S.ifc_out or S.ifc_path()
    S.say("IFC Writing the IFC file ...")
    try:
        r = ifc_export.write(S.run_dir, path, _overrides().get("project"), log=S.say,
                             name=os.path.basename(S.project) if S.project else "")
        S.ifc_out = r["path"]
        S.say("IFC DONE")
    except ifc_export.IfcError as exc:
        S.say(f"IFC STOPPED: {exc}")


def _rhino():
    """Write the model as a Rhino file (.3dm): no Rhino needed. The IFC file's elements,
    levels and rules; beam sizes in the Revit filters' colours, undecided ones red."""
    from building import rhino_export
    path = S.rhino_out or S.rhino_path()
    S.say("RHINO Writing the Rhino file ...")
    try:
        r = rhino_export.write(S.run_dir, path, _overrides().get("project"), log=S.say,
                               name=os.path.basename(S.project) if S.project else "")
        S.rhino_out = r["path"]
        S.say("RHINO DONE")
    except rhino_export.RhinoError as exc:
        S.say(f"RHINO STOPPED: {exc}")


def _sample():
    """A sample project to try the app with: a small drawing set in its own folder."""
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    import make_synthetic_set
    home = os.path.join(os.path.expanduser("~"), "Documents", "CAD to Revit sample")
    if _who.state is not None:                  # an office colleague: in their own folder
        home = os.path.join(_who.state.office_home, "CAD to Revit sample")
    drawings = os.path.join(home, "drawings")
    if not os.path.isdir(drawings) or not os.listdir(drawings):
        make_synthetic_set.main(drawings)
    S.open(os.path.join(home, "project"))
    if not S.files:
        _add([drawings])


# the review page, told to hand every decision to the app as it is made
HOOK = r"""<script>
(function(){
 if(window.parent===window)return;
 // "saved" only when the app took the change; otherwise a warning that stays until a save works
 const saved=ok=>{const t=document.getElementById('c2rsaved');if(!t)return;
  t.textContent=ok?'saved in the project automatically':'NOT SAVED - the app did not take your last change. Check the app is still open, then save again.';
  t.style.color=ok?'':'#c0392b';t.style.fontWeight=ok?'':'bold'};
 const send=()=>{try{fetch('/api/decisions',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify(dec)}).then(r=>{saved(r.ok);if(r.ok)parent.postMessage({c2r:'decided'},'*')},()=>saved(false))}catch(e){saved(false)}};
 const _save=window.save;window.save=function(){_save();send()};
 // the project's saved decisions are the truth: this browser's copy is used only when the project has none
 fetch('/api/decisions').then(r=>r.json()).then(d=>{
  if(d&&(Object.keys(d.elements||{}).length||Object.keys(d.marks||{}).length||(d.exclude||[]).length||Object.keys(d.prefixes||{}).length)){
   dec={elements:d.elements||{},marks:d.marks||{},exclude:d.exclude||[],prefixes:d.prefixes||{}};_save();chips();plans();draw();
   if(typeof issues==='function')issues()}}).catch(()=>{});
 // inside the app the file buttons are not needed: decisions are saved for you
 ['export','import','clear'].forEach(id=>{const b=document.getElementById(id);if(b)b.style.display='none'});
 document.querySelectorAll('footer label, footer .small').forEach(n=>n.style.display='none');
 const nd=document.getElementById('ndec');if(nd){const t=document.createElement('span');t.className='small';t.id='c2rsaved';
  t.textContent='saved in the project automatically';nd.after(t)}
})();
</script>"""


SESSIONS: dict[str, dict] = {}         # sha256(sign-in token) -> {"email", "until", ...}
SESSION_HOURS = 12                     # (kept for older callers; a sign-in now lasts SESSION_DAYS)
SESSION_DAYS = 30                      # signed in until "Sign out", or this long without using the app
RECHECK_SECONDS = 600                  # a removed or expired user is signed out within this time
_sessions_lock = threading.Lock()
_sessions_loaded = [False]


def _tok_key(tok):
    import hashlib
    return hashlib.sha256(tok.encode()).hexdigest()


def _sessions_file():
    from building import access
    return os.path.join(access.CACHE_DIR, "sessions.json")


def _load_sessions():
    """Sign-ins survive a restart of the app (only hashes of the tokens are kept on disk)."""
    import time
    if _sessions_loaded[0]:
        return
    _sessions_loaded[0] = True
    try:
        with open(_sessions_file(), encoding="utf-8") as f:
            for k, v in json.load(f).items():
                if v.get("until", 0) > time.time():
                    SESSIONS.setdefault(k, v)
    except (OSError, ValueError, AttributeError):
        pass


def _save_sessions():
    import time
    try:
        with _sessions_lock:
            live = {k: v for k, v in SESSIONS.items() if v.get("until", 0) > time.time()}
            os.makedirs(os.path.dirname(_sessions_file()), exist_ok=True)
            tmp = _sessions_file() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(live, f)
            os.replace(tmp, _sessions_file())
    except OSError:
        pass


def _new_session(info):
    """A sign-in: (token, its Set-Cookie header). The browser keeps it until Sign out."""
    import secrets
    import time
    _load_sessions()
    tok = secrets.token_urlsafe(24)
    now = time.time()
    SESSIONS[_tok_key(tok)] = {**info, "until": now + SESSION_DAYS * 86400, "checked": now, "saved": now}
    _save_sessions()
    return tok, ("Set-Cookie", f"c2r_session={tok}; Max-Age={365 * 86400}; HttpOnly; SameSite=Strict; Path=/")
OFFICE_PORT = 8770                     # the office mode's own port: run_app.bat's 8765 is left alone
UPLOAD_MB = 300
OFFICE_STATES: dict[tuple, State] = {}  # (office colleague's e-mail, user key) -> their projects
_NOTIFIED: set[str] = set()            # requests the admin was already e-mailed about
_states_lock = threading.Lock()


def _office_state(email, user):
    """An office colleague's own projects, in their own folder (building.office.home_of). Their
    list holds only their own projects. Projects in the folder made under their e-mail before
    user names are left where they are, unlisted: one goes on a person's list when the admin
    hands it to them (its path added to their recent_projects.json)."""
    from building import office
    key = (email, office.user_key(user))
    with _states_lock:
        st = OFFICE_STATES.get(key)
        if st is None:
            home = office.home_of(email, user)
            os.makedirs(home, exist_ok=True)
            st = State(os.path.join(home, "recent_projects.json"))
            st.office_home = home
            st.shared_home = office.shared_home(email)
            st.recent = [r for r in st.recent if _office_may_open(st, r)]
            OFFICE_STATES[key] = st
        return st


def _office_may_open(st, path) -> bool:
    from building import office
    return office.inside(path, st.office_home) or bool(st.shared_home and office.inside(path, st.shared_home))


class H(BaseHTTPRequestHandler):
    # Sign-in: on when the app finds access_config.json (main() sets it). A server made
    # without main() - the tests - works as before, with no sign-in.
    require_login = False
    # The office mode (run_office.bat): colleagues on the office network use the app on this
    # PC through their browser, signing in with e-mail and password. Off unless main() is
    # given --office; this PC's own browser (LOCAL) is served exactly as without it.
    office = False
    office_url = ""
    LOCAL = ("127.0.0.1", "::1")

    def log_message(self, *a):
        pass

    def _remote(self) -> bool:
        """A colleague's browser on another PC (the office mode only)."""
        return bool(self.office) and self.client_address[0] not in self.LOCAL

    def _whose(self):
        """Point S at the projects of whoever this request is for."""
        _who.state = None
        if self._remote():
            s = self._session()
            if s:
                _who.state = _office_state(s["email"], s["user"])

    def _send(self, code, body, ctype="application/json", headers=()):
        data = body if isinstance(body, bytes) else (json.dumps(body) if ctype == "application/json" else body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if not ctype.startswith("application/octet") else ""))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _session(self):
        """The signed-in user of this browser, or None."""
        import time
        from building import access
        _load_sessions()
        m = re.search(r"(?:^|;\s*)c2r_session=([\w-]+)", self.headers.get("Cookie") or "")
        key = _tok_key(m.group(1)) if m else ""
        s = SESSIONS.get(key) if m else None
        now = time.time()
        if s and s["until"] < now:
            SESSIONS.pop(key, None)
            _save_sessions()
            return None
        if s and bool(s.get("remote")) != self._remote():
            return None                 # a sign-in on this PC is not one over the network, nor back
        if s and s.get("remote") and not s.get("user"):
            SESSIONS.pop(key, None)     # signed in before user names: sign in again with one
            _save_sessions()
            return None
        if s and now - s.get("checked", 0) > RECHECK_SECONDS:
            # still on the approved list? A removed or expired user is signed out
            s["checked"] = now
            if not access.still_allowed(s["email"], any_pc=bool(s.get("remote"))):
                SESSIONS.pop(key, None)
                _save_sessions()
                return None
        if s:
            s["until"] = now + SESSION_DAYS * 86400       # kept signed in while the app is used
            if now - s.get("saved", 0) > 3600:
                s["saved"] = now
                _save_sessions()
        return s

    @staticmethod
    def _key_here() -> bool:
        """Is the admin's signing key on this PC? Users are managed only there."""
        try:
            sys.path.insert(0, os.path.join(ROOT, "tools"))
            import access_admin
            return access_admin.can_sign()
        except Exception:
            return False

    def _access_view(self):
        from building import access
        s = self._session()
        remote = self._remote()
        return {"required": self.require_login, "signed_in": bool(s), "email": s["email"] if s else "",
                "user": s.get("user", "") if s else "",
                # the Users button: an admin, on the admin's own PC (the one holding the signing key) -
                # never on anybody else's PC, whoever signs in there (nor over the office network)
                "admin": bool(s and s.get("role") == "admin" and not remote and self._key_here()),
                "plan": s.get("plan", "") if s else "", "expires": s.get("expires", "") if s else "",
                "pc": "" if remote else access.machine_code(), "last": "" if remote else access.last_email(),
                "remote": remote}

    def _gate_get(self, p):
        """True when this GET was answered by the sign-in gate (no session yet)."""
        if p == "/reset" and self._remote():
            self._send(200, RESET_PAGE, "text/html")
            return True
        if not self.require_login or self._session():
            return False
        if p == "/api/access":
            self._send(200, self._access_view())
        elif p in ("/", "/login"):
            self._send(200, OFFICE_LOGIN_PAGE if self._remote() else LOGIN_PAGE, "text/html")
        else:
            self._send(401, {"error": "Sign in first."})
        return True

    # -- the office mode: e-mail and password, a link by e-mail to set or reset it
    def _tell_admin(self, email, user=""):
        """E-mail the admin once about a colleague waiting for approval."""
        from building import office
        cfg = office.mail_config()
        if email in _NOTIFIED or not cfg:
            return
        _NOTIFIED.add(email)
        port = self.server.server_address[1]
        who = f"{user} ({email})" if user else email
        office.send_later(cfg.get("admin_to") or cfg["user"], f"CAD to Revit: {who} asks to use the app",
                          f"{who} has asked to use CAD to Revit.\n\n"
                          f"To approve: on your own PC open the app (http://127.0.0.1:{port}/?users=1), "
                          "press Users, then Approve beside their e-mail. They get an e-mail when it is done.\n\n"
                          "If you do not know them, do nothing: they cannot use the app.")

    def _office_login(self, b):
        from building import access, office
        email = (b.get("email") or "").strip().lower()
        user = b.get("user") or ""
        why = office.check_password(email, b.get("password") or "", user)
        if why:
            return self._send(403, {"error": why})
        r = access.check(email, pc="OFFICE", any_pc=True)
        name = office.user_name(email, user)
        if not r["ok"]:
            if r.get("requested"):
                self._tell_admin(email, name)
            return self._send(403, {"error": r["message"]})
        _tok, cookie = _new_session({"email": r["email"], "user": name, "plan": r["plan"], "expires": r["expires"],
                                     "role": r.get("role", ""), "remote": True})
        return self._send(200, {"ok": True, "email": r["email"], "user": name}, headers=[cookie])

    def _forgot(self, b):
        """A link by e-mail to set one user name's password: a new user name makes a new user."""
        from building import access, office
        email = (b.get("email") or "").strip().lower()
        if not access.EMAIL.match(email):
            return self._send(400, {"error": "Type your e-mail address."})
        try:
            user = office.clean_name(b.get("user") or "")
        except ValueError as exc:
            return self._send(400, {"error": str(exc)})
        user = office.user_name(email, user) or user          # as its owner first typed it
        new = not office.has_password(email, user)
        if office.may_send(f"{email}|{user.lower()}"):
            tok = office.new_reset(email, user)
            office.send_later(email, f"{'Create' if new else 'Reset'} the CAD to Revit password for {user}",
                              f"Someone (we hope {user}) asked to {'create' if new else 'reset'} the CAD to Revit "
                              f"password for the user name \"{user}\" with this e-mail.\n\n"
                              f"Choose the password here (the link works for {office.RESET_MINUTES} minutes, "
                              f"once):\n{self.office_url}reset?t={tok}\n\n"
                              "Only this user name's password changes; others using this e-mail keep theirs.\n"
                              "If it was not you, ignore this e-mail: nothing changes.")
        return self._send(200, {"ok": True, "message": f"A link to set the password for {user} was sent to {email}. "
                                                       f"Open it within {office.RESET_MINUTES} minutes "
                                                       "(check Spam too)."})

    def _set_password(self, b):
        from building import access, office
        tok = b.get("token") or ""
        email, user = office.reset_for(tok)
        if not email:
            return self._send(400, {"error": "This link is out of date or was already used. Ask for a new one "
                                             "on the sign-in page."})
        try:
            office.set_password(email, b.get("password") or "", user)
        except ValueError as exc:
            return self._send(400, {"error": str(exc)})
        office.reset_for(tok, use=True)
        r = access.check(email, pc="OFFICE", any_pc=True)
        if r.get("requested"):
            self._tell_admin(email, user)
        return self._send(200, {"ok": True, "email": email, "user": user, "approved": r["ok"],
                                "message": f"Password saved for {user}." + ("" if r["ok"] else " " + r["message"])})

    def _reset_name(self, b):
        """Whose password a reset link sets, to show on the password page."""
        from building import office
        email, user = office.reset_for(b.get("token") or "")
        if not email:
            return self._send(400, {"error": "This link is out of date or was already used. Ask for a new one "
                                             "on the sign-in page."})
        return self._send(200, {"email": email, "user": user, "new": not office.has_password(email, user)})

    def _login(self, b):
        from building import access
        if self._remote():
            return self._office_login(b)
        r = access.check(b.get("email", ""))
        if not r["ok"]:
            return self._send(403, {"error": r["message"], "pc": r["pc"]})
        access.remember(r["email"])
        _tok, cookie = _new_session({"email": r["email"], "plan": r["plan"], "expires": r["expires"],
                                     "role": r.get("role", "")})
        return self._send(200, {"ok": True, "email": r["email"], "plan": r["plan"], "expires": r["expires"]},
                          headers=[cookie])

    # -- admins: approve, add, renew and remove users from inside the app
    def _admin_tool(self):
        """tools/access_admin, or an error answer when this session may not use it."""
        s = self._session()
        if not (self.require_login and s and s.get("role") == "admin") or self._remote():
            self._send(403, {"error": "Only an admin can manage users, on the admin's own PC."})
            return None
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        import access_admin
        if not access_admin.can_sign():
            self._send(409, {"error": "Users are approved on the PC that holds the admin's signing key "
                                      f"(the folder {access_admin.ADMIN_DIR} on that PC). This PC does not "
                                      "have it: approve there, or copy that folder to this PC."})
            return None
        return access_admin

    def _admin_view(self, tool):
        from building import access, office
        users, requests = tool.users_data(), tool.requests_data()
        for x in users + requests:               # the user names signing in with this e-mail (office mode)
            x.setdefault("name", office.name_of(x.get("email", "")))
        return {"users": users, "requests": requests, "pc": access.machine_code(),
                "me": self._session()["email"]}

    def _admin_get(self):
        tool = self._admin_tool()
        if tool:
            self._send(200, self._admin_view(tool))

    def _admin_post(self, b):
        import contextlib
        import io as _io
        tool = self._admin_tool()
        if not tool:
            return
        act, email = b.get("action", ""), (b.get("email") or "").strip().lower()
        if act == "remove" and email == self._session()["email"]:
            return self._send(400, {"error": "You cannot remove yourself - ask another admin."})
        try:
            with contextlib.redirect_stdout(_io.StringIO()):
                if act == "add":
                    tool.add(email, (b.get("pc") or "").strip(), (b.get("until") or "").strip(),
                             (b.get("name") or "").strip(), "", (b.get("role") or "").strip())
                elif act == "approve":
                    tool.approve(email, (b.get("until") or "").strip())
                elif act == "renew":
                    tool.renew(email, (b.get("until") or "").strip())
                elif act == "remove":
                    tool.remove(email)
                else:
                    return self._send(400, {"error": "Unknown action."})
        except (SystemExit, ValueError, OSError) as exc:
            return self._send(400, {"error": str(exc) or "Not done."})
        if self.office and act in ("approve", "add"):
            # the office mode: the colleague hears at once that they can sign in
            from building import office
            if office.mail_config():
                _NOTIFIED.discard(email)
                office.send_later(email, "You can now use CAD to Revit",
                                  f"Your access to CAD to Revit is approved.\n\nOpen {self.office_url} in your browser "
                                  "(on the office network) and sign in with your e-mail and password.\n"
                                  "No password yet? Press \"Set or reset my password\" on the sign-in page.")
        return self._send(200, self._admin_view(tool))

    def _from_this_app(self):
        """A change sent by this app's own page (or with no Origin: a script on this PC),
        never by another website open in the same browser. The app's port is whichever
        free_port found, so it is compared with this server's own."""
        origin = self.headers.get("Origin")
        if origin is None:
            return True
        port = self.server.server_address[1]
        if self.office and origin == f"http://{self.headers.get('Host', '')}":
            return True                 # the office mode: the page as a colleague's browser reached it
        return origin in (f"http://127.0.0.1:{port}", f"http://localhost:{port}")

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return {}

    def _upload(self):
        """The office mode: a drawing sent from a colleague's browser, kept in their project."""
        from urllib.parse import parse_qs
        if not self._remote():
            return self._send(404, {"error": "not found"})
        if not self._from_this_app():
            return self._send(403, {"error": "Changes can only be made from the CAD to Revit app itself."})
        if self.require_login and not self._session():
            return self._send(401, {"error": "Sign in first."})
        if not S.project:
            return self._send(400, {"error": "Start or open a project first."})
        n = int(self.headers.get("Content-Length") or 0)
        qs = parse_qs(urlparse(self.path).query)
        name = os.path.basename((qs.get("name") or [""])[0])
        name = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", name).strip(" .")
        if not name.lower().endswith((".dwg", ".dxf")):
            return self._send(400, {"error": f"{name or 'This file'}: only DWG or DXF drawings can be added."})
        if n <= 0 or n > UPLOAD_MB * 1048576:
            return self._send(400, {"error": f"{name}: empty, or larger than {UPLOAD_MB} MB."})
        folder = os.path.join(S.project, "drawings")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, name)
        rep = qs.get("replace")
        if rep is not None:
            # a revised drawing for drawing i: one of the same name is moved aside first, kept
            # as the previous revision
            try:
                i = int(rep[0])
                old = S.files[i]
            except (ValueError, IndexError):
                return self._send(400, {"error": "That drawing is no longer in the list."})
            if os.path.exists(path):
                key = os.path.normcase(os.path.abspath(path))
                if key != os.path.normcase(os.path.abspath(old)) and \
                        any(os.path.normcase(os.path.abspath(f)) == key for f in S.files):
                    return self._send(400, {"error": f"{name} is already in the list."})
                import datetime
                prev = os.path.join(folder, "_previous")
                os.makedirs(prev, exist_ok=True)
                kept = os.path.join(prev, datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S ") + name)
                os.replace(path, kept)
                if key == os.path.normcase(os.path.abspath(old)):
                    S.files[i] = kept
        with open(path + ".part", "wb") as f:
            left = n
            while left > 0:
                chunk = self.rfile.read(min(left, 1 << 20))
                if not chunk:
                    break
                f.write(chunk)
                left -= len(chunk)
        if left > 0:
            os.remove(path + ".part")
            return self._send(400, {"error": f"{name}: the upload was cut off. Try again."})
        os.replace(path + ".part", path)
        if rep is not None:
            try:
                _replace(int(rep[0]), path)
            except RuntimeError as exc:
                return self._send(400, {"error": str(exc)})
            return self._send(200, {**S.view(), "replaced": True})
        _add([path])
        return self._send(200, S.view())

    def _who_email(self) -> str:
        s = self._session()
        if s:
            return s.get("email", "")
        try:
            from building import access
            return access.last_email() or ""
        except Exception:
            return ""

    def _notes_reply(self):
        """The consultant's returned Excel, sent from the page (this PC or a colleague's)."""
        from urllib.parse import parse_qs
        from building import notes
        if not self._from_this_app():
            return self._send(403, {"error": "Changes can only be made from the CAD to Revit app itself."})
        if self.require_login and not self._session():
            return self._send(401, {"error": "Sign in first."})
        if not S.project:
            return self._send(400, {"error": "Start or open a project first."})
        n = int(self.headers.get("Content-Length") or 0)
        name = os.path.basename((parse_qs(urlparse(self.path).query).get("name") or [""])[0])
        name = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", name).strip(" .") or "reply.xlsx"
        if not name.lower().endswith(".xlsx"):
            return self._send(400, {"error": "Choose the Excel file (.xlsx) the consultant sent back."})
        if n <= 0 or n > 200 * 1048576:
            return self._send(400, {"error": f"{name}: empty, or too large."})
        import datetime
        folder = notes.folder(S.project, "replies")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S ") + name)
        with open(path, "wb") as f:
            left = n
            while left > 0:
                chunk = self.rfile.read(min(left, 1 << 20))
                if not chunk:
                    break
                f.write(chunk)
                left -= len(chunk)
        try:
            res = notes.apply_reply(S.project, path, name)
        except Exception as exc:
            return self._send(400, {"error": f"{name} could not be read as the queries' Excel: {exc}"})
        if not res["rows"]:
            return self._send(400, {"error": f"No answers found in {name}. Is it the queries Excel from this app, "
                                             "with the answers in the \"Consultant reply\" column?"})
        return self._send(200, res)

    def _notes_report(self, b):
        from building import notes
        if not S.project:
            return self._send(400, {"error": "Start or open a project first."})
        fmt = "pdf" if b.get("fmt") == "pdf" else "xlsx"
        items = notes.rows(S.project, include_closed=bool(b.get("closed")))
        if b.get("open_values") and S.run_dir:
            items += notes.open_values(S.run_dir, {n.get("el") for n in items if n.get("el")})
        if not items:
            return self._send(400, {"error": "Nothing to send: no open notes yet. In Review, click an element and "
                                             "add a note for the consultant."})
        name = notes.report_name(S.project, fmt)
        out = notes.folder(S.project, "reports", name)
        (notes.write_pdf if fmt == "pdf" else notes.write_xlsx)(S.project, items, out, self._who_email())
        notes.mark_sent(S.project, [n["id"] for n in items if n.get("id")])
        return self._send(200, {"name": name, "count": len(items)})

    # -- the architect's 3D model, shown over the structure in Review's 3D view (display only)
    def _arch_upload(self):
        """A model file sent from the page (this PC or a colleague's), read in the background."""
        from urllib.parse import parse_qs
        from building import arch_model
        if not self._from_this_app():
            return self._send(403, {"error": "Changes can only be made from the CAD to Revit app itself."})
        if self.require_login and not self._session():
            return self._send(401, {"error": "Sign in first."})
        if not S.project:
            return self._send(400, {"error": "Start or open a project first."})
        n = int(self.headers.get("Content-Length") or 0)
        name = os.path.basename((parse_qs(urlparse(self.path).query).get("name") or [""])[0])
        name = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", name).strip(" .")
        try:
            arch_model.check_name(name)
        except ValueError as exc:
            return self._send(400, {"error": f"{name or 'This file'}: {exc}"})
        if n <= 0 or n > arch_model.UPLOAD_MB * 1048576:
            return self._send(400, {"error": f"{name}: empty, or larger than {arch_model.UPLOAD_MB} MB."})
        m, path = arch_model.add(S.project, name, self._who_email())
        with open(path + ".part", "wb") as f:
            left = n
            while left > 0:
                chunk = self.rfile.read(min(left, 1 << 20))
                if not chunk:
                    break
                f.write(chunk)
                left -= len(chunk)
        if left > 0:
            arch_model.remove(S.project, m["id"])
            return self._send(400, {"error": f"{name}: the upload was cut off. Try again."})
        os.replace(path + ".part", path)
        arch_model.start(S.project, m["id"])
        return self._send(200, {"model": m})

    def _arch_file(self, rest):
        """A model's geometry.bin / meta.json, streamed (they can be tens of MB)."""
        from building import arch_model
        parts = rest.split("/")
        full = arch_model.model_path(S.project, parts[0], parts[1]) if len(parts) == 2 else ""
        if not full:
            return self._send(404, {"error": "not found"})
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8" if full.endswith(".json")
                         else "application/octet-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(os.path.getsize(full)))
        self.end_headers()
        with open(full, "rb") as f:
            while True:
                chunk = f.read(1 << 20)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def _arch_post(self, what, b):
        from building import arch_align, arch_model
        if not S.project:
            return self._send(400, {"error": "Start or open a project first."})
        if what == "two_points":              # the line-up from two points picked on the page
            return self._send(200, arch_align.two_points(*(tuple(map(float, b[k][:2])) for k in ("s1", "s2", "a1", "a2"))))
        mid = str(b.get("id") or "")
        m = next((x for x in arch_model.load(S.project)["models"] if x["id"] == mid), None)
        if m is None:
            return self._send(400, {"error": "That model is no longer in the project."})
        if what == "settings":
            return self._send(200, arch_model.settings(S.project, mid, {**b, "by": self._who_email()}))
        if what == "remove":
            arch_model.remove(S.project, mid)
            return self._send(200, {"ok": True})
        if what == "again":                   # read the file again (a failed read, or a newer app)
            arch_model._update(S.project, mid, state="waiting", message="Waiting ...")
            arch_model.start(S.project, mid)
            return self._send(200, {"ok": True})
        if what in ("align", "refine"):       # a proposal: shown on the page, saved only when the user says
            meta_p = arch_model.model_path(S.project, mid, "meta.json")
            if not meta_p:
                return self._send(400, {"error": "The model has not been read yet."})
            with open(meta_p, encoding="utf-8") as f:
                meta = json.load(f)
            cols = [(float(x), float(y)) for x, y in (b.get("cols") or [])[:20000]]
            if what == "refine":              # from where the user moved it by hand
                return self._send(200, arch_align.refine(arch_align.columns(meta, m.get("unit_mm"), m.get("up")),
                                                         cols, b.get("al") or {}))
            return self._send(200, arch_align.propose(meta, m, cols, b.get("levels") or []))
        return self._send(404, {"error": "not found"})

    def _file(self, full, name, ctype="application/octet-stream", attach=True):
        with open(full, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        if attach:
            from urllib.parse import quote
            plain = name.encode("ascii", "replace").decode().replace('"', "'")
            self.send_header("Content-Disposition", f"attachment; filename=\"{plain}\"; filename*=UTF-8''{quote(name)}")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        p = u.path
        self._whose()
        if self._gate_get(p):
            return
        if p == "/api/access":
            return self._send(200, self._access_view())
        if p == "/api/admin":
            return self._admin_get()
        if p == "/":
            return self._send(200, PAGE, "text/html")
        if p == "/api/state":
            m = re.search(r"since=(\d+)", u.query or "")
            return self._send(200, S.view(int(m.group(1)) if m else 0))
        if p == "/api/drawing_floors":
            return self._send(200, {"floors": _drawing_floors(S.run_dir)})
        if p == "/api/levels":
            return self._send(200, {"floors": _floors_with_added(parse_levels(S.levels)), "names": revit_names(),
                                    "known": [k for k, _ in FLOORS],
                                    "detected": detect_levels(S.files), "chosen": parse_levels(S.levels),
                                    # heights already given, by Revit level name (the same decisions
                                    # the Export page shows and the build uses)
                                    "elevations": (_overrides().get("project") or {}).get("levels") or {}})
        if p == "/api/decisions":
            return self._send(200, _overrides())
        if p == "/api/notes":
            from building import notes
            if not S.project:
                return self._send(200, {"notes": [], "changes": None})
            return self._send(200, {"notes": notes.load(S.project)["notes"],
                                    "changes": notes.revision_changes(S.run_dir) if S.run_dir else None,
                                    "me": self._who_email()})
        if p == "/api/cadtext":
            # the drawing's own text for one plan, laid over the CAD underneath (display only)
            from urllib.parse import parse_qs
            from building import cad_text
            sid = (parse_qs(u.query or "").get("sheet") or [""])[0]
            try:
                return self._send(200, cad_text.for_plan(S.run_dir or "", sid))
            except Exception as exc:
                return self._send(200, {"t": [], "error": str(exc)})
        if p.startswith("/notes/img/") and S.project:
            from building import notes
            full = notes.image_path(S.project, unquote(p[len("/notes/img/"):]))
            if not full:
                return self._send(404, {"error": "not found"})
            return self._file(full, "", "image/png" if full.endswith(".png") else "image/jpeg", attach=False)
        if p.startswith("/notes/report/") and S.project:
            from building import notes
            name = os.path.basename(unquote(p[len("/notes/report/"):]))
            full = notes.folder(S.project, "reports", name)
            if not name or not os.path.isfile(full):
                return self._send(404, {"error": "not found"})
            return self._file(full, name)
        if p == "/api/arch":
            # the architect's models of this project (display only, Review's 3D view)
            from building import arch_model
            return self._send(200, {"models": arch_model.load(S.project)["models"] if S.project else []})
        if p.startswith("/arch/") and S.project:
            return self._arch_file(unquote(p[len("/arch/"):]))
        if p == "/api/check":
            from building.prebuild import check
            proj = _overrides().get("project") or {}
            return self._send(200, {**check(S.run_dir, proj), "decided": proj} if S.run_dir else {"exported": False})
        if p == "/api/revit":
            if S.running == "revit":
                return self._send(200, {"connected": True, "busy": True, "message": "Building in Revit ..."})
            from building import revit_mcp
            return self._send(200, revit_mcp.status())
        if p.startswith("/download/") and S.project:
            name = os.path.basename(unquote(p[len("/download/"):]))
            full = os.path.join(S.project, "packages", name)
            if S.rvt_file() and name == os.path.basename(S.rvt_file()):
                full = S.rvt_file()                  # saved wherever the user chose
            elif S.ifc_file() and name == os.path.basename(S.ifc_file()):
                full = S.ifc_file()
            elif S.rhino_file() and name == os.path.basename(S.rhino_file()):
                full = S.rhino_file()
            if not os.path.isfile(full):
                return self._send(404, {"error": "not found"})
            with open(full, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Disposition", f'attachment; filename="{name}"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if p.startswith("/run/") and S.run_dir:
            rel = unquote(p[len("/run/"):]).replace("/", os.sep)
            full = os.path.normpath(os.path.join(S.run_dir, rel))
            run = os.path.normcase(os.path.normpath(S.run_dir))
            try:                        # inside the run folder itself, not a sibling like "run2"
                inside = os.path.commonpath([os.path.normcase(full), run]) == run
            except ValueError:          # another drive
                inside = False
            if not inside or not os.path.isfile(full):
                return self._send(404, {"error": "not found"})
            with open(full, "rb") as f:
                data = f.read()
            if full.endswith(".html"):
                html = data.decode("utf-8")
                if full.endswith("review.html"):
                    from building.notes_ui import REVIEW_HOOK
                    from building.arch_ui import ARCH_HOOK
                    html = html.replace("</body>", HOOK + REVIEW_HOOK + ARCH_HOOK + "</body>")
                return self._send(200, html, "text/html")
            return self._send(200, data, "text/plain" if full.endswith(".txt") else "application/octet-stream")
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        p = urlparse(self.path).path
        self._whose()
        if p == "/api/upload":
            return self._upload()
        if p == "/api/notes/reply":
            return self._notes_reply()
        if p == "/api/arch/upload":
            return self._arch_upload()
        b = self._body()
        if not self._from_this_app():
            return self._send(403, {"error": "Changes can only be made from the CAD to Revit app itself."})
        if p == "/api/login":
            return self._login(b)
        if p in ("/api/password/forgot", "/api/password/set", "/api/password/name"):
            if not self._remote():
                return self._send(404, {"error": "not found"})
            if p.endswith("name"):
                return self._reset_name(b)
            return self._forgot(b) if p.endswith("forgot") else self._set_password(b)
        if p == "/api/logout":
            m = re.search(r"(?:^|;\s*)c2r_session=([\w-]+)", self.headers.get("Cookie") or "")
            if m:
                _load_sessions()
                SESSIONS.pop(_tok_key(m.group(1)), None)
                _save_sessions()
            return self._send(200, {"ok": True},
                              headers=[("Set-Cookie", "c2r_session=; Max-Age=0; HttpOnly; SameSite=Strict; Path=/")])
        if self.require_login and not self._session():
            return self._send(401, {"error": "Sign in first."})
        if p == "/api/admin":
            return self._admin_post(b)
        try:
            if p == "/api/pick":
                kind = b.get("kind", "files")
                if self._remote():
                    # a colleague's browser: nothing opens on this PC's screen. A new project is
                    # made in their own folder; drawings are uploaded; projects open from their list
                    if kind != "new":
                        return self._send(400, {"error": "Use the buttons on the page: drawings are uploaded "
                                                         "from your PC, projects open from your list."})
                    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', " ", (b.get("name") or "").strip()).strip(" .") \
                        or "New project"
                    S.open(os.path.join(S.office_home, name))
                    return self._send(200, S.view())
                if kind in ("files", "folder") and not S.project:
                    return self._send(400, {"error": "Start or open a project first."})
                got = _dialog(kind)
                if got and kind in ("files", "folder"):
                    _add(got)
                elif got and kind == "open":
                    S.open(got[0])
                elif got and kind == "new":
                    name = re.sub(r'[<>:"/\\|?*]+', " ", (b.get("name") or "").strip()) or "New project"
                    S.open(os.path.join(got[0], name))
                return self._send(200, S.view())
            if p == "/api/recent":
                if self._remote():
                    from building import office
                    if not _office_may_open(S, b.get("path", "")):
                        return self._send(403, {"error": "That project is not one of yours."})
                same = S.project and os.path.abspath(b.get("path", "")) == S.project
                if os.path.isdir(b.get("path", "")) and not (same and S.running):
                    # (the open project while it is analysing or exporting: just carry on with it)
                    S.open(b["path"])
                return self._send(200, S.view())
            if p == "/api/sample":
                _sample()
                return self._send(200, S.view())
            if p == "/api/remove":
                i = int(b.get("i", -1))
                if 0 <= i < len(S.files):
                    del S.files[i]
                    S.save()
                return self._send(200, S.view())
            if p == "/api/replace":
                i = int(b.get("i", -1))
                if not 0 <= i < len(S.files):
                    return self._send(400, {"error": "That drawing is no longer in the list."})
                if self._remote():
                    return self._send(400, {"error": "Use the button on the page: the revision is uploaded."})
                got = _dialog("revision:" + S.files[i])
                if not got:
                    return self._send(200, {**S.view(), "cancelled": True})
                _replace(i, got[0])
                return self._send(200, {**S.view(), "replaced": True})
            if p == "/api/notes":
                from building import notes
                if not S.project:
                    return self._send(400, {"error": "No project is open, so the note was not saved."})
                return self._send(200, notes.change(S.project, b, self._who_email(),
                                                    os.path.basename(S.run_dir or "")))
            if p == "/api/notes/report":
                return self._notes_report(b)
            if p.startswith("/api/arch/"):
                return self._arch_post(p[len("/api/arch/"):], b)
            if p == "/api/clear":
                S.files = []
                S.save()
                return self._send(200, S.view())
            if p == "/api/reset":
                # a new set of drawings for this project: the list emptied and the last analysis
                # set aside (kept on disk, with every decision), so Review and Export do not show
                # results of drawings no longer in the project
                if S.running:
                    return self._send(400, {"error": "Wait until the current work finishes."})
                S.files = []
                if S.run_dir:
                    S.runs_after = os.path.basename(S.run_dir)
                S.run_dir = None
                S.package = S.rvt_out = S.ifc_out = S.rhino_out = S.exported_to = ""
                S.log = []
                S.save()
                return self._send(200, S.view())
            if p == "/api/levels":
                S.levels = levels_text({k: v for k, v in (b.get("chosen") or {}).items() if v})
                S.save()
                return self._send(200, S.view())
            if p == "/api/options":
                S.suggested = bool(b.get("suggested", S.suggested))
                S.save()
                return self._send(200, S.view())
            if p == "/api/analyse":
                if not S.files:
                    return self._send(400, {"error": "Import the drawings first (step 1)."})
                ok = _job("analyse", _analyse)
                return self._send(200 if ok else 409, S.view())
            if p == "/api/export":
                if not S.run_dir:
                    return self._send(400, {"error": "Analyse the drawings first (step 2)."})
                dest = None
                if b.get("ask") and not self._remote():   # the page asks where the files should go
                    got = _dialog("export")
                    if not got:
                        return self._send(200, {**S.view(), "cancelled": True})
                    dest = S.export_dir = got[0]
                    S.save()
                ok = _job("export", lambda: _export(dest))
                return self._send(200 if ok else 409, S.view())
            if p == "/api/revit":
                if not (S.run_dir and os.path.isdir(os.path.join(S.run_dir, "revit_build"))):
                    return self._send(400, {"error": "Export to Revit first."})
                to_file = b.get("to") == "file"
                if self._remote() and not to_file:
                    return self._send(400, {"error": "Building into an open Revit model works only on the app's "
                                                     "own PC. Create a Revit file and download it instead."})
                if to_file and b.get("ask") and not self._remote():
                    got = _dialog("save_rvt")
                    if not got:
                        return self._send(200, {**S.view(), "cancelled": True})
                    S.rvt_out = got[0]
                ok = _job("revit", lambda: _revit(to_file))
                return self._send(200 if ok else 409, S.view())
            if p == "/api/ifc":
                if not (S.run_dir and os.path.isdir(os.path.join(S.run_dir, "revit_build"))):
                    return self._send(400, {"error": "Export to Revit first."})
                if b.get("ask") and not self._remote():
                    got = _dialog("save_ifc")
                    if not got:
                        return self._send(200, {**S.view(), "cancelled": True})
                    S.ifc_out = got[0]
                ok = _job("ifc", _ifc)
                return self._send(200 if ok else 409, S.view())
            if p == "/api/rhino":
                if not (S.run_dir and os.path.isdir(os.path.join(S.run_dir, "revit_build"))):
                    return self._send(400, {"error": "Export to Revit first."})
                if b.get("ask") and not self._remote():
                    got = _dialog("save_3dm")
                    if not got:
                        return self._send(200, {**S.view(), "cancelled": True})
                    S.rhino_out = got[0]
                ok = _job("rhino", _rhino)
                return self._send(200 if ok else 409, S.view())
            if p == "/api/package":
                path = None
                if b.get("ask") and not self._remote():
                    got = _dialog("save_zip")
                    if not got:
                        return self._send(200, {**S.view(), "cancelled": True})
                    path = got[0]
                path = _package(path)
                return self._send(200, {**S.view(), "package": path})
            if p == "/api/decisions":
                if not S.project:             # nowhere to keep them: the page says NOT SAVED
                    return self._send(400, {"error": "No project is open, so the decisions were not saved."})
                d = _overrides()              # the Export page's project decisions are kept
                d.update({"elements": b.get("elements", {}), "marks": b.get("marks", {}),
                          "exclude": b.get("exclude", [])})
                if "prefixes" in b:           # (a review page from before 2026-10-15 sends none: kept)
                    d["prefixes"] = {str(k).upper(): v for k, v in (b.get("prefixes") or {}).items() if v}
                    _save_prefix_hints(d["prefixes"])
                _save_overrides(d)
                return self._send(200, {"ok": True})
            if p == "/api/project":
                if not S.project:
                    return self._send(400, {"error": "Start or open a project first."})
                _project_decisions(b)
                if b.get("no_export"):     # the Floors step: kept for the next analysis
                    return self._send(200, S.view())
                if S.run_dir:              # the Revit files follow the decisions at once
                    ok = _job("export", lambda: _export(None))
                    if not ok:
                        return self._send(409, {"error": "The app is busy - your decisions are saved; "
                                                         "export again when it is done."})
                return self._send(200, S.view())
            if p == "/api/open":
                if self._remote():          # it would open on this PC's screen, not theirs
                    return self._send(400, {"error": "Download the files from the page instead."})
                what = b.get("what")
                target = {"revit": os.path.join(S.run_dir or "", "revit_build"), "project": S.project,
                          "xlsx": os.path.join(S.run_dir or "", "review.xlsx"),
                          "package": os.path.dirname(S.package) if S.package else
                          (os.path.join(S.project, "packages") if S.project else ""),
                          "rvtdir": os.path.dirname(S.rvt_file()) if S.rvt_file() else "",
                          "ifcdir": os.path.dirname(S.ifc_file()) if S.ifc_file() else "",
                          "rhinodir": os.path.dirname(S.rhino_file()) if S.rhino_file() else "",
                          "notes": os.path.join(S.project, "notes", "reports") if S.project else "",
                          "exported": S.exported_to}.get(what)
                if target and os.path.exists(target):
                    os.startfile(target)
                return self._send(200, {"ok": bool(target and os.path.exists(target))})
        except Exception as exc:
            return self._send(400, {"error": str(exc)})
        return self._send(404, {"error": "not found"})


PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>CAD to Revit</title>
<style>
:root{--bg:#f4f3ef;--panel:#fff;--ink:#16202B;--mute:#5f6c77;--line:#e1ded6;--accent:#1f6fb2;--accent2:#e8f1fa;
 --ok:#2e8b57;--warn:#b98a00;--bad:#c0392b;--r:10px}
*{box-sizing:border-box}html,body{height:100%}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 system-ui,'Segoe UI',sans-serif;display:flex;flex-direction:column}
button{font:inherit;border:1px solid var(--line);background:#fff;border-radius:8px;padding:8px 14px;cursor:pointer;color:var(--ink)}
button:hover{border-color:#c8c4b8}button:focus-visible{outline:3px solid #9cc3e8;outline-offset:1px}
button.go{background:var(--accent);color:#fff;border-color:var(--accent);font-weight:600}button.go:hover{filter:brightness(1.07)}
button:disabled{opacity:.45;cursor:not-allowed}button.link{border:0;background:none;color:var(--accent);padding:4px}
header{display:flex;align-items:center;gap:14px;padding:10px 20px;background:var(--panel);border-bottom:1px solid var(--line)}
.logo{display:flex;align-items:center;gap:10px;font-weight:700;font-size:16px}
.logo i{width:28px;height:28px;border-radius:7px;background:linear-gradient(135deg,#1f6fb2,#2e8b57);display:inline-block}
#projbtn{display:flex;gap:8px;align-items:center;background:#f7f6f2}#projbtn small{color:var(--mute)}
.stepper{display:flex;gap:0;margin-left:20px;flex:1}
.st{display:flex;align-items:center;gap:8px;padding:6px 14px;border-radius:20px;cursor:pointer;color:var(--mute);white-space:nowrap}
.st .n{width:24px;height:24px;border-radius:50%;border:2px solid #cfcbc0;display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:700}
.st.on{background:var(--accent2);color:var(--ink)}.st.on .n{border-color:var(--accent);color:var(--accent)}
.st.done .n{background:var(--ok);border-color:var(--ok);color:#fff}.st.done .n::after{content:''}
.sep{width:26px;height:2px;background:#d8d4ca;align-self:center}
#help{margin-left:auto}
main{flex:1;overflow:auto;position:relative}
.pane{display:none;max-width:1080px;margin:0 auto;padding:26px 24px 40px}.pane.on{display:block}
#p3.on{display:flex;flex-direction:column;height:100%;max-width:none;padding:0}
#p3 iframe{flex:1;border:0;width:100%;background:#fff}
.note{padding:8px 16px;background:#fff;border-bottom:1px solid var(--line);font-size:13px;color:var(--mute);display:flex;gap:12px;align-items:center}
h2{margin:0 0 4px;font-size:22px}.sub{color:var(--mute);margin:0 0 20px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);padding:18px;margin-bottom:16px}
.card h3{margin:0 0 4px;font-size:15px}.card p{margin:0 0 12px;color:var(--mute)}
.drop{border:2px dashed #cfcbc0;border-radius:var(--r);padding:28px;text-align:center;background:#fbfaf7}
.drop b{display:block;font-size:16px;margin-bottom:6px}.drop .row{justify-content:center}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.flist{margin-top:14px}.f{display:flex;align-items:center;gap:12px;padding:9px 6px;border-bottom:1px solid #efede7}
.f:last-child{border:0}.badge{font-size:11px;font-weight:700;padding:2px 7px;border-radius:5px;background:#e9eef5;color:#2a5b8a}
.f select.un{font:inherit;font-size:12px;padding:3px 4px;border:1px solid #d9d6cf;border-radius:6px;background:#fff;color:inherit}
.f .nm{flex:1;min-width:0}.f .nm div{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.f small{color:var(--mute)}
.x{border:0;background:none;color:var(--mute);font-size:18px;padding:0 6px}.x:hover{color:var(--bad)}
.empty{color:var(--mute);text-align:center;padding:16px}
.lv{display:grid;grid-template-columns:repeat(auto-fill,minmax(400px,1fr));gap:6px 24px}
.lv .elv{width:96px;font:inherit;padding:4px 6px;border:1px solid var(--line);border-radius:6px}.lv label.off .elv{opacity:.4}
.lv label{display:flex;align-items:center;gap:10px;padding:4px 6px;border-radius:6px}.lv label:hover{background:#f7f6f2}
.lv label span{flex:1}.lv select{font:inherit;padding:4px 6px;border:1px solid var(--line);border-radius:6px;min-width:160px}
.lv label.off span{color:var(--mute)}.lv label.off select{opacity:.4}
.lv label input[data-n]{flex:1;min-width:120px}.lv label.off input[data-n]{opacity:.45}.lv .lvfl{flex:0 1 auto;white-space:nowrap}
.lv label.hid{display:none}.lv .lvwarn{color:var(--warn)!important;font-weight:600}
.lv .lvx{border:0;background:none;color:var(--mute);font-size:18px;line-height:1;padding:0 4px;cursor:pointer}.lv .lvx:hover{color:var(--bad)}
.prog{height:10px;background:#ece9e1;border-radius:6px;overflow:hidden;margin:12px 0 6px}.prog i{display:block;height:100%;background:var(--accent);transition:width .4s}
.stages{display:flex;gap:18px;flex-wrap:wrap;font-size:13px;color:var(--mute)}.stages .ok{color:var(--ok)}.stages .now{color:var(--ink);font-weight:600}
details{margin-top:10px}summary{cursor:pointer;color:var(--mute)}
pre{background:#fbfaf7;border:1px solid var(--line);border-radius:8px;padding:10px;max-height:300px;overflow:auto;font-size:12px;white-space:pre-wrap}
.cards{display:flex;gap:12px;flex-wrap:wrap;margin:6px 0 14px}.k{background:#fbfaf7;border:1px solid var(--line);border-radius:8px;padding:10px 16px;min-width:110px}
.k b{font-size:22px;display:block}.k span{font-size:11px;color:var(--mute);text-transform:uppercase;letter-spacing:.4px}
ol.todo{padding-left:20px;margin:6px 0}ol.todo li{margin:6px 0}
.welcome{max-width:760px;margin:60px auto;text-align:center}.welcome h1{font-size:28px;margin:10px 0 6px}
.welcome .tiles{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin:26px 0;text-align:left}
.tile{background:#fff;border:1px solid var(--line);border-radius:var(--r);padding:18px;cursor:pointer}.tile:hover{border-color:var(--accent)}
.tile b{display:block;margin-bottom:4px}.tile small{color:var(--mute)}
.recent{text-align:left}.recent div{padding:8px 10px;border-radius:6px;cursor:pointer;display:flex;justify-content:space-between}
.recent div:hover{background:#fff}.recent small{color:var(--mute)}
.two{display:grid;grid-template-columns:1fr 1fr;gap:16px}
.modal{position:fixed;inset:0;background:rgba(22,32,43,.35);display:none;align-items:center;justify-content:center;z-index:10}
.modal.on{display:flex}.box{background:#fff;border-radius:12px;padding:22px;max-width:560px;width:92%}
.box h3{margin:0 0 8px}.box input{font:inherit;width:100%;padding:8px 10px;border:1px solid var(--line);border-radius:8px;margin:6px 0 14px}
.toast{position:fixed;bottom:18px;left:50%;transform:translateX(-50%);background:var(--ink);color:#fff;padding:10px 16px;border-radius:8px;display:none;z-index:20}
.rvstat{display:flex;align-items:center;gap:8px;color:var(--ink)!important}.rvstat i{width:10px;height:10px;border-radius:50%;background:#c8c4b8;flex:none}
.rvstat.ok i{background:var(--ok)}.rvstat.no i{background:var(--warn)}.rvmsg{margin:10px 0 0!important;font-size:13px}
.pnote{margin:2px 0 8px!important;font-size:12px}.plist{max-height:180px;overflow:auto;font-size:13px;border:1px solid var(--line);border-radius:8px;padding:6px 10px;background:#fbfaf7}
.plist div{padding:3px 0;border-bottom:1px solid #efede7}.plist div:last-child{border:0}.plist small{color:var(--mute)}
.dec{margin-top:16px;padding-top:14px;border-top:1px solid var(--line)}
.lvin{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:6px 18px}
.lvin label{display:flex;align-items:center;gap:8px}.lvin label span{flex:1}.lvin input{font:inherit;width:110px;padding:5px 8px;border:1px solid var(--line);border-radius:6px}
.lvin label.need input{border-color:var(--bad);background:#fff6f5}.lvin small{color:var(--mute);width:96px}
@media (max-width:820px){.stepper .lbl{display:none}.welcome .tiles,.two{grid-template-columns:1fr}}
.remote .hostonly{display:none!important}
</style></head><body>
<header><div class="logo"><i></i>CAD to Revit</div>
 <button id="projbtn" onclick="go(0)"><span id="pname">No project</span><small>change</small></button>
 <nav class="stepper" id="stepper">
  <div class="st" data-s="1"><span class="n">1</span><span class="lbl">Import CAD</span></div><div class="sep"></div>
  <div class="st" data-s="2"><span class="n">2</span><span class="lbl">Analyse</span></div><div class="sep"></div>
  <div class="st" data-s="3"><span class="n">3</span><span class="lbl">Review</span></div><div class="sep"></div>
  <div class="st" data-s="4"><span class="n">4</span><span class="lbl">Export to Revit</span></div></nav>
 <span id="who" style="margin-left:auto;font-size:13px;color:var(--mute)"></span>
 <button id="help" style="margin-left:0" onclick="$('helpm').classList.add('on')">Help</button></header>
<main>
<div class="pane" id="p0"><div class="welcome"><div class="logo" style="justify-content:center"><i></i></div>
 <h1>Structural drawings to Revit</h1><p class="sub">Import a job's structural CAD drawings, check every value with its source, and build the Revit model.</p>
 <div class="tiles">
  <div class="tile" onclick="newProject()"><b>New project</b><small>Start a job: choose where its project is kept.</small></div>
  <div class="tile hostonly" onclick="post('/api/pick',{kind:'open'}).then(afterOpen)"><b>Open project</b><small>Continue a job you started before.</small></div>
  <div class="tile" onclick="post('/api/sample').then(afterOpen)"><b>Try a sample</b><small>A small drawing set to see how it works.</small></div>
 </div>
 <div class="recent" id="recent"></div></div></div>

<div class="pane" id="p1"><h2>Import CAD</h2><p class="sub">Add the job's structural drawings - DWG or DXF, from any consultant, as many as the job has.</p>
 <div class="card"><div class="drop"><b>Add your drawings</b><p style="color:var(--mute);margin:0 0 12px">Plans, schedules, details and sections - all of them help.</p>
  <div class="row"><button class="go" onclick="pick('files')">Add drawings...</button><button onclick="pick('folder')">Add a whole folder...</button></div></div>
  <div class="flist" id="files"></div></div>
 <div class="row"><button class="go" id="to2" onclick="go(2)">Continue to Analyse &rarr;</button><span id="nf" style="color:var(--mute)"></span>
  <button id="reset" onclick="resetSet()" style="margin-left:auto" title="Remove every drawing from this project to import a new set">Start a new drawing set</button></div></div>

<div class="pane" id="p2"><h2>Analyse</h2><p class="sub">Tell the app which floors the building has and what they are called in your Revit model, then analyse.</p>
 <div class="card"><h3>Floors</h3><p id="lvhint">Ticked from your drawings' names. Tick every floor the building has.</p>
  <div class="row" style="margin-bottom:12px"><span>Ground up to</span><select id="upto" style="font:inherit;padding:6px;border-radius:6px;border:1px solid var(--line)"></select>
   <span style="color:var(--mute)">|</span><span>Names:</span><button onclick="scheme('level')">Level 0, 1, 2 ...</button><button onclick="scheme('words')">Ground, First, Second ...</button>
   <button onclick="namesFromDrawings()" title="Tick the floors the last analysis found plans of, and name each one as the plans' titles write it">As in the drawings</button>
   <button class="link" onclick="lvclear()">Untick all</button></div>
  <div class="row" style="margin-bottom:12px"><span>Height of each level (mm from the drawings' &plusmn;0) - type them beside the floors, or fill them from one floor-to-floor height:</span>
   <input type="number" id="ftf" step="1" placeholder="e.g. 3000" style="width:110px;font:inherit;padding:6px;border:1px solid var(--line);border-radius:6px">
   <button onclick="fillHeights()">Fill heights</button></div>
  <div class="lv" id="lv"></div>
  <p style="margin:6px 0 0"><button class="link" id="lvall" type="button" onclick="lvToggleAll()" style="display:none"></button></p>
  <div class="row" style="margin-top:12px"><span>A floor not in the list:</span>
   <input id="addfl" placeholder="e.g. Service floor" title="Its name as the drawings' titles write it" style="width:170px;font:inherit;padding:6px;border:1px solid var(--line);border-radius:6px">
   <select id="addwhere" style="font:inherit;padding:6px;border-radius:6px;border:1px solid var(--line)"><option value="UNDER">below</option><option value="OVER">above</option></select>
   <select id="addbase" style="font:inherit;padding:6px;border-radius:6px;border:1px solid var(--line)"></select>
   <button onclick="addFloor()">Add floor</button></div>
  <p id="lvbase" style="margin:10px 0 0"></p></div>
 <div class="card"><h3>Layer names</h3><label><input type="checkbox" id="sugg"> Let the app work out what unfamiliar layers hold (recommended for a new consultant - you check the result in Review)</label></div>
 <div class="card" id="runcard"><div class="row"><button class="go" id="ana" onclick="analyse()">Analyse drawings</button><span id="eta" style="color:var(--mute)"></span></div>
  <div id="progress" style="display:none"><div class="prog"><i id="bar" style="width:0"></i></div><div class="stages" id="stages"></div></div>
  <div id="result"></div>
  <details><summary>Show details</summary><pre id="log"></pre></details></div></div>

<div class="pane" id="p3"><div class="note"><span>Click any element to see where each value comes from. Choose or type a value where sources disagree or nothing is given.</span>
 <span style="margin-left:auto"><b id="ndec3">0</b> decisions &middot; saved automatically</span><button class="go" onclick="go(4)">Done - Export &rarr;</button></div>
 <iframe id="rv" title="Review"></iframe></div>

<div class="pane" id="p4"><h2>Export to Revit</h2><p class="sub">Your decisions go into the model, and the files Revit builds from are written.</p>
 <div class="card"><div class="row"><button class="go" id="exp" onclick="exportit()">Export to Revit</button><span id="expmsg" style="color:var(--mute)"></span></div></div>
 <div class="card" id="pre" style="display:none"><h3>Before you build</h3>
  <p>What goes to Revit, and what waits for you. Nothing is built on a guessed size, depth or height.</p>
  <div class="cards" id="prek"></div>
  <div class="two"><div><b id="pren_h">Needs a decision</b><p class="pnote" id="pren_n">Not built until you decide it in Review.</p><div class="plist" id="pren"></div>
    <label style="display:block;margin:8px 0"><input type="checkbox" id="bunset" onchange="saveProject()"> Build these anyway at a working size, coloured red in Revit with the reason in their Comments</label>
    <button class="link" onclick="go(3)">Open the review &rarr;</button></div>
   <div><b>Not built by the app yet</b><p class="pnote">Model these by hand for now.</p><div class="plist" id="pres"></div></div></div>
  <div class="dec"><b>Floor levels</b><p class="pnote">The height of each level in mm, measured from the drawings' &plusmn;0. Levels the drawings give are filled in; the rest are needed before building into a new file.</p>
   <div class="lvin" id="prelv"></div>
   <p class="pnote" id="prelh" style="margin-top:6px"></p>
   <div id="prest"></div>
   <div id="prefd"></div>
   <div id="preor"></div>
   <div class="row" style="margin-top:10px"><button class="go" onclick="saveProject()">Save and update the Revit files</button><span id="premsg" style="color:var(--mute)"></span></div></div></div>
 <div class="card"><h3>Queries for the consultant</h3><p>Your notes and screenshots from Review, one row each, with a column for the consultant's answer. Send the Excel or the PDF; when the Excel comes back filled in, load it below.</p>
  <p id="nqn" style="color:var(--ink)"></p>
  <label style="display:block;margin:4px 0"><input type="checkbox" id="nqall"> Also list every value still in conflict or not given (no screenshot for those)</label>
  <label style="display:block;margin:4px 0 12px"><input type="checkbox" id="nqclosed"> Include closed queries</label>
  <div class="row"><button class="go" onclick="notesReport('xlsx')">Download Excel</button><button onclick="notesReport('pdf')">Download PDF</button>
   <span style="color:var(--mute)">|</span><button onclick="replyPick()">Load the consultant's reply (Excel)...</button></div>
  <p id="nqmsg" class="rvmsg"></p></div>
 <div class="two">
  <div class="card hostonly"><h3>Build in Revit now</h3><p>Builds straight into the project open in Revit, through the live connection (Revit MCP). Every floor, bottom up, then foundations and stairs.</p>
   <p id="rvstat" class="rvstat"><i></i><span>Checking Revit ...</span> <button class="link" onclick="revitStatus()">Check again</button></p>
   <button class="go" id="rvopen" onclick="revitGo('open')">Build in the open model</button><p class="rvmsg" id="rvmsg_open"></p></div>
  <div class="card"><h3>Create a file</h3><p><b>Revit file (.rvt)</b> - a new project from Revit's structural template with the model built into it, saved where you choose. No live connection needed and nothing to do in Revit: the app starts a Revit of its own, which builds, saves and closes by itself; a Revit you have open is not touched. On a PC without Revit 2025 or later, an IFC file is made instead.</p>
   <p id="lvhint" style="display:none;color:#b42318"></p>
   <button id="rvfile" onclick="revitGo('file')">Create Revit file</button><p class="rvmsg" id="rvmsg_file"></p><p id="rvtdl"></p>
   <p style="margin-top:14px"><b>IFC file (.ifc)</b> - the same model in the open BIM format, written by the app alone: no Revit needed. Revit opens it (File &gt; Open &gt; IFC), as do other BIM programs; its elements are less editable than native Revit ones.</p>
   <button id="ifcfile" onclick="ifcGo()">Create IFC file</button><p class="rvmsg" id="ifcmsg"></p><p id="ifcdl"></p>
   <p style="margin-top:14px"><b>Rhino file (.3dm)</b> - the same model for Rhino, written by the app alone: no Rhino needed. A layer per level and type, and per beam size in the colours of the Revit build's filters; what still needs a decision is red, its reason in the object's user text.</p>
   <button id="rhinofile" onclick="rhinoGo()">Create Rhino file</button><p class="rvmsg" id="rhinomsg"></p><p id="rhinodl"></p></div>
  <div class="card"><h3>Build it in Revit yourself</h3><p>With Rhino.Inside.Revit, the <b>CAD to Revit</b> Grasshopper component builds every floor and the foundations into the open model in one press.</p>
   <ol class="todo"><li>Or by hand: run <b>build_model.py</b> on each floor's model.json, lowest first.</li><li>Then <b>build_extras.py</b> on foundations.json.</li></ol>
   <button id="openr" class="hostonly" onclick="openit('revit')">Open the Revit files</button></div>
  <div class="card"><h3>Send it to your Revit team</h3><p>One .zip with the Revit files, the review page and the summary - for a colleague who builds the model.</p>
   <button id="pkg" onclick="makepkg()">Create Revit package (.zip)</button><p id="pkgmsg" style="margin-top:10px"></p></div></div>
 <div class="card"><h3>Also</h3><div class="row"><button onclick="openit('xlsx')">Review workbook (Excel)</button>
  <button onclick="window.open('/run/model3d.html')">3D view in its own tab</button><button onclick="window.open('/run/SUMMARY.txt')">Summary</button>
  <button class="hostonly" onclick="openit('project')">Project folder</button></div></div></div>
</main>
<div class="modal" id="newm"><div class="box"><h3>New project</h3><p id="newsub" style="color:var(--mute);margin:0">Name the job, then choose where its project folder is kept.</p>
 <input id="newname" placeholder="e.g. Hotel Main Block"><div class="row" style="justify-content:flex-end"><button onclick="$('newm').classList.remove('on')">Cancel</button>
 <button class="go" id="newgo" onclick="createProject()">Choose location...</button></div></div></div>
<div class="modal" id="helpm"><div class="box"><h3>How it works</h3><ol class="todo">
 <li><b>Import CAD</b> - add every structural drawing of the job.</li>
 <li><b>Analyse</b> - tick the floors and name them as in Revit; the app reads and cross-checks every drawing.</li>
 <li><b>Review</b> - green is read and agreed; red means sources disagree; yellow means nothing gives the value. Click, decide, move on. Switch Plan / 3D at the top. Something only the consultant can answer? Take a screenshot and write a note in the element's panel.</li>
 <li><b>Export to Revit</b> - build in Revit, or send the package to whoever does. Send the consultant your queries as Excel or PDF, and load their filled-in Excel back.</li></ol>
 <p style="color:var(--mute)">A revised drawing from the consultant: <b>Replace with revision</b> beside the old one in Import CAD, then Analyse. Your decisions and notes move to it, and Review lists what changed.</p>
 <p style="color:var(--mute)">The app never guesses a size: anything unclear is shown for you to decide. Everything stays on this computer.</p>
 <div class="row" style="justify-content:flex-end"><button class="go" onclick="$('helpm').classList.remove('on')">Got it</button></div></div></div>
<div class="toast" id="toast"></div>
<script>
const $=id=>document.getElementById(id);let st={},logn=0,lvopt=null,cur=0,lastRun=null,lines=[];
function toast(t){const e=$('toast');e.textContent=t;e.style.display='block';clearTimeout(e._t);e._t=setTimeout(()=>e.style.display='none',3500)}
async function post(u,b){const r=await fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b||{})});
 let j={};try{j=await r.json()}catch(e){}if(!r.ok&&j.error){toast(j.error)}if(j.files)apply(j);return j}
const UNITS=[['','Units: auto'],['mm','Millimetres'],['cm','Centimetres'],['m','Metres'],['in','Inches'],['ft','Feet']];
let REMOTE=false;   // the office mode, in a colleague's browser: drawings are uploaded, results downloaded
function pick(kind){if(REMOTE){upPick(kind);return}post('/api/pick',{kind})}
function upPick(kind){const i=document.createElement('input');i.type='file';i.multiple=true;
 if(kind==='folder')i.webkitdirectory=true;else i.accept='.dwg,.dxf';
 i.onchange=()=>upload([...i.files].filter(f=>/\.(dwg|dxf)$/i.test(f.name)));i.click()}
async function upload(fs){if(!fs.length){toast('No DWG or DXF drawings were chosen.');return}
 for(let k=0;k<fs.length;k++){const f=fs[k];$('nf').textContent=`Uploading ${k+1} of ${fs.length}: ${f.name} ...`;
  try{const r=await fetch('/api/upload?name='+encodeURIComponent(f.name),{method:'POST',headers:{'Content-Type':'application/octet-stream'},body:f});
   const j=await r.json();if(!r.ok){toast(j.error||('Not added: '+f.name))}else apply(j)}catch(e){toast('Upload stopped: '+f.name)}}
 toast(fs.length+' drawing'+(fs.length>1?'s':'')+' uploaded.')}
function newProject(){$('newname').value='';$('newm').classList.add('on');setTimeout(()=>$('newname').focus(),50)}
function createProject(){const name=$('newname').value.trim()||'New project';$('newm').classList.remove('on');post('/api/pick',{kind:'new',name}).then(afterOpen)}
function afterOpen(j){if(j&&j.project){lvopt=null;$('log').textContent='';lines=[];$('rv').dataset.run='';go(j.files.length?(j.run?3:2):1)}}
function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
function go(n){if(n>0&&!st.project){toast('Start or open a project first.');n=0}
 if(n===3&&!st.run){toast('Analyse the drawings first.');n=st.files&&st.files.length?2:1}
 if(n===4&&!st.run){toast('Analyse the drawings first.');n=2}
 cur=n;document.querySelectorAll('.st').forEach(s=>s.classList.toggle('on',+s.dataset.s===n));
 document.querySelectorAll('.pane').forEach(p=>p.classList.toggle('on',p.id==='p'+n));
 if(n===3&&$('rv').dataset.run!==st.run){$('rv').src='/run/review.html?'+Date.now();$('rv').dataset.run=st.run}
 if(n===2)levels();if(n===4){revitStatus();precheck();notesInfo()}}
document.querySelectorAll('.st').forEach(s=>s.onclick=()=>go(+s.dataset.s));
const STAGES=[['prepare','Preparing'],['read','Reading drawings'],['link','Linking and checking'],['review','Writing the review'],['revit','Writing Revit files'],['done','Done']];
function stage(){let s='prepare',i=0,n=Math.max(1,(st.files||[]).length);
 for(const r of lines){if(/^\n\S.*\.(dwg|dxf)$/i.test(r))i++;const l=r.trim();
  if(l.startsWith('Linked:'))s='link';else if(l.startsWith('Review:'))s='review';else if(l.startsWith('Writing the Revit'))s='revit';
  else if(l==='STAGE done')s='done';else if(i&&s==='prepare')s='read'}
 return {s,i:Math.min(i,n),n}}
function showProgress(){const p=stage(),idx=STAGES.findIndex(x=>x[0]===p.s);
 let pct=[3,0,80,88,94,100][idx];if(p.s==='read')pct=5+70*(p.i-1+0.5)/p.n;
 $('bar').style.width=pct+'%';$('stages').innerHTML=STAGES.map((x,k)=>`<span class="${k<idx?'ok':k===idx?'now':''}">${k<idx?'&#10003; ':''}${x[1]}${x[0]==='read'&&k===idx?` (${p.i} of ${p.n})`:''}</span>`).join('')}
function apply(j){st=j;$('pname').textContent=j.name||'No project';
 $('recent').innerHTML=j.recent&&j.recent.length?'<p class="sub" style="text-align:left;margin:0 0 6px">Recent projects</p>'+j.recent.map(r=>`<div onclick="post('/api/recent',{path:'${esc(r.path).replace(/\\/g,'\\\\').replace(/'/g,"\\'")}'}).then(afterOpen)"><span>${esc(r.name)}</span><small>${esc(r.path)}</small></div>`).join(''):'';
 $('files').innerHTML=j.files.length?j.files.map((f,i)=>`<div class="f"><span class="badge">${esc(f.kind)}</span><div class="nm"><div>${esc(f.name)}</div><small>${esc(f.folder)}${f.mb?' · '+f.mb+' MB':''}${f.rev_of?' · <b>revision</b> of '+esc(f.rev_of):''}</small></div><select class="un" title="The drawing's units. Auto: the app finds them from the drawing itself" onchange="post('/api/project',{units_set:{[st.files[${i}].name]:this.value},no_export:true})">${UNITS.map(([v,t])=>`<option value="${v}"${(f.units||'')===v?' selected':''}>${t}</option>`).join('')}</select><button class="link" title="The consultant sent a revised drawing: it takes this one's place, your decisions and notes move to it, and the changes are listed in Review" onclick="replaceRev(${i})">Replace with revision...</button><button class="x" title="Remove" onclick="post('/api/remove',{i:${i}})">&times;</button></div>`).join('')
  :'<div class="empty">No drawings yet. Add the job\'s DWG or DXF files above.</div>';
 $('nf').textContent=j.files.length?j.files.length+' drawing'+(j.files.length>1?'s':'')+' ready':'';$('to2').disabled=!j.files.length;
 const done={1:j.files.length>0,2:!!j.run,3:j.decisions>0,4:j.built};
 document.querySelectorAll('.st').forEach(s=>s.classList.toggle('done',!!done[+s.dataset.s]&&+s.dataset.s!==cur));
 $('sugg').checked=j.suggested;$('ana').disabled=!!j.running||!j.files.length;$('ana').textContent=j.run?'Analyse again':'Analyse drawings';
 $('eta').textContent=j.running==='analyse'?'Working - a few minutes; drawings converted before are reused.':(j.files.length?'':'Import drawings first.');
 $('exp').disabled=!!j.running||!j.run;$('openr').disabled=!j.built;$('pkg').disabled=!j.built||!!j.running;
 $('ndec3').textContent=j.decisions||0;if(!asking)$('expmsg').textContent=j.running==='export'?'Exporting...':(j.built?`Exported${j.decisions?' with '+j.decisions+' decision'+(j.decisions>1?'s':''):''}.`:'');
 revButtons();
 $('rvtdl').innerHTML=j.rvt&&!revTo?`<a href="/download/${encodeURIComponent(j.rvt.split(/[\\/]/).pop())}" download><button class="go">Download ${esc(j.rvt.split(/[\\/]/).pop())}</button></a> <button class="link hostonly" onclick="openit('rvtdir')">Show in folder</button>`:'';
 $('ifcdl').innerHTML=j.ifc&&!ifcOn?`<a href="/download/${encodeURIComponent(j.ifc.split(/[\\/]/).pop())}" download><button class="go">Download ${esc(j.ifc.split(/[\\/]/).pop())}</button></a> <button class="link hostonly" onclick="openit('ifcdir')">Show in folder</button>`:'';
 if(j.exported_to&&!j.running&&!asking)$('expmsg').innerHTML=`Saved in <b>${esc(j.exported_to)}</b> <button class="link hostonly" onclick="openit('exported')">Open folder</button>`;
 if(j.package)$('pkgmsg').innerHTML=`Saved: <b>${esc(j.package.split(/[\\/]/).pop())}</b> <button class="link hostonly" onclick="openit('package')">Show in folder</button>`+
  (REMOTE?` <a href="/download/${encodeURIComponent(j.package.split(/[\\/]/).pop())}" download><button class="go">Download</button></a>`:'');
 if(j.log&&j.log.length){lines.push(...j.log);$('log').textContent=lines.filter(l=>!l.startsWith('STAGE')).join('\n');$('log').scrollTop=1e9}
 revitProgress();ifcProgress();rhinoProgress();
 $('rhinodl').innerHTML=j.rhino&&!rhinoOn?`<a href="/download/${encodeURIComponent(j.rhino.split(/[\\/]/).pop())}" download><button class="go">Download ${esc(j.rhino.split(/[\\/]/).pop())}</button></a> <button class="link hostonly" onclick="openit('rhinodir')">Show in folder</button>`:'';
 if(wasRunning==='export'&&!j.running&&cur===4)precheck();wasRunning=j.running||'';
 logn=j.logn||logn;$('progress').style.display=j.running==='analyse'||(lines.length&&cur===2&&!j.summary)?'':'none';if(j.running==='analyse')showProgress();
 const s=j.summary;if(s&&!j.running){const v=s.values||{},el=s.elements||{};
  $('result').innerHTML=`<h3 style="margin:16px 0 6px">Result</h3><div class="cards">`+[[s.plans,'plans'],...Object.entries(el).filter(([k])=>['column','beam','slab','wall','footing','stair'].includes(k)).map(([k,n])=>[n,k+'s']),
   [v.read||0,'values read'],[v.conflict||0,'to decide'],[v.missing||0,'not given']].map(([n,l])=>`<div class="k"><b style="color:${l=='to decide'&&n?'var(--bad)':l=='not given'&&n?'var(--warn)':'inherit'}">${n}</b><span>${l}</span></div>`).join('')+
   `</div>${carryText(j.carry)}<h3 style="margin:8px 0 2px">What to look at</h3><ol class="todo">${(s.next||[]).map(x=>`<li>${esc(x)}</li>`).join('')}</ol><button class="go" onclick="go(3)">Open the review &rarr;</button>`}
 if(lastRun!==null&&j.run&&j.run!==lastRun&&!j.running){toast('Analysis done.')}
 if(j.running===''&&lastRun!==j.run&&cur===4&&j.built)toast('Revit files written.');lastRun=j.run}
async function poll(){try{const r=await fetch('/api/state?since='+logn);apply(await r.json())}catch(e){}setTimeout(poll,st.running?700:2500)}
window.addEventListener('message',e=>{if(e.data&&e.data.c2r==='decided')fetch('/api/state?since='+logn).then(r=>r.json()).then(apply)});
async function levels(){lvopt=await (await fetch('/api/levels')).json();const ch=lvopt.chosen||{},det=lvopt.detected||[];
 // the floors the last analysis found plans of: shown always; the rest only when ticked or asked for
 try{lvopt.df=((await (await fetch('/api/drawing_floors')).json()).floors)||{}}catch(e){lvopt.df={}}
 $('lvhint').textContent=det.length?'Ticked from your drawings\' names. Tick every floor the building has.':'Tick every floor the building has.';
 const ev=lvopt.elevations||{};
 // one name per floor: the floor's own name, which is also its Revit level name - type over it to
 // call it something else in Revit (then the drawings' floor is shown beside it)
 $('lv').innerHTML=`<datalist id="lvnames">${lvopt.names.map(x=>`<option value="${esc(x)}">`).join('')}</datalist>`+lvopt.floors.map(([k,n])=>{const on=k in ch||det.includes(k),nm=ch[k]||ownName(k,n);return `<label class="${on?'':'off'}" data-row="${k}"><input type="checkbox" data-k="${k}" ${on?'checked':''}>
  <input type="text" list="lvnames" data-n="${k}" data-own="${esc(ownName(k,n))}" data-floor="${esc(n)}" value="${esc(nm)}" title="This floor's name - also its Revit level name. Type over it to call it something else in Revit." style="width:150px;font:inherit;padding:4px 6px;border:1px solid var(--line);border-radius:6px">
  <input type="number" class="elv" step="1" data-e="${k}" value="${ev[nm]!=null?Math.round(ev[nm]):''}" placeholder="height mm" title="Height of this level in mm, from the drawings' ±0">
  <small class="lvfl" style="color:var(--mute)"></small><button class="lvx" type="button" data-x="${k}" title="Remove this floor from the list (untick and hide it)">&times;</button></label>`}).join('');
 document.querySelectorAll('#lv .lvx').forEach(b=>b.onclick=ev=>{ev.preventDefault();const c=document.querySelector(`#lv input[data-k="${b.dataset.x}"]`);c.checked=false;savelv()});
 lvVisible();
 $('upto').innerHTML='<option value="">choose a floor</option>'+lvopt.floors.filter(([k])=>/^L\d/.test(k)).map(([k,n])=>`<option value="${k}">${esc(n)}</option>`).join('');
 $('upto').onchange=e=>{if(!e.target.value)return;const n=+e.target.value.slice(1);document.querySelectorAll('#lv input[type=checkbox]').forEach(c=>{const k=c.dataset.k;if(k==='GF'||(/^L\d/.test(k)&&+k.slice(1)<=n))c.checked=true});savelv()};
 document.querySelectorAll('#lv input[type=checkbox]').forEach(c=>c.onchange=savelv);
 // a renamed level shows the height kept under its new name
 document.querySelectorAll('#lv input[data-n]').forEach(s=>s.onchange=()=>{if(!s.value.trim())s.value=s.dataset.own;s.value=s.value.trim();
  const e=document.querySelector(`#lv .elv[data-e="${s.dataset.n}"]`);
  const v=(lvopt.elevations||{})[s.value];e.value=v!=null?Math.round(v):'';floorNote(s);savelv()});
 document.querySelectorAll('#lv .elv').forEach(e=>e.onchange=()=>setHeight(nameOf(e.dataset.e),e.value));
 const known=lvopt.known||[],keep=$('addbase').value||'GF';
 $('addbase').innerHTML=lvopt.floors.filter(([k])=>known.includes(k)).map(([k,n])=>`<option value="${k}"${k===keep?' selected':''}>${esc(n)}</option>`).join('');
 baseLine()}
// a floor the app has no name for ("Service floor"): added for this project, just below or above
// a floor it knows; plans titled with its name are read as that floor
async function addFloor(){const nm=$('addfl').value.trim(),w=nm.toUpperCase().match(/[A-Z0-9]+/g);
 if(!w){toast('Type the floor\'s name as the drawings write it, e.g. Service floor.');return}
 const k='X_'+w.join('_')+'_'+$('addwhere').value+'_'+$('addbase').value,chosen={};
 document.querySelectorAll('#lv input[type=checkbox]:checked').forEach(c=>chosen[c.dataset.k]=nameOf(c.dataset.k));
 chosen[k]=chosen[k]||nm.replace(/\b[a-z]/g,c=>c.toUpperCase());
 await post('/api/levels',{chosen});$('addfl').value='';await levels();
 toast('Added. Plans titled "'+nm+'" are read as this floor - type its height beside it.')}
function nameOf(k){return document.querySelector(`#lv input[data-n="${k}"]`).value}
// the floors the last analysis found, ticked and named as the plans' titles write them ("THIRD FLOOR
// LAYOUT PLAN" -> Third Floor); the other floors stay as they are
async function namesFromDrawings(){const f=((await (await fetch('/api/drawing_floors')).json()).floors)||{},ks=Object.keys(f);
 if(!ks.length){toast('Analyse the drawings first - the names are read from the plans\' titles.');return}
 if(!confirm('Tick the '+ks.length+' floors your drawings have plans of, and name each one as the drawings write it?'))return;
 const chosen={};document.querySelectorAll('#lv input[type=checkbox]:checked').forEach(c=>chosen[c.dataset.k]=nameOf(c.dataset.k));
 for(const k of ks){const row=document.querySelector(`#lv input[data-n="${k}"]`);chosen[k]=f[k]||(row?row.dataset.own:k)}
 await post('/api/levels',{chosen});await levels();toast('Named as in the drawings - type over any name to change it.')}
// a floor's own name: the usual Revit name of the floors with one (Stilt, Mumty), else as listed
function ownName(k,n){return defname(k,'own')||n}
// renamed for Revit: which floor of the drawings it is, beside it; an added floor says where it sits
function floorNote(s){const f=s.dataset.floor,x=/\((above|below) [^)]*\)$/.exec(f),l=s.parentElement,t=l.querySelector('.lvfl'),df=lvopt.df||{};
 // ticked, but the drawings have no plan of this floor (a name typed into the row of another floor)
 const none=l.querySelector('input[type=checkbox]').checked&&Object.keys(df).length&&!(s.dataset.n in df);
 // an added floor found in no plan yet: added since the last analysis (or no title says its name)
 t.classList.toggle('lvwarn',!!none&&!x);
 t.textContent=x?x[0]+(none?' - Analyse again to find its plan':''):none?'no '+f+' plan in the drawings':(s.value.trim().toLowerCase()!==s.dataset.own.toLowerCase()?'('+f+' in the drawings)':'')}
// only the floors in use: those the drawings have plans of, and those ticked; the others behind
// "Show all floors". A floor with no plan can be removed (unticked and hidden) with its x
let lvShowAll=false;
function lvVisible(){const df=lvopt.df||{},det=lvopt.detected||[],rows=[...document.querySelectorAll('#lv label[data-row]')];
 const any=Object.keys(df).length||det.length||rows.some(l=>l.querySelector('input[type=checkbox]').checked);let hidden=0;
 rows.forEach(l=>{const k=l.dataset.row,on=l.querySelector('input[type=checkbox]').checked,show=lvShowAll||!any||on||k in df||det.includes(k);
  l.classList.toggle('hid',!show);if(!show)hidden++;l.querySelector('.lvx').style.visibility=(k in df||!on)?'hidden':''});
 $('lvall').textContent=lvShowAll?'Show only the floors in use':`Show all floors (${hidden} more)`;$('lvall').style.display=lvShowAll||hidden?'':'none';
 document.querySelectorAll('#lv input[data-n]').forEach(floorNote)}
function lvToggleAll(){lvShowAll=!lvShowAll;lvVisible()}
function setHeight(name,v){lvopt.elevations=lvopt.elevations||{};if(v==='')delete lvopt.elevations[name];else lvopt.elevations[name]=+v;
 const s={};s[name]=v;return post('/api/project',{level_set:s,no_export:true})}
function floorNo(k){return k==='GF'?0:/^L\d+$/.test(k)?+k.slice(1):null}
function belowName(n){const m=/^(level\s*)(-?\d+)$/i.exec(n||'');return m?m[1]+(+m[2]-1):'Below '+n}
// the lowest ticked floor's columns rise from the level below it: its height is asked here too
function baseLine(){const on=[...document.querySelectorAll('#lv input[type=checkbox]:checked')].map(c=>c.dataset.k);
 const ranked=lvopt.floors.map(([k])=>k).filter(k=>on.includes(k));if(!ranked.length){$('lvbase').innerHTML='';return}
 const bn=belowName(nameOf(ranked[0]));if(on.some(k=>nameOf(k)===bn)){$('lvbase').innerHTML='';return}
 const v=(lvopt.elevations||{})[bn];
 $('lvbase').innerHTML=`The lowest floor's columns start from <b>${esc(bn)}</b> at <input type="number" id="elvbase" step="1" value="${v!=null?Math.round(v):''}" placeholder="height mm" style="width:110px;font:inherit;padding:4px 6px;border:1px solid var(--line);border-radius:6px"> mm`;
 $('elvbase').onchange=e=>setHeight(bn,e.target.value)}
// one floor-to-floor height: the ground floor at ±0, floor n at n heights up, and the level the
// lowest floor's columns start from one height below it
async function fillHeights(){const h=+$('ftf').value;if(!h){toast('Type the floor-to-floor height first (mm).');return}
 const on=[...document.querySelectorAll('#lv input[type=checkbox]:checked')].map(c=>c.dataset.k).filter(k=>floorNo(k)!==null);
 if(!on.length){toast('Tick the ground floor or numbered floors first.');return}
 const s={},low=Math.min(...on.map(floorNo));
 for(const k of on){const z=floorNo(k)*h;s[nameOf(k)]=z;document.querySelector(`#lv .elv[data-e="${k}"]`).value=z}
 if($('elvbase')){const bn=belowName(nameOf(on.find(k=>floorNo(k)===low)));s[bn]=(low-1)*h;$('elvbase').value=(low-1)*h}
 Object.assign(lvopt.elevations=lvopt.elevations||{},s);await post('/api/project',{level_set:s,no_export:true});toast('Heights filled - change any of them if a floor differs.')}
const ORD=['','First','Second','Third','Fourth','Fifth','Sixth','Seventh','Eighth','Ninth','Tenth','Eleventh','Twelfth','Thirteenth','Fourteenth','Fifteenth'];
function defname(k,s){const sp={FDN:'Foundation',B2:'Basement 2',B1:'Basement 1',PLINTH:'Plinth',STILT:'Stilt',MEZZ:'Mezzanine',TERRACE:'Terrace',ROOF:'Roof',MUMTY:'Mumty',OHT:'OHT'};if(sp[k])return sp[k];
 const x=/^X_(.+?)_(OVER|UNDER)_/.exec(k);if(x)return x[1].toLowerCase().replace(/_/g,' ').replace(/\b[a-z]/g,c=>c.toUpperCase());
 const el=/^EL([+-])(\d+)$/.exec(k);if(el)return el[1]+el[2]+' Level';   // a floor the drawings name by its height
 if(s==='own')return '';            // the ground and numbered floors: their name as listed
 if(k.endsWith('_LINTEL'))return defname(k.slice(0,-7),s)+' Lintel';
 const n=k==='GF'?0:+k.slice(1);return s==='level'?'Level '+n:(n?ORD[n]+' Floor':'Ground Floor')}
function scheme(s){document.querySelectorAll('#lv input[data-n]').forEach(x=>{if(!x.dataset.n.startsWith('X_')){x.value=defname(x.dataset.n,s);floorNote(x)}});savelv()}
function lvclear(){document.querySelectorAll('#lv input[type=checkbox]').forEach(c=>c.checked=false);savelv()}
function savelv(){const chosen={};document.querySelectorAll('#lv input[type=checkbox]').forEach(c=>{c.parentElement.classList.toggle('off',!c.checked);
 if(c.checked)chosen[c.dataset.k]=nameOf(c.dataset.k)});baseLine();lvVisible();post('/api/levels',{chosen})}
$('sugg').onchange=e=>post('/api/options',{suggested:e.target.checked});
function analyse(){if(!document.querySelectorAll('#lv input[type=checkbox]:checked').length&&!confirm('No floor is ticked. Analyse anyway, with the floors as the drawings name them?'))return;
 lines=[];$('log').textContent='';$('result').innerHTML='';$('progress').style.display='';showProgress();post('/api/analyse')}
let asking=false;
function exportit(){asking=true;$('expmsg').textContent=REMOTE?'Exporting ...':'Choose where the Revit files should go ...';
 post('/api/export',{ask:!REMOTE}).then(j=>{asking=false;if(j.cancelled)$('expmsg').textContent=''},()=>{asking=false})}
function makepkg(){$('pkgmsg').textContent=REMOTE?'Making the package ...':'Choose where to save the package ...';post('/api/package',{ask:!REMOTE}).then(j=>{if(j.cancelled)$('pkgmsg').textContent='';else if(j.package)toast('Revit package created.')})}
function openit(what){if(REMOTE){if(what==='xlsx')location.href='/run/review.xlsx';return}post('/api/open',{what})}
let revTo=null,revOk=false;
async function revitStatus(){const e=$('rvstat');e.className='rvstat';e.querySelector('span').textContent='Checking Revit ...';
 try{const s=await (await fetch('/api/revit')).json();revOk=!!s.connected;e.className='rvstat '+(s.connected?'ok':'no');e.querySelector('span').textContent=s.message}
 catch(x){revOk=false;e.className='rvstat no';e.querySelector('span').textContent='Could not reach the app.'}revButtons()}
function revButtons(){const busy=!!st.running,lvm=!!(pre&&pre.levels_missing&&pre.levels_missing.length);
 // a new file needs no open Revit: the app starts it, or writes IFC - only the levels
 $('rvopen').disabled=busy||!st.built||!revOk;$('rvfile').disabled=busy||!st.built||lvm;$('ifcfile').disabled=busy||!st.built||lvm;
 $('rvfile').title=$('ifcfile').title=lvm?'Enter the floor levels marked red under "Before you build" first':'';
 $('rhinofile').disabled=$('ifcfile').disabled;$('rhinofile').title=$('ifcfile').title;
 const h=$('lvhint');h.style.display=lvm&&st.built?'':'none';
 if(lvm)h.textContent='Both files need the height of every floor. Still missing: '+pre.levels_missing.join(', ')+'. Enter them under "Before you build" above, then press "Save and update the Revit files".'}
let pre=null,wasRunning='';
async function precheck(){if(!st.run){$('pre').style.display='none';return}
 let c;try{c=await (await fetch('/api/check')).json()}catch(e){return}pre=c;
 if(!c.exported){$('pre').style.display='none';revButtons();return}
 $('pre').style.display='';const r=c.ready||{},nd=c.needs_decision||[],ns=c.not_supported||[],dec=c.decided||{};
 if(c.stale){$('prek').innerHTML='<p>These Revit files were written by an older version. Press <b>Export to Revit</b> above to check them before building.</p>';
  $('pren').innerHTML=$('pres').innerHTML='';}else{
 $('prek').innerHTML=Object.entries(r).map(([k,n])=>`<div class="k"><b>${n}</b><span>${esc(k)}</span></div>`).join('')+
  `<div class="k"><b style="color:${nd.length?'var(--bad)':'inherit'}">${nd.length}</b><span>need a decision</span></div>`;
 $('pren_h').textContent='Needs a decision ('+nd.length+')';
 const bu=dec.build_unsettled!==false;$('bunset').checked=bu;
 $('pren_n').textContent=bu?'Built at the working size shown, coloured red in Revit, until you decide it in Review.':'Not built until you decide it in Review.';
 $('pren').innerHTML=nd.length?nd.map(x=>`<div>${x.built?'<b style="color:var(--bad)">built red</b> ':''}${esc(x.what)} <small>&middot; ${esc(x.where)}</small></div>`).join(''):'<div>Nothing - every element has what it needs.</div>';
 $('pres').innerHTML=(c.blocked_drawings||[]).map(d=>`<div><b>Drawing not read:</b> ${esc(d)}</div>`).join('')+
  (ns.length?ns.map(x=>`<div>${esc(x.what)} <small>&middot; ${esc(x.where)}</small></div>`).join(''):(c.blocked_drawings||[]).length?'':'<div>Nothing.</div>')}
 $('prelv').innerHTML=(c.levels||[]).map(l=>{const v=l.elevation_mm==null?'':Math.round(l.elevation_mm),fromdwg=l.source==='drawings';
  return `<label class="${v===''?'need':''}"><span>${esc(l.name)}</span><input type="number" step="1" data-lv="${esc(l.name)}" value="${v}" placeholder="needed"${fromdwg?' readonly title="Given by the drawings"':''}>
  <small>${fromdwg?'from the drawings':l.source==='decided'?'your decision':'not given'}</small></label>`}).join('');
 const lh=c.level_hints||[];
 $('prelh').innerHTML=lh.length?'Levels written in the drawings - click one to put its value in the level you last clicked (or the first empty one): '+
  lh.map(h=>`<button class="link" onclick="useLv(${+h.elevation_mm})">${esc(h.text)}</button>`).join(' &middot; '):'';
 document.querySelectorAll('#prelv input:not([readonly])').forEach(i=>i.onfocus=()=>{lvLast=i});
 const slabT=dec.slab_thickness_mm,stm=c.slab_thickness_missing||0;
 $('prest').innerHTML=(stm||slabT!=null)?`<p style="margin:12px 0 4px"><b>Slab thickness</b></p><p class="pnote">${stm?stm+' slabs have no thickness anywhere in the drawings, so they are not built. ':''}One thickness for every slab the drawings give none (mm); they are built at it, coloured red, until decided in Review.</p>
  <div class="lvin"><label class="${slabT==null&&stm?'need':''}"><span>Slabs with no thickness</span><input type="number" step="1" id="sth" value="${slabT==null?'':slabT}" placeholder="e.g. 150"><small>${slabT==null?'not given':'your decision'}</small></label></div>`:'';
 const fd=dec.founding_level_mm;
 $('prefd').innerHTML=(c.founding_missing||fd!=null)?`<p style="margin:12px 0 4px"><b>Founding level</b></p><p class="pnote">${c.founding_missing?c.founding_missing+' footings or rafts have no founding level in the drawings. ':''}One level for all of them (mm from &plusmn;0, e.g. -1500), or decide each footing in Review. Footings whose drawings give one keep theirs.</p>
  <div class="lvin"><label class="${fd==null&&c.founding_missing?'need':''}"><span>Bottom of footings</span><input type="number" step="1" id="fdl" value="${fd==null?'':fd}" placeholder="e.g. -1500"><small>${fd==null?'not given':'your decision'}</small></label></div>`:'';
 const og=dec.origin,orr=Object.values(c.origin||{});
 $('preor').innerHTML=(orr.length||og)?`<p style="margin:12px 0 4px"><b>Shared origin</b></p><p class="pnote">${og?'Every floor is built from the point you set.':'The floors\' own base points differ, so every floor is built from one point - the most south-westerly ('+orr.map(o=>Math.round(o.origin[0])+', '+Math.round(o.origin[1])).join('; ')+'). If the drawings share a grid point, type it here once.'} Drawing units (x, y).</p>
  <div class="lvin"><label><span>x</span><input type="number" step="1" id="orx" value="${og?og[0]:''}" placeholder="from the drawings"></label><label><span>y</span><input type="number" step="1" id="ory" value="${og?og[1]:''}" placeholder="from the drawings"></label></div>`:'';
 $('premsg').textContent=(c.levels_missing||[]).length?'Levels marked red are needed before creating a Revit file.':
  justSaved?'Saved and the Revit files updated. Revit itself changes only when you build again: press "Build in the open model" or "Create Revit file" below.':'';revButtons()}
let justSaved=false;
let lvLast=null;
function useLv(z){const ins=[...document.querySelectorAll('#prelv input:not([readonly])')];
 const i=(lvLast&&ins.includes(lvLast))?lvLast:ins.find(x=>x.value==='');if(!i)return;
 i.value=Math.round(z);i.closest('label').classList.remove('need');i.focus();
 $('premsg').textContent='Press "Save and update the Revit files" to use it.'}
function saveProject(){const levels={};document.querySelectorAll('#prelv input:not([readonly])').forEach(i=>{levels[i.dataset.lv]=i.value});
 const body={levels,build_unsettled:$('bunset').checked};if($('fdl'))body.founding_level_mm=$('fdl').value;if($('sth'))body.slab_thickness_mm=$('sth').value;if($('orx'))body.origin=[$('orx').value,$('ory').value];$('premsg').textContent='Saved - updating the Revit files ...';
 justSaved=true;post('/api/project',body).then(j=>{if(j&&j.error){justSaved=false;$('premsg').textContent=''}})}
let revFrom=0;
function revitGo(to){revTo=to;revFrom=lines.length;$('rvmsg_'+to).textContent=to==='file'&&!REMOTE?'Choose where to save the Revit file ...':'Starting ...';$('rvtdl').innerHTML='';
 post('/api/revit',{to,ask:to==='file'&&!REMOTE}).then(j=>{if(j.cancelled){$('rvmsg_'+to).textContent='';revTo=null}else if(to==='file')$('rvmsg_'+to).textContent='Starting ...'})}
function revitProgress(){if(!revTo)return;const r=lines.slice(revFrom).filter(l=>l.startsWith('REVIT '));if(!r.length)return;
 const last=r[r.length-1].slice(6),done=last==='DONE',stop=last.startsWith('STOPPED'),nf=r.filter(l=>l.startsWith('REVIT FLOOR')).length;
 $('rvmsg_'+revTo).innerHTML=done?'<b style="color:var(--ok)">Done.</b> '+nf+' step'+(nf==1?'':'s')+' built. Each floor\'s build log is beside its model.json.'
  :stop?'<b style="color:var(--bad)">Stopped:</b> '+esc(last.slice(9)):esc(last.replace(/^FLOOR /,'Building '));
 if(done||stop){if(done&&revTo==='open')toast('Built in Revit.');if(done&&revTo==='file')toast(st.ifc&&!st.rvt?'IFC file created (no Revit on this PC).':'Revit file created.');revTo=null}}
let ifcOn=false,ifcFrom=0;
function ifcGo(){ifcOn=true;ifcFrom=lines.length;$('ifcmsg').textContent=REMOTE?'Writing ...':'Choose where to save the IFC file ...';$('ifcdl').innerHTML='';
 post('/api/ifc',{ask:!REMOTE}).then(j=>{if(j.cancelled){$('ifcmsg').textContent='';ifcOn=false}else $('ifcmsg').textContent='Writing ...'})}
function ifcProgress(){if(!ifcOn)return;const r=lines.slice(ifcFrom).filter(l=>l.startsWith('IFC '));if(!r.length)return;
 const last=r[r.length-1].slice(4),done=last==='DONE',stop=last.startsWith('STOPPED'),w=r.find(l=>l.startsWith('IFC Written ')),lo=r.find(l=>l.startsWith('IFC Left out'));
 $('ifcmsg').innerHTML=done?'<b style="color:var(--ok)">Done.</b> '+esc(w?w.replace(/^IFC Written .*?: /,''):'')+(lo?' &middot; '+esc(lo.slice(4,lo.indexOf(':')))+' (see the log)':'')
  :stop?'<b style="color:var(--bad)">Stopped:</b> '+esc(last.slice(9)):esc(last);
 if(done||stop){if(done)toast('IFC file created.');ifcOn=false}}
let rhinoOn=false,rhinoFrom=0;
function rhinoGo(){rhinoOn=true;rhinoFrom=lines.length;$('rhinomsg').textContent=REMOTE?'Writing ...':'Choose where to save the Rhino file ...';$('rhinodl').innerHTML='';
 post('/api/rhino',{ask:!REMOTE}).then(j=>{if(j.cancelled){$('rhinomsg').textContent='';rhinoOn=false}else $('rhinomsg').textContent='Writing ...'})}
function rhinoProgress(){if(!rhinoOn)return;const r=lines.slice(rhinoFrom).filter(l=>l.startsWith('RHINO '));if(!r.length)return;
 const last=r[r.length-1].slice(6),done=last==='DONE',stop=last.startsWith('STOPPED'),w=r.find(l=>l.startsWith('RHINO Written ')),lo=r.find(l=>l.startsWith('RHINO Left out'));
 $('rhinomsg').innerHTML=done?'<b style="color:var(--ok)">Done.</b> '+esc(w?w.replace(/^RHINO Written .*?: /,''):'')+(lo?' &middot; '+esc(lo.slice(6,lo.indexOf(':')))+' (see the log)':'')
  :stop?'<b style="color:var(--bad)">Stopped:</b> '+esc(last.slice(9)):esc(last);
 if(done||stop){if(done)toast('Rhino file created.');rhinoOn=false}}
// the app always opens on its start page: new, open, or one of the recent projects
fetch('/api/state').then(r=>r.json()).then(j=>{apply(j);lastRun=j.run;go(0)});poll();
// a consultant's revised drawing in place of drawing i: decisions and notes follow at the next analysis
async function replaceRev(i){
 if(REMOTE){const inp=document.createElement('input');inp.type='file';inp.accept='.dwg,.dxf';
  inp.onchange=async()=>{const f=inp.files[0];if(!f)return;$('nf').textContent='Uploading '+f.name+' ...';
   try{const r=await fetch('/api/upload?replace='+i+'&name='+encodeURIComponent(f.name),{method:'POST',headers:{'Content-Type':'application/octet-stream'},body:f});
    const j=await r.json();if(!r.ok)toast(j.error||'Not replaced.');else{apply(j);toast('Revision added - press Analyse: your decisions and notes move to it and the changes are listed.')}}
   catch(e){toast('Upload stopped: '+f.name)}};inp.click();return}
 const j=await post('/api/replace',{i});
 if(j&&j.replaced)toast('Revision added - press Analyse: your decisions and notes move to it and the changes are listed.')}
function carryText(c){if(!c)return '';const p=[];
 if(c.revised)p.push(`<b>${c.changes}</b> change${c.changes==1?'':'s'} in the revised drawing - listed in Review under "Changes in the revision"`);
 if(c.moved)p.push(`${c.moved} decision${c.moved==1?'':'s'} moved to the revised drawing`);
 if(c.notes_moved)p.push(`${c.notes_moved} note${c.notes_moved==1?'':'s'} moved to the revised drawing`);
 if(c.unmatched)p.push(`<span style="color:var(--bad)">${c.unmatched} decision${c.unmatched==1?'':'s'} or note${c.unmatched==1?'':'s'} could not be matched to an element and were kept as they were</span>`);
 return p.length?`<p style="margin:4px 0 10px">${p.join(' &middot; ')}</p>`:''}
async function notesInfo(){try{const j=await (await fetch('/api/notes')).json(),ns=j.notes||[],c=k=>ns.filter(n=>n.state===k).length;
 $('nqn').innerHTML=ns.length?`<b>${ns.length}</b> quer${ns.length==1?'y':'ies'}: ${c('open')} new, ${c('sent')} sent, ${c('answered')} answered, ${c('closed')} closed`
  :'No notes yet. In Review, click an element, take a screenshot and write what the consultant should check.'}catch(e){}}
async function notesReport(fmt){$('nqmsg').textContent='Writing the '+(fmt==='pdf'?'PDF':'Excel')+' ...';
 const j=await post('/api/notes/report',{fmt,open_values:$('nqall').checked,closed:$('nqclosed').checked});
 if(j&&j.name){$('nqmsg').innerHTML=`Saved: <b>${esc(j.name)}</b> (${j.count} quer${j.count==1?'y':'ies'}) <button class="link hostonly" onclick="openit('notes')">Show in folder</button>`;
  location.href='/notes/report/'+encodeURIComponent(j.name);notesInfo();$('rv').dataset.run=''}else $('nqmsg').textContent=''}
function replyPick(){const i=document.createElement('input');i.type='file';i.accept='.xlsx';
 i.onchange=async()=>{const f=i.files[0];if(!f)return;$('nqmsg').textContent='Reading '+f.name+' ...';
  try{const r=await fetch('/api/notes/reply?name='+encodeURIComponent(f.name),{method:'POST',headers:{'Content-Type':'application/octet-stream'},body:f});
   const j=await r.json();if(!r.ok){$('nqmsg').textContent='';toast(j.error||'Not read.');return}
   $('nqmsg').innerHTML=`<b>${j.answered}</b> answer${j.answered==1?'':'s'} added to their queries - open Review to see them and use the values.`+
    (j.unknown.length?` ${j.unknown.length} row${j.unknown.length==1?'':'s'} did not match a query of this project.`:'');
   $('rv').dataset.run='';notesInfo()}catch(e){$('nqmsg').textContent='';toast('The reply could not be sent to the app.')}};i.click()}
async function resetSet(){const n=(st.files||[]).length;
 if(!confirm(`Start a new drawing set for this project?\n\n${n?'The '+n+' drawing'+(n>1?'s':'')+' in the list will be removed, and ':''}`+
  `the last analysis set aside, so Review and Export wait for the new drawings.\n\nNothing is deleted: earlier results and `+
  `your decisions stay in the project folder.`))return;
 const j=await post('/api/reset');if(j&&j.project){$('rv').dataset.run='';lastRun=null;$('log').textContent='';lines=[];
  toast('Ready for the new drawings: add them above.');go(1)}}
fetch('/api/access').then(r=>r.json()).then(a=>{if(a.remote){REMOTE=true;document.body.classList.add('remote');
  $('newgo').textContent='Create';$('newsub').textContent='Name the job. It is kept with your other projects.'}
 if(a.signed_in)$('who').innerHTML=(a.user?'<b>'+esc(a.user)+'</b> &middot; ':'')+esc(a.email)+
 (a.expires?' &middot; until '+esc(a.expires):'')+(a.admin?' <button class="link" onclick="usersOpen()">Users</button>':'')+
 ' <button class="link" onclick="signOut()">Sign out</button>';
 if(a.admin&&/[?&]users=1/.test(location.search))usersOpen()}).catch(()=>{});
async function signOut(){await fetch('/api/logout',{method:'POST'});location.href='/'}
// admins: who may use the app - requests, approved users, add / renew / remove
function usersOpen(){let m=$('usersm');if(!m){m=document.createElement('div');m.id='usersm';m.className='modal';
 m.onclick=e=>{if(e.target===m)m.classList.remove('on')};document.body.appendChild(m)}m.classList.add('on');usersDraw()}
async function usersDraw(){const m=$('usersm');const r=await fetch('/api/admin');const a=await r.json();
 const close='<div class="row" style="margin-top:14px"><button onclick="$(\'usersm\').classList.remove(\'on\')">Close</button></div>';
 if(!r.ok){m.innerHTML=`<div class="box"><h3>Users</h3><p>${esc(a.error||'Not allowed.')}</p>${close}</div>`;return}
 const req=a.requests.length?a.requests.map(q=>`<div class="f"><div class="nm"><div>${q.name?'<b>'+esc(q.name)+'</b> &middot; ':''}${esc(q.email)}</div><small>PC ${esc(q.pc)} &middot; asked ${esc(q.asked||'')} ${q.computer?'&middot; '+esc(q.computer):''}</small></div>
  <button class="go" data-e="${esc(q.email)}" onclick="adm('approve',this.dataset.e)">Approve</button></div>`).join(''):'<div class="empty">No requests waiting.</div>';
 const us=a.users.length?a.users.map(u=>`<div class="f"><div class="nm"><div>${u.name?'<b>'+esc(u.name)+'</b> &middot; ':''}${esc(u.email)} ${u.role==='admin'?'<span class="badge">admin</span>':''}
  ${u.status!=='active'?'<span class="badge" style="background:#f6e3e1;color:#a33">removed</span>':''}</div>
  <small>${esc(u.plan||'')} &middot; until ${esc(u.expires||'-')} &middot; PCs ${esc((u.machines||[]).join(', ')||'-')}</small></div>
  <button data-e="${esc(u.email)}" onclick="adm('renew',this.dataset.e)">Renew</button>
  ${u.status==='active'?(u.email===a.me?'':`<button data-e="${esc(u.email)}" onclick="adm('remove',this.dataset.e)">Remove</button>`)
   :`<button data-e="${esc(u.email)}" onclick="adm('add',this.dataset.e)">Restore</button>`}</div>`).join(''):'<div class="empty">No users yet.</div>';
 m.innerHTML=`<div class="box" style="max-width:780px;max-height:90vh;overflow:auto"><h3>Who may use CAD to Revit</h3>
  <p class="pnote">Changes take effect at the user's next sign-in. This PC's code: <b>${esc(a.pc)}</b></p>
  <h3 style="margin-top:14px;font-size:14px">Requests</h3><div class="plist" style="max-height:none">${req}</div>
  <h3 style="margin-top:14px;font-size:14px">Approved users</h3><div class="plist" style="max-height:260px">${us}</div>
  <h3 style="margin-top:14px;font-size:14px">Add a user</h3>
  <div class="row"><input id="ad_em" type="email" placeholder="e-mail" style="width:220px;margin:0">
   <input id="ad_pc" placeholder="PC code, or any" style="width:140px;margin:0">
   <input id="ad_un" type="date" title="access until (empty: 180 days)" style="width:150px;margin:0">
   <select id="ad_role" style="font:inherit;padding:7px;border:1px solid var(--line);border-radius:8px"><option value="user">user</option><option value="admin">admin</option></select>
   <button class="go" onclick="adm('add',$('ad_em').value,{pc:$('ad_pc').value,until:$('ad_un').value,role:$('ad_role').value})">Add</button></div>${close}</div>`}
async function adm(action,email,extra){if(action==='remove'&&!confirm('Remove '+email+'? They will not be able to sign in.'))return;
 let until='';if(action==='renew'){until=prompt('Access for '+email+' until (YYYY-MM-DD):','');if(!until)return}
 const r=await fetch('/api/admin',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action,email,until,...(extra||{})})});
 const j=await r.json();toast(r.ok?'Saved: '+email:(j.error||'Not done.'));usersDraw()}
</script></body></html>"""

LOGIN_PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>CAD to Revit - sign in</title>
<style>
:root{--bg:#f4f3ef;--panel:#fff;--ink:#16202B;--mute:#5f6c77;--line:#e1ded6;--accent:#1f6fb2;--bad:#c0392b}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 system-ui,'Segoe UI',sans-serif;
 min-height:100vh;display:flex;align-items:center;justify-content:center;padding:16px}
.box{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:28px;width:100%;max-width:400px}
.logo{display:flex;align-items:center;gap:10px;font-weight:700;font-size:18px;margin-bottom:18px}
.logo i{width:30px;height:30px;border-radius:7px;background:linear-gradient(135deg,#1f6fb2,#2e8b57);display:inline-block}
label{display:block;font-weight:600;margin-bottom:6px}
input{font:inherit;width:100%;padding:10px 12px;border:1px solid var(--line);border-radius:8px}
input:focus-visible,button:focus-visible{outline:3px solid #9cc3e8;outline-offset:1px}
button{font:inherit;width:100%;margin-top:14px;padding:10px;border-radius:8px;border:1px solid var(--accent);
 background:var(--accent);color:#fff;font-weight:600;cursor:pointer}button:disabled{opacity:.5}
.msg{margin-top:14px;color:var(--bad);min-height:1em}.pc{margin-top:18px;font-size:12px;color:var(--mute)}
.pc b{color:var(--ink);letter-spacing:.5px}
</style></head><body>
<form class="box" onsubmit="go(event)">
 <div class="logo"><i></i>CAD to Revit</div>
 <label for="em">Your e-mail</label>
 <input id="em" type="email" autocomplete="email" required placeholder="name@company.com">
 <button id="b">Sign in</button>
 <div class="msg" id="msg" role="alert"></div>
 <div class="pc">Only e-mail addresses the admin has approved can use this app.<br>This PC's code: <b id="pc"></b></div>
</form>
<script>
const $=id=>document.getElementById(id);
fetch('/api/access').then(r=>r.json()).then(a=>{$('pc').textContent=a.pc;if(a.last)$('em').value=a.last;
 if(a.signed_in||!a.required)location.href='/';$('em').focus()});
async function go(e){e.preventDefault();$('b').disabled=true;$('msg').textContent='';
 try{const r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({email:$('em').value})});const j=await r.json();
  if(r.ok)location.href='/';else $('msg').textContent=j.error||'Not signed in.'}
 catch(x){$('msg').textContent='The app is not responding. Start it again (run_app.bat).'}
 $('b').disabled=false}
</script></body></html>"""

# the office mode, in a colleague's browser: e-mail and password, and a link by e-mail to set it
_OFFICE_STYLE = LOGIN_PAGE[LOGIN_PAGE.index("<style>"):LOGIN_PAGE.index("</style>") + 8].replace(
    "</style>", ".ok{color:#2e8b57}.alt{background:none;color:#1f6fb2;border:0;margin-top:10px;padding:4px;font-weight:400}"
                "\n.sub{color:#5f6c77;margin:0 0 14px}</style>")

OFFICE_LOGIN_PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>CAD to Revit - sign in</title>
""" + _OFFICE_STYLE + r"""</head><body>
<form class="box" id="f1" onsubmit="signin(event)">
 <div class="logo"><i></i>CAD to Revit</div>
 <label for="em">Your e-mail</label>
 <input id="em" type="email" autocomplete="email" required placeholder="name@company.com">
 <label for="us" style="margin-top:12px">User name</label>
 <input id="us" type="text" autocomplete="username" required maxlength="60" placeholder="Your own name">
 <label for="pw" style="margin-top:12px">Password</label>
 <input id="pw" type="password" autocomplete="current-password" required>
 <button id="b">Sign in</button>
 <button type="button" class="alt" onclick="forgot()">Set or reset my password</button>
 <div class="msg" id="msg" role="alert"></div>
 <div class="pc">Only e-mail addresses the admin has approved can use this app. Several people can share
  one e-mail: each uses their own user name and password, and has their own projects. First time here?
  Type the e-mail and your user name, then press "Set or reset my password": a link comes to the e-mail.</div>
</form>
<script>
const $=id=>document.getElementById(id);
async function call(u,b){const r=await fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)});
 let j={};try{j=await r.json()}catch(e){}return [r.ok,j]}
async function signin(e){e.preventDefault();$('b').disabled=true;$('msg').className='msg';$('msg').textContent='';
 try{const [ok,j]=await call('/api/login',{email:$('em').value,user:$('us').value,password:$('pw').value});
  if(ok)location.href='/';else $('msg').textContent=j.error||'Not signed in.'}
 catch(x){$('msg').textContent='The app is not responding. Ask the admin whether it is running.'}
 $('b').disabled=false}
async function forgot(){const em=$('em').value.trim(),us=$('us').value.trim();$('msg').className='msg';
 if(!em){$('msg').textContent='Type the e-mail first.';$('em').focus();return}
 if(!us){$('msg').textContent='Type your user name first: the password is set for that user name only.';$('us').focus();return}
 try{const [ok,j]=await call('/api/password/forgot',{email:em,user:us});$('msg').className='msg'+(ok?' ok':'');
  $('msg').textContent=ok?j.message:(j.error||'Not sent.')}catch(x){$('msg').textContent='The app is not responding.'}}
$('em').focus();
</script></body></html>"""

RESET_PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>CAD to Revit - password</title>
""" + _OFFICE_STYLE + r"""</head><body>
<form class="box" onsubmit="save(event)">
 <div class="logo"><i></i>CAD to Revit</div>
 <p class="sub" id="who">Choose your password (at least 8 characters).</p>
 <input id="us" type="text" autocomplete="username" readonly hidden>
 <label for="p1">New password</label>
 <input id="p1" type="password" autocomplete="new-password" required minlength="8">
 <label for="p2" style="margin-top:12px">Type it again</label>
 <input id="p2" type="password" autocomplete="new-password" required minlength="8">
 <button id="b">Save password</button>
 <div class="msg" id="msg" role="alert"></div>
</form>
<script>
const $=id=>document.getElementById(id);const tok=new URLSearchParams(location.search).get('t')||'';
history.replaceState(null,'','/reset');
const esc=s=>String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;');
fetch('/api/password/name',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:tok})})
 .then(r=>r.json()).then(j=>{if(!j.user){$('msg').textContent=j.error||'';return}$('us').value=j.user;
  $('who').innerHTML=(j.new?'New user ':'User name ')+'<b>'+esc(j.user)+'</b> ('+esc(j.email)+
   '). Choose '+(j.new?'a':'a new')+' password (at least 8 characters).';$('p1').focus()})
 .catch(()=>{});
async function save(e){e.preventDefault();$('msg').className='msg';
 if($('p1').value!==$('p2').value){$('msg').textContent='The two passwords are not the same.';return}
 $('b').disabled=true;
 try{const r=await fetch('/api/password/set',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({token:tok,password:$('p1').value})});const j=await r.json();
  if(r.ok){$('msg').className='msg ok';$('msg').innerHTML=(j.message||'Password saved.').replace(/&/g,'&amp;').replace(/</g,'&lt;')+
   (j.approved?' <a href="/">Sign in now</a>':'');$('b').style.display='none'}
  else{$('msg').textContent=j.error||'Not saved.';$('b').disabled=false}}
 catch(x){$('msg').textContent='The app is not responding.';$('b').disabled=false}}
</script></body></html>"""


def free_port(start=8765):
    for port in range(start, start + 50):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    return start


def _office_server():
    """The office mode (run_office.bat): the office network reaches the app on this PC.
    Only with sign-in on and an e-mail account to send the password links from."""
    from building import access, office
    need = []
    if not access.enabled():
        need.append("Sign-in is not set up on this PC (admin_access.bat, choice 8).")
    if not office.mail_config():
        need.append("No e-mail account for the app to send password links from (admin_access.bat, choice 9).")
    if need:
        print("The office mode cannot start yet:\n  " + "\n  ".join(need))
        sys.exit(1)
    H.require_login = True
    H.office = True
    try:
        srv = ThreadingHTTPServer(("0.0.0.0", OFFICE_PORT), H)
    except OSError:
        print(f"Port {OFFICE_PORT} is in use - is the office mode already running?")
        sys.exit(1)
    name = socket.gethostname()
    H.office_url = f"http://{name}:{OFFICE_PORT}/"
    try:
        ip = socket.gethostbyname(name)
    except OSError:
        ip = ""
    print("CAD to Revit - office mode. Colleagues on the office network open:")
    print(f"    {H.office_url}" + (f"    (or http://{ip}:{OFFICE_PORT}/)" if ip else ""))
    print(f"On this PC: http://127.0.0.1:{OFFICE_PORT}/   Close this window to stop it.")
    print("If Windows asks whether Python may use the network, allow it for private networks only.")
    threading.Timer(0.8, lambda: webbrowser.open(f"http://127.0.0.1:{OFFICE_PORT}/")).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


def main():
    from building import access
    if "--office" in sys.argv[1:]:
        return _office_server()
    H.require_login = access.enabled()
    port = free_port()
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    url = f"http://127.0.0.1:{port}/"
    print(f"CAD to Revit is running at {url}  (close this window to stop it)")
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
