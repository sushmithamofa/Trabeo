"""Test suite for the CAD to Revit extractor.

    python selftest.py              run everything
    python selftest.py -v           show each test
    python selftest.py --fixture X  write the synthetic test drawing to X.dxf and stop,
                                    so it can be opened in CAD and looked at

Most of these tests build a drawing in memory with known answers, so a failure
means the extractor is wrong, not that the sample drawing changed. The last group
runs against the real Wing C file and is skipped if it is not on this machine.
"""
from __future__ import annotations

import math
import os
import shutil
import sys
import tempfile
import unittest
import warnings

warnings.filterwarnings("ignore", category=DeprecationWarning)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ezdxf                                                     # noqa: E402

from core import (cleanout, extract, geometry as G, legend, pipeline,  # noqa: E402
                  reader, report, schedule, slabs)
from core.model import BLOCKER                                   # noqa: E402

REAL_DWG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "1153 - 1ST FLOOR PLAN ( C WING ) 14-07-2026.dwg")

# Known-good result for the real drawing. Update deliberately, never to make a
# failing test pass.
#
# 2026-09-25: beams 151 -> 117 and girders 22 -> 20. 30 pairs of plain lines were
# found to be the two faces of one beam and merged, and 6 lines lying on top of
# another beam were dropped: 36 fewer, which is exactly the 34 + 2 seen here.
# Counts for the Wing C 1st floor GFC drawing. They changed on 2026-09-25 when the
# extractor started leaving out the drawing legend and the outlines drawn twice:
# columns fell from 65 to 48, because 8 of the 65 were the legend sample boxes and
# 9 were exact repeats of another outline. All 44 column marks and all 4 wall marks
# are unchanged, which is what says the 17 that went were not real structure.
# girders fell 19 -> 17 on 2026-09-26, when a wide beam drawn as two faces began
# to be merged where the mark between the faces has that width in the schedule.
# BP4 (1500) and GB4 (900) were each two beams at their faces before that, 750 and
# 450 mm either side of where the girder really is.
# girders fell again, 17 -> 16, when leftover face lines began to be dropped:
# lines with no width and no mark sitting exactly half a beam width off a beam
# already in the model. The marked beam count did not move, which is what says
# nothing real went with them.
# marked beams rose 114 -> 115 on 2026-09-26, when beam ends sitting on a column
# stopped being trimmed back to a cross beam up to 800 mm away. One beam kept its
# drawn length and B99, 181 mm from it, was placed on it; before, B99 was a
# blocker ("was not placed"). Raw counts did not move.
# slab panels rose 0 -> 52 on 2026-09-28, when panels began to be taken from the
# bays the beams enclose (the drawing has no slab outlines). Every other count is
# unchanged. The 7 marks with no panel sit in bays a beam gap or the big wall
# outline leaves open, and are each reported; checked against a plot of the plan.
# On 2026-09-29 sunk depths began to come from the legend, by hatch pattern.
# sunk zones rose 20 -> 30: the ten 150 mm hatches sit on a layer called HATCH
# that only the legend identifies. slab panels rose 52 -> 54: S5 and S6 are
# only partly hatched and were split into a sunk part and a part at the level.
# Both splits checked against a plot of the hatch outlines.
# slab panels rose 54 -> 72 on 2026-09-29: slab is everywhere but a drawn
# cut-out (the X), so 16 closed bays with no mark now carry the nearest mark's
# slab, and the two 10-13 m2 R.C.C. PARDI outlines with S8A marks inside them no
# longer block those slabs. Checked against a plot of the whole plan.
# On 2026-10-14 the user ruled "no slab mark, no slab": those 16 unmarked bays are
# left open again (slab_fill_unmarked_bays brings them back). The drawing is not on
# this PC, so "slab panels" below was not re-measured: expect about 72 - 16.
# beams 116 -> 117 and marked beams 115 -> 118 on 2026-09-29, when a mark
# began to go to a beam running the same way as its text (these drawings write
# every beam mark along its beam) and one line carrying B41 and B42, a span
# each, was split at the joint between them. Unplaced beam marks fell 7 -> 4.
REAL_EXPECTED = {"columns": 48, "walls": 7, "beams": 117, "girders": 16,
                 "slab panels": 72, "slab tags": 65, "sunk zones": 30}
REAL_MARKED_BEAMS = 118
REAL_MARKED_COLUMNS = 44      # the number that actually has to hold

COL_W, COL_D = 230.0, 950.0
COL_CENTRE = (10000.0, 20000.0)
COL_ROT = 2.0
BEAM_WIDTH = 230.0
BEAM_A, BEAM_B = (0.0, 0.0), (5000.0, 0.0)


def rotated_rect(cx, cy, w, d, deg):
    """Corners of a w x d rectangle centred on (cx, cy), long side at `deg`."""
    a = math.radians(deg)
    ux, uy = math.cos(a), math.sin(a)          # along the long side
    vx, vy = -math.sin(a), math.cos(a)         # across it
    hw, hd = w / 2.0, d / 2.0
    return [(cx - ux * hd - vx * hw, cy - uy * hd - vy * hw),
            (cx + ux * hd - vx * hw, cy + uy * hd - vy * hw),
            (cx + ux * hd + vx * hw, cy + uy * hd + vy * hw),
            (cx - ux * hd + vx * hw, cy - uy * hd + vy * hw)]


def build_fixture(path: str, *, open_column: bool = False, slab_mark: bool = False) -> str:
    """A small drawing whose correct answers are known exactly."""
    doc = ezdxf.new("R2010", setup=True)
    doc.header["$INSUNITS"] = 4
    msp = doc.modelspace()
    for lay in ("COLUMN", "colno", "BEAM", "BEAM NO (H)", "SLAB", "SLAB NO", "S-GRID", "SHEET"):
        if lay not in doc.layers:
            doc.layers.add(lay)

    corners = rotated_rect(*COL_CENTRE, COL_W, COL_D, COL_ROT)
    msp.add_lwpolyline(corners, close=True, dxfattribs={"layer": "COLUMN"})
    t = msp.add_text("5", dxfattribs={"layer": "colno", "height": 250})
    t.set_placement((COL_CENTRE[0] + 200, COL_CENTRE[1]))

    if open_column:                       # an outline left genuinely open
        far = rotated_rect(30000.0, 20000.0, COL_W, COL_D, COL_ROT)
        msp.add_lwpolyline(far[:3], close=False, dxfattribs={"layer": "COLUMN"})

    m = msp.add_mline([BEAM_A, BEAM_B], dxfattribs={"layer": "BEAM"})
    m.set_scale_factor(BEAM_WIDTH)
    m.set_justification(0)                # Top: the drawn line is a face
    tb = msp.add_text("B1", dxfattribs={"layer": "BEAM NO (H)", "height": 250})
    tb.set_placement((2500.0, -115.0))

    msp.add_lwpolyline([(0, 0), (4000, 0), (4000, 3000), (0, 3000)],
                       close=True, dxfattribs={"layer": "SLAB"})
    if slab_mark:                         # the slab's mark, written inside its outline
        msp.add_text("S1", dxfattribs={"layer": "SLAB NO", "height": 250}).set_placement((2000, 1500))
    msp.add_line((-1000, -1000), (-1000, 9000), dxfattribs={"layer": "S-GRID"})
    msp.add_text("NOT A MARK, A SITE NOTE", dxfattribs={"layer": "colno", "height": 250}) \
       .set_placement((50000, 50000))

    doc.saveas(path)
    return path


class TempCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="c2r_test_")
        self.cfg = pipeline.load_config()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def fixture(self, **kw):
        p = os.path.join(self.tmp, "fixture.dxf")
        build_fixture(p, **kw)
        return extract.extract(p, self.cfg, log=lambda *_: None)


# ---------------------------------------------------------------- geometry
class TestGeometry(unittest.TestCase):
    def test_rectangle_is_recovered_after_rotation(self):
        pts = rotated_rect(0, 0, 230, 950, 2.0)
        got = G.rectangle_of(pts, 5, 3)
        self.assertIsNotNone(got)
        c, short, long_, rot = got
        self.assertAlmostEqual(short, 230, delta=0.01)
        self.assertAlmostEqual(long_, 950, delta=0.01)
        self.assertAlmostEqual(rot, 2.0, delta=0.01)
        self.assertAlmostEqual(c[0], 0, delta=0.01)

    def test_bounding_box_would_have_been_wrong(self):
        """Guards the bug this replaced: a bbox reads 264 x 958, not 230 x 950."""
        pts = rotated_rect(0, 0, 230, 950, 2.0)
        x0, y0, x1, y1 = G.bbox(pts)
        self.assertGreater(x1 - x0, 255)
        self.assertNotAlmostEqual(x1 - x0, 230, delta=5)

    def test_non_rectangle_is_rejected(self):
        self.assertIsNone(G.rectangle_of([(0, 0), (100, 0), (130, 90), (0, 100)], 5, 3))
        self.assertIsNone(G.rectangle_of([(0, 0), (100, 0), (100, 100)], 5, 3))

    def test_point_to_segment(self):
        self.assertAlmostEqual(G.point_to_segment((50, 10), (0, 0), (100, 0)), 10)
        self.assertAlmostEqual(G.point_to_segment((150, 0), (0, 0), (100, 0)), 50)

    def test_dedupe_drops_closing_point(self):
        pts = [(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)]
        self.assertEqual(len(G.dedupe_points(pts, 1.0)), 4)

    def test_long_axis_runs_along_the_long_side(self):
        a, b = G.long_axis_of_rect(rotated_rect(0, 0, 230, 950, 0.0))
        self.assertAlmostEqual(G.dist(a, b), 950, delta=0.01)


# ------------------------------------------------------------------- marks
class TestMarks(unittest.TestCase):
    def test_marks_are_accepted(self):
        for m in ("C-12", "12", "FC1", "S4A", "ST2A", "LW1", "BKT", "B107/BP", "GB5"):
            self.assertTrue(extract.is_mark(m), m)

    def test_prose_is_rejected(self):
        for m in ("NoteforthesiteteamUseM40concrete", "SCHEDULEOFCOLUMN", ""):
            self.assertFalse(extract.is_mark(m), m)


# ------------------------------------------------------------------ layers
class TestLayerMapping(unittest.TestCase):
    def setUp(self):
        self.cfg = pipeline.load_config()
        self.lm = reader.LayerMap(self.cfg["layer_roles"])

    def test_known_layers(self):
        self.assertEqual(self.lm.role_of("COLUMN"), "column")
        self.assertEqual(self.lm.role_of("FL COL"), "column_float")
        self.assertEqual(self.lm.role_of("GIRDER BEAM"), "girder")
        self.assertEqual(self.lm.role_of("300mm SUNK"), "sunk")
        self.assertEqual(self.lm.role_of("COL HATCH"), "ignore")

    def test_case_insensitive(self):
        self.assertEqual(self.lm.role_of("column"), "column")

    def test_a_full_stop_at_the_end_is_the_same_layer(self):
        # Sasan Gir UGWT: its slab marks on 'SLAB NO.' were all missed (one slab built)
        lm = reader.LayerMap({"slab_tag": ["SLAB NO"], "column": ["COLUMN"]})
        self.assertEqual(lm.role_of("SLAB NO."), "slab_tag")
        self.assertEqual(lm.role_of("slab no. "), "slab_tag")
        self.assertEqual(lm.role_of("SLAB NO"), "slab_tag")
        self.assertEqual(lm.role_of("SLAB NOS."), "unmapped")       # a different name stays unknown
        self.assertEqual(lm.role_of("COLUMN.X"), "unmapped")

    def test_unknown_layer_is_flagged_not_swallowed(self):
        self.assertEqual(self.lm.role_of("ZZ-SOMETHING"), "unmapped")

    def test_guesser(self):
        for layer, role in (("STR-COLUMNS", "column"), ("STR_BEAMS_RCC", "beam"),
                            ("STR_GIRDER", "girder"), ("TEXT-COL-NUM", "column_tag"),
                            ("TEXT-BM-NUM", "beam_tag"), ("STR-SHEARWALL", "wall"),
                            ("COL HATCH", "ignore")):
            self.assertEqual(reader.guess_role(layer), role, layer)

    def test_guesser_admits_defeat(self):
        self.assertEqual(reader.guess_role("LAYER1"), "unmapped")

    def test_sunk_depth_from_name_and_override(self):
        self.assertEqual(reader.sunk_depth_from_layer("300mm SUNK", {})[0], 300.0)
        d, src = reader.sunk_depth_from_layer("300mm SUNK", {"300mm SUNK": 150})
        self.assertEqual((d, src), (150.0, "override"))
        self.assertEqual(reader.sunk_depth_from_layer("SUNK", {})[1], "unknown")


# -------------------------------------------------------------- extraction
class TestExtraction(TempCase):
    def test_column_size_and_rotation(self):
        res = self.fixture()
        self.assertEqual(len(res.columns), 1)
        c = res.columns[0]
        self.assertAlmostEqual(c.width, COL_W, delta=0.1)
        self.assertAlmostEqual(c.depth, COL_D, delta=0.1)
        self.assertAlmostEqual(c.rotation, COL_ROT, delta=0.05)
        self.assertAlmostEqual(c.x, COL_CENTRE[0], delta=0.1)

    def test_column_mark_is_matched(self):
        self.assertEqual(self.fixture().columns[0].mark, "5")

    def test_prose_on_a_tag_layer_is_not_used_as_a_mark(self):
        res = self.fixture()
        self.assertNotIn("NOTAMARK", [c.mark for c in res.columns])
        self.assertEqual(len(res.columns), 1)

    def test_top_justified_beam_is_moved_to_its_centreline(self):
        """The whole point of the tool: the drawn line is a face, not the centre."""
        res = self.fixture()
        beams = [b for b in res.beams if b.kind == "beam"]
        self.assertEqual(len(beams), 1)
        b = beams[0]
        self.assertAlmostEqual(b.offset_applied, BEAM_WIDTH / 2, delta=0.1)
        self.assertEqual(b.centreline_source, "multiline-faces")
        self.assertAlmostEqual(b.start[1], -BEAM_WIDTH / 2, delta=0.1)
        self.assertAlmostEqual(b.end[1], -BEAM_WIDTH / 2, delta=0.1)
        self.assertAlmostEqual(b.width, BEAM_WIDTH, delta=0.1)

    def test_beam_mark_is_matched(self):
        beams = [b for b in self.fixture().beams if b.kind == "beam"]
        self.assertEqual(beams[0].mark, "B1")

    def test_closed_slab_is_extracted(self):
        res = self.fixture(slab_mark=True)
        self.assertEqual([(s.mark, s.source_layer) for s in res.slabs], [("S1", "SLAB")])

    def test_a_closed_slab_with_no_mark_on_the_plan_is_not_built(self):
        # the user's rule (14 Oct): no slab mark in the whole plan, no slab
        self.assertEqual(self.fixture().slabs, [])

    def test_grid_is_found_so_no_setting_out_blocker(self):
        res = self.fixture()
        self.assertEqual(len(res.grids), 1)
        self.assertFalse([i for i in res.issues if i.category == "setting out"
                          and i.severity == BLOCKER])

    def test_open_outline_blocks_and_is_not_invented(self):
        res = self.fixture(open_column=True)
        self.assertEqual(len(res.columns), 1, "the open shape must not become a column")
        opened = [i for i in res.issues
                  if i.severity == BLOCKER and "open by" in i.message]
        self.assertTrue(opened, "an open outline must raise a blocker")


# ------------------------------------------------------ joining the network
class TestBeamJoin(unittest.TestCase):
    """Moving a beam to its centreline pulls corners apart by half its width.
    The join step has to close them again without distorting anything."""

    def setUp(self):
        self.cfg = pipeline.load_config()
        self.tol = self.cfg["tolerances"]

    def _beam(self, a, b):
        from core.model import Beam
        return Beam(mark="", kind="beam", start=a, end=b, width=230.0,
                    width_source="multiline", centreline_source="multiline-faces",
                    offset_applied=115.0, source_layer="BEAM", handle="X")

    def test_corner_is_closed(self):
        # two perpendicular beams whose ends miss each other by 115 mm
        h = self._beam((0.0, 0.0), (5000.0, 0.0))
        v = self._beam((4885.0, -3000.0), (4885.0, -115.0))
        res = extract.Result(beams=[h, v])
        extract._join_beam_network(res, self.tol)
        self.assertAlmostEqual(v.end[1], 0.0, delta=0.01, msg="vertical should reach the horizontal")
        self.assertAlmostEqual(h.end[0], 4885.0, delta=0.01, msg="horizontal should stop at the vertical")

    def test_an_end_drawn_to_a_column_face_is_not_pulled_back_off_it(self):
        """ALCAZAR B98: drawn up to column 9's face, a cross beam 238 mm short of it;
        trimmed back to the cross beam, a gap was left under the column."""
        from core.model import Column
        col = Column(mark="", kind="column", x=0.0, y=3350.0, width=300.0, depth=700.0, rotation=90.0,
                     outline=[(-150, 3000), (150, 3000), (150, 3700), (-150, 3700)], source_layer="COL", handle="C")
        up = self._beam((0.0, 0.0), (0.0, 2995.0))                  # at the column's bottom face
        cross = self._beam((-3000.0, 2762.0), (0.0, 2762.0))        # 233 below it
        res = extract.Result(beams=[up, cross], columns=[col])
        extract._join_beam_network(res, self.tol)
        self.assertAlmostEqual(up.end[1], 2995.0, delta=1.0)        # old code: cut back to 2762

    def test_an_end_at_a_column_face_still_runs_out_to_a_crossing(self):
        """J1153 B67: the old behaviour, kept - an end at a face may still move OUT."""
        from core.model import Column
        col = Column(mark="", kind="column", x=0.0, y=3350.0, width=300.0, depth=700.0, rotation=90.0,
                     outline=[(-150, 3000), (150, 3000), (150, 3700), (-150, 3700)], source_layer="COL", handle="C")
        up = self._beam((400.0, 0.0), (400.0, 3000.0))             # beside the column, level with its face
        cross = self._beam((-3000.0, 3200.0), (3000.0, 3200.0))     # 200 further on
        res = extract.Result(beams=[up, cross], columns=[col])
        extract._join_beam_network(res, self.tol)
        self.assertAlmostEqual(up.end[1], 3200.0, delta=1.0)

    def test_parallel_beams_are_left_alone(self):
        a = self._beam((0.0, 0.0), (5000.0, 0.0))
        b = self._beam((0.0, 230.0), (5000.0, 230.0))
        res = extract.Result(beams=[a, b])
        extract._join_beam_network(res, self.tol)
        self.assertEqual(a.start, (0.0, 0.0))
        self.assertEqual(b.end, (5000.0, 230.0))

    def test_a_beam_is_never_collapsed(self):
        long_ = self._beam((0.0, 0.0), (5000.0, 0.0))
        near = self._beam((100.0, -400.0), (100.0, 400.0))
        res = extract.Result(beams=[long_, near])
        before = long_.length
        extract._join_beam_network(res, self.tol)
        self.assertGreater(long_.length, self.tol["beam_min_length_mm"])
        self.assertAlmostEqual(long_.length, before, delta=200)

    def test_distant_crossing_is_not_snapped_to(self):
        h = self._beam((0.0, 0.0), (5000.0, 0.0))
        far = self._beam((20000.0, -3000.0), (20000.0, 3000.0))   # crossing 15 m away
        res = extract.Result(beams=[h, far])
        extract._join_beam_network(res, self.tol)
        self.assertAlmostEqual(h.end[0], 5000.0, delta=0.01)

    def test_an_end_on_a_column_is_not_cut_back_to_a_cross_beam(self):
        # Wing B column 23: B66 is drawn down to the top of the column and B40/B41
        # cross it 560 mm higher. Trimming B66 back to that crossing left the
        # column hanging off one beam line instead of two.
        from core.model import Column
        col = Column(mark="23", kind="column", x=0.0, y=-600.0, width=230.0,
                     depth=1200.0, rotation=90.0,
                     outline=[(-115.0, -1200.0), (115.0, -1200.0), (115.0, 0.0),
                              (-115.0, 0.0)],
                     source_layer="COLUMN", handle="C")
        down = self._beam((0.0, 4000.0), (0.0, -50.0))       # ends inside the column
        cross = self._beam((-4000.0, 560.0), (4000.0, 560.0))
        res = extract.Result(beams=[down, cross], columns=[col])
        extract._join_beam_network(res, self.tol)
        self.assertAlmostEqual(down.end[1], -50.0, delta=0.01,
                               msg="the end on the column must stay on the column")

    def test_an_end_is_not_trimmed_back_past_the_overshoot(self):
        # a beam carrying on 560 mm past a crossing is real beam, not overshoot
        v = self._beam((0.0, 4000.0), (0.0, 0.0))
        h = self._beam((-4000.0, 560.0), (4000.0, 560.0))
        res = extract.Result(beams=[v, h])
        extract._join_beam_network(res, self.tol)
        self.assertAlmostEqual(v.end[1], 0.0, delta=0.01)


class TestFacePairMerge(unittest.TestCase):
    """A beam drawn as two ordinary parallel lines, one per face, has to become a
    single beam on the centreline, with its width read from the gap."""

    def setUp(self):
        self.cfg = pipeline.load_config()
        self.tol = self.cfg["tolerances"]

    def _plain(self, a, b):
        from core.model import Beam
        return Beam(mark="", kind="beam", start=a, end=b, width=None,
                    width_source="schedule-required", centreline_source="as-drawn",
                    offset_applied=0.0, source_layer="BEAM", handle="X")

    def test_two_faces_become_one_beam_of_the_right_width(self):
        f1 = self._plain((0.0, 0.0), (5000.0, 0.0))
        f2 = self._plain((0.0, 230.0), (5000.0, 230.0))
        res = extract.Result(beams=[f1, f2])
        extract._merge_face_pairs(res, self.tol)
        self.assertEqual(len(res.beams), 1, "the pair must collapse to one beam")
        b = res.beams[0]
        self.assertAlmostEqual(b.width, 230.0, delta=0.1)
        self.assertAlmostEqual(b.start[1], 115.0, delta=0.1, msg="must sit midway")
        self.assertAlmostEqual(b.end[1], 115.0, delta=0.1)
        self.assertEqual(b.centreline_source, "face-pair")

    def test_faces_drawn_in_opposite_directions_still_merge(self):
        f1 = self._plain((0.0, 0.0), (5000.0, 0.0))
        f2 = self._plain((5000.0, 230.0), (0.0, 230.0))      # drawn the other way
        res = extract.Result(beams=[f1, f2])
        extract._merge_face_pairs(res, self.tol)
        self.assertEqual(len(res.beams), 1)
        b = res.beams[0]
        self.assertAlmostEqual(b.start[1], 115.0, delta=0.1)
        self.assertAlmostEqual(b.length, 5000.0, delta=1.0)

    def test_two_separate_beams_far_apart_are_not_merged(self):
        f1 = self._plain((0.0, 0.0), (5000.0, 0.0))
        f2 = self._plain((0.0, 3000.0), (5000.0, 3000.0))    # 3 m apart, real beams
        res = extract.Result(beams=[f1, f2])
        extract._merge_face_pairs(res, self.tol)
        self.assertEqual(len(res.beams), 2)

    def test_barely_overlapping_lines_are_not_merged(self):
        f1 = self._plain((0.0, 0.0), (5000.0, 0.0))
        f2 = self._plain((4800.0, 230.0), (9000.0, 230.0))   # only 4% alongside
        res = extract.Result(beams=[f1, f2])
        extract._merge_face_pairs(res, self.tol)
        self.assertEqual(len(res.beams), 2)

    def test_faces_with_a_slab_mark_between_are_not_one_beam(self):
        """Stilt A wing: the sunk S1 strip, 600 wide between two beam rows, became a
        600 beam over the slab. A mark on a 230 beam's line changes nothing."""
        from core.model import SlabTag
        f1 = self._plain((0.0, 0.0), (5000.0, 0.0))
        f2 = self._plain((0.0, 600.0), (5000.0, 600.0))
        res = extract.Result(beams=[f1, f2])
        res.slab_tags.append(SlabTag(mark="S1", x=2000.0, y=212.0, source_layer="", handle=""))
        extract._merge_face_pairs(res, self.tol)
        self.assertEqual(len(res.beams), 2)
        g1 = self._plain((0.0, 0.0), (5000.0, 0.0))
        g2 = self._plain((0.0, 230.0), (5000.0, 230.0))
        res = extract.Result(beams=[g1, g2])
        res.slab_tags.append(SlabTag(mark="S8", x=2000.0, y=115.0, source_layer="", handle=""))
        extract._merge_face_pairs(res, self.tol)
        self.assertEqual(len(res.beams), 1)

    def test_a_wide_girder_captioned_with_its_size_merges(self):
        """ALCAZAR 1st floor: GB1(1100X1200) written beside two lines 1100 apart, on
        the girder layer, no schedule on the sheet. One girder midway, not one face
        taken as the girder and the other left as a 230 beam beside it."""
        f1 = self._plain((0.0, 0.0), (7800.0, 0.0))
        f2 = self._plain((0.0, 1100.0), (7800.0, 1100.0))
        caps = [("GB1", 1100.0, [(1600.0, 1190.0), (3400.0, 1190.0), (1600.0, 1440.0), (3400.0, 1440.0)])]
        res = extract.Result(beams=[f1, f2])
        extract._merge_face_pairs(res, self.tol, None, (), caps)
        self.assertEqual(len(res.beams), 1)
        self.assertAlmostEqual(res.beams[0].width, 1100.0, delta=0.1)
        self.assertAlmostEqual(res.beams[0].start[1], 550.0, delta=0.1)

    def test_a_caption_of_another_width_or_far_away_does_not_merge(self):
        for caps in ([("GB1", 900.0, [(2000.0, 1200.0)])],          # writes another width
                     [("GB1", 1100.0, [(2000.0, 4000.0)])]):        # 2.9 m off the band
            f1 = self._plain((0.0, 0.0), (7800.0, 0.0))
            f2 = self._plain((0.0, 1100.0), (7800.0, 1100.0))
            res = extract.Result(beams=[f1, f2])
            extract._merge_face_pairs(res, self.tol, None, (), caps)
            self.assertEqual(len(res.beams), 2, caps)

    def test_a_girder_mark_written_just_beside_its_band_merges(self):
        """J1153 Wing C 1st floor: GB3 (schedule 900) written 300 mm below the lower of
        two faces 900 apart. It stayed two beams: GB3 centred on one face, an extra beam
        on the other. A mark of another width, or well away, still merges nothing."""
        sched_sizes = {"GB3": {"width": 900.0, "depth": 700.0}, "B25": {"width": 230.0, "depth": 600.0}}
        for tag, n in ((("GB3", (3000.0, -300.0), "h", "BEAM NO"), 1),
                       (("B25", (3000.0, -300.0), "h", "BEAM NO"), 2),
                       (("GB3", (3000.0, -1200.0), "h", "BEAM NO"), 2)):
            f1 = self._plain((0.0, 0.0), (5600.0, 0.0))
            f2 = self._plain((0.0, 900.0), (5600.0, 900.0))
            res = extract.Result(beams=[f1, f2])
            extract._merge_face_pairs(res, self.tol, sched_sizes, [tag])
            self.assertEqual(len(res.beams), n, tag)
            if n == 1:
                self.assertAlmostEqual(res.beams[0].start[1], 450.0, delta=0.1)

    def test_a_band_drawn_a_little_off_its_scheduled_width_merges(self):
        """GB2A: scheduled 700, drawn 736 apart, its mark inside the band."""
        f1 = self._plain((0.0, 0.0), (3600.0, 0.0))
        f2 = self._plain((0.0, 736.0), (3600.0, 736.0))
        res = extract.Result(beams=[f1, f2])
        extract._merge_face_pairs(res, self.tol, {"GB2A": {"width": 700.0}}, [("GB2A", (1800.0, 300.0), "h", "T")])
        self.assertEqual(len(res.beams), 1)
        self.assertAlmostEqual(res.beams[0].width, 736.0, delta=0.1)

    def test_the_line_closing_a_girders_end_is_not_a_beam(self):
        """GB2: a 750 line across the end of its two faces 750 apart stayed as an extra
        beam. A 230 beam meeting the girder's end, or a long line across it, stays."""
        f1 = self._plain((0.0, 0.0), (7000.0, 0.0))
        f2 = self._plain((0.0, 750.0), (7000.0, 750.0))
        cap = self._plain((0.0, 0.0), (0.0, 750.0))
        cross = self._plain((3500.0, -3000.0), (3500.0, 4000.0))      # a long line across: a beam
        res = extract.Result(beams=[f1, f2, cap, cross])
        extract._merge_face_pairs(res, self.tol, {"GB2": {"width": 750.0}}, [("GB2", (3000.0, 375.0), "h", "T")])
        extract._drop_end_caps(res, self.tol, [("GB2", (3000.0, 375.0), "h", "T")])
        self.assertEqual(len(res.beams), 2)
        self.assertIn(cross, res.beams)
        self.assertNotIn(cap, res.beams)

    def test_size_captions_are_read_from_any_layer(self):
        doc = ezdxf.new()
        msp = doc.modelspace()
        msp.add_text("GB1(1100X1200)", dxfattribs={"layer": "aGIRDER BEAM", "insert": (10, 20), "height": 150})
        msp.add_text("GB13(1100X1300/1200)", dxfattribs={"layer": "TXT", "insert": (0, 0), "height": 150})
        msp.add_text("SECTION A-A", dxfattribs={"layer": "TXT", "insert": (0, 0), "height": 150})
        got = sorted((m, w) for m, w, _ in extract._size_captions(list(msp)))
        self.assertEqual(got, [("GB1", 1100.0), ("GB13", 1100.0)])

    def test_a_multiline_with_a_corner_gives_one_beam_per_leg(self):
        """ALCAZAR BKT2: one 200 multiline up 446 then left 1059 came out as two
        diagonal beams across the corner (faces of different legs paired)."""
        doc = ezdxf.new()
        ml = doc.modelspace().add_mline([(0, 0), (0, 446), (-1059, 446)],
                                        dxfattribs={"scale_factor": 200, "justification": 0})
        legs = extract._mline_centreline(ml, extract.Result())
        self.assertEqual(len(legs), 2)
        for a, b, *_ in legs:
            self.assertTrue(abs(a[0] - b[0]) < 1 or abs(a[1] - b[1]) < 1, (a, b))   # square, not diagonal

    def test_a_short_multiline_beam_is_kept_a_short_scrap_is_not(self):
        """ALCAZAR 1st floor BC17: a multiline 256 long, 230 wide, closing the corner with
        B63 under column 64, was dropped as shorter than 300 - the corner stood open and
        the slab leaked round it. A multiline shorter than its own width, or a short plain
        line, is still not a beam."""
        doc = ezdxf.new()
        msp = doc.modelspace()
        keep = msp.add_mline([(0, 0), (0, -256)], dxfattribs={"scale_factor": 230, "justification": 1,
                                                                 "layer": "BEAM"})
        scrap = msp.add_mline([(5000, 0), (5000, -150)], dxfattribs={"scale_factor": 230, "justification": 1,
                                                                        "layer": "BEAM"})
        line = msp.add_line((9000, 0), (9000, -256), dxfattribs={"layer": "BEAM"})
        res = extract.Result()
        extract._extract_beams(doc, {"beam": [keep, scrap, line], "girder": []}, res, self.cfg, self.tol)
        self.assertEqual([(round(b.start[0]), round(b.length), b.width) for b in res.beams], [(0, 256, 230.0)])

    def test_a_column_drawn_dashed_and_solid_is_one_column(self):
        """ALCAZAR 1st floor: the column below drawn dashed 300 x 700, the one above
        solid 230 x 700 in the same place: built once, the dashed one."""
        from core.model import Column
        below = Column(mark="", kind="column", x=150, y=350, width=300, depth=700, rotation=90,
                       outline=[(0, 0), (300, 0), (300, 700), (0, 700)], source_layer="COLUMN", handle="A")
        above = Column(mark="", kind="column", x=115, y=350, width=230, depth=700, rotation=90,
                       outline=[(0, 0), (230, 0), (230, 700), (0, 700)], source_layer="COLUMN", handle="B")
        other = Column(mark="", kind="column", x=5115, y=350, width=230, depth=700, rotation=90,
                       outline=[(5000, 0), (5230, 0), (5230, 700), (5000, 700)], source_layer="COLUMN", handle="C")
        res = extract.Result(columns=[below, above, other])
        extract._one_column_per_stack(res, {id(below): True}, {}, self.tol)
        self.assertEqual(res.columns, [below, other])
        self.assertIn("230 x 700", below.notes[-1])

    def test_a_face_carried_on_to_the_corner_is_the_beams_not_a_stub(self):
        """ALCAZAR B26: a 300 mm line carries the beam's face on to the outer corner;
        BC7, written out on the balcony, was nearer to it, so it stayed a 300 mm beam
        sticking off the corner. It is the beam's face: the beam runs on over it."""
        from core.model import Beam
        k = Beam(mark="", kind="beam", start=(0.0, 115.0), end=(4800.0, 115.0), width=230.0,
                 width_source="multiline", centreline_source="multiline-faces", offset_applied=115.0,
                 source_layer="BEAM", handle="K")
        b = self._plain((-300.0, 0.0), (0.0, 0.0))
        res = extract.Result(beams=[k, b])
        extract._drop_orphan_faces(res, self.tol, [("BC7", (-350.0, 450.0), "T", "BEAM NO")])
        self.assertEqual(res.beams, [k])
        self.assertAlmostEqual(k.start[0], -300.0, delta=1.0)

    def test_a_face_piece_running_past_the_end_still_splits_the_long_face(self):
        """New block NB3b: bottom face one line 0..3996, top face in two pieces broken
        where SB meets it (0..1471, 1701..4146 - running 150 past the bottom face over
        the column). The beam stopped 1.3 m short of the column; it reaches it now."""
        long_ = self._plain((0.0, 0.0), (3996.0, 0.0))
        p1 = self._plain((0.0, 230.0), (1471.0, 230.0))
        p2 = self._plain((1701.0, 230.0), (4146.0, 230.0))
        res = extract.Result(beams=[long_, p1, p2])
        extract._split_long_faces(res, self.tol)
        extract._merge_face_pairs(res, self.tol)
        self.assertGreater(max(max(b.start[0], b.end[0]) for b in res.beams), 3990.0)

    def test_a_multiline_beam_is_never_merged_away(self):
        from core.model import Beam
        ml = Beam(mark="B-1", kind="beam", start=(0.0, 0.0), end=(5000.0, 0.0),
                  width=230.0, width_source="multiline",
                  centreline_source="multiline-faces", offset_applied=115.0,
                  source_layer="BEAM", handle="X")
        near = self._plain((0.0, 230.0), (5000.0, 230.0))
        res = extract.Result(beams=[ml, near])
        extract._merge_face_pairs(res, self.tol)
        self.assertIn(ml, res.beams, "a beam with a known centreline must survive")

    def test_every_mark_gets_its_own_beam_when_one_is_free(self):
        """Marks compete for beams. A mark must never be dropped because a nearer
        mark took the line first, when a free line is within reach."""
        from core.model import Beam
        a = Beam(mark="", kind="beam", start=(0.0, 0.0), end=(5000.0, 0.0),
                 width=230.0, width_source="multiline",
                 centreline_source="multiline-faces", offset_applied=115.0,
                 source_layer="BEAM", handle="A")
        b = Beam(mark="", kind="beam", start=(0.0, 400.0), end=(5000.0, 400.0),
                 width=230.0, width_source="multiline",
                 centreline_source="multiline-faces", offset_applied=115.0,
                 source_layer="BEAM", handle="B")
        res = extract.Result(beams=[a, b])
        # both marks sit nearer to beam A, but B is free and within reach
        tags = [("B-1", (2500.0, 100.0), "t1", "BEAM NO (H)"),
                ("B-2", (2500.0, 180.0), "t2", "BEAM NO (H)")]
        extract._match_beam_tags(tags, res, self.tol)
        self.assertEqual({a.mark, b.mark}, {"B-1", "B-2"},
                         "both marks should be placed, one per beam")

    def test_a_mark_is_not_flung_onto_a_distant_beam(self):
        """Better to report a mark as unplaced than to put it on a beam metres away."""
        from core.model import Beam
        a = Beam(mark="", kind="beam", start=(0.0, 0.0), end=(5000.0, 0.0),
                 width=230.0, width_source="multiline",
                 centreline_source="multiline-faces", offset_applied=115.0,
                 source_layer="BEAM", handle="A")
        res = extract.Result(beams=[a])
        tags = [("B-1", (2500.0, 100.0), "t1", "BEAM NO (H)"),
                ("B-2", (2500.0, 150.0), "t2", "BEAM NO (H)")]
        extract._match_beam_tags(tags, res, self.tol)
        self.assertEqual(a.mark, "B-1", "the closer mark wins the only beam")
        unplaced = [i for i in res.issues if "was not placed" in i.message]
        self.assertEqual(len(unplaced), 1)
        self.assertEqual(unplaced[0].mark, "B-2")

    def test_stacked_duplicate_is_dropped_keeping_the_longer(self):
        long_ = self._plain((0.0, 0.0), (5000.0, 0.0))
        short = self._plain((500.0, 0.0), (4000.0, 0.0))
        res = extract.Result(beams=[long_, short])
        extract._drop_stacked_beams(res, self.tol)
        self.assertEqual(len(res.beams), 1)
        self.assertAlmostEqual(res.beams[0].length, 5000.0, delta=1.0)


# ------------------------------------------------------------- round trips
# ---------------------------------------------------------------- blocks

BLK_PLAN_CENTRE = (40000.0, 20000.0)      # a column drawn inside a 1:1 block
BLK_DETAIL_CENTRE = (60000.0, 20000.0)    # a column inside a 4x detail block


def build_block_fixture(path: str) -> str:
    """Two columns that exist only inside blocks, one at plan scale, one not.

    This is the shape of the 3rd floor plan, where the consultant drew the whole
    column grid once and inserted it as a single block. Reading model space
    alone finds neither column.
    """
    doc = ezdxf.new("R2010", setup=True)
    doc.header["$INSUNITS"] = 4
    msp = doc.modelspace()
    for lay in ("COLUMN", "colno", "BEAM"):
        if lay not in doc.layers:
            doc.layers.add(lay)

    # the plan, drawn about its own origin and inserted rotated, as CAD does it
    grid = doc.blocks.new("COLUMN-GRID")
    grid.add_lwpolyline(rotated_rect(0.0, 0.0, COL_W, COL_D, 0.0),
                        close=True, dxfattribs={"layer": "COLUMN"})
    grid.add_text("7", dxfattribs={"layer": "colno", "height": 250})         .set_placement((200.0, 0.0))
    msp.add_blockref("COLUMN-GRID", BLK_PLAN_CENTRE,
                     dxfattribs={"layer": "COLUMN", "rotation": 0.0})

    # a section detail, same layers, inserted at 4x - must NOT become a column
    det = doc.blocks.new("SECTION-DETAIL")
    det.add_lwpolyline(rotated_rect(0.0, 0.0, COL_W, COL_D, 0.0),
                       close=True, dxfattribs={"layer": "COLUMN"})
    det.add_lwpolyline(rotated_rect(0.0, 2000.0, COL_W, COL_D, 0.0),
                       close=True, dxfattribs={"layer": "COLUMN"})
    det.add_lwpolyline(rotated_rect(0.0, 4000.0, COL_W, COL_D, 0.0),
                       close=True, dxfattribs={"layer": "COLUMN"})
    msp.add_blockref("SECTION-DETAIL", BLK_DETAIL_CENTRE,
                     dxfattribs={"layer": "COLUMN", "xscale": 4.0,
                                 "yscale": 4.0, "zscale": 4.0})

    doc.saveas(path)
    return path


class TestBlocks(TempCase):
    """Structure drawn inside a block reference must still be extracted."""

    def run_fixture(self, cfg=None):
        p = os.path.join(self.tmp, "blocks.dxf")
        build_block_fixture(p)
        return extract.extract(p, cfg or self.cfg, log=lambda *_: None)

    def test_column_inside_plan_scale_block_is_found(self):
        res = self.run_fixture()
        found = [c for c in res.columns
                 if abs(c.x - BLK_PLAN_CENTRE[0]) < 50 and abs(c.y - BLK_PLAN_CENTRE[1]) < 50]
        self.assertEqual(len(found), 1,
                         "the column inside the 1:1 block was not extracted")
        self.assertAlmostEqual(found[0].width, COL_W, delta=1.0)
        self.assertAlmostEqual(found[0].depth, COL_D, delta=1.0)

    def test_tag_inside_the_block_is_matched(self):
        res = self.run_fixture()
        found = [c for c in res.columns
                 if abs(c.x - BLK_PLAN_CENTRE[0]) < 50 and abs(c.y - BLK_PLAN_CENTRE[1]) < 50]
        self.assertEqual(found[0].mark, "7",
                         "the mark inside the block did not reach its column")

    def test_detail_block_at_another_scale_is_left_out(self):
        res = self.run_fixture()
        near = [c for c in res.columns if abs(c.x - BLK_DETAIL_CENTRE[0]) < 20000]
        self.assertEqual(near, [],
                         "a 4x section detail was extracted as real columns")

    def test_the_skipped_detail_block_is_reported_not_dropped_silently(self):
        res = self.run_fixture()
        said = [i for i in res.issues
                if i.category == "blocks" and "SECTION-DETAIL" in i.message]
        self.assertTrue(said, "a block holding structure was skipped without saying so")
        self.assertIn("4x", said[0].message)

    def test_explode_names_forces_a_block_open(self):
        cfg = pipeline.load_config()
        cfg.setdefault("blocks", {})["explode_names"] = ["SECTION-DETAIL"]
        res = self.run_fixture(cfg)
        near = [c for c in res.columns if abs(c.x - BLK_DETAIL_CENTRE[0]) < 20000]
        self.assertEqual(len(near), 3,
                         "explode_names did not force the block open")

    def test_explode_false_reads_model_space_only(self):
        cfg = pipeline.load_config()
        cfg.setdefault("blocks", {})["explode"] = False
        res = self.run_fixture(cfg)
        self.assertEqual(res.columns, [],
                         "with explode off, nothing inside a block should be read")

    def test_a_mirrored_outline_is_read_where_it_is_drawn(self):
        """ALCAZAR 1st floor: a slab outline in a mirrored block has its plane facing
        down, its stored X the drawing's X negated - read as stored it came out 3.3 km
        away, across the origin. Mirrored in a block, or drawn so loose, it is read
        where it stands on the drawing."""
        doc = ezdxf.new("R2010", setup=True)
        doc.header["$INSUNITS"] = 4
        msp = doc.modelspace()
        doc.layers.add("COLUMN")
        blk = doc.blocks.new("MIRRORED")
        blk.add_lwpolyline(rotated_rect(3000.0, 0.0, COL_W, COL_D, 0.0), close=True,
                           dxfattribs={"layer": "COLUMN"})
        msp.add_blockref("MIRRORED", (40000.0, 20000.0), dxfattribs={"xscale": -1.0})
        # drawn loose on a plane facing down: stored X = -(drawing X)
        msp.add_lwpolyline([(-x, y) for x, y in rotated_rect(50000.0, 20000.0, COL_W, COL_D, 0.0)],
                           close=True, dxfattribs={"layer": "COLUMN", "extrusion": (0, 0, -1)})
        p = os.path.join(self.tmp, "mirrored.dxf")
        doc.saveas(p)
        res = extract.extract(p, self.cfg, log=lambda *_: None)
        got = sorted((round(c.x), round(c.y)) for c in res.columns)
        self.assertEqual(got, [(37000, 20000), (50000, 20000)])


class TestGridBlockCopyOfTheColumns(TempCase):
    """ALCAZAR plinth: a 'Grid' block (grids + a copy of the column layout, 230 x
    1050) is inserted 1:1 under the plan, which draws its own hatched columns
    (400 x 1050) over it. Each column came out twice, the copy taking the mark,
    and two columns only in the copy stood alone in the slabs. The plan's own
    outline wins; what only the copy has is left out and said."""

    def build(self, own=True):
        p = os.path.join(self.tmp, "gridcopy.dxf")
        doc = ezdxf.new("R2010", setup=True)
        doc.header["$INSUNITS"] = 4
        msp = doc.modelspace()
        for lay in ("COLUMN", "colno", "BEAM"):
            doc.layers.add(lay)
        grid = doc.blocks.new("Grid 5")
        for k in range(3):
            grid.add_lwpolyline(rotated_rect(k * 6000.0, -85.0, 1050.0, 230.0, 0.0),
                                close=True, dxfattribs={"layer": "COLUMN"})
        grid.add_lwpolyline(rotated_rect(3000.0, 4000.0, 600.0, 230.0, 0.0),
                            close=True, dxfattribs={"layer": "COLUMN"})   # only in the copy
        msp.add_blockref("Grid 5", (0.0, 0.0), dxfattribs={"layer": "BEAM"})
        if own:
            for k in range(3):
                msp.add_lwpolyline(rotated_rect(k * 6000.0, 0.0, 1050.0, 400.0, 0.0),
                                   close=True, dxfattribs={"layer": "COLUMN"})
                msp.add_text(str(20 + k), dxfattribs={"layer": "colno", "height": 250}) \
                   .set_placement((k * 6000.0, 400.0))
        doc.saveas(p)
        return extract.extract(p, self.cfg, log=lambda *_: None)

    def test_the_plans_own_column_is_kept_once_with_its_mark(self):
        res = self.build()
        self.assertEqual(len(res.columns), 3)
        self.assertEqual(sorted((round(c.width), round(c.depth)) for c in res.columns), [(400, 1050)] * 3)
        self.assertEqual(sorted(c.mark for c in res.columns), ["20", "21", "22"])
        msgs = " ".join(i.message for i in res.issues if i.category == "geometry")
        self.assertIn("Grid 5", msgs)
        self.assertIn("230 x 1050", msgs)              # the other size is said, not hidden
        self.assertIn("not drawn on the plan", msgs)   # the copy-only column is said

    def test_a_grid_block_that_is_the_only_drawing_of_the_columns_is_still_read(self):
        res = self.build(own=False)
        self.assertEqual(len(res.columns), 4)


# ---------------------------------------------------------------- schedules

def build_schedule_fixture(path: str) -> str:
    """A plan with the beam schedule printed beside it, as the consultant does.

    The cells of one printed row do not share a baseline - they scatter a little
    in Y, exactly as they do in the real drawing - so this fixture also pins the
    bug where sorting by Y and X together turned 230 x 450 into 450 x 230.
    """
    doc = ezdxf.new("R2010", setup=True)
    doc.header["$INSUNITS"] = 4
    msp = doc.modelspace()
    for lay in ("BEAM", "BEAM NO (H)", "beam sch. text"):
        if lay not in doc.layers:
            doc.layers.add(lay)

    m = msp.add_mline([BEAM_A, BEAM_B], dxfattribs={"layer": "BEAM"})
    m.set_scale_factor(BEAM_WIDTH)
    m.set_justification(0)
    msp.add_text("B1", dxfattribs={"layer": "BEAM NO (H)", "height": 250}) \
       .set_placement((2500.0, -115.0))

    m2 = msp.add_mline([(0.0, 6000.0), (5000.0, 6000.0)], dxfattribs={"layer": "BEAM"})
    m2.set_scale_factor(BEAM_WIDTH)
    m2.set_justification(0)
    msp.add_text("B2", dxfattribs={"layer": "BEAM NO (H)", "height": 250}) \
       .set_placement((2500.0, 5885.0))

    # The schedule table, well clear of the plan. Y is jittered within each row,
    # as the real drawing is: its cells scatter over 40 mm.
    rows = [("B", "-", "1", "230", "450"),
            ("B", "-", "2", "230", "750")]
    y = 40000.0
    for row in rows:
        for i, cell in enumerate(row):
            # The left-hand cells sit LOWER than the right-hand ones, which is how
            # the real drawing is: the mark sits 40 mm below the size. Sorting by
            # Y and X together therefore reverses the row, and 230 x 450 is read
            # as 450 x 230. That is the bug this jitter exists to catch.
            jitter = (len(row) - 1 - i) * 10.0
            msp.add_text(cell, dxfattribs={"layer": "beam sch. text", "height": 200}) \
               .set_placement((40000.0 + i * 900.0, y - jitter))
        y -= 2000.0

    doc.saveas(path)
    return path


class TestSchedule(TempCase):
    """The beam schedule printed on the drawing is the only source of depth."""

    def run_fixture(self, cfg=None):
        p = os.path.join(self.tmp, "sched.dxf")
        build_schedule_fixture(p)
        c = cfg or pipeline.load_config()
        c.setdefault("schedules", {})["beam_layers"] = ["beam sch. text"]
        return extract.extract(p, c, log=lambda *_: None)

    def beam(self, res, mark):
        for b in res.beams:
            if b.mark == mark:
                return b
        return None

    def test_depth_is_read_from_the_drawn_schedule(self):
        res = self.run_fixture()
        self.assertEqual(self.beam(res, "B1").depth, 450.0)
        self.assertEqual(self.beam(res, "B2").depth, 750.0)

    def test_width_and_depth_are_not_swapped(self):
        """The bug that made every beam 230 deep and 450 wide."""
        res = self.run_fixture()
        b = self.beam(res, "B1")
        self.assertAlmostEqual(b.width, 230.0, delta=1.0)
        self.assertEqual(b.depth, 450.0)

    def test_depth_source_is_recorded(self):
        res = self.run_fixture()
        self.assertEqual(self.beam(res, "B1").depth_source, "drawing schedule")

    def test_rows_survive_the_baseline_jitter(self):
        """Cells of one row scatter in Y; they must still read left to right."""
        p = os.path.join(self.tmp, "sched.dxf")
        build_schedule_fixture(p)
        ents = list(ezdxf.readfile(p).modelspace())
        rows = schedule.rows_from(ents, "beam sch. text")
        self.assertEqual(rows[0], ["B", "-", "1", "230", "450"])

    def test_a_mark_absent_from_the_schedule_keeps_no_depth(self):
        """Nothing is invented for a beam the schedule does not cover."""
        res = self.run_fixture()
        for b in res.beams:
            if not b.mark:
                self.assertIsNone(b.depth)

    def test_reading_can_be_turned_off(self):
        cfg = pipeline.load_config()
        cfg.setdefault("schedules", {})["read"] = False
        res = self.run_fixture(cfg)
        self.assertIsNone(self.beam(res, "B1").depth)


# ---------------------------------------------------------------- legend

class TestLegendAndDuplicates(TempCase):
    """A legend is drawn on the structural layers, and must not be modelled."""

    def fixture_with(self, legend=True, twice=False):
        doc = ezdxf.new("R2010", setup=True)
        doc.header["$INSUNITS"] = 4
        msp = doc.modelspace()
        for lay in ("COLUMN", "colno", "TXT", "MAIN TEXT"):
            if lay not in doc.layers:
                doc.layers.add(lay)

        corners = rotated_rect(*COL_CENTRE, COL_W, COL_D, COL_ROT)
        msp.add_lwpolyline(corners, close=True, dxfattribs={"layer": "COLUMN"})
        if twice:                       # the same outline drawn on top of itself
            msp.add_lwpolyline(corners, close=True, dxfattribs={"layer": "COLUMN"})
        msp.add_text("5", dxfattribs={"layer": "colno", "height": 250}) \
           .set_placement((COL_CENTRE[0] + 200, COL_CENTRE[1]))

        if legend:
            lx, ly = 60000.0, 60000.0
            msp.add_text("LEGEND-:", dxfattribs={"layer": "MAIN TEXT", "height": 300}) \
               .set_placement((lx, ly))
            captions = (": COLUMN", ": COLUMN STOP", ": BEAM")
            for k, caption in enumerate(captions):
                cy = ly - 900.0 * (k + 1)
                msp.add_lwpolyline(rotated_rect(lx + 400.0, cy, 500.0, 1200.0, 0.0),
                                   close=True, dxfattribs={"layer": "COLUMN"})
                msp.add_text(caption, dxfattribs={"layer": "TXT", "height": 200}) \
                   .set_placement((lx + 1600.0, cy))

        p = os.path.join(self.tmp, "legend.dxf")
        doc.saveas(p)
        return p

    def test_legend_swatches_do_not_become_columns(self):
        res = extract.extract(self.fixture_with(legend=True), self.cfg,
                              log=lambda *_: None)
        self.assertEqual(len(res.columns), 1,
                         "the legend sample boxes were extracted as real columns")
        self.assertEqual(res.columns[0].mark, "5")

    def test_without_a_legend_nothing_is_excluded(self):
        res = extract.extract(self.fixture_with(legend=False), self.cfg,
                              log=lambda *_: None)
        self.assertEqual(len(res.columns), 1)
        self.assertEqual([i for i in res.issues if i.category == "legend"], [])

    def test_the_exclusion_is_reported(self):
        res = extract.extract(self.fixture_with(legend=True), self.cfg,
                              log=lambda *_: None)
        said = [i for i in res.issues if i.category == "legend"]
        self.assertTrue(said, "the legend was excluded without saying so")

    def test_legend_detection_can_be_turned_off(self):
        cfg = pipeline.load_config()
        cfg.setdefault("legend", {})["detect"] = False
        res = extract.extract(self.fixture_with(legend=True), cfg, log=lambda *_: None)
        self.assertEqual(len(res.columns), 4,
                         "with detection off the legend boxes should come through")

    def test_an_outline_drawn_twice_makes_one_column(self):
        res = extract.extract(self.fixture_with(legend=False, twice=True), self.cfg,
                              log=lambda *_: None)
        self.assertEqual(len(res.columns), 1,
                         "a repeated outline became two columns stacked at one point")

    def test_the_repeat_is_reported(self):
        res = extract.extract(self.fixture_with(legend=False, twice=True), self.cfg,
                              log=lambda *_: None)
        said = [i for i in res.issues if "drawn twice in the same place" in i.message]
        self.assertTrue(said, "a duplicated outline was dropped without saying so")


class TestRoundColumns(TempCase):
    """Akanistha draws C6 as a 650 circle on the column layer, its mark beside it: it was
    never read. VILLA5 draws its slab marks in circles on the column layer: not columns."""

    def fixture(self):
        doc = ezdxf.new("R2010", setup=True)
        doc.header["$INSUNITS"] = 4
        msp = doc.modelspace()
        for lay in ("COLUMN", "colno", "TXT"):
            if lay not in doc.layers:
                doc.layers.add(lay)
        col = {"layer": "COLUMN"}
        msp.add_circle((0.0, 0.0), 325.0, dxfattribs=col)                   # round column C6
        msp.add_text("C6", dxfattribs={"layer": "colno", "height": 250}).set_placement((600.0, 0.0))
        msp.add_circle((10000.0, 0.0), 180.0, dxfattribs=col)               # a slab mark's bubble
        msp.add_text("S3", dxfattribs={"layer": "TXT", "height": 150}).set_placement((9950.0, -60.0))
        msp.add_circle((20000.0, 0.0), 210.0, dxfattribs=col)               # a tube: 420 outside
        msp.add_circle((20000.0, 0.0), 200.0, dxfattribs=col)               # ... 400 inside
        msp.add_lwpolyline(rotated_rect(30000.0, 0.0, 300.0, 600.0, 0.0), close=True, dxfattribs=col)
        msp.add_circle((30000.0, 0.0), 100.0, dxfattribs=col)               # a detail inside a column
        msp.add_lwpolyline([(39750.0, 0.0, 0, 0, 1.0), (40250.0, 0.0, 0, 0, 1.0)], format="xyseb",
                           close=True, dxfattribs=col)                       # a donut, 500 across
        p = os.path.join(self.tmp, "round.dxf")
        doc.saveas(p)
        return p

    def run_it(self):
        return extract.extract(self.fixture(), self.cfg, log=lambda *_: None)

    def test_a_circle_on_the_column_layer_is_a_round_column(self):
        res = self.run_it()
        c6 = [c for c in res.columns if abs(c.x) < 1 and abs(c.y) < 1]
        self.assertEqual(len(c6), 1, "the 650 circle was not read as a column")
        c6 = c6[0]
        self.assertEqual((c6.diameter, c6.width, c6.depth), (650.0, 650.0, 650.0))
        self.assertEqual(len(c6.outline), extract.ROUND_SIDES)
        self.assertEqual(c6.mark, "C6")

    def test_bubbles_tubes_and_details_are_not_columns_of_their_own(self):
        res = self.run_it()
        self.assertEqual(sorted(c.diameter or 0 for c in res.columns), [0, 420.0, 500.0, 650.0],
                         "a bubble, the inside of a tube or a detail in a column became a column")
        tube = next(c for c in res.columns if c.diameter == 420.0)
        self.assertTrue(any("tube" in n for n in tube.notes))

    def test_a_rectangular_column_is_written_as_before(self):
        res = self.run_it()
        rows = res.to_dict()["columns"]
        rect = [r for r in rows if abs(r["x"] - 30000.0) < 1]
        self.assertEqual(len(rect), 1)
        self.assertNotIn("diameter", rect[0], "model.json gained a field for every rectangular column")
        self.assertTrue(all(r.get("diameter") for r in rows if r is not rect[0]))


class TestLegendFromTheDrawing(TempCase):
    """No legend is built into the tool (the office table was removed at the user's
    request): every sunk depth comes from the drawings - a legend with or without a
    heading, a legend sheet of the same set, or the words written in the area."""

    def fixture(self, legend_rows=(), schedule_column=False, written=None, pattern="AR-PARQ1"):
        doc = ezdxf.new("R2010", setup=True)
        doc.header["$INSUNITS"] = 4
        msp = doc.modelspace()
        h = msp.add_hatch(dxfattribs={"layer": "SUNK"})       # the plan's sunk area
        h.set_pattern_fill(pattern, scale=1.0)
        h.paths.add_polyline_path([(0, 0), (3000, 0), (3000, 3000), (0, 3000)])
        if written:
            msp.add_text(written, dxfattribs={"layer": "TXT", "height": 200}).set_placement((1200, 1500))
        # a legend with no heading: captions one above another, a sample right of each
        for k, (cap, pat) in enumerate(legend_rows):
            y = 60000.0 - 1000.0 * k
            msp.add_text(cap, dxfattribs={"layer": "TXT", "height": 200}).set_placement((60000.0, y))
            s = msp.add_hatch(dxfattribs={"layer": "HATCH LIGHT"})
            s.set_pattern_fill(pat, scale=1.0)
            s.paths.add_polyline_path([(64500, y - 350), (66000, y - 350), (66000, y + 350), (64500, y + 350)])
        if schedule_column:     # a slab schedule's remarks: lined up, but no samples
            for k, cap in enumerate(("400mm SUNK", "150mm SUNK", "100mm SUNK")):
                y = 90000.0 - 600.0 * k
                msp.add_text(cap, dxfattribs={"layer": "slab sch. text", "height": 150}).set_placement((90000.0, y))
                msp.add_line((88000, y - 300), (95000, y - 300), dxfattribs={"layer": "slab sch. text"})
        p = os.path.join(self.tmp, "sunk.dxf")
        doc.saveas(p)
        return p

    ROWS = (("100MM SUNK", "ANGLE"), ("150MM SUNK", "ANSI36"), ("250MM SUNK", "AR-PARQ1"))

    def run_it(self, path, cfg=None):
        return extract.extract(path, cfg or self.cfg, log=lambda *_: None)

    def test_no_office_legend_is_built_in(self):
        self.assertNotIn("standard_legend", self.cfg)
        self.assertFalse(hasattr(legend, "standard"))

    def test_a_legend_with_no_heading_is_read_from_the_drawing(self):
        res = self.run_it(self.fixture(self.ROWS))
        self.assertEqual([(z.depth_mm, z.depth_source) for z in res.sunk], [(250.0, "legend")],
                         "the samples must be read as the key, and left out of the model")

    def test_a_schedule_remarks_column_is_not_a_legend(self):
        ents = list(ezdxf.readfile(self.fixture(schedule_column=True)).modelspace())
        self.assertEqual(legend.find_caption_columns(ents, legend.settings(self.cfg)), [])

    def test_the_depth_written_inside_the_area(self):
        for text, want in (("SUNK 150", 150.0), ("(-200)", 200.0), ("-0.250", 250.0), ("300MM SUNK", 300.0)):
            res = self.run_it(self.fixture(written=text))
            self.assertEqual([(z.depth_mm, z.depth_source) for z in res.sunk], [(want, "text in the area")], text)

    def test_nothing_says_it_nothing_is_guessed(self):
        res = self.run_it(self.fixture())
        self.assertEqual([z.depth_mm for z in res.sunk], [None])

    def test_the_sets_legend_sheet_when_the_drawing_has_none(self):
        cfg = pipeline.load_config()
        cfg["set_legend"] = {"sheet": "KEY", "samples": [["HATCH LIGHT", "AR-PARQ1", 1.0, 250.0]], "lines": []}
        res = self.run_it(self.fixture(), cfg)
        self.assertEqual([(z.depth_mm, z.depth_source) for z in res.sunk], [(250.0, "legend sheet KEY")])

    def test_the_drawings_own_legend_comes_first(self):
        cfg = pipeline.load_config()
        cfg["set_legend"] = {"sheet": "KEY", "samples": [["HATCH LIGHT", "AR-PARQ1", 1.0, 900.0]], "lines": []}
        res = self.run_it(self.fixture(self.ROWS), cfg)
        self.assertEqual([z.depth_mm for z in res.sunk], [250.0])


# ---------------------------------------------------------------- beam joining

def build_join_fixture(path: str) -> str:
    """Two situations the joining has to tell apart.

    Row 1 is a beam that genuinely falls short: of a column at one end and of a
    crossing beam at the other. Both ends should be walked out to their support.

    Row 2 is the trap. Beam G ends at x = 4000. Beam E is 2 m away and does not
    reach it, but the INFINITE LINE through E crosses G 600 mm past its end, well
    inside the snap distance. Joining to that is how a beam ends up pointing at
    empty space, so G must be left exactly where it is.
    """
    doc = ezdxf.new("R2010", setup=True)
    doc.header["$INSUNITS"] = 4
    msp = doc.modelspace()
    for lay in ("COLUMN", "BEAM"):
        if lay not in doc.layers:
            doc.layers.add(lay)

    # row 1: a 230 x 950 column at the origin. rotated_rect puts the LONG side
    # along the given angle, so at 0 degrees it spans x = -475 .. +475.
    msp.add_lwpolyline(rotated_rect(0.0, 0.0, COL_W, COL_D, 0.0),
                       close=True, dxfattribs={"layer": "COLUMN"})
    # beam A stops 525 mm short of the column face and 115 mm short of beam B
    msp.add_line((1000.0, 0.0), (4885.0, 0.0), dxfattribs={"layer": "BEAM"})
    # beam B, crossing A at x = 5000
    msp.add_line((5000.0, -2000.0), (5000.0, 2000.0), dxfattribs={"layer": "BEAM"})

    # row 2: the phantom intersection
    msp.add_line((0.0, 20000.0), (4000.0, 20000.0), dxfattribs={"layer": "BEAM"})
    msp.add_line((4600.0, 22000.0), (4600.0, 24000.0), dxfattribs={"layer": "BEAM"})

    doc.saveas(path)
    return path


class TestBeamJoining(TempCase):
    """A beam end is walked to its support, or left alone - never to nowhere."""

    def setUp(self):
        super().setUp()
        p = os.path.join(self.tmp, "join.dxf")
        build_join_fixture(p)
        self.res = extract.extract(p, self.cfg, log=lambda *_: None)

    def find(self, x1, y1, x2, y2, tol=800.0):
        """The beam that started life closest to this pair of points."""
        best, bd = None, 1e18
        for b in self.res.beams:
            for s, e in ((b.start, b.end), (b.end, b.start)):
                d = G.dist(s, (x1, y1)) + G.dist(e, (x2, y2))
                if d < bd:
                    best, bd = (b, s, e), d
        return best if bd < tol * 4 else None

    def test_a_beam_is_extended_into_its_column(self):
        got = self.find(1000.0, 0.0, 5000.0, 0.0)
        self.assertIsNotNone(got, "beam A was not extracted")
        _b, s, _e = got
        self.assertLess(abs(s[1]), 1.0, "the end moved sideways, off its own line")
        self.assertLess(s[0], 480.0,
                        "beam A was not extended to the column: start x = %.0f" % s[0])
        self.assertGreater(s[0], -480.0, "beam A was pushed straight through the column")

    def test_a_beam_is_extended_to_the_beam_that_crosses_it(self):
        got = self.find(1000.0, 0.0, 5000.0, 0.0)
        _b, _s, e = got
        self.assertAlmostEqual(e[0], 5000.0, delta=1.0)
        self.assertAlmostEqual(e[1], 0.0, delta=1.0)

    def test_a_phantom_intersection_is_refused(self):
        """The crossing must be on the neighbour, not on the line through it."""
        got = self.find(0.0, 20000.0, 4000.0, 20000.0)
        self.assertIsNotNone(got, "beam G was not extracted")
        _b, _s, e = got
        self.assertAlmostEqual(e[0], 4000.0, delta=1.0,
                               msg="beam G was snapped to a crossing that is not there")

    def test_an_end_that_reaches_nothing_is_reported(self):
        said = [i for i in self.res.issues if "touch nothing" in i.message]
        self.assertTrue(said, "beam ends left floating were not reported")

    def test_no_end_was_moved_sideways(self):
        """Every repair runs along the beam own line, never across it."""
        for b in self.res.beams:
            for note in b.notes:
                if "extended" in note:
                    self.assertIn("along its own line", note)


class TestChainWidth(TempCase):
    """One beam, one width - even where it bends."""

    def fixture(self):
        doc = ezdxf.new("R2010", setup=True)
        doc.header["$INSUNITS"] = 4
        msp = doc.modelspace()
        if "BEAM" not in doc.layers:
            doc.layers.add("BEAM")

        # Two face polylines 200 apart, bending through 45 degrees. The second
        # face is offset in Y, which is how drafters draw it, so the diagonal
        # runs measure 200/root 2 = 141 mm across instead of 200.
        face_a = [(0.0, 0.0), (3000.0, 0.0), (4000.0, 1000.0), (7000.0, 1000.0)]
        face_b = [(p[0], p[1] - 200.0) for p in face_a]
        msp.add_lwpolyline(face_a, dxfattribs={"layer": "BEAM"})
        msp.add_lwpolyline(face_b, dxfattribs={"layer": "BEAM"})

        p = os.path.join(self.tmp, "chain.dxf")
        doc.saveas(p)
        return extract.extract(p, self.cfg, log=lambda *_: None)

    def test_the_bend_is_not_left_narrower_than_the_run(self):
        res = self.fixture()
        merged = [b for b in res.beams if b.centreline_source == "face-pair"]
        self.assertGreaterEqual(len(merged), 2, "the face pair was not merged")
        for b in merged:
            self.assertAlmostEqual(b.width, 200.0, delta=2.0,
                                   msg="a bend kept its 141 mm diagonal reading")

    def test_the_correction_is_reported(self):
        res = self.fixture()
        said = [i for i in res.issues if "narrower than the rest" in i.message]
        self.assertTrue(said, "widths were corrected without saying so")

    def test_the_offset_matches_the_corrected_width(self):
        res = self.fixture()
        for b in res.beams:
            if b.centreline_source == "face-pair" and b.width:
                self.assertAlmostEqual(b.offset_applied, b.width / 2.0, delta=0.6)


class TestPlanView(TempCase):
    """The report carries a plan, because a bad beam is visible and not tabular."""

    def test_the_report_holds_a_drawable_plan(self):
        res = self.fixture_result()
        svg = report.plan_svg(res)
        self.assertIn("<svg", svg)
        self.assertIn("</svg>", svg)
        self.assertGreater(svg.count("<line"), 0, "no beams were drawn")

    def test_a_free_end_is_marked_on_the_plan(self):
        res = self.fixture_result()
        svg = report.plan_svg(res)
        self.assertIn("an end touching nothing", svg)

    def test_an_empty_result_does_not_crash(self):
        from core.model import Result
        self.assertEqual(report.plan_svg(Result(dxf_file="none")), "")

    def fixture_result(self):
        p = os.path.join(self.tmp, "fixture.dxf")
        build_fixture(p)
        return extract.extract(p, self.cfg, log=lambda *_: None)


# ---------------------------------------------------------------- loose lines

def build_line_column_fixture(path: str, detached: bool = False) -> str:
    """A column drawn as four separate lines instead of one closed polyline.

    Identical on the sheet, and invisible to anything that only reads polylines.
    On the Wing B first floor 127 of the 142 entities on the COLUMN layer were
    loose lines like these, and 31 columns never reached the model.
    """
    doc = ezdxf.new("R2010", setup=True)
    doc.header["$INSUNITS"] = 4
    msp = doc.modelspace()
    for lay in ("COLUMN", "colno", "BEAM"):
        if lay not in doc.layers:
            doc.layers.add(lay)

    corners = rotated_rect(*COL_CENTRE, COL_W, COL_D, COL_ROT)
    for i in range(4):
        msp.add_line(corners[i], corners[(i + 1) % 4], dxfattribs={"layer": "COLUMN"})
    msp.add_text("5", dxfattribs={"layer": "colno", "height": 250}) \
       .set_placement((COL_CENTRE[0] + 200, COL_CENTRE[1]))

    # a stray line that closes nothing: it must be left alone, not invented into
    # a shape
    msp.add_line((COL_CENTRE[0] + 6000, COL_CENTRE[1]),
                 (COL_CENTRE[0] + 8000, COL_CENTRE[1]),
                 dxfattribs={"layer": "COLUMN"})

    # enough beams near the column that the debris is a small share of the whole,
    # which is one of the two things that has to be true before it is dropped
    for k in range(20):
        y = COL_CENTRE[1] + 1500 * k
        msp.add_line((COL_CENTRE[0], y), (COL_CENTRE[0] + 4000, y),
                     dxfattribs={"layer": "BEAM"})

    if detached:
        # debris a long way off, like the 2.4 km fragment on the Wing B sheet
        fx, fy = COL_CENTRE[0] + 2000000.0, COL_CENTRE[1] + 2000000.0
        for k in range(3):
            msp.add_line((fx, fy + 400 * k), (fx + 2000, fy + 400 * k),
                         dxfattribs={"layer": "BEAM"})

    doc.saveas(path)
    return path


class TestLinesIntoOutlines(TempCase):
    """A column drawn as four lines is still a column."""

    def run_it(self, cfg=None, detached=False):
        p = os.path.join(self.tmp, "lines.dxf")
        build_line_column_fixture(p, detached=detached)
        return extract.extract(p, cfg or self.cfg, log=lambda *_: None)

    def test_four_lines_become_a_column(self):
        res = self.run_it()
        near = [c for c in res.columns
                if abs(c.x - COL_CENTRE[0]) < 50 and abs(c.y - COL_CENTRE[1]) < 50]
        self.assertEqual(len(near), 1,
                         "a column drawn as four lines was not assembled")
        self.assertAlmostEqual(near[0].width, COL_W, delta=1.0)
        self.assertAlmostEqual(near[0].depth, COL_D, delta=1.0)

    def test_the_assembled_column_still_takes_its_mark(self):
        res = self.run_it()
        self.assertEqual(res.columns[0].mark, "5")

    def test_it_says_the_outline_was_assembled(self):
        res = self.run_it()
        self.assertTrue(any("assembled" in n for n in res.columns[0].notes))
        self.assertTrue([i for i in res.issues if "assembled from" in i.message],
                        "stitching loose lines was not reported")

    def test_a_line_that_closes_nothing_invents_nothing(self):
        res = self.run_it()
        self.assertEqual(len(res.columns), 1,
                         "a dangling line was turned into a shape")

    def test_stitching_can_be_turned_off(self):
        cfg = pipeline.load_config()
        cfg.setdefault("assemble_loops", {})["roles"] = []
        res = self.run_it(cfg)
        self.assertEqual(res.columns, [],
                         "with stitching off, loose lines should stay loose")


class TestDetachedIslands(TempCase):
    """Debris far from the building is not structure."""

    def run_it(self, cfg=None):
        p = os.path.join(self.tmp, "lines.dxf")
        build_line_column_fixture(p, detached=True)
        return extract.extract(p, cfg or self.cfg, log=lambda *_: None)

    def test_debris_two_kilometres_away_is_left_out(self):
        res = self.run_it()
        far = [b for b in res.beams
               if abs(b.start[0] - COL_CENTRE[0]) > 100000]
        self.assertEqual(far, [], "debris far from the building was modelled")

    def test_the_building_itself_survives(self):
        res = self.run_it()
        self.assertEqual(len(res.columns), 1)
        self.assertGreaterEqual(len(res.beams), 4,
                                "the real beams were dropped with the debris")

    def test_it_is_reported_not_silently_dropped(self):
        res = self.run_it()
        said = [i for i in res.issues if "from the building" in i.message]
        self.assertTrue(said, "detached debris was dropped without saying so")

    def test_dropping_can_be_turned_off(self):
        cfg = pipeline.load_config()
        cfg.setdefault("islands", {})["drop"] = False
        res = self.run_it(cfg)
        far = [b for b in res.beams
               if abs(b.start[0] - COL_CENTRE[0]) > 100000]
        self.assertTrue(far, "with dropping off the debris should come through")


# ---------------------------------------------------------------- hatch lines

def build_hatch_fixture(path: str, spacing: float = 300.0, n: int = 20) -> str:
    """A real beam beside a zone shaded with lines on the same layer.

    Every one of those shading lines is straight, on the beam layer, and about
    the length of a beam. On the Wing B first floor 162 of the 186 lines on
    GIRDER BEAM were exactly this, and 79 of them became girders.
    """
    doc = ezdxf.new("R2010", setup=True)
    doc.header["$INSUNITS"] = 4
    msp = doc.modelspace()
    for lay in ("BEAM", "BEAM NO (H)"):
        if lay not in doc.layers:
            doc.layers.add(lay)

    m = msp.add_mline([BEAM_A, BEAM_B], dxfattribs={"layer": "BEAM"})
    m.set_scale_factor(BEAM_WIDTH)
    m.set_justification(0)
    msp.add_text("B1", dxfattribs={"layer": "BEAM NO (H)", "height": 250}) \
       .set_placement((2500.0, -115.0))

    # the fill: n parallel lines at 45 degrees, evenly spaced, on the beam layer
    for k in range(n):
        x = 20000.0 + k * spacing
        msp.add_line((x, 20000.0), (x + 2200.0, 22200.0),
                     dxfattribs={"layer": "BEAM"})

    doc.saveas(path)
    return path


class TestHatchLines(TempCase):
    """Shading drawn as lines on a structural layer is not structure."""

    def run_it(self, cfg=None, spacing=300.0, n=20):
        p = os.path.join(self.tmp, "hatch.dxf")
        build_hatch_fixture(p, spacing=spacing, n=n)
        return extract.extract(p, cfg or self.cfg, log=lambda *_: None)

    def test_the_fill_does_not_become_beams(self):
        res = self.run_it()
        far = [b for b in res.beams if b.start[0] > 15000.0]
        self.assertEqual(far, [], "a hatch fill was extracted as beams")

    def test_the_real_beam_survives(self):
        res = self.run_it()
        self.assertTrue([b for b in res.beams if b.mark == "B1"],
                        "the real beam went with the hatching")

    def test_it_is_reported(self):
        res = self.run_it()
        said = [i for i in res.issues if "hatch fill" in i.message]
        self.assertTrue(said, "the fill was dropped without saying so")

    def test_widely_spaced_parallel_beams_are_kept(self):
        """The test is the spacing, not the angle. Real beams are far apart."""
        res = self.run_it(spacing=4000.0)
        far = [b for b in res.beams if b.start[0] > 15000.0]
        self.assertTrue(far, "beams 4 m apart were mistaken for hatching")

    def test_a_few_parallel_lines_are_kept(self):
        """Three lines are not a fill, however close together."""
        res = self.run_it(n=3)
        far = [b for b in res.beams if b.start[0] > 15000.0]
        self.assertTrue(far, "three parallel lines were treated as a fill")

    def test_detection_can_be_turned_off(self):
        cfg = pipeline.load_config()
        cfg.setdefault("hatch_lines", {})["detect"] = False
        res = self.run_it(cfg)
        far = [b for b in res.beams if b.start[0] > 15000.0]
        self.assertTrue(far, "with detection off the fill should come through")

    def test_beam_faces_pair_after_pair_are_not_a_fill(self):
        """New block: SB beams in a row of toilets - faces 230 apart, pairs 1.1 m apart -
        measured a regular 230 spacing over the whole plan and were dropped as shading."""
        from core import hatchlines

        class E:
            def __init__(self):
                self.dxf = type("D", (), {"layer": "SB"})()
        lines = []
        for row in (0.0, 4000.0):
            for i in range(4):
                x = i * 1300.0
                lines += [(E(), (x, row), (x, row + 1435.0)), (E(), (x + 230.0, row), (x + 230.0, row + 1435.0))]
        self.assertEqual(hatchlines.find_fills(lines, hatchlines.settings({})), [])
        fill = [(E(), (i * 300.0, 0.0), (i * 300.0, 3000.0)) for i in range(10)]   # real shading
        self.assertEqual(len(hatchlines.find_fills(fill, hatchlines.settings({}))), 1)


class TestBeamFacesBrokenAtJoints(unittest.TestCase):
    """A beam's faces broken where other beams meet it must still make one whole beam."""

    def beam(self, a, b, layer="BEAM-230X450"):
        from core.model import Beam
        return Beam(mark="", kind="beam", start=a, end=b, width=None, width_source="", centreline_source="as-drawn",
                    offset_applied=0.0, source_layer=layer, handle="")

    def test_a_face_broken_by_a_crossing_beam_carries_the_beam_on(self):
        """NB2: top face in two pieces (broken 230 mm for an SB), bottom face whole."""
        faces = [(0.0, 0.0), (4145.0, 0.0), (0.0, 230.0), (1470.0, 230.0)]
        rest = self.beam((1700.0, 230.0), (4145.0, 230.0))
        self.assertTrue(extract._continues_face(rest, faces))
        stray = self.beam((1700.0, 230.0), (3000.0, 230.0))      # ends where neither face ends
        self.assertFalse(extract._continues_face(stray, faces))

    def test_faces_broken_at_staggered_places_make_one_beam(self):
        """The stair's column line: left face broken lower down, right face higher up."""
        from core.model import Result
        res = Result(dxf_file="t")
        a = self.beam((0.0, 0.0), (0.0, -700.0))
        a.centreline_source, a.width = "face-pair", 230.0
        a._face_ends = [(-115.0, 0.0), (-115.0, -465.0), (115.0, 0.0), (115.0, -1135.0)]
        b = self.beam((0.0, -1250.0), (0.0, -2600.0))
        b.centreline_source, b.width = "face-pair", 230.0
        b._face_ends = [(-115.0, -915.0), (-115.0, -2600.0), (115.0, -1365.0), (115.0, -2600.0)]
        res.beams = [a, b]
        extract._bridge_staggered_faces(res, {})
        self.assertEqual(len(res.beams), 1)
        self.assertAlmostEqual(min(res.beams[0].start[1], res.beams[0].end[1]), -2600.0)

    def test_a_lone_face_carrying_on_a_beam_goes_onto_its_centreline(self):
        """The right-hand column line: a face piece past the beam's end stayed on the face
        line, 150 mm off. It is put on the centreline with the beam's width."""
        from core.model import Result
        res = Result(dxf_file="t")
        k = self.beam((150.0, 0.0), (150.0, -2600.0), layer="BEAM-300X600")
        k.centreline_source, k.width = "face-pair", 300.0
        lone = self.beam((0.0, -3500.0), (0.0, -4500.0), layer="BEAM-300X600")
        far = self.beam((0.0, -9000.0), (0.0, -9500.0), layer="BEAM-300X600")      # nowhere near
        res.beams = [k, lone, far]
        extract._align_face_continuations(res, {})
        self.assertEqual((lone.start[0], lone.end[0], lone.width), (150.0, 150.0, 300.0))
        self.assertEqual((far.start[0], far.width), (0.0, None))

    def test_a_real_break_is_left_open(self):
        from core.model import Result
        res = Result(dxf_file="t")
        a = self.beam((0.0, 0.0), (0.0, -700.0))
        a.centreline_source, a.width = "face-pair", 230.0
        a._face_ends = [(-115.0, 0.0), (-115.0, -700.0), (115.0, 0.0), (115.0, -700.0)]
        b = self.beam((0.0, -1250.0), (0.0, -2600.0))
        b.centreline_source, b.width = "face-pair", 230.0
        b._face_ends = [(-115.0, -1250.0), (-115.0, -2600.0), (115.0, -1250.0), (115.0, -2600.0)]
        res.beams = [a, b]
        extract._bridge_staggered_faces(res, {})
        self.assertEqual(len(res.beams), 2)


# ---------------------------------------------------------------- wide girders

def build_wide_girder_fixture(path: str, mark: str = "GB5",
                              gap: float = 1500.0, tag_inside: bool = True,
                              sched_width: float = 1500.0) -> str:
    """A 1500 mm girder drawn as two face lines, with its mark between them.

    The ordinary face-pair limit is 700 mm, so without other evidence these two
    lines stay two beams, 750 mm either side of where the girder really is. The
    floating column that sits on the girder then touches nothing.
    """
    doc = ezdxf.new("R2010", setup=True)
    doc.header["$INSUNITS"] = 4
    msp = doc.modelspace()
    for lay in ("GIRDER BEAM", "BEAM NO (H)", "beam sch. text", "COLUMN", "colno"):
        if lay not in doc.layers:
            doc.layers.add(lay)

    # the two faces
    msp.add_line((0.0, 0.0), (12000.0, 0.0), dxfattribs={"layer": "GIRDER BEAM"})
    msp.add_line((0.0, gap), (12000.0, gap), dxfattribs={"layer": "GIRDER BEAM"})

    ty = gap / 2.0 if tag_inside else gap + 4000.0
    msp.add_text(mark, dxfattribs={"layer": "BEAM NO (H)", "height": 250}) \
       .set_placement((6000.0, ty))

    # a floating column sitting on the girder centreline
    msp.add_lwpolyline(rotated_rect(6000.0, gap / 2.0, COL_W, COL_D, 90.0),
                       close=True, dxfattribs={"layer": "COLUMN"})
    msp.add_text("FC1", dxfattribs={"layer": "colno", "height": 250}) \
       .set_placement((6000.0, gap / 2.0))

    # the schedule row: GB5 is 1500 wide and 850 deep
    for i, cell in enumerate(("GB", "-", mark[2:], "%g" % sched_width, "850")):
        msp.add_text(cell, dxfattribs={"layer": "beam sch. text", "height": 200}) \
           .set_placement((40000.0 + i * 900.0, 40000.0 - (4 - i) * 10.0))

    doc.saveas(path)
    return path


class TestWideGirders(TempCase):
    """A beam wider than the face-pair limit, merged on the drawing's own word."""

    def run_it(self, **kw):
        p = os.path.join(self.tmp, "wide.dxf")
        build_wide_girder_fixture(p, **kw)
        cfg = pipeline.load_config()
        cfg.setdefault("schedules", {})["beam_layers"] = ["beam sch. text"]
        return extract.extract(p, cfg, log=lambda *_: None)

    def girders(self, res):
        return [b for b in res.beams if b.kind == "girder"]

    def test_the_two_faces_become_one_girder(self):
        res = self.run_it()
        merged = [b for b in self.girders(res) if b.centreline_source == "face-pair"]
        self.assertEqual(len(merged), 1,
                         "a 1500 mm girder was left as its two faces")
        self.assertAlmostEqual(merged[0].width, 1500.0, delta=2.0)

    def test_the_centreline_is_between_the_faces(self):
        res = self.run_it()
        merged = [b for b in self.girders(res) if b.centreline_source == "face-pair"][0]
        self.assertAlmostEqual(merged.start[1], 750.0, delta=2.0)

    def test_the_floating_column_now_sits_on_it(self):
        res = self.run_it()
        merged = [b for b in self.girders(res) if b.centreline_source == "face-pair"][0]
        col = [c for c in res.columns if c.mark == "FC1"]
        self.assertTrue(col, "the floating column was not extracted")
        d = G.point_to_segment((col[0].x, col[0].y), merged.start, merged.end)
        self.assertLessEqual(d, merged.width / 2.0,
                             "the floating column still floats: %.0f mm off" % d)

    def test_without_a_mark_between_the_faces_nothing_is_merged(self):
        """The evidence has to be local, or ordinary beams get welded together."""
        res = self.run_it(tag_inside=False)
        merged = [b for b in self.girders(res) if b.centreline_source == "face-pair"]
        self.assertEqual(merged, [],
                         "two lines were merged with no mark to justify it")

    def test_a_gap_the_schedule_does_not_name_is_not_merged(self):
        """Faces 1100 apart, schedule says 1500: the evidence does not fit."""
        res = self.run_it(gap=1100.0, sched_width=1500.0)
        merged = [b for b in self.girders(res) if b.centreline_source == "face-pair"]
        self.assertEqual(merged, [],
                         "a gap matching no scheduled width was merged anyway")


# ---------------------------------------------------------------- orphan faces

def build_orphan_face_fixture(path: str, offset: float = 115.0,
                              give_it_a_mark: bool = False) -> str:
    """A beam drawn as two faces, plus a third line along one of those faces.

    The pair merges into one 230 mm beam. The third line finds no partner and
    survives as a beam of its own with no width, lying exactly half a beam width
    off the centreline of the beam that is already there. In Revit that is a thin
    extra beam pressed against a real one, ending in mid air.
    """
    doc = ezdxf.new("R2010", setup=True)
    doc.header["$INSUNITS"] = 4
    msp = doc.modelspace()
    for lay in ("BEAM", "BEAM NO (H)"):
        if lay not in doc.layers:
            doc.layers.add(lay)

    # the two faces of one 230 mm beam
    msp.add_line((0.0, 0.0), (6000.0, 0.0), dxfattribs={"layer": "BEAM"})
    msp.add_line((0.0, 230.0), (6000.0, 230.0), dxfattribs={"layer": "BEAM"})
    msp.add_text("B1", dxfattribs={"layer": "BEAM NO (H)", "height": 250}) \
       .set_placement((3000.0, 115.0))

    # the orphan: a shorter line running along the beam, `offset` off its centre
    msp.add_line((1500.0, 115.0 + offset), (3000.0, 115.0 + offset),
                 dxfattribs={"layer": "BEAM"})
    if give_it_a_mark:
        msp.add_text("B9", dxfattribs={"layer": "BEAM NO (H)", "height": 250}) \
           .set_placement((2250.0, 115.0 + offset))

    doc.saveas(path)
    return path


class TestOrphanFaces(TempCase):
    """A face whose partner merged without it is not a beam."""

    def run_it(self, **kw):
        p = os.path.join(self.tmp, "orphan.dxf")
        build_orphan_face_fixture(p, **kw)
        return extract.extract(p, self.cfg, log=lambda *_: None)

    def test_the_orphan_face_is_dropped(self):
        res = self.run_it()
        self.assertEqual(len(res.beams), 1,
                         "the leftover face survived as a beam of its own")
        self.assertEqual(res.beams[0].mark, "B1")
        self.assertAlmostEqual(res.beams[0].width, 230.0, delta=2.0)

    def test_it_is_reported(self):
        res = self.run_it()
        said = [i for i in res.issues if "one face of a beam" in i.message]
        self.assertTrue(said, "a beam was dropped without saying so")

    def test_a_line_at_some_other_offset_is_kept(self):
        """Only half a beam width says 'this is that beam's other face'."""
        res = self.run_it(offset=600.0)
        self.assertEqual(len(res.beams), 2,
                         "a line 600 mm away was dropped as a face")

    def test_a_tagged_line_is_never_dropped(self):
        """A mark means the consultant says it is an element. Keep it."""
        res = self.run_it(give_it_a_mark=True)
        marks = sorted(b.mark for b in res.beams if b.mark)
        self.assertIn("B9", marks, "a tagged beam was dropped as a leftover face")


def build_curve_fixture(path: str, *, as_lines: bool = False, twice: bool = False,
                        mark_on_curve: bool = False, straight_end: bool = False,
                        inner_split: bool = False) -> str:
    """A straight 230 mm beam drawn as two faces, whose end curves away.

    The curve is drawn as its two faces too: arcs on one centre, radii 1000 and
    1230, from 90 to 180 degrees, so its centreline starts exactly where the
    straight beam's does. `as_lines` draws each arc as a dozen short LINEs, the
    way the Wing B 2nd floor balcony beams are drawn.
    """
    doc = ezdxf.new("R2010", setup=True)
    doc.header["$INSUNITS"] = 4
    msp = doc.modelspace()
    for lay in ("BEAM", "BEAM NO (H)"):
        if lay not in doc.layers:
            doc.layers.add(lay)
    msp.add_line((1000.0, 0.0), (6000.0, 0.0), dxfattribs={"layer": "BEAM"})
    msp.add_line((1000.0, 230.0), (6000.0, 230.0), dxfattribs={"layer": "BEAM"})
    msp.add_text("B1", dxfattribs={"layer": "BEAM NO (H)", "height": 250}) \
       .set_placement((3500.0, 115.0))
    c = (1000.0, -1000.0)
    for _ in range(2 if twice else 1):
        for r in (1000.0, 1230.0):
            if as_lines:
                pts = [(c[0] + r * math.cos(math.radians(90 + 90 * i / 12)),
                        c[1] + r * math.sin(math.radians(90 + 90 * i / 12))) for i in range(13)]
                for a, b in zip(pts, pts[1:]):
                    msp.add_line(a, b, dxfattribs={"layer": "BEAM"})
            elif inner_split and r == 1000.0:
                # broken where another beam meets it (Akanistha Winter Villa)
                msp.add_arc(c, r, 90.0, 120.0, dxfattribs={"layer": "BEAM"})
                msp.add_arc(c, r, 128.0, 180.0, dxfattribs={"layer": "BEAM"})
            else:
                msp.add_arc(c, r, 90.0, 180.0, dxfattribs={"layer": "BEAM"})
    if mark_on_curve:
        a = math.radians(135)
        msp.add_text("B7", dxfattribs={"layer": "BEAM NO (H)", "height": 250}) \
           .set_placement((c[0] + 1115 * math.cos(a), c[1] + 1115 * math.sin(a)))
    if straight_end:
        # both faces carry on 220 mm straight past the curve's free end into the
        # face of B2, the way J1153 Wing C 3rd floor draws B101A meeting B101
        msp.add_line((0.0, -1000.0), (0.0, -1220.0), dxfattribs={"layer": "BEAM"})
        msp.add_line((-230.0, -1000.0), (-230.0, -1220.0), dxfattribs={"layer": "BEAM"})
        msp.add_line((-2000.0, -1220.0), (2000.0, -1220.0), dxfattribs={"layer": "BEAM"})
        msp.add_line((-2000.0, -1450.0), (2000.0, -1450.0), dxfattribs={"layer": "BEAM"})
        msp.add_text("B2", dxfattribs={"layer": "BEAM NO (H)", "height": 250}) \
           .set_placement((1000.0, -1335.0))
    doc.saveas(path)
    return path


class TestSlabMarksOnBeamLayers(unittest.TestCase):
    """Sasan Gir terrace: "S4" with "135 Thk" under it, on the beam mark layer, was read as a
    beam mark - S4 given to beams, and the rooms it names got no slab."""

    def test_a_mark_with_a_thickness_under_it_is_a_slab_mark(self):
        doc = ezdxf.new()
        msp = doc.modelspace()
        t = lambda s, x, y, rot=0: msp.add_text(s, height=150, rotation=rot,            # noqa: E731
                                                 dxfattribs={"insert": (x, y), "layer": "BEAM NO"})
        tags = {"beam_tag": [t("S4", 0, 0), t("135 Thk", 0, -380),               # a slab mark
                             t("S7", 5000, 0, 90), t("140 MM THK.", 5380, 0, 90),  # turned with its note
                             t("B4", 10000, 0),                                    # a beam mark: no note
                             t("SW3", 15000, 0), t("200 THK", 15000, -380),        # a wall: never moved
                             t("B9", 20000, 0), t("125 thk", 20000, -3000)],       # note too far below
                "slab_tag": [], "column_tag": []}
        moved = extract._slab_marks_on_beam_layers(tags)
        self.assertEqual(sorted((m, thk) for m, _x, _y, thk in moved), [("S4", 135.0), ("S7", 140.0)])
        self.assertEqual(sorted(e.dxf.text for e in tags["slab_tag"]), ["S4", "S7"])
        left = [e.dxf.text for e in tags["beam_tag"]]
        for kept in ("B4", "SW3", "B9"):
            self.assertIn(kept, left)
        self.assertNotIn("S4", left)

    def test_no_thickness_notes_changes_nothing(self):
        doc = ezdxf.new()
        e = doc.modelspace().add_text("S4", height=150, dxfattribs={"insert": (0, 0), "layer": "BEAM NO"})
        tags = {"beam_tag": [e], "slab_tag": []}
        self.assertEqual(extract._slab_marks_on_beam_layers(tags), [])
        self.assertEqual(tags["beam_tag"], [e])
        self.assertEqual(tags["slab_tag"], [])


class TestCurvedBeams(TempCase):
    """A curved beam is a beam. It used to be skipped without a word."""

    def run_it(self, **kw):
        p = os.path.join(self.tmp, "curve.dxf")
        build_curve_fixture(p, **kw)
        return extract.extract(p, self.cfg, log=lambda *_: None)

    def check_curve(self, res):
        curves = [b for b in res.beams if b.mid]
        self.assertEqual(len(curves), 1, "expected exactly one curved beam")
        cb = curves[0]
        self.assertAlmostEqual(cb.width, 230.0, delta=2.0)
        # on the centreline arc, radius 1115 about (1000, -1000)
        for p in (cb.start, cb.mid, cb.end):
            self.assertAlmostEqual(G.dist(p, (1000.0, -1000.0)), 1115.0, delta=15.0)
        straight = [b for b in res.beams if not b.mid]
        self.assertEqual([b.mark for b in straight], ["B1"], "the straight beam changed")
        return cb

    def test_two_arc_faces_make_one_curved_beam(self):
        self.check_curve(self.run_it())

    def test_a_curve_drawn_as_short_lines_is_a_curve(self):
        """Each piece is shorter than a beam can be; together they are an arc."""
        self.check_curve(self.run_it(as_lines=True))

    def test_a_curve_drawn_twice_is_one_beam(self):
        self.check_curve(self.run_it(twice=True))

    def test_the_curve_is_the_end_of_the_beam_it_continues(self):
        cb = self.check_curve(self.run_it())
        self.assertIn("B1", " ".join(cb.notes))

    def test_an_unclaimed_mark_on_the_curve_is_its_own(self):
        """MLB2 on Wing C: a curved beam that carries a mark keeps it."""
        cb = self.check_curve(self.run_it(mark_on_curve=True))
        self.assertEqual(cb.mark, "B7")
        self.assertFalse([i for i in (self.run_it(mark_on_curve=True).issues)
                          if i.mark == "B7" and "was not placed" in i.message])

    def test_the_cleaned_dxf_draws_an_arc(self):
        res = self.run_it()
        out = cleanout.write_clean_dxf(res, os.path.join(self.tmp, "c.dxf"), self.cfg)
        arcs = [e for e in ezdxf.readfile(out).modelspace() if e.dxftype() == "ARC"]
        self.assertEqual(len(arcs), 1)
        self.assertAlmostEqual(arcs[0].dxf.radius, 1115.0, delta=15.0)
        self.assertLess((arcs[0].dxf.end_angle - arcs[0].dxf.start_angle) % 360, 180,
                        "drew the long way round the circle")

    def test_the_straight_end_drawn_past_the_curve_is_built(self):
        """B101A: 220 mm legs after the curve, too short to read alone, close the bay."""
        res = self.run_it(straight_end=True)
        ends = [b for b in res.beams if "straight end of a curved beam" in " ".join(b.notes)]
        self.assertEqual(len(ends), 1, "the straight end of the curve was not built")
        e = ends[0]
        self.assertAlmostEqual(e.width, 230.0, delta=2.0)
        got = sorted([e.start, e.end], key=lambda p: p[1])
        self.assertAlmostEqual(got[0][0], -115.0, delta=5.0)
        self.assertAlmostEqual(got[0][1], -1335.0, delta=5.0, msg="not carried to B2's centreline")
        self.assertAlmostEqual(got[1][1], -1000.0, delta=5.0)

    def test_a_face_broken_in_two_still_gives_one_beam_on_the_centreline(self):
        """Akanistha: the inner face in two arcs where a beam comes in, the outer one whole.
        The whole face paired with one piece; the other piece was built on itself, 115 mm
        off the centreline; then, centred, the two were a beam with a gap in it."""
        res = self.run_it(inner_split=True)
        curves = [b for b in res.beams if b.mid]
        self.assertEqual(len(curves), 1, [(b.start, b.end) for b in curves])
        cb = curves[0]
        self.assertAlmostEqual(cb.width or 0.0, 230.0, delta=2.0)
        for p in (cb.start, cb.mid, cb.end):
            self.assertAlmostEqual(G.dist(p, (1000.0, -1000.0)), 1115.0, delta=15.0)
        ends = sorted([cb.start, cb.end])
        self.assertAlmostEqual(ends[0][0], -115.0, delta=15.0, msg="does not run the whole curve")
        self.assertAlmostEqual(ends[1][1], 115.0, delta=15.0, msg="does not run the whole curve")

    def test_a_curve_ending_on_a_wall_is_built(self):
        """Akanistha 2nd floor: the curved B1 runs on past shear wall SW16 (drawn on the column
        layer following the curve, so not a rectangle: a wall) to a free end. It touched no
        beam or column and was left out. A curve that touches nothing at all still is."""
        def build(on_wall):
            doc = ezdxf.new("R2010", setup=True)
            doc.header["$INSUNITS"] = 4
            msp = doc.modelspace()
            for lay in ("BEAM", "COLUMN"):
                if lay not in doc.layers:
                    doc.layers.add(lay)
            msp.add_line((20000.0, 0.0), (26000.0, 0.0), dxfattribs={"layer": "BEAM"})   # a beam elsewhere
            msp.add_line((20000.0, 230.0), (26000.0, 230.0), dxfattribs={"layer": "BEAM"})
            pt = lambda r, a: (r * math.cos(math.radians(a)), r * math.sin(math.radians(a)))   # noqa: E731
            if on_wall:          # the wall: the curve's own band from -20 to 0 degrees
                msp.add_lwpolyline([pt(5000, -20), pt(5000, 0), pt(5230, 0), pt(5230, -20)], close=True,
                                   dxfattribs={"layer": "COLUMN"})
            for r in (5000.0, 5230.0):          # the curve on from the wall's end to a free end
                msp.add_arc((0.0, 0.0), r, 0.0, 30.0, dxfattribs={"layer": "BEAM"})
            p = os.path.join(self.tmp, f"wall_{on_wall}.dxf")
            doc.saveas(p)
            return extract.extract(p, self.cfg, log=lambda *_: None)
        res = build(True)
        self.assertTrue(res.walls, "the curved outline was not read as a wall")
        curves = [b for b in res.beams if b.mid]
        self.assertEqual(len(curves), 1, "the curve on the wall was left out")
        self.assertAlmostEqual(curves[0].width or 0.0, 230.0, delta=2.0)
        self.assertAlmostEqual(min(G.dist(curves[0].start, (5115.0, 0.0)), G.dist(curves[0].end, (5115.0, 0.0))),
                               0.0, delta=20.0, msg="does not start at the wall")
        res = build(False)
        self.assertFalse([b for b in res.beams if b.mid], "a curve touching nothing was built")
        self.assertTrue([i for i in res.issues if "touch no beam, column or wall" in i.message])

    def test_no_straight_end_without_its_faces(self):
        res = self.run_it()
        self.assertFalse([b for b in res.beams if "straight end of a curved beam" in " ".join(b.notes)])


class TestBoxOverBeams(TempCase):
    """aldoot: a box with a cross drawn on the beam layer, its sides inside / along the beams."""

    def run_doc(self, draw):
        return TestTerraceCorners.run_doc(self, draw)

    @staticmethod
    def box(msp, x0, y0, x1, y1):
        msp.add_lwpolyline([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], close=True, dxfattribs={"layer": "BEAM"})
        msp.add_line((x0, y0), (x1, y1), dxfattribs={"layer": "BEAM"})
        msp.add_line((x0, y1), (x1, y0), dxfattribs={"layer": "BEAM"})

    def test_a_box_side_inside_a_beam_is_not_its_face(self):
        """The box's bottom side lies between the row's faces, 130 under the top one: read first,
        it paired with it into a 130 "beam" and the real bottom face was left over."""
        def draw(msp):
            TestTerraceCorners.column(msp, 0, -375, 300, 375)
            TestTerraceCorners.column(msp, 14000, -375, 14300, 375)
            self.box(msp, 3700, 15, 8500, 5400)                   # bottom side 15 above the centreline
            TestTerraceCorners.beam(msp, (300, 0), (14000, 0))
        res = self.run_doc(draw)
        row = [b for b in res.beams if abs(b.start[1]) < 200 and abs(b.end[1]) < 200]
        self.assertTrue(row, "the row is lost")
        self.assertEqual({round(b.width or 0) for b in row}, {230}, "the row is not 230 wide")
        self.assertLessEqual(min(min(b.start[0], b.end[0]) for b in row), 310)
        self.assertGreaterEqual(max(max(b.start[0], b.end[0]) for b in row), 13990)

    def test_a_box_side_just_inside_a_beam_face_leaves_the_beam_whole(self):
        """The box's right side, 35 mm inside a 300 beam's face, paired with the far face (265):
        the beam stopped where the box ends, 2 m short of its column."""
        def draw(msp):
            TestTerraceCorners.column(msp, -150, 9000, 150, 9750)
            TestTerraceCorners.column(msp, -150, -750, 150, 0)
            self.box(msp, -4900, 5000, -115, 9000)                # right side 35 inside the left face
            TestTerraceCorners.beam(msp, (0, 9000), (0, 0), w=300.0)
        res = self.run_doc(draw)
        v = [b for b in res.beams if abs(b.start[0] - b.end[0]) < 1.0 and abs(b.start[0]) < 60.0]
        self.assertTrue(v, "the beam is lost")
        self.assertEqual({round(b.width or 0) for b in v}, {300})
        self.assertLessEqual(min(min(b.start[1], b.end[1]) for b in v), 10.0, "the beam stops short")

    def test_a_face_in_two_pieces_beside_a_long_edge_line_stays_a_face(self):
        """Guard (NIFT level 5): a 300 beam's top face in two pieces, a 20 m edge line 150 above
        it. The edge line and the bottom face are no pair: the pieces are not set aside."""
        from core.model import Beam

        def line(a, b):
            return Beam("", "beam", a, b, None, "schedule-required", "as-drawn", 0.0, "BEAM", "")
        lines = [line((0, -150), (4700, -150)), line((0, 150), (623, 150)),
                 line((623, 150), (4700, 150)), line((-200, 300), (20000, 300))]
        self.assertEqual(extract._lines_along_a_beam(lines, 100.0, 700.0, 0.5, 3.0, []), {})

    def test_a_short_beam_beside_a_long_one_keeps_its_faces(self):
        """Guard: a short beam's face beside a long beam is a face of its own beam."""
        def draw(msp):
            TestTerraceCorners.column(msp, -300, -150, 0, 150)
            TestTerraceCorners.column(msp, 10000, -150, 10300, 150)
            TestTerraceCorners.beam(msp, (0, 0), (10000, 0))
            TestTerraceCorners.column(msp, 3000, 230, 3300, 690)
            TestTerraceCorners.column(msp, 7000, 230, 7300, 690)
            TestTerraceCorners.beam(msp, (3300, 460), (7000, 460))
        res = self.run_doc(draw)
        short = [b for b in res.beams if abs(b.start[1] - 460) < 5 and abs(b.end[1] - 460) < 5]
        self.assertTrue(short, "the short beam is lost")
        self.assertEqual({round(b.width or 0) for b in short}, {230})


class TestBeamTurningIntoACurve(TempCase):
    """J1153 Wing C 3rd floor B61: its faces stop and bend as two arcs into BC6."""

    def run_doc(self, draw):
        return TestTerraceCorners.run_doc(self, draw)

    def test_the_straight_part_ends_where_the_curve_begins(self):
        """B61 was carried straight on up to BC6 through the corner, the curve built beside it."""
        def draw(msp):
            TestTerraceCorners.column(msp, -150, -300, 150, 0)
            TestTerraceCorners.column(msp, -2300, 4300, -2000, 4830)
            TestTerraceCorners.column(msp, 3000, 4300, 3300, 4830)
            msp.add_line((-115, 0), (-115, 4000), dxfattribs={"layer": "BEAM"})
            msp.add_line((115, 0), (115, 4000), dxfattribs={"layer": "BEAM"})
            msp.add_arc((590, 4000), 705, 145.4, 180.0, dxfattribs={"layer": "BEAM"})   # outer face
            msp.add_arc((590, 4000), 475, 122.6, 180.0, dxfattribs={"layer": "BEAM"})   # inner face
            TestTerraceCorners.beam(msp, (-2000, 4515), (3000, 4515))                   # BC6
        res = self.run_doc(draw)
        up = [b for b in res.beams if b.mid is None and abs(b.start[0] - b.end[0]) < 5 and abs(b.start[0]) < 30]
        self.assertTrue(up, "the straight beam is lost")
        top = max(max(b.start[1], b.end[1]) for b in up)
        self.assertLessEqual(top, 4030.0, f"the straight beam runs on to y {top:.0f}, past the curve")
        self.assertTrue([b for b in res.beams if b.mid is not None], "the curve is lost")


class TestTerraceCorners(TempCase):
    """Sasan Gir terrace: beams stopping short of their supports, a column drawn open."""

    def run_doc(self, draw):
        doc = ezdxf.new("R2010", setup=True)
        doc.header["$INSUNITS"] = 4
        for lay in ("BEAM", "COLUMN"):
            if lay not in doc.layers:
                doc.layers.add(lay)
        draw(doc.modelspace())
        p = os.path.join(self.tmp, "t.dxf")
        doc.saveas(p)
        return extract.extract(p, self.cfg, log=lambda *_: None)

    @staticmethod
    def beam(msp, a, b, w=230.0):
        """a straight beam's two faces, a and b on its centreline"""
        L = math.dist(a, b)
        nx, ny = -(b[1] - a[1]) / L * w / 2.0, (b[0] - a[0]) / L * w / 2.0
        for s in (1, -1):
            msp.add_line((a[0] + s * nx, a[1] + s * ny), (b[0] + s * nx, b[1] + s * ny), dxfattribs={"layer": "BEAM"})

    @staticmethod
    def column(msp, x0, y0, x1, y1):
        msp.add_lwpolyline([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], close=True, dxfattribs={"layer": "COLUMN"})

    def test_a_column_whose_closing_side_runs_on_is_a_column(self):
        """The column under B25c: its left side drawn 570 mm on past the first corner. Closed
        across that "gap" it was a 5-point L, built as a wall."""
        def draw(msp):
            msp.add_lwpolyline([(0, 0), (230, 0), (230, -457), (0, -457), (0, 570)], dxfattribs={"layer": "COLUMN"})
        res = self.run_doc(draw)
        self.assertFalse(res.walls, "the column became a wall")
        self.assertEqual(len(res.columns), 1)
        c = res.columns[0]
        self.assertEqual((c.width, c.depth), (230.0, 457.0))

    def test_a_beam_runs_on_past_a_beam_from_one_side_to_its_column(self):
        """B25c: its left face stops where B6 comes in from the left, its right face runs on
        past B6 to the column under it. It stopped at B6, 450 mm short of the column."""
        def draw(msp):
            self.column(msp, 0, 5000, 230, 5457)                  # B25c's top support
            self.column(msp, 0, -570, 230, -1027)                 # the column under it
            self.column(msp, -3230, -230, -3000, 0)               # B6's far support
            for a, b in (((-3000, -115), (0, -115)),               # B6, from the left
                         ((230, -800), (4000, -800))):             # B4, from the column to the right
                self.beam(msp, a, b)
            self.column(msp, 4000, -915, 4230, -685)
            msp.add_line((0, 5000), (0, 0), dxfattribs={"layer": "BEAM"})        # left face: stops at B6
            msp.add_line((230, 5000), (230, -685), dxfattribs={"layer": "BEAM"})  # right face: on to B4
        res = self.run_doc(draw)
        v = [b for b in res.beams if abs(b.start[0] - b.end[0]) < 1.0 and abs(b.start[0] - 115.0) < 5.0]
        self.assertTrue(v, "B25c lost")
        low = min(min(b.start[1], b.end[1]) for b in v)
        self.assertLessEqual(low, -560.0, f"B25c stops at y {low:.0f}, short of the column at -570")
        self.assertGreaterEqual(low, -700.0, "B25c runs on through the column")

    def test_a_beam_turning_a_corner_is_not_carried_past_it(self):
        """An L corner: the outer face ends at the far face of the beam it turns into. That
        is the corner, not a face running on."""
        def draw(msp):
            self.column(msp, 0, 5000, 230, 5457)
            self.column(msp, 4000, -230, 4230, 0)
            msp.add_line((0, 5000), (0, -230), dxfattribs={"layer": "BEAM"})      # outer face
            msp.add_line((230, 5000), (230, 0), dxfattribs={"layer": "BEAM"})     # inner face
            msp.add_line((0, -230), (4000, -230), dxfattribs={"layer": "BEAM"})
            msp.add_line((230, 0), (4000, 0), dxfattribs={"layer": "BEAM"})
        res = self.run_doc(draw)
        self.assertFalse([b for b in res.beams if "coming in from one side" in " ".join(b.notes)])

    def test_a_curved_beam_short_of_its_support_is_carried_to_it(self):
        """B32 by the stair: both face arcs start 300 mm short of the column-wall it runs into,
        nothing drawn between. The curve stopped there."""
        def draw(msp):
            self.column(msp, 0, -1500, 230, 1500)                 # the column-wall
            self.column(msp, -3230, -115, -3000, 115)
            self.beam(msp, (-3000, 0), (0, 0))                    # B32, straight, from the left
            for r in (1200.0, 1430.0):                            # the curve, centre (530, 1315)
                msp.add_arc((530.0, 1315.0), r, 270.0, 360.0, dxfattribs={"layer": "BEAM"})
            self.beam(msp, (1845, 1315), (1845, 5000))            # it runs into a beam going up
            self.column(msp, 1730, 5000, 1960, 5230)
        res = self.run_doc(draw)
        self.assertTrue([b for b in res.beams if b.mid], "the curve was lost")
        piece = [b for b in res.beams if "along its tangent" in " ".join(b.notes)]
        self.assertEqual(len(piece), 1, "the curve was not carried to the column")
        xs = sorted((piece[0].start[0], piece[0].end[0]))
        self.assertAlmostEqual(xs[0], 230.0, delta=5.0)
        self.assertAlmostEqual(xs[1], 530.0, delta=5.0)

    def two_pieces(self, msp, column_in_overlap=False, marks=None):
        """B30c: drawn as two face pairs on one centreline, 1.5 m over each other."""
        self.column(msp, 0, 6000, 230, 6230)
        self.column(msp, 0, -230, 230, 0)
        self.beam(msp, (115, 6000), (115, 2290))                  # the first piece, from the top
        self.beam(msp, (115, 3817), (115, 0))                     # the second, on to the bottom
        if column_in_overlap:
            self.column(msp, 0, 2900, 230, 3130)
        if marks:
            if "BEAM NO" not in msp.doc.layers:
                msp.doc.layers.add("BEAM NO")
            for m, y in zip(marks, (5000, 1000)):
                msp.add_text(m, dxfattribs={"layer": "BEAM NO", "height": 150, "rotation": 90}) \
                   .set_placement((300, y))

    def test_a_beam_drawn_as_two_overlapping_pieces_is_one_beam(self):
        res = self.run_doc(self.two_pieces)
        self.assertEqual(len(res.beams), 1, "two beams lie over each other")
        ys = sorted((res.beams[0].start[1], res.beams[0].end[1]))
        self.assertLessEqual(ys[0], 20.0)
        self.assertGreaterEqual(ys[1], 5980.0)

    def test_two_beams_drawn_past_each_other_at_a_column_stay_two(self):
        """Villa 5: MB at the mid landing and GB19 drawn 892 mm over each other at C4."""
        res = self.run_doc(lambda msp: self.two_pieces(msp, column_in_overlap=True))
        self.assertFalse([b for b in res.beams if "drawn over it" in " ".join(b.notes)])

    def test_two_overlapping_beams_with_their_own_marks_stay_two(self):
        """J1153 Wing C 2nd floor: B5 and B6 drawn overlapping either side of the lift wall."""
        res = self.run_doc(lambda msp: self.two_pieces(msp, marks=("B5", "B6")))
        self.assertEqual(sorted(b.mark for b in res.beams), ["B5", "B6"])

    def test_a_beam_ends_where_the_beam_sharing_its_face_takes_over(self):
        """B3a (300) and B10f (230): one 35 m bottom face, top faces 300 and 230 above it
        either side of a column. B3a ran on 4.4 m over B10f."""
        from core.model import Beam, Result
        res = Result(dxf_file="t")

        def beam(a, b, w):
            return Beam(mark="", kind="beam", start=a, end=b, width=w, width_source="", centreline_source="face-pair",
                        offset_applied=w / 2.0, source_layer="BEAM", handle="")
        # B3a as it came out of the pairing: ended half way between its faces' ends
        b3a = beam((6000.0, 35.0), (-3000.0, 35.0), 300.0)
        b3a._face_ends = [(6000.0, -115.0), (-6000.0, -115.0), (6000.0, 185.0), (0.0, 185.0)]
        b10f = beam((0.0, 0.0), (-6000.0, 0.0), 230.0)            # its bottom face is B3a's
        same = beam((-3000.0, 35.0), (-9000.0, 35.0), 300.0)      # as wide: never cut back by this
        res.beams = [b3a, b10f]
        extract._trim_over_neighbour(res, {})
        self.assertAlmostEqual(min(b3a.start[0], b3a.end[0]), 0.0, delta=1.0, msg="B3a runs on over B10f")
        self.assertEqual((b10f.start, b10f.end), ((0.0, 0.0), (-6000.0, 0.0)), "B10f changed")
        b3a.start, b3a.end = (6000.0, 35.0), (-3000.0, 35.0)
        res.beams = [b3a, same]
        extract._trim_over_neighbour(res, {})
        self.assertAlmostEqual(min(b3a.start[0], b3a.end[0]), -3000.0, delta=1.0, msg="cut back by a beam as wide")
        # as wide, from the face end on: before the marks never cut back (B29c's copies);
        # after the marks, only when the two carry different marks (BC3 / B88)
        bc3 = beam((6000.0, 35.0), (-3000.0, 35.0), 300.0)
        bc3._face_ends = list(b3a._face_ends)
        b88 = beam((0.0, 35.0), (-6000.0, 35.0), 300.0)
        res.beams = [bc3, b88]
        extract._trim_over_neighbour(res, {})
        self.assertAlmostEqual(min(bc3.start[0], bc3.end[0]), -3000.0, delta=1.0, msg="as wide, cut before the marks")
        extract._trim_over_neighbour(res, {}, marked_only=True)
        self.assertAlmostEqual(min(bc3.start[0], bc3.end[0]), -3000.0, delta=1.0, msg="cut with no marks")
        bc3.mark, b88.mark = "BC3", "B88"
        extract._trim_over_neighbour(res, {}, marked_only=True)
        self.assertAlmostEqual(min(bc3.start[0], bc3.end[0]), 0.0, delta=1.0, msg="BC3 runs on over B88")

    def box(self, msp, text, touch=False):
        """Villa 5: a 1384 x 234 box round a note, on the beam layer."""
        if "TEXT" not in msp.doc.layers:
            msp.doc.layers.add("TEXT")
        msp.add_lwpolyline([(0, 0), (1384, 0), (1384, 234), (0, 234)], close=True, dxfattribs={"layer": "BEAM"})
        msp.add_text(text, dxfattribs={"layer": "TEXT", "height": 100}).set_placement((80, 60))
        self.beam(msp, (-3000, 5000), (3000, 5000))           # a beam elsewhere
        self.column(msp, -3230, 4885, -3000, 5115)
        self.column(msp, 3000, 4885, 3230, 5115)
        if touch:                                             # a beam coming onto the box's end
            self.beam(msp, (1500, 117), (1500, 5000))

    def test_a_box_round_a_note_is_not_a_beam(self):
        res = self.run_doc(lambda msp: self.box(msp, "MUD FILLING"))
        self.assertFalse([b for b in res.beams if abs(extract._mid(b)[1] - 117) < 50], "the note's box became a beam")
        self.assertTrue([i for i in res.issues if "frames round the note" in i.message])

    def test_a_box_with_a_size_written_in_it_is_kept(self):
        """GB29(230X500): a label style the marks may not read is still the beam's own caption."""
        res = self.run_doc(lambda msp: self.box(msp, "GB29(230X500)"))
        self.assertTrue([b for b in res.beams if abs(extract._mid(b)[1] - 117) < 50])

    def test_a_box_touching_a_beam_is_kept(self):
        res = self.run_doc(lambda msp: self.box(msp, "MUD FILLING", touch=True))
        self.assertTrue([b for b in res.beams if abs(extract._mid(b)[1] - 117) < 50 and b.length > 1000])

    def test_a_face_in_two_lines_end_to_end_is_one_beam(self):
        """B17c between B7 and B19: its left face is two lines end to end (the line style
        changes), the right face runs on from above. Both pieces became beams on the
        centreline and were stretched over each other."""
        from core.model import Beam, Result
        res = Result(dxf_file="t")

        def line(a, b, w=None, src="as-drawn"):
            return Beam(mark="", kind="beam", start=a, end=b, width=w, width_source="", centreline_source=src,
                        offset_applied=0.0, source_layer="BEAM", handle="")
        k = line((115.0, 6000.0), (115.0, 0.0), 230.0, "face-pair")
        k._face_ends = [(0.0, 6000.0), (0.0, 0.0), (230.0, 6000.0), (230.0, 0.0)]
        p1 = line((0.0, -400.0), (0.0, -700.0))                   # the face past a gap, in two lines
        p2 = line((0.0, -700.0), (0.0, -950.0))
        res.beams = [k, p1, p2]
        extract._align_face_continuations(res, {})
        on = [b for b in res.beams if b is not k and abs(b.start[0] - 115.0) < 1.0]
        self.assertEqual(len(on), 1, "each piece became a beam of its own on the centreline")
        self.assertEqual(sorted((on[0].start[1], on[0].end[1])), [-950.0, -400.0])

    def test_a_curve_already_touching_its_support_gets_no_piece(self):
        """Akanistha: curves ending 30-90 mm short of a wall face - their concrete is on it."""
        def draw(msp):
            self.column(msp, 0, -1500, 230, 1500)
            for r in (1200.0, 1430.0):                            # starts 60 mm off the column
                msp.add_arc((290.0, 1315.0), r, 270.0, 360.0, dxfattribs={"layer": "BEAM"})
            self.beam(msp, (1605, 1315), (1605, 5000))
            self.column(msp, 1490, 5000, 1720, 5230)
        res = self.run_doc(draw)
        self.assertFalse([b for b in res.beams if "along its tangent" in " ".join(b.notes)])


class TestBeamLayerExtras(TempCase):
    """J1153 Wing C 3rd floor: beams built where the drawing has none."""

    def run_doc(self, draw):
        doc = ezdxf.new("R2010", setup=True)
        doc.header["$INSUNITS"] = 4
        for lay in ("BEAM", "BEAM NO (H)", "VOID"):
            if lay not in doc.layers:
                doc.layers.add(lay)
        msp = doc.modelspace()
        msp.add_line((0.0, 0.0), (6000.0, 0.0), dxfattribs={"layer": "BEAM"})
        msp.add_line((0.0, 230.0), (6000.0, 230.0), dxfattribs={"layer": "BEAM"})
        msp.add_text("B1", dxfattribs={"layer": "BEAM NO (H)", "height": 250}) \
           .set_placement((3000.0, 115.0))
        draw(msp)
        p = os.path.join(self.tmp, "x.dxf")
        doc.saveas(p)
        return extract.extract(p, self.cfg, log=lambda *_: None)

    @staticmethod
    def cross(msp, x0, y0, x1, y1):
        msp.add_line((x0, y0), (x1, y1), dxfattribs={"layer": "VOID"})
        msp.add_line((x1, y0), (x0, y1), dxfattribs={"layer": "VOID"})

    def test_a_shaft_box_on_the_beam_layer_is_not_beams(self):
        def draw(msp):
            msp.add_lwpolyline([(1000, -200), (1740, -200), (1740, -1400), (1000, -1400)],
                               dxfattribs={"layer": "BEAM"})
            self.cross(msp, 1000, -200, 1740, -1400)
        res = self.run_doc(draw)
        self.assertEqual([b.mark for b in res.beams], ["B1"], "the shaft's outline became beams")
        self.assertTrue([i for i in res.issues if "outline of a cut-out" in i.message])

    def test_two_sides_of_a_duct_box_are_not_a_wide_beam(self):
        """A 500 x 323 box against B1's face, its two sides 500 apart."""
        def draw(msp):
            for a, b in (((3000, 0), (3000, -323)), ((3000, -323), (3500, -323)),
                         ((3500, -323), (3500, 0))):
                msp.add_line(a, b, dxfattribs={"layer": "BEAM"})
            self.cross(msp, 3000, 0, 3500, -323)
        res = self.run_doc(draw)
        self.assertEqual([b.mark for b in res.beams], ["B1"])

    def test_a_marked_line_on_a_cut_out_is_kept(self):
        """A mark means the consultant says it is a beam."""
        def draw(msp):
            msp.add_lwpolyline([(1000, -200), (1740, -200), (1740, -1400), (1000, -1400)],
                               dxfattribs={"layer": "BEAM"})
            self.cross(msp, 1000, -200, 1740, -1400)
            msp.add_text("B5", dxfattribs={"layer": "BEAM NO (H)", "height": 250,
                                           "rotation": 90}).set_placement((1800.0, -800.0))
        res = self.run_doc(draw)
        self.assertIn("B5", [b.mark for b in res.beams])

    def test_the_cut_out_still_parts_a_marked_bay_from_an_unmarked_one(self):
        """Sasan Gir terrace: the cut-out's long sides read as one 610 mm beam kept the
        bay below apart. Without that beam the bays may not run together: the one with
        no mark gets no slab (no mark, no slab)."""
        doc = ezdxf.new("R2010", setup=True)
        doc.header["$INSUNITS"] = 4
        for lay in ("BEAM", "BEAM NO (H)", "VOID", "SLAB NO"):
            if lay not in doc.layers:
                doc.layers.add(lay)
        msp = doc.modelspace()
        n = 0
        for (x0, y0), (x1, y1) in (((0, 0), (6000, 0)), ((0, 6000), (6000, 6000)),
                                   ((0, 0), (0, 6000)), ((6000, 0), (6000, 6000))):
            n += 1
            dx, dy = (0, 115) if y0 == y1 else (115, 0)
            for s in (1, -1):
                msp.add_line((x0 + s * dx, y0 + s * dy), (x1 + s * dx, y1 + s * dy),
                             dxfattribs={"layer": "BEAM"})
            msp.add_text(f"B{n}", dxfattribs={"layer": "BEAM NO (H)", "height": 200}) \
               .set_placement(((x0 + x1) / 2 + dx, (y0 + y1) / 2 + dy))
        msp.add_line((115, 2700), (5885, 2700), dxfattribs={"layer": "BEAM"})
        msp.add_line((115, 3300), (5885, 3300), dxfattribs={"layer": "BEAM"})
        self.cross(msp, 115, 2700, 5885, 3300)
        msp.add_text("S1", dxfattribs={"layer": "SLAB NO", "height": 200}).set_placement((3000, 1500))
        p = os.path.join(self.tmp, "bays.dxf")
        doc.saveas(p)
        res = extract.extract(p, self.cfg, log=lambda *_: None)
        self.assertEqual(sorted(b.mark for b in res.beams), ["B1", "B2", "B3", "B4"],
                         "the cut-out's sides became a beam")
        from shapely.geometry import Point, Polygon
        self.assertTrue(res.slabs, "the marked bay lost its slab")
        self.assertFalse([s for s in res.slabs if Polygon(s.outline).contains(Point(3000, 4500))],
                         "the bay with no mark got a slab across the cut-out")

    def test_beams_framing_a_duct_run_on_to_their_support(self):
        """Sasan Gir terrace, B12 / B7 at B17c: the duct between them has B12's lower face
        and B7's upper face for its long sides, each broken where an SB comes in. The face
        pieces past the break paired into a 610 "beam" in the hole (dropped as the
        cut-out), and B12 and B7 stopped half way, 1.3 m short of B17c."""
        doc = ezdxf.new("R2010", setup=True)
        doc.header["$INSUNITS"] = 4
        for lay in ("BEAM", "BEAM NO (H)", "VOID"):
            doc.layers.add(lay)
        msp = doc.modelspace()

        def line(a, b):
            msp.add_line(a, b, dxfattribs={"layer": "BEAM"})

        def mark(text, x, y, rot=0):
            msp.add_text(text, dxfattribs={"layer": "BEAM NO (H)", "height": 150, "rotation": rot}) \
               .set_placement((x, y))
        line((0, 0), (6000, 0))                    # B7, lower face whole
        line((0, 230), (3000, 230))                # B7, upper face broken by the SB
        line((3230, 230), (6000, 230))             # ... and on as the duct's lower side
        line((0, 1070), (6000, 1070))              # B12, upper face whole
        line((0, 840), (3000, 840))
        line((3230, 840), (6000, 840))             # ... the duct's upper side
        line((3000, 230), (3000, 840))             # the SB between them
        line((3230, 230), (3230, 840))
        line((6000, 230), (6000, 840))             # the duct's end, on B3's face
        self.cross(msp, 3230, 230, 6000, 840)
        line((6000, -2000), (6000, 0))             # B3, the beam both end on
        line((6000, 1070), (6000, 3000))
        line((6230, -2000), (6230, 3000))
        line((-230, -2000), (-230, 3000))          # B4 at the other end
        line((0, -2000), (0, 0))
        line((0, 1070), (0, 3000))
        mark("B7", 1500, 50)
        mark("B12", 1500, 890)
        mark("SB(230X125)", 2950, 240, 90)
        mark("B3", 6050, 2000, 90)
        mark("B4", -180, 2000, 90)
        p = os.path.join(self.tmp, "duct.dxf")
        doc.saveas(p)
        res = extract.extract(p, self.cfg, log=lambda *_: None)
        for m in ("B7", "B12"):
            got = [b for b in res.beams if b.mark == m]
            self.assertTrue(got, f"{m} lost")
            right = max(max(b.start[0], b.end[0]) for b in got)
            self.assertGreaterEqual(right, 5990.0, f"{m} stops at x {right:.0f}, short of B3")
        self.assertFalse([b for b in res.beams if b.width and b.width > 400], "the duct became a beam")

    def core(self, msp, mark=None, named=True):
        """A C-shaped concrete wall on COLUMN, its right leg drawn again on BEAM."""
        for lay in ("COLUMN", "COL NO"):
            if lay not in msp.doc.layers:
                msp.doc.layers.add(lay)
        msp.add_lwpolyline([(1000, -4000), (3000, -4000), (3000, -1000), (1000, -1000),
                            (1000, -1230), (2770, -1230), (2770, -3770), (1000, -3770)],
                           close=True, dxfattribs={"layer": "COLUMN"})
        if named:
            msp.add_text("LW1", dxfattribs={"layer": "COL NO", "height": 200}) \
               .set_placement((2000.0, -1600.0))
        msp.add_line((2770, -1230), (2770, -3770), dxfattribs={"layer": "BEAM"})
        msp.add_line((3000, -1230), (3000, -3770), dxfattribs={"layer": "BEAM"})
        if mark:
            msp.add_text(mark, dxfattribs={"layer": "BEAM NO (H)", "height": 200,
                                           "rotation": 90}).set_placement((2885.0, -2500.0))

    def test_a_lift_wall_drawn_on_the_beam_layer_is_not_a_beam(self):
        """Wing C 3rd floor LW1/LW2: the core's walls drawn on BEAM too became beams."""
        res = self.run_doc(self.core)
        self.assertEqual([b.mark for b in res.beams], ["B1"], "the wall's faces became a beam")
        self.assertTrue(res.walls, "the wall itself was lost")
        self.assertTrue([i for i in res.issues if "lie within a concrete wall" in i.message])

    def test_a_marked_beam_on_a_wall_is_kept(self):
        res = self.run_doc(lambda msp: self.core(msp, mark="B9"))
        self.assertIn("B9", [b.mark for b in res.beams])

    def test_beams_in_an_unnamed_outline_are_kept(self):
        """Villa 5 / Sasan Gir plinth: real beams (labelled GB15, GB8 in a style the app
        does not read) lie in an unnamed outline on a column layer. Not a named wall: kept."""
        res = self.run_doc(lambda msp: self.core(msp, named=False))
        self.assertEqual(len(res.beams), 2, "a beam in an unnamed outline was dropped")

    def test_a_duct_under_a_beam_does_not_take_its_face(self):
        """Wing C 3rd floor B47: its top face paired with the bottom edge of a duct box
        under it (both overlapping "wholly"; its own face a hair under), and B47 was built
        on its lower face line, 115 mm out of place."""
        doc = ezdxf.new("R2010", setup=True)
        doc.header["$INSUNITS"] = 4
        for lay in ("BEAM", "BEAM NO (H)", "VOID"):
            doc.layers.add(lay)
        msp = doc.modelspace()
        msp.add_line((0.0, 230.0), (6000.0, 230.0), dxfattribs={"layer": "BEAM"})
        msp.add_line((0.0, 0.0), (6000.0, 0.4), dxfattribs={"layer": "BEAM"})
        for a, b in (((1000, 0), (1000, -230)), ((1000, -230), (1432, -230)), ((1432, -230), (1432, 0))):
            msp.add_line(a, b, dxfattribs={"layer": "BEAM"})
        self.cross(msp, 1000, 0, 1432, -230)
        msp.add_text("B1", dxfattribs={"layer": "BEAM NO (H)", "height": 250}).set_placement((3000.0, 300.0))
        p = os.path.join(self.tmp, "duct.dxf")
        doc.saveas(p)
        res = extract.extract(p, self.cfg, log=lambda *_: None)
        self.assertEqual([b.mark for b in res.beams], ["B1"], "the duct's edge became a beam")
        b = res.beams[0]
        self.assertAlmostEqual(b.width, 230.0, delta=3.0)
        self.assertAlmostEqual((b.start[1] + b.end[1]) / 2.0, 115.0, delta=5.0, msg="not between its faces")

    def test_a_duct_on_the_other_side_at_the_same_distance_does_not_take_its_face(self):
        """Wing C 2nd floor 08-09-2026 BC6/B33: the row's top face had its own bottom face
        230 below and the top edge of a duct box on it 229 above - the same distance, the
        other side. The short box edge "overlaps wholly" and won: the row was built 115 mm
        up and no longer met the curve at its end."""
        doc = ezdxf.new("R2010", setup=True)
        doc.header["$INSUNITS"] = 4
        for lay in ("BEAM", "BEAM NO (H)", "VOID"):
            doc.layers.add(lay)
        msp = doc.modelspace()
        msp.add_line((0.0, 0.0), (8000.0, 0.3), dxfattribs={"layer": "BEAM"})        # top face, first
        msp.add_line((8040.0, -230.0), (0.0, -230.0), dxfattribs={"layer": "BEAM"})  # its bottom face
        for a, b in (((2000, 0), (2000, 229)), ((2000, 229), (2432, 229)), ((2432, 229), (2432, 0))):
            msp.add_line(a, b, dxfattribs={"layer": "BEAM"})                       # the duct box above
        self.cross(msp, 2000, 0, 2432, 229)
        msp.add_text("B1", dxfattribs={"layer": "BEAM NO (H)", "height": 250}).set_placement((5000.0, -500.0))
        p = os.path.join(self.tmp, "duct_above.dxf")
        doc.saveas(p)
        res = extract.extract(p, self.cfg, log=lambda *_: None)
        b1 = [b for b in res.beams if b.mark == "B1"]
        self.assertEqual(len(b1), 1, [(b.mark, b.start, b.end, b.width) for b in res.beams])
        self.assertAlmostEqual(b1[0].width, 230.0, delta=3.0)
        self.assertAlmostEqual((b1[0].start[1] + b1[0].end[1]) / 2.0, -115.0, delta=5.0, msg="not between its faces")

    def test_a_face_broken_by_a_beam_coming_in_gives_one_beam_not_two_laid_over(self):
        """Akanistha Winter Villa beside SW1: one face whole (5600), the other a 323 stub,
        a 300 gap where a beam comes in, then the rest - on the ground floor as two lines
        end to end. The stub paired with the whole face, the beam ended half way, and the
        rest became a second beam over it (and an unmarked third)."""
        doc = ezdxf.new("R2010", setup=True)
        doc.header["$INSUNITS"] = 4
        for lay in ("BEAM", "BEAM NO (H)"):
            doc.layers.add(lay)
        msp = doc.modelspace()
        msp.add_line((0.0, 300.0), (323.0, 300.0), dxfattribs={"layer": "BEAM"})       # the stub, first
        msp.add_line((0.0, 0.0), (5600.0, 0.0), dxfattribs={"layer": "BEAM"})          # the whole face
        msp.add_line((623.0, 300.0), (3000.0, 300.0), dxfattribs={"layer": "BEAM"})    # the rest, in two
        msp.add_line((3000.0, 300.0), (5600.0, 300.0), dxfattribs={"layer": "BEAM"})
        msp.add_line((323.0, 300.0), (323.0, 3600.0), dxfattribs={"layer": "BEAM"})    # the beam coming in
        msp.add_line((623.0, 300.0), (623.0, 3600.0), dxfattribs={"layer": "BEAM"})
        msp.add_text("B1", dxfattribs={"layer": "BEAM NO (H)", "height": 250}).set_placement((4000.0, -400.0))
        p = os.path.join(self.tmp, "broken_face.dxf")
        doc.saveas(p)
        res = extract.extract(p, self.cfg, log=lambda *_: None)
        along = [b for b in res.beams if not b.mid and abs(b.start[1] - b.end[1]) < 5.0]
        self.assertEqual(len(along), 1, [(b.mark, b.start, b.end, b.width) for b in along])
        b = along[0]
        self.assertAlmostEqual(b.width, 300.0, delta=3.0)
        self.assertAlmostEqual(b.start[1], 150.0, delta=5.0)
        self.assertLess(min(b.start[0], b.end[0]), 10.0)
        self.assertGreater(max(b.start[0], b.end[0]), 5590.0)
        self.assertEqual(b.mark, "B1")

    def test_a_box_at_a_beam_end_is_not_a_curved_beam(self):
        """Three sides of a small box lie on one circle; it is not a curve."""
        def draw(msp):
            msp.add_lwpolyline([(6000, 0), (6257, 0), (6257, 230), (6000, 230)], close=True,
                               dxfattribs={"layer": "BEAM"})
        res = self.run_doc(draw)
        self.assertFalse([b for b in res.beams if b.mid], "a box was built as a curved beam")


class TestWallOpenByItsThickness(TempCase):
    """The Wing B 2nd floor stair wall: one outline, left open by 230 mm."""

    def test_it_is_closed_and_kept(self):
        doc = ezdxf.new("R2010", setup=True)
        doc.layers.add("COLUMN")
        pts = [(0, 0), (2000, 0), (2000, 3000), (0, 3000), (0, 2770),
               (1770, 2770), (1770, 230), (0, 230)]
        doc.modelspace().add_lwpolyline(pts, close=False, dxfattribs={"layer": "COLUMN"})
        p = os.path.join(self.tmp, "u.dxf")
        doc.saveas(p)
        res = extract.extract(p, self.cfg, log=lambda *_: None)
        self.assertEqual(len(res.walls), 1, "the open wall outline was dropped")
        self.assertTrue([i for i in res.issues if "open by 230" in i.message
                         and i.severity != BLOCKER], "closing it was not reported")


class TestOutlineEndingOnItsStart(TempCase):
    """New block 246 / 258: closed flag off, but the last point is the first.

    Removing that repeated point first left a whole side looking like the gap:
    the 375x750 and 230x2460 columns were dropped as open by 750 / 2460 mm, and
    the 230x600 ones were "closed across a 600 mm gap" that was never there.
    """

    def test_they_are_closed_columns(self):
        doc = ezdxf.new("R2010", setup=True)
        doc.layers.add("COLUMN")
        msp = doc.modelspace()
        for x0, w, d in ((0, 375, 750), (5000, 230, 2460), (10000, 230, 600)):
            pts = [(x0, 0), (x0 + w, 0), (x0 + w, d), (x0, d), (x0, 0)]
            msp.add_lwpolyline(pts, close=False, dxfattribs={"layer": "COLUMN"})
        p = os.path.join(self.tmp, "c.dxf")
        doc.saveas(p)
        res = extract.extract(p, self.cfg, log=lambda *_: None)
        self.assertEqual(sorted((c.width, c.depth) for c in res.columns),
                         [(230.0, 600.0), (230.0, 2460.0), (375.0, 750.0)])
        self.assertFalse([i for i in res.issues if "open by" in i.message],
                         "a polyline ending on its start point is not open")
        self.assertFalse([c for c in res.columns if c.repaired])


class TestLongFaceAgainstBrokenFace(TempCase):
    """Main Block terrace, BEAM-300X600 at y -135100: the south face one solid line
    795170..803610, the north face in four pieces (broken where cross beams meet it).
    Paired with one piece, the beam ran from the middle of their starts (798605): the
    first 3.4 m of the beam and its north faces were dropped, the bay stayed open and
    the slab leaked through the gap with a rounded edge."""

    def run_it(self, pieces):
        doc = ezdxf.new("R2010", setup=True)
        doc.layers.add("BEAM")
        msp = doc.modelspace()
        msp.add_line((0, 0), (8440, 0), dxfattribs={"layer": "BEAM"})
        for x0, x1 in pieces:
            msp.add_line((x0, 300), (x1, 300), dxfattribs={"layer": "BEAM"})
        p = os.path.join(self.tmp, "f.dxf")
        doc.saveas(p)
        return extract.extract(p, self.cfg, log=lambda *_: None)

    def test_every_piece_of_the_broken_face_gets_its_beam(self):
        res = self.run_it([(0, 1773), (2003, 3995), (4225, 6640), (6870, 8440)])
        beams = sorted((b for b in res.beams if abs(b.start[1] - 150) < 5 and abs(b.end[1] - 150) < 5),
                       key=lambda b: min(b.start[0], b.end[0]))
        covered = sorted((min(b.start[0], b.end[0]), max(b.start[0], b.end[0])) for b in beams)
        self.assertTrue(covered and covered[0][0] < 300 and covered[-1][1] > 8100, covered)
        self.assertFalse([1 for (a0, a1), (b0, b1) in zip(covered, covered[1:]) if b0 - a1 > 300],
                         f"a gap in the beam: {covered}")
        self.assertTrue(all(abs(b.width - 300) < 1 for b in beams))

    def test_a_line_inside_a_beam_is_not_taken_for_its_other_face(self):
        """Main Block edge: outer face, slope line 230 in (broken at columns), inner face
        750 in. The slope line is not the other face: no cutting (the strip between them
        became a slab when it was)."""
        doc = ezdxf.new("R2010", setup=True)
        doc.layers.add("BEAM")
        msp = doc.modelspace()
        msp.add_line((0, 0), (8440, 0), dxfattribs={"layer": "BEAM"})
        for x0, x1 in ((0, 1773), (2003, 3995), (4225, 6640), (6870, 8440)):
            msp.add_line((x0, -230), (x1, -230), dxfattribs={"layer": "BEAM"})
        msp.add_line((0, -750), (8440, -750), dxfattribs={"layer": "BEAM"})
        p = os.path.join(self.tmp, "g.dxf")
        doc.saveas(p)
        res = extract.extract(p, self.cfg, log=lambda *_: None)
        self.assertFalse([i for i in res.issues if "cut to the pieces" in i.message])

    def test_one_face_in_one_piece_pairs_as_before(self):
        res = self.run_it([(0, 8440)])
        beams = [b for b in res.beams if abs(b.start[1] - 150) < 5]
        self.assertEqual(len(beams), 1)


class TestColumnDrawnAsTwoRectangles(TempCase):
    """New block middle line: a 70x600 strip against a 230x600 = one 300x600 column."""

    def build(self, gap=0.0):
        doc = ezdxf.new("R2010", setup=True)
        doc.layers.add("COLUMN")
        msp = doc.modelspace()
        msp.add_lwpolyline([(0, 0), (70, 0), (70, 600), (0, 600)], close=True,
                           dxfattribs={"layer": "COLUMN"})
        x0 = 70 + gap
        msp.add_lwpolyline([(x0, 0), (x0 + 230, 0), (x0 + 230, 600), (x0, 600)], close=True,
                           dxfattribs={"layer": "COLUMN"})
        p = os.path.join(self.tmp, "s.dxf")
        doc.saveas(p)
        return extract.extract(p, self.cfg, log=lambda *_: None)

    def test_one_column_of_the_whole_size(self):
        cols = self.build().columns
        self.assertEqual(len(cols), 1)
        self.assertEqual((cols[0].width, cols[0].depth), (300.0, 600.0))
        self.assertAlmostEqual(cols[0].x, 150.0, delta=0.5)
        self.assertAlmostEqual(cols[0].y, 300.0, delta=0.5)

    def test_a_strip_apart_is_still_only_flagged(self):
        res = self.build(gap=200.0)
        self.assertEqual([(c.width, c.depth) for c in res.columns], [(230.0, 600.0)])
        self.assertTrue([i for i in res.issues if "outside the expected range" in i.message])


class TestFaceInTwoPieces(TempCase):
    """B42: one face drawn in two pieces must not cost the beam its end."""

    def build(self, extra_on_girder_layer=False, split=True):
        doc = ezdxf.new("R2010", setup=True)
        for lay in ("BEAM", "GIRDER BEAM"):
            doc.layers.add(lay)
        msp = doc.modelspace()
        msp.add_line((0, 0), (6000, 0), dxfattribs={"layer": "BEAM"})
        if split:
            msp.add_line((0, 230), (3000, 230), dxfattribs={"layer": "BEAM"})
            msp.add_line((3000, 230), (6000, 230), dxfattribs={"layer": "BEAM"})
        else:
            msp.add_line((0, 230), (6000, 230), dxfattribs={"layer": "BEAM"})
        if extra_on_girder_layer:
            # a girder's end edge lying along this beam's face, past its end
            msp.add_line((5000, 230), (7500, 230), dxfattribs={"layer": "GIRDER BEAM"})
        p = os.path.join(self.tmp, "f.dxf")
        doc.saveas(p)
        return extract.extract(p, self.cfg, log=lambda *_: None)

    def test_the_beam_keeps_its_full_length(self):
        beams = [b for b in self.build().beams if b.kind == "beam"]
        self.assertEqual(len(beams), 1)
        xs = sorted(x for b in beams for x in (b.start[0], b.end[0]))
        self.assertLess(xs[0], 100.0)
        self.assertGreater(xs[-1], 5900.0, "the beam stopped short of its drawn end")

    def test_another_layer_along_the_face_does_not_lengthen_it(self):
        """The 1st floor beams ending on a girder must stay where they end."""
        beams = [b for b in self.build(extra_on_girder_layer=True, split=False).beams
                 if b.kind == "beam"]
        self.assertLess(max(x for b in beams for x in (b.start[0], b.end[0])), 6100.0)


class TestRoundTrip(TempCase):
    def test_cleaned_dxf_reads_back_the_same(self):
        res = self.fixture()
        out = cleanout.write_clean_dxf(res, os.path.join(self.tmp, "clean.dxf"), self.cfg)
        again = extract.extract(out, self.cfg, log=lambda *_: None)
        self.assertEqual(len(again.columns), len(res.columns))
        self.assertEqual(len(again.beams), len(res.beams))
        self.assertEqual([c.mark for c in again.columns], [c.mark for c in res.columns])

    def test_cleaned_dxf_is_flat_and_closed(self):
        res = self.fixture()
        out = cleanout.write_clean_dxf(res, os.path.join(self.tmp, "clean.dxf"), self.cfg)
        doc = ezdxf.readfile(out)
        polys = [e for e in doc.modelspace() if e.dxftype() == "LWPOLYLINE"]
        self.assertTrue(polys)
        self.assertTrue(all(e.closed for e in polys), "every outline must be closed")
        self.assertTrue(all(abs(float(e.dxf.elevation or 0)) < 1e-6 for e in polys),
                        "everything must be flat at Z = 0")
        self.assertEqual(doc.header["$INSUNITS"], 4)

    def test_cleaned_beam_keeps_its_corrected_position(self):
        res = self.fixture()
        out = cleanout.write_clean_dxf(res, os.path.join(self.tmp, "clean.dxf"),
                                       self.cfg, local=False)
        doc = ezdxf.readfile(out)
        lines = [e for e in doc.modelspace() if e.dxf.layer == "S-BEAM"]
        self.assertEqual(len(lines), 1)
        self.assertAlmostEqual(lines[0].dxf.start.y, -BEAM_WIDTH / 2, delta=0.1)


# ------------------------------------------------------ slabs between beams
class TestSlabPanelsFromBeams(unittest.TestCase):
    """No slab outlines are drawn, so each panel is the bay its beams enclose.

    Two bays side by side, 6 m x 4 m each, framed by 230 beams on a 12 m x 4 m
    rectangle with one beam across the middle.
    """

    def build(self, tags, middle=True, cut_out=False):
        from core.model import Beam, Opening, Result, SlabTag
        res = Result()
        if cut_out:                           # an X over the whole right bay
            res.openings.append(Opening(kind="opening", source_layer="void", handle="",
                                        outline=[(6100, 100), (11900, 100),
                                                 (11900, 3900), (6100, 3900)]))
        lines = [((0, 0), (12000, 0)), ((12000, 0), (12000, 4000)),
                 ((12000, 4000), (0, 4000)), ((0, 4000), (0, 0))]
        if middle:
            lines.append(((6000, 0), (6000, 4000)))
        for a, b in lines:
            res.beams.append(Beam(mark="", kind="beam", start=a, end=b, width=230.0,
                                  width_source="", centreline_source="",
                                  offset_applied=0.0, source_layer="BEAM", handle=""))
        for mark, x, y, th in tags:
            res.slab_tags.append(SlabTag(mark=mark, x=x, y=y, source_layer="SLAB NO",
                                         handle="", thickness=th))
        slabs.derive_panels(res, pipeline.load_config())
        return res

    def test_each_marked_bay_becomes_a_panel_at_its_thickness(self):
        res = self.build([("S1", 3000, 2000, 150.0), ("S2", 9000, 2000, 200.0)])
        got = sorted((s.mark, s.thickness) for s in res.slabs)
        self.assertEqual(got, [("S1", 150.0), ("S2", 200.0)])

    def test_the_panel_runs_to_the_beam_faces(self):
        res = self.build([("S1", 3000, 2000, 150.0)])
        left = [s for s in res.slabs if G.bbox(s.outline)[0] < 3000][0]
        x0, y0, x1, y1 = G.bbox(left.outline)
        self.assertAlmostEqual(x0, 115, delta=1)
        self.assertAlmostEqual(x1, 6000 - 115, delta=1)
        self.assertAlmostEqual(y1 - y0, 4000 - 230, delta=1)

    def test_a_bay_with_no_mark_is_left_open(self):
        # the user's rule (14 Oct): no slab mark, no slab - the bay is reported, not built
        res = self.build([("S1", 3000, 2000, 150.0)])
        self.assertEqual([s.mark for s in res.slabs], ["S1"])
        self.assertLess(G.bbox(res.slabs[0].outline)[2], 6000)
        self.assertTrue(any("no slab mark written in it" in i.message for i in res.issues))

    def test_filling_unmarked_bays_can_still_be_asked_for(self):
        # slab_fill_unmarked_bays: the old rule, slab everywhere but at a drawn cut-out (the X)
        from core.model import Beam, Result, SlabTag
        cfg = pipeline.load_config()
        cfg.setdefault("tolerances", {})["slab_fill_unmarked_bays"] = True
        res = Result()
        for a, b in [((0, 0), (12000, 0)), ((12000, 0), (12000, 4000)), ((12000, 4000), (0, 4000)),
                     ((0, 4000), (0, 0)), ((6000, 0), (6000, 4000))]:
            res.beams.append(Beam(mark="", kind="beam", start=a, end=b, width=230.0, width_source="",
                                  centreline_source="", offset_applied=0.0, source_layer="BEAM", handle=""))
        res.slab_tags.append(SlabTag(mark="S1", x=3000, y=2000, source_layer="", handle="", thickness=150.0))
        slabs.derive_panels(res, cfg)
        self.assertEqual(len(res.slabs), 2)
        empty = [s for s in res.slabs if any("no slab mark in this bay" in n for n in s.notes)]
        self.assertEqual([(s.mark, s.thickness) for s in empty], [("S1", 150.0)])

    def test_a_drawn_outline_with_no_mark_is_not_a_slab(self):
        from core.model import Result, SlabPanel, SlabTag
        res = Result()
        res.slabs.append(SlabPanel(mark="", outline=[(0, 0), (800, 0), (800, 300), (0, 300)],
                                   source_layer="SLAB", handle=""))
        slabs.derive_panels(res, pipeline.load_config())          # no mark anywhere on the plan
        self.assertEqual(res.slabs, [])
        res.slabs.append(SlabPanel(mark="", outline=[(0, 0), (800, 0), (800, 300), (0, 300)],
                                   source_layer="SLAB", handle=""))
        res.slab_tags.append(SlabTag(mark="S1", x=5000, y=5000, source_layer="", handle=""))
        slabs.derive_panels(res, pipeline.load_config())          # a mark, but not in it
        self.assertEqual([s.source_layer for s in res.slabs if s.source_layer == "SLAB"], [])

    def stairs(self, tags, tread_gap=300.0):
        """The left bay holds a flight of 10 treads 1200 wide (x 1000..2200, y 500..)."""
        from core.model import Beam, Result, SlabTag
        res = Result()
        for a, b in [((0, 0), (12000, 0)), ((12000, 0), (12000, 4000)), ((12000, 4000), (0, 4000)),
                     ((0, 4000), (0, 0)), ((6000, 0), (6000, 4000))]:
            res.beams.append(Beam(mark="", kind="beam", start=a, end=b, width=230.0, width_source="",
                                  centreline_source="", offset_applied=0.0, source_layer="BEAM", handle=""))
        segs = [((1000, 500 + i * tread_gap), (2200, 500 + i * tread_gap)) for i in range(10)]
        segs += [((1000, 500), (1000, 500 + 9 * tread_gap)), ((2200, 500), (2200, 500 + 9 * tread_gap))]
        res.stats["_stair_segments"] = segs
        for mark, x, y in tags:
            res.slab_tags.append(SlabTag(mark=mark, x=x, y=y, source_layer="", handle="", thickness=150.0))
        slabs.derive_panels(res, pipeline.load_config())
        return res

    def test_a_staircase_is_taken_out_of_its_slab(self):
        from shapely.geometry import Point, Polygon
        res = self.stairs([("S1", 4000, 2000), ("S2", 9000, 2000)])
        left = [s for s in res.slabs if s.mark == "S1"]
        self.assertEqual(len(left), 1)
        p = Polygon(left[0].outline, left[0].holes or None)
        self.assertFalse(p.contains(Point(1600, 1500)))            # on the flight: open
        self.assertTrue(p.contains(Point(4000, 2000)))             # beside it: slab
        self.assertAlmostEqual(p.area, 5770 * 3770 - 1200 * 2700, delta=150000)   # the flight, no more
        self.assertTrue(any("staircases" in i.message for i in res.issues))

    def test_scraps_beside_a_stair_go_with_it_a_staircase_bay_keeps_only_a_floor_marked_piece(self):
        from shapely.geometry import Point, Polygon
        # treads 300 apart reach only y 500..3200: the strip above them (3200..3885, 0.8 m2
        # beside a 1200-wide flight) is too small and unmarked - a scrap, the stair's
        res = self.stairs([("S1", 4000, 2000)])
        p = Polygon(res.slabs[0].outline, res.slabs[0].holes or None)
        self.assertTrue(p.contains(Point(4000, 2000)))
        # the bay marked ST (the staircase) with S1 written beside the flight: the piece is S1
        res = self.stairs([("ST", 1600, 1500), ("S1", 4000, 2000)])
        self.assertEqual(sorted(s.mark for s in res.slabs), ["S1"])
        # ...and with only ST in it, nothing is left of it but the flight's open well
        res = self.stairs([("ST", 1600, 1500), ("S2", 9000, 2000)])
        self.assertEqual(sorted(s.mark for s in res.slabs), ["S2"])

    def test_a_ledge_hatch_with_no_stated_depth_is_its_slab_when_only_its_mark_is_in_it(self):
        # J1153 1st floor 13-11-2025: S3 and S7 ledges outside the edge beams, each in a hatch
        # on SUNK with no legend depth, were left unbuilt
        from core.model import Beam, Result, SlabTag, SunkZone
        def run(tags):
            res = Result()
            for a, b in [((0, 0), (6000, 0)), ((6000, 0), (6000, 4000)), ((6000, 4000), (0, 4000)),
                         ((0, 4000), (0, 0))]:
                res.beams.append(Beam(mark="", kind="beam", start=a, end=b, width=230.0, width_source="",
                                      centreline_source="", offset_applied=0.0, source_layer="BEAM", handle=""))
            res.sunk.append(SunkZone(depth_mm=None, depth_source="unknown", source_layer="SUNK", handle="h",
                                     outline=[(6115, 0), (7500, 0), (7500, 4000), (6115, 4000)]))
            for m, x, y, sunk in tags:
                res.slab_tags.append(SlabTag(mark=m, x=x, y=y, source_layer="", handle="", thickness=150.0,
                                             sunk_mm=sunk))
            slabs.derive_panels(res, pipeline.load_config())
            return res
        res = run([("S1", 3000, 2000, 0.0), ("S7", 6800, 2000, 150.0)])
        ledge = [s for s in res.slabs if s.mark == "S7"]
        self.assertEqual(len(ledge), 1)
        self.assertEqual(ledge[0].sunk_mm, 150.0)                   # from the slab schedule
        res = run([("S1", 3000, 2000, 0.0), ("S7", 6800, 2000, 0.0), ("S9", 6800, 3000, 0.0)])
        self.assertEqual([s for s in res.slabs if s.mark in ("S7", "S9")], [])   # two slabs: not one ledge

    def test_a_stair_mark_on_the_flight_does_not_keep_the_slab_a_floor_mark_does(self):
        from shapely.geometry import Point, Polygon
        res = self.stairs([("S1", 4000, 2000), ("ST", 1600, 1500)])
        p = [Polygon(s.outline, s.holes or None) for s in res.slabs if s.mark == "S1"][0]
        self.assertFalse(p.contains(Point(1600, 1500)))
        res = self.stairs([("S1", 1600, 1500)])                       # S1 written on the flight
        self.assertTrue(Polygon(res.slabs[0].outline).contains(Point(1600, 1500)))
        self.assertTrue(any("slab mark is written" in i.message for i in res.issues))

    def test_a_bay_with_an_x_cut_out_stays_open(self):
        from core.model import Opening
        from shapely.geometry import Polygon
        res = self.build([("S1", 3000, 2000, 150.0)], cut_out=True)
        right = [s for s in res.slabs if G.bbox(s.outline)[0] > 5000]
        self.assertEqual(right, [])

    def test_a_mark_outside_every_bay_is_reported_not_built(self):
        res = self.build([("S1", 3000, 2000, 150.0), ("SC", 3000, 5000, None)])
        self.assertNotIn("SC", [s.mark for s in res.slabs])
        self.assertTrue(any(i.mark == "SC" and "not inside" in i.message
                            for i in res.issues))

    def test_two_marks_in_one_bay_take_the_thicker_and_are_reported(self):
        res = self.build([("S1", 3000, 2000, 150.0), ("S2", 9000, 2000, 200.0)],
                         middle=False)
        self.assertEqual(len(res.slabs), 1)
        self.assertEqual(res.slabs[0].thickness, 200.0)
        self.assertTrue(any("S1 and S2" in i.message for i in res.issues))

    def test_a_small_gap_at_a_beam_end_still_closes_the_bay(self):
        from core.model import Beam, Result, SlabTag
        res = Result()
        for a, b in [((0, 0), (6000, 0)), ((6000, 100), (6000, 4000)),
                     ((6000, 4000), (0, 4000)), ((0, 4000), (0, 0))]:
            res.beams.append(Beam(mark="", kind="beam", start=a, end=b, width=230.0,
                                  width_source="", centreline_source="",
                                  offset_applied=0.0, source_layer="BEAM", handle=""))
        res.slab_tags.append(SlabTag(mark="S1", x=3000, y=2000, source_layer="",
                                     handle="", thickness=150.0))
        slabs.derive_panels(res, pipeline.load_config())
        self.assertEqual(len(res.slabs), 1)

    def test_a_bay_closed_across_a_gap_stops_at_the_brick_wall_there(self):
        """ALCAZAR 1st floor, below column 57: the bottom beam stops 600 short and a brick
        wall closes the rest. Brick no longer cuts bays, so the bay was closed across the
        gap with the rounded closing - a quarter circle of slab past the wall."""
        from core.model import Beam, Result, SlabTag, Wall
        res = Result()
        # the bay 0..1300 x 0..2700; its right beam runs on 1200 further down to the
        # next beam, the bottom closed only by a brick wall (as B86A past column 57)
        for a, b in [((0, 0), (0, 2700)), ((0, 2700), (1300, 2700)), ((1300, 2700), (1300, -1200)),
                     ((1300, -1200), (4000, -1200))]:
            res.beams.append(Beam(mark="", kind="beam", start=a, end=b, width=230.0,
                                  width_source="", centreline_source="",
                                  offset_applied=0.0, source_layer="BEAM", handle=""))
        res.walls.append(Wall(mark="", thickness=230.0, start=(0, 0), end=(1300, 0),
                              outline=[(0, -115), (1300, -115), (1300, 115), (0, 115)],
                              source_layer="BRICK WALL", handle=""))
        res.slab_tags.append(SlabTag(mark="S1", x=650, y=1350, source_layer="", handle="", thickness=150.0))
        slabs.derive_panels(res, pipeline.load_config())
        self.assertEqual(len(res.slabs), 1)
        from shapely.geometry import Polygon
        self.assertGreater(Polygon(res.slabs[0].outline).bounds[1], -200.0)   # not past the wall

    def test_drawn_outlines_are_used_as_they_are(self):
        from core.model import Result, SlabPanel, SlabTag
        res = Result()
        res.slabs.append(SlabPanel(mark="", outline=[(0, 0), (1, 0), (1, 1)],
                                   source_layer="SLAB", handle=""))
        res.slab_tags.append(SlabTag(mark="S1", x=0, y=0, source_layer="", handle=""))
        slabs.derive_panels(res, pipeline.load_config())
        self.assertEqual(len(res.slabs), 1)
        self.assertEqual(res.slabs[0].source_layer, "SLAB")


class TestSlabSunkFromSchedule(unittest.TestCase):
    """The slab schedule prints "400mm SUNK" in its remarks, one or two lines under
    the slab's own row. Laid out as on the Wing B 1st floor sheet."""

    def entities(self):
        doc = ezdxf.new()
        msp = doc.modelspace()
        lay = "slab sch. text"

        def row(y, cells):
            for x, t in cells:
                msp.add_text(t, dxfattribs={"layer": lay, "insert": (x, y)})
        row(5000, [(0, "S"), (400, "-"), (800, "1"), (1500, "200"), (9000, "ONE WAY")])
        row(4560, [(5000, "TOP & BOTTOM"), (10000, "DECK SLAB")])
        row(4140, [(10000, "400mm SUNK")])
        row(3600, [(0, "S"), (800, "4"), (1500, "150"), (9000, "TWO WAY")])
        row(3000, [(0, "S"), (800, "8A"), (1500, "125"), (9000, "ONE WAY")])
        row(2560, [(10000, "PARTIALLY 100MM SUNK")])
        return list(msp)

    def test_each_remark_goes_to_the_row_above_it(self):
        got, partial = schedule.read_slab_sunk(self.entities(), "slab sch. text")
        self.assertEqual(got, {"S1": 400.0})
        self.assertEqual(partial, ["S8A"])

    def test_the_panel_drops_by_its_marks_sunk_depth(self):
        from core.model import Beam, Result, SlabTag
        res = Result()
        for a, b in [((0, 0), (6000, 0)), ((6000, 0), (6000, 4000)),
                     ((6000, 4000), (0, 4000)), ((0, 4000), (0, 0))]:
            res.beams.append(Beam(mark="", kind="beam", start=a, end=b, width=230.0,
                                  width_source="", centreline_source="",
                                  offset_applied=0.0, source_layer="BEAM", handle=""))
        res.slab_tags.append(SlabTag(mark="S1", x=3000, y=2000, source_layer="",
                                     handle="", thickness=200.0, sunk_mm=400.0))
        slabs.derive_panels(res, pipeline.load_config())
        self.assertEqual(res.slabs[0].sunk_mm, 400.0)



class TestCutOutsDrawnAsAnX(unittest.TestCase):
    """A duct or shaft is drawn as a cross on the void layer, no outline. On the
    Wing B 1st floor two sit in a bay that also holds an S8A slab mark, and the
    slab was built straight over them."""

    def test_a_cross_is_an_opening_through_its_line_ends(self):
        got = slabs.openings_from_crosses([((0, 0), (1500, 800), "void", "a"),
                                           ((1500, 0), (0, 800), "void", "b")])
        self.assertEqual(len(got), 1)
        self.assertEqual(sorted(got[0][0]), [(0, 0), (0, 800), (1500, 0), (1500, 800)])

    def test_a_diagonal_broken_for_its_label_is_joined_first(self):
        got = slabs.openings_from_crosses([
            ((0, 0), (600, 560), "void", "a"), ((1000, 933), (1600, 1493), "void", "b"),
            ((1600, 0), (1000, 560), "void", "c"), ((600, 933), (0, 1493), "void", "d")])
        self.assertEqual(len(got), 1)

    def test_lines_that_do_not_cross_are_no_opening(self):
        got = slabs.openings_from_crosses([((0, 0), (1000, 0), "void", "a"),
                                           ((0, 500), (1000, 500), "void", "b")])
        self.assertEqual(got, [])

    def test_the_cut_out_is_taken_out_of_the_slab_in_its_bay(self):
        from core.model import Beam, Opening, Result, SlabTag
        res = Result()
        for a, b in [((0, 0), (6000, 0)), ((6000, 0), (6000, 4000)),
                     ((6000, 4000), (0, 4000)), ((0, 4000), (0, 0))]:
            res.beams.append(Beam(mark="", kind="beam", start=a, end=b, width=230.0,
                                  width_source="", centreline_source="",
                                  offset_applied=0.0, source_layer="BEAM", handle=""))
        res.slab_tags.append(SlabTag(mark="S8A", x=1500, y=2000, source_layer="",
                                     handle="", thickness=125.0))
        # against the top beam, 20 mm short of its face, as drafted by eye
        res.openings.append(Opening(kind="opening", source_layer="void", handle="",
                                    outline=[(4000, 2500), (5865, 2500),
                                             (5865, 3865), (4000, 3865)]))
        # in the middle, touching nothing: a hole
        res.openings.append(Opening(kind="opening", source_layer="void", handle="",
                                    outline=[(2000, 1000), (2600, 1000),
                                             (2600, 1600), (2000, 1600)]))
        slabs.derive_panels(res, pipeline.load_config())
        self.assertEqual(len(res.slabs), 1)
        s = res.slabs[0]
        self.assertEqual(len(s.holes), 1)
        from shapely.geometry import Polygon
        area = Polygon(s.outline).area - sum(Polygon(h).area for h in s.holes)
        full = (6000 - 230) * (4000 - 230)
        # the 20 mm slivers go: the cut-out runs to the beam faces, 1885 x 1385
        self.assertAlmostEqual(area, full - 1885 * 1385 - 600 * 600, delta=2000)



def _frame(res, rect=(0, 0, 12000, 4000), middle=True):
    """Beams round a rectangle, and one across the middle: two bays."""
    from core.model import Beam
    x0, y0, x1, y1 = rect
    lines = [((x0, y0), (x1, y0)), ((x1, y0), (x1, y1)), ((x1, y1), (x0, y1)), ((x0, y1), (x0, y0))]
    if middle:
        xm = (x0 + x1) / 2
        lines.append(((xm, y0), (xm, y1)))
    for a, b in lines:
        res.beams.append(Beam(mark="", kind="beam", start=a, end=b, width=230.0,
                              width_source="", centreline_source="",
                              offset_applied=0.0, source_layer="BEAM", handle=""))


class TestSlabsOnEveryPlan(unittest.TestCase):
    """Layouts met across the nine Reis Magos floor plans, each one a way the
    slabs went wrong on a plan other than the one being worked on."""

    def tag(self, mark, x, y, th=150.0, sunk=0.0):
        from core.model import SlabTag
        return SlabTag(mark=mark, x=x, y=y, source_layer="", handle="",
                       thickness=th, sunk_mm=sunk)

    def test_one_drawn_outline_does_not_switch_the_rest_off(self):
        # Wing B 1st floor DWG: 72 marks, two small outlines. Built as 2 slabs.
        from core.model import Result, SlabPanel
        res = Result()
        _frame(res)
        res.slabs.append(SlabPanel(mark="", outline=[(200, 200), (2000, 200), (2000, 1500), (200, 1500)],
                                   source_layer="SLAB", handle="h"))
        res.slab_tags += [self.tag("S8A", 1000, 800, 125.0, 100.0),
                          self.tag("S4", 4000, 3000), self.tag("S2", 9000, 2000)]
        slabs.derive_panels(res, pipeline.load_config())
        got = sorted((s.mark, s.source_layer) for s in res.slabs)
        self.assertEqual(got, [("S2", slabs.SOURCE), ("S4", slabs.SOURCE), ("S8A", "SLAB")])
        drawn = [s for s in res.slabs if s.source_layer == "SLAB"][0]
        self.assertEqual((drawn.thickness, drawn.sunk_mm), (125.0, 100.0))
        # the S4 panel is its bay less the drawn S8A outline
        from shapely.geometry import Polygon
        s4 = [s for s in res.slabs if s.mark == "S4"][0]
        self.assertEqual(len(s4.holes), 1)
        self.assertFalse(Polygon(s4.outline, s4.holes).buffer(-1)
                         .intersects(Polygon(drawn.outline)))

    def test_an_outline_round_several_marks_is_not_a_slab(self):
        # Wing C 2nd floor: one 24 x 12 m rectangle on SLAB over half the plan.
        from core.model import Result, SlabPanel
        res = Result()
        _frame(res)
        res.slabs.append(SlabPanel(mark="", outline=[(-500, -500), (12500, -500), (12500, 4500), (-500, 4500)],
                                   source_layer="SLAB", handle="big"))
        res.slab_tags += [self.tag("S1", 3000, 2000), self.tag("S2", 9000, 2000)]
        slabs.derive_panels(res, pipeline.load_config())
        self.assertEqual(sorted(s.mark for s in res.slabs), ["S1", "S2"])
        self.assertTrue(all(s.source_layer == slabs.SOURCE for s in res.slabs))
        self.assertTrue(any(i.handle == "big" for i in res.issues))

    def test_a_mark_on_the_beam_line_goes_to_the_empty_bay_beside_it(self):
        from core.model import Result
        res = Result()
        _frame(res)
        res.slab_tags += [self.tag("S7", 3000, 2000), self.tag("S8", 6000 + 115 + 40, 2000)]
        res.slab_tags[-1].x = 6000 + 115 - 40          # 40 mm short of the right bay
        slabs.derive_panels(res, pipeline.load_config())
        self.assertEqual(sorted(s.mark for s in res.slabs), ["S7", "S8"])

    def test_a_mark_a_metre_away_is_not_moved(self):
        from core.model import Result
        res = Result()
        _frame(res)
        res.slab_tags += [self.tag("S1", 3000, 2000), self.tag("SC", 9000, 5000)]
        slabs.derive_panels(res, pipeline.load_config())
        # the cantilever mark outside is not pulled into the empty right bay, which has no
        # mark of its own and so is left open (no mark, no slab)
        self.assertEqual(sorted(s.mark for s in res.slabs), ["S1"])


class TestSlabScheduleLayouts(unittest.TestCase):
    """Slab schedule rows as printed on the different sheets."""

    def test_row_layouts(self):
        R = schedule._slab_row
        self.assertEqual(R(["S", "-", "4A", "150", "8"]), (["S4A"], 150.0))
        self.assertEqual(R(["S", "1,9", "125", "8"]), (["S1", "S9"], 125.0))
        self.assertEqual(R(["SC-1", "150", "12"]), (["SC1"], 150.0))
        self.assertEqual(R(["ST-2,4", "175", "10"]), (["ST2", "ST4"], 175.0))
        # a mark that is only a prefix, not SC150 with no thickness
        self.assertEqual(R(["SC", "150", "10", "150"]), (["SC"], 150.0))
        self.assertIsNone(R(["200", "12", "125", "8"]))

    def msp(self, rows, layer="beam sch. text"):
        doc = ezdxf.new()
        m = doc.modelspace()
        for y, cells in rows:
            for x, t in cells:
                m.add_text(t, dxfattribs={"layer": layer, "insert": (x, y)})
        return list(m)

    def test_side_by_side_tables_on_one_layer(self):
        # Wing C 2nd floor: the beam table's remark column runs up against the
        # slab table, and beam B22,29 prints on the same line as slab S8A.
        beam = [(-16000, "B"), (-15500, "22,29"), (-12900, "230"), (-12000, "550")]
        ents = self.msp([
            (5000, [(0, "S"), (380, "-"), (600, "8"), (1850, "200"), (8700, "DECK SLAB")]),
            (4750, [(-3150, "250MM SUNK")]),             # the beam table's remark
            (4640, [(8560, "150MM SUNK")]),
            (4250, beam + [(0, "S"), (600, "8A"), (1850, "200")]),
            (4190, [(380, "-"), (8800, "ONE  WAY")]),
            (3840, [(8560, "300MM SUNK")]),
        ])
        th, _, _ = schedule.read_slab_schedule(ents, "beam sch. text")
        self.assertEqual(th.get("S8A"), 200.0)
        sunk, _ = schedule.read_slab_sunk(ents, "beam sch. text")
        self.assertEqual({k: v for k, v in sunk.items() if k.startswith("S")},
                         {"S8": 150.0, "S8A": 300.0})

    def test_the_dash_below_a_row_is_not_a_row(self):
        # Wing A: the "-" between prefix and mark sits 65 mm below its row.
        ents = self.msp([
            (5000, [(160, "S"), (1050, "6A"), (2930, "150"), (13370, "ONE  WAY")]),
            (4935, [(736, "-")]),
            (4450, [(13000, "300MM SUNK")]),
        ], layer="slab sch. text")
        sunk, _ = schedule.read_slab_sunk(ents, "slab sch. text")
        self.assertEqual(sunk, {"S6A": 300.0})

    def test_a_remark_under_a_row_with_no_mark_is_not_guessed(self):
        ents = self.msp([
            (5000, [(0, "S"), (600, "8"), (1850, "200"), (8800, "TWO  WAY")]),
            (4500, [(380, "-"), (8800, "ONE  WAY")]),    # its mark is elsewhere
            (4130, [(8560, "150MM SUNK")]),
        ], layer="slab sch. text")
        sunk, _ = schedule.read_slab_sunk(ents, "slab sch. text")
        self.assertEqual(sunk, {})

    def test_a_row_split_a_few_mm_off_its_baseline_keeps_its_mark(self):
        # Wing C 2nd floor: S8A's "- ONE WAY" is printed 63 mm below its mark.
        ents = self.msp([
            (5000, [(0, "S"), (600, "8A"), (1850, "200")]),
            (4937, [(380, "-"), (8800, "ONE  WAY")]),
            (4587, [(8560, "150MM SUNK")]),
        ], layer="slab sch. text")
        sunk, _ = schedule.read_slab_sunk(ents, "slab sch. text")
        self.assertEqual(sunk, {"S8A": 150.0})



class TestSunkDepthFromTheLegend(unittest.TestCase):
    """The legend, not the layer name, says how deep a sunk hatch is."""

    def legend_doc(self, legend_scale=1.0):
        doc = ezdxf.new()
        m = doc.modelspace()
        m.add_text("LEGEND-:", dxfattribs={"layer": "TXT", "insert": (0, 0)})
        rows = [("400MM SUNK", "900MM SUNK", "FLEX", 20), ("100MM SUNK", "300mm SUNK", "ANSI33", 1000),
                ("150MM SUNK", "HATCH", "HONEY", 50), ("300MM SUNK", "300mm SUNK", "ANSI33", 50)]
        for i, (cap, lay, pat, sc) in enumerate(rows):
            y = -1000 - 900 * i
            m.add_text(cap, dxfattribs={"layer": "TXT", "insert": (2500, y - 150)})
            h = m.add_hatch(dxfattribs={"layer": lay})
            h.set_pattern_fill(pat, scale=sc * legend_scale)
            h.paths.add_polyline_path([(0, y - 300), (2000, y - 300), (2000, y + 300), (0, y + 300)])
        return doc, m

    def plan_hatch(self, m, lay, pat, sc):
        h = m.add_hatch(dxfattribs={"layer": lay})
        h.set_pattern_fill(pat, scale=sc)
        h.paths.add_polyline_path([(50000, 0), (52000, 0), (52000, 2000), (50000, 2000)])
        return h

    def test_the_layer_name_is_not_the_depth(self):
        # Wing B 1st floor: layer '300mm SUNK' holds the hatch captioned 100MM SUNK
        doc, m = self.legend_doc()
        samples = legend.sunk_key(list(m), {})
        h = self.plan_hatch(m, "300mm SUNK", "ANSI33", 1000)
        self.assertEqual(legend.depth_for(h, samples), 100.0)
        self.assertEqual(legend.depth_for(self.plan_hatch(m, "HATCH", "HONEY", 50), samples), 150.0)

    def test_pattern_counts_not_layer(self):
        # Wing B 2nd floor: the plan's hatches are on SUNK, the sample on 300mm SUNK
        doc, m = self.legend_doc()
        samples = legend.sunk_key(list(m), {})
        self.assertEqual(legend.depth_for(self.plan_hatch(m, "SUNK", "FLEX", 20), samples), 400.0)

    def test_one_pattern_two_depths_told_apart_by_scale(self):
        # Wing A 1st floor: ANSI33 is both 100 and 300, at different scales, and
        # the legend is drawn at 1.866 x the plan's hatch scale
        doc, m = self.legend_doc(legend_scale=1.866)
        samples = legend.sunk_key(list(m), {})
        plan = [self.plan_hatch(m, "900MM SUNK", "FLEX", 20),
                self.plan_hatch(m, "HATCH", "HONEY", 50)]
        f = legend.legend_scale(samples, plan)
        self.assertAlmostEqual(f, 1.866, places=2)
        big = self.plan_hatch(m, "300mm SUNK", "ANSI33", 1000)
        small = self.plan_hatch(m, "300mm SUNK", "ANSI33", 50)
        self.assertEqual(legend.depth_for(big, samples, f), 100.0)
        self.assertEqual(legend.depth_for(small, samples, f), 300.0)

    def test_a_legend_written_as_thus_marked_notes(self):
        """New block (Sasan Gir hotel): no LEGEND heading - '*THUS MARKED 200 mm SUNK
        SLAB WITH' and '...150 mm DOWN RCC PROJECTION' in the notes, each with its
        sample left of it. Both depths are read and the samples are left out."""
        doc = ezdxf.new()
        m = doc.modelspace()
        for i, (cap, pat) in enumerate((("%%U*THUS MARKED 200 mm SUNK SLAB WITH", "CROSS"),
                                        ("%%U*THUS MARKED 150 mm DOWN RCC PROJECTION.", "ANGLE"),
                                        ("%%U*THUS MARKED CUT OUT", "ANSI31"))):
            y = -900.0 * i
            m.add_text(cap, height=225, dxfattribs={"layer": "S-TEXT", "insert": (1200, y)})
            h = m.add_hatch(dxfattribs={"layer": "SUNK-HATCH"})
            h.set_pattern_fill(pat, scale=300)
            h.paths.add_polyline_path([(0, y - 250), (900, y - 250), (900, y + 220), (0, y + 220)])
        samples = legend.sunk_key(list(m), {})
        self.assertEqual(sorted((q[1], q[3]) for q in samples), [("ANGLE", 150.0), ("CROSS", 200.0)])
        kept = legend.exclude(list(m), {})
        self.assertFalse([e for e in kept if e.dxftype() == "HATCH"])

    def test_a_thus_marked_note_with_no_sample_is_not_a_legend(self):
        doc = ezdxf.new()
        m = doc.modelspace()
        m.add_text("*THUS MARKED 200 mm SUNK SLAB", height=225, dxfattribs={"layer": "S-TEXT", "insert": (0, 0)})
        self.assertEqual(legend.find_thus_marked(list(m), legend.settings({})), [])

    def test_the_date_in_an_attached_layer_is_not_a_depth(self):
        # the stilt plan: 382 hatches were sunk 13 mm by '13_03_2026_...$0$SUNK'
        self.assertEqual(reader.sunk_depth_from_layer(
            "13_03_2026_REIS MARINA _PLANS AND SECTIONS_ARCHITECTURAL$0$SUNK", {})[0], None)
        self.assertEqual(reader.sunk_depth_from_layer("S-SUNK-100", {})[0], 100.0)
        self.assertEqual(reader.sunk_depth_from_layer("SUNK ZONE 2", {})[0], None)


class TestSlabStripFromHatch(unittest.TestCase):
    """Stilt A wing: the sunk S1 strip between the edge beam and the retaining wall,
    one hatch, its mark written once per bay. No beams close it."""

    def run_it(self, marks, depth=None):
        from core.model import Result, SlabTag, SunkZone
        res = Result()
        _frame(res, middle=False, rect=(0, 0, 6000, 12000))
        for i, mk in enumerate(marks):
            res.slab_tags.append(SlabTag(mark=mk, x=-300, y=2000 + 4000 * i, source_layer="", handle="",
                                         thickness=150.0))
        res.slab_tags.append(SlabTag(mark="S2", x=3000, y=6000, source_layer="", handle="", thickness=125.0))
        res.sunk.append(SunkZone(depth_mm=depth, depth_source="", outline=[(-600, -115), (-115, -115),
                                                                           (-115, 12115), (-600, 12115)],
                                 source_layer="SUNK", handle="H1"))
        slabs.derive_panels(res, pipeline.load_config())
        return res

    def test_one_slab_written_in_every_bay_is_built_from_its_hatch(self):
        res = self.run_it(["S1", "S1", "S1"])
        strip = [s for s in res.slabs if s.mark == "S1"]
        self.assertEqual(len(strip), 1)
        from shapely.geometry import Polygon
        self.assertAlmostEqual(Polygon(strip[0].outline).area, 485 * 12230, delta=20000)
        self.assertEqual(strip[0].thickness, 150.0)
        self.assertFalse(any("not inside any bay" in i.message for i in res.issues))

    def test_different_marks_in_one_hatch_are_not_one_slab(self):
        res = self.run_it(["S1", "S3"])
        self.assertFalse([s for s in res.slabs if s.mark in ("S1", "S3")])

    def test_a_strip_closed_only_by_short_cross_pieces_keeps_its_hatch_edge(self):
        """Stilt A wing top right: B89 along the wall drawn only as short cross pieces.
        Closing the strip across the gaps between them gave it scalloped (rounded) edges;
        its hatch is its drawn shape, straight."""
        from core.model import Beam, Result, SlabTag, SunkZone
        from shapely.geometry import Polygon
        res = Result()
        _frame(res, middle=False, rect=(0, 0, 6000, 12000))
        for y in (0, 3000, 6000, 9000, 12000):            # cross pieces at the wall, 600 long
            res.beams.append(Beam(mark="B89", kind="beam", start=(-1900, y), end=(-1300, y), width=230.0,
                                  width_source="", centreline_source="", offset_applied=0.0,
                                  source_layer="BEAM", handle=""))
        for y in (1500, 4500, 7500, 10500):
            res.slab_tags.append(SlabTag(mark="S1", x=-700, y=y, source_layer="", handle="", thickness=150.0))
        res.slab_tags.append(SlabTag(mark="S2", x=3000, y=6000, source_layer="", handle="", thickness=125.0))
        res.sunk.append(SunkZone(depth_mm=None, depth_source="", outline=[(-1600, -115), (-115, -115),
                                                                          (-115, 12115), (-1600, 12115)],
                                 source_layer="SUNK", handle="H1"))
        slabs.derive_panels(res, pipeline.load_config())
        strip = [s for s in res.slabs if s.mark == "S1"]
        self.assertTrue(strip)
        for s in strip:                          # inside the hatch, edges straight (no rounded arcs)
            self.assertGreaterEqual(Polygon(s.outline).bounds[0], -1601)
            self.assertLessEqual(len(s.outline), 30)
        self.assertAlmostEqual(sum(Polygon(s.outline).area for s in strip),
                               1485 * 12230 - 5 * 300 * 230, delta=60000)


class TestPartlySunkSlab(unittest.TestCase):
    """A sunk hatch over only part of a bay, no beam round it (Wing B 1st floor,
    the S9 bay between B79 and B82): the slab is split along the hatch."""

    def run_it(self, hatch, schedule_sunk=0.0):
        from core.model import Result, SlabTag, SunkZone
        res = Result()
        _frame(res, middle=False, rect=(0, 0, 6000, 4000))
        res.slab_tags.append(SlabTag(mark="S9", x=1000, y=3000, source_layer="", handle="",
                                     thickness=125.0, sunk_mm=schedule_sunk))
        res.sunk.append(SunkZone(depth_mm=100.0, depth_source="legend", outline=hatch,
                                 source_layer="300mm SUNK", handle=""))
        slabs.derive_panels(res, pipeline.load_config())
        return res

    def test_the_hatched_part_drops_and_the_rest_stays(self):
        res = self.run_it([(3000, 115), (5885, 115), (5885, 2000), (3000, 2000)])
        got = sorted((s.mark, s.sunk_mm, s.thickness) for s in res.slabs)
        self.assertEqual(got, [("S9", 0.0, 125.0), ("S9", 100.0, 125.0)])
        from shapely.geometry import Polygon
        sunk = [s for s in res.slabs if s.sunk_mm][0]
        self.assertAlmostEqual(Polygon(sunk.outline).area, 2885 * 1885, delta=5000)

    def test_a_hatch_over_the_whole_bay_drops_the_whole_slab(self):
        res = self.run_it([(100, 100), (5900, 100), (5900, 3900), (100, 3900)])
        self.assertEqual([(s.mark, s.sunk_mm) for s in res.slabs], [("S9", 100.0)])

    def test_the_schedule_depth_is_kept_for_a_mark_it_calls_sunk(self):
        res = self.run_it([(100, 100), (5900, 100), (5900, 3900), (100, 3900)], schedule_sunk=150.0)
        self.assertEqual([s.sunk_mm for s in res.slabs], [150.0])
        self.assertTrue(any("disagree" in i.message for i in res.issues))

    def test_a_hatch_stopping_short_of_the_beam_leaves_no_gap(self):
        # an exploded hatch ends ~60 mm short of the beam face; that strip used
        # to be dropped (0.6 m2 of holes round the Wing B 2nd floor lift core)
        res = self.run_it([(3000, 175), (5825, 175), (5825, 2000), (3000, 2000)])
        from shapely.geometry import Polygon
        from shapely.ops import unary_union
        whole = unary_union([Polygon(s.outline) for s in res.slabs])
        self.assertAlmostEqual(whole.area, 5770 * 3770, delta=5000)
        self.assertEqual(sorted(s.sunk_mm for s in res.slabs), [0.0, 100.0])

    def test_a_sunk_part_takes_the_mark_written_in_its_sunk_area(self):
        """New block, far right toilet: the NS2 room's bay runs on into the toilet row
        (no beam between), and the toilet part kept NS2. The row's hatch carries NS4."""
        from core.model import Result, SlabTag, SunkZone
        res = Result()
        _frame(res, rect=(0, 0, 12000, 4000))                     # two bays
        res.slab_tags.append(SlabTag(mark="NS2", x=1000, y=3000, source_layer="", handle="",
                                     thickness=125.0))
        res.slab_tags.append(SlabTag(mark="NS4", x=9000, y=1000, source_layer="", handle="",
                                     thickness=125.0))
        res.sunk.append(SunkZone(depth_mm=600.0, depth_source="legend", source_layer="SUNK",
                                 handle="", outline=[(115, 115), (11885, 115), (11885, 2000), (115, 2000)]))
        slabs.derive_panels(res, pipeline.load_config())
        got = sorted((s.mark, s.sunk_mm) for s in res.slabs)
        self.assertEqual(got, [("NS2", 0.0), ("NS4", 0.0), ("NS4", 600.0), ("NS4", 600.0)])

    def test_a_sunk_part_with_no_other_mark_in_its_area_keeps_the_bays(self):
        """Wing B S9: the toilet corner's hatch holds no mark of its own - stays S9."""
        res = self.run_it([(3000, 115), (5885, 115), (5885, 2000), (3000, 2000)])
        self.assertEqual({s.mark for s in res.slabs}, {"S9"})


class TestBeamsRunUnderFloatingColumns(unittest.TestCase):
    """Wing B 1st floor FC2: B53 and B54 stopped at its faces, 1050 mm apart, and
    in Revit the floating column stood on nothing."""

    def fc(self, mark="FC2"):
        from core.model import Column
        return Column(mark=mark, kind="column", x=0.0, y=0.0, width=230.0, depth=1050.0,
                      rotation=90.0, outline=[(-115.0, -525.0), (115.0, -525.0),
                                              (115.0, 525.0), (-115.0, 525.0)],
                      source_layer="COLUMN", handle="C")

    def beam(self, mark, a, b):
        from core.model import Beam
        return Beam(mark=mark, kind="beam", start=a, end=b, width=230.0, width_source="",
                    centreline_source="", offset_applied=0.0, source_layer="BEAM", handle="")

    def test_two_beams_meet_under_it(self):
        b53 = self.beam("B53", (0.0, -4000.0), (0.0, -525.0))
        b54 = self.beam("B54", (0.0, 525.0), (0.0, 4000.0))
        res = extract.Result(beams=[b53, b54], columns=[self.fc()])
        extract._carry_floating_columns(res)
        self.assertAlmostEqual(b53.end[1], 0.0, delta=0.5)
        self.assertAlmostEqual(b54.start[1], 0.0, delta=0.5)

    def test_a_lone_beam_runs_to_its_far_face(self):
        b = self.beam("B69", (0.0, -4000.0), (0.0, -525.0))
        res = extract.Result(beams=[b], columns=[self.fc()])
        extract._carry_floating_columns(res)
        self.assertAlmostEqual(b.end[1], 525.0, delta=0.5)

    def test_an_fc_on_a_girder_and_an_ordinary_column_are_left_alone(self):
        side = self.beam("B1", (-4000.0, 0.0), (-115.0, 0.0))
        girder = self.beam("GB1", (0.0, -4000.0), (0.0, 4000.0))
        res = extract.Result(beams=[side, girder], columns=[self.fc()])
        extract._carry_floating_columns(res)
        self.assertEqual(side.end, (-115.0, 0.0))
        b = self.beam("B2", (0.0, -4000.0), (0.0, -525.0))
        res = extract.Result(beams=[b], columns=[self.fc(mark="12")])
        extract._carry_floating_columns(res)
        self.assertEqual(b.end, (0.0, -525.0))


class TestSunkFromExplodedHatch(unittest.TestCase):
    """Wing B 2nd floor as the team has it: every hatch exploded into LINEs, the
    legend's samples too. The S1A toilet strips (250 sunk) came out flat."""

    def doc(self):
        doc = ezdxf.new()
        m = doc.modelspace()
        m.add_text("LEGEND-:", dxfattribs={"layer": "TXT", "insert": (0, 0)})
        m.add_text("COLUMN", dxfattribs={"layer": "TXT", "insert": (2500, -500)})
        m.add_text("250MM SUNK", dxfattribs={"layer": "TXT", "insert": (2500, -1150)})
        m.add_text("150MM SUNK", dxfattribs={"layer": "TXT", "insert": (2500, -2050)})

        def diagonal(lay, x0, y0, w, h, gap, ang):   # a line hatch, clipped to a box
            from shapely.geometry import LineString, box
            b = box(x0, y0, x0 + w, y0 + h)
            t = math.radians(ang)
            d = (math.cos(t), math.sin(t))
            for k in range(-60, 60):
                c = (x0 + w / 2 - k * gap * d[1], y0 + h / 2 + k * gap * d[0])
                seg = LineString([(c[0] - 5000 * d[0], c[1] - 5000 * d[1]),
                                  (c[0] + 5000 * d[0], c[1] + 5000 * d[1])]).intersection(b)
                if seg.length > 1:
                    m.add_line(seg.coords[0], seg.coords[-1], dxfattribs={"layer": lay})

        def pluses(lay, x0, y0, w, h, pitch, size):
            for i in range(int(w // pitch)):
                for j in range(int(h // pitch)):
                    x, y = x0 + pitch / 2 + i * pitch, y0 + pitch / 2 + j * pitch
                    m.add_line((x - size, y), (x + size, y), dxfattribs={"layer": lay})
                    m.add_line((x, y - size), (x, y + size), dxfattribs={"layer": lay})

        # legend samples: the 250 on '300mm SUNK' at 135 deg, the 150 as '+' on HATCH
        diagonal("300mm SUNK", 0, -1400, 2000, 500, 125, 135)
        pluses("HATCH", 0, -2300, 2000, 500, 190, 47)
        # plan: 250 on SUNK at 47 deg, 150 as '+' on HATCH
        diagonal("SUNK", 50000, 0, 1500, 6000, 48, 47)
        pluses("HATCH", 60000, 0, 3000, 3000, 142, 32)
        return m

    def test_exploded_shading_is_read_by_its_look(self):
        from core.model import Result
        m = self.doc()
        samples = legend.sunk_key_lines(list(m), {})
        self.assertEqual(sorted((q[1], q[3]) for q in samples),
                         [("lines", 250.0), ("plus", 150.0)])
        res = Result()
        extract._sunk_from_lines(legend.exclude(list(m), {}, log=lambda *a: None),
                                 res, {}, samples)
        got = sorted((z.source_layer, z.depth_mm) for z in res.sunk)
        self.assertEqual(got, [("HATCH", 150.0), ("SUNK", 250.0)])
        from shapely.geometry import Polygon
        area = {z.source_layer: Polygon(z.outline).area for z in res.sunk}
        self.assertAlmostEqual(area["SUNK"], 1500 * 6000, delta=0.03 * 1500 * 6000)
        # matched by look across layers, which is said
        self.assertTrue(any("by look" in i.message or "only one drawn" in i.message
                            for i in res.issues))

    def test_real_hatches_need_no_exploded_reading(self):
        # a legend of real hatches gives no line samples, so nothing changes
        doc, m = TestSunkDepthFromTheLegend().legend_doc()
        self.assertEqual(legend.sunk_key_lines(list(m), {}), [])



class TestSlabFolds(unittest.TestCase):
    """The red lines on the slab layer: a slab fold or hidden beam at a sunk
    edge, drawn as two lines 200 mm apart (every Reis Magos plan)."""

    def test_a_pair_of_lines_is_one_fold_on_their_centreline(self):
        got = slabs.folds_from_lines([((0, 0), (3000, 0), "SLAB", "a"),
                                      ((100, 200), (2900, 200), "SLAB", "b")])
        self.assertEqual(len(got), 1)
        p, q, w, _l, _h = got[0]
        self.assertEqual(w, 200.0)
        self.assertAlmostEqual(p[1], 100.0)
        self.assertAlmostEqual(abs(q[0] - p[0]), 2800.0, delta=1)

    def test_a_lone_line_has_no_width(self):
        got = slabs.folds_from_lines([((0, 0), (3000, 0), "SLAB", "a")])
        self.assertEqual([g[2] for g in got], [None])

    def bay_with_fold(self, sunk_tag):
        """One 6 x 4 m bay, a fold across it at y = 2000 stopping 100 mm short of
        the beams (as drafted), S8A below it and S8 above."""
        from core.model import Fold, Result, SlabTag
        res = Result()
        _frame(res, middle=False, rect=(0, 0, 6000, 4000))
        res.folds.append(Fold(start=(215, 2000), end=(5785, 2000), width=200.0,
                              source_layer="SLAB", handle=""))
        res.slab_tags += [SlabTag(mark="S8A", x=3000, y=1000, source_layer="", handle="",
                                  thickness=125.0, sunk_mm=sunk_tag),
                          SlabTag(mark="S8", x=3000, y=3000, source_layer="", handle="",
                                  thickness=125.0)]
        slabs.derive_panels(res, pipeline.load_config())
        return res

    def test_a_fold_divides_the_bay_it_crosses(self):
        # Wing B 1st floor: S8A and S8 shared one bay and were built as one slab
        res = self.bay_with_fold(100.0)
        self.assertEqual(sorted((s.mark, s.sunk_mm) for s in res.slabs),
                         [("S8", 0.0), ("S8A", 100.0)])
        self.assertFalse(any("sit in one bay" in i.message for i in res.issues))

    def test_the_fold_is_the_step_plus_the_lower_slab(self):
        res = self.bay_with_fold(100.0)
        f = res.folds[0]
        self.assertEqual((f.depth, f.top_mm), (225.0, 0.0))

    def test_a_fold_with_no_step_is_as_deep_as_the_slab(self):
        # a hidden beam: both sides at the level
        res = self.bay_with_fold(0.0)
        self.assertEqual(res.folds[0].depth, 125.0)

    def test_a_piece_with_no_mark_keeps_its_bays_mark(self):
        # the S9 bay: the sunk corner beyond the fold has no mark of its own
        from core.model import Fold, Result, SlabTag
        res = Result()
        _frame(res, middle=False, rect=(0, 0, 6000, 4000))
        res.folds += [Fold(start=(3000, 115), end=(3000, 2000), width=200.0, source_layer="SLAB", handle=""),
                      Fold(start=(3000, 2000), end=(5885, 2000), width=200.0, source_layer="SLAB", handle="")]
        res.slab_tags.append(SlabTag(mark="S9", x=1000, y=3000, source_layer="", handle="",
                                     thickness=125.0))
        slabs.derive_panels(res, pipeline.load_config())
        self.assertEqual(len(res.slabs), 2)
        self.assertEqual({s.mark for s in res.slabs}, {"S9"})



class TestBeamMarksFollowTheirBeams(unittest.TestCase):
    """Beam marks are written along their beam: 'BEAM NO (H)' at 2 degrees for
    horizontal beams, 'BEAM NO (V)' at 92 for vertical ones (Wing B 2nd floor:
    47 and 52 of them). B7, written horizontally, had been given a vertical beam."""

    def test_the_drawing_convention_is_recognised(self):
        self.assertTrue(extract._marks_follow_beams([2.0] * 47 + [92.0] * 52))
        self.assertFalse(extract._marks_follow_beams([0.0] * 60))
        self.assertFalse(extract._marks_follow_beams([0.0] * 3 + [90.0] * 3))

    def res_with(self, tags):
        from core.model import Beam, Result
        res = Result()
        for a, b in [((0, 0), (0, 4000)), ((0, 4000), (5000, 4000))]:
            res.beams.append(Beam(mark="", kind="beam", start=a, end=b, width=230.0,
                                  width_source="", centreline_source="",
                                  offset_applied=0.0, source_layer="BEAM", handle=""))
        rot = {}
        bt = []
        for i, (m, x, y, r) in enumerate(tags):
            bt.append((m, (x, y), str(i), "BEAM NO"))
            rot[str(i)] = r
        res.stats["_tag_rot"] = rot
        return res, bt

    def test_a_horizontal_mark_goes_to_the_horizontal_beam(self):
        # B7 sits nearer the vertical beam but is written horizontally
        filler = [(f"X{i}", 90000 + i, 90000, 0.0 if i % 2 else 90.0) for i in range(12)]
        res, bt = self.res_with([("B7", 250, 3500, 0.0)] + filler)
        extract._match_beam_tags(bt, res, extract_tol(), pipeline.load_config())
        horiz = [b for b in res.beams if abs(b.start[1] - b.end[1]) < 1][0]
        self.assertEqual(horiz.mark, "B7")

    def test_a_line_with_two_marks_is_split_between_them(self):
        # Wing B 2nd floor: B89 and BP on two spans of one vertical line
        from core.model import Beam, Result
        res = Result()
        res.beams.append(Beam(mark="", kind="beam", start=(0, 0), end=(0, 6000), width=230.0,
                              width_source="", centreline_source="", offset_applied=0.0,
                              source_layer="BEAM", handle=""))
        res.beams.append(Beam(mark="", kind="beam", start=(-4000, 3000), end=(0, 3000), width=230.0,
                              width_source="", centreline_source="", offset_applied=0.0,
                              source_layer="BEAM", handle=""))
        filler = [(f"X{i}", 90000 + i, 90000, 0.0 if i % 2 else 90.0) for i in range(12)]
        tags = [("BP", 300, 1500, 90.0), ("B89", 300, 4500, 90.0)] + filler
        rot = {str(i): t[3] for i, t in enumerate(tags)}
        res.stats["_tag_rot"] = rot
        bt = [(m, (x, y), str(i), "BEAM NO") for i, (m, x, y, _r) in enumerate(tags)]
        extract._match_beam_tags(bt, res, extract_tol(), pipeline.load_config())
        self.assertEqual(sorted(b.mark for b in res.beams if b.start[0] == 0 and b.end[0] == 0),
                         ["B89", "BP"])


def extract_tol():
    return dict(pipeline.load_config()["tolerances"])



class TestScheduleTables(unittest.TestCase):
    """The beam schedule's remarks and the column schedule, as printed."""

    def msp(self, rows, layer):
        doc = ezdxf.new()
        m = doc.modelspace()
        for y, cells in rows:
            for x, t in cells:
                m.add_text(t, dxfattribs={"layer": layer, "insert": (x, y)})
        return m

    def test_beam_remarks_set_levels(self):
        m = self.msp([
            (5000, [(0, "B"), (400, "-"), (700, "20"), (2500, "230"), (3200, "350"), (9000, "250MM SUNK")]),
            (4500, [(0, "GB"), (400, "-"), (700, "1,2,3"), (2500, "600"), (3200, "800")]),
            (4210, [(9000, "BT@ +0.200M")]),
            (3500, [(0, "B"), (400, "-"), (700, "25"), (2500, "230"), (3200, "600"), (9000, "PARTLY SUNK")]),
            (3000, [(0, "B"), (400, "-"), (700, "52"), (2500, "230"), (3200, "600"), (9000, "BEAM @ LVL 10.25M")]),
        ], "beam sch. text")
        lv, nt = schedule.read_beam_levels(list(m), "beam sch. text")
        self.assertEqual(lv.get("B20"), [-250.0])
        self.assertEqual({k: v for k, v in lv.items() if k.startswith("GB")},
                         {"GB1": [200.0], "GB2": [200.0], "GB3": [200.0]})
        self.assertIn("B25", nt)                  # stepped: reported, not placed
        self.assertIn("B52", nt)                  # absolute level: reported
        self.assertNotIn("B25", lv)

    def test_column_schedule_is_read_per_wing(self):
        m = self.msp([
            (5000, [(0, "C"), (400, "-"), (700, "1"), (2000, "300"), (2600, "X"), (3000, "1250")]),
            (4500, [(0, "C"), (400, "-"), (700, "2,7"), (2000, "230"), (2600, "X"), (3000, "950")]),
            (5000, [(30000, "C"), (30400, "-"), (30700, "1"), (32000, "230"), (32600, "X"), (33000, "1050")]),
        ], "Col. sch.")
        m.add_text("SCHEDULE OF COLUMN B WING :-", dxfattribs={"layer": "MAIN TEXT", "insert": (0, 6000)})
        m.add_text("SCHEDULE OF COLUMN C WING :-", dxfattribs={"layer": "MAIN TEXT", "insert": (30000, 6000)})
        t = schedule.read_column_schedules(list(m), "col. sch.")
        b = [v for h, v in t.items() if "B WING" in h][0]
        c = [v for h, v in t.items() if "C WING" in h][0]
        self.assertEqual(b["C1"], (300.0, 1250.0))
        self.assertEqual(c["C1"], (230.0, 1050.0))
        self.assertEqual(b["C7"], (230.0, 950.0))

    def merged_table(self, ruled):
        m = self.msp([
            (6400, [(0, "B"), (400, "-"), (700, "2"), (2500, "230"), (3200, "600"), (6000, "8")]),
            (5800, [(0, "B"), (400, "-"), (700, "1"), (2500, "230"), (3200, "450"), (6000, "8")]),
            # one ruled cell: four mark lines, the size printed once in the middle
            (5300, [(0, "B"), (400, "-"), (700, "10,11")]),
            (4650, [(2500, "230"), (3200, "750"), (4000, "3"), (6000, "8")]),
            (4300, [(0, "B"), (400, "-"), (700, "12,13")]),
            (4000, [(0, "B"), (400, "-"), (700, "14")]),
            # a mark 64 mm off its sizes' line: beside it only the bar spacings
            (3450, [(0, "B"), (400, "-"), (700, "4"), (6600, "175"), (7200, "200")]),
            (3386, [(2500, "230"), (3200, "450"), (4000, "2"), (6000, "8")]),
            # a bracket with no number: its "mark" is the width
            (2700, [(0, "BKT"), (2500, "230"), (3200, "300"), (4000, "2")]),
        ], "beam sch. text")
        if ruled:
            for y in (6700, 6100, 5500, 3800, 3000, 2400):
                m.add_line((-100, y), (8000, y), dxfattribs={"layer": "LINE- 2"})
        return schedule.read_beam_schedule(list(m), "beam sch. text")[0]

    def test_mark_lines_in_one_ruled_cell_share_its_size(self):
        """Stilt A wing: "B - 10,11,12,13" ... "B - 38,39" in one cell, 230 450 once."""
        got = self.merged_table(ruled=True)
        for k in ("B10", "B11", "B12", "B13", "B14"):
            self.assertEqual(got.get(k), {"width": 230.0, "depth": 750.0}, k)
        self.assertEqual(got["B1"], {"width": 230.0, "depth": 450.0})
        self.assertEqual(got["B2"], {"width": 230.0, "depth": 600.0})
        self.assertEqual(got["B4"], {"width": 230.0, "depth": 450.0})   # not 175 x 200
        self.assertNotIn("BKT230", got)

    def test_without_rules_no_line_is_given_a_size(self):
        got = self.merged_table(ruled=False)
        self.assertNotIn("B10", got)
        self.assertNotIn("B4", got)          # the spacings are not taken for its size
        self.assertEqual(got["B1"], {"width": 230.0, "depth": 450.0})

    def test_a_drawing_of_several_wings_has_no_single_wing(self):
        self.assertEqual(schedule._wing_of("1153 - 1ST FLOOR PLAN ( B WING ) (6).dwg"), "B")
        self.assertEqual(schedule._wing_of("STILT FLOOR PLAN ( A,B & C WING ).dwg"), "")
        self.assertEqual(schedule._wing_of("BASEMENT FLOOR PLAN (PART WING A,B & C ).dwg"), "")


# --------------------------------------------------------- the real drawing
@unittest.skipUnless(os.path.exists(REAL_DWG), "the Wing C DWG is not on this machine")
class TestRealDrawing(unittest.TestCase):
    """Regression against the actual GFC file. Numbers come from a run that was
    checked by hand; a change here means the extractor's behaviour moved."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="c2r_real_")
        st = os.stat(REAL_DWG)                      # recorded BEFORE the run
        cls.before = (st.st_mtime, st.st_size)
        cls.res, cls.out_dir, cls.files = pipeline.run(
            REAL_DWG, out_dir=cls.tmp, log=lambda *_: None)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_counts_have_not_moved(self):
        got = self.res.counts()
        for key, expected in REAL_EXPECTED.items():
            self.assertEqual(got.get(key), expected, f"{key}: got {got.get(key)}")

    def test_no_marked_element_was_lost(self):
        """Dropping a legend or a repeat must never cost a mark.

        A mark is the only thing tying an element to its schedule row, so this is
        the check that matters: the raw counts may fall when the tool learns to
        leave something out, but the marks may not.
        """
        marked = [c for c in self.res.columns if c.mark]
        self.assertEqual(len(marked), REAL_MARKED_COLUMNS)
        self.assertEqual(len([b for b in self.res.beams if b.mark]),
                         REAL_MARKED_BEAMS,
                         "a tagged beam was lost while tidying untagged lines")
        self.assertEqual(sorted(w.mark for w in self.res.walls if w.mark),
                         ["LW1", "LW2", "LW3", "LW4"])

    def test_every_multiline_beam_was_corrected(self):
        fixed = [b for b in self.res.beams if b.centreline_source == "multiline-faces"]
        self.assertTrue(fixed)
        for b in fixed:
            self.assertAlmostEqual(b.offset_applied, (b.width or 0) / 2, delta=0.5)

    def test_all_expected_output_files_exist(self):
        for name in ("cleaned.dxf", "columns.csv", "beams.csv", "walls.csv",
                     "slab_tags.csv", "sunk_zones.csv", "issues.csv",
                     "model.json", "report.html"):
            self.assertTrue(os.path.exists(os.path.join(self.out_dir, name)), name)

    def test_the_known_blockers_are_still_reported(self):
        msgs = " ".join(i.message for i in self.res.issues if i.severity == BLOCKER)
        for expected in ("no grid", "carry no mark", "open by"):
            self.assertIn(expected, msgs)

    def test_source_drawing_was_not_modified(self):
        """The consultant's file must come out of a run byte-identical."""
        st = os.stat(REAL_DWG)
        self.assertEqual(self.before, (st.st_mtime, st.st_size),
                         "the source DWG changed during the run")


class TestConverterThatWillNotStart(TempCase):
    """A CAD program set to "run as administrator" must not crash the run: it
    is started as the current user, and if Windows still refuses, the next
    converter is tried and the message says why."""

    def test_refused_start_is_reported_not_raised(self):
        import subprocess
        from core import dwgconv
        seen = []

        def refuse(cmd, **kw):
            seen.append(kw.get("env", {}).get("__COMPAT_LAYER"))
            raise OSError(740, "The requested operation requires elevation")
        src = os.path.join(self.tmp, "a.dwg")
        with open(src, "wb") as f:
            f.write(b"x")
        # ZWCAD is started with Popen (a watcher answers its known messages), ODA with run
        logs, real, real_popen = [], subprocess.run, subprocess.Popen
        subprocess.run = refuse
        subprocess.Popen = refuse
        try:
            ok = dwgconv._convert_with_zwcad("ZWCAD.exe", src, os.path.join(self.tmp, "a.dxf"), logs.append)
            ok_oda = dwgconv._convert_with_oda("ODA.exe", src, os.path.join(self.tmp, "a.dxf"), logs.append)
        finally:
            subprocess.run = real
            subprocess.Popen = real_popen
        self.assertFalse(ok)
        self.assertFalse(ok_oda)
        self.assertEqual(seen[0], "RunAsInvoker")
        self.assertTrue(any("could not start" in m for m in logs))


def main() -> int:
    if "--fixture" in sys.argv:
        i = sys.argv.index("--fixture")
        target = sys.argv[i + 1] if len(sys.argv) > i + 1 else "fixture.dxf"
        if not target.lower().endswith(".dxf"):
            target += ".dxf"
        build_fixture(target)
        print(f"Wrote {target}. Open it in CAD: one 230 x 950 column at 2 degrees marked 5, "
              f"one Top-justified 230 multiline beam marked B1, one closed slab, one grid line.")
        return 0
    argv = [a for a in sys.argv if a != "--fixture"]
    return 0 if unittest.main(argv=argv, exit=False).result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
