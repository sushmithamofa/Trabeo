r"""Tests for Stage 9: the workflow (drawings -> review -> Revit files) and its window.

    python -m building.stage9_selftest
"""
from __future__ import annotations

import os
import shutil
import tempfile
import time
import unittest

from building.workflow import keep_converted, next_steps, seed_cache, stale_converters

TASKLIST = '''"Image Name","PID","Session Name","Session#","Mem Usage","Status","User Name","CPU Time","Window Title"
"ZWCAD.exe","23996","Console","1","412,000 K","Running","PC\\Admin","0:00:07","ZWCAD 2024 Professional Edition - [C:\\Users\\Admin\\AppData\\Local\\Temp\\c2r_zw_vzquqy4e\\drawing.dwg]"
"ZWCAD.exe","31000","Console","1","380,000 K","Running","PC\\Admin","0:03:12","ZWCAD 2024 Professional Edition - [D:\\Jobs\\Hotel\\GF PLAN.dwg]"
"ZWCAD.exe","31252","Console","1","400,000 K","Not Responding","PC\\Admin","0:00:04","ZWCAD 2024 Professional Edition - [C:\\Users\\Admin\\AppData\\Local\\Temp\\c2r_zw_69p7ogz7\\drawing.dwg]"
'''


class Converters(unittest.TestCase):
    def test_only_this_tools_temporary_copies_are_closed(self):
        self.assertEqual(stale_converters(TASKLIST), [23996, 31252])        # never the job drawing

    def test_no_zwcad_running(self):
        self.assertEqual(stale_converters('INFO: No tasks are running which match the specified criteria.'), [])
        self.assertEqual(stale_converters(""), [])


class Cache(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.dwg = os.path.join(self.tmp, "GF PLAN.dwg")
        open(self.dwg, "w").write("dwg")
        self.cache = os.path.join(self.tmp, "cache")
        self.work = os.path.join(self.tmp, "work")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_converted_drawing_is_kept_and_reused_while_the_dwg_is_unchanged(self):
        os.makedirs(self.work)
        time.sleep(0.05)
        open(os.path.join(self.work, "GF PLAN.dxf"), "w").write("dxf")
        self.assertEqual(keep_converted(self.work, self.cache, [self.dwg]), 1)
        nxt = os.path.join(self.tmp, "work2")
        self.assertEqual(seed_cache(self.cache, nxt, [self.dwg]), 1)
        self.assertTrue(os.path.exists(os.path.join(nxt, "GF PLAN.dxf")))
        # the drawing changes: its old conversion must not be used
        time.sleep(0.05)
        open(self.dwg, "w").write("dwg, revised")
        self.assertEqual(seed_cache(self.cache, os.path.join(self.tmp, "work3"), [self.dwg]), 0)


class Summary(unittest.TestCase):
    def test_next_steps_put_decisions_first_and_end_with_the_revit_build(self):
        s = {"values": {"read": 900, "conflict": 12, "missing": 40},
             "issues": {"warning: column mark": 21, "warning: wall": 9, "info: missing": 3},
             "suggested_profiles_used": True}
        steps = next_steps(s)
        self.assertTrue(steps[0].startswith("12 values have sources that disagree"))
        self.assertTrue(any("40 values no drawing gives" in x for x in steps))
        self.assertTrue(any("21 column marks" in x for x in steps))
        self.assertTrue(any("suggested profiles" in x for x in steps))
        self.assertIn("build_model.py", steps[-1])

    def test_blockers_come_first(self):
        steps = next_steps({"values": {"conflict": 2}, "issues": {"blocker: setting out": 4, "blocker: tags": 4}})
        self.assertTrue(steps[0].startswith("8 blockers (4 setting out, 4 tags)"))

    def test_built_red_is_not_called_left_out(self):
        """New block: '85 elements are left out of Revit' while the export built all 85, red."""
        step = next(x for x in next_steps({"values": {}, "issues": {},
                                           "prebuild": {"needs_decision": 85, "built_red": 85}})
                    if "need a decision" in x)
        self.assertIn("85 are built meanwhile", step)
        self.assertNotIn("left out", step)

    def test_a_clean_run_goes_straight_to_revit(self):
        self.assertEqual(len(next_steps({"values": {"read": 100}, "issues": {}})), 1)


class Scorecard(unittest.TestCase):
    def test_the_four_verdicts(self):
        import sys
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
        import scorecard
        from building import Building, Element, Value
        b = Building()
        for n, (mark, w, d, st) in enumerate((("C1", 300.0, 600.0, "read"), ("C2", 230.0, 450.0, "read"),
                                              ("C3", 300.0, 600.0, "conflict"))):
            e = b.add(Element(id=f"P/column/{n}", type="column", sheet="P", mark=mark))
            e.resolved = {"mark": Value(mark, "read", 1.0, []), "width": Value(w, st, 1.0, []),
                          "depth": Value(d, "read", 1.0, [])}
        s = scorecard.score(b, {"columns": [{"mark": "C1", "width": 600, "depth": 300},      # either way round
                                            {"mark": "C2", "width": 300, "depth": 600},
                                            {"mark": "C3", "width": 300, "depth": 600},
                                            {"mark": "C4", "width": 300, "depth": 600}]})
        self.assertEqual([r["verdict"] for r in s["rows"]], ["correct", "WRONG", "flagged", "not found"])
        self.assertEqual(s["silent_wrong_pct"], 25.0)

    def test_a_beam_read_the_wrong_way_round_is_wrong(self):
        """600 x 230 against 230 x 600 counted as correct: a beam's sizes are not sorted."""
        import sys
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
        import scorecard
        from building import Building, Element, Value
        b = Building()
        e = b.add(Element(id="P/beam/0", type="beam", sheet="P", mark="B1"))
        e.resolved = {"mark": Value("B1", "read", 1.0, []), "width": Value(600.0, "read", 1.0, []),
                      "depth": Value(230.0, "read", 1.0, [])}
        s = scorecard.score(b, {"beams": [{"mark": "B1", "width": 230, "depth": 600}]})
        self.assertEqual(s["rows"][0]["verdict"], "WRONG")
        s = scorecard.score(b, {"beams": [{"mark": "B1", "width": 600, "depth": 230}]})
        self.assertEqual(s["rows"][0]["verdict"], "correct")


class View3D(unittest.TestCase):
    def test_floors_stack_on_the_lowest_plan_and_storeys_follow_the_level_number(self):
        """Two plans drawn at different origins (offset 50 m): the upper one is moved onto
        the lower by its columns; L3 sits 9 m up although no L2 plan was sent."""
        from building import Building, Element, Evidence, Sheet, Value
        from building.viewer3d import scene
        b = Building()
        bays = [0, 4000, 7500, 12000, 15000, 19500, 23000, 27500]
        for sid, key, dx in (("A/plan", "L1", 0.0), ("B/plan", "L3", 50000.0)):
            b.add_sheet(Sheet(id=sid, file=sid, reader="plan.legacy", extra={"set": "P", "level_key": key}))
            for i, x in enumerate(bays):
                e = b.add(Element(id=f"{sid}/column/{i}", type="column", sheet=sid, mark=f"C{i + 1}",
                                  geometry={"x": x + dx, "y": 0.0}))
                ev = [Evidence("p")]
                e.resolved = {"mark": Value(f"C{i + 1}", "read", 1.0, ev), "width": Value(230.0, "read", 1.0, ev),
                              "depth": Value(600.0, "read", 1.0, ev)}
        sc = scene(b, "P")
        self.assertEqual([(l["key"], l["z"]) for l in sc["levels"]], [("L1", 3000.0), ("L3", 9000.0)])
        upper = [i for i in sc["items"] if i["lv"] == "L3"]
        self.assertEqual(sorted(round(i["x"]) for i in upper), bays)                 # moved onto L1
        self.assertEqual({(i["z0"], i["z1"]) for i in upper}, {(3000.0, 9000.0)})   # from the floor below
        self.assertIn("3 m per storey", sc["notes"][0])

    def test_a_curved_beam_is_drawn_curved(self):
        """Main Block terrace: the quarter-arc beam was a straight box in the 3D view."""
        import math
        from building import Building, Element, Evidence, Sheet, Value
        from building.viewer3d import scene
        b = Building()
        b.add_sheet(Sheet(id="A/plan", file="a", reader="plan.legacy", extra={"set": "P", "level_key": "L1"}))
        ev = [Evidence("p")]
        e = b.add(Element(id="A/plan/beam/0", type="beam", sheet="A/plan", mark="B1",
                          geometry={"start": [1315.0, 0.0], "end": [0.0, 1315.0],
                                    "mid": [1315.0 * math.cos(math.pi / 4), 1315.0 * math.sin(math.pi / 4)]}))
        e.resolved = {"width": Value(230.0, "read", 1.0, ev), "depth": Value(600.0, "read", 1.0, ev)}
        it = next(i for i in scene(b, "P")["items"] if i["id"] == e.id)
        radii = [math.hypot(x, y) for x, y in it["p"]]
        self.assertAlmostEqual(min(radii), 1200.0, delta=1.0)
        self.assertAlmostEqual(max(radii), 1430.0, delta=1.0)


class Window(unittest.TestCase):
    def test_the_window_opens(self):
        import tkinter as tk
        try:
            import app
            w = app.App()
        except tk.TclError:
            self.skipTest("no display")
        try:
            w.project.set(tempfile.mkdtemp())
            w.run_dir = None
            w.files.clear()
            w._refresh()
            self.assertEqual(str(w.b_analyse["state"]), "disabled")       # nothing imported yet
            self.assertEqual(str(w.b_export["state"]), "disabled")        # nothing analysed yet
            here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            w._add([os.path.join(here, "samples", "synthetic", "KA_consultant")])
            self.assertEqual(len(w.files), 3)
            self.assertEqual(str(w.b_analyse["state"]), "normal")
            # KA-101 FIRST FLOOR FRAMING PLAN: the 1st floor is put in the level list at once
            self.assertEqual(app.parse_levels(w.levels.get()).get("L1"), "Level 1")
            # the picker: tick floors, pick names from lists
            got = {}
            p = app.LevelPicker(w, app.parse_levels(w.levels.get()), app.detect_levels(w.files), got.update)
            p.upto.set("4th floor")
            p._tick_upto()
            p._scheme("words")
            p._ok()
            self.assertEqual(got, {"GF": "Ground Floor", "L1": "First Floor", "L2": "Second Floor",
                                   "L3": "Third Floor", "L4": "Fourth Floor"})
        finally:
            w.destroy()


class WebAppSafety(unittest.TestCase):
    def test_other_websites_cannot_send_changes_or_read_beside_the_run(self):
        """Any website open in the browser could POST fake decisions; /run/ served a sibling
        folder ('run2') because its check was a bare startswith."""
        import importlib
        import threading
        import urllib.error
        import urllib.request
        tmp = tempfile.mkdtemp(prefix="c2r_safe_")
        os.environ["C2R_SETTINGS"] = os.path.join(tmp, "settings.json")
        import webapp
        webapp = importlib.reload(webapp)
        from http.server import ThreadingHTTPServer
        port = webapp.free_port(8950)
        srv = ThreadingHTTPServer(("127.0.0.1", port), webapp.H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{port}"

        def code(path, origin=None, post=True):
            h = {"Content-Type": "application/json", **({"Origin": origin} if origin else {})}
            req = urllib.request.Request(base + path, data=b'{"i": -1}' if post else None,
                                         method="POST" if post else "GET", headers=h)
            try:
                with urllib.request.urlopen(req) as r:
                    return r.status
            except urllib.error.HTTPError as exc:
                return exc.code
        try:
            self.assertEqual(code("/api/remove", "https://evil.example"), 403)
            self.assertEqual(code("/api/remove", "null"), 403)
            self.assertEqual(code("/api/remove", f"http://127.0.0.1:{port}"), 200)    # the app's own page
            self.assertEqual(code("/api/remove", f"http://localhost:{port}"), 200)
            self.assertEqual(code("/api/remove"), 200)                                 # a script on this PC
            run, run2 = os.path.join(tmp, "run"), os.path.join(tmp, "run2")
            os.makedirs(run)
            os.makedirs(run2)
            open(os.path.join(run, "export_report.txt"), "w").write("ok")
            open(os.path.join(run2, "secret.txt"), "w").write("not yours")
            webapp.S.run_dir = run
            self.assertEqual(code("/run/export_report.txt", post=False), 200)
            self.assertEqual(code("/run/%2e%2e/run2/secret.txt", post=False), 404)
            # decisions with no project open were answered "ok" and kept nowhere
            webapp.S.project = None
            self.assertEqual(code("/api/decisions"), 400)
            webapp.S.project = os.path.join(tmp, "project")
            os.makedirs(webapp.S.project)
            self.assertEqual(code("/api/decisions"), 200)
            self.assertTrue(os.path.exists(os.path.join(webapp.S.project, "overrides.json")))
        finally:
            webapp.S.project = None
            srv.shutdown()
            srv.server_close()
            webapp.S.run_dir = None
            shutil.rmtree(tmp, ignore_errors=True)


class WebApp(unittest.TestCase):
    def test_one_window_import_analyse_review_decide_export(self):
        """The browser app end to end, through its own server: import, levels from lists,
        analyse, the review inside the app hands decisions to the project, export."""
        import json
        import threading
        import time
        import urllib.request
        tmp = tempfile.mkdtemp(prefix="c2r_web_")
        os.environ["C2R_SETTINGS"] = os.path.join(tmp, "settings.json")
        import importlib
        import webapp
        webapp = importlib.reload(webapp)
        from http.server import ThreadingHTTPServer
        port = webapp.free_port(8900)
        srv = ThreadingHTTPServer(("127.0.0.1", port), webapp.H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{port}"

        def get(p):
            with urllib.request.urlopen(base + p) as r:
                return r.read().decode("utf-8")

        def post(p, body):
            req = urllib.request.Request(base + p, data=json.dumps(body).encode(), method="POST",
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req) as r:
                return json.loads(r.read() or b"{}")
        try:
            page = get("/")
            for word in ("Import CAD", "Analyse", "Review", "Export to Revit", "Try a sample", "Create Revit package"):
                self.assertIn(word, page)
            for jargon in ("overrides.json", "profile", "readset", "building.json"):   # plain words on screen
                self.assertNotIn(jargon, page.split("<script>")[0])
            here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            webapp.S.project = os.path.join(tmp, "project")
            webapp._add([os.path.join(here, "samples", "synthetic", "KA_consultant")])
            # a drawing's units chosen on Import CAD: kept by file name; Auto takes it back
            name = os.path.basename(webapp.S.files[0])
            st0 = post("/api/project", {"units_set": {name: "ft"}, "no_export": True})
            self.assertEqual(next(f["units"] for f in st0["files"] if f["name"] == name), "ft")
            st0 = post("/api/project", {"units_set": {name: "auto"}, "no_export": True})
            self.assertEqual(next(f["units"] for f in st0["files"] if f["name"] == name), "")
            post("/api/project", {"units_set": {name: "mm"}, "no_export": True})    # read as stated
            lv = json.loads(get("/api/levels"))
            self.assertIn("L1", lv["detected"])
            post("/api/levels", {"chosen": {"GF": "Ground Floor", "L1": "First Floor"}})
            post("/api/analyse", {})
            for _ in range(600):
                st = json.loads(get("/api/state"))
                if not st["running"]:
                    break
                time.sleep(0.5)
            self.assertTrue(st["run"], st["log"][-5:])
            review = get("/run/review.html")
            self.assertIn("/api/decisions", review)              # the review hands decisions to the app
            b = json.load(open(os.path.join(st["run"], "building.json"), encoding="utf-8"))
            els = b["elements"] if isinstance(b["elements"], list) else list(b["elements"].values())
            c01 = next(e["id"] for e in els if e["type"] == "column" and e["resolved"]["mark"]["value"] == "C01")
            post("/api/decisions", {"elements": {}, "marks": {}, "exclude": [c01]})
            self.assertEqual(json.loads(get("/api/state"))["decisions"], 1)
            post("/api/export", {})
            for _ in range(600):
                st = json.loads(get("/api/state"))
                if not st["running"]:
                    break
                time.sleep(0.5)
            self.assertTrue(st["built"])
            plan = next(d for d in os.listdir(os.path.join(st["run"], "revit_build")) if d.startswith("KA_101"))
            with open(os.path.join(st["run"], "revit_build", plan, "model.json"), encoding="utf-8") as f:
                self.assertNotIn("C01", [c["mark"] for c in json.load(f)["columns"]])
            pk = post("/api/package", {})                       # the .zip for whoever builds in Revit
            import zipfile
            with zipfile.ZipFile(pk["package"]) as z:
                names = z.namelist()
            self.assertTrue(any(n.endswith("model.json") for n in names))
            self.assertIn("review.html", names)
        finally:
            srv.shutdown()
            os.environ.pop("C2R_SETTINGS", None)
            shutil.rmtree(tmp, ignore_errors=True)


class SetLegend(unittest.TestCase):
    def test_a_legend_sheet_reaches_the_plans_of_its_own_set_only(self):
        """A key sheet imported with the plans (an office's or a consultant's) is read
        from the drawing; a plan sheet's own legend is never shared."""
        import types
        import ezdxf
        from building.readset import _set_legend
        tmp = tempfile.mkdtemp(prefix="c2r_key_")
        try:
            doc = ezdxf.new("R2010")
            m = doc.modelspace()
            for k, (cap, pat) in enumerate((("100MM SUNK", "ANGLE"), ("150MM SUNK", "ANSI36"),
                                            ("250MM SUNK", "AR-PARQ1"))):
                y = -1000.0 * k
                m.add_text(cap, dxfattribs={"layer": "TXT", "height": 200}).set_placement((0, y))
                h = m.add_hatch(dxfattribs={"layer": "HATCH LIGHT"})
                h.set_pattern_fill(pat, scale=1.0)
                h.paths.add_polyline_path([(4500, y - 350), (6000, y - 350), (6000, y + 350), (4500, y + 350)])
            key = os.path.join(tmp, "OFFICE KEY.dxf")
            doc.saveas(key)

            def rec(kind):
                return types.SimpleNamespace(verdict=types.SimpleNamespace(kind=kind),
                                             view=types.SimpleNamespace(source="model region"))
            got = _set_legend([(key, [rec("legend")])], tmp, log=lambda *a: None)
            self.assertEqual(got["sheet"], "OFFICE KEY")
            self.assertIn(["HATCH LIGHT", "AR-PARQ1", 1.0, 250.0], got["samples"])
            # a plan with its own legend is not a key sheet for the others
            self.assertEqual(_set_legend([(key, [rec("legend"), rec("framing_plan")])], tmp,
                                         log=lambda *a: None), {})
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class FourSteps(unittest.TestCase):
    def test_import_analyse_decide_export(self):
        """The app's four steps on the blind set: a decision excludes one column, and
        Export to Revit leaves it out of the plan's model.json without reading again."""
        import json
        from building.workflow import apply_decisions, run
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        drawings = os.path.join(here, "samples", "synthetic", "KA_consultant")
        files = [os.path.join(drawings, f) for f in sorted(os.listdir(drawings))]
        project = tempfile.mkdtemp(prefix="c2r_app_")
        try:
            run_dir = run(files, project, "L1=Level 1", use_suggested=True, log=lambda *a, **k: None)
            plan = next(d for d in os.listdir(os.path.join(run_dir, "revit_build")) if d.startswith("KA_101"))
            model = os.path.join(run_dir, "revit_build", plan, "model.json")
            with open(model, encoding="utf-8") as f:
                cols = json.load(f)["columns"]
            self.assertEqual(len(cols), 12)
            b = json.load(open(os.path.join(run_dir, "building.json"), encoding="utf-8"))
            els = b["elements"] if isinstance(b["elements"], list) else list(b["elements"].values())
            c01 = next(e["id"] for e in els if e["type"] == "column" and e["resolved"]["mark"]["value"] == "C01")
            dec = os.path.join(project, "decisions_from_review.json")
            with open(dec, "w", encoding="utf-8") as f:
                json.dump({"elements": {}, "marks": {}, "exclude": [c01]}, f)
            apply_decisions(run_dir, dec, log=lambda *a, **k: None)
            with open(model, encoding="utf-8") as f:
                self.assertEqual(sorted(c["mark"] for c in json.load(f)["columns"]),
                                 [f"C{n:02d}" for n in range(2, 13)])
            self.assertTrue(os.path.exists(os.path.join(project, "overrides.json")))   # kept for later analyses
        finally:
            shutil.rmtree(project, ignore_errors=True)


class NewDrawingSet(unittest.TestCase):
    def test_a_new_set_empties_the_list_and_sets_the_old_analysis_aside(self):
        """'Start a new drawing set': the drawings go, the last analysis is set aside (still on
        disk, decisions kept) - also after the project is opened again - and the next
        analysis is the project's again."""
        import importlib
        import json
        import threading
        import urllib.request
        tmp = tempfile.mkdtemp(prefix="c2r_reset_")
        os.environ["C2R_SETTINGS"] = os.path.join(tmp, "settings.json")
        import webapp
        webapp = importlib.reload(webapp)
        from http.server import ThreadingHTTPServer
        proj = os.path.join(tmp, "job")
        old = os.path.join(proj, "runs", "2026-10-13_120000")
        os.makedirs(old)
        open(os.path.join(old, "review.html"), "w").write("<html></html>")
        dwg = os.path.join(tmp, "a.dxf")
        open(dwg, "w").write("0\nEOF\n")
        open(os.path.join(proj, "overrides.json"), "w").write('{"project": {"levels": {"L1": 3000}}}')
        webapp.S.open(proj)
        webapp.S.files = [dwg]
        webapp.S.save()
        self.assertEqual(webapp.S.run_dir, old)
        srv = ThreadingHTTPServer(("127.0.0.1", webapp.free_port(8990)), webapp.H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{srv.server_address[1]}/api/reset", data=b"{}",
                                         method="POST", headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req) as r:
                v = json.loads(r.read())
            self.assertEqual((v["files"], v["run"]), ([], None))
            self.assertTrue(os.path.exists(os.path.join(old, "review.html")))          # nothing deleted
            self.assertIn("L1", open(os.path.join(proj, "overrides.json")).read())    # decisions kept
            webapp.S.open(proj)                                                         # opened again
            self.assertIsNone(webapp.S.run_dir)
            new = os.path.join(proj, "runs", "2026-10-14_090000")
            os.makedirs(new)
            open(os.path.join(new, "review.html"), "w").write("<html></html>")
            webapp.S.open(proj)
            self.assertEqual(webapp.S.run_dir, new)
        finally:
            srv.shutdown()
            srv.server_close()
            webapp.S.project, webapp.S.run_dir = None, None
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=1)
