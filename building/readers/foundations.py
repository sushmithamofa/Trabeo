"""Foundations: footings (with their PCC bed and steps), pedestals and rafts.

Drawn very differently by consultants, so read from geometry first:

* Footings are closed outlines on a foundation plan. Outlines nested around one
  centre belong together: the outermost ring, when it is uniformly 50-200 mm
  bigger than the next, is the PCC bed; the next is the footing; smaller rings
  of 600 mm or more are its steps (Sasan Gir F7: 3600 x 3900 footing, 2100 x 2400
  step - "d1(STEP-1)+d2(STEP-2)"); anything smaller is a column or pedestal.
  A layer may hold one footing type per layer (Sasan Gir: F1, F5, F7, F14) or
  everything together - it is never relied on.
* A footing's mark is a footing-family text inside or beside it (F5); failing
  that, a layer named like a footing mark (F7) as weaker evidence. The columns
  standing on it are kept too: Villa 5 names its footings after their columns.
* A raft is a large outline labelled RAFT; its thickness comes from notes
  ("RAFT THICKNESS - 600 mm", "500MM THK RAFT").
* A pedestal is a block or outline marked PD / PED.

Sizes measured from the outline are the drawing's own; sizes from a footing
schedule come in as specs through the table reader, and the linker weighs both.
"""
from __future__ import annotations

import math
import re

from ..marks import family_of, labels_in, parse_mark
from ..model import Building, Element, Evidence, Value, READ

READER = "foundations"
KINDS = ("foundation_plan",)
TITLE_HINT = re.compile(r"\b(?:RAFT|FOOTING|FOUNDATION)\s+(?:LAYOUT|PLAN)\b|\bFDN\b", re.I)
SKIP_LAYER = re.compile(r"DIM|TEXT|TXT|HATCH|GRID|LEADER|DEFPOINT|TITLE|BORDER|SHEET|CLOUD", re.I)
# a layer holding column numbers alone ('colno', 'COL NO', 'COLUMN NOS', 'COL-MARK')
COLNO_LAYER = re.compile(r"^\s*COL(?:UMN)?[\s._-]*(?:NO|NOS|NUM|NUMBER|NUMBERS|MARK|MARKS|TAG|TAGS)\s*$", re.I)
MIN_FOOTING = 600.0         # mm, smallest side of a footing or a step
MAX_FOOTING = 12000.0       # mm, larger is a raft or a site outline
PCC_RING = (40.0, 250.0)    # mm, how much bigger a PCC bed is on each side
RAFT_THK = re.compile(r"RAFT\s+THICKNESS\s*[-:=]?\s*(\d{3,4})\s*MM|(\d{3,4})\s*MM\s*THK\.?\s*RAFT", re.I)
# a layer of the foundations themselves; a PCC bed's; a column's or wall's
FOOT_LAYER = re.compile(r"FOOT|FTG|RAFT|P\.?\s?C\.?\s?C|FDN|FOUND", re.I)
PCC_LAYER = re.compile(r"P\.?\s?C\.?\s?C", re.I)
COLUMNISH_LAYER = re.compile(r"COL|CLM|SHEAR|WALL|PARDI|CORE", re.I)
JOIN_MM = 5.0               # line ends this close meet
MAX_LOOSE_LINES = 3000      # more loose lines on footing layers than this: not joined


def _walk(e, top, depth=0):
    """An entity, or what its block holds (blocks in blocks too), with the model-space
    entity it belongs to."""
    if e.dxftype() == "INSERT" and depth < 3:
        try:
            for x in e.virtual_entities():
                yield from _walk(x, top, depth + 1)
        except Exception:
            return
    else:
        yield e, top


def _loops(lines, layer):
    """Closed outlines drawn as loose lines, end to end: Akanistha's raft, 14 lines on
    'FOOTING AVADH' inside a block, its PCC bed 14 more on 'P.C.C. LINE AVADH'. Each a
    rect tuple as _rect gives (handle: the model-space entity holding the first line)."""
    out, used = [], set()
    for i, (a, c, top) in enumerate(lines):
        if i in used:
            continue
        chain, ids, cur = [a], {i}, c
        closed = False
        for _ in range(len(lines)):
            chain.append(cur)
            if math.hypot(cur[0] - a[0], cur[1] - a[1]) <= JOIN_MM:
                closed = True
                break
            nxt = [j for j, (p, q, _t) in enumerate(lines) if j not in used and j not in ids and
                   (math.hypot(p[0] - cur[0], p[1] - cur[1]) <= JOIN_MM or math.hypot(q[0] - cur[0], q[1] - cur[1]) <= JOIN_MM)]
            if len(nxt) != 1:
                break                       # an open end, or a branch: not one outline
            j = nxt[0]
            ids.add(j)
            p, q, _t = lines[j]
            cur = q if math.hypot(p[0] - cur[0], p[1] - cur[1]) <= JOIN_MM else p
        if not closed or len(chain) < 5:
            continue
        used |= ids
        pts = chain[:-1]
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
        out.append((x0, y0, x1, y1, (x0 + x1) / 2, (y0 + y1) / 2, top, layer, pts))
    return out


def _in_poly(p, pts) -> bool:
    x, y, inside = p[0], p[1], False
    for i in range(len(pts)):
        (x1, y1), (x2, y2) = pts[i - 1], pts[i]
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            inside = not inside
    return inside


def _rect(e):
    """(x0, y0, x1, y1, cx, cy, handle, layer) for a closed outline, axis box."""
    if e.dxftype() not in ("LWPOLYLINE", "POLYLINE"):
        return None
    try:
        pts = [(p[0], p[1]) for p in e.get_points("xy")] if e.dxftype() == "LWPOLYLINE" else \
            [(v.dxf.location.x, v.dxf.location.y) for v in e.vertices]
    except Exception:
        return None
    if len(pts) < 4:
        return None
    closed = getattr(e, "closed", False) or getattr(e, "is_closed", False) or \
        math.hypot(pts[0][0] - pts[-1][0], pts[0][1] - pts[-1][1]) < 1.0
    if not closed:
        return None
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
    return (x0, y0, x1, y1, (x0 + x1) / 2, (y0 + y1) / 2, e.dxf.handle, e.dxf.layer, pts)


def _inside(p, r) -> bool:
    return r[0] <= p[0] <= r[2] and r[1] <= p[1] <= r[3]


def _contains(a, b) -> bool:
    return a[0] <= b[0] + 1 and a[1] <= b[1] + 1 and a[2] >= b[2] - 1 and a[3] >= b[3] - 1


def _size(r):
    return r[2] - r[0], r[3] - r[1]


def _sides(r):
    """A footing's own two sides. A rectangle drawn turned (NIFT, 1.6 degrees) is bigger
    in its axis box than it is (2600 x 3200 measured 2690 x 3273), so a four-corner
    rectangle is measured along its sides; anything else (and every rectangle drawn
    square to the axes, where the two agree) by its box, as always."""
    pts = list(r[8])
    if len(pts) == 5 and math.hypot(pts[0][0] - pts[-1][0], pts[0][1] - pts[-1][1]) < 1.0:
        pts = pts[:4]
    if len(pts) != 4:
        return _size(r)
    e = [(pts[(i + 1) % 4][0] - pts[i][0], pts[(i + 1) % 4][1] - pts[i][1]) for i in range(4)]
    ln = [math.hypot(*v) for v in e]
    if min(ln) <= 0:
        return _size(r)
    square = all(abs(e[i][0] * e[(i + 1) % 4][0] + e[i][1] * e[(i + 1) % 4][1]) <= 0.01 * ln[i] * ln[(i + 1) % 4]
                 for i in range(4))
    if not square or abs(ln[0] - ln[2]) > 0.01 * ln[0] or abs(ln[1] - ln[3]) > 0.01 * ln[1]:
        return _size(r)
    a, c = (ln[0] + ln[2]) / 2, (ln[1] + ln[3]) / 2
    # keep the box's order: the side lying more along x first
    return (a, c) if abs(e[0][0]) >= abs(e[0][1]) else (c, a)


def read(b: Building, rec) -> dict[str, int]:
    if rec.doc is None or not (rec.verdict.kind in KINDS or TITLE_HINT.search(rec.title or "")):
        return {}
    hs = set(rec.view.handles)
    ents = [e for e in rec.doc.modelspace() if e.dxf.handle in hs]
    rects = [r for e in ents if not SKIP_LAYER.search(e.dxf.layer) and (r := _rect(e))]
    texts = [t for t in rec.texts if t.space == "model"]
    made = {"footings": 0, "rafts": 0, "pedestals": 0}

    # foundations drawn as loose lines on their own layers (in blocks too): joined into outlines
    loose = {}
    for e in ents:
        for x, top in _walk(e, e.dxf.handle):
            lay = x.dxf.layer or ""
            if x.dxftype() == "LINE" and FOOT_LAYER.search(lay) and not SKIP_LAYER.search(lay):
                loose.setdefault(lay, []).append(((x.dxf.start.x, x.dxf.start.y), (x.dxf.end.x, x.dxf.end.y), top))
    loops = [r for lay, ls in loose.items() if len(ls) <= MAX_LOOSE_LINES for r in _loops(ls, lay)]

    def colish(r):
        return not FOOT_LAYER.search(r[7]) and bool(COLUMNISH_LAYER.search(r[7]))

    def footing_mark_in(r):
        return next((lb.mark.key for t in texts if _inside((t.x, t.y), r) for lb in labels_in(t.text)
                     if lb.mark and lb.mark.family() == "footing" and lb.mark.number), "")

    # rafts drawn as loose lines: a large outline on a foundation layer (not the PCC bed's) with
    # a footing or raft mark inside - Akanistha "RF1", "RF2"
    loop_rafts = []
    for r in loops:
        w, h = _size(r)
        if max(w, h) <= MAX_FOOTING or PCC_LAYER.search(r[7]):
            continue
        label = next((t for t in texts for lb in labels_in(t.text)
                      if lb.mark and lb.mark.family() in ("footing", "raft") and _in_poly((t.x, t.y), r[8])), None)
        if label is None:
            continue
        inner = sum(1 for o in rects if _in_poly((o[4], o[5]), r[8]) and not colish(o)
                    and MIN_FOOTING <= min(_size(o)) and max(_size(o)) <= MAX_FOOTING)
        if inner >= 3:
            continue                        # holds several footings: a frame, not a raft
        loop_rafts.append((r, label))
    if loops:
        # the sheet draws its foundations as lines on a layer of their own: an outline on a column
        # or wall layer is that column or wall standing on them, not a footing (Akanistha: its 28
        # columns and shear walls were read as footings, the raft not at all) - unless it carries a
        # footing mark of its own that no raft has (NIFT's F2 is drawn on 'RCE-COL')
        raft_marks = {lb.mark.key for _r, lab in loop_rafts for lb in labels_in(lab.text) if lb.mark}
        rects = [r for r in rects if not colish(r) or footing_mark_in(r) not in ("", *raft_marks)]

    # rafts: large outlines with RAFT written inside, thickness from the notes
    raft_thk = None
    for t in texts:
        m = RAFT_THK.search(t.text)
        if m:
            raft_thk = (float(m.group(1) or m.group(2)), t)
    rafts = []
    for r in rects:
        w, h = _size(r)
        if max(w, h) <= MAX_FOOTING and "RAFT" not in r[7].upper():
            continue
        # a label, not a sentence: Villa 5's notes box ("NOTES: 1. GRADE ... CLEAR COVER
        # FOR RAFT = 50MM") is a rectangle with RAFT written inside, and no raft
        label = next((t for t in texts if re.search(r"\bRAFT\b", t.text, re.I) and len(t.text.split()) <= 4
                      and _inside((t.x, t.y), r)), None)
        if label is None and not ("RAFT" in r[7].upper() and min(w, h) > MAX_FOOTING / 2):
            continue
        # the frame round a schedule ("SCHEDULE OF RAFT SLAB", its remark "RAFT BOTTOM"),
        # J1153 B-block foundation: a table, not a raft
        inside_t = [t for t in texts if _inside((t.x, t.y), r)]
        if any(re.search(r"SCHEDULE|\bTABLE\b", t.text, re.I) for t in inside_t) or (
                len(inside_t) >= 10 and any(re.fullmatch(r"\s*(?:NOS?|REMARKS?|MARKS?)\s*\.?\s*", t.text, re.I)
                                            for t in inside_t)):
            continue
        # a frame or sheet border holding a RAFT label is not a raft: a raft
        # never encloses several separate footings
        inner = sum(1 for o in rects if o is not r and _contains(r, o)
                    and MIN_FOOTING <= min(_size(o)) and max(_size(o)) <= MAX_FOOTING)
        if inner >= 3:
            continue
        if any(_contains(o, r) and o is not r and "RAFT" in o[7].upper() for o in rafts):
            continue
        rafts.append(r)
        ev = Evidence(reader=READER, sheet=rec.id, source="raft outline", layer=r[7], handle=r[6],
                      at=[r[4], r[5]], text=label.text if label else r[7])
        props = {"length": Value(round(max(w, h)), READ, 0.7, [ev]),
                 "width": Value(round(min(w, h)), READ, 0.7, [ev])}
        if raft_thk:
            props["thickness"] = Value(raft_thk[0], READ, 0.9, [Evidence(
                reader=READER, sheet=rec.id, source="note", handle=raft_thk[1].handle,
                text=raft_thk[1].text[:80])])
        b.add(Element(id=f"{rec.id}/raft/{len(b.elements)}", type="raft", sheet=rec.id,
                      level="FDN", wing=rec.wing, mark=(re.sub(r"\s+", "", label.text.upper())[:12]
                                                         if label else ""),
                      geometry={"outline": [list(p) for p in r[8]]}, props=props))
        made["rafts"] += 1

    for r, label in loop_rafts:
        w, h = _size(r)
        rafts.append(r)
        mk = next((lb.mark.key for lb in labels_in(label.text) if lb.mark), label.text.strip().upper()[:12])
        ev = Evidence(reader=READER, sheet=rec.id, source="raft outline (drawn as lines)", layer=r[7], handle=r[6],
                      at=[r[4], r[5]], text=label.text)
        props = {"length": Value(round(max(w, h)), READ, 0.7, [ev]),
                 "width": Value(round(min(w, h)), READ, 0.7, [ev]),
                 "mark": Value(mk, READ, 0.9, [Evidence(reader=READER, sheet=rec.id, source="raft mark",
                                                         layer=label.layer, handle=label.handle,
                                                         at=[label.x, label.y], text=label.text)])}
        if raft_thk:
            props["thickness"] = Value(raft_thk[0], READ, 0.9, [Evidence(
                reader=READER, sheet=rec.id, source="note", handle=raft_thk[1].handle,
                text=raft_thk[1].text[:80])])
        b.add(Element(id=f"{rec.id}/raft/{len(b.elements)}", type="raft", sheet=rec.id,
                      level="FDN", wing=rec.wing, mark=mk,
                      geometry={"outline": [list(p) for p in r[8]]}, props=props))
        made["rafts"] += 1

    # footings: groups of nested outlines around one centre (outlines drawn as lines too)
    cand = [r for r in rects + [o for o in loops if max(_size(o)) <= MAX_FOOTING]
            if min(_size(r)) >= MIN_FOOTING and max(_size(r)) <= MAX_FOOTING
            and not any(_contains(f, r) for f in rafts)]
    cand.sort(key=lambda r: -(_size(r)[0] * _size(r)[1]))
    used = set()
    groups = []
    for r in cand:
        if r[6] in used:
            continue
        g = [r] + [o for o in cand if o[6] not in used and o is not r and _contains(r, o)
                   and math.hypot(o[4] - r[4], o[5] - r[5]) <= 0.25 * min(_size(r))]
        for o in g:
            used.add(o[6])
        groups.append(g)

    small = [r for r in rects if min(_size(r)) < MIN_FOOTING]
    for g in groups:
        g.sort(key=lambda r: -(_size(r)[0] * _size(r)[1]))
        pcc = None
        if len(g) > 1:
            (w0, h0), (w1, h1) = _size(g[0]), _size(g[1])
            ox, oy = (w0 - w1) / 2, (h0 - h1) / 2
            # a PCC bed is a thin, even ring round the footing; a PCC layer name
            # only allows a slightly wider ring, it never makes one
            ring = PCC_RING[1] + (150.0 if "PCC" in g[0][7].upper().replace(".", "") else 0.0)
            if PCC_RING[0] <= ox <= ring and abs(ox - oy) <= 30:
                pcc = (g[0], round((ox + oy) / 2))
                g = g[1:]
        elif "PCC" in g[0][7].upper().replace(".", ""):
            continue                     # a PCC bed whose footing is drawn elsewhere
        foot, steps = g[0], g[1:]
        fw, fh = _size(foot)
        # its mark: a footing mark inside or beside it; else the layer, if named like one
        mark, mark_ev, conf = "", None, 0.0
        reach = max(1000.0, 0.5 * math.hypot(fw, fh))
        near = []
        for t in texts:
            for lb in labels_in(t.text):
                if lb.mark and lb.mark.family() == "footing" and lb.mark.number:
                    d = 0.0 if _inside((t.x, t.y), foot) else \
                        math.hypot(t.x - foot[4], t.y - foot[5]) - 0.5 * math.hypot(fw, fh)
                    if d <= reach:
                        near.append((d, lb.mark, t))
        tag_key = ""
        if near:
            d, mk, t = min(near, key=lambda x: x[0])
            mark, conf = mk.key, 1.0 if d <= 0 else 0.8
            tag_key = mk.key
            mark_ev = Evidence(reader=READER, sheet=rec.id, source="footing mark", layer=t.layer,
                               handle=t.handle, at=[t.x, t.y], text=t.text)
        # a consultant drawing one layer per footing type (F7) says the mark too;
        # kept as a second, weaker source for the linker to weigh against the tag
        layer_mark = parse_mark(foot[7])
        layer_mark = layer_mark.key if layer_mark and layer_mark.family() == "footing" else ""
        if not mark and layer_mark:
            mark = layer_mark
        cols = sorted({lb.mark.key for t in texts if _inside((t.x, t.y), foot)
                       for lb in labels_in(t.text) if lb.mark and lb.mark.family() == "column"} |
                      {m.key for t in texts if _inside((t.x, t.y), foot)
                       for lb in labels_in(t.text) for m in lb.marks if m.family() == "column"} |
                      # a bare number on a column-number layer ("22" on 'colno', in its circle):
                      # that column, C22 - as the schedule writes it ("C - 22,23,24,25")
                      {mk.key for t in texts if _inside((t.x, t.y), foot) and COLNO_LAYER.match(t.layer or "")
                       and re.fullmatch(r"\d{1,3}[A-Z]?", t.text.strip().upper())
                       for mk in [parse_mark("C" + t.text.strip().upper())] if mk and mk.family() == "column"})
        if rec.verdict.kind not in KINDS and not cols and not (near and near[0][0] <= 0):
            # not a foundation plan (Villa 5's raft layout shares a region with its
            # details): only an outline carrying a footing or column mark is a footing
            continue
        sw, sh = _sides(foot)
        ev = Evidence(reader=READER, sheet=rec.id, source="footing outline", layer=foot[7],
                      handle=foot[6], at=[foot[4], foot[5]], text=f"{sw:.0f} x {sh:.0f}")
        props = {"length": Value(round(max(sw, sh)), READ, 0.8, [ev]),
                 "width": Value(round(min(sw, sh)), READ, 0.8, [ev])}
        if mark_ev:
            props["mark"] = Value(tag_key, READ, conf, [mark_ev])
        if layer_mark:
            props["mark_layer"] = Value(layer_mark, READ, 0.6, [Evidence(
                reader=READER, sheet=rec.id, source="layer name", layer=foot[7], text=foot[7])])
        if pcc:
            props["pcc_offset"] = Value(pcc[1], READ, 0.9, [Evidence(
                reader=READER, sheet=rec.id, source="PCC outline", layer=pcc[0][7], handle=pcc[0][6],
                text=f"{_size(pcc[0])[0]:.0f} x {_size(pcc[0])[1]:.0f}")])
        if steps:
            props["steps"] = Value([[round(min(_size(s))), round(max(_size(s)))] for s in steps], READ, 0.8,
                                   [Evidence(reader=READER, sheet=rec.id, source="step outline",
                                             layer=s[7], handle=s[6]) for s in steps[:3]])
        if cols:
            props["columns"] = Value(cols, READ, 0.9, [ev])
        # where its column is drawn on this sheet, by number (the column outline nearest the
        # number written inside the footing): lets a sheet drawn at another angle be lined
        # up with the plan by the columns both number (export._fit_by_columns)
        column_at = {}
        col_rects = [s for s in small if "COL" in (s[7] or "").upper() and "HATCH" not in (s[7] or "").upper()
                     and _inside((s[4], s[5]), foot)]
        for t in texts:
            if _inside((t.x, t.y), foot) and COLNO_LAYER.match(t.layer or "") \
                    and re.fullmatch(r"\d{1,3}[A-Z]?", t.text.strip().upper()) and col_rects:
                near = min(col_rects, key=lambda s: math.hypot(s[4] - t.x, s[5] - t.y))
                column_at["C" + t.text.strip().upper()] = [near[4], near[5]]
        extra = {"_layer": foot[7]}
        if column_at:
            extra["column_at"] = column_at
        b.add(Element(id=f"{rec.id}/footing/{len(b.elements)}", type="footing", sheet=rec.id,
                      level="FDN", wing=rec.wing, mark=mark,
                      geometry={"outline": [list(p) for p in foot[8]], "x": foot[4], "y": foot[5]},
                      props=props, extra=extra))
        made["footings"] += 1

    # pedestals: blocks or outlines marked PD / PED
    for e in ents:
        if e.dxftype() != "INSERT":
            continue
        mk = parse_mark(e.dxf.name)
        if not mk or family_of(mk.prefix) != "pedestal":
            continue
        try:
            from ezdxf import bbox
            bb = bbox.extents(e.virtual_entities(), fast=True)
            w, h = bb.size.x, bb.size.y
        except Exception:
            w = h = 0.0
        p = e.dxf.insert
        ev = Evidence(reader=READER, sheet=rec.id, source="pedestal block", layer=e.dxf.layer,
                      handle=e.dxf.handle, at=[p.x, p.y], text=e.dxf.name)
        props = {"mark": Value(mk.key, READ, 0.9, [ev])}
        if w and h:
            props["length"] = Value(round(max(w, h)), READ, 0.7, [ev])
            props["width"] = Value(round(min(w, h)), READ, 0.7, [ev])
        b.add(Element(id=f"{rec.id}/pedestal/{len(b.elements)}", type="pedestal", sheet=rec.id,
                      level="FDN", wing=rec.wing, mark=mk.key, geometry={"x": p.x, "y": p.y},
                      props=props))
        made["pedestals"] += 1
    return {k: v for k, v in made.items() if v}
