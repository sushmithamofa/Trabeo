"""Check the part of build_model.py that decides what to DELETE.

    python selftest.py

build_model.py can only run inside Revit, but the code that chooses which
elements to remove is plain logic, and getting it wrong means deleting someone's
model. So those functions are lifted out of the file with ast and run against a
stubbed Revit API, where a wrong deletion is a failing test rather than a lost
afternoon.
"""
import ast, io, json, os, shutil, sys, tempfile, unittest

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "build_model.py")
WANT = ("write_placed", "delete_previous", "doc_key", "doc_id", "_is_saved", "eid", "remember",
        "_belongs_to_floor",
        "column_rotation_deg", "wall_runs",
        "floor_number", "level_for_floor", "is_roof", "pick_level",
        "slab_loop_points", "set_floor_offset",
        "sunk_depths", "sunk_filter_name", "footing_size_key", "column_size_groups",
        "section_points", "slope_z_mm", "profile_points", "level_at", "earlier_records", "drawing_levels",
        "wall_bases", "_concrete_first", "lean_points", "slant_angle")
# module-level constants the lifted functions rely on. These are taken from the
# file too, never supplied by the test: a test that hands in its own ROT_OFFSET
# passes happily while the shipped one is wrong, which is exactly the bug the
# rotation tests exist to catch.
WANT_CONSTS = ("ROT_OFFSET", "REMOVED", "FLOOR_WORDS", "ROOF_WORDS", "LEVEL_USED",
               "LEGACY_OTHER", "NOT_A_SHEAR_WALL", "NOT_CAST_CONCRETE")


def load_funcs(ns):
    tree = ast.parse(io.open(SRC, encoding="utf-8").read())
    picked = [n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name in WANT]
    assert len(picked) == len(WANT), [n.name for n in picked]

    consts = [n for n in tree.body
              if isinstance(n, ast.Assign)
              and any(isinstance(t, ast.Name) and t.id in WANT_CONSTS
                      for t in n.targets)]
    assert len(consts) == len(WANT_CONSTS), "missing a constant the tests need"

    mod = ast.Module(body=consts + picked, type_ignores=[])
    exec(compile(mod, SRC, "exec"), ns)
    return ns


class FakeId(object):
    def __init__(self, n):
        self.Value = int(n)

    def __eq__(self, o):
        return isinstance(o, FakeId) and o.Value == self.Value

    def __hash__(self):
        return hash(self.Value)


class FakeEl(object):
    """An element: its id number and a UniqueId that, unlike the number, is
    never handed to another element."""
    def __init__(self, n, uid=None):
        self.Id = FakeId(n)
        self.UniqueId = uid or "uid-%d" % int(n)


class FakeDoc(object):
    def __init__(self, path, ids):
        self.PathName = path
        self.Title = os.path.basename(path)
        self.elements = {int(i): FakeEl(i) for i in ids}
        self.deleted = None

    def add(self, n, uid=None):
        self.elements[int(n)] = FakeEl(n, uid)

    def GetElement(self, i):
        if isinstance(i, str):
            return next((e for e in self.elements.values() if e.UniqueId == i), None)
        return self.elements.get(i.Value)

    def Delete(self, coll):
        self.deleted = [i.Value for i in coll]
        for v in self.deleted:
            self.elements.pop(v, None)
        return self.deleted


class _FakeListMeta(type):
    def __getitem__(cls, _t):
        return cls


class FakeList(object, metaclass=_FakeListMeta):
    """Stands in for System.Collections.Generic.List[ElementId]."""
    def __init__(self, *_a):
        self.items = []

    def Add(self, x):
        self.items.append(x)

    def __iter__(self):
        return iter(self.items)

    def __len__(self):
        return len(self.items)


class FakeSystem(object):
    class Collections(object):
        class Generic(object):
            List = FakeList


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="placed_")
        self.placed_file = os.path.join(self.tmp, "placed.json")
        self.said = []
        sys.modules["System"] = FakeSystem
        sys.modules["System.Collections"] = FakeSystem.Collections
        sys.modules["System.Collections.Generic"] = FakeSystem.Collections.Generic

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make(self, doc, placed=(), record=None):
        for i in placed:                     # what a run placed is in the model
            if int(i) not in doc.elements:
                doc.add(i)
        ns = {"os": os, "json": json, "ElementId": FakeId, "doc": doc,
              "say": self.said.append, "PLACED": list(placed),
              "PLACED_UID": [doc.elements[int(i)].UniqueId for i in placed],
              "PLACED_FILE": self.placed_file,
              "MODEL_JSON": os.path.join(self.tmp, "model.json")}
        load_funcs(ns)
        ns["REMOVED"] = []
        if record is not None:
            with open(self.placed_file, "w") as f:
                json.dump(record, f)
        return ns

    # ---------------------------------------------------------------- write
    def test_it_records_what_it_placed(self):
        doc = FakeDoc(r"C:\jobs\a.rvt", [])
        ns = self.make(doc, placed=[11, 22, 33])
        ns["write_placed"]()
        with open(self.placed_file) as f:
            rec = json.load(f)
        self.assertEqual(rec["ids"], [11, 22, 33])
        self.assertEqual(rec["document"], r"C:\jobs\a.rvt")

    # ---------------------------------------------------------------- delete
    def test_it_deletes_exactly_what_it_placed(self):
        doc = FakeDoc(r"C:\jobs\a.rvt", [11, 22, 33, 44])
        ns = self.make(doc, record={"document": r"C:\jobs\a.rvt",
                                    "ids": [11, 22, 33], "when": "yesterday"})
        n = ns["delete_previous"]()
        self.assertEqual(n, 3)
        self.assertEqual(sorted(doc.deleted), [11, 22, 33])
        self.assertIn(44, doc.elements, "an element it never placed was deleted")

    def test_it_refuses_a_different_document(self):
        doc = FakeDoc(r"C:\jobs\OTHER.rvt", [11, 22, 33])
        ns = self.make(doc, record={"document": r"C:\jobs\a.rvt",
                                    "ids": [11, 22, 33], "when": "yesterday"})
        n = ns["delete_previous"]()
        self.assertEqual(n, 0)
        self.assertIsNone(doc.deleted, "it deleted from the wrong model")
        self.assertTrue(any("different model" in m for m in self.said))

    def test_it_skips_ids_that_are_already_gone(self):
        doc = FakeDoc(r"C:\jobs\a.rvt", [22])          # 11 and 33 deleted by hand
        ns = self.make(doc, record={"document": r"C:\jobs\a.rvt",
                                    "ids": [11, 22, 33], "when": "yesterday"})
        n = ns["delete_previous"]()
        self.assertEqual(n, 1)
        self.assertEqual(doc.deleted, [22])

    def test_no_record_means_no_deletion(self):
        doc = FakeDoc(r"C:\jobs\a.rvt", [11, 22])
        ns = self.make(doc)                             # no placed.json at all
        n = ns["delete_previous"]()
        self.assertEqual(n, 0)
        self.assertIsNone(doc.deleted)
        self.assertTrue(any("no placed.json" in m for m in self.said))

    # ------------------------------------------------------- accumulation
    def test_the_record_adds_to_what_is_already_there(self):
        """An update run places a handful; the record must keep the rest.

        Writing only this run's ids throws away the record of the hundreds
        already in the model, after which nothing can remove them. That happened
        for real: a run that added 9 beams reduced a record of 238 elements to 9.
        """
        doc = FakeDoc(r"C:\jobs\a.rvt", list(range(1, 239)))
        ns = self.make(doc, placed=[901, 902],
                       record={"document": r"C:\jobs\a.rvt",
                               "ids": list(range(1, 239)), "when": "earlier"})
        n = ns["write_placed"]()
        self.assertEqual(n, 240)
        with open(self.placed_file) as f:
            rec = json.load(f)
        self.assertEqual(len(rec["ids"]), 240)
        self.assertIn(1, rec["ids"], "the earlier build was forgotten")
        self.assertIn(902, rec["ids"], "this run was not recorded")

    def test_the_record_does_not_repeat_an_id(self):
        doc = FakeDoc(r"C:\jobs\a.rvt", [5, 7])
        ns = self.make(doc, placed=[5, 6],
                       record={"document": r"C:\jobs\a.rvt", "ids": [5, 7],
                               "when": "earlier"})
        ns["write_placed"]()
        with open(self.placed_file) as f:
            rec = json.load(f)
        self.assertEqual(sorted(rec["ids"]), [5, 6, 7])

    def test_what_a_replace_removed_drops_out_of_the_record(self):
        doc = FakeDoc(r"C:\jobs\a.rvt", [])
        ns = self.make(doc, placed=[50, 51],
                       record={"document": r"C:\jobs\a.rvt", "ids": [1, 2, 3],
                               "when": "earlier"})
        ns["REMOVED"].extend([1, 2, 3])
        ns["write_placed"]()
        with open(self.placed_file) as f:
            rec = json.load(f)
        self.assertEqual(sorted(rec["ids"]), [50, 51],
                         "deleted elements were left in the record")

    def test_a_record_from_another_model_is_not_merged_in(self):
        doc = FakeDoc(r"C:\jobs\a.rvt", [])
        ns = self.make(doc, placed=[9],
                       record={"document": r"C:\jobs\OTHER.rvt", "ids": [1, 2],
                               "when": "earlier"})
        ns["write_placed"]()
        with open(self.placed_file) as f:
            rec = json.load(f)
        self.assertEqual(rec["ids"], [9])
        self.assertEqual(rec["document"], r"C:\jobs\a.rvt")

    def test_a_replace_round_trip_leaves_the_record_right(self):
        """Delete the previous build, place a new one, record only the new one."""
        doc = FakeDoc(r"C:\jobs\a.rvt", [11, 22, 33])
        ns = self.make(doc, placed=[],
                       record={"document": r"C:\jobs\a.rvt", "ids": [11, 22, 33],
                               "when": "earlier"})
        ns["delete_previous"]()
        for i in (44, 55):
            doc.add(i)
            ns["PLACED"].append(i)
            ns["PLACED_UID"].append(doc.elements[i].UniqueId)
        ns["write_placed"]()
        with open(self.placed_file) as f:
            rec = json.load(f)
        self.assertEqual(sorted(rec["ids"]), [44, 55])

    def test_a_corrupt_record_deletes_nothing(self):
        doc = FakeDoc(r"C:\jobs\a.rvt", [11, 22])
        ns = self.make(doc)
        with open(self.placed_file, "w") as f:
            f.write("{not json")
        n = ns["delete_previous"]()
        self.assertEqual(n, 0)
        self.assertIsNone(doc.deleted)

    # ------------------------------------------- ids reused by another floor
    def test_the_new_record_deletes_by_unique_id(self):
        """An undo can hand an old id number to a new element - the other floor's
        beam. The new record names elements by UniqueId, which is never reused."""
        doc = FakeDoc(r"C:\jobs\a.rvt", [11, 22])
        doc.add(33, uid="OTHER-FLOOR-BEAM")          # number 33, a different element
        ns = self.make(doc, record={"document": r"C:\jobs\a.rvt", "when": "earlier",
                                    "ids": [11, 22, 33],
                                    "uids": ["uid-11", "uid-22", "uid-33"]})
        n = ns["delete_previous"]()
        self.assertEqual(n, 2)
        self.assertEqual(sorted(doc.deleted), [11, 22])
        self.assertIn(33, doc.elements, "the other floor's element was deleted")

    def test_an_old_record_leaves_another_floors_element_alone(self):
        doc = FakeDoc(r"C:\jobs\a.rvt", [11, 22, 33])
        ns = self.make(doc, record={"document": r"C:\jobs\a.rvt", "when": "earlier",
                                    "ids": [11, 22, 33]})
        other = doc.elements[33]
        ns["_belongs_to_floor"] = lambda el, level, fc: el is not other
        n = ns["delete_previous"]("Level 1", [])
        self.assertEqual(n, 2)
        self.assertIn(33, doc.elements, "the other floor's element was deleted")
        self.assertTrue(any("LEFT ALONE" in m for m in self.said))
        ns["write_placed"]()
        with open(self.placed_file) as f:
            rec = json.load(f)
        self.assertNotIn("uid-33", rec["uids"], "the other floor's element was taken into this record")

    def test_an_old_record_is_rewritten_by_unique_id(self):
        doc = FakeDoc(r"C:\jobs\a.rvt", [1, 2])
        ns = self.make(doc, placed=[7], record={"document": r"C:\jobs\a.rvt",
                                                "when": "earlier", "ids": [1, 2, 3]})
        ns["write_placed"]()
        with open(self.placed_file) as f:
            rec = json.load(f)
        self.assertEqual(sorted(rec["uids"]), ["uid-1", "uid-2", "uid-7"])
        self.assertEqual(sorted(rec["ids"]), [1, 2, 7], "3 is gone and must drop out")

    # ------------------------------- a new unsaved 'Project1' in a new session
    def unsaved(self, ids, fingerprint):
        doc = FakeDoc("", ids)
        doc.Title = "Project1"
        doc.ProjectInformation = FakeEl(1, uid=fingerprint)
        return doc

    def test_last_sessions_record_deletes_nothing_in_a_new_project1(self):
        """First bake of a floor in a new session: the record names last session's
        Project1. Its numbers now belong to the floor baked just before."""
        doc = self.unsaved([11, 22, 33], "THIS-SESSION")
        ns = self.make(doc, record={"document": "Project1", "doc_id": "LAST-SESSION",
                                    "when": "yesterday", "ids": [11, 22, 33],
                                    "uids": ["uid-11", "uid-22", "uid-33"]})
        n = ns["delete_previous"]()
        self.assertEqual(n, 0)
        self.assertIsNone(doc.deleted, "the other floor was deleted")

    def test_an_old_unsaved_record_deletes_nothing(self):
        doc = self.unsaved([11, 22, 33], "THIS-SESSION")
        ns = self.make(doc, record={"document": "Project1", "when": "yesterday",
                                    "ids": [11, 22, 33]})
        n = ns["delete_previous"]()
        self.assertEqual(n, 0)
        self.assertIsNone(doc.deleted, "the other floor was deleted")
        ns["write_placed"]()
        with open(self.placed_file) as f:
            rec = json.load(f)
        self.assertEqual(rec["uids"], [], "the other floor was taken into this record")
        self.assertEqual(rec["doc_id"], "THIS-SESSION")

    def test_the_same_project1_still_replaces_its_own(self):
        doc = self.unsaved([11, 22, 33], "THIS-SESSION")
        ns = self.make(doc, record={"document": "Project1", "doc_id": "THIS-SESSION",
                                    "when": "earlier", "ids": [11, 22],
                                    "uids": ["uid-11", "uid-22"]})
        n = ns["delete_previous"]()
        self.assertEqual(n, 2)
        self.assertEqual(sorted(doc.deleted), [11, 22])
        self.assertIn(33, doc.elements)


class Rotation(unittest.TestCase):
    """A column has to end up pointing the way the drawing points it.

    model.json gives the direction of the column LONG side. A rectangular column
    family already has its long side up the Y axis, so the instance has to be
    turned by the reported angle LESS the 90 degrees the family starts with.
    Feeding the reported angle straight in turns every column a further quarter
    turn, which is how Wing B came out with 26 of its 34 columns on their side.
    """

    def setUp(self):
        self.ns = load_funcs({"os": os, "json": json})
        self.turn = self.ns["column_rotation_deg"]

    def test_a_column_along_y_needs_no_turn(self):
        self.assertAlmostEqual(self.turn(90.0), 0.0, places=6)

    def test_a_column_along_x_is_a_quarter_turn(self):
        self.assertAlmostEqual(abs(self.turn(0.0)), 90.0, places=6)

    def test_the_wing_b_case(self):
        """92 degrees in the plan is 2 degrees of instance rotation, not 92."""
        self.assertAlmostEqual(self.turn(92.0), 2.0, places=6)

    def test_the_other_wing_b_case(self):
        """2 degrees in the plan is a quarter turn back, not 2."""
        self.assertAlmostEqual(self.turn(2.0), -88.0, places=6)

    def test_the_answer_stays_small(self):
        """A rectangle is the same after half a turn, so keep the angle small."""
        for rot in range(0, 360, 7):
            self.assertLessEqual(abs(self.turn(float(rot))), 90.0 + 1e-9)

    def test_half_a_turn_makes_no_difference(self):
        for rot in (0.0, 17.0, 92.0, 133.0):
            a = self.turn(rot)
            b = self.turn(rot + 180.0)
            self.assertAlmostEqual((a - b) % 180.0, 0.0, places=6)

    def test_a_missing_angle_is_treated_as_zero(self):
        self.assertAlmostEqual(self.turn(None), self.turn(0.0), places=6)

    def test_the_offset_is_honoured(self):
        """If the family turns out to start along X, the offset changes."""
        self.assertAlmostEqual(self.turn(92.0, offset=0.0), -88.0, places=6)


class WallRuns(unittest.TestCase):
    """A lift shaft drawn as one outline has to become straight walls.

    Wing B's LW1/LW2 is a single E-shaped outline. Left out, beams B51, B175 and
    B176 were built with nothing under them.
    """

    LIFT = [[2901163.2, 85043.4], [2898983.3, 85055.4], [2898982.0, 84825.4],
            [2901162.0, 84813.4], [2901151.8, 82963.5], [2898971.8, 82975.5],
            [2898970.6, 82745.5], [2901380.5, 82732.2], [2901404.7, 87122.1],
            [2898994.7, 87135.4], [2898993.5, 86905.4], [2901173.4, 86893.4]]

    def setUp(self):
        self.runs = load_funcs({"os": os, "json": json})["wall_runs"](self.LIFT)

    def test_three_arms_and_one_spine(self):
        self.assertEqual(len(self.runs), 4)
        for s, e, t in self.runs:
            self.assertAlmostEqual(t, 230.0, delta=5.0)

    def test_the_spine_runs_the_full_height(self):
        spine = [r for r in self.runs if abs(r[0][1] - r[1][1]) > 3000]
        self.assertEqual(len(spine), 1)
        s, e, _ = spine[0]
        self.assertAlmostEqual(min(s[1], e[1]), 82850, delta=30)
        self.assertAlmostEqual(max(s[1], e[1]), 87010, delta=30)

    def test_the_arms_meet_the_spine_on_its_centreline(self):
        spine = [r for r in self.runs if abs(r[0][1] - r[1][1]) > 3000][0]
        sx = (spine[0][0] + spine[1][0]) / 2.0
        for s, e, _ in self.runs:
            if s is spine[0]:
                continue
            self.assertAlmostEqual(max(s[0], e[0]), sx, delta=40)


class FloorLevel(unittest.TestCase):
    """A 1st floor plan is framed at Level 1, so its columns stand on Level 0.

    Building it on the lowest level instead put the columns 3 m underground.
    """

    class L(object):
        def __init__(self, name):
            self.Name = name

    def setUp(self):
        ns = load_funcs({"os": os, "json": json})
        self.floor, self.level = ns["floor_number"], ns["level_for_floor"]

    def test_the_floor_is_read_from_the_file_name(self):
        self.assertEqual(self.floor("C:/x/1153 - 1ST FLOOR PLAN ( B WING ) (6).dxf"), 1)
        self.assertEqual(self.floor("1153 - 3RD FLOOR PLAN.dwg"), 3)
        self.assertEqual(self.floor("GROUND FLOOR PLAN.dxf"), 0)
        self.assertIsNone(self.floor("typical plan.dxf"))

    def test_the_level_is_found_by_its_number(self):
        lv = [self.L("Level 0"), self.L("Level 1"), self.L("Level 2")]
        self.assertEqual(self.level(1, lv).Name, "Level 1")
        self.assertIsNone(self.level(5, lv))
        self.assertEqual(self.level(1, [self.L("Ground Floor"), self.L("First Floor")]).Name,
                         "First Floor")


    def pick(self, drawing, names, name=None):
        ns = load_funcs({"os": os, "json": json})
        lv = [self.L(n) for n in names]
        ns["levels"] = lambda: lv
        ns["say"] = lambda s: None
        got = ns["pick_level"](name, drawing)
        return got.Name if got is not None else None

    def test_a_roof_plan_goes_on_the_roof_level(self):
        names = ["Level 0", "Level 1", "Level 2", "Terrace"]
        self.assertEqual(self.pick("1153 - TERRACE FLOOR PLAN.dxf", names), "Terrace")
        self.assertEqual(self.pick("1153 - ROOF BEAM LAYOUT.dxf", names), "Terrace")
        self.assertEqual(self.pick("1153 - 2ND FLOOR PLAN.dxf", names), "Level 2")

    def test_no_matching_level_stops_rather_than_building_on_level_0(self):
        # With several floors in one model, the old fallback to the lowest level
        # built the 2nd floor straight through the 1st.
        names = ["Level 0", "Level 1"]
        self.assertIsNone(self.pick("1153 - 2ND FLOOR PLAN.dxf", names))
        self.assertIsNone(self.pick("1153 - TERRACE PLAN.dxf", names))
        self.assertIsNone(self.pick("typical plan.dxf", names))
        self.assertIsNone(self.pick("1ST FLOOR PLAN.dxf", names, name="Level 9"))
        self.assertEqual(self.pick("typical plan.dxf", names, name="level 1"), "Level 1")



class TestSlabOutline(unittest.TestCase):
    """A floor's boundary must close. The old loop skipped a near-zero edge while
    drawing it, which leaves a gap Revit rejects the whole floor for."""

    def setUp(self):
        self.pts = load_funcs({"os": os, "json": json})["slab_loop_points"]

    def test_a_repeated_closing_point_is_dropped(self):
        sq = [(0, 0), (1000, 0), (1000, 1000), (0, 1000), (0, 0)]
        self.assertEqual(self.pts(sq), [(0, 0), (1000, 0), (1000, 1000), (0, 1000)])

    def test_a_tiny_edge_is_merged_not_left_as_a_gap(self):
        got = self.pts([(0, 0), (1000, 0), (1000.4, 0.3), (1000, 1000), (0, 1000)])
        self.assertEqual(len(got), 4)

    def test_too_few_points_is_no_floor(self):
        self.assertEqual(self.pts([(0, 0), (1000, 0), (0, 0)]), [])
        self.assertEqual(self.pts(None), [])



class TestSunkSlabOffset(unittest.TestCase):
    """The sunk depth has to reach Revit. Set once and trusted, every sunk slab
    on Wing B 2nd floor stayed flush and the log said nothing."""

    class P(object):
        def __init__(self, locked=False, sticks=True):
            self.IsReadOnly, self.sticks, self.v = locked, sticks, 0.0

        def Set(self, v):
            if self.sticks:
                self.v = v

        def AsDouble(self):
            return self.v

    class Floor(object):
        def __init__(self, by_id, by_name=None):
            self.by_id, self.by_name = by_id, by_name

        def get_Parameter(self, bip):
            return self.by_id

        def LookupParameter(self, name):
            return self.by_name

    def offset(self, floor, unlock_on_regen=None):
        class Doc(object):
            def Regenerate(self_inner):
                if unlock_on_regen is not None:
                    unlock_on_regen.IsReadOnly = False
        class BIP(object):
            FLOOR_HEIGHTABOVELEVEL_PARAM = 1
        ns = load_funcs({"os": os, "json": json})
        ns["doc"] = Doc()
        ns["BuiltInParameter"] = BIP
        return ns["set_floor_offset"](floor, -0.82)

    def test_it_sets_and_checks(self):
        p = self.P()
        self.assertTrue(self.offset(self.Floor(p)))
        self.assertEqual(p.v, -0.82)

    def test_a_locked_parameter_is_tried_again_after_regenerating(self):
        p = self.P(locked=True)
        self.assertTrue(self.offset(self.Floor(p), unlock_on_regen=p))

    def test_found_by_name_when_the_id_gives_nothing(self):
        p = self.P()
        self.assertTrue(self.offset(self.Floor(None, p)))

    def test_a_value_that_does_not_stick_is_reported(self):
        self.assertFalse(self.offset(self.Floor(self.P(sticks=False))))


class TestDrawnSection(unittest.TestCase):
    """A beam built to its drawn section (SECTION X-X): u is to the LEFT of start -> end."""

    def test_the_profile_stands_across_the_beam_left_of_its_direction(self):
        ns = load_funcs({"os": os, "json": json})
        prof = [[-375.0, -600.0], [-375.0, -500.0], [0.0, 0.0], [375.0, 0.0], [375.0, -600.0]]
        east = ns["section_points"](prof, (0.0, 0.0), (5000.0, 0.0))        # running east: left is north
        self.assertEqual([tuple(round(c, 6) for c in p) for p in east][:2], [(0.0, -375.0, -600.0), (0.0, -375.0, -500.0)])
        self.assertAlmostEqual(east[3][1], 375.0)
        north = ns["section_points"](prof, (0.0, 0.0), (0.0, 5000.0))       # running north: left is west
        self.assertAlmostEqual(north[3][0], -375.0)
        self.assertAlmostEqual(north[3][2], 0.0)


class TestShearWallType(unittest.TestCase):
    """The template's 'Wall-Fnd_300Con_Footing' had been given to every 300 mm shear wall."""

    def test_a_shear_wall_type_is_made_from_a_concrete_wall_never_a_footing_first(self):
        ns = load_funcs({"os": os, "json": json})
        names =["Wall-Fnd_300Con_Footing", "Generic - 200mm", "Wall-Ext_102Bwk-75Ins-100LBlk", "Wall-Int_RCC 230"]
        got = ns["wall_bases"](names)
        self.assertEqual(got[:2], ["Wall-Int_RCC 230", "Generic - 200mm"])
        self.assertEqual(set(got[2:]), {"Wall-Fnd_300Con_Footing", "Wall-Ext_102Bwk-75Ins-100LBlk"})


class TestConcreteMaterial(unittest.TestCase):
    """Winter Villa's template: 'Concrete Masonry Units' gave the shear walls a block pattern."""

    def test_cast_in_place_concrete_first_masonry_last(self):
        ns = load_funcs({"os": os, "json": json})
        names = ["Concrete Masonry Units _High Density", "Concrete, Precast", "Generic", "Concrete, Cast-in-Place gray",
                 "Wood - Sheathing"]
        got = ns["_concrete_first"](names, lambda n: n)
        self.assertEqual(got[0], "Concrete, Cast-in-Place gray")
        self.assertEqual(got[1], "Generic")
        self.assertEqual(set(got[2:]), {"Concrete Masonry Units _High Density", "Concrete, Precast", "Wood - Sheathing"})


class TestEarlierRuns(unittest.TestCase):
    """Each Analyse makes a new run folder: a floor's previous build is recorded in another one."""

    def test_this_floors_records_from_the_other_runs_newest_first(self):
        ns = load_funcs({"os": os, "json": json})
        root = tempfile.mkdtemp()
        try:
            def rec(run, floor):
                d = os.path.join(root, "runs", run, "revit_build", floor)
                os.makedirs(d)
                p = os.path.join(d, "placed.json")
                io.open(p, "w").write(u"{}")
                return p
            old, mine, other_floor = rec("2026-10-28_1300", "M8_plan"), rec("2026-10-28_1400", "M8_plan"),                 rec("2026-10-28_1200", "M6_plan")
            newer = rec("2026-10-28_1430", "M8_plan")
            got = ns["earlier_records"](mine)
            self.assertEqual([os.path.normcase(p) for p in got], [os.path.normcase(newer), os.path.normcase(old)])
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_a_floor_builds_on_the_level_at_its_height_when_none_has_its_name(self):
        ns = load_funcs({"os": os, "json": json})
        entries = [{"revit_name": "Service Floor", "elevation_mm": 0.0, "status": "override"},
                   {"revit_name": "Ground floor", "elevation_mm": 2800.0, "status": "override"}]
        self.assertEqual(ns["level_at"]("Service Floor", entries, {"Level 0": 0.0, "Ground floor": 2800.0}), "Level 0")
        self.assertIsNone(ns["level_at"]("Service Floor", entries, {"Ground floor": 0.0}))


class TestProfileWall(unittest.TestCase):
    """A wall built to the shape its elevation draws (SW15 leaning): [along, up] laid from the
    origin along dir, up from the level below."""

    def test_the_profile_is_laid_along_the_wall(self):
        ns = load_funcs({"os": os, "json": json})
        prof = {"origin": [1000.0, 2000.0], "dir": [0.0, 1.0], "thickness": 300.0,
                "points": [[0, 0], [1250, 0], [1500, 3900], [250, 3900]]}
        got = ns["profile_points"](prof)
        self.assertEqual(got[0], (1000.0, 2000.0, 0.0))
        self.assertEqual(got[2], (1000.0, 3500.0, 3900.0))


class TestLean(unittest.TestCase):
    """C12 and SW13 leaning as SHEAR WALL ELEVATION (SW15 , SA14 , SW13 ,C12 ) draws them."""

    def test_a_columns_ends_move_by_its_base_and_top(self):
        ns = load_funcs({"os": os, "json": json})
        ln = {"base": [100.0, 0.0], "top": [400.0, -500.0]}
        a, b = ns["lean_points"]((0.0, 0.0, 10.0), (0.0, 0.0, 0.0), ln, 1.0)      # Revit's top end first
        self.assertEqual((a, b), ((400.0, -500.0, 10.0), (100.0, 0.0, 0.0)))

    def test_a_wall_slants_by_its_lean_across_it(self):
        import math
        ns = load_funcs({"os": os, "json": json})
        ang, along = ns["slant_angle"]({"base": [0.0, 0.0], "top": [0.0, -390.0]}, (0.0, -1.0), 3900.0)
        self.assertAlmostEqual(ang, math.atan2(390.0, 3900.0))
        self.assertAlmostEqual(along, 0.0)
        ang, along = ns["slant_angle"]({"base": [0.0, 0.0], "top": [100.0, 390.0]}, (0.0, -1.0), 3900.0)
        self.assertLess(ang, 0.0)
        self.assertAlmostEqual(along, 100.0)


class TestSlopingSlab(unittest.TestCase):
    """Main Block S6: its top 2270 below the terrace at B19, at the terrace at B7 3300 east."""

    def test_the_top_runs_from_one_beam_to_the_other_and_no_further(self):
        ns = load_funcs({"os": os, "json": json})
        sl = {"origin": [0.0, 0.0], "dir": [1.0, 0.0], "p0": 0.0, "z0": -2270.0, "p1": 3300.0, "z1": 0.0}
        z = ns["slope_z_mm"]
        self.assertAlmostEqual(z(sl, 0.0, 500.0), -2270.0)
        self.assertAlmostEqual(z(sl, 1650.0, 500.0), -1135.0)
        self.assertAlmostEqual(z(sl, 3300.0, 4000.0), 0.0)
        self.assertAlmostEqual(z(sl, 5000.0, 0.0), 0.0)        # beyond B7: held at its height


class TestColourFilterGroups(unittest.TestCase):
    """Same sunk depth = one filter = one colour; same footing size likewise."""

    def setUp(self):
        self.ns = load_funcs({"os": os, "json": json})

    def test_slabs_sunk_by_the_same_depth_share_one_filter(self):
        # feet from Revit read back in mm carry a little noise
        got = self.ns["sunk_depths"]([-150.0000004, -149.9999, -250.0, -50.0, 0.0, 0.3, None, 100.0])
        self.assertEqual(got, [50, 150, 250])
        self.assertEqual(self.ns["sunk_filter_name"](150), "Sunk slab 150 mm")

    def test_flat_or_raised_slabs_get_no_sunk_filter(self):
        self.assertEqual(self.ns["sunk_depths"]([0.0, -0.4, 25.0]), [])

    def test_footings_in_size_order_steps_last(self):
        names = ["Footing step 1200x1500x300", "Footing 2400x2700x750", "Footing 1800x2100x600",
                 "Footing 1800x1800x600"]
        self.assertEqual(sorted(names, key=self.ns["footing_size_key"]),
                         ["Footing 1800x1800x600", "Footing 1800x2100x600", "Footing 2400x2700x750",
                          "Footing step 1200x1500x300"])

    def test_a_column_is_one_colour_whichever_way_it_faces(self):
        got = self.ns["column_size_groups"](["C 300x600", "C 600x300", "PD 300x600", "C 230x230",
                                             "M_Concrete-Rectangular-Column 300 x 450", "B 230x450", None])
        self.assertEqual(got, {"Column 300x600": ["C 300x600", "C 600x300", "PD 300x600"],
                               "Column 230x230": ["C 230x230"]})


if __name__ == "__main__":
    unittest.main(verbosity=2)
