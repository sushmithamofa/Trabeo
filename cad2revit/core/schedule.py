"""Read the beam and slab schedules printed on the drawing itself.

A beam's width is in the plan. Its **depth is not** - a centreline carries no
depth, so without a schedule every beam has to be built at a guessed default and
the model comes out with every beam the same size.

But the schedule is usually right there on the same sheet, as a table of plain
text. On the Reis Magos drawings it is 1,762 text objects on 'beam sch. text',
laid out as a grid:

    -- | 150 | 150 | - | -- | 8 | -- | 2 | 16 | -- | 2 | 16 | -- | -- | 230 | 450 | B | 1,1A
                                                                     ^^^   ^^^   ^   ^^^^
                                                                   width depth pre marks

So the text is grouped back into rows by its Y position, and each row is read
from the right: marks, prefix, depth, width. Everything to the left is
reinforcement, which Revit does not need.

Rows that do not fit that shape - continuation lines carrying a second layer of
bars, headers, notes - are skipped and counted, never guessed at.

A slab schedule is the same idea, read from the left instead:

    S | 2A | 150 | 10 | 150 | 10 | 150 | TWO WAY | -
    ^   ^^   ^^^
  pre mark thickness

Nothing here is invented. If a mark is not in the schedule it keeps no depth at
all, and the report says which ones.
"""
from __future__ import annotations

import os
import re

from .model import INFO, WARNING, Result

ROW_TOLERANCE_MM = 60.0      # text within this in Y is the same row
MIN_WIDTH, MAX_WIDTH = 75.0, 1500.0
MIN_DEPTH, MAX_DEPTH = 75.0, 3000.0
MIN_THICK, MAX_THICK = 75.0, 1000.0

_NUM = re.compile(r"^\s*(\d+(?:\.\d+)?)")
_PREFIX = re.compile(r"^[A-Za-z]{1,3}$")
_MARK = re.compile(r"^[A-Za-z]{0,3}\d+[A-Za-z]?$")


def _lead_number(s: str) -> float | None:
    """The number a cell starts with. '550/' -> 550, for a beam of two depths."""
    m = _NUM.match(str(s))
    return float(m.group(1)) if m else None


def _text_of(e) -> str:
    try:
        t = e.dxf.text if e.dxftype() == "TEXT" else e.plain_text()
    except Exception:
        return ""
    return str(t).strip()


def rows_from(entities, layer: str) -> list[list[str]]:
    """Text on `layer`, grouped back into table rows, each read left to right."""
    return [[c[1] for c in row] for _, row in _rows_at(entities, layer)]


def _rows_at(entities, layer: str) -> list[tuple[float, list[tuple[float, str]]]]:
    """Table rows as (y, [(x, text) left to right]), top row first."""
    cells = []
    for e in entities:
        if e.dxftype() not in ("TEXT", "MTEXT"):
            continue
        if str(e.dxf.layer).upper() != layer.upper():
            continue
        t = _text_of(e)
        if not t:
            continue
        try:
            p = e.dxf.insert
            cells.append((float(p[1]), float(p[0]), t))
        except Exception:
            continue

    # Group by Y, then order each row by X. The two have to be separate steps:
    # the cells of one printed row do not share a baseline - on this drawing they
    # scatter over 40 mm - so sorting by Y and X together shuffles the columns,
    # and 230 x 450 comes out as 450 x 230.
    cells.sort(key=lambda c: -c[0])
    rows, cur, y0 = [], [], None
    for y, x, t in cells:
        if y0 is None or abs(y - y0) > ROW_TOLERANCE_MM:
            if cur:
                rows.append((y0, sorted(cur)))
            cur, y0 = [], y
        cur.append((x, t))
    if cur:
        rows.append((y0, sorted(cur)))
    return rows


def _marks_in(cell: str, prefix: str) -> list[str]:
    """'1,1A' with prefix 'B' -> ['B1', 'B1A']."""
    out = []
    for part in re.split(r"[,/&]| AND ", str(cell).upper()):
        part = part.strip().replace(" ", "")
        if not part:
            continue
        if not _MARK.fullmatch(part):
            return []                       # not a mark list at all
        out.append(part if part.startswith(prefix.upper()) else prefix.upper() + part)
    return out


def _size_pair(row: list[str], lo: int, hi: int) -> tuple[float, float] | None:
    """The two size cells beside the mark block, whichever side they sit on.

    Some offices print SIZE then the mark, some the mark then SIZE, so neither
    end of the row can be assumed. The pair is always the two adjacent numeric
    cells, and always reads width then depth left to right.
    """
    for a, b in ((hi + 1, hi + 2), (lo - 2, lo - 1)):
        if a < 0 or b >= len(row):
            continue
        w, d = _lead_number(row[a]), _lead_number(row[b])
        if w is None or d is None:
            continue
        if MIN_WIDTH <= w <= MAX_WIDTH and MIN_DEPTH <= d <= MAX_DEPTH:
            return w, d
    return None


def _mark_block(row: list[str]) -> tuple[int, int, list[str]] | None:
    """Locate the prefix cell and the mark list beside it.

    A separator cell often sits between them - 'B | - | 3,8' - so the mark is
    looked for one or two cells either side, nearest first.
    """
    for i, cell in enumerate(row):
        pre = cell.strip()
        if not _PREFIX.fullmatch(pre):
            continue
        for j in (i + 1, i + 2, i - 1, i - 2):
            if not (0 <= j < len(row)):
                continue
            marks = _marks_in(row[j], pre)
            if marks:
                return min(i, j), max(i, j), marks
    return None


def read_beam_schedule(entities, layer: str) -> tuple[dict[str, dict], int, int]:
    """{mark: {width, depth}} from the beam schedule table."""
    entities = list(entities)
    sizes: dict[str, dict] = {}
    used = skipped = 0
    rows = _rows_at(entities, layer)
    sized, loose = [], []          # (y, mark x, width x, depth x, w, d, marks) / (y, mark x, marks)
    for y, cells in rows:
        row = [t for _, t in cells]
        found = _mark_block(row) if len(row) >= 3 else None
        if len(row) < 4 or not found:
            if found:
                loose.append((y, cells[found[0]][0], found[2], cells[found[1]][0]))
            skipped += 1
            continue
        lo, hi, marks = found
        pair = _size_pair(row, lo, hi)
        if pair is None:
            loose.append((y, cells[lo][0], marks, cells[hi][0]))
            skipped += 1
            continue
        w, d = pair
        xs = next(((cells[a][0], cells[b][0]) for a, b in ((hi + 1, hi + 2), (lo - 2, lo - 1))
                   if 0 <= a and b < len(cells) and (_lead_number(row[a]), _lead_number(row[b])) == (w, d)),
                  (None, None))
        sized.append((y, cells[lo][0], xs[0], xs[1], w, d, marks))
    # A row whose mark sits a little off its sizes' line splits in two (Stilt A wing
    # B4: mark 64 mm above "230 450"), and the two numbers left beside the mark are
    # bar spacings, "175 200". Such a pair is not in the table's size columns - where
    # every other row of that mark column prints its size - so it is not taken; the
    # row is read through its ruled cell below like any mark line without a size.
    cols = _size_columns(sized)
    good = []
    for s in sized:
        c = cols.get(_column_key(s[1], cols))
        if c and s[2] is not None and abs(s[2] - c[0]) > CELL_X_MM:
            loose.append((s[0], s[1], s[6], s[1]))
            skipped += 1
            continue
        good.append(s)
        for m in s[6]:
            sizes.setdefault(m, {"width": s[4], "depth": s[5]})
        used += 1
    sized = [s for s in good if s[2] is not None]
    for (w, d), marks in _merged_rows(entities, rows, sized, loose):
        for m in marks:
            sizes.setdefault(m, {"width": w, "depth": d})
        used += 1
        skipped -= 1
    return sizes, used, skipped


CELL_X_MM = 150.0            # a size cell this near its column's x is in that column


def _column_key(mx: float, cols: dict) -> float | None:
    return next((k for k in cols if abs(k - mx) <= MARK_COLUMN_MM), None)


def _size_columns(sized) -> dict[float, tuple[float, float]]:
    """{mark column x: (width x, depth x)} - the table's size columns.

    Width and depth are printed in the cells next to the mark. When a row splits
    over two lines, the pair found beside the mark is further along the row (bar
    spacings), never nearer. So the size columns are the pair positions nearest
    the mark column that at least two rows share - not the commonest, which on
    the 1153 basement table, where most rows split, are the spacings."""
    groups: dict[float, list] = {}
    for s in sized:
        if s[2] is None:
            continue
        k = next((k for k in groups if abs(k - s[1]) <= MARK_COLUMN_MM), s[1])
        groups.setdefault(k, []).append((s[2], s[3], s[1]))
    out = {}
    for k, xs in groups.items():
        # by the width cell only: a depth of two values ("1147/", J1153) is centred
        # differently from a plain one
        shared = [p for p in xs if sum(1 for q in xs if abs(q[0] - p[0]) <= CELL_X_MM) >= 2]
        if shared:
            best = min(shared, key=lambda p: abs(p[0] - p[2]))
            out[k] = (best[0], best[1])
    return out


def _horizontal_rules(entities) -> list[tuple[float, float, float]]:
    """Straight horizontal lines: (y, x from, x to)."""
    out = []
    for e in entities:
        t = e.dxftype()
        try:
            if t == "LINE":
                segs = [((e.dxf.start[0], e.dxf.start[1]), (e.dxf.end[0], e.dxf.end[1]))]
            elif t == "LWPOLYLINE":
                pts = [(p[0], p[1]) for p in e.get_points("xy")]
                if e.closed and pts:
                    pts.append(pts[0])
                segs = list(zip(pts, pts[1:]))
            else:
                continue
        except Exception:
            continue
        for (x0, y0), (x1, y1) in segs:
            if abs(y0 - y1) < 1.0 and abs(x1 - x0) > 1.0:
                out.append((float(y0), float(min(x0, x1)), float(max(x0, x1))))
    return out


def _merged_rows(entities, rows, sized, loose):
    """Mark lines that share one ruled cell with a size printed once for all of them.

    A table often lists several mark lines in one cell and prints the size once,
    centred down the cell (Stilt A wing: "B - 10,11,12,13" ... "B - 38,39" over
    eight lines, one "230 450" beside them). The table's own ruling lines say which
    lines share the cell: everything between the rule above a mark line and the
    rule below it, rules that run from the mark column across the size columns.
    The cell must hold exactly one size; with none, two, or no rules at all, the
    line stays unread as before.
    """
    if not loose or not sized:
        return []
    rules = _horizontal_rules(entities)
    if not rules:
        return []
    out = []
    for y, mx, marks, mx2 in loose:
        # this table's size columns: from the nearest sized row with its mark column here
        same = [s for s in sized if abs(s[1] - mx) <= MARK_COLUMN_MM]
        if not same:
            continue
        ref = min(same, key=lambda s: abs(s[0] - y))
        xw, xd = ref[2], ref[3]
        if min(xw, xd) - CELL_X_MM <= mx2:
            continue            # "BKT | 230 | 300": the "mark" is the width (J1153 4th), not a mark line
        across = [r for r in rules if r[1] <= mx + CELL_X_MM and r[2] >= max(xw, xd) - CELL_X_MM]
        above = [r[0] for r in across if r[0] > y]
        below = [r[0] for r in across if r[0] < y]
        if not above or not below:
            continue
        top, bot = min(above), max(below)
        found = set()
        for ry, cells in rows:
            if not (bot < ry < top):
                continue
            w = next((_lead_number(t) for x, t in cells if abs(x - xw) <= CELL_X_MM), None)
            d = next((_lead_number(t) for x, t in cells if abs(x - xd) <= CELL_X_MM), None)
            if w is not None and d is not None and MIN_WIDTH <= w <= MAX_WIDTH and MIN_DEPTH <= d <= MAX_DEPTH:
                found.add((w, d))
        if len(found) == 1:
            out.append((found.pop(), marks))
    return out


def read_slab_schedule(entities, layer: str) -> tuple[dict[str, float], int, int]:
    """{mark: thickness} from the slab schedule table."""
    out: dict[str, float] = {}
    used = skipped = 0
    for row in rows_from(entities, layer):
        if len(row) < 3:
            skipped += 1
            continue
        # Two tables side by side on one layer print their rows on one line (Wing
        # C 2nd floor: beam B22,29 and slab S8A), so every mark block is read.
        found = False
        for i, cell in enumerate(row):
            if i and not (_PREFIX.fullmatch(cell.strip()) or _WHOLE_MARK.fullmatch(cell.strip())):
                continue
            got = _slab_row(row[i:])
            if got:
                found = True
                for m in got[0]:
                    out.setdefault(m, got[1])
        if found:
            used += 1
        else:
            skipped += 1
    return out, used, skipped


_WHOLE_MARK = re.compile(r"^([A-Za-z]{1,3})\s*-\s*(\d+[A-Za-z]?(?:\s*,\s*\d+[A-Za-z]?)*)$")


def _thick(cell) -> float | None:
    v = _lead_number(cell)
    return v if v is not None and MIN_THICK <= v <= MAX_THICK else None


def _slab_row(row: list[str]) -> tuple[list[str], float] | None:
    """The marks and thickness of one slab schedule row, or None.

    Three layouts turn up:
        S | - | 4A | 150 | ...     prefix and mark in separate cells
        SC-1 | 150 | ...           the whole mark in one cell (or ST-2,4)
        SC | 150 | 10 | ...        a mark that is only a prefix
    The last would otherwise read as mark SC150 with no thickness after it.
    """
    found = _mark_block(row)
    if found:
        lo, hi, marks = found
        # the thickness is the first numeric cell beside the mark block
        for k in (hi + 1, lo - 1):
            if 0 <= k < len(row) and _thick(row[k]):
                return marks, _thick(row[k])
    for i, cell in enumerate(row[:-1]):
        m = _WHOLE_MARK.fullmatch(cell.strip())
        if m and _thick(row[i + 1]):
            marks = _marks_in(m.group(2), m.group(1))
            if marks:
                return marks, _thick(row[i + 1])
    for i, cell in enumerate(row[:-1]):
        if _PREFIX.fullmatch(cell.strip()) and _thick(row[i + 1]):
            return [cell.strip().upper()], _thick(row[i + 1])
    return None


_SUNK = re.compile(r"(\d+(?:\.\d+)?)\s*MM\s*SUNK", re.I)
_PARTLY = re.compile(r"PART", re.I)
SUNK_ROW_REACH_MM = 1500.0   # a remark this far below its slab row at most
TABLE_EDGE_MM = 1500.0       # how far either side of a table its cells may sit
MARK_COLUMN_MM = 800.0       # a cell this near the mark column makes a real row
ROW_SPLIT_MM = 150.0         # two lines this close are one printed row
_SLAB_TYPE = re.compile(r"\bWAY\b", re.I)


def read_slab_sunk(entities, layer: str) -> tuple[dict[str, float], list[str]]:
    """{mark: sunk depth} from the remarks of the slab schedule.

    The remark is printed under the slab's own row, one or two lines down, in
    the last column:

        S | - | 1 | 200 | 12 | 150 | 12 | 150 | ONE WAY
                      TOP & BOTTOM            DECK SLAB
                                              400mm SUNK

    So each "N mm SUNK" belongs to the first schedule row above it in its own
    table. On all the Reis Magos plans the remark is 250-550 mm under its row
    and further from the next.

    Tables can share a layer side by side (Wing C 2nd floor: the beam table's
    remark column runs right up against the slab table, and a beam remark sits
    on the same line as slab S4). So a remark's table is the one whose mark
    column is nearest on its left, and only that table's cells are looked at.
    The first real row above decides - one with a mark in the mark column, or
    a slab type (ONE WAY / TWO WAY). If it carries a mark, that mark is sunk; if
    not (a row whose mark is on another layer) nothing is guessed. Remark lines
    ("TOP & BOTTOM", "DECK SLAB", "CANTILEVER") and the "-" printed between
    prefix and mark, which on Wing A sits 65 mm below its row, are passed over. A remark under a beam row gives a beam mark, which no
    slab carries, so it changes nothing.

    A remark saying PARTLY or PARTIALLY sunk is not applied to the whole panel;
    it comes back in the second list to be reported instead.
    """
    rows = _rows_at(entities, layer)
    mark_cols = []
    for y, row in rows:
        x = _mark_x(row)
        if x is not None:
            mark_cols.append(x)
    out: dict[str, float] = {}
    partial: list[str] = []
    for i, (y, row) in enumerate(rows):
        for x, t in row:
            m = _SUNK.search(t)
            if not m:
                continue
            lefts = [c for c in mark_cols if c <= x]
            if not lefts:
                continue
            edge = max(lefts)
            marks = None
            def in_table(rrow):
                return [(cx, ct) for cx, ct in rrow
                        if edge - TABLE_EDGE_MM <= cx <= x + TABLE_EDGE_MM
                        and not _SUNK.search(ct)]

            def marks_of(cells):
                texts = [ct for _, ct in cells]
                got = _slab_row(texts)
                return got[0] if got else (_mark_block(texts) or (0, 0, None))[2]

            for j in range(i - 1, -1, -1):              # upwards from the remark
                ry, rrow = rows[j]
                if ry - y > SUNK_ROW_REACH_MM:
                    break
                cells = in_table(rrow)
                if not any((abs(cx - edge) <= MARK_COLUMN_MM and ct.strip("- "))
                           or _SLAB_TYPE.search(ct) for cx, ct in cells):
                    continue                             # a remark line, not a row
                marks = marks_of(cells)
                # A row printed a few mm off its own baseline splits in two: S8A
                # on Wing C 2nd floor has its "- ONE WAY" 63 mm below its mark.
                # The half with no mark takes the mark of the half just above.
                if not marks and j and rows[j - 1][0] - ry <= ROW_SPLIT_MM:
                    marks = marks_of(in_table(rows[j - 1][1]))
                break
            for mk in marks or []:
                if _PARTLY.search(t):
                    partial.append(mk)
                else:
                    out.setdefault(_key(mk), float(m.group(1)))
    return out, partial


_BEAM_REMARK = re.compile(r"SUNK|BT\s*@|LVL|INVERTED", re.I)
_BT_REL = re.compile(r"BT\s*@\s*([+-])\s*(\d+(?:\.\d+)?)\s*(MM|M)?\b", re.I)
_OWN_MARK = re.compile(r"^\s*([A-Z]{1,3})\s*-?\s*(\d+[A-Z]?)\s*-\s*(?:BT|BEAM)", re.I)


def read_beam_levels(entities, layer: str) -> tuple[dict, dict]:
    """What the beam schedule's remarks say about each beam's level.

    Returns ({mark: [top offsets in mm]}, {mark: [remarks that cannot be applied]}).
    A remark printed on the beam's own row belongs to it; one on a line of its
    own belongs to the first real row above it in the same table - the rule
    that reads the slab remarks. "B54- BT@ 12.5M" names its beam itself.

        250MM SUNK   -> -250        BT@ +0.200M -> +200
        PARTLY SUNK, INVERTED, BT@ 12.5M, BEAM @ LVL 10.25M -> reported: a
        stepped beam's step is not in the schedule, and an absolute level needs
        the floor's own level, which the drawing does not give.
    """
    rows = _rows_at(entities, layer)
    mark_cols = [x for x in (_mark_x(r) for _, r in rows) if x is not None]
    levels: dict[str, list] = {}
    notes: dict[str, list] = {}

    def marks_in(cells):
        texts = [ct for _, ct in cells]
        got = _mark_block(texts)
        return got[2] if got else None

    for i, (y, row) in enumerate(rows):
        for x, t in row:
            if not _BEAM_REMARK.search(t):
                continue
            own = _OWN_MARK.match(t)
            if own:
                marks = [(own.group(1) + own.group(2)).upper()]
            else:
                lefts = [c for c in mark_cols if c <= x]
                if not lefts:
                    continue
                edge = max(lefts)

                def in_table(rrow):
                    return [(cx, ct) for cx, ct in rrow
                            if edge - TABLE_EDGE_MM <= cx <= x + TABLE_EDGE_MM
                            and not _BEAM_REMARK.search(ct)]
                marks = None
                for j in range(i, -1, -1):              # its own row, then upwards
                    ry, rrow = rows[j]
                    if ry - y > SUNK_ROW_REACH_MM:
                        break
                    cells = in_table(rrow)
                    if not any(abs(cx - edge) <= MARK_COLUMN_MM and ct.strip("- ")
                               for cx, ct in cells):
                        continue
                    marks = marks_in(cells)
                    if not marks and j and rows[j - 1][0] - ry <= ROW_SPLIT_MM:
                        marks = marks_in(in_table(rows[j - 1][1]))
                    break
            for mk in marks or []:
                k = _key(mk)
                m_sunk = _SUNK.search(t)
                m_bt = _BT_REL.search(t)
                if _PARTLY.search(t) or re.search(r"INVERTED", t, re.I):
                    notes.setdefault(k, []).append(t.strip())
                elif m_sunk:
                    levels.setdefault(k, []).append(-float(m_sunk.group(1)))
                elif m_bt:
                    v = float(m_bt.group(2))
                    unit = (m_bt.group(3) or "").upper()
                    mm = v if unit == "MM" or (not unit and v > 20) else v * 1000.0
                    levels.setdefault(k, []).append(mm if m_bt.group(1) == "+" else -mm)
                else:
                    notes.setdefault(k, []).append(t.strip())      # absolute level
    return levels, notes


_SIZE = re.compile(r"^\d{3,4}$")


def read_column_schedules(entities, layer: str) -> dict[str, dict[str, tuple]]:
    """{table heading: {mark: (short side, long side)}} from the column schedule.

    Rows read 'C | - | 2,7 | 230 | X | 1500 | ...'. A sheet can hold one table
    per wing, and the same mark is a different column in each (C-1 is 300 x 1250
    in one Reis Magos wing and 230 x 1050 in another), so every row is kept
    under the heading above it - "SCHEDULE OF COLUMN WING C" and so on.
    """
    heads = []
    for e in entities:
        if e.dxftype() in ("TEXT", "MTEXT"):
            t = _text_of(e).upper()
            if "SCHEDULE OF COLUMN" in t:
                try:
                    heads.append((float(e.dxf.insert[0]), float(e.dxf.insert[1]),
                                  re.sub(r"%%[A-Z]", "", t).strip()))
                except Exception:
                    pass
    out: dict[str, dict] = {}
    for y, row in _rows_at(entities, layer):
        cells = [t for _, t in row]
        for i, c in enumerate(cells):
            pre = c.strip().upper()
            if pre not in ("C", "FC"):
                continue
            j = i + 1
            if j < len(cells) and cells[j].strip() == "-":
                j += 1
            if j >= len(cells):
                continue
            marks = _marks_in(cells[j], pre)
            rest = cells[j + 1:j + 4]
            if not marks or len(rest) < 3 or not _SIZE.match(rest[0].strip()) \
                    or rest[1].strip().upper() != "X" or not _SIZE.match(rest[2].strip()):
                continue
            w, d = float(rest[0]), float(rest[2])
            x = row[i][0]
            above = [h for h in heads if h[1] > y and abs(h[0] - x) < 15000]
            head = min(above, key=lambda h: (h[1] - y) + abs(h[0] - x))[2] if above else ""
            for m in marks:
                out.setdefault(head, {}).setdefault(_key(m), (min(w, d), max(w, d)))
    return out


def _wing_of(text: str) -> str:
    """The one wing a name refers to, or "" - also when it names several
    ("STILT FLOOR PLAN ( A,B & C WING )"), where no single wing's table applies."""
    t = text.upper()
    if re.search(r"\b[A-Z]\s*(?:,|&|\bAND\b)\s*[A-Z]\b[^()]*WING", t) or \
            re.search(r"WING\s*-?\s*[A-Z]\s*(?:,|&|\bAND\b)\s*[A-Z]\b", t):
        return ""
    m = re.search(r"WING\s*-?\s*([A-Z])\b|\(\s*([A-Z])\s*WING\s*\)|\b([A-Z])\s+WING\b", text.upper())
    return next((g for g in (m.groups() if m else ()) if g), "")


def check_columns(entities, res: Result, layers) -> None:
    """Cross-check each marked column's drawn size against the column schedule.

    Reported, not changed: on the sheets that carry both, the drawn outlines
    agree with the schedule for 129 of 133 columns, and the few that differ
    are real questions (Wing C 1st floor C-24 is drawn 350 x 1480; its own table
    says 230 x 900, the stilt sheet's says 350 x 1480 - a column changing size
    between floors). Resizing it silently would hide that.
    """
    tables: dict[str, dict] = {}
    for lay in layers:
        for h, t in read_column_schedules(entities, lay).items():
            tables.setdefault(h, {}).update(t)
    if not tables:
        return
    # the original's path is only filled in after extraction; the converted DXF
    # keeps its name ("... ( A WING ) ....dxf")
    wing = _wing_of(os.path.basename(res.source_file or res.dxf_file or ""))
    if len(tables) == 1:
        use = next(iter(tables.values()))
    else:
        match = [t for h, t in tables.items() if wing and _wing_of(h) == wing
                 and "PART" not in h]
        if len(match) != 1:
            res.add(INFO, "schedule",
                    f"The sheet has {len(tables)} column schedules ({'; '.join(sorted(tables))}) "
                    "and none is clearly this drawing's wing, so the columns were not checked "
                    "against them.")
            return
        use = match[0]
    ok, off = 0, []
    for c in res.columns:
        if not c.mark:
            continue
        k = _key(c.mark)
        k = k if k in use else ("C" + k if ("C" + k) in use else None)
        if k is None:
            continue
        w, d = use[k]
        if abs(c.width - w) <= 25 and abs(c.depth - d) <= 25:
            ok += 1
        else:
            off.append(c)
            res.add(WARNING, "schedule",
                    f"Column {c.mark} is drawn {c.width:.0f} x {c.depth:.0f} but the column "
                    f"schedule says {w:.0f} x {d:.0f}. It is built as drawn; check which is "
                    "right (a column can change size between floors).",
                    mark=c.mark, x=c.x, y=c.y, handle=c.handle)
    res.add(INFO, "schedule",
            f"The column schedule was read: {ok} marked columns match their drawn size; "
            f"{len(off)} differ and are listed.")


def _mark_x(row) -> float | None:
    """Where the mark column of a schedule row is: its prefix cell, or None."""
    texts = [t for _, t in row]
    got = _slab_row(texts)
    marks = got[0] if got else (_mark_block(texts) or (0, 0, None))[2]
    if not marks:
        return None
    for x, t in row:
        c = t.strip().upper()
        if (_PREFIX.fullmatch(c) or _WHOLE_MARK.fullmatch(c)) and \
                any(mk.upper().startswith(re.sub(r"[^A-Z]", "", c)) for mk in marks):
            return x
    return None


def _key(mark: str) -> str:
    """Loose match: 'B-107', 'B 107' and 'B107' are the same beam. A separator
    between two numbers is kept: 'S1-2' is not 'S12'."""
    s = re.sub(r"(?<=\d)\s*[-–—.]\s*(?=\d)", "~", str(mark).upper())
    return re.sub(r"[^A-Z0-9~]", "", s).replace("~", "-")



def read_all(entities, cfg: dict):
    """Everything the printed schedules say, without touching the Result.

    Read once, early, because two different steps need it: the merging of face
    pairs needs to know what widths exist before it decides what is a beam, and
    apply() needs the depths after the marks have been matched.
    """
    s = cfg.get("schedules") or {}
    if s.get("read") is False:
        return {}, {}, (0, 0, 0, 0)

    beam_sizes: dict[str, dict] = {}
    b_used = b_skipped = 0
    for lay in s.get("beam_layers") or []:
        got, u, k = read_beam_schedule(entities, lay)
        for m, v in got.items():
            beam_sizes.setdefault(_key(m), v)
        b_used += u
        b_skipped += k

    slab_th: dict[str, float] = {}
    s_used = s_skipped = 0
    for lay in s.get("slab_layers") or []:
        got, u, k = read_slab_schedule(entities, lay)
        for m, v in got.items():
            slab_th.setdefault(_key(m), v)
        s_used += u
        s_skipped += k
    # Some sheets print the slab table on the beam schedule's layer (Wing C 2nd
    # floor). Only slab marks are taken from there, so no beam row can pass for a
    # slab; the slab layer's own reading wins where both give one.
    pre = _slab_prefixes(cfg)
    for lay in s.get("beam_layers") or []:
        got, u, _ = read_slab_schedule(entities, lay)
        for m, v in got.items():
            if _key(m).startswith(pre):
                if _key(m) not in slab_th:
                    s_used += 1
                slab_th.setdefault(_key(m), v)

    return beam_sizes, slab_th, (b_used, b_skipped, s_used, s_skipped)


def _slab_prefixes(cfg: dict) -> tuple[str, ...]:
    return tuple(p.upper() for p in
                 ((cfg or {}).get("mark_prefixes", {}).get("slab") or ["S"]))


def known_widths(beam_sizes: dict) -> list[float]:
    """The distinct beam widths the drawing's own schedule says exist."""
    return sorted({round(float(v["width"]), 1) for v in beam_sizes.values()
                   if v.get("width")})


def apply(entities, res: Result, cfg: dict, log=print, pre=None) -> None:
    """Give every marked beam and slab tag its size from the drawing's schedule.

    `pre` is the result of read_all, so the tables are not parsed twice.
    """
    s = cfg.get("schedules") or {}
    if s.get("read") is False:
        return

    beam_layers = s.get("beam_layers") or []
    slab_layers = s.get("slab_layers") or []

    if pre is None:
        pre = read_all(entities, cfg)
    beam_sizes, slab_th, counts = pre
    b_used, b_skipped, s_used, s_skipped = counts

    if not beam_sizes and not slab_th:
        if beam_layers or slab_layers:
            res.add(INFO, "schedule",
                    "No schedule table was found on the layers named in 'schedules' in "
                    "config.json, so no beam depth could be read from the drawing. Every "
                    "beam will be built at the default depth until the schedule workbook "
                    "is filled in.")
        return

    # ---- beams ----------------------------------------------------------
    hit = missed = 0
    unknown: list[str] = []
    for b in res.beams:
        if not b.mark:
            missed += 1
            continue
        v = beam_sizes.get(_key(b.mark))
        if not v:
            missed += 1
            unknown.append(b.mark)
            continue
        b.depth = v["depth"]
        b.depth_source = "drawing schedule"
        if b.width is None:
            b.width = v["width"]
            b.width_source = "drawing schedule"
        elif abs(b.width - v["width"]) > 25.0:
            res.add(WARNING, "schedule",
                    f"Beam {b.mark} measures {b.width:.0f} mm wide on the plan but the "
                    f"schedule says {v['width']:.0f} mm. The plan width has been kept; "
                    "confirm which is right.",
                    mark=b.mark, x=b.start[0], y=b.start[1])
        elif b.width != v["width"]:
            # A few mm off is drafting slack (GB2 drawn 1503 for a 1500 girder), and
            # left alone it becomes its own Revit type beside the scheduled one.
            b.width = v["width"]
            b.width_source = "drawing schedule (plan within 25 mm)"
        hit += 1

    # ---- columns: cross-checked against the column schedule --------------
    check_columns(entities, res, s.get("column_layers") or ["col. sch."])

    # ---- beam levels, from the remarks --------------------------------
    levels: dict[str, list] = {}
    cannot: dict[str, list] = {}
    for lay in beam_layers:
        lv, nt = read_beam_levels(entities, lay)
        for m, v in lv.items():
            levels.setdefault(m, []).extend(v)
        for m, v in nt.items():
            cannot.setdefault(m, []).extend(v)
    moved, stepped = [], []
    for b in res.beams:
        k = _key(b.mark) if b.mark else ""
        vals = sorted(set(levels.get(k, [])))
        if len(vals) == 1 and k not in cannot:
            b.top_mm = vals[0]
            b.notes.append(f"top {vals[0]:+.0f} mm (beam schedule remark)")
            moved.append(b.mark)
        elif vals or k in cannot:
            stepped.append(b.mark)
    if moved:
        res.add(INFO, "schedule",
                f"{len(set(moved))} beams are set off the level by the beam schedule's remarks "
                "(\"N MM SUNK\" drops the top, \"BT@ +0.200M\" raises it): " + ", ".join(
                    f"{m} {levels[_key(m)][0]:+.0f}" for m in sorted(set(moved))) + ".")
    for m in sorted(set(stepped)):
        what = sorted(set(cannot.get(_key(m), [])) | {f"{v:+.0f} mm" for v in levels.get(_key(m), [])})
        res.add(WARNING, "schedule",
                f"Beam {m}: the schedule's remarks say {'; '.join(what)}. That is a stepped, "
                "inverted or absolute-level beam, which the remark alone cannot place, so it "
                "is built flush with the level. Model its level by hand.", mark=m)

    # ---- slab tags ------------------------------------------------------
    s_hit = 0
    for t in res.slab_tags:
        th = slab_th.get(_key(t.mark))
        if th:
            t.thickness = th
            t.thickness_source = "drawing schedule"
            s_hit += 1

    sunk: dict[str, float] = {}
    partial: list[str] = []
    pre = _slab_prefixes(cfg)
    for lay in list(slab_layers) + list(beam_layers):
        got, part = read_slab_sunk(entities, lay)
        for m, v in got.items():
            if m.startswith(pre):
                sunk.setdefault(m, v)
        partial += [m for m in part if _key(m).startswith(pre)]
    for t in res.slab_tags:
        if _key(t.mark) in sunk:
            t.sunk_mm = sunk[_key(t.mark)]
    if sunk:
        res.add(INFO, "schedule",
                "The slab schedule marks these slabs as sunk, and they are built that "
                "far below the level: " + ", ".join(
                    f"{m} {v:.0f} mm" for m, v in sorted(sunk.items())) + ".")
    for m in sorted(set(partial)):
        res.add(WARNING, "slabs",
                f"The slab schedule says {m} is only partly sunk. The panel is built at "
                "the level; model the sunk part by hand.", mark=m)

    if b_used:
        log(f"  Read the beam schedule off the drawing: {len(beam_sizes)} marks, "
            f"{hit} of {len(res.beams)} beams sized")
        res.add(INFO, "schedule",
                f"The beam schedule printed on the drawing was read: {b_used} rows giving "
                f"{len(beam_sizes)} marks their width and depth. {hit} of the "
                f"{len(res.beams)} beams now carry a real depth instead of a default."
                + (f" {b_skipped} rows were not in the expected shape (continuation lines, "
                   "headers and notes) and were skipped." if b_skipped else ""))
    if s_used:
        log(f"  Read the slab schedule off the drawing: {len(slab_th)} marks, "
            f"{s_hit} of {len(res.slab_tags)} slab tags sized")
        res.add(INFO, "schedule",
                f"The slab schedule printed on the drawing was read: {len(slab_th)} marks "
                f"with a thickness, matching {s_hit} of the {len(res.slab_tags)} slab tags.")

    if missed:
        names = sorted(set(unknown))
        res.add(WARNING, "schedule",
                f"{missed} beams have no size from the schedule and will be built at the "
                "default depth. "
                + (f"{len(names)} marks are on the plan but not in the schedule: "
                   + ", ".join(names[:20]) + ("..." if len(names) > 20 else "")
                   if names else "They carry no mark at all, so nothing connects them to "
                                 "a schedule row."))
