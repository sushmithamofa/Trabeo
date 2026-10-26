"""Start Revit by itself, so 'Create a Revit file' needs no one at Revit.

Revit is started with a journal - Revit's own record-and-replay file - that does
what a person would: open a new project (Revit's default template) and press the
Revit MCP switch on the Add-Ins tab. The app then talks to it over the live
connection as usual: makes the real project from the structural template, builds
every floor, saves the .rvt, and closes the Revit it started. A Revit the user had
open is never closed, and never touched unless it is already connected.

The journal lines are the ones Revit itself records for those clicks (see a
journal in %LOCALAPPDATA%\\Autodesk\\Revit\\Autodesk Revit 20xx\\Journals).
"""
from __future__ import annotations

import glob
import os
import re
import subprocess
import tempfile
import time

from building import revit_mcp

PROGRAM_FILES = os.environ.get("ProgramW6432") or os.environ.get("ProgramFiles") or r"C:\Program Files"
ADDINS = os.path.join(os.environ.get("APPDATA", ""), "Autodesk", "Revit", "Addins")

# The MCP switch: Add-Ins tab, 'Revit MCP Plugin' panel, button ID_EXCMD_TOGGLE_REVIT_MCP.
SWITCH = ("Execute external command:CustomCtrl_%CustomCtrl_%Add-Ins%Revit MCP Plugin%"
          "ID_EXCMD_TOGGLE_REVIT_MCP:revit_mcp_plugin.Core.MCPServiceConnection")

JOURNAL = """' CAD to Revit: open a project and switch on the Revit MCP service.
' Written by the app; Revit replays it at start-up. Safe to delete.
Dim Jrn
Set Jrn = CrsJournalScript
Jrn.Command "Ribbon" , "Create a new project , ID_FILE_NEW_CHOOSE_TEMPLATE"
Jrn.PushButton "Modal , New Project , Dialog_Revit_NewProject" , "OK, IDOK"
Jrn.RibbonEvent "%s"
""" % SWITCH


def installed() -> list[tuple[str, str]]:
    """[(version, Revit.exe)] of each installed Revit that has the MCP plug-in, newest first."""
    out = []
    for exe in glob.glob(os.path.join(PROGRAM_FILES, "Autodesk", "Revit 20*", "Revit.exe")):
        m = re.search(r"Revit (20\d\d)", exe)
        if not m:
            continue
        ver = m.group(1)
        if glob.glob(os.path.join(ADDINS, ver, "revit_mcp_plugin", "RevitMCPPlugin.dll")):
            out.append((ver, exe))
    return sorted(out, reverse=True)


def any_revit_installed() -> bool:
    return bool(glob.glob(os.path.join(PROGRAM_FILES, "Autodesk", "Revit 20*", "Revit.exe")))


def running() -> bool:
    """Is any Revit open on this PC?"""
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Revit.exe", "/NH"], capture_output=True,
                             text=True, timeout=20).stdout
    except Exception:
        return False
    return "Revit.exe" in out


def start(log=print, wait_s: float = 900.0, port=None):
    """Start Revit, open a project in it and switch the MCP service on.

    Returns the Revit process once the connection answers with a document open.
    Raises revit_mcp.RevitError when there is no Revit with the plug-in, or it
    does not come up in wait_s seconds (it is then closed again)."""
    inst = installed()
    if not inst:
        raise revit_mcp.RevitError(
            "No Revit with the Revit MCP plug-in is installed on this PC, so the app cannot start Revit by "
            "itself. Create an IFC file instead (no Revit needed), or install the plug-in.")
    ver, exe = inst[0]
    journal = os.path.join(tempfile.gettempdir(), "c2r_start_revit_%s.txt" % ver)
    with open(journal, "w", encoding="ascii") as f:
        f.write(JOURNAL)
    log("REVIT Starting Revit %s by itself (it can take a few minutes) ..." % ver)
    proc = subprocess.Popen([exe, journal], cwd=os.path.dirname(exe))
    t0 = time.time()
    while time.time() - t0 < wait_s:
        if proc.poll() is not None:
            raise revit_mcp.RevitError("Revit closed while it was starting (exit code %s)." % proc.returncode)
        if revit_mcp.status(port).get("connected"):
            log("REVIT Revit %s is up and connected (%.0f s)." % (ver, time.time() - t0))
            return proc
        time.sleep(5)
    stop(proc, log)
    raise revit_mcp.RevitError("Revit did not come up within %d minutes. Open Revit, switch on the MCP "
                               "service on the Add-Ins tab, and try again." % (wait_s // 60))


def stop(proc, log=print) -> None:
    """Close a Revit this module started. Its own project was saved and closed by the
    build; the scratch project it opened to start with is never saved."""
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
    # not a REVIT line: the page reads the last one as the result (DONE / STOPPED)
    log("  Closed the Revit the app started.")
