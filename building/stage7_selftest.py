r"""Tests for Stage 7: level markers, floor heights, the level table.

    python -m building.stage7_selftest
"""
from __future__ import annotations

import unittest
from collections import Counter
from types import SimpleNamespace

import ezdxf

from building import Building, Sheet
from building.linker import link
from building.readers import elevations
from building.readers.elevations import values_in
from building.sheets.source import TextItem


def T(s, x, y, h=200.0, layer="TXT"):
    return TextItem(s, s, h, x, y, layer, f"{s}@{x:.0f},{y:.0f}", "model", False, x, y)


def view(texts, kind="section", rid="S:V", levels=()):
    return SimpleNamespace(id=rid, verdict=SimpleNamespace(kind=kind), texts=texts, doc=ezdxf.new(),
                           layers=Counter(), levels=list(levels), wing="", title="",
                           view=SimpleNamespace(handles=[], same_as="", source="viewport"))


def setup(texts, kind="section", rid="S:V", levels=(), set_key="P"):
    b = Building()
    b.add_sheet(Sheet(id=rid, file="x", reader="sheets.registry", extra={"set": set_key}))
    got = elevations.read(b, view(texts, kind, rid, levels))
    return b, got


def table(b):
    return {e.level: e for e in b.of("level")}


class Mentions(unittest.TestCase):
    def test_schedule_rows_are_mentions_and_lone_plan_tags_are_not(self):
        from building.readers.labels import mentions
        row = [T("B", 0, 0), T("-", 300, 0), T("66,83", 600, 0), T("230", 3000, 0), T("600", 4000, 0)]
        tags = [T("B175", 0, 5000), T("B176", 90000, 5000), T("230X600", 0, 4700)]
        got = mentions(None if False else type("R", (), {"id": "V"})(), row + tags, plan=True)
        self.assertEqual(sorted(got), ["B66", "B83"])


class Values(unittest.TestCase):
    def test_how_levels_are_written(self):
        for text, want in (("FIRST FLOOR LVL +3.150", [3150]), ("TERRACE LVL. (+) 12.600", [12600]),
                           ("MIDLANDING UFL. LVL. (-)1910", [-1910]), ("+ 600 LVL", [600]),
                           ("MIDLANDING UFL. LVL. (+) 00", [0]), ("BT@ +0.200M", [200]),
                           ("SFL +6.300", [6300]), ("-1.500", [-1500]),
                           ("MIDLANDING UFL. LVL. +1440", [1440]), ("MIDLANDING LVL -1760MM", [-1760]),
                           ("CIR 1 LVL B BLOCK +9500", [9500]), ("Stilt LVL B BLOCK +6500", [6500]),
                           ("Basement LVL B BLOCK", [])):
            self.assertEqual(values_in(text), want, text)

    def test_marks_bars_depths_and_dates_are_not_levels(self):
        for text in ("B-39", "GB-1", "750+300", "T8@150 C/C", "3-T16+2-T12", "DATE:- 30/07/2026",
                     "C-506/507", "SECTION - 3-3", "T12-100mm C/C", "4-T20+10-T16"):
            self.assertEqual(values_in(text), [], text)


class Markers(unittest.TestCase):
    SECTION = [T("GROUND FLOOR LVL", 0, 0), T("±0.000", 3000, 0),
               T("FIRST FLOOR LVL", 0, 3150), T("+3.150", 3000, 3150),
               T("SECOND FLOOR SLAB LVL +6.300", 0, 6300),
               T("TERRACE LVL. (+) 9.450", 0, 9450)]

    def test_a_section_gives_the_level_table(self):
        b, got = setup(self.SECTION)
        self.assertEqual(got, {"level marks": 4})
        link(b)
        t = table(b)
        self.assertEqual({k: e.final("elevation") for k, e in t.items()},
                         {"GF": 0, "L1": 3150, "L2": 6300, "TERRACE": 9450})
        self.assertEqual({k: e.final("height_below") for k, e in t.items()},
                         {"GF": None, "L1": 3150, "L2": 3150, "TERRACE": 3150})
        self.assertEqual(t["L2"].resolved["elevation"].status, "read")

    def test_a_marker_off_the_sections_scale_is_not_used(self):
        texts = self.SECTION[:4] + [T("SECOND FLOOR SLAB LVL +6.300", 0, 7400), T("TERRACE LVL. (+) 9.450", 0, 9450)]
        b, _ = setup(texts)
        link(b)
        self.assertEqual(table(b)["L2"].resolved["elevation"].status, "missing")
        self.assertTrue(any("not where the other markers" in i.message for i in b.issues))

    def test_two_sheets_disagreeing_is_a_conflict(self):
        b, _ = setup([T("FIRST FLOOR LVL +3.150", 0, 0)], rid="A")
        b.add_sheet(Sheet(id="B", file="y", reader="sheets.registry", extra={"set": "P"}))
        elevations.read(b, view([T("FIRST FLOOR LVL +3.300", 0, 0)], rid="B"))
        link(b)
        self.assertEqual(table(b)["L1"].resolved["elevation"].status, "conflict")

    def test_landings_beams_and_details_stay_out_of_the_table(self):
        b, _ = setup([T("MIDLANDING UFL. LVL. (+)1440", 0, 0), T("BT@ +0.200M", 0, 900),
                      T("+ 600 LVL", 0, 2000), T("+ 000 LVL", 0, 2600), T("B-39", 0, 3000)])
        kinds = sorted(m.get("kind") for m in b.of("level_mark"))
        self.assertEqual(kinds, ["beam", "landing", "local", "local"])
        link(b)
        self.assertEqual(table(b), {})

    def test_finished_and_structural_levels_are_kept_apart(self):
        b, _ = setup([T("FIRST FLOOR FFL +3.150", 0, 3150), T("FIRST FLOOR SFL +3.100", 4000, 3100)])
        link(b)
        e = table(b)["L1"]
        self.assertEqual((e.final("elevation"), e.final("elevation_finished")), (3100, 3150))

    def test_a_height_note_is_shown_not_turned_into_levels(self):
        """J1153 2nd floor plan: HEIGHT REVISED TO 3150MM."""
        b, got = setup([T("HEIGHT REVISED TO 3150MM", 0, 0), T("150MM HEIGHT INCREASED", 0, 500)],
                       kind="framing_plan", levels=["L2"])
        self.assertEqual(got, {"height notes": 1})
        b.add_sheet(Sheet(id="S:V/plan", file="x", reader="plan.legacy", extra={"set": "P", "level_key": "L2"}))
        link(b)
        e = table(b)["L2"]
        self.assertEqual((e.final("height_note"), e.resolved["elevation"].status), ([3150], "missing"))


class WallElevations(unittest.TestCase):
    """Winter Villa, SHEAR WALL ELEVATION (SW15): SW15 on its raft 1325 above the lowest level
    line (left unnamed), up to LEVEL 02; its left face broken at the LEVEL 01 slab."""

    def rec(self, title="SHEAR WALL ELEVATION (SW15)"):
        from building.readers import wall_elevations
        doc = ezdxf.new()
        msp = doc.modelspace()
        for y in (0, 2800, 6700, 10600):
            msp.add_line((0, y), (10000, y), dxfattribs={"layer": "GRID LINE"})
        for s, y in (("LEVEL 00", 2800), ("LEVEL 01", 6700), ("LEVEL 02", 10600)):
            msp.add_text(s, height=150, dxfattribs={"layer": "TEXT", "insert": (500, y + 100)})
        for s, y in (("2800", 1400), ("3900", 4750)):
            msp.add_text(s, height=150, dxfattribs={"layer": "DIM", "insert": (9000, y)})
        msp.add_line((5000, 1325), (5000, 6700), dxfattribs={"layer": "COLUMN"})
        msp.add_line((5000, 6850), (5000, 10600), dxfattribs={"layer": "COLUMN"})
        msp.add_line((6250, 1325), (6250, 10600), dxfattribs={"layer": "COLUMN"})
        msp.add_line((3000, 1325), (6250, 1325), dxfattribs={"layer": "COVER"})
        msp.add_line((3000, 725), (6250, 725), dxfattribs={"layer": "COVER"})
        b = Building()
        b.add_sheet(Sheet(id="E:V", file="x", reader="sheets.registry", title=title, extra={"set": "P"}))
        r = SimpleNamespace(id="E:V", verdict=SimpleNamespace(kind="section"), doc=doc, title="",
                            view=SimpleNamespace(handles=[e.dxf.handle for e in msp], same_as="", source="model region"))
        return b, wall_elevations.read(b, r)

    def test_levels_walls_and_the_raft_under_them_are_read(self):
        b, n = self.rec()
        self.assertEqual(n, 1)
        we = b.sheets["E:V"].extra["wall_elevation"]
        self.assertEqual([(lv["name"], lv["y"]) for lv in we["levels"]],
                         [("", 0), ("00", 2800), ("01", 6700), ("02", 10600)])
        self.assertEqual(we["scale"], 1.0)
        w = we["walls"][0]
        self.assertEqual((w["marks"], w["base"], w["top"], w["shaped_at"]), (["SW15"], 1325, 10600, []))
        self.assertEqual((w["foundation"]["top"], w["foundation"]["bottom"]), (1325, 725))

    def test_one_wall_drawn_for_every_mark_the_title_names(self):
        """Winter Villa, SHEAR WALL ELEVATION (SW15 , SA14 , SW13 ,C12 ): one wall drawn, no label -
        all four stand so (SA14 a typo the linker matches to the plans)."""
        b, n = self.rec("SHEAR WALL ELEVATION (SW15 , SA14 , SW13 ,C12 )")
        self.assertEqual(n, 4)
        walls = b.sheets["E:V"].extra["wall_elevation"]["walls"]
        self.assertEqual([w["marks"] for w in walls], [["SW15"], ["SW13"], ["SA14"], ["C12"]])
        self.assertEqual({(w["base"], w["top"]) for w in walls}, {(1325, 10600)})

    def test_a_mistyped_mark_is_matched_to_the_plans(self):
        from building.linker import _wall_heights
        b = self.building()
        b.sheets["E:V"].extra["wall_elevation"]["walls"][0]["marks"] = ["SA1"]
        _wall_heights(b, Counter())
        el = {e.id: e for e in b.elements}
        self.assertEqual(el["P:L1/plan/wall/0"].resolved["base_mm"].value, 1000.0)
        self.assertTrue(any("names SA1, which no plan carries - read as SW1" in i.message for i in b.issues))

    def test_a_plan_view_is_not_read(self):
        from building.readers import wall_elevations
        b = Building()
        b.add_sheet(Sheet(id="P:V", file="x", title="SHEAR WALL LAYOUT PLAN SW1"))
        r = SimpleNamespace(id="P:V", verdict=SimpleNamespace(kind="framing_plan"), doc=ezdxf.new(), title="",
                            view=SimpleNamespace(handles=[], same_as="", source="model region"))
        self.assertEqual(wall_elevations.read(b, r), 0)

    def building(self, levels_y=(0, 3000, 6000)):
        """Floors GF, L1, L2; SW1 drawn from 1000 (on raft RF1) to L1 (3000). The foundation
        plan F places C1 on RF1; C2, C3 stand off it."""
        from building.model import Element, Value
        b = Building()
        b.add_sheet(Sheet(id="E:V", file="x", reader="sheets.registry", extra={"set": "P", "wall_elevation": {
            "title": "SHEAR WALL ELEVATION (SW1)", "scale": 1.0,
            "levels": [{"name": n, "y": y} for n, y in zip(("00", "01", "02"), levels_y)],
            "walls": [{"marks": ["SW1"], "base": 1000, "top": 2850, "x0": 0, "x1": 1500, "shaped_at": [],
                       "foundation": {"top": 1000, "bottom": 400, "x0": -500, "x1": 2000}}]}}))
        b.add_sheet(Sheet(id="F:V", file="x", reader="sheets.registry", extra={"set": "P"}))
        b.add(Element(id="F:V/raft/0", type="raft", sheet="F:V", mark="RF1",
                      geometry={"outline": [[-1000, -1000], [3000, -1000], [3000, 3000], [-1000, 3000]]}))
        b.add(Element(id="F:V/cp/1", type="column_position", sheet="F:V", mark="SW1", geometry={"x": 500, "y": 500}))
        cols = {"C1": (2000, 2000), "C2": (9000, 0), "C3": (9000, 9000)}
        for m, (x, y) in cols.items():
            b.add(Element(id=f"F:V/caption/{m}", type="caption", sheet="F:V", mark=m,
                          props={"family": Value("column")}, geometry={"at": [x, y]}))
        for k in ("GF", "L1", "L2"):
            sid = f"P:{k}/plan"
            b.add_sheet(Sheet(id=sid, file="x", reader="plan.legacy", level=k, extra={"set": "P", "level_key": k}))
            b.add(Element(id=f"{sid}/wall/0", type="wall", sheet=sid, mark="SW1",
                          geometry={"outline": [[0, 400], [1500, 400], [1500, 700], [0, 700]]}))
            for m, (x, y) in cols.items():
                b.add(Element(id=f"{sid}/column/{m}", type="column", sheet=sid, mark=m,
                              props={"mark": Value(m)}, geometry={"x": x + 100000, "y": y}))
        return b

    def test_bases_from_the_elevation_and_from_the_raft_under_it(self):
        from building.linker import _wall_heights
        b = self.building()
        _wall_heights(b, Counter())
        el = {e.id: e for e in b.elements}

        def state(eid):
            e = el[eid]
            if "scope" in e.resolved:
                return e.resolved["scope"].value.split(" - ")[0]
            return {k: e.resolved[k].value for k in ("base_mm", "top_mm") if k in e.resolved}
        # GF: its storey lies under SW1's base and RF1's top - not built; off the raft, as before
        self.assertEqual(state("P:GF/plan/wall/0"), "other: under its base")
        self.assertEqual(state("P:GF/plan/column/C1"), "other: under its base")
        self.assertEqual(state("P:GF/plan/column/C2"), {})
        # L1: SW1 and what stands on RF1 start 1000 up; SW1 ends at L1 (within a slab's depth)
        self.assertEqual(state("P:L1/plan/wall/0"), {"base_mm": 1000.0})
        self.assertEqual(state("P:L1/plan/column/C1"), {"base_mm": 1000.0})
        self.assertEqual(state("P:L1/plan/column/C3"), {})
        # L2: SW1 ended at L1
        self.assertEqual(state("P:L2/plan/wall/0"), "other: over its top")
        self.assertEqual(state("P:L2/plan/column/C1"), {})
        self.assertTrue(any(i.category == "scope" and "Choose 'this block'" in i.message for i in b.issues))
        # run again (the review re-links): the same, nothing doubled
        _wall_heights(b, Counter())
        self.assertEqual(state("P:L1/plan/column/C1"), {"base_mm": 1000.0})
        self.assertEqual(state("P:GF/plan/wall/0"), "other: under its base")

    def test_a_wall_drawn_to_the_lowest_level_line_carries_on_below_it(self):
        """SW9 / SW10 drawn from LEVEL -01, the elevation's lowest line: it shows nothing below, so the
        plan below that draws them (the raft lower, at the foundation level) builds them."""
        from building.linker import _wall_heights
        b = self.building()
        we = b.sheets["E:V"].extra["wall_elevation"]
        we["walls"][0].update(base=0, foundation=None)
        _wall_heights(b, Counter())
        gf = next(e for e in b.elements if e.id == "P:GF/plan/wall/0")
        self.assertNotIn("scope", gf.resolved)
        self.assertNotIn("base_mm", gf.resolved)

    def test_a_decided_foundation_level_wins_over_the_slab_the_elevation_draws(self):
        """Winter Villa: Foundation -1200 on the Floors page; the 600 slab drawn under SW9 is the floor's."""
        from building.linker import _wall_heights
        from building.model import OVERRIDE, Value
        b = self.building()
        raft = next(e for e in b.elements if e.type == "raft")
        raft.resolved["founding_level"] = Value(-1200.0, OVERRIDE)
        _wall_heights(b, Counter())
        el = {e.id: e for e in b.elements}
        self.assertEqual(raft.resolved["founding_level"].value, -1200.0)
        self.assertNotIn("scope", el["P:GF/plan/column/C1"].resolved)        # nothing started on that slab
        self.assertNotIn("base_mm", el["P:L1/plan/column/C1"].resolved)
        # SW1 drawn on that slab carries on down to the raft, as the plans draw it - not left
        # standing on a slab nothing builds (user 2026-10-28: "columns floating ... start from foundation")
        self.assertNotIn("base_mm", el["P:L1/plan/wall/0"].resolved)
        self.assertNotIn("scope", el["P:GF/plan/wall/0"].resolved)

    def test_storey_heights_that_disagree_take_nothing(self):
        from building.linker import _match_levels
        we = {"levels": [{"name": "00", "y": 0}, {"name": "01", "y": 3000}], "scale": 1.0}
        self.assertEqual(_match_levels(we, ["GF", "L1"], {})[0], ["GF", "L1"])
        self.assertEqual(_match_levels(we, ["GF", "L1"], {"GF": 0.0, "L1": 3000.0})[0], ["GF", "L1"])
        self.assertIsNone(_match_levels(we, ["GF", "L1"], {"GF": 0.0, "L1": 3600.0})[0])
        self.assertIsNone(_match_levels(we, ["GF", "L1", "L2"], {})[0])
        self.assertEqual(_match_levels(we, ["GF", "L1", "L2"], {"GF": 0.0, "L1": 3000.0, "L2": 6000.0})[0],
                         ["GF", "L1"])

    def test_a_review_decision_is_left_as_it_is(self):
        from building.linker import _wall_heights
        from building.model import OVERRIDE, Value
        b = self.building()
        e = next(x for x in b.elements if x.id == "P:GF/plan/column/C1")
        e.resolved["scope"] = Value("this block", OVERRIDE)
        _wall_heights(b, Counter())
        self.assertEqual(e.resolved["scope"].value, "this block")

    def lean(self, l2_start=450.0):
        """SW1 straight from GF to L1 (x 0..1000), then leaning 500 by L2; each plan draws it
        where it stands just under its slab."""
        from building.model import Element
        b = Building()
        keys = ["GF", "L1", "L2"]
        runs = {"GF": (0.0, 1000.0), "L1": (0.0, 1000.0), "L2": (l2_start, l2_start + 1000.0)}
        for k in keys:
            b.add_sheet(Sheet(id=f"P:{k}/plan", file="x", reader="plan.legacy", level=k, extra={"set": "P", "level_key": k}))
            x0, x1 = runs[k]
            b.add(Element(id=f"P:{k}/plan/wall/0", type="wall", sheet=f"P:{k}/plan", mark="SW1",
                          geometry={"outline": [[x0, -150], [x1, -150], [x1, 150], [x0, 150]]},
                          extra={"runs": [{"start": [x0, 0.0], "end": [x1, 0.0], "thickness": 300.0}]}))
        w = {"outline": [[0, 0], [1000, 0], [1000, 3000], [1500, 6000], [500, 6000], [0, 3000]]}
        floor_of = {f"P:{k}/plan": i for i, k in enumerate(keys)}
        shifts = {f"P:{k}/plan": (0.0, 0.0) for k in keys}
        return b, w, keys, floor_of, shifts

    def test_a_leaning_wall_is_built_to_its_shape(self):
        from building.linker import _profile_walls
        b, w, keys, floor_of, shifts = self.lean()
        why, fit = _profile_walls(b, w, "SW1", [2], [0, 3000, 6000], 1.0, keys, floor_of, b.elements, shifts, "E")
        self.assertEqual(why, {2: ""})
        pw = b.elements[2].extra["profile_wall"]
        self.assertEqual((pw["origin"], pw["dir"], pw["thickness"]), ([450.0, 0.0], [1.0, 0.0], 300.0))
        # at L2's level exactly where the L2 plan draws it (0..1000 from its start: its beams meet it
        # there), at L1's where the L1 plan does (-450..550); the elevation's 50 mm spread between
        self.assertEqual(sorted(map(tuple, pw["points"])), [(-450.0, 0.0), (0.0, 3000.0), (550.0, 0.0), (1000.0, 3000.0)])
        self.assertNotIn("profile_wall", b.elements[1].extra)          # the straight storey stays as it is

    def test_a_shaped_walls_top_under_the_slab_goes_up_to_its_level(self):
        """SW15 / SW14 at the Terrace: drawn 175 under LEVEL 05 (the slab's underside) - built up to
        it, as a straight wall's top is; 400 under is its own top."""
        from building.linker import _up_to_level
        pts = [[0, 0], [1250, 0], [1300, 3725], [50, 3725]]
        self.assertEqual(_up_to_level(pts, 3900.0), [[0, 0], [1250, 0], [1300, 3900.0], [50, 3900.0]])
        self.assertEqual(_up_to_level(pts, 4125.0), pts)

    def test_plans_far_from_the_elevation_leave_it_straight(self):
        from building.linker import _profile_walls
        b, w, keys, floor_of, shifts = self.lean(l2_start=3000.0)
        why, _fit = _profile_walls(b, w, "SW1", [2], [0, 3000, 6000], 1.0, keys, floor_of, b.elements, shifts, "E")
        self.assertIn("from where the elevation puts it", why[2])
        self.assertNotIn("profile_wall", b.elements[2].extra)

    def test_a_column_beside_a_leaning_wall_leans_as_the_section_draws_it(self):
        """C12 in SHEAR WALL ELEVATION (SW15 , SA14 , SW13 ,C12 ): from where the plan under its lean
        draws it, up to where the plan above draws it (its beams there) - the plans 400 mm off the
        section's lean, said as a warning."""
        from building.linker import _section_lean
        from building.model import Element, Value
        b, w, keys, floor_of, shifts = self.lean()
        for k, x in (("GF", 5000.0), ("L1", 5000.0), ("L2", 5100.0)):
            b.add(Element(id=f"P:{k}/plan/column/C1", type="column", sheet=f"P:{k}/plan", mark="C1",
                          props={"mark": Value("C1")}, geometry={"x": x, "y": 0.0}))
        why, fit = _section_lean(b, w, "C1", [2], [0, 3000, 6000], 1.0, keys, floor_of, b.elements, shifts, "E",
                                 (1.0, 0.0))
        self.assertEqual(why, {2: ""})
        ln = next(e for e in b.elements if e.id == "P:L2/plan/column/C1").extra["lean"]
        self.assertEqual((ln["base"], ln["top"]), ([-100.0, 0.0], [0.0, 0.0]))
        self.assertIn("a slanted column", fit)
        self.assertTrue(any(i.severity == "warning" and "L2 plan draws it 400 mm" in i.message for i in b.issues))
        # no wall showing which way the elevation faces: left upright, said
        why, _fit = _section_lean(b, w, "C1", [2], [0, 3000, 6000], 1.0, keys, floor_of, b.elements, shifts, "E", None)
        self.assertIn("which way it faces", why[2])

    def test_a_column_ending_here_is_understood_however_it_is_worded(self):
        from building.linker import COLUMN_ENDS, COLUMN_STARTS
        from building.readers.legend_zones import kind_of
        for layer in ("COLUMN STOP", "STOP COLUMN", "COL_STOP", "S-COL-STOP", "COLUMN TERMINATES", "COLUMN_ENDS",
                      "COL ENDING", "COL UPTO THIS LVL", "DISCONTINUED COL"):
            self.assertTrue(COLUMN_ENDS.search(layer), layer)
        for layer in ("COLUMN AVADH", "SC", "BACKSTOP", "LEGEND", "WEEKEND COL"):
            self.assertFalse(COLUMN_ENDS.search(layer), layer)          # "SC" alone: stub? steel? stop? - never read
        self.assertTrue(COLUMN_STARTS.search("STUB COLUMN"))
        for text in ("COLUMN TERMINATES AT THIS LEVEL", "COLUMN STOPS HERE", "COLUMN UP TO THIS LEVEL", "RCC COLUMN ENDS"):
            self.assertEqual(kind_of(text), "column ends", text)
        self.assertEqual(kind_of("RCC COLUMN THROUGH"), "column through")

    def test_columns_on_the_column_stop_layer_are_said(self):
        from building.linker import _column_stops
        from building.model import Element, Value
        b = Building()
        for k in ("L1", "L2"):
            b.add_sheet(Sheet(id=f"P:{k}/plan", file="x", reader="plan.legacy", level=k,
                              extra={"set": "P", "level_key": k}))
        b.add(Element(id="a", type="column", sheet="P:L1/plan", mark="C7", props={"mark": Value("C7")},
                      geometry={"x": 0, "y": 0}, extra={"_layer": "COLUMN STOP"}))
        _column_stops(b, Counter())
        self.assertTrue(b.elements[0].extra.get("stops_here"))
        self.assertEqual([(i.severity, i.category) for i in b.issues], [("info", "column stop")])
        b.issues.clear()
        b.add(Element(id="c", type="column", sheet="P:L2/plan", mark="C7", props={"mark": Value("C7")},
                      geometry={"x": 0, "y": 0}))
        _column_stops(b, Counter())
        self.assertEqual([(i.severity, i.category) for i in b.issues], [("warning", "column stop")])


if __name__ == "__main__":
    unittest.main(verbosity=1)
