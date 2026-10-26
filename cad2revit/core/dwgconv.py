"""Turn a DWG into a DXF so it can be read.

A DWG cannot be parsed directly. This module finds a converter already installed
on the machine and drives it in batch mode. The source DWG is copied first and
the copy is converted, so the consultant's file is never touched.

Order of preference:
  1. ODA File Converter, if installed - fastest and needs no CAD licence.
  2. ZWCAD, driven by a script file.
  3. BricsCAD or AutoCAD accoreconsole, same idea.
"""
from __future__ import annotations

import glob
import os
import shutil
import subprocess
import tempfile
import threading

ODA_GLOBS = [
    r"C:\Program Files\ODA\*\ODAFileConverter.exe",
    r"C:\Program Files\ODA\ODAFileConverter*\ODAFileConverter.exe",
]
ZWCAD_GLOBS = [
    r"C:\Program Files\ZWSOFT\ZWCAD*\ZWCAD.exe",
    r"C:\Program Files (x86)\ZWSOFT\ZWCAD*\ZWCAD.exe",
]
ACAD_GLOBS = [
    r"C:\Program Files\Autodesk\AutoCAD *\accoreconsole.exe",
    r"C:\Program Files\Bricsys\BricsCAD*\bricscad.exe",
]


def _first(patterns: list[str]) -> str | None:
    for pat in patterns:
        hits = sorted(glob.glob(pat))
        if hits:
            return hits[-1]
    return None


def available_converters() -> dict[str, str | None]:
    return {
        "ODA File Converter": _first(ODA_GLOBS),
        "ZWCAD": _first(ZWCAD_GLOBS),
        "AutoCAD / BricsCAD": _first(ACAD_GLOBS),
    }


def _convert_with_oda(exe: str, src: str, out_dxf: str, log) -> bool:
    in_dir = tempfile.mkdtemp(prefix="c2r_in_")
    out_dir = tempfile.mkdtemp(prefix="c2r_out_")
    try:
        shutil.copy2(src, os.path.join(in_dir, "drawing.dwg"))
        # in_dir out_dir version type recurse audit filter
        cmd = [exe, in_dir, out_dir, "ACAD2018", "DXF", "0", "1"]
        log(f"    {os.path.basename(exe)} ...")
        try:
            subprocess.run(cmd, capture_output=True, timeout=600)
        except OSError as err:
            log(f"    could not start {os.path.basename(exe)}: {err}")
            return False
        made = glob.glob(os.path.join(out_dir, "*.dxf"))
        if made:
            shutil.move(made[0], out_dxf)
            return True
        return False
    finally:
        shutil.rmtree(in_dir, ignore_errors=True)
        shutil.rmtree(out_dir, ignore_errors=True)


# Messages the CAD program can stop on while opening a drawing in batch mode, and the
# answer that changes nothing in the drawing or in the person's CAD settings. Driven from
# here nobody sees the message, and the conversion would wait for it until its time-out.
# Title -> button, both lower case.
#   "Too Many Hatch Lines" (ZWCAD 2024, Alcazar 1151-ALL FLOOR): dense hatches shown as
#   solid on screen only - the hatch itself is exported as drawn
_PROMPTS = {
    "too many hatch lines": "keep degenerated display",
}
_UNKNOWN_AFTER_S = 30.0


def _answer_prompts(pid: int, stop, log) -> None:
    """While the CAD program runs: press the harmless answer of a known message
    (_PROMPTS); say which message it waits on when it is one of no known answer."""
    if os.name != "nt":
        return
    try:
        import ctypes
        import time
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    except Exception:
        return

    def text(h):
        n = user32.GetWindowTextLengthW(h)
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(h, buf, n + 1)
        return buf.value

    seen_at, told = {}, set()
    while not stop.wait(1.0):
        try:
            tops = []

            def on_top(h, _l):
                p = wintypes.DWORD()
                user32.GetWindowThreadProcessId(h, ctypes.byref(p))
                if p.value == pid and user32.IsWindowVisible(h):
                    tops.append(h)
                return True
            user32.EnumWindows(enum_proc(on_top), 0)
            for h in tops:
                title = text(h).strip()
                answer = _PROMPTS.get(title.lower())
                if answer:
                    kids = []

                    def on_kid(c, _l):
                        kids.append(c)
                        return True
                    user32.EnumChildWindows(h, enum_proc(on_kid), 0)
                    for c in kids:
                        if text(c).strip().lower() == answer:
                            user32.SendMessageW(c, 0x00F5, 0, 0)         # BM_CLICK
                            log(f"    CAD asked '{title}': answered '{text(c).strip()}' (the drawing is unchanged)")
                            break
                elif title and not title.upper().startswith(("ZWCAD", "BRICSCAD")) and title not in told:
                    # a message with no known answer: say so instead of waiting in silence
                    seen_at.setdefault(title, time.time())
                    if time.time() - seen_at[title] >= _UNKNOWN_AFTER_S:
                        told.add(title)
                        log(f"    CAD is waiting on a message '{title}' - answer it in the CAD window, "
                            "or the conversion gives up after 15 minutes")
        except Exception:
            continue


def _convert_with_zwcad(exe: str, src: str, out_dxf: str, log) -> bool:
    # A space in a CAD script file is the same keystroke as Enter, so every path
    # written into the script has to be space-free. The work folder is a temp
    # folder and the names below are fixed, which guarantees that.
    work = tempfile.mkdtemp(prefix="c2r_zw_")
    if " " in work:
        log("    Temp folder path contains a space; falling back to C:\\c2r_tmp")
        work = os.path.join("C:\\", "c2r_tmp")
        os.makedirs(work, exist_ok=True)
    try:
        copy = os.path.join(work, "drawing.dwg")
        shutil.copy2(src, copy)
        staged = os.path.join(work, "converted.dxf")
        scr = os.path.join(work, "convert.scr")
        with open(scr, "w", encoding="ascii", errors="replace") as f:
            f.write("FILEDIA\n0\n_DXFOUT\n" + staged.replace("\\", "/") + "\n16\n_QUIT\n_Y\n")
        log(f"    {os.path.basename(exe)} (this opens CAD briefly) ...")
        # A "run as administrator" setting on the CAD program makes Windows refuse
        # to start it from here; exporting a DXF needs no admin rights, so ask
        # Windows to start it as the current user.
        env = dict(os.environ, __COMPAT_LAYER="RunAsInvoker")
        try:
            proc = subprocess.Popen([exe, copy, "/b", scr], stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, env=env)
        except OSError as err:
            log(f"    could not start {os.path.basename(exe)}: {err}")
            return False
        stop = threading.Event()
        watcher = threading.Thread(target=_answer_prompts, args=(proc.pid, stop, log), daemon=True)
        watcher.start()
        try:
            proc.wait(timeout=900)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            raise
        finally:
            stop.set()
        if os.path.exists(staged) and os.path.getsize(staged) > 1024:
            os.makedirs(os.path.dirname(out_dxf), exist_ok=True)
            shutil.move(staged, out_dxf)
            return True
        return False
    finally:
        shutil.rmtree(work, ignore_errors=True)


def to_dxf(path: str, out_dxf: str, log=print) -> str:
    """Return a path to a DXF for `path`, converting only if it is a DWG."""
    if path.lower().endswith(".dxf"):
        return path
    if not path.lower().endswith(".dwg"):
        raise ValueError(f"Expected a .dwg or .dxf file, got {os.path.basename(path)}")

    log("  Converting DWG to DXF (the original file is not modified)")
    found = available_converters()

    exe = found["ODA File Converter"]
    if exe and _convert_with_oda(exe, path, out_dxf, log):
        log("  Converted with ODA File Converter")
        return out_dxf

    exe = found["ZWCAD"]
    if exe and _convert_with_zwcad(exe, path, out_dxf, log):
        log("  Converted with ZWCAD")
        return out_dxf

    exe = found["AutoCAD / BricsCAD"]
    if exe and exe.lower().endswith("bricscad.exe") and _convert_with_zwcad(exe, path, out_dxf, log):
        log("  Converted with BricsCAD")
        return out_dxf

    raise RuntimeError(
        "No DWG converter found on this machine.\n\n"
        "Install the free ODA File Converter from opendesign.com, or keep ZWCAD "
        "installed, or save the drawing as DXF in your CAD package and feed the "
        "DXF to this tool instead."
    )
