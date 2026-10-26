"""Detail sheets: confirm a caption's size against the drawing, and collect its bars.

Detail sheets come in two kinds, and the training sets have both:

* Cross-sections (Sasan Gir plinth: "STB1 (230x600)" under a section dimensioned
  600 high and 230 wide). There the dimensions are the section's size.
* Longitudinal elevations (Sasan Gir and Villa 5 beam details), drawn along the
  span. The dimensions near the caption are span lengths and curtailments
  (2100, 1000, 1400) and say nothing about the section.

So a dimension only ever confirms: one close to the caption whose value equals
the caption's width or depth adds that as evidence. A caption with no matching
dimension is left as it was - never called wrong because of a span length.

Bar callouts (3-T16, 2-T16+2-T12, T8 @150 c/c) are given to the nearest caption
within reach and kept as written, with where they sit relative to it. Telling top
bars from bottom bars needs the beam's drawn outline and is left for later.
"""
from __future__ import annotations

import math
import re

from ..marks import BAR
from ..model import Building, Evidence, Value, READ

READER = "details"
DETAIL_KINDS = ("beam_details", "column_details", "footing_details", "slab_details",
                "stair_details", "section")
DIM_REACH = 8.0       # caption heights: how close a confirming dimension must be
BAR_REACH = 14.0      # caption heights: how far a bar callout can be from its caption
STIRRUP = re.compile(r"C\s*/\s*C|STIRR|LINKS?|RINGS?", re.I)


def dimension_reading(e) -> tuple[float | None, float | None, str]:
    """(value shown, length drawn, note) for one DIMENSION.

    The value shown is what a person reads on the drawing:
    * a number typed over the dimension ("T8@150 C/C" typed over it: no value);
    * else the drawn length times the dimension's scale, DIMLFAC. Sections drawn
      enlarged carry one (Sasan Gir's column schedule: drawn 4x, DIMLFAC 0.25, so
      a 2400 line reads 600);
    * where the dimension's own picture holds a different number, that number:
      it is what was displayed when the drawing was saved (Sasan Gir C20: the
      line was stretched to 266 x 4 but still reads 300). The note says so.
    """
    try:
        drawn = e.get_measurement()
        drawn = float(drawn) if not hasattr(drawn, "x") else None
    except Exception:
        drawn = None
    override = (e.dxf.get("text", "") or "").strip()
    if override and override != "<>":
        num = re.fullmatch(r"\s*(\d{2,4})\s*(?:MM)?\s*", override, re.I)
        return (float(num.group(1)) if num else None), drawn, "typed over"
    if drawn is None:
        return None, None, ""
    try:
        lfac = abs(float(e.override().get("dimlfac", 1.0) or 1.0))
    except Exception:
        lfac = 1.0
    value = float(round(drawn * lfac))
    shown = None
    try:
        blk = e.get_geometry_block()
        for x in (blk or []):
            if x.dxftype() in ("MTEXT", "TEXT"):
                s = x.plain_text() if x.dxftype() == "MTEXT" else x.dxf.text
                num = re.fullmatch(r"\s*(\d{2,5})\s*", s or "")
                shown = float(num.group(1)) if num else None
                break
    except Exception:
        shown = None
    if shown is not None and not value:
        return shown, drawn, "displayed only"           # a length ezdxf cannot measure (Villa 5)
    if shown is not None and abs(shown - value) > max(2.0, 0.02 * value):
        return shown, drawn, f"displayed {shown:.0f}, drawn length gives {value:.0f}"
    return value, drawn, ("scale %g" % lfac if lfac != 1.0 else "")


def _dimensions(doc, handles: set[str]) -> list[tuple[float, float, float, str, str]]:
    """(value, x, y, handle, direction) for each dimension; direction is "h", "v" or ""."""
    out = []
    for e in doc.modelspace():
        if e.dxftype() != "DIMENSION" or e.dxf.handle not in handles:
            continue
        m, _, _ = dimension_reading(e)
        p = e.dxf.get("text_midpoint", None) or e.dxf.get("defpoint", None)
        direction = ""
        a, c = e.dxf.get("defpoint2", None), e.dxf.get("defpoint3", None)
        if a is not None and c is not None:
            dx, dy = abs(c.x - a.x), abs(c.y - a.y)
            direction = "h" if dx > 3 * dy else "v" if dy > 3 * dx else ""
        if m and p is not None:
            out.append((float(round(m)), p.x, p.y, e.dxf.handle, direction))
    return out


def _boxes(doc, handles: set[str]) -> list[tuple[float, float, float, float, str]]:
    """Closed four-sided outlines: section boxes. (x0, y0, x1, y1, handle)"""
    out = []
    for e in doc.modelspace():
        if e.dxftype() != "LWPOLYLINE" or e.dxf.handle not in handles:
            continue
        pts = [(p[0], p[1]) for p in e.get_points("xy")]
        if len(pts) == 5 and math.hypot(pts[0][0] - pts[4][0], pts[0][1] - pts[4][1]) < 1e-6:
            pts = pts[:4]
        if len(pts) != 4 or not (e.closed or len(pts) == 4):
            continue
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        if all(abs(x - min(xs)) < 1e-6 or abs(x - max(xs)) < 1e-6 for x in xs) and \
                max(xs) > min(xs) and max(ys) > min(ys):
            out.append((min(xs), min(ys), max(xs), max(ys), e.dxf.handle))
    return out


SECTION_RANGE = {"beam": ((75, 1500), (100, 3000)), "column": ((150, 1200), (150, 1200))}


def _plausible(family: str, w: float, d: float) -> bool:
    (w0, w1), (d0, d1) = SECTION_RANGE.get(family, ((0, 1e9), (0, 1e9)))
    return w0 <= w <= w1 and d0 <= d <= d1


def _sections_from_dimensions(b, rec, dims, made) -> None:
    """A section captioned with its mark only (GB1), its size given by dimensions.

    Read only when all three agree: a closed section box near the caption, a
    horizontal and a vertical dimension beside that box, and the two dimension
    values in the same proportion as the box (within 10%). On an elevation the
    numbers near a caption are span lengths and no box matches them, so nothing
    is read there.
    """
    from ..marks import labels_in, parse_mark
    from ..model import Element
    have = {(round(s.geometry["at"][0]), round(s.geometry["at"][1]))
            for s in b.of("spec", sheet=rec.id) if s.geometry.get("at")}
    boxes = _boxes(rec.doc, set(rec.view.handles))
    for t in rec.texts:
        if t.space != "model" or (round(t.x), round(t.y)) in have:
            continue
        if any(lb.width for lb in labels_in(t.text)):
            continue                                       # already a sized caption
        mk = parse_mark(t.text)
        if mk is None or mk.family() not in ("beam", "column"):
            continue
        h = max(t.height, 1.0)
        near = [bx for bx in boxes
                if math.hypot((bx[0] + bx[2]) / 2 - t.x, (bx[1] + bx[3]) / 2 - t.y) <= DIM_REACH * h * 2]
        for x0, y0, x1, y1, bh in sorted(near, key=lambda bx: math.hypot((bx[0] + bx[2]) / 2 - t.x,
                                                                         (bx[1] + bx[3]) / 2 - t.y)):
            bw, bd = x1 - x0, y1 - y0
            reach = 1.5 * max(bw, bd)
            beside = [d for d in dims if x0 - reach <= d[1] <= x1 + reach and y0 - reach <= d[2] <= y1 + reach]
            hs = [d for d in beside if d[4] == "h" and 80 <= d[0] <= 3000]
            vs = [d for d in beside if d[4] == "v" and 80 <= d[0] <= 3000]
            pair = next(((dh, dv) for dh in hs for dv in vs
                         if abs((dh[0] / dv[0]) / (bw / bd) - 1.0) <= 0.10), None)
            if pair is None:
                continue
            dh, dv = pair
            if not _plausible(mk.family(), dh[0], dv[0]):
                continue    # Villa 5: "C3" over a 1000 x 1400 box is the footing under C3
            evs = [Evidence(reader=READER, sheet=rec.id, source="section dimension",
                            handle=d[3], at=[d[1], d[2]], text=f"{d[0]:.0f}") for d in pair]
            box_ev = Evidence(reader=READER, sheet=rec.id, source="section box", handle=bh,
                              at=[(x0 + x1) / 2, (y0 + y1) / 2], text=f"{bw:.0f} x {bd:.0f} drawn")
            cap_ev = Evidence(reader=READER, sheet=rec.id, source="section caption", layer=t.layer,
                              handle=t.handle, at=[t.x, t.y], text=t.text)
            b.add(Element(
                id=f"{rec.id}/spec/{len(b.elements)}", type="spec", sheet=rec.id,
                level=rec.levels[0] if len(rec.levels) == 1 else "", wing=rec.wing, mark=mk.key,
                geometry={"at": [t.x, t.y], "text_height": t.height},
                props={"family": Value(mk.family(), READ, 1.0, [cap_ev]),
                       "width": Value(dh[0], READ, 0.7, [evs[0], box_ev]),
                       "depth": Value(dv[0], READ, 0.7, [evs[1], box_ev])},
                extra={"raw": mk.raw, "from": "section dimensions"}))
            made["sizes from section dimensions"] = made.get("sizes from section dimensions", 0) + 1
            break


def _to_caption(s, x: float, y: float) -> float:
    """Distance from a point to a caption's text, not to its centre: a long
    caption such as "STB1 (230x600)" has a dimension beside its first letters."""
    cx, cy = s.geometry["at"]
    h = max(s.geometry.get("text_height") or 100.0, 1.0)
    half = 0.4 * h * (s.geometry.get("text_len") or 0)
    r = math.radians(s.geometry.get("rotation") or 0.0)
    ux, uy = math.cos(r), math.sin(r)
    along = (x - cx) * ux + (y - cy) * uy
    across = -(x - cx) * uy + (y - cy) * ux
    return math.hypot(max(0.0, abs(along) - half), across)


def read(b: Building, rec) -> dict[str, int]:
    if rec.verdict.kind not in DETAIL_KINDS or rec.doc is None:
        return {}
    dims = _dimensions(rec.doc, set(rec.view.handles))
    made = {"confirmed by dimension": 0, "bar callouts": 0}
    _sections_from_dimensions(b, rec, dims, made)
    specs = [e for e in b.of("spec", sheet=rec.id) if e.geometry.get("at")]
    if not specs:
        return {k: v for k, v in made.items() if v}

    for s in specs:
        if s.extra.get("from") == "section dimensions":
            continue                    # its size came from those dimensions already
        x, y = s.geometry["at"]
        h = max(s.geometry.get("text_height") or 100.0, 1.0)
        for name in ("width", "depth"):
            v = s.props.get(name)
            if v is None:
                continue
            match = [d for d in dims if abs(d[0] - v.value) <= 5.0
                     and _to_caption(s, d[1], d[2]) <= DIM_REACH * h]
            if match:
                d = min(match, key=lambda d: _to_caption(s, d[1], d[2]))
                v.evidence.append(Evidence(reader=READER, sheet=rec.id, source="dimension",
                                           handle=d[3], at=[d[1], d[2]], text=f"{d[0]:.0f}"))
                made["confirmed by dimension"] += 1

    # each bar callout to its nearest caption, if one is within reach
    for t in rec.texts:
        if t.space != "model" or not BAR.search(t.text):
            continue
        best = min(specs, key=lambda s: _to_caption(s, t.x, t.y))
        bx, by = best.geometry["at"]
        h = max(best.geometry.get("text_height") or 100.0, 1.0)
        if _to_caption(best, t.x, t.y) > BAR_REACH * h:
            continue
        kind = "stirrups" if STIRRUP.search(t.text) and not re.search(r"\d+\s*-\s*T", t.text) else "bars"
        ev = Evidence(reader=READER, sheet=rec.id, source="bar callout", layer=t.layer,
                      handle=t.handle, at=[t.x, t.y], text=t.text[:60])
        entry = {"text": t.text, "dx": round(t.x - bx), "dy": round(t.y - by)}
        if kind in best.props:
            best.props[kind].value.append(entry)
            best.props[kind].evidence.append(ev)
        else:
            best.props[kind] = Value([entry], READ, 0.8, [ev])
        made["bar callouts"] += 1
    return {k: v for k, v in made.items() if v}
