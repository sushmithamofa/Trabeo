"""Drawing units: every reader works in millimetres, so a drawing in other units is
converted first - or stopped, when its units cannot be known.

A DXF says its units in $INSUNITS, but that header is often wrong: a drawing made
in millimetres from a template set to metres still says metres. So the declared
unit is believed only when the drawing's own text agrees with it. Text is drawn at
a size a person can read on paper (2-5 mm at 1:50-1:200), so in model space it is
roughly 100-500 mm tall whatever the units are - 0.1-0.5 in a metre drawing,
10-50 in a centimetre one. Where there is no text, the size of the drawing is used.

    millimetres                 read as they are
    metres, centimetres, dm     every model-space entity scaled to millimetres
    drawn in mm, labelled not   read as millimetres; the note says so
    feet and inches             scaled to millimetres, and every size WRITTEN in feet and
                                inches (12"x24", 1'-6", +10'-6") rewritten in millimetres,
                                so every reader reads mm - but only when the drawing also
                                writes feet and inches; labelled so with none written,
                                stopped (state its units)
    no units, not mm-sized      stopped: the units cannot be known

A person can settle a stopped drawing by stating its units (a project decision);
that is then used as given.
"""
from __future__ import annotations

import re
import statistics

MM = 4
FACTOR = {4: 1.0, 5: 10.0, 14: 100.0, 6: 1000.0, 1: 25.4, 2: 304.8}
NAME = {0: "not set", 1: "inches", 2: "feet", 4: "millimetres", 5: "centimetres",
        6: "metres", 14: "decimetres"}
CODE = {"mm": 4, "millimetres": 4, "millimeters": 4, "cm": 5, "centimetres": 5, "centimeters": 5,
        "dm": 14, "m": 6, "metres": 6, "meters": 6, "in": 1, "inches": 1, "ft": 2, "feet": 2}
IMPERIAL = (1, 2)
# where model-space text heights fall, per unit (paper text 1.5-10 mm at 1:20 - 1:200)
TEXT_RANGE = {4: (30.0, 2000.0), 5: (3.0, 200.0), 14: (0.3, 20.0), 6: (0.03, 2.0),
              1: (1.2, 80.0), 2: (0.1, 7.0)}
# where a drawing's overall width falls (a building or a sheet of details: 2 m - 1 km)
SIZE_RANGE = {4: (2000.0, 1e6), 5: (200.0, 1e5), 14: (20.0, 1e4), 6: (2.0, 1000.0),
              1: (80.0, 40000.0), 2: (6.5, 3300.0)}


class UnitsError(Exception):
    """The drawing's units cannot be used; the message says why, in plain words."""


def header_units(path: str) -> int | None:
    """$INSUNITS from a DXF's header without loading the drawing (None if not found)."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            prev = ""
            want = False
            for n, line in enumerate(f):
                s = line.strip()
                if want and prev == "70":
                    return int(s)
                if s == "$INSUNITS":
                    want = True
                if s == "ENDSEC" and n > 4:
                    return None
                prev = s
    except (OSError, ValueError):
        return None
    return None


def _text_heights(msp) -> list[float]:
    out = []
    for e in msp:
        t = e.dxftype()
        try:
            if t == "TEXT":
                h = float(e.dxf.height)
            elif t == "MTEXT":
                h = float(e.dxf.char_height)
            else:
                continue
        except Exception:
            continue
        if h > 0:
            out.append(h)
    return out


ENOUGH_POINTS = 40       # fewer, and the drawing's size says nothing about its units


def _width(msp) -> float | None:
    xs, ys = [], []
    for e in msp:
        t = e.dxftype()
        try:
            if t == "LINE":
                pts = [e.dxf.start, e.dxf.end]
            elif t == "LWPOLYLINE":
                pts = list(e.get_points("xy"))
            elif t in ("CIRCLE", "ARC"):
                pts = [e.dxf.center]
            elif t in ("INSERT", "TEXT", "MTEXT"):
                pts = [e.dxf.insert]
            else:
                continue
        except Exception:
            continue
        for p in pts:
            xs.append(p[0])
            ys.append(p[1])
    if len(xs) < ENOUGH_POINTS:
        return None
    xs.sort()
    ys.sort()
    k = len(xs) // 50                  # a few far-off scraps do not make the drawing
    return max(xs[-1 - k] - xs[k], ys[-1 - k] - ys[k])


def _fits(code: int, h: float | None, w: float | None) -> bool | None:
    if h is not None:
        lo, hi = TEXT_RANGE[code]
        return lo <= h <= hi
    if w is not None:
        lo, hi = SIZE_RANGE[code]
        return lo <= w <= hi
    return None


def decide(doc, forced: str | int | None = None) -> tuple[float, str, str]:
    """(factor to millimetres, unit name, note). Raises UnitsError when the drawing
    cannot be read in millimetres. `forced`: the units a person stated."""
    if forced not in (None, ""):
        code = forced if isinstance(forced, int) else CODE.get(str(forced).strip().lower())
        if code not in FACTOR:
            raise UnitsError(f"The units given for this drawing ({forced}) cannot be used: give mm, cm, dm, m, "
                             "in or ft.")
        # a choice the drawing plainly contradicts (aldoot, drawn in mm, said to be feet: its
        # 250 text would be 76 m tall) is not used when the drawing's own units are clear
        msp = doc.modelspace()
        hs = _text_heights(msp)
        h = statistics.median(hs) if len(hs) >= 5 else None
        w = None if h is not None else _width(msp)
        if _fits(code, h, w) is False:
            try:
                k, name, note = decide(doc)
            except UnitsError:
                k = None
            if k is not None:
                seen = f"its text is {h:.3g} tall" if h is not None else f"it is {w:.4g} across"
                return k, name, (f"the units chosen for it ({NAME[code]}) do not fit this drawing ({seen}); read "
                                 f"as {name}, as it is drawn" + (f"; {note}" if note else ""))
        return FACTOR[code], NAME[code], f"units {NAME[code]}, as decided in the review"
    declared = int(doc.header.get("$INSUNITS", 0) or 0)
    msp = doc.modelspace()
    hs = _text_heights(msp)
    h = statistics.median(hs) if len(hs) >= 5 else None
    w = None if h is not None else _width(msp)
    seen = f"its text is {h:.3g} tall" if h is not None else (f"it is {w:.4g} across" if w is not None else "")
    if declared == MM:
        # believed, unless the drawing is plainly metre-sized: text under 2 units
        # tall on something under a metre across is a metre drawing labelled mm
        if h is not None and h < 2.0:
            w2 = _width(msp)
            if w2 is not None and w2 < 1000.0:
                raise UnitsError(f"This drawing says millimetres, but its text is {h:.3g} tall and it is "
                                 f"{w2:.4g} across - it looks like metres. State its units in the review.")
        return 1.0, NAME[MM], ""
    if declared in FACTOR and _fits(declared, h, w):
        if declared in IMPERIAL:
            if imperial_written(msp) < IMPERIAL_SEEN:
                raise UnitsError(f"This drawing says {NAME[declared]}, but no sizes in feet and inches "
                                 "(12\"x24\", 1'-6\") are written on it, so its units cannot be trusted. State its "
                                 "units (mm, cm, m, in or ft) in the review.")
            return FACTOR[declared], NAME[declared], (f"drawn in {NAME[declared]} ({seen}); converted to "
                                                      "millimetres, sizes written in feet and inches read in mm")
        return FACTOR[declared], NAME[declared], f"drawn in {NAME[declared]} ({seen}); converted to millimetres"
    if _fits(MM, h, w):
        note = (f"labelled {NAME.get(declared, 'unit code %d' % declared)} but drawn in millimetres ({seen}); "
                "read as millimetres") if declared else ""
        return 1.0, NAME[MM], note
    if not seen:
        # too little in it to tell (a scrap, a test sheet): read as millimetres, said plainly
        return 1.0, NAME[MM], (f"labelled {NAME.get(declared, 'unit code %d' % declared)}, but too little is "
                               "drawn to check that; read as millimetres - confirm")
    raise UnitsError(f"This drawing's units are unclear: it says {NAME.get(declared, 'unit code %d' % declared)}, "
                     f"but {seen}, which does not fit that or millimetres. State its units (mm, cm, m, in or ft) "
                     "in the review.")


def scale_doc(doc, k: float, imperial: bool = False) -> int:
    """Scale model space by k into millimetres, in place. Viewports keep looking at the
    same things and dimensions keep showing the same numbers - except in a drawing in
    feet and inches, whose dimensions show feet and inches: they then show millimetres.
    Returns how many entities could not be scaled (removed, so they cannot mislead)."""
    from ezdxf.math import Matrix44
    if abs(k - 1.0) < 1e-12:
        doc.header["$INSUNITS"] = MM
        return 0
    m = Matrix44.scale(k)
    msp = doc.modelspace()
    bad = []
    for e in list(msp):
        lfac = None
        if e.dxftype() == "DIMENSION":
            lfac = _lfac(doc, e)
        try:
            e.transform(m)
        except Exception:
            bad.append(e)
            continue
        if lfac is not None:
            # the number shown stays what it was: a metre drawing showing mm (DIMLFAC
            # 1000) now shows them at 1; one showing metres keeps reading in mm. Feet and
            # inches shown become mm: the length as drawn, times a detail's own scale
            # (DIMLFAC 0.5); a feet drawing showing inches (DIMLFAC 12) shows mm at 1
            new = (1.0 if abs(lfac - 12.0) < 1e-9 else lfac) if imperial else lfac / k
            try:
                ov = e.override()
                ov.set("dimlfac", new if new >= 0.05 else 1.0)
                ov.commit()
            except Exception:
                pass
    for e in bad:
        msp.delete_entity(e)
    for lay in doc.layouts:
        if lay.name == "Model":
            continue
        for e in lay:
            if e.dxftype() != "VIEWPORT":
                continue
            c = e.dxf.get("view_center_point", None)
            if c is not None:
                e.dxf.view_center_point = (c[0] * k, c[1] * k)
            t = e.dxf.get("view_target_point", None)
            if t is not None:
                e.dxf.view_target_point = (t[0] * k, t[1] * k, t[2] * k)
            if e.dxf.get("view_height", None):
                e.dxf.view_height = float(e.dxf.view_height) * k
    doc.header["$INSUNITS"] = MM
    return len(bad)


def _lfac(doc, dim) -> float:
    try:
        v = dim.override().get("dimlfac", None)
        if v is None:
            st = doc.dimstyles.get(dim.dxf.get("dimstyle", "Standard"))
            v = st.dxf.get("dimlfac", None) if st is not None else None
        return abs(float(v)) if v else 1.0
    except Exception:
        return 1.0


def to_mm(doc, forced=None) -> tuple[float, str, str, int]:
    """decide() then scale_doc(): (factor, unit name, note, entities dropped). A drawing
    in feet and inches also has the sizes written on it rewritten in millimetres."""
    k, name, note = decide(doc, forced)
    imperial = name in (NAME[1], NAME[2])
    dropped = scale_doc(doc, k, imperial) if k != 1.0 else 0
    if dropped:
        note += f"; {dropped} entities of a kind that cannot be scaled were left out"
    if imperial:
        n = rewrite_imperial(doc)
        note += f"; {n} texts with sizes in feet and inches rewritten in millimetres"
    return k, name, note, dropped


# ---------------------------------------------------------------- feet and inches written
# 1'-6"  10' 6"  2'-0 1/2"  10'  (feet, then inches)    9"  12"  4 1/2"  4.5"  1/2"  (inches)
_Q = "\"″"                      # " and the double prime
_P = "'′"                       # ' and the prime
_FT_IN = re.compile(
    r"(?<![\w.])(\d{1,3})\s*[" + _P + r"]"
    r"(?:\s*-\s*(\d{1,2}(?:\.\d+)?)(?:\s+(\d{1,2})/(\d{1,2}))?\s*[" + _Q + r"]?"
    r"|\s*(\d{1,2}(?:\.\d+)?)(?:\s+(\d{1,2})/(\d{1,2}))?\s*[" + _Q + r"])?")
# (after any letter but the x of a size: 9"x18" is two values, B18" is not one)
_IN = re.compile(r"(?<![\d.'/′])(?<![A-WYZa-wyz])(\d{1,3}(?:\.\d+)?)(?:\s+(\d{1,2})/(\d{1,2}))?\s*["
                 + _Q + r"]")
_FRAC_IN = re.compile(r"(?<![\d.'/′])(?<![A-WYZa-wyz])(\d{1,2})/(\d{1,2})\s*[" + _Q + r"]")
_GRID_PRIME = re.compile(r"^\s*\d[" + _P + r"]\s*$")      # 1' alone: a grid named 1-prime, not a foot
# a size written as two bare numbers on a drawing in feet and inches: 9x18, B1 (12 X 24)
_BARE_PAIR = re.compile(r"(?<![\w.])(\d{1,2})(\s*[xX×*]\s*)(\d{1,2})(?![\d.\w" + _Q + _P + r"])")
_SCALE = re.compile(r"[" + _Q + _P + r"]\s*=|=\s*\d+\s*[" + _P + r"]")      # 1/8"=1'-0": a scale
_LEVEL_BEFORE = re.compile(r"(?:(?<![\w)\"″])([+\-±])\s*\(?\s*|"
                           r"\b(?:LVL|LEVEL|EL|ELEV|FFL|SFL|TOS|TOC|BOF|BOS)\b\.?\s*:?\s*)$", re.I)
IMPERIAL_SEEN = 2                    # values in feet and inches that make a drawing so
BARE_INCHES_MAX = 72                 # a bare pair this small is inches (no member is 72 mm)


def _num(a, b=None, c=None) -> float:
    v = float(a) if a else 0.0
    if b and c and float(c):
        v += float(b) / float(c)
    return v


def imperial_value(text: str) -> bool:
    return bool(_FT_IN.search(text) or _IN.search(text) or _FRAC_IN.search(text))


def imperial_written(msp) -> int:
    """How many texts write a value in feet and inches (1'-6", 9", 4 1/2")."""
    n = 0
    for e in msp:
        try:
            t = e.dxftype()
            s = e.dxf.text if t == "TEXT" else (e.plain_text() if t == "MTEXT" else
                                                (e.dxf.get("text", "") if t == "DIMENSION" else ""))
        except Exception:
            continue
        if s and not _SCALE.search(s) and not _GRID_PRIME.match(s) and imperial_value(s):
            n += 1
    return n


def imperial_to_mm(s: str) -> str:
    """The text with each value in feet and inches written in millimetres: 9"x18" ->
    229X457, 1'-6" -> 457, a level +10'-6" -> +3.200 (levels are read in metres), and a
    bare size pair 9x18 -> 229X457. A scale (1/8"=1'-0") is left as written."""
    if not s or _SCALE.search(s) or _GRID_PRIME.match(s):
        return s

    def out(mm: float, start: int, text: str, level_ok: bool = False) -> str:
        # a level is written in feet (+10'-6", EL 98'-0"): read in metres, as +3.200
        before = _LEVEL_BEFORE.search(text[:start]) if level_ok else None
        if before:
            m = f"{mm / 1000.0:.3f}"
            return m if before.group(1) else "+" + m
        return f"{mm:.0f}"

    def ft_in(m):
        ft = float(m.group(1))
        inch = _num(m.group(2), m.group(3), m.group(4)) if m.group(2) else _num(m.group(5), m.group(6), m.group(7))
        return out((ft * 12.0 + inch) * 25.4, m.start(), m.string, level_ok=True)

    def bare(m):
        a, b = int(m.group(1)), int(m.group(3))
        if not (2 <= a <= BARE_INCHES_MAX and 2 <= b <= BARE_INCHES_MAX):
            return m.group(0)
        return f"{a * 25.4:.0f}X{b * 25.4:.0f}"

    s = _BARE_PAIR.sub(bare, s)
    s = _FT_IN.sub(ft_in, s)
    s = _IN.sub(lambda m: out(_num(m.group(1), m.group(2), m.group(3)) * 25.4, m.start(), m.string), s)
    s = _FRAC_IN.sub(lambda m: out(_num(None, m.group(1), m.group(2)) * 25.4, m.start(), m.string), s)
    return s


ORIGINAL_APPID = "C2R_ORIGINAL"     # the text as the consultant wrote it, kept on a rewritten text


def keep_original(e, old: str) -> None:
    """Keep a rewritten text's original wording on it (extended data no reader reads), so
    the Review page can show the drawing as the consultant wrote it: 9"X18", not 229X457."""
    try:
        doc = e.doc
        if doc is not None and ORIGINAL_APPID not in doc.appids:
            doc.appids.new(ORIGINAL_APPID)
        e.set_xdata(ORIGINAL_APPID, [(1000, (old or "")[:250])])
    except Exception:
        pass


def original_text(e) -> str:
    """The wording kept by keep_original, or ""."""
    try:
        if e.has_xdata(ORIGINAL_APPID):
            for code, value in e.get_xdata(ORIGINAL_APPID):
                if code == 1000:
                    return str(value)
    except Exception:
        pass
    return ""


def rewrite_imperial(doc, convert=None, anonymous_blocks: bool = True) -> int:
    """Rewrite every text of the drawing's model space and blocks (not paper-space
    sheets: their title blocks and scales are not read for sizes) in millimetres.
    Returns how many texts changed."""
    convert = convert or imperial_to_mm
    n = 0

    def fix(e):
        nonlocal n
        t = e.dxftype()
        try:
            if t in ("TEXT", "ATTRIB", "ATTDEF"):
                new = convert(e.dxf.text or "")
                if new != (e.dxf.text or ""):
                    keep_original(e, e.dxf.text or "")
                    e.dxf.text = new
                    n += 1
            elif t == "MTEXT":
                new = convert(e.text or "")
                if new != (e.text or ""):
                    keep_original(e, e.plain_text())
                    e.text = new
                    n += 1
            elif t == "DIMENSION":
                old = e.dxf.get("text", "") or ""
                if old and old != "<>":
                    new = convert(old)
                    if new != old:
                        e.dxf.text = new
                        n += 1
            if t == "INSERT":
                for a in e.attribs:
                    fix(a)
        except Exception:
            pass
    for e in doc.modelspace():
        fix(e)
    for blk in doc.blocks:
        name = blk.name.upper()
        if name.startswith("*PAPER_SPACE") or name.startswith("*MODEL_SPACE") or \
                (not anonymous_blocks and name.startswith("*")):
            continue
        for e in blk:
            fix(e)
    return n


# A member size written as two inch values on a drawing in millimetres: 9"X21", 12"x24",
# B1 (9"x18") - the inch marks say the unit (aldoot: plan in mm, beam sizes in inches).
# Only this form: dimensions shown in feet too ([28'-10"]), room sizes (9'6"x12'6'') and
# scales on metric drawings are left as written.
_IN_PAIR = re.compile(r"(?<![\d'.′])(\d{1,2}(?:\.\d)?)\s*[" + _Q + r"]\s*[xX×*]\s*"
                      r"(\d{1,2}(?:\.\d)?)\s*[" + _Q + r"]")
IN_PAIR_BYTES = re.compile(rb"\n\s*[13]\r?\n[^\r\n]*(?<![\d'.])\d{1,2}(?:\.\d)?\s*(?:\"|\xe2\x80\xb3)\s*[xX*]\s*"
                           rb"\d{1,2}(?:\.\d)?\s*(?:\"|\xe2\x80\xb3)")


def inch_sizes_to_mm(s: str) -> str:
    """9"X21" -> 229X533, B1 (12"x24") -> B1 (305X610); nothing else changes."""
    if not s:
        return s
    return _IN_PAIR.sub(lambda m: f"{float(m.group(1)) * 25.4:.0f}X{float(m.group(2)) * 25.4:.0f}", s)


def rewrite_inch_sizes(doc) -> int:
    """Member sizes written in inches on a drawing in millimetres, rewritten in mm
    (model space and named blocks; dimension pictures are left as they are)."""
    return rewrite_imperial(doc, inch_sizes_to_mm, anonymous_blocks=False)
