r"""Tests for the two ways to a file without anyone at Revit: the IFC export (no Revit
at all) and starting Revit by itself.

    python -m building.ifc_selftest
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import unittest

from building import ifc_export, revit_launch


def run_with(floors: dict) -> str:
    """A minimal exported analysis: runs\\x\\revit_build\\<plan>\\model.json per floor."""
    run = tempfile.mkdtemp(prefix="ifc_run_")
    for plan, model in floors.items():
        d = os.path.join(run, "revit_build", plan)
        os.makedirs(d)
        with open(os.path.join(d, "model.json"), "w", encoding="utf-8") as f:
            json.dump({"strict": True, "base_point": [1000.0, 2000.0], **model}, f)
    return run


def rect(x0, y0, x1, y1):
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]


GF = {
    "level_name": "GF",
    "columns": [{"mark": "C1", "kind": "column", "x": 1150.0, "y": 2300.0, "width": 300.0, "depth": 600.0,
                 "rotation": 90.0, "outline": rect(1000, 2000, 1300, 2600)}],
    "beams": [{"mark": "B1", "kind": "beam", "start": [1000.0, 2000.0], "end": [7000.0, 2000.0],
               "width": 230.0, "depth": 450.0, "top_mm": 0.0},
              {"mark": "B2", "kind": "beam", "start": [1000.0, 6000.0], "end": [7000.0, 6000.0],
               "width": 230.0, "depth": None, "top_mm": 0.0}],
    "slabs": [{"mark": "S1", "outline": rect(1000, 2000, 7000, 6000), "thickness": 150.0, "sunk_mm": 100.0,
               "holes": [rect(2000, 3000, 2500, 3500)]},
              {"mark": "S2", "outline": rect(7000, 2000, 9000, 6000), "thickness": None}],
    "walls": [{"mark": "W1", "start": [1000.0, 8000.0], "end": [5000.0, 8000.0], "thickness": 200.0}],
}
LEVELS = {"levels": {"Below GF": -3000, "GF": 0}}


class IfcExport(unittest.TestCase):
    def setUp(self):
        self.run = run_with({"P1_plan": GF})
        self.out = os.path.join(self.run, "model.ifc")

    def tearDown(self):
        shutil.rmtree(self.run, ignore_errors=True)

    def write(self, decisions=LEVELS):
        r = ifc_export.write(self.run, self.out, decisions, log=lambda *_: None)
        with open(self.out, encoding="ascii") as f:
            return r, f.read()

    def test_it_is_an_ifc4_file(self):
        _r, text = self.write()
        self.assertTrue(text.startswith("ISO-10303-21;"))
        self.assertIn("FILE_SCHEMA(('IFC4'));", text)
        self.assertTrue(text.rstrip().endswith("END-ISO-10303-21;"))

    def test_what_is_sized_is_written_and_nothing_else(self):
        r, text = self.write()
        self.assertEqual(r["made"]["columns"], 1)
        self.assertEqual(r["made"]["beams"], 1)                  # B2 has no depth
        self.assertEqual(r["made"]["slabs"], 1)                  # S2 has no thickness
        self.assertEqual(r["made"]["walls"], 1)
        self.assertTrue(any("B2" in x and "depth" in x for x in r["left_out"]))
        self.assertTrue(any("S2" in x and "thickness" in x for x in r["left_out"]))
        self.assertEqual(len(re.findall(r"=IFCBEAM\(", text)), 1)

    def test_storeys_carry_the_levels(self):
        _r, text = self.write()
        st = re.findall(r"IFCBUILDINGSTOREY\('[^']+',\$,'([^']+)',.*?,\.ELEMENT\.,([-\d.]+)\)", text)
        self.assertEqual(st, [("Below GF", "-3000.0"), ("GF", "0.0")])

    def test_a_column_rises_from_the_level_below(self):
        _r, text = self.write()
        # the column's prism: 3000 tall, starting 3000 below its storey
        self.assertRegex(text, r"IFCEXTRUDEDAREASOLID\(#\d+,#\d+,#\d+,3000\.0\)")

    def test_no_elevation_stops_it(self):
        with self.assertRaises(ifc_export.IfcError) as cm:
            self.write({})
        self.assertIn("need an elevation", str(cm.exception))
        self.assertFalse(os.path.exists(self.out))

    def test_no_level_below_leaves_columns_and_walls_out(self):
        # a decision for GF only: the level below has no elevation, so it stops
        with self.assertRaises(ifc_export.IfcError):
            self.write({"levels": {"GF": 0}})

    def test_levels_entered_at_one_height_stop_it(self):
        """Level 0 and Level 1 both entered as 4000: the file came out with none of the
        floor's columns or walls (the walls counted as written, 0 mm tall). Stop and say so."""
        for same in ({"Below GF": 4000, "GF": 4000}, {"Below GF": 4000, "GF": 0}):
            with self.assertRaises(ifc_export.IfcError) as cm:
                self.write({"levels": same})
            self.assertIn("do not rise", str(cm.exception))
            self.assertIn("'GF'", str(cm.exception))
            self.assertFalse(os.path.exists(self.out))

    def test_every_global_id_is_unique_and_22_characters(self):
        _r, text = self.write()
        ids = re.findall(r"\(\'([0-9A-Za-z_$]{22})\',\$", text)
        self.assertTrue(ids)
        self.assertEqual(len(ids), len(set(ids)))

    def test_text_is_ascii_and_quotes_are_doubled(self):
        self.assertEqual(ifc_export._s("O'Brien"), "'O''Brien'")
        self.assertEqual(ifc_export._s("Ø"), "'\\X2\\00D8\\X0\\'")

    def test_sizes_coloured_as_the_revit_filters(self):
        """Beams, columns and sunk slabs each by size, as the Revit build's view filters."""
        from building import size_colours
        _r, text = self.write()
        pal = size_colours.palette()
        styles = dict(re.findall(r"=IFCSURFACESTYLE\('([^']+)',\.BOTH\.,\((#\d+)\)\)", text))
        self.assertEqual(set(styles), {"Beam 230x450", "Column 300x600", "Sunk slab 100 mm"})

        def rgb(name):
            shade = re.search(r"%s=IFCSURFACESTYLESHADING\((#\d+)," % styles[name], text).group(1)
            c = re.search(r"%s=IFCCOLOURRGB\(\$,([\d.]+),([\d.]+),([\d.]+)\)" % shade, text).groups()
            return tuple(round(float(v) * 255) for v in c)
        self.assertEqual(rgb("Beam 230x450"), pal[0])
        self.assertEqual(rgb("Column 300x600"), pal[1])
        self.assertEqual(rgb("Sunk slab 100 mm"), pal[2])
        self.assertEqual(len(re.findall(r"=IFCSTYLEDITEM\(", text)), 3)     # the wall keeps no colour

    def test_an_arc_runs_through_its_middle_point(self):
        pts = ifc_export._arc_points((0.0, 0.0), (5.0, 5.0), (10.0, 0.0), n=4)
        self.assertAlmostEqual(pts[2][0], 5.0, places=6)
        self.assertAlmostEqual(pts[2][1], 5.0, places=6)
        self.assertEqual(pts[0], (0.0, 0.0))

    def test_a_beam_with_a_drawn_section_is_extruded_from_that_section(self):
        prof = [[-375, -600], [-375, -500], [0, 0], [375, 0], [375, -600]]
        m = ifc_export._Model("t")
        sid = m.swept((0.0, 0.0), (5000.0, 0.0), prof, 0.0)
        text = "\n".join(m.w.lines)
        self.assertIn("IFCARBITRARYCLOSEDPROFILEDEF", text)
        self.assertIn(sid + "=IFCEXTRUDEDAREASOLID", text)
        self.assertIn("IFCCARTESIANPOINT((-375.0,-500.0))", text)
        from building import rhino_export
        tri = rhino_export._triangles([(-375, -600), (-375, -500), (0, 0), (375, 0), (375, -600)])
        self.assertEqual(len(tri), 3)                                   # n - 2 triangles
        kind, verts, faces, _ = rhino_export.RhinoModel.swept(None, (0.0, 0.0), (5000.0, 0.0), prof, 0.0)
        self.assertEqual((kind, len(verts), len(faces)), ("mesh", 10, 5 + 2 * 3))
        self.assertEqual(verts[2], (0.0, 0.0, 0.0))                     # top middle at the start
        self.assertEqual(verts[3], (0.0, 375.0, 0.0))                   # u +375: to the left (north)
        self.assertEqual(verts[0], (0.0, -375.0, -600.0))               # u is to the left: north of east

    def test_a_sloping_slab_and_beam_are_built_sloping(self):
        """Main Block SECTION C-C: S6 from -2270 at B19 to 0 at B7 (3300 east); the beam
        along its edge the same."""
        import math
        sl = {"origin": [1000.0, 0.0], "dir": [1.0, 0.0], "p0": 0.0, "z0": -2270.0, "p1": 3300.0, "z1": 0.0}
        pts = [(1000, 0), (4300, 0), (4300, 5000), (1000, 5000)]
        verts, faces = ifc_export.sloped_slab(pts, sl, 125.0)
        tops = {(round(x), round(y)): round(z) for x, y, z in verts[:4]}
        self.assertEqual((tops[(1000, 0)], tops[(4300, 5000)]), (-2270, 0))
        self.assertEqual(round(verts[4][2] - verts[0][2]), -125)
        self.assertEqual((len(faces), len(faces[0])), (6, 4))
        m = ifc_export._Model("t")
        sid = m.mesh(verts, faces)
        text = "\n".join(m.w.lines)
        self.assertIn(sid + "=IFCFACETEDBREP", text)
        m._shape([sid])
        self.assertIn("'Body','Brep'", "\n".join(m.w.lines))              # a faceted solid is a Brep body
        bar = m.slanted((0.0, 0.0), (3300.0, 0.0), 230.0, 600.0, -2270.0, 0.0)
        line = next(ln for ln in m.w.lines if ln.startswith(bar + "="))
        self.assertIn("IFCEXTRUDEDAREASOLID", line)
        L3 = math.hypot(3300.0, 2270.0)
        self.assertIn(ifc_export._f(L3), line)                            # along its own slope
        from building import rhino_export
        kind, c, f, _ = rhino_export.RhinoModel.slanted(None, (0.0, 0.0), (3300.0, 0.0), 230.0, 600.0, -2270.0, 0.0)
        self.assertEqual((kind, c[4][2], c[6][2], c[0][2]), ("mesh", -2270.0, 0.0, -2870.0))

    def test_a_curved_beam_is_one_band_along_its_arc(self):
        """Main Block terrace: the 230 quarter-arc beam (faces at R1200 / R1430) came out
        as 8 straight boxes in IFC and Rhino, and straight in the 3D view."""
        import math
        band = ifc_export.arc_band((1315.0, 0.0), (1315.0 * math.cos(math.pi / 4), 1315.0 * math.sin(math.pi / 4)),
                                   (0.0, 1315.0), 230.0)
        radii = [math.hypot(x, y) for x, y in band]
        self.assertAlmostEqual(min(radii), 1200.0, delta=1.0)
        self.assertAlmostEqual(max(radii), 1430.0, delta=1.0)
        self.assertGreaterEqual(len(band), 2 * 25)                           # smooth: 24 pieces a face


class FloatingColumnsOnEveryFloor(unittest.TestCase):
    """As the Revit build (the user's rule): each plan's FC stands on that floor and rises
    to the next level - the upper plan's FC is written too, above its own floor (red
    where no level above it is given)."""

    def test_each_plans_fc_stands_on_its_own_floor(self):
        fc = {"mark": "FC1", "kind": "column_float", "x": 4000.0, "y": 4000.0, "width": 230.0, "depth": 600.0,
              "rotation": 0.0, "outline": []}
        c = {"mark": "C1", "kind": "column", "x": 1150.0, "y": 2300.0, "width": 300.0, "depth": 600.0,
             "rotation": 0.0, "outline": []}
        run = run_with({"A_plan": {"level_name": "Level 1", "columns": [c, fc], "provisional_ok": True},
                        "B_plan": {"level_name": "Level 3", "columns": [c, fc], "provisional_ok": True}})
        try:
            from unittest import mock
            from building import revit_mcp
            floors = [("L1", os.path.join(run, "revit_build", "A_plan", "model.json"), "Level 1"),
                      ("L3", os.path.join(run, "revit_build", "B_plan", "model.json"), "Level 3")]
            rir = revit_mcp._rir()
            rir.exported_floors = lambda _run: floors
            with mock.patch.object(revit_mcp, "_rir", lambda: rir):
                r = ifc_export.write(run, os.path.join(run, "t.ifc"),
                                     {"levels": {"Level 0": 0, "Level 1": 3000, "Level 3": 6000}}, log=lambda *_: None)
            text = open(os.path.join(run, "t.ifc"), encoding="ascii").read()
        finally:
            shutil.rmtree(run, ignore_errors=True)
        self.assertEqual(r["made"]["columns"], 4)            # C1 and FC1 on each floor
        self.assertEqual(text.count("'FC1'") // 2, 2)         # Level 1 -> 3, and Level 3 -> above
        self.assertEqual(text.count("NEEDS DECISION"), 1)     # the top one: no level above Level 3


class RhinoExport(unittest.TestCase):
    """The same elements as the IFC file, as a .3dm: beam sizes in the Revit filters' colours."""

    def setUp(self):
        try:
            import rhino3dm  # noqa: F401
        except ImportError:
            self.skipTest("rhino3dm not installed")
        gf = json.loads(json.dumps(GF))
        gf["beams"].append({"mark": "B3", "kind": "beam", "start": [1000.0, 4000.0], "end": [7000.0, 4000.0],
                            "width": 230.0, "depth": 600.0, "top_mm": 0.0,
                            "provisional": "depth not given - built at 600"})
        self.run = run_with({"P1_plan": gf})
        self.out = os.path.join(self.run, "model.3dm")

    def tearDown(self):
        shutil.rmtree(self.run, ignore_errors=True)

    def test_a_leaning_wall_is_a_closed_solid_facing_out(self):
        """SW13 leaning as its wall elevation draws it: a wall's rectangle comes clockwise - its
        faces must still point out, or Rhino shows it hollow (user 2026-10-28)."""
        from building.rhino_export import RhinoModel
        m = RhinoModel.__new__(RhinoModel)
        for rect in ([(0, 150), (1200, 150), (1200, -150), (0, -150)], [(0, -150), (1200, -150), (1200, 150), (0, 150)]):
            _k, v, faces, _size = m.leaned(rect, 0.0, 3900.0, (0.0, 0.0), (0.0, 400.0))
            vol = 0.0
            for f in faces:
                a = v[f[0]]
                for i in range(1, len(f) - 1):
                    b, c = v[f[i]], v[f[i + 1]]
                    vol += (a[0] * (b[1] * c[2] - b[2] * c[1]) - a[1] * (b[0] * c[2] - b[2] * c[0])
                            + a[2] * (b[0] * c[1] - b[1] * c[0])) / 6.0
            self.assertAlmostEqual(vol, 1200 * 300 * 3900, delta=1.0)        # positive: faces outward

    def test_the_ifc_elements_on_layers_coloured_like_the_revit_filters(self):
        import rhino3dm
        from building import rhino_export
        r = rhino_export.write(self.run, self.out, LEVELS, log=lambda *_: None)
        self.assertEqual((r["made"]["columns"], r["made"]["beams"], r["made"]["slabs"]), (1, 2, 1))
        m = rhino3dm.File3dm.Read(self.out)
        layers = {ly.FullPath: tuple(ly.Color)[:3] for ly in m.Layers}
        pal = rhino_export.beam_colours()
        # sorted as the Revit build sorts its filters: 230x450 first, then 230x600
        # by element, not by floor (user 2026-10-28); the floor is each object's Level
        self.assertEqual(layers["Beams::Beam 230x450"], pal[0])
        self.assertEqual(layers["Beams::Beam 230x600"], pal[1])
        self.assertFalse(any(k.startswith("GF") for k in layers))
        objs = list(m.Objects)
        self.assertTrue(all(o.Geometry.IsValid for o in objs))
        b3 = next(o for o in objs if o.Attributes.Name == "B3")
        self.assertEqual(tuple(b3.Attributes.ObjectColor)[:3], rhino_export.RED)
        self.assertIn("depth not given", b3.Attributes.GetUserString("Note"))
        self.assertEqual(b3.Attributes.GetUserString("Level"), "GF")
        # a column and a sunk slab by size too, in the next colours after the beams'
        self.assertEqual(layers["Columns::Column 300x600"], pal[2])
        self.assertEqual(layers["Slabs::Sunk slab 100 mm"], pal[3])
        s1 = next(o for o in objs if o.Attributes.Name == "S1")
        self.assertEqual(s1.Geometry.ProfileCount, 2)                 # its opening
        self.assertEqual(m.Settings.ModelUnitSystem, rhino3dm.UnitSystem.Millimeters)

    def test_every_element_is_a_closed_polysurface_not_a_mesh(self):
        """Sasan Gir terrace: the sloping beams (B1, B9A) and the beams built to their drawn
        section (B2A, 750 wide) came into Rhino as meshes; every element is a polysurface."""
        import rhino3dm
        from building import rhino_export
        gf = json.loads(json.dumps(GF))
        gf["beams"] += [
            {"mark": "B4", "kind": "beam", "start": [1000.0, 6000.0], "end": [7000.0, 6000.0], "width": 750.0,
             "depth": 600.0, "top_mm": 0.0,
             "section": {"profile": [[-375, -600], [-375, -500], [0, 0], [375, 0], [375, -600]]}},
            {"mark": "B5", "kind": "beam", "start": [1000.0, 8000.0], "end": [4300.0, 8000.0], "width": 230.0,
             "depth": 600.0, "top_mm": -2270.0, "top_end_mm": 0.0}]
        gf["slabs"].append({"mark": "S9", "outline": [[1000, 9000], [4300, 9000], [4300, 12000], [1000, 12000]],
                            "thickness": 125.0, "slope": {"origin": [1000.0, 0.0], "dir": [1.0, 0.0], "p0": 0.0,
                                                          "z0": -2270.0, "p1": 3300.0, "z1": 0.0}})
        shutil.rmtree(self.run, ignore_errors=True)
        self.run = run_with({"P1_plan": gf})
        self.out = os.path.join(self.run, "model.3dm")
        r = rhino_export.write(self.run, self.out, LEVELS, log=lambda *_: None)
        self.assertEqual(r["made"]["beams"], len([b for b in gf["beams"] if b["depth"]]))
        objs = {o.Attributes.Name: o.Geometry for o in rhino3dm.File3dm.Read(self.out).Objects}
        self.assertFalse([n for n, g in objs.items() if isinstance(g, rhino3dm.Mesh)])
        self.assertEqual([n for n, g in objs.items() if not (g.IsValid and g.IsSolid)], [])
        b5 = objs["B5"].GetBoundingBox()
        self.assertEqual((round(b5.Min.Z), round(b5.Max.Z)), (-2870, 0))      # still sloping, 600 deep
        b4 = objs["B4"].ToBrep(True)
        self.assertEqual(len(b4.Faces), 5 + 2)                                 # its section, capped
        self.assertIn("S9", objs)

    def test_levels_entered_at_one_height_stop_the_rhino_file_too(self):
        from building import rhino_export
        with self.assertRaises(rhino_export.RhinoError) as cm:
            rhino_export.write(self.run, self.out, {"levels": {"Below GF": 4000, "GF": 4000}}, log=lambda *_: None)
        self.assertIn("do not rise", str(cm.exception))
        self.assertFalse(os.path.exists(self.out))

    def test_the_ifc_file_is_unchanged_by_the_rhino_writer(self):
        r = ifc_export.write(self.run, os.path.join(self.run, "a.ifc"), LEVELS, log=lambda *_: None)
        self.assertEqual(r["made"]["beams"], 2)


class SizeColours(unittest.TestCase):
    """The colour order of the Revit build's filters: beams by depth, then columns,
    sunk slabs, footings, steps - each size once, never a beam's colour while free."""

    def test_the_order(self):
        from building import size_colours as sc
        pal = sc.palette()
        got = sc.plan(["Footing step 1200x1500x300", "Sunk slab 150 mm", "Beam 300x600", "Column 600x300",
                       "Beam 230x450", "Footing 1800x2100x600", "Fold 230x300", "Beam 230x450"])
        self.assertEqual([got[k] for k in ("Beam 230x450", "Beam 300x600", "Fold 230x300", "Column 600x300",
                                           "Sunk slab 150 mm", "Footing 1800x2100x600",
                                           "Footing step 1200x1500x300")], pal[:7])

    def test_labels_match_the_revit_names(self):
        from building import size_colours as sc
        self.assertEqual(sc.column_label(600, 300), "Column 300x600")
        self.assertEqual(sc.sunk_label(149.6), "Sunk slab 150 mm")
        self.assertEqual(sc.footing_label("footing", 2700, 2400, 750), "Footing 2400x2700x750")
        self.assertEqual(sc.footing_label("step", 1500, 1200, 300), "Footing step 1200x1500x300")


class RevitStart(unittest.TestCase):
    def test_the_journal_opens_a_project_then_presses_the_mcp_switch(self):
        j = revit_launch.JOURNAL
        self.assertLess(j.index("ID_FILE_NEW_CHOOSE_TEMPLATE"), j.index("ID_EXCMD_TOGGLE_REVIT_MCP"))
        self.assertIn("Set Jrn = CrsJournalScript", j)

    def test_only_a_revit_with_the_plugin_is_used(self):
        tmp = tempfile.mkdtemp(prefix="rvt_")
        try:
            pf, ad = os.path.join(tmp, "pf"), os.path.join(tmp, "addins")
            for v in ("2024", "2025"):
                os.makedirs(os.path.join(pf, "Autodesk", "Revit %s" % v))
                open(os.path.join(pf, "Autodesk", "Revit %s" % v, "Revit.exe"), "w").close()
            os.makedirs(os.path.join(ad, "2024", "revit_mcp_plugin"))
            open(os.path.join(ad, "2024", "revit_mcp_plugin", "RevitMCPPlugin.dll"), "w").close()
            old = revit_launch.PROGRAM_FILES, revit_launch.ADDINS
            revit_launch.PROGRAM_FILES, revit_launch.ADDINS = pf, ad
            try:
                got = revit_launch.installed()
            finally:
                revit_launch.PROGRAM_FILES, revit_launch.ADDINS = old
            self.assertEqual([v for v, _ in got], ["2024"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_stopping_nothing_is_harmless(self):
        revit_launch.stop(None, log=lambda *_: None)


if __name__ == "__main__":
    unittest.main(verbosity=2)
