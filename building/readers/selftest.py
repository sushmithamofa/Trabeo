r"""Tests for the Stage 3 readers: labels, tables, details, layer sizes and profiles.

    python -m building.readers.selftest

Each test rebuilds, small, a case met in the training drawings.
"""
from __future__ import annotations

import unittest
from collections import Counter
from types import SimpleNamespace

import ezdxf

from building import Building
from building import profiles
from building.profiles.suggest import suggest
from building.readers import labels, layer_sizes, tables
from building.sheets.source import TextItem

H = 300.0


def T(s, x, y, h=H, layer="TXT", handle=""):
    """A text whose centre is at (x, y)."""
    return TextItem(s, s, h, x, y, layer, handle or f"{s}@{x:.0f},{y:.0f}", "model", False, x, y)


def rec(kind, texts, rid="S:V1", layers=None, doc=None, handles=None):
    return SimpleNamespace(id=rid, verdict=SimpleNamespace(kind=kind), texts=texts,
                           layers=Counter(layers or {}), levels=["L1"], wing="B", doc=doc,
                           view=SimpleNamespace(handles=handles or [], same_as="", source="viewport"))


def specs(b, mark=None):
    return [e for e in b.elements if e.type == "spec" and (mark is None or e.mark == mark)]


class Tables(unittest.TestCase):
    def beam_table(self, ox, title, rows):
        """Reis Magos style: a header two rows deep, the mark split in three texts."""
        t = [T(title, ox + 2000, 2000),
             T("BEAM", ox + 500, 1000), T("S I Z E", ox + 4000, 1000), T("STIRRUPS", ox + 8000, 1000),
             T("NOS .", ox + 500, 500), T("B", ox + 3500, 500), T("D", ox + 4500, 500),
             T("T", ox + 7500, 500), T("SPACING", ox + 8800, 500)]
        y = -200
        for prefix, nums, w, d, extra in rows:
            t += [T(prefix, ox + 200, y), T("-", ox + 450, y), T(nums, ox + 900, y),
                  T(w, ox + 3500, y), T(d, ox + 4500, y), T("8", ox + 7500, y), T("150", ox + 8800, y)]
            if extra:                                   # a second depth line: "850/" then "750"
                t += [T(extra, ox + 4500, y - 350)]
            y -= 700
        return t

    def test_two_tables_side_by_side_with_split_marks(self):
        texts = self.beam_table(0, "SCHEDULE OF FLOOR BEAMS WING B", [("B", "1,2,3", "230", "600", "")]) + \
            self.beam_table(11000, "SCHEDULE OF FLOOR BEAMS WING C", [("B", "1", "230", "750", "")])
        b = Building()
        tables.read(b, rec("beam_schedule", texts))
        self.assertEqual(sorted(e.mark for e in specs(b) if e.wing == "B"), ["B1", "B2", "B3"])
        self.assertEqual({e.get("depth") for e in specs(b) if e.wing == "B"}, {600.0})
        c = [e for e in specs(b) if e.wing == "C"]
        self.assertEqual([(e.mark, e.get("depth")) for e in c], [("B1", 750.0)])

    def test_varying_depth_is_kept_and_flagged(self):
        texts = self.beam_table(0, "SCHEDULE OF BEAMS", [("GB", "4", "1500", "850/", "750")])
        b = Building()
        tables.read(b, rec("beam_schedule", texts))
        s = specs(b, "GB4")[0]
        self.assertEqual(s.get("depth_varies"), [850.0, 750.0])
        self.assertEqual(s.props["depth"].status, "conflict")

    def test_values_on_the_line_below_the_marks(self):
        """Villa 5: 'S1, S2' on one line, '150  2-Way' on the next."""
        texts = [T("SLAB SCHEDULE (M25 : FE415)", 2000, 2000),
                 T("SLAB", 500, 1000), T("NUMBERS", 500, 600), T("THK", 3000, 600), T("TYPE", 5000, 600),
                 T("S1, S2", 500, 0), T("150", 3000, -400), T("2-Way", 5000, -400),
                 T("S3", 500, -1000), T("125", 3000, -1400), T("1-Way", 5000, -1400)]
        b = Building()
        tables.read(b, rec("slab_schedule", texts))
        got = sorted((e.mark, e.get("thickness"), e.get("type")) for e in specs(b))
        self.assertEqual(got, [("S1", 150.0, "2-Way"), ("S2", 150.0, "2-Way"), ("S3", 125.0, "1-Way")])

    def test_two_column_member_schedule(self):
        """Sasan Gir: BEAM NO. and BEAM SIZE, header words 12.3 heights apart."""
        texts = [T("MEMBER SCHEDULE", 0, 1000), T("BEAM NO.", 1300, 400), T("BEAM SIZE", 5000, 400),
                 T("M1", 700, -100), T("200X100X6mm", 4800, -100)]
        b = Building()
        tables.read(b, rec("beam_schedule", texts))
        s = specs(b, "M1")[0]
        self.assertEqual((s.get("width"), s.get("depth"), s.get("size_text")), (200.0, 100.0, "200X100X6mm"))

    def test_long_mark_lists_and_the_lines_that_carry_them_on(self):
        """ALCAZAR plinth beams: 'PB' '-' '1,2,3,4,5,6,7,10,12,16,' written from the mark column
        runs on over the SIZE column, and the row's list carries on below ('17,20,30,33,' then
        '77,82,104'). A row whose list does not end in a comma takes no line below it."""
        def L(s, x, y, h=250.0):                       # a left-aligned text starting at x
            return TextItem(s, s, h, x, y, "TXT", f"{s}@{x:.0f},{y:.0f}", "model", False,
                            x + 0.4 * h * len(s), y)
        t = [T("SCHEDULE OF PLNTH BEAMS :-", 0, 2300), T("S I Z E", 4600, 1500), T("STIRRUPS", 11600, 1500),
             T("BEAM", 500, 1400), T("NOS .", 550, 1000),
             T("B", 4670, 700), T("D", 5430, 700), T("T", 11600, 700), T("SPACING", 12000, 700)]
        rows = [("1,2,3,4,5,6,7,10,12,16,", ["17,20,30,33,", "77,82,104"], "450"),
                ("8,9,11,22,25,27,28,31,", ["34,45,72"], "600"),
                ("63,64", ["99"], "450")]
        y = 0
        for first, more, d in rows:
            t += [L("PB", 150, y), L("-", 720, y), L(first, 880, y), L("230", 4440, y), L(d, 5250, y),
                  L("8", 11400, y), L("150", 11970, y)]
            t += [L(m, 880, y - 350 * (i + 1)) for i, m in enumerate(more)]
            y -= 350 * (len(more) + 1) + 450
        b = Building()
        tables.read(b, rec("beam_schedule", t))
        got = {e.mark: (e.get("width"), e.get("depth")) for e in specs(b)}
        want = {f"PB{n}": (230.0, 450.0) for n in (1, 2, 3, 4, 5, 6, 7, 10, 12, 16, 17, 20, 30, 33, 77, 82, 104, 63, 64)}
        want.update({f"PB{n}": (230.0, 600.0) for n in (8, 9, 11, 22, 25, 27, 28, 31, 34, 45, 72)})
        self.assertEqual(got, want)

    def test_no_header_no_table(self):
        b = Building()
        tables.read(b, rec("beam_schedule", [T("B1", 0, 0), T("230", 2000, 0), T("600", 3000, 0)]))
        self.assertEqual(specs(b), [])

    @staticmethod
    def ruled(texts, ys, x0=-500, x1=10000, kind="beam_schedule"):
        """The texts and a horizontal line across the table at each y, as a drawn table."""
        doc = ezdxf.new()
        msp = doc.modelspace()
        hs = [msp.add_line((x0, y), (x1, y)).dxf.handle for y in ys]
        return rec(kind, texts, doc=doc, handles=hs)

    def test_one_size_printed_for_several_mark_lines_in_one_ruled_cell(self):
        """Stilt A wing: 'B - 10,11,12,13' ... 'B - 38,39' in one ruled cell, '230 450'
        printed once beside the middle line. Every line of the cell takes it; the rows
        in their own cells keep theirs."""
        t = [T("SCHEDULE OF FLOOR BEAMS", 2000, 2000), T("BEAM", 500, 1000), T("S I Z E", 4000, 1000),
             T("NOS .", 500, 500), T("B", 3500, 500), T("D", 4500, 500)]
        rows = [("1", 0, "230", "600"), ("2,3", -700, None, None), ("4", -1400, "230", "450"),
                ("5,6", -2100, None, None), ("7", -2800, "230", "750")]
        for nums, y, w, d in rows:
            t += [T("B", 200, y), T("-", 450, y), T(nums, 900, y)]
            if w:
                t += [T(w, 3500, y - 60), T(d, 4500, y - 60)]
        ys = [250, -350, -2450, -3150]               # B-2,3 .. B-5,6 share the cell -350 .. -2450
        b = Building()
        tables.read(b, self.ruled(t, ys))
        got = {e.mark: (e.get("width"), e.get("depth")) for e in specs(b)}
        self.assertEqual(got, {"B1": (230.0, 600.0), "B2": (230.0, 450.0), "B3": (230.0, 450.0),
                               "B4": (230.0, 450.0), "B5": (230.0, 450.0), "B6": (230.0, 450.0),
                               "B7": (230.0, 750.0)})
        # with no lines drawn nothing says the cell is shared: those lines stay unread
        b = Building()
        tables.read(b, rec("beam_schedule", t))
        self.assertEqual(specs(b, "B2")[0].get("width"), None)

    def test_a_ruled_cell_with_two_sizes_gives_none_to_the_others(self):
        t = [T("SCHEDULE OF FLOOR BEAMS", 2000, 2000), T("BEAM", 500, 1000), T("S I Z E", 4000, 1000),
             T("NOS .", 500, 500), T("B", 3500, 500), T("D", 4500, 500),
             T("B1", 600, 0), T("230", 3500, 0), T("600", 4500, 0),
             T("B2", 600, -700), T("B3", 600, -1400), T("230", 3500, -1400), T("450", 4500, -1400),
             T("B4", 600, -2800), T("230", 3500, -2800), T("750", 4500, -2800)]
        b = Building()
        tables.read(b, self.ruled(t, [250, -2450, -3150]))
        got = {e.mark: e.get("depth") for e in specs(b)}
        self.assertEqual(got["B2"], None)                    # 600 or 450? not guessed
        self.assertEqual((got["B1"], got["B3"], got["B4"]), (600.0, 450.0, 750.0))

    def test_a_mark_with_no_number_is_a_row_of_its_own_in_the_mark_column(self):
        """Stilt A wing slab schedule: 'S' '-' '1' 150, then 'ST' 200 - the ST row's values
        fell into S-1's record. In the table's mark column ST is a mark."""
        t = [T("SCHEDULE OF SLAB:-", 2000, 2000), T("SLAB", 500, 1000), T("THICK", 3000, 1000),
             T("NOS.", 500, 600), T("T", 6000, 600),
             T("S", 100, 0), T("-", 400, -40), T("1", 700, 0), T("150", 3000, 0), T("8", 6000, 0),
             T("ST", 300, -500), T("200", 3000, -500), T("10", 6000, -500)]
        for drawn in (rec("slab_schedule", t), self.ruled(t, [250, -250, -750], kind="slab_schedule")):
            b = Building()
            tables.read(b, drawn)
            got = {e.mark: (e.get("family"), e.get("thickness")) for e in specs(b)}
            self.assertEqual(got, {"S1": ("slab", 150.0), "ST": ("stair", 200.0)})

    def test_a_ruled_row_whose_label_is_not_a_mark_gives_nothing_to_the_row_above(self):
        t = [T("SCHEDULE OF SLAB:-", 2000, 2000), T("SLAB", 500, 1000), T("THICK", 3000, 1000),
             T("NOS.", 500, 600),
             T("300", 3000, -500),                   # stored first in the file
             T("S1", 500, 0), T("150", 3000, 0),
             T("LIFT PIT", 500, -500)]
        b = Building()
        tables.read(b, self.ruled(t, [250, -250, -750], kind="slab_schedule"))
        self.assertEqual([(e.mark, e.get("thickness")) for e in specs(b)], [("S1", 150.0)])

    def test_bare_letters_stay_letters_on_plans_and_in_split_marks(self):
        from building.marks import bare_mark, parse_mark
        self.assertEqual((parse_mark("ST"), parse_mark("C")), (None, None))      # plans: unchanged
        self.assertEqual([bare_mark(s).key for s in ("ST", "SB", "C")], ["ST", "SB", "C"])
        self.assertEqual([bare_mark(s) for s in ("D", "T", "GF", "LIFT", "St", "NO")], [None] * 6)
        # "C" with its "-" "2" drawn a little below its line is the split mark C-2, not "C"
        t = [T("SCHEDULE OF COLUMNS", 2000, 2000), T("COLUMN", 500, 1000), T("NOS", 500, 600),
             T("SIZE", 3500, 600), T("C", 100, 0), T("-", 400, -200), T("2", 700, -200), T("230X600", 3500, 0)]
        b = Building()
        tables.read(b, rec("column_schedule", t))
        self.assertNotIn("C", [e.mark for e in specs(b)])

    def test_a_size_written_in_pieces(self):
        """Stilt A column schedule: '230' 'X' '1050' three texts; 'REFER' 'DETAIL' is no size."""
        t = [T("SCHEDULE OF COLUMNS", 2000, 2000), T("COLUMN", 500, 1000), T("NOS", 500, 600),
             T("SIZE", 3500, 600),
             T("C-1", 500, 0), T("REFER", 3100, 0), T("DETAIL", 3900, 0),
             T("C-4", 500, -600), T("230", 3000, -600), T("X", 3500, -600), T("1050", 4000, -600),
             T("C-10", 500, -1500), T("300", 3000, -1500), T("X", 3500, -1500), T("1600", 4000, -1500),
             T("300", 3000, -1200), T("X", 3500, -1200), T("950", 4000, -1200)]
        b = Building()
        tables.read(b, self.ruled(t, [300, -300, -900, -1800], kind="column_schedule"))
        c10 = specs(b, "C10")[0]                 # two sizes in its ruled cell: neither is guessed
        self.assertEqual((c10.get("width"), c10.get("size_text")), (None, "300 X 950 300 X 1600"))
        c1, c4 = specs(b, "C1")[0], specs(b, "C4")[0]
        self.assertEqual((c1.get("width"), c1.get("size_text")), (None, "REFER DETAIL"))
        self.assertEqual((c4.get("width"), c4.get("depth"), c4.get("size_text")), (230.0, 1050.0, "230 X 1050"))


class Labels(unittest.TestCase):
    def test_plan_captions_and_a_size_written_beside_a_mark(self):
        texts = [T("B4a(230X600)", 0, 0, h=150), T("B12", 5000, 0, h=150), T("230X450", 5000, -300, h=150),
                 T("T8 @ 150", 9000, 0, h=150)]
        b = Building()
        labels.read(b, rec("framing_plan", texts))
        caps = {e.mark: (e.get("width"), e.get("depth")) for e in b.elements if e.type == "caption"}
        self.assertEqual(caps, {"B4A": (230.0, 600.0), "B12": (230.0, 450.0)})

    def test_detail_captions_are_specs_and_bare_marks_are_not(self):
        texts = [T("GB 1(230x450)", 0, 0), T("GB2", 3000, 0)]
        b = Building()
        labels.read(b, rec("beam_details", texts))
        self.assertEqual([(e.mark, e.get("depth")) for e in specs(b)], [("GB1", 450.0)])

    def test_profile_prefix_meaning(self):
        b = Building()
        labels.read(b, rec("framing_plan", [T("PB3(230X450)", 0, 0)]), {"PB": "footing"})
        self.assertEqual(b.elements[0].get("family"), "footing")

    def test_a_thickness_written_under_a_slab_mark(self):
        """New block: 'NS1' with '140 MM THK.' under it; Sasan Gir 1st: 'S8' over '150 MM THK.'.
        A beam mark near it takes no thickness."""
        texts = [T("NS1", 0, 0, h=225), T("140 MM THK.", 0, -380, h=225),
                 T("S8", 9000, 0, h=225), T("150 MM THK.", 9000, -420, h=225),
                 T("NB4", 20000, 0, h=200), T("125 THK", 20000, -300, h=200)]
        b = Building()
        labels.read(b, rec("framing_plan", texts))
        caps = {e.mark: e.get("thickness") for e in b.elements if e.type == "caption"}
        self.assertEqual(caps, {"NS1": 140.0, "S8": 150.0, "NB4": None})

    def test_the_plans_general_slab_note(self):
        """'* ALL SLABS ARE 125mm THK. UNLESS OTHERWISE SPECIFIED.' is kept with the plan."""
        from building.model import Sheet
        b = Building()
        b.add_sheet(Sheet(id="S:V1", file="x.dxf", layout="", kind="framing_plan", title="", wing="", level="",
                          reader="test", extra={}))
        labels.read(b, rec("framing_plan", [T("* ALL SLABS ARE 125mm THK. UNLESS OTHERWISE SPECIFIED.", 0, 0)]))
        self.assertEqual(b.sheets["S:V1"].extra["slab_thickness_note"]["value"], 125.0)
        self.assertIsNotNone(labels.SLAB_NOTE.search("SLAB THICKNESS 150 MM UNLESS NOTED OTHERWISE"))
        self.assertIsNone(labels.SLAB_NOTE.search("ALL BEAMS ARE 230 THK"))


class SectionsWithoutSizes(unittest.TestCase):
    """A section captioned 'GB1' only, its size given by its dimension lines."""

    def sheet(self, elevation=False):
        doc = ezdxf.new()
        msp = doc.modelspace()
        if elevation:
            # a beam elevation: a long outline, span dimensions near the caption
            msp.add_lwpolyline([(0, 0), (6000, 0), (6000, 600), (0, 600)], close=True)
            msp.add_linear_dim(base=(0, -400), p1=(0, 0), p2=(2100, 0)).render()
            msp.add_linear_dim(base=(-400, 0), p1=(0, 0), p2=(0, 1000), angle=90).render()
            texts = [T("B2", 3000, -900, h=100)]
        else:
            msp.add_lwpolyline([(0, 0), (230, 0), (230, 600), (0, 600)], close=True)
            msp.add_linear_dim(base=(0, -150), p1=(0, 0), p2=(230, 0)).render()
            msp.add_linear_dim(base=(-150, 0), p1=(0, 0), p2=(0, 600), angle=90).render()
            texts = [T("GB1", 115, -450, h=75)]
        handles = [e.dxf.handle for e in msp]
        return rec("beam_details", texts, doc=doc, handles=handles)

    def test_size_read_from_the_section_dimensions(self):
        from building.readers import details
        b = Building()
        details.read(b, self.sheet())
        got = [(e.mark, e.get("width"), e.get("depth"), e.extra.get("from")) for e in specs(b)]
        self.assertEqual(got, [("GB1", 230.0, 600.0, "section dimensions")])
        self.assertLess(specs(b)[0].props["depth"].confidence, 1.0)

    def test_span_dimensions_on_an_elevation_are_not_a_size(self):
        from building.readers import details
        b = Building()
        details.read(b, self.sheet(elevation=True))
        self.assertEqual(specs(b), [])


class LayerSizes(unittest.TestCase):
    def test_size_in_a_layer_name_is_a_hint(self):
        b = Building()
        n = layer_sizes.read(b, rec("framing_plan", [], layers={"BEAM-230X600": 40, "S-B5-230X600": 3,
                                                                   "COLUMN": 10, "A-WALL": 5}))
        self.assertEqual(n, 2)
        self.assertEqual({(e.extra["layer"], e.get("width"), e.get("depth")) for e in b.elements},
                         {("BEAM-230X600", 230.0, 600.0), ("S-B5-230X600", 230.0, 600.0)})

    def test_a_drawing_without_them_is_fine(self):
        self.assertEqual(layer_sizes.read(Building(), rec("framing_plan", [], layers={"BEAM": 9})), 0)

    def test_an_area_hatched_as_another_block_is_a_scope_zone(self):
        """New block: an ANSI31 hatch on layer 'Main Block' over the lifts, stair and corridor."""
        from building.readers import scope
        doc = ezdxf.new()
        msp = doc.modelspace()
        h = msp.add_hatch(dxfattribs={"layer": "Main Block"})
        h.paths.add_polyline_path([(0, 0), (5000, 0), (5000, 18000), (0, 18000)], is_closed=True)
        small = msp.add_hatch(dxfattribs={"layer": "Main Block"})           # a symbol, not an area
        small.paths.add_polyline_path([(0, 0), (500, 0), (500, 500), (0, 500)], is_closed=True)
        other = msp.add_hatch(dxfattribs={"layer": "SUNK 150"})
        other.paths.add_polyline_path([(0, 0), (5000, 0), (5000, 5000), (0, 5000)], is_closed=True)
        handles = [h.dxf.handle, small.dxf.handle, other.dxf.handle]
        b = Building()
        r = rec("framing_plan", [], doc=doc, handles=handles)
        r.title = "STRUCTURAL LAY-OUT AT SECOND FLOOR LVL."
        self.assertEqual(scope.read(b, r), 1)
        z = b.of("scope_zone")[0]
        self.assertEqual((z.extra["layer"], len(z.geometry["outline"])), ("Main Block", 4))
        # the Main Block's own drawing: its block is not "another" one
        r.title = "MAIN BLOCK - TERRACE LVL FRAMING"
        self.assertEqual(scope.read(Building(), r), 0)

    def test_a_drawn_section_is_read_at_its_written_sizes(self):
        """Main Block terrace SECTION X-X, drawn NOT to scale (375 drawn 561 / 600 units, 600
        drawn 1050, 100 drawn 347): the shape must come at the written sizes."""
        from building.readers import beam_sections
        from building.sheets.source import texts_in
        doc = ezdxf.new()
        msp = doc.modelspace()
        out = [(561, 0), (0, 0), (0, 347), (561, 1050), (1161, 1050), (1161, 0)]
        msp.add_lwpolyline(out, close=True, dxfattribs={"layer": "S-BEAM"})
        hung = msp.add_lwpolyline([(0, 0), (352, 0), (352, -641), (0, -641)], close=True,
                                  dxfattribs={"layer": "S-BEAM"})
        lay = {"layer": "S-BEAM"}
        msp.add_line((0, -330), (1161, -330), dxfattribs=lay)                 # 375 | 375 under it
        for x in (0, 580, 1161):
            msp.add_line((x, -360), (x, -300), dxfattribs=lay)
        msp.add_text("375", height=75, rotation=0, dxfattribs={"insert": (198, -318)})
        msp.add_text("375", height=75, rotation=0, dxfattribs={"insert": (749, -318)})
        msp.add_line((1299, 0), (1299, 1050), dxfattribs=lay)                 # 600 on the right
        for y in (0, 1050):
            msp.add_line((1240, y), (1359, y), dxfattribs=lay)
        msp.add_text("600", height=75, rotation=270, dxfattribs={"insert": (1327, 831)})
        msp.add_line((-93, 0), (-93, 334), dxfattribs=lay)                    # 100 on the left
        for y in (0, 334):
            msp.add_line((-117, y), (-68, y), dxfattribs=lay)
        msp.add_text("100", height=75, rotation=90, dxfattribs={"insert": (-121, 45)})
        msp.add_text("SECTION :- X-X", height=100, dxfattribs={"insert": (35, -1300)})
        handles = [e.dxf.handle for e in msp]
        r = rec("beam_details", texts_in(msp, "model"), doc=doc, handles=handles)
        b = Building()
        self.assertEqual(beam_sections.read(b, r), 1)
        s = b.of("beam_section")[0]
        self.assertEqual((s.mark, s.get("width"), s.get("depth")), ("X", 750.0, 600.0))
        self.assertEqual(sorted(map(tuple, s.extra["profile"])),
                         [(0.0, -600.0), (0.0, -500.0), (375.0, -600.0), (375.0, 0.0), (750.0, -600.0), (750.0, 0.0)])
        self.assertEqual(s.extra["undimensioned"], [])
        self.assertTrue(s.extra["hung"] and "not modelled" in s.extra["hung"][0])
        # the plan: X ... X across a beam is a cut; a letter no section is titled with is not
        msp.add_text("X", height=260, dxfattribs={"insert": (5000, 9000)})
        msp.add_text("X", height=260, dxfattribs={"insert": (5070, 7260)})
        msp.add_text("G", height=260, dxfattribs={"insert": (8000, 9000)})
        msp.add_text("G", height=260, dxfattribs={"insert": (8000, 7260)})
        plan = rec("framing_plan", texts_in(msp, "model"), doc=doc)
        b2 = Building()
        self.assertEqual(beam_sections.read(b2, plan), 1)
        self.assertEqual(b2.of("section_cut")[0].mark, "X")
        del hung

    def test_dashed_or_solid_is_read_from_the_line_pattern(self):
        """HIDDEN is dashed; CONTINOUS (sic) and '1' are solid whatever they are called."""
        from building.readers import linework
        doc = ezdxf.new()
        doc.linetypes.add("HIDDEN", pattern=[9.525, 6.35, -3.175])           # dash, gap: dashed
        doc.linetypes.add("CONTINOUS", pattern=[0.0])
        doc.linetypes.add("1", pattern=[0.0])
        doc.layers.add("BEAM-230X600", linetype="HIDDEN")
        msp = doc.modelspace()
        a = msp.add_line((0, 0), (5000, 0), dxfattribs={"layer": "BEAM-230X600"})                 # by layer: dashed
        c = msp.add_line((0, 230), (5000, 230), dxfattribs={"layer": "BEAM-230X600", "linetype": "CONTINOUS"})
        d = msp.add_line((0, 460), (5000, 460), dxfattribs={"layer": "BEAM-230X600", "linetype": "1"})
        t = msp.add_line((0, 900), (5000, 900), dxfattribs={"layer": "TEXT"})                       # not a beam layer
        b = Building()
        from building import Sheet
        r = rec("framing_plan", [], doc=doc, handles=[x.dxf.handle for x in (a, c, d, t)])
        b.add_sheet(Sheet(id=r.id, file="x.dwg"))
        self.assertEqual(linework.read(b, r), 3)
        self.assertEqual([ln[5] for ln in b.sheets[r.id].extra["beam_lines"]["lines"]], [1, 0, 0])

    def test_a_building_section_in_a_scaled_block_is_read(self):
        """Main Block 'SECTION: C-C': a block at scale 1.495, level lines 'Terrace lvl' /
        '2nd Floor lvl', beam boxes with an X marked B19 and B7, the slab sloping from B19
        up to B7, and a steel roof drawn above (not a slab: it does not end on two boxes)."""
        from building.readers import building_sections
        doc = ezdxf.new()
        blk = doc.blocks.new("SEC")
        def box(x, top, mark):
            blk.add_lwpolyline([(x, top - 600), (x + 230, top - 600), (x + 230, top), (x, top)], close=True)
            blk.add_line((x, top - 600), (x + 230, top))
            blk.add_line((x + 230, top - 600), (x, top))
            blk.add_text(mark, height=100, dxfattribs={"insert": (x + 330, top - 300)})
        box(0, -2270, "B19")
        box(3300, 0, "B7")
        blk.add_line((0, -2270), (3300, 0))                       # the sloping slab's top
        blk.add_line((3300, 0), (6000, 2000))                      # the steel roof above
        blk.add_line((3530, 0), (5800, 0))
        blk.add_text("Terrace lvl", height=120, dxfattribs={"insert": (4200, 240)})
        blk.add_line((3530, -3600), (5800, -3600))
        blk.add_text("2nd Floor lvl", height=120, dxfattribs={"insert": (4200, -3360)})
        blk.add_text("%%USECTION: C-C", height=150, dxfattribs={"insert": (1000, -4500)})
        ins = doc.modelspace().add_blockref("SEC", (10000, 20000), dxfattribs={"xscale": 1.495, "yscale": 1.495})
        b = Building()
        r = rec("framing_plan", [], doc=doc, handles=[ins.dxf.handle])
        self.assertEqual(building_sections.read(b, r), 1)
        s = b.of("building_section")[0]
        self.assertEqual((s.mark, round(s.get("scale"), 3)), ("C", 1.495))
        lv = {x["name"]: x["y"] for x in s.extra["levels"]}
        self.assertEqual(sorted(lv), ["2nd Floor", "Terrace"])
        boxes = {x["mark"]: x for x in s.extra["beams"]}
        self.assertAlmostEqual((boxes["B19"]["top"] - lv["Terrace"]) / 1.495, -2270, delta=1)
        self.assertEqual(len(s.extra["slopes"]), 1, "the steel roof is not a sloping slab")

    def test_every_legend_row_is_read_and_found_on_the_plan(self):
        """Main Block legend: '*THUS MARKED FABRICATION ROOF ...' (ANSI31 sample) and '*THUS
        MARKED RCC COLUMN ENDS AT THIS LVL.' (ANSI37 on the column layer); a small solid
        marker beside the words is not a sample; the plan's areas are found, in a block too."""
        from building.readers import legend_zones
        doc = ezdxf.new()
        msp = doc.modelspace()

        def hatch(container, pts, pattern, layer):
            h = container.add_hatch(dxfattribs={"layer": layer})
            if pattern == "SOLID":
                h.set_solid_fill()
            else:
                h.set_pattern_fill(pattern, scale=10)
            h.paths.add_polyline_path(pts, is_closed=True)
            return h
        ents = []
        ents.append(msp.add_text("%%U*THUS MARKED FABRICATION ROOF AS PER ARCH. PROFILE.", height=100,
                                 dxfattribs={"insert": (1000, 0)}))
        ents.append(hatch(msp, [(-100, -180), (812, -180), (812, 286), (-100, 286)], "ANSI31", "HATCH"))
        ents.append(hatch(msp, [(950, 0), (980, 0), (980, 30), (950, 30)], "SOLID", "0"))       # a marker
        ents.append(msp.add_text("%%U*THUS MARKED RCC COLUMN ENDS AT THIS LVL.", height=100,
                                 dxfattribs={"insert": (1000, -645)}))
        ents.append(hatch(msp, [(-100, -825), (812, -825), (812, -359), (-100, -359)], "ANSI37", "STR.COLUMN"))
        ents.append(hatch(msp, [(0, 10000), (9000, 10000), (9000, 13000), (0, 13000)], "ANSI31", "Roof"))
        blk = doc.blocks.new("COLS")
        hatch(blk, [(0, 0), (230, 0), (230, 600), (0, 600)], "ANSI37", "STR.COLUMN")
        hatch(blk, [(5000, 0), (5230, 0), (5230, 600), (5000, 600)], "SOLID", "STR.COLUMN")
        ents.append(msp.add_blockref("COLS", (20000, 20000)))
        b = Building()
        r = rec("framing_plan", [], doc=doc, handles=[e.dxf.handle for e in ents])
        legend_zones.read(b, r)
        got = sorted((e.type, e.mark) for e in b.elements)
        self.assertEqual(got, [("legend_zone", "column ends"), ("legend_zone", "steel roof")])
        self.assertEqual(legend_zones.kind_of("SLOPING RCC SLAB AS PER ARCH"), "sloping slab")
        self.assertEqual(legend_zones.kind_of("RCC COLUMN THROUGH"), "column through")

    def test_blocks_on_a_size_layer_are_counted(self):
        """New block: the stair block sits on BEAM-230X1325, so that name is not only a beam's."""
        doc = ezdxf.new()
        doc.blocks.new("STAIR").add_line((0, 0), (1000, 0))
        msp = doc.modelspace()
        ins = msp.add_blockref("STAIR", (0, 0), dxfattribs={"layer": "BEAM-230X1325"})
        ln = msp.add_line((0, 0), (5000, 0), dxfattribs={"layer": "BEAM-230X1325"})
        other = msp.add_line((0, 0), (5000, 0), dxfattribs={"layer": "BEAM-230X600"})
        b = Building()
        layer_sizes.read(b, rec("framing_plan", [], layers={"BEAM-230X1325": 2, "BEAM-230X600": 1}, doc=doc,
                                handles=[ins.dxf.handle, ln.dxf.handle, other.dxf.handle]))
        got = {e.extra["layer"]: e.extra.get("blocks", 0) for e in b.elements}
        self.assertEqual(got, {"BEAM-230X1325": 1, "BEAM-230X600": 0})


class Profiles(unittest.TestCase):
    def test_profile_layers_win_and_leave_other_roles(self):
        cfg = {"layer_roles": {"beam": ["BEAM"], "ignore": ["*HATCH*", "BEAM-230X600"]}}
        out = profiles.apply(cfg, {"layer_roles": {"beam": ["BEAM-230X600"]}})
        self.assertEqual(out["layer_roles"]["beam"][0], "BEAM-230X600")
        self.assertNotIn("BEAM-230X600", out["layer_roles"]["ignore"])
        self.assertEqual(cfg["layer_roles"]["ignore"], ["*HATCH*", "BEAM-230X600"])    # base untouched

    def test_later_profile_wins(self):
        out = profiles.apply({"layer_roles": {}}, {"layer_roles": {"beam": ["X"]}},
                             {"layer_roles": {"column": ["X"]}})
        self.assertEqual(out["layer_roles"]["column"], ["X"])
        self.assertEqual(out["layer_roles"]["beam"], [])

    def test_suggestions_judge_content_not_just_names(self):
        doc = ezdxf.new()
        msp = doc.modelspace()
        for i in range(25):                       # beam faces: pairs 230 apart
            y = i * 3000
            msp.add_line((0, y), (6000, y), dxfattribs={"layer": "BEAM-230X600"})
            msp.add_line((0, y + 230), (6000, y + 230), dxfattribs={"layer": "BEAM-230X600"})
            msp.add_line((0, y), (6000, y), dxfattribs={"layer": "00_AUDMOL COMMENT"})
            msp.add_line((0, y + 230), (6000, y + 230), dxfattribs={"layer": "00_AUDMOL COMMENT"})
            msp.add_line((0, y), (6000, y), dxfattribs={"layer": "ALL FOUNDATION"})
            msp.add_line((0, y + 300), (6000, y + 300), dxfattribs={"layer": "ALL FOUNDATION"})
        for i in range(8):                        # columns as closed rectangles
            msp.add_lwpolyline([(i * 4000, -2000), (i * 4000 + 230, -2000), (i * 4000 + 230, -1400),
                                (i * 4000, -1400)], close=True, dxfattribs={"layer": "STR.COLUMN"})
            for dx, dy in ((0, 0), (200, 0), (200, 200), (0, 200)):          # leader boxes
                msp.add_line((i * 4000 + dx, -900), (i * 4000 + dx + 200, -900 + dy),
                             dxfattribs={"layer": "COL.NO."})
        handles = [e.dxf.handle for e in msp]
        p = suggest([rec("framing_plan", [], doc=doc, handles=handles)])
        roles = {l: r for r, ls in p["layer_roles"].items() for l in ls}
        self.assertEqual(roles.get("BEAM-230X600"), "beam")
        self.assertEqual(roles.get("STR.COLUMN"), "column")
        self.assertNotIn("00_AUDMOL COMMENT", roles)      # markup, however beam-like
        self.assertNotIn("ALL FOUNDATION", roles)         # foundations get their own reader
        self.assertNotIn("COL.NO.", roles)                # a number layer is never an element
        self.assertFalse(p["confirmed"])

    def test_every_layer_the_plan_draws_on_is_judged(self):
        """New block: BEAM-150X450 (2 lines), SB (the toilet beams), S-STRU. CUTOUT (the X
        boxes) and a staircase inside a block were all ignored, and the slabs went wrong.
        A block's brick piers and overhead lines are still not structure."""
        doc = ezdxf.new()
        msp = doc.modelspace()
        msp.add_line((0, 0), (3000, 0), dxfattribs={"layer": "BEAM-150X450"})
        msp.add_line((0, 150), (3000, 150), dxfattribs={"layer": "BEAM-150X450"})
        for i in range(4):                                        # SB beams: face pairs 230 apart
            x = 5000 + i * 1300
            msp.add_line((x, 0), (x, 1435), dxfattribs={"layer": "SB"})
            msp.add_line((x + 230, 0), (x + 230, 1435), dxfattribs={"layer": "SB"})
        for x0 in (0, 3000):                                      # two cut-outs, each an X in a box
            msp.add_lwpolyline([(x0, 3000), (x0 + 2000, 3000), (x0 + 2000, 5000), (x0, 5000)], close=True,
                               dxfattribs={"layer": "S-STRU. CUTOUT"})
            msp.add_line((x0, 3000), (x0 + 2000, 5000), dxfattribs={"layer": "S-STRU. CUTOUT"})
            msp.add_line((x0, 5000), (x0 + 2000, 3000), dxfattribs={"layer": "S-STRU. CUTOUT"})
        blk = doc.blocks.new("STAIR-BLOCK")
        for i in range(10):                                       # treads, inside a block
            blk.add_line((0, i * 280), (1200, i * 280), dxfattribs={"layer": "Stair"})
        for i in range(6):                                        # brick piers and overhead lines
            blk.add_lwpolyline([(3000 + i * 600, 0), (3230 + i * 600, 0), (3230 + i * 600, 230),
                                (3000 + i * 600, 230)], close=True, dxfattribs={"layer": "Brick Piller"})
        for i in range(12):
            blk.add_line((0, 4000 + i * 400), (6000, 4000 + i * 400), dxfattribs={"layer": "OVERHEAD LINES"})
        msp.add_blockref("STAIR-BLOCK", (20000, 0), dxfattribs={"layer": "0"})
        handles = [e.dxf.handle for e in msp]
        p = suggest([rec("framing_plan", [], doc=doc, handles=handles)])
        roles = {l: r for r, ls in p["layer_roles"].items() for l in ls}
        self.assertEqual(roles.get("BEAM-150X450"), "beam")
        self.assertEqual(roles.get("SB"), "beam")
        self.assertEqual(roles.get("S-STRU. CUTOUT"), "opening")
        self.assertEqual(roles.get("Stair"), "stair")
        self.assertNotIn("Brick Piller", roles)
        self.assertNotIn("OVERHEAD LINES", roles)


if __name__ == "__main__":
    unittest.main(verbosity=1)
