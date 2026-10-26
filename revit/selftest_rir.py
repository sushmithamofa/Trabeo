"""Check the plain-logic part of rir_cad2revit.py (the Rhino.Inside.Revit component) outside Revit.

    python selftest_rir.py
"""
import ast, io, json, os, shutil, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "rir_cad2revit.py")
WANT = ("latest_run", "level_rank", "exported_floors", "_key_of", "chosen", "foundations_file")


def load():
    tree = ast.parse(io.open(SRC, encoding="utf-8").read())
    keep = [n for n in tree.body if (isinstance(n, ast.FunctionDef) and n.name in WANT)
            or (isinstance(n, ast.Assign) and any(getattr(t, "id", "") == "RANK" for t in n.targets))]
    ns = {"os": os, "io": io, "json": json}
    exec(compile(ast.Module(body=keep, type_ignores=[]), SRC, "exec"), ns)
    return ns


F = load()


class Floors(unittest.TestCase):
    def setUp(self):
        self.p = tempfile.mkdtemp()
        rb = os.path.join(self.p, "runs", "2026-10-07_1200", "revit_build")
        for name, lvl, cols in (("A_TERRACE", "Terrace", 1), ("B_GF", "Ground Floor", 1), ("C_L2", "Level 2", 1),
                                ("D_SCHEDULES", "", 0), ("E_L1", "First Floor", 1)):
            os.makedirs(os.path.join(rb, name))
            with open(os.path.join(rb, name, "model.json"), "w") as f:
                json.dump({"level_name": lvl, "columns": [{}] * cols, "beams": [], "slabs": [],
                           "export": {"plan": name}}, f)
        with open(os.path.join(rb, "B_GF", "foundations.json"), "w") as f:
            f.write("{}")

    def tearDown(self):
        shutil.rmtree(self.p, ignore_errors=True)

    def test_floors_bottom_up_and_schedules_left_out(self):
        run = F["latest_run"](self.p)
        fl = F["exported_floors"](run)
        self.assertEqual([f[2] for f in fl], ["Ground Floor", "First Floor", "Level 2", "Terrace"])
        self.assertTrue(F["foundations_file"](run).endswith(os.path.join("B_GF", "foundations.json")))

    def test_a_floor_whose_name_tells_no_order_goes_where_the_export_says(self):
        # "Service Floor" added below the ground floor; "EL+6600" named by its height: their
        # names tell the build nothing, the export gives their place (level_rank)
        rb = os.path.join(self.p, "runs", "2026-10-07_1200", "revit_build")
        for name, lvl, rank in (("F_SERVICE", "Service Floor", -0.3), ("G_EL", "EL+6600", 2.2)):
            os.makedirs(os.path.join(rb, name))
            with open(os.path.join(rb, name, "model.json"), "w") as f:
                json.dump({"level_name": lvl, "columns": [{}], "beams": [], "slabs": [],
                           "export": {"plan": name, "level_rank": rank}}, f)
        fl = F["exported_floors"](F["latest_run"](self.p))
        self.assertEqual([f[2] for f in fl], ["Service Floor", "Ground Floor", "First Floor", "Level 2", "EL+6600",
                                              "Terrace"])

    def test_choosing_floors(self):
        fl = F["exported_floors"](F["latest_run"](self.p))
        self.assertEqual([f[2] for f in F["chosen"](fl, ["level 2", "terrace"])], ["Level 2", "Terrace"])
        self.assertEqual(len(F["chosen"](fl, [])), 4)

    def test_no_syntax_ironpython_cannot_read(self):
        src = io.open(SRC, encoding="utf-8").read()
        tree = ast.parse(src)
        self.assertFalse(any(isinstance(n, ast.JoinedStr) for n in ast.walk(tree)), "f-string")


if __name__ == "__main__":
    unittest.main(verbosity=1)
