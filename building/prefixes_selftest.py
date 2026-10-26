r"""Tests for marks written each consultant's way: prefixes read from the drawings,
marks compared safely, near misses suggested (2026-10-15).

    python -m building.prefixes_selftest
"""
from __future__ import annotations

import os
import sys
import unittest
from collections import Counter
from types import SimpleNamespace

from building import Element, Sheet, Value
from building.linker import _key_of, _near, link, loose_key
from building.linker_selftest import P, V, beam, building, caption, ev
from building.marks import parse_mark
from building.prefixes import decide, decide_all, families_map, layer_family, title_family
from building.profiles.suggest import _layer_facts

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "cad2revit"))
from core.schedule import _key as schedule_key                      # noqa: E402


def slab(b, n, x0, y0, x1, y1, mark="", level="Level 1", sheet=P):
    return b.add(Element(id=f"{sheet}/slab/{n}", type="slab", sheet=sheet, level=level, wing="B", mark=mark,
                         geometry={"outline": [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]},
                         extra={"_layer": "derived from beams"}))


def slab_spec(b, n, mark, thk, level="L1", table="SLAB SCHEDULE", family="slab"):
    return b.add(Element(id=f"T:{n}/spec", type="spec", sheet="T", level=level, wing="B", mark=mark,
                         props={"family": Value(family, "read", 1.0, [ev("table", "T")]),
                                "thickness": Value(thk, "read", 1.0, [ev("table", "T")])},
                         extra={"table": table, "part": ""}))


class MarksComparedSafely(unittest.TestCase):
    def test_s1_and_s_dash_1_were_already_one_mark(self):
        """Checked before anything else was built: both comparisons already did this."""
        for k in ("S1", "S-1", "s 1", "S - 1"):
            self.assertEqual(_key_of(k), "S1", k)
            self.assertEqual(schedule_key(k), "S1", k)
        self.assertEqual(loose_key("S–1"), "S1")          # a long dash, as AutoCAD often types it

    def test_a_separator_between_two_numbers_is_kept(self):
        self.assertEqual(loose_key("S1-2"), "S1-2")
        self.assertEqual(schedule_key("S1-2"), "S1-2")
        self.assertNotEqual(loose_key("S1-2"), loose_key("S12"))
        self.assertNotEqual(schedule_key("S1-2"), schedule_key("S12"))
        self.assertIsNone(parse_mark("S1-2"))                   # never read as S12 either

    def test_leading_zeros_are_kept_and_only_suggested(self):
        self.assertNotEqual(_key_of("S01"), _key_of("S1"))
        self.assertNotEqual(schedule_key("S01"), schedule_key("S1"))
        self.assertTrue(_near("S01", "S1"))
        self.assertTrue(_near("SL1", "S1"))                     # SL is not a usual prefix
        self.assertFalse(_near("B12", "BC12"))                  # two usual prefixes: two beams
        self.assertFalse(_near("S1", "S2"))


class WeightedEvidence(unittest.TestCase):
    def test_a_schedule_title_teaches_an_unknown_prefix(self):
        d = decide("SL", {"schedule": Counter(slab=6)})
        self.assertEqual((d["family"], d["status"]), ("slab", "learned"))

    def test_the_schedule_outweighs_two_weaker_sources_together(self):
        """Weighted, not equal votes: the slab schedule (5) beats a layer name (2) and a hint (2)."""
        d = decide("XQ", {"schedule": Counter(slab=4), "layer": Counter(beam=4)}, hint="beam")
        self.assertEqual(d["family"], "slab")

    def test_a_usual_prefix_is_never_reread_by_itself_only_sent_to_review(self):
        """Measured on 36 plans: re-reading usual prefixes (LW, SC, NC) broke what worked."""
        d = decide("SB", {"schedule": Counter(slab=3)})
        self.assertEqual((d["family"], d["status"], d.get("suggested")), ("beam", "check", "slab"))
        d = decide("SB", {"layer": Counter(slab=3)})            # a layer name alone: nothing to ask
        self.assertEqual((d["family"], d["status"]), ("beam", "agreed"))
        d = decide("LW", {"schedule": Counter(column=8), "position": Counter(column=14)})
        self.assertEqual((d["family"], d["status"]), ("wall", "agreed"))   # lift walls in a column schedule
        d = decide("NC", {"schedule": Counter(beam=4)})         # N + a usual prefix is usual too
        self.assertEqual((d["family"], d["status"]), ("column", "check"))

    def test_a_guessed_prefix_can_be_learned(self):
        d = decide("SLAB", {"schedule": Counter(slab=1)})       # 'ends in B' was only a guess
        self.assertEqual((d["family"], d["status"]), ("slab", "learned"))

    def test_a_close_call_goes_to_review_with_the_usual_meaning_kept(self):
        d = decide("S", {"schedule": Counter(slab=1, beam=1)})
        self.assertEqual((d["family"], d["status"]), ("slab", "agreed"))   # usual prefix: as before
        d = decide("QX", {"schedule": Counter(slab=1, beam=1)})
        self.assertEqual((d["family"], d["status"]), ("unknown", "close"))

    def test_a_saved_hint_is_used_when_the_drawing_is_silent_and_loses_when_it_disagrees(self):
        self.assertEqual(decide("P", {}, hint="slab")["family"], "slab")
        d = decide("P", {"schedule": Counter(beam=5)}, hint="slab")
        self.assertEqual(d["family"], "beam")                    # the drawing wins

    def test_a_decision_in_review_is_final(self):
        d = decide("SB", {"schedule": Counter(beam=9)}, decided="slab")
        self.assertEqual((d["family"], d["status"]), ("slab", "decided"))

    def test_usual_prefixes_with_nothing_said_keep_their_meaning(self):
        got = decide_all({"B": {}, "S": {"schedule": Counter(slab=3)}})
        self.assertEqual(families_map(got), {})                 # nothing differs from the built-in list

    def test_titles_and_layers(self):
        self.assertEqual(title_family("SCHEDULE OF SLABS"), "slab")
        self.assertEqual(title_family("BEAM & SLAB DETAILS"), "")   # two families: says nothing
        self.assertEqual(layer_family("SLAB NO"), "slab")
        self.assertEqual(layer_family("beam sch. text"), "")


class LayersFoundByTheSetsOwnPrefixes(unittest.TestCase):
    def test_a_layer_of_sl_marks_counts_as_slab_marks(self):
        texts = [SimpleNamespace(layer="TXT-2", text=f"SL{i}") for i in range(1, 7)]
        before = _layer_facts([], texts)["TXT-2"]["fam"]
        after = _layer_facts([], texts, {"SL": "slab"})["TXT-2"]["fam"]
        self.assertEqual(before, Counter(unknown=6))             # unchanged without the set's meanings
        self.assertEqual(after, Counter(slab=6))


class Linked(unittest.TestCase):
    def test_an_unknown_slab_prefix_from_the_slab_schedule_finds_its_bays(self):
        b = building()
        bays = [slab(b, i, i * 4000, 0, i * 4000 + 3800, 3800) for i in range(3)]
        for i in range(3):
            caption(b, i, (i * 4000 + 1900, 1900), f"SL{i + 1}", family="unknown")
            slab_spec(b, i, f"SL{i + 1}", 125.0 + 25 * i)
        link(b)
        self.assertEqual([s.final("thickness") for s in bays], [125.0, 150.0, 175.0])
        self.assertEqual([s.final("mark") for s in bays], ["SL1", "SL2", "SL3"])
        self.assertTrue(any(i.category == "mark prefix" and i.mark == "SL" for i in b.issues))

    def test_a_marks_size_is_taken_from_its_own_floor_only(self):
        b = building()
        b.add_sheet(Sheet(id="DWG:V2", file="y.dwg", kind="framing_plan", level="L2", wing="B",
                          reader="sheets.registry"))
        b.add_sheet(Sheet(id="DWG:V2/plan", file="y.dwg", kind="framing_plan", level="Level 2", wing="B",
                          reader="plan.legacy", extra={"view_sheet": "DWG:V2", "level_key": "L2"}))
        one = slab(b, 0, 0, 0, 3800, 3800, mark="S1")
        two = slab(b, 1, 0, 0, 3800, 3800, mark="S1", level="Level 2", sheet="DWG:V2/plan")
        slab_spec(b, 0, "S1", 150.0, level="L1")                # the 1st floor's slab schedule
        link(b)
        self.assertEqual(one.final("thickness"), 150.0)
        self.assertIsNone(two.final("thickness"))               # not the 1st floor's S1

    def test_a_near_miss_is_suggested_never_matched(self):
        b = building()
        bay = slab(b, 0, 0, 0, 3800, 3800, mark="S01")
        slab_spec(b, 0, "S1", 150.0)
        link(b)
        self.assertIsNone(bay.final("thickness"))
        hits = [i for i in b.issues if i.category == "did you mean"]
        self.assertEqual(len(hits), 1)
        self.assertIn("S1", hits[0].message)

    def test_linking_twice_gives_the_same(self):
        b = building()
        caption(b, 0, (1900, 1900), "SL1", family="unknown")
        slab(b, 0, 0, 0, 3800, 3800)
        slab_spec(b, 0, "SL1", 125.0)
        link(b)
        first = [(e.id, e.get("family")) for e in b.elements if e.type in ("caption", "spec")]
        link(b)
        self.assertEqual(first, [(e.id, e.get("family")) for e in b.elements if e.type in ("caption", "spec")])
        cap = b.of("caption")[0]
        self.assertEqual((cap.get("family"), cap.extra.get("family_read")), ("slab", "unknown"))

    def test_beams_with_usual_marks_are_untouched(self):
        b = building()
        along = beam(b, 0, (0, 0), (6000, 0))
        caption(b, 0, (3150, 200), "B7", 230, 600)
        link(b)
        self.assertEqual(along.final("mark"), "B7")
        self.assertFalse([i for i in b.issues if i.category in ("mark prefix", "did you mean")])


if __name__ == "__main__":
    unittest.main(verbosity=1)
