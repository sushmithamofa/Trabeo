r"""Tests for Stage 6: walls and cores, stairs, ramps.

    python -m building.stage6_selftest

Each test rebuilds, small, a case met on the training drawings.
"""
from __future__ import annotations

import unittest
from collections import Counter, defaultdict
from types import SimpleNamespace

import ezdxf

from building import Building, Element, Evidence, Sheet, Value
from building.linker import link
from building.readers import stairs
from building.sheets.source import TextItem
from building.walls import analyse, shape_of, wall_runs


def T(s, x, y, h=200.0, layer="TXT"):
    return TextItem(s, s, h, x, y, layer, f"{s}@{x:.0f},{y:.0f}", "model", False, x, y)


def view(doc, texts, kind="framing_plan", rid="P:V"):
    msp = doc.modelspace()
    return SimpleNamespace(id=rid, verdict=SimpleNamespace(kind=kind), texts=texts, doc=doc,
                           layers=Counter(), levels=[], wing="", title="",
                           view=SimpleNamespace(handles=[e.dxf.handle for e in msp], same_as="", source="viewport"))


def wall(b, n, outline, layer="STR.COLUMN", sheet="P:V/plan", mark=""):
    return b.add(Element(id=f"{sheet}/wall/{n}", type="wall", sheet=sheet, mark=mark,
                         geometry={"start": None, "end": None, "outline": [list(p) for p in outline]},
                         props={"thickness": Value(None, "read", 1.0, [Evidence("plan.legacy", sheet)])},
                         extra={"_layer": layer}))


L_COLUMN = [(0, 0), (450, 0), (450, 230), (230, 230), (230, 750), (0, 750)]     # Sasan Gir C7, 450 x 750
U_WALL = [(0, 0), (3000, 0), (3000, 2500), (2770, 2500), (2770, 230), (230, 230), (230, 2500), (0, 2500)]


class Walls(unittest.TestCase):
    def test_runs_are_the_revit_builds_own(self):
        """A straight wall drawn as an outline: one run, 230 thick, along its middle."""
        runs = wall_runs([(0, 0), (3000, 0), (3000, 230), (0, 230)])
        self.assertEqual([(round(a[1]), round(c[1]), round(t)) for a, c, t in runs], [(115, 115, 230)])

    def test_shapes(self):
        rect = [(0, 0), (3000, 0), (3000, 230), (0, 230)]
        ell = [(0, 0), (2000, 0), (2000, 230), (230, 230), (230, 2000), (0, 2000)]
        for outline, shape in ((rect, "straight"), (ell, "L"), (U_WALL, "U")):
            xs, ys = [p[0] for p in outline], [p[1] for p in outline]
            self.assertEqual(shape_of(wall_runs(outline), (min(xs), min(ys), max(xs), max(ys))), shape, shape)
        core = [([0, 115], [2400, 115], 230), ([2285, 0], [2285, 4400], 230),
                ([0, 4285], [2400, 4285], 230), ([115, 0], [115, 4400], 230)]
        self.assertEqual(shape_of(core, (0, 0, 2400, 4400)), "core")
        tee = [([0, 115], [3000, 115], 230), ([1500, 0], [1500, 2000], 230)]
        self.assertEqual(shape_of(tee, (0, 0, 3000, 2000)), "L")       # two runs square to each other
        tee3 = [([0, 115], [1400, 115], 230), ([1600, 115], [3000, 115], 230), ([1500, 0], [1500, 2000], 230)]
        self.assertEqual(shape_of(tee3, (0, 0, 3000, 2000)), "T")

    def test_a_column_drawn_as_an_L_is_not_a_wall(self):
        """Sasan Gir: C7 is a 450 x 750 L drawn on the column layer; the plan reader
        calls it a wall and Revit would build three short wall pieces."""
        b = Building()
        w = wall(b, 0, L_COLUMN)
        stats = defaultdict(int)
        analyse(b, stats)
        self.assertEqual(w.resolved["role"].value, "column")
        self.assertEqual((w.extra["column_like"]["width"], w.extra["column_like"]["depth"]), (450, 750))
        self.assertEqual([i.category for i in b.issues], ["wall"])

    def test_a_thin_pier_stays_a_wall(self):
        b = Building()
        w = wall(b, 0, [(0, 0), (230, 0), (230, 1025), (0, 1025)])
        analyse(b, defaultdict(int))
        self.assertEqual(w.resolved["role"].value, "RC wall")

    def test_masonry_is_flagged(self):
        b = Building()
        w = wall(b, 0, [(0, 0), (7787, 0), (7787, 230), (0, 230)], layer="BRICK WALL_MOFA")
        analyse(b, defaultdict(int))
        self.assertEqual(w.resolved["role"].value, "masonry")

    def test_a_short_piece_of_brick_wall_is_masonry_not_a_column(self):
        """ALCAZAR 1st floor: short brick pieces beside doors had been read as columns
        drawn as odd shapes, and built."""
        b = Building()
        w = wall(b, 0, [(0, 0), (230, 0), (230, 350), (0, 350)], layer="BRICK WALL_MOFA")
        analyse(b, defaultdict(int))
        self.assertEqual(w.resolved["role"].value, "masonry")
        self.assertNotIn("column_like", w.extra)

    def test_a_wall_with_no_runs_is_reported(self):
        b = Building()
        wall(b, 0, [(0, 0), (1500, 0), (0, 1500)], layer="RCC COLUMN")      # Villa 5's triangle
        analyse(b, defaultdict(int))
        self.assertIn("will not be built", b.issues[0].message)

    def test_wall_thickness_resolves_from_its_runs(self):
        b = Building()
        b.add_sheet(Sheet(id="P:V/plan", file="p", reader="plan.legacy", extra={"set": "P", "level_key": "L1"}))
        w = wall(b, 0, U_WALL, layer="SHEAR WALL")
        link(b)
        self.assertEqual((w.final("width"), w.final("shape"), w.final("runs")), (230, "U", 3))

    def test_wall_captions(self):
        """SW3 written inside an untagged wall names it; LW2 written beside a wall
        tagged LW1 names another wall, not drawn here (J1153)."""
        b = Building()
        b.add_sheet(Sheet(id="P:V", file="p", reader="sheets.registry", extra={"set": "P"}))
        b.add_sheet(Sheet(id="P:V/plan", file="p", reader="plan.legacy",
                          extra={"set": "P", "level_key": "L1", "view_sheet": "P:V"}))
        sw = wall(b, 0, [(0, 0), (3000, 0), (3000, 230), (0, 230)], layer="SHEAR WALL")
        lw = wall(b, 1, [(10000, 0), (13000, 0), (13000, 230), (10000, 230)], layer="SHEAR WALL", mark="LW1")
        for n, (m, x, y) in enumerate((("SW3", 1500, 115), ("LW2", 11500, 900))):
            ev = Evidence("labels", "P:V", "plan caption")
            b.add(Element(id=f"P:V/caption/{n}", type="caption", sheet="P:V", mark=m,
                          geometry={"at": [x, y], "text_height": 200.0},
                          props={"family": Value("wall", "read", 1.0, [ev])}))
        link(b)
        self.assertEqual((sw.final("mark"), lw.final("mark"), lw.resolved["mark"].status), ("SW3", "LW1", "read"))

    def test_an_L_column_takes_the_mark_of_the_column_at_its_place(self):
        """The L sits where the column layout marks C3; plain columns keep their marks."""
        b = Building()
        b.add_sheet(Sheet(id="L:V", file="c", reader="sheets.registry", extra={"set": "P"}))
        b.add_sheet(Sheet(id="P:V/plan", file="p", reader="plan.legacy", extra={"set": "P", "level_key": "L2"}))
        ev = Evidence("column_positions", "L:V")
        bays = [0, 4000, 7500, 12000, 15000, 19500, 23000, 27500]
        for i, bx in enumerate(bays):
            b.add(Element(id=f"L:V/column_position/{i}", type="column_position", sheet="L:V", mark=f"C{i + 1}",
                          geometry={"x": float(bx), "y": 0.0}, props={"mark": Value(f"C{i + 1}", "read", 1.0, [ev])}))
        pev = Evidence("plan.legacy", "P:V/plan")
        cols = []
        for i, bx in enumerate(bays):
            if i == 2:
                continue                                   # C3's place: the L below
            cols.append(b.add(Element(id=f"P:V/plan/column/{i}", type="column", sheet="P:V/plan",
                                      geometry={"x": float(bx), "y": 0.0},
                                      props={"width": Value(230.0, "read", 1.0, [pev]),
                                             "depth": Value(600.0, "read", 1.0, [pev])})))
        x = bays[2]
        w = wall(b, 9, [(x - 225 + px, -375 + py) for px, py in L_COLUMN])
        link(b)
        self.assertEqual(w.final("mark"), "C3")
        self.assertEqual((w.final("width"), w.final("depth")), (450, 750))
        self.assertEqual([c.final("mark") for c in cols], ["C1", "C2", "C4", "C5", "C6", "C7", "C8"])


class WallSchedule(unittest.TestCase):
    """Sasan Gir COL SCH: under the column tables, a table of wall sections drawn 4x,
    bands per level range, and no mark row at all."""

    def table(self):
        from building.foundations_selftest import line, section
        from building.readers import column_schedule
        doc = ezdxf.new()
        msp = doc.modelspace()
        for y in (0, 4000, 8000):
            line(msp, -200, y, 30000, y)
        for x in (-200, 1000):
            line(msp, x, 0, x, 8000)
        for y0 in (1500, 5500):
            section(msp, 2000, y0, 1025, 230, scale=4)
            section(msp, 9000, y0, 1800, 300, scale=4)
        texts = [T("FOUNDATION", 0, 3200, h=225), T("TO", 0, 2500, h=225), T("GF SLAB LVL", 0, 1800, h=225),
                 T("GF SLAB LVL", 0, 7200, h=225), T("TO", 0, 6500, h=225), T("FF SLAB LVL", 0, 5800, h=225),
                 T("20-T12", 4000, 700, h=225)]
        b = Building()
        b.add_sheet(Sheet(id="CS", file="x", reader="sheets.registry", extra={"set": "P"}))
        got = column_schedule.read(b, view(doc, texts, kind="column_schedule", rid="CS"))
        return b, got

    def test_sections_without_marks_are_read_per_level_range(self):
        b, got = self.table()
        self.assertEqual(got, {"wall schedule sections": 4})
        sizes = {(tuple(s.extra["level_range"]), s.get("thickness"), s.get("length")) for s in b.of("spec")}
        self.assertEqual(sizes, {(("FDN", "GF"), 230.0, 1025.0), (("FDN", "GF"), 300.0, 1800.0),
                                 (("GF", "L1"), 230.0, 1025.0), (("GF", "L1"), 300.0, 1800.0)})
        pier = next(s for s in b.of("spec") if s.extra["level_range"] == ["FDN", "GF"] and s.get("length") == 1025)
        self.assertEqual(pier.get("bars"), ["20-T12"])

    def test_a_plan_wall_takes_the_one_section_of_its_size(self):
        """The terrace pier: its outline splits badly, the schedule's 1025 x 230 decides."""
        b, _ = self.table()
        b.add_sheet(Sheet(id="P:V/plan", file="p", reader="plan.legacy", extra={"set": "P", "level_key": "L1"}))
        pier = wall(b, 0, [(0, 0), (230, 0), (230, 1025), (0, 1025)], layer="SHEAR WALL")
        other = wall(b, 1, [(5000, 0), (5230, 0), (5230, 4000), (5000, 4000)], layer="SHEAR WALL")
        link(b)
        self.assertEqual((pier.final("width"), pier.final("length")), (230.0, 1025.0))
        self.assertEqual(pier.extra["schedule_section"]["range"], ["GF", "L1"])
        self.assertNotIn("schedule_section", other.extra)             # no section of its size: left alone


def treads(msp, x0, y0, n, going=300.0, width=1500.0, layer="STAIRS_MOFA"):
    for i in range(n):
        msp.add_line((x0, y0 + i * going), (x0 + width, y0 + i * going), dxfattribs={"layer": layer})


class Stairs(unittest.TestCase):
    def test_a_dog_leg_among_other_lines(self):
        """Sasan Gir: two flights of 10 side by side, among the plan's beam lines."""
        doc = ezdxf.new()
        msp = doc.modelspace()
        treads(msp, 0, 0, 10)
        treads(msp, 1700, 0, 10)
        for i in range(6):                                 # beam faces running through the same band
            msp.add_line((-3000, 140 + i * 520), (-1500 + i * 37, 140 + i * 520), dxfattribs={"layer": "BEAM"})
        b = Building()
        got = stairs.read(b, view(doc, []))
        self.assertEqual(got, {"stairs": 1, "stair flights": 2})
        s = b.of("stair")[0]
        self.assertEqual((s.get("flights"), s.get("treads"), s.get("going"), s.get("width")), (2, [10, 10], 300, 1500))

    def test_even_lines_are_a_stair_only_with_a_stair_layer_or_word(self):
        doc = ezdxf.new()
        treads(doc.modelspace(), 0, 0, 8, layer="SLAB")
        b = Building()
        stairs.read(b, view(doc, []))
        self.assertEqual(b.of("stair"), [])
        stairs.read(b, view(doc, [T("UP", 750, -400)]))
        self.assertEqual(b.of("stair")[0].get("direction_text"), ["UP"])

    def test_a_stair_drawn_as_a_block(self):
        """Sasan Gir GF: the stair is a block named STAIR."""
        doc = ezdxf.new()
        blk = doc.blocks.new("STAIR")
        treads(blk, 0, 0, 9, layer="0")
        doc.modelspace().add_blockref("STAIR", (5000, 5000), dxfattribs={"layer": "Stair"})
        b = Building()
        stairs.read(b, view(doc, []))
        self.assertEqual(b.of("stair")[0].get("treads"), [9])

    def test_notes(self):
        """Villa 5: RISER = 0.160M THREAD = 0.30M, MIDLANDING UFL. LVL. (+)1440."""
        doc = ezdxf.new()
        b = Building()
        stairs.read(b, view(doc, [T("RISER = 0.160M THREAD = 0.30M", 0, 0),
                                  T("MIDLANDING UFL. LVL. (+)1440", 0, 500),
                                  T("MIDLANDING LVL -1760MM", 0, 900)], kind="beam_details"))
        s = b.of("spec")[0]
        self.assertEqual((s.get("riser"), s.get("tread"), s.get("landing_levels")), (160, 300, [-1760, 1440]))

    def test_a_ramp_and_its_slope(self):
        doc = ezdxf.new()
        doc.modelspace().add_lwpolyline([(0, 0), (12000, 0), (12000, 3000), (0, 3000)], close=True,
                                        dxfattribs={"layer": "SLAB"})
        b = Building()
        stairs.read(b, view(doc, [T("RAMP", 6000, 1500), T("SLOPE 1:12", 6000, 1000)]))
        r = b.of("ramp")[0]
        self.assertEqual((r.get("width"), r.get("length"), r.get("slope")), (3000, 12000, "1:12"))

    def test_the_sheet_border_round_a_ramp_note_is_not_a_ramp(self):
        """A note 'RAMP IN FILLING AS PER ARCH SLOPE' written on the sheet: the only closed
        outline round it is the sheet's border, which holds all the sheet's writing."""
        doc = ezdxf.new()
        msp = doc.modelspace()
        msp.add_lwpolyline([(0, 0), (66000, 0), (66000, 47000), (0, 47000)], close=True, dxfattribs={"layer": "0"})
        msp.add_lwpolyline([(20000, 20000), (32000, 20000), (32000, 23000), (20000, 23000)], close=True,
                           dxfattribs={"layer": "SLAB"})
        notes = [T(f"NOTE {n}", 2000 + n * 1500, 40000) for n in range(30)]
        b = Building()
        stairs.read(b, view(doc, notes + [T("RAMP", 26000, 21500), T("RAMP IN FILLING AS PER ARCH SLOPE", 26000, 15000)]))
        self.assertEqual([(r.get("width"), r.get("length")) for r in b.of("ramp")], [(3000, 12000)])

    def test_notes_give_the_tread_and_the_plan_going_is_kept_beside_it(self):
        """As for every size: what the notes say wins over what is measured on the plan;
        both are listed, so a 300 drawn against a 275 written is visible."""
        for tread in (300, 275):
            b = Building()
            b.add_sheet(Sheet(id="P:V", file="p", reader="sheets.registry", extra={"set": "P"}))
            b.add_sheet(Sheet(id="D:V", file="d", reader="sheets.registry", extra={"set": "P"}))
            doc = ezdxf.new()
            treads(doc.modelspace(), 0, 0, 10)
            stairs.read(b, view(doc, []))
            stairs.read(b, view(ezdxf.new(), [T(f"RISER 150 TREAD {tread}", 0, 0)], kind="beam_details", rid="D:V"))
            link(b)
            s = b.of("stair")[0]
            self.assertEqual((s.final("going"), s.final("riser")), (tread, 150))
            given = {ev.source.rsplit(": ", 1)[1] for ev in s.resolved["going"].evidence}
            self.assertEqual(given, {"300"} if tread == 300 else {"300", str(tread)})


if __name__ == "__main__":
    unittest.main(verbosity=1)
