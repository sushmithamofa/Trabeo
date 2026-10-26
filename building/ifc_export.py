"""The model as an IFC file, written without Revit.

For a PC with no Revit, or for anyone who works in another BIM tool. Revit opens
the file directly (File > Open > IFC) or links it. The elements come in as
IFC objects: they look and schedule right, but they are less editable than the
native beams and columns the Revit build makes.

It reads exactly what the Revit build reads - each floor's model.json in
revit_build, the levels and the foundations - and keeps the same rules:

* nothing is placed on an assumed size, depth or height: a beam with no width or
  depth, a slab with no thickness, a column or wall with no level below it to
  rise from, a footing with no founding level is left out and listed;
* columns and walls rise from the level below to their own level; a floating
  column (FC) from its level to the one above;
* a beam's top is at its level plus its top offset; a slab's top at its level
  less its sunk depth;
* plan coordinates are measured from each floor's base point, as in Revit.

The file is IFC4, coordinates in millimetres. Every value is written by this
module - no IFC library is needed.
"""
from __future__ import annotations

import ast
import io
import json
import math
import os
import time
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REVIT_DIR = os.path.join(ROOT, "revit")
STOREY_HEIGHT_MM = 3000.0     # as revit\build_model.py: only for a provisional column with no level to reach
SAME_PLACE = 50.0             # mm, as revit\build_model.py: nearer counts as the same column
FC_CARRY_ON = False           # as revit\build_model.py: every plan's FC stands on its own floor (the user's rule)


class IfcError(RuntimeError):
    pass


# ---------------------------------------------------------------- the build's own rules
_EXTRAS = None


def _extras():
    """footing_boxes, raft_slabs, pedestal_box from revit\\build_extras.py - the same
    functions the Revit build uses, loaded from that file, not copied."""
    global _EXTRAS
    if _EXTRAS is None:
        path = os.path.join(REVIT_DIR, "build_extras.py")
        with io.open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        want = ("bottom_in_model", "footing_boxes", "raft_slabs", "pedestal_box")
        fns = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in want]
        ns: dict = {}
        exec(compile(ast.Module(fns, []), path, "exec"), ns)
        _EXTRAS = ns
    return _EXTRAS


def _is_floating(c) -> bool:
    return c.get("kind") == "column_float" or (c.get("mark") or "").upper().startswith("FC")


# ---------------------------------------------------------------- IFC writing
_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz_$"


def guid() -> str:
    """A new IFC GlobalId: a UUID in IFC's 22-character base-64."""
    n = uuid.uuid4().int
    out = []
    for _ in range(22):
        out.append(_CHARS[n % 64])
        n //= 64
    return "".join(reversed(out))


def _s(text) -> str:
    """An IFC string: quotes doubled, non-ASCII as \\X2\\ escapes."""
    t = str(text or "").replace("'", "''")
    out = []
    for ch in t:
        if ord(ch) < 32:
            continue
        if ord(ch) > 126:
            out.append("\\X2\\%04X\\X0\\" % ord(ch))
        elif ch == "\\":
            out.append("\\\\")
        else:
            out.append(ch)
    return "'" + "".join(out) + "'"


def _f(x: float) -> str:
    s = "%.4f" % float(x)
    s = s.rstrip("0")
    return s if not s.endswith(".") else s + "0" if s != "-0." else "0."


class _Writer:
    def __init__(self):
        self.lines: list[str] = []

    def add(self, entity: str) -> str:
        n = len(self.lines) + 1
        self.lines.append(f"#{n}={entity};")
        return f"#{n}"

    def pt(self, *c):
        return self.add("IFCCARTESIANPOINT((" + ",".join(_f(v) for v in c) + "))")

    def dir(self, *c):
        return self.add("IFCDIRECTION((" + ",".join(_f(v) for v in c) + "))")

    def axis(self, origin=(0.0, 0.0, 0.0), z=None, x=None):
        return self.add("IFCAXIS2PLACEMENT3D(%s,%s,%s)" % (
            self.pt(*origin), self.dir(*z) if z else "$", self.dir(*x) if x else "$"))

    def text(self, header: dict) -> str:
        head = ("ISO-10303-21;\nHEADER;\nFILE_DESCRIPTION(('ViewDefinition [ReferenceView_V1.2]'),'2;1');\n"
                "FILE_NAME(%s,%s,(%s),(%s),%s,%s,'');\nFILE_SCHEMA(('IFC4'));\nENDSEC;\nDATA;\n" % (
                    _s(header["name"]), _s(time.strftime("%Y-%m-%dT%H:%M:%S")), _s(header.get("author", "")),
                    _s(header.get("org", "")), _s("CAD to Revit"), _s("CAD to Revit IFC export")))
        return head + "\n".join(self.lines) + "\nENDSEC;\nEND-ISO-10303-21;\n"


class _Model:
    """Project, site, building, storeys and the elements in them."""

    def __init__(self, name: str):
        w = self.w = _Writer()
        self.origin = w.axis()
        self.ctx = w.add("IFCGEOMETRICREPRESENTATIONCONTEXT($,'Model',3,1.E-05,%s,$)" % self.origin)
        self.body = w.add("IFCGEOMETRICREPRESENTATIONSUBCONTEXT('Body','Model',*,*,*,*,%s,$,.MODEL_VIEW.,$)" % self.ctx)
        units = [w.add("IFCSIUNIT(*,.LENGTHUNIT.,.MILLI.,.METRE.)"),
                 w.add("IFCSIUNIT(*,.AREAUNIT.,$,.SQUARE_METRE.)"),
                 w.add("IFCSIUNIT(*,.VOLUMEUNIT.,$,.CUBIC_METRE.)"),
                 w.add("IFCSIUNIT(*,.PLANEANGLEUNIT.,$,.RADIAN.)")]
        ua = w.add("IFCUNITASSIGNMENT((%s))" % ",".join(units))
        self.project = w.add("IFCPROJECT(%s,$,%s,%s,$,$,$,(%s),%s)" % (
            _s(guid()), _s(name), _s("Structural model from CAD to Revit"), self.ctx, ua))
        self.site_place = w.add("IFCLOCALPLACEMENT($,%s)" % w.axis())
        self.site = w.add("IFCSITE(%s,$,'Site',$,$,%s,$,$,.ELEMENT.,$,$,$,$,$)" % (_s(guid()), self.site_place))
        self.bldg_place = w.add("IFCLOCALPLACEMENT(%s,%s)" % (self.site_place, w.axis()))
        self.bldg = w.add("IFCBUILDING(%s,$,%s,$,$,%s,$,$,.ELEMENT.,$,$,$)" % (
            _s(guid()), _s(name), self.bldg_place))
        w.add("IFCRELAGGREGATES(%s,$,$,$,%s,(%s))" % (_s(guid()), self.project, self.site))
        w.add("IFCRELAGGREGATES(%s,$,$,$,%s,(%s))" % (_s(guid()), self.site, self.bldg))
        self.concrete = w.add("IFCMATERIAL('Concrete',$,'Concrete')")
        self.storeys: dict[str, tuple[str, str, float]] = {}      # name -> (storey, placement, elevation)
        self.contents: dict[str, list[str]] = {}
        self.all: list[str] = []
        self.styled: list = []                  # (solids, size label, needs a decision) for the colours

    def storey(self, name: str, elevation: float):
        if name in self.storeys:
            return self.storeys[name]
        w = self.w
        place = w.add("IFCLOCALPLACEMENT(%s,%s)" % (self.bldg_place, w.axis((0.0, 0.0, elevation))))
        st = w.add("IFCBUILDINGSTOREY(%s,$,%s,$,$,%s,$,$,.ELEMENT.,%s)" % (
            _s(guid()), _s(name), place, _f(elevation)))
        self.storeys[name] = (st, place, elevation)
        self.contents[name] = []
        return self.storeys[name]

    def _shape(self, solids: list[str]) -> str:
        w = self.w
        kind = "Brep" if solids and all(s in self.breps for s in solids) else "SweptSolid"
        rep = w.add("IFCSHAPEREPRESENTATION(%s,'Body','%s',(%s))" % (self.body, kind, ",".join(solids)))
        return w.add("IFCPRODUCTDEFINITIONSHAPE($,$,(%s))" % rep)

    breps: set = set()

    def mesh(self, verts, faces, size=None) -> str:
        """A closed solid from its corners and faces (a sloping slab): IfcFacetedBrep."""
        w = self.w
        ids = [w.add("IFCCARTESIANPOINT((%s,%s,%s))" % (_f(x), _f(y), _f(z))) for x, y, z in verts]
        fs = []
        for f in faces:
            loop = w.add("IFCPOLYLOOP((%s))" % ",".join(ids[i] for i in f))
            fs.append(w.add("IFCFACE((%s))" % w.add("IFCFACEOUTERBOUND(%s,.T.)" % loop)))
        sid = w.add("IFCFACETEDBREP(%s)" % w.add("IFCCLOSEDSHELL((%s))" % ",".join(fs)))
        self.breps = self.breps | {sid}
        return sid

    def slanted(self, a, b, width: float, depth: float, z_top: float, z_top_end: float, size=None) -> str:
        """A beam sloping from z_top at a to z_top_end at b (building section): its section
        width x depth square to its own inclined axis, the top edge at the given heights."""
        w = self.w
        dx, dy, dz = b[0] - a[0], b[1] - a[1], z_top_end - z_top
        L3 = math.sqrt(dx * dx + dy * dy + dz * dz)
        Lh = math.hypot(dx, dy)
        zx, zy, zz = dx / L3, dy / L3, dz / L3
        xx, xy = -dy / Lh, dx / Lh                                  # across, horizontal
        yx, yy, yz = -zz * dx / Lh, -zz * dy / Lh, Lh / L3           # up, square to the axis
        prof = w.add("IFCRECTANGLEPROFILEDEF(.AREA.,$,%s,%s,%s)" % (
            w.add("IFCAXIS2PLACEMENT2D(%s,$)" % w.add("IFCCARTESIANPOINT((0.,0.))")), _f(width), _f(depth)))
        c = (a[0] - yx * depth / 2.0, a[1] - yy * depth / 2.0, z_top - yz * depth / 2.0)
        pos = w.axis(c, z=(zx, zy, zz), x=(xx, xy, 0.0))
        return w.add("IFCEXTRUDEDAREASOLID(%s,%s,%s,%s)" % (prof, pos, w.dir(0.0, 0.0, 1.0), _f(L3)))

    def _polyline(self, pts) -> str:
        """A closed outline. Repeated points, and a last point repeating the first,
        are dropped: a zero-length edge makes an invalid profile."""
        w = self.w
        clean = []
        for p in pts:
            if not clean or math.dist(p, clean[-1]) > 0.01:
                clean.append((float(p[0]), float(p[1])))
        if len(clean) > 1 and math.dist(clean[0], clean[-1]) <= 0.01:
            clean.pop()
        ids = [w.add("IFCCARTESIANPOINT((%s,%s))" % (_f(x), _f(y))) for x, y in clean]
        return w.add("IFCPOLYLINE((%s))" % ",".join(ids + ids[:1]))

    def prism(self, outline, z0: float, height: float, holes=()) -> str:
        """An outline in plan (mm, storey coordinates) extruded up from z0."""
        w = self.w
        outer = self._polyline(outline)
        if holes:
            inner = [self._polyline(h) for h in holes if len(h) >= 3]
            prof = w.add("IFCARBITRARYPROFILEDEFWITHVOIDS(.AREA.,$,%s,(%s))" % (outer, ",".join(inner)))
        else:
            prof = w.add("IFCARBITRARYCLOSEDPROFILEDEF(.AREA.,$,%s)" % outer)
        return w.add("IFCEXTRUDEDAREASOLID(%s,%s,%s,%s)" % (
            prof, w.axis((0.0, 0.0, z0)), w.dir(0.0, 0.0, 1.0), _f(height)))

    def bar(self, a, b, width: float, depth: float, z_top: float) -> str:
        """A rectangular section width x depth along a to b, its top at z_top."""
        w = self.w
        dx, dy = b[0] - a[0], b[1] - a[1]
        L = math.hypot(dx, dy)
        ux, uy = dx / L, dy / L
        prof = w.add("IFCRECTANGLEPROFILEDEF(.AREA.,$,%s,%s,%s)" % (
            w.add("IFCAXIS2PLACEMENT2D(%s,$)" % w.add("IFCCARTESIANPOINT((0.,0.))")), _f(width), _f(depth)))
        # local Z runs along the beam, local X across it: local Y is then up
        pos = w.axis((a[0], a[1], z_top - depth / 2.0), z=(ux, uy, 0.0), x=(-uy, ux, 0.0))
        return w.add("IFCEXTRUDEDAREASOLID(%s,%s,%s,%s)" % (prof, pos, w.dir(0.0, 0.0, 1.0), _f(L)))

    def swept(self, a, b, profile, z_top: float, size=None) -> str:
        """A beam built to its drawn section: the profile ([u, v] mm, u to the left of a -> b,
        v down from the top) carried from a to b - the frame bar() uses."""
        w = self.w
        dx, dy = b[0] - a[0], b[1] - a[1]
        L = math.hypot(dx, dy)
        ux, uy = dx / L, dy / L
        prof = w.add("IFCARBITRARYCLOSEDPROFILEDEF(.AREA.,$,%s)" % self._polyline(profile))
        pos = w.axis((a[0], a[1], z_top), z=(ux, uy, 0.0), x=(-uy, ux, 0.0))
        return w.add("IFCEXTRUDEDAREASOLID(%s,%s,%s,%s)" % (prof, pos, w.dir(0.0, 0.0, 1.0), _f(L)))

    def element(self, kind: str, storey_name: str, solids: list[str], mark: str = "", predefined: str = ".NOTDEFINED.",
                description: str = "", size_key: str = "") -> str:
        w = self.w
        st, place, _z = self.storeys[storey_name]
        lp = w.add("IFCLOCALPLACEMENT(%s,%s)" % (place, w.axis()))
        e = w.add("%s(%s,$,%s,%s,$,%s,%s,%s,%s)" % (
            kind, _s(guid()), _s(mark or kind[3:].title()), _s(description) if description else "$", lp,
            self._shape(solids), _s(mark) if mark else "$", predefined))
        self.contents[storey_name].append(e)
        self.all.append(e)
        self.styled.append((solids, size_key, (description or "").startswith("NEEDS DECISION")))
        return e

    def _colours(self) -> None:
        """Each size its colour, as the Revit build's view filters (size_colours); an
        element still needing a decision red, as in Revit."""
        from building import size_colours
        w = self.w
        plan = size_colours.plan(k for _s, k, _p in self.styled if k)
        style = {}

        def style_for(name, rgb):
            if name not in style:
                c = w.add("IFCCOLOURRGB($,%s,%s,%s)" % tuple(_f(v / 255.0) for v in rgb))
                shade = w.add("IFCSURFACESTYLESHADING(%s,$)" % c)
                style[name] = w.add("IFCSURFACESTYLE(%s,.BOTH.,(%s))" % (_s(name), shade))
            return style[name]
        for solids, key, provisional in self.styled:
            if provisional:
                st = style_for("Decision needed", size_colours.RED)
            elif key:
                st = style_for(key, plan[key])
            else:
                continue
            for s in solids:
                w.add("IFCSTYLEDITEM(%s,(%s),$)" % (s, st))

    def finish(self) -> None:
        w = self.w
        self._colours()
        if self.storeys:
            w.add("IFCRELAGGREGATES(%s,$,$,$,%s,(%s))" % (
                _s(guid()), self.bldg, ",".join(s[0] for s in self.storeys.values())))
        for name, els in self.contents.items():
            if els:
                w.add("IFCRELCONTAINEDINSPATIALSTRUCTURE(%s,$,$,$,(%s),%s)" % (
                    _s(guid()), ",".join(els), self.storeys[name][0]))
        if self.all:
            w.add("IFCRELASSOCIATESMATERIAL(%s,$,$,$,(%s),%s)" % (_s(guid()), ",".join(self.all), self.concrete))


# ---------------------------------------------------------------- the export
def _arc_points(a, m, b, n=8):
    """Points along the circle through a, m, b, from a to b (n pieces)."""
    ax, ay = a
    bx, by = b
    mx, my = m
    d = 2.0 * (ax * (my - by) + mx * (by - ay) + bx * (ay - my))
    if abs(d) < 1e-9:
        return [a, b]
    ux = ((ax * ax + ay * ay) * (my - by) + (mx * mx + my * my) * (by - ay) + (bx * bx + by * by) * (ay - my)) / d
    uy = ((ax * ax + ay * ay) * (bx - mx) + (mx * mx + my * my) * (ax - bx) + (bx * bx + by * by) * (mx - ax)) / d
    r = math.hypot(ax - ux, ay - uy)
    t0, tm, t1 = (math.atan2(p[1] - uy, p[0] - ux) for p in (a, m, b))

    def ccw(s, e):
        return (e - s) % (2 * math.pi)
    sweep = ccw(t0, t1) if ccw(t0, tm) <= ccw(t0, t1) else -ccw(t1, t0)
    inner = [(ux + r * math.cos(t0 + sweep * k / n), uy + r * math.sin(t0 + sweep * k / n)) for k in range(1, n)]
    return [tuple(a)] + inner + [tuple(b)]          # the ends exactly where the beam ends


def sloped_slab(pts, slope, th: float, shift=(0.0, 0.0)):
    """A sloping slab (building section) as a closed solid: its top through the slope's
    heights (z0 at p0 to z1 at p1 along dir, held beyond them), its thickness under it.
    pts in storey coordinates (drawing minus shift). Returns (corners, faces): faces are
    corner-index lists - the top, the bottom, and one quad per edge."""
    ox, oy = slope["origin"][0] - shift[0], slope["origin"][1] - shift[1]
    lo, hi = sorted((slope["p0"], slope["p1"]))

    def z(p):
        q = min(max((p[0] - ox) * slope["dir"][0] + (p[1] - oy) * slope["dir"][1], lo), hi)
        return slope["z0"] + (q - slope["p0"]) * (slope["z1"] - slope["z0"]) / (slope["p1"] - slope["p0"])
    ring = [tuple(p[:2]) for p in pts]
    if len(ring) > 3 and math.dist(ring[0], ring[-1]) < 0.01:
        ring = ring[:-1]
    if sum(ring[i][0] * ring[i - 1][1] - ring[i - 1][0] * ring[i][1] for i in range(len(ring))) > 0:
        ring = ring[::-1]                                   # anticlockwise seen from above
    n = len(ring)
    verts = [(x, y, z((x, y))) for x, y in ring] + [(x, y, z((x, y)) - th) for x, y in ring]
    faces = [list(range(n)), list(range(2 * n - 1, n - 1, -1))]
    faces += [[i, (i + 1) % n, n + (i + 1) % n, n + i][::-1] for i in range(n)]
    return verts, faces


def arc_band(a, m, b, width: float, n: int = 24) -> list[tuple[float, float]]:
    """The plan outline of a curved beam: its arc (through a, m, b) offset half its width
    to each side - one face out, the other back. Built as one solid, not as straight boxes."""
    pts = _arc_points(a, m, b, n)
    left, right = [], []
    for i, p in enumerate(pts):
        q0, q1 = pts[max(i - 1, 0)], pts[min(i + 1, len(pts) - 1)]
        tx, ty = q1[0] - q0[0], q1[1] - q0[1]
        L = math.hypot(tx, ty) or 1.0
        nx, ny = -ty / L * width / 2.0, tx / L * width / 2.0
        left.append((p[0] + nx, p[1] + ny))
        right.append((p[0] - nx, p[1] - ny))
    return left + right[::-1]


def write(run: str, path: str, decisions: dict | None = None, log=print, name: str = "",
          model_cls=None) -> dict:
    """Write the analysis' model as IFC at `path`. Returns what was written and left out.

    Raises IfcError when there is nothing to write or a level has no elevation - the
    same stops as building a new Revit file.

    model_cls: another writer with the same storey / prism / bar / element / finish /
    save methods (rhino_export's), given the very same elements and rules; IFC when None."""
    from building import revit_mcp
    from building.size_colours import column_label, footing_label, sunk_label
    from building.walls import wall_runs
    rir = revit_mcp._rir()
    floors = rir.exported_floors(run)
    if not floors:
        raise IfcError("This analysis has no floor to write. Export to Revit first.")
    targets = revit_mcp.level_targets(run, decisions)
    missing = revit_mcp.levels_needed(targets, {})
    if missing:
        raise IfcError("These levels need an elevation first: %s. The drawings give none, so nothing was "
                       "written. Enter them on the Export page under 'Floor levels'." % ", ".join(missing))
    order = [n for n, _z, _s in targets]                  # bottom up, the level below first
    elev = {n: float(z) for n, z, _s in targets}

    def neighbour(name, step):
        i = order.index(name) if name in order else -1
        j = i + step
        return order[j] if i >= 0 and 0 <= j < len(order) else None

    # A floor's columns and walls stand between the level below and its own. Two levels
    # entered at one height (Level 0 and Level 1 both 4000) leave them no height: the
    # file came out without a floor's columns and walls. Stop and say which, as for a
    # missing elevation, rather than write a file with elements missing.
    flat = []
    for n in order:
        b_ = neighbour(n, -1)
        if b_ is not None and elev[n] - elev[b_] <= 1.0:
            flat.append(f"'{n}' at {elev[n]:.0f} mm is not above '{b_}' at {elev[b_]:.0f} mm")
    if flat:
        raise IfcError("The floor levels do not rise: %s. The columns and walls of a floor stand between "
                       "the level below and its own, so they would have no height and be left out. Nothing "
                       "was written. Correct the heights on the Export page under 'Floor levels' (each in mm "
                       "from the drawings' ±0, the level below lower than the floor)." % "; ".join(flat))

    m = (model_cls or _Model)(name or os.path.splitext(os.path.basename(path))[0])
    for n in order:
        m.storey(n, elev[n])
    made = {"columns": 0, "beams": 0, "slabs": 0, "walls": 0, "folds": 0, "footings": 0, "pedestals": 0,
            "rafts": 0}
    left: list[str] = []
    rising: dict[str, list] = {}           # level -> (x, y) of the floating columns rising to it

    for label, mj, lvl in floors:
        with io.open(mj, encoding="utf-8") as f:
            d = json.load(f)
        lvl = lvl or d.get("level_name") or ""
        if lvl not in elev:
            left.append(f"{label}: its level '{lvl}' is not among the levels")
            continue
        z = elev[lvl]
        bp = d.get("base_point") or [0.0, 0.0]
        ox, oy = float(bp[0]), float(bp[1])
        below, above = neighbour(lvl, -1), neighbour(lvl, +1)

        prov_ok = bool(d.get("provisional_ok"))

        def why(row, extra=""):
            """An element's description: a provisional one says what needs deciding."""
            notes = "; ".join(x for x in (row.get("provisional") or "", extra) if x)
            return ("NEEDS DECISION: " + notes) if notes else ""

        for c in d.get("columns") or []:
            fl = _is_floating(c)
            if FC_CARRY_ON and fl and any(abs(c["x"] - ox - bx) + abs(c["y"] - oy - by) <= SAME_PLACE
                          for bx, by in rising.get(lvl, [])):
                # the floor below's FC carrying on, already standing up to this level: one
                # column, as the Revit build has it (an FC with a column under it continues)
                continue
            if fl and above is not None:
                rising.setdefault(above, []).append((c["x"] - ox, c["y"] - oy))
            other = above if fl else below
            note = ""
            if other is None:
                gap = f"no level {'above' if fl else 'below'} to {'rise to' if fl else 'rise from'}"
                if not prov_ok:
                    left.append(f"column {c.get('mark') or '?'} ({lvl}): {gap}")
                    continue
                note = f"{gap} - built {STOREY_HEIGHT_MM:.0f} mm {'above' if fl else 'below'}"
                z0, z1 = (z, z + STOREY_HEIGHT_MM) if fl else (z - STOREY_HEIGHT_MM, z)
            else:
                z0, z1 = (z, elev[other]) if fl else (elev[other], z)
            if not fl and c.get("top_mm"):
                z1 += float(c["top_mm"])                     # ends under a lowered beam (building section)
            if z1 - z0 <= 1.0:
                left.append(f"column {c.get('mark') or '?'} ({lvl}): no height between its levels")
                continue
            outline = c.get("outline") or []
            if len(outline) < 3:
                w_, d_ = float(c.get("width") or 0), float(c.get("depth") or 0)
                if not w_ or not d_:
                    left.append(f"column {c.get('mark') or '?'} ({lvl}): no size")
                    continue
                r = math.radians(float(c.get("rotation") or 0.0))
                ex, ey = math.cos(r), math.sin(r)            # along the long side
                corners = [(-d_ / 2, -w_ / 2), (d_ / 2, -w_ / 2), (d_ / 2, w_ / 2), (-d_ / 2, w_ / 2)]
                outline = [(c["x"] + u * ex - v * ey, c["y"] + u * ey + v * ex) for u, v in corners]
            pts = [(p[0] - ox, p[1] - oy) for p in outline]
            m.element("IFCCOLUMN", lvl, [m.prism(pts, z0 - z, z1 - z0)], c.get("mark") or "", ".COLUMN.",
                      why(c, note) or ("floating column" if fl else ""),
                      size_key=column_label(c["width"], c["depth"]) if c.get("width") and c.get("depth") else "")
            made["columns"] += 1

        # beams turning an L corner are built on to fill it, as Revit joins them
        from building.beam_ends import corner_extensions, carried
        plain = [b for b in d.get("beams") or [] if b.get("width") and b.get("depth") and not b.get("mid")
                 and not (b.get("section") or {}).get("profile")]
        ends = {id(b): ext for b, ext in zip(plain, corner_extensions(
            [(b["start"], b["end"], float(b["width"])) for b in plain]))}
        for b in d.get("beams") or []:
            wd, dp = b.get("width"), b.get("depth")
            if not wd or not dp:
                left.append(f"beam {b.get('mark') or '?'} ({lvl}): no " +
                            " and ".join(k for k, v in (("width", wd), ("depth", dp)) if not v))
                continue
            bs, be = carried(b["start"], b["end"], ends.get(id(b), [0.0, 0.0]))
            a = (bs[0] - ox, bs[1] - oy)
            e = (be[0] - ox, be[1] - oy)
            if math.dist(a, e) < 1.0:
                continue
            top = float(b.get("top_mm") or 0.0)            # storey coordinates: the level is 0
            if b.get("mid"):
                mid = (b["mid"][0] - ox, b["mid"][1] - oy)
                solid = m.prism(arc_band(a, mid, e, float(wd)), top - float(dp), float(dp))
                if isinstance(solid, tuple):          # the Rhino writer: its size, for the size layers
                    solid = solid + ((round(float(wd)), round(float(dp))),)
                solids = [solid]
            elif (b.get("section") or {}).get("profile"):
                # built to its drawn section (SECTION X-X), not as a box
                solids = [m.swept(a, e, b["section"]["profile"], top, (round(float(wd)), round(float(dp))))]
            elif b.get("top_end_mm") is not None and abs(float(b["top_end_mm"]) - top) > 1.0:
                # sloping with a slab (building section)
                solids = [m.slanted(a, e, float(wd), float(dp), top, float(b["top_end_mm"]),
                                    (round(float(wd)), round(float(dp))))]
            else:
                solids = [m.bar(a, e, float(wd), float(dp), top)]
            m.element("IFCBEAM", lvl, solids, b.get("mark") or "", ".BEAM.",
                      why(b) or ("girder" if b.get("kind") == "girder" else ""),
                      size_key="Beam %dx%d" % (round(float(wd)), round(float(dp))))
            made["beams"] += 1

        for fd in d.get("folds") or []:
            if not fd.get("width") or not fd.get("depth"):
                continue
            a = (fd["start"][0] - ox, fd["start"][1] - oy)
            e = (fd["end"][0] - ox, fd["end"][1] - oy)
            if math.dist(a, e) < 1.0:
                continue
            m.element("IFCBEAM", lvl, [m.bar(a, e, float(fd["width"]), float(fd["depth"]),
                                             float(fd.get("top_mm") or 0.0))], "SLAB FOLD", ".BEAM.",
                      why(fd) or "slab fold",
                      size_key="Fold %dx%d" % (round(float(fd["width"])), round(float(fd["depth"]))))
            made["folds"] += 1

        for s in d.get("slabs") or []:
            th = s.get("thickness")
            outline = s.get("outline") or []
            if not th or len(outline) < 3:
                left.append(f"slab {s.get('mark') or '?'} ({lvl}): " + ("no thickness" if not th else "no outline"))
                continue
            top = -float(s.get("sunk_mm") or 0.0)
            pts = [(p[0] - ox, p[1] - oy) for p in outline]
            holes = [[(p[0] - ox, p[1] - oy) for p in h] for h in (s.get("holes") or [])]
            if s.get("slope") and not holes:
                # sloping (building section): a closed solid, its top through the beams' heights
                solid = m.mesh(*sloped_slab(pts, s["slope"], float(th), (ox, oy)))
            else:
                solid = m.prism(pts, top - float(th), float(th), holes)
            m.element("IFCSLAB", lvl, [solid], s.get("mark") or "",
                      ".FLOOR.", why(s) or (("sunk %d mm" % round(s["sunk_mm"])) if s.get("sunk_mm") else ""),
                      size_key=sunk_label(s["sunk_mm"]) if float(s.get("sunk_mm") or 0.0) >= 1.0 else "")
            made["slabs"] += 1

        for wl in d.get("walls") or []:
            if below is None and not prov_ok:
                left.append(f"wall {wl.get('mark') or '?'} ({lvl}): no level below to rise from")
                continue
            if wl.get("start") and wl.get("end"):
                if not wl.get("thickness"):
                    left.append(f"wall {wl.get('mark') or '?'} ({lvl}): no thickness")
                    continue
                runs = [(wl["start"], wl["end"], float(wl["thickness"]))]
            elif wl.get("outline"):
                runs = wall_runs(wl["outline"])
            else:
                runs = []
            if not runs:
                left.append(f"wall {wl.get('mark') or '?'} ({lvl}): its outline does not split into straight runs")
                continue
            h = z - elev[below] if below is not None else STOREY_HEIGHT_MM
            if h <= 1.0:
                # never counted as written with no height (the Rhino file had none of them)
                left.append(f"wall {wl.get('mark') or '?'} ({lvl}): no height between its levels")
                continue
            solids = []
            for a, e, t in runs:
                a2, e2 = (a[0] - ox, a[1] - oy), (e[0] - ox, e[1] - oy)
                L = math.dist(a2, e2)
                if L < 1.0:
                    continue
                ux, uy = (e2[0] - a2[0]) / L, (e2[1] - a2[1]) / L
                nx, ny = -uy * t / 2, ux * t / 2
                rect = [(a2[0] + nx, a2[1] + ny), (e2[0] + nx, e2[1] + ny), (e2[0] - nx, e2[1] - ny),
                        (a2[0] - nx, a2[1] - ny)]
                solids.append(m.prism(rect, -h, h))
            if solids:
                m.element("IFCWALL", lvl, solids, wl.get("mark") or "", ".SOLIDWALL.",
                          why(wl, "" if below is not None else
                              f"no level below to rise from - built {STOREY_HEIGHT_MM:.0f} mm tall"))
                made["walls"] += 1

    fj = rir.foundations_file(run)
    if fj:
        with io.open(fj, encoding="utf-8") as f:
            fd = json.load(f)
        lvl = fd.get("level_name") or ""
        if lvl in elev:
            x = _extras()
            z = elev[lvl]
            bp = fd.get("base_point") or [0.0, 0.0]
            ox, oy = float(bp[0]), float(bp[1])
            items = fd.get("items") or []
            # the model's datum is the drawings': a founding level is used as given
            boxes = x["footing_boxes"](items, z, z)
            for bx in boxes:
                cx, cy = bx["x"] - ox, bx["y"] - oy
                hx, hy = bx["size_x"] / 2, bx["size_y"] / 2
                rect = [(cx - hx, cy - hy), (cx + hx, cy - hy), (cx + hx, cy + hy), (cx - hx, cy + hy)]
                if bx.get("rotation"):
                    # a foundation sheet drawn turned against the plan: the footing turned with it
                    a = math.radians(bx["rotation"])
                    rect = [(cx + (x - cx) * math.cos(a) - (y - cy) * math.sin(a),
                             cy + (x - cx) * math.sin(a) + (y - cy) * math.cos(a)) for x, y in rect]
                m.element("IFCFOOTING", lvl, [m.prism(rect, bx["bottom"] - z, bx["top"] - bx["bottom"])],
                          bx["mark"], ".PAD_FOOTING.", "footing step" if bx["kind"] == "step" else "",
                          size_key=footing_label(bx["kind"], bx["size_x"], bx["size_y"], bx["thickness"]))
                made["footings"] += 1
            for it in items:
                if it.get("type") != "pedestal":
                    continue
                p = x["pedestal_box"](it, boxes, z)
                if p is None:
                    left.append(f"pedestal {it.get('mark') or '?'}: no footing under it with a founding level")
                    continue
                cx, cy = p["x"] - ox, p["y"] - oy
                hx, hy = p["size_x"] / 2, p["size_y"] / 2
                rect = [(cx - hx, cy - hy), (cx + hx, cy - hy), (cx + hx, cy + hy), (cx - hx, cy + hy)]
                m.element("IFCCOLUMN", lvl, [m.prism(rect, p["bottom"] - z, p["top"] - p["bottom"])],
                          p["mark"], ".COLUMN.", "pedestal", size_key=column_label(p["size_x"], p["size_y"]))
                made["pedestals"] += 1
            for r in x["raft_slabs"](items, z, z):
                pts = [(q[0] - ox, q[1] - oy) for q in r["outline"]]
                m.element("IFCFOOTING", lvl, [m.prism(pts, r["top"] - r["thickness"] - z, r["thickness"])],
                          r["mark"], ".FOOTING_BEAM.", "raft")
                made["rafts"] += 1
            for it in items:
                if it.get("type") in ("footing", "raft") and it.get("bottom_mm") is None:
                    left.append(f"{it['type']} {it.get('mark') or '?'}: no founding level")
            left += [str(s) for s in fd.get("not_placed") or []]
        else:
            left.append(f"foundations: their level '{lvl}' is not among the levels")

    if not any(made.values()):
        raise IfcError("Nothing could be written: every element waits for a decision. See 'Before you build'.")
    m.finish()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    if model_cls is not None:
        m.save(path)
        return {"path": path, "made": made, "left_out": left, "levels": [(n, elev[n]) for n in order]}
    with io.open(path, "w", encoding="ascii", newline="\n") as f:
        f.write(m.w.text({"name": os.path.basename(path)}))
    log("IFC Written %s: " % path + ", ".join(f"{v} {k}" for k, v in made.items() if v))
    if left:
        log("IFC Left out (%d): %s%s" % (len(left), "; ".join(left[:8]), " ..." if len(left) > 8 else ""))
    return {"path": path, "made": made, "left_out": left, "levels": [(n, elev[n]) for n in order]}
