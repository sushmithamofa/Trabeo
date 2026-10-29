r"""Tests for Stage 8: the reviewed building written out as the Revit build's model.json.

    python -m building.stage8_selftest
"""
from __future__ import annotations

import copy
import unittest

from building import Building, Element, Evidence, Value
from building.export import export_plan
from building.linker import link
from building.readers.legacy_plan import from_legacy, to_legacy

SID = "P:V/plan"


def plan(columns=(), beams=(), walls=(), slabs=()):
    d = {"source_file": "p.dwg", "level_name": "Level 1", "dxf_file": "p.dxf", "units": "mm",
         "dxf_version": "AC1032", "base_point": [0.0, 0.0], "stats": {}, "layer_map": {}, "grids": [],
         "columns": list(columns), "walls": list(walls), "beams": list(beams), "slabs": list(slabs),
         "slab_tags": [], "folds": [], "sunk": [], "openings": [], "issues": [],
         "counts": {}, "issue_counts": {}}
    b = from_legacy(copy.deepcopy(d), SID)
    b.sheets[SID].extra.update({"set": "P", "level_key": "L1"})
    return b, d


def col(mark, x, y, w=230.0, d=450.0, h="C"):
    return {"mark": mark, "x": x, "y": y, "rotation": 0.0, "width": w, "depth": d, "kind": "column",
            "outline": [], "source_layer": "COL", "handle": f"{h}{x:.0f}"}


def spec(b, mark, w, d, kind_extra=None, n=0):
    ev = [Evidence("tables", "S")]
    b.add(Element(id=f"S/spec/{mark}{n}", type="spec", sheet="S", mark=mark,
                  props={"family": Value("column", "read", 1.0, ev), "width": Value(w, "read", 1.0, ev),
                         "depth": Value(d, "read", 1.0, ev)},
                  extra=kind_extra or {"table": "COLUMN SCHEDULE"}))


def _with_section(b, width, xs, y0=900.0, y1=-1500.0):
    """A drawn section X-X of this width, cut across at each x on the plan."""
    from building import Sheet
    b.sheets[SID].extra["view_sheet"] = "P:V"
    if "P:V" not in b.sheets:
        b.add_sheet(Sheet(id="P:V", file="p.dwg", kind="framing_plan", reader="sheets.registry", extra={"set": "P"}))
        b.add_sheet(Sheet(id="P:D", file="p.dwg", kind="beam_details", reader="sheets.registry", extra={"set": "P"}))
    ev = [Evidence("beam_sections", "P:D")]
    b.add(Element(id="P:D/beam_section/0", type="beam_section", sheet="P:D", mark="X",
                  props={"width": Value(width, "read", 1.0, ev), "depth": Value(600.0, "read", 1.0, ev)},
                  extra={"profile": [[375, -600], [0, -600], [0, -500], [375, 0], [750, 0], [750, -600]],
                         "title": "SECTION :- X-X", "undimensioned": [], "hung": []}))
    for n, x in enumerate(xs):
        b.add(Element(id=f"P:V/section_cut/{n}", type="section_cut", sheet="P:V", mark="X",
                      geometry={"a": [x, y0], "b": [x, y1]}, props={"name": Value("X", "read", 0.9, ev)}))


class Export(unittest.TestCase):
    def test_nothing_to_settle_changes_nothing(self):
        b, d = plan(columns=[col("C1", 0, 0, 230.0, 450.0)])
        link(b)
        out, rep = export_plan(b, SID)
        base = to_legacy(b, SID)
        out.pop("export")
        self.assertIs(out.pop("strict"), True)                  # the build assumes nothing
        self.assertEqual(out, base)
        self.assertEqual(rep.changed, [])

    def test_reviewed_sizes_go_in_and_a_conflict_is_not_built(self):
        """C2's depth: 600 against 750. Its drawn 450 never stands in for a decision:
        C2 is held back, listed as needing one. (Drawn at the schedule's size: a drawn
        size that disagrees is a conflict too, since 2026-10-10.)"""
        b, _ = plan(columns=[col("C1", 0, 0, 300.0, 600.0), col("C2", 5000, 0, 300.0, 600.0)])
        spec(b, "C1", 300.0, 600.0)
        spec(b, "C2", 300.0, 600.0)
        spec(b, "C2", 300.0, 750.0, {"from": "detail"}, n=1)       # two strong sources disagree
        link(b)
        out, rep = export_plan(b, SID)
        self.assertEqual([(c["mark"], c["width"], c["depth"]) for c in out["columns"]], [("C1", 300.0, 600.0)])
        self.assertTrue(any("C2" in h and "depth conflict" in h for h in rep.held), rep.held)
        self.assertEqual(out["export"]["needs_decision"], 1)
        # decided in the review: built at the decided size
        link(b, {"marks": {"C2": {"depth": 750.0}}})
        out, rep = export_plan(b, SID)
        self.assertEqual([(c["mark"], c["depth"]) for c in out["columns"]], [("C1", 600.0), ("C2", 750.0)])
        self.assertEqual(rep.held, [])

    def test_a_column_the_schedule_starts_here_is_written_as_rising_from_this_level(self):
        """C9 'FF TO SF' on the 1st floor plan: written as a floating column, which the Revit
        build, the IFC and the 3D view all raise from this level to the next."""
        b, _ = plan(columns=[col("C1", 0, 0, 300.0, 600.0), col("C9", 5000, 0, 300.0, 600.0)])
        spec(b, "C1", 300.0, 600.0, {"table": "column schedule", "level_range": ["GF", "L1"]})
        spec(b, "C9", 300.0, 600.0, {"table": "column schedule", "level_range": ["L1", "L2"]})
        link(b)
        out, rep = export_plan(b, SID)
        self.assertEqual([(c["mark"], c["kind"]) for c in out["columns"]], [("C1", "column"), ("C9", "column_float")])
        self.assertTrue(any("C9" in c and "kind column -> column_float" in c for c in rep.changed), rep.changed)

    def test_a_size_of_zero_or_below_is_never_built(self):
        """A column decided -230 wide was exported at -230: held as needing a decision, and
        built at a working size (marked) only when unsettled elements are built."""
        b, _ = plan(columns=[col("C1", 0, 0, 300.0, 600.0), col("C2", 5000, 0, 300.0, 600.0)])
        link(b, {"marks": {"C1": {"width": -230}, "C2": {"depth": 0}}})
        out, rep = export_plan(b, SID)
        self.assertEqual(out["columns"], [])
        self.assertTrue(any(h.startswith("column C1") and "width -230" in h and "not a size" in h
                            for h in rep.held), rep.held)
        self.assertTrue(any(h.startswith("column C2") and "depth 0" in h for h in rep.held), rep.held)
        out, rep = export_plan(b, SID, provisional=True)
        for c in out["columns"]:
            self.assertGreater(min(c["width"], c["depth"]), 0)
            self.assertIn("is not a size", c["provisional"])
        link(b, {"marks": {"C1": {"width": 230}}})              # a real size: built at it
        out, rep = export_plan(b, SID)
        self.assertEqual([(c["mark"], c["width"]) for c in out["columns"]], [("C1", 230.0), ("C2", 300.0)])

    def test_a_beam_with_no_depth_is_not_built_at_a_default(self):
        """No schedule, caption or decision gives B7 a depth: held, never 450."""
        beam = {"mark": "B7", "kind": "beam", "start": [0.0, 0.0], "end": [5000.0, 0.0], "width": 230.0,
                "width_source": "measured", "depth": None, "depth_source": "", "top_mm": 0.0,
                "source_layer": "BEAM", "handle": "B7"}
        b, _ = plan(beams=[beam])
        link(b)
        out, rep = export_plan(b, SID)
        self.assertEqual(out["beams"], [])
        self.assertTrue(any(h.startswith("beam B7") and "depth" in h for h in rep.held))
        link(b, {"marks": {"B7": {"depth": 600}}})
        out, _ = export_plan(b, SID)
        self.assertEqual(out["beams"][0]["depth"], 600.0)

    def test_a_section_drawn_with_lines_sizes_only_the_beams_nothing_else_sizes(self):
        """NIFT SECTION A-A (300 x 625, only the slab dimensioned) cut across three beams:
        the one with no depth is built at 625, open until decided; 2B5 keeps its detail's
        600; a 150 wide line is not that beam. Never a shape: they stay Revit beams."""
        from building import Sheet

        def bm(mark, x, w, depth):
            return {"mark": mark, "kind": "beam", "start": [x, -3000.0], "end": [x, 3000.0], "width": w,
                    "width_source": "measured", "depth": depth, "depth_source": "", "top_mm": 0.0,
                    "source_layer": "BEAM", "handle": f"h{x:.0f}"}
        b, _ = plan(beams=[bm("", 0.0, 300.0, None), bm("2B5", 2000.0, 300.0, 600.0), bm("", 4000.0, 150.0, None)])
        b.sheets[SID].extra["view_sheet"] = "P:V"
        b.add_sheet(Sheet(id="P:V", file="p.dwg", kind="framing_plan", reader="sheets.registry", extra={"set": "P"}))
        b.add_sheet(Sheet(id="P:D", file="p.dwg", kind="section", reader="sheets.registry", extra={"set": "P"}))
        ev = [Evidence("beam_sections", "P:D")]
        b.add(Element(id="P:D/beam_section/L9", type="beam_section", sheet="P:D", mark="A",
                      props={"width": Value(300.0, "read", 1.0, ev), "depth": Value(625.0, "read", 1.0, ev)},
                      extra={"profile": [[0, -625], [0, 0], [300, 0], [300, -625]], "title": "SECTION A-A",
                             "undimensioned": ["across", "up"], "hung": [], "drawn_as": "lines", "scale": 0.25,
                             "full_width": 300.0, "rectangle": True}))
        b.add(Element(id="P:V/section_cut/0", type="section_cut", sheet="P:V", mark="A",
                      geometry={"a": [-500.0, 0.0], "b": [4500.0, 0.0]}, props={"name": Value("A", "read", 0.9, ev)}))
        link(b)
        got = {(e.final("mark"), e.get("width")): e.resolved["depth"] for e in b.of("beam")}
        self.assertEqual((got[(None, 300.0)].value, got[(None, 300.0)].status), (625.0, "missing"))
        self.assertEqual((got[("2B5", 300.0)].value, got[("2B5", 300.0)].status), (600.0, "read"))
        self.assertIsNone(got[(None, 150.0)].value)
        self.assertTrue(all("section" not in e.resolved and not e.extra.get("section_profile") for e in b.of("beam")))
        out, rep = export_plan(b, SID)                       # strict: the measured depth is not settled
        self.assertEqual(sorted(r["mark"] for r in out["beams"]), ["2B5"])
        out, rep = export_plan(b, SID, provisional=True)
        row = next(r for r in out["beams"] if r["width"] == 300.0 and not r["mark"])
        self.assertEqual(row["depth"], 625.0)
        self.assertIn("section A-A", row["provisional"])
        self.assertNotIn("section", row)
        # decided in the review: that stands
        link(b, {"elements": {next(e.id for e in b.of("beam") if not e.mark and e.get("width") == 300.0): {"depth": 600}}})
        self.assertEqual(next(e.final("depth") for e in b.of("beam") if not e.mark and e.get("width") == 300.0), 600)

    def test_the_slab_a_section_draws_beside_its_beam_is_built_as_slab_not_beams(self):
        """NIFT 5th floor SECTION A-A: 5B2 300 x 600, the 150 slab running 150 past its outer
        face and 300 past its inner one. On the plan: a line pair 150 wide outside (read as a
        150 'beam') and one line 450 inside (the slab's edge, read as a beam with no width) -
        built as two slab strips, not as beams; 5B2 itself as it was."""
        from building import Sheet

        def bm(mark, y, w, depth, note=""):
            return {"mark": mark, "kind": "beam", "start": [0.0, y], "end": [10000.0, y], "width": w,
                    "width_source": "measured", "depth": depth, "depth_source": "", "top_mm": 0.0,
                    "source_layer": "BEAM", "handle": f"h{y:.0f}", "notes": [note] if note else []}
        b, _ = plan(beams=[bm("5B2", 0.0, 300.0, 600.0), bm("", -225.0, 150.0, None),
                           bm("", 450.0, None, None, "drawn as a plain line: centreline assumed, width from schedule")])
        b.sheets[SID].extra["view_sheet"] = "P:V"
        b.add_sheet(Sheet(id="P:V", file="p.dwg", kind="framing_plan", reader="sheets.registry", extra={"set": "P"}))
        b.add_sheet(Sheet(id="P:D", file="p.dwg", kind="section", reader="sheets.registry", extra={"set": "P"}))
        ev = [Evidence("beam_sections", "P:D")]
        b.add(Element(id="P:D/beam_section/L9", type="beam_section", sheet="P:D", mark="A",
                      props={"width": Value(300.0, "read", 1.0, ev), "depth": Value(600.0, "read", 1.0, ev)},
                      extra={"profile": [[0, -600], [0, 0], [300, 0], [300, -600]], "title": "SECTION A-A",
                             "undimensioned": ["across", "up"], "hung": [], "drawn_as": "lines", "scale": 0.25,
                             "full_width": 300.0, "rectangle": True, "slab_thickness": {"mm": 150.0, "written": True},
                             "flanges": [{"side": "left", "width": 150.0, "closed": True, "written": True},
                                         {"side": "right", "width": 300.0, "closed": True, "written": True}]}))
        b.add(Element(id="P:V/section_cut/0", type="section_cut", sheet="P:V", mark="A",
                      geometry={"a": [5000.0, -600.0], "b": [5000.0, 800.0]}, props={"name": Value("A", "read", 0.9, ev)}))
        link(b)
        out, rep = export_plan(b, SID, provisional=True)
        self.assertEqual([(r["mark"], r["width"], r["depth"]) for r in out["beams"]], [("5B2", 300.0, 600.0)])
        strips = sorted((min(p[1] for p in r["outline"]), max(p[1] for p in r["outline"]), r["thickness"])
                        for r in out["slabs"])
        self.assertEqual([tuple(round(v) for v in s) for s in strips], [(-300, -150, 150), (150, 450, 150)])
        self.assertTrue(all("provisional" not in r for r in out["slabs"]))

    def test_pairs_of_lines_along_a_beam_are_read_as_the_section_draws_them(self):
        """NIFT 5th floor top edge: lines at -450, -150, +150, +300 from 5B2's centre (SECTION A-A:
        300 beam, slab 300 in and 150 out). The plan reader's pairs: the beam (A, 5B2); a span with
        no mark between two 5B2 (B); 450 wide captioned 5B2 = the beam + its outer strip (C); the
        beam again inside C's stretch (D); 600 wide = the beam + its inner strip (E)."""
        from building import Sheet

        def bm(mark, x0, x1, y, w, depth):
            return {"mark": mark, "kind": "beam", "start": [x0, y], "end": [x1, y], "width": w,
                    "width_source": "measured", "depth": depth, "depth_source": "", "top_mm": 0.0,
                    "source_layer": "BEAM", "handle": f"h{x0:.0f}{w:.0f}", "notes": []}
        b, _ = plan(beams=[bm("5B2", 0.0, 5000.0, 0.0, 300.0, 600.0), bm("", 5000.0, 10000.0, 0.0, 300.0, None),
                           bm("5B2", 10000.0, 20000.0, 75.0, 450.0, None), bm("", 12000.0, 16000.0, 0.0, 300.0, None),
                           bm("", 10000.0, 20000.0, -150.0, 600.0, None)])
        b.sheets[SID].extra["view_sheet"] = "P:V"
        b.add_sheet(Sheet(id="P:V", file="p.dwg", kind="framing_plan", reader="sheets.registry", extra={"set": "P"}))
        b.add_sheet(Sheet(id="P:D", file="p.dwg", kind="section", reader="sheets.registry", extra={"set": "P"}))
        ev = [Evidence("beam_sections", "P:D")]
        b.add(Element(id="S/spec/5B2", type="spec", sheet="P:D", mark="5B2",
                      props={"family": Value("beam", "read", 1.0, ev), "width": Value(300.0, "read", 0.95, ev),
                             "depth": Value(600.0, "read", 0.95, ev)}, extra={"raw": "5B2"}))
        b.add(Element(id="P:D/beam_section/L9", type="beam_section", sheet="P:D", mark="A",
                      props={"width": Value(300.0, "read", 1.0, ev), "depth": Value(600.0, "read", 1.0, ev)},
                      extra={"profile": [[0, -600], [0, 0], [300, 0], [300, -600]], "title": "SECTION A-A",
                             "undimensioned": ["across", "up"], "hung": [], "drawn_as": "lines", "scale": 0.25,
                             "full_width": 300.0, "rectangle": True, "slab_thickness": {"mm": 150.0, "written": True},
                             "flanges": [{"side": "left", "width": 150.0, "closed": True, "written": True},
                                         {"side": "right", "width": 300.0, "closed": True, "written": True}]}))
        b.add(Element(id="P:V/section_cut/0", type="section_cut", sheet="P:V", mark="A",
                      geometry={"a": [2000.0, -800.0], "b": [2000.0, 800.0]}, props={"name": Value("A", "read", 0.9, ev)}))
        link(b)
        out, rep = export_plan(b, SID)
        beams = sorted((r["start"][0], round(r["start"][1]), r["mark"], r["width"], r["depth"]) for r in out["beams"])
        self.assertEqual(beams, [(0.0, 0, "5B2", 300.0, 600.0), (5000.0, 0, "5B2", 300.0, 600.0),
                                 (10000.0, 0, "5B2", 300.0, 600.0)])
        strips = sorted((round(min(p[1] for p in r["outline"])), round(max(p[1] for p in r["outline"])), r["thickness"])
                        for r in out["slabs"])
        self.assertEqual(strips, [(-450, -150, 150.0), (150, 300, 150.0)])
        self.assertTrue(any("drawn twice" in x for x in rep.left_out), rep.left_out)

    def test_the_beam_is_built_where_only_its_slab_strips_were_paired(self):
        """NIFT 5th floor Y12-Y13: all four lines drawn, but the reader paired each beam face with
        the slab edge beside it - two strips, no beam - between two 5B2 spans. The strips' inner
        edges are the beam's faces: 5B2 is built there. A break the width of a column is not."""
        from building import Sheet

        def bm(mark, x0, x1, y, w, depth):
            return {"mark": mark, "kind": "beam", "start": [x0, y], "end": [x1, y], "width": w,
                    "width_source": "measured", "depth": depth, "depth_source": "", "top_mm": 0.0,
                    "source_layer": "BEAM", "handle": f"h{x0:.0f}{y:.0f}", "notes": []}
        b, _ = plan(beams=[bm("5B2", 0.0, 5000.0, 0.0, 300.0, 600.0), bm("5B2", 10000.0, 15000.0, 0.0, 300.0, 600.0),
                           bm("5B2", 15300.0, 20000.0, 0.0, 300.0, 600.0),
                           bm("", 0.0, 15000.0, 225.0, 150.0, None), bm("", 0.0, 15000.0, -300.0, 300.0, None)])
        b.sheets[SID].extra["view_sheet"] = "P:V"
        b.add_sheet(Sheet(id="P:V", file="p.dwg", kind="framing_plan", reader="sheets.registry", extra={"set": "P"}))
        b.add_sheet(Sheet(id="P:D", file="p.dwg", kind="section", reader="sheets.registry", extra={"set": "P"}))
        ev = [Evidence("beam_sections", "P:D")]
        b.add(Element(id="P:D/beam_section/L9", type="beam_section", sheet="P:D", mark="A",
                      props={"width": Value(300.0, "read", 1.0, ev), "depth": Value(600.0, "read", 1.0, ev)},
                      extra={"profile": [[0, -600], [0, 0], [300, 0], [300, -600]], "title": "SECTION A-A",
                             "undimensioned": ["across", "up"], "hung": [], "drawn_as": "lines", "scale": 0.25,
                             "full_width": 300.0, "rectangle": True, "slab_thickness": {"mm": 150.0, "written": True},
                             "flanges": [{"side": "left", "width": 150.0, "closed": True, "written": True},
                                         {"side": "right", "width": 300.0, "closed": True, "written": True}]}))
        b.add(Element(id="P:V/section_cut/0", type="section_cut", sheet="P:V", mark="A",
                      geometry={"a": [2000.0, -800.0], "b": [2000.0, 800.0]}, props={"name": Value("A", "read", 0.9, ev)}))
        link(b)
        out, rep = export_plan(b, SID)
        beams = sorted((round(r["start"][0]), round(r["end"][0]), r["mark"], r["width"], r["depth"]) for r in out["beams"])
        self.assertEqual(beams, [(0, 5000, "5B2", 300.0, 600.0), (5000, 10000, "5B2", 300.0, 600.0),
                                 (10000, 15000, "5B2", 300.0, 600.0), (15300, 20000, "5B2", 300.0, 600.0)])
        self.assertEqual(len(out["slabs"]), 2)

    def test_an_end_beam_drawn_only_as_its_slab_strips_with_its_mark_on_the_strip(self):
        """NIFT 5th floor Y23, X1-X2: '5B4A' written on the outer strip (150), the inner strip drawn
        300 wide, no pair for the beam; the cut crosses only the strips. 5B4A stands on from X2 to
        X3. The beam is built X1-X2 on its line (both strips line it), the strips as slab; a 300
        mm single line wholly along the column at X1 is part of the column, not a beam."""
        from building import Sheet

        def bm(mark, x0, x1, y, w, depth, note=""):
            return {"mark": mark, "kind": "beam", "start": [x0, y], "end": [x1, y], "width": w,
                    "width_source": "measured", "depth": depth, "depth_source": "", "top_mm": 0.0,
                    "source_layer": "BEAM", "handle": f"h{x0:.0f}{y:.0f}", "notes": [note] if note else []}
        column = dict(col("", -150.0, 0.0, 300.0, 900.0), outline=[[-300, -450], [0, -450], [0, 450], [-300, 450]])
        b, _ = plan(columns=[column],
                    beams=[bm("5B4A", 5000.0, 10000.0, 0.0, 300.0, 600.0), bm("5B4A", 0.0, 6000.0, 225.0, 150.0, 600.0),
                           bm("", 0.0, 6000.0, -300.0, 300.0, None),
                           bm("", -100.0, -100.0, 0.0, None, None)])
        b.elements[-1].geometry.update(start=[100.0, 550.0], end=[100.0, 250.0])
        b.elements[-1].notes.append("drawn as a plain line: centreline assumed, width from schedule")
        b.sheets[SID].extra["view_sheet"] = "P:V"
        b.add_sheet(Sheet(id="P:V", file="p.dwg", kind="framing_plan", reader="sheets.registry", extra={"set": "P"}))
        b.add_sheet(Sheet(id="P:D", file="p.dwg", kind="section", reader="sheets.registry", extra={"set": "P"}))
        ev = [Evidence("beam_sections", "P:D")]
        b.add(Element(id="P:D/beam_section/L9", type="beam_section", sheet="P:D", mark="A",
                      props={"width": Value(300.0, "read", 1.0, ev), "depth": Value(600.0, "read", 1.0, ev)},
                      extra={"profile": [[0, -600], [0, 0], [300, 0], [300, -600]], "title": "SECTION A-A",
                             "undimensioned": ["across", "up"], "hung": [], "drawn_as": "lines", "scale": 0.25,
                             "full_width": 300.0, "rectangle": True, "slab_thickness": {"mm": 150.0, "written": True},
                             "flanges": [{"side": "left", "width": 150.0, "closed": True, "written": True},
                                         {"side": "right", "width": 300.0, "closed": True, "written": True}]}))
        b.add(Element(id="P:V/section_cut/0", type="section_cut", sheet="P:V", mark="A",
                      geometry={"a": [3000.0, -800.0], "b": [3000.0, 800.0]}, props={"name": Value("A", "read", 0.9, ev)}))
        link(b)
        out, rep = export_plan(b, SID)
        beams = sorted((round(r["start"][0]), round(r["end"][0]), round(r["start"][1]), r["mark"], r["width"])
                       for r in out["beams"])
        self.assertEqual(beams, [(0, 5000, 0, "5B4A", 300.0), (5000, 10000, 0, "5B4A", 300.0)])
        strips = sorted((round(min(p[1] for p in r["outline"])), round(max(p[1] for p in r["outline"]))) for r in out["slabs"])
        self.assertEqual(strips, [(-450, -150), (150, 300)])
        self.assertTrue(any("part of how the column is drawn" in x for x in rep.left_out), rep.left_out)

    def test_a_beam_runs_as_far_as_both_its_faces_are_drawn(self):
        """NIFT 4th floor, 4B2 by the stair: both faces drawn 0-4850, the reader's pair stopped at
        2811. Carried on to 4850 at its own size; a beam whose faces stop where it stops, or one
        with only one face drawn further, is left as it is."""
        from building import Sheet

        def bm(mark, x0, x1, y, w, depth):
            return {"mark": mark, "kind": "beam", "start": [x0, y], "end": [x1, y], "width": w,
                    "width_source": "measured", "depth": depth, "depth_source": "", "top_mm": 0.0,
                    "source_layer": "BEAM", "handle": f"h{x0:.0f}{y:.0f}", "notes": []}
        b, _ = plan(beams=[bm("4B2", 150.0, 2811.0, 0.0, 300.0, 600.0), bm("4B3", 150.0, 2811.0, 5000.0, 300.0, 600.0)])
        b.sheets[SID].extra["view_sheet"] = "P:V"
        lines = [[0, 150.0, 150.0, 4850.0, 150.0, 1], [0, 150.0, -150.0, 4850.0, -150.0, 1],      # 4B2: both faces on
                 [0, 150.0, 5150.0, 4850.0, 5150.0, 0], [0, 150.0, 4850.0, 2811.0, 4850.0, 0]]  # 4B3: one face only
        b.add_sheet(Sheet(id="P:V", file="p.dwg", kind="framing_plan", reader="sheets.registry",
                          extra={"set": "P", "beam_lines": {"layers": ["BEAM"], "lines": lines}}))
        link(b)
        out, rep = export_plan(b, SID)
        beams = sorted((r["mark"], round(r["start"][0]), round(r["end"][0]), r["width"], r["depth"]) for r in out["beams"])
        self.assertEqual(beams, [("4B2", 150, 2811, 300.0, 600.0), ("4B2", 2811, 4850, 300.0, 600.0),
                                 ("4B3", 150, 2811, 300.0, 600.0)])

    def test_a_beam_detail_running_over_grids_marks_the_unmarked_spans_between(self):
        """NIFT: 4B2 written once on the plan; its detail runs from grid Y1 to Y3, over a
        column. The other span between Y1 and Y3 is 4B2 (size from its detail); a span past
        Y3, or one not as wide, is not. Y3 is shown only by its label: it runs through it,
        as Y1 and Y2 (drawn) run."""
        from building import Sheet

        def bm(mark, x0, x1, w=300.0):
            return {"mark": mark, "kind": "beam", "start": [x0, 0.0], "end": [x1, 0.0], "width": w,
                    "width_source": "measured", "depth": None, "depth_source": "", "top_mm": 0.0,
                    "source_layer": "BEAM", "handle": f"h{x0:.0f}{w:.0f}"}
        cols = [dict(col(f"C{n}", x, 0.0, 300.0, 300.0), outline=[[x - 150, -150], [x + 150, -150], [x + 150, 150],
                                                                    [x - 150, 150]])
                for n, x in enumerate((0.0, 5000.0, 10000.0, 15000.0))]
        b, _ = plan(columns=cols,
                    beams=[bm("4B2", 0.0, 5000.0), bm("", 5000.0, 10000.0), bm("", 10000.0, 15000.0),
                           bm("", 5000.0, 10000.0, 150.0)])
        b.sheets[SID].grids = [{"label": "", "start": [x, -3000.0], "end": [x, 3000.0], "handle": f"g{x:.0f}"}
                               for x in (0.0, 5000.0)]
        b.sheets[SID].extra["view_sheet"] = "P:V"
        b.add_sheet(Sheet(id="P:V", file="p.dwg", kind="framing_plan", reader="sheets.registry", extra={
            "set": "P", "grid_labels": [["(A)Y1", 0.0, 3600.0, 250.0], ["(A)Y2", 5000.0, 3600.0, 250.0],
                                        ["(A)Y3", 10000.0, 3600.0, 250.0]]}))
        b.add_sheet(Sheet(id="P:D", file="p.dwg", kind="beam_details", reader="sheets.registry", extra={"set": "P"}))
        ev = [Evidence("labels", "P:D")]
        b.add(Element(id="P:D/spec/0", type="spec", sheet="P:D", mark="4B2",
                      props={"family": Value("beam", "read", 1.0, ev), "width": Value(300.0, "read", 0.95, ev),
                             "depth": Value(600.0, "read", 0.95, ev)},
                      extra={"raw": "4B2", "grid_span": ["(A)Y1", "(A)Y2", "(A)Y3"]}))
        link(b)
        got = {(e.geometry["start"][0], e.get("width")): (e.final("mark"), e.final("depth")) for e in b.of("beam")}
        self.assertEqual(got[(5000.0, 300.0)], ("4B2", 600.0))
        self.assertEqual(got[(10000.0, 300.0)][0], None)            # past Y3: not that beam
        self.assertEqual(got[(5000.0, 150.0)][0], None)             # not as wide
        b.of("beam")[1].mark = "B9"                                  # a mark of its own wins
        link(b)
        self.assertEqual(b.of("beam")[1].final("mark"), "B9")

    def test_built_anyway_unsettled_goes_out_at_a_working_size_with_its_reason(self):
        """The app's default: B7 (no depth anywhere) is built at the depth most beams as
        wide settled on, C2 (600 against 750) at the strongest source's - each marked."""
        def bm(mark, y, depth):
            return {"mark": mark, "kind": "beam", "start": [0.0, y], "end": [5000.0, y], "width": 230.0,
                    "width_source": "measured", "depth": depth, "depth_source": "", "top_mm": 0.0,
                    "source_layer": "BEAM", "handle": mark}
        b, _ = plan(columns=[col("C1", 0, 0, 300.0, 600.0), col("C2", 5000, 0, 300.0, 600.0)],
                    beams=[bm("B7", 0.0, None), bm("B8", 3000.0, None), bm("B9", 6000.0, None),
                           bm("B10", 9000.0, None)])
        spec(b, "C1", 300.0, 600.0)
        spec(b, "C2", 300.0, 600.0)
        spec(b, "C2", 300.0, 750.0, {"from": "detail"}, n=1)
        for m, dp in (("B8", 600.0), ("B9", 600.0), ("B10", 450.0)):
            b.add(Element(id=f"S/spec/{m}", type="spec", sheet="S", mark=m,
                          props={"family": Value("beam", "read", 1.0, [Evidence("tables", "S")]),
                                 "width": Value(230.0, "read", 1.0, [Evidence("tables", "S")]),
                                 "depth": Value(dp, "read", 1.0, [Evidence("tables", "S")])},
                          extra={"table": "BEAM SCHEDULE"}))
        link(b)
        out, rep = export_plan(b, SID, provisional=True)
        b7 = next(x for x in out["beams"] if x["mark"] == "B7")
        c2 = next(x for x in out["columns"] if x["mark"] == "C2")
        self.assertEqual((b7["depth"], c2["depth"]), (600.0, 600.0))
        self.assertIn("depth not given - built at 600", b7["provisional"])
        self.assertIn("strongest source", c2["provisional"])
        self.assertNotIn("provisional", next(x for x in out["beams"] if x["mark"] == "B8"))
        self.assertEqual((rep.held, len(rep.provisional), out["export"]["provisional"]), ([], 2, 2))
        self.assertIs(out["strict"], True)              # the build itself still assumes nothing more

    def test_a_bay_with_no_mark_is_built_as_before_but_marked(self):
        """Never invent (external review, point C): the plan reader gives an unmarked bay
        the nearest mark and its thickness. That is a working value, not a reading:
        built the same, marked until a person confirms it."""
        def slab(mark, x, notes):
            return {"mark": mark, "outline": [[x, 0.0], [x + 4000.0, 0.0], [x + 4000.0, 3000.0], [x, 3000.0]],
                    "holes": [], "thickness": 150.0, "thickness_source": "slab schedule", "sunk_mm": 0.0,
                    "source_layer": "", "handle": "", "notes": notes}
        b, _ = plan(slabs=[slab("S1", 0.0, []),
                           slab("S1", 5000.0, ["no slab mark in this bay: mark and thickness of the nearest, S1"])])
        link(b)
        own, nearest = b.of("slab")
        self.assertEqual((own.resolved["mark"].status, own.resolved["thickness"].status), ("read", "read"))
        self.assertEqual((nearest.final("mark"), nearest.resolved["mark"].status), ("S1", "missing"))
        self.assertEqual((nearest.final("thickness"), nearest.resolved["thickness"].status), (150.0, "missing"))
        out, rep = export_plan(b, SID, provisional=True)
        self.assertEqual([(s["mark"], s["thickness"]) for s in out["slabs"]], [("S1", 150.0), ("S1", 150.0)])
        self.assertNotIn("provisional", out["slabs"][0])
        self.assertIn("no slab mark", out["slabs"][1]["provisional"])

    def test_another_blocks_area_is_built_marked_until_decided(self):
        b, _ = plan(columns=[col("C1", 0, 0), col("C2", 9000, 0), col("C3", 12000, 0),
                             col("C4", 15000, 0), col("C5", 18000, 0)])
        b.add(Element(id="P:V/scope_zone/0", type="scope_zone", sheet="P:V",
                      geometry={"outline": [[-1000, -1000], [1000, -1000], [1000, 1000], [-1000, 1000]]},
                      props={"kind": Value("other block", "read", 0.8, [Evidence("scope", "P:V")])},
                      extra={"layer": "Main Block"}))
        b.sheets[SID].extra["view_sheet"] = "P:V"
        from building import Sheet
        b.add_sheet(Sheet(id="P:V", file="p.dwg", kind="framing_plan", reader="sheets.registry"))
        link(b)
        out, rep = export_plan(b, SID, provisional=True)
        c1 = next(c for c in out["columns"] if c["mark"] == "C1")
        self.assertIn("Main Block", c1["provisional"])
        self.assertNotIn("provisional", next(c for c in out["columns"] if c["mark"] == "C2"))
        out, _ = export_plan(b, SID)                           # nothing unsettled built: held
        self.assertNotIn("C1", [c["mark"] for c in out["columns"]])
        link(b, {"elements": {f"{SID}/column/0": {"scope": "other block"}}})
        out, rep = export_plan(b, SID, provisional=True)
        self.assertNotIn("C1", [c["mark"] for c in out["columns"]])
        self.assertTrue(any("another block" in x for x in rep.left_out))
        link(b, {"elements": {f"{SID}/column/0": {"scope": "this block"}}})
        out, _ = export_plan(b, SID, provisional=True)
        self.assertNotIn("provisional", next(c for c in out["columns"] if c["mark"] == "C1"))

    def test_a_square_standing_clear_of_the_frame_is_not_built_as_a_column(self):
        """ALCAZAR plinth: the legend's 'STUB COLUMN 300X300' sample square and the stub
        column detail beside the plan came out as columns no beam or slab reaches."""
        cols = [col(f"C{n}", n * 4000.0, 0) for n in range(10)] + [col("", 30000.0, 20000.0, 460, 460)]
        beams = [{"mark": "PB1", "kind": "beam", "start": [n * 4000.0, 0.0], "end": [n * 4000.0 + 4000.0, 0.0],
                  "width": 230.0, "width_source": "", "depth": 450.0, "depth_source": "", "top_mm": 0.0,
                  "source_layer": "BEAM", "handle": f"pb{n}"} for n in range(9)]
        b, _ = plan(columns=cols, beams=beams)
        link(b)
        out, rep = export_plan(b, SID, provisional=True)
        self.assertEqual(len(out["columns"]), 10)
        self.assertTrue(any("clear of every beam" in x for x in rep.left_out))
        self.assertTrue(any(i.category == "scope" and i.elements == [f"{SID}/column/10"] for i in b.issues))
        link(b, {"elements": {f"{SID}/column/10": {"scope": "this block"}}})
        out, _ = export_plan(b, SID, provisional=True)
        self.assertEqual(len(out["columns"]), 11)
        b, _ = plan(columns=cols)                              # columns only (a column layout): all kept
        link(b)
        out, _ = export_plan(b, SID, provisional=True)
        self.assertEqual(len(out["columns"]), 11)

    def test_what_lies_wholly_beyond_the_plans_beams_is_not_built(self):
        """ALCAZAR 1st floor: the architect's background carries on 35 m past the structural
        plan; its brick walls came out as two columns and a slab out there. Left out, said,
        and 'this block' builds them. Something the beams' box reaches is kept."""
        cols = [col(f"C{n}", n * 4000.0, 0) for n in range(10)] + [col("", -35000.0, 2000.0, 600, 600)]
        beams = [{"mark": "PB1", "kind": "beam", "start": [n * 4000.0, 0.0], "end": [n * 4000.0 + 4000.0, 0.0],
                  "width": 230.0, "width_source": "", "depth": 450.0, "depth_source": "", "top_mm": 0.0,
                  "source_layer": "BEAM", "handle": f"pb{n}"} for n in range(9)]

        def slab(x, y, notes=()):
            return {"mark": "S1", "outline": [[x, y], [x + 3000.0, y], [x + 3000.0, y + 2500.0], [x, y + 2500.0]],
                    "holes": [], "thickness": 150.0, "thickness_source": "slab schedule", "sunk_mm": 0.0,
                    "source_layer": "", "handle": "", "notes": list(notes)}
        slabs = [slab(n * 4000.0 + 500.0, 300.0) for n in range(9)] + [slab(-36000.0, 2500.0)]
        b, _ = plan(columns=cols, beams=beams, slabs=slabs)
        link(b)
        out, rep = export_plan(b, SID, provisional=True)
        self.assertEqual((len(out["columns"]), len(out["slabs"])), (10, 9))
        self.assertTrue(any("beyond every beam" in x for x in rep.left_out))
        far = [f"{SID}/column/10", f"{SID}/slab/9"]
        self.assertTrue(any(i.category == "scope" and sorted(i.elements) == sorted(far) for i in b.issues))
        link(b, {"elements": {far[0]: {"scope": "this block"}}})
        out, _ = export_plan(b, SID, provisional=True)
        self.assertEqual(len(out["columns"]), 11)
        b, _ = plan(columns=cols, slabs=slabs)                 # no beams: nothing to judge by, all kept
        link(b)
        out, _ = export_plan(b, SID, provisional=True)
        self.assertEqual((len(out["columns"]), len(out["slabs"])), (11, 10))

    def test_what_the_build_leaves_out_is_not_shown_as_built(self):
        """A sample square / detail beside the plan is left out of Revit: the 3D view leaves it
        out too, and the review shows it as 'left out', not as a column missing its mark."""
        from building import review
        from building.viewer3d import scene
        cols = [col(f"C{n}", n * 4000.0, 0) for n in range(10)] + [col("", 30000.0, 20000.0, 460, 460)]
        beams = [{"mark": "PB1", "kind": "beam", "start": [n * 4000.0, 0.0], "end": [n * 4000.0 + 4000.0, 0.0],
                  "width": 230.0, "width_source": "", "depth": 450.0, "depth_source": "", "top_mm": 0.0,
                  "source_layer": "BEAM", "handle": f"pb{n}"} for n in range(9)]
        b, _ = plan(columns=cols, beams=beams)
        link(b)
        lone = f"{SID}/column/10"
        self.assertNotIn(lone, [i["id"] for i in scene(b, "P")["items"]])
        self.assertEqual(sum(1 for i in scene(b, "P")["items"] if i["t"] == "column"), 10)
        els = {e["id"]: e for s in review.data(b)["sheets"] for e in s["els"]}
        self.assertTrue(els[lone]["out"])
        self.assertFalse(els[f"{SID}/column/0"]["out"])
        link(b, {"elements": {lone: {"scope": "this block"}}})      # decided: built, and shown
        self.assertIn(lone, [i["id"] for i in scene(b, "P")["items"]])
        self.assertFalse(next(e for s in review.data(b)["sheets"] for e in s["els"] if e["id"] == lone)["out"])

    def test_a_beam_marked_with_a_drawn_section_is_built_to_its_shape(self):
        """Main Block terrace: edge beams cut X ... X on the plan, SECTION X-X a 750 wide
        beam full 600 deep on one half, sloping to 100 on the other - built as boxes."""
        from building import Sheet
        def bm(n, y):
            return {"mark": f"B{n}", "kind": "beam", "start": [0.0, y], "end": [6000.0, y], "width": 750.0,
                    "width_source": "", "depth": 600.0, "depth_source": "", "top_mm": 0.0,
                    "source_layer": "BEAM", "handle": f"b{n}"}

        def slab(y0, y1):
            return {"mark": "S1", "outline": [[0, y0], [6000, y0], [6000, y1], [0, y1]], "holes": [],
                    "thickness": 125.0, "thickness_source": "", "sunk_mm": 0.0, "source_layer": "", "handle": "",
                    "notes": []}
        # B1 on the north edge (slab south of it), B2 inside (slab both sides)
        b, _ = plan(beams=[bm(1, 0.0), bm(2, -5000.0)], slabs=[slab(-4700, -400), slab(-9000, -5400)])
        b.sheets[SID].extra["view_sheet"] = "P:V"
        b.add_sheet(Sheet(id="P:V", file="p.dwg", kind="framing_plan", reader="sheets.registry", extra={"set": "P"}))
        b.add_sheet(Sheet(id="P:D", file="p.dwg", kind="beam_details", reader="sheets.registry", extra={"set": "P"}))
        ev = [Evidence("beam_sections", "P:D")]
        b.add(Element(id="P:D/beam_section/0", type="beam_section", sheet="P:D", mark="X",
                      props={"width": Value(750.0, "read", 1.0, ev), "depth": Value(600.0, "read", 1.0, ev)},
                      extra={"profile": [[375, -600], [0, -600], [0, -500], [375, 0], [750, 0], [750, -600]],
                             "title": "SECTION :- X-X", "undimensioned": [], "hung": []}))
        for n, x in enumerate((2000.0, 4000.0)):
            b.add(Element(id=f"P:V/section_cut/{n}", type="section_cut", sheet="P:V", mark="X",
                          geometry={"a": [x, 900.0], "b": [x, -5900.0]},
                          props={"name": Value("X", "read", 0.9, ev)}))
        link(b)
        b1, b2 = [e for e in b.of("beam") if e.mark in ("B1", "B2")]
        self.assertEqual((b1.resolved["section"].status, b2.resolved["section"].status), ("read", "conflict"))
        out, rep = export_plan(b, SID, provisional=True)
        r1 = next(r for r in out["beams"] if r["mark"] == "B1")
        top = [u for u, v in r1["section"]["profile"] if v > -1]
        self.assertTrue(all(u <= 0.5 for u in top), "the full-height half faces the slab, south (right of east)")
        r2 = next(r for r in out["beams"] if r["mark"] == "B2")
        self.assertIn("which way round", r2["provisional"])
        link(b, {"elements": {b1.id: {"section": "X-X mirrored"}}})
        out, _ = export_plan(b, SID, provisional=True)
        top = [u for u, v in next(r for r in out["beams"] if r["mark"] == "B1")["section"]["profile"] if v > -1]
        self.assertTrue(all(u >= -0.5 for u in top))
        link(b, {"elements": {b1.id: {"section": "none"}}})
        out, _ = export_plan(b, SID, provisional=True)
        self.assertNotIn("section", next(r for r in out["beams"] if r["mark"] == "B1"))

    def test_a_section_drawn_as_two_lines_is_one_beam_of_that_section(self):
        """Main Block north edge: face lines 230 and 520 apart (= the 750 section), the
        middle one where the slope meets the flat top - read as two beams. They are one
        750 beam (merge_beams) carrying the whole section, its full height to the slab."""
        from building import Sheet
        def bm(n, y, w):
            return {"mark": "", "kind": "beam", "start": [0.0, y], "end": [6000.0, y], "width": w,
                    "width_source": "", "depth": 600.0, "depth_source": "", "top_mm": 0.0,
                    "source_layer": "BEAM", "handle": f"b{n}"}
        slab = {"mark": "S1", "outline": [[0, -900], [6000, -900], [6000, -4000], [0, -4000]], "holes": [],
                "thickness": 125.0, "thickness_source": "", "sunk_mm": 0.0, "source_layer": "", "handle": "", "notes": []}
        b, _ = plan(beams=[bm(1, 0.0, 230.0), bm(2, -375.0, 520.0)], slabs=[slab])
        b.sheets[SID].extra["view_sheet"] = "P:V"
        b.add_sheet(Sheet(id="P:V", file="p.dwg", kind="framing_plan", reader="sheets.registry", extra={"set": "P"}))
        b.add_sheet(Sheet(id="P:D", file="p.dwg", kind="beam_details", reader="sheets.registry", extra={"set": "P"}))
        ev = [Evidence("beam_sections", "P:D")]
        b.add(Element(id="P:D/beam_section/0", type="beam_section", sheet="P:D", mark="X",
                      props={"width": Value(750.0, "read", 1.0, ev), "depth": Value(600.0, "read", 1.0, ev)},
                      extra={"profile": [[375, -600], [0, -600], [0, -500], [375, 0], [750, 0], [750, -600]],
                             "title": "SECTION :- X-X", "undimensioned": [], "hung": []}))
        b.add(Element(id="P:V/section_cut/0", type="section_cut", sheet="P:V", mark="X",
                      geometry={"a": [3000.0, 900.0], "b": [3000.0, -1500.0]}, props={"name": Value("X", "read", 0.9, ev)}))
        link(b)
        joined = [e for e in b.of("beam") if e.extra.get("merged_from")]
        self.assertEqual(len(joined), 1)
        e = joined[0]
        self.assertEqual((e.final("width"), round(e.geometry["start"][1])), (750.0, -260))
        prof = e.extra["section_profile"]
        self.assertEqual(len(prof), 6)                                         # the whole section
        self.assertTrue(all(u <= 0.5 for u, v in prof if v > -1), "full height to the slab (south)")
        out, _ = export_plan(b, SID, provisional=True)
        self.assertEqual([r.get("section", {}).get("name") for r in out["beams"]], ["X-X"])

    def test_one_beam_drawn_with_three_lines_is_one_beam(self):
        """Main Block terrace edge (user 2026-10-10: 'multiple beams to create one beam'):
        faces 750 apart plus the slope line between - read as a 230 and a 520 beam, each
        cut at different places. One 750 beam per column span, the pieces not built."""
        def bm(n, y, x0, x1, w):
            return {"mark": "", "kind": "beam", "start": [x0, y], "end": [x1, y], "width": w,
                    "width_source": "", "depth": 600.0, "depth_source": "", "top_mm": 0.0,
                    "source_layer": "BEAM-230X600", "handle": f"b{n}"}
        outer = [bm(0, 0.0, 0.0, 4000.0, 230.0), bm(1, 0.0, 4450.0, 9000.0, 230.0)]          # a gap at the column
        inner = [bm(2, -375.0, 0.0, 3000.0, 520.0), bm(3, -375.0, 3000.0, 9000.0, 520.0)]    # cut elsewhere
        apart = [bm(4, 5000.0, 0.0, 9000.0, 230.0), bm(5, 5400.0, 0.0, 9000.0, 230.0)]       # 170 between faces
        brief = [bm(6, 9000.0, 0.0, 9000.0, 230.0), bm(7, 9230.0, 8000.0, 9000.0, 230.0)]    # touch for 1 m only
        cols = [col("C1", 0.0, -250.0, 300.0, 750.0), col("C2", 4225.0, -250.0, 300.0, 750.0),
                col("C3", 9000.0, -250.0, 300.0, 750.0)]
        # a column drawn as an odd shape (read as a wall) across the beam at x 6525 - Main Block's edge columns
        odd = {"mark": "", "thickness": None, "outline": [[6300, -700], [6750, -700], [6750, 150], [6300, 150]],
               "source_layer": "STR.COLUMN", "handle": "w1"}
        b, _ = plan(columns=cols, beams=outer + inner + apart + brief, walls=[odd])
        self.assertFalse([e for e in b.of("beam") if e.extra.get("merged_from")])
        link(b)                                     # no section says it is one beam: left as drawn
        self.assertFalse([e for e in b.of("beam") if e.extra.get("merged_from")])
        _with_section(b, 750.0, [2000.0])           # SECTION X-X, 750 wide, its cut across the lines
        link(b)
        joined = sorted((e for e in b.of("beam") if e.extra.get("merged_from")), key=lambda e: e.geometry["start"][0])
        self.assertEqual([(round(e.geometry["start"][0]), round(e.geometry["end"][0])) for e in joined],
                         [(0, 4225), (4225, 6525), (6525, 9000)])
        self.assertEqual({round(e.geometry["start"][1]) for e in joined}, {-260})      # outer +115 .. inner -635
        self.assertEqual({e.final("width") for e in joined}, {750.0})
        self.assertEqual(sum(1 for e in b.of("beam") if e.extra.get("merged_into")), 4)
        out, rep = export_plan(b, SID, provisional=True)
        ys = sorted(round(r["start"][1]) for r in out["beams"])
        self.assertEqual(ys, [-260, -260, -260, 5000, 5400, 9000, 9230])             # the rest as before
        link(b)                                                                       # again: the same
        self.assertEqual(sum(1 for e in b.of("beam") if e.extra.get("merged_from")), 3)

    def test_how_a_beam_is_drawn_dashed_or_solid_is_read_and_checked(self):
        """Drafting: a face under the slab is dashed; one drawn solid is seen from above -
        the slab ends there or the beam stands above it. Solid on both faces with level
        slab either side: an upturned beam? - asked, not changed."""
        from building import Sheet
        def bm(n, y):
            return {"mark": f"B{n}", "kind": "beam", "start": [0.0, y], "end": [6000.0, y], "width": 230.0,
                    "width_source": "", "depth": 600.0, "depth_source": "", "top_mm": 0.0,
                    "source_layer": "BEAM", "handle": f"b{n}"}
        slab = {"mark": "S1", "outline": [[0, -4000], [6000, -4000], [6000, 4000], [0, 4000]], "holes": [],
                "thickness": 125.0, "thickness_source": "", "sunk_mm": 0.0, "source_layer": "", "handle": "", "notes": []}
        b3 = dict(bm(3, -2000.0), source_layer="GIRDER")                  # a layer drawn solid throughout
        b, _ = plan(beams=[bm(1, 0.0), bm(2, 2000.0), b3], slabs=[slab])
        b.sheets[SID].layer_map = {"BEAM": "beam", "GIRDER": "girder"}
        b.sheets[SID].extra["view_sheet"] = "P:V"
        b.add_sheet(Sheet(id="P:V", file="p.dwg", kind="framing_plan", reader="sheets.registry", extra={"set": "P"}))
        # B1 dashed both faces (a normal beam under the slab); B2 solid both faces; B3 solid on
        # a layer with no dashed line at all (J1153 draws every beam solid): says nothing
        b.sheets["P:V"].extra["beam_lines"] = {"layers": ["BEAM", "GIRDER"], "lines": [
            [0, 0, 115, 6000, 115, 1], [0, 0, -115, 6000, -115, 1],
            [0, 0, 2115, 6000, 2115, 0], [0, 0, 1885, 6000, 1885, 0],
            [1, 0, -1885, 6000, -1885, 0], [1, 0, -2115, 6000, -2115, 0]]}
        link(b)
        b1, b2, _ = sorted(b.of("beam"), key=lambda e: e.mark)
        self.assertEqual(b1.extra["faces"], {"left": "dashed", "right": "dashed"})
        self.assertEqual(b2.extra["faces"], {"left": "solid", "right": "solid"})
        up = [i for i in b.issues if i.category == "line style" and "upturned" in i.message]
        self.assertEqual([i.elements for i in up], [[b2.id]])
        self.assertEqual(b2.final("depth"), 600.0)                         # nothing changed by it
        self.assertFalse(b2.geometry.get("top_mm"))

    def test_lines_with_a_gap_across_are_not_joined(self):
        """Two 230 beams 840 apart (a 610 gap) linked only through a longer piece between
        them: not one beam (Main Block east side was joined 1070 wide)."""
        def bm(n, y, x0, x1, w):
            return {"mark": "", "kind": "beam", "start": [x0, y], "end": [x1, y], "width": w, "width_source": "",
                    "depth": 450.0, "depth_source": "", "top_mm": 0.0, "source_layer": "BEAM", "handle": f"g{n}"}
        b, _ = plan(beams=[bm(0, 0.0, 0.0, 3400.0, 230.0), bm(1, 840.0, 0.0, 3400.0, 230.0),
                           bm(2, 420.0, -6000.0, 9000.0, 610.0)])
        link(b)
        self.assertFalse([e for e in b.of("beam") if e.extra.get("merged_from")])

    def test_stairs_are_not_modelled_unless_switched_on(self):
        """User 2026-10-10: 'no need staircase structure model now - remove this solid box'.
        Read and listed, not built (no 3D box, no stairs.json for Revit) until decided."""
        import os
        import tempfile
        from building.export import export
        from building.viewer3d import scene
        b, _ = plan(columns=[col("C1", 0, 0, 300.0, 600.0)])
        flight = {"first": [[0, 0], [1500, 0]], "last": [[0, 3000], [1500, 3000]], "treads": 10, "going": 300,
                  "width": 1500}
        b.add(Element(id=f"{SID}/stair/0", type="stair", sheet=SID, geometry={"flights": [flight]},
                      props={"width": Value(1500.0, "read", 1.0, [Evidence("stairs", SID)])}))
        link(b)
        self.assertFalse([i for i in scene(b, "P")["items"] if i["t"] == "stair"])
        self.assertTrue([i for i in scene(b, "P", {"model_stairs": True})["items"] if i["t"] == "stair"])
        with tempfile.TemporaryDirectory() as tmp:
            b.save(os.path.join(tmp, "building.json"))
            out = export(tmp, log=lambda *a, **k: None)
            stairs = os.path.join(out, "P_V_plan", "stairs.json")
            self.assertFalse(os.path.exists(stairs))
            export(tmp, log=lambda *a, **k: None, decisions={"model_stairs": True})
            self.assertTrue(os.path.exists(stairs))
            export(tmp, log=lambda *a, **k: None)                  # switched off again: an old one goes
            self.assertFalse(os.path.exists(stairs))

    def test_the_slab_says_which_way_round_a_section_goes(self):
        """User 2026-10-19 (Sasan Gir terrace B31 / B2): 'the slope of the beam should be
        outward'. The slab frames into the full-height side, so the slope falls away from it,
        even where the outer face is the one drawn solid. And the beam is the section's 750
        wide, not the 375 its caption gives for the full-height part."""
        from building import Sheet
        def bm(n, y, w):
            return {"mark": "", "kind": "beam", "start": [0.0, y], "end": [6000.0, y], "width": w,
                    "width_source": "", "depth": 600.0, "depth_source": "", "top_mm": 0.0,
                    "source_layer": "BEAM", "handle": f"b{n}"}
        slab = {"mark": "S1", "outline": [[0, -900], [6000, -900], [6000, -4000], [0, -4000]], "holes": [],
                "thickness": 125.0, "thickness_source": "", "sunk_mm": 0.0, "source_layer": "", "handle": "", "notes": []}
        b, _ = plan(beams=[bm(0, 0.0, 230.0), bm(1, -375.0, 520.0)], slabs=[slab])
        b.sheets[SID].layer_map = {"BEAM": "beam"}
        b.sheets[SID].extra["view_sheet"] = "P:V"
        b.add_sheet(Sheet(id="P:V", file="p.dwg", kind="framing_plan", reader="sheets.registry", extra={"set": "P"}))
        b.add_sheet(Sheet(id="P:D", file="p.dwg", kind="beam_details", reader="sheets.registry", extra={"set": "P"}))
        b.sheets["P:V"].extra["beam_lines"] = {"layers": ["BEAM"], "lines": [
            [0, 0, 115, 6000, 115, 0], [0, 0, -115, 6000, -115, 1], [0, 0, -635, 6000, -635, 1]]}
        ev = [Evidence("beam_sections", "P:D")]
        b.add(Element(id="P:D/beam_section/0", type="beam_section", sheet="P:D", mark="X",
                      props={"width": Value(750.0, "read", 1.0, ev), "depth": Value(600.0, "read", 1.0, ev)},
                      extra={"profile": [[375, -600], [0, -600], [0, -500], [375, 0], [750, 0], [750, -600]],
                             "title": "SECTION :- X-X", "undimensioned": [], "hung": []}))
        b.add(Element(id="P:V/section_cut/0", type="section_cut", sheet="P:V", mark="X",
                      geometry={"a": [3000.0, 900.0], "b": [3000.0, -1500.0]}, props={"name": Value("X", "read", 0.9, ev)}))
        link(b)
        e = next(x for x in b.of("beam") if x.extra.get("merged_from"))
        self.assertEqual(e.extra["faces"], {"left": "solid", "right": "dashed"})
        prof = e.extra["section_profile"]
        self.assertTrue(all(u <= 0.5 for u, v in prof if v > -1), "full height to the slab (south), slope outward")
        self.assertEqual(e.resolved["section"].status, "read")
        self.assertIn("to the slab", e.resolved["section"].evidence[0].source)
        self.assertEqual(e.final("width"), 750.0)

    def test_without_a_slab_the_line_styles_say_which_way_round(self):
        """No slab beside the beam: the face drawn solid is the full-height side, the slope
        on the dashed side (user 2026-10-10)."""
        from building import Sheet
        def bm(n, y, w):
            return {"mark": "", "kind": "beam", "start": [0.0, y], "end": [6000.0, y], "width": w,
                    "width_source": "", "depth": 600.0, "depth_source": "", "top_mm": 0.0,
                    "source_layer": "BEAM", "handle": f"b{n}"}
        b, _ = plan(beams=[bm(0, 0.0, 230.0), bm(1, -375.0, 520.0)], slabs=[])
        b.sheets[SID].layer_map = {"BEAM": "beam"}
        b.sheets[SID].extra["view_sheet"] = "P:V"
        b.add_sheet(Sheet(id="P:V", file="p.dwg", kind="framing_plan", reader="sheets.registry", extra={"set": "P"}))
        b.add_sheet(Sheet(id="P:D", file="p.dwg", kind="beam_details", reader="sheets.registry", extra={"set": "P"}))
        b.sheets["P:V"].extra["beam_lines"] = {"layers": ["BEAM"], "lines": [
            [0, 0, 115, 6000, 115, 0], [0, 0, -115, 6000, -115, 1], [0, 0, -635, 6000, -635, 1]]}
        ev = [Evidence("beam_sections", "P:D")]
        b.add(Element(id="P:D/beam_section/0", type="beam_section", sheet="P:D", mark="X",
                      props={"width": Value(750.0, "read", 1.0, ev), "depth": Value(600.0, "read", 1.0, ev)},
                      extra={"profile": [[375, -600], [0, -600], [0, -500], [375, 0], [750, 0], [750, -600]],
                             "title": "SECTION :- X-X", "undimensioned": [], "hung": []}))
        b.add(Element(id="P:V/section_cut/0", type="section_cut", sheet="P:V", mark="X",
                      geometry={"a": [3000.0, 900.0], "b": [3000.0, -1500.0]}, props={"name": Value("X", "read", 0.9, ev)}))
        link(b)
        e = next(x for x in b.of("beam") if x.extra.get("merged_from"))
        prof = e.extra["section_profile"]
        self.assertTrue(all(u >= -0.5 for u, v in prof if v > -1), "full height at the solid (north) face")
        self.assertIn("drawn solid", e.resolved["section"].evidence[0].source)

    def test_a_layer_name_alone_does_not_dispute_a_section_the_captions_agree_with(self):
        """Sasan Gir terrace B31a-c / B2a-e (user 2026-10-19, note N1 'drawings disagree 750 /
        375 / 230'): SECTION Z-Z is 750 overall, its full-height part 375 - the captions'
        375X600 - and the plan draws 750 face to face. The 230 is only the layer's name
        (BEAM-230X600, the layer the beams are drawn on): no conflict. Without a caption
        agreeing, or with a caption that does not fit the section, it stays one."""
        from building import linker
        ev = [Evidence("beam_sections", "P:D")]
        sec = Element(id="P:D/beam_section/0", type="beam_section", sheet="P:D", mark="Z",
                      props={"width": Value(750.0, "read", 1.0, ev), "depth": Value(600.0, "read", 1.0, ev)})
        prof = [[375, -600], [750, -600], [750, -400], [375, 0], [0, 0], [0, -600]]

        def width_status(*sources):
            e = Element(id="P:V/beam/1", type="beam", sheet="P:V", props={"width": Value(750.0, "read", 1.0, ev)})
            e.resolved["width"] = Value(750.0, "conflict", 0.5, [Evidence("linker", "P:V", source=s) for s in sources])
            linker._section_width(e, sec, prof, "Z-Z")
            return e.resolved["width"].status, e.final("width")
        self.assertEqual(width_status("detail caption: 375.0", "plan caption: 375.0", "plan geometry: 750.0",
                                      "layer name: 230.0"), ("read", 750.0))
        self.assertEqual(width_status("plan geometry: 750.0", "layer name: 230.0")[0], "conflict")
        self.assertEqual(width_status("plan caption: 300.0", "plan geometry: 750.0", "layer name: 230.0")[0],
                         "conflict")
        self.assertEqual(width_status("plan caption: 375.0", "plan geometry: 750.0")[0], "read")

    def test_a_building_section_gives_heights_and_slopes(self):
        """Main Block terrace SECTION C-C (a block, scale 1.495): B19's top 2270 below the
        terrace, the slab between B19 and B7 rising to the terrace, the beams across it too.
        The plan alone built all of it flat at the terrace."""
        from building import Sheet
        def bm(mark, a, c):
            return {"mark": mark, "kind": "beam", "start": list(a), "end": list(c), "width": 230.0,
                    "width_source": "", "depth": 600.0, "depth_source": "", "top_mm": 0.0,
                    "source_layer": "BEAM", "handle": mark or f"h{a[0]}{a[1]}"}
        slab = {"mark": "S6", "outline": [[115, 115], [3185, 115], [3185, 4885], [115, 4885]], "holes": [],
                "thickness": 125.0, "thickness_source": "", "sunk_mm": 0.0, "source_layer": "", "handle": "", "notes": []}
        b, _ = plan(beams=[bm("B19", (0, 0), (0, 5000)), bm("B7", (3300, 0), (3300, 5000)),
                           bm("B21", (6900, 0), (6900, 5000)), bm("", (0, 5000), (6900, 5000)),
                           bm("", (0, 9000), (6900, 9000))], slabs=[slab])
        b.sheets[SID].extra.update({"level_key": "TERRACE", "view_sheet": "P:V"})
        b.add_sheet(Sheet(id="P:V", file="p.dwg", kind="framing_plan", reader="sheets.registry", extra={"set": "P"}))
        k = 1.495                                                 # drawn units per mm, as the block
        def box(mark, x, top):
            return {"mark": mark, "x0": x * k - 172, "x1": x * k + 172, "top": top, "bottom": top - 600 * k}
        b.add(Element(id="P:V/building_section/0", type="building_section", sheet="P:V", mark="C",
                      props={"scale": Value(k, "read", 0.7, [Evidence("building_sections", "P:V")])},
                      extra={"levels": [{"name": "Terrace", "y": 0.0}, {"name": "2nd Floor", "y": -3600 * k}],
                             "beams": [box("B19", 0, -2270 * k), box("B7", 3300, 0.0), box("B21", 6900, 0.0)],
                             "slopes": [{"between": [0, 1], "line": [0, -2270 * k, 3300 * k, 0]}],
                             "title": "SECTION: C-C"}))
        link(b)
        out, _ = export_plan(b, SID, provisional=True)
        b19 = next(r for r in out["beams"] if r["mark"] == "B19")
        self.assertEqual(b19["top_mm"], -2270.0)
        self.assertEqual(next(r for r in out["beams"] if r["mark"] == "B7").get("top_mm", 0.0), 0.0)
        edge = sorted((r for r in out["beams"] if not r["mark"] and r["start"][1] == 5000.0),
                      key=lambda r: min(r["start"][0], r["end"][0]))
        self.assertEqual(len(edge), 2, "split where it leaves the slope")
        self.assertEqual(sorted((edge[0]["top_mm"], edge[0]["top_end_mm"])), [-2270.0, 0.0])
        self.assertFalse(edge[1].get("top_end_mm"))                # flat beyond B7
        far = [r for r in out["beams"] if r["start"][1] == 9000.0]
        self.assertEqual(len(far), 1)                               # beyond the frame: untouched
        self.assertFalse(far[0].get("top_end_mm"))
        s6 = next(s for s in out["slabs"] if s["mark"] == "S6")
        self.assertEqual((s6["slope"]["z0"], s6["slope"]["z1"]), (-2270, 0))

    def test_a_plans_cut_letters_mean_its_own_drawings_section_of_that_name(self):
        """Sasan Gir: the terrace drawing's C-C (B21 at the terrace, 3600 above the 2nd floor)
        fitted the 2nd floor plan of another drawing - marks B19, B21 repeat floor to floor -
        whose own drawing has a C-C of its own. Without one, another drawing's C-C still counts."""
        from building import Sheet
        def bm(mark, a, c):
            return {"mark": mark, "kind": "beam", "start": list(a), "end": list(c), "width": 230.0,
                    "width_source": "", "depth": 600.0, "depth_source": "", "top_mm": 0.0,
                    "source_layer": "BEAM", "handle": mark}
        k = 1.0
        def box(mark, x, top):
            return {"mark": mark, "x0": x - 115, "x1": x + 115, "top": top, "bottom": top - 600}
        for own_cc, want in ((True, None), (False, 3600.0)):
            b, _ = plan(beams=[bm("B19", (0, 0), (0, 5000)), bm("B21", (6900, 0), (6900, 5000))])
            b.sheets[SID].extra.update({"level_key": "L2", "view_sheet": "P:V"})
            b.add_sheet(Sheet(id="P:V", file="p.dwg", kind="framing_plan", reader="sheets.registry", extra={"set": "P"}))
            b.add_sheet(Sheet(id="T:V", file="t.dwg", kind="framing_plan", reader="sheets.registry", extra={"set": "P"}))
            if own_cc:
                b.add_sheet(Sheet(id="P:S", file="p.dwg", kind="section", title="SECTION: C-C",
                                  reader="sheets.registry", extra={"set": "P"}))
            b.add(Element(id="T:V/building_section/0", type="building_section", sheet="T:V", mark="C",
                          props={"scale": Value(k, "read", 0.7, [Evidence("building_sections", "T:V")])},
                          extra={"levels": [{"name": "Terrace", "y": 3600.0}, {"name": "2nd Floor", "y": 0.0}],
                                 "beams": [box("B19", 0, 1330.0), box("B21", 6900, 3600.0)], "slopes": [],
                                 "title": "SECTION: C-C"}))
            link(b)
            b21 = next(e for e in b.of("beam") if e.mark == "B21")
            self.assertEqual(b21.extra.get("section_top"), want, f"own C-C drawn: {own_cc}")

    def test_a_column_under_a_lowered_beam_ends_there_unless_a_beam_reaches_it_higher(self):
        """B19 2270 below the terrace: the column under its north end (centre 110 past the
        beam's end, inside the column) stops under it; the one at its south end also
        carries a beam at terrace level and rises to the terrace."""
        from building import Sheet
        def bm(mark, a, c):
            return {"mark": mark, "kind": "beam", "start": list(a), "end": list(c), "width": 230.0,
                    "width_source": "", "depth": 600.0, "depth_source": "", "top_mm": 0.0,
                    "source_layer": "BEAM", "handle": mark or f"h{a[0]}{a[1]}"}
        cols = [col("C1", 0.0, 5110.0, 230.0, 450.0), col("C2", 0.0, -110.0, 230.0, 450.0)]
        b, _ = plan(columns=cols, beams=[bm("B19", (0, 0), (0, 5000)), bm("B7", (3300, 0), (3300, 5000)),
                                         bm("", (0, -110), (0, -4000))])              # terrace beam on C2
        b.sheets[SID].extra.update({"level_key": "TERRACE", "view_sheet": "P:V"})
        b.add_sheet(Sheet(id="P:V", file="p.dwg", kind="framing_plan", reader="sheets.registry", extra={"set": "P"}))
        k = 1.495
        def box(mark, x, top):
            return {"mark": mark, "x0": x * k - 172, "x1": x * k + 172, "top": top, "bottom": top - 600 * k}
        b.add(Element(id="P:V/building_section/0", type="building_section", sheet="P:V", mark="C",
                      props={"scale": Value(k, "read", 0.7, [Evidence("building_sections", "P:V")])},
                      extra={"levels": [{"name": "Terrace", "y": 0.0}],
                             "beams": [box("B19", 0, -2270 * k), box("B7", 3300, 0.0)], "slopes": [],
                             "title": "SECTION: C-C"}))
        link(b)
        out, _ = export_plan(b, SID, provisional=True)
        tops = {c["mark"]: c.get("top_mm") for c in out["columns"]}
        self.assertEqual((tops["C1"], tops["C2"]), (-2270.0, None))

    def test_what_the_legend_marks_is_put_on_the_model_and_said(self):
        from building import Sheet
        slab = {"mark": "S8", "outline": [[0, 0], [2000, 0], [2000, 1000], [0, 1000]], "holes": [],
                "thickness": 125.0, "thickness_source": "", "sunk_mm": 0.0, "source_layer": "", "handle": "", "notes": []}
        b, _ = plan(columns=[col("C1", 5000, 5000), col("C2", 9000, 5000)], slabs=[slab])
        b.sheets[SID].extra["view_sheet"] = "P:V"
        b.add_sheet(Sheet(id="P:V", file="p.dwg", kind="framing_plan", reader="sheets.registry", extra={"set": "P"}))
        ev = [Evidence("legend_zones", "P:V")]

        def zone(n, kind, pts, words):
            b.add(Element(id=f"P:V/legend_zone/{n}", type="legend_zone", sheet="P:V", mark=kind,
                          geometry={"outline": pts}, props={"kind": Value(kind, "read", 0.9, ev)},
                          extra={"words": words, "pattern": "X"}))
        zone(0, "column ends", [[4800, 4600], [5200, 4600], [5200, 5400], [4800, 5400]], "RCC COLUMN ENDS AT THIS LVL")
        zone(1, "steel roof", [[-500, -500], [2500, -500], [2500, 1500], [-500, 1500]], "FABRICATION ROOF AS PER ARCH")
        link(b)
        c1, c2 = sorted(b.of("column"), key=lambda e: e.geometry["x"])
        self.assertIn("ends at this level", c1.extra["legend"])
        self.assertNotIn("legend", c2.extra)
        self.assertTrue(any(i.category == "not modelled" and "steel roof" in i.message for i in b.issues))
        self.assertTrue(any(i.category == "confirm" and "steel (fabrication) roof" in i.message for i in b.issues))
        out, _ = export_plan(b, SID, provisional=True)
        self.assertEqual(len(out["slabs"]), 1)                 # nothing built differently: said, not changed

    def test_every_floor_of_a_set_is_built_from_one_origin(self):
        """External review, point I: each floor's origin was the south-west corner of its
        own columns, so a floor with one column more (or one dropped) landed shifted."""
        import json
        import os
        import tempfile
        from building.export import export
        b = Building()
        for n, (sid, bp) in enumerate((("P:L1/plan", [0.0, 0.0]), ("P:L2/plan", [500.0, 300.0]))):
            d = {"source_file": "p.dwg", "level_name": f"Level {n + 1}", "dxf_file": "", "units": "mm",
                 "dxf_version": "AC1032", "base_point": bp, "stats": {}, "layer_map": {}, "grids": [],
                 "columns": [col("C1", 1000.0, 1000.0)], "walls": [], "beams": [], "slabs": [], "slab_tags": [],
                 "folds": [], "sunk": [], "openings": [], "issues": [], "counts": {}, "issue_counts": {}}
            from_legacy(d, sid, b)
            b.sheets[sid].extra.update({"set": "P", "level_key": f"L{n + 1}"})
        link(b)
        with tempfile.TemporaryDirectory() as tmp:
            b.save(os.path.join(tmp, "building.json"))

            def origins(decisions=None):
                out = export(tmp, log=lambda *a, **k: None, decisions=decisions)
                return [json.load(open(os.path.join(out, f, "model.json"), encoding="utf-8"))["base_point"]
                        for f in ("P_L1_plan", "P_L2_plan")]
            self.assertEqual(origins(), [[0.0, 0.0], [0.0, 0.0]])
            self.assertEqual(origins({"origin": [100.0, 200.0]}), [[100.0, 200.0], [100.0, 200.0]])

    def test_a_hidden_beam_on_an_assumed_slab_is_held(self):
        b, d = plan()
        fold = {"start": [0.0, 0.0], "end": [3000.0, 0.0], "width": 200.0, "depth": 250.0, "top_mm": 0.0,
                "notes": ["between S1 and S2, step 100 mm",
                          "depth assumes a 150 mm lower slab: its thickness is not given"]}
        ok = dict(fold, start=[0.0, 4000.0], end=[3000.0, 4000.0], notes=["between S1 and S3, step 100 mm"])
        d["folds"] = [fold, ok]
        b = from_legacy(copy.deepcopy(d), SID)
        b.sheets[SID].extra.update({"set": "P", "level_key": "L1"})
        link(b)
        out, rep = export_plan(b, SID)
        self.assertEqual(len(out["folds"]), 1)
        self.assertTrue(any("hidden beam" in h for h in rep.held))

    def test_an_excluded_element_is_left_out(self):
        b, _ = plan(columns=[col("C1", 0, 0), col("C2", 5000, 0)])
        link(b, {"elements": {f"{SID}/column/1": {"excluded": True}}})
        out, rep = export_plan(b, SID)
        self.assertEqual([c["mark"] for c in out["columns"]], ["C1"])
        self.assertEqual(len(rep.left_out), 1)

    def test_an_L_column_goes_out_as_a_column_and_masonry_is_dropped(self):
        ell = [[0, 0], [450, 0], [450, 230], [230, 230], [230, 750], [0, 750]]
        brick = [[0, 5000], [7000, 5000], [7000, 5230], [0, 5230]]
        walls = [{"mark": "", "start": None, "end": None, "outline": ell, "thickness": None,
                  "source_layer": "STR.COLUMN", "handle": "W1"},
                 {"mark": "", "start": None, "end": None, "outline": brick, "thickness": None,
                  "source_layer": "BRICK WALL", "handle": "W2"}]
        b, _ = plan(walls=walls)
        link(b)
        out, rep = export_plan(b, SID)
        self.assertEqual(out["walls"], [])
        self.assertEqual(len(out["columns"]), 1)
        c = out["columns"][0]
        self.assertEqual((c["x"], c["y"], c["width"], c["depth"]), (225.0, 375.0, 450.0, 750.0))
        self.assertEqual(out["counts"]["columns"], 1)

    def test_a_split_beam_goes_out_as_its_spans(self):
        beam = {"mark": "B1", "kind": "beam", "start": [0.0, 0.0], "end": [8000.0, 0.0], "width": 230.0,
                "width_source": "measured", "depth": None, "depth_source": "", "top_mm": 0.0,
                "source_layer": "BEAM", "handle": "B1"}
        b, _ = plan(beams=[beam], columns=[col("C1", 4000, 0, 230.0, 230.0)])
        b.add_sheet(__import__("building").Sheet(id="P:V", file="p", reader="sheets.registry", extra={"set": "P"}))
        b.sheets[SID].extra["view_sheet"] = "P:V"
        for n, (m, x) in enumerate((("B1", 2000.0), ("B2", 6000.0))):
            ev = Evidence("labels", "P:V", "plan caption")
            b.add(Element(id=f"P:V/caption/{n}", type="caption", sheet="P:V", mark=m,
                          geometry={"at": [x, 150.0], "text_height": 150.0, "rotation": 0.0},
                          props={"family": Value("beam", "read", 1.0, [ev]), "width": Value(230.0, "read", 0.9, [ev]),
                                 "depth": Value(450.0 + 150 * n, "read", 0.9, [ev])}))
        link(b)
        out, rep = export_plan(b, SID)
        got = sorted((r["mark"], r["start"][0], r["end"][0], r["depth"]) for r in out["beams"])
        self.assertEqual([g[0] for g in got], ["B1", "B2"])
        self.assertEqual([g[3] for g in got], [450.0, 600.0])
        self.assertAlmostEqual(got[0][2], got[1][1])                    # the spans meet


class SlabsFromBays(unittest.TestCase):
    def test_marked_bays_become_slabs_with_their_scheduled_thickness(self):
        """The blind KA set: S1 / S2 written in the bays, no slab outline drawn."""
        from building import Sheet
        cols = [col(f"C{i}", x, y, 300.0, 300.0) for i, (x, y) in
                enumerate([(0, 0), (4500, 0), (9000, 0), (0, 5000), (4500, 5000), (9000, 5000)], 1)]

        def beam(n, a, c):
            return {"mark": f"B{n}", "kind": "beam", "start": list(a), "end": list(c), "width": 230.0,
                    "width_source": "measured", "depth": None, "depth_source": "", "top_mm": 0.0,
                    "source_layer": "BEAM", "handle": f"B{n}"}
        beams = [beam(1, (150, 0), (4350, 0)), beam(2, (4650, 0), (8850, 0)), beam(3, (150, 5000), (4350, 5000)),
                 beam(4, (4650, 5000), (8850, 5000)), beam(5, (0, 150), (0, 4850)), beam(6, (4500, 150), (4500, 4850)),
                 beam(7, (9000, 150), (9000, 4850))]
        b, _ = plan(columns=cols, beams=beams)
        b.add_sheet(Sheet(id="P:V", file="p", reader="sheets.registry", extra={"set": "P"}))
        b.sheets[SID].extra["view_sheet"] = "P:V"
        for n, (m, x) in enumerate((("S1", 2250.0), ("S2", 6750.0))):
            ev = [Evidence("labels", "P:V", "plan caption")]
            b.add(Element(id=f"P:V/caption/{n}", type="caption", sheet="P:V", mark=m,
                          geometry={"at": [x, 2500.0], "text_height": 200.0},
                          props={"family": Value("slab", "read", 1.0, ev)}))
            b.add(Element(id=f"S/spec/{m}", type="spec", sheet="S", mark=m,
                          props={"family": Value("slab", "read", 1.0, ev),
                                 "thickness": Value(125.0 + 25 * n, "read", 1.0, ev)},
                          extra={"table": "SLAB SCHEDULE"}))
        stats = link(b)
        self.assertEqual(stats["slab panels from beam bays"], 2)
        out, rep = export_plan(b, SID)
        self.assertEqual(sorted((s["mark"], s["thickness"]) for s in out["slabs"]), [("S1", 125.0), ("S2", 150.0)])
        self.assertEqual(to_legacy(b, SID)["slabs"], [])                  # the plan reader's own output untouched


def footings(founding=None):
    """Eight footings on another sheet, offset by (50000, -20000) from the plan; F2's
    depth in conflict. `founding`: F1's founding level in its schedule."""
    from building import Sheet
    bays = [0, 4000, 7500, 12000, 15000, 19500, 23000, 27500]
    b, _ = plan(columns=[col(f"C{i + 1}", float(x), 0.0) for i, x in enumerate(bays)])
    b.sheets[SID].extra["level_key"] = "GF"
    b.add_sheet(Sheet(id="F:V", file="f.dwg", reader="sheets.registry", extra={"set": "P"}))
    ev = [Evidence("foundations", "F:V")]
    for i, x in enumerate(bays):
        props = {"width": Value(1500.0, "read", 0.8, ev), "length": Value(1800.0, "read", 0.8, ev),
                 "mark": Value("F1" if i else "F2", "read", 0.8, ev),
                 "steps": Value([[900.0, 1200.0]], "read", 0.8, ev)}
        b.add(Element(id=f"F:V/footing/{i}", type="footing", sheet="F:V", mark="F1" if i else "F2",
                      geometry={"x": x + 50000.0, "y": -20000.0,
                                "outline": [[x + 49250, -20900], [x + 50750, -20900], [x + 50750, -19100],
                                            [x + 49250, -19100]]}, props=props))
    for mark, d in (("F1", 600.0), ("F2", 450.0)):
        props = {"family": Value("footing", "read", 1.0, ev), "depth": Value(d, "read", 1.0, ev),
                 "step_depths": Value("450+150", "read", 1.0, ev)}
        if founding is not None and mark == "F1":
            props["founding_level"] = Value(founding, "read", 1.0, ev)
        b.add(Element(id=f"S/spec/{mark}", type="spec", sheet="F:V", mark=mark, props=props,
                      extra={"table": "SCHEDULE OF FOOTING"}))
    b.add(Element(id="S/spec/F2b", type="spec", sheet="F:V", mark="F2",
                  props={"family": Value("footing", "read", 1.0, ev), "depth": Value(750.0, "read", 1.0, ev)},
                  extra={"table": "FOOTING DETAIL"}))
    return b


class Foundations(unittest.TestCase):
    def test_footings_are_placed_under_the_plans_columns_and_conflicts_listed(self):
        """Lined up by their centres on the plan's columns; each at the founding level
        its schedule gives. F2's depth is in conflict: listed, not built."""
        from building.export import foundations_for
        b = footings(founding=-1800.0)
        link(b)
        fd = foundations_for(b, "P")
        foot = [i for i in fd["items"] if i["type"] == "footing"]
        self.assertEqual(len(foot), 7)
        self.assertTrue(all(abs(f["y"]) < 1 for f in foot))                   # moved onto the plan
        self.assertTrue(all(f["under_plan_column"] for f in foot))
        self.assertEqual((foot[0]["thickness"], foot[0]["size_x"], foot[0]["size_y"]), (450.0, 1500.0, 1800.0))
        self.assertTrue(all(f["bottom_mm"] == -1800.0 for f in foot))
        self.assertEqual(len([i for i in fd["items"] if i["type"] == "step"]), 7)
        self.assertTrue(any(x.startswith("footing F2") and "depth" in x for x in fd["not_placed"]))

    def test_no_founding_level_no_footing(self):
        """The drawings give no founding level: nothing is placed at an assumed depth."""
        from building.export import foundations_for
        b = footings()
        stats = link(b)
        self.assertEqual(stats["founding_level missing"], 8)
        fd = foundations_for(b, "P")
        self.assertEqual([i for i in fd["items"] if i["type"] == "footing"], [])
        self.assertTrue(all("founding level" in x for x in fd["not_placed"] if x.startswith("footing F1")))
        self.assertEqual(sum(1 for i in b.issues if "founding level" in i.message), 1)   # one issue, not eight

    def test_one_founding_level_decided_for_all_and_one_footing_its_own(self):
        """A project decision fills every footing without one; a footing decided on its
        own keeps its own; a conflict is never settled by the blanket value."""
        from building.export import foundations_for
        b = footings()
        link(b, {"project": {"founding_level_mm": -1500},
                 "elements": {"F:V/footing/3": {"founding_level": -2100}}})
        fd = foundations_for(b, "P")
        got = {round(i["x"]): i["bottom_mm"] for i in fd["items"] if i["type"] == "footing"}
        self.assertEqual(got[12000], -2100.0)
        self.assertEqual({v for k, v in got.items() if k != 12000}, {-1500.0})
        f2 = next(e for e in b.elements if e.id == "F:V/footing/0")
        self.assertEqual(f2.resolved["founding_level"].status, "override")      # F2: still held by its depth
        self.assertFalse(any(round(i["x"]) == 0 for i in fd["items"] if i["type"] == "footing"))


class Levels(unittest.TestCase):
    def test_floor_levels_decided_fill_the_gaps_and_add_the_base(self):
        from building.export import level_table
        b, _ = plan()
        b.sheets[SID].level = "Level 1"
        link(b)
        lv = level_table(b, {"levels": {"Level 1": 3150, "Level 0": 0}})
        got = {x["revit_name"]: (x["elevation_mm"], x["status"]) for x in lv}
        self.assertEqual(got["Level 1"], (3150.0, "override"))
        self.assertEqual(got["Level 0"], (0.0, "override"))                       # the level below, added
        self.assertEqual({x["revit_name"]: x["status"] for x in level_table(b)}, {"Level 1": "missing"})


class FoundingInSchedules(unittest.TestCase):
    def test_a_signed_level_is_read_and_a_bare_number_is_not(self):
        from types import SimpleNamespace as T
        from building.readers.tables import founding_level
        for text, want in (("-1.800", -1800.0), ("(-)1500", -1500.0), ("- 1.5 M", -1500.0), ("-2100MM", -2100.0),
                           ("+0.450", 450.0), ("1500", None), ("GOOD SOIL", None)):
            self.assertEqual(founding_level([T(text=text)]), want, text)


class BeamCorners(unittest.TestCase):
    """ALCAZAR 1st floor: beams meeting at an L corner (mitred faces, centrelines
    meeting) were drawn as boxes stopping at the corner point - a notch at every
    corner with no column. Their solids are carried on half the other's width."""

    def test_an_L_corner_is_filled(self):
        from building.beam_ends import corner_extensions
        ext = corner_extensions([((0, 0), (5000, 0), 230), ((0, 0), (0, 4000), 300)])
        self.assertEqual(ext, [[150.0, 0.0], [115.0, 0.0]])

    def test_a_tee_and_a_run_are_left(self):
        from building.beam_ends import corner_extensions
        tee = corner_extensions([((0, 0), (5000, 0), 230), ((2500, 0), (2500, 3000), 230)])
        self.assertEqual(tee, [[0.0, 0.0], [0.0, 0.0]])
        run = corner_extensions([((0, 0), (5000, 0), 230), ((5000, 0), (9000, 0), 230)])
        self.assertEqual(run, [[0.0, 0.0], [0.0, 0.0]])
        # three ends at one joint: not an L
        three = corner_extensions([((0, 0), (5000, 0), 230), ((0, 0), (0, 4000), 230), ((0, 0), (-3000, 0), 230)])
        self.assertEqual(three, [[0.0, 0.0]] * 3)



class BeamsOntoSupports(unittest.TestCase):
    """Winter Villa: beams stopping 50-260 mm short of the angled walls that carry them."""

    def test_a_beam_stopping_short_is_carried_onto_its_wall(self):
        from shapely.geometry import box
        from building.beam_ends import onto_supports
        got = onto_supports([((0.0, 0.0), (1000.0, 0.0), 300.0)], [box(1060, -200, 1360, 200)])
        self.assertEqual(got, [[None, [1210.0, 0.0], None]])        # to the middle of its bearing

    def test_a_curved_beam_is_carried_on_round_its_arc(self):
        import math
        from shapely.geometry import box
        from building.beam_ends import onto_supports
        a, m, b = (5000.0, 0.0), (5000 * math.cos(math.pi / 4), 5000 * math.sin(math.pi / 4)), (0.0, 5000.0)
        (s0, e, mid), = onto_supports([(a, b, 300.0, m)], [box(-400, 4800, -100, 5200)])
        self.assertIsNone(s0)
        self.assertAlmostEqual(math.hypot(*e[:2]), 5000.0, places=3)
        self.assertAlmostEqual(math.hypot(*mid[:2]), 5000.0, places=3)
        self.assertTrue(-400 < e[0] < -100)

    def test_beams_in_line_and_free_ends_stay_as_drawn(self):
        from building.beam_ends import onto_supports
        # two spans of one line, a column's width apart: not carried into each other
        self.assertEqual(onto_supports([((0.0, 0.0), (5000.0, 0.0), 300.0), ((5300.0, 0.0), (9000.0, 0.0), 300.0)], []),
                         [[None, None, None], [None, None, None]])
        # nothing ahead within reach: a free end
        self.assertEqual(onto_supports([((0.0, 0.0), (1000.0, 0.0), 300.0)], []), [[None, None, None]])

    def test_a_beam_lands_on_the_beam_across_its_end(self):
        from building.beam_ends import onto_supports
        got = onto_supports([((0.0, 0.0), (1000.0, 0.0), 230.0), ((1250.0, -2000.0), (1250.0, 2000.0), 300.0)], [])
        self.assertEqual(got[0][1], [1250.0, 0.0])                   # 100 short: onto its centre line
        self.assertEqual(got[1], [None, None, None])


if __name__ == "__main__":
    unittest.main(verbosity=1)
