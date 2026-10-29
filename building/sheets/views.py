"""Splitting a drawing into views: the unit everything else reads.

A view is one rectangle of model space with one purpose - a framing plan, a set
of beam sections, a schedule. Consultants arrange them two ways, and both are
handled:

* Paper-space layouts. Each viewport on a layout looks at one window of model
  space. This is the only reliable way to tell a plan from the details drawn
  right next to it: on the Sasan Gir terrace sheet the framing plan and its beam
  details touch in model space and only the viewports separate them.
* Everything in model space, no layouts (Reis Magos). Then the drawing is split
  into regions: groups of entities with clear space between them.

Viewports that look at nothing (title-block windows, empty frames) are dropped,
and a window shown twice on one layout is kept once. Model space that no
viewport shows is reported as its own regions, so nothing is silently ignored.
"""
from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import dataclass, field

from .source import centre_of, clean, extent_of

MIN_ENTITIES = 5          # a viewport showing fewer is a frame, not a drawing
STRAY_MAX = 30            # a model region this small is a scrap: listed, not read
OVERVIEW_SCALE = 1000.0   # a viewport at 1:1000 or smaller shows the whole site
REGION_CELL_MM = 2000.0   # gap that separates two model-space regions
SAME_WINDOW = 0.02        # windows within 2% of each other are the same view


@dataclass
class View:
    id: str
    source: str                         # "viewport" | "model region" | "unplaced region"
    layout: str                         # layout name, "" for model-space regions
    window: tuple[float, float, float, float]   # model space x0, y0, x1, y1
    scale: float = 1.0                  # model units per paper unit (100 = 1:100)
    paper: tuple[float, float, float, float] | None = None   # rectangle on the layout
    handles: list[str] = field(default_factory=list)          # model entities shown
    same_as: str = ""                   # id of an identical view on another layout
    # set when one model region held several titled drawings and was split by
    # their titles: the region it came from, and the title it was grouped under
    split_from: str = ""
    title_hint: str = ""
    # one of two drawings of the same floor side by side (the same grid twice): "plan" for the
    # framing plan, "reinforcement" for its slab reinforcement drawing (not read as a floor)
    twin: str = ""
    whole: object = None                # the view the twin was cut from: its title is the plan's
    # slab marks the plan borrows from its reinforcement twin (Pirgal writes them there), and
    # the shift that puts them on the plan
    borrow: list = field(default_factory=list)
    borrow_shift: tuple = (0.0, 0.0)
    # a section or detail whose drawing is kept inside blocks: read as a section or detail,
    # never as a plan (registry)
    inside_blocks: bool = False

    @property
    def size(self) -> tuple[float, float]:
        return self.window[2] - self.window[0], self.window[3] - self.window[1]

    def contains(self, x: float, y: float) -> bool:
        x0, y0, x1, y1 = self.window
        return x0 <= x <= x1 and y0 <= y <= y1


def _index(msp) -> list[tuple[str, float, float, object]]:
    out = []
    for e in msp:
        c = centre_of(e)
        if c is not None:
            out.append((e.dxf.handle, c[0], c[1], e))
    return out


def _viewports(doc) -> list[View]:
    out = []
    for lay in doc.layouts:
        if lay.name == "Model":
            continue
        n = 0
        for e in lay:
            if e.dxftype() != "VIEWPORT":
                continue
            if e.dxf.get("id", 2) == 1:           # the layout's own overall viewport
                continue
            h, w = float(e.dxf.height or 0), float(e.dxf.width or 0)
            vh = float(e.dxf.get("view_height", 0) or 0)
            if h <= 0 or w <= 0 or vh <= 0:
                continue
            direction = e.dxf.get("view_direction_vector", (0.0, 0.0, 1.0))
            if abs(direction[0]) > 1e-9 or abs(direction[1]) > 1e-9 or direction[2] <= 0:
                continue                          # a 3D view: not a plan, not readable
            scale = vh / h
            n += 1
            pc = e.dxf.center
            out.append(View(
                id=f"{lay.name}#{n}", source="viewport", layout=lay.name,
                window=_model_window(e, w * scale, vh), scale=scale,
                paper=(pc[0] - w / 2, pc[1] - h / 2, pc[0] + w / 2, pc[1] + h / 2)))
    return out


def _model_window(vp, vw: float, vh: float) -> tuple[float, float, float, float]:
    """The model-space rectangle a viewport shows.

    The view centre is measured from the view target, not from the model origin:
    model point = target + centre (Villa 5 shifts every view by 367 m, 232 m this
    way). A twisted view turns the window, so its bounding box is taken.
    """
    c = vp.dxf.view_center_point
    t = vp.dxf.get("view_target_point", (0.0, 0.0, 0.0))
    twist = math.radians(float(vp.dxf.get("view_twist_angle", 0.0) or 0.0))
    cos, sin = math.cos(-twist), math.sin(-twist)
    xs, ys = [], []
    for dx, dy in ((-vw / 2, -vh / 2), (vw / 2, -vh / 2), (vw / 2, vh / 2), (-vw / 2, vh / 2)):
        px, py = c[0] + dx, c[1] + dy
        xs.append(t[0] + px * cos - py * sin)
        ys.append(t[1] + px * sin + py * cos)
    return min(xs), min(ys), max(xs), max(ys)


def _same(a, b) -> bool:
    """Two windows showing the same thing: near-identical corners, or 85% overlap
    (Villa 5 shows its face-line drawing on two layouts, framed 1 m differently)."""
    tol = SAME_WINDOW * max(a[2] - a[0], a[3] - a[1], b[2] - b[0], b[3] - b[1])
    if all(abs(p - q) <= tol for p, q in zip(a, b)):
        return True
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return union > 0 and inter / union >= 0.85


# a floor plan's own title ("TERRACE LEVEL LAYOUT PLAN", "SECOND FLOOR PLAN")
_FLOOR_PLAN_TITLE = re.compile(r"\b(?:FLOOR|FLR|LEVEL|LVL|STOREY|STORY|TERRACE|ROOF)\b.*\bPLAN\b")


def _titled_floor_plan(es) -> bool:
    for e in es:
        if e.dxftype() in ("TEXT", "MTEXT"):
            try:
                s = e.plain_text() if e.dxftype() == "MTEXT" else e.dxf.text
            except Exception:
                continue
            if _FLOOR_PLAN_TITLE.search(clean(s or "").upper()):
                return True
    return False


# a section's or detail's own title ("SECTION 1", "SECTION A-A", "FOOTING DETAILS", "TYP. BEAM
# SECTION"); not "SECTION MARK" or a note
_HELD_TITLE = re.compile(r"^(?:TYP(?:ICAL)?\.?\s+)?(?:[A-Z]+\s+){0,2}(?:SECTION|DETAILS?|ELEVATION)S?"
                         r"(?!\s*(?:MARK|TYPE|SYMBOL|LINE))(?:\s*(?:OF\s+[A-Z ]+|[:\-]?\s*[A-Z0-9]{1,3}'?"
                         r"(?:\s*-\s*[A-Z0-9]{1,3}'?)?))?\s*$")


def _titled_detail(es) -> bool:
    """A text of its own (not inside a block) that titles a section or detail."""
    for e in es:
        if e.dxftype() in ("TEXT", "MTEXT"):
            try:
                s = e.plain_text() if e.dxftype() == "MTEXT" else e.dxf.text
            except Exception:
                continue
            s = clean(s or "").upper().strip()
            if len(s) <= 40 and _HELD_TITLE.match(s):
                return True
    return False


def _drawn(e, seen: dict, depth: int = 0) -> int:
    """How many drawn things an entity stands for: an INSERT counts what its block
    holds (AKANISTHA's terrace plan is one block "TERRACE" - 22 entities on top,
    the whole floor inside), any other entity 1."""
    if e.dxftype() != "INSERT" or depth > 4:
        return 1
    name = e.dxf.get("name", "")
    if name not in seen:
        seen[name] = 1                   # a block holding itself counts once
        try:
            blk = e.doc.blocks.get(name)
            seen[name] = max(1, sum(_drawn(x, seen, depth + 1) for x in blk)) if blk is not None else 1
        except Exception:
            pass
    return seen[name]


def _regions(items, source: str, prefix: str) -> list[View]:
    """Group entities into regions separated by at least REGION_CELL_MM of space."""
    cells = defaultdict(list)
    ents = {h: e for h, cx, cy, e in items}
    held = {}                       # block name -> how many drawn things it holds
    for h, cx, cy, e in items:
        ext = extent_of(e)
        if ext is None:
            continue
        x0, y0 = int(math.floor(ext[0] / REGION_CELL_MM)), int(math.floor(ext[1] / REGION_CELL_MM))
        x1, y1 = int(math.floor(ext[2] / REGION_CELL_MM)), int(math.floor(ext[3] / REGION_CELL_MM))
        if (x1 - x0 + 1) * (y1 - y0 + 1) > 4000 or x1 - x0 > 40 or y1 - y0 > 40:
            # a sheet border, or a line longer than 80 m: it would join everything
            # it passes, so it only marks the cell its centre is in
            x0 = x1 = int(math.floor(cx / REGION_CELL_MM))
            y0 = y1 = int(math.floor(cy / REGION_CELL_MM))
        for i in range(x0, x1 + 1):
            for j in range(y0, y1 + 1):
                cells[(i, j)].append((h, ext))
    seen, views = set(), []
    for start in sorted(cells):
        if start in seen:
            continue
        stack, comp = [start], []
        seen.add(start)
        while stack:
            p = stack.pop()
            comp.append(p)
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    q = (p[0] + dx, p[1] + dy)
                    if q in cells and q not in seen:
                        seen.add(q)
                        stack.append(q)
        members = {}
        for p in comp:
            for h, ext in cells[p]:
                members[h] = ext
        exts = list(members.values())
        win = (min(e[0] for e in exts), min(e[1] for e in exts),
               max(e[2] for e in exts), max(e[3] for e in exts))
        big = len(members) >= STRAY_MAX
        if not big and source == "model region" and _titled_floor_plan(ents[h] for h in members):
            # a floor plan titled as one, its drawing kept in a block: counted by what the
            # blocks hold, not as a scrap (only then - a lone title block stays a scrap)
            big = sum(_drawn(ents[h], held) for h in members) >= STRAY_MAX
        views.append(View(id="", source=source if big else "stray",
                          layout="", window=win, handles=sorted(members)))
    views.sort(key=lambda v: (v.source == "stray", -len(v.handles)))
    if source == "model region" and views and all(v.source == "stray" for v in views) and \
            len(views[0].handles) >= MIN_ENTITIES:
        # a drawing that is all small pieces is not all scraps: its largest region is
        # the drawing (a section of a few level lines and names - 14 entities)
        views[0].source = source
    for n, v in enumerate(views, 1):
        v.id = f"{'S' if v.source == 'stray' else prefix}{n}"
    for v in views:
        if v.source == "stray" and _titled_detail(ents[h] for h in v.handles) and \
                sum(_drawn(ents[h], held) for h in v.handles) >= STRAY_MAX:
            # a section or detail drawn inside blocks (Winter Villa's "SECTION 1": 15 entities on
            # top, about 800 in its blocks) is read, under the id it had - every other view
            # keeps its id, so decisions saved against them stay where they were
            v.source, v.inside_blocks = source, True
    return views


# --------------------------------------------------------------------------
# one model region, several titled drawings
# --------------------------------------------------------------------------

SUB_CELL_MM = (500.0, 300.0, 150.0)   # finer and finer gaps, until each piece has one plan title
_TITLE_WORDS = re.compile(r"\bLAY\s*-?\s*OUT\b|\bPLAN\b|\bFRAMING\b|\bSECTION\b|\bSCHEDULE\b|"
                          r"\bDETAILS?\b|\bELEVATION\b")
_PLAN_WORDS = re.compile(r"\bLAY\s*-?\s*OUT\b|\bPLAN\b|\bFRAMING\b")
_NOT_TITLE = re.compile(r"^\s*\*|\bNOTES?\b|\bREFER\b|\bAS\s+PER\b|\bFOLLOW\b")


@dataclass
class _Title:
    handle: str
    text: str
    x: float
    y: float
    is_plan: bool
    level: str = ""          # the floor it names first
    height: float = 0.0


def _drawing_titles(items) -> list[_Title]:
    """The titles under the drawings of one region: short, named like a drawing
    (LAYOUT, PLAN, SECTION, SCHEDULE, DETAILS...), underlined or as tall as the
    tallest such text. A plan title must also name a floor."""
    from .levels import levels_in
    cands = []
    for h, _cx, _cy, e in items:
        if e.dxftype() not in ("TEXT", "MTEXT"):
            continue
        try:
            raw = e.dxf.text if e.dxftype() == "TEXT" else e.text
            t = clean(e.plain_text() if e.dxftype() == "MTEXT" else raw).upper()
            hgt = float(e.dxf.height if e.dxftype() == "TEXT" else e.dxf.char_height or 0)
        except Exception:
            continue
        if not (4 <= len(t) <= 70) or not _TITLE_WORDS.search(t) or _NOT_TITLE.search(t):
            continue
        ext = extent_of(e)
        if ext is None:
            continue
        under = "%%U" in (raw or "").upper() or "\\L" in (raw or "")
        cands.append((t, (ext[0] + ext[2]) / 2, (ext[1] + ext[3]) / 2, hgt, under, h))
    if not cands:
        return []
    tall = max(c[3] for c in cands)
    out = []
    for t, x, y, hgt, under, h in cands:
        if not (under or hgt >= 0.8 * tall):
            continue
        lv = [k for k in levels_in(t) if k != "TYPICAL"]
        out.append(_Title(h, t, x, y, bool(_PLAN_WORDS.search(t)) and bool(lv), lv[0] if lv else "", hgt))
    return out


# "stilt floor roof", "basement roof", "u.g. floor roof": a plan named by its floor alone
_FLOOR_NAME = re.compile(r"^(?:(?:BASEMENT|STILT|PODIUM|PLINTH|GROUND|UPPER|LOWER|U\.?\s?G\.?|L\.?\s?G\.?|"
                         r"G\.?\s?F\.?|MEZZ(?:ANINE)?|TERRACE|FIRST|SECOND|THIRD|FOURTH|FIFTH|SIXTH|SEVENTH|"
                         r"EIGHTH|NINTH|TENTH|\d{1,2}\s*(?:ST|ND|RD|TH)|FLOOR|FLR|ROOF|SLAB|LEVEL|LVL)\s*)+$")


def _floor_titles(items) -> list[_Title]:
    """Plans named by their floor alone, with no word like PLAN or LAYOUT (Aldott:
    five floors side by side, each named under it "basement roof" ... "second floor
    roof"). Texts of two words or more made of floor words only, the tallest such."""
    from .levels import levels_in
    cands = []
    for h, _cx, _cy, e in items:
        if e.dxftype() not in ("TEXT", "MTEXT"):
            continue
        try:
            raw = e.dxf.text if e.dxftype() == "TEXT" else e.text
            t = clean(e.plain_text() if e.dxftype() == "MTEXT" else raw).upper()
            hgt = float(e.dxf.height if e.dxftype() == "TEXT" else e.dxf.char_height or 0)
        except Exception:
            continue
        if not (4 <= len(t) <= 40) or len(t.split()) < 2 or not _FLOOR_NAME.match(t):
            continue
        lv = [k for k in levels_in(t) if k != "TYPICAL"]
        ext = extent_of(e)
        if lv and ext is not None:
            cands.append(_Title(h, t, (ext[0] + ext[2]) / 2, (ext[1] + ext[3]) / 2, True, lv[0], hgt))
    tall = max((t.height for t in cands), default=0.0)
    return [t for t in cands if t.height >= 0.8 * tall]


def _pieces(items, cell: float, span: tuple[float, float],
            titles: list[_Title] = ()) -> list[tuple[list[str], tuple[float, float, float, float]]]:
    """Groups of entities with at least `cell` of space round them.

    A sheet border, a frame round several sheets or a title-block rule would join
    every drawing it passes. So anything that surrounds two or more drawing
    titles, or reaches across half the region (`span`), only marks the cell its
    centre is in.
    """
    cells = defaultdict(list)
    for h, cx, cy, e in items:
        ext = extent_of(e)
        if ext is None:
            continue
        x0, y0 = int(math.floor(ext[0] / cell)), int(math.floor(ext[1] / cell))
        x1, y1 = int(math.floor(ext[2] / cell)), int(math.floor(ext[3] / cell))
        frames = sum(1 for t in titles if ext[0] <= t.x <= ext[2] and ext[1] <= t.y <= ext[3]) >= 2
        if frames or (ext[2] - ext[0]) > 0.5 * span[0] or (ext[3] - ext[1]) > 0.5 * span[1] or \
                (x1 - x0 + 1) * (y1 - y0 + 1) > 20000:
            x0 = x1 = int(math.floor(cx / cell))
            y0 = y1 = int(math.floor(cy / cell))
            ext = (cx, cy, cx, cy)          # nor does it stretch the piece it lands in
        for i in range(x0, x1 + 1):
            for j in range(y0, y1 + 1):
                cells[(i, j)].append((h, ext))
    seen, out = set(), []
    for start in sorted(cells):
        if start in seen:
            continue
        stack, members = [start], {}
        seen.add(start)
        while stack:
            p = stack.pop()
            for h, ext in cells[p]:
                members[h] = ext
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    q = (p[0] + dx, p[1] + dy)
                    if q in cells and q not in seen:
                        seen.add(q)
                        stack.append(q)
        exts = list(members.values())
        out.append((sorted(members), (min(e[0] for e in exts), min(e[1] for e in exts),
                                      max(e[2] for e in exts), max(e[3] for e in exts))))
    return out


def _owner(box, inside: list[_Title], titles: list[_Title]) -> _Title:
    """The title a piece of drawing belongs to.

    One inside it is its own. Otherwise the nearest title in line with it: a
    plan is named underneath, a schedule usually above, so both are looked at and
    the one below is preferred when they are about as close. Failing that, the
    nearest title.
    """
    if len(inside) == 1:
        return inside[0]
    x0, y0, x1, y1 = box
    w = x1 - x0
    in_line = [t for t in titles if x0 - 0.15 * w - 500 <= t.x <= x1 + 0.15 * w + 500
               and (t.y <= y0 + 1.0 or t.y >= y1 - 1.0)]
    if in_line:
        def cost(t):
            gap = (y0 - t.y) if t.y <= y0 + 1.0 else 1.5 * (t.y - y1)
            return max(0.0, gap) + 0.2 * abs(t.x - (x0 + x1) / 2)
        return min(in_line, key=cost)

    def gap(t):
        dx = max(x0 - t.x, 0.0, t.x - x1)
        dy = max(y0 - t.y, 0.0, t.y - y1)
        return math.hypot(dx, dy)
    return min(titles, key=gap)


def _stacked(plans: list[_Title]) -> bool:
    """Plan titles one above the other rather than side by side."""
    xs, ys = [t.x for t in plans], [t.y for t in plans]
    return max(ys) - min(ys) > max(xs) - min(xs)


def _stacked_owner(at, plans: list[_Title], inside: list[_Title]) -> _Title:
    """The title an entity of two touching plans, one above the other, belongs to.

    NIFT "LEVEL-1 & 2": the level 1 plan over the level 2 plan, each titled under it,
    with a slab section A-A between them touching both. A section's drawing sits
    right over its own title; anything else belongs to the plan titled under it."""
    cx, cy = at
    cuts = [t for t in inside if not t.is_plan and not _OWN_TO_PLAN.search(t.text) and t.height > 0
            and abs(cx - t.x) <= 6 * t.height and 0.0 <= cy - t.y <= 8 * t.height]
    if cuts:
        return min(cuts, key=lambda t: math.hypot(cx - t.x, cy - t.y))
    below = [t for t in plans if t.y <= cy]
    if below:
        return max(below, key=lambda t: (t.y, -abs(cx - t.x)))
    return min(plans, key=lambda t: abs(cy - t.y) + 0.3 * abs(cx - t.x))


def split_by_titles(region: View, items, floor_titles: list[_Title] = (), framed: bool = False) -> list[View]:
    """One model region holding several titled plans, split into one view per title.

    A sheet drawn whole in model space - the lintel plan, the roof plan, their
    sections and the steel schedule side by side, under 2 m apart - is one region
    to _regions(), and a region is read as one plan: the roof beams joined the
    lintel beams and the sections' beams were built as plan beams. Each drawing
    is named underneath, so the titles say where one ends and the next begins.

    Only a region whose plan titles name two or more different floors is split
    (GF_LINTEL and ROOF here). Anything else is returned as it was ([]), so a
    sheet with one plan and its schedules - even one whose plan is named twice,
    in its title and again in the title block - reads exactly as before.

    floor_titles: plans named by their floor alone ("stilt floor roof"), used only
    where no title here names a plan.
    framed: the region is one viewport framing a whole sheet (NIFT). Only then are
    beam details titled by mark and size, and plans stacked one above the other,
    told apart - a sheet drawn in model space reads exactly as it always has.
    """
    titles = _drawing_titles(items)
    if floor_titles and not any(t.is_plan for t in titles):
        titles = titles + list(floor_titles)
    if len({t.level for t in titles if t.is_plan}) == 1:
        return _split_plan_from_sections(region, items, titles, framed)
    if len({t.level for t in titles if t.is_plan}) < 2:
        return []
    if framed:
        titles = titles + _detail_titles(items, titles)
    title_handles = {t.handle for t in titles}
    body = [it for it in items if it[0] not in title_handles]
    groups: dict[str, list[str]] = defaultdict(list)
    span = region.size
    centre = {h: (cx, cy) for h, cx, cy, _e in items}
    for n, cell in enumerate(SUB_CELL_MM):
        pieces = _pieces(body, cell, span, titles)
        last = n == len(SUB_CELL_MM) - 1
        ok = True
        groups = defaultdict(list)
        for handles, box in pieces:
            inside = [t for t in titles if box[0] <= t.x <= box[2] and box[1] <= t.y <= box[3]]
            plans_in = [t for t in inside if t.is_plan]
            # plan titles right under (or over) this piece, within its width: two of
            # them mean two plans touching, named side by side beneath
            near = max(2000.0, 0.15 * (box[3] - box[1]))
            lined = [t for t in titles if t.is_plan and t not in plans_in and box[0] <= t.x <= box[2]
                     and (0.0 <= box[1] - t.y <= near or 0.0 <= t.y - box[3] <= near)]
            if len(plans_in) + len(lined) > 1:
                plans_in = plans_in + lined
                ok = False                      # two plans still touching: try a finer gap
                if last:
                    # they really touch (one plan's grid runs into the next):
                    # each entity goes to the plan title nearest it, mostly across
                    if framed and _stacked(plans_in):
                        for h in handles:
                            t = _stacked_owner(centre.get(h, (box[0], box[1])), plans_in, inside)
                            groups[t.handle].append(h)
                        continue
                    for h in handles:
                        cx, cy = centre.get(h, (box[0], box[1]))
                        t = min(plans_in, key=lambda t: abs(cx - t.x) + 0.3 * abs(cy - t.y))
                        groups[t.handle].append(h)
                    continue
            owner = _owner(box, plans_in or inside, titles)
            groups[owner.handle] += handles
        if ok:
            break
    by_handle = {t.handle: t for t in titles}
    ext = {h: extent_of(e) for h, _x, _y, e in items}

    def framing(b):
        # a frame round several drawings does not stretch the view it was put in
        return (b[2] - b[0]) > 0.5 * span[0] or (b[3] - b[1]) > 0.5 * span[1] or \
            sum(1 for t in titles if b[0] <= t.x <= b[2] and b[1] <= t.y <= b[3]) >= 2
    out = []
    for th, handles in groups.items():
        t = by_handle[th]
        hs = sorted(set(handles) | {th})
        boxes = [ext[h] for h in hs if ext.get(h) and not framing(ext[h])] or \
            [ext[h] for h in hs if ext.get(h)]
        win = (min(b[0] for b in boxes), min(b[1] for b in boxes),
               max(b[2] for b in boxes), max(b[3] for b in boxes))
        out.append(View(id="", source="model region" if len(hs) >= MIN_ENTITIES else "stray",
                        layout="", window=win, handles=hs, split_from=region.id, title_hint=t.text))
    if sum(1 for v in out if v.source == "model region") < 2:
        return []
    # top to bottom, left to right, as the sheet is read
    out.sort(key=lambda v: (-round(v.window[3] / 1000.0), v.window[0]))
    for n, v in enumerate(out):
        v.id = f"{region.id}{_letters(n)}"
    return out


# "NB6:NB6", "A-A", "1-1", "2'-2'": a section cut named by the same mark at both ends,
# underlined under its drawing - the title of a section, not of the plan
_CUT = re.compile(r"^([A-Z]{0,3}\d{0,3}[A-Z]?'?)\s*[:\-]\s*([A-Z]{0,3}\d{0,3}[A-Z]?'?)$")
_STRUCT_LAYER = re.compile(r"BEAM|COL|SLAB|FOOT|FDN|RAFT|PILE|GIRDER|STR|RCC|PARDI|SHEAR|CORE", re.I)
_CURVES = ("LINE", "LWPOLYLINE", "POLYLINE", "ARC", "CIRCLE", "INSERT")
SECTION_STRUCT_MIN = 5    # lines on structural layers that make a section a drawing of members
_OWN_TO_PLAN = re.compile(r"SCHEDULE|TABLE|LEGEND|NOTE")
# a drawing titled as an elevation or a section ("SHEAR WALL ELEVATION (SW15)", "SECTION A-A"):
# taken out of the plan by its title and its lines, whatever layers they are on
_ELEV_TITLE = re.compile(r"\bELEVATIONS?\b|\bSECTIONS?\b(?!\s+(?:MARK|TYPE|SYMBOL))")
SECTION_LINES_MIN = 50


def _cut_titles(items, known: set) -> list[_Title]:
    """Underlined section-cut names ("NB6:NB6", "A-A") the title words miss."""
    out = []
    for h, _cx, _cy, e in items:
        if h in known or e.dxftype() not in ("TEXT", "MTEXT"):
            continue
        try:
            raw = e.dxf.text if e.dxftype() == "TEXT" else e.text
            t = clean(e.plain_text() if e.dxftype() == "MTEXT" else raw).upper()
        except Exception:
            continue
        m = _CUT.match(t)
        if not m or not m.group(1) or m.group(1) != m.group(2):
            continue
        if not ("%%U" in (raw or "").upper() or "\\L" in (raw or "")):
            continue
        ext = extent_of(e)
        if ext is not None:
            out.append(_Title(h, t, (ext[0] + ext[2]) / 2, (ext[1] + ext[3]) / 2, False))
    return out


# "PB4 (300X600)", "3B3A (300X600)", "B12, B14 (230X450)": a beam drawn out on its own
# (its elevation with the bars), titled by its mark and size
_MARK = r"[A-Z0-9]{0,4}-?\d{1,3}[A-Z]?"
_DETAIL = re.compile(rf"^{_MARK}(\s*[,&/]\s*{_MARK})*\s*\(\s*\d{{2,4}}\s*[X*]\s*\d{{2,4}}\s*\)$")


def _detail_titles(items, titles: list[_Title]) -> list[_Title]:
    """Beam details titled by their mark and size, which the title words miss.

    NIFT framing sheets: under the plan, PB1-PB4 drawn out with their bars, each
    titled "PB4 (300X600)" - read as plan beams. Taken only when underlined and as
    tall as the sheet's own drawing titles, so a beam tag on a plan never is one."""
    known = {t.handle for t in titles}
    ent = {h: e for h, _x, _y, e in items}
    tall = 0.0
    for t in titles:
        e = ent.get(t.handle)
        try:
            tall = max(tall, float(e.dxf.height if e.dxftype() == "TEXT" else e.dxf.char_height or 0))
        except Exception:
            continue
    if tall <= 0:
        return []
    out = []
    for h, _cx, _cy, e in items:
        if h in known or e.dxftype() not in ("TEXT", "MTEXT"):
            continue
        try:
            raw = e.dxf.text if e.dxftype() == "TEXT" else e.text
            t = clean(e.plain_text() if e.dxftype() == "MTEXT" else raw).upper()
            hgt = float(e.dxf.height if e.dxftype() == "TEXT" else e.dxf.char_height or 0)
        except Exception:
            continue
        if hgt < 0.8 * tall or not _DETAIL.match(t):
            continue
        if not ("%%U" in (raw or "").upper() or "\\L" in (raw or "")):
            continue
        ext = extent_of(e)
        if ext is not None:
            out.append(_Title(h, t, (ext[0] + ext[2]) / 2, (ext[1] + ext[3]) / 2, False, "", hgt))
    return out


def _split_plan_from_sections(region: View, items, titles: list[_Title], framed: bool = False) -> list[View]:
    """One floor's plan with its sections drawn beside it, in one model region.

    New block (2nd floor, Sasan Gir hotel): the plan, and under it three strips of
    beam sections (S-BEAM, m-col) and a slab section A-A drawn on the plan's own
    BEAM-230X600 layer - all one region, so read as one plan: 63 section lines were
    built as beams and the sections' columns as walls.

    The region is cut into pieces with space round them, each going to its title.
    Only a section that draws members (lines on structural layers) is taken out of
    the plan as its own view; everything else - the plan's notes, legends, steel
    schedule, title block - stays with the plan, which reads them as before. A
    region with no such section is returned as it was ([])."""
    known = {t.handle for t in titles}
    titles = titles + _cut_titles(items, known) + (_detail_titles(items, titles) if framed else [])
    if not any(not t.is_plan for t in titles):
        return []
    title_handles = {t.handle for t in titles}
    body = [it for it in items if it[0] not in title_handles]
    span = region.size
    plan_titles = [t for t in titles if t.is_plan]
    groups: dict[str, list[str]] = defaultdict(list)
    item_of = {it[0]: it for it in body}
    orphans = []                    # pieces with no title of their own: (owner, handles, box)
    biggest = {}                    # title -> (entities, box) of its largest piece

    def assign(part, level):
        for handles, box in _pieces(part, SUB_CELL_MM[level], span, titles):
            inside = [t for t in titles if box[0] <= t.x <= box[2] and box[1] <= t.y <= box[3]]
            plans_in = [t for t in inside if t.is_plan]
            sections_in = [t for t in inside if not t.is_plan and not _OWN_TO_PLAN.search(t.text)]
            # the plan's title right under (or over) this piece: NIFT level 3 is titled
            # under the plan, with a small section A-A touching its right end - the piece
            # held only the section's title and the whole plan went to the section
            near = max(2000.0, 0.15 * (box[3] - box[1]))
            lined = [t for t in plan_titles if t not in plans_in and box[0] <= t.x <= box[2]
                     and (0.0 <= box[1] - t.y <= near or 0.0 <= t.y - box[3] <= near)] if framed else []
            if (plans_in or lined) and sections_in:
                if level + 1 < len(SUB_CELL_MM):
                    # a section still touching the plan (a frame line 600 mm off it):
                    # only this piece is cut again, with a finer gap
                    assign([item_of[h] for h in handles if h in item_of], level + 1)
                    continue
                plans_in = plans_in or lined    # they really touch: the plan keeps it, as before
                if framed:
                    # NIFT level 4: sections A-A and B-B drawn against the end of the plan's grid
                    # lines - what sits right over a section's own title is that section's
                    owner = _owner(box, plans_in, titles)
                    for h in handles:
                        it = item_of.get(h)
                        cx, cy = (it[1], it[2]) if it else (box[0], box[1])
                        near = [t for t in sections_in if t.height > 0 and abs(cx - t.x) <= 6 * t.height
                                and 0.0 <= cy - t.y <= 8 * t.height]
                        t = min(near, key=lambda t: math.hypot(cx - t.x, cy - t.y)) if near else owner
                        groups[t.handle].append(h)
                    continue
            if plans_in:
                owner = _owner(box, plans_in, titles)
            elif sections_in:
                owner = sections_in[0]          # a strip of sections: its own title(s)
            elif inside:
                owner = _owner(box, [], plan_titles)    # a schedule or legend: the plan's, as before
            else:
                owner = _owner(box, [], titles)
                orphans.append((owner.handle, handles, box))
            groups[owner.handle] += handles
            if len(handles) > biggest.get(owner.handle, (0, None))[0]:
                biggest[owner.handle] = (len(handles), box)     # each title's largest piece
    assign(body, 0)
    by_handle = {t.handle: t for t in titles}
    plan_groups = [h for h in groups if by_handle[h].is_plan]
    if not plan_groups:
        return []
    main = max(plan_groups, key=lambda h: len(groups[h]))
    ent = {h: e for h, _x, _y, e in items}
    # a piece with no title of its own lying inside the plan belongs to the plan, not to
    # whichever title is nearest: ALCAZAR 1st floor's beam schedule is titled right under
    # the plan's upper wing, and the slab marks in that wing (with their oval frames on
    # 'SLAB MARK') went to it - taken for a section, out of the plan: 6 bays lost their S1
    orphan_hs = {h for _o, hs, _b in orphans for h in hs}
    core = [extent_of(ent[h]) for h in groups[main] if h not in orphan_hs and ent.get(h) is not None
            and ent[h].dxftype() in _CURVES and _STRUCT_LAYER.search(ent[h].dxf.layer or "")]
    # (a frame round the whole sheet is large both ways: not the plan's own extent)
    core = [b for b in core if b is not None and not ((b[2] - b[0]) > 0.9 * span[0] and (b[3] - b[1]) > 0.9 * span[1])]
    if core:
        px0, py0 = min(b[0] for b in core), min(b[1] for b in core)
        px1, py1 = max(b[2] for b in core), max(b[3] for b in core)
        for owner, hs, box in orphans:
            t = by_handle[owner]
            if framed and not t.is_plan and t.height > 0 and box[0] - t.height <= t.x <= box[2] + t.height \
                    and 0.0 <= box[1] - t.y <= 3 * t.height:
                # sitting right on its own title (NIFT level 4: section A-A drawn inside the
                # plan's outer box, by its top-right wing): that section's, not the plan's
                continue
            if owner != main and not t.is_plan and \
                    px0 <= box[0] and box[2] <= px1 and py0 <= box[1] and box[3] <= py1:
                moved = set(hs)
                groups[owner] = [h for h in groups[owner] if h not in moved]
                groups[main] += hs
    if framed:
        _off_the_frame(groups, main, by_handle, {it[0]: it for it in items}, span)

    def draws_members(hs):
        return sum(1 for h in hs if ent.get(h) is not None and ent[h].dxftype() in _CURVES
                   and _STRUCT_LAYER.search(ent[h].dxf.layer or "")) >= SECTION_STRUCT_MIN

    def drawn_over_its_title(th, hs):
        # a drawing titled as an elevation or a section, sitting right over its title, of many
        # lines whatever their layers: Akanistha's "SHEAR WALL ELEVATION (SW9 & SW10)" beside
        # the third-floor plan, 7799 lines on 'HATCH 1 AVADH' - the layer the plan's shear-wall
        # hatches use, so the elevation was built as tall walls
        t = by_handle[th]
        if not _ELEV_TITLE.search(t.text) or _OWN_TO_PLAN.search(t.text) or t.height <= 0 or th not in biggest:
            return False
        if sum(1 for h in hs if ent.get(h) is not None and ent[h].dxftype() in _CURVES) < SECTION_LINES_MIN:
            return False
        # its largest piece (not scraps that went to the nearest title) stands right on the title
        bx0, by0, bx1, _by1 = biggest[th][1]
        return bx0 - t.height <= t.x <= bx1 + t.height and -t.height <= by0 - t.y <= 3 * t.height
    plan_hs = list(groups[main]) + [main]
    sections = []
    for th, hs in groups.items():
        if th == main:
            continue
        if by_handle[th].is_plan or not (draws_members(hs) or drawn_over_its_title(th, hs)):
            plan_hs += list(hs) + [th]          # the title block, notes, schedules: as before
        else:
            sections.append((th, list(hs) + [th]))
    if not sections:
        return []
    ext = {h: extent_of(e) for h, _x, _y, e in items}

    def framing(b):
        return (b[2] - b[0]) > 0.5 * span[0] or (b[3] - b[1]) > 0.5 * span[1] or \
            sum(1 for t in titles if b[0] <= t.x <= b[2] and b[1] <= t.y <= b[3]) >= 2

    def view(th, hs):
        hs = sorted(set(hs))
        boxes = [ext[h] for h in hs if ext.get(h) and not framing(ext[h])] or [ext[h] for h in hs if ext.get(h)]
        win = (min(b[0] for b in boxes), min(b[1] for b in boxes),
               max(b[2] for b in boxes), max(b[3] for b in boxes))
        return View(id="", source="model region" if len(hs) >= MIN_ENTITIES else "stray", layout="",
                    window=win, handles=hs, split_from=region.id, title_hint=by_handle[th].text)
    out = [view(main, plan_hs)] + [view(th, hs) for th, hs in sections]
    out.sort(key=lambda v: (-round(v.window[3] / 1000.0), v.window[0]))
    for n, v in enumerate(out):
        v.id = f"{region.id}{_letters(n)}"
    return out


OFF_FRAME_MM = 1500.0     # members this far clear of the plan's own frame can be a section's


def _off_the_frame(groups, main: str, by_handle, item_of, span) -> None:
    """Members given to the plan that lie clear of the plan's own frame, beside a
    section's drawing, are that section's (framed sheets only).

    NIFT level 4: SECTION B-B is titled under it and stands 7 m tall, its top - the
    stringer beam on its bearing pad - 2 m clear of the plan's right end; those beam
    lines went to the plan and were built. The plan is one connected frame of members:
    its biggest cluster of lines on structural layers. A smaller cluster more than
    OFF_FRAME_MM clear of it, and nearer a section's members than the frame, goes to
    that section. Anything within the frame's box stays with the plan."""
    def struct(h):
        it = item_of.get(h)
        e = it[3] if it else None
        return e is not None and e.dxftype() in _CURVES and bool(_STRUCT_LAYER.search(e.dxf.layer or ""))

    def box_of(hs):
        bs = [extent_of(item_of[h][3]) for h in hs]
        bs = [b for b in bs if b is not None and not ((b[2] - b[0]) > 0.5 * span[0] or (b[3] - b[1]) > 0.5 * span[1])]
        if not bs:
            return None
        return (min(b[0] for b in bs), min(b[1] for b in bs), max(b[2] for b in bs), max(b[3] for b in bs))

    def gap(a, b):
        return math.hypot(max(a[0] - b[2], 0.0, b[0] - a[2]), max(a[1] - b[3], 0.0, b[1] - a[3]))

    secs = {}
    for th, hs in groups.items():
        if th != main and not by_handle[th].is_plan:
            b = box_of([h for h in hs if struct(h)])
            if b is not None:
                secs[th] = b
    if not secs:
        return
    mine = [item_of[h] for h in groups[main] if struct(h)]
    clusters = _pieces(mine, SUB_CELL_MM[0], span)
    if len(clusters) < 2:
        return
    frame = max(clusters, key=lambda c: len(c[0]))
    fbox = frame[1]
    for hs, box in clusters:
        if hs is frame[0]:
            continue
        to_frame = gap(box, fbox)
        if to_frame <= OFF_FRAME_MM:
            continue
        th, to_sec = min(((th, gap(box, b)) for th, b in secs.items()), key=lambda x: x[1])
        if to_sec < to_frame and to_sec <= OFF_FRAME_MM:
            moved = set(hs)
            groups[main] = [h for h in groups[main] if h not in moved]
            groups[th] += hs


def _letters(n: int) -> str:
    s = ""
    n += 1
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(97 + r) + s
    return s


def _mark_overviews(views: list[View]) -> None:
    """A viewport showing the whole site (or two other views at once) is an
    overview: useful to a person, but reading it would read everything twice."""
    for v in views:
        inside = sum(1 for o in views if o is not v and o.source == "viewport"
                     and v.contains((o.window[0] + o.window[2]) / 2, (o.window[1] + o.window[3]) / 2)
                     and o.size[0] * o.size[1] < v.size[0] * v.size[1])
        if v.scale >= OVERVIEW_SCALE or inside >= 2:
            v.source = "overview"


_GRID_TEXT = re.compile(r"^(?:[A-Z]{1,2}|\d{1,2})'?$")
_REINF_LAYER = re.compile(r"REINF|REBAR|\bBARS?\b", re.I)
TWIN_MIN_MM = 5000.0


def _grid_labels(items) -> list[tuple[str, float, float]]:
    out = []
    for _h, _x, _y, e in items:
        t = e.dxftype()
        try:
            if t in ("TEXT", "MTEXT"):
                s = clean(e.plain_text() if t == "MTEXT" else e.dxf.text).upper().strip()
                p = e.dxf.insert
                if _GRID_TEXT.match(s):
                    out.append((s, p[0], p[1]))
            elif t == "INSERT":
                for a in e.attribs:
                    s = clean(a.dxf.text or "").upper().strip()
                    if _GRID_TEXT.match(s):
                        out.append((s, a.dxf.insert[0], a.dxf.insert[1]))
        except Exception:
            continue
    return out


def _split_twins(v: View, items) -> list[View]:
    """Two drawings of the same floor side by side in one view: [plan, reinforcement] or [].

    Pirgal draws each floor's framing plan and, beside it in the same sheet window, the same
    floor again with its slab bars; read as one floor, every column and wall came twice. The
    sign is the grid: its numbered AND its lettered labels all repeat shifted by one same
    distance (a grid labelled at both ends repeats its numbers one way and its letters the
    other, so it never qualifies). The copy drawn with reinforcement layers is the slab
    reinforcement drawing; without a clear one, nothing is split."""
    labels = _grid_labels(items)
    if len(labels) < 12:
        return []
    by = defaultdict(list)
    for s, x, y in labels:
        by[s].append((x, y))
    votes = defaultdict(set)
    for s, ps in by.items():
        for i, p in enumerate(ps):
            for q in ps[i + 1:]:
                d = (round((q[0] - p[0]) / 100.0), round((q[1] - p[1]) / 100.0))
                if d[0] < 0 or (d[0] == 0 and d[1] < 0):
                    d = (-d[0], -d[1])
                votes[d].add(s)
    # shifts within 200 mm of each other are one shift (Pirgal 2020-11-04's third floor: its
    # letters say 14.1 m, its numbers 14.0 m - in 100 mm steps two shifts, neither with both)
    support = {d: set().union(*(votes[k] for k in votes if abs(k[0] - d[0]) <= 2 and abs(k[1] - d[1]) <= 2))
               for d in votes}
    best = None
    for d, ss in support.items():
        L = math.hypot(d[0], d[1]) * 100.0
        digits = sum(1 for s in ss if s[0].isdigit())
        if L >= TWIN_MIN_MM and digits >= 3 and len(ss) - digits >= 3 and \
                (best is None or len(ss) > len(support[best])):
            best = d
    if best is None:
        return []
    votes = support
    ox, oy = best[0] * 100.0, best[1] * 100.0
    L = math.hypot(ox, oy)
    ux, uy = ox / L, oy / L
    proj = []
    for h, _x, _y, e in items:
        ext = extent_of(e)
        if ext is None:
            continue
        cs = [(ext[0], ext[1]), (ext[2], ext[1]), (ext[0], ext[3]), (ext[2], ext[3])]
        ps = [x * ux + y * uy for x, y in cs]
        proj.append((h, min(ps), max(ps), e))
    a0 = min(x * ux + y * uy for s, x, y in labels if s in votes[best])
    lo, hi = a0 + 0.3 * L, a0 + 1.1 * L
    # the cut between the two copies: where the fewest drawn things cross
    t = min((lo + (hi - lo) * k / 100.0 for k in range(101)),
            key=lambda t: sum(1 for _h, p0, p1, _e in proj if p0 < t < p1 and p1 - p0 < 0.9 * L))
    a = [(h, e) for h, p0, p1, e in proj if (p0 + p1) / 2 < t]
    b = [(h, e) for h, p0, p1, e in proj if (p0 + p1) / 2 >= t]
    ra = sum(1 for _h, e in a if _REINF_LAYER.search(e.dxf.layer or ""))
    rb = sum(1 for _h, e in b if _REINF_LAYER.search(e.dxf.layer or ""))
    if max(ra, rb) < 20 or max(ra, rb) < 2 * max(min(ra, rb), 1):
        return []
    plan_part, reinf_part = (a, b) if ra < rb else (b, a)
    shift = (-ox, -oy) if ra < rb else (ox, oy)        # from the reinforcement copy onto the plan
    marks_p = [h for h, e in plan_part if _slab_mark(e)]
    borrow = [h for h, e in reinf_part if _slab_mark(e)] if not marks_p else []
    out = []
    for part, kind in ((a, "plan" if ra < rb else "reinforcement"), (b, "reinforcement" if ra < rb else "plan")):
        exts = [extent_of(e) for _h, e in part]
        exts = [x for x in exts if x is not None]
        if len(part) < MIN_ENTITIES or not exts:
            return []
        out.append(View(id=f"{v.id}{'p' if kind == 'plan' else 'r'}", source=v.source, layout=v.layout,
                        window=(min(x[0] for x in exts), min(x[1] for x in exts), max(x[2] for x in exts),
                                max(x[3] for x in exts)),
                        scale=v.scale, paper=v.paper, handles=sorted(h for h, _e in part), split_from=v.id,
                        title_hint=v.title_hint, twin=kind, whole=v,
                        borrow=borrow if kind == "plan" else [], borrow_shift=shift if kind == "plan" else (0.0, 0.0)))
    return out


_SLAB_MARK_TEXT = re.compile(r"^S\s*-?\s*\d{1,3}[A-Z]?$")


def _slab_mark(e) -> bool:
    """A slab's mark: on a slab-mark layer, or a text / block attribute reading S1, S2A ..."""
    layer = (e.dxf.get("layer", "") or "").upper()
    if re.search(r"SLAB", layer) and re.search(r"MARK|\bNO\b|TAG|NUM", layer):
        return True
    t = e.dxftype()
    try:
        if t in ("TEXT", "MTEXT"):
            return bool(_SLAB_MARK_TEXT.match(clean(e.plain_text() if t == "MTEXT" else e.dxf.text).upper().strip()))
        if t == "INSERT":
            return any(_SLAB_MARK_TEXT.match(clean(a.dxf.text or "").upper().strip()) for a in e.attribs)
    except Exception:
        return False
    return False


def _like_a_sheet(region: View, its, floor_titles) -> list[View]:
    """A model region laid out as NIFT's framing sheets are - beams drawn out under their
    mark and size ("PB4 (300X600)"), or two floors' plans one above the other - split as
    such a sheet's viewport is (framed). NIFT 2's issue of "LEVEL-1 & 2" has no paper
    sheet: level 1 took half of level 2's beams, level 2 three sections' beams. As for a
    viewport, a split whose biggest drawing is no plan is not used (the staircase sheet).
    Anything else: [] - the region is read exactly as before."""
    titles = _drawing_titles(its)
    plans = [t for t in titles if t.is_plan]
    if not plans:
        return []
    stacked = len({t.level for t in plans}) >= 2 and _stacked(plans)
    if not stacked and not _detail_titles(its, titles):
        return []
    parts = split_by_titles(region, its, floor_titles, framed=True)
    plan_texts = {t.text for t in plans}
    if parts and max(parts, key=lambda p: len(p.handles)).title_hint not in plan_texts:
        return []
    return parts


def find_views(doc) -> list[View]:
    """Every view in the drawing, with the model entities each one shows."""
    items = _index(doc.modelspace())
    views = []
    for v in _viewports(doc):
        v.handles = [h for h, x, y, _ in items if v.contains(x, y)]
        if len(v.handles) < MIN_ENTITIES:
            continue
        if any(o.layout == v.layout and _same(o.window, v.window) for o in views):
            continue                                  # shown twice on one layout
        twin = next((o for o in views if _same(o.window, v.window)), None)
        if twin is not None:
            v.same_as = twin.id
        views.append(v)

    if not views:
        regions = _regions(items, "model region", "M")
        by_handle = {h: (h, x, y, e) for h, x, y, e in items}
        # floors named by their floor alone count only in a drawing naming two or more
        floors = _floor_titles(items)
        if len({t.level for t in floors}) < 2:
            floors = []
        out = []
        for r in regions:
            if r.inside_blocks:
                out.append(r)                       # one section or detail, kept whole
                continue
            its = [by_handle[h] for h in r.handles if h in by_handle]
            mine = [t for t in floors if t.handle in set(r.handles)]
            parts = _like_a_sheet(r, its, mine) if r.source == "model region" else []
            if not parts:
                parts = split_by_titles(r, its, mine) if r.source == "model region" else []
            if not parts and len(mine) == 1 and not any(t.is_plan for t in _drawing_titles(its)):
                r.title_hint = mine[0].text         # its one floor name is its title
            if not parts and r.source == "model region":
                parts = _split_twins(r, its)
            out += parts or [r]
        return out
    _mark_overviews(views)

    shown = {h for v in views if v.source == "viewport" for h in v.handles}
    rest = [it for it in items if it[0] not in shown]
    # one viewport framing a whole sheet - two floors' plans, their sections and beam
    # details (NIFT "FRAMING PLAN & BEAM DETAILS AT LEVEL-1 & 2") - is split by its
    # titles exactly as a sheet drawn in model space is. Only a viewport holding plans
    # of two floors, or beams drawn out under their mark and size ("PB4 (300X600)"):
    # any other viewport (a foundation sheet with its footing details) is kept whole
    by_handle = {it[0]: it for it in items}
    out = []
    for v in views:
        parts = []
        if v.source == "viewport" and not v.same_as:
            its = [by_handle[h] for h in v.handles if h in by_handle]
            titles = _drawing_titles(its)
            if len({t.level for t in titles if t.is_plan}) >= 2 or \
                    (any(t.is_plan for t in titles) and _detail_titles(its, titles)):
                parts = split_by_titles(v, its, framed=True)
                plan_texts = {t.text for t in titles if t.is_plan}
                if parts and max(parts, key=lambda p: len(p.handles)).title_hint not in plan_texts:
                    # its biggest drawing is no plan (NIFT staircase sheet: sections and
                    # a small PART PLAN): a details sheet, kept whole as before
                    parts = []
        for p in parts:
            p.source = "viewport" if p.source == "model region" else p.source
            p.layout, p.scale, p.paper = v.layout, v.scale, v.paper
        if not parts and v.source == "viewport" and not v.same_as:
            parts = _split_twins(v, [by_handle[h] for h in v.handles if h in by_handle])
        out += parts or [v]
    return out + _regions(rest, "unplaced region", "U")
