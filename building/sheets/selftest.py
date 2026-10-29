r"""Tests for the sheet registry (Stage 2).

    python -m building.sheets.selftest

Fast tests only: text rules, the classifier's decisions, and view finding on
small drawings built here. The training drawings themselves are checked by
tools\sheets_baseline.py, which takes a few minutes.
"""
from __future__ import annotations

import os
import tempfile
import unittest

import ezdxf

from building.sheets.classify import classify, is_detail_like, is_plan, mark_family
from building.sheets.levels import (custom_floor, custom_floors_in, custom_key, elevations_in, floor_name, key_elevation,
                                    level_rank, levels_in, title_elevations, wing_in)
from building.sheets.notes import read_notes, summary
from building.sheets.source import TextItem, clean
from building.sheets.views import find_views


def T(s, x=0.0, y=0.0, h=100.0, u=False):
    return TextItem(s, s, h, x, y, "TEXT", "", "model", u)


class Levels(unittest.TestCase):
    CASES = {
        "STR. LAYOUT AT 1st FLOOR LVL": ["L1"],
        "STRUCTURAL LAY-OUT AT SECOND FLOOR LVL.": ["L2"],
        "01_08_2026_1153 - 3ST FLOOR PLAN ( B WING ) 29-07-2026 (6)": ["L3"],   # typo kept
        "08_09_2026_1153 - 4ND FLOOR PLAN ( B WING )dwg": ["L4"],               # typo kept
        "R.C.C. PLAN @ WING B 1ST FLOOR.": ["L1"],
        "FIRST FLOOR FRAMING PLAN": ["L1"],
        "STR. LAYOUT AT TERRACE LVL": ["TERRACE"],
        "STRU. LAYOUT AT GROUND LVL.": ["GF"],
        "PLINTH BEAM LAYOUT": ["PLINTH"],
        "GROUND FLOOR PLINTH BEAM & SLAB LAYOUT": ["GF", "PLINTH"],
        "TYPICAL FLOOR PLAN (2ND TO 5TH FLOOR)": ["L2", "L3", "L4", "L5"],
        "STILT FLOOR PLAN ( A,B & C WING )": ["STILT"],
        "BASEMENT FLOOR PLAN": ["B1"],
        "2ND BASEMENT PLAN": ["B2"],             # not the 2nd floor
        "1ST BASEMENT FLOOR PLAN": ["B1"],       # not the 1st floor
        "MEZZANINE FLOOR PLAN": ["MEZZ"],
        "FOOTING & BASEMENT": ["FDN", "B1"],
        "OHT SLAB": ["OHT"],
        "LMR ROOF": ["MUMTY", "ROOF"],
        "SECTION C-C": [],
        "B4a(230X600)": [],
    }

    def test_titles_reduce_to_level_keys(self):
        for text, want in self.CASES.items():
            with self.subTest(text=text):
                self.assertEqual(levels_in(text), want)

    def test_wing(self):
        self.assertEqual(wing_in("1ST FLOOR PLAN ( B WING )"), "B")
        self.assertEqual(wing_in("STRUCT BOMBAY CONSULTANTS. WING - B"), "B")
        self.assertEqual(wing_in("C-WING"), "C")
        self.assertEqual(wing_in("MAIN BLOCK - SASANGIR"), "MAIN")
        self.assertEqual(wing_in("TOWER 2 PODIUM"), "2")
        self.assertEqual(wing_in("SASANGIR HOTEL"), "")

    def test_elevations(self):
        self.assertEqual(elevations_in("LVL +3.150"), [3150.0])
        self.assertEqual(elevations_in("SFL (+) 6.30 M"), [6300.0])
        self.assertEqual(elevations_in("BOF -1.500"), [-1500.0])
        self.assertEqual(elevations_in("EL +450 MM"), [450.0])

    def test_plan_named_by_its_level_value(self):
        # Goa Haat: "FRAMING PLAN @(+)6600 LVL." names no floor - its level is its value
        for text, want in {"FRAMING PLAN @(+)6600 LVL.": ["EL+6600"], "LAYOUT AT +6.600 LEVEL": ["EL+6600"],
                           "PLAN AT EL. 9.600": ["EL+9600"], "SLAB @ (-)1.200": ["EL-1200"],
                           "FRAMING PLAN": [], "PLAN AT 1:100": [], "8 TOR @ 150 C/C": [], "COLUMN @ 3": []}.items():
            with self.subTest(text=text):
                self.assertEqual(title_elevations(text), want)
        self.assertEqual(key_elevation("EL+12600"), 12600.0)
        self.assertEqual(key_elevation("L3"), None)
        self.assertLess(level_rank("EL+12600"), level_rank("TERRACE"))
        self.assertGreater(level_rank("EL+6600"), level_rank("FDN"))

    def test_floor_added_on_the_floors_page(self):
        # Akanistha's "SERVICE FLOOR LAYOUT PLAN": a floor the app has no name for, added by
        # the person below the ground floor - plans titled with its name are that floor
        k = custom_key("Service floor", False, "GF")
        self.assertEqual(k, "X_SERVICE_FLOOR_UNDER_GF")
        self.assertEqual(custom_floor(k), ("SERVICE FLOOR", False, "GF"))
        self.assertTrue(level_rank("PLINTH") < level_rank(k) < level_rank("GF"))
        self.assertTrue(level_rank("L5") < level_rank(custom_key("Transfer", True, "L5")) < level_rank("L6"))
        for text, hit in {"SERVICE FLOOR LAYOUT PLAN (WINTER VILLA)": True, "SERVICE FLR. PLAN": True,
                          "service-floor beam schedule": True, "SERVICES NOTE": False,
                          "GROUND FLOOR LAYOUT PLAN": False}.items():
            with self.subTest(text=text):
                self.assertEqual(custom_floors_in(text, [k]), [k] if hit else [])
        for known in ("GF", "L3", "GF_LINTEL", "TERRACE", "EL+6600"):
            self.assertIsNone(custom_floor(known))

    def test_added_floor_renamed_finds_its_plans_by_the_new_name(self):
        # test 1: added as "xzzcs", renamed "Service Floor" - the SERVICE FLOOR plan is found;
        # a new name that is a known floor's ("Ground Floor") would take that floor's plan: not used
        k = custom_key("xzzcs", False, "GF")
        self.assertEqual(custom_floors_in("SERVICE FLOOR LAYOUT PLAN", [k]), [])
        self.assertEqual(custom_floors_in("SERVICE FLOOR LAYOUT PLAN", [k], {k: "Service Floor"}), [k])
        self.assertEqual(custom_floors_in("XZZCS PLAN", [k], {k: "Service Floor"}), [k])
        self.assertEqual(custom_floors_in("GROUND FLOOR LAYOUT PLAN", [k], {k: "Ground Floor"}), [])
        self.assertEqual(custom_floors_in("LEVEL 0 PLAN", [k], {k: "Level 0"}), [])

    def test_floor_named_as_the_drawings_write_it(self):
        # the Floors page's "As in the drawings": only words naming that very floor
        for texts, key, want in [(["THIRD FLOOR LAYOUT PLAN (WINTER VILLA)"], "L3", "Third Floor"),
                                 (["TERRACE LEVEL LAYOUT PLAN"], "TERRACE", "Terrace Level"),
                                 (["3ST FLOOR PLAN ( B WING )"], "L3", "3rd Floor"),
                                 (["STRU. LAYOUT AT GROUND LVL."], "GF", "Ground Level"),
                                 (["FRAMING PLAN AT LEVEL 3"], "L3", "Level 3"),
                                 (["FRAMING PLAN @(+)6600 LVL."], "EL+6600", "+6600 Level"),
                                 (["u.g. floor roof"], "L1", "U.G. Floor Roof"),        # Aldott: slab over U.G.
                                 (["SERVICE FLOOR LAYOUT PLAN"], "X_SERVICE_FLOOR_UNDER_GF", "Service Floor"),
                                 (["TYPICAL FLOOR PLAN (2ND TO 5TH FLOOR)"], "L3", ""),   # names several
                                 (["COLUMN LAYOUT", "1ST FLOOR PLAN"], "L1", "1st Floor")]:   # file name next
            with self.subTest(texts=texts):
                self.assertEqual(floor_name(texts, key), want)


class Notes(unittest.TestCase):
    def test_grades_units_cover(self):
        facts = read_notes([T("SLAB SCHEDULE (M25 : FE415)"), T("ALL DIMENSIONS ARE IN MM"),
                            T("CLEAR COVER TO MAIN STEEL 25 MM"), T("CONCRETE M 30 FOR COLUMNS"),
                            T("HYSD Fe500D BARS")])
        s = summary(facts)
        self.assertEqual(set(s["concrete_grade"]), {"M25", "M30"})
        self.assertEqual(set(s["steel_grade"]), {"Fe415", "Fe500D"})
        self.assertEqual(s["units"], ["mm"])
        self.assertEqual(s["cover_mm"], ["25"])

    def test_text_cleaning(self):
        self.assertEqual(clean("%%USECTION: 6a-6a"), "SECTION: 6a-6a")
        self.assertEqual(clean("%%c12 @ 150"), "Ø12 @ 150")


def feats(**kw):
    base = {"texts": 100, "size_captions": 0, "section_titles": 0, "grid_bubbles": 0,
            "element_captions": 0, "beam_marks": 0, "column_marks": 0, "footing_marks": 0,
            "slab_marks": 0, "bar_callouts": 0, "rebar_share": 0.0, "struct_share": 0.0,
            "arch_share": 0.0}
    base.update(kw)
    return base


class Classifier(unittest.TestCase):
    def test_a_plan_without_a_grid_is_still_a_plan(self):
        """Reis Magos draws no grid: marks over structural layers make the plan."""
        f = feats(beam_marks=130, slab_marks=75, column_marks=9, struct_share=0.78, texts=1510)
        self.assertTrue(is_plan(f, (77, 54)))
        v = classify("SECTION FOR BEAMS IN SLOPE FOR CLUB HOUSE", "", "1ST FLOOR PLAN", [], f, (77, 54))
        self.assertEqual(v.kind, "framing_plan")

    def test_a_detail_sheet_full_of_marks_is_not_a_plan(self):
        """Sasan Gir beam details: 36 marks, but reinforcement everywhere."""
        f = feats(beam_marks=36, size_captions=38, bar_callouts=161, texts=510,
                  rebar_share=0.59, struct_share=0.35)
        self.assertTrue(is_detail_like(f))
        self.assertFalse(is_plan(f, (30, 26)))
        self.assertEqual(classify("DETAILS-1", "DETAILS-1", "TERRACE LVL FRAMING & BEAM DETAILS",
                                  [], f, (30, 26)).kind, "beam_details")

    def test_a_gridded_plan_stays_a_plan(self):
        f = feats(grid_bubbles=86, beam_marks=137, size_captions=153, struct_share=0.57,
                  rebar_share=0.12, bar_callouts=15, texts=452)
        self.assertEqual(classify("STRUCTURAL LAY-OUT AT TERRACE LVL.", "FRAMING", "", [], f,
                                  (82, 50)).kind, "framing_plan")

    def test_footing_marks_make_a_foundation_plan(self):
        f = feats(grid_bubbles=52, column_marks=111, footing_marks=142, struct_share=0.15)
        self.assertEqual(classify("DIMENSIONAL PLAN OF RAFT-1", "FDN LAYOUT", "", [], f,
                                  (117, 76)).kind, "foundation_plan")

    def test_a_legend_is_a_legend_whatever_its_size(self):
        f = feats(texts=19, element_captions=19, struct_share=0.05)
        self.assertEqual(classify("100MM SUNK", "", "1ST FLOOR PLAN", [], f, (6, 19)).kind, "legend")

    def test_architectural_sheet(self):
        f = feats(arch_share=0.5)
        self.assertEqual(classify("GROUND FLOOR ELECTRICAL", "ELECTRICAL", "", [], f, (30, 25)).kind,
                         "architectural")

    def test_a_structural_floor_plan_is_not_architectural(self):
        f = feats(beam_marks=100, struct_share=0.7)
        self.assertNotEqual(classify("1ST FLOOR PLAN", "", "", [], f, (60, 40)).kind, "architectural")

    def test_mark_families(self):
        self.assertEqual(mark_family("GB"), "beam")
        self.assertEqual(mark_family("STB"), "beam")
        self.assertEqual(mark_family("F"), "footing")
        self.assertEqual(mark_family("FC"), "column")
        self.assertEqual(mark_family("S"), "slab")

    def test_reasons_are_given(self):
        v = classify("STR. LAYOUT AT 1st FLOOR LVL", "", "", [], feats(), (50, 40))
        self.assertTrue(v.reasons)


class Views(unittest.TestCase):
    @staticmethod
    def drawing(target=(0.0, 0.0), twin=False):
        """Two drawings far apart in model space, each shown by a viewport."""
        doc = ezdxf.new()
        msp = doc.modelspace()
        for ox in (0.0, 100000.0):
            for i in range(10):
                msp.add_line((ox + i * 1000, 0), (ox + i * 1000, 8000))
        lay = doc.layouts.new("SHEET")
        for n, cx in enumerate((4500.0, 104500.0)):
            vp = lay.add_viewport(center=(100 + n * 200, 100), size=(180, 150),
                                  view_center_point=(cx - target[0], 4000 - target[1]),
                                  view_height=10000)
            vp.dxf.view_target_point = (target[0], target[1], 0)
        if twin:
            vp = doc.layouts.new("COPY").add_viewport(center=(100, 100), size=(180, 150),
                                                      view_center_point=(4500 - target[0], 4000 - target[1]),
                                                      view_height=10000)
            vp.dxf.view_target_point = (target[0], target[1], 0)
        return doc

    def test_each_viewport_sees_its_own_drawing(self):
        views = [v for v in find_views(self.drawing()) if v.source == "viewport"]
        self.assertEqual(len(views), 2)
        self.assertEqual(sorted(len(v.handles) for v in views), [10, 10])

    def test_view_target_offset_is_applied(self):
        """Villa 5: every viewport's centre is measured from a view target 367 m away."""
        views = [v for v in find_views(self.drawing(target=(367675.0, 231694.0)))
                 if v.source == "viewport"]
        self.assertEqual(sorted(len(v.handles) for v in views), [10, 10])

    def test_same_view_on_two_layouts_is_marked(self):
        views = [v for v in find_views(self.drawing(twin=True)) if v.source == "viewport"]
        self.assertEqual(sum(1 for v in views if v.same_as), 1)

    def test_no_layouts_means_model_regions(self):
        doc = ezdxf.new()
        msp = doc.modelspace()
        for ox in (0.0, 100000.0):
            for i in range(40):
                msp.add_line((ox + i * 500, 0), (ox + i * 500, 8000))
        msp.add_line((50000, 50000), (50100, 50000))                  # a scrap
        views = find_views(doc)
        self.assertEqual(sorted(v.source for v in views), ["model region", "model region", "stray"])

    def test_two_floors_stacked_in_model_space(self):
        """NIFT 2's "LEVEL-1 & 2" with no paper sheet: the level 1 strip over the level 2
        plan and a section between them touching both - one model region. Each plan keeps
        its own lines only."""
        doc = ezdxf.new()
        msp = doc.modelspace()
        own = {"L1": set(), "L2": set(), "other": set()}

        def line(a, b, key, layer="BEAM"):
            own[key].add(msp.add_line(a, b, dxfattribs={"layer": layer}).dxf.handle)

        for i in range(8):                                            # level 1: a 2-row strip
            line((i * 5000, 20000), (i * 5000, 25000), "L1")
            if i < 7:                                                 # beams span by span
                line((i * 5000, 20000), (i * 5000 + 5000, 20000), "L1")
                line((i * 5000, 25000), (i * 5000 + 5000, 25000), "L1")
        for i in range(8):                                            # level 2: 3 rows
            line((i * 5000, 0), (i * 5000, 5000), "L2")
            line((i * 5000, 5000), (i * 5000, 10000), "L2")
            if i < 7:
                for y in (0, 5000, 10000):
                    line((i * 5000, y), (i * 5000 + 5000, y), "L2")
        for i in range(6):                                            # section A-A between them
            line((12000 + i * 400, 11800), (12000 + i * 400, 16800), "other")
        for s, x, y in (("%%UFRAMING PLAN AT LEVEL 1", 15000, 18600), ("%%UFRAMING PLAN AT LEVEL 2", 15000, -1400),
                        ("%%USECTION A-A", 13000, 11200)):
            msp.add_text(s, height=400, dxfattribs={"insert": (x, y), "layer": "TITLE"})
        views = [v for v in find_views(doc) if v.source != "stray"]
        by_title = {v.title_hint: set(v.handles) for v in views}
        l1 = by_title.get("FRAMING PLAN AT LEVEL 1", set())
        l2 = by_title.get("FRAMING PLAN AT LEVEL 2", set())
        self.assertEqual(own["L1"] & l1, own["L1"])
        self.assertFalse((own["L2"] | own["other"]) & l1)
        self.assertEqual(own["L2"] & l2, own["L2"])
        self.assertFalse((own["L1"] | own["other"]) & l2)

    def test_floor_drawn_twice_side_by_side(self):
        """Pirgal: the framing plan and, beside it in the same region, the same floor with its
        slab bars - the same grid twice. Split; the copy with the bars is the reinforcement.
        A grid labelled at both ends of its lines is not two drawings."""
        def sheet(twice):
            doc = ezdxf.new()
            msp = doc.modelspace()
            for ox in ((0.0, 13500.0) if twice else (0.0,)):
                for i, lab in enumerate("1234"):
                    msp.add_line((ox + i * 4000, 0), (ox + i * 4000, 12000), dxfattribs={"layer": "BEAM"})
                    msp.add_text(lab, height=300, dxfattribs={"insert": (ox + i * 4000, 12800)})
                    if not twice:
                        msp.add_text(lab, height=300, dxfattribs={"insert": (ox + i * 4000, -800)})
                for j, lab in enumerate("ABCD"):
                    msp.add_line((ox, j * 4000), (ox + 12000, j * 4000), dxfattribs={"layer": "BEAM"})
                    msp.add_text(lab, height=300, dxfattribs={"insert": (ox - 800, j * 4000)})
                    if not twice:
                        msp.add_text(lab, height=300, dxfattribs={"insert": (ox + 12800, j * 4000)})
            if twice:
                for k in range(30):
                    msp.add_line((14000 + k * 300, 500), (14000 + k * 300, 11500), dxfattribs={"layer": "SLAB REINF"})
            return doc
        views = [v for v in find_views(sheet(True)) if v.source != "stray"]
        self.assertEqual(sorted(v.twin for v in views), ["plan", "reinforcement"])
        plan = next(v for v in views if v.twin == "plan")
        self.assertLess(plan.window[2], 13500)                      # the left copy, the bars' copy is the other
        self.assertEqual([v.twin for v in find_views(sheet(False)) if v.source != "stray"], [""])

    def test_elevation_is_not_a_plan(self):
        """Floors named one above the other up the drawing: an elevation, however many beam
        marks it carries; a plan titled as a plan keeps its title's kind."""
        from building.sheets.classify import classify, features
        texts = [T(f"B{i}", x=1000 * i, y=500 * (i % 7)) for i in range(30)]
        texts += [T(n, x=-1500, y=3500 * k, h=250) for k, n in enumerate(("STILT FLOOR", "GROUND FLOOR", "FIRST FLOOR",
                                                                             "SECOND FLOOR"))]
        f = features(texts, __import__("collections").Counter({"BEAM": 200}), [])
        self.assertEqual(f["floor_stack"], 4)
        self.assertEqual(classify("S-PR-10", "S-PR-10", "S-PR-12_ELEVATIONS", ["ELEVATION ALONG GRID H"], f,
                                  (30.0, 14.0)).kind, "section")
        self.assertNotEqual(classify("TYP. ISOLATED FOOTING PLAN", "", "FOUNDATION PLAN",
                                     ["TYPICAL COLUMNS ELEVATION"], f, (30.0, 14.0)).kind, "section")

    def test_shear_wall_elevation_with_numbered_levels(self):
        """Akanistha: "SHEAR WALL ELEVATION (SW9 & SW10)" beside the third-floor plan, its grid
        bubbles making it look like a plan - LEVEL 00 ... LEVEL 05 up its side say elevation."""
        from building.sheets.classify import classify, features
        texts = [T(n, x=0, y=3800 * k, h=300) for k, n in enumerate(("LEVEL 00", "LEVEL 01", "LEVEL 03", "LEVEL 04",
                                                                     "LEVEL 05"))]
        f = features(texts, __import__("collections").Counter({"HATCH 1 AVADH": 7800}), [])
        self.assertEqual(f["floor_stack"], 5)
        f = dict(f, grid_bubbles=6)
        self.assertEqual(classify("SHEAR WALL ELEVATION (SW9 & SW10)", "", "Akanistha Structure", [], f,
                                  (27.0, 38.0)).kind, "section")
        # a plan with one "LEVEL 3" note keeps its kind
        g = dict(features([T("LEVEL 3", x=0, y=0, h=300)], __import__("collections").Counter({"BEAM": 200}), []),
                 grid_bubbles=6)
        self.assertEqual(g["floor_stack"], 1)

    def test_plan_kept_as_a_block_is_not_a_scrap(self):
        """AKANISTHA terrace: the floor plan is one block insert with a few texts beside it -
        a drawing, read like the other floors; a small block alone is still a scrap."""
        doc = ezdxf.new()
        msp = doc.modelspace()
        for i in range(40):                                           # another floor, drawn loose
            msp.add_line((i * 500, 0), (i * 500, 8000))
        plan = doc.blocks.new("TERRACE")
        for i in range(40):
            plan.add_line((i * 500, 0), (i * 500, 8000))
        msp.add_blockref("TERRACE", (100000, 0))
        msp.add_text("TERRACE LEVEL LAYOUT PLAN", height=300, dxfattribs={"insert": (100000, -1000)})
        arrow = doc.blocks.new("NORTH")
        for i in range(6):
            arrow.add_line((0, 0), (300, i * 50))
        msp.add_blockref("NORTH", (50000, 50000))
        views = find_views(doc)
        self.assertEqual(sorted(v.source for v in views), ["model region", "model region", "stray"])

    def test_section_kept_in_blocks_is_read_as_a_section_and_ids_stay(self):
        """Winter Villa's "SECTION 1": a few texts and inserts on top, ~800 entities in its
        blocks - read, under the id it had as a scrap, and only ever as a section. A small
        block with a section-looking text, and a scrap with no such title, stay scraps."""
        from building.sheets.registry import register
        doc = ezdxf.new()
        msp = doc.modelspace()
        for i in range(40):                                           # a floor, drawn loose
            msp.add_line((i * 500, 0), (i * 500, 8000), dxfattribs={"layer": "BEAM"})
        cut = doc.blocks.new("SEC1")
        for i in range(60):
            cut.add_line((0, i * 100), (9000, i * 100))
        msp.add_blockref("SEC1", (100000, 0))
        msp.add_text("SECTION 1", height=300, dxfattribs={"insert": (100000, -1000)})
        logo = doc.blocks.new("LOGO")
        for i in range(60):
            logo.add_line((0, 0), (300, i * 5))
        msp.add_blockref("LOGO", (200000, 0))                         # a big block, no title
        tiny = doc.blocks.new("TINY")
        for i in range(3):
            tiny.add_line((0, 0), (300, i * 50))
        msp.add_blockref("TINY", (300000, 0))
        msp.add_text("SECTION 2", height=300, dxfattribs={"insert": (300000, -1000)})
        before = {v.id: v.source for v in find_views(doc)}
        held = [v for v in find_views(doc) if v.inside_blocks]
        self.assertEqual(len(held), 1)
        self.assertTrue(held[0].id.startswith("S"))                  # the id it had as a scrap
        self.assertEqual(sorted(before.values()), ["model region", "model region", "stray", "stray"])
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "sections.dxf")
            doc.saveas(p)
            recs = {r.view.id: r for r in register(p, d)}
        self.assertEqual(recs[held[0].id].verdict.kind, "section")
        self.assertEqual(sorted(r.view.id for r in recs.values()), sorted(before))

    @staticmethod
    def _plan_with_sections(section_layer="S-BEAM", schedule=False):
        """New block's sheet in small: a 2nd-floor plan, its title under it, and
        1.5 m below that a strip of beam sections under their cut name NB6:NB6."""
        doc = ezdxf.new()
        msp = doc.modelspace()
        for i in range(12):                                           # the plan's beams
            msp.add_line((0, i * 1500), (15000, i * 1500), dxfattribs={"layer": "BEAM-230X600"})
            msp.add_line((i * 1300, 0), (i * 1300, 16500), dxfattribs={"layer": "BEAM-230X600"})
        msp.add_text("%%USTRUCTURAL LAY-OUT AT SECOND FLOOR LVL.", height=450,
                     dxfattribs={"layer": "S- Text", "insert": (0, -1500)})
        for i in range(8):                                            # the sections' beams
            msp.add_line((i * 1800, -4000), (i * 1800 + 1200, -4000), dxfattribs={"layer": section_layer})
            msp.add_line((i * 1800, -4600), (i * 1800 + 1200, -4600), dxfattribs={"layer": section_layer})
            msp.add_text("3-T16", height=100, dxfattribs={"layer": "STEEL TEXT", "insert": (i * 1800, -4800)})
        msp.add_text("%%UNB6:NB6", height=150, dxfattribs={"layer": "S-L.S. NO", "insert": (6000, -5200)})
        if schedule:
            for i in range(6):
                msp.add_line((0, -4000 - i * 300), (6000, -4000 - i * 300), dxfattribs={"layer": "TABLE"})
            msp.add_text("%%USTEEL SCHEDULE", height=250, dxfattribs={"layer": "TXT", "insert": (0, -3700)})
        return doc

    def test_one_floors_plan_and_its_member_sections_are_two_views(self):
        """New block: the sections' beams were built as plan beams. They are a view of
        their own now, and the plan keeps its title."""
        views = find_views(self._plan_with_sections())
        self.assertEqual(len(views), 2)
        plan = next(v for v in views if "LAY-OUT" in v.title_hint)
        self.assertLess(plan.window[1], -1000)                        # its title under it
        self.assertGreater(plan.window[1], -3000)                     # but no section
        self.assertTrue(any(v.title_hint == "NB6:NB6" for v in views))

    def test_a_slab_mark_inside_the_plan_stays_with_the_plan(self):
        """ALCAZAR 1st floor: the beam schedule is titled right under the plan's upper
        wing; the slab marks in that wing's rooms (S1 with its oval frames on 'SLAB MARK')
        went to that title - taken for a section and cut out of the plan."""
        doc = ezdxf.new()
        msp = doc.modelspace()
        lines = [(0, 0, 20000, 0), (0, 16500, 20000, 16500), (0, 0, 0, 16500), (20000, 0, 20000, 16500),
                 (10000, 0, 10000, 16500), (0, 8000, 10000, 8000)]
        lines += [(10000 + i * 1250, 0, 10000 + i * 1250, 16500) for i in range(1, 8)]   # the right half's beams
        for x0, y0, x1, y1 in lines:                                  # beams span bay by bay
            n = max(1, int(max(abs(x1 - x0), abs(y1 - y0)) // 2000))
            for k in range(n):
                msp.add_line((x0 + (x1 - x0) * k / n, y0 + (y1 - y0) * k / n),
                             (x0 + (x1 - x0) * (k + 1) / n, y0 + (y1 - y0) * (k + 1) / n), dxfattribs={"layer": "BEAM"})
        mark = msp.add_text("S1", height=250, dxfattribs={"layer": "SLAB NO", "insert": (5000, 12000)})
        for k in range(6):                                            # oval frames round slab marks
            msp.add_lwpolyline([(4700 + k * 10, 11900), (5400, 11900), (5400, 12300), (4700 + k * 10, 12300)],
                               close=True, dxfattribs={"layer": "SLAB MARK"})
        msp.add_text("%%UR.C.C. PLAN @ 1ST FLOOR LEVEL", height=450, dxfattribs={"layer": "TXT", "insert": (12000, 500)})
        msp.add_text("%%USCHEDULE OF FLOOR BEAMS:-", height=300, dxfattribs={"layer": "TXT", "insert": (2000, -1000)})
        views = find_views(doc)
        owner = next(v for v in views if mark.dxf.handle in v.handles)
        self.assertIn("PLAN", owner.title_hint or "PLAN")

    def test_one_floors_plan_with_only_a_schedule_reads_as_before(self):
        """A schedule beside the plan draws no members: one region, as it always was."""
        views = find_views(self._plan_with_sections(section_layer="TABLE", schedule=True))
        self.assertEqual(len(views), 1)


class BlockTexts(unittest.TestCase):
    def test_a_text_in_a_block_knows_the_outline_drawn_beside_it(self):
        """New block: 'SC1' (layer 0) sits beside a 300x550 post on 'Steel Col', both
        in one block. The text names that post, not the RCC column nearby."""
        from building.sheets.source import texts_in
        doc = ezdxf.new()
        blk = doc.blocks.new("POSTS")
        blk.add_lwpolyline([(0, 0), (300, 0), (300, 550), (0, 550)], close=True,
                           dxfattribs={"layer": "Steel Col"})
        blk.add_text("SC1", height=250, dxfattribs={"layer": "0", "insert": (650, 300)})
        msp = doc.modelspace()
        msp.add_blockref("POSTS", (10000, 20000), dxfattribs={"layer": "STEEL"})
        msp.add_text("C1", height=250, dxfattribs={"layer": "TEXT", "insert": (0, 0)})
        got = {t.text: t for t in texts_in(msp, "model")}
        self.assertEqual(got["SC1"].block_outline, [10150.0, 20275.0, "Steel Col"])
        self.assertIsNone(got["C1"].block_outline)

    def test_an_attribute_definition_on_the_plan_reads_as_its_tag(self):
        """NIFT: beam marks 4B3, 4B6A placed on the plan as attribute definitions -
        AutoCAD shows the tag. Ones in blocks or on paper-space sheets stay as they are."""
        import os
        import tempfile
        from building.sheets.source import has_loose_attdefs, in_mm, show_attdefs, texts_in
        doc = ezdxf.new()
        doc.header["$INSUNITS"] = 4
        msp = doc.modelspace()
        msp.add_attdef("4B6A", (1000, 2000), dxfattribs={"layer": "TEXT", "height": 250})
        blk = doc.blocks.new("TB")
        blk.add_attdef("NAME", (0, 0), dxfattribs={"height": 100})
        doc.layouts.get("Layout1").add_attdef("SIGNATURE", (0, 0), dxfattribs={"height": 100})
        msp.add_text("C1", height=250, dxfattribs={"layer": "TEXT", "insert": (0, 0)})
        d = tempfile.mkdtemp()
        path = os.path.join(d, "plan.dxf")
        doc.saveas(path)
        self.assertTrue(has_loose_attdefs(path))
        src, _note = in_mm(path, d)
        self.assertNotEqual(src, path)
        got = {t.text: t for t in texts_in(ezdxf.readfile(src).modelspace(), "model")}
        self.assertEqual((got["4B6A"].x, got["4B6A"].y, got["4B6A"].layer, got["4B6A"].height),
                         (1000.0, 2000.0, "TEXT", 250.0))
        out = ezdxf.readfile(src)
        self.assertEqual(len(out.blocks.get("TB").query("ATTDEF")), 1)
        self.assertEqual(len(out.layouts.get("Layout1").query("ATTDEF")), 1)
        self.assertEqual(show_attdefs(out), 0)
        # a drawing without any is read from where it is, as before
        plain = ezdxf.new()
        plain.header["$INSUNITS"] = 4
        plain.modelspace().add_text("C1", height=250)
        p2 = os.path.join(d, "plain.dxf")
        plain.saveas(p2)
        self.assertFalse(has_loose_attdefs(p2))
        self.assertEqual(in_mm(p2, d)[0], p2)


if __name__ == "__main__":
    unittest.main(verbosity=1)
