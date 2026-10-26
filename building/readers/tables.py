"""Schedule tables, read by their headers - whatever their layout.

Consultants draw tables in very different ways (all seen in the training sets):

* Reis Magos: a header four rows deep (SIZE over B and D; BOTTOM STRAIGHT over
  NO. and T), a mark split into three texts ("B" "-" "1,2,3,4,5"), and two
  tables side by side, each with its own title.
* Villa 5: the slab marks on one line and their values on the line below.
* Sasan Gir: two columns only, BEAM NO. and BEAM SIZE.

So a table is found by its header words, never by layer or position:

1. Header texts are texts made only of schedule vocabulary (BEAM, NO., SIZE, B,
   D, THK, TOP, BOTTOM, STIRRUPS, REMARK...). A cluster of them, with at least
   one naming the mark column and one naming a value, is a table header.
2. The header's lowest texts are the columns; the texts above a column give its
   context ("SIZE" over "B" makes the B column a width).
3. Below the header, every text in the mark column starts a record, and every
   other text belongs to the record whose mark is nearest above it - which is
   what makes Villa's two-line rows and Reis Magos's extra bar lines work.
4. Two tables side by side are split at the midpoint between their titles.
5. Where the table is ruled, its horizontal lines are its cells: a value belongs
   to the mark line in its own cell, never to a row across a line. A cell holding
   several mark lines and its size printed once (Stilt A wing: "B - 10,11,12,13"
   ... "B - 38,39" beside one "230 450") gives that size to every mark line in it.
6. In the mark column a mark needs no number (ST, SB, C): the column says it is
   a mark. Plans keep requiring one - there a lone letter is a grid label.

Every value keeps the cell text and handle it came from. Nothing is guessed:
a cell that does not parse is kept as text, a column that maps to nothing is
kept under its header's words.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field

from ..marks import bare_mark, grouped, labels_in, parse_mark
from ..model import Building, Element, Evidence, Value, READ
from ..sheets.levels import levels_in, wing_in

READER = "tables"

VOCAB = {
    "BEAM", "BEAMS", "COLUMN", "COLUMNS", "SLAB", "SLABS", "FOOTING", "FOOTINGS", "MEMBER",
    "NO", "NOS", "NUMBER", "NUMBERS", "MARK", "MARKS", "ID", "NAME", "TYPE", "REF",
    "SIZE", "SECTION", "B", "D", "W", "H", "L", "WIDTH", "DEPTH", "BREADTH", "LENGTH",
    "THK", "THICK", "THICKNESS", "OVERALL",
    "STEEL", "REINFORCEMENT", "REINF", "MAIN", "BOTTOM", "BOT", "TOP", "STRAIGHT", "CURT",
    "CURTAIL", "CURTAILED", "BENT", "CRANK", "CRANKED", "EXTRA", "STIRRUPS", "STIRRUP",
    "LINKS", "LINK", "TIES", "SPACING", "END", "ENDS", "MID", "SUPPORT", "SUPPORTS", "SPAN",
    "OTHER", "DISTRIBUTION", "DIA", "T", "Y", "LEGS", "REMARK", "REMARKS", "RE", "NOTE",
    "LEVEL", "LVL", "AT", "OF", "AND", "@", "Ø", "MM", "(MM)", "IN", "TO", "FROM", "GRADE",
    # footing schedules
    "DIMENSION", "DIMENSIONS", "D1", "D2", "D3", "MKD", "MARKED", "PCC", "P", "C", "STEP",
    "CONCRETE", "MIX", "SHORTER", "LONGER", "SIDE", "EDGE", "CENTRE", "CENTER",
    # NIFT: "FTG. MK'D | DIMENSIONS LX LY T1"
    "FTG", "LX", "LY", "T1",
}
MARK_HEAD = re.compile(r"\b(?:NO|NOS|NUMBERS?|MARKS?|MKD|MARKED|ID|NAME|MEMBER|REF)\b")
FOOTING_TABLE = re.compile(r"FOOTING|FDN|FOUNDATION|PILE\s*CAP", re.I)
VALUE_HEAD = re.compile(r"\b(?:SIZE|SECTION|B|D|W|WIDTH|DEPTH|BREADTH|THK|THICK|THICKNESS|"
                        r"TOP|BOTTOM|BOT|STIRRUPS?|LINKS?|MAIN|DISTRIBUTION|TYPE)\b")
TITLE = re.compile(r"SCHEDULE|\bTABLE\b", re.I)
# Header words of one table can sit 14 text heights apart (Reis Magos: BEAM NOS
# to SIZE); tables side by side are kept apart by their titles, not by distance.
REACH = 16


def _norm(s: str) -> str:
    """'S I Z E' -> 'SIZE', 'RE MARK.' -> 'REMARK', punctuation out, upper case."""
    s = s.upper().replace("RE MARK", "REMARK")
    s = re.sub(r"\bMK\s*'\s*D\b", "MKD", s)          # "MK'D" (marked), NIFT footing schedule
    if re.fullmatch(r"(?:[A-Z]\s)+[A-Z]\.?", s.strip()):
        s = s.replace(" ", "")
    return re.sub(r"\s+", " ", re.sub(r"[^\w@Ø() ]", " ", s)).strip()


def _is_header_word(t) -> bool:
    words = _norm(t.text).split()
    return bool(words) and all(w in VOCAB for w in words) and len(t.text) <= 30


@dataclass
class Column:
    texts: list
    cx: float
    label: str                    # parents + own words, e.g. "SIZE B"
    field: str = ""               # what it maps to


@dataclass
class Table:
    title: str
    header: list
    columns: list[Column]
    x0: float
    x1: float
    top: float
    h: float                      # text height
    records: list[dict] = field(default_factory=list)


def _field(label: str) -> str:
    L = label
    if "REMARK" in L or L.startswith("NOTE"):
        return "remark"
    if MARK_HEAD.search(L) and not re.search(r"\b(?:T|DIA|SIZE|THK|LEGS)\b", L) and \
            not re.search(r"\b(?:BOTTOM|TOP|STIRRUPS?|EXTRA|LINKS?|STEEL|REINF|REINFORCEMENT)\b", L):
        # (the "NO." under STEEL is the number of bars, not a column of marks - J1153 column schedule)
        return "mark"
    if re.search(r"\bTHK\b|THICK", L):
        return "thickness"
    # a footing's plan sides and thickness: "DIMENSIONS LX LY T1" (NIFT)
    if re.search(r"(?:^|\s)LX$", L) and not re.search(r"BOTTOM|TOP|PARALLEL", L):
        return "width"
    if re.search(r"(?:^|\s)LY$", L) and not re.search(r"BOTTOM|TOP|PARALLEL", L):
        return "length"
    if re.fullmatch(r"(?:DIMENSIONS?\s+)?T1", L):
        return "depth"
    if re.search(r"(?:^|\s)(?:L|LENGTH)$", L) and not re.search(r"BOTTOM|TOP|ALONG", L):
        return "length"
    if re.search(r"(?:^|\s)D1$", L):
        return "depth"                       # a sloped footing: D1 at the centre ...
    if re.search(r"(?:^|\s)D2$", L):
        return "depth_edge"                  # ... D2 at the edge
    if re.search(r"(?:^|\s)(?:B|W|WIDTH|BREADTH)$", L) and not re.search(r"BOTTOM|TOP|STIRR", L):
        return "width"
    if re.search(r"(?:^|\s)(?:D|H|DEPTH)$", L) and not re.search(r"BOTTOM|TOP|STIRR", L):
        return "depth"
    if re.search(r"\bSIZE\b|\bSECTION\b", L):
        return "size"
    if re.search(r"\bTYPE\b", L):
        return "type"
    if re.search(r"\bLEVEL\b|\bLVL\b|FOUNDING", L):
        return "level"
    # steel groups keep their header words: "STEEL BOTTOM STRAIGHT NO" etc.
    return "steel:" + L


def _find_tables(texts) -> list[Table]:
    heads = [t for t in texts if _is_header_word(t)]
    seeds = [t for t in heads if len(_norm(t.text)) > 1]          # not lone B, D, T
    titles = [t for t in texts if TITLE.search(t.text)]
    clusters: list[list] = []
    for t in sorted(seeds, key=lambda t: (-t.cy, t.cx)):
        h = max(t.height, 1.0)
        for c in clusters:
            if any(abs(t.cy - o.cy) <= 4 * h and abs(t.cx - o.cx) <= REACH * h for o in c):
                c.append(t)
                break
        else:
            clusters.append([t])
    # merge clusters that chain together
    merged = True
    while merged:
        merged = False
        for i in range(len(clusters)):
            for j in range(i + 1, len(clusters)):
                h = max(o.height for o in clusters[i] + clusters[j])
                if any(abs(a.cy - b.cy) <= 4 * h and abs(a.cx - b.cx) <= REACH * h
                       for a in clusters[i] for b in clusters[j]):
                    clusters[i] += clusters.pop(j)
                    merged = True
                    break
            if merged:
                break

    tables = []
    for c in clusters:
        h = sorted(t.height for t in c)[len(c) // 2]
        top = max(t.cy for t in c) + h
        # Tables side by side (Reis Magos) have headers close enough to chain into
        # one cluster; each has its own title above, so the titles split it.
        x0c, x1c = min(t.cx for t in c) - 4 * h, max(t.cx for t in c) + 6 * h
        above = sorted((t for t in titles if top - 2 * h <= t.cy <= top + 6 * h
                        and x0c - REACH * h <= t.cx <= x1c + REACH * h), key=lambda t: t.cx)
        # cut each pair of neighbouring tables in the widest empty gap between
        # their header words - whether the titles are left-aligned or centred
        cuts = []
        xs = sorted(t.cx for t in c)
        for a, b in zip(above, above[1:]):
            inside = [x for x in xs if a.cx <= x <= b.cx]
            gaps = [(q - p, (p + q) / 2) for p, q in zip(inside, inside[1:])]
            cuts.append(max(gaps)[1] if gaps else (a.cx + b.cx) / 2)
        parts = defaultdict(list)
        for t in c:
            parts[sum(1 for x in cuts if t.cx >= x)].append(t)
        for i, part in sorted(parts.items()):
            words = " ".join(_norm(t.text) for t in part)
            if not MARK_HEAD.search(words) or not VALUE_HEAD.search(words) or len(part) < 2:
                continue
            ptop = max(t.cy for t in part) + h
            bottom = min(t.cy for t in part) - 1.2 * h
            x0 = min(t.cx for t in part) - 4 * h
            x1 = max(t.cx for t in part) + 6 * h
            if i > 0:
                x0 = max(x0, cuts[i - 1])
            if i < len(cuts):
                x1 = min(x1, cuts[i])
            title = above[i].text if i < len(above) else ""
            band = [t for t in heads if x0 <= t.cx <= x1 and bottom <= t.cy <= ptop]
            # header words lie above the first data row (a line with three or more numbers):
            # a word on or below it is a cell - the "C" of "C - 2,3,4,5  300 X 950", the "SLAB"
            # of a remark "RAFT BOTTOM SLAB" (J1153 B-block foundation schedules)
            lines = defaultdict(int)
            for t in texts:
                if x0 <= t.cx <= x1 and bottom - 2 * h <= t.cy <= ptop and re.fullmatch(r"\d+(?:\.\d+)?", t.text.strip()):
                    lines[round(t.cy / (0.6 * h))] += 1
            data = [k * 0.6 * h for k, n in lines.items() if n >= 3]
            if data:
                first = max(data)
                kept = [t for t in band if t.cy > first + 0.4 * h]
                if len(kept) >= 2 and kept != band:
                    band = kept
                    bottom = min(t.cy for t in band) - 1.2 * h
            tables.append(Table(title, band, _columns(band, h), x0, x1, bottom, h))
    return tables


def _columns(band, h) -> list[Column]:
    """The lowest header texts are columns; those above give them context."""
    def span(t):
        w = 0.8 * t.height * len(t.text)
        return t.cx - w / 2 - h, t.cx + w / 2 + h

    leaves = []
    for t in band:
        x0, x1 = span(t)
        below = [o for o in band if o is not t and o.cy < t.cy - 0.5 * h and x0 <= o.cx <= x1]
        if not below:
            leaves.append(t)
    cols: list[Column] = []
    for t in sorted(leaves, key=lambda t: t.cx):
        parents = sorted((o for o in band if o.cy > t.cy + 0.5 * h and span(o)[0] <= t.cx <= span(o)[1]),
                         key=lambda o: -o.cy)
        label = " ".join([_norm(p.text) for p in parents] + [_norm(t.text)])
        if cols and abs(cols[-1].cx - t.cx) < 1.2 * h:            # one column, two words
            cols[-1].texts.append(t)
            cols[-1].label = label if len(label) > len(cols[-1].label) else cols[-1].label
            continue
        cols.append(Column([t], t.cx, label))
    for c in cols:
        c.field = _field(c.label)
    # a mark column needs a home: in tables whose first column is unnamed
    # ("SLAB" over "NUMBERS" is named), the leftmost column is the mark
    if not any(c.field == "mark" for c in cols) and cols:
        cols[0].field = "mark"
    return cols


_PREFIX = re.compile(r"^[A-Z]{1,3}\.?$")
# a list of marks ("PB-1,2,3,16," / "1,2,3,16,") and a line of bare numbers carrying one on
_MARK_LIST = re.compile(r"^(?:[A-Z]{1,4}\.?-?)?\d+[A-Z]?(?:,\d+[A-Z]?)*,?$")
_MORE_MARKS = re.compile(r"^\d+[A-Z]?(?:,\d+[A-Z]?)*,?$")


def _records(table: Table, texts, rules=()) -> None:
    h = table.h
    # the table ends where the next table's title starts (a schedule of columns under
    # the footing schedule is not more footing rows)
    below = [t.cy for t in texts if TITLE.search(t.text) and t not in table.header
             and table.x0 - 4 * h <= t.cx <= table.x1 and t.cy < table.top - 0.2 * h]
    stop = max(below) + 0.5 * h if below else None
    mark_first = bool(table.columns) and min(table.columns, key=lambda c: c.cx).field == "mark"
    # a tightly drawn table: its first row only a text height under the header (J1153 column
    # schedule, "C - 2,3,4,5  300 X 950" 340 mm under "NO."). A line of three or more numbers
    # just under the header is that first row, not header.
    limit = table.top - 0.2 * h
    near = defaultdict(int)
    for t in texts:
        if table.x0 <= t.cx <= table.x1 and limit <= t.cy < table.top + 0.6 * h and t not in table.header \
                and re.fullmatch(r"\d+(?:\.\d+)?", t.text.strip()):
            near[round(t.cy / (0.6 * h))] += 1
    tight = [k * 0.6 * h for k, n in near.items() if n >= 3]
    if tight:
        limit = max(tight) + 0.5 * h
    body = [t for t in texts if t.cy < limit and (stop is None or t.cy > stop)
            and t not in table.header and not TITLE.search(t.text)
            and (table.x0 <= t.cx <= table.x1
                 # a mark's letters written just left of the header ("C" "-" "2", J1153 B-block
                 # foundation): taken with the mark, only when the first column is the marks'
                 or (mark_first and table.x0 - 2 * h <= t.cx < table.x0 and _PREFIX.match(t.text.strip().upper())))]
    if not body or not table.columns:
        return
    def col_of(t):
        c = min(table.columns, key=lambda c: abs(c.cx - t.cx))
        if c.field != "mark" and "," in t.text and _MARK_LIST.match(t.text.replace(" ", "").upper()):
            # a long list of marks written from the mark column runs on over the next
            # columns (ALCAZAR "1,2,3,4,5,6,7,10,12,16,"): it belongs where it starts
            first = min(table.columns, key=lambda c: abs(c.cx - (t.cx - 0.4 * t.height * len(t.text))))
            if first.field == "mark":
                return first
        return c
    mark_cols = [c for c in table.columns if c.field == "mark"]
    if not mark_cols:
        return
    # mark cells: on the same line, joined left to right ("B" "-" "1,2,3,4,5")
    lines = defaultdict(list)
    for t in body:
        if col_of(t).field == "mark":
            lines[round(t.cy / (0.6 * h))].append(t)
    in_mark_col = [t for key in lines for t in lines[key]]
    starts = []
    for key in sorted(lines, reverse=True):                     # top to bottom
        cells = sorted(lines[key], key=lambda t: t.cx)
        text = "".join(c.text for c in cells).replace(" ", "")
        marks = grouped(text) or ([m] if (m := parse_mark(text)) else []) or \
            [lb.mark for lb in labels_in(text) if lb.mark]
        if any(m.floor for m in marks) and not re.match(r"\d{1,2}\s?[A-Za-z]", cells[0].text.strip()):
            # a floor number counts only written with the mark (4B3): a number in a text
            # of its own over the cell is not one (Sasan Gir member schedule: a "14"
            # marker over SC1's cell)
            marks = [m for m in marks if not m.floor]
        if not marks and len(cells) == 1 and (bm := bare_mark(text)) and not any(
                o is not cells[0] and abs(o.cy - cells[0].cy) <= 1.2 * h and re.match(r"[-\d]", o.text.strip())
                for o in in_mark_col):
            # a mark with no number (ST, SB, C) - but not the letters of a split mark
            # whose "-" "2" sit a little off its line
            marks = [bm]
        if marks:
            starts.append((max(c.cy for c in cells), cells, marks, text))
            continue
        # bare numbers just under a list ending in a comma carry that list on, with its
        # prefix (ALCAZAR "PB-1,2,...,16," then "17,20,30,..." then "77,82,83,100,101,104")
        if starts and _MORE_MARKS.match(text.upper()) and starts[-1][3].endswith(",") \
                and min(c.cy for c in starts[-1][1]) - max(c.cy for c in cells) <= 2.5 * h:
            y0, cells0, marks0, text0 = starts[-1]
            prefix = re.match(r"[A-Z]+", marks0[0].key)
            more = text.rstrip(",")
            more = (grouped(f"{prefix.group(0)}-{more}") or
                    ([m] if (m := parse_mark(f"{prefix.group(0)}-{more}")) else [])) if prefix else []
            if more:
                starts[-1] = (y0, cells0 + cells, marks0 + more, f"{text0} {text}")
    if not starts:
        return
    starts.sort(key=lambda s: -s[0])
    # where the table ends: a gap of more than 5 lines below the last record
    last_y = starts[-1][0]
    body = [t for t in body if t.cy >= last_y - 5 * h]
    recs = [{"y": y, "cells": defaultdict(list), "marks": mk, "mark_text": txt,
             "evidence": list(cells)} for y, cells, mk, txt in starts]
    read_marks = {id(c) for _, cells, _, _ in starts for c in cells}
    unread = [t for t in body if col_of(t).field == "mark" and id(t) not in read_marks]
    cell_of = _cells_by_rules(table, rules, last_y)
    for t in body:
        c = col_of(t)
        if c.field == "mark":
            continue
        # the record whose mark is nearest above (or on the same line)
        above = [r for r in recs if r["y"] >= t.cy - 0.6 * h]
        r = min(above, key=lambda r: r["y"] - t.cy) if above else None
        if r is not None and r["y"] - t.cy > 6 * h:
            r = None
        cell = cell_of(t.cx, t.cy)
        if cell:
            # a ruled table: the value belongs to a mark line in its own cell
            inside = [q for q in recs if cell[0] < q["y"] < cell[1]]
            if inside and r not in inside:
                r = min(inside, key=lambda q: abs(q["y"] - t.cy))
            elif not inside and any(cell[0] < o.cy < cell[1] for o in unread):
                r = None              # its row's own label is not a mark: never into the row above
        if r is None:
            continue
        r["cells"][c.field].append(t)
        r["evidence"].append(t)
    _merged_cells(table, recs, body, col_of, cell_of)
    table.records = recs


SHARED_IN_CELL = ("width", "depth", "thickness", "size", "length", "depth_edge")


def _cells_by_rules(table: Table, rules, last_y):
    """(below, above) - the table's horizontal lines round a point, or None.

    Only lines inside the table, and only those running through the point's x:
    a line drawn across the mark column alone does not divide the size column."""
    h = table.h
    mine = [r for r in rules if r[1] <= table.x1 and r[2] >= table.x0 - 4 * h
            and last_y - 8 * h <= r[0] <= table.top + h]

    def cell(x, y):
        ys = [r[0] for r in mine if r[1] - 0.5 * h <= x <= r[2] + 0.5 * h]
        lo = [v for v in ys if v < y - 0.05 * h]
        hi = [v for v in ys if v > y + 0.05 * h]
        return (max(lo), min(hi)) if lo and hi else None
    return cell


def _merged_cells(table: Table, recs, body, col_of, cell_of) -> None:
    """One size printed once for several mark lines that share its ruled cell.

    A mark line with no value of its own in a size column takes the value of its
    cell in that column when the cell holds two or more mark lines (not the whole
    table) and exactly one line of values there. Otherwise nothing is taken."""
    h = table.h
    for c in table.columns:
        if c.field not in SHARED_IN_CELL:
            continue
        for q in recs:
            if q["cells"].get(c.field):
                continue
            cell = cell_of(c.cx, q["y"])
            if not cell:
                continue
            inside = [o for o in recs if cell[0] < o["y"] < cell[1]]
            if len(inside) < 2 or len(inside) == len(recs):
                continue
            vals = [t for t in body if col_of(t) is c and cell[0] < t.cy < cell[1]]
            if not vals or max(t.cy for t in vals) - min(t.cy for t in vals) > 0.6 * h:
                continue
            q["cells"][c.field] = list(vals)
            q["evidence"] += vals
            q.setdefault("merged", []).append(c.field)


def _rules(rec) -> list[tuple[float, float, float]]:
    """The view's straight horizontal lines, any layer: (y, x from, x to)."""
    doc, view = getattr(rec, "doc", None), getattr(rec, "view", None)
    db = getattr(doc, "entitydb", None)
    if db is None or view is None:
        return []
    out = []
    for hd in getattr(view, "handles", ()) or ():
        e = db.get(hd)
        if e is None or e.dxftype() not in ("LINE", "LWPOLYLINE"):
            continue
        try:
            if e.dxftype() == "LINE":
                segs = [((e.dxf.start.x, e.dxf.start.y), (e.dxf.end.x, e.dxf.end.y))]
            else:
                pts = [(p[0], p[1]) for p in e.get_points("xy")]
                if e.closed and pts:
                    pts.append(pts[0])
                segs = list(zip(pts, pts[1:]))
        except Exception:
            continue
        for (x0, y0), (x1, y1) in segs:
            if abs(y0 - y1) < 1.0 and abs(x1 - x0) > 1.0:
                out.append((float(y0), float(min(x0, x1)), float(max(x0, x1))))
    return out


_NUM = re.compile(r"^\s*(\d{2,4}(?:\.\d+)?)\s*(?:MM)?\s*$", re.I)
_SIZE = re.compile(r"(\d{2,4})\s*[xX×*]\s*(\d{2,4})(?:\s*MM)?\.?", re.I)
_SUNK = re.compile(r"(\d{2,4})\s*MM\s*SUNK", re.I)


def _num(cells) -> float | None:
    for t in cells:
        m = _NUM.match(t.text)
        if m:
            return float(m.group(1))
    return None


# a founding level written in a footing schedule: -1.800, (-)1500, -1.5 M. The sign is
# required - a bare 1500 could be a depth below ground or a level, and is not guessed
_FOUND = re.compile(r"^\s*(?:\(\s*([+\-])\s*\)|([+\-]))\s*(\d{1,5}(?:\.\d{1,3})?)\s*(MM|M)?\.?\s*$", re.I)


def founding_level(cells) -> float | None:
    """The founding level (mm, signed, against the drawings' datum) in these cells."""
    for t in cells:
        m = _FOUND.match(t.text)
        if not m:
            continue
        sign = -1.0 if (m.group(1) or m.group(2)) == "-" else 1.0
        num, unit = float(m.group(3)), (m.group(4) or "").upper()
        mm = num if unit == "MM" or (not unit and "." not in m.group(3)) else num * 1000.0
        if mm <= 50000.0:
            return sign * mm
    return None


_VARIES = re.compile(r"^\s*(\d{2,4})\s*/\s*(\d{2,4})\s*$")


def _varying(cells) -> tuple[float, float] | None:
    """A size written as two values, "850/750", often split over two cells
    ("850/" and "750"): a beam whose depth changes along its length. Neither
    number alone is the beam's depth, so both are kept."""
    joined = "".join(t.text for t in sorted(cells, key=lambda t: (-t.cy, t.cx))).replace(" ", "")
    m = _VARIES.match(joined)
    return (float(m.group(1)), float(m.group(2))) if m else None


def read(b: Building, rec) -> dict[str, int]:
    """Every schedule table in one registered view, as specs per mark."""
    texts = [t for t in rec.texts if t.space == "model" and t.cx is not None]
    made = {"tables": 0, "table specs": 0}
    found = _find_tables(texts)
    rules = _rules(rec) if found else []
    for table in found:
        _records(table, texts, rules)
        if not table.records:
            continue
        made["tables"] += 1
        for r in table.records:
            cells = r["cells"]
            props: dict[str, Value] = {}

            def put(name, value, src):
                if value is None or value == "":
                    return
                ev = [Evidence(reader=READER, sheet=rec.id, source=f"table: {table.title[:40]}",
                               layer=t.layer, handle=t.handle, at=[t.x, t.y], text=t.text[:80])
                      for t in src[:4]]
                props[name] = Value(value, READ, 1.0, ev)

            for name in ("width", "depth", "thickness", "length", "depth_edge"):
                cs = cells.get(name, [])
                two = _varying(cs)
                if two:
                    # kept as written, flagged for review; the larger is the
                    # working value so nothing is built undersized meanwhile
                    put(f"{name}_varies", list(two), cs)
                    put(name, max(two), cs)
                    props[name].status = "conflict"
                    props[name].confidence = 0.5
                else:
                    put(name, _num(cs), cs)
            for t in cells.get("size", []):
                m = _SIZE.search(t.text)
                if m and "width" not in props:
                    put("width", float(m.group(1)), [t])
                    put("depth", float(m.group(2)), [t])
                put("size_text", t.text, [t])
            if cells.get("size"):
                # a size written in pieces, "230" "X" "1050" (Stilt A column schedule): read
                # as one line; "REFER" "DETAIL" is no size and is kept as written
                by_line = defaultdict(list)
                for t in cells["size"]:
                    by_line[round(t.cy / (0.6 * table.h))].append(t)
                parts = [sorted(v, key=lambda t: t.cx) for _, v in sorted(by_line.items(), reverse=True)]
                if len(cells["size"]) > 1:
                    put("size_text", " ".join(t.text.strip() for p in parts for t in p), cells["size"])
                # two different sizes in one cell (B BLOCK C-10: "300 X 1600" and "300 X 950")
                # are not one size: neither is taken
                sizes = [(m, p) for p in parts if len(p) > 1
                         and (m := _SIZE.fullmatch(" ".join(t.text.strip() for t in p)))]
                if sizes and "width" not in props and len({m.groups()[:2] for m, _ in sizes}) == 1:
                    m, p = sizes[0]
                    put("width", float(m.group(1)), p)
                    put("depth", float(m.group(2)), p)
            put("type", " ".join(t.text for t in cells.get("type", [])), cells.get("type", []))
            remark = " ".join(t.text for t in cells.get("remark", []) if t.text.strip("-"))
            put("remark", remark, cells.get("remark", []))
            m = _SUNK.search(remark)
            if m:
                put("sunk_mm", float(m.group(1)), cells.get("remark", []))
            steel = {k[6:]: " ".join(t.text for t in sorted(v, key=lambda t: (-t.cy, t.cx))
                                     if t.text.strip("-"))
                     for k, v in cells.items() if k.startswith("steel:")}
            steel = {k: v for k, v in steel.items() if v}
            if steel:
                put("steel", steel, [t for k, v in cells.items() if k.startswith("steel:") for t in v])
            footing_table = bool(FOOTING_TABLE.search(table.title or "") or
                                 any(FOOTING_TABLE.search(c.label) for c in table.columns))
            if footing_table:
                put("founding_level", founding_level(cells.get("level", [])), cells.get("level", []))
            for mk in r["marks"]:
                fam_ev = [Evidence(reader=READER, sheet=rec.id, source=f"table: {table.title[:40]}",
                                   layer=t.layer, handle=t.handle, at=[t.x, t.y], text=r["mark_text"])
                          for t in r["evidence"][:1]]
                p = dict(props)
                fam = mk.family()
                if footing_table and fam != "footing":
                    # Villa 5: a footing schedule listed by the column each footing carries
                    p["by_column"] = Value(mk.key, READ, 1.0, fam_ev)
                    fam = "footing"
                p["family"] = Value(fam, READ, 1.0, fam_ev)
                if len(r["marks"]) > 1:
                    p["shared_with"] = Value([m.key for m in r["marks"]], READ, 1.0, fam_ev)
                # a table's own title wins over its sheet's: the Reis Magos B-wing
                # sheet also carries "SCHEDULE OF FLOOR BEAMS WING C REVISED"
                t_wing, t_levels = wing_in(table.title), levels_in(table.title)
                lv = t_levels[0] if len(t_levels) == 1 else \
                    (rec.levels[0] if len(rec.levels) == 1 else "")
                b.add(Element(
                    id=f"{rec.id}/spec/{len(b.elements)}", type="spec", sheet=rec.id,
                    level=lv, wing=t_wing or rec.wing, mark=mk.key, props=p,
                    extra={"table": table.title, "columns": [c.label for c in table.columns],
                           "part": "club house" if "CLUB" in table.title.upper() else ""}))
                made["table specs"] += 1
    for got in _transposed(b, rec, texts):
        made["tables"] += 1
        made["table specs"] += got
    return {k: v for k, v in made.items() if v}


# ---------------------------------------------------------------- transposed tables
ROW_HEAD = re.compile(r"^(?:\w+\s+)?(?:MKD|MARKED|MARKS?|NO|NOS|TYPE|ID)\.?$", re.I)
_SIZE_CELL = re.compile(r"^\s*(\d{3,5})\s*[xX×]\s*(\d{3,5})\s*$")


def _row_field(label: str, seen: set) -> str:
    L = _norm(label)
    if "PCC" in L.replace(" ", "") or "P C C" in L:
        return "pcc_size"
    if re.search(r"\bSIZE\b", L):
        return "size"
    if re.match(r"^D\b|^DEPTH\b|^OVERALL\s+DEPTH", L) and "D1" not in L:
        return "depth"
    if re.search(r"\bD1\b", L):
        return "step_depths"
    if re.search(r"STEEL|REINF|BAR", L):
        return "steel:" + L
    if re.search(r"GRADE|MIX|CONCRETE", L):
        return "grade"
    if re.search(r"\bLEVEL\b|\bLVL\b|FOUNDING", L):
        return "level"
    return ""


def _transposed(b: Building, rec, texts) -> list[int]:
    """Tables with the marks across a row and the properties down the side.

    Sasan Gir's footing schedule: "FOOTING MKD. | F1 | F2 | ... | F7", then rows
    "P.C.C. SIZE (B' X L')", "FOOTING SIZE  STEP-1" (with a second line for
    STEP-2), "D (STEP-1+STEP2)", steel rows. Each row's label is the text left of
    the first mark; its cells sit under the marks. A row with no label of its
    own continues the one above (the STEP-2 sizes under FOOTING SIZE).
    """
    out = []
    used: set = set()
    for head in texts:
        if id(head) in used or not ROW_HEAD.match(_norm(head.text)):
            continue
        h = max(head.height, 1.0)
        row = sorted((t for t in texts if t is not head and abs(t.cy - head.cy) <= 0.6 * h
                      and t.cx > head.cx), key=lambda t: t.cx)
        marks = [(t, parse_mark(t.text)) for t in row]
        marks = [(t, m) for t, m in marks if m]
        if len(marks) < 3 or len(marks) < 0.7 * len(row):
            continue
        used.add(id(head))
        cols = [t.cx for t, _ in marks]
        pitch = min(b2 - a2 for a2, b2 in zip(cols, cols[1:])) if len(cols) > 1 else 10 * h
        left = cols[0] - 0.6 * pitch                    # labels sit left of this
        right = cols[-1] + 0.6 * pitch
        title = next((t.text for t in sorted(texts, key=lambda t: t.cy)
                      if TITLE.search(t.text) and 0 < t.cy - head.cy <= 12 * h
                      and abs(t.x - head.x) <= 20 * h), "")
        # the rows below the mark row, top to bottom, until a long gap
        below = sorted((t for t in texts if t.cy < head.cy - 0.4 * h and head.x - 2 * h <= t.cx <= right),
                       key=lambda t: -t.cy)
        lines, last = [], head.cy
        for t in below:
            if last - t.cy > 5 * h:
                break
            if lines and abs(lines[-1][0] - t.cy) <= 0.6 * h:
                lines[-1][1].append(t)
            else:
                lines.append([t.cy, [t]])
            last = t.cy
        cells: dict[int, dict[str, list]] = {i: {} for i in range(len(marks))}
        field_now, sizes_in_block = "", 0
        for cy, items in lines:
            label = " ".join(t.text for t in sorted(items, key=lambda t: t.cx) if t.cx < left)
            values = [t for t in items if t.cx >= left]
            f = _row_field(label, set()) if label else ""
            if f:
                field_now, sizes_in_block = f, 0
            elif not field_now:
                continue
            if not values:
                continue
            target = field_now
            if field_now == "size":
                sizes_in_block += 1
                if sizes_in_block > 1:
                    target = "step2_size"
            for t in values:
                i = min(range(len(cols)), key=lambda k: abs(cols[k] - t.cx))
                if abs(cols[i] - t.cx) <= 0.6 * pitch:
                    cells[i].setdefault(target, []).append(t)
        n = 0
        for i, (mt, mk) in enumerate(marks):
            c = cells[i]
            props: dict[str, Value] = {}

            def put(name, value, src, conf=1.0):
                if value in (None, "", []):
                    return
                props[name] = Value(value, READ, conf, [
                    Evidence(reader=READER, sheet=rec.id, source=f"table: {title[:40]}", layer=t.layer,
                             handle=t.handle, at=[t.x, t.y], text=t.text[:60]) for t in src[:3]])

            for t in c.get("size", [])[:1]:
                m = _SIZE_CELL.match(t.text)
                if m:
                    a, bb = sorted((float(m.group(1)), float(m.group(2))))
                    put("width", a, [t])
                    put("length", bb, [t])
            for t in c.get("step2_size", [])[:1]:
                m = _SIZE_CELL.match(t.text)
                if m:
                    put("steps", [sorted((float(m.group(1)), float(m.group(2))))], [t])
            for t in c.get("pcc_size", [])[:1]:
                m = _SIZE_CELL.match(t.text)
                if m:
                    put("pcc_size", sorted((float(m.group(1)), float(m.group(2)))), [t])
            put("depth", _num(c.get("depth", [])), c.get("depth", []))
            put("step_depths", " ".join(t.text for t in c.get("step_depths", [])), c.get("step_depths", []))
            if FOOTING_TABLE.search(title or "") or mk.family() == "footing":
                put("founding_level", founding_level(c.get("level", [])), c.get("level", []))
            steel = {k[6:]: " ".join(t.text for t in v if t.text.strip("-")) for k, v in c.items()
                     if k.startswith("steel:")}
            put("steel", {k: v for k, v in steel.items() if v},
                [t for k, v in c.items() if k.startswith("steel:") for t in v])
            fam_ev = [Evidence(reader=READER, sheet=rec.id, source=f"table: {title[:40]}", layer=mt.layer,
                               handle=mt.handle, at=[mt.x, mt.y], text=mt.text)]
            props["family"] = Value(mk.family(), READ, 1.0, fam_ev)
            t_wing, t_levels = wing_in(title), levels_in(title)
            b.add(Element(id=f"{rec.id}/spec/{len(b.elements)}", type="spec", sheet=rec.id,
                          level=t_levels[0] if len(t_levels) == 1 else "", wing=t_wing or rec.wing,
                          mark=mk.key, props=props,
                          extra={"table": title, "layout": "transposed", "part": ""}))
            n += 1
        out.append(n)
    return out
