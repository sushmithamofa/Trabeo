"""CAD to Revit extractor - desktop front end.

Pick a structural DWG (or DXF), press Run, and the tool writes a cleaned DXF plus
the data files needed to build the Revit model, and a report of anything it could
not resolve.

Start it with run.bat, or:  python app.py
"""
from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
import traceback
import webbrowser
from tkinter import filedialog, font, messagebox, ttk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from core import inspect_layers, pipeline                    # noqa: E402
from core.model import BLOCKER, INFO, WARNING                # noqa: E402

INK = "#16202B"
PAPER = "#F7F6F2"
PANEL = "#FDFCF9"
LINE = "#DCD9D0"
BLUE = "#1F6FB2"
OCHRE = "#A4500F"
MUTE = "#4C5A66"
RED = "#B3261E"


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("CAD to Revit  -  structural extractor")
        self.geometry("980x720")
        self.minsize(820, 600)
        self.configure(bg=PAPER)

        self.src = tk.StringVar()
        self.out = tk.StringVar()
        self.level = tk.StringVar()
        self.cfg = tk.StringVar(value=pipeline.DEFAULT_CONFIG)
        self.q: queue.Queue[tuple] = queue.Queue()
        self.result = None
        self.out_dir = None
        self.running = False

        self._fonts()
        self._style()
        self._build()
        self.after(80, self._drain)

    # -- look ------------------------------------------------------------
    def _fonts(self) -> None:
        fam = "Segoe UI" if "Segoe UI" in font.families() else "Helvetica"
        mono = "Consolas" if "Consolas" in font.families() else "Courier"
        self.f_h1 = (fam, 17, "bold")
        self.f_lbl = (fam, 10)
        self.f_body = (fam, 10)
        self.f_small = (fam, 9)
        self.f_mono = (mono, 9)
        self.f_num = (fam, 20, "bold")

    def _style(self) -> None:
        s = ttk.Style(self)
        try:
            s.theme_use("clam")
        except tk.TclError:
            pass
        s.configure("TFrame", background=PAPER)
        s.configure("Card.TFrame", background=PANEL, relief="solid", borderwidth=1)
        s.configure("TLabel", background=PAPER, foreground=INK, font=self.f_body)
        s.configure("Mute.TLabel", background=PAPER, foreground=MUTE, font=self.f_small)
        s.configure("CardNum.TLabel", background=PANEL, foreground=INK, font=self.f_num)
        s.configure("CardLbl.TLabel", background=PANEL, foreground=MUTE, font=self.f_small)
        s.configure("TButton", font=self.f_body, padding=(12, 6))
        s.configure("Go.TButton", font=(self.f_body[0], 11, "bold"), padding=(20, 9))
        s.configure("TEntry", fieldbackground="white")

    # -- layout ----------------------------------------------------------
    def _build(self) -> None:
        pad = {"padx": 18}

        head = ttk.Frame(self)
        head.pack(fill="x", pady=(16, 4), **pad)
        ttk.Label(head, text="CAD to Revit", font=self.f_h1).pack(anchor="w")
        ttk.Label(head, style="Mute.TLabel",
                  text="Cleans the layers of a structural drawing and extracts the "
                       "geometry and marks needed to build the model.").pack(anchor="w")

        box = ttk.Frame(self)
        box.pack(fill="x", pady=(14, 6), **pad)
        box.columnconfigure(1, weight=1)

        self._row(box, 0, "Drawing", self.src, self._pick_src, "Browse...")
        self._level_row(box, 1)
        self._row(box, 2, "Output folder", self.out, self._pick_out, "Browse...",
                  hint="leave blank to write beside the drawing")
        self._row(box, 3, "Layer rules", self.cfg, self._edit_cfg, "Edit...")
        self.src.trace_add("write", lambda *_: self._guess_level())

        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(8, 10), **pad)
        self.go = ttk.Button(bar, text="Run", style="Go.TButton", command=self._run)
        self.go.pack(side="left")
        self.b_inspect = ttk.Button(bar, text="Inspect layers", command=self._inspect)
        self.b_inspect.pack(side="left", padx=(10, 0))
        self.prog = ttk.Progressbar(bar, mode="indeterminate", length=180)
        self.prog.pack(side="left", padx=14)
        self.b_report = ttk.Button(bar, text="Open report", command=self._open_report,
                                   state="disabled")
        self.b_report.pack(side="right")
        self.b_folder = ttk.Button(bar, text="Open folder", command=self._open_folder,
                                   state="disabled")
        self.b_folder.pack(side="right", padx=8)

        self.cards = ttk.Frame(self)
        self.cards.pack(fill="x", **pad)

        ttk.Label(self, text="Log", style="Mute.TLabel").pack(anchor="w", pady=(12, 2), **pad)
        wrap = ttk.Frame(self)
        wrap.pack(fill="both", expand=True, pady=(0, 18), **pad)
        self.log = tk.Text(wrap, bg="white", fg=INK, font=self.f_mono, wrap="word",
                           relief="solid", bd=1, padx=10, pady=8, height=14)
        sb = ttk.Scrollbar(wrap, command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.log.pack(side="left", fill="both", expand=True)
        for tag, col in (("err", RED), ("ok", "#1B7F3B"), ("warn", OCHRE), ("hd", BLUE)):
            self.log.tag_configure(tag, foreground=col)
        self.log.configure(state="disabled")

        self._say("Choose a structural DWG or DXF and press Run.\n")

    def _row(self, parent, r, label, var, cmd, btn, hint=""):
        ttk.Label(parent, text=label).grid(row=r, column=0, sticky="w", pady=5)
        e = ttk.Entry(parent, textvariable=var, font=self.f_body)
        e.grid(row=r, column=1, sticky="ew", padx=10, pady=5)
        ttk.Button(parent, text=btn, command=cmd).grid(row=r, column=2, pady=5)
        if hint:
            ttk.Label(parent, text=hint, style="Mute.TLabel").grid(
                row=r, column=3, sticky="w", padx=(10, 0))

    LEVEL_CHOICES = ("Level 0", "Level 1", "Level 2", "Level 3", "Level 4", "Level 5",
                     "Ground Floor", "First Floor", "Second Floor", "Third Floor",
                     "Terrace", "Roof")

    def _level_row(self, parent, r):
        """The Revit level this floor is built on, typed exactly as Revit names it.

        Each floor is its own drawing and its own extraction; this is what puts
        them on different levels of the one model.
        """
        ttk.Label(parent, text="Revit level").grid(row=r, column=0, sticky="w", pady=5)
        row = ttk.Frame(parent)
        row.grid(row=r, column=1, columnspan=3, sticky="w", padx=10, pady=5)
        ttk.Combobox(row, textvariable=self.level, values=self.LEVEL_CHOICES,
                     font=self.f_body, width=22).pack(side="left")
        ttk.Label(row, style="Mute.TLabel",
                  text="spelt exactly as in Revit. This floor's beams go on it; "
                       "its columns rise from the level below.").pack(side="left", padx=(10, 0))

    def _guess_level(self) -> None:
        """Fill the level from the drawing's name whenever the drawing changes.

        Always, not only when the box is empty: a level kept over from the last
        drawing would put the next floor on the wrong level without a word. The
        user corrects it after choosing the drawing, and that is what is used.
        """
        self.level.set(pipeline.guess_level(self.src.get().strip('" ')))

    # -- actions ---------------------------------------------------------
    def _pick_src(self) -> None:
        p = filedialog.askopenfilename(
            title="Choose the structural drawing",
            filetypes=[("CAD drawings", "*.dwg *.dxf"), ("DWG", "*.dwg"),
                       ("DXF", "*.dxf"), ("All files", "*.*")])
        if p:
            self.src.set(p)

    def _pick_out(self) -> None:
        p = filedialog.askdirectory(title="Where should the output go?")
        if p:
            self.out.set(p)

    def _edit_cfg(self) -> None:
        p = self.cfg.get()
        if os.path.exists(p):
            os.startfile(p)              # opens in whatever edits .json
        else:
            messagebox.showerror("Not found", f"No config file at\n{p}")

    def _say(self, text, tag=None) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text, tag or ())
        self.log.see("end")
        self.log.configure(state="disabled")

    def _run(self) -> None:
        if self.running:
            return
        src = self.src.get().strip('" ')
        if not src or not os.path.exists(src):
            messagebox.showerror("No drawing", "Choose a DWG or DXF file first.")
            return

        self.running = True
        self.go.configure(state="disabled", text="Working...")
        self.b_report.configure(state="disabled")
        self.b_folder.configure(state="disabled")
        for w in self.cards.winfo_children():
            w.destroy()
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        self.prog.start(12)

        out = self.out.get().strip() or None
        cfg = self.cfg.get().strip() or None
        lvl = self.level.get().strip()
        threading.Thread(target=self._work, args=(src, out, cfg, lvl), daemon=True).start()

    def _inspect(self) -> None:
        """List the layers and what the tool makes of them. Extracts nothing.

        This is the first step on a drawing whose layer names are new."""
        if self.running:
            return
        src = self.src.get().strip('" ')
        if not src or not os.path.exists(src):
            messagebox.showerror("No drawing", "Choose a DWG or DXF file first.")
            return
        self.running = True
        self.b_inspect.configure(state="disabled")
        self.go.configure(state="disabled")
        self.prog.start(12)
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        threading.Thread(target=self._inspect_work,
                         args=(src, self.cfg.get().strip() or None), daemon=True).start()

    def _inspect_work(self, src, cfg_path) -> None:
        import shutil
        import tempfile
        from core import dwgconv
        tmp = tempfile.mkdtemp(prefix="c2r_ins_")
        try:
            cfg = pipeline.load_config(cfg_path)
            dxf = dwgconv.to_dxf(src, os.path.join(tmp, "inspect.dxf"),
                                 log=lambda m: self.q.put(("log", m)))
            rows = inspect_layers.inspect(dxf, cfg)
            self.q.put(("inspect", inspect_layers.format_table(rows, src)))
        except Exception as exc:
            self.q.put(("fail", (str(exc), traceback.format_exc())))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _work(self, src, out, cfg, level="") -> None:
        try:
            res, out_dir, files = pipeline.run(
                src, out_dir=out, config_path=cfg,
                log=lambda m: self.q.put(("log", m)), level_name=level)
            self.q.put(("done", (res, out_dir)))
        except Exception as exc:
            self.q.put(("fail", (str(exc), traceback.format_exc())))

    def _drain(self) -> None:
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self._say(str(payload) + "\n")
                elif kind == "done":
                    self._finish(*payload)
                elif kind == "inspect":
                    self.running = False
                    self.prog.stop()
                    self.b_inspect.configure(state="normal")
                    self.go.configure(state="normal")
                    self._say(str(payload) + "\n")
                    self._say("\nEdit the layer rules with the Edit button above, "
                              "then press Run.\n", "hd")
                elif kind == "fail":
                    self._fail(*payload)
        except queue.Empty:
            pass
        self.after(80, self._drain)

    def _finish(self, res, out_dir) -> None:
        self.running = False
        self.prog.stop()
        self.go.configure(state="normal", text="Run")
        self.result, self.out_dir = res, out_dir
        self.b_report.configure(state="normal")
        self.b_folder.configure(state="normal")

        ic = res.issue_counts()
        cards = [(str(v), k) for k, v in res.counts().items() if v]
        cards += [(str(ic.get(BLOCKER, 0)), "blockers"),
                  (str(ic.get(WARNING, 0)), "warnings")]
        for i, (n, lbl) in enumerate(cards):
            c = ttk.Frame(self.cards, style="Card.TFrame", padding=(14, 10))
            c.grid(row=0, column=i, padx=(0, 10), sticky="w")
            col = RED if lbl == "blockers" and n != "0" else (
                OCHRE if lbl == "warnings" and n != "0" else INK)
            ttk.Label(c, text=n, style="CardNum.TLabel", foreground=col).pack(anchor="w")
            ttk.Label(c, text=lbl.upper(), style="CardLbl.TLabel").pack(anchor="w")

        nb = ic.get(BLOCKER, 0)
        self._say("\n")
        if nb:
            self._say(f"{nb} blockers. Those elements cannot be modelled until the "
                      "drawing changes. Open the report for the list.\n", "err")
        else:
            self._say("No blockers. Everything resolved.\n", "ok")
        self._say(f"Files are in {out_dir}\n", "hd")

    def _fail(self, msg, tb) -> None:
        self.running = False
        self.prog.stop()
        self.go.configure(state="normal", text="Run")
        self._say("\nFailed: " + msg + "\n", "err")
        self._say(tb + "\n")
        messagebox.showerror("Could not read the drawing", msg)

    def _open_report(self) -> None:
        if self.out_dir:
            webbrowser.open("file:///" + os.path.join(self.out_dir, "report.html")
                            .replace("\\", "/"))

    def _open_folder(self) -> None:
        if self.out_dir:
            subprocess.Popen(["explorer", os.path.normpath(self.out_dir)])


if __name__ == "__main__":
    App().mainloop()
