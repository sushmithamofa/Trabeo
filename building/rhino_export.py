"""The model as a Rhino file (.3dm): written by the app alone, no Rhino needed.

The elements, levels and rules are exactly the IFC file's: ifc_export.write runs as
it does for IFC and hands each solid to this writer instead (model_cls). Nothing
about building is repeated here.

In the file:
* layers  <Columns | Beams | Hidden beams | Slabs | Shear walls | Footings | Rafts ...>,
  by element, not by floor (user 2026-10-28: all the beams, all the shear walls at one
  click); each object's floor is its user text Level. Under Beams and Hidden beams
  one layer per size - in the colour the Revit
  build's view filters give that size (revit\\build_model.py BEAM_COLOURS, in the
  same order), so the Rhino model reads like the Revit one; columns, sunk slabs
  and footings likewise one layer per size ("Column 300x600", "Sunk slab 150 mm",
  "Footing 1800x2100x600"), coloured by building.size_colours;
* an element still needing a decision drawn red, its reason in the object's
  user text (Note), as in Revit's Comments;
* each object named by its mark, with user text Mark / Size / Level / Kind;
* every element a closed polysurface (an extrusion), never a mesh - sloping beams
  and beams built to their drawn section too (the user's rule, 2026-10-19).
Units are millimetres, the drawings' own.

    write(run, path, decisions=..., log=..., name=...)
"""
from __future__ import annotations

import ast
import io
import math
import os

from building import ifc_export, size_colours

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RED = (255, 0, 0)                           # as revit\build_model.py PROVISIONAL_RGB
TYPE_COLOURS = {"Columns": (150, 150, 150), "Slabs": (200, 200, 200), "Shear walls": (170, 170, 170),
                "Footings": (150, 120, 90), "Pedestals": (130, 130, 130), "Rafts": (150, 120, 90)}


class RhinoError(RuntimeError):
    pass


def beam_colours() -> list[tuple[int, int, int]]:
    """BEAM_COLOURS from revit\\build_model.py, read from that file - not copied."""
    path = os.path.join(ROOT, "revit", "build_model.py")
    with io.open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    for n in tree.body:
        if isinstance(n, ast.Assign) and any(getattr(t, "id", "") == "BEAM_COLOURS" for t in n.targets):
            return [tuple(c) for c in ast.literal_eval(n.value)]
    return [(0, 130, 200)]


def _ccw(pts):
    a = sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(pts, pts[1:] + pts[:1]))
    return list(pts) if a >= 0 else list(reversed(pts))


class _Solid(tuple):
    """A ("mesh", corners, faces, size) solid that is also a straight prism: .prism holds
    (its end outline, the vector to the other end), so save() writes it as an extrusion
    - a polysurface, as every other element - not as a mesh."""
    prism = None


def _with_prism(solid, outline, along):
    s = _Solid(solid)
    s.prism = ([tuple(p) for p in outline], tuple(along))
    return s


class RhinoModel:
    """The writer ifc_export.write drives: storey / prism / bar / element / finish / save."""

    def __init__(self, name: str):
        import rhino3dm
        self.r = rhino3dm
        self.name = name
        self.z = {}
        self.items = []                     # (layer path, solids, attributes) until save()

    # -- what ifc_export.write calls
    def storey(self, name: str, elevation: float) -> None:
        self.z[name] = float(elevation)

    def prism(self, outline, z0: float, height: float, holes=()):
        return ("prism", [tuple(p) for p in outline], float(z0), float(height), [list(h) for h in holes or []])

    def bar(self, a, b, width: float, depth: float, z_top: float):
        import math
        L = math.dist(a, b)
        ux, uy = (b[0] - a[0]) / L, (b[1] - a[1]) / L
        nx, ny = -uy * width / 2.0, ux * width / 2.0
        rect = [(a[0] + nx, a[1] + ny), (b[0] + nx, b[1] + ny), (b[0] - nx, b[1] - ny), (a[0] - nx, a[1] - ny)]
        return ("prism", rect, z_top - depth, depth, [], (round(width), round(depth)))

    def mesh(self, verts, faces, size=None):
        """A closed solid from corners and faces (a sloping slab): a mesh, faces of more
        than four corners cut into triangles."""
        out = []
        for f in faces:
            if len(f) <= 4:
                out.append(tuple(f))
            else:
                out += [(f[i], f[j], f[k]) for i, j, k in _triangles([verts[q][:2] for q in f])]
        return ("mesh", [tuple(v) for v in verts], out, size)

    def slanted(self, a, b, width: float, depth: float, z_top: float, z_top_end: float, size=None):
        """A beam sloping from z_top at a to z_top_end at b: a closed mesh of its 8 corners."""
        import math
        L = math.dist(a, b)
        nx, ny = -(b[1] - a[1]) / L * width / 2.0, (b[0] - a[0]) / L * width / 2.0
        c = [(a[0] - nx, a[1] - ny, z_top - depth), (a[0] + nx, a[1] + ny, z_top - depth),
             (b[0] + nx, b[1] + ny, z_top_end - depth), (b[0] - nx, b[1] - ny, z_top_end - depth),
             (a[0] - nx, a[1] - ny, z_top), (a[0] + nx, a[1] + ny, z_top),
             (b[0] + nx, b[1] + ny, z_top_end), (b[0] - nx, b[1] - ny, z_top_end)]
        faces = [(0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4), (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7)]
        # in Rhino: its side view (a parallelogram) carried across its width
        return _with_prism(("mesh", c, faces, size), [c[0], c[3], c[7], c[4]],
                           [c[1][i] - c[0][i] for i in range(3)])

    def leaned(self, outline, z0: float, height: float, base, top):
        """An outline in plan leaning as a wall elevation draws it: a closed mesh, its bottom
        moved across by base, its top by top (mm)."""
        pts = [(float(p[0]), float(p[1])) for p in outline]
        if len(pts) > 1 and math.dist(pts[0], pts[-1]) <= 0.01:
            pts.pop()
        pts = _ccw(pts)                     # anticlockwise: its faces outward (a wall's comes clockwise)
        n = len(pts)
        verts = [(x + float(base[0]), y + float(base[1]), z0) for x, y in pts] + \
                [(x + float(top[0]), y + float(top[1]), z0 + height) for x, y in pts]
        faces = [(i, (i + 1) % n, n + (i + 1) % n, n + i) for i in range(n)]
        if n == 4:
            faces += [(0, 3, 2, 1), (4, 5, 6, 7)]
        else:
            tri = _triangles(pts)
            faces += [(i, k, j) for i, j, k in tri] + [(n + i, n + j, n + k) for i, j, k in tri]
        s = _Solid(("mesh", verts, faces, None))
        s.brep = True
        return s

    def swept(self, a, b, profile, z_top: float, size=None):
        """A beam built to its drawn section: a closed mesh, the profile at each end."""
        import math
        L = math.dist(a, b)
        nx, ny = -(b[1] - a[1]) / L, (b[0] - a[0]) / L          # to the left of a -> b
        prof = [(float(u), float(v)) for u, v in profile]
        ends = [[(p[0] + nx * u, p[1] + ny * u, z_top + v) for u, v in prof] for p in (a, b)]
        n = len(prof)
        faces = [(i, (i + 1) % n, n + (i + 1) % n, n + i) for i in range(n)]
        tri = _triangles(prof)
        faces += [(i, k, j) for i, j, k in tri] + [(n + i, n + j, n + k) for i, j, k in tri]
        # in Rhino: the section carried along the beam
        return _with_prism(("mesh", ends[0] + ends[1], faces, size), ends[0],
                           [ends[1][0][i] - ends[0][0][i] for i in range(3)])

    def element(self, kind: str, storey_name: str, solids, mark: str = "", predefined: str = ".NOTDEFINED.",
                description: str = "", size_key: str = ""):
        desc = description or ""
        if kind == "IFCBEAM":
            group = "Hidden beams" if mark == "SLAB FOLD" else "Beams"
        else:
            group = {"IFCCOLUMN": "Pedestals" if desc == "pedestal" else "Columns", "IFCSLAB": "Slabs",
                     "IFCWALL": "Shear walls", "IFCFOOTING": "Rafts" if desc == "raft" else "Footings"}.get(kind, "Other")
        size = next((s[5] for s in solids if len(s) > 5), None) or \
            next((s[3] for s in solids if s[0] == "mesh" and s[3]), None)
        self.items.append({"level": storey_name, "group": group, "size": size, "solids": solids, "key": size_key,
                           "mark": "" if mark == "SLAB FOLD" else (mark or ""),
                           "note": desc[len("NEEDS DECISION: "):] if desc.startswith("NEEDS DECISION") else "",
                           "desc": "" if desc.startswith("NEEDS DECISION") else desc})

    def finish(self) -> None:
        pass

    # -- the file
    def save(self, path: str) -> None:
        r = self.r
        model = r.File3dm()
        model.Settings.ModelUnitSystem = r.UnitSystem.Millimeters
        # beam sizes coloured as the Revit build's filters: sorted (beams before hidden
        # beams, then by depth, then width) and given BEAM_COLOURS in that order
        palette = beam_colours()
        sizes = sorted({(it["group"] == "Hidden beams", it["size"][1], it["size"][0]) for it in self.items
                        if it["group"] in ("Beams", "Hidden beams") and it["size"]})
        colour_of = {s: palette[i % len(palette)] for i, s in enumerate(sizes)}
        # columns, sunk slabs and footings: one layer per size, coloured as the Revit
        # build's filters for them (size_colours, which leaves the beams' colours to them)
        by_size = size_colours.plan(it["key"] for it in self.items if it.get("key"))
        layers = {}

        def layer(path_parts, colour):
            key = "::".join(path_parts)
            if key in layers:
                return layers[key]
            parent = layer(path_parts[:-1], (0, 0, 0)) if len(path_parts) > 1 else None
            ly = r.Layer()
            ly.Name = path_parts[-1]
            ly.Color = tuple(colour) + (255,)
            if parent is not None:
                ly.ParentLayerId = model.Layers[parent].Id
            layers[key] = model.Layers.Add(ly)
            return layers[key]

        for it in self.items:
            z0 = self.z.get(it["level"], 0.0)
            parts = [it["group"]]                   # by element; its floor is its Level user text
            colour = TYPE_COLOURS.get(it["group"], (120, 120, 120))
            if it["group"] in ("Beams", "Hidden beams") and it["size"]:
                w, d = it["size"]
                parts.append(("Fold " if it["group"] == "Hidden beams" else "Beam ") + "%dx%d" % (w, d))
                colour = colour_of[(it["group"] == "Hidden beams", d, w)]
            elif it["group"] in ("Columns", "Pedestals", "Slabs", "Footings") and it.get("key"):
                parts.append(it["key"])
                colour = by_size[it["key"]]
            idx = layer(parts, colour)
            for s in it["solids"]:
                if s[0] == "mesh":
                    # a polysurface, as every other element: a straight prism (a sloping
                    # or sectioned beam, a sloping rectangular bay) as an extrusion; a
                    # mesh only for a solid that is no straight prism
                    ext = None
                    prism = getattr(s, "prism", None) or _prism_of(s[1], s[2])
                    if prism:
                        outline, along = prism
                        ext = self._prism([(x, y, z + z0) for x, y, z in outline], along)
                    if ext is None:
                        ext = r.Mesh()
                        for x, y, z in s[1]:
                            ext.Vertices.Add(x, y, z + z0)
                        for f in s[2]:
                            ext.Faces.AddFace(*f)
                        ext.Normals.ComputeNormals()
                        ext.Compact()
                        if getattr(s, "brep", False):
                            # a leaning column or wall: its flat faces as a polysurface
                            ext = r.Brep.CreateFromMesh(ext, True) or ext
                else:
                    _, outline, zb, h, holes = s[:5]
                    ext = self._extrusion(outline, z0 + zb, h, holes)
                if ext is None:
                    continue
                a = r.ObjectAttributes()
                a.LayerIndex = idx
                if it["mark"]:
                    a.Name = it["mark"]
                for k, v in (("Mark", it["mark"]), ("Kind", it["desc"] or it["group"][:-1].lower()),
                             ("Level", it["level"]), ("Size", "%dx%d" % it["size"] if it["size"] else ""),
                             ("Note", it["note"])):
                    if v:
                        a.SetUserString(k, v)
                if it["note"]:
                    # still needs a decision: red, as in Revit
                    a.ObjectColor = RED + (255,)
                    a.ColorSource = r.ObjectColorSource.ColorFromObject
                if isinstance(ext, r.Mesh):
                    model.Objects.AddMesh(ext, a)
                elif isinstance(ext, r.Brep):
                    model.Objects.AddBrep(ext, a)
                else:
                    model.Objects.AddExtrusion(ext, a)
        try:
            model.Strings.SetString("CAD to Revit", "made by the CAD to Revit app from " + self.name)
        except AttributeError:
            pass                            # older rhino3dm: no document text table
        if not model.Write(path, 8):
            raise RhinoError("The Rhino file could not be written: " + path)

    def _extrusion(self, outline, z, height, holes):
        r = self.r
        if len(outline) < 3 or height <= 0:
            return None

        def curve(pts):
            pl = r.Polyline([r.Point3d(x, y, z) for x, y in pts] + [r.Point3d(pts[0][0], pts[0][1], z)])
            return r.PolylineCurve(pl)
        ext = r.Extrusion.Create(curve(_ccw(outline)), height, True)
        if ext is None:
            return None
        # an opening is given in the profile's own plane (its coordinates, not the world's)
        plane = ext.GetProfilePlane(0)
        o, ax, ay = plane.Origin, plane.XAxis, plane.YAxis
        for h in holes:
            if len(h) < 3:
                continue
            local = []
            for x, y in list(reversed(_ccw(h))):
                dx, dy, dz = x - o.X, y - o.Y, z - o.Z
                local.append(r.Point3d(dx * ax.X + dy * ax.Y + dz * ax.Z, dx * ay.X + dy * ay.Y + dz * ay.Z, 0.0))
            try:
                ext.AddInnerProfile(r.PolylineCurve(r.Polyline(local + [local[0]])))
            except Exception:
                pass
        return ext

    def _prism(self, outline, along):
        """A straight prism as an extrusion: `outline` (3D, flat) carried by the vector
        `along`, which stands square to it; None when it does not."""
        import math
        r = self.r
        L = math.sqrt(sum(c * c for c in along))
        if len(outline) < 3 or L < 1e-6:
            return None
        n = [c / L for c in along]
        p0 = outline[0]

        def rel(p):
            return [p[i] - p0[i] for i in range(3)]

        def dot(u, v):
            return sum(u[i] * v[i] for i in range(3))
        if any(abs(dot(rel(p), n)) > 0.5 for p in outline):
            return None                                 # not square to its run
        if abs(n[2]) < 1e-9:
            x = [0.0, 0.0, 1.0]                         # an upright section: its frame square to the
        else:                                           # level, so the extrusion's box fits it
            x = next(([c / math.sqrt(dot(rel(p), rel(p))) for c in rel(p)] for p in outline[1:]
                      if dot(rel(p), rel(p)) > 1e-6), None)
        if x is None:
            return None
        y = [n[1] * x[2] - n[2] * x[1], n[2] * x[0] - n[0] * x[2], n[0] * x[1] - n[1] * x[0]]
        flat = _ccw([(dot(rel(p), x), dot(rel(p), y)) for p in outline])
        ext = r.Extrusion.Create(r.PolylineCurve(r.Polyline([r.Point3d(u, v, 0.0) for u, v in flat + flat[:1]])),
                                 L, True)
        if ext is None:
            return None
        ext.Transform(r.Transform.PlaneToPlane(r.Plane.WorldXY(), r.Plane(r.Point3d(*p0), r.Vector3d(*x),
                                                                         r.Vector3d(*y))))
        return ext


def _prism_of(verts, faces):
    """(end outline, vector to the other end) when the closed solid of `verts` / `faces`
    is a straight prism: two faces the same outline, one the other moved square to it,
    and every corner on one of them; None otherwise."""
    import math
    for i, f in enumerate(faces):
        if len(f) < 3 or 2 * len(f) != len(verts):
            continue
        p = [verts[k] for k in f]
        nrm = [0.0, 0.0, 0.0]
        for a, b in zip(p, p[1:] + p[:1]):              # Newell: the face's normal
            nrm[0] += (a[1] - b[1]) * (a[2] + b[2])
            nrm[1] += (a[2] - b[2]) * (a[0] + b[0])
            nrm[2] += (a[0] - b[0]) * (a[1] + b[1])
        ln = math.sqrt(sum(c * c for c in nrm))
        if ln < 1e-9:
            continue
        nrm = [c / ln for c in nrm]
        for g in faces[i + 1:]:
            if len(g) != len(f) or set(g) & set(f):
                continue
            q = [verts[k] for k in g]
            for start in range(len(q)):
                for step in (1, -1):
                    m = [q[(start + step * k) % len(q)] for k in range(len(q))]
                    v = [m[0][c] - p[0][c] for c in range(3)]
                    L = math.sqrt(sum(c * c for c in v))
                    if L < 1.0 or abs(abs(sum(v[c] * nrm[c] for c in range(3))) - L) > 0.5:
                        continue                        # not square to the face
                    if all(abs(m[k][c] - p[k][c] - v[c]) <= 0.5 for k in range(len(p)) for c in range(3)):
                        return p, v
    return None


def _triangles(poly) -> list[tuple[int, int, int]]:
    """Ear clipping: a simple polygon (a beam's section) as triangles of its corner indices."""
    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])
    idx = list(range(len(poly)))
    ccw = sum(poly[i][0] * poly[i - 1][1] - poly[i - 1][0] * poly[i][1] for i in idx) < 0
    if not ccw:
        idx.reverse()
    out = []
    while len(idx) > 3:
        for k in range(len(idx)):
            i, j, l = idx[k - 1], idx[k], idx[(k + 1) % len(idx)]
            if cross(poly[i], poly[j], poly[l]) <= 0:
                continue                                # a reflex corner is no ear
            if any(cross(poly[i], poly[j], poly[m]) > 0 and cross(poly[j], poly[l], poly[m]) > 0
                   and cross(poly[l], poly[i], poly[m]) > 0 for m in idx if m not in (i, j, l)):
                continue
            out.append((i, j, l))
            idx.pop(k)
            break
        else:
            break                                       # not simple: what is left as a fan
    out += [(idx[0], idx[k], idx[k + 1]) for k in range(1, len(idx) - 1)]
    return out


def write(run: str, path: str, decisions: dict | None = None, log=print, name: str = "") -> dict:
    """The analysis' model as a .3dm at `path` - the IFC file's elements, in Rhino."""
    try:
        import rhino3dm  # noqa: F401
    except ImportError:
        raise RhinoError("The Rhino writer (the free 'rhino3dm' library from McNeel) is not installed. "
                         "With internet, run:  python -m pip install rhino3dm")
    try:
        res = ifc_export.write(run, path, decisions, log=lambda *a: None, name=name, model_cls=RhinoModel)
    except ifc_export.IfcError as exc:
        raise RhinoError(str(exc).replace("IFC", "Rhino"))
    made = res["made"]
    log("RHINO Written %s: " % path + ", ".join(f"{v} {k}" for k, v in made.items() if v))
    if res["left_out"]:
        log("RHINO Left out (%d): %s%s" % (len(res["left_out"]), "; ".join(res["left_out"][:8]),
                                            " ..." if len(res["left_out"]) > 8 else ""))
    return res
