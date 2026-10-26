"""Tests of the live Revit connection (building.revit_mcp) against a stand-in for the
Revit MCP plug-in: same wire format (one JSON-RPC message per line, send_code_to_revit).

    python -m building.revit_mcp_selftest
"""
import json
import os
import shutil
import socket
import socketserver
import tempfile
import threading
import time
import unittest

from building import revit_mcp as rm


class FakeRevit(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), Handler)
        self.calls = []
        self.time_out_extras = False
        self.connected_doc = True
        self.stop_level5 = False
        self.existing = ["Level 1|0"]                   # the levels in the open model


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        line = self.rfile.readline()
        if not line:
            return
        req = json.loads(line)
        srv = self.server
        code, p = req["params"]["code"], req["params"].get("parameters", [])
        if code == rm.STATUS_CS:
            kind, res = "status", ("OK|2025|Tower.rvt" if srv.connected_doc else "NODOC")
        elif code == rm.NEW_CS:
            kind, res = "new", "OK\nC:\\templates\\Structural.rte"
        elif code == rm.LEVEL_LIST_CS:
            kind, res = "level list", "OK\n" + "\n".join(srv.existing)
        elif code == rm.LEVELS_CS:
            kind = "levels"
            res = "OK\n" + "\n".join("created|%s|%s" % tuple(x.split("|")) for x in p[2:])
        elif code == rm.FAMILIES_CS:
            kind, res = "families", "OK\nloaded|column|M_Concrete-Rectangular-Column\nhave|beam"
        elif code in (rm.RUN_CS, rm.RUN_FX_CS):
            kind = "run:" + os.path.basename(p[2])
            res = "OK\nlog of %s\nbuilt %s" % (os.path.basename(p[2]), p[3].replace("\n", ";"))
            if "Level 5" in p[3] and srv.stop_level5:
                res += "\nSTOP: no rectangular concrete column family is loaded."
            if srv.time_out_extras and p[2].endswith("build_extras.py"):
                def later(path=p[5], text=res):
                    time.sleep(0.5)
                    with open(path, "w", encoding="utf-8") as f:
                        f.write(text)
                threading.Thread(target=later).start()
                srv.calls.append((kind, p))
                self.wfile.write((json.dumps({"jsonrpc": "2.0", "id": req["id"],
                                              "error": {"message": "Command execution timed out."}}) + "\n").encode())
                return
        elif code == rm.CLOSE_CS:
            kind, res = "close", "OK\nsaved"
        else:
            kind, res = "other", "?"
        srv.calls.append((kind, p))
        self.wfile.write((json.dumps({"jsonrpc": "2.0", "id": req["id"],
                                      "result": {"success": True, "result": res}}) + "\n").encode())


def _run_folder(tmp):
    """A minimal exported analysis: two floors (given top first) and foundations."""
    run = os.path.join(tmp, "runs", "2026-10-07_120000")
    for plan, lvl in (("KA_102", "Level 5"), ("KA_101", "Level 4"), ("KA_501", "")):
        d = os.path.join(run, "revit_build", plan)
        os.makedirs(d)
        m = {"columns": [{"mark": "C1"}] if lvl else [], "beams": [], "slabs": [], "level_name": lvl,
             "export": {"plan": plan}}
        with open(os.path.join(d, "model.json"), "w", encoding="utf-8") as f:
            json.dump(m, f)
    with open(os.path.join(run, "revit_build", "KA_101", "foundations.json"), "w", encoding="utf-8") as f:
        json.dump({"plan": "KA_101", "items": []}, f)
    with open(os.path.join(run, "revit_build", "levels.json"), "w", encoding="utf-8") as f:
        json.dump([{"revit_name": "Level 5", "elevation_mm": 15300.0, "status": "read"}], f)
    return run


# the floor levels a person entered on the Export page (the drawings give only Level 5)
DECIDED = {"levels": {"Level 4": 12150, "Level 3": 9000}}


class RevitConnection(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="c2r_mcp_")
        self.srv = FakeRevit()
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self._ipy = rm.ironpython
        rm.ironpython = lambda v: ("C:\\ipy", "C:\\ipy\\Lib")

    def tearDown(self):
        rm.ironpython = self._ipy
        self.srv.shutdown()
        self.srv.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_status(self):
        st = rm.status(port=self.port)
        self.assertTrue(st["connected"])
        self.assertEqual((st["version"], st["document"]), ("2025", "Tower.rvt"))
        self.srv.connected_doc = False
        self.assertIn("no project is open", rm.status(port=self.port)["message"])

    def test_not_running(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()                                           # nothing listens there
        st = rm.status(port=port)
        self.assertFalse(st["connected"])
        self.assertIn("switch on the MCP", st["message"])

    def test_build_in_open_model_bottom_floor_first(self):
        run = _run_folder(self.tmp)
        log = []
        rm.build(run, log=log.append, port=self.port, decisions=DECIDED)
        kinds = [k for k, _ in self.srv.calls]
        self.assertEqual(kinds, ["status", "level list", "levels", "families", "run:build_model.py",
                                 "run:build_model.py", "run:build_extras.py"])
        runs = [p for k, p in self.srv.calls if k == "run:build_model.py"]
        self.assertIn("level_name=Level 4", runs[0][3])     # the lower floor first
        self.assertIn("level_name=Level 5", runs[1][3])
        self.assertEqual(runs[0][4], "")                    # the open model
        lv = [p for k, p in self.srv.calls if k == "levels"][0]
        self.assertEqual(lv[1], "open")
        self.assertIn("Level 5|15300.0", lv)                # from the drawings
        self.assertIn("Level 4|12150.0", lv)                # decided on the Export page
        self.assertIn("Level 3|9000.0", lv)                 # the level the lowest columns rise from
        self.assertIn("REVIT DONE", log)
        self.assertFalse(any("assumed" in x for x in log))
        self.assertTrue(any("Level 4" in x and "your decision" in x for x in log))
        self.assertIn("REVIT Loaded Revit's column family 'M_Concrete-Rectangular-Column'", log)

    def test_levels_without_an_elevation_stop_before_anything_is_built(self):
        """The drawings give Level 5 only: no storey height is assumed for Levels 3 and 4."""
        run = _run_folder(self.tmp)
        with self.assertRaises(rm.RevitError) as cm:
            rm.build(run, log=lambda m: None, port=self.port)
        self.assertIn("Level 3, Level 4", str(cm.exception))
        self.assertEqual([k for k, _ in self.srv.calls], ["status", "level list"])   # nothing made

    def test_a_level_already_in_the_users_model_counts(self):
        """Their model has Levels 3 and 4: those are theirs, used as they stand."""
        run = _run_folder(self.tmp)
        self.srv.existing = ["Level 3|9000", "Level 4|12000"]
        rm.build(run, log=lambda m: None, port=self.port)
        lv = [p for k, p in self.srv.calls if k == "levels"][0]
        self.assertEqual(lv[2:], ["Level 5|15300.0"])       # only the one to create

    def test_a_new_files_template_levels_do_not_count(self):
        run = _run_folder(self.tmp)
        self.srv.existing = ["Level 3|9000", "Level 4|12000"]
        with self.assertRaises(rm.RevitError):
            rm.build(run, new_file=os.path.join(self.tmp, "p", "job.rvt"), log=lambda m: None, port=self.port)
        self.assertNotIn("new", [k for k, _ in self.srv.calls])                      # no file made

    def test_a_floor_that_stops_is_reported_not_called_done(self):
        """Seen live: build_model said STOP (no concrete family) and the app said Done."""
        run = _run_folder(self.tmp)
        self.srv.stop_level5 = True
        log = []
        rm.build(run, log=log.append, port=self.port, decisions=DECIDED)
        self.assertTrue(any(x.startswith("REVIT PROBLEM") and "Level 5" in x for x in log))
        self.assertTrue(log[-1].startswith("REVIT DONE: 1 step stopped"))

    def test_revit_family_found_in_its_library(self):
        lib = os.path.join(self.tmp, "Libraries")
        for rel in ("English/UK/Structural Columns/Concrete/Concrete Rectangular.rfa",
                    "English/US/Structural Columns/Concrete/M_Concrete-Rectangular-Column with Drop Caps.rfa",
                    "English/US/Structural Columns/Concrete/M_Concrete-Rectangular-Column.rfa",
                    "English/UK/Structural Framing/Concrete/Concrete - Rectangular Beam.rfa"):
            f = os.path.join(lib, *rel.split("/"))
            os.makedirs(os.path.dirname(f), exist_ok=True)
            open(f, "w").close()
        self.assertTrue(rm.family_file("column", "2025", lib).endswith("M_Concrete-Rectangular-Column.rfa"))
        self.assertTrue(rm.family_file("beam", "2025", lib).endswith("Concrete - Rectangular Beam.rfa"))
        self.assertEqual(rm.family_file("beam", "2025", os.path.join(self.tmp, "none")), "")

    def test_revit_2025_uses_the_bundled_ironpython_3(self):
        """Rhino's IronPython 2.7 cannot define a class on Revit's .NET 10."""
        rm.ironpython = self._ipy
        d, lib = rm.ironpython("2025")
        self.assertEqual(os.path.normcase(d), os.path.normcase(rm.BUNDLED_IPY))
        self.assertTrue(os.path.exists(os.path.join(lib, "json", "__init__.py")))
        self.assertIs(rm._run_cs_for(d), rm.RUN_CS)
        self.assertIn("AssemblyLoadContext", rm.RUN_CS)
        self.assertNotIn("AssemblyLoadContext", rm.RUN_FX_CS)

    def test_new_file_saved_and_closed(self):
        run = _run_folder(self.tmp)
        rvt = os.path.join(self.tmp, "packages", "job.rvt")
        rm.build(run, new_file=rvt, log=lambda m: None, port=self.port, decisions=DECIDED)
        kinds = [k for k, _ in self.srv.calls]
        self.assertEqual(kinds[:2], ["status", "new"])
        self.assertEqual(kinds[-1], "close")
        for k, p in self.srv.calls:
            if k.startswith("run:"):
                self.assertEqual(p[4], rvt)                 # built into the new file, not the open one
        self.assertEqual([p for k, p in self.srv.calls if k == "levels"][0][1], "new")

    def test_waits_for_revit_when_the_plugin_gives_up(self):
        run = _run_folder(self.tmp)
        self.srv.time_out_extras = True
        log = []
        rm.build(run, log=log.append, port=self.port, decisions=DECIDED)
        self.assertIn("REVIT DONE", log)
        self.assertTrue(any("build_extras.py" in x for x in log))

    def test_level_below_named_in_the_same_scheme(self):
        run = _run_folder(self.tmp)
        names = [n for n, _, _ in rm.level_targets(run)]
        self.assertEqual(names, ["Level 3", "Level 4", "Level 5"])

    def test_csharp_is_plain(self):
        """The plug-in's compiler version is unknown: no C# 6+ syntax."""
        for code in (rm.RUN_CS, rm.RUN_FX_CS, rm.LEVELS_CS, rm.LEVEL_LIST_CS, rm.FAMILIES_CS, rm.NEW_CS,
                     rm.CLOSE_CS, rm.STATUS_CS):
            self.assertNotIn('$"', code)
            self.assertNotIn("?.", code)
            self.assertNotIn("nameof(", code)


class ScriptReport(unittest.TestCase):
    def test_one_element_that_failed_is_a_problem(self):
        """build_model says 'beam B1 failed: ...' for one element: not to be called done."""
        out = "COLUMN   12 placed\nbeam B1 failed: invalid geometry\nBEAM     40 placed\n"
        self.assertEqual(rm._report(out, "Level 5", lambda m: None), ["Level 5: beam B1 failed: invalid geometry"])
        for line in ("STOP: no level to build on (see above). Nothing was placed.", "FAILED:",
                     "footing F3 failed: no type", "Error: the document is read-only"):
            self.assertEqual(len(rm._report(line, "L1", lambda m: None)), 1, line)

    def test_a_clean_build_has_no_problem(self):
        out = ("COLUMN   12 placed\nSUNK     2 slabs dropped below the level and checked in Revit: S1, S2\n"
               "WARNING  Could not make the floor type 'S1 150'; its slabs were given 'Generic 150' instead. "
               "Change their type by hand. S1 150: InvalidOperationException error\nStopping at Level 5 as asked\n")
        self.assertEqual(rm._report(out, "Level 5", lambda m: None), [])


if __name__ == "__main__":
    unittest.main()
