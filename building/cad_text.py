r"""The drawing's own text - marks, sizes, grid names in their bubbles - for the Review page
to lay over the CAD underneath, where the drawing puts it (position, height, angle). For
display only: the model, the decisions and review.html are not touched. The app (webapp.py)
serves it per plan; the page draws it over the plan and into screenshots for the consultant.

    python -m building.cad_text RUN_FOLDER SHEET_ID
"""
from __future__ import annotations

import json
import math
import os
import sys
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

MAX_TEXTS = 20000            # a plan with more keeps the shortest (marks, grid names)
MAX_LEN = 80                 # longer: a note paragraph, not a label
_lock = threading.Lock()
_BUILDINGS: dict = {}        # building.json path -> (modified time, Building)
_TEXTS: dict = {}            # (dxf path, modified time, box) -> texts


def _building(run_dir: str):
    from building import Building
    p = os.path.join(run_dir, "building.json")
    mt = os.path.getmtime(p)
    with _lock:
        got = _BUILDINGS.get(p)
        if got and got[0] == mt:
            return got[1]
    b = Building.load(p)
    with _lock:
        _BUILDINGS.clear()                    # one analysis at a time is enough
        _BUILDINGS[p] = (mt, b)
    return b


def _dxf_of(run_dir: str, s) -> str:
    """The plan's DXF, found the way the review page finds its CAD underneath."""
    path = s.dxf_file or ""
    if path and not os.path.exists(path) and run_dir:
        name = os.path.basename(path)
        path = next((p for p in (os.path.join(run_dir, "_work", name),
                                 os.path.join(os.path.dirname(os.path.dirname(run_dir)), "dxf_cache", name))
                     if os.path.exists(p)), "")
    return path if path and os.path.exists(path) else ""


def _box(b, sid: str):
    """The plan's extent, as the review page cuts its CAD underneath."""
    from building.review import DRAWN, _geom
    xs, ys = [], []
    for e in b.elements:
        if e.sheet != sid or e.type not in DRAWN:
            continue
        g = _geom(e)
        if g:
            xs += [p[0] for p in g["p"]]
            ys += [p[1] for p in g["p"]]
    if not xs:
        return None
    m = 0.15 * max(max(xs) - min(xs), max(ys) - min(ys)) + 2000.0
    return (min(xs) - m, min(ys) - m, max(xs) + m, max(ys) + m)


def _hidden_layers(doc) -> set:
    out = set()
    for layer in doc.layers:
        try:
            if layer.is_off() or layer.is_frozen():
                out.add(layer.dxf.name)
        except Exception:
            pass
    return out


def _angle(e, rot_deg: float) -> float:
    """The text's direction on the drawing (a mirrored block's text reads its own way)."""
    try:
        from ezdxf.math import Vec3
        ocs = e.ocs()
        a = ocs.to_wcs(Vec3(0, 0, 0))
        d = ocs.to_wcs(Vec3.from_deg_angle(rot_deg))
        return math.degrees(math.atan2(d.y - a.y, d.x - a.x))
    except Exception:
        return rot_deg


def _original(e) -> str:
    """The wording the drawing had before the app rewrote it in mm (core.units), cleaned of
    TEXT codes (%%U underline ...), or ""."""
    sys.path.insert(0, os.path.join(ROOT, "cad2revit"))
    from core.units import original_text                      # noqa: E402
    from building.sheets.source import clean
    s = original_text(e)
    return clean(s) if s else ""


def read(path: str, box=None) -> list[list]:
    """[x, y, height, angle, h-align 0/1/2, v-align 0..3, text] of every TEXT, MTEXT and block
    attribute (inside blocks too), on layers that are shown, within box (x0, y0, x1, y1)."""
    import ezdxf
    mt = os.path.getmtime(path)
    key = (path, mt, tuple(round(v) for v in box) if box else None)
    with _lock:
        if key in _TEXTS:
            return _TEXTS[key]
    doc = ezdxf.readfile(path)
    hidden = _hidden_layers(doc)
    out = []

    def keep(x, y):
        return box is None or (box[0] <= x <= box[2] and box[1] <= y <= box[3])

    def text(e):
        t = e.dxftype()
        if e.dxf.get("layer", "0") in hidden:
            return
        try:
            if t in ("TEXT", "ATTRIB", "ATTDEF"):
                # an attribute definition placed on the plan shows its tag (NIFT's beam marks
                # "4B3", "PB2" - read as text by the readers since 2026-10-21)
                s = str(e.dxf.get("tag", "")).strip() if t == "ATTDEF" else \
                    (e.plain_text().strip() if hasattr(e, "plain_text") else str(e.dxf.text).strip())
                h = float(e.dxf.get("height", 0) or 0)
                ha, va = int(e.dxf.get("halign", 0) or 0), int(e.dxf.get("valign", 0) or 0)
                p = e.dxf.align_point if (ha or va) and e.dxf.hasattr("align_point") else e.dxf.insert
                p = e.ocs().to_wcs(p)
                if ha in (3, 5):          # aligned / fit: between its two points
                    ha = 0
                if ha == 4:               # "middle": centred both ways
                    ha, va = 1, 2
                rot = _angle(e, float(e.dxf.get("rotation", 0) or 0))
            elif t == "MTEXT":
                s = e.plain_text().strip()
                h = float(e.dxf.get("char_height", 0) or 0)
                p = e.dxf.insert
                ap = int(e.dxf.get("attachment_point", 1) or 1)
                ha = (ap - 1) % 3
                va = {0: 3, 1: 2, 2: 0}[(ap - 1) // 3]
                rot = float(e.get_rotation()) if hasattr(e, "get_rotation") else float(e.dxf.get("rotation", 0) or 0)
            else:
                return
        except Exception:
            return
        # a size the app rewrote in mm (9"X18" -> 229X457): shown as the consultant wrote
        # it; the mm the model uses rides along and is placed by for_plan, clear of beams
        orig = _original(e)
        mm = ""
        if orig and orig != s:
            s, mm = orig, s
        if not s or h <= 0 or len(s) > MAX_LEN or not keep(p[0], p[1]):
            return
        out.append([round(p[0]), round(p[1]), round(h, 1), round(rot, 1), ha, va, s] + ([mm] if mm else []))

    def walk(e, depth=0):
        t = e.dxftype()
        if t == "INSERT":
            if e.dxf.get("layer", "0") in hidden:
                return
            for a in getattr(e, "attribs", []) or []:
                text(a)
            if depth < 2:
                try:
                    for v in e.virtual_entities():
                        walk(v, depth + 1)
                except Exception:
                    pass
        elif t in ("TEXT", "MTEXT", "ATTRIB"):
            text(e)
        elif t == "ATTDEF" and depth == 0:
            text(e)                     # on the plan itself, not a block's template

    for e in doc.modelspace():
        walk(e)
    if len(out) > MAX_TEXTS:
        out.sort(key=lambda r: len(r[6]))
        out = out[:MAX_TEXTS]
    with _lock:
        if len(_TEXTS) > 20:
            _TEXTS.clear()
        _TEXTS[key] = out
    return out


def for_plan(run_dir: str, sid: str) -> dict:
    """The text of one plan of an analysis: {"t": [...]} (empty when it cannot be read)."""
    if not run_dir or not os.path.exists(os.path.join(run_dir, "building.json")):
        return {"t": []}
    b = _building(run_dir)
    s = b.sheets.get(sid)
    if s is None:
        return {"t": []}
    path = _dxf_of(run_dir, s)
    box = _box(b, sid)
    if not path or box is None:
        return {"t": []}
    return {"t": _with_mm_lines(read(path, box), _beam_bands(b, sid))}


DISPLAY = 1.35          # the page draws a text this many times its drawing height
MM_SCALE = 0.8          # the mm line, smaller than the size as written
GAP = 0.35              # between the two lines, in text heights


def _beam_bands(b, sid: str):
    """The plan's beams as bands of their own width (shapely), to keep the mm lines off them."""
    from shapely.geometry import LineString
    from shapely.ops import unary_union
    from building.merge_beams import gone
    out = []
    for e in b.elements:
        if e.sheet != sid or e.type not in ("beam", "beam_span") or gone(e):
            continue
        g = e.geometry
        if not g.get("start") or not g.get("end"):
            continue
        try:
            w = float(e.get("width") or 230.0)
            out.append(LineString([g["start"][:2], g["end"][:2]]).buffer(w / 2.0, cap_style=2))
        except Exception:
            continue
    return unary_union(out) if out else None


def _rect(x, y, h, rot, ha, va, n):
    """The box a text of n characters covers on the page, as drawn there (shapely)."""
    from shapely.geometry import Polygon
    fs = DISPLAY * h
    w = 0.6 * fs * n
    u0 = {0: 0.0, 1: -w / 2, 2: -w}.get(ha, 0.0)
    v0 = {0: -0.2 * fs, 1: 0.0, 2: -fs / 2, 3: -fs}.get(va, 0.0)
    r = math.radians(rot)
    cu, su = math.cos(r), math.sin(r)

    def at(u, v):
        return (x + u * cu - v * su, y + u * su + v * cu)
    return Polygon([at(u0, v0), at(u0 + w, v0), at(u0 + w, v0 + fs), at(u0, v0 + fs)])


def _with_mm_lines(rows, beams):
    """Rows for the page: a size the app rewrote (9"X18") is shown as written, and its mm
    value (229X457) on a smaller line just below or above it - whichever covers the least
    beam and text (below when both are clear)."""
    from shapely.ops import unary_union
    plain = [r[:7] for r in rows]
    taken = [_rect(r[0], r[1], r[2], r[3], r[4], r[5], len(r[6])) for r in plain]
    out = list(plain)
    for r in rows:
        if len(r) < 8:
            continue
        x, y, h, rot, ha, va, s, mm = r
        label = f"({mm})"
        box = _rect(x, y, h, rot, ha, va, len(s))
        fs = DISPLAY * h
        # the original's middle and its bottom / top, along its own up direction
        rad = math.radians(rot)
        cu, su = math.cos(rad), math.sin(rad)
        c = box.centroid
        hm = MM_SCALE * h
        best = None
        others = [t for t in taken if not t.equals(box)]
        blocked = unary_union(others) if others else None
        wm = 0.6 * DISPLAY * hm * len(label)
        k = 0
        for side, far in ((-1, 0), (1, 0), (-1, 1), (1, 1)):
            # half the original's height, the gap, half the mm line's own height; the
            # second pair one line further out
            off = (fs / 2 + GAP * fs + DISPLAY * hm / 2 + 1.2 * DISPLAY * hm * far) * side
            for slide in (0.0, 0.6, -0.6, 1.1, -1.1):         # along the text, past a crossing beam
                px = c.x - su * off + cu * slide * wm
                py = c.y + cu * off + su * slide * wm
                cand = _rect(px, py, hm, rot, 1, 2, len(label))
                hit = 0.0
                if beams is not None:
                    hit += cand.intersection(beams).area
                if blocked is not None:
                    hit += cand.intersection(blocked).area
                score = hit + k * 1e-6                        # ties: below, then above, nearest first
                k += 1
                if best is None or score < best[0]:
                    best = (score, px, py, cand)
        _s, px, py, cand = best
        taken.append(cand)
        out.append([round(px), round(py), round(hm, 1), rot, 1, 2, label])
    return out


def main(argv=None) -> int:
    a = (argv or sys.argv[1:])
    got = for_plan(a[0], a[1])
    print(json.dumps({"n": len(got["t"]), "first": got["t"][:20]}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
