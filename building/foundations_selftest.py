r"""Tests for Stage 5: footings, rafts, pedestals, footing schedules, columns across sheets.

    python -m building.foundations_selftest

Each test rebuilds, small, a case met on the training drawings.
"""
from __future__ import annotations

import unittest
from collections import Counter
from types import SimpleNamespace

import ezdxf

from building import Building, Element, Evidence, Sheet, Value
from building.linker import _shift, link
from building.readers import column_schedule, columns, foundations, tables
from building.sheets.levels import levels_in
from building.sheets.source import TextItem


def T(s, x, y, h=200.0, layer="TXT"):
    return TextItem(s, s, h, x, y, layer, f"{s}@{x:.0f},{y:.0f}", "model", False, x, y)


def rect(msp, x, y, w, h, layer):
    return msp.add_lwpolyline([(x, y), (x + w, y), (x + w, y + h), (x, y + h)], close=True,
                              dxfattribs={"layer": layer})


def view(doc, texts, kind="foundation_plan", rid="F:V", title="FOUNDATION PLAN"):
    msp = doc.modelspace()
    return SimpleNamespace(id=rid, verdict=SimpleNamespace(kind=kind), texts=texts, doc=doc,
                           layers=Counter(), levels=[], wing="", title=title,
                           view=SimpleNamespace(handles=[e.dxf.handle for e in msp], same_as="",
                                                source="viewport"))


class Footings(unittest.TestCase):
    def test_raft_drawn_as_loose_lines_in_a_block(self):
        """Akanistha: raft RF1 drawn as loose lines on 'FOOTING AVADH' inside a block, its PCC bed
        on 'P.C.C. LINE AVADH'; the columns and shear walls on 'COLUMN AVADH' are not footings."""
        doc = ezdxf.new()
        msp = doc.modelspace()
        blk = doc.blocks.new("FDN")
        pts = [(0, 0), (20000, 0), (20000, 14000), (9000, 14000), (9000, 18000), (0, 18000)]
        for lay, d in (("FOOTING AVADH", 0), ("P.C.C. LINE AVADH", 150)):
            q = [(x - d if x == 0 else x + d, y - d if y == 0 else y + d) for x, y in pts]
            for i in range(len(q)):
                blk.add_line(q[i], q[(i + 1) % len(q)], dxfattribs={"layer": lay})
        msp.add_blockref("FDN", (0, 0))
        for x in (2000, 7000, 12000):
            rect(msp, x, 3000, 300, 1500, "COLUMN AVADH")           # shear walls
            rect(msp, x, 9000, 900, 1200, "COLUMN AVADH")           # columns
        b = Building()
        got = foundations.read(b, view(doc, [T("RF1", 5000, 6000)]))
        self.assertEqual(got, {"rafts": 1})
        r = b.of("raft")[0]
        self.assertEqual((r.mark, len(r.geometry["outline"])), ("RF1", 6))
        self.assertEqual(b.of("footing"), [])

    def test_footing_marked_on_a_column_layer_is_still_a_footing(self):
        """NIFT: footing F2 drawn on 'RCE-COL' - a footing mark inside keeps it a footing (no
        foundation drawn as lines here: nothing changes)."""
        doc = ezdxf.new()
        rect(doc.modelspace(), 0, 0, 2800, 3400, "RCE-COL")
        b = Building()
        foundations.read(b, view(doc, [T("F2", 1400, 1700)]))
        self.assertEqual([f.mark for f in b.of("footing")], ["F2"])

    def test_footing_with_pcc_bed_and_a_step(self):
        """Sasan Gir F7: 3600 x 3900 footing, 2100 x 2400 step, PCC 100 mm all round."""
        doc = ezdxf.new()
        msp = doc.modelspace()
        rect(msp, -100, -100, 3800, 4100, "Pcc_ST")
        rect(msp, 0, 0, 3600, 3900, "F7")
        rect(msp, 750, 750, 2100, 2400, "F7")
        rect(msp, 1650, 1600, 300, 600, "STR.COLUMN")
        b = Building()
        foundations.read(b, view(doc, [T("F7", 4200, 2000), T("C10", 1800, 1900)]))
        f = b.of("footing")
        self.assertEqual(len(f), 1)
        f = f[0]
        self.assertEqual((f.get("width"), f.get("length"), f.get("pcc_offset")), (3600, 3900, 100))
        self.assertEqual(f.get("steps"), [[2100, 2400]])
        self.assertEqual((f.mark, f.get("columns")), ("F7", ["C10"]))

    def test_a_bare_column_number_on_a_column_number_layer_names_the_footings_column(self):
        """J1153 B-block foundation: '22' in its circle on layer 'colno' is column C22."""
        doc = ezdxf.new()
        rect(doc.modelspace(), 0, 0, 2700, 3600, "FOOTING")
        b = Building()
        foundations.read(b, view(doc, [T("22", 1350, 1800, layer="colno"), T("7", 900, 900, layer="TXT")]))
        self.assertEqual(b.of("footing")[0].get("columns"), ["C22"])     # a bare 7 elsewhere is not a column

    def test_a_tight_column_schedule_reads_every_row(self):
        """J1153 B-block 'SCHEDULE OF COLUMN': first row a text height under the header, the
        'C' of each mark left of it, and 'NO.' under STEEL (bars, not marks)."""
        h = 300.0
        texts = [T("SCHEDULE OF COLUMN :-", 6000, 27500, h), T("COLUMN", 7000, 25500, h), T("NOS .", 7000, 24950, h),
                 T("S I Z E", 10700, 24900, h), T("STEEL", 12600, 25100, h), T("NO.", 12300, 24570, h),
                 T("T", 12940, 24570, h)]
        for y, mark, size in ((24230, "2,3,4,5", "950"), (23330, "8", "2800"), (22430, "10", "1600")):
            texts += [T("C", 6120, y, h), T("-", 6500, y, h), T(mark, 7300, y, h), T("300", 9980, y, h),
                      T("X", 10570, y, h), T(size, 11250, y, h), T("16", 12280, y, h), T("20", 12970, y, h)]
        rows = []
        for tb in tables._find_tables(texts):
            tables._records(tb, texts)
            rows += [(r["mark_text"], [t.text for t in r["cells"].get("size", [])]) for r in tb.records]
        self.assertEqual(rows, [("C-2,3,4,5", ["300", "X", "950"]), ("C-8", ["300", "X", "2800"]),
                                ("C-10", ["300", "X", "1600"])])

    def test_a_foundation_sheet_drawn_at_an_angle_is_turned_onto_the_plan(self):
        """J1153 B-block foundation: square grid; the plan's grid turned 2.04 degrees."""
        import math
        from building.export import _fit_by_columns
        a = math.radians(2.04)
        b = Building()
        grid = {"C21": (0, 0), "C22": (7660, 0), "C23": (15320, 0), "C15": (0, 4630), "C16": (7660, 4630)}
        for i, (m, (x, y)) in enumerate(grid.items()):
            b.add(Element(id=f"F:V/footing/{i}", type="footing", sheet="F:V", geometry={"x": x, "y": y},
                          extra={"column_at": {m: [x, y]}}))
            px, py = 500000 + x * math.cos(a) - y * math.sin(a), 90000 + x * math.sin(a) + y * math.cos(a)
            b.add(Element(id=f"P/col/{i}", type="column", sheet="P", mark=m[1:], geometry={"x": px, "y": py}))
        (c, s, dx, dy), deg, n, miss = _fit_by_columns(b, "F:V", "P")
        self.assertAlmostEqual(deg, 2.04, places=2)
        self.assertEqual(n, 5)
        self.assertLess(miss, 1.0)
        # one column on the plan elsewhere: the fit no longer holds, nothing is guessed
        next(e for e in b.elements if e.id == "P/col/4").geometry["x"] += 2000
        self.assertIsNone(_fit_by_columns(b, "F:V", "P"))

    def test_the_height_given_to_the_foundation_level_is_the_footings_bottom(self):
        """'Foundation' -2000 typed with the floor heights, no separate founding level."""
        from building.linker import apply_overrides
        b = Building()
        b.add_sheet(Sheet(id="F:V", file="f.dwg", level="FDN", reader="sheets.registry"))
        f = b.add(Element(id="F:V/footing/0", type="footing", sheet="F:V", level="FDN"))
        f.resolved["founding_level"] = Value(None, "missing", 0.0, [])
        apply_overrides(b, {"project": {"levels": {"Level 0": 0, "Foundation": -2000}}})
        self.assertEqual((f.final("founding_level"), f.resolved["founding_level"].status), (-2000.0, "override"))
        # a founding level given on its own still wins
        f.resolved["founding_level"] = Value(None, "missing", 0.0, [])
        apply_overrides(b, {"project": {"founding_level_mm": -1500, "levels": {"Foundation": -2000}}})
        self.assertEqual(f.final("founding_level"), -1500.0)

    def test_the_frame_round_a_raft_schedule_is_not_a_raft(self):
        doc = ezdxf.new()
        rect(doc.modelspace(), 0, 0, 13800, 3900, "0")
        words = ["NOS.", "SLAB", "THICK", "REMARK.", "RS-1", "230", "10", "150", "10", "150", "RAFT BOTTOM", "TOP"]
        b = Building()
        foundations.read(b, view(doc, [T(w, 500 + 1000 * i, 1500) for i, w in enumerate(words)]))
        self.assertEqual(b.of("raft"), [])

    def test_a_layer_named_like_a_footing_mark_is_kept_as_a_second_source(self):
        doc = ezdxf.new()
        rect(doc.modelspace(), 0, 0, 1500, 10800, "F4")
        b = Building()
        foundations.read(b, view(doc, [T("F12", 1700, 5000)]))
        f = b.of("footing")[0]
        self.assertEqual((f.get("mark"), f.get("mark_layer")), ("F12", "F4"))

    def test_a_sheet_border_with_a_raft_label_is_not_a_raft(self):
        doc = ezdxf.new()
        msp = doc.modelspace()
        rect(msp, -1000, -1000, 80000, 120000, "0")                 # the sheet frame
        for i in range(4):
            rect(msp, i * 5000, 0, 1500, 1800, "F1")
        b = Building()
        foundations.read(b, view(doc, [T("RAFT-04", 30000, 50000)] +
                                 [T("F1", i * 5000 + 2000, 900) for i in range(4)]))
        self.assertEqual(len(b.of("raft")), 0)
        self.assertEqual(len(b.of("footing")), 4)

    def test_a_raft_and_its_thickness_from_a_note(self):
        doc = ezdxf.new()
        rect(doc.modelspace(), 0, 0, 20000, 30000, "RAFT")
        b = Building()
        foundations.read(b, view(doc, [T("RAFT-01", 10000, 15000), T("500MM THK RAFT", 2000, 2000)]))
        r = b.of("raft")[0]
        self.assertEqual((r.get("width"), r.get("length"), r.get("thickness")), (20000, 30000, 500.0))

    def test_a_notes_box_mentioning_raft_is_not_a_raft(self):
        """Villa 5: the notes box says CLEAR COVER FOR RAFT = 50MM inside a 14.6 x 3.5 m rectangle."""
        doc = ezdxf.new()
        rect(doc.modelspace(), 0, 0, 14575, 3533, "0")
        b = Building()
        foundations.read(b, view(doc, [T("NOTES: 1. GRADE OF CONCRETE: M30 2. GRADE OF STEEL: FE500 "
                                         "3. CLEAR COVER FOR RAFT = 50MM", 500, 1500)]))
        self.assertEqual(b.of("raft"), [])

    def test_outside_a_foundation_plan_only_marked_outlines_are_footings(self):
        """Villa 5's raft layout shares a region with details: section outlines are not footings."""
        doc = ezdxf.new()
        rect(doc.modelspace(), 0, 0, 1200, 2800, "0")              # a detail's outline
        rect(doc.modelspace(), 5000, 0, 2500, 2800, "0")           # a footing, C12 on it
        b = Building()
        foundations.read(b, view(doc, [T("C12", 6200, 1400)], kind="column_details", title="RAFT LAYOUT"))
        self.assertEqual([f.get("columns") for f in b.of("footing")], [["C12"]])


class FootingSchedules(unittest.TestCase):
    def test_transposed_schedule(self):
        """Sasan Gir: marks across, properties down; a second size line is the step."""
        p = 3000
        texts = [T("SCHEDULE OF FOOTING.", 0, 1000),
                 T("FOOTING MKD.", 500, 0)] + [T(f"F{i}", 6000 + i * p, 0) for i in (1, 2, 3)] + \
                [T("FOOTING SIZE", 500, -500), T("1500 X 1800", 6000 + p, -500),
                 T("2400 X 2700", 6000 + 2 * p, -500), T("1800 X 2100", 6000 + 3 * p, -500),
                 T("(B x L)", 500, -900), T("-", 6000 + p, -900), T("1350 X 1650", 6000 + 2 * p, -900),
                 T("D (STEP-1+STEP2)", 500, -1400), T("450", 6000 + p, -1400), T("750", 6000 + 2 * p, -1400),
                 T("600", 6000 + 3 * p, -1400)]
        b = Building()
        tables.read(b, SimpleNamespace(id="S", texts=texts, levels=[], wing=""))
        got = {e.mark: (e.get("width"), e.get("length"), e.get("depth"), e.get("steps")) for e in b.of("spec")}
        self.assertEqual(got, {"F1": (1500.0, 1800.0, 450.0, None),
                               "F2": (2400.0, 2700.0, 750.0, [[1350.0, 1650.0]]),
                               "F3": (1800.0, 2100.0, 600.0, None)})

    def test_footing_schedule_listed_by_column(self):
        """Villa 5: COLUMN NUMBERS / FOOTING TYPE / FOOTING DIMENSION L B D1 D2."""
        texts = [T("FOOTING SCHEDULE (M25:Fe415)", 0, 1500),
                 T("COLUMN", 300, 900), T("NUMBERS", 300, 600), T("FOOTING", 2000, 900), T("TYPE", 2000, 600),
                 T("FOOTING DIMENSION", 4000, 900), T("L", 3300, 600), T("B", 4000, 600),
                 T("D1", 4700, 600), T("D2", 5400, 600),
                 T("C12,C13", 300, 0), T("Sloped", 2000, 0), T("2800", 3300, 0), T("2500", 4000, 0),
                 T("600", 4700, 0), T("300", 5400, 0)]
        b = Building()
        tables.read(b, SimpleNamespace(id="S", texts=texts, levels=[], wing=""))
        s = {e.mark: e for e in b.of("spec")}
        self.assertEqual(sorted(s), ["C12", "C13"])
        e = s["C12"]
        self.assertEqual((e.get("family"), e.get("by_column"), e.get("length"), e.get("width"),
                          e.get("depth"), e.get("depth_edge")), ("footing", "C12", 2800.0, 2500.0, 600.0, 300.0))


class Linking(unittest.TestCase):
    def setUp(self):
        self.b = Building()
        self.b.add_sheet(Sheet(id="F:V", file="x", reader="sheets.registry", extra={"set": "P"}))
        self.b.add_sheet(Sheet(id="S", file="x", reader="sheets.registry", extra={"set": "P"}))

    def spec(self, mark, w, ln, d, by_column=None):
        props = {"family": Value("footing", "read", 1.0, [Evidence("t", "S")]),
                 "width": Value(w, "read", 1.0, [Evidence("t", "S")]),
                 "length": Value(ln, "read", 1.0, [Evidence("t", "S")]),
                 "depth": Value(d, "read", 1.0, [Evidence("t", "S")])}
        if by_column:
            props["by_column"] = Value(by_column, "read", 1.0, [Evidence("t", "S")])
        self.b.add(Element(id=f"S/spec/{mark}", type="spec", sheet="S", mark=mark, props=props,
                           extra={"table": "SCHEDULE OF FOOTING"}))

    def footing(self, n, w, ln, tag=None, layer_mark=None, cols=None):
        props = {"width": Value(w, "read", 0.8, [Evidence("f", "F:V")]),
                 "length": Value(ln, "read", 0.8, [Evidence("f", "F:V")])}
        if tag:
            props["mark"] = Value(tag, "read", 0.8, [Evidence("f", "F:V", "footing mark")])
        if layer_mark:
            props["mark_layer"] = Value(layer_mark, "read", 0.6, [Evidence("f", "F:V", "layer name")])
        if cols:
            props["columns"] = Value(cols, "read", 0.9, [Evidence("f", "F:V")])
        return self.b.add(Element(id=f"F:V/footing/{n}", type="footing", sheet="F:V", level="FDN",
                                  mark=tag or layer_mark or "", props=props))

    def test_the_schedule_decides_between_tag_and_layer(self):
        """Tagged F12 on layer F4, 1500 x 10800: F4's schedule size matches, so it is F4."""
        self.spec("F4", 1500, 10800, 600)
        self.spec("F12", 1350, 1350, 450)
        f = self.footing(0, 1500, 10800, tag="F12", layer_mark="F4")
        link(self.b)
        self.assertEqual(f.final("mark"), "F4")
        self.assertEqual((f.final("width"), f.final("length"), f.final("depth")), (1500, 10800, 600))

    def test_a_footing_gets_the_spec_of_the_column_it_carries(self):
        self.spec("C12", 2500, 2800, 600, by_column="C12")
        f = self.footing(0, 2500, 2800, cols=["C12"])
        link(self.b)
        self.assertEqual((f.final("depth"), f.resolved["depth"].status), (600, "read"))

    def test_no_schedule_means_depth_missing(self):
        f = self.footing(0, 1200, 1200, tag="F14")
        link(self.b)
        self.assertEqual(f.resolved["depth"].status, "missing")


class Columns(unittest.TestCase):
    def test_shift_between_drawings_is_found_and_must_be_clear(self):
        pts = [(i * 4000.0, j * 5000.0) for i in range(5) for j in range(3)]
        moved = [(x + 246300.0, y - 199300.0) for x, y in pts]
        (dx, dy), hits = _shift(pts, moved)
        self.assertEqual((round(dx), round(dy), hits), (246300, -199300, 15))
        self.assertIsNone(_shift(pts[:3], moved[:3]))           # too few columns to believe

    def test_plan_columns_take_marks_from_the_column_layout(self):
        """Different origins; marks drawn in tag boxes beside the columns."""
        doc = ezdxf.new()
        msp = doc.modelspace()
        texts = []
        bays = [0, 4000, 7500, 12000, 15000, 19500, 23000, 27500]    # uneven, as buildings are
        for i, bx in enumerate(bays):
            x = 100000 + bx
            rect(msp, x, 0, 230, 600, "STR.COLUMN")
            rect(msp, x + 500, 700, 274, 439, "0")               # a tag box round the mark
            texts.append(T(f"C{i + 1}", x + 637, 920, h=150))
        b = Building()
        b.add_sheet(Sheet(id="L:V", file="c.dwg", reader="sheets.registry", extra={"set": "P"}))
        r = view(doc, texts, kind="column_layout", rid="L:V")
        columns.read(b, r)
        self.assertEqual(len([p for p in b.of("column_position") if not p.extra.get("label_only")]), 8)
        b.add_sheet(Sheet(id="P:V/plan", file="p.dwg", reader="plan.legacy",
                          extra={"set": "P", "view_sheet": "P:V", "level_key": "L1"}))
        cols = []
        for i, bx in enumerate(bays):
            cols.append(b.add(Element(id=f"P:V/plan/column/{i}", type="column", sheet="P:V/plan",
                                      geometry={"x": 115 + bx - 5000, "y": 300 - 3000},
                                      props={"width": Value(230.0, "read", 1.0, [Evidence("p")]),
                                             "depth": Value(600.0, "read", 1.0, [Evidence("p")])})))
        link(b)
        self.assertEqual([c.final("mark") for c in cols], [f"C{i + 1}" for i in range(8)])


def line(msp, x0, y0, x1, y1):
    msp.add_line((x0, y0), (x1, y1), dxfattribs={"layer": "s_text"})


def section(msp, x, y, w, d, scale=1.0, dim_w=True, dim_d=True):
    """A column section drawn at `scale` times full size, dimensioned to read w x d."""
    W, D = w * scale, d * scale
    rect(msp, x, y, W, D, "Columns")
    ov = {"dimlfac": 1.0 / scale} if scale != 1.0 else None
    if dim_w:
        msp.add_linear_dim(base=(x + W / 2, y - 0.2 * D), p1=(x, y), p2=(x + W, y), angle=0,
                           override=ov).render()
    if dim_d:
        msp.add_linear_dim(base=(x - 0.2 * W, y + D / 2), p1=(x, y), p2=(x, y + D), angle=90,
                           override=ov).render()


def ranges(b):
    return {(s.mark, tuple(s.extra["level_range"])): (s.get("width"), s.get("depth"))
            for s in b.of("spec")}


class ColumnSchedule(unittest.TestCase):
    def test_sections_drawn_enlarged_are_read_at_their_dimension_scale(self):
        """Sasan Gir COL SCH: a full grid, sections drawn 4x with DIMLFAC 0.25, no size
        written anywhere; C1 stops at GF (no section in the upper band)."""
        doc = ezdxf.new()
        msp = doc.modelspace()
        for y in (-300, 300, 8000, 16000):
            line(msp, -200, y, 15000, y)
        for x in (-200, 15000):
            line(msp, x, -300, x, 16000)
        section(msp, 4800, 2000, 600, 230, scale=4)
        section(msp, 10600, 2000, 700, 300, scale=4)
        section(msp, 10600, 10000, 700, 300, scale=4)
        texts = [T("COLUMN MARKED", 0, 0, h=225), T("C1", 6000, 0, h=225), T("C2", 12000, 0, h=225),
                 T("FOUNDATION", 0, 6000, h=225), T("TO", 0, 5000, h=225), T("GF SLAB LVL", 0, 4000, h=225),
                 T("(M30)", 0, 3500, h=225), T("12-T16", 6000, 1000, h=225), T("T8 @ 100", 6000, 7000),
                 T("Z1 MAIN LINK", 5900, 7500),
                 T("GF SLAB LVL", 0, 14000, h=225), T("TO", 0, 13000, h=225), T("FF SLAB LVL", 0, 12000, h=225)]
        b = Building()
        b.add_sheet(Sheet(id="CS", file="x", reader="sheets.registry"))
        got = column_schedule.read(b, view(doc, texts, kind="column_schedule", rid="CS"))
        self.assertEqual(got, {"column schedule specs": 3})
        self.assertEqual(ranges(b), {("C1", ("FDN", "GF")): (600.0, 230.0),
                                     ("C2", ("FDN", "GF")): (700.0, 300.0),
                                     ("C2", ("GF", "L1")): (700.0, 300.0)})
        c1 = next(s for s in b.of("spec") if s.mark == "C1")
        self.assertEqual((c1.get("grade"), c1.get("bars")), ("M30", ["12-T16"]))
        self.assertEqual(c1.get("stirrups"), [{"text": "T8 @ 100", "zone": "Z1 MAIN LINK"}])
        self.assertIn({"mark": "C1", "range": ["GF", "L1"], "size": None}, b.sheets["CS"].extra["column_schedule"])

    def villa(self, dim_c7=True):
        """Villa 5: no lines across the table - each cell border is drawn band by band;
        marks grouped in one cell; only the width dimensioned."""
        doc = ezdxf.new()
        msp = doc.modelspace()
        line(msp, -100, -150, -100, 1500)
        line(msp, -100, 1500, -100, 3200)
        for x in (750, 2250, 3750):
            line(msp, x, 150, x, 1500)
            line(msp, x, 1500, x, 3200)
        section(msp, 1250, 500, 500, 230, dim_d=False)
        section(msp, 2750, 500, 500, 230, dim_w=dim_c7, dim_d=False)
        section(msp, 1250, 2000, 600, 230, dim_d=False)
        texts = [T("COLUMN MARKED", 0, 0, h=70), T("C2,C5", 1500, 0, h=70), T("C7", 3000, 0, h=70),
                 T("RAFT", 0, 1200, h=80), T("TO", 0, 1000, h=80), T("GROUND FLOOR", 0, 800, h=80),
                 T("M25:Fe415", 1500, 1300, h=70),
                 T("GROUND FLOOR", 0, 2900, h=80), T("TO", 0, 2700, h=80), T("FIRST FLOOR", 0, 2500, h=80)]
        b = Building()
        b.add_sheet(Sheet(id="U1", file="x", reader="sheets.registry"))
        column_schedule.read(b, view(doc, texts, kind="column_details", rid="U1"))
        return b

    def test_grouped_marks_and_a_side_measured_at_the_sections_own_scale(self):
        b = self.villa()
        self.assertEqual(ranges(b), {("C2", ("FDN", "GF")): (500.0, 230.0), ("C5", ("FDN", "GF")): (500.0, 230.0),
                                     ("C7", ("FDN", "GF")): (500.0, 230.0),
                                     ("C2", ("GF", "L1")): (600.0, 230.0), ("C5", ("GF", "L1")): (600.0, 230.0)})
        c2 = next(s for s in b.of("spec") if s.mark == "C2")
        self.assertEqual((c2.props["width"].confidence, c2.props["depth"].confidence), (0.85, 0.7))
        self.assertEqual(c2.get("grade"), "M25")

    def test_a_section_without_dimensions_is_reported_not_sized(self):
        b = self.villa(dim_c7=False)
        self.assertNotIn(("C7", ("FDN", "GF")), ranges(b))
        self.assertEqual([i.mark for i in b.issues if i.category == "column schedule"], ["C7"])

    def test_each_plan_gets_the_size_of_the_range_below_its_slab(self):
        """The GF plan's columns are the FOUNDATION TO GF ones; the 1st floor plan's, GF TO FF."""
        b = Building()
        b.add_sheet(Sheet(id="CS", file="x", reader="sheets.registry", extra={"set": "P"}))
        for rng, (w, d) in {("FDN", "GF"): (700, 300), ("GF", "L1"): (600, 300)}.items():
            ev = [Evidence("column_schedule", "CS")]
            b.add(Element(id=f"CS/spec/{rng[0]}", type="spec", sheet="CS", mark="C2",
                          props={"family": Value("column", "read", 1.0, ev), "width": Value(w, "read", 0.85, ev),
                                 "depth": Value(d, "read", 0.85, ev)},
                          extra={"table": "column schedule", "level_range": list(rng)}))
        cols = {}
        for key in ("GF", "L1", "L2"):
            sid = f"P:{key}/plan"
            b.add_sheet(Sheet(id=sid, file="p", reader="plan.legacy", extra={"set": "P", "level_key": key}))
            cols[key] = b.add(Element(id=f"{sid}/column/0", type="column", sheet=sid, mark="C2",
                                      geometry={"x": 0.0, "y": 0.0}))
        link(b)
        self.assertEqual((cols["GF"].final("width"), cols["GF"].final("depth")), (300, 700))
        self.assertEqual((cols["L1"].final("width"), cols["L1"].final("depth")), (300, 600))
        self.assertEqual(cols["L2"].resolved["depth"].status, "missing")   # no range reaches L2

    def test_the_plans_orientation_is_kept_when_the_schedule_writes_it_the_other_way(self):
        """Schedule 700 x 300 (across x down), plan column measured 300 x 700: one column, not swapped."""
        b = Building()
        b.add_sheet(Sheet(id="CS", file="x", reader="sheets.registry", extra={"set": "P"}))
        ev = [Evidence("column_schedule", "CS")]
        b.add(Element(id="CS/spec/0", type="spec", sheet="CS", mark="C2",
                      props={"family": Value("column", "read", 1.0, ev), "width": Value(700.0, "read", 0.85, ev[:]),
                             "depth": Value(300.0, "read", 0.85, [Evidence("column_schedule", "CS", "dim 2")])},
                      extra={"table": "column schedule", "level_range": ["FDN", "GF"]}))
        b.add_sheet(Sheet(id="P:GF/plan", file="p", reader="plan.legacy", extra={"set": "P", "level_key": "GF"}))
        pev = Evidence("plan.legacy", "P:GF/plan")
        c = b.add(Element(id="P:GF/plan/column/0", type="column", sheet="P:GF/plan", mark="C2",
                          geometry={"x": 0.0, "y": 0.0},
                          props={"width": Value(300.0, "read", 1.0, [pev]), "depth": Value(700.0, "read", 1.0, [pev])}))
        link(b)
        self.assertEqual((c.final("width"), c.final("depth")), (300, 700))
        self.assertEqual((c.resolved["width"].status, c.resolved["depth"].status), ("read", "read"))

    def test_a_label_further_off_is_believed_when_its_schedule_size_matches(self):
        """Sasan Gir: tags up to ~2.5 m from their column. Of two labels in reach, the
        one whose scheduled size is the size drawn on the plan names the column."""
        b = Building()
        b.add_sheet(Sheet(id="L:V", file="c.dwg", reader="sheets.registry", extra={"set": "P"}))
        b.add_sheet(Sheet(id="CS", file="c.dwg", reader="sheets.registry", extra={"set": "P"}))
        b.add_sheet(Sheet(id="P:V/plan", file="p.dwg", reader="plan.legacy",
                          extra={"set": "P", "view_sheet": "P:V", "level_key": "GF"}))
        ev = Evidence("column_positions", "L:V")
        bays = [0, 4000, 7500, 12000, 15000, 19500, 23000, 27500]
        for i, bx in enumerate(bays):                       # marked outlines: they align the plan
            b.add(Element(id=f"L:V/column_position/{i}", type="column_position", sheet="L:V", mark=f"C{i + 1}",
                          geometry={"x": float(bx), "y": 0.0}, props={"mark": Value(f"C{i + 1}", "read", 1.0, [ev])}))
        for n, (m, x) in enumerate((("C9", 52000.0), ("C4", 47700.0))):    # 2000 and 2300 mm away
            b.add(Element(id=f"L:V/column_position/l{n}", type="column_position", sheet="L:V", mark=m,
                          geometry={"x": x, "y": 0.0}, props={"mark": Value(m, "read", 0.7, [ev])},
                          extra={"label_only": True}))
        for m, (w, d) in {"C9": (600.0, 230.0), "C4": (750.0, 450.0)}.items():
            sev = [Evidence("column_schedule", "CS")]
            b.add(Element(id=f"CS/spec/{m}", type="spec", sheet="CS", mark=m,
                          props={"family": Value("column", "read", 1.0, sev), "width": Value(w, "read", 0.85, sev),
                                 "depth": Value(d, "read", 0.85, sev)},
                          extra={"table": "column schedule", "level_range": ["FDN", "GF"]}))
        pev = Evidence("plan.legacy", "P:V/plan")
        cols = [b.add(Element(id=f"P:V/plan/column/{i}", type="column", sheet="P:V/plan",
                              geometry={"x": x, "y": 0.0},
                              props={"width": Value(230.0, "read", 1.0, [pev]), "depth": Value(600.0, "read", 1.0, [pev])}))
                for i, x in enumerate([float(bx) for bx in bays] + [50000.0])]
        stats = link(b)
        self.assertEqual(cols[-1].final("mark"), "C9")
        self.assertEqual(stats["columns marked from the column layout by schedule size"], 1)
        # without the schedule the 2 m label is too far to believe
        b.elements = [e for e in b.elements if e.type != "spec"]
        link(b)
        self.assertIsNone(cols[-1].final("mark"))

    def extent(self, rows, plans, x_above=0.0):
        """A schedule (mark -> [range]) and plans (level key -> [marks]), linked."""
        b = Building()
        b.add_sheet(Sheet(id="CS", file="x", reader="sheets.registry", extra={"set": "P"}))
        for mark, rngs in rows.items():
            for rng in rngs:
                ev = [Evidence("column_schedule", "CS")]
                b.add(Element(id=f"CS/spec/{mark}{rng[0]}", type="spec", sheet="CS", mark=mark,
                              props={"family": Value("column", "read", 1.0, ev), "width": Value(600, "read", 0.85, ev),
                                     "depth": Value(300, "read", 0.85, ev)},
                              extra={"table": "column schedule", "level_range": list(rng)}))
        cols = {}
        for n, (key, marks) in enumerate(plans.items()):
            sid = f"P:{key}/plan"
            b.add_sheet(Sheet(id=sid, file="p", reader="plan.legacy", extra={"set": "P", "level_key": key}))
            for i, m in enumerate(marks):
                cols[(key, m)] = b.add(Element(id=f"{sid}/column/{i}", type="column", sheet=sid, mark=m,
                                               geometry={"x": 5000.0 * i + (x_above if n else 0.0), "y": 0.0}))
        return b, cols

    def ext_issues(self, b):
        return [i for i in b.issues if i.category == "column extent"]

    def test_columns_the_schedule_agrees_with_are_left_as_they_are(self):
        b, cols = self.extent({"C2": [("FDN", "GF"), ("GF", "L1")]}, {"GF": ["C2"], "L1": ["C2"]})
        link(b)
        self.assertEqual([c.final("kind") for c in cols.values()], [None, None])
        self.assertEqual(self.ext_issues(b), [])

    def test_a_column_the_schedule_starts_at_this_floor_is_built_upward(self):
        """C9 'FF TO SF' on the 1st floor plan: it sits on this floor, like a floating column."""
        b, cols = self.extent({"C9": [("L1", "L2")], "C2": [("FDN", "GF"), ("GF", "L1")]}, {"L1": ["C2", "C9"]})
        link(b)
        self.assertEqual(cols[("L1", "C9")].resolved["kind"].value, "column_float")
        self.assertNotIn("kind", cols[("L1", "C2")].resolved)
        self.assertEqual([(i.severity, i.mark) for i in self.ext_issues(b)], [("info", "C9")])
        # decided otherwise in the review: that stands
        link(b, {"elements": {cols[("L1", "C9")].id: {"kind": "column"}}})
        self.assertEqual(cols[("L1", "C9")].final("kind"), "column")

    def test_drawn_where_it_starts_and_on_the_plan_above_it_is_built_once(self):
        """C9 drawn on the 1st floor plan (where it starts) and on the 2nd (under its slab):
        the 2nd floor plan builds it; the 1st floor one is not made to rise as well."""
        b, cols = self.extent({"C9": [("L1", "L2")]}, {"L1": ["C9"], "L2": ["C9"]}, x_above=40.0)
        link(b)
        self.assertNotIn("kind", cols[("L1", "C9")].resolved)
        self.assertEqual([(i.severity, i.mark) for i in self.ext_issues(b)], [("warning", "C9")])
        self.assertIn("exclude it in the review", self.ext_issues(b)[0].message)

    def test_a_column_the_schedule_has_not_at_this_floor_is_reported(self):
        b, cols = self.extent({"C1": [("FDN", "GF")]}, {"GF": ["C1"], "L1": ["C1"]})
        link(b)
        got = self.ext_issues(b)
        self.assertEqual([(i.severity, i.mark, i.sheet) for i in got], [("warning", "C1", "P:L1/plan")])
        self.assertNotIn("kind", cols[("L1", "C1")].resolved)

    def test_columns_running_on_above_the_highest_plan_are_reported(self):
        """C1 runs to ROOF in the schedule; only GF and L1 plans imported: nothing above L1 is built."""
        b, _ = self.extent({"C1": [("FDN", "GF"), ("GF", "L1"), ("L1", "ROOF")], "C3": [("FDN", "GF"), ("GF", "L1")]},
                           {"GF": ["C1", "C3"], "L1": ["C1", "C3"]})
        link(b)
        got = self.ext_issues(b)
        self.assertEqual([(i.severity, i.sheet) for i in got], [("warning", "P:L1/plan")])
        self.assertIn("C1 (to ROOF)", got[0].message)
        self.assertNotIn("C3", got[0].message)

    def test_a_section_drawn_without_dimensions_still_says_the_column_is_there(self):
        b, cols = self.extent({}, {"L1": ["C9"]})
        b.sheets["CS"].extra["column_schedule"] = [{"mark": "C9", "range": ["L1", "L2"], "size": "drawn, not dimensioned"},
                                                   {"mark": "C9", "range": ["GF", "L1"], "size": None}]
        link(b)
        self.assertEqual(cols[("L1", "C9")].resolved["kind"].value, "column_float")

    def test_floor_abbreviations_before_a_floor_word(self):
        self.assertEqual(levels_in("FF SLAB LVL"), ["L1"])
        self.assertEqual(levels_in("rev SF lvl slab"), ["L2"])
        self.assertEqual(levels_in("SF"), [])                 # a footing mark to some consultants
        self.assertEqual(levels_in("FFL +3.150"), [])          # finished floor level


if __name__ == "__main__":
    unittest.main(verbosity=1)
