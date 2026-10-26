r"""Tests for the architect's model in Review's 3D view: reading (OBJ, glTF, Rhino, IFC),
lining up (as it is, by the columns, two points, heights), the project's store, and the
notes' kept view.

    python -m building.arch_selftest
"""
from __future__ import annotations

import json
import math
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from building import arch_align, arch_model  # noqa: E402


def _box(x0, y0, z0, x1, y1, z1):
    V = [(x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0), (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)]
    F = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4), (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6),
         (3, 0, 4), (3, 4, 7)]
    return V, F


def _grid(nx, ny, step=6000.0, x0=0.0, y0=0.0):
    return [(x0 + i * step, y0 + j * step) for i in range(nx) for j in range(ny)]


def _turn(pts, deg, dx, dy):
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    return [(c * x - s * y + dx, s * x + c * y + dy) for x, y in pts]


def _meta_with(boxes, groups=("Model",), unit=1.0, up="z"):
    """A meta.json as arch_model writes it, from boxes (lo, hi, group index, name)."""
    return {"unit_mm": unit, "up": up, "groups": list(groups),
            "elements": [{"n": n, "c": "", "g": g, "s": -1, "id": "", "b": [*lo, *hi]} for lo, hi, g, n in boxes]}


class Reading(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _check_bin(self, out, meta):
        size = os.path.getsize(os.path.join(out, "geometry.bin"))
        self.assertEqual(size, meta["nverts"] * 12 + meta["nidx"] * 4)
        for c in meta["chunks"]:
            self.assertEqual(sum(e[1] for e in c["els"]) * 3, c["in"])
            raw = np.fromfile(os.path.join(out, "geometry.bin"), dtype=np.uint32, count=c["in"],
                              offset=meta["nverts"] * 12 + c["i0"] * 4)
            self.assertTrue(raw.max() < c["vn"])          # indices stay inside their own piece

    def test_obj(self):
        p = os.path.join(self.tmp, "m.obj")
        lines = []
        for k, (x, mtl) in enumerate(((0, "Concrete"), (10, "Glass"))):
            V, F = _box(x, 0, 0, x + 0.5, 0.5, 3)          # metres, Y up as most OBJ files
            lines += [f"o thing{k}", f"usemtl {mtl}"] + [f"v {a} {c} {-b}" for a, b, c in V]
            base = 8 * k
            lines += [f"f {a + 1 + base}/1/1 {b + 1 + base} {c + 1 + base}" for a, b, c in F]
        open(p, "w").write("\n".join(lines))
        meta = arch_model.convert(p, os.path.join(self.tmp, "out"))
        self.assertEqual(meta["groups"], ["Concrete", "Glass"])
        self.assertEqual(len(meta["elements"]), 2)
        self.assertEqual((meta["unit_mm"], meta["unit_sure"], meta["up"], meta["up_sure"]), (1000.0, False, "y", False))
        self._check_bin(os.path.join(self.tmp, "out"), meta)

    def test_glb(self):
        V, F = _box(0, 0, 0, 1, 2, 3)
        pos = np.array(V, dtype=np.float32).tobytes()
        idx = np.array(F, dtype=np.uint16).tobytes()
        js = {"asset": {"version": "2.0"}, "scene": 0, "scenes": [{"nodes": [0]}],
              "nodes": [{"name": "Tower", "children": [1], "translation": [100, 0, 0]},
                        {"name": "box", "mesh": 0, "scale": [2, 2, 2]}],
              "meshes": [{"primitives": [{"attributes": {"POSITION": 0}, "indices": 1}]}],
              "buffers": [{"byteLength": len(pos) + len(idx)}],
              "bufferViews": [{"buffer": 0, "byteOffset": 0, "byteLength": len(pos)},
                              {"buffer": 0, "byteOffset": len(pos), "byteLength": len(idx)}],
              "accessors": [{"bufferView": 0, "componentType": 5126, "count": 8, "type": "VEC3"},
                            {"bufferView": 1, "componentType": 5123, "count": 36, "type": "SCALAR"}]}
        j = json.dumps(js).encode()
        j += b" " * (-len(j) % 4)
        binc = pos + idx
        binc += b"\0" * (-len(binc) % 4)
        glb = b"glTF" + struct.pack("<II", 2, 12 + 8 + len(j) + 8 + len(binc)) + struct.pack("<II", len(j), 0x4E4F534A) + j \
            + struct.pack("<II", len(binc), 0x004E4942) + binc
        p = os.path.join(self.tmp, "m.glb")
        open(p, "wb").write(glb)
        meta = arch_model.convert(p, os.path.join(self.tmp, "out"))
        self.assertEqual(meta["groups"], ["Tower"])
        self.assertEqual(meta["elements"][0]["b"], [100.0, 0.0, 0.0, 102.0, 4.0, 6.0])   # child scaled, parent moved
        self.assertEqual((meta["unit_mm"], meta["unit_sure"], meta["up"]), (1000.0, True, "y"))
        self._check_bin(os.path.join(self.tmp, "out"), meta)

    def test_3dm(self):
        import rhino3dm as r
        f = r.File3dm()
        f.Settings.ModelUnitSystem = r.UnitSystem.Meters
        on, off = r.Layer(), r.Layer()
        on.Name, off.Name = "COLUMNS", "FURNITURE"
        off.Visible = False
        i_on, i_off = f.Layers.Add(on), f.Layers.Add(off)
        m = r.Mesh()
        V, F = _box(0, 0, 0, 0.3, 0.6, 3)
        for v in V:
            m.Vertices.Add(*v)
        for a, b, c in F:
            m.Faces.AddFace(a, b, c)
        at = r.ObjectAttributes()
        at.LayerIndex = i_on
        f.Objects.AddMesh(m, at)
        at2 = r.ObjectAttributes()
        at2.LayerIndex = i_off
        f.Objects.AddMesh(m, at2)
        # an extrusion saved with no display mesh: built from its profile here
        rect = r.Polyline(5)
        for x, y in ((5, 0), (5.4, 0), (5.4, 0.4), (5, 0.4), (5, 0)):
            rect.Add(x, y, 0)
        ext = r.Extrusion.Create(rect.ToPolylineCurve(), 2.5, True)
        f.Objects.AddExtrusion(ext, at)
        # a polysurface saved with no display meshes (as a script or "save small" leaves it): made here
        bb = r.BoundingBox(r.Point3d(10, 0, 0), r.Point3d(11, 2, 3))
        f.Objects.AddBrep(r.Brep.CreateFromBox(r.Box(bb)), at)
        p = os.path.join(self.tmp, "m.3dm")
        f.Write(p, 7)
        meta = arch_model.convert(p, os.path.join(self.tmp, "out"))
        self.assertIn("COLUMNS", meta["groups"])
        br = [e for e in meta["elements"] if e["c"] == "Brep"]
        self.assertEqual(len(br), 1)
        self.assertEqual([round(v, 3) for v in br[0]["b"]], [10.0, 0.0, 0.0, 11.0, 2.0, 3.0])
        self.assertTrue(any("made here from their edges" in n for n in meta["notes"]))
        ch = next(c for c in meta["chunks"] if any(e[2] == meta["elements"].index(br[0]) for e in c["els"]))
        self.assertEqual(next(e[1] for e in ch["els"] if e[2] == meta["elements"].index(br[0])), 12)   # 6 faces x 2
        self.assertIn("FURNITURE (off in the file)", meta["groups_off"])        # read, but starts unticked
        self.assertEqual((meta["unit_mm"], meta["unit_sure"], meta["up"]), (1000.0, True, "z"))
        ex = [e for e in meta["elements"] if e["c"] == "Extrusion"]
        self.assertEqual(len(ex), 1)
        lo, hi = ex[0]["b"][:3], ex[0]["b"][3:]
        self.assertAlmostEqual(hi[0] - lo[0], 0.4, 3)
        self.assertAlmostEqual(abs(hi[2] - lo[2]), 2.5, 3)
        self._check_bin(os.path.join(self.tmp, "out"), meta)

    def test_ifc(self):
        try:
            import ifcopenshell
            import ifcopenshell.api
        except ImportError:
            self.skipTest("ifcopenshell is not installed on this PC")
        try:
            f = ifcopenshell.api.run("project.create_file", version="IFC4")      # ifcopenshell 0.8+
        except Exception:
            f = ifcopenshell.api.run("root.create_file", version="IFC4")
        proj = ifcopenshell.api.run("root.create_entity", f, ifc_class="IfcProject", name="P")
        ifcopenshell.api.run("unit.assign_unit", f)                       # millimetres
        ctx = ifcopenshell.api.run("context.add_context", f, context_type="Model")
        body = ifcopenshell.api.run("context.add_context", f, context_type="Model", context_identifier="Body",
                                    target_view="MODEL_VIEW", parent=ctx)
        site = ifcopenshell.api.run("root.create_entity", f, ifc_class="IfcSite", name="S")
        bld = ifcopenshell.api.run("root.create_entity", f, ifc_class="IfcBuilding", name="B")
        ifcopenshell.api.run("aggregate.assign_object", f, products=[site], relating_object=proj)
        ifcopenshell.api.run("aggregate.assign_object", f, products=[bld], relating_object=site)
        def place(product, m):          # matrix in the file's mm (0.8+ reads it in metres unless told)
            try:
                ifcopenshell.api.run("geometry.edit_object_placement", f, product=product, matrix=m, is_si=False)
            except TypeError:
                ifcopenshell.api.run("geometry.edit_object_placement", f, product=product, matrix=m)

        floors = []
        for name, z in (("GROUND FLOOR", 0.0), ("FIRST FLOOR", 3200.0)):
            st = ifcopenshell.api.run("root.create_entity", f, ifc_class="IfcBuildingStorey", name=name)
            ifcopenshell.api.run("aggregate.assign_object", f, products=[st], relating_object=bld)
            m = np.eye(4)
            m[2, 3] = z
            place(st, m)
            floors.append(st)
        for x, y in ((0, 0), (6000, 0), (6000, 5000)):
            col = ifcopenshell.api.run("root.create_entity", f, ifc_class="IfcColumn", name="C1")
            rep = ifcopenshell.api.run("geometry.add_profile_representation", f, context=body,
                                       profile=f.createIfcRectangleProfileDef("AREA", None, None, 300.0, 600.0),
                                       depth=3.0)
            ifcopenshell.api.run("geometry.assign_representation", f, product=col, representation=rep)
            m = np.eye(4)
            m[0, 3], m[1, 3] = x, y
            place(col, m)
            ifcopenshell.api.run("spatial.assign_container", f, products=[col], relating_structure=floors[0])
        p = os.path.join(self.tmp, "m.ifc")
        f.write(p)
        try:
            meta = arch_model.convert(p, os.path.join(self.tmp, "out"))
        except ValueError as exc:
            if str(exc) == arch_model.BLOCKED:
                self.skipTest("Windows blocks ifcopenshell's 3D engine on this PC (Smart App Control)")
            raise
        self.assertEqual(meta["groups"], ["Columns"])
        self.assertEqual([s["name"] for s in meta["storeys"]], ["GROUND FLOOR", "FIRST FLOOR"])
        self.assertAlmostEqual(meta["storeys"][1]["elev"], 3200.0, 0)
        self.assertEqual(sorted((round(x), round(y)) for x, y in arch_align.columns(meta)),
                         [(0, 0), (6000, 0), (6000, 5000)])
        self._check_bin(os.path.join(self.tmp, "out"), meta)

    def test_names(self):
        self.assertEqual(arch_model.check_name("a.IFC"), "IFC")
        for bad, word in (("a.rvt", "Export > IFC"), ("a.dwg", "CAD drawing"), ("a.skp", "Export"), ("a.txt", "Add an IFC")):
            with self.assertRaises(ValueError) as cm:
                arch_model.check_name(bad)
            self.assertIn(word, str(cm.exception))


class Columns(unittest.TestCase):
    def test_columns_named_and_shaped_not_walls(self):
        boxes = [((0, 0, 0), (300, 600, 3000), 0, "anything"),             # shaped like a column
                 ((6000, 0, 0), (6300, 300, 3000), 1, "x"),                  # on a COLUMN layer
                 ((0, 6000, 0), (230, 6230, 3000), 2, "w"),                  # on a wall layer
                 ((0, 9000, 0), (900, 9900, 300), 0, "table"),              # low: not a column
                 ((9000, 0, 0), (9800, 300, 3000), 0, "door frame")]       # a door frame piece by name? kept out by shape (ratio)
        # a curved wall in short straight pieces, each touching the next: not columns
        for k in range(6):
            boxes.append(((20000 + 600 * k, 0, 0), (20000 + 600 * (k + 1), 300, 3000), 0, "piece"))
        meta = _meta_with(boxes, groups=("Layer 1", "S-COLUMNS", "03_WALL::EXT"))
        got = sorted((round(x), round(y)) for x, y in arch_align.columns(meta))
        self.assertEqual(got, [(150, 300), (6150, 150)])

    def test_units_and_up(self):
        # metres, Y up: a 0.3 x 0.3 x 3 m post at x 5, depth (-z) 2
        meta = _meta_with([((5, 0, -2.3), (5.3, 3, -2.0), 0, "post")], unit=1000.0, up="y")
        (x, y), = arch_align.columns(meta)
        self.assertAlmostEqual(x, 5150.0)
        self.assertAlmostEqual(y, 2150.0)


class Fitting(unittest.TestCase):
    S = _grid(4, 3) + [(3000.0, 9000.0)]

    def test_as_it_is(self):
        A = [(x + 20, y - 15) for x, y in self.S] + [(50000.0, 0.0)]
        r = arch_align.fit(A, self.S)
        self.assertTrue(r["ok"])
        self.assertEqual(r["how"], "as it is")
        self.assertEqual((r["rot"], r["dx"], r["dy"]), (0.0, 0.0, 0.0))
        self.assertEqual(r["matched"], len(self.S))

    def test_turned_and_moved(self):
        # the architect's model: the structure turned back and moved, columns a few mm off,
        # two structural columns missing, three extra posts
        A0 = [p for k, p in enumerate(self.S) if k not in (2, 7)] + [(1500.0, 1500.0), (-4000.0, 2000.0), (20000.0, 3000.0)]
        A = _turn(A0, -31.0, 125400.0, -38200.0)
        A = [(x + (7 if k % 2 else -5), y + 4) for k, (x, y) in enumerate(A)]
        r = arch_align.fit(A, self.S)
        self.assertTrue(r["ok"], r["text"])
        self.assertEqual(r["how"], "by the columns")
        back = arch_align.apply(r, A)
        miss = [min(math.hypot(bx - sx, by - sy) for sx, sy in self.S) for bx, by in back[:len(self.S) - 2]]
        self.assertLess(max(miss), 20.0)
        self.assertAlmostEqual(r["rot"], 31.0, 1)
        self.assertEqual(r["n_off"], 2)                     # the two the architect has no column for

    def test_more_than_one_place(self):
        # a 2 x 3 bay structure inside a bigger, regular grid: fits in many places
        S = _grid(2, 3)
        A = _turn(_grid(6, 6), 0.0, 1000.0, 1000.0)
        r = arch_align.fit(A, S)
        self.assertFalse(r["ok"])
        self.assertEqual(r["how"], "ambiguous")

    def test_no_fit_and_mirrored(self):
        S = [(0, 0), (7000, 0), (7000, 4000), (0, 9000), (3000, 5000), (12000, 2000)]
        r = arch_align.fit([(x, -y * 3 + 7) for x, y in _grid(3, 3, 3100)], S)
        self.assertFalse(r["ok"])
        mirrored = [(-x + 500.0, y + 200.0) for x, y in S]
        r = arch_align.fit(mirrored, S)
        self.assertFalse(r["ok"])
        self.assertEqual(r["how"], "mirrored")

    def test_too_few(self):
        self.assertEqual(arch_align.fit([(0, 0)], self.S)["how"], "none")
        self.assertEqual(arch_align.fit(self.S, [(0, 0), (1, 1)])["how"], "none")

    def test_two_points(self):
        s1, s2 = (1000.0, 2000.0), (13000.0, 2000.0)
        al = {"rot": 30.0, "dx": 5000.0, "dy": -700.0}
        inv = _turn([(s1[0] - 5000.0, s1[1] + 700.0), (s2[0] - 5000.0, s2[1] + 700.0)], -30.0, 0, 0)
        r = arch_align.two_points(s1, s2, inv[0], inv[1])
        self.assertAlmostEqual(r["rot"], 30.0, 3)
        self.assertAlmostEqual(r["dx"], al["dx"], 0)
        self.assertAlmostEqual(r["dy"], al["dy"], 0)
        self.assertNotIn("warn", r)
        r = arch_align.two_points(s1, s2, (0, 0), (12500, 0))   # 500 mm longer: said, not scaled
        self.assertTrue(r["warn"])
        with self.assertRaises(ValueError):
            arch_align.two_points(s1, s1, (0, 0), (5, 0))

    def test_refine_from_a_hand_placement(self):
        # the architect's columns: the structure turned 12 deg and moved; the user drags it to within
        # 400 mm and 1.5 deg - fine-tuning lands it exactly
        A = _turn(self.S, -12.0, 3000.0, -2000.0)
        exact = arch_align._refine(np.array(A), np.array(self.S), list(zip(range(len(A)), range(len(A)))))
        hand = {"rot": exact["rot"] + 1.5, "dx": exact["dx"] + 300.0, "dy": exact["dy"] - 250.0}
        r = arch_align.refine(A, self.S, hand)
        self.assertTrue(r["ok"], r["text"])
        self.assertEqual(r["matched"], len(self.S))
        self.assertAlmostEqual(r["rot"], exact["rot"], 2)
        self.assertLess(math.hypot(r["dx"] - exact["dx"], r["dy"] - exact["dy"]), 5.0)
        far = {"rot": exact["rot"], "dx": exact["dx"] + 9000.0, "dy": exact["dy"]}
        self.assertFalse(arch_align.refine(A, self.S, far)["ok"])          # too far by hand: said, not searched
        self.assertFalse(arch_align.refine([], self.S, hand)["ok"])         # no columns: the hand placement stays

    def test_heights(self):
        levels = [{"key": "GF", "z": 0.0}, {"key": "L1", "z": 3150.0}, {"key": "L2", "z": 6300.0}]
        st = [{"name": "Ground Floor", "elev": 50.0}, {"name": "FIRST FLOOR", "elev": 3200.0},
              {"name": "Level 2", "elev": 6350.0}, {"name": "Roof garden", "elev": 9000.0}]
        h = arch_align.heights(st, levels)
        self.assertEqual(h["dz"], -50.0)
        self.assertEqual([p["level"] for p in h["pairs"]], ["GF", "L1", "L2"])
        st[1]["elev"] = 3300.0
        self.assertIsNone(arch_align.heights(st, levels)["dz"])          # they disagree: no offset proposed
        self.assertIsNone(arch_align.heights([{"name": "Zone A", "elev": 0.0}], levels)["dz"])

    def test_propose_end_to_end(self):
        # a model file's elements: columns as the structure, turned 90 deg and moved
        A = _turn(self.S, 90.0, 400000.0, 7000.0)
        meta = _meta_with([((x - 150, y - 300, 0), (x + 150, y + 300, 3000), 0, "col") for x, y in A])
        meta["storeys"] = [{"name": "GROUND FLOOR", "elev": 0.0}]
        r = arch_align.propose(meta, {}, self.S, [{"key": "GF", "z": -75.0}])
        self.assertTrue(r["ok"], r["text"])
        self.assertAlmostEqual((r["rot"] + 360) % 360, 270.0, 1)
        self.assertEqual(r["dz"], -75.0)


class Store(unittest.TestCase):
    def setUp(self):
        self.p = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.p, ignore_errors=True)

    def test_add_read_settings_remove(self):
        m, path = arch_model.add(self.p, "Arch model.obj", "me@x")
        V, F = _box(0, 0, 0, 1, 1, 3)
        open(path, "w").write("\n".join([f"v {a} {b} {c}" for a, b, c in V] + [f"f {a + 1} {b + 1} {c + 1}" for a, b, c in F]))
        arch_model.run(self.p, m["id"])
        got = arch_model.load(self.p)["models"][0]
        self.assertEqual(got["state"], "ready", got.get("message"))
        self.assertIsNone(got["hidden"])
        self.assertTrue(arch_model.model_path(self.p, m["id"], "geometry.bin"))
        self.assertEqual(arch_model.model_path(self.p, m["id"], "..\\models.json"), "")
        self.assertEqual(arch_model.model_path(self.p, "..", "meta.json"), "")
        s = arch_model.settings(self.p, m["id"], {"show": False, "opacity": 3, "hidden": ["Model"], "unit_mm": 1000,
                                                  "set": "J1153", "align": {"rot": 2, "dx": 5, "how": "typed"}, "by": "me"})
        self.assertEqual((s["show"], s["opacity"], s["hidden"], s["unit_mm"]), (False, 1.0, ["Model"], 1000.0))
        self.assertEqual(s["align"]["J1153"]["rot"], 2.0)
        self.assertFalse(s["align"]["J1153"]["pinned"])
        s = arch_model.settings(self.p, m["id"], {"set": "J1153", "align": {"rot": 2, "dx": 5, "pinned": True}})
        self.assertTrue(s["align"]["J1153"]["pinned"])
        s = arch_model.settings(self.p, m["id"], {"set": "J1153", "align": None})
        self.assertEqual(s["align"], {})
        with self.assertRaises(ValueError):
            arch_model.settings(self.p, m["id"], {"up": "x"})
        self.assertTrue(arch_model.remove(self.p, m["id"]))
        self.assertEqual(arch_model.load(self.p)["models"], [])
        self.assertFalse(os.path.exists(arch_model.folder(self.p, m["id"])))
        m2, _ = arch_model.add(self.p, "b.ifc")
        self.assertEqual(m2["id"], "A2")                    # ids are never reused

    def test_failed_read_is_said(self):
        m, path = arch_model.add(self.p, "broken.glb")
        open(path, "wb").write(b"not a model")
        arch_model.run(self.p, m["id"])
        got = arch_model.load(self.p)["models"][0]
        self.assertEqual(got["state"], "failed")
        self.assertTrue(got["message"])


class NotesKeepTheView(unittest.TestCase):
    def setUp(self):
        self.p = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.p, ignore_errors=True)

    def test_view_and_arch_kept_small_only(self):
        from building import notes
        v = {"set": "J1153", "cam": {"x": 1, "y": 2, "z": 3, "d": 40, "t": 1.1, "p": 0.9}, "box": None}
        n = notes.change(self.p, {"action": "add", "note": {"type": "architecture", "mark": "Wall 12", "set": "J1153",
                                                             "view": v, "arch": {"model": "A1", "el": "g1"},
                                                             "text": "Beam B4 cuts the window head"}})
        self.assertEqual(n["view"], v)
        self.assertEqual(n["arch"]["model"], "A1")
        n = notes.change(self.p, {"action": "add", "note": {"type": "area", "view": {"x": "y" * 9000}, "arch": "x",
                                                             "text": "t"}})
        self.assertNotIn("view", n)
        self.assertNotIn("arch", n)
        out = os.path.join(self.p, "r.xlsx")
        notes.write_xlsx(self.p, notes.rows(self.p), out)      # the report takes them as any other note
        self.assertTrue(os.path.getsize(out) > 0)


class PageScript(unittest.TestCase):
    def test_scripts_parse(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not on this PC")
        import re
        from building.arch_ui import ARCH_HOOK
        from building.notes_ui import REVIEW_HOOK
        with tempfile.TemporaryDirectory() as d:
            for name, html in (("arch", ARCH_HOOK), ("notes", REVIEW_HOOK)):
                for i, js in enumerate(re.findall(r"<script>(.*?)</script>", html, re.S)):
                    p = os.path.join(d, f"{name}{i}.js")
                    open(p, "w", encoding="utf-8").write(js)
                    r = subprocess.run([node, "--check", p], capture_output=True, text=True)
                    self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
