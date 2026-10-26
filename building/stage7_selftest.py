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


if __name__ == "__main__":
    unittest.main(verbosity=1)
