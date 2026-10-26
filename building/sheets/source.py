"""Opening a drawing and reading its text, the same way for every consultant.

A drawing comes in as DWG or DXF. DWGs are converted with the converter the
extractor already uses (ODA or ZWCAD); the original is never modified.

Text is read from TEXT, MTEXT and block attributes, in model space and on every
paper-space layout, with what a reader needs to judge it: the clean string, the
raw one, its height, where it is, whether it is underlined (drawing titles
usually are) and which layer and handle it came from.
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
from dataclasses import dataclass

import ezdxf
from ezdxf import bbox

HERE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@dataclass
class TextItem:
    text: str            # cleaned: control codes removed, whitespace collapsed
    raw: str
    height: float        # in the units of the space it is in
    x: float
    y: float
    layer: str
    handle: str
    space: str           # "model" or the layout name
    underlined: bool
    cx: float | None = None   # estimated centre x: tables need where a cell really is
    cy: float | None = None
    rotation: float | None = None   # degrees; a beam's caption runs along the beam
    # a text inside a block: [x, y, layer] of the closed outline drawn beside it in
    # that block, the thing it names (New block: 'SC1' beside its steel post)
    block_outline: list | None = None
    # the wording before the app rewrote it in mm (9"X18" for 229X457), shown to people
    original: str = ""


def _rotation(e) -> float | None:
    """The text's direction in degrees (an MTEXT may give it as a vector)."""
    import math
    try:
        if e.dxftype() == "MTEXT":
            d = e.dxf.get("text_direction", None)
            if d is not None and (abs(d[0]) > 1e-9 or abs(d[1]) > 1e-9):
                return math.degrees(math.atan2(d[1], d[0]))
        return float(e.dxf.get("rotation", 0.0) or 0.0)
    except Exception:
        return None


def _centre(e, text: str, h: float, x: float, y: float) -> tuple[float, float]:
    """Where the text actually sits, whatever its alignment.

    A TEXT's insertion point is its left end unless it is centred or right
    aligned, when the alignment point is what counts; an MTEXT is anchored by its
    attachment point. Width is estimated from the character count.
    """
    import math
    w = 0.8 * h * len(text)
    dx, dy = w / 2, h / 2            # offset to the centre, along and across the text
    try:
        if e.dxftype() == "TEXT":
            ha = e.dxf.get("halign", 0)
            if ha or e.dxf.get("valign", 0):
                p = e.dxf.get("align_point", None)
                if p is not None:
                    x, y = p.x, p.y
            if ha in (1, 4):            # centre, middle
                dx = 0.0
            elif ha == 2:               # right
                dx = -w / 2
        elif e.dxftype() == "MTEXT":
            ap = e.dxf.get("attachment_point", 1)
            col = (ap - 1) % 3          # 0 left, 1 centre, 2 right
            row = (ap - 1) // 3         # 0 top, 1 middle, 2 bottom
            dx = w / 2 if col == 0 else (0.0 if col == 1 else -w / 2)
            dy = -h / 2 if row == 0 else (0.0 if row == 1 else h / 2)
    except Exception:
        pass
    # a rotated text runs along its rotation: turn the offset with it
    r = math.radians(_rotation(e) or 0.0)
    return x + dx * math.cos(r) - dy * math.sin(r), y + dx * math.sin(r) + dy * math.cos(r)


_CODES = re.compile(r"%%[UuOoKk]")
_SYMBOL = {"%%C": "Ø", "%%c": "Ø", "%%D": "°", "%%d": "°", "%%P": "±", "%%p": "±"}


def clean(s: str) -> str:
    for k, v in _SYMBOL.items():
        s = s.replace(k, v)
    s = _CODES.sub("", s)
    return re.sub(r"\s+", " ", s).strip()


def dxf_for(path: str, work_dir: str | None = None, log=None) -> str:
    """A DXF path for a DWG or DXF. DWGs are converted into work_dir, once."""
    if path.lower().endswith(".dxf"):
        return path
    sys.path.insert(0, os.path.join(HERE, "cad2revit"))
    from core import dwgconv                                   # noqa: E402
    work_dir = work_dir or tempfile.mkdtemp(prefix="c2r_sheet_")
    os.makedirs(work_dir, exist_ok=True)
    out = os.path.join(work_dir, os.path.splitext(os.path.basename(path))[0] + ".dxf")
    if not os.path.exists(out) or os.path.getmtime(out) < os.path.getmtime(path):
        dwgconv.to_dxf(path, out, log=log or (lambda *a, **k: None))
    return out


def in_mm(path: str, work_dir: str | None = None, forced=None, log=None) -> tuple[str, str]:
    """(path to read, note): the drawing as a DXF in millimetres, which every reader
    assumes. A drawing in millimetres is the DXF itself (its header is enough to tell,
    so it is not loaded twice) and is read from where it is, exactly as before; one in
    metres or centimetres is converted into work_dir\\mm. Raises core.units.UnitsError
    when the units cannot be known - the drawing is then not read. `forced`: units a
    person stated for it."""
    sys.path.insert(0, os.path.join(HERE, "cad2revit"))
    from core import units                                     # noqa: E402
    dxf = dxf_for(path, work_dir, log)
    loose = has_loose_attdefs(dxf)
    inch = has_inch_sizes(dxf)
    if not loose and not inch and forced in (None, "") and units.header_units(dxf) == units.MM:
        return path, ""
    doc = ezdxf.readfile(dxf)
    k, name, note, _ = units.to_mm(doc, forced)
    shown = show_attdefs(doc) if loose else 0
    # member sizes written in inches on a drawing in mm (9"X21"): read in mm. A drawing in
    # feet and inches has had every size rewritten by to_mm already
    sized = units.rewrite_inch_sizes(doc) if inch and name not in (units.NAME[1], units.NAME[2]) else 0
    if sized:
        note = (note + "; " if note else "") + f"{sized} member sizes written in inches (9\"x18\") read in mm"
    if k == 1.0 and forced in (None, "") and not shown and not sized:
        return path, note           # the extractor will come to the same view of it
    if shown and log:
        log(f"     {shown} attribute tags placed on the drawing read as the text they show")
    # converted, units a person stated, or loose attribute tags shown as text: a copy
    # labelled mm, so every reader agrees
    out_dir = os.path.join(work_dir or tempfile.mkdtemp(prefix="c2r_mm_"), "mm")
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, os.path.splitext(os.path.basename(path))[0] + ".dxf")
    doc.header["$INSUNITS"] = units.MM
    doc.saveas(out)
    return out, note


_ENTITIES = re.compile(rb"\n\s*2\r?\nENTITIES\r?\n")
_ATTDEF = re.compile(rb"\n\s*0\r?\nATTDEF\r?\n")
_TEXT_LOOK = ("layer", "color", "true_color", "linetype", "lineweight", "style", "height", "rotation",
              "width", "oblique", "halign", "valign", "text_generation_flag", "insert", "align_point",
              "extrusion")


def has_loose_attdefs(dxf: str) -> bool:
    """Whether the drawing itself (not a block) holds attribute definitions. Read off
    the file, so drawings without any are not loaded for it."""
    try:
        with open(dxf, "rb") as f:
            data = f.read()
    except OSError:
        return False
    m = _ENTITIES.search(data)
    return bool(m and _ATTDEF.search(data, m.end()))


def has_inch_sizes(dxf: str) -> bool:
    """Whether a text in the drawing writes a member size in inches (9"X21"). Read off
    the file, so drawings without any are not loaded for it."""
    sys.path.insert(0, os.path.join(HERE, "cad2revit"))
    from core import units                                     # noqa: E402
    try:
        with open(dxf, "rb") as f:
            return units.IN_PAIR_BYTES.search(f.read()) is not None
    except OSError:
        return False


def show_attdefs(doc) -> int:
    """An attribute definition placed on the drawing, not in a block, shows its tag:
    that is what the drafter sees and means (NIFT framing plans: beam marks 4B3, 4B6A
    written as attribute definitions). Each becomes a TEXT of that tag, in the same
    place, layer, style and size, so every reader reads it as the drawing shows it.
    Model space only: on paper-space sheets they are a title block's leftover fields
    (SIGNATURE, R0, 1:200@A2 - Sasan Gir, AKANISTHA), not the drawing. Attribute
    definitions inside blocks are untouched. Returns how many."""
    n = 0
    for lay in (doc.modelspace(),):
        for a in list(lay.query("ATTDEF")):
            tag = (a.dxf.get("tag", "") or "").strip()
            if not tag:
                continue
            look = {k: a.dxf.get(k) for k in _TEXT_LOOK if a.dxf.hasattr(k)}
            try:
                lay.add_text(tag, dxfattribs=look)
            except Exception:
                continue
            lay.delete_entity(a)
            n += 1
    return n


def open_drawing(path: str, work_dir: str | None = None, log=None):
    """An ezdxf document for a DWG or DXF."""
    return ezdxf.readfile(dxf_for(path, work_dir, log))


def clip_to(path: str, handles, out_path: str, work_dir: str | None = None,
            borrow=(), shift=(0.0, 0.0)) -> str:
    """A copy of the drawing holding only these model-space entities.

    Used to hand one view (a framing plan) to a reader without the detail
    drawings beside it. Blocks, layers and paper space are kept as they are;
    the original file is not touched. `borrow`: entities from elsewhere moved
    in by `shift` (a twin drawing's slab marks onto the plan)."""
    doc = open_drawing(path, work_dir)
    keep = set(handles)
    moved = set(borrow) - keep
    msp = doc.modelspace()
    for e in list(msp):
        h = e.dxf.handle
        if h in moved:
            try:
                e.translate(shift[0], shift[1], 0.0)
            except Exception:
                msp.delete_entity(e)
        elif h not in keep:
            msp.delete_entity(e)
    doc.saveas(out_path)
    return out_path


def clip_out(path: str, handles, out_path: str, work_dir: str | None = None) -> str:
    """A copy of the drawing without these model-space entities - the other floors'
    plans drawn beside this one. Everything else stays: the schedules and details
    beside a plan are read with it, as when the plan is alone in its file."""
    doc = open_drawing(path, work_dir)
    drop = set(handles)
    msp = doc.modelspace()
    for e in list(msp):
        if e.dxf.handle in drop:
            msp.delete_entity(e)
    doc.saveas(out_path)
    return out_path


def _text_of(e) -> tuple[str, float, tuple[float, float], bool] | None:
    t = e.dxftype()
    try:
        if t == "MTEXT":
            raw = e.text or ""
            return raw, float(e.dxf.char_height or 0), (e.dxf.insert.x, e.dxf.insert.y), \
                "\\L" in raw or "%%U" in raw.upper()
        if t in ("TEXT", "ATTRIB"):
            raw = e.dxf.text or ""
            p = e.dxf.insert
            return raw, float(e.dxf.height or 0), (p.x, p.y), "%%U" in raw.upper()
    except Exception:
        return None
    return None


def texts_in(entities, space: str, depth: int = 0) -> list[TextItem]:
    """Every text in these entities, opening blocks (title blocks are blocks)."""
    out: list[TextItem] = []
    for e in entities:
        kind = e.dxftype()
        if kind == "INSERT" and depth < 3:
            try:
                for a in e.attribs:
                    got = _text_of(a)
                    if got:
                        raw, h, (x, y), u = got
                        out.append(TextItem(clean(_plain(a, raw)), raw, h, x, y,
                                            a.dxf.layer, a.dxf.handle or "", space, u))
                inner = list(e.virtual_entities())
                got = texts_in(inner, space, depth + 1)
                _outline_beside(got, inner)
                out.extend(got)
            except Exception:
                continue
            continue
        got = _text_of(e)
        if not got:
            continue
        raw, h, (x, y), u = got
        s = clean(_plain(e, raw))
        if s:
            cx, cy = _centre(e, s, h, x, y)
            out.append(TextItem(s, raw, h, x, y, e.dxf.layer,
                                getattr(e.dxf, "handle", "") or "", space, u, cx, cy, _rotation(e),
                                original=_original_of(e)))
    return out


def _original_of(e) -> str:
    """The wording kept on a text the app rewrote in mm (core.units.keep_original), or ""."""
    try:
        if not e.has_xdata("C2R_ORIGINAL"):
            return ""
    except Exception:
        return ""
    sys.path.insert(0, os.path.join(HERE, "cad2revit"))
    from core.units import original_text                      # noqa: E402
    return clean(original_text(e))


def _outline_beside(texts: list[TextItem], entities) -> None:
    """Give each text of a block the closed outline drawn beside it in that block
    (within 4 text heights), if it has none from a block nested deeper."""
    import math
    boxes = []
    for v in entities:
        if v.dxftype() != "LWPOLYLINE":
            continue
        pts = [(p[0], p[1]) for p in v.get_points("xy")]
        if len(pts) < 3 or not (v.closed or (len(pts) > 3 and math.dist(pts[0], pts[-1]) <= 1.0)):
            continue
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        boxes.append(((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2, v.dxf.layer))
    if not boxes:
        return
    for t in texts:
        if t.block_outline is not None:
            continue
        at = [(t.x, t.y)] + ([(t.cx, t.cy)] if t.cx is not None else [])

        def gap(o):
            return min(math.hypot(o[0] - x, o[1] - y) for x, y in at)
        best = min(boxes, key=gap)
        if gap(best) <= 4.0 * max(t.height or 0.0, 1.0):
            t.block_outline = [round(best[0], 1), round(best[1], 1), best[2]]


def _plain(e, raw: str) -> str:
    if e.dxftype() == "MTEXT":
        try:
            return e.plain_text()
        except Exception:
            return raw
    return raw


def centre_of(e) -> tuple[float, float] | None:
    """A point to decide which view an entity belongs to."""
    try:
        b = bbox.extents([e], fast=True)
    except Exception:
        return None
    if not b.has_data:
        return None
    c = b.center
    return c.x, c.y


def extent_of(e) -> tuple[float, float, float, float] | None:
    try:
        b = bbox.extents([e], fast=True)
    except Exception:
        return None
    if not b.has_data:
        return None
    return b.extmin.x, b.extmin.y, b.extmax.x, b.extmax.y
