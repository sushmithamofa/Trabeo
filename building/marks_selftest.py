r"""Tests for building\marks.py, on text exactly as it appears in the training drawings.

    python -m building.marks_selftest
"""
from __future__ import annotations

import unittest

from building.marks import family_of, labels_in, parse_mark, size_only


def L(text):
    return [(lb.mark.key if lb.mark else None, lb.width, lb.depth, lb.thickness) for lb in labels_in(text)]


class Marks(unittest.TestCase):
    def test_the_same_mark_typed_differently_has_one_key(self):
        for t in ("B12", "B-12", "B 12", "b12"):
            self.assertEqual(parse_mark(t).key, "B12", t)
        self.assertEqual(parse_mark("B4a").key, "B4A")
        self.assertEqual(parse_mark("B55A").key, "B55A")
        self.assertEqual(parse_mark("GB 1").key, "GB1")
        self.assertEqual(parse_mark("Bk1").key, "BK1")
        self.assertEqual(parse_mark("FC2A").key, "FC2A")
        self.assertEqual(parse_mark("STB1").key, "STB1")

    def test_a_floor_number_written_first(self):
        # NIFT: 4B3 is beam B3 of level 4; its family is the prefix's
        for t, key in (("4B3", "4B3"), ("3B3A", "3B3A"), ("12B1", "12B1"), ("2C1", "2C1")):
            m = parse_mark(t)
            self.assertEqual(m.key, key, t)
        self.assertEqual((parse_mark("4B3").floor, parse_mark("4B3").family()), ("4", "beam"))
        self.assertEqual(L("4B9 (300X750)"), [("4B9", 300.0, 750.0, None)])
        self.assertEqual([m.key for m in labels_in("4B1,2")[0].marks], ["4B1", "4B2"])
        # bars, words and sizes still are not marks
        for t in ("2T16", "4D12", "8Y10", "3RD", "1ST", "2B", "10MM", "4NOS", "230X600", "2T12 (230X600)"):
            self.assertIsNone(parse_mark(t), t)
            self.assertEqual(L(t), [], t)

    def test_compound_mark(self):
        m = parse_mark("B107/BP")
        self.assertEqual((m.key, m.also), ("B107", "BP"))

    def test_things_that_are_not_marks(self):
        for t in ("T8 @ 100", "T16-100mm C/C", "M25:Fe415", "M25", "3300 X 3600", "C", "B",
                  "VOID", "STAIRCASE", "T8-150 c/c rest", "8T@350mm C/C EXTRA AT TOP."):
            self.assertIsNone(parse_mark(t), t)
            self.assertEqual(L(t), [], t)

    def test_families(self):
        self.assertEqual(family_of("GB"), "beam")
        self.assertEqual(family_of("STB"), "beam")
        self.assertEqual(family_of("XB"), "beam")          # unknown prefix ending in B
        self.assertEqual(family_of("FC"), "column")
        self.assertEqual(family_of("F"), "footing")
        self.assertEqual(family_of("SW"), "wall")
        self.assertEqual(family_of("S"), "slab")
        self.assertEqual(family_of("D"), "unknown")
        self.assertEqual(family_of("GB", {"GB": "beam"}), "beam")
        self.assertEqual(family_of("PB", {"PB": "footing"}), "footing")   # profile override

    def test_n_for_new_before_an_ordinary_prefix(self):
        """New block: NB beams beside NS slabs - NS1 was taken for a beam's mark."""
        self.assertEqual(family_of("NS"), "slab")
        self.assertEqual(family_of("NC"), "column")
        self.assertEqual(family_of("NB"), "beam")
        self.assertEqual(family_of("NX"), "unknown")
        self.assertEqual(family_of("NS", {"NS": "beam"}), "beam")          # a profile still decides


class Labels(unittest.TestCase):
    def test_mark_with_size_every_way_it_is_written(self):
        self.assertEqual(L("B4a(230X600)"), [("B4A", 230, 600, None)])          # Sasan Gir
        self.assertEqual(L("GB 1(230x450)"), [("GB1", 230, 450, None)])         # Sasan Gir plinth
        self.assertEqual(L("B16 (230X600)"), [("B16", 230, 600, None)])
        self.assertEqual(L("%%UB3a(300X750)".replace("%%U", "")), [("B3A", 300, 750, None)])
        self.assertEqual(L("GB1:230x300"), [("GB1", 230, 300, None)])           # Villa 5
        self.assertEqual(L("GB16(230X500)"), [("GB16", 230, 500, None)])         # Villa 5
        self.assertEqual(L("STB1 (230x600)"), [("STB1", 230, 600, None)])
        # the size after a space, nothing else in the text (blind test, KA style)
        self.assertEqual(L("B01 230x450"), [("B01", 230, 450, None)])
        self.assertEqual(L("GB 2 230 X 600 MM"), [("GB2", 230, 600, None)])
        self.assertEqual(L("PROVIDE B5 230X450 AT GRID 3"), [])                 # a sentence is not a caption

    def test_marks_without_a_number_need_their_size(self):
        self.assertEqual(L("C(230X500)"), [("C", 230, 500, None)])              # Villa 5 columns
        self.assertEqual(L("SB(230X125)"), [("SB", 230, 125, None)])
        self.assertEqual(L("Bn (300X300)"), [("BN", 300, 300, None)])

    def test_two_labels_in_one_text(self):
        self.assertEqual(L("GB2 (230x450)/STB1 (230x600)"),
                         [("GB2", 230, 450, None), ("STB1", 230, 600, None)])

    def test_a_label_inside_a_sentence(self):
        self.assertEqual(L("provided to support GB3 (230x450)"), [("GB3", 230, 450, None)])

    def test_slab_thickness(self):
        self.assertEqual(L("S2(200THK)"), [("S2", None, None, 200)])
        self.assertEqual(L("S7A(200thk)"), [("S7A", None, None, 200)])

    def test_grouped_marks(self):
        lb = labels_in("B-52,54")
        self.assertEqual([m.key for m in lb[0].marks], ["B52", "B54"])
        lb = labels_in("S3,S5")
        self.assertEqual([m.key for m in lb[0].marks], ["S3", "S5"])
        lb = labels_in("B2,4 & 7")
        self.assertEqual([m.key for m in lb[0].marks], ["B2", "B4", "B7"])
        self.assertEqual(labels_in("1, 2, 3"), [])          # numbers alone are not marks

    def test_a_bare_mark(self):
        self.assertEqual(L("B1"), [("B1", None, None, None)])
        self.assertEqual(L("LW1"), [("LW1", None, None, None)])

    def test_size_written_on_its_own(self):
        self.assertEqual(size_only("230X600"), (230.0, 600.0))
        self.assertEqual(size_only("(230 x 450)"), (230.0, 450.0))
        self.assertIsNone(size_only("3300 X 3600 X 600"))
        self.assertIsNone(size_only("B1(230X600)"))

    def test_not_labels(self):
        for t in ("450x450 brick pillar shall be", "172X92X5.4mm", "DECK 3.6M x 1.5M",
                  "M25 : FE415", "T10@ 300MMC/C"):
            self.assertEqual(L(t), [], t)


if __name__ == "__main__":
    unittest.main(verbosity=1)
