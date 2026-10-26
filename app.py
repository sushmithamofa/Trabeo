"""CAD to Revit - a job's structural drawings to Revit, in four steps.

  1. Import CAD     add the job's DWG / DXF drawings (files or a whole folder)
  2. Analyse        read them all, link what they say, check it against itself
  3. Review         every value with its source - plan or 3D - decide what is unclear
  4. Export         your decisions into the model, and the files Revit builds from

Start it with run_app.bat, or:  python app.py
Each job is a project folder: its analyses, your decisions and the converted
drawings are kept there. The single-drawing extractor (cad2revit\\run.bat) is
unchanged and still there.
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
import traceback
import webbrowser
from tkinter import filedialog, font, messagebox, ttk

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

INK, PAPER, PANEL, BLUE, OCHRE, MUTE, RED, GREEN = ("#16202B", "#F7F6F2", "#FDFCF9", "#1F6FB2", "#A4500F",
                                                    "#4C5A66", "#B3261E", "#1B7F3B")
SETTINGS = os.path.join(os.path.expanduser("~"), ".c2r_workflow.json")

# the floors a building can have: (key used in the drawings, name shown)
ORD = ["", "First", "Second", "Third", "Fourth", "Fifth", "Sixth", "Seventh", "Eighth", "Ninth", "Tenth",
       "Eleventh", "Twelfth", "Thirteenth", "Fourteenth", "Fifteenth"]
# Basements and the mezzanine are read from drawing names (building.sheets.levels)
# and ordered by the Revit build; they have to be here too, or a basement plan is
# never ticked and cannot be given a level.
FLOORS = ([("FDN", "Foundation"), ("B2", "Basement 2 (lower)"), ("B1", "Basement 1"),
           ("PLINTH", "Plinth"), ("GF", "Ground floor"), ("GF_LINTEL", "Ground floor lintel"),
           ("STILT", "Stilt / podium"), ("MEZZ", "Mezzanine")]
          + [(f"L{n}", f"{n}{'st' if n % 10 == 1 and n != 11 else 'nd' if n % 10 == 2 and n != 12 else 'rd' if n % 10 == 3 and n != 13 else 'th'} floor")
             for n in range(1, 16)]
          + [("TERRACE", "Terrace"), ("ROOF", "Roof"), ("MUMTY", "Mumty / LMR"), ("OHT", "Overhead tank")])


def revit_names() -> list[str]:
    """The Revit level names offered in every dropdown."""
    return ([f"Level {n}" for n in range(0, 16)] + ["Ground Floor"] + [f"{ORD[n]} Floor" for n in range(1, 16)]
            + ["Foundation", "Basement 2", "Basement 1", "Plinth", "Level 0 Lintel",
               "Ground Floor Lintel", "Stilt", "Mezzanine", "Terrace", "Roof", "Mumty", "OHT"]
            + [f"L{n}" for n in range(0, 16)] + ["GF", "FF", "SF", "TF", "TERRACE"])


def name_scheme(key: str, scheme: str) -> str:
    """A level's Revit name in one of the two usual schemes."""
    special = {"FDN": "Foundation", "B2": "Basement 2", "B1": "Basement 1", "PLINTH": "Plinth",
               "STILT": "Stilt", "MEZZ": "Mezzanine", "TERRACE": "Terrace", "ROOF": "Roof",
               "MUMTY": "Mumty", "OHT": "OHT"}
    if key in special:
        return special[key]
    if key.endswith("_LINTEL"):
        # a lintel level inside a storey: named after the storey it is in
        return name_scheme(key[:-len("_LINTEL")], scheme) + " Lintel"
    n = 0 if key == "GF" else int(key[1:])
    if scheme == "level":
        return f"Level {n}"
    return "Ground Floor" if n == 0 else f"{ORD[n]} Floor"


def detect_levels(files) -> list[str]:
    """Floors named in the drawings' file names (1ST FLOOR PLAN -> L1; TERRACE -> TERRACE)."""
    from building.sheets.levels import levels_in
    keys = {k for k, _ in FLOORS}
    out = []
    for f in files:
        for k in levels_in(os.path.splitext(os.path.basename(f))[0]):
            if k in keys and k not in out:
                out.append(k)
    return out


def parse_levels(text: str) -> dict:
    return dict(p.split("=", 1) for p in (text or "").split(",") if "=" in p)


def levels_text(mapping: dict) -> str:
    order = [k for k, _ in FLOORS]
    return ",".join(f"{k}={mapping[k]}" for k in sorted(mapping, key=lambda k: order.index(k) if k in order else 99))


class LevelPicker(tk.Toplevel):
    """Tick the building's floors and pick each one's Revit level name from a list."""

    def __init__(self, master, mapping: dict, detected: list[str], on_ok):
        super().__init__(master)
        self.title("Levels of this building")
        self.configure(bg=PAPER)
        self.transient(master)
        self.on_ok = on_ok
        names = revit_names()
        top = ttk.Frame(self, padding=(14, 10))
        top.pack(fill="x")
        ttk.Label(top, text="Tick every floor this building has, and choose its level name in Revit.",
                  font=master.f_body).pack(anchor="w")
        ttk.Label(top, style="Mute.TLabel",
                  text="Ticked from the drawings' names: " + (", ".join(dict(FLOORS)[k] for k in detected)
                                                             or "nothing recognised - tick them yourself")).pack(anchor="w")
        bar = ttk.Frame(self, padding=(14, 0))
        bar.pack(fill="x")
        ttk.Button(bar, text="Name as Level 0, 1, 2 ...", command=lambda: self._scheme("level")).pack(side="left")
        ttk.Button(bar, text="Name as Ground, First, Second ...", command=lambda: self._scheme("words")).pack(
            side="left", padx=6)
        ttk.Label(bar, text="  Tick up to").pack(side="left")
        self.upto = ttk.Combobox(bar, state="readonly", width=12, values=[n for k, n in FLOORS if k.startswith("L")])
        self.upto.pack(side="left", padx=4)
        self.upto.bind("<<ComboboxSelected>>", lambda e: self._tick_upto())
        ttk.Button(bar, text="Clear", command=self._clear).pack(side="left", padx=6)

        grid = ttk.Frame(self, padding=(14, 8))
        grid.pack(fill="both", expand=True)
        self.rows = {}
        half = (len(FLOORS) + 1) // 2
        for i, (key, label) in enumerate(FLOORS):
            col, r = (0, i) if i < half else (3, i - half)
            on = tk.BooleanVar(value=key in mapping or key in detected)
            name = tk.StringVar(value=mapping.get(key) or name_scheme(key, "level"))
            ttk.Checkbutton(grid, text=label, variable=on, style="Pick.TCheckbutton").grid(
                row=r, column=col, sticky="w", pady=1)
            cb = ttk.Combobox(grid, textvariable=name, values=names, width=16)
            cb.grid(row=r, column=col + 1, sticky="w", padx=(4, 24), pady=1)
            self.rows[key] = (on, name)
        foot = ttk.Frame(self, padding=(14, 4, 14, 12))
        foot.pack(fill="x")
        ttk.Button(foot, text="OK", style="Go.TButton", command=self._ok).pack(side="right")
        ttk.Button(foot, text="Cancel", command=self.destroy).pack(side="right", padx=8)
        self.grab_set()

    def _scheme(self, s):
        for key, (_on, name) in self.rows.items():
            name.set(name_scheme(key, s))

    def _tick_upto(self):
        last = next(k for k, n in FLOORS if n == self.upto.get())
        n = int(last[1:])
        for key, (on, _name) in self.rows.items():
            if key == "GF" or (key.startswith("L") and int(key[1:]) <= n):
                on.set(True)

    def _clear(self):
        for on, _name in self.rows.values():
            on.set(False)

    def _ok(self):
        chosen = {k: name.get().strip() for k, (on, name) in self.rows.items() if on.get() and name.get().strip()}
        if not chosen:
            messagebox.showerror("No floors", "Tick at least one floor.", parent=self)
            return
        self.on_ok(chosen)
        self.destroy()


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("CAD to Revit")
        self.geometry("1080x820")
        self.minsize(900, 680)
        self.configure(bg=PAPER)
        saved = self._load()
        self.files: list[str] = []
        self.project = tk.StringVar(value=saved.get("project", ""))
        self.levels = tk.StringVar(value=saved.get("levels", ""))
        self.suggested = tk.BooleanVar(value=saved.get("suggested", True))
        self.q: queue.Queue = queue.Queue()
        self.run_dir = None
        self.running = False
        self._fonts()
        self._style()
        self._build()
        self._refresh()
        self.after(80, self._drain)

    # -- settings remembered between sessions
    def _load(self) -> dict:
        try:
            with open(SETTINGS, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save(self) -> None:
        try:
            with open(SETTINGS, "w", encoding="utf-8") as f:
                json.dump({"project": self.project.get(), "levels": self.levels.get(),
                           "suggested": self.suggested.get()}, f)
        except Exception:
            pass

    # -- look
    def _fonts(self) -> None:
        fam = "Segoe UI" if "Segoe UI" in font.families() else "Helvetica"
        mono = "Consolas" if "Consolas" in font.families() else "Courier"
        self.f_h1, self.f_h2, self.f_body, self.f_small = (fam, 17, "bold"), (fam, 11, "bold"), (fam, 10), (fam, 9)
        self.f_mono, self.f_num = (mono, 9), (fam, 18, "bold")

    def _style(self) -> None:
        s = ttk.Style(self)
        try:
            s.theme_use("clam")
        except tk.TclError:
            pass
        s.configure("TFrame", background=PAPER)
        s.configure("Step.TFrame", background=PANEL, relief="solid", borderwidth=1)
        s.configure("Card.TFrame", background=PANEL, relief="solid", borderwidth=1)
        s.configure("TLabel", background=PAPER, foreground=INK, font=self.f_body)
        s.configure("Step.TLabel", background=PANEL, foreground=INK, font=self.f_body)
        s.configure("StepH.TLabel", background=PANEL, foreground=INK, font=self.f_h2)
        s.configure("StepMute.TLabel", background=PANEL, foreground=MUTE, font=self.f_small)
        s.configure("Mute.TLabel", background=PAPER, foreground=MUTE, font=self.f_small)
        s.configure("CardNum.TLabel", background=PANEL, foreground=INK, font=self.f_num)
        s.configure("CardLbl.TLabel", background=PANEL, foreground=MUTE, font=self.f_small)
        s.configure("TButton", font=self.f_body, padding=(10, 5))
        s.configure("Go.TButton", font=(self.f_body[0], 10, "bold"), padding=(14, 6))
        s.configure("TCheckbutton", background=PANEL, font=self.f_body)
        s.configure("Pick.TCheckbutton", background=PAPER, font=self.f_body)

    # -- layout: four steps across the top, the log below
    def _build(self) -> None:
        head = ttk.Frame(self)
        head.pack(fill="x", padx=18, pady=(14, 6))
        ttk.Label(head, text="CAD to Revit", font=self.f_h1).pack(side="left")
        ttk.Label(head, style="Mute.TLabel", text="   any consultant's structural drawings  >  review  >  Revit"
                  ).pack(side="left", pady=(6, 0))

        proj = ttk.Frame(self)
        proj.pack(fill="x", padx=18, pady=(0, 8))
        ttk.Label(proj, text="Project folder").pack(side="left")
        ttk.Entry(proj, textvariable=self.project, font=self.f_body).pack(side="left", fill="x", expand=True, padx=8)
        ttk.Button(proj, text="Choose...", command=self._pick_project).pack(side="left")
        ttk.Label(proj, style="Mute.TLabel", text="  one per job: analyses, decisions, converted drawings").pack(
            side="left")

        steps = ttk.Frame(self)
        steps.pack(fill="x", padx=18)
        for i in range(4):
            steps.columnconfigure(i, weight=1, uniform="s")

        # 1 Import CAD
        s1 = self._step(steps, 0, "1  Import CAD", "the job's DWG / DXF drawings - any consultant")
        row = ttk.Frame(s1, style="Step.TFrame")
        row.pack(fill="x", pady=(4, 4))
        ttk.Button(row, text="Add drawings...", command=self._add_files).pack(side="left")
        ttk.Button(row, text="Add folder...", command=self._add_folder).pack(side="left", padx=6)
        self.lst = tk.Listbox(s1, height=7, font=self.f_small, relief="solid", bd=1, activestyle="none",
                              selectmode="extended")
        self.lst.pack(fill="both", expand=True)
        row = ttk.Frame(s1, style="Step.TFrame")
        row.pack(fill="x", pady=(4, 0))
        ttk.Button(row, text="Remove", command=self._remove).pack(side="left")
        ttk.Button(row, text="Clear", command=self._clear).pack(side="left", padx=6)
        self.n_files = ttk.Label(row, style="StepMute.TLabel", text="")
        self.n_files.pack(side="left", padx=6)

        # 2 Analyse
        s2 = self._step(steps, 1, "2  Analyse", "read every drawing, link, check")
        ttk.Label(s2, style="Step.TLabel", text="Levels").pack(anchor="w", pady=(4, 0))
        self.lv_lbl = ttk.Label(s2, style="StepMute.TLabel", text="", wraplength=230)
        self.lv_lbl.pack(anchor="w")
        ttk.Button(s2, text="Choose levels...", command=self._pick_levels).pack(anchor="w", pady=(2, 0))
        ttk.Checkbutton(s2, text="Use suggested layer roles for\nlayers not known yet",
                        variable=self.suggested).pack(anchor="w", pady=(6, 6))
        self.b_analyse = ttk.Button(s2, text="Analyse drawings", style="Go.TButton", command=self._analyse)
        self.b_analyse.pack(anchor="w")
        self.prog = ttk.Progressbar(s2, mode="indeterminate")
        self.prog.pack(fill="x", pady=(8, 0))

        # 3 Review
        s3 = self._step(steps, 2, "3  Review", "every value with its source - decide the unclear")
        self.b_review = ttk.Button(s3, text="Review: plan and 3D", style="Go.TButton", command=self._open_review)
        self.b_review.pack(anchor="w", pady=(4, 6))
        self.b_xlsx = ttk.Button(s3, text="Review workbook (Excel)", command=self._open_xlsx)
        self.b_xlsx.pack(anchor="w", pady=2)
        self.b_sum = ttk.Button(s3, text="Summary: what to decide", command=self._open_summary)
        self.b_sum.pack(anchor="w", pady=2)
        ttk.Label(s3, style="StepMute.TLabel", wraplength=230,
                  text="In the review, decide values, then press 'Export overrides.json' at its bottom.").pack(
            anchor="w", pady=(6, 0))

        # 4 Export to Revit
        s4 = self._step(steps, 3, "4  Export to Revit", "your decisions in, the Revit files out")
        self.b_dec = ttk.Button(s4, text="Load decisions (overrides.json)...", command=self._load_decisions)
        self.b_dec.pack(anchor="w", pady=(4, 2))
        self.dec_lbl = ttk.Label(s4, style="StepMute.TLabel", text="no decisions loaded", wraplength=230)
        self.dec_lbl.pack(anchor="w")
        self.b_export = ttk.Button(s4, text="Export to Revit", style="Go.TButton", command=self._export)
        self.b_export.pack(anchor="w", pady=(8, 4))
        self.b_revit = ttk.Button(s4, text="Open Revit files", command=self._open_revit)
        self.b_revit.pack(anchor="w", pady=2)
        ttk.Label(s4, style="StepMute.TLabel", wraplength=230,
                  text="In Revit: revit\\build_model.py on each plan's model.json, then revit\\build_extras.py "
                       "on foundations.json.").pack(anchor="w", pady=(6, 0))

        self.cards = ttk.Frame(self)
        self.cards.pack(fill="x", padx=18, pady=(10, 0))
        ttk.Label(self, text="Log", style="Mute.TLabel").pack(anchor="w", padx=18, pady=(8, 2))
        wrap = ttk.Frame(self)
        wrap.pack(fill="both", expand=True, padx=18, pady=(0, 16))
        self.log = tk.Text(wrap, bg="white", fg=INK, font=self.f_mono, wrap="word", relief="solid", bd=1,
                           padx=10, pady=8, height=10)
        sb = ttk.Scrollbar(wrap, command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.log.pack(side="left", fill="both", expand=True)
        for tag, col in (("err", RED), ("ok", GREEN), ("warn", OCHRE), ("hd", BLUE)):
            self.log.tag_configure(tag, foreground=col)
        self.log.configure(state="disabled")
        self._say("1. Import the job's drawings.  2. Analyse.  3. Review.  4. Export to Revit.\n", "hd")
        last = self._latest_run()
        if last:
            self.run_dir = last
            self._say(f"Last analysis of this project: {last}\n")

    def _step(self, parent, col, title, sub):
        f = ttk.Frame(parent, style="Step.TFrame", padding=(12, 10))
        f.grid(row=0, column=col, sticky="nsew", padx=(0 if col == 0 else 8, 0))
        ttk.Label(f, text=title, style="StepH.TLabel").pack(anchor="w")
        ttk.Label(f, text=sub, style="StepMute.TLabel", wraplength=230).pack(anchor="w")
        return f

    # -- state of the buttons: each step opens when the one before it is done
    def _refresh(self) -> None:
        busy = self.running
        has = bool(self.files)
        done = bool(self.run_dir and os.path.exists(os.path.join(self.run_dir, "review.html")))
        self.b_analyse.configure(state="normal" if has and not busy else "disabled")
        for b in (self.b_review, self.b_xlsx, self.b_sum, self.b_dec, self.b_export):
            b.configure(state="normal" if done and not busy else "disabled")
        built = bool(self.run_dir and os.path.isdir(os.path.join(self.run_dir, "revit_build")))
        self.b_revit.configure(state="normal" if built and not busy else "disabled")
        self.n_files.configure(text=f"{len(self.files)} drawing(s)")
        m = parse_levels(self.levels.get())
        label = dict(FLOORS)
        self.lv_lbl.configure(text=(f"{len(m)} level(s): " + ", ".join(f"{label.get(k, k)} = {v}" for k, v in m.items()))
                              if m else "none chosen - press Choose levels")
        keep = os.path.join(self.project.get(), "overrides.json") if self.project.get() else ""
        if keep and os.path.exists(keep):
            try:
                with open(keep, encoding="utf-8") as f:
                    d = json.load(f)
                n = len(d.get("elements", {})) + len(d.get("marks", {})) + len(d.get("exclude", []))
                self.dec_lbl.configure(text=f"{n} decision(s) in the project - applied on export")
            except Exception:
                pass

    def _latest_run(self):
        p = self.project.get()
        runs = os.path.join(p, "runs") if p else ""
        if runs and os.path.isdir(runs):
            done = [os.path.join(runs, d) for d in sorted(os.listdir(runs))
                    if os.path.exists(os.path.join(runs, d, "review.html"))]
            return done[-1] if done else None
        return None

    # -- 1 import
    def _add(self, paths) -> None:
        from building.sheets.__main__ import drawings
        for p in drawings(list(paths)):
            if p.lower().endswith((".dwg", ".dxf")) and p not in self.files:
                self.files.append(p)
                self.lst.insert("end", os.path.basename(p))
        if self.files and not self.project.get():
            first = os.path.dirname(self.files[0])
            self.project.set(os.path.join(os.path.dirname(first), os.path.basename(first) + "_c2r"))
        # the floors the drawings' names mention go into the level list straight away
        m = parse_levels(self.levels.get())
        for k in detect_levels(self.files):
            m.setdefault(k, name_scheme(k, "level"))
        self.levels.set(levels_text(m))
        self._refresh()

    def _add_files(self) -> None:
        ps = filedialog.askopenfilenames(title="Import the job's structural drawings",
                                         filetypes=[("CAD drawings", "*.dwg *.dxf"), ("All files", "*.*")])
        if ps:
            self._add(ps)

    def _add_folder(self) -> None:
        p = filedialog.askdirectory(title="Import every drawing in a folder")
        if p:
            self._add([p])

    def _remove(self) -> None:
        for i in reversed(self.lst.curselection()):
            self.lst.delete(i)
            del self.files[i]
        self._refresh()

    def _clear(self) -> None:
        self.lst.delete(0, "end")
        self.files.clear()
        self._refresh()

    def _pick_project(self) -> None:
        p = filedialog.askdirectory(title="Project folder for this job")
        if p:
            self.project.set(p)
            self.run_dir = self._latest_run()
            self._refresh()

    # -- 2 analyse
    def _pick_levels(self) -> None:
        def ok(chosen):
            self.levels.set(levels_text(chosen))
            self._save()
            self._refresh()
        LevelPicker(self, parse_levels(self.levels.get()), detect_levels(self.files), ok)

    def _analyse(self) -> None:
        if self.running or not self.files:
            return
        if not self.project.get().strip():
            messagebox.showerror("Project folder", "Choose a project folder for this job first.")
            return
        self._save()
        self._start("Analysing ...")
        threading.Thread(target=self._work_analyse, args=(list(self.files), self.project.get().strip(),
                                                          self.levels.get(), self.suggested.get()), daemon=True).start()

    def _work_analyse(self, files, project, levels, suggested) -> None:
        try:
            from building.workflow import run
            run_dir = run(files, project, levels, suggested, log=lambda m: self.q.put(("log", m)))
            self.q.put(("analysed", run_dir))
        except Exception as exc:
            self.q.put(("fail", (str(exc), traceback.format_exc())))

    # -- 4 export
    def _load_decisions(self) -> None:
        p = filedialog.askopenfilename(title="The decisions exported from the review (overrides.json)",
                                       filetypes=[("Review decisions", "*.json")],
                                       initialdir=os.path.join(os.path.expanduser("~"), "Downloads"))
        if p:
            import shutil
            keep = os.path.join(self.project.get(), "overrides.json")
            os.makedirs(self.project.get(), exist_ok=True)
            shutil.copy2(p, keep)
            self._say(f"Decisions loaded into the project: {keep}\n", "ok")
            self._refresh()

    def _export(self) -> None:
        if self.running or not self.run_dir:
            return
        self._start("Exporting ...")
        threading.Thread(target=self._work_export, args=(self.run_dir,), daemon=True).start()

    def _work_export(self, run_dir) -> None:
        try:
            from building.workflow import apply_decisions
            out = apply_decisions(run_dir, None, log=lambda m: self.q.put(("log", m)))
            self.q.put(("exported", out))
        except Exception as exc:
            self.q.put(("fail", (str(exc), traceback.format_exc())))

    # -- running
    def _start(self, what) -> None:
        self.running = True
        self.prog.start(12)
        self._say(f"\n{what}\n", "hd")
        self._refresh()

    def _say(self, text, tag=None) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text, tag or ())
        self.log.see("end")
        self.log.configure(state="disabled")

    def _drain(self) -> None:
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self._say(str(payload) + "\n")
                elif kind == "analysed":
                    self._done()
                    self.run_dir = payload
                    self._cards()
                    self._say("\nAnalysis done. Step 3: open the review.\n", "ok")
                elif kind == "exported":
                    self._done()
                    self._cards()
                    self._say(f"\nRevit files written: {payload}\n", "ok")
                elif kind == "fail":
                    self._done()
                    self._say("\nStopped: " + payload[0] + "\n", "err")
                    self._say(payload[1] + "\n")
                    messagebox.showerror("Stopped", payload[0])
                self._refresh()
        except queue.Empty:
            pass
        self.after(80, self._drain)

    def _done(self) -> None:
        self.running = False
        self.prog.stop()

    def _cards(self) -> None:
        for w in self.cards.winfo_children():
            w.destroy()
        try:
            with open(os.path.join(self.run_dir, "summary.json"), encoding="utf-8") as f:
                s = json.load(f)
        except Exception:
            return
        cards = [(str(s.get("plans", 0)), "plans")] + [(str(v), k + "s") for k, v in s.get("elements", {}).items()
                                                       if k in ("column", "beam", "slab", "wall", "footing", "stair")]
        vals = s.get("values", {})
        cards += [(str(vals.get("conflict", 0)), "conflicts"), (str(vals.get("missing", 0)), "missing")]
        for i, (n, lbl) in enumerate(cards):
            c = ttk.Frame(self.cards, style="Card.TFrame", padding=(12, 8))
            c.grid(row=0, column=i, padx=(0, 8), sticky="w")
            col = RED if lbl == "conflicts" and n != "0" else OCHRE if lbl == "missing" and n != "0" else INK
            ttk.Label(c, text=n, style="CardNum.TLabel", foreground=col).pack(anchor="w")
            ttk.Label(c, text=lbl.upper(), style="CardLbl.TLabel").pack(anchor="w")
        for i, step in enumerate(s.get("next", []), start=1):
            self._say(f"  {i}. {step}\n")

    # -- opening what was written
    def _open(self, *parts) -> None:
        if not self.run_dir:
            return
        p = os.path.join(self.run_dir, *parts)
        if os.path.isdir(p):
            subprocess.Popen(["explorer", os.path.normpath(p)])
        elif os.path.exists(p):
            if p.endswith(".html"):
                webbrowser.open("file:///" + p.replace("\\", "/"))
            else:
                os.startfile(p)
        else:
            messagebox.showinfo("Not there yet", f"{os.path.basename(p)} has not been written yet.")

    def _open_review(self):
        self._open("review.html")

    def _open_xlsx(self):
        self._open("review.xlsx")

    def _open_summary(self):
        self._open("SUMMARY.txt")

    def _open_revit(self):
        self._open("revit_build")


if __name__ == "__main__":
    App().mainloop()
