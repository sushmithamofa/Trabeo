r"""Tests for the shared building model.

    python building\selftest.py

The key test: every locked baseline's model.json goes into a Building and comes
back out identical - also after the Building is saved to disk and loaded again.
If that holds, the new model can carry everything the working tool produces.
"""
from __future__ import annotations

import copy
import glob
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

from building import Building, MISSING, READ, SUPPORT, support_of     # noqa: E402
from building.readers import legacy_plan                               # noqa: E402

RUNS = sorted(glob.glob(os.path.join(ROOT, "baselines", "*", "run", "model.json")))


def load(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


class RoundTrip(unittest.TestCase):
    """model.json -> Building -> model.json must lose nothing."""

    def test_there_are_baseline_runs_to_check(self):
        self.assertGreaterEqual(len(RUNS), 4, "run tools\\baseline.py capture first")

    def test_every_baseline_comes_back_identical(self):
        for p in RUNS:
            with self.subTest(case=os.path.basename(os.path.dirname(os.path.dirname(p)))):
                d = load(p)
                b = legacy_plan.from_legacy(copy.deepcopy(d), "S1")
                self.assertEqual(legacy_plan.to_legacy(b, "S1"), d)

    def test_identical_after_saving_and_loading(self):
        for p in RUNS[:2]:
            d = load(p)
            b = legacy_plan.from_legacy(copy.deepcopy(d), "S1")
            with tempfile.TemporaryDirectory() as tmp:
                b2 = Building.load(b.save(os.path.join(tmp, "building.json")))
            self.assertEqual(legacy_plan.to_legacy(b2, "S1"), d)

    def test_baseline_snapshot_unchanged_through_the_model(self):
        import baseline
        for p in RUNS:
            case = os.path.basename(os.path.dirname(os.path.dirname(p)))
            exp = load(os.path.join(ROOT, "baselines", case, "expected.json"))
            b = legacy_plan.from_legacy(load(p), "S1")
            self.assertEqual(json.loads(json.dumps(baseline.snapshot(legacy_plan.to_legacy(b, "S1")))),
                             exp["model"], case)

    def test_an_older_model_json_with_fewer_fields_round_trips(self):
        """Extractions from before beam depths and slab sunk were read."""
        d = load(RUNS[0])
        for b in d["beams"]:
            for k in ("depth", "depth_source", "top_mm", "mid"):
                b.pop(k, None)
        d.pop("level_name", None)
        d.pop("folds", None)             # the old tool had no folds, and did not count them
        d["counts"].pop("slab folds", None)
        b = legacy_plan.from_legacy(copy.deepcopy(d), "OLD")
        self.assertEqual(legacy_plan.to_legacy(b, "OLD"), d)

    def test_a_field_the_model_does_not_know_still_travels(self):
        d = load(RUNS[0])
        d["beams"][0]["future_field"] = {"anything": [1, 2]}
        d["something_new"] = 7
        b = legacy_plan.from_legacy(copy.deepcopy(d), "S1")
        self.assertEqual(legacy_plan.to_legacy(b, "S1"), d)


class Evidence(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = load(RUNS[0])
        cls.b = legacy_plan.from_legacy(copy.deepcopy(cls.d), "B_L1", wing="B")

    def test_one_element_per_extracted_item(self):
        self.assertEqual(len(self.b.of("beam")), len(self.d["beams"]))
        self.assertEqual(len(self.b.of("column")), len(self.d["columns"]))
        self.assertEqual(len(self.b.of("slab")), len(self.d["slabs"]))

    def test_every_value_traces_back_to_its_cad_entity(self):
        for el, row in zip(self.b.of("beam"), self.d["beams"]):
            for v in el.props.values():
                self.assertEqual(v.evidence[0].handle, row["handle"])
                self.assertEqual(v.evidence[0].layer, row["source_layer"])
                self.assertEqual(v.evidence[0].sheet, "B_L1")

    def test_where_a_depth_came_from_is_kept(self):
        for el, row in zip(self.b.of("beam"), self.d["beams"]):
            if "depth" in row:
                self.assertEqual(el.props["depth"].evidence[0].source, row.get("depth_source", ""))

    def test_no_depth_is_missing_not_zero(self):
        gone = [e for e in self.b.of("beam") if e.get("depth") is None]
        self.assertTrue(gone, "the B-wing L1 plan has beams the schedule does not cover")
        self.assertTrue(all(e.props["depth"].status == MISSING for e in gone))
        self.assertTrue(all(e.props["depth"].status == READ
                            for e in self.b.of("beam") if e.get("depth")))

    def test_sheet_level_and_wing_carry_onto_elements(self):
        self.assertEqual(self.b.levels, ["Level 1"])
        self.assertTrue(all(e.level == "Level 1" and e.wing == "B" for e in self.b.elements))

    def test_issues_link_to_their_elements_by_handle(self):
        linked = [i for i in self.b.issues if i.elements]
        self.assertTrue(linked)
        ids = {e.id for e in self.b.elements}
        self.assertTrue(all(set(i.elements) <= ids for i in linked))

    def test_two_sheets_in_one_building_stay_apart(self):
        b = legacy_plan.from_legacy(copy.deepcopy(self.d), "L1")
        legacy_plan.from_legacy(load(RUNS[1]), "L2", b)
        self.assertEqual(legacy_plan.to_legacy(b, "L1"), self.d)
        self.assertEqual(legacy_plan.to_legacy(b, "L2"), load(RUNS[1]))
        with self.assertRaises(ValueError):
            legacy_plan.from_legacy(copy.deepcopy(self.d), "L1", b)


class Support(unittest.TestCase):
    def test_every_type_the_reader_makes_is_in_the_table(self):
        types = {s["type"] for s in legacy_plan.SPEC.values()}
        self.assertLessEqual(types, set(SUPPORT))

    def test_reading_and_modelling_are_separate(self):
        self.assertEqual(support_of("rebar")["model"], "no")
        self.assertEqual(support_of("slab_tag")["model"], "n/a")
        self.assertEqual((support_of("stair")["read"], support_of("stair")["model"]),
                         ("partial", "planned"))            # read in stage 6, built in stage 8
        self.assertEqual((support_of("footing")["read"], support_of("footing")["model"]),
                         ("supported", "planned"))          # read in stage 5, built in stage 8
        self.assertEqual(support_of("beam"), {"read": "supported", "model": "supported",
                                              "note": SUPPORT["beam"][2]})

    def test_saved_building_states_support_per_element(self):
        b = legacy_plan.from_legacy(load(RUNS[0]), "S1")
        rows = b.to_dict()["elements"]
        self.assertTrue(all("support" in r for r in rows))


if __name__ == "__main__":
    unittest.main(verbosity=1)
