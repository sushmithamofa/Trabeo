r"""Tests for the checks that keep unsettled data out of Revit: drawing units, several
floors drawn side by side in model space, and the pre-build check.

    python -m building.checks_selftest
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest

import ezdxf

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "cad2revit"))
sys.path.insert(0, os.path.join(ROOT, "tools"))

from core import units                                      # noqa: E402
import make_synthetic_set as ka                             # noqa: E402


def drawing(code, text_h, width, n=60):
    """A drawing in units `code`: n lines spread over `width`, n texts text_h tall."""
    doc = ezdxf.new("R2018")
    doc.header["$INSUNITS"] = code
    msp = doc.modelspace()
    for i in range(n):
        x = width * i / n
        msp.add_line((x, 0), (x, width / 3))
        msp.add_text("B%d" % i, height=text_h, dxfattribs={"insert": (x, -text_h * 3)})
    return doc


class Units(unittest.TestCase):
    def test_millimetres_are_read_as_they_are(self):
        self.assertEqual(units.decide(drawing(4, 250.0, 30000.0))[0], 1.0)

    def test_metres_are_converted(self):
        k, name, note = units.decide(drawing(6, 0.25, 30.0))
        self.assertEqual((k, name), (1000.0, "metres"))
        self.assertIn("converted", note)

    def test_centimetres_are_converted(self):
        self.assertEqual(units.decide(drawing(5, 25.0, 3000.0))[0], 10.0)

    def test_labelled_metres_but_drawn_in_mm_is_read_as_mm(self):
        """A drawing made in mm from a template set to metres - common."""
        k, _, note = units.decide(drawing(6, 250.0, 30000.0))
        self.assertEqual(k, 1.0)
        self.assertIn("drawn in millimetres", note)

    def test_labelled_inches_with_nothing_written_in_feet_and_inches_is_stopped(self):
        with self.assertRaises(units.UnitsError) as cm:
            units.decide(drawing(1, 9.0, 1200.0))
        self.assertIn("feet and inches", str(cm.exception))

    def test_inches_and_feet_written_on_it_are_converted(self):
        doc = drawing(1, 9.0, 1200.0)
        doc.modelspace().add_text('B1 (9"x18")', height=9.0)
        doc.modelspace().add_text("SPAN 20'-0\"", height=9.0)
        self.assertEqual(units.decide(doc)[:2], (25.4, "inches"))
        doc = drawing(2, 0.5, 100.0)
        doc.modelspace().add_text('B1 (9"x18")', height=0.5)
        doc.modelspace().add_text("+10'-6\"", height=0.5)
        self.assertEqual(units.decide(doc)[:2], (304.8, "feet"))

    def test_sizes_written_in_feet_and_inches_read_in_mm(self):
        f = units.imperial_to_mm
        self.assertEqual(f('B1 (9"x18")'), "B1 (229x457)")
        self.assertEqual(f("1'-6\" x 2'-0\""), "457 x 610")
        self.assertEqual(f('2\'-0 1/2"'), "622")
        self.assertEqual(f('S1 5" THK'), "S1 127 THK")
        self.assertEqual(f("B1 (9x18)"), "B1 (229X457)")        # a bare pair on such a drawing
        self.assertEqual(f("+10'-6\""), "+3.200")                # a level, read in metres
        self.assertEqual(f("FFL 10'-6\""), "FFL +3.200")
        for same in ("SCALE 1/8\"=1'-0\"", "MK'D", "1'", "300X450", "2-T16", "PB4 (300X600)", "4B3"):
            self.assertEqual(f(same), same)

    def test_no_units_and_not_mm_sized_is_stopped(self):
        with self.assertRaises(units.UnitsError) as cm:
            units.decide(drawing(0, 0.25, 30.0))
        self.assertIn("State its units", str(cm.exception))

    def test_labelled_mm_but_plainly_metres_is_stopped(self):
        with self.assertRaises(units.UnitsError):
            units.decide(drawing(4, 0.25, 30.0))

    def test_units_a_person_stated_are_used(self):
        self.assertEqual(units.decide(drawing(0, 0.25, 30.0), "m")[0], 1000.0)
        self.assertEqual(units.decide(drawing(0, 0.25, 30.0), "ft")[0], 304.8)
        with self.assertRaises(units.UnitsError):
            units.decide(drawing(0, 0.25, 30.0), "yards")

    def test_units_chosen_that_the_drawing_contradicts_are_not_used(self):
        """aldoot: drawn in mm (text 250 tall), feet chosen on Import - read as mm, and said."""
        k, name, note = units.decide(drawing(4, 250.0, 30000.0), "ft")
        self.assertEqual((k, name), (1.0, "millimetres"))
        self.assertIn("do not fit", note)
        k, name, _ = units.decide(drawing(4, 250.0, 30000.0), "in")
        self.assertEqual(k, 1.0)

    def test_scaling_keeps_views_and_dimension_values(self):
        """A metre drawing showing its dimensions in mm (DIMLFAC 1000): after scaling,
        the dimension still reads 230 and the viewport still looks at the same things."""
        from building.readers.details import dimension_reading
        doc = ezdxf.new("R2018", setup=True)
        doc.header["$INSUNITS"] = 6
        msp = doc.modelspace()
        msp.add_line((1.0, 2.0), (4.0, 2.0))
        dim = msp.add_linear_dim(base=(0, 1), p1=(0, 0), p2=(0.23, 0), override={"dimlfac": 1000.0})
        dim.render()
        lay = doc.layouts.get("Layout1")
        vp = lay.add_viewport(center=(100, 100), size=(200, 150), view_center_point=(2.5, 2.0), view_height=6.0)
        self.assertEqual(units.scale_doc(doc, 1000.0), 0)
        line = [e for e in msp if e.dxftype() == "LINE"][0]
        self.assertEqual((line.dxf.start.x, line.dxf.end.x), (1000.0, 4000.0))
        d = [e for e in msp if e.dxftype() == "DIMENSION"][0]
        self.assertEqual(dimension_reading(d)[0], 230.0)
        self.assertEqual((tuple(vp.dxf.view_center_point)[:2], vp.dxf.view_height), ((2500.0, 2000.0), 6000.0))
        self.assertEqual(doc.header["$INSUNITS"], 4)

    def test_header_read_without_loading(self):
        tmp = tempfile.mkdtemp(prefix="c2r_u_")
        try:
            p = os.path.join(tmp, "a.dxf")
            drawing(6, 0.25, 30.0).saveas(p)
            self.assertEqual(units.header_units(p), 6)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class ReadingASet(unittest.TestCase):
    """Whole drawings through building.readset (the plan reader included)."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="c2r_chk_")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def read(self, name, files, overrides=None):
        from building.readset import read_set
        out = os.path.join(self.tmp, name, "run")
        return read_set(out, files, log=lambda *a, **k: None, use_suggested=True, overrides=overrides,
                        set_name="KA")

    def plans(self, b):
        return {s.extra.get("level_key"): sum(1 for e in b.elements if e.sheet == sid and e.type == "column")
                for sid, s in b.sheets.items() if s.reader == "plan.legacy"}

    def test_two_floors_side_by_side_are_read_one_by_one(self):
        """L1 and L2 drawn 40 m apart in one model space, no layouts: two plans of 12
        columns each - not one plan of 24, and not L1 alone."""
        d = os.path.join(self.tmp, "pair")
        os.makedirs(d)
        doc = ka.new_doc()
        ka.draw_plan(doc.modelspace(), 0.0, "FIRST FLOOR FRAMING PLAN")
        ka.draw_plan(doc.modelspace(), 40000.0, "SECOND FLOOR FRAMING PLAN")
        doc.saveas(os.path.join(d, "KA-100 FRAMING PLANS.dxf"))
        b = self.read("pair", [d])
        self.assertEqual(self.plans(b), {"L1": 12, "L2": 12})

    def test_a_floor_the_app_does_not_know_is_not_added_to_another(self):
        """L1, L2 and a 'SERVICE FLOOR' plan side by side (Akanistha, 22-10): the service floor
        is read on its own, with no level - its 12 columns are never another floor's (they
        were added to the largest floor, which then did not stack on the others)."""
        d = os.path.join(self.tmp, "three")
        os.makedirs(d)
        doc = ka.new_doc()
        ka.draw_plan(doc.modelspace(), 0.0, "FIRST FLOOR FRAMING PLAN")
        ka.draw_plan(doc.modelspace(), 40000.0, "SECOND FLOOR FRAMING PLAN")
        ka.draw_plan(doc.modelspace(), 80000.0, "SERVICE FLOOR FRAMING PLAN")
        doc.saveas(os.path.join(d, "KA-102 FRAMING PLANS.dxf"))
        b = self.read("three", [d])
        self.assertEqual(self.plans(b), {"L1": 12, "L2": 12, "": 12})

    def test_a_drawing_in_metres_reads_the_same_as_in_mm(self):
        d = os.path.join(self.tmp, "metres")
        os.makedirs(d)
        doc = ka.new_doc()
        ka.draw_plan(doc.modelspace())
        units.scale_doc(doc, 0.001)
        doc.header["$INSUNITS"] = 6
        doc.saveas(os.path.join(d, "KA-101 FIRST FLOOR FRAMING PLAN.dxf"))
        b = self.read("metres", [d])
        cols = sorted((e.final("width"), e.final("depth")) for e in b.elements if e.type == "column")
        self.assertEqual(len(cols), 12)
        self.assertEqual(set(cols), {(300.0, 450.0), (300.0, 600.0)})
        self.assertTrue(any(i.category == "units" and "metres" in i.message for i in b.issues))

    def test_a_drawing_in_inches_reads_like_in_mm(self):
        """The same plan drawn in inches, its captions written 9"x18": the same columns
        and beams, the captions' sizes in mm (9" = 229)."""
        import re
        inch = {230: '9"', 300: '12"', 450: '18"', 500: '20"', 600: '24"'}
        d = os.path.join(self.tmp, "inches")
        os.makedirs(d)
        doc = ka.new_doc()
        ka.draw_plan(doc.modelspace())
        for t in doc.modelspace().query("TEXT"):
            t.dxf.text = re.sub(r"\b(\d{3})x(\d{3})\b",
                                lambda m: inch[int(m.group(1))] + "x" + inch[int(m.group(2))], t.dxf.text)
        units.scale_doc(doc, 1 / 25.4)
        doc.header["$INSUNITS"] = 1
        doc.saveas(os.path.join(d, "KA-101 FIRST FLOOR FRAMING PLAN.dxf"))
        b = self.read("inches", [d])
        cols = sorted((e.final("width"), e.final("depth")) for e in b.elements if e.type == "column")
        self.assertEqual(len(cols), 12)
        self.assertEqual(set(cols), {(300.0, 450.0), (300.0, 600.0)})
        beams = {e.final("mark"): (e.final("width"), e.final("depth")) for e in b.elements if e.type == "beam"}
        self.assertEqual(len(beams), 17)
        self.assertEqual((beams["B01"], beams["B04"]), ((229.0, 457.0), (305.0, 610.0)))
        self.assertTrue(any(i.category == "units" and "inches" in i.message for i in b.issues))

    def test_a_drawing_whose_units_cannot_be_known_is_not_read(self):
        d = os.path.join(self.tmp, "unknown")
        os.makedirs(d)
        doc = ka.new_doc()
        ka.draw_plan(doc.modelspace())
        units.scale_doc(doc, 0.001)
        doc.header["$INSUNITS"] = 0
        doc.saveas(os.path.join(d, "KA-101 FIRST FLOOR FRAMING PLAN.dxf"))
        b = self.read("unknown", [d])
        self.assertEqual(self.plans(b), {})
        self.assertTrue(any(i.severity == "blocker" and i.category == "units" for i in b.issues))
        # stated in the review: read
        b = self.read("unknown2", [d], {"project": {"units": {"KA-101 FIRST FLOOR FRAMING PLAN.dxf": "m"}}})
        self.assertEqual(self.plans(b), {"L1": 12})


class ReusedDrawing(unittest.TestCase):
    """J1153 '3ST FLOOR PLAN' carries the title '... 1ST FLOOR.': the file name decides."""

    def recs(self, title_lv, file_lv, levels=None, split=False, plans=1):
        from types import SimpleNamespace as N
        out = []
        for i in range(plans):
            out.append(N(verdict=N(kind="framing_plan"), view=N(same_as="", source="model region", split_from=split),
                         level_conflict=title_lv != file_lv, title="R.C.C. PLAN @ WING B 1ST FLOOR.", id=f"F:M{i + 1}",
                         level_sources=[("title", "x", [title_lv]), ("file name", "y", [file_lv])],
                         levels=list(levels or [title_lv]), levels_from=""))
        out.append(N(verdict=N(kind="slab_schedule"), view=N(same_as="", source="model region", split_from=False),
                     level_conflict=False, title="SCHEDULE OF SLAB WING B 1ST FLOOR", id="F:M9",
                     level_sources=[("title", "x", [title_lv])], levels=[title_lv], levels_from=""))
        return out

    def test_one_plan_file_takes_its_file_names_floor_with_its_schedules(self):
        from building import Building
        from building.readset import _reused_drawing
        b, r = Building(), self.recs("L1", "L3")
        self.assertEqual(_reused_drawing(r, "3ST FLOOR PLAN.dwg", lambda *a: None, b), ("L1", "L3"))
        self.assertEqual([x.levels for x in r], [["L3"], ["L3"]])
        self.assertTrue(any("file name says L3" in i.message for i in b.issues))

    def test_a_persons_correction_a_sheet_of_several_plans_and_agreement_stand(self):
        from building import Building
        from building.readset import _reused_drawing
        for r in (self.recs("L1", "L3", levels=["L2"]), self.recs("L1", "L3", split=True),
                  self.recs("L1", "L3", plans=2), self.recs("L1", "L1")):
            self.assertIsNone(_reused_drawing(r, "x.dwg", lambda *a: None, Building()))


class PreBuild(unittest.TestCase):
    def test_the_check_lists_held_elements_and_missing_levels(self):
        from building.prebuild import check, lines
        tmp = tempfile.mkdtemp(prefix="c2r_pre_")
        try:
            rb = os.path.join(tmp, "revit_build", "P_L1")
            os.makedirs(rb)
            with open(os.path.join(rb, "model.json"), "w", encoding="utf-8") as f:
                json.dump({"columns": [{"mark": "C1"}], "beams": [], "slabs": [], "level_name": "Level 1",
                           "export": {"plan": "P:L1"}}, f)
            with open(os.path.join(tmp, "revit_build", "check.json"), "w", encoding="utf-8") as f:
                json.dump({"ready": {"columns": 1}, "needs_decision": [{"where": "Level 1", "what": "beam B7: depth missing"}],
                           "not_supported": [], "founding_missing": 0, "blocked_drawings": []}, f)
            with open(os.path.join(tmp, "revit_build", "levels.json"), "w", encoding="utf-8") as f:
                json.dump([{"revit_name": "Level 1", "elevation_mm": 3150, "status": "read"}], f)
            c = check(tmp)
            self.assertEqual(c["levels_missing"], ["Level 0"])         # the level the columns rise from
            self.assertFalse(c["can_build"])
            self.assertIn("beam B7: depth missing", "\n".join(lines(c)))
            c = check(tmp, {"levels": {"Level 0": 0}})
            self.assertEqual((c["levels_missing"], c["can_build"]), ([], True))
            self.assertEqual([l["source"] for l in c["levels"]], ["decided", "drawings"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=1)
