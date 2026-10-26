r"""The architect's 3D model, shown over the structure in Review's 3D view - for checking
the structure against the architecture and writing queries. Display only: it is never
read by the linker, the export, or any Revit / IFC / Rhino file the app writes.

Everything lives in the project folder:

    PROJECT\architecture\models.json         the models, their line-up per drawing set
    PROJECT\architecture\A1\source.ifc       the file as received (kept: a newer app reads it again)
    PROJECT\architecture\A1\geometry.bin     its triangles: float32 positions, then uint32 indices
    PROJECT\architecture\A1\meta.json        groups (layers / kinds), floors, elements, units

Read: IFC (any version ifcopenshell reads - installed the first time an IFC is added),
Rhino .3dm (the display meshes Rhino saved, blocks opened, extrusions built here),
glTF .glb / .gltf with the data inside, and OBJ.

Positions are kept in the file's own units and axes, relative to the middle of the model
(so float32 keeps them to well under a millimetre); meta.json says what one unit is in mm
and which way is up, and says whether the file itself said so ("sure") or the size of the
model suggested it - then the page asks the user to confirm.

    python -m building.arch_model FILE OUT_FOLDER     read a file (for testing)
"""
from __future__ import annotations

import datetime
import json
import math
import os
import re
import shutil
import struct
import sys
import threading
import traceback

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

DIR = "architecture"
FORMATS = {".ifc": "IFC", ".3dm": "Rhino", ".glb": "glTF", ".gltf": "glTF", ".obj": "OBJ"}
UPLOAD_MB = 2048
ARC_SEGMENTS = 24                     # a full circle of a Rhino extrusion's profile, in pieces
_lock = threading.Lock()              # models.json
_work = threading.Lock()              # one model read at a time


# ---------------------------------------------------------------- storage
def folder(project: str, *sub: str) -> str:
    return os.path.join(project, DIR, *sub)


def load(project: str) -> dict:
    try:
        with open(folder(project, "models.json"), encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        d = {}
    d.setdefault("models", [])
    d.setdefault("next", 1 + max([int(m["id"][1:]) for m in d["models"] if re.fullmatch(r"A\d+", m.get("id", ""))]
                                 or [0]))
    return d


def save(project: str, d: dict) -> None:
    os.makedirs(folder(project), exist_ok=True)
    tmp = folder(project, "models.json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=1)
    os.replace(tmp, folder(project, "models.json"))


def _now() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M")


def _update(project: str, mid: str, **kw) -> dict | None:
    with _lock:
        d = load(project)
        m = next((x for x in d["models"] if x["id"] == mid), None)
        if m is None:
            return None
        m.update(kw)
        save(project, d)
        return m


def model_path(project: str, mid: str, name: str) -> str:
    """A model's own file, or "" (only the files this module writes)."""
    if not re.fullmatch(r"A\d+", mid or "") or name not in ("geometry.bin", "meta.json"):
        return ""
    p = folder(project, mid, name)
    return p if os.path.isfile(p) else ""


def check_name(name: str) -> str:
    """The file's kind, or a plain reason it cannot be read."""
    ext = os.path.splitext(name or "")[1].lower()
    if ext in FORMATS:
        return FORMATS[ext]
    if ext == ".rvt":
        raise ValueError("A Revit file cannot be read here yet. In Revit: File > Export > IFC, "
                         "then add the .ifc file.")
    if ext in (".dwg", ".dxf"):
        raise ValueError("That is a CAD drawing. Add the architect's 3D model: IFC, Rhino (.3dm), "
                         "glTF (.glb) or OBJ.")
    if ext in (".skp", ".fbx", ".nwd", ".nwc", ".pln"):
        raise ValueError(f"{ext} files cannot be read. Export the model as IFC (best), glTF (.glb) "
                         "or OBJ, then add that file.")
    raise ValueError("Add an IFC, Rhino (.3dm), glTF (.glb) or OBJ file.")


def add(project: str, name: str, who: str = "") -> tuple[dict, str]:
    """A new model entry; returns it and the path the file should be written to."""
    fmt = check_name(name)
    with _lock:
        d = load(project)
        mid = f"A{d['next']}"
        d["next"] += 1
        ext = os.path.splitext(name)[1].lower()
        m = {"id": mid, "name": name, "fmt": fmt, "file": "source" + ext, "state": "waiting",
             "added": _now(), "by": who, "show": True, "opacity": 0.35,
             "hidden": None,                  # None: as the file had them (its layers switched off)
             "align": {}}
        d["models"].append(m)
        save(project, d)
    os.makedirs(folder(project, mid), exist_ok=True)
    return m, folder(project, mid, "source" + ext)


def remove(project: str, mid: str) -> bool:
    with _lock:
        d = load(project)
        m = next((x for x in d["models"] if x["id"] == mid), None)
        if m is None:
            return False
        d["models"].remove(m)
        save(project, d)
    shutil.rmtree(folder(project, mid), ignore_errors=True)
    return True


def settings(project: str, mid: str, body: dict) -> dict:
    """What the page changes: shown, opacity, hidden groups, units / up axis, a line-up."""
    with _lock:
        d = load(project)
        m = next((x for x in d["models"] if x["id"] == mid), None)
        if m is None:
            raise ValueError("That model is no longer in the project.")
        if "show" in body:
            m["show"] = bool(body["show"])
        if "opacity" in body:
            m["opacity"] = min(max(float(body["opacity"]), 0.05), 1.0)
        if "hidden" in body:
            m["hidden"] = None if body["hidden"] is None else [str(x) for x in body["hidden"]][:5000]
        if "unit_mm" in body:
            u = float(body["unit_mm"])
            if not 0.001 <= u <= 10000:
                raise ValueError("Unknown unit.")
            m["unit_mm"] = u
        if "up" in body:
            if body["up"] not in ("z", "y"):
                raise ValueError("Up is z or y.")
            m["up"] = body["up"]
        if "align" in body:
            set_key, al = str(body.get("set") or ""), body["align"]
            if al is None:
                m["align"].pop(set_key, None)
            else:
                m["align"][set_key] = {"rot": float(al.get("rot") or 0.0), "dx": float(al.get("dx") or 0.0),
                                       "dy": float(al.get("dy") or 0.0), "dz": float(al.get("dz") or 0.0),
                                       "how": str(al.get("how") or "")[:300], "date": _now(),
                                       "by": str(body.get("by") or ""),
                                       # pinned: locked where it is, not moved by accident (the page)
                                       "pinned": bool(al.get("pinned"))}
        save(project, d)
        return m


# ---------------------------------------------------------------- collecting triangles
class Mesh:
    """Triangles of the whole model, kept by (group, floor) so the page can switch each on
    and off; every triangle remembers its element, for a click on it."""

    def __init__(self):
        self.groups: list[str] = []
        self.storeys: list[dict] = []
        self.elements: list[dict] = []
        self.parts: dict[tuple[int, int], list] = {}
        self.notes: list[str] = []
        self.off: set[str] = set()          # groups switched off in the file: shown unticked

    def group(self, name: str) -> int:
        name = (name or "Other").strip() or "Other"
        if name not in self.groups:
            self.groups.append(name)
        return self.groups.index(name)

    def storey(self, name: str, elev=None) -> int:
        for i, s in enumerate(self.storeys):
            if s["name"] == name:
                return i
        self.storeys.append({"name": name, "elev": elev})
        return len(self.storeys) - 1

    def add(self, pos, tris, group: str, name: str = "", cls: str = "", storey: int = -1, guid: str = ""):
        pos = np.asarray(pos, dtype=np.float64).reshape(-1, 3)
        tris = np.asarray(tris, dtype=np.int64).reshape(-1, 3)
        if not len(pos) or not len(tris):
            return
        ok = np.all((tris >= 0) & (tris < len(pos)), axis=1)
        tris = tris[ok]
        if not len(tris) or not np.all(np.isfinite(pos)):
            return
        g = self.group(group)
        lo, hi = pos.min(axis=0), pos.max(axis=0)
        self.elements.append({"n": str(name or "")[:120], "c": str(cls or "")[:60], "g": g, "s": storey,
                              "id": str(guid or "")[:64], "b": [round(float(v), 3) for v in (*lo, *hi)]})
        self.parts.setdefault((g, storey), []).append((pos, tris, len(self.elements) - 1))

    def write(self, out: str, meta: dict) -> dict:
        """geometry.bin + meta.json in out; the meta written."""
        if not self.parts:
            raise ValueError("No 3D objects with surfaces were found in the file.")
        allpos = np.concatenate([p for parts in self.parts.values() for p, _, _ in parts])
        lo, hi = allpos.min(axis=0), allpos.max(axis=0)
        origin = (lo + hi) / 2.0
        chunks, pos_out, idx_out, nv, ni = [], [], [], 0, 0
        for (g, s), parts in sorted(self.parts.items()):
            c = {"g": g, "s": s, "v0": nv, "i0": ni, "els": []}
            base, t0 = 0, 0
            for pos, tris, ei in parts:
                pos_out.append((pos - origin).astype(np.float32))
                idx_out.append((tris + base).astype(np.uint32))
                c["els"].append([t0, len(tris), ei])
                base += len(pos)
                t0 += len(tris)
            c["vn"], c["in"] = base, t0 * 3
            nv += base
            ni += t0 * 3
            chunks.append(c)
        os.makedirs(out, exist_ok=True)
        tmp = os.path.join(out, "geometry.bin.tmp")
        with open(tmp, "wb") as f:
            for a in pos_out:
                f.write(a.tobytes())
            for a in idx_out:
                f.write(a.tobytes())
        os.replace(tmp, os.path.join(out, "geometry.bin"))
        meta = {"version": 1, **meta, "origin": [float(v) for v in origin],
                "bbox": [float(v) for v in (*lo, *hi)], "nverts": nv, "nidx": ni, "groups": self.groups,
                "groups_off": sorted(g for g in self.off if g in self.groups), "storeys": self.storeys, "chunks": chunks, "elements": self.elements, "notes": self.notes}
        with open(os.path.join(out, "meta.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, separators=(",", ":"))
        return meta


def guess_unit(extent: float) -> float:
    """mm per file unit, from the size of a building (only when the file does not say)."""
    if extent <= 0:
        return 1000.0
    if extent < 2000:            # under 2 km in metres
        return 1000.0
    return 1.0                   # millimetres


# ---------------------------------------------------------------- OBJ
def read_obj(path: str) -> tuple[Mesh, dict]:
    mesh = Mesh()
    verts: list[tuple[float, float, float]] = []
    cur = {"name": "", "mtl": ""}
    faces: dict[tuple[str, str], list] = {}
    order: list[tuple[str, str]] = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if not line or line[0] == "#":
                continue
            t = line.split()
            if not t:
                continue
            k = t[0]
            if k == "v" and len(t) >= 4:
                verts.append((float(t[1]), float(t[2]), float(t[3])))
            elif k in ("o", "g"):
                cur["name"] = " ".join(t[1:])
            elif k == "usemtl":
                cur["mtl"] = " ".join(t[1:])
            elif k == "f" and len(t) >= 4:
                ids = []
                for x in t[1:]:
                    i = int(x.split("/")[0])
                    ids.append(i - 1 if i > 0 else len(verts) + i)
                key = (cur["name"], cur["mtl"])
                if key not in faces:
                    faces[key] = []
                    order.append(key)
                for j in range(1, len(ids) - 1):
                    faces[key].append((ids[0], ids[j], ids[j + 1]))
    if not verts:
        raise ValueError("The OBJ file holds no points.")
    V = np.array(verts, dtype=np.float64)
    mtls = {m for _, m in order if m}
    by_mtl = 0 < len(mtls) <= 60
    for key in order:
        name, mtl = key
        T = np.array(faces[key], dtype=np.int64)
        used = np.unique(T)
        remap = np.full(len(V), -1, dtype=np.int64)
        remap[used] = np.arange(len(used))
        mesh.add(V[used], remap[T], mtl if by_mtl else "Model", name or mtl, "")
    ext = float(np.max(V.max(axis=0) - V.min(axis=0)))
    return mesh, {"unit_mm": guess_unit(ext), "unit_sure": False, "up": "y", "up_sure": False}


# ---------------------------------------------------------------- glTF / GLB
_CT = {5120: np.int8, 5121: np.uint8, 5122: np.int16, 5123: np.uint16, 5125: np.uint32, 5126: np.float32}
_NC = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT4": 16}


def _gltf_load(path: str) -> tuple[dict, list[bytes]]:
    import base64
    with open(path, "rb") as f:
        raw = f.read()
    if raw[:4] == b"glTF":
        n = struct.unpack_from("<I", raw, 8)[0]
        off, js, binc = 12, None, None
        while off < min(n, len(raw)):
            ln, kind = struct.unpack_from("<II", raw, off)
            body = raw[off + 8: off + 8 + ln]
            if kind == 0x4E4F534A:
                js = json.loads(body.decode("utf-8"))
            elif kind == 0x004E4942:
                binc = body
            off += 8 + ln
        if js is None:
            raise ValueError("The GLB file has no model description.")
        bufs = []
        for b in js.get("buffers", []):
            if "uri" not in b:
                bufs.append(binc or b"")
            else:
                bufs.append(_uri(b["uri"], path))
        return js, bufs
    js = json.loads(raw.decode("utf-8"))
    return js, [_uri(b.get("uri", ""), path) for b in js.get("buffers", [])]


def _uri(uri: str, path: str) -> bytes:
    import base64
    if uri.startswith("data:"):
        return base64.b64decode(uri.split(",", 1)[1])
    p = os.path.join(os.path.dirname(path), uri)
    if not os.path.isfile(p):
        raise ValueError(f"The glTF file needs {uri} beside it. Export as .glb (one file) instead.")
    with open(p, "rb") as f:
        return f.read()


def _accessor(js, bufs, i):
    a = js["accessors"][i]
    if "bufferView" not in a:
        return np.zeros((a["count"], _NC[a["type"]]))
    v = js["bufferViews"][a["bufferView"]]
    dt = np.dtype(_CT[a["componentType"]])
    nc = _NC[a["type"]]
    start = v.get("byteOffset", 0) + a.get("byteOffset", 0)
    stride = v.get("byteStride") or dt.itemsize * nc
    buf = bufs[v["buffer"]]
    if stride == dt.itemsize * nc:
        arr = np.frombuffer(buf, dtype=dt, count=a["count"] * nc, offset=start).reshape(-1, nc)
    else:
        rows = [np.frombuffer(buf, dtype=dt, count=nc, offset=start + k * stride) for k in range(a["count"])]
        arr = np.array(rows).reshape(-1, nc)
    return arr.astype(np.float64)


def _node_matrix(n) -> np.ndarray:
    if "matrix" in n:
        return np.array(n["matrix"], dtype=np.float64).reshape(4, 4).T
    t = n.get("translation", [0, 0, 0])
    x, y, z, w = n.get("rotation", [0, 0, 0, 1])
    s = n.get("scale", [1, 1, 1])
    R = np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                  [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                  [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])
    M = np.eye(4)
    M[:3, :3] = R * np.array(s)
    M[:3, 3] = t
    return M


def read_gltf(path: str) -> tuple[Mesh, dict]:
    js, bufs = _gltf_load(path)
    if "KHR_draco_mesh_compression" in (js.get("extensionsUsed") or []):
        raise ValueError("This glTF is Draco-compressed. Export it without compression.")
    mesh = Mesh()
    mats = js.get("materials", [])
    names = {m.get("name") for m in mats if m.get("name")}
    by_mtl = 0 < len(names) <= 60
    scene = js.get("scenes", [{}])[js.get("scene", 0)] if js.get("scenes") else {"nodes": list(range(len(js.get("nodes", []))))}
    nodes = js.get("nodes", [])

    def walk(i, parent, top):
        n = nodes[i]
        M = parent @ _node_matrix(n)
        top = top or n.get("name") or f"node {i}"
        if "mesh" in n:
            m = js["meshes"][n["mesh"]]
            for p in m.get("primitives", []):
                if p.get("mode", 4) != 4 or "POSITION" not in p.get("attributes", {}):
                    continue
                P = _accessor(js, bufs, p["attributes"]["POSITION"])[:, :3]
                P = (np.c_[P, np.ones(len(P))] @ M.T)[:, :3]
                I = (_accessor(js, bufs, p["indices"]).astype(np.int64).reshape(-1, 3) if "indices" in p
                     else np.arange(len(P) - len(P) % 3).reshape(-1, 3))
                mname = mats[p["material"]].get("name", "") if "material" in p and p["material"] < len(mats) else ""
                mesh.add(P, I, mname if by_mtl and mname else top, n.get("name") or m.get("name") or top, "")
        for c in n.get("children", []):
            walk(c, M, top)
    for i in scene.get("nodes", []):
        walk(i, np.eye(4), "")
    # glTF is in metres with Y up (the format says so)
    return mesh, {"unit_mm": 1000.0, "unit_sure": True, "up": "y", "up_sure": True}


# ---------------------------------------------------------------- Rhino .3dm
_RH_UNITS = {"Millimeters": 1.0, "Centimeters": 10.0, "Meters": 1000.0, "Inches": 25.4, "Feet": 304.8,
             "Decimeters": 100.0, "Kilometers": 1e6, "Microns": 0.001, "Yards": 914.4}


def _xf(t) -> np.ndarray:
    return np.array([[t.M00, t.M01, t.M02, t.M03], [t.M10, t.M11, t.M12, t.M13],
                     [t.M20, t.M21, t.M22, t.M23], [t.M30, t.M31, t.M32, t.M33]], dtype=np.float64)


def _rh_mesh(m):
    V = np.array([(p.X, p.Y, p.Z) for p in m.Vertices.ToPoint3dArray()], dtype=np.float64) \
        if hasattr(m.Vertices, "ToPoint3dArray") else \
        np.array([(m.Vertices[i].X, m.Vertices[i].Y, m.Vertices[i].Z) for i in range(len(m.Vertices))])
    T = []
    for i in range(m.Faces.Count):
        a, b, c, d = m.Faces[i]
        T.append((a, b, c))
        if d != c:
            T.append((a, c, d))
    return V, np.array(T, dtype=np.int64).reshape(-1, 3)


def _curve_points(c, n_arc=ARC_SEGMENTS) -> list:
    """A closed profile as points (the last not repeating the first)."""
    import rhino3dm as r
    pl = None
    try:
        ok = c.TryGetPolyline()
        pl = ok[1] if isinstance(ok, tuple) and ok[0] else (ok if ok and not isinstance(ok, (bool, tuple)) else None)
    except Exception:
        pl = None
    if pl is not None:
        try:
            pts = [(pl[i].X, pl[i].Y, pl[i].Z) for i in range(pl.Count)]
        except Exception:
            pts = []
        if len(pts) >= 3:
            return pts[:-1] if np.allclose(pts[0], pts[-1]) else pts
    dom = c.Domain
    n = n_arc
    if isinstance(c, r.PolyCurve):
        n = max(n_arc, 8 * c.SegmentCount)
    elif isinstance(c, r.NurbsCurve):
        n = max(n_arc, 4 * len(c.Points))
    elif isinstance(c, r.LineCurve):
        n = 2
    pts = []
    for k in range(n):
        p = c.PointAt(dom.T0 + (dom.T1 - dom.T0) * k / n)
        pts.append((p.X, p.Y, p.Z))
    return pts


def _cap(rings3d, normal):
    """Triangles closing a planar region (outer ring, holes) - points indexed into the rings
    as concatenated."""
    import shapely
    n = np.asarray(normal, dtype=np.float64)
    n = n / (np.linalg.norm(n) or 1.0)
    u = np.cross(n, [0, 0, 1.0] if abs(n[2]) < 0.9 else [1.0, 0, 0])
    u /= np.linalg.norm(u)
    v = np.cross(n, u)
    o = np.asarray(rings3d[0][0])
    flat = [[((np.asarray(p) - o) @ u, (np.asarray(p) - o) @ v) for p in ring] for ring in rings3d]
    try:
        poly = shapely.Polygon(flat[0], flat[1:])
        if not poly.is_valid:
            poly = poly.buffer(0)
        tris = shapely.constrained_delaunay_triangles(poly)
    except Exception:
        return []
    index = {}
    k = 0
    for ring in flat:
        for p in ring:
            index.setdefault((round(p[0], 3), round(p[1], 3)), k)
            k += 1
    out = []
    for t in getattr(tris, "geoms", []):
        cs = list(t.exterior.coords)[:3]
        ids = [index.get((round(x, 3), round(y, 3))) for x, y in cs]
        if None not in ids:
            out.append(ids)
    return out


def _edge_points(e) -> list:
    """An edge as points, start to end (straight: its two ends)."""
    try:
        lin = e.IsLinear(0.001)
    except TypeError:
        lin = e.IsLinear()
    except Exception:
        lin = False
    a, b = e.PointAtStart, e.PointAtEnd
    if lin:
        return [(a.X, a.Y, a.Z), (b.X, b.Y, b.Z)]
    try:
        ok = e.TryGetPolyline()
        pl = ok[1] if isinstance(ok, tuple) and ok[0] else None
        if pl is not None and pl.Count >= 2:
            return [(pl[i].X, pl[i].Y, pl[i].Z) for i in range(pl.Count)]
    except Exception:
        pass
    dom, n = e.Domain, ARC_SEGMENTS
    return [(p.X, p.Y, p.Z) for p in (e.PointAt(dom.T0 + (dom.T1 - dom.T0) * k / n) for k in range(n + 1))]


def _loop_points(brep, loop) -> list:
    """A face's loop as one ring of points, its edges chained end to end."""
    pts: list = []
    for k in range(loop.TrimCount):
        t = loop.Trims[k]
        if t.EdgeIndex < 0:                       # a trim at a singular point: no edge
            continue
        ps = _edge_points(brep.Edges[t.EdgeIndex])
        if t.IsReversed:
            ps = ps[::-1]
        if pts:
            if math.dist(pts[-1], ps[-1]) < math.dist(pts[-1], ps[0]):
                ps = ps[::-1]                      # chained the way the ring runs
            if math.dist(pts[-1], ps[0]) < 1e-6:
                ps = ps[1:]
        pts.extend(ps)
    if len(pts) > 2 and math.dist(pts[0], pts[-1]) < 1e-6:
        pts.pop()
    return pts


def _brep_face(brep, fc, grid: int = 12):
    """A brep face saved with no display mesh, made here: a flat face filled inside its
    edges (holes kept), a curved one as a grid over its surface when its edges span it.
    (V, T) or None."""
    try:
        loops = [fc.Loops[i] for i in range(len(fc.Loops))]
    except Exception:
        return None
    if not loops:
        return None
    outer = fc.OuterLoop
    rings = [_loop_points(brep, outer)] + [_loop_points(brep, lp) for lp in loops
                                           if str(lp.LoopType).split(".")[-1] == "Inner"]
    rings = [rg for rg in rings if len(rg) >= 3]
    if not rings:
        return None
    du, dv = fc.Domain(0), fc.Domain(1)
    try:
        planar = fc.IsPlanar(0.01)
    except TypeError:
        planar = fc.IsPlanar()
    if planar:
        n = fc.NormalAt((du.T0 + du.T1) / 2, (dv.T0 + dv.T1) / 2)
        tris = _cap(rings, (n.X, n.Y, n.Z))
        if not tris:
            return None
        return np.array([p for rg in rings for p in rg], dtype=np.float64), np.array(tris, dtype=np.int64)
    G = [[fc.PointAt(du.T0 + (du.T1 - du.T0) * i / grid, dv.T0 + (dv.T1 - dv.T0) * j / grid)
          for j in range(grid + 1)] for i in range(grid + 1)]
    V = np.array([(p.X, p.Y, p.Z) for row in G for p in row], dtype=np.float64)
    ring = np.array(rings[0])
    span_g = np.linalg.norm(V.max(axis=0) - V.min(axis=0))
    span_r = np.linalg.norm(ring.max(axis=0) - ring.min(axis=0))
    if span_g <= 0 or span_r < 0.8 * span_g:      # trimmed well inside its surface: not guessed
        return None
    T = []
    for i in range(grid):
        for j in range(grid):
            a, b, c, d = i * (grid + 1) + j, (i + 1) * (grid + 1) + j, (i + 1) * (grid + 1) + j + 1, i * (grid + 1) + j + 1
            T += [(a, b, c), (a, c, d)]
    return V, np.array(T, dtype=np.int64)


def _extrusion(e):
    """An extrusion with no display mesh saved: its profiles carried along its path, capped."""
    rings_b, rings_t = [], []
    for i in range(e.ProfileCount):
        cb, ct = e.Profile3d(i, 0.0), e.Profile3d(i, 1.0)
        if cb is None or ct is None:
            continue
        pb, pt = _curve_points(cb), _curve_points(ct)
        if len(pb) < 2 or len(pb) != len(pt):
            continue
        rings_b.append(pb)
        rings_t.append(pt)
    if not rings_b:
        return None
    V, T = [], []
    for pb, pt in zip(rings_b, rings_t):
        k0, n = len(V), len(pb)
        V.extend(pb)
        V.extend(pt)
        closed = n >= 3
        for j in range(n if closed else n - 1):
            a, b = k0 + j, k0 + (j + 1) % n
            T.append((a, b, b + n))
            T.append((a, b + n, a + n))
    a, b = e.PathStart, e.PathEnd
    normal = (b.X - a.X, b.Y - a.Y, b.Z - a.Z)
    if all(len(r_) >= 3 for r_ in rings_b):
        nb = sum(len(r_) for r_ in rings_b)
        base = len(V)
        V.extend(p for r_ in rings_b for p in r_)
        V.extend(p for r_ in rings_t for p in r_)
        cap = _cap(rings_b, normal)
        for a, b, c in cap:
            T.append((base + a, base + c, base + b))
            T.append((base + nb + a, base + nb + b, base + nb + c))
    return np.array(V, dtype=np.float64), np.array(T, dtype=np.int64).reshape(-1, 3)


def read_3dm(path: str) -> tuple[Mesh, dict]:
    import rhino3dm as r
    f = r.File3dm.Read(path)
    if f is None:
        raise ValueError("The Rhino file could not be opened (a newer Rhino version, or damaged).")
    mesh = Mesh()
    layers = [f.Layers[i] for i in range(len(f.Layers))]
    defs = {}
    for i in range(len(f.InstanceDefinitions)):
        d = f.InstanceDefinitions[i]
        defs[str(d.Id)] = d
    by_id = {str(o.Attributes.Id): o for o in f.Objects}
    skipped = {"off": 0, "nomesh": 0, "made": 0}

    def layer_of(o):
        i = o.Attributes.LayerIndex
        return layers[i] if 0 <= i < len(layers) else None

    def layer_on(ly):
        k = 0
        while ly is not None and k < 20:            # a layer inside one switched off is off too
            try:
                if not ly.Visible:
                    return False
            except Exception:
                return True
            pid = str(getattr(ly, "ParentLayerId", "") or "")
            ly = next((x for x in layers if str(x.Id) == pid), None) if pid and set(pid) != {"0", "-"} else None
            k += 1
        return True

    def emit(o, X, depth, outer_layer=None, hidden=False):
        g = o.Geometry
        ly = layer_of(o)
        lname = ly.FullPath if ly is not None else "Default"
        if outer_layer and lname in ("Default", "Layer0", "0"):
            lname = outer_layer                       # a block's own objects on the block's layer
        if hidden or not o.Attributes.Visible or not layer_on(ly):
            # switched off in the file: read all the same, its layer shown unticked
            lname = lname + " (off in the file)"
            mesh.off.add(lname)
            skipped["off"] += 1
        name = o.Attributes.Name or ""
        parts = []
        if isinstance(g, r.Mesh):
            parts.append(_rh_mesh(g))
        elif isinstance(g, r.Brep):
            for i in range(len(g.Faces)):
                fc = g.Faces[i]
                m = fc.GetMesh(r.MeshType.Any)
                if m is not None:
                    parts.append(_rh_mesh(m))
                    continue
                # saved with no display mesh ("save small", a file made by a script): made here
                try:
                    got = _brep_face(g, fc)
                except Exception:
                    got = None
                if got is not None:
                    parts.append(got)
                    skipped["made"] += 1
                else:
                    skipped["nomesh"] += 1
        elif isinstance(g, r.Extrusion):
            m = g.GetMesh(r.MeshType.Any)
            if m is not None:
                parts.append(_rh_mesh(m))
            else:
                got = _extrusion(g)
                if got is not None:
                    parts.append(got)
                else:
                    skipped["nomesh"] += 1
        elif isinstance(g, r.InstanceReference):
            d = defs.get(str(g.ParentIdefId))
            if d is None or depth > 8:
                return
            Xi = X @ _xf(g.Xform)
            off = lname.endswith(" (off in the file)")
            base = lname[:-len(" (off in the file)")] if off else lname
            for oid in d.GetObjectIds():
                c = by_id.get(str(oid))
                if c is not None:
                    emit(c, Xi, depth + 1, base, off)
            return
        if not parts:
            return
        Vs, Ts, k = [], [], 0
        for V, T in parts:
            Vs.append(V)
            Ts.append(T + k)
            k += len(V)
        V = np.concatenate(Vs)
        V = (np.c_[V, np.ones(len(V))] @ X.T)[:, :3]
        mesh.add(V, np.concatenate(Ts), lname, name or type(g).__name__, type(g).__name__, -1,
                 str(o.Attributes.Id))

    for o in f.Objects:
        if o.Attributes.IsInstanceDefinitionObject:
            continue
        emit(o, np.eye(4), 0)
    n_off = len([g for g in mesh.groups if g in mesh.off])
    if n_off:
        mesh.notes.append(f"{n_off} layer(s) switched off in the file are read but start unticked.")
    if skipped["made"]:
        mesh.notes.append(f"{skipped['made']} surfaces had no display mesh saved in the file: made here from "
                          "their edges.")
    if skipped["nomesh"]:
        mesh.notes.append(f"{skipped['nomesh']} surfaces had no display mesh saved and could not be made here - not "
                          "shown (in Rhino: shade the view once, then save).")
    us = str(f.Settings.ModelUnitSystem).split(".")[-1]
    unit = _RH_UNITS.get(us)
    if unit is None:
        b = mesh.elements and np.array([e["b"] for e in mesh.elements])
        ext = float(np.max(b[:, 3:].max(axis=0) - b[:, :3].min(axis=0))) if len(b) else 0.0
        return mesh, {"unit_mm": guess_unit(ext), "unit_sure": False, "up": "z", "up_sure": True}
    return mesh, {"unit_mm": unit, "unit_sure": True, "up": "z", "up_sure": True}


# ---------------------------------------------------------------- IFC
_IFC_GROUP = [
    (("IfcWall", "IfcWallStandardCase", "IfcWallElementedCase"), "Walls"),
    (("IfcCurtainWall", "IfcPlate", "IfcMember"), "Curtain walls and frames"),
    (("IfcDoor",), "Doors"), (("IfcWindow",), "Windows"),
    (("IfcColumn", "IfcColumnStandardCase"), "Columns"), (("IfcBeam", "IfcBeamStandardCase"), "Beams"),
    (("IfcRoof",), "Roofs"), (("IfcSlab", "IfcSlabStandardCase", "IfcSlabElementedCase"), "Floors and slabs"),
    (("IfcStair", "IfcStairFlight", "IfcRamp", "IfcRampFlight"), "Stairs and ramps"),
    (("IfcRailing",), "Railings"), (("IfcCovering",), "Finishes and ceilings"),
    (("IfcFooting", "IfcPile"), "Foundations"),
    (("IfcFurnishingElement", "IfcFurniture", "IfcSystemFurnitureElement"), "Furniture"),
    (("IfcSite", "IfcGeographicElement"), "Site"),
    (("IfcFlowTerminal", "IfcFlowSegment", "IfcFlowFitting", "IfcDistributionElement", "IfcFlowController",
      "IfcEnergyConversionDevice", "IfcSanitaryTerminal", "IfcLightFixture", "IfcPipeSegment",
      "IfcDuctSegment"), "Services"),
]


def _ifc_group(el) -> str:
    cls = el.is_a()
    if cls in ("IfcSlab", "IfcSlabStandardCase") and str(getattr(el, "PredefinedType", "") or "") == "ROOF":
        return "Roofs"
    for names, g in _IFC_GROUP:
        if cls in names:
            return g
    for names, g in _IFC_GROUP:
        if any(el.is_a(n) for n in names if n in ("IfcDistributionElement",)):
            return g
    return "Other"


def ensure_ifcopenshell(log=print) -> None:
    try:
        import ifcopenshell  # noqa: F401
        return
    except ImportError:
        pass
    import subprocess
    log("Installing the IFC reader (ifcopenshell) - one time, needs the internet ...")
    p = subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "ifcopenshell"],
                       capture_output=True, text=True)
    if p.returncode:
        raise ValueError("The IFC reader could not be installed. With the internet on, run: "
                         "python -m pip install ifcopenshell")
    import importlib
    importlib.invalidate_caches()


BLOCKED = ("Windows blocked the IFC reader's 3D engine on this PC (Smart App Control or a company "
           "security policy). Add the model as Rhino (.3dm), glTF (.glb) or OBJ instead - Revit can export "
           "glTF/OBJ with a free add-in, or open the IFC in Rhino and save a .3dm.")


def read_ifc(path: str, log=print) -> tuple[Mesh, dict]:
    ensure_ifcopenshell(log)
    import ifcopenshell
    import ifcopenshell.geom
    f = ifcopenshell.open(path)
    mesh = Mesh()
    try:
        import ifcopenshell.util.unit as uu
        scale = float(uu.calculate_unit_scale(f)) * 1000.0      # file length unit -> mm
    except Exception:
        scale = 1000.0
    # floors: name and height, in the order of their height
    storeys = []
    for s in f.by_type("IfcBuildingStorey"):
        z = None
        try:
            import ifcopenshell.util.placement as up
            z = float(up.get_local_placement(s.ObjectPlacement)[2][3]) * scale
        except Exception:
            z = float(s.Elevation) * scale if getattr(s, "Elevation", None) is not None else None
        storeys.append((z if z is not None else 0.0, s))
    storeys.sort(key=lambda t: t[0])
    sidx = {}
    for z, s in storeys:
        sidx[s.id()] = mesh.storey(s.Name or s.LongName or f"Floor {len(sidx) + 1}", round(z, 1))

    def storey_of(el):
        try:
            import ifcopenshell.util.element as ue
            c = ue.get_container(el)
            k = 0
            while c is not None and not c.is_a("IfcBuildingStorey") and k < 6:
                c = ue.get_aggregate(c) or ue.get_container(c)
                k += 1
            if c is not None and c.is_a("IfcBuildingStorey"):
                return sidx.get(c.id(), -1)
            agg = ue.get_aggregate(el)               # a stair flight inside its stair
            if agg is not None:
                return storey_of(agg)
        except Exception:
            pass
        return -1

    st = ifcopenshell.geom.settings()
    for key in ("use-world-coords", "USE_WORLD_COORDS"):
        try:
            st.set(key if "-" in key else getattr(st, key), True)
            break
        except Exception:
            continue
    try:
        st.set("apply-default-materials", False)
    except Exception:
        pass
    exclude = [x for x in ("IfcOpeningElement", "IfcSpace", "IfcVirtualElement", "IfcAnnotation")
               if _has(f, x)]
    try:
        try:
            it = ifcopenshell.geom.iterator(st, f, max(1, (os.cpu_count() or 2) - 1), exclude=exclude)
        except TypeError:
            it = ifcopenshell.geom.iterator(st, f, max(1, (os.cpu_count() or 2) - 1))
    except RuntimeError as exc:
        if "Application Control" in str(exc) or "load() failed" in str(exc):
            raise ValueError(BLOCKED) from exc
        raise
    n = 0
    if it.initialize():
        while True:
            sh = it.get()
            try:
                el = f.by_id(sh.id)
                if el.is_a() not in ("IfcOpeningElement", "IfcSpace", "IfcVirtualElement"):
                    g = sh.geometry
                    V = np.array(g.verts, dtype=np.float64).reshape(-1, 3) * 1000.0   # metres -> mm
                    T = np.array(g.faces, dtype=np.int64).reshape(-1, 3)
                    mesh.add(V, T, _ifc_group(el), el.Name or el.is_a()[3:], el.is_a(), storey_of(el),
                             getattr(el, "GlobalId", ""))
                    n += 1
                    if n % 2000 == 0:
                        log(f"{n} objects read ...")
            except Exception:
                pass
            if not it.next():
                break
    return mesh, {"unit_mm": 1.0, "unit_sure": True, "up": "z", "up_sure": True, "schema": f.schema}


def _has(f, cls) -> bool:
    try:
        f.by_type(cls)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- reading a model
def read(path: str, log=print) -> tuple[Mesh, dict]:
    fmt = check_name(path)
    if fmt == "IFC":
        return read_ifc(path, log)
    if fmt == "Rhino":
        return read_3dm(path)
    if fmt == "glTF":
        return read_gltf(path)
    return read_obj(path)


def convert(path: str, out: str, log=print) -> dict:
    """Read a model file into out (geometry.bin, meta.json); the meta."""
    mesh, info = read(path, log)
    return mesh.write(out, {**info, "source": os.path.basename(path)})


def run(project: str, mid: str, log=print) -> None:
    """Read a model added to the project (in a thread of the app): its state goes
    waiting -> reading -> ready / failed, with a message the page shows."""
    with _work:
        d = load(project)
        m = next((x for x in d["models"] if x["id"] == mid), None)
        if m is None:
            return
        _update(project, mid, state="reading", message="Reading the model ...")
        try:
            meta = convert(folder(project, mid, m["file"]), folder(project, mid),
                           log=lambda s: _update(project, mid, message=s))
            n_tri = meta["nidx"] // 3
            msg = (f"{len(meta['elements'])} objects, {n_tri:,} triangles"
                   + (f", {len(meta['storeys'])} floors" if meta["storeys"] else "") + ".")
            _update(project, mid, state="ready", message=msg, unit_mm=meta["unit_mm"], unit_sure=meta["unit_sure"],
                    up=meta["up"], up_sure=meta["up_sure"], notes=meta.get("notes", []), read=_now())
        except Exception as exc:
            _update(project, mid, state="failed", message=str(exc) or exc.__class__.__name__,
                    detail=traceback.format_exc()[-2000:])


def start(project: str, mid: str) -> None:
    threading.Thread(target=run, args=(project, mid), daemon=True).start()


def main(argv=None) -> int:
    a = argv or sys.argv[1:]
    if len(a) != 2:
        print(__doc__)
        return 2
    meta = convert(a[0], a[1])
    print(f"{len(meta['elements'])} objects, {meta['nidx'] // 3} triangles, groups {meta['groups']}, "
          f"floors {[s['name'] for s in meta['storeys']]}, unit {meta['unit_mm']} mm ({meta['unit_sure']}), "
          f"up {meta['up']}")
    for n in meta["notes"]:
        print("  " + n)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
