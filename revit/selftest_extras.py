"""Check the plain-logic part of build_extras.py outside Revit.

    python selftest_extras.py

build_extras.py places footings, steps and rafts and sets up levels in Revit. What
it decides - where each box sits, which levels to create, what to leave alone - is
plain logic, lifted out of the file with ast (as selftest.py does for
build_model.py) and checked here. The Revit calls themselves need Revit.
"""
import ast, io, os, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "build_extras.py")
WANT = ("bottom_in_model", "footing_boxes", "raft_slabs", "plan_levels", "type_name", "pedestal_box", "stair_runs",
        "level_at")
LIFTED = ("collect", "levels", "neighbour_level", "_setp", "set_floor_offset", "set_comment", "set_mark",
          "_concrete_first", "NOT_CAST_CONCRETE", "concrete_material", "_single_layer", "_is_single_layer",
          "floor_type_for", "slab_loop_points", "set_size", "base_symbol", "collect_same_family",
          "activate", "TypeCache", "SIZE_PARAMS",
          "eid", "BEAM_COLOURS", "_solid_fill_id", "_equals_rule", "_filter_views",
          "_apply_colour_filters", "footing_size_key", "make_foundation_size_filters",
          "column_size_groups", "make_column_size_filters", "_as_list")


def load():
    tree = ast.parse(io.open(SRC, encoding="utf-8").read())
    picked = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in WANT]
    assert len(picked) == len(WANT), [n.name for n in picked]
    ns = {}
    exec(compile(ast.Module(body=picked, type_ignores=[]), SRC, "exec"), ns)
    return ns


F = load()


class Plan(unittest.TestCase):
    def test_a_stepped_footing(self):
        """Sasan Gir F7: 3600 x 3900, 450 slab with a 300 step 2100 x 2400 on it,
        founded at -1500 below the GF level at 0."""
        items = [{"type": "footing", "mark": "F7", "x": 10.0, "y": 20.0, "size_x": 3600, "size_y": 3900,
                  "thickness": 450, "bottom_mm": -1500.0},
                 {"type": "step", "mark": "F7", "on": "F7", "x": 10.0, "y": 20.0, "size_x": 2100,
                  "size_y": 2400, "thickness": 300}]
        boxes = F["footing_boxes"](items, 0.0, 0.0)
        self.assertEqual([(b["kind"], b["bottom"], b["top"]) for b in boxes],
                         [("footing", -1500.0, -1050.0), ("step", -1050.0, -750.0)])

    def test_each_footing_at_its_own_founding_level(self):
        """Two footings founded 1200 and 2100 below the datum: never one depth for all."""
        items = [{"type": "footing", "mark": "F1", "x": 0, "y": 0, "size_x": 1200, "size_y": 1200,
                  "thickness": 400, "bottom_mm": -1200.0},
                 {"type": "footing", "mark": "F2", "x": 9000, "y": 0, "size_x": 1500, "size_y": 1500,
                  "thickness": 450, "bottom_mm": -2100.0}]
        self.assertEqual([b["bottom"] for b in F["footing_boxes"](items, 0.0, 0.0)], [-1200.0, -2100.0])

    def test_a_model_on_another_datum(self):
        """GF is +0 in the drawings but stands at +10000 in the model (a site datum):
        a footing founded at -1500 goes 1500 below the GF level, at +8500."""
        items = [{"type": "footing", "mark": "F1", "x": 0, "y": 0, "size_x": 1, "size_y": 1,
                  "thickness": 400, "bottom_mm": -1500.0}]
        self.assertEqual(F["footing_boxes"](items, 10000.0, 0.0)[0]["bottom"], 8500.0)

    def test_no_founding_level_is_not_built(self):
        """No founding level read or decided, or no elevation for the level they hang
        from: nothing is built at an assumed depth."""
        items = [{"type": "footing", "mark": "F1", "x": 0, "y": 0, "size_x": 1, "size_y": 1, "thickness": 400}]
        self.assertEqual(F["footing_boxes"](items, 0.0, 0.0), [])
        items[0]["bottom_mm"] = -1500.0
        self.assertEqual(F["footing_boxes"](items, 0.0, None), [])
        self.assertEqual(F["raft_slabs"]([{"type": "raft", "outline": [[0, 0], [1, 0], [1, 1]],
                                           "thickness": 500}], 0.0, 0.0), [])

    def test_a_step_without_its_footing_is_not_built(self):
        items = [{"type": "step", "mark": "F7", "on": "F7", "x": 0, "y": 0, "size_x": 1, "size_y": 1,
                  "thickness": 300}]
        self.assertEqual(F["footing_boxes"](items, 0.0, 0.0), [])

    def test_a_raft(self):
        r = F["raft_slabs"]([{"type": "raft", "outline": [[0, 0], [1, 0], [1, 1]], "thickness": 500,
                              "bottom_mm": 1500.0}], 3000.0, 3000.0)[0]
        self.assertEqual((r["thickness"], r["top"]), (500.0, 2000.0))

    def test_levels_are_created_never_moved_unless_asked(self):
        entries = [{"revit_name": "GF", "elevation_mm": 0, "status": "read"},
                   {"revit_name": "Level 1", "elevation_mm": 3150, "status": "read"},
                   {"revit_name": "Level 2", "elevation_mm": 6300, "status": "read"},
                   {"revit_name": "TERRACE", "elevation_mm": None, "status": "missing"},
                   {"revit_name": "Level 9", "elevation_mm": 0, "status": "conflict"}]
        existing = {"GF": 0.0, "Level 1": 3000.0, "Level 0": 6300.0}
        got = F["plan_levels"](entries, existing)
        self.assertEqual([(a, n) for a, n, _ in got],
                         [("same", "GF"), ("differs", "Level 1"), ("skip", "Level 2"),
                          ("skip", "TERRACE"), ("skip", "Level 9")])
        moved = F["plan_levels"](entries, existing, True)
        self.assertEqual(moved[1], ("move", "Level 1", (3000.0, 3150.0)))
        made = F["plan_levels"]([{"revit_name": "Level 3", "elevation_mm": 9450, "status": "read"}], existing)
        self.assertEqual(made, [("create", "Level 3", 9450.0)])

    def test_a_floor_with_no_level_of_its_name_hangs_from_the_level_at_its_height(self):
        """Winter Villa: 'Service Floor' at +0, the model's +0 level left from earlier named 'Level 0'."""
        entries = [{"revit_name": "Service Floor", "elevation_mm": 0.0, "status": "override"},
                   {"revit_name": "Ground floor", "elevation_mm": 2800.0, "status": "override"}]
        self.assertEqual(F["level_at"]("Service Floor", entries, {"Level 0": 0.0, "Ground floor": 2800.0}), "Level 0")
        # never another floor of the drawings, never one of two at that height, never an unknown height
        self.assertIsNone(F["level_at"]("Service Floor", entries, {"Ground floor": 0.4}))
        self.assertIsNone(F["level_at"]("Service Floor", entries, {"Level 0": 0.0, "Base": 0.5}))
        self.assertIsNone(F["level_at"]("Basement", entries, {"Level 0": 0.0}))

    def test_a_pedestal_stands_on_its_footing_and_its_step(self):
        items = [{"type": "footing", "mark": "F7", "x": 0, "y": 0, "size_x": 3600, "size_y": 3900, "thickness": 450,
                  "bottom_mm": -1500.0},
                 {"type": "step", "mark": "F7", "on": "F7", "x": 0, "y": 0, "size_x": 2100, "size_y": 2400,
                  "thickness": 300}]
        boxes = F["footing_boxes"](items, 0.0, 0.0)
        p = F["pedestal_box"]({"mark": "PD1", "x": 0, "y": 0, "size_x": 450, "size_y": 450, "on": "F7",
                               "on_xy": [0, 0]}, boxes, 0.0)
        self.assertEqual((p["bottom"], p["top"]), (-750.0, 0.0))
        self.assertIsNone(F["pedestal_box"]({"mark": "PD1", "x": 0, "y": 0, "size_x": 450, "size_y": 450,
                                             "on": "F9", "on_xy": [0, 0]}, boxes, 0.0))

    def test_a_dog_leg_is_chained_and_the_height_shared(self):
        """Sasan Gir: two flights of 10 tread lines side by side, 3000 mm floor to floor."""
        stair = {"runs": [{"start": [0, 0], "end": [0, 2700], "treads": 10, "width": 1500, "going": 300},
                          {"start": [1700, 0], "end": [1700, 2700], "treads": 10, "width": 1500, "going": 300}],
                 "riser": 150}
        runs, h, note = F["stair_runs"](stair, 0.0, 3000.0)
        self.assertAlmostEqual(h, 3000.0 / 22)
        self.assertEqual(runs[1]["start"], [1700, 2700 + 300])        # the second flight comes back
        self.assertEqual(runs[1]["end"], [1700, 0])
        self.assertEqual([r["risers"] for r in runs], [11, 11])
        self.assertAlmostEqual(runs[1]["top"], 3000.0)
        self.assertIn("150 riser", note)                                 # notes and floor height disagree

    def test_a_stair_with_no_going_is_not_built(self):
        """No going (tread) and no width anywhere: not built at an assumed 300 / 1000."""
        stair = {"runs": [{"start": [0, 0], "end": [0, 2700], "treads": 10, "width": 1500, "going": None}]}
        runs, h, note = F["stair_runs"](stair, 0.0, 3000.0)
        self.assertEqual(runs, [])
        self.assertIn("going or width", note)

    def test_type_names(self):
        self.assertEqual(F["type_name"]("footing", 3900, 3600, 450), "Footing 3600x3900x450")
        self.assertEqual(F["type_name"]("step", 2100, 2400, 300), "Footing step 2100x2400x300")


class Helpers(unittest.TestCase):
    def test_the_helpers_it_borrows_from_build_model_are_there(self):
        """build_extras.py loads these from build_model.py at run time; renaming one
        there must fail here, not in Revit."""
        tree = ast.parse(io.open(os.path.join(HERE, "build_model.py"), encoding="utf-8").read())
        have = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))} | \
            {t.id for n in tree.body if isinstance(n, ast.Assign) for t in n.targets if isinstance(t, ast.Name)}
        self.assertEqual(sorted(set(LIFTED) - have), [])
        src = io.open(SRC, encoding="utf-8").read()
        for name in LIFTED:
            self.assertIn('"%s"' % name, src)

    def test_no_syntax_ironpython_cannot_read(self):
        """pyRevit runs IronPython 2.7: no f-strings, no annotations, no walrus."""
        src = io.open(SRC, encoding="utf-8").read()
        tree = ast.parse(src)
        self.assertFalse(any(isinstance(n, ast.JoinedStr) for n in ast.walk(tree)), "f-string")
        self.assertFalse(any(isinstance(n, ast.NamedExpr) for n in ast.walk(tree)), "walrus")
        self.assertNotIn("math.dist", src)                               # Python 3.8+, not IronPython
        self.assertFalse(any(isinstance(n, ast.AnnAssign) or (isinstance(n, ast.FunctionDef) and
                             (n.returns or any(a.annotation for a in n.args.args))) for n in ast.walk(tree)),
                         "annotation")


if __name__ == "__main__":
    unittest.main(verbosity=1)
