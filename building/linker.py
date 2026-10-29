"""The linker: every source about an element, brought together and weighed.

Readers each report what they found, with evidence, and never look at each
other. The linker connects them:

1. Plan captions attach to the element they sit on, on the plan they were read
   from: a beam caption to the nearest beam centreline it lies along, a column
   caption to the column it labels, a slab caption to the panel it is inside.
   When two beams are almost equally close, the caption is left unattached and
   reported as ambiguous - never guessed.
2. Specs (from tables, detail captions, section dimensions) attach by mark, within
   the same wing and level. A spec from another wing or building part ("WING C
   REVISED", "CLUB HOUSE") never applies.
3. Each property is resolved from all its sources: agreeing values raise the
   confidence; disagreeing values make a CONFLICT (all kept, the strongest shown);
   no value makes it MISSING. Every source stays in the evidence.
4. Project overrides (decisions from the review screen) are applied last and
   always win, marked OVERRIDE.

The results go in Element.resolved; what each reader read stays in props.
"""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass

from . import levels_table, merge_beams, prefixes, slab_panels, walls
from .merge_beams import gone
from .marks import parse_mark
from .readers.column_schedule import in_range
from .sheets.levels import level_rank
from .model import (Building, Element, Evidence, Issue, Value, CONFLICT, MISSING, OVERRIDE, READ,
                    WARNING, INFO)

READER = "linker"

# how much each kind of source is trusted
WEIGHT = {
    "override": 10.0,
    "schedule table": 1.0,
    "detail caption": 0.95,
    "plan caption": 0.9,
    "column layout": 0.9,
    "old extractor schedule": 0.8,
    "slab panel": 0.75,           # the bay's panel is marked; only secondary beams between
    "beam run": 0.75,             # the beam it carries on (no column between) is marked
    "beam line": 0.75,            # the same mark both ways along its straight beam line
    "beam detail span": 0.75,     # in line with a marked beam, within the grids its detail spans
    "section dimensions": 0.7,
    "plan geometry": 0.7,
    "layer name": 0.6,
    "general note": 0.5,          # "ALL SLABS ARE 125mm THK." - only where nothing else says
}
REQUIRED = {"beam": ("width", "depth"), "beam_span": ("width", "depth"),
            "column": ("width", "depth"), "slab": ("thickness",),
            "footing": ("width", "length", "depth", "founding_level"), "raft": ("thickness", "founding_level")}
# founding level: the bottom of a footing or raft, in mm against the drawings' level
# datum (as the level table). Read from a footing schedule's level column, or decided
# in the review - per footing, or once for all (project "founding_level_mm"). Never
# assumed: a footing without one is not built.
FOUNDING = "founding_level"
FAMILY_OF_TYPE = {"beam": "beam", "beam_span": "beam", "column": "column", "slab": "slab", "wall": "wall",
                  "footing": "footing", "pedestal": "pedestal", "raft": "raft", "stair": "stair", "ramp": "ramp"}
SIZE_PROPS = ("width", "depth", "thickness", "length", "depth_edge")


@dataclass
class Candidate:
    value: object
    kind: str                     # a WEIGHT key
    evidence: list[Evidence]
    note: str = ""
    pair: str = ""                # width and depth given together (one spec): compared as a pair

    @property
    def weight(self) -> float:
        return WEIGHT.get(self.kind, 0.5)


# ---------------------------------------------------------------- geometry
def _seg_dist(p, a, b) -> tuple[float, float]:
    """Distance from p to segment ab, and where along it (0..1) p projects."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    L2 = dx * dx + dy * dy
    if L2 == 0:
        return math.hypot(p[0] - a[0], p[1] - a[1]), 0.0
    t = ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / L2
    tc = max(0.0, min(1.0, t))
    return math.hypot(p[0] - (a[0] + tc * dx), p[1] - (a[1] + tc * dy)), t


def _beam_dist(p, el) -> tuple[float, bool]:
    g = el.geometry
    pts = [g["start"], g["mid"], g["end"]] if g.get("mid") else [g["start"], g["end"]]
    best, inside = None, False
    for a, b in zip(pts, pts[1:]):
        d, t = _seg_dist(p, a, b)
        if best is None or d < best:
            best, inside = d, -0.05 <= t <= 1.05
    # measured to the beam's face, not its centreline: a caption written on a
    # 1500-wide girder is 700 mm from its centreline but on the girder itself
    half = 0.5 * float(el.get("width") or 0.0)
    return max(0.0, best - half), inside


def _split(b: Building, beam, caps, neighbours):
    """Split a beam carrying captions of several marks into one span per mark.

    Sasan Gir marks every span of a continuous beam line (B3a, B3b, B10e, B10f)
    while the plan reader joins the line into one beam. The cut between two
    marks is made where the beam is supported between their captions - a
    crossing beam or a column - and, only if there is none, midway between the
    captions (then flagged). Returns [(span element, its captions)] or None.
    """
    from .model import Element
    a, e = beam.geometry["start"], beam.geometry["end"]
    dx, dy = e[0] - a[0], e[1] - a[1]
    L2 = dx * dx + dy * dy
    if L2 <= 0:
        return None
    L = math.sqrt(L2)
    ux, uy = dx / L, dy / L

    def t_of(p):
        return ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / L2

    order = sorted(caps, key=lambda c: t_of(c.geometry["at"]))
    groups = []
    for c in order:
        if groups and groups[-1][0].mark == c.mark:
            groups[-1].append(c)
        else:
            groups.append([c])
    if len(groups) < 2:
        return None

    # where the beam is supported along its length
    reach = 0.5 * float(beam.get("width") or 230.0) + 300.0
    supports = []
    for o in neighbours:
        if o is beam:
            continue
        if o.type == "beam" and not o.geometry.get("mid"):
            p, q = o.geometry["start"], o.geometry["end"]
            ex, ey = q[0] - p[0], q[1] - p[1]
            den = dx * ey - dy * ex
            if abs(den) < 1e-9 * L * math.hypot(ex, ey):
                continue                                       # parallel
            s = ((p[0] - a[0]) * ey - (p[1] - a[1]) * ex) / den     # along this beam
            r = ((p[0] - a[0]) * dy - (p[1] - a[1]) * dx) / den     # along the other
            m = math.hypot(ex, ey)
            if 0.0 < s < 1.0 and -reach / m <= r <= 1.0 + reach / m:
                supports.append((s, f"crossing beam {o.mark or o.id.rsplit('/', 1)[-1]}"))
        elif o.type == "column" and o.geometry.get("x") is not None:
            c = (o.geometry["x"], o.geometry["y"])
            off = abs((c[0] - a[0]) * -uy + (c[1] - a[1]) * ux)
            s = t_of(c)
            if 0.0 < s < 1.0 and off <= reach + 0.5 * float(o.get("depth") or 0):
                supports.append((s, f"column {o.mark or ''}".strip()))

    cuts = []
    for g1, g2 in zip(groups, groups[1:]):
        lo, hi = t_of(g1[-1].geometry["at"]), t_of(g2[0].geometry["at"])
        between = [s for s in supports if lo < s[0] < hi]
        if between:
            mid = (lo + hi) / 2
            cuts.append(min(between, key=lambda s: abs(s[0] - mid)))
        else:
            cuts.append(((lo + hi) / 2, "midway between captions - no support found, check"))

    out, edges = [], [0.0] + [c[0] for c in cuts] + [1.0]
    for i, g in enumerate(groups):
        t0, t1 = edges[i], edges[i + 1]
        span = Element(
            id=f"{beam.id}/span{i}", type="beam_span", sheet=beam.sheet, level=beam.level,
            wing=beam.wing, mark=g[0].mark,
            geometry={"start": [a[0] + t0 * dx, a[1] + t0 * dy], "end": [a[0] + t1 * dx, a[1] + t1 * dy]},
            props={k: v for k, v in beam.props.items() if k in ("width", "kind")},
            extra={"parent": beam.id, "_layer": beam.extra.get("_layer", ""),
                   "cut_before": cuts[i - 1][1] if i > 0 else "",
                   "cut_after": cuts[i][1] if i < len(cuts) else ""})
        b.add(span)
        out.append((span, g))
    beam.extra["split_into"] = [s.id for s, _ in out]
    guessed = [c[1] for c in cuts if c[1].startswith("midway")]
    if guessed:
        b.issues.append(Issue(WARNING, "span split", f"beam {beam.id.rsplit('/', 1)[-1]} split into "
                              f"{len(out)} spans ({', '.join(s.mark for s, _ in out)}); {len(guessed)} cut(s) "
                              "made midway - no crossing beam or column found", sheet=beam.sheet,
                              elements=[beam.id] + [s.id for s, _ in out], extra={"from_linker": True}))
    return out


def _set_aside_stacked(b: Building, beam, caps):
    """Captions of different marks written at the same spot on a beam (New block:
    'NB2' and 'NB1' at one point - a drafting slip) name no span each: they must not
    split the beam. The beam's own tag is kept; the other marks are set aside and
    shown for review. Returns the captions kept."""
    keep, own = list(caps), _key_of(beam.mark) if beam.mark else ""
    for c in caps:
        th = max(c.geometry.get("text_height") or 100.0, 1.0)
        here = [o for o in caps if math.dist(o.geometry["at"], c.geometry["at"]) <= 0.5 * th]
        if len({o.mark for o in here}) < 2 or c not in keep:
            continue
        drop = [o for o in here if not own or _key_of(o.mark) != own]
        keep = [o for o in keep if o not in drop]
        names = " and ".join(sorted({f"'{o.mark}'" for o in here}))
        kept = f"the beam keeps its own tag {beam.mark}" if own and len(drop) < len(here) \
            else "none is used - set the mark in Review"
        b.issues.append(Issue(WARNING, "caption", f"captions {names} are written at the same spot on beam "
                              f"{beam.id.rsplit('/', 1)[-1]}; {kept}. Check the drawing", sheet=beam.sheet,
                              elements=[beam.id] + [o.id for o in here], mark=c.mark, at=list(c.geometry["at"]),
                              extra={"from_linker": True}))
    return keep


def _parallel(rot_deg: float, el, tol: float = 20.0) -> bool:
    """Does a text at this rotation run along the beam (either way round)?"""
    g = el.geometry
    a, b = g["start"], g["end"]
    ang = math.degrees(math.atan2(b[1] - a[1], b[0] - a[0]))
    diff = abs((rot_deg - ang + 90.0) % 180.0 - 90.0)
    return diff <= tol or g.get("mid") is not None       # a curved beam: any direction


def _inside(p, poly) -> bool:
    x, y = p
    hit = False
    for i in range(len(poly)):
        x1, y1 = poly[i][0], poly[i][1]
        x2, y2 = poly[i - 1][0], poly[i - 1][1]
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            hit = not hit
    return hit


def _outline_dist(p, poly) -> float:
    """Distance from a point to the edges of an outline."""
    pts = [tuple(q) for q in poly]
    return min(_seg_dist(p, a, c)[0] for a, c in zip(pts, pts[1:] + pts[:1]))


def _column_dist(p, el) -> float:
    g = el.geometry
    c = (g.get("x"), g.get("y"))
    if c[0] is None:
        return 1e18
    outline = g.get("outline") or []
    if outline and _inside(p, outline):
        return 0.0
    half = 0.5 * max(el.get("width") or 0, el.get("depth") or 0)
    return max(0.0, math.hypot(p[0] - c[0], p[1] - c[1]) - half)


# ---------------------------------------------------------------- the linker
def _key_of(mark: str) -> str:
    mk = parse_mark((mark or "").split("/")[0]) if mark else None
    return mk.key if mk else loose_key(mark)


def loose_key(mark: str) -> str:
    """S-1, S 1, s1 and S–1 are one mark (S1); a separator between two numbers is
    kept, as it means something: S1-2 is not S12. Leading zeros are kept too: S01
    and S1 are only suggested to Review as the same, never matched here."""
    s = re.sub(r"(?<=\d)\s*[-–—.]\s*(?=\d)", "~", (mark or "").upper())
    return re.sub(r"[^A-Z0-9~]", "", s).replace("~", "-")


def _twins_of(b, sid: str) -> tuple:
    """The other drawing of the same floor beside this plan in its sheet window (Pirgal: the
    framing plan and its slab reinforcement drawing): its general notes are the floor's too."""
    s = b.sheets.get(sid)
    v = (s.extra.get("view") or {}) if s else {}
    if v.get("twin") != "plan":
        return ()
    return tuple(o for o, t in b.sheets.items() if o != sid and t.file == s.file
                 and (t.extra.get("view") or {}).get("twin") == "reinforcement"
                 and (t.extra.get("view") or {}).get("split_from") == v.get("split_from"))


def _shift(src: list[tuple[float, float]], dst: list[tuple[float, float]],
           tol: float = 200.0) -> tuple[tuple[float, float], int] | None:
    """The translation that lays most of src onto dst, found by voting.

    Two drawings of one building need not share an origin; the columns, which
    stand in the same places on every sheet, say how far apart the origins are.
    Returns (dx, dy) and how many points it matches, or None if no shift
    matches enough of them to be believed.
    """
    if not src or not dst:
        return None
    votes: dict[tuple[int, int], int] = defaultdict(int)
    step = 100.0
    for p in src[:80]:
        for q in dst:
            votes[(round((q[0] - p[0]) / step), round((q[1] - p[1]) / step))] += 1
    tried = []
    for (i, j), _n in sorted(votes.items(), key=lambda kv: -kv[1])[:8]:
        dx, dy = i * step, j * step
        hits = sum(1 for p in src if any(abs(p[0] + dx - q[0]) <= tol and abs(p[1] + dy - q[1]) <= tol
                                         for q in dst))
        tried.append(((dx, dy), hits))
    tried.sort(key=lambda t: -t[1])
    if not tried:
        return None
    best = tried[0]
    # the runner-up: the best shift not just a step away from the winner
    rival = next((t[1] for t in tried[1:] if abs(t[0][0] - best[0][0]) > 2 * tol
                  or abs(t[0][1] - best[0][1]) > 2 * tol), 0)
    # Believed when it lines up enough columns and clearly beats any other shift.
    # On a regular grid a shift of one bay also lines up most columns, but always
    # misses a whole row: the right shift wins by at least that row.
    if best[1] >= max(6, 0.25 * len(src)) and best[1] - rival >= max(2, 0.1 * len(src)):
        return best
    return None


def _layout_marks(b: Building, stats) -> dict[str, tuple[str, Evidence]]:
    """Plan columns without marks, given the mark of the column at the same place
    on the column layout (shifted if the two drawings' origins differ)."""
    out: dict[str, tuple[str, Evidence]] = {}
    positions = b.of("column_position")
    if not positions:
        return out
    # Every column is placed in one frame per drawing set - the column layout's.
    # The layout's own marked columns go in first; each plan that lines up with
    # what is already placed is shifted into the frame and its columns added,
    # so a floor carrying only some of the columns is matched against the floor
    # below it, not against the whole foundation (columns stop at upper floors).
    placed: dict[str, list] = defaultdict(list)       # set -> [(x, y, mark, evidence, element id)]
    labels: dict[str, list] = defaultdict(list)       # marks beside columns: name, never place
    odd: dict[str, list] = defaultdict(list)          # set -> [(x, y, wall element)]
    for p in positions:
        if p.extra.get("label_only"):
            labels[_set_of(b, p)].append(p)
            continue
        placed[_set_of(b, p)].append((p.geometry["x"], p.geometry["y"], p.mark,
                                      p.props["mark"].evidence[0], None))
    pending = [sid for sid, s in b.sheets.items() if s.reader == "plan.legacy"]
    progress = True
    while pending and progress:
        progress = False
        for sid in list(pending):
            s = b.sheets[sid]
            cols = [e for e in b.elements if e.sheet == sid and e.type == "column"
                    and e.geometry.get("x") is not None]
            key = s.extra.get("set", "")
            if not cols or not placed.get(key):
                pending.remove(sid)
                continue
            src = [(e.geometry["x"], e.geometry["y"]) for e in cols]
            found = _shift(src, [(r[0], r[1]) for r in placed[key]])
            if found is None:
                continue
            (dx, dy), hits = found
            s.extra["column_layout_shift"] = [dx, dy, hits]
            pending.remove(sid)
            progress = True
            for e, (x, y) in zip(cols, src):
                placed[key].append((x + dx, y + dy, None, None, e.id))
            # columns drawn as odd shapes (L, skewed) that the plan reader took for
            # walls: put in the frame with the plan's shift, never used to find it,
            # and marked after the columns so they cannot change a column's mark
            for w in b.elements:
                if w.sheet == sid and w.type == "wall" and w.extra.get("column_like"):
                    cl = w.extra["column_like"]
                    odd[key].append((cl["x"] + dx, cl["y"] + dy, w))

    # what each drawing set's column schedule says: mark -> [(level range, sorted size)]
    sched: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for s in b.of("spec"):
        if s.extra.get("level_range") and s.get("width") and s.get("depth"):
            sched[_set_of(b, s)][s.mark].append((s.extra["level_range"],
                                                 tuple(sorted((s.get("width"), s.get("depth"))))))

    def from_labels(key, cx, cy, els):
        """The column mark written beside this spot: (label, source) or None."""
        near = [(math.hypot(lb.geometry["x"] - cx, lb.geometry["y"] - cy), lb) for lb in labels.get(key, [])]
        near.sort(key=lambda x: x[0])
        close = [x for x in near if x[0] <= 1200.0]
        if close and not (len(close) > 1 and close[1][0] < 1.2 * close[0][0]
                          and close[1][1].mark != close[0][1].mark):
            return close[0][1], "column layout"
        if sched.get(key):
            # Sasan Gir's tags sit up to ~2.5 m from their column. Further out,
            # or with two marks equally close, a label is believed only when
            # it is the one mark within reach whose schedule size matches this
            # column on every floor it stands on
            fit = {x[1].mark: x[1] for x in near if x[0] <= LABEL_REACH_WITH_SCHEDULE
                   and _fits_schedule(sched[key], x[1].mark, els, b) == "yes"}
            if len(fit) == 1:
                stats["columns marked from the column layout by schedule size"] += len(els)
                return next(iter(fit.values())), "column layout, size matches the column schedule"
        return None

    def give(key, mark, ev, source, els):
        if sched.get(key) and _fits_schedule(sched[key], mark, els, b) == "no":
            b.issues.append(Issue(WARNING, "column mark", f"columns here are marked {mark} from the column "
                                  f"layout, but the column schedule's {mark} does not match them (size, or "
                                  f"no {mark} at that floor) - check the mark",
                                  sheet=els[0].sheet if els else "", elements=[e.id for e in els], mark=mark,
                                  extra={"from_linker": True}))
            stats["column marks contradicting the schedule"] += len(els)
        for e in els:
            out[e.id] = (mark, Evidence(reader=READER, sheet=ev.sheet, source=source,
                                        handle=ev.handle, at=ev.at, text=ev.text))
            stats["columns marked from the column layout"] += 1

    # stacks: columns standing in the same place, floor above floor
    stack_marks: dict[str, list] = defaultdict(list)          # set -> [(x, y, mark, evidence)]
    for key, pts in placed.items():
        stacks: list[list] = []
        for p in pts:
            for st in stacks:
                if math.hypot(st[0][0] - p[0], st[0][1] - p[1]) <= 300.0:
                    st.append(p)
                    break
            else:
                stacks.append([p])
        for n, st in enumerate(stacks):
            marked = [p for p in st if p[2]]
            members = [p for p in st if p[4]]
            els = []
            for p in members:
                by_id_el = next(e for e in b.elements if e.id == p[4])
                by_id_el.extra["stack"] = f"{key}#{n}"
                els.append(by_id_el)
            source = "column layout"
            cx = sum(p[0] for p in st) / len(st)
            cy = sum(p[1] for p in st) / len(st)
            if marked:
                mark, ev = marked[0][2], marked[0][3]
            else:
                got = from_labels(key, cx, cy, els)
                if got is None:
                    continue
                lb, source = got
                mark, ev = lb.mark, lb.props["mark"].evidence[0]
            stack_marks[key].append((cx, cy, mark, ev))
            if members:
                give(key, mark, ev, source, els)

    # columns drawn as odd shapes: the mark of the column standing at the same
    # place on another floor, else the label beside them, as for any column
    for key, items in odd.items():
        for x, y, w in items:
            same = [(math.hypot(sx - x, sy - y), m, ev) for sx, sy, m, ev in stack_marks.get(key, [])]
            same = [s for s in same if s[0] <= 300.0]
            if same:
                _, mark, ev = min(same, key=lambda s: s[0])
                give(key, mark, ev, "column layout (column at the same place)", [w])
                continue
            got = from_labels(key, x, y, [w])
            if got:
                lb, source = got
                give(key, lb.mark, lb.props["mark"].evidence[0], source, [w])
    stats["plans not aligned to a column layout"] += len(pending)
    for sid in pending:
        b.issues.append(Issue(INFO, "alignment", "no column layout lines up with this plan's columns; its "
                              "columns keep the marks drawn on the plan (if any)", sheet=sid,
                              extra={"from_linker": True}))
    return out


LABEL_REACH_WITH_SCHEDULE = 2500.0     # mm


def _column_size(e) -> tuple:
    """(width, depth) of a plan column - or of a column drawn as an odd shape that
    was read as a wall (its outline's box)."""
    cl = e.extra.get("column_like") if e.type == "wall" else None
    if cl:
        return cl["width"], cl["depth"]
    return e.get("width"), e.get("depth")


def _fits_schedule(rows: dict[str, list], mark: str, cols, b: Building) -> str:
    """Does a column schedule agree that these plan columns are `mark`?

    "yes": the schedule gives the mark a size at a floor of each column, and it is
    the size measured on the plan (either way round, within 15 mm); "no": at some
    floor the schedule has no such column, or another size; "?": cannot tell
    (mark not scheduled, or nothing measured).
    """
    if mark not in rows:
        return "?"
    seen = False
    for e in cols:
        w, d = _column_size(e)
        lv = _level_key(b, e)
        if not w or not d or level_rank(lv) is None:
            continue
        sizes = [sz for rng, sz in rows[mark] if in_range(lv, rng)]
        if not sizes:
            return "no"
        lo, hi = sorted((w, d))
        if not any(abs(sz[0] - lo) <= 15 and abs(sz[1] - hi) <= 15 for sz in sizes):
            return "no"
        seen = True
    return "yes" if seen else "?"


def _mark_by_size(b: Building, e, keys) -> tuple[str, list[Evidence]] | None:
    """Of several possible marks, the one whose schedule size matches the outline."""
    w, ln = e.get("width"), e.get("length")
    if not w or not ln:
        return None
    hits = []
    for s in b.of("spec"):
        if s.mark not in keys or s.get("family") != "footing":
            continue
        sw, sl = s.get("width"), s.get("length")
        if sw and sl and abs(sw - w) <= 25 and abs(sl - ln) <= 25:
            hits.append((s.mark, s.props["width"].evidence[:1]))
    marks = {m for m, _ in hits}
    return hits[0] if len(marks) == 1 else None


def _set_of(b: Building, el) -> str:
    """The drawing set (project) an element or spec came from."""
    s = b.sheets.get(el.sheet)
    return s.extra.get("set", "") if s else ""


def _drawing_of(sid: str) -> str:
    """The drawing a sheet comes from: sheet ids are '<drawing>:<view>' (sheets/registry.py)."""
    return (sid or "").split(":", 1)[0]


def _own_sections(b: Building) -> dict:
    """{(set, drawing): the section names that drawing titles itself} - from its sheets'
    titles ("SECTION: C-C") and the sections read off it. A plan's cut letters C ... C mean
    its own drawing's C-C; another drawing's C-C only when its own has none (Sasan Gir: the
    Jacuzzi, 2nd floor and terrace drawings each have a C-C, and beam marks B19, B21 repeat
    floor to floor at the same places - the terrace's C-C fitted the 2nd floor plans)."""
    from .readers.beam_sections import TITLE
    out = defaultdict(set)
    for sid, s in b.sheets.items():
        m = TITLE.search(re.sub(r"%%[UuOo]", "", s.title or "").strip())
        if m:
            out[(s.extra.get("set", ""), _drawing_of(sid))].add(m.group(1).upper())
    for e in b.elements:
        if e.type in ("building_section", "beam_section") and e.mark:
            out[(_set_of(b, e), _drawing_of(e.sheet))].add(e.mark.upper())
    return out


def _level_key(b: Building, el) -> str:
    s = b.sheets.get(el.sheet)
    return (s.extra.get("level_key") if s else "") or el.level


def _wing_zones(b: Building, attached, by_id, stats) -> None:
    """A note on a plan naming another wing ("WING C REVISED BEAMS" on the B-wing
    plan) makes the beams tagged around it that wing's. Taken only when the tags
    nearest the note are exactly the marks that wing's schedule lists, one each -
    never on distance alone."""
    beam_of = {c.id: eid for eid, caps in attached.items() for c in caps}
    for sid, sh in b.sheets.items():
        notes = sh.extra.get("wing_notes") or []
        if not notes:
            continue
        tags = [c for c in b.of("caption", sheet=sid) if c.id in beam_of
                and by_id[beam_of[c.id]].type in ("beam", "beam_span")]
        set_key = sh.extra.get("set", "")
        for note in notes:
            w = note["wing"]
            listed = {s.mark for s in b.of("spec") if s.wing == w and s.get("family") == "beam"
                      and not s.extra.get("part") and _set_of(b, s) == set_key}
            here = {c.mark for c in tags} & listed
            if not here:
                continue
            x, y = note["at"]
            near = sorted(tags, key=lambda c: math.hypot(c.geometry["at"][0] - x, c.geometry["at"][1] - y))[:len(here)]
            if {c.mark for c in near} != here or len({c.mark for c in near}) != len(near):
                b.issues.append(Issue(INFO, "schedule", f"note '{note['text']}' on the plan: the tags beside it are not "
                                      f"just the marks of the wing {w} schedule ({', '.join(sorted(here))}), so its "
                                      f"beams keep this plan's wing - check them in Review",
                                      sheet=sid, at=list(note["at"]), extra={"from_linker": True}))
                continue
            for c in near:
                e = by_id[beam_of[c.id]]
                e.wing = w
                e.extra["wing_from_note"] = note["text"]
            stats["beams given the wing of a note beside them"] += len(near)
            b.issues.append(Issue(INFO, "schedule", f"note '{note['text']}' on the plan: {', '.join(sorted(here))} "
                                  f"beside it are sized from the wing {w} schedule", sheet=sid,
                                  at=list(note["at"]), extra={"from_linker": True}))


def _prefix_positions(b: Building, readings, by_sheet) -> dict:
    """{set: {prefix: Counter(family)}}: where each prefix's marks sit on the plan -
    inside a column outline, inside a wall, on a beam, else inside a slab bay."""
    out: dict = defaultdict(lambda: defaultdict(Counter))
    for cap in b.of("caption"):
        targets = [e for sid in readings.get(cap.sheet, []) for e in by_sheet[sid]]
        pre = prefixes.prefix_of(cap.mark)
        if not targets or not pre:
            continue
        p = cap.geometry["at"]
        th = max(cap.geometry.get("text_height") or 100.0, 1.0)
        rot = cap.geometry.get("rotation")
        fam = ""
        if any(e.type == "column" and _column_dist(p, e) == 0.0 for e in targets):
            fam = "column"
        elif any(e.type == "wall" and len(e.geometry.get("outline") or []) >= 3
                 and _inside(p, e.geometry["outline"]) for e in targets):
            fam = "wall"
        elif any(e.type == "beam" and (rot is None or _parallel(rot, e))
                 and (lambda d: d[1] and d[0] <= max(1.5 * th, 300.0))(_beam_dist(p, e)) for e in targets):
            fam = "beam"
        elif any(e.type == "slab" and e.geometry.get("outline") and _inside(p, e.geometry["outline"])
                 for e in targets):
            fam = "slab"
        if fam:
            out[_set_of(b, cap)][pre][fam] += 1
    return out


def _settle_prefixes(b: Building, readings, by_sheet, overrides, stats) -> dict[str, dict[str, str]]:
    """What each drawing set's mark prefixes mean (building.prefixes), with where the
    marks sit on the plan added; captions and specs take that family, and Review gets
    every prefix that was learned, re-read, or is not clear. {set: {prefix: family}}."""
    ev = prefixes.drawing_evidence(b)
    pos = _prefix_positions(b, readings, by_sheet)
    hints_of = {}
    for s in b.sheets.values():
        if s.extra.get("prefix_hints"):
            hints_of.setdefault(s.extra.get("set", ""), s.extra["prefix_hints"])
    decisions = (overrides or {}).get("prefixes") or {}
    maps: dict[str, dict[str, str]] = {}
    for set_key in sorted(set(ev) | set(pos)):
        merged = {p: dict(srcs) for p, srcs in ev.get(set_key, {}).items()}
        for p, cnt in pos.get(set_key, {}).items():
            if sum(cnt.values()) >= 2:          # one mark is not a habit
                merged.setdefault(p, {})["position"] = cnt
        hints = hints_of.get(set_key) or {}
        decided = prefixes.decide_all(merged, hints, decisions)
        maps[set_key] = {p: d["family"] for p, d in decided.items() if d["family"] not in ("", "unknown")}
        for p, d in decided.items():
            why = prefixes.describe(d)
            hint = (hints.get(p) or "").lower()
            msg, sev = "", INFO
            if d["status"] == "learned":
                msg = (f"marks {p}.. read as {d['family']} marks ({why}). {p} is not a usual prefix: "
                       "confirm or change it here")
            elif d["status"] == "check":
                msg, sev = (f"marks {p}.. are kept as {d['family']} marks, as {p} usually means, but these drawings "
                            f"point to {d['suggested']} ({why}): if they are {d['suggested']} marks, decide it "
                            "here"), WARNING
            elif d["status"] == "close":
                msg, sev = (f"marks {p}..: the drawings do not say clearly what they are ({why}); kept as "
                            + ({"unknown": "not known", "other": "not element marks (they name nothing)"}
                               .get(d["family"], d["family"])) + " - decide it here"), WARNING
            elif d["status"] == "hint":
                msg = (f"marks {p}.. read as {d['family']} marks from your earlier decision - nothing in these "
                       "drawings says otherwise")
            elif d["status"] == "decided":
                msg = f"marks {p}..: {d['family']} marks, as you decided ({why or 'no other evidence'})"
            elif hint and hint != d["family"]:
                msg = (f"marks {p}..: your earlier decision ({hint}) was not used - these drawings say "
                       f"{d['family']} ({why})")
            if msg:
                stats["mark prefixes to review"] += 1
                b.issues.append(Issue(sev, "mark prefix", msg[0].upper() + msg[1:], mark=p,
                                      extra={"from_linker": True, "prefix": p, "family": d["family"],
                                             "set": set_key}))
    # captions and specs take their prefix's family (the one read is kept)
    for el in b.elements:
        if el.type not in ("caption", "spec") or el.props.get("by_column") is not None or "family" not in el.props:
            continue
        read = el.extra.get("family_read", el.get("family"))
        target = maps.get(_set_of(b, el), {}).get(prefixes.prefix_of(el.mark)) or read
        if target != el.get("family"):
            if "family_read" not in el.extra:
                el.extra["family_read"] = read
            v = el.props["family"]
            el.props["family"] = Value(target, v.status, v.confidence, v.evidence)
            stats["marks given their prefix's meaning from the drawings"] += 1
    return maps


def _rect(outline):
    """(centre, unit along the long side, length, thickness) of a four-cornered outline."""
    if len(outline) != 4:
        return None
    pts = [tuple(p[:2]) for p in outline]
    edges = [(pts[i], pts[(i + 1) % 4]) for i in range(4)]
    a, c = max(edges, key=lambda e: math.dist(*e))
    L = math.dist(a, c)
    if L <= 0:
        return None
    return ((sum(p[0] for p in pts) / 4.0, sum(p[1] for p in pts) / 4.0),
            ((c[0] - a[0]) / L, (c[1] - a[1]) / L), L, min(math.dist(*e) for e in edges))


def _walls_drawn_as_columns(b: Building, readings, by_sheet, stats) -> list:
    """An outline the drawing labels with a wall mark is a wall, whatever layer it is on.

    Akanistha Winter Villa: shear walls SW1 ... SW18 drawn as 300 x 1000 to 3000
    rectangles on the column layer, each label written beside its outline in one block -
    every one was built as an unmarked column. A wall caption that names a column
    outline (the outline drawn with it in its block, else the one it is written in)
    makes that column a wall; so does an unmarked piece drawn end to end with it, as
    thick and in line (SW7: 2000 + 1000). Left as columns: one with a mark of its own or
    a column caption on it, and one as squat as a column. Returns the walls made."""
    def at(cap):
        own = cap.extra.get("block_outline")
        return own[:2] if own else cap.geometry["at"]

    def outline(e):
        o = e.geometry.get("outline") or []
        return o if len(o) >= 3 else None

    col_caps = defaultdict(list)
    for cap in b.of("caption"):
        if cap.get("family") == "column":
            col_caps[cap.sheet].append(at(cap))

    def free(e, view):
        """An unmarked column outline no column caption names."""
        return e.type == "column" and not e.mark and outline(e) is not None and \
            not any(_inside(p, outline(e)) for p in col_caps[view])

    def convert(e, mark, part_of=None):
        e.extra["was_column"] = {"named_by": mark, **{k: e.props[k].value for k in ("kind", "width", "depth")
                                                      if k in e.props},
                                 **{k: e.geometry.get(k) for k in ("x", "y", "rotation")},
                                 "_keys": e.extra.get("_keys"), "repaired": e.extra.pop("repaired", None)}
        if part_of:
            e.extra["part_of"] = part_of
        e.type = "wall"
        e.geometry = {"start": None, "end": None, "outline": e.geometry["outline"]}
        e.props = {"thickness": Value(None, MISSING, 1.0, [Evidence(
            reader="plan.legacy", sheet=e.sheet, source="", layer=e.extra.get("_layer", ""),
            handle=e.extra.get("_handle"))])}
        e.extra["_keys"] = ["mark", "thickness", "start", "end", "outline", "source_layer", "handle",
                            "tag_distance", "notes"]

    wall_caps = defaultdict(list)
    for cap in b.of("caption"):
        if cap.get("family") == "wall":
            wall_caps[cap.sheet].append(cap)

    def beside(cap, p, targets):
        """The outline a wall label written beside it names, when the label has no outline of
        its own (Akanistha re-saved 2026-10-24: the blocks holding each SW label with its
        rectangle exploded - the labels lie 200-1500 mm off their walls). Only the nearest
        outline, clearly nearer than the next, within five text heights - and the label the
        nearest wall label to it."""
        reach = 5.0 * max(float(cap.geometry.get("text_height") or 0.0), 100.0)
        ds = sorted(((_outline_dist(p, outline(e)), e) for e in targets
                     if e.type in ("column", "wall") and outline(e)), key=lambda de: de[0])
        if not ds or ds[0][0] > reach or (len(ds) > 1 and ds[1][0] < 1.5 * max(ds[0][0], 1.0)):
            return []
        d, e = ds[0]
        if e.type != "column":
            return []
        if any(o is not cap and not o.extra.get("block_outline") and _outline_dist(at(o), outline(e)) < d
               for o in wall_caps[cap.sheet]):
            return []                                  # another wall label is nearer to it
        return [e]

    made = []
    for cap in b.of("caption"):
        if cap.get("family") != "wall":
            continue
        targets = [e for sid in readings.get(cap.sheet, []) for e in by_sheet[sid]]
        p = at(cap)
        if any(e.type == "wall" and outline(e) and _inside(p, outline(e)) for e in targets):
            continue
        cols = [e for e in targets if e.type == "column" and outline(e) and _inside(p, outline(e))]
        if not cols and not cap.extra.get("block_outline"):
            cols = beside(cap, p, targets)
        if len(cols) != 1 or not free(cols[0], cap.sheet) or cols[0].extra.get("diameter"):
            continue                                   # a round column is not a wall
        col = cols[0]
        try:
            lo, hi = sorted((float(col.props["width"].value), float(col.props["depth"].value)))
        except (KeyError, TypeError, ValueError):
            continue
        if hi <= walls.COLUMN_LIKE and hi <= 3.0 * lo:
            continue                                   # as squat as a column
        r0 = _rect(outline(col))
        convert(col, cap.mark)
        made.append(col)
        if r0 is None:
            continue
        (cx, cy), u, _L, t = r0
        for o in targets:
            if o is col or not free(o, cap.sheet):
                continue
            r1 = _rect(outline(o))
            if r1 is None:
                continue
            (ox, oy), v, _L1, t1 = r1
            if abs(u[0] * v[1] - u[1] * v[0]) > math.sin(math.radians(3.0)) or abs(t - t1) > 10.0 \
                    or abs((ox - cx) * -u[1] + (oy - cy) * u[0]) > 10.0 \
                    or min(_outline_dist(q, outline(col)) for q in outline(o)) > 5.0:
                continue
            convert(o, cap.mark, part_of=col.id)
            made.append(o)
    return made


def link(b: Building, overrides: dict | None = None) -> dict[str, int]:
    """Resolve every modelled element from all its sources. Returns counts."""
    stats = defaultdict(int)
    b.issues = [i for i in b.issues if not i.extra.get("from_linker")]
    for e in b.elements:
        e.resolved = {}
    merge_beams.merge(b, stats)           # one beam drawn with three lines is one beam
    walls.analyse(b, stats)
    slab_panels.make(b, stats)

    # the plan sheets each registry view was read into
    readings = defaultdict(list)
    for sid, s in b.sheets.items():
        if s.extra.get("view_sheet"):
            readings[s.extra["view_sheet"]].append(sid)

    cands: dict[str, dict[str, list[Candidate]]] = defaultdict(lambda: defaultdict(list))
    marks_from_captions: dict[str, list[tuple[str, Evidence]]] = defaultdict(list)
    attached: dict[str, list] = defaultdict(list)
    # spans made by an earlier run are made again from scratch
    b.elements = [e for e in b.elements if e.type != "beam_span"]
    by_sheet = defaultdict(list)
    for e in b.elements:
        if not e.extra.get("merged_into"):         # a joined beam's pieces carry nothing themselves
            by_sheet[e.sheet].append(e)
    by_id = {e.id: e for e in b.elements}
    # 0. what this consultant's mark prefixes mean, from the drawings themselves
    prefix_map = _settle_prefixes(b, readings, by_sheet, overrides, stats)

    def family_of_mark(mk, el) -> str:
        return prefix_map.get(_set_of(b, el), {}).get(mk.prefix) or mk.family()

    # 0b. shear walls drawn on the column layer: the wall mark the drawing gives them
    walls.analyse(b, stats, _walls_drawn_as_columns(b, readings, by_sheet, stats))
    for e in b.of("wall"):
        was = e.extra.get("was_column")
        if not was:
            continue
        stats["column outlines labelled as walls, built as walls"] += 1
        b.issues.append(Issue(INFO, "wall", f"the {was.get('width') or 0:.0f} x {was.get('depth') or 0:.0f} outline on "
                              f"layer {e.extra.get('_layer', '')} is "
                              + (f"drawn in line with wall {was['named_by']}, as thick: part of it"
                                 if e.extra.get("part_of") else f"labelled {was['named_by']}, a wall mark")
                              + " - built as a wall, not a column", sheet=e.sheet, elements=[e.id],
                              mark=was["named_by"], extra={"from_linker": True}))

    # 1. plan captions -> the element they sit on
    for cap in b.of("caption"):
        targets = [e for sid in readings.get(cap.sheet, []) for e in by_sheet[sid]]
        if not targets:
            continue
        p = cap.geometry["at"]
        th = max(cap.geometry.get("text_height") or 100.0, 1.0)
        fam = cap.get("family")
        hit, why = None, ""
        if fam in ("beam", "unknown"):
            reach = max(4 * th, 600.0)
            rot = cap.geometry.get("rotation")
            # a beam's caption runs along it: a caption across a beam belongs to
            # the beam crossing it there (B56 was given GB1's caption without this)
            near = sorted(((_beam_dist(p, e), e) for e in targets if e.type == "beam"
                           and (rot is None or _parallel(rot, e))),
                          key=lambda x: (not x[0][1], x[0][0]))
            near = [(d, e) for (d, ins), e in near if d <= reach and ins] or \
                   [(d, e) for (d, ins), e in near if d <= reach]
            if near:
                if len(near) > 1 and near[1][0] < 1.25 * max(near[0][0], th * 0.5) and \
                        _key_of(near[0][1].mark) != _key_of(near[1][1].mark):
                    why = f"two beams almost equally close ({near[0][0]:.0f} and {near[1][0]:.0f} mm)"
                else:
                    hit = near[0][1]
        if hit is None and not why and fam in ("column", "unknown"):
            reach = max(3 * th, 1500.0)
            near = sorted(((_column_dist(p, e), e) for e in targets if e.type == "column"),
                          key=lambda x: x[0])
            if near and near[0][0] <= reach:
                own = cap.extra.get("block_outline")
                if own and own[2] != near[0][1].extra.get("_layer") and _column_dist(own[:2], near[0][1]) > 0:
                    # written in a block beside an outline of its own (New block: 'SC1'
                    # beside a steel post on 'Steel Col'): it names that, not this column
                    why = f"it names the outline on '{own[2]}' drawn with it in its block, not this column"
                else:
                    hit = near[0][1]
        if hit is None and not why and fam == "wall":
            # SW3 / LW1 written on or beside a wall: the wall whose outline it is in,
            # else the nearest within reach - two equally near are left for review
            reach = max(3 * th, 1500.0)
            near = sorted(((0.0 if _inside(p, e.geometry["outline"]) else _outline_dist(p, e.geometry["outline"]), e)
                           for e in targets if e.type == "wall" and len(e.geometry.get("outline") or []) >= 3),
                          key=lambda x: x[0])
            near = [x for x in near if x[0] <= reach]
            own = cap.extra.get("block_outline")
            named = [e for _d, e in near if own and _inside(own[:2], e.geometry["outline"])]
            if len(named) == 1:
                # written in a block beside this outline: it names this wall
                hit = named[0]
            elif near:
                d0, w0 = near[0]
                own = _key_of(w0.mark) if w0.mark else ""
                if len(near) > 1 and near[1][0] < 1.25 * max(d0, th) and d0 > 0:
                    why = f"two walls almost equally close ({d0:.0f} and {near[1][0]:.0f} mm)"
                elif d0 > 0 and own and own != cap.mark:
                    # beside a wall tagged with another mark (J1153: LW2 next to LW1):
                    # it names a wall that is not drawn here, not this one
                    why = f"beside wall {own}, outside it"
                else:
                    hit = w0
        if hit is None and not why and fam == "slab":
            inside = [e for e in targets if e.type == "slab" and e.geometry.get("outline")
                      and _inside(p, e.geometry["outline"])]
            if inside:
                hit = inside[0]
        if hit is None:
            stats["captions not attached"] += 1
            if why:
                b.issues.append(Issue(WARNING, "caption", f"caption '{cap.extra.get('raw', cap.mark)}' not "
                                      f"attached: {why}", sheet=cap.sheet, elements=[cap.id],
                                      mark=cap.mark, at=list(p), extra={"from_linker": True}))
            continue
        stats["captions attached"] += 1
        attached[hit.id].append(cap)

    # 1b. one extracted beam carrying several marks is several beams: split it
    for bid, caps in list(attached.items()):
        beam = by_id[bid]
        if beam.type == "beam" and len({c.mark for c in caps}) > 1:
            caps = attached[bid] = _set_aside_stacked(b, beam, caps)
        if beam.type != "beam" or len({c.mark for c in caps}) < 2 or beam.geometry.get("mid"):
            continue
        spans = _split(b, beam, caps, by_sheet[beam.sheet])
        if spans:
            stats["beams split into spans"] += 1
            stats["spans"] += len(spans)
            del attached[bid]
            for span, span_caps in spans:
                attached[span.id] = span_caps
                by_id[span.id] = span
    _wing_zones(b, attached, by_id, stats)

    for eid, caps in attached.items():
        for cap in caps:
            ev = cap.props["family"].evidence[0]
            cap.extra["attached_to"] = eid
            marks_from_captions[eid].append((cap.mark, ev))
            for prop in ("width", "depth", "thickness"):
                if cap.get(prop):
                    cands[eid][prop].append(Candidate(cap.get(prop), "plan caption", [ev]))
    _size_captions(b, readings, cands, stats)
    # a wall drawn in two pieces end to end: the unlabelled piece takes the other's mark
    for e in b.of("wall"):
        part_of = e.extra.get("part_of")
        if part_of and not marks_from_captions.get(e.id) and marks_from_captions.get(part_of):
            marks_from_captions[e.id] = list(marks_from_captions[part_of])

    # 2. each modelled element: its mark, then specs by mark, then its own values
    specs = defaultdict(list)
    for s in b.of("spec"):
        specs[s.mark].append(s)
    from_layout = _layout_marks(b, stats)
    hints = defaultdict(list)
    for h in b.of("layer_size"):
        hints[(h.sheet, h.extra.get("layer", "").upper())].append(h)

    _stairs_and_ramps(b, cands)
    working: dict[str, str] = {}              # slab id -> the nearest mark the plan reader gave it
    panel = _panel_marks(b)                   # unmarked bays of a marked panel (secondary beams between)
    runs = _run_marks(b)                      # unmarked spans carrying on a marked beam
    lines = _line_marks(b, marks_from_captions)   # unmarked spans between two of one mark
    over = _detail_span_marks(b, marks_from_captions)   # unmarked spans of a beam its detail runs over
    for e in b.elements:
        if e.type in ("stair", "ramp"):
            continue                    # done above: no marks, no sizes by mark
        if e.type not in FAMILY_OF_TYPE or gone(e):
            continue                    # a split beam is resolved through its spans
        # the mark: the drawing's tag, else the caption's; a disagreement is a conflict
        own = _key_of(e.mark) if e.mark and "mark" not in e.props and "mark_layer" not in e.props else ""
        inherited = None                          # (mark, kind, source) read off the drawing around it
        if e.type == "slab" and own and any("no slab mark in this" in str(n) for n in e.notes):
            # never invent: the plan reader gave a bay with no mark the nearest mark (and
            # its thickness) to keep the floor whole - a working value, not a reading
            working[e.id] = e.mark
            own = ""
            if e.id in panel:
                # but a bay of a marked panel, only secondary beams between, is that slab
                del working[e.id]
                inherited = (panel[e.id][0], "slab panel", "same panel as the bay marked "
                             f"{panel[e.id][0]}: only secondary beams (resting on beams) lie between")
        if e.type in ("beam", "beam_span") and not e.mark and e.id in runs:
            inherited = (runs[e.id][0], "beam run", f"carries on beam {runs[e.id][0]} past a crossing "
                         "beam, in line, same width, no column between")
        elif e.type in ("beam", "beam_span") and not e.mark and e.id in lines:
            inherited = (lines[e.id], "beam line", f"on one straight beam line between two spans marked "
                         f"{lines[e.id]}, same width: the mark written at both ends of the line")
        elif e.type in ("beam", "beam_span") and not e.mark and e.id in over:
            inherited = (over[e.id][0], "beam detail span", f"in line with beam {over[e.id][0]}, same width, "
                         "between the grids its beam detail runs over")
        if e.type in ("footing", "pedestal", "raft"):
            # foundation readers give their own mark sources: the tag beside the
            # footing, and the layer it is drawn on where a consultant uses one per type
            mc = []
            if e.props.get("mark"):
                mc.append(Candidate(e.get("mark"), "plan caption", e.props["mark"].evidence[:1]))
            if e.props.get("mark_layer"):
                mc.append(Candidate(e.get("mark_layer"), "layer name", e.props["mark_layer"].evidence[:1]))
            keys = {c.value for c in mc}
            if len(keys) > 1:
                # tag and layer disagree (a footing tagged F12 on layer F4): the
                # schedule decides - the mark whose size matches the drawn outline
                best = _mark_by_size(b, e, keys)
                if best:
                    mc.append(Candidate(best[0], "schedule table", best[1], note="size matches the outline"))
                    keys = {best[0]}
            if mc:
                cands[e.id]["mark"] = mc
            for prop in ("width", "length", "thickness"):
                v = e.props.get(prop)
                if v is not None and v.value:
                    cands[e.id][prop].append(Candidate(v.value, "plan geometry", v.evidence[:1]))
            # specs by mark, and (Villa 5) footing specs listed by the column it carries
            cols = set(e.get("columns") or [])
            my_set = _set_of(b, e)
            for s in b.of("spec"):
                if s.get("family") != FAMILY_OF_TYPE[e.type]:
                    continue
                if my_set and _set_of(b, s) and _set_of(b, s) != my_set:
                    continue
                by_col = s.get("by_column")
                if not ((by_col and by_col in cols) or (not by_col and s.mark in keys)):
                    continue
                kind = "schedule table" if s.extra.get("table") is not None else "detail caption"
                for prop in SIZE_PROPS + (FOUNDING,):
                    v = s.props.get(prop)
                    if v is not None and v.value:
                        cands[e.id][prop].append(Candidate(v.value, kind, v.evidence[:2],
                                                           note=s.extra.get("table") or ""))
                for prop in ("steps", "pcc_size", "step_depths", "steel", "type"):
                    if s.props.get(prop) is not None and prop not in e.resolved:
                        e.resolved[prop] = Value(s.get(prop), READ, 0.9, s.props[prop].evidence[:2])
                stats["specs used"] += 1
            continue
        own_mk = parse_mark(own) if own else None
        # a stair slab: ST1 written in a bay, and the drawing's slab schedule lists ST-1,2,3
        # with a thickness (ALCAZAR: "ST-1,2,3 200 ... STAIR WAIST") - the bay's own mark
        own_fam = family_of_mark(own_mk, e) if own_mk else ""
        stair_slab = bool(e.type == "slab" and own_mk and own_fam == "stair" and any(
            "SLAB" in str(s.extra.get("table") or "").upper() for s in specs.get(own, [])))
        if own_mk and own_fam not in (FAMILY_OF_TYPE[e.type], "unknown") and \
                not (e.type == "wall" and own_fam == "column") and not stair_slab:
            # a slab's tag picked up by a beam (S4 on beam B30A): not this element's mark
            e.extra["tag_set_aside"] = f"{e.mark} is a {own_fam} mark, not a {e.type}'s"
            own = ""
        cap_marks = {m for m, _ in marks_from_captions.get(e.id, [])}
        mark_c = []
        if own:
            mark_c.append(Candidate(own, "old extractor schedule", [Evidence(
                reader="plan.legacy", sheet=e.sheet, source="tag on plan",
                handle=e.extra.get("_handle", ""), text=e.mark)], "tag"))
        for m, ev in marks_from_captions.get(e.id, []):
            mark_c.append(Candidate(m, "plan caption", [ev]))
        if e.id in from_layout:
            m, ev = from_layout[e.id]
            mark_c.append(Candidate(m, "column layout", [ev]))
            cap_marks = cap_marks | {m}
        if inherited and not mark_c:
            mark_c.append(Candidate(inherited[0], inherited[1], [Evidence(
                reader=READER, sheet=e.sheet, source=inherited[2], text=inherited[0])]))
            cap_marks = cap_marks | {inherited[0]}
        if mark_c:
            cands[e.id]["mark"] = mark_c
        keys = ({own} if own else set()) | cap_marks

        lv = _level_key(b, e)
        fam = FAMILY_OF_TYPE[e.type]
        my_set = _set_of(b, e)
        elsewhere = set()                         # depths this mark has in another wing's or part's table
        for k in keys:
            for s in specs.get(k, []):
                if my_set and _set_of(b, s) and _set_of(b, s) != my_set:
                    continue                      # another project's GB1 is not this GB1
                sf = s.get("family")
                if sf not in (fam, "unknown") and not (fam == "wall" and sf == "column") and \
                        not (stair_slab and "SLAB" in str(s.extra.get("table") or "").upper()):
                    continue
                if s.extra.get("part") or (s.wing and e.wing and s.wing != e.wing):
                    if s.get("depth"):            # club house, another building part, another wing
                        elsewhere.add(s.get("depth"))
                    continue
                rng = s.extra.get("level_range")
                if rng:
                    if not in_range(lv, rng):
                        continue                  # a column schedule's other level range
                elif s.level and lv and s.level != lv:
                    continue
                kind = ("section dimensions" if s.extra.get("from") == "section dimensions"
                        else "schedule table" if s.extra.get("table") is not None else "detail caption")
                for prop in ("width", "depth", "thickness"):
                    v = s.props.get(prop)
                    if v is not None and v.value:
                        cands[e.id][prop].append(Candidate(v.value, kind, v.evidence[:3],
                                                           note=s.extra.get("table") or "", pair=s.id))
                if s.get("depth_varies"):
                    cands[e.id]["depth_varies"].append(Candidate(s.get("depth_varies"), kind,
                                                                 s.props["depth_varies"].evidence[:2]))
                stats["specs used"] += 1

        # what the plan reader itself measured or read
        if e.type in ("beam", "wall", "column"):
            w = e.get("width") if e.type != "wall" else e.get("thickness")
            if w:
                src = e.props.get("width") or e.props.get("thickness")
                cands[e.id]["width"].append(Candidate(w, "plan geometry", src.evidence[:1]))
        if e.type == "column" and e.get("depth"):
            cands[e.id]["depth"].append(Candidate(e.get("depth"), "plan geometry", e.props["depth"].evidence[:1]))
        if e.type == "wall" and not e.get("thickness"):
            wev = (e.resolved.get("role") or Value(None, READ, 0, [Evidence(reader=READER)])).evidence[:1]
            cl = e.extra.get("column_like")
            if cl:
                # a column drawn as an odd shape: its size is its outline's box
                cands[e.id]["width"].append(Candidate(float(cl["width"]), "plan geometry", wev, pair=e.id + "/box"))
                cands[e.id]["depth"].append(Candidate(float(cl["depth"]), "plan geometry", wev, pair=e.id + "/box"))
            elif e.extra.get("thickness_from_runs"):
                cands[e.id]["width"].append(Candidate(float(e.extra["thickness_from_runs"]), "plan geometry", wev))
        if e.type == "wall" and not e.extra.get("column_like"):
            got = _wall_section_by_size(b, e, lv, my_set)
            if got is not None:
                s = got
                for prop, to in (("thickness", "width"), ("length", "length")):
                    v = s.props.get(prop)
                    if v is not None and v.value:
                        cands[e.id][to].append(Candidate(v.value, "schedule table", v.evidence[:3],
                                                         note="matched by size - the schedule names no mark"))
                e.extra["schedule_section"] = {"spec": s.id, "range": s.extra.get("level_range"),
                                               "bars": s.get("bars"), "boundary_elements": s.get("boundary_elements")}
                stats["walls sized from an unmarked schedule section"] += 1
        if e.type == "beam" and e.get("depth"):
            od = e.get("depth")
            scoped = {c.value for c in cands[e.id]["depth"] if c.kind == "schedule table"}
            if scoped and od not in scoped and od in elsewhere:
                # the old reader looks marks up in every table on the sheet, whatever
                # its wing: its B63 750 is the WING C REVISED row, not this wing's
                e.extra["old_depth_set_aside"] = f"{od:.0f} - that is this mark's depth in another wing's table"
                stats["old reader's depth from another wing's table set aside"] += 1
            else:
                cands[e.id]["depth"].append(Candidate(od, "old extractor schedule", e.props["depth"].evidence[:1]))
        if e.type == "slab" and e.get("thickness") and e.id not in working and not inherited:
            cands[e.id]["thickness"].append(Candidate(e.get("thickness"), "old extractor schedule",
                                                      e.props["thickness"].evidence[:1]))
        # a size in the layer the element is drawn on
        view = b.sheets[e.sheet].extra.get("view_sheet", "") if e.sheet in b.sheets else ""
        if e.type == "slab" and not cands[e.id].get("thickness"):
            # nothing else sizes it: the plan's note "ALL SLABS ARE 125mm THK. UNLESS
            # OTHERWISE SPECIFIED" does - and only then, as the note says
            note = next((b.sheets[s].extra["slab_thickness_note"] for s in (view, e.sheet) + _twins_of(b, view)
                         if s in b.sheets and b.sheets[s].extra.get("slab_thickness_note")), None)
            if note:
                cands[e.id]["thickness"].append(Candidate(note["value"], "general note", [Evidence(
                    reader="labels", sheet=view or e.sheet, source="the plan's general note",
                    layer=note["layer"], handle=note["handle"], at=note["at"], text=note["text"])]))
                stats["slab thickness from the plan's note"] += 1
        for h in hints.get((view, (e.extra.get("_layer") or "").upper()), []):
            if e.type == "beam":
                cands[e.id]["width"].append(Candidate(h.get("width"), "layer name", h.props["width"].evidence))
                cands[e.id]["depth"].append(Candidate(h.get("depth"), "layer name", h.props["depth"].evidence))

    # 3. resolve
    no_founding: dict[str, list] = defaultdict(list)
    for e in b.elements:
        if e.type not in FAMILY_OF_TYPE or gone(e):
            continue
        cs = cands.get(e.id, {})
        if e.type == "column" or e.extra.get("column_like"):
            _column_sides(cs)
        for prop, lst in cs.items():
            e.resolved[prop] = _resolve(prop, lst, e.type)
            st = e.resolved[prop].status
            stats[f"{prop} {st}"] += 1
            if st == CONFLICT and prop != "depth_varies":
                vals = ", ".join(f"{c.value} ({c.kind}{': ' + c.note[:30] if c.note else ''})" for c in lst)
                b.issues.append(Issue(WARNING, "conflict", f"{e.type} {e.final('mark') or e.mark or e.id}: "
                                      f"{prop} disagrees - {vals}", sheet=e.sheet, elements=[e.id],
                                      mark=str(e.final('mark') or e.mark or ""), extra={"from_linker": True}))
        if e.type == "beam":
            view = b.sheets[e.sheet].extra.get("view_sheet", "") if e.sheet in b.sheets else ""
            _layer_name_check(b, e, cs, hints.get((view, (e.extra.get("_layer") or "").upper()), []))
        if e.id in working:
            _working_values(b, e, working[e.id], stats)
        if cs.get("depth_varies"):
            b.issues.append(Issue(WARNING, "varying depth", f"{e.type} {e.final('mark')}: depth written "
                                  f"{cs['depth_varies'][0].value} - a beam deeper at one end; confirm",
                                  sheet=e.sheet, elements=[e.id], mark=str(e.final("mark") or ""),
                                  extra={"from_linker": True}))
        for prop in REQUIRED.get(e.type, ()):
            if prop not in e.resolved:
                e.resolved[prop] = Value(None, MISSING, 0.0, [])
                stats[f"{prop} missing"] += 1
                if prop == FOUNDING:
                    no_founding[_set_of(b, e)].append(e)
                    continue                # one issue per drawing set, below
                b.issues.append(Issue(INFO if e.type == "column" else WARNING, "missing",
                                      f"{e.type} {e.final('mark') or e.mark or '(no mark)'}: no source gives "
                                      f"its {prop}", sheet=e.sheet, elements=[e.id],
                                      mark=str(e.final('mark') or e.mark or ""), extra={"from_linker": True}))

    for set_key, els in no_founding.items():
        b.issues.append(Issue(WARNING, "missing", f"{len(els)} footings/rafts of {set_key or 'the drawings'} have no "
                              f"founding level in the drawings: they are not built until one is decided "
                              f"(per footing, or one level for all)", elements=[e.id for e in els][:200],
                              extra={"from_linker": True}))

    _unmodelled_layers(b)
    _scope(b, stats)
    _off_the_frame(b, stats)
    _beyond_the_beams(b, stats)
    _face_styles(b, stats)          # before the sections: a face's style says which way round
    _beam_sections(b, stats)
    _column_ticks(b, stats)           # short lines along a column's face: not beams
    _reseat_on_faces(b, stats)        # a marked beam paired with the wrong line: between its faces
    _slab_edge_bands(b, stats)        # a solid slab edge paired with a beam's dashed face: slab
    _beam_face_runs(b, stats)         # a beam runs as far as both its faces are drawn
    _marked_bays(b, stats)            # a marked bay with no slab, once the beams are settled
    _section_heights(b, stats)        # building sections: heights off the level, slopes
    _legend_zones(b, stats)           # what the legend marks: steel roofs, columns through / ending
    levels_table.build(b, stats)
    if overrides:
        stats["overrides applied"] = apply_overrides(b, overrides)
    for e in b.elements:
        # a line the drawings say is not a beam (slab strip, drawn twice, part of a column)
        # that the review gave a mark or size to is a beam, as decided
        if any(e.resolved.get(p) is not None and e.resolved[p].status == OVERRIDE for p in ("mark", "width", "depth")):
            for k in ("slab_strip", "same_beam_as", "not_a_beam"):
                e.extra.pop(k, None)
    _column_extent(b, stats)          # how far each column runs, from the column schedule's level ranges
    _wall_heights(b, stats, overrides)  # where walls and columns start and end, from wall elevations
    _column_stops(b, stats)           # columns the plan marks as ending at its floor, said
    _written_elsewhere(b, stats)
    _near_misses(b, stats)
    return dict(stats)


def _near(m: str, k: str) -> bool:
    """Two marks that may be one mark written two ways: S01 / S1 (leading zero),
    S1-2 / S12 (a separator), SL1 / S1 (one prefix inside the other, and at least
    one of them not a usual prefix - B12 and BC12 are two beams)."""
    if m == k:
        return False
    zeros = lambda x: re.sub(r"(?<=[A-Z])0+(?=\d)", "", x)      # noqa: E731
    flat = lambda x: re.sub(r"[^A-Z0-9]", "", x)                 # noqa: E731
    if zeros(m) == zeros(k) or flat(m) == flat(k):
        return True
    pm, pk = prefixes.prefix_of(m), prefixes.prefix_of(k)
    rm, rk = m[len(pm):], k[len(pk):]
    return bool(pm and pk and pm != pk and rm and rm == rk and (pm.startswith(pk) or pk.startswith(pm))
                and (pm not in prefixes.KNOWN or pk not in prefixes.KNOWN))


def _near_misses(b: Building, stats) -> None:
    """A mark on a floor with no schedule row of its own, where the drawings list a
    mark written almost the same way that this floor does not use: suggested in
    Review ("did you mean"), never matched - S1 and S01 can be two slabs."""
    listed: dict[str, set] = defaultdict(set)
    for s in b.of("spec"):
        if s.mark and s.props.get("by_column") is None and not s.extra.get("part"):
            listed[_set_of(b, s)].add(s.mark)
    used: dict = defaultdict(lambda: defaultdict(list))
    for e in b.elements:
        if e.type not in ("beam", "beam_span", "column", "slab", "wall") or gone(e) or e.extra.get("merged_into"):
            continue
        m = e.final("mark")
        if m:
            used[(_set_of(b, e), _level_key(b, e))][str(m)].append(e.id)
    for (set_key, lv), marks in sorted(used.items()):
        keys = listed.get(set_key, set())
        for m, ids in sorted(marks.items()):
            if m in keys:
                continue
            near = sorted(k for k in keys if k not in marks and _near(m, k))
            if not near:
                continue
            stats["marks with a near match to check"] += 1
            b.issues.append(Issue(INFO, "did you mean", f"{m} ({len(ids)} on {lv or 'this floor'}) has no schedule "
                                  f"row of its own; the drawings list {', '.join(near[:3])}, which this floor does "
                                  f"not use. If it is the same mark written another way, give {m} its size here - "
                                  "it is not matched automatically", elements=ids[:6], mark=m,
                                  extra={"from_linker": True, "near": near[:3]}))


SCOPED = ("column", "beam", "beam_span", "slab", "wall")


def _centre_of(e):
    g = e.geometry
    if g.get("x") is not None:
        return (g["x"], g["y"])
    if g.get("start") and g.get("end"):
        return ((g["start"][0] + g["end"][0]) / 2.0, (g["start"][1] + g["end"][1]) / 2.0)
    pts = g.get("outline") or []
    if len(pts) < 3:
        return None
    a = cx = cy = 0.0
    for i in range(len(pts)):
        (x0, y0), (x1, y1) = pts[i - 1][:2], pts[i][:2]
        k = x0 * y1 - x1 * y0
        a, cx, cy = a + k, cx + (x0 + x1) * k, cy + (y0 + y1) * k
    if abs(a) < 1e-9:
        return (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))
    return (cx / (3.0 * a), cy / (3.0 * a))


def _scope(b: Building, stats) -> None:
    """What lies in an area marked as another block or existing work (readers/scope.py)
    is not quietly this block's: each element there gets a 'scope' to decide - this
    block, or the other (then left out). Built marked until decided."""
    zones = defaultdict(list)
    for z in b.of("scope_zone"):
        zones[z.sheet].append(z)
    if not zones:
        return
    for sid, s in b.sheets.items():
        view = s.extra.get("view_sheet", "")
        if s.reader != "plan.legacy" or view not in zones:
            continue
        mine = [e for e in b.elements if e.sheet == sid and e.type in SCOPED and not gone(e)]
        for z in zones[view]:
            poly = z.geometry["outline"]
            inside = [e for e in mine if (c := _centre_of(e)) is not None and _inside(c, poly)]
            if not inside:
                continue
            layer = z.extra.get("layer", "")
            if len(inside) >= 0.8 * len(mine):
                b.issues.append(Issue(INFO, "scope", f"the area on layer '{layer}' covers nearly the whole plan: "
                                      "taken as this drawing's own block, nothing marked", sheet=sid,
                                      extra={"from_linker": True}))
                continue
            ev = [Evidence(reader=READER, sheet=sid, source="drawn on this plan: this block"),
                  Evidence(reader=READER, sheet=view, source=f"inside the area on layer '{layer}': other block",
                           layer=layer, handle=z.props["kind"].evidence[0].handle)]
            for e in inside:
                e.resolved["scope"] = Value(None, CONFLICT, 0.4, ev)
            n = defaultdict(int)
            for e in inside:
                n[e.type.replace("beam_span", "beam")] += 1
            stats["elements in an area marked as another block"] += len(inside)
            b.issues.append(Issue(WARNING, "scope", f"{len(inside)} elements (" + ", ".join(
                f"{v} {k}s" for k, v in sorted(n.items())) + f") lie inside the area drawn on layer '{layer}' - "
                "probably another block's structure, drawn for reference. They are built, marked, until you "
                "decide: 'this block', or 'other block' (left out)", sheet=sid,
                elements=[e.id for e in inside], extra={"from_linker": True}))


CLEAR_MM = 3000.0       # a column this far from every beam, slab and wall is not on the frame


def _to_segment(p, a, c) -> float:
    dx, dy = c[0] - a[0], c[1] - a[1]
    L2 = dx * dx + dy * dy
    t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / L2))
    return math.hypot(p[0] - a[0] - t * dx, p[1] - a[1] - t * dy)


def _off_the_frame(b: Building, stats) -> None:
    """A 'column' no beam, slab or wall of its plan comes near (CLEAR_MM) is not one of
    the plan's columns: the legend's sample square ('STUB COLUMN 300X300') or a detail
    drawn beside the plan (ALCAZAR plinth). It gets a scope read as 'other' - left out,
    said, and 'this block' in the review builds it. Not on a plan with no frame (a
    column layout), nor when many columns would go (the frame was not read)."""
    for sid, s in b.sheets.items():
        if s.reader != "plan.legacy":
            continue
        mine = [e for e in b.elements if e.sheet == sid and not gone(e)]
        segs, polys = [], []
        for e in mine:
            g = e.geometry
            if e.type in ("beam", "beam_span") and g.get("start") and g.get("end"):
                segs.append((g["start"][:2], g["end"][:2]))
            elif e.type in ("slab", "wall") and len(g.get("outline") or []) >= 3:
                pts = [p[:2] for p in g["outline"]]
                polys.append(pts)
                segs += list(zip(pts, pts[1:] + pts[:1]))
        cols = [e for e in mine if e.type == "column" and "scope" not in e.resolved]
        if not segs or not cols:
            continue
        lone = []
        for e in cols:
            c = _centre_of(e)
            if c is None or any(_inside(c, p) for p in polys):
                continue
            d = min(_to_segment(c, a, q) for a, q in segs)
            if d > CLEAR_MM:
                lone.append((e, d))
        if not lone or len(lone) > 0.1 * len(cols):
            continue
        for e, d in lone:
            ev = [Evidence(reader=READER, sheet=sid, source=f"stands clear of every beam, slab and wall of the "
                           f"plan ({d / 1000:.1f} m): a legend sample or a detail, not a column of this plan")]
            e.resolved["scope"] = Value("other: clear of every beam and slab", READ, 0.7, ev)
        stats["columns clear of the frame"] += len(lone)
        b.issues.append(Issue(WARNING, "scope", f"{len(lone)} column-like shapes stand clear of every beam, slab and "
                              f"wall of the plan (nearest {min(d for _, d in lone) / 1000:.1f} m away) - a legend's "
                              "sample square or a detail drawn beside the plan, not columns. Left out; choose "
                              "'this block' to build them", sheet=sid, elements=[e.id for e, _ in lone],
                              extra={"from_linker": True}))


BEYOND = ("column", "wall", "slab")


def _points_of(e) -> list:
    g = e.geometry
    pts = [p[:2] for p in (g.get("outline") or [])]
    pts += [g[k][:2] for k in ("start", "end") if g.get(k)]
    if g.get("x") is not None:
        pts.append((g["x"], g["y"]))
    return pts


def _beyond_the_beams(b: Building, stats) -> None:
    """Columns, walls and slabs lying wholly outside the plan's beams (more than CLEAR_MM
    beyond the box round every beam) are not this plan's structure: the architect's
    background drawing carried on past the structural plan (ALCAZAR 1st floor: its brick
    walls 35 m to one side, read as two columns and a slab). Scope read as 'other' - left
    out, said, 'this block' in the review builds them - as _off_the_frame. Only on a plan
    with beams, and not when many would go (the beams were not all read)."""
    for sid, s in b.sheets.items():
        if s.reader != "plan.legacy":
            continue
        mine = [e for e in b.elements if e.sheet == sid and not gone(e)]
        ends = [p[:2] for e in mine if e.type in ("beam", "beam_span")
                for p in (e.geometry.get("start"), e.geometry.get("end")) if p]
        built = [e for e in mine if e.type in BEYOND]
        if len(ends) < 4 or not built:
            continue
        x0, x1 = min(p[0] for p in ends) - CLEAR_MM, max(p[0] for p in ends) + CLEAR_MM
        y0, y1 = min(p[1] for p in ends) - CLEAR_MM, max(p[1] for p in ends) + CLEAR_MM

        def outside(e):             # its whole extent clear of the box
            pts = _points_of(e)
            return bool(pts) and (max(x for x, _ in pts) < x0 or min(x for x, _ in pts) > x1 or
                                  max(y for _, y in pts) < y0 or min(y for _, y in pts) > y1)
        far = [e for e in built if "scope" not in e.resolved and outside(e)]
        if not far or len(far) > 0.1 * len(built):
            continue
        for e in far:
            ev = [Evidence(reader=READER, sheet=sid, source="lies wholly beyond the plan's beams: the background "
                           "drawing carried on past the plan, not this plan's structure")]
            e.resolved["scope"] = Value("other: beyond every beam of the plan", READ, 0.7, ev)
        stats["elements beyond the plan's beams"] += len(far)
        n = defaultdict(int)
        for e in far:
            n[e.type] += 1
        b.issues.append(Issue(WARNING, "scope", f"{len(far)} shapes (" + ", ".join(
            f"{v} {k}s" for k, v in sorted(n.items())) + ") lie wholly beyond the plan's beams - drawing carried "
            "on past the structural plan (often the architect's background), not this plan's structure. Left out; "
            "choose 'this block' to build them", sheet=sid, elements=[e.id for e in far],
            extra={"from_linker": True}))


def _crosses(p1, p2, q1, q2) -> bool:
    def side(a, b, c):
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
    return side(p1, p2, q1) * side(p1, p2, q2) < 0 and side(q1, q2, p1) * side(q1, q2, p2) < 0


def _beam_sections(b: Building, stats) -> None:
    """A beam crossed on the plan by a section's cut letters (X ... X) takes that
    section's shape (readers/beam_sections.py). Which way round: the section's
    full-height side is where the slab meets the beam - the side of the beam the plan
    has slab on. Slab on both sides or on neither: a decision (as read / mirrored)."""
    for e in b.elements:
        for k in ("section_profile", "section_profile_mirrored", "section_part", "slab_strip",
                  "section_parts", "same_beam_as"):
            e.extra.pop(k, None)
    for s in b.sheets.values():
        s.extra.pop("section_gap_beams", None)
        s.extra.pop("section_edge_strips", None)
    secs = defaultdict(list)
    for s in b.of("beam_section"):
        secs[(_set_of(b, s), s.mark)].append(s)
    cuts = defaultdict(list)
    for c in b.of("section_cut"):
        cuts[c.sheet].append(c)
    if not secs or not cuts:
        return
    own = _own_sections(b)
    used, used_sec = defaultdict(list), {}
    sketched = defaultdict(lambda: defaultdict(list))         # (plan, section) -> what it did
    edge_jobs = []                                            # slab edges to follow, once every cut is read
    for sid, s in b.sheets.items():
        view = s.extra.get("view_sheet", "")
        if s.reader != "plan.legacy" or view not in cuts:
            continue
        beams = [e for e in b.elements if e.sheet == sid and e.type in ("beam", "beam_span")
                 and not gone(e) and not e.geometry.get("mid")
                 and e.geometry.get("start") and e.geometry.get("end")]
        slabs = [e.geometry["outline"] for e in b.elements if e.sheet == sid and e.type == "slab"
                 and len(e.geometry.get("outline") or []) >= 3]
        here = (s.extra.get("set", ""), _drawing_of(sid))
        for c in cuts[view]:
            cands = secs.get((here[0], c.mark)) or []
            mine = [x for x in cands if _drawing_of(x.sheet) == here[1]]
            # its own drawing's section of that name; another drawing's only when it has none
            sec = mine[-1] if mine else (None if c.mark.upper() in own.get(here, ()) else
                                          (cands[-1] if cands else None))
            if sec is None:
                continue
            hit = [e for e in beams if "section" not in e.resolved
                   and _crosses(c.geometry["a"], c.geometry["b"], e.geometry["start"], e.geometry["end"])]
            if sec.extra.get("drawn_as") == "lines":
                ref = _sized_by_sketch(hit, sec, sketched[(sid, sec.id)], beams, c)
                if ref is not None:
                    edge_jobs.append((view, ref, sec, beams, sketched[(sid, sec.id)]))
                continue
            used_sec[c.mark] = sec
            for grp in _parallel_groups(hit):
                done = _give_section(grp, sec, c, slabs)
                used[c.mark] += done
    for job in edge_jobs:
        _edge_strips(b, *job)
    _sketch_issues(b, sketched, stats)
    for name, els in used.items():
        sec = used_sec[name]
        stats["beams built to a drawn section"] += len(els)
        b.issues.append(Issue(INFO, "section", f"section {name}-{name} ({sec.get('width'):.0f} wide, "
                              f"{sec.get('depth'):.0f} deep, not a rectangle) is marked on the plan across "
                              f"{len(els)} beams: they are built to its shape", sheet=els[0].sheet,
                              elements=[e.id for e in els], mark=f"{name}-{name}", extra={"from_linker": True}))
        for h in sec.extra.get("hung") or []:
            b.issues.append(Issue(WARNING, "not modelled", f"section {name}-{name}: {h}", sheet=sec.sheet,
                                  elements=[e.id for e in els], mark=f"{name}-{name}", extra={"from_linker": True}))
        if sec.extra.get("undimensioned"):
            b.issues.append(Issue(WARNING, "confirm", f"section {name}-{name}: some corners have no written size "
                                  f"({', '.join(sec.extra['undimensioned'])}); they are kept in proportion - check",
                                  sheet=sec.sheet, mark=f"{name}-{name}", extra={"from_linker": True}))


def _offset(a, c) -> float | None:
    """How far c's centreline lies from a's (signed, to a's left), if they run the same way."""
    (px, py), (qx, qy) = a.geometry["start"][:2], a.geometry["end"][:2]
    (ex, ey), (fx, fy) = c.geometry["start"][:2], c.geometry["end"][:2]
    L, M = math.hypot(qx - px, qy - py) or 1.0, math.hypot(fx - ex, fy - ey) or 1.0
    ux, uy = (qx - px) / L, (qy - py) / L
    if abs(((fx - ex) / M) * uy - ((fy - ey) / M) * ux) > 0.035:
        return None
    mx, my = (ex + fx) / 2.0, (ey + fy) / 2.0
    return (mx - px) * -uy + (my - py) * ux


def _cut_line(hit, beams, cut, W) -> object | None:
    """The beam whose line a section's cut marks: one the cut crosses, drawn as wide as the
    section's beam and marked or sized; else, where the cut crosses only the slab's lines (NIFT
    5th floor Y23, X1-X2: the mark written on the outer strip, the inner strip drawn 300 wide), a
    beam like that further along whose line runs through the cut. Never a line merely drawn as
    wide: a slab strip can be. None when there is no such beam."""
    own = [a for a in hit if abs(_drawn_width(a) - W) <= 15.0 and _sized_beam(a, W)]
    if own:
        return own[0]
    (ax, ay), (bx, by) = cut.geometry["a"][:2], cut.geometry["b"][:2]
    best = None
    for e in beams:
        if e in hit or abs(_drawn_width(e) - W) > 15.0 or not _sized_beam(e, W):
            continue
        if not any(_offset(e, h) is not None for h in hit):
            continue                                    # not running the way the crossed lines do
        (px, py), (qx, qy) = e.geometry["start"][:2], e.geometry["end"][:2]
        L = math.hypot(qx - px, qy - py) or 1.0
        ux, uy = (qx - px) / L, (qy - py) / L
        sa, sb = (ax - px) * -uy + (ay - py) * ux, (bx - px) * -uy + (by - py) * ux
        if sa * sb > 0:
            continue                                    # its line does not pass through the cut
        t = ((ax + bx) / 2.0 - px) * ux + ((ay + by) / 2.0 - py) * uy
        far = max(0.0, -t, t - L)                       # how far along from the beam the cut is
        if best is None or far < best[0]:
            best = (far, e)
    return best[1] if best else None


def _slab_strips(hit, beams, sec, ref=None) -> dict:
    """The slab beside a beam, drawn on the plan as lines along it and read as beams.

    NIFT 5th floor, SECTION A-A: the 300 x 600 edge beam with its slab running 150 past the
    outer face and 300 past the inner one, both ends drawn. On the plan those slab edges are
    lines 150 outside and 300 inside the beam, which the plan reader pairs with the beam's
    faces into a 150 and a 300 wide 'beam' - with no mark and no depth. A beam the cut
    crosses as wide as a closed flange, lying that far off the section's own beam (the
    marked or sized one), is that strip of slab; so are its pieces carrying on in line, as
    wide, unmarked and with no depth, still beside a beam as wide as the section's.
    {beam id: flange}"""
    flanges = [f for f in sec.extra.get("flanges") or [] if f.get("closed")]
    if not flanges or not sec.extra.get("slab_thickness"):
        return {}
    W = float(sec.extra.get("full_width") or sec.get("width"))

    def width(e):
        try:
            return float(e.final("width") or e.get("width") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def loose(e):
        dv = e.resolved.get("depth")
        return not e.final("mark") and (dv is None or dv.status == MISSING)

    owners = [ref] if ref is not None else []
    if not owners:
        return {}
    ref_mark = _key_of(str(ref.final("mark") or "")) if ref.final("mark") else ""

    def named_for_line(e):
        # the beam's own mark written on its strip (NIFT Y23: '5B4A' on the outer strip):
        # it names the beam beside it, not the strip
        return bool(ref_mark) and _key_of(str(e.final("mark") or "")) == ref_mark
    # the beam line the cut marks: the section's beam and its spans in line with it, as wide
    # (NIFT 5th floor top edge: the cut 5B2 span drawn 74 mm off the line of the others)
    line = owners + [e for e in beams if e not in owners and abs(width(e) - W) <= 15.0
                     and any((o := _offset(a, e)) is not None and abs(o) <= W / 3.0 for a in owners)]

    def overlaps(a, c):
        (px, py), (qx, qy) = a.geometry["start"][:2], a.geometry["end"][:2]
        L = math.hypot(qx - px, qy - py) or 1.0
        ux, uy = (qx - px) / L, (qy - py) / L
        ts = sorted(((x - px) * ux + (y - py) * uy) for x, y in (c.geometry["start"][:2], c.geometry["end"][:2]))
        return ts[1] > 100.0 and ts[0] < L - 100.0
    found = {}
    for c in beams:
        if c in line or not (loose(c) or named_for_line(c)):
            continue
        plain = not width(c) or any("plain line" in str(n) for n in c.notes)
        drawn = _drawn_width(c) or width(c)          # as drawn: a mark may have sized it otherwise
        for a in line:
            off = _offset(a, c)
            if off is None or not overlaps(a, c):
                continue
            side = 1.0 if off > 0 else -1.0
            f = next((f for f in flanges if not plain and abs(drawn - f["width"]) <= 15.0
                      and abs(abs(off) - (W / 2.0 + f["width"] / 2.0)) <= 40.0), None)
            if f:
                found[c.id] = (c, f, 0.0)              # its two lines are the strip's two edges
                break
            f = next((f for f in flanges if plain and abs(abs(off) - (W / 2.0 + f["width"])) <= 40.0), None)
            if f:
                # one line: the slab's edge; the strip runs from it back to the beam's face -
                # toward the beam, said to the left or right of the line's own direction
                same = (_unit(a)[0] * _unit(c)[0] + _unit(a)[1] * _unit(c)[1]) > 0
                found[c.id] = (c, f, -side * f["width"] / 2.0 * (1.0 if same else -1.0))
                break
    return found


def _along(a, c) -> tuple[float, float]:
    """c's stretch along a's line: (from, to), mm from a's start."""
    (px, py), (qx, qy) = a.geometry["start"][:2], a.geometry["end"][:2]
    L = math.hypot(qx - px, qy - py) or 1.0
    ux, uy = (qx - px) / L, (qy - py) / L
    ts = sorted(((x - px) * ux + (y - py) * uy) for x, y in (c.geometry["start"][:2], c.geometry["end"][:2]))
    return ts[0], ts[1]


def _covered(span, others) -> float:
    """How much of a stretch (from, to) the other stretches cover, 0 to 1."""
    lo, hi = span
    if hi <= lo:
        return 0.0
    cuts = sorted((max(lo, a), min(hi, c)) for a, c in others if c > lo and a < hi)
    got, at = 0.0, lo
    for a, c in cuts:
        if c > at:
            got += c - max(a, at)
            at = c
    return got / (hi - lo)


def _section_faces(hit, beams, sec, strips, out, ref=None) -> None:
    """The lines along a beam line read pair by pair, as the section says they lie.

    NIFT 5th floor top edge: lines at -450, -150, +150 and +300 from 5B2's centre - the slab's
    inner edge, the beam's two faces, the slab's outer edge (SECTION A-A: 300 beam, slab 300 in,
    150 out). The plan reader pairs them every way: the beam (-150/+150), the strips (_slab_strips),
    and the beam with a strip in one (19: -150/+300, 450 wide, captioned 5B2; 30: -450/+150, 600).
      * a pair spanning the beam and a strip is that beam and that strip: marked or sized, the
        beam keeps its mark, on the line at the section's width (450 explained, not a conflict);
        unmarked, only its strip where another beam of the line already stands there;
      * an unmarked beam inside the stretch of a marked one on the line is that beam drawn twice;
      * an unmarked span of the line between two spans of one mark is that mark, at its size.
    Only pairs whose BOTH faces are the section's lines; anything else is left as it was."""
    flanges = [f for f in sec.extra.get("flanges") or [] if f.get("closed")]
    th = sec.extra.get("slab_thickness")
    if not flanges or not th:
        return
    W = float(sec.extra.get("full_width") or sec.get("width"))
    name = f"{sec.mark}-{sec.mark}"
    # the line is measured from the beam _cut_line found: drawn as wide as the section's and
    # marked or sized (the marked span may be a wider pair: NIFT 19, 450 = the beam with its
    # outer strip; or only the slab's lines crossed: NIFT Y23 X1-X2)
    if ref is None:
        return
    owners = [ref]
    edges = [W / 2.0 + f["width"] for f in flanges]
    reach = W / 2.0 + max(f["width"] for f in flanges) + 60.0

    def at(v, targets):
        return any(abs(v - t) <= 40.0 for t in targets)
    pieces = []                                   # (element, offset, along) on the line
    for c in beams:
        if c.id in strips or c.extra.get("slab_strip") or not _drawn_width(c) or \
                any("plain line" in str(n) for n in c.notes):
            continue
        o = _offset(ref, c)
        if o is None or abs(o) > reach:
            continue
        pieces.append((c, o, _along(ref, c)))
    line_beams = [p for p in pieces if abs(p[1]) <= W / 3.0 and abs(_drawn_width(p[0]) - W) <= 15.0]
    same = {}                                     # c id -> +1 / -1: runs as ref does, or against
    for c, o, _ in pieces:
        (px, py), (qx, qy) = ref.geometry["start"][:2], ref.geometry["end"][:2]
        (ex, ey), (fx, fy) = c.geometry["start"][:2], c.geometry["end"][:2]
        same[c.id] = 1.0 if (qx - px) * (fx - ex) + (qy - py) * (fy - ey) > 0 else -1.0
    merged = []
    for c, o, span in pieces:
        w = _drawn_width(c)
        lo, hi = o - w / 2.0, o + w / 2.0
        if abs(lo + W / 2.0) <= 40.0 and abs(hi - W / 2.0) <= 40.0:
            continue                              # the beam itself
        if not (lo <= -W / 2.0 + 40.0 and hi >= W / 2.0 - 40.0):
            continue                              # not across the beam: a strip, or not the section's
        if not ((abs(lo + W / 2.0) <= 40.0 or at(-lo, edges)) and (abs(hi - W / 2.0) <= 40.0 or at(hi, edges))):
            continue                              # a face that is none of the section's lines
        parts = []
        if hi > W / 2.0 + 40.0:
            parts.append((W / 2.0 + (hi - W / 2.0) / 2.0, hi - W / 2.0))
        if lo < -W / 2.0 - 40.0:
            parts.append((-(W / 2.0 + (-W / 2.0 - lo) / 2.0), -W / 2.0 - lo))
        merged.append((c, o, span, parts))
    marked = [(c, o, span) for c, o, span in line_beams if _sized_beam(c, W)]
    marked += [(c, o, span) for c, o, span, _ in merged if c.final("mark")]
    ev = Evidence(reader=READER, sheet=sec.sheet, source=f"section {name}: beam {W:.0f} wide, the slab "
                  + " / ".join(f"{f['width']:.0f}" for f in flanges) + f" past its faces, {th['mm']:.0f} thick",
                  text=sec.extra.get("title", ""))
    for c, o, span, parts in merged:
        keep_beam = bool(c.final("mark")) or _covered(span, [s for x, _, s in line_beams + marked if x is not c]) < 0.8
        sgn = same[c.id]
        c.extra["section_parts"] = {
            "section": name,
            "beam": {"shift": round(-o * sgn, 1), "width": W} if keep_beam else None,
            "strips": [{"shift": round((t - o) * sgn, 1), "width": round(f, 1), "thickness": th["mm"],
                        "written": bool(th.get("written"))} for t, f in parts]}
        wv = c.resolved.get("width")
        if keep_beam and wv is not None and wv.status == CONFLICT and abs(float(wv.value or 0) - W) <= 15.0:
            # the plan's wider pair is the beam with its slab beside it: no conflict left
            c.resolved["width"] = Value(W, READ, 0.9, [ev] + wv.evidence)
        if not keep_beam:
            c.resolved["kind"] = Value("slab edge strip", READ, 0.85, [ev])
            dv = c.resolved.get("depth")
            if dv is not None and dv.status == MISSING:
                c.resolved["depth"] = Value(None, MISSING, 0.0, [])
        out["merged"].append(c)
    # the same beam drawn twice: an unmarked one inside a marked one's stretch on the line
    twice = set()
    for c, o, span in line_beams:
        if c in owners or not _loose_beam(c):
            continue
        for m, _, mspan in marked:
            if m is not c and _covered(span, [mspan]) >= 0.8:
                c.extra["same_beam_as"] = m.id
                c.resolved["kind"] = Value(f"beam {m.final('mark') or ''} drawn twice", READ, 0.85, [ev])
                twice.add(c.id)
                out["twice"].append(c)
                break
    # unmarked spans of the line between two spans of one mark
    for c, o, span in line_beams:
        if c.id in twice or not _loose_beam(c):
            continue
        mid = (span[0] + span[1]) / 2.0
        before = [(s[1], m) for m, _, s in marked if s[1] <= mid]
        after = [(s[0], m) for m, _, s in marked if s[0] >= mid]
        if not before or not after:
            continue
        left, right = max(before, key=lambda x: x[0])[1], min(after, key=lambda x: x[0])[1]
        mk = left.final("mark")
        if not mk or _key_of(str(mk)) != _key_of(str(right.final("mark") or "")):
            continue
        lev = Evidence(reader=READER, sheet=c.sheet, source=f"beam line: between two spans of {mk} on the line "
                       f"section {name} marks, as wide", text=str(mk))
        c.resolved["mark"] = Value(mk, READ, 0.75, [lev])
        for p in ("width", "depth"):
            v = left.resolved.get(p)
            if v is not None and v.status in (READ, OVERRIDE) and v.value:
                c.resolved[p] = Value(v.value, READ, 0.75, [lev] + v.evidence[:2])
        out["line"].append(c)
    _beam_line_gaps(ref, line_beams, merged, strips, twice, W, name, out)


def _beam_line_gaps(ref, line_beams, merged, strips, twice, W, name, out) -> None:
    """The beam where the plan reader made only its slab strips, and no beam twice.

    With the beam's two faces and both slab edges drawn along a stretch (NIFT 5th floor, Y12-Y13),
    the reader may pair each face with the slab edge beside it: two strips, no beam. The strips'
    inner edges ARE the beam's faces - so between two spans of one mark on the line, a gap lined
    (80 %) by this section's strips is that beam, at that mark and size. And a pair split into beam +
    strip (_section_faces) starts its beam where a span of the line already standing ends."""
    pieces = []                                          # (from, to, element) beams standing on the line
    for c, o, span in line_beams:
        if c.id not in twice and (c.final("mark") or not _loose_beam(c)):
            pieces.append((span[0], span[1], c))
    for c, o, span, _ in merged:
        bp = (c.extra.get("section_parts") or {}).get("beam")
        if not bp:
            continue
        lo, hi = span
        for a, z, _x in pieces:                          # trimmed where a span of the line stands
            if a <= lo + 100.0 < z:
                lo = max(lo, z)
            if a < hi - 100.0 <= z:
                hi = min(hi, a)
        if hi - lo < 100.0:
            continue
        # kept along c's own direction: mm from its start
        bp["from"], bp["to"] = ((round(lo - span[0], 1), round(hi - span[0], 1)) if _dir_same(ref, c)
                                else (round(span[1] - hi, 1), round(span[1] - lo, 1)))
        if bp["from"] > 1.0 or bp["to"] < span[1] - span[0] - 1.0:
            out["trimmed"].append(c)
        pieces.append((lo, hi, c))
    pieces.sort(key=lambda p: p[0])
    lined = [_along(ref, e) for e, _f, _s in strips.values()] + \
        [span for c, _o, span, parts in merged if parts]
    (px, py), (qx, qy) = ref.geometry["start"][:2], ref.geometry["end"][:2]
    L = math.hypot(qx - px, qy - py) or 1.0
    ux, uy = (qx - px) / L, (qy - py) / L
    for (a0, a1, left), (b0, b1, right) in zip(pieces, pieces[1:]):
        mk = left.final("mark")
        if not mk or _key_of(str(mk)) != _key_of(str(right.final("mark") or "")) or b0 - a1 <= 2.0 * W:
            continue                                     # (a break the width of a column is the column)
        if _covered((a1, b0), lined) < 0.8:
            continue
        dv, wv = left.resolved.get("depth"), left.resolved.get("width")
        if dv is None or dv.status not in (READ, OVERRIDE) or not dv.value:
            continue
        out["gap_beams"].append({"start": [px + ux * a1, py + uy * a1], "end": [px + ux * b0, py + uy * b0], "mark": mk,
                     "width": float(wv.value) if wv is not None and wv.value else W, "depth": float(dv.value),
                     "section": name, "between": [left.id, right.id]})
    # past the line's last beam: where the section's strips line BOTH sides (the outer and the
    # inner slab edge drawn, so both beam faces are there - NIFT Y23, X1-X2: '5B4A' written on
    # the outer strip, no beam pair), the beam carries on as far as both strips go
    if not pieces:
        return
    sides = {1: [], -1: []}
    for e, _f, shift in strips.values():
        o = _offset(ref, e)
        if o is not None:
            o += shift if _dir_same(ref, e) else -shift
            sides[1 if o > 0 else -1].append(_along(ref, e))
    for c, _o, span, parts in merged:
        for t, _f in parts:
            sides[1 if t > 0 else -1].append(span)
    both = [(max(a, c), min(z, d)) for a, z in _union(sides[1]) for c, d in _union(sides[-1])
            if min(z, d) - max(a, c) > 0]
    for (p0, p1, end), outward in ((pieces[0], -1), (pieces[-1], 1)):
        mk = end.final("mark")
        dv, wv = end.resolved.get("depth"), end.resolved.get("width")
        if not mk or dv is None or dv.status not in (READ, OVERRIDE) or not dv.value:
            continue
        at = p0 if outward < 0 else p1
        for lo, hi in both:
            far = lo if outward < 0 else hi
            near = hi if outward < 0 else lo
            if outward * (far - at) > 2.0 * W and outward * (at - near) >= -2.0 * W:
                a, z = sorted((at, far))
                out["gap_beams"].append({"start": [px + ux * a, py + uy * a], "end": [px + ux * z, py + uy * z],
                                         "mark": mk, "width": float(wv.value) if wv is not None and wv.value else W,
                                         "depth": float(dv.value), "section": name, "between": [end.id]})


def _union(spans) -> list:
    """Stretches (from, to) joined where they overlap."""
    out: list[list[float]] = []
    for a, z in sorted(spans):
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], z)
        else:
            out.append([a, z])
    return [tuple(x) for x in out]


def _dir_same(a, c) -> bool:
    (px, py), (qx, qy) = a.geometry["start"][:2], a.geometry["end"][:2]
    (ex, ey), (fx, fy) = c.geometry["start"][:2], c.geometry["end"][:2]
    return (qx - px) * (fx - ex) + (qy - py) * (fy - ey) > 0


def _beam_width(e) -> float:
    try:
        return float(e.final("width") or e.get("width") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _drawn_width(e) -> float:
    """The width the plan draws it at (its two lines apart), whatever it is resolved to."""
    try:
        return float(e.get("width") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _loose_beam(e) -> bool:
    """No mark and no depth of its own."""
    dv = e.resolved.get("depth")
    return not e.final("mark") and (dv is None or dv.status == MISSING)


def _sized_beam(e, W) -> bool:
    """As wide as a section's beam, and marked or given a depth."""
    dv = e.resolved.get("depth")
    return abs(_beam_width(e) - W) <= 15.0 and (bool(e.final("mark")) or (dv is not None and dv.status in (READ, OVERRIDE)))


def _beam_face_runs(b: Building, stats) -> None:
    """A beam runs as far as both its faces are drawn.

    NIFT 4th floor, 4B2 by the stair: both faces drawn the whole bay (4.85 m, 4B6A to 4B9), but
    lines of the stair close beside it drew the plan reader's pair off - it stopped at 2.8 m, and
    the last 2 m of the beam was never made. Read from the plan's own beam-layer lines (readers/
    linework): where a beam's two face lines (its width apart) carry on past its end, in line,
    and no beam of the line stands there, it carries on to where its faces end - its own mark and
    size (marked or sized only: nothing guessed). Faces end at the supports. Kept with the beams
    _beam_line_gaps finds (sheet extra "section_gap_beams"), built the same way."""
    for sid, s in b.sheets.items():
        vs = b.sheets.get(s.extra.get("view_sheet", ""))
        lines = ((vs.extra.get("beam_lines") or {}).get("lines") or []) if vs else []
        if s.reader != "plan.legacy" or not lines:
            continue
        beams = [e for e in b.elements if e.sheet == sid and e.type in ("beam", "beam_span") and not gone(e)
                 and e.geometry.get("start") and e.geometry.get("end") and not e.geometry.get("mid")
                 and not any(e.extra.get(k) for k in ("slab_strip", "same_beam_as", "not_a_beam", "section_parts"))]
        made = s.extra.setdefault("section_gap_beams", [])
        for e in beams:
            w = _drawn_width(e)
            dv, wv = e.resolved.get("depth"), e.resolved.get("width")
            if not w or not e.final("mark") or dv is None or dv.status not in (READ, OVERRIDE) or not dv.value:
                continue
            (px, py), (qx, qy) = e.geometry["start"][:2], e.geometry["end"][:2]
            L = math.hypot(qx - px, qy - py)
            if L <= 0:
                continue
            ux, uy = (qx - px) / L, (qy - py) / L
            tol = min(40.0, w / 6.0)
            faces = {1.0: [], -1.0: []}
            for _lay, x0, y0, x1, y1, _dash in lines:
                a0, a1 = (x0 - px) * ux + (y0 - py) * uy, (x1 - px) * ux + (y1 - py) * uy
                o0, o1 = (x0 - px) * -uy + (y0 - py) * ux, (x1 - px) * -uy + (y1 - py) * ux
                if abs(a1 - a0) < 1.0 or abs(o1 - o0) > 0.01 * abs(a1 - a0) + 2.0:
                    continue
                for side in (1.0, -1.0):
                    if abs((o0 + o1) / 2.0 - side * w / 2.0) <= tol:
                        faces[side].append(tuple(sorted((a0, a1))))
            # pieces of a face 100 mm apart or less are one face (a dash pattern drawn as lines)
            joined = {k: [(a + 50.0, z - 50.0) for a, z in _union([(a - 50.0, z + 50.0) for a, z in v])]
                      for k, v in faces.items()}
            both = [(max(a, c), min(z, d)) for a, z in joined[1.0] for c, d in joined[-1.0] if min(z, d) - max(a, c) > 0]
            # the rest of the line: other beams in line, as wide, and pieces already made
            others = []
            for o in b.elements:
                if o is e or o.sheet != sid or o.type not in ("beam", "beam_span") or gone(o) \
                        or not o.geometry.get("start") or o.extra.get("slab_strip") or o.extra.get("not_a_beam"):
                    continue
                oo = _offset(e, o)
                if oo is None:
                    continue
                # a beam already standing over most of its width there, whatever its own width
                # (Sasan Gir terrace: B4's line runs inside B2E, the 750 edge beam - B2E's outer
                # face and the slope line drawn inside it are 230 apart, like B4's two faces)
                wo = _drawn_width(o) or _beam_width(o)
                over = min(w / 2.0, oo + wo / 2.0) - max(-w / 2.0, oo - wo / 2.0)
                if over >= 0.5 * w or ((o.extra.get("section_parts") or {}).get("beam") and abs(oo) <= w):
                    others.append(_along(e, o))           # a beam of the line (a split pair's too)
            for g in made:
                ga = (g["start"][0] - px) * ux + (g["start"][1] - py) * uy
                gz = (g["end"][0] - px) * ux + (g["end"][1] - py) * uy
                go = ((g["start"][0] + g["end"][0]) / 2.0 - px) * -uy + ((g["start"][1] + g["end"][1]) / 2.0 - py) * ux
                if abs(go) <= w / 3.0:
                    others.append(tuple(sorted((ga, gz))))
            for lo, hi in both:
                stretches = []                            # where its faces carry on past its end / before its start
                if lo <= L + 50.0 and hi > L:
                    stretches.append((L, hi))
                if lo < 0.0 and hi >= -50.0:
                    stretches.append((lo, 0.0))
                for a, z in stretches:
                    if z - a <= 2.0 * w:
                        continue
                    if _covered((a, z), _union(others)) > 0.2:
                        continue                          # a beam of the line stands there
                    made.append({"start": [px + ux * a, py + uy * a], "end": [px + ux * z, py + uy * z],
                                 "mark": e.final("mark"), "width": float(wv.value) if wv is not None and wv.value else w,
                                 "depth": float(dv.value), "section": "", "between": [e.id],
                                 "why": f"its two faces are drawn {z - a:.0f} mm further than the plan reader's beam"})
                    others.append((a, z))
                    b.issues.append(Issue(INFO, "geometry", f"beam {e.final('mark')}: both its faces drawn {z - a:.0f} mm "
                                          "past where the plan reader's beam stops - carried on to where they end",
                                          sheet=sid, elements=[e.id], mark=str(e.final("mark")),
                                          extra={"from_linker": True}))
                    stats["beams carried on along their drawn faces"] += 1


def _reseat_on_faces(b: Building, stats) -> None:
    """A marked beam the plan reader paired with the wrong line: put between its two drawn faces.

    NIFT 3rd floor top edge, 3B1 (detail 300 x 600): its faces are two dashed lines 300 apart,
    the slab's edge a solid line 150 outside them. The reader paired the slab edge with the
    outer face - 3B1 150 wide, 75 out of place - and left the inner face as loose lines, built as
    beams with no width. Where a marked beam's width from its detail or schedule disagrees with
    the lines it was paired from, and the plan draws two lines exactly that width apart along it
    (80 % of its length), one of them a face it was paired from, it stands between those two -
    at that width (its parts kept as section_parts.beam, built as the split pairs are). Loose lines
    (no width, no mark) lying on those faces are those faces, not beams (extra not_a_beam)."""
    for sid, s in b.sheets.items():
        vs = b.sheets.get(s.extra.get("view_sheet", ""))
        lines = ((vs.extra.get("beam_lines") or {}).get("lines") or []) if vs else []
        if s.reader != "plan.legacy" or not lines:
            continue
        els = [e for e in b.elements if e.sheet == sid and e.type in ("beam", "beam_span") and not gone(e)
               and e.geometry.get("start") and e.geometry.get("end") and not e.geometry.get("mid")]
        for e in els:
            if any(e.extra.get(k) for k in ("slab_strip", "section_parts", "same_beam_as", "not_a_beam")):
                continue
            w, wv = _drawn_width(e), e.resolved.get("width")
            if not w or not e.final("mark") or wv is None or wv.status not in (READ, OVERRIDE, CONFLICT) or not wv.value:
                continue
            given = any(any(k in ev.source for k in ("detail caption", "schedule table", "review")) for ev in wv.evidence)
            W = float(wv.value) if given else None        # a width its detail / schedule gives, if any
            if W is not None and abs(W - w) <= 15.0:
                continue
            (px, py), (qx, qy) = e.geometry["start"][:2], e.geometry["end"][:2]
            L = math.hypot(qx - px, qy - py)
            if L < 600.0:
                continue
            ux, uy = (qx - px) / L, (qy - py) / L
            near = []                                     # (offset, stretch, dashed) of the lines along it
            for _lay, x0, y0, x1, y1, d in lines:
                a0, a1 = (x0 - px) * ux + (y0 - py) * uy, (x1 - px) * ux + (y1 - py) * uy
                o0, o1 = (x0 - px) * -uy + (y0 - py) * ux, (x1 - px) * -uy + (y1 - py) * ux
                if abs(a1 - a0) < 1.0 or abs(o1 - o0) > 0.01 * abs(a1 - a0) + 2.0:
                    continue
                o = (o0 + o1) / 2.0
                if abs(o) <= 760.0 + w:
                    near.append((o, tuple(sorted((a0, a1))), bool(d)))
            # one line drawn in pieces sits a few mm differently bay to bay: within 15 mm, one line
            groups: list[list] = []
            for o, span, d in sorted(near):
                if groups and o - groups[-1][-1][0] <= 15.0:
                    groups[-1].append((o, span, d))
                else:
                    groups.append([(o, span, d)])
            faces = {}                                    # offset -> drawn dashed, for lines along 80 % of it
            for g in groups:
                if _covered((0.0, L), _union([sp for _o, sp, _d in g])) >= 0.8:
                    faces[sum(o for o, _sp, _d in g) / len(g)] = all(d for _o, _sp, d in g)
            own = [f for f in faces if abs(abs(f) - w / 2.0) <= 15.0]          # lines it was paired from
            best = None
            if W is not None:
                # the width its detail gives: two lines that far apart, one of its own
                for f1 in own:
                    for f2 in faces:
                        if abs(abs(f2 - f1) - W) <= 10.0 and (best is None or abs(abs(f2 - f1) - W) < best[0]):
                            best = (abs(abs(f2 - f1) - W), f1, f2)
                why = f"its two faces drawn {W:.0f} apart, as its {'detail' if 'detail' in str(wv.evidence) else 'schedule'} gives it"
            elif len(own) == 2 and faces[own[0]] != faces[own[1]]:
                # no width given: paired from a solid line (the slab's edge, seen from above) and a
                # dashed one (a face under the slab) - its other face is the next dashed line past
                # the dashed one, 150-750 away: a beam's faces are both under the slab
                fd = next(f for f in own if faces[f])
                fs = next(f for f in own if not faces[f])
                side = 1.0 if fd > fs else -1.0
                beyond = sorted((f for f, dsh in faces.items() if dsh and side * (f - fd) >= 150.0
                                 and side * (f - fd) <= 750.0), key=lambda f: side * (f - fd))
                if beyond:
                    f2 = beyond[0]
                    W = float(round(abs(f2 - fd) / 5.0) * 5.0)
                    # only a width this plan's beams are drawn at (3 or more): NIFT 3B1 300, as its
                    # neighbours; never a width nothing else has (Sasan Gir hotel: 230 -> 415, a guess)
                    if sum(1 for o in els if o is not e and abs(_drawn_width(o) - W) <= 10.0) >= 3:
                        best = (0.0, fd, f2)
                why = (f"paired from the slab's edge (solid) and a face (dashed); its faces are the two dashed lines "
                       f"{W or 0:.0f} apart")
            if best is None:
                continue
            _, f1, f2 = best
            shift = (f1 + f2) / 2.0
            # never onto a beam already standing there (Akanistha B2: moved onto its faces it would
            # have lain 0.9 m over the next B2 span of the same line) - then left as it was
            taken = False
            for o in els:
                if o is e or any(o.extra.get(k) for k in ("slab_strip", "not_a_beam", "same_beam_as")):
                    continue
                oo = _offset(e, o)
                if oo is None or abs(oo - shift) > W / 3.0:
                    continue
                oa, oz = _along(e, o)
                if min(oz, L) - max(oa, 0.0) > 0.2 * L:
                    taken = True
                    break
            if taken:
                continue
            ev = Evidence(reader=READER, sheet=sid, source=why, text=str(e.final("mark")))
            strips = []
            if not given:
                e.resolved["width"] = Value(W, READ, 0.8, [ev] + wv.evidence[:2])
                # the slab runs out to its edge (the solid line): the band the reader paired, between
                # that edge and the beam's face, is slab - as thick as the slab in the bay beside it
                t = _slab_beside(b, sid, e, shift, W)
                if t is not None:
                    strips.append({"shift": 0.0, "width": round(w, 1), "thickness": float(t.value), "written": True})
            e.extra["section_parts"] = {"section": "", "beam": {"shift": round(shift, 1), "width": W}, "strips": strips}
            if wv.status == CONFLICT:
                e.resolved["width"] = Value(W, READ, 0.9, [ev] + wv.evidence)
            # loose lines on those faces are the faces
            for o in els:
                if o is e or o.final("mark") or _drawn_width(o) or o.extra.get("not_a_beam"):
                    continue
                oo = _offset(e, o)
                if oo is None or min(abs(oo - f1), abs(oo - f2)) > 15.0:
                    continue
                oa, oz = _along(e, o)
                if oa >= -100.0 and oz <= L + 100.0:
                    o.extra["not_a_beam"] = f"a face of beam {e.final('mark')} (its two faces drawn {W:.0f} apart)"
            b.issues.append(Issue(INFO, "geometry", f"beam {e.final('mark')}: paired from lines {w:.0f} apart, but its "
                                  f"faces are drawn {W:.0f} apart as its detail gives - built between them",
                                  sheet=sid, elements=[e.id], mark=str(e.final("mark")), extra={"from_linker": True}))
            stats["beams put between their drawn faces"] += 1


def _slab_edge_bands(b: Building, stats) -> None:
    """The slab running past a beam to its drawn edge, read off the linework (no section needed).

    NIFT 4th floor east end: the beam's faces are dashed (under the slab), the slab's edge a
    solid line 150 outside (seen from above). The reader paired the solid edge with the outer
    face - an unmarked 150 wide 'beam' with no depth, built red - and it stopped 2.1 m short of
    where both lines run. Such a pair (no mark, no depth; one solid line, one dashed line), whose
    dashed line is the face of a beam standing beside it, is the slab's edge band: built as slab,
    as thick as the slab on the beam's other side, along wherever the solid edge and that face are
    both drawn (sheet extra section_edge_strips). No slab beside the beam: nothing guessed."""
    from shapely.geometry import LineString, Point, Polygon
    for sid, s in b.sheets.items():
        vs = b.sheets.get(s.extra.get("view_sheet", ""))
        lines = ((vs.extra.get("beam_lines") or {}).get("lines") or []) if vs else []
        if s.reader != "plan.legacy" or not lines:
            continue
        els = [e for e in b.elements if e.sheet == sid and e.type in ("beam", "beam_span") and not gone(e)
               and e.geometry.get("start") and e.geometry.get("end") and not e.geometry.get("mid")]
        standing = [m for m in els if not _loose_beam(m) and _drawn_width(m)
                    and not any(m.extra.get(k) for k in ("slab_strip", "not_a_beam", "same_beam_as"))]
        slabs = [(Polygon([tuple(p[:2]) for p in x.geometry["outline"]]).buffer(0), x.resolved["thickness"])
                 for x in b.elements if x.sheet == sid and x.type == "slab" and len(x.geometry.get("outline") or []) >= 3
                 and x.resolved.get("thickness") is not None and x.resolved["thickness"].status in (READ, OVERRIDE)
                 and x.resolved["thickness"].value]
        for e in els:
            w = _drawn_width(e)
            if not _loose_beam(e) or not w or w > 400.0 or any(
                    e.extra.get(k) for k in ("slab_strip", "not_a_beam", "same_beam_as", "section_parts")):
                continue
            (px, py), (qx, qy) = e.geometry["start"][:2], e.geometry["end"][:2]
            L = math.hypot(qx - px, qy - py)
            if L < 600.0:
                continue
            ux, uy = (qx - px) / L, (qy - py) / L
            sides = {1.0: [], -1.0: []}                 # its two lines: stretches and dashed or not
            for _lay, x0, y0, x1, y1, d in lines:
                a0, a1 = (x0 - px) * ux + (y0 - py) * uy, (x1 - px) * ux + (y1 - py) * uy
                o0, o1 = (x0 - px) * -uy + (y0 - py) * ux, (x1 - px) * -uy + (y1 - py) * ux
                if abs(a1 - a0) < 1.0 or abs(o1 - o0) > 0.01 * abs(a1 - a0) + 2.0:
                    continue
                for side in (1.0, -1.0):
                    if abs((o0 + o1) / 2.0 - side * w / 2.0) <= 15.0:
                        sides[side].append((tuple(sorted((a0, a1))), bool(d)))
            kind = {}
            for side, got in sides.items():
                on = [sp for sp, _d in got]
                if on and _covered((0.0, L), _union(on)) >= 0.8:
                    kind[side] = all(d for _sp, d in got)
            if len(kind) != 2 or kind[1.0] == kind[-1.0]:
                continue                                  # not one solid and one dashed line
            face = 1.0 if kind[1.0] else -1.0             # the dashed line: a beam's face
            m = None
            for cand in standing:
                oo = _offset(e, cand)
                if oo is None or abs(abs(oo - face * w / 2.0) - _drawn_width(cand) / 2.0) > 15.0 \
                        or face * (oo - face * w / 2.0) <= 0:
                    continue                              # its face is not on that dashed line, beyond it
                ca, cz = _along(e, cand)
                if min(cz, L) - max(ca, 0.0) > 0.3 * L:
                    m = cand
                    break
            if m is None:
                continue
            # the slab on the beam's other side: its thickness
            beyond = face * (w / 2.0 + _drawn_width(m) + 300.0)
            probe = LineString([(px + ux * 0.25 * L - uy * beyond, py + uy * 0.25 * L + ux * beyond),
                                (px + ux * 0.75 * L - uy * beyond, py + uy * 0.75 * L + ux * beyond)])
            t = next((th for poly, th in slabs if poly.buffer(300.0).intersects(probe)), None)
            if t is None:
                continue
            ev = Evidence(reader=READER, sheet=sid, source=f"the slab's edge (solid) beside beam {m.final('mark') or ''}'s "
                          f"face (dashed): slab {float(t.value):.0f} thick, as beside it", text="")
            e.extra["slab_strip"] = {"section": "", "width": round(w, 1), "thickness": float(t.value),
                                     "written": True, "shift": 0.0}
            e.resolved["kind"] = Value("slab edge strip", READ, 0.8, [ev])
            b.issues = [i for i in b.issues if not (i.category == "missing" and i.elements == [e.id])]
            stats["slab edge bands read off the linework"] += 1
            # ... along wherever the solid edge and the beam's dashed face are both drawn
            solid_side = -face
            edge_at, face_at = solid_side * w / 2.0, face * w / 2.0
            runs = {"edge": [], "face": []}
            for _lay, x0, y0, x1, y1, d in lines:
                a0, a1 = (x0 - px) * ux + (y0 - py) * uy, (x1 - px) * ux + (y1 - py) * uy
                o0, o1 = (x0 - px) * -uy + (y0 - py) * ux, (x1 - px) * -uy + (y1 - py) * ux
                if abs(a1 - a0) < 1.0 or abs(o1 - o0) > 0.01 * abs(a1 - a0) + 2.0:
                    continue
                o = (o0 + o1) / 2.0
                if abs(o - edge_at) <= 15.0 and not d:
                    runs["edge"].append(tuple(sorted((a0, a1))))
                elif abs(o - face_at) <= 15.0 and d:
                    runs["face"].append(tuple(sorted((a0 - 50.0, a1 + 50.0))))
            both = [(max(a, c), min(z, dd)) for a, z in _union(runs["edge"]) for c, dd in _union(runs["face"])
                    if min(z, dd) - max(a, c) > 0]
            have = s.extra.setdefault("section_edge_strips", [])
            for lo, hi in both:
                for a, z in _uncovered((lo, hi), [(0.0, L)]):
                    if z - a <= 600.0:
                        continue
                    h = w / 2.0
                    pts = [(a, -h), (z, -h), (z, h), (a, h)]
                    have.append({"outline": [[px + ux * tt - uy * oo, py + uy * tt + ux * oo] for tt, oo in pts],
                                 "width": round(w, 1), "thickness": float(t.value), "written": True, "section": ""})


def _slab_beside(b: Building, sid: str, e, shift: float, W: float):
    """The settled thickness of the slab against a beam's inner face (the side away from the
    shift's slab edge) - a slab within 600 mm of that face along its middle half - or None."""
    from shapely.geometry import LineString, Polygon
    (px, py), (qx, qy) = e.geometry["start"][:2], e.geometry["end"][:2]
    L = math.hypot(qx - px, qy - py) or 1.0
    ux, uy = (qx - px) / L, (qy - py) / L
    inner = shift + (W / 2.0 if shift > 0 else -W / 2.0)          # the face away from the edge band at 0
    side = 1.0 if shift > 0 else -1.0
    probe = inner + side * 300.0
    seg = LineString([(px + ux * 0.25 * L - uy * probe, py + uy * 0.25 * L + ux * probe),
                      (px + ux * 0.75 * L - uy * probe, py + uy * 0.75 * L + ux * probe)])
    for s in b.elements:
        o = s.geometry.get("outline") or []
        if s.sheet != sid or s.type != "slab" or len(o) < 3:
            continue
        t = s.resolved.get("thickness")
        if t is None or t.status not in (READ, OVERRIDE) or not t.value:
            continue
        if Polygon([tuple(p[:2]) for p in o]).buffer(0).buffer(300.0).intersects(seg):
            return t
    return None


def _marked_bays(b: Building, stats) -> None:
    """A bay closed by beams, with a slab mark written in it and no slab there, is that slab.

    slab_panels.make makes panels from the beam bays only on a plan where the plan reader found
    no slab at all. NIFT's plans have some slab outlines, so its marked bays without one (2nd
    floor S3, S3, S4; 4th floor S2 by the stair) had no slab. Here, once the beams are settled
    (the pieces _beam_line_gaps and _beam_face_runs added close bays too): any bay of the beam
    network holding one slab mark, that no slab covers, gets a panel - its thickness from a
    settled slab of that mark on the floor, else that mark's slab schedule row. Bays with no
    mark, two marks, or a slab already over part of them are left alone."""
    from shapely.geometry import LineString, Point, Polygon
    from shapely.ops import unary_union
    from .export import beam_line
    from .slab_panels import _snap
    for sid, s in b.sheets.items():
        if s.reader != "plan.legacy":
            continue
        els = [e for e in b.elements if e.sheet == sid]
        slabs = []
        for e in els:
            o = e.geometry.get("outline") or []
            if e.type == "slab" and len(o) >= 3 and not (e.resolved.get("excluded") and e.resolved["excluded"].value):
                slabs.append((Polygon([tuple(p[:2]) for p in o]).buffer(0), e))
        caps = [c for c in b.of("caption") if c.sheet == s.extra.get("view_sheet", "") and c.get("family") == "slab"
                and c.mark and c.geometry.get("at")]
        open_caps = [c for c in caps if not any(p.buffer(10.0).contains(Point(*c.geometry["at"][:2])) for p, _ in slabs)]
        if not open_caps:
            continue
        cols = [(e.geometry["x"], e.geometry["y"]) for e in els if e.type == "column" and e.geometry.get("x") is not None]
        lines = []
        for e in els:
            if e.type not in ("beam", "beam_span") or gone(e) or not e.geometry.get("start") or not e.geometry.get("end") \
                    or e.geometry.get("mid") or any(e.extra.get(k) for k in ("slab_strip", "not_a_beam", "same_beam_as")):
                continue
            ends = (e.geometry["start"][:2], e.geometry["end"][:2])
            if e.extra.get("section_parts"):
                ends = beam_line(e)
                if ends is None:
                    continue
            w = _drawn_width(e) or 230.0
            a, c = _snap(tuple(ends[0]), cols, max(600.0, 2 * w)), _snap(tuple(ends[1]), cols, max(600.0, 2 * w))
            if math.dist(a, c) > 1.0:
                lines.append(LineString([a, c]))
        for e in els:
            # an RC wall closes a bay as a beam does (NIFT 2nd floor S3, S3, S4: their far side is
            # the RCC wall) - its runs' centrelines; masonry never
            if e.type != "wall" or e.final("role") == "masonry" or e.resolved.get("excluded"):
                continue
            for r in e.extra.get("runs") or []:
                if r.get("start") and r.get("end"):
                    reach = max(600.0, 2 * float(r.get("thickness") or 230.0))
                    a, c = _snap(tuple(r["start"][:2]), cols, reach), _snap(tuple(r["end"][:2]), cols, reach)
                    if math.dist(a, c) > 1.0:
                        lines.append(LineString([a, c]))
        # ... and an RC wall the plan reader did not make an element of: two parallel lines on an RC
        # wall layer 100-450 apart (readers/linework, sheet wall_lines) - its centreline where both run
        vw = b.sheets.get(s.extra.get("view_sheet", ""))
        wl = [((x0, y0), (x1, y1)) for x0, y0, x1, y1 in ((vw.extra.get("wall_lines") or []) if vw else [])]
        for i, (a0, a1) in enumerate(wl):
            La = math.dist(a0, a1)
            ux, uy = (a1[0] - a0[0]) / La, (a1[1] - a0[1]) / La
            for b0, b1 in wl[i + 1:]:
                Lb = math.dist(b0, b1)
                if abs(ux * (b1[1] - b0[1]) / Lb - uy * (b1[0] - b0[0]) / Lb) > 0.01:
                    continue
                o = (b0[0] - a0[0]) * -uy + (b0[1] - a0[1]) * ux
                if not 100.0 <= abs(o) <= 450.0:
                    continue
                ts = sorted(((b0[0] - a0[0]) * ux + (b0[1] - a0[1]) * uy, (b1[0] - a0[0]) * ux + (b1[1] - a0[1]) * uy))
                lo, hi = max(0.0, ts[0]), min(La, ts[1])
                if hi - lo < 600.0:
                    continue
                p = (a0[0] + ux * lo - uy * o / 2.0, a0[1] + uy * lo + ux * o / 2.0)
                q = (a0[0] + ux * hi - uy * o / 2.0, a0[1] + uy * hi + ux * o / 2.0)
                reach = max(600.0, 2 * abs(o))
                a, c = _snap(p, cols, reach), _snap(q, cols, reach)
                if math.dist(a, c) > 1.0:
                    lines.append(LineString([a, c]))
        for g in s.extra.get("section_gap_beams") or []:
            # carried to the column centre at an end, as the beams are (they stop at its face)
            reach = max(600.0, 2 * float(g.get("width") or 230.0))
            a, c = _snap(tuple(g["start"][:2]), cols, reach), _snap(tuple(g["end"][:2]), cols, reach)
            if math.dist(a, c) > 1.0:
                lines.append(LineString([a, c]))
        if len(lines) < 3:
            continue
        taken = unary_union([p for p, _ in slabs]) if slabs else None
        made = 0
        # each mark's bay: the region round it the beam lines close (found this way, two bays
        # meeting at one corner stay two - NIFT 4th floor S2 and the bay below-right); a region
        # reaching the edge of the 30 m window round the mark is not closed: no bay
        walls_ = unary_union(lines).buffer(5.0)
        faces = []
        for c in open_caps:
            pt = Point(*c.geometry["at"][:2])
            if any(f.contains(pt) for f in faces) or walls_.contains(pt):
                continue
            window = pt.buffer(30000.0)
            rest = window.difference(walls_)
            comp = next((g for g in getattr(rest, "geoms", [rest]) if g.contains(pt)), None)
            if comp is None or comp.exterior.distance(window.exterior) < 1.0:
                continue
            faces.append(Polygon(comp.exterior).buffer(5.0, join_style=2))
        for poly in faces:
            inside = [c for c in open_caps if poly.contains(Point(*c.geometry["at"][:2]))]
            marks = sorted({_key_of(c.mark) for c in inside})
            if len(marks) != 1 or poly.area < 1.0e6:
                continue                                  # unmarked, or two marks: not guessed
            if taken is not None and poly.intersection(taken).area > 0.1 * poly.area:
                continue                                  # a slab already over part of it
            mark = inside[0].mark
            ev = Evidence(reader=READER, sheet=sid, source="beam bay with a slab mark, no slab drawn",
                          at=list(inside[0].geometry["at"][:2]), text=f"{mark} in a {poly.area / 1e6:.1f} m2 bay")
            # its thickness: a settled slab of that mark on this floor, else the slab schedule
            th = next((e.resolved["thickness"] for _p, e in slabs if _key_of(str(e.final("mark") or e.mark or "")) == marks[0]
                       and e.resolved.get("thickness") is not None and e.resolved["thickness"].status in (READ, OVERRIDE)
                       and e.resolved["thickness"].value), None)
            if th is None:
                for sp in b.of("spec"):
                    if sp.get("family") == "slab" and _key_of(sp.mark or "") == marks[0] and sp.get("thickness") \
                            and (not _set_of(b, sp) or _set_of(b, sp) == s.extra.get("set", "")):
                        th = sp.props["thickness"]
                        break
            el = Element(id=f"{sid}/slab/b{made}", type="slab", sheet=sid, level=s.level, wing=s.wing, mark=mark,
                         geometry={"outline": [[round(x, 1), round(y, 1)] for x, y in list(poly.exterior.coords)[:-1]],
                                   "holes": []},
                         props={"mark": Value(mark, READ, 0.8, [ev])}, extra={"generated": True, "_layer": ""})
            el.resolved["mark"] = Value(mark, READ, 0.8, [ev])
            el.resolved["thickness"] = (Value(th.value, READ, min(th.confidence, 0.85), [ev] + th.evidence[:2])
                                        if th is not None else Value(None, MISSING, 0.0, []))
            b.add(el)
            taken = poly if taken is None else unary_union([taken, poly])
            made += 1
        if made:
            b.issues.append(Issue(INFO, "slabs", f"{made} bay(s) closed by beams, with a slab mark and no slab drawn: "
                                  "built as that slab", sheet=sid, extra={"from_linker": True}))
            stats["slab panels from marked bays the plan reader left empty"] += made


TICK_MAX_MM = 600.0          # a line no longer than this can lie wholly along one column's face
TICK_REACH_MM = 150.0        # ... within this of the column's outline, all along


def _column_ticks(b: Building, stats) -> None:
    """A short single line on the beam layer, lying wholly along a column's face, is part of
    how the column (or the slab edge round it) is drawn - not a beam.

    NIFT 5th floor: 300 mm lines 100 mm outside the 300 x 900 columns at the ends of the edge
    beams - one line each (no second face, no width of its own), built as 300 x 600 beams 300
    long, some given the mark of a caption nearby. A beam spans support to support: a line no
    longer than TICK_MAX_MM within TICK_REACH_MM of one column all along is not one. A pair of
    faces (a width of its own) is never touched; a review decision on it still builds it."""
    from shapely.geometry import LineString, Polygon
    for e in b.elements:
        e.extra.pop("not_a_beam", None)
    cols = defaultdict(list)
    for e in b.elements:
        o = e.geometry.get("outline") or []
        if e.type == "column" and len(o) >= 3:
            try:
                cols[e.sheet].append((Polygon([tuple(p[:2]) for p in o]).buffer(TICK_REACH_MM), e))
            except Exception:
                continue
    flagged = defaultdict(list)
    for e in b.elements:
        if e.type != "beam" or gone(e) or e.sheet not in cols or not e.geometry.get("start") or e.geometry.get("mid"):
            continue
        if e.get("width") and not any("plain line" in str(n) for n in e.notes):
            continue                                     # two faces drawn: a beam's width
        p, q = e.geometry["start"][:2], e.geometry["end"][:2]
        L = math.dist(p, q)
        if L > TICK_MAX_MM or L <= 0:
            continue
        ls = LineString([tuple(p), tuple(q)])
        col = next((c for poly, c in cols[e.sheet] if poly.contains(ls)), None)
        if col is not None:
            e.extra["not_a_beam"] = (f"a {L:.0f} mm single line wholly along column "
                                     f"{col.final('mark') or ''} - part of how the column is drawn, not a beam").replace("  ", " ")
            flagged[e.sheet].append(e)
    ids = {e.id for els in flagged.values() for e in els}
    b.issues = [i for i in b.issues if not (i.category == "missing" and len(i.elements) == 1 and i.elements[0] in ids)]
    for sid, els in flagged.items():
        b.issues.append(Issue(INFO, "geometry", f"{len(els)} short single line(s) on the beam layer lie wholly along a "
                              f"column's face (no longer than {TICK_MAX_MM:.0f} mm): part of the column's drawing, "
                              "not beams - not built", sheet=sid, elements=[e.id for e in els], extra={"from_linker": True}))
        stats["short lines along a column, not built as beams"] += len(els)


def _sized_by_sketch(hit, sec, out, beams=(), cut=None) -> None:
    """A section drawn with loose lines (beam_sections._sketch_sections, NIFT) gives the beams
    its cut crosses their size - only what nothing else gives them. Never a shape: a plain
    beam stays a Revit beam. A depth the section writes is read; one only measured at its
    drawing's scale (the beam hangs under a dimensioned slab) is the working size, open
    until decided. A beam not as wide as the section's full-height part is not the beam the
    section shows (D-D, 230 wide, across 300 wide 2B7): left as it is, and said. The slab
    the section draws beside the beam, read on the plan as beams, is that slab (_slab_strips)."""
    W, D = float(sec.extra.get("full_width") or sec.get("width")), float(sec.get("depth"))
    loose = sec.extra.get("undimensioned") or []
    name = f"{sec.mark}-{sec.mark}"
    ref = _cut_line(hit, beams, cut, W) if cut is not None else None
    strips = _slab_strips(hit, beams, sec, ref)
    th = sec.extra.get("slab_thickness") or {}
    for e, f, shift in strips.values():
        ev = Evidence(reader=READER, sheet=sec.sheet, source=f"section {name}: the slab running {f['width']:.0f} "
                      f"past the beam's face, {th['mm']:.0f} thick", text=sec.extra.get("title", ""))
        # shift: across the line (to its left, mm) from where it is drawn to the strip's middle
        e.extra["slab_strip"] = {"section": name, "width": f["width"], "thickness": th["mm"],
                                 "written": bool(th.get("written")), "shift": round(shift, 1)}
        e.resolved["kind"] = Value("slab edge strip", READ, 0.85, [ev])
        dv = e.resolved.get("depth")
        if dv is not None and dv.status == MISSING and dv.value is not None:
            e.resolved["depth"] = Value(None, MISSING, 0.0, [])     # not a beam: no working depth
        out["strip"].append(e)
    _section_faces(hit, beams, sec, strips, out, ref)
    for e in hit:
        if e.id in strips or e.extra.get("section_parts") or e.extra.get("same_beam_as"):
            continue
        wv = e.resolved.get("width")
        w = wv.value if wv is not None and wv.status in (READ, OVERRIDE) and wv.value else e.get("width")
        if w and abs(float(w) - W) > 15.0:
            out["other width"].append((e, float(w)))
            continue
        dv = e.resolved.get("depth")
        if dv is not None and dv.status == MISSING and dv.value not in (None, ""):
            continue                                  # another cut of a section has sized it already
        if dv is None or dv.status == MISSING or dv.value in (None, ""):
            if "up" in loose:
                ev = Evidence(reader=READER, sheet=sec.sheet, source=f"section {name}, measured at its drawing's "
                              f"scale (its depth is not written): {D:.0f}", text=sec.extra.get("title", ""))
                e.resolved["depth"] = Value(D, MISSING, 0.3, [ev] + (dv.evidence if dv else []))
                out["measured"].append(e)
            else:
                ev = Evidence(reader=READER, sheet=sec.sheet, source=f"section {name}: {D:.0f}",
                              text=sec.extra.get("title", ""))
                e.resolved["depth"] = Value(D, READ, 0.85, [ev])
                out["read"].append(e)
        elif "up" not in loose and dv.status in (READ, OVERRIDE) and abs(float(dv.value) - D) > 25.0:
            out["other depth"].append((e, float(dv.value)))
        else:
            out["agrees"].append(e)
        if (wv is None or wv.status == MISSING or not wv.value) and "across" not in loose:
            e.resolved["width"] = Value(W, READ, 0.85, [Evidence(reader=READER, sheet=sec.sheet,
                                                                 source=f"section {name}: {W:.0f} wide")])
    out["section"] = [sec]
    return ref


def _edge_strips(b: Building, view: str, ref, sec, beams, out) -> None:
    """The slab beside a beam along its whole drawn edge, not only where the plan reader paired it.

    NIFT 3rd floor, 3B4: the slab's outer edge is ONE line 60 m long, 300 off the beam's centre
    (SECTION A-A: the slab 150 past the outer face); the beam's faces are 12 pieces. The reader
    paired that line with one piece only, so the strip stopped half way. Read from the plan's own
    beam-layer lines (readers/linework): a line where the section puts a slab edge (a closed one),
    along the beam line the cut marks - the strip runs as far as both run. Stretches a strip
    already covers are left as they are; nothing where no edge line is drawn."""
    flanges = [f for f in sec.extra.get("flanges") or [] if f.get("closed")]
    th = sec.extra.get("slab_thickness")
    vs = b.sheets.get(view)
    lines = ((vs.extra.get("beam_lines") or {}).get("lines") or []) if vs else []
    if not flanges or not th or not lines:
        return
    W = float(sec.extra.get("full_width") or sec.get("width"))
    (px, py), (qx, qy) = ref.geometry["start"][:2], ref.geometry["end"][:2]
    L = math.hypot(qx - px, qy - py) or 1.0
    ux, uy = (qx - px) / L, (qy - py) / L

    def frame(x, y):                                    # (along, across to the left) on ref's line
        return (x - px) * ux + (y - py) * uy, (x - px) * -uy + (y - py) * ux
    # where the beam line stands: its beams, and the beam parts of split pairs; a column's
    # width between two of them is the line carrying on
    spans = []
    for e in beams:
        o = _offset(ref, e)
        if o is None:
            continue
        sp = e.extra.get("section_parts") or {}
        if (abs(o) <= W / 3.0 and abs(_drawn_width(e) - W) <= 15.0 and not e.extra.get("slab_strip")) or sp.get("beam"):
            spans.append(_along(ref, e))
    # ... and the beams built where only its strips were paired (_beam_line_gaps), and wherever
    # both its faces are drawn (NIFT 5th floor: the faces drawn, the reader paired them otherwise)
    for g in out.get("gap_beams") or []:
        spans.append(tuple(sorted((frame(*g["start"][:2])[0], frame(*g["end"][:2])[0]))))
    faces = {1.0: [], -1.0: []}
    for _lay, x0, y0, x1, y1, _dash in lines:
        (a0, o0), (a1, o1) = frame(x0, y0), frame(x1, y1)
        if abs(a1 - a0) >= 1.0 and abs(o1 - o0) <= 0.02 * abs(a1 - a0):
            for side in (1.0, -1.0):
                if abs((o0 + o1) / 2.0 - side * W / 2.0) <= 40.0:
                    faces[side].append(tuple(sorted((a0, a1))))
    spans += [(max(a, c), min(z, d)) for a, z in _union(faces[1.0]) for c, d in _union(faces[-1.0])
              if min(z, d) - max(a, c) > 0]
    line = []
    for a, z in sorted(spans):
        if line and a <= line[-1][1] + 1000.0:
            line[-1][1] = max(line[-1][1], z)
        else:
            line.append([a, z])
    if not line:
        return
    name = f"{sec.mark}-{sec.mark}"
    for f in flanges:
        for side in (1.0, -1.0):
            edge_at, mid = side * (W / 2.0 + f["width"]), side * (W / 2.0 + f["width"] / 2.0)
            edges = []
            for _lay, x0, y0, x1, y1, _dash in lines:
                (a0, o0), (a1, o1) = frame(x0, y0), frame(x1, y1)
                if abs(a1 - a0) < 1.0 or abs(o1 - o0) > 0.02 * abs(a1 - a0):
                    continue                            # not along the beam
                if abs((o0 + o1) / 2.0 - edge_at) <= 40.0:
                    edges.append(tuple(sorted((a0, a1))))
            if not edges:
                continue
            covered = []                                # strips already there, this side and width
            for e in beams:
                s = e.extra.get("slab_strip")
                o = _offset(ref, e)
                if s and o is not None:
                    o += float(s.get("shift") or 0.0) * (1.0 if _dir_same(ref, e) else -1.0)
                    if abs(o - mid) <= 40.0:
                        covered.append(_along(ref, e))
                for part in (e.extra.get("section_parts") or {}).get("strips") or []:
                    o = _offset(ref, e)
                    if o is not None:
                        o += float(part["shift"]) * (1.0 if _dir_same(ref, e) else -1.0)
                        if abs(o - mid) <= 40.0:
                            covered.append(_along(ref, e))
            for ea, ez in _union(edges):
                for la, lz in line:
                    a, z = max(ea, la), min(ez, lz)
                    if z - a <= 2.0 * W:
                        continue
                    for ga, gz in _uncovered((a, z), _union(covered)):
                        if gz - ga <= 2.0 * W:
                            continue
                        h = f["width"] / 2.0
                        pts = [(ga, mid - h), (gz, mid - h), (gz, mid + h), (ga, mid + h)]
                        out["edge_strips"].append({
                            "outline": [[px + ux * t - uy * o, py + uy * t + ux * o] for t, o in pts],
                            "width": f["width"], "thickness": th["mm"], "written": bool(th.get("written")),
                            "section": name})


def _uncovered(span, covered) -> list:
    """The parts of a stretch (from, to) that no stretch in covered (joined) lies over."""
    a, z = span
    out, at = [], a
    for c0, c1 in covered:
        if c1 <= at or c0 >= z:
            continue
        if c0 > at:
            out.append((at, c0))
        at = max(at, c1)
    if at < z:
        out.append((at, z))
    return out


def _sketch_issues(b: Building, sketched, stats) -> None:
    """One line per section and plan saying what the section gave its beams."""
    strip_ids = {e.id for got in sketched.values() for e in got["strip"] + got["twice"]} | \
        {e.id for got in sketched.values() for e in got["merged"] if not e.extra["section_parts"].get("beam")}
    # a slab strip, or a beam drawn twice, is not a beam missing its depth
    b.issues = [i for i in b.issues if not (i.category == "missing" and len(i.elements) == 1
                                            and i.elements[0] in strip_ids and "depth" in i.message)]
    for (sid, _), got in sketched.items():
        sec = got["section"][0]
        name = f"{sec.mark}-{sec.mark}"
        if got["edge_strips"] and sid in b.sheets:
            from shapely.geometry import Polygon
            from shapely.ops import unary_union
            have = b.sheets[sid].extra.setdefault("section_edge_strips", [])
            added = 0.0
            for g in got["edge_strips"]:
                # another cut of the same line gives (nearly) the same strip: only what is new
                poly = Polygon(g["outline"])
                if have:
                    poly = poly.difference(unary_union([Polygon(h["outline"]).buffer(20.0) for h in have]))
                for piece in getattr(poly, "geoms", [poly]):
                    if piece.is_empty or piece.area < 2.0 * g["width"] * g["width"]:
                        continue
                    have.append(dict(g, outline=[[round(x, 1), round(y, 1)] for x, y in list(piece.exterior.coords)[:-1]]))
                    added += piece.area / g["width"]
            if added:
                b.issues.append(Issue(INFO, "section", f"section {name}: the slab's edge is drawn along the beam line "
                                      f"for {added / 1000.0:.1f} m more than the plan reader paired it - the slab strip "
                                      "built along it there too", sheet=sid, mark=name, extra={"from_linker": True}))
                stats["metres of slab strip built along a drawn slab edge"] += round(added / 1000.0)
        if got["gap_beams"] and sid in b.sheets:
            have = b.sheets[sid].extra.setdefault("section_gap_beams", [])
            seen = {(round(g["start"][0]), round(g["start"][1]), round(g["end"][0]), round(g["end"][1])) for g in have}
            for g in got["gap_beams"]:
                k = (round(g["start"][0]), round(g["start"][1]), round(g["end"][0]), round(g["end"][1]))
                if k not in seen:
                    seen.add(k)
                    have.append(g)
                    b.issues.append(Issue(INFO, "section", f"beam {g['mark']}: {math.dist(g['start'], g['end']):.0f} mm "
                                          f"{'between two of its spans' if len(g['between']) > 1 else 'past its last span'} has only its slab strips drawn as pairs (section "
                                          f"{name}): the strips' inner edges are its faces - built there, "
                                          f"{g['width']:.0f} x {g['depth']:.0f}", sheet=sid, elements=list(g["between"]),
                                          mark=str(g["mark"]), extra={"from_linker": True}))
                    stats["beams built where only their slab strips were paired"] += 1
        W, D = float(sec.extra.get("full_width") or sec.get("width")), float(sec.get("depth"))
        size = f"{W:.0f} x {D:.0f}" + (" with a ledge or step, built as a plain beam" if not sec.extra.get("rectangle") else "")
        how = " (depth measured at the drawing's scale - only the slab is dimensioned)" if "up" in (
            sec.extra.get("undimensioned") or []) else ""
        parts = []
        strips = list({e.id: e for e in got["strip"]}.values())
        if strips:
            ws = sorted({f"{e.extra['slab_strip']['width']:.0f}" for e in strips})
            parts.append(f"{len(strips)} line(s) along it read as beams {' / '.join(ws)} wide are the slab the "
                         f"section draws beside the beam ({sec.extra['slab_thickness']['mm']:.0f} thick) - built as "
                         "slab, not as beams")
        if got["read"]:
            parts.append(f"{len(got['read'])} beam(s) with no depth from any other source take {D:.0f}")
        if got["measured"]:
            parts.append(f"{len(got['measured'])} beam(s) with no depth are built {D:.0f} deep, red until you "
                         "confirm or type the depth")
        if got["agrees"]:
            parts.append(f"{len(got['agrees'])} already sized")
        if got["other depth"]:
            parts.append(", ".join(f"{e.final('mark') or 'a beam'} is {d:.0f} deep from its own source (kept)"
                                   for e, d in got["other depth"][:5]))
        if got["other width"]:
            parts.append(", ".join(f"{e.final('mark') or 'a beam'} is {w:.0f} wide, not this section's beam (left as it is)"
                                   for e, w in got["other width"][:5]))
        els = [e.id for k in ("read", "measured", "agrees") for e in got[k]] + \
              [e.id for k in ("other depth", "other width") for e, _ in got[k]] + [e.id for e in strips]
        sev = WARNING if got["measured"] or got["other depth"] or got["other width"] else INFO
        b.issues.append(Issue(sev, "section", f"section {name} ({size}{how}) is marked across "
                              f"{len(els)} beam(s): " + "; ".join(parts), sheet=sid, elements=els,
                              mark=name, extra={"from_linker": True}))
        stats["beams sized from a section drawn with lines"] += len(got["read"])
        stats["beams given a working depth measured from a section"] += len(got["measured"])
        stats["lines read as beams that a section shows as slab"] += len(strips)


def _unit(e):
    (ax, ay), (bx, by) = e.geometry["start"][:2], e.geometry["end"][:2]
    L = math.hypot(bx - ax, by - ay) or 1.0
    return (bx - ax) / L, (by - ay) / L


def _parallel_groups(beams):
    """The beams one cut crosses, in groups running the same way (within 5 degrees)."""
    groups = []
    for e in beams:
        ux, uy = _unit(e)
        for g in groups:
            vx, vy = _unit(g[0])
            if abs(ux * vy - uy * vx) < math.sin(math.radians(5.0)):
                g.append(e)
                break
        else:
            groups.append([e])
    return groups


def _clip(poly, lo, hi):
    """The part of a profile between u = lo and u = hi (two half-planes, Sutherland-Hodgman)."""
    def cut(pts, keep, at):
        out = []
        for i in range(len(pts)):
            p, q = pts[i - 1], pts[i]
            kp, kq = keep(p[0]), keep(q[0])
            if kq:
                if not kp:
                    t = (at - p[0]) / (q[0] - p[0])
                    out.append([at, p[1] + t * (q[1] - p[1])])
                out.append(list(q))
            elif kp:
                t = (at - p[0]) / (q[0] - p[0])
                out.append([at, p[1] + t * (q[1] - p[1])])
        return out
    pts = cut(poly, lambda u: u >= lo - 1e-6, lo)
    return cut(pts, lambda u: u <= hi + 1e-6, hi) if len(pts) >= 3 else []


def _give_section(grp, sec, cut, slabs) -> list:
    """The section across the beams one cut crosses. Drawn as one beam, it is that beam's
    shape; drawn as parallel pieces that together span the section (Main Block: face
    lines 230 and 520 apart = the 750 section, the middle line where the slope meets the
    flat top), each piece takes its own slice of it. Returns the beams given a shape."""
    ref = grp[0]
    ux, uy = _unit(ref)
    nx, ny = -uy, ux                                          # to the left of ref's start -> end
    a, b = cut.geometry["a"], cut.geometry["b"]
    W = float(sec.get("width"))

    def at(e):                                                # where the cut crosses e
        (px, py), (qx, qy) = e.geometry["start"][:2], e.geometry["end"][:2]
        dx, dy, ex, ey = qx - px, qy - py, b[0] - a[0], b[1] - a[1]
        den = dx * ey - dy * ex
        t = ((a[0] - px) * ey - (a[1] - py) * ex) / den if den else 0.5
        return px + t * dx, py + t * dy

    p0 = at(ref)
    parts = []
    for e in grp:
        p = at(e)
        s = (p[0] - p0[0]) * nx + (p[1] - p0[1]) * ny        # its centreline across ref's
        w = float(e.get("width") or e.final("width") or 0.0)
        parts.append((e, s, w))
    lo = min(s - w / 2.0 for _, s, w in parts)
    hi = max(s + w / 2.0 for _, s, w in parts)
    if len(grp) > 1 and abs((hi - lo) - W) > max(60.0, 0.1 * W):
        # pieces that do not make up the section: each alone, as one beam of that shape
        return [x for e in grp for x in _give_section([e], sec, cut, slabs)]
    if len(grp) == 1:
        lo, hi = -W / 2.0, W / 2.0
    prof = sec.extra["profile"]
    top = [q[0] for q in prof if q[1] > -1.0]
    full_right = (sum(top) / len(top) if top else W / 2.0) > W / 2.0
    # which way round. First the slab: it sits at the beam's top, so it frames into the
    # full-height side and the slope falls away from it - at an edge, outward (user
    # 2026-10-19, Sasan Gir terrace B31 / B2: "the slope of the beam should be outward").
    # Only when the slab does not tell (on both sides, or none): the drafting - the face
    # drawn solid is the full-height side, the slope on the dashed side (user 2026-10-10).
    def style_at(edge, left_side):
        for e, s, w in parts:
            same = (_unit(e)[0] * ux + _unit(e)[1] * uy) > 0
            if abs((s + (w / 2.0 if left_side else -w / 2.0)) - edge) <= 30.0:
                return (e.extra.get("faces") or {}).get("left" if left_side == same else "right")
        return None
    reach = 300.0
    left = any(_inside((p0[0] + nx * (hi + reach), p0[1] + ny * (hi + reach)), o) for o in slabs)
    right = any(_inside((p0[0] + nx * (lo - reach), p0[1] + ny * (lo - reach)), o) for o in slabs)
    sure, by_line = left != right, False
    sl, sr = style_at(hi, True), style_at(lo, False)
    if not sure and {sl, sr} == {"solid", "dashed"}:
        left, right, sure, by_line = sl == "solid", sr == "solid", True, True
    face_left = left or not sure                              # undecided: as read, for review
    sign = (1.0 if full_right else -1.0) * (1.0 if face_left else -1.0)
    whole = [[sign * (u - W / 2.0) + (lo + hi) / 2.0, v] for u, v in prof]
    turned = [[-sign * (u - W / 2.0) + (lo + hi) / 2.0, v] for u, v in prof]     # the review's "mirrored"
    name = f"{sec.mark}-{sec.mark}"
    done = []
    for e, s, w in parts:
        piece = whole if len(grp) == 1 else _clip(whole, s - w / 2.0, s + w / 2.0)
        if len(piece) < 3:
            continue
        same = 1.0 if (_unit(e)[0] * ux + _unit(e)[1] * uy) > 0 else -1.0
        e.extra["section_profile"] = [[round(same * (u - s), 1), round(v, 1)] for u, v in piece]
        other = turned if len(grp) == 1 else _clip(turned, s - w / 2.0, s + w / 2.0)
        e.extra["section_profile_mirrored"] = [[round(same * (u - s), 1), round(v, 1)] for u, v in other]
        ev = [Evidence(reader=READER, sheet=sec.sheet, source=f"as read: {name}", text=sec.extra.get("title", "")),
              Evidence(reader=READER, sheet=cut.sheet, source=f"mirrored: {name} mirrored"),
              Evidence(reader=READER, sheet=cut.sheet, source="plain rectangle: none")]
        if sure:
            side = "left" if (left and same > 0) or (right and same < 0) else "right"
            ev[0].source = (f"full height on the face drawn solid, the slope on the dashed ({side} of the beam "
                            f"full): {name}" if by_line else f"full-height side to the slab ({side} of the beam): {name}")
        if len(grp) > 1:
            e.extra["section_part"] = f"{w:.0f} of the {W:.0f} wide section {name}: {len(grp)} lines drawn for one beam"
        e.resolved["section"] = Value(name, READ if sure else CONFLICT, 0.9 if sure else 0.4, ev)
        if len(grp) == 1:
            _section_width(e, sec, prof, name)
        done.append(e)
    return done


def _section_width(e, sec, prof, name) -> None:
    """A beam built to its drawn section is as wide as the section, when the plan draws it
    that wide face to face (Main Block edge: 750 between the faces, SECTION X-X 375 + 375).
    A caption giving the width of the section's full-height part (375 x 600) describes that
    part - it agrees. A layer name alone that disagrees does not outweigh the section, the
    plan and a written caption that agree with it (Main Block edge: BEAM-230X600 is the layer
    the beams are drawn on, not this beam's size; user 2026-10-19). Anything else that
    disagrees - or a layer name with no caption agreeing - keeps it a conflict."""
    W = float(sec.get("width"))
    try:
        geo = float(e.get("width") or 0.0)
    except (TypeError, ValueError):
        return
    if abs(geo - W) > 15.0:
        return
    top = [u for u, v in prof if v > -1.0]
    part = (max(top) - min(top)) if len(top) >= 2 else W
    cur = e.resolved.get("width")
    evid = list(cur.evidence) if cur is not None else []
    odd, layer_only, written = [], [], False
    for ev in evid:
        kind, _, val = (ev.source or "").partition(": ")
        try:
            v = float(val)
        except ValueError:
            continue
        if abs(v - W) > 15.0 and abs(v - part) > 15.0:
            (layer_only if kind.strip().lower() == "layer name" else odd).append(f"{v:.0f} ({kind})")
        elif "caption" in kind.lower() or "schedule" in kind.lower():
            written = True
    if layer_only and not written:
        odd += layer_only
    note = Evidence(reader=READER, sheet=sec.sheet, source=f"section {name}, its whole width: {W:.0f}",
                    text=f"{part:.0f} is its full-height part")
    if layer_only and not odd:
        note.text += "; the layer name's size (%s) is the layer's, not this beam's" % ", ".join(layer_only)
    e.resolved["width"] = Value(W, CONFLICT if odd else READ, 0.5 if odd else 0.95, [note] + evid)


def section_profile(e):
    """The cross-section a beam is built to: [[u, v], ...] mm, u to the left of start -> end
    from its centreline, v down from its top; None for a plain rectangle. A review
    decision 'X-X mirrored' turns it round, 'none' takes it off."""
    prof, v = e.extra.get("section_profile"), e.resolved.get("section")
    if not prof or v is None:
        return None
    val = str(v.value or "").lower()
    if v.status == OVERRIDE and (val in ("none", "plain", "rectangle", "") or "none" in val):
        return None
    if v.status == OVERRIDE and "mirror" in val:
        m = e.extra.get("section_profile_mirrored")
        return m if m and len(m) >= 3 else [[-u, w] for u, w in prof]
    return prof


def _face_styles(b: Building, stats) -> dict:
    """Each beam's two faces as drawn - dashed (under the slab) or solid (seen from above:
    the slab ends there, or the beam stands above it) - from readers/linework.py.
    e.extra["faces"] = {"left": .., "right": ..}, left of start -> end; "mixed" when
    pieces of one face are drawn both ways. Returns {beam id: (left, right)}."""
    out = {}
    for sid, s in b.sheets.items():
        view = s.extra.get("view_sheet", "")
        if s.reader != "plan.legacy" or view not in b.sheets:
            continue
        bl = b.sheets[view].extra.get("beam_lines")
        if not bl:
            continue
        roles = s.layer_map or {}
        names = bl["layers"]
        lines = [ln for ln in bl["lines"] if roles.get(names[ln[0]], "") in ("beam", "girder")]
        # a layer the drawing draws solid throughout (J1153: every beam line solid) uses
        # no dashed convention: there "solid" says nothing, and nothing is checked
        dashed_layers = {names[ln[0]] for ln in lines if ln[5]}
        for e in b.elements:
            if e.sheet == sid:
                e.extra.pop("faces", None)                      # read again on every link
            if e.sheet != sid or e.type not in ("beam", "beam_span") or gone(e) or e.geometry.get("mid"):
                continue
            g = e.geometry
            (ax, ay), (bx, by) = g["start"][:2], g["end"][:2]
            L = math.hypot(bx - ax, by - ay)
            if L < 300.0:
                continue
            ux, uy = (bx - ax) / L, (by - ay) / L
            try:
                half = 0.5 * float(e.get("width") or e.final("width") or 0.0)
            except (TypeError, ValueError):
                half = 0.0
            if half <= 0:
                continue
            side = {}
            for _, x1, y1, x2, y2, dashed in lines:
                dx, dy = x2 - x1, y2 - y1
                ll = math.hypot(dx, dy)
                if abs(ux * dy - uy * dx) > 0.02 * ll:
                    continue                                    # not parallel
                sm = ((x1 + x2) / 2.0 - ax) * -uy + ((y1 + y2) / 2.0 - ay) * ux
                key = "left" if abs(sm - half) <= 30.0 else "right" if abs(sm + half) <= 30.0 else None
                if key is None:
                    continue
                t = sorted(((x1 - ax) * ux + (y1 - ay) * uy, (x2 - ax) * ux + (y2 - ay) * uy))
                over = min(L, t[1]) - max(0.0, t[0])
                if over > 0:
                    side.setdefault(key, []).append((over, dashed))
            faces = {}
            for key, got in side.items():
                if sum(o for o, _ in got) < 0.3 * L:
                    continue
                kinds = {d for _, d in got}
                faces[key] = "dashed" if kinds == {1} else "solid" if kinds == {0} else "mixed"
            if faces:
                e.extra["faces"] = faces
                out[e.id] = (faces.get("left"), faces.get("right"))
                stats["beam faces read as drawn (dashed / solid)"] += 1
        _face_checks(b, sid, stats, dashed_layers)
    return out


def _face_checks(b: Building, sid: str, stats, dashed_layers=frozenset()) -> None:
    """What the line styles say against the model - asked in the review, nothing changed:
    a face drawn solid with a level slab beyond it is not under that slab: the slab ends
    there (an edge, an opening, a step the model may miss), or - solid on both faces with
    level slab either side - the beam stands above the slab (an upturned beam)."""
    slabs = [(e.geometry["outline"], float(e.get("sunk_mm") or 0.0)) for e in b.elements
             if e.sheet == sid and e.type == "slab" and len(e.geometry.get("outline") or []) >= 3]
    if not slabs:
        return
    odd, up = [], []
    for e in b.elements:
        f = e.extra.get("faces") if e.sheet == sid else None
        if not f or e.extra.get("_layer") not in dashed_layers:
            continue
        g = e.geometry
        (ax, ay), (bx, by) = g["start"][:2], g["end"][:2]
        L = math.hypot(bx - ax, by - ay) or 1.0
        nx, ny = -(by - ay) / L, (bx - ax) / L
        half = 0.5 * float(e.get("width") or e.final("width") or 0.0)
        mx, my = (ax + bx) / 2.0, (ay + by) / 2.0
        level = {}
        for side, k in (("left", 1.0), ("right", -1.0)):
            p = (mx + k * nx * (half + 300.0), my + k * ny * (half + 300.0))
            level[side] = any(s == 0.0 and _inside(p, o) for o, s in slabs)
        solid_on_level = [side for side in ("left", "right") if f.get(side) == "solid" and level[side]]
        if len(solid_on_level) == 2:
            up.append(e)
        elif solid_on_level:
            odd.append(e)
    for e in up:
        mark = e.final("mark") or e.mark or e.id.rsplit("/", 1)[-1]
        b.issues.append(Issue(WARNING, "line style", f"beam {mark}: both faces drawn solid with level slab on both "
                              f"sides - by drafting, an upturned beam (standing above the slab), or a slip in the "
                              f"line style. Built below the slab until you decide", sheet=sid, elements=[e.id],
                              mark=str(mark), extra={"from_linker": True}))
    if odd:
        b.issues.append(Issue(INFO, "line style", f"{len(odd)} beam faces are drawn solid (seen from above) where the "
                              f"model has a level slab beyond them: a slab edge, opening or step the model may miss "
                              f"- check them", sheet=sid, elements=[e.id for e in odd][:200], extra={"from_linker": True}))
    stats["beams drawn solid on both faces inside level slab (upturned?)"] += len(up)


def _legend_zones(b: Building, stats) -> None:
    """What the plan's legend marks (readers/legend_zones.py), on the model - said, nothing
    built differently: a column in a 'column through' / 'column ends at this level' hatch
    carries that note; areas marked as a steel (fabrication) roof are not concrete - listed
    as not modelled, and a slab the model has there is to be checked; a slab in an area
    marked sloping that no section gives a slope to is flat until decided."""
    for e in b.elements:
        e.extra.pop("legend", None)
    zones = defaultdict(list)
    for z in b.of("legend_zone"):
        zones[z.sheet].append(z)
    for sid, s in b.sheets.items():
        view = s.extra.get("view_sheet", "")
        if s.reader != "plan.legacy" or not zones.get(view):
            continue
        cols = [e for e in b.elements if e.sheet == sid and e.type == "column" and e.geometry.get("x") is not None]
        slabs = [e for e in b.elements if e.sheet == sid and e.type == "slab" and len(e.geometry.get("outline") or []) >= 3
                 and not gone(e)]
        tally = defaultdict(int)
        roofs, odd_slabs, flat = [], [], []
        for z in zones[view]:
            poly, kind = z.geometry["outline"], z.mark
            if kind in ("column through", "column ends"):
                for c in cols:
                    if _inside((c.geometry["x"], c.geometry["y"]), poly):
                        c.extra["legend"] = ("RCC column through: continues above this level" if kind == "column through"
                                             else "RCC column ends at this level")
                        tally[kind] += 1
            elif kind == "steel roof":
                roofs.append(z)
                for e in slabs:
                    o = e.geometry["outline"]
                    if sum(_inside(q, poly) for q in o) >= 0.5 * len(o) or _inside(_centre_of(e) or o[0], poly):
                        odd_slabs.append(e)
            elif kind == "sloping slab":
                for e in slabs:
                    c = _centre_of(e)
                    if c and _inside(c, poly) and not e.extra.get("slope"):
                        e.extra["legend"] = "marked SLOPING RCC SLAB (legend): no section gives its slope - built flat"
                        flat.append(e)
        if tally:
            b.issues.append(Issue(INFO, "legend", "columns marked in the legend: " + ", ".join(
                f"{v} {k.replace('column ', '')}" for k, v in sorted(tally.items())) + " (shown on each column)",
                sheet=sid, extra={"from_linker": True}))
        if roofs:
            words = roofs[0].extra.get("words", "")
            b.issues.append(Issue(WARNING, "not modelled", f"{len(roofs)} areas hatched '{words}' (the legend): a "
                                  "steel roof, not concrete - not modelled", sheet=sid, extra={"from_linker": True}))
        for e in odd_slabs:
            e.extra["legend"] = "in an area the legend marks as a steel (fabrication) roof - a concrete slab here?"
        if odd_slabs:
            b.issues.append(Issue(WARNING, "confirm", f"{len(odd_slabs)} slab(s) lie in an area the legend marks as a "
                                  "steel (fabrication) roof: concrete there? - check", sheet=sid,
                                  elements=[e.id for e in odd_slabs], extra={"from_linker": True}))
        if flat:
            b.issues.append(Issue(WARNING, "missing", f"{len(flat)} slab(s) in areas marked SLOPING RCC SLAB, but no "
                                  "section gives their slope: built flat until you decide", sheet=sid,
                                  elements=[e.id for e in flat], extra={"from_linker": True}))
        stats["columns marked by the legend (through / ends)"] += sum(tally.values())
    for r in b.of("legend_row"):
        if r.mark in ("cut out", "sunk"):
            continue                       # read by the plan reader itself
        b.issues.append(Issue(INFO, "legend", f"legend row 'THUS MARKED {r.extra.get('words', '')}': no area on "
                              "this plan is drawn with its sample", sheet=r.sheet, extra={"from_linker": True}))


def _schedule_ranges(b: Building) -> dict[str, dict[str, list]]:
    """set -> mark -> [level range] in which the column schedule draws that column.
    From the schedule sheets' own record (column_schedule.read: unsized sections count,
    'no section in a band' does not), and from the sized specs."""
    out: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for sid, s in b.sheets.items():
        for row in s.extra.get("column_schedule") or []:
            if row.get("size") is not None and row.get("range"):
                out[s.extra.get("set", "")][row["mark"]].append(tuple(row["range"]))
    for s in b.of("spec"):
        rng = s.extra.get("level_range")
        if rng and s.mark and s.get("family") == "column" and tuple(rng) not in out[_set_of(b, s)][s.mark]:
            out[_set_of(b, s)][s.mark].append(tuple(rng))
    return out


def _run_top(ranges: list, rank: float) -> str | None:
    """The level the column standing at `rank` runs up to: its range, then each range
    starting where the last one ends (FDN-GF, GF-L1, L1-TERRACE: TERRACE)."""
    cur = next((r for r in ranges if _in_range_rank(rank, r)), None)
    if cur is None:
        return None
    top, seen = cur[1], {cur}
    while True:
        nxt = next((r for r in ranges if r not in seen and r[0] == top and level_rank(r[1]) is not None
                    and level_rank(r[1]) > level_rank(top)), None)
        if nxt is None:
            return top
        seen.add(nxt)
        top = nxt[1]


def _in_range_rank(rank: float, rng) -> bool:
    """column_schedule.in_range, for a level already ranked."""
    lo, hi = level_rank(rng[0]), level_rank(rng[1])
    if lo is None or hi is None:
        return False
    return rank == lo if lo == hi else lo < rank <= hi


def _column_extent(b: Building, stats) -> None:
    """How far each plan column runs, from the column schedule's level ranges.

    A plan shows the columns standing under its slab (column_schedule.in_range), and
    those are built from the level below up to it. The schedule says where that is not so:

      * the schedule's column STARTS at this plan's level (C9 'L1 TO L2' on the L1 plan):
        it sits on this floor and rises to the next, like a floating column - built so,
        unless the plan above draws it too (then that plan builds it; said, for the review);
      * the schedule has no such column at this level: the mark, or the drawing, is to check;
      * the schedule's column runs on above the highest plan imported (C1 to ROOF, no ROOF
        plan): nothing above that plan is built - said once per plan.
    A column marked FC is left as it is; so is one whose kind was decided in the review,
    and one excluded there. Runs after apply_overrides, so marks decided there are used.
    """
    for e in b.elements:
        e.extra.pop("schedule_extent", None)
    sched = _schedule_ranges(b)
    if not any(sched.values()):
        return
    plans: dict[str, list] = defaultdict(list)            # set -> [(rank, sheet id)]
    for sid, s in b.sheets.items():
        r = level_rank(s.extra.get("level_key") or "")
        if s.reader == "plan.legacy" and r is not None:
            plans[s.extra.get("set", "")].append((r, sid))
    cols = [e for e in b.elements if e.type == "column" and e.sheet in b.sheets
            and b.sheets[e.sheet].reader == "plan.legacy" and not gone(e) and not _not_built(e)]
    warned = {eid for i in b.issues if i.category == "column mark" for eid in i.elements}
    by_mark: dict[tuple, list] = defaultdict(list)          # (set, mark) -> [(rank, column)]
    for e in cols:
        r = level_rank(_level_key(b, e))
        if r is not None:
            by_mark[(_set_of(b, e), _key_of(str(e.final("mark") or e.mark or "")))].append((r, e))
    above_open: dict[str, list] = defaultdict(list)       # sheet -> ["C1 (to ROOF)"]
    for e in cols:
        mark = str(e.final("mark") or e.mark or "")
        lv = _level_key(b, e)
        r = level_rank(lv)
        my_set = _set_of(b, e)
        # a schedule with no drawing set applies to every set, as the sizes do
        ranges = sched.get(my_set, {}).get(mark) or (sched.get("", {}).get(mark) if my_set else None)
        if not mark or r is None or not ranges or _key_of(mark).startswith("FC") \
                or e.get("kind") == "column_float" or "kind" in e.resolved:
            continue
        known = [g for g in ranges if level_rank(g[0]) is not None and level_rank(g[1]) is not None]
        if not known:
            continue
        text = ", ".join(f"{g[0]} to {g[1]}" for g in sorted(known, key=lambda g: level_rank(g[0])))
        ev = Evidence(reader=READER, sheet=e.sheet, source="column schedule level ranges", text=f"{mark}: {text}")
        top = _run_top(known, r)
        if top is not None:
            e.extra["schedule_extent"] = top
            higher = [p for p, sid in plans.get(my_set, []) if r < p <= level_rank(top)]
            if level_rank(top) > r and not higher:
                above_open[e.sheet].append(f"{mark} (to {top})")
            continue
        starts = [g for g in known if level_rank(g[0]) == r and level_rank(g[1]) > r]
        if starts:
            hi = max(level_rank(g[1]) for g in starts)
            upper = [c for p, c in by_mark.get((my_set, _key_of(mark)), []) if r < p <= hi and c is not e
                     and _same_column(e, c)]
            if upper:
                b.issues.append(Issue(WARNING, "column extent", f"column {mark}: the column schedule starts it at "
                                      f"{lv} ({text}), and the plan above draws it too - that plan builds it from "
                                      f"{lv} up; this one would stand below {lv}: exclude it in the review",
                                      sheet=e.sheet, elements=[e.id, upper[0].id], mark=mark,
                                      extra={"from_linker": True}))
                stats["columns the schedule starts here, drawn above too"] += 1
                continue
            e.resolved["kind"] = Value("column_float", READ, 0.85, [ev])
            b.issues.append(Issue(INFO, "column extent", f"column {mark}: the column schedule starts it at {lv} "
                                  f"({text}) - built from {lv} up to the next level, like a floating column",
                                  sheet=e.sheet, elements=[e.id], mark=mark, extra={"from_linker": True}))
            stats["columns the schedule starts at their plan's level (built upward)"] += 1
            continue
        if e.id not in warned:
            b.issues.append(Issue(WARNING, "column extent", f"column {mark}: the column schedule has no {mark} at "
                                  f"{lv} ({text}) - check the mark, or which floor this plan is",
                                  sheet=e.sheet, elements=[e.id], mark=mark, extra={"from_linker": True}))
            stats["columns the schedule has not at their plan's level"] += 1
    for e in cols:
        if (e.extra.get("legend") or "").startswith("RCC column through"):
            r = level_rank(_level_key(b, e))
            if r is not None and not any(p > r for p, _ in plans.get(_set_of(b, e), [])):
                above_open[e.sheet].append(f"{e.final('mark') or e.mark or 'unmarked'} (legend: column through)")
    for sid, items in above_open.items():
        lv = b.sheets[sid].extra.get("level_key") or sid
        uniq = sorted(set(items))
        b.issues.append(Issue(WARNING, "column extent", f"{len(items)} column(s) run on above {lv} - "
                              f"{', '.join(uniq[:12])}{' ...' if len(uniq) > 12 else ''} - but no plan above {lv} "
                              f"was imported: they are built up to {lv} only. Import the plan above to build them",
                              sheet=sid, extra={"from_linker": True}))
        stats["columns running on above the highest plan"] += len(items)


def _same_column(a, c) -> bool:
    """The same column on two floors: one stack (aligned to the column layout), or,
    with no stack, within 300 mm in the drawings' own coordinates."""
    sa, sc = a.extra.get("stack"), c.extra.get("stack")
    if sa and sc:
        return sa == sc
    ga, gc = a.geometry, c.geometry
    if ga.get("x") is None or gc.get("x") is None:
        return False
    return math.hypot(ga["x"] - gc["x"], ga["y"] - gc["y"]) <= 300.0


def _not_built(e) -> bool:
    """Excluded in the review, or left out as another block's (export.left_out)."""
    if e.resolved.get("excluded") and e.resolved["excluded"].value:
        return True
    v = e.resolved.get("scope")
    return v is not None and v.status in (OVERRIDE, READ) and str(v.value or "").lower().startswith("other")


ELEV_TOL_MM = 300.0     # a wall ending this near a level line ends at it (its slab, a beam)
WALL_ELEV = "wall elevation"


def _plan_floors(b: Building, set_key: str) -> list:
    """[(rank, level key, [plan sheet ids])] of one drawing set, bottom up."""
    by = defaultdict(list)
    for sid, s in b.sheets.items():
        k = s.extra.get("level_key") or ""
        if s.reader == "plan.legacy" and s.extra.get("set", "") == set_key and level_rank(k) is not None:
            by[k].append(sid)
    return sorted(((level_rank(k), k, sorted(v)) for k, v in by.items()), key=lambda t: (t[0], t[1]))


def _match_levels(we: dict, keys: list, heights: dict) -> tuple:
    """Each level line of a wall elevation (bottom up) -> the floor (level key) it is, or None.
    One to one with the set's plan floors in order when there are as many - and where the
    floors' elevations are known (read or decided) and the elevation's scale is, its storey
    heights must agree. When the counts differ, by the floors' elevations alone.
    Returns (keys or None, how - or why not)."""
    lv, scale = we["levels"], we.get("scale")
    known = bool(scale) and all(heights.get(k) is not None for k in keys)
    if len(lv) == len(keys):
        if known:
            bad = [f"{keys[i]} to {keys[i + 1]}" for i in range(len(keys) - 1)
                   if abs((lv[i + 1]["y"] - lv[i]["y"]) / scale - (heights[keys[i + 1]] - heights[keys[i]])) > 50.0]
            if bad:
                return None, f"its storey heights differ from the floors' ({', '.join(bad)})"
            return list(keys), "one to one with the floors, in order; the storey heights agree"
        return list(keys), "one to one with the floors, in order"
    if scale and sum(1 for k in keys if heights.get(k) is not None) >= 2:
        for l0 in lv:
            for k0 in keys:
                if heights.get(k0) is None:
                    continue
                got = []
                for l in lv:
                    z = heights[k0] + (l["y"] - l0["y"]) / scale
                    got.append(next((k for k in keys if heights.get(k) is not None and abs(heights[k] - z) <= 50.0),
                                    None))
                hit = [g for g in got if g]
                if len(hit) >= max(2, len(lv) - 1) and len(set(hit)) == len(hit):
                    return got, "by the floors' elevations"
    return None, f"{len(lv)} level lines for {len(keys)} floors, and no floor elevations to match them by"


def _band_extent(ys: list, i: int, base: float, top: float, scale) -> tuple:
    """What a wall standing from base to top (drawing units) is of floor i's storey - from
    floor i-1's level line to floor i's (ys, None where the elevation has no such line):
    'below' (it starts at or above floor i), 'above' (it ended by floor i-1), 'in' with its
    base / top off those levels in mm where it starts or ends between them, or '' (the
    elevation does not show floor i)."""
    tol = ELEV_TOL_MM * (scale or 1.0)
    hi = ys[i]
    lo = ys[i - 1] if i > 0 else None
    if hi is None:
        return "", {}
    if base >= hi - tol:
        return "below", {}
    if lo is not None and top <= lo + tol:
        return "above", {}
    got = {}
    if scale and lo is not None and base > lo + tol:
        got["base_mm"] = round((base - lo) / scale)
    if scale and top < hi - tol:
        got["top_mm"] = -round((hi - top) / scale)
    return "in", got


def _wall_heights(b: Building, stats, overrides: dict | None = None) -> None:
    """Walls drawn in elevation (readers/wall_elevations.py): where each wall - and what
    stands on the raft under it - starts and ends, floor by floor.

    A plan shows the columns and walls under its slab, built from the level below up to
    it. A wall elevation says where that is not so (Winter Villa, SHEAR WALL ELEVATION
    (SW15): SW15 stands on its raft 1325 above the lowest level line - the site slopes):
      * its level lines are matched to the plan floors (_match_levels);
      * a plan's wall of that mark whose storey lies under the wall's base, or over its top,
        is not built (scope read 'other', as a legend sample beside a plan: 'this block' in
        the review builds it); one that starts or ends inside its storey gets base_mm /
        top_mm, its base above the level below / its top off its level, in mm;
      * the raft drawn under the wall's base, found on the foundation plan by the wall's
        mark, has its top there: every other column and wall standing on that raft starts
        at it the same way (the plans placed on the foundation plan by column marks).
    Elements decided in the review (scope, excluded) and ones already given a scope are
    left as they are; a top from a building section wins. Runs after apply_overrides.
    """
    for e in b.elements:
        e.extra.pop("profile_wall", None)
        e.extra.pop("lean", None)
        wh = e.extra.pop("wall_height", None)
        for k in (wh or {}).get("set", []):
            v = e.resolved.get(k)
            if v is not None and v.status == READ and any(ev.reader == READER and ev.source.startswith(WALL_ELEV)
                                                          for ev in v.evidence):
                del e.resolved[k]
    elevs = [(sid, s.extra["wall_elevation"]) for sid, s in b.sheets.items() if s.extra.get("wall_elevation")]
    if not elevs:
        return
    from .export import _shift_by_marks, level_table
    heights: dict[str, dict] = defaultdict(dict)
    for row in level_table(b, (overrides or {}).get("project")):
        if row.get("elevation_mm") is not None and row.get("level"):
            heights[row.get("set") or ""][row["level"]] = float(row["elevation_mm"])
    by_set = defaultdict(list)
    for sid, we in elevs:
        by_set[b.sheets[sid].extra.get("set", "")].append((sid, we))
    by_id = {e.id: e for e in b.elements}
    for set_key, items in by_set.items():
        floors = _plan_floors(b, set_key)
        if len(floors) < 2:
            continue
        keys = [k for _r, k, _s in floors]
        floor_of = {sid: i for i, (_r, _k, sids) in enumerate(floors) for sid in sids}
        name_of = {k: b.sheets[sids[0]].level or k for _r, k, sids in floors}    # "Service Floor", not its key
        verticals = [e for e in b.elements if e.type in ("column", "wall") and e.sheet in floor_of
                     and not gone(e) and "scope" not in e.resolved
                     and not (e.resolved.get("excluded") and e.resolved["excluded"].value)]
        done: dict[str, tuple] = {}            # element id -> (kind, {base_mm, top_mm}, why, elevation sheet)
        raft_tops = defaultdict(list)          # raft id -> [(ys, scale, top y, sheet, why, drawn thickness)]
        drawn = set()
        shifts = None                          # each plan's offset onto the lowest (viewer3d.stack), when needed
        for sid, we in items:
            title = we.get("title") or sid
            got_keys, how = _match_levels(we, keys, heights.get(set_key, {}))
            if got_keys is None:
                b.issues.append(Issue(INFO, WALL_ELEV, f"{title}: its levels could not be matched to the floors ({how}) "
                                      "- nothing taken from it", sheet=sid, extra={"from_linker": True}))
                continue
            scale = we.get("scale")
            ys = [next((lv["y"] for lv, k in zip(we["levels"], got_keys) if k == key), None) for key in keys]
            tol = ELEV_TOL_MM * (scale or 1.0)

            def where(y, we=we, got_keys=got_keys, scale=scale, tol=tol):
                lv, k = min(((lv, k) for lv, k in zip(we["levels"], got_keys) if k), key=lambda t: abs(t[0]["y"] - y))
                name = f"LEVEL {lv['name']} ({name_of[k]})" if lv["name"] else name_of[k]
                return name if abs(y - lv["y"]) <= tol or not scale else f"{(y - lv['y']) / scale:+.0f} mm from {name}"
            said = []
            # a mark no plan carries, mistyped (SA14 beside SW13, SW15): the one plan column or
            # wall of its first letter and number, not named by the elevation already
            on_plans = {_key_of(str(e.final("mark") or e.mark or "")) for e in verticals}
            named = {_key_of(m) for w in we["walls"] for m in w["marks"]}
            typo = {}
            for k in sorted(named - on_plans):
                p = re.match(r"([A-Z])[A-Z]*(\d+)$", k)
                cand = sorted(o for o in on_plans - named if p and re.fullmatch(
                    re.escape(p.group(1)) + r"[A-Z]*0*" + p.group(2), o))
                if len(cand) == 1:
                    typo[k] = cand[0]
                    b.issues.append(Issue(INFO, WALL_ELEV, f"{title}: names {k}, which no plan carries - read as "
                                          f"{cand[0]}, the one plan mark of its letter and number; correct the "
                                          "title if not", sheet=sid, mark=k, extra={"from_linker": True}))
            plane = {}                         # which way the elevation's +x runs on the plans (a wall built to it)
            later = []                         # (wall drawn, mark, storeys) leaning as the section draws them

            def shaped(mark, fi, how, sid=sid, title=title):
                lo_k, hi_k = keys[fi - 1], keys[fi]
                b.issues.append(Issue(INFO, WALL_ELEV, f"{mark}: {title} draws it changing shape between "
                                      f"{name_of[lo_k]} and {name_of[hi_k]} (an arch, a lean) - {how}",
                                      sheet=sid, mark=mark, extra={"from_linker": True}))
            for w in we["walls"]:
                mark = typo.get(_key_of(w["marks"][0]), w["marks"][0])
                drawn.add(_key_of(mark))
                span = f"{mark} from {where(w['base'])} to {where(w['top'])}"
                said.append(span)
                # an elevation says nothing below its lowest level line: a wall drawn down to it
                # carries on below as the plans draw it (Winter Villa: SW9 / SW10 to LEVEL -01, the
                # raft lower, at the foundation level); only a base drawn above that line is its own
                base = w["base"] if w["base"] > we["levels"][0]["y"] + tol else -math.inf
                # the rafts on the foundation plan under this mark
                f = w.get("foundation")
                under = [r for r in b.of("raft") if f and b.sheets.get(r.sheet) and not gone(r)
                         and b.sheets[r.sheet].extra.get("set", "") == set_key
                         and len(r.geometry.get("outline") or []) >= 3
                         and any(_key_of(str(o.final("mark") or o.mark or "")) == _key_of(mark) and o is not r
                                 and (c := _centre_of(o)) is not None and _inside(c, r.geometry["outline"])
                                 for o in b.elements if o.sheet == r.sheet)]
                if base != -math.inf and any((fv := r.resolved.get(FOUNDING)) is not None and fv.status == OVERRIDE
                                             for r in under):
                    # its raft's founding level decided, lower (Winter Villa: Foundation -1200): the
                    # slab drawn under it is a floor slab, which nothing builds - it carries on down
                    # to the raft as the plans draw it, not left standing on nothing (user 2026-10-28)
                    base = -math.inf
                    said[-1] += " (its raft's founding level decided: it carries on down to the raft)"
                for e in verticals:
                    if _key_of(str(e.final("mark") or e.mark or "")) != _key_of(mark):
                        continue
                    kind, got = _band_extent(ys, floor_of[e.sheet], base, w["top"], scale)
                    if kind:
                        done[e.id] = (kind, got, f"{title} draws {span}", sid)
                bands = [keys.index(got_keys[j]) for j in w.get("shaped_at") or []
                         if got_keys[j - 1] and got_keys[j]]
                if bands:
                    if shifts is None:
                        from .viewer3d import stack
                        shifts = stack(b, set_key, [(f[2][0], b.sheets[f[2][0]]) for f in floors])[0]
                    kinds = [e.type for e in verticals if _key_of(str(e.final("mark") or e.mark or "")) == _key_of(mark)]
                    if kinds and "wall" not in kinds:
                        # a column the title names beside the walls (C12): it leans as they do
                        later.append((w, mark, bands, None, ""))
                    else:
                        # which way the elevation faces on the plans; its shape is then the section's
                        # alone (_section_lean, below) - a plan drawing it shifted from floor to floor
                        # is not a lean (user 2026-10-28)
                        why, fit = _profile_walls(b, w, mark, bands, ys, scale, keys, floor_of, verticals, shifts,
                                                  title, plane)
                        later.append((w, mark, bands, why, fit))
                if not f:
                    continue
                said[-1] += (f", on a slab {(f['top'] - f['bottom']) / scale:.0f} thick" if scale else ", on a slab")
                for r in under:
                    raft_tops[r.id].append((ys, scale, f["top"], sid, f"{title} draws its top at "
                                            f"{where(f['top'])}, under {mark}",
                                            (f["top"] - f["bottom"]) / scale if scale else None))
            for w, mark, bands, pwhy, pfit in later:
                key = _key_of(mark)
                fitted = {e.id: e.extra.pop("profile_wall") for e in verticals if "profile_wall" in e.extra
                          and _key_of(str(e.final("mark") or e.mark or "")) == key}
                why, fit = _section_lean(b, w, mark, bands, ys, scale, keys, floor_of, verticals, shifts, title,
                                         plane.get("dir"))
                for fi in bands:
                    if not why[fi]:
                        shaped(mark, fi, f"built leaning as drawn ({fit})")
                        stats["columns/walls built leaning as a wall elevation draws them"] += 1
                    elif pwhy and not pwhy[fi]:
                        # the section cannot be started from the plan under its lean: its shape
                        # placed on each floor's plan, as before
                        for e in verticals:
                            if e.id in fitted and floor_of[e.sheet] == fi:
                                e.extra["profile_wall"] = fitted[e.id]
                        shaped(mark, fi, f"built to that shape, a profile wall ({pfit})")
                        stats["walls built to the shape a wall elevation draws"] += 1
                    else:
                        shaped(mark, fi, f"built straight up between them, as the {name_of[keys[fi]]} plan "
                               f"draws it ({why[fi]}): shape it in Revit as drawn")
                        stats["walls a wall elevation shapes, built straight"] += 1
            b.issues.append(Issue(INFO, WALL_ELEV, f"{title}: its levels are the floors {how} ("
                                  + ", ".join(f"{lv['name'] or '(unnamed)'} = {name_of[k]}" for lv, k in zip(we["levels"], got_keys)
                                              if k) + "); " + "; ".join(said), sheet=sid, extra={"from_linker": True}))
        # what else stands on those rafts starts on their tops
        for rid, tops in raft_tops.items():
            r = by_id[rid]
            rmark = r.final("mark") or r.mark or "raft"

            def floor_z(t):                      # the top as (floor index, mm above that floor's line)
                ys, scale, y = t[0], t[1], t[2]
                i = max((i for i, v in enumerate(ys) if v is not None and v <= y + ELEV_TOL_MM * (scale or 1.0)),
                        default=None)
                return (i, (y - ys[i]) / (scale or 1.0)) if i is not None else (None, 0.0)
            zs = [floor_z(t) for t in tops]
            if any(z[0] != zs[0][0] or abs(z[1] - zs[0][1]) > 50.0 for z in zs):
                b.issues.append(Issue(WARNING, WALL_ELEV, f"raft {rmark}: the wall elevations draw its top at different "
                                      f"heights ({'; '.join(t[4] for t in tops)}) - nothing taken from them",
                                      sheet=r.sheet, elements=[rid], extra={"from_linker": True}))
                continue
            ys, scale, ytop, src, why, drawn_t = tops[0]
            fv = r.resolved.get(FOUNDING)
            if fv is not None and fv.status == OVERRIDE:
                # its founding level decided (for it, or the Foundation height for all): the raft is
                # there, and the slab the elevation draws under the wall is not it - nothing is
                # started on that slab
                b.issues.append(Issue(INFO, WALL_ELEV, f"raft {rmark}: founding level {float(fv.value):+.0f} as decided "
                                      f"- {why} is taken as a floor slab, not this raft", sheet=r.sheet,
                                      elements=[rid], extra={"from_linker": True}))
                continue
            _raft_level(b, r, zs[0], drawn_t, heights.get(set_key, {}), keys, src, why, stats)
            for sid in sorted({e.sheet for e in verticals}):
                shift = _shift_by_marks(b, r.sheet, sid)
                mine = [e for e in verticals if e.sheet == sid and e.id not in done
                        and _key_of(str(e.final("mark") or e.mark or "")) not in drawn]
                if not mine:
                    continue
                kind, got = _band_extent(ys, floor_of[sid], ytop, math.inf, scale)
                if kind != "below" and not got:
                    continue                          # the raft's top is not in this floor's storey
                if shift is None:
                    b.issues.append(Issue(INFO, WALL_ELEV, f"raft {rmark}: its top is off this floor's storey "
                                          f"({why}), but this plan could not be placed on the foundation plan "
                                          "(too few column marks on both) - its columns and walls are built from "
                                          "the level below as usual", sheet=sid, extra={"from_linker": True}))
                    continue
                (dx, dy), _n = shift
                for e in mine:
                    c = _centre_of(e)
                    if c is not None and _inside((c[0] - dx, c[1] - dy), r.geometry["outline"]):
                        done[e.id] = (kind, got, f"stands on raft {rmark}: {why}", src)
        # applied, and said per plan
        out, off = defaultdict(list), defaultdict(list)
        for eid, (kind, got, why, src) in done.items():
            e = by_id[eid]
            ev = [Evidence(reader=READER, sheet=src, source=f"{WALL_ELEV}: {why}")]
            mark = str(e.final("mark") or e.mark or "unmarked")
            if kind in ("below", "above"):
                e.resolved["scope"] = Value(f"other: {'under its base' if kind == 'below' else 'over its top'} "
                                            f"- {why}", READ, 0.8, ev)
                e.extra["wall_height"] = {"set": ["scope"]}
                out[e.sheet].append((e, mark, kind, why))
                continue
            put = []
            for k, v in got.items():
                if k == "top_mm" and "top_mm" in e.resolved:
                    continue                          # a building section's top wins
                e.resolved[k] = Value(float(v), READ, 0.85, ev)
                put.append(k)
            if put:
                e.extra["wall_height"] = {"set": put}
                off[e.sheet].append((e, mark, ", ".join(f"{'base' if k == 'base_mm' else 'top'} {got[k]:+.0f}"
                                                        for k in put), why))
        for sid, rows in out.items():
            lv = b.sheets[sid].level or b.sheets[sid].extra.get("level_key") or sid
            marks = sorted({m for _e, m, _k, _w in rows})
            whys = sorted({w for _e, _m, _k, w in rows})
            b.issues.append(Issue(WARNING, "scope", f"{len(rows)} columns/walls of the {lv} plan ({', '.join(marks[:20])}"
                                  f"{' ...' if len(marks) > 20 else ''}) lie outside the height the wall elevations "
                                  f"give them, so they are not built: {' | '.join(whys[:4])}. Choose 'this block' "
                                  "to build them", sheet=sid, elements=[e.id for e, *_ in rows],
                                  extra={"from_linker": True}))
            stats["columns/walls outside the height a wall elevation gives them (not built)"] += len(rows)
        for sid, rows in off.items():
            lv = b.sheets[sid].level or b.sheets[sid].extra.get("level_key") or sid
            whys = sorted({w for *_x, w in rows})
            b.issues.append(Issue(INFO, WALL_ELEV, f"{len(rows)} columns/walls of the {lv} plan start or end off its "
                                  f"levels, as the wall elevations draw them: " + ", ".join(
                                      f"{m} {t}" for _e, m, t, _w in rows[:20]) + (" ..." if len(rows) > 20 else "")
                                  + f" mm ({' | '.join(whys[:4])})",
                                  sheet=sid, elements=[e.id for e, *_ in rows], extra={"from_linker": True}))
            stats["columns/walls given a base or top by a wall elevation"] += len(rows)


def _across(poly, y) -> list:
    """The stretches of a drawn outline cut across at height y: [(x0, x1)]."""
    from shapely.geometry import LineString
    x0, _y0, x1, _y1 = poly.bounds
    cut = poly.intersection(LineString([(x0 - 1, y), (x1 + 1, y)]))
    parts = [g for g in getattr(cut, "geoms", [cut]) if g.geom_type == "LineString" and g.length > 1]
    return sorted((min(c[0] for c in g.coords), max(c[0] for c in g.coords)) for g in parts)


def _profile_walls(b: Building, w: dict, mark: str, bands: list, ys: list, scale, keys: list, floor_of: dict,
                   verticals: list, shifts: dict, title: str, plane: dict | None = None) -> tuple:
    """A wall a wall elevation draws changing shape (SW10's arch, SW15's lean) is built to that
    shape over those storeys (bands: floor indexes, each its storey from the floor below):
    the elevation's outline of the wall cut to the storey, as a profile in the plan wall's
    vertical plane - extra["profile_wall"] {section, origin, dir, thickness, points [[along, up]]},
    mm from the origin (on the plan wall's line) and from the level below. Export / Revit / 3D /
    IFC make it a profile wall.

    The plans (moved onto each other as the 3D view does) give the wall's line, and where along
    it the wall stands at each floor's level - a plan shows the wall under its slab, where that
    floor's beams meet it. Which way round: the way the elevation and the plans move it from
    floor to floor, all agreeing. The elevation is fitted to the plans over every floor; at each
    floor's level the wall is moved onto that floor's plan, the difference spread evenly up the
    storey, so its beams meet it and its storeys join. A floor more than 750 mm off the fit, or
    anything unclear - floors not lined up, the mark not one straight wall, the runs not on one
    line - leaves the wall straight.
    plane: given, gets "dir" - the way the elevation's +x runs on the plans - once the plans and
    the elevation agree which way round the wall stands (for _section_lean).
    Returns ({floor index: '' (shaped) or why not}, how it was placed)."""
    from shapely.geometry import Polygon, box
    s_ = scale or 0.0

    def no(why):
        return {fi: why for fi in bands}, ""
    if not s_ or len(w.get("outline") or []) < 3:
        return no("the elevation's scale is not known")
    poly = Polygon(w["outline"]).buffer(0)
    key = _key_of(mark)
    # every floor's plan wall of this mark: one straight run, lined up
    runs = {}
    for e in verticals:
        if e.type == "wall" and _key_of(str(e.final("mark") or e.mark or "")) == key and len(e.extra.get("runs") or []) == 1:
            runs.setdefault(floor_of[e.sheet], []).append(e)
    runs = {fi: es[0] for fi, es in runs.items() if len(es) == 1 and shifts.get(es[0].sheet) is not None}
    if any(fi not in runs for fi in bands):
        return no(f"the plan does not draw {mark} as one straight wall on that floor, or that floor is not "
                  "lined up with the others")
    ref = runs[bands[0]]
    r0, sh0 = ref.extra["runs"][0], shifts[ref.sheet]
    p0 = (r0["start"][0] + sh0[0], r0["start"][1] + sh0[1])
    dx, dy = r0["end"][0] - r0["start"][0], r0["end"][1] - r0["start"][1]
    L0 = math.hypot(dx, dy) or 1.0
    ux, uy = dx / L0, dy / L0
    pts = {}                                   # floor -> (plan centre along the line, elevation's centre across)
    for fi, e in runs.items():
        r, sh = e.extra["runs"][0], shifts[e.sheet]
        a = (r["start"][0] + sh[0] - p0[0], r["start"][1] + sh[1] - p0[1])
        c = (r["end"][0] + sh[0] - p0[0], r["end"][1] + sh[1] - p0[1])
        if abs((c[0] - a[0]) * uy - (c[1] - a[1]) * ux) > 0.05 * math.hypot(c[0] - a[0], c[1] - a[1])                 or max(abs(-a[0] * uy + a[1] * ux), abs(-c[0] * uy + c[1] * ux)) > 300.0:
            if fi in bands:
                return no(f"its plan walls are not on one line from floor to floor")
            continue
        if ys[fi] is None:
            continue
        # the elevation across, at the floor's level: where it is as long as the plan's wall
        Lp = math.hypot(c[0] - a[0], c[1] - a[1])
        best, h = None, ys[fi] - 600 * s_
        while h <= ys[fi] + 600 * s_:
            got = _across(poly, h)
            if len(got) == 1:
                rank = (round(abs((got[0][1] - got[0][0]) / s_ - Lp) / 25), abs(h - ys[fi]))
                if best is None or rank < best[0]:
                    best = (rank, got[0])
            h += 25 * s_
        if best is None or best[0][0] * 25 > max(150.0, 0.1 * Lp):
            continue
        along = ((a[0] + c[0]) / 2) * ux + ((a[1] + c[1]) / 2) * uy
        pts[fi] = (along, (best[1][0] + best[1][1]) / 2 / s_, fi)
    if len(pts) < 2:
        return no("too few floors draw it to place the elevation on the plan")
    # which way round: across the elevation and along the plan, from floor to floor
    order = sorted(pts)
    votes = []
    for f0, f1 in zip(order, order[1:]):
        dp, de = pts[f1][0] - pts[f0][0], pts[f1][1] - pts[f0][1]
        if abs(dp) > 100 and abs(de) > 100:
            votes.append(1 if dp * de > 0 else -1)
    if not votes or len(set(votes)) > 1:
        return no("which way round it stands on the plan is unclear" if not votes else
                  "the plans and the elevation move it in different directions")
    sg = votes[0]
    if plane is not None and "dir" not in plane:
        plane["dir"] = (sg * ux, sg * uy)
    k = sum(p[0] - sg * p[1] for p in pts.values()) / len(pts)
    off = {fi: p[0] - (sg * p[1] + k) for fi, p in pts.items()}
    worst = max(off, key=lambda fi: abs(off[fi]))
    if abs(off[worst]) > 750.0:
        return no(f"the {keys[worst]} plan draws it {abs(off[worst]):.0f} mm from where the elevation puts it")
    why = {}
    for fi in bands:
        if ys[fi - 1] is None if fi > 0 else True:
            why[fi] = "the elevation does not show the floor below"
            continue
        cut = poly.intersection(box(poly.bounds[0] - 1, ys[fi - 1], poly.bounds[2] + 1, ys[fi]))
        if cut.geom_type != "Polygon":
            cut = max(getattr(cut, "geoms", []), key=lambda g: g.area, default=None)
        if cut is None or cut.geom_type != "Polygon" or cut.area <= 0:
            why[fi] = "the elevation draws nothing of it in that storey"
            continue
        e = runs[fi]
        r, sh = e.extra["runs"][0], shifts[e.sheet]
        rs, re_ = r["start"], r["end"]
        if (re_[0] - rs[0]) * ux + (re_[1] - rs[1]) * uy < 0:
            rs, re_ = re_, rs                          # this floor's run drawn the other way
        t0 = (rs[0] + sh[0] - p0[0]) * ux + (rs[1] + sh[1] - p0[1]) * uy
        # at each floor's level the wall stands where that floor's plan draws it (its beams meet
        # it there); the difference from the elevation is spread evenly up the storey
        lo_off = off.get(fi - 1, off.get(fi, 0.0))
        hi_off = off.get(fi, lo_off)
        H = (ys[fi] - ys[fi - 1]) / s_

        def along(x, y):
            z = (y - ys[fi - 1]) / s_
            return sg * x / s_ + k + lo_off + (hi_off - lo_off) * min(max(z / H, 0.0), 1.0) - t0
        e.extra["profile_wall"] = {
            "section": title, "origin": [rs[0], rs[1]], "dir": [ux, uy], "thickness": float(r["thickness"]),
            "points": _up_to_level([[round(along(x, y), 1), round((y - ys[fi - 1]) / s_, 1)]
                                    for x, y in cut.exterior.coords[:-1]], H)}
        why[fi] = ""
    fit = ", ".join(f"{keys[fi]} {off[fi]:+.0f}" for fi in sorted(off))
    return why, (f"at each floor where its plan draws it; the plans of {len(pts)} floors differ from the "
                 f"elevation's lean by, mm: {fit} - spread up the storeys")


def _up_to_level(points: list, H: float) -> list:
    """A shaped wall's profile ([along, up] mm, up from the level below; H its storey): a top
    drawn under its level by no more than ELEV_TOL_MM (the slab's or beam's underside) taken
    up to the level, as a straight wall's top is (_band_extent) - its beams and slab sit on it."""
    top = max((p[1] for p in points), default=H)
    if not 0.0 < H - top <= ELEV_TOL_MM:
        return points
    return [[p[0], round(H, 1) if p[1] >= top - 1.0 else p[1]] for p in points]


def _section_lean(b: Building, w: dict, mark: str, bands: list, ys: list, scale, keys: list, floor_of: dict,
                  verticals: list, shifts: dict, title: str, plane) -> tuple:
    """What a wall elevation draws changing shape, built as the section draws it (user
    2026-10-28: "SW13 and C12 should be leaning"; "leaning ... based on details and sections,
    not ... column shift"). Winter Villa, SHEAR WALL ELEVATION (SW15 , SA14 , SW13 ,C12 ): SW13
    stands across the elevation, C12 is a column, the plans above draw them shifted up to ~1 m
    off the lean.

    It starts where the plan of the floor under its first leaning storey draws it, and moves
    with the drawn outline's middle, along the way the elevation runs on the plans (plane, from
    _profile_walls); at each floor's level it is moved onto that floor's plan, where its beams
    meet it, the difference spread up the storey (user 2026-10-28: "why disjoint again"), and
    said as a warning past 300 mm. Only what a section draws leaning leans: a column a plan
    merely draws shifted from the floor below stays upright. A wall along that way gets
    extra["profile_wall"], as _profile_walls; anything else extra["lean"] {section, base, top}:
    where its bottom and top stand, mm from where its plan draws it.
    Returns ({floor index: '' (leaning) or why not}, how it was placed)."""
    from shapely.geometry import Polygon, box
    s_ = scale or 0.0

    def no(why):
        return {fi: why for fi in bands}, ""
    if not s_ or len(w.get("outline") or []) < 3:
        return no("the elevation's scale is not known")
    if not plane:
        return no("no wall of the elevation shows which way it faces on the plans")
    poly = Polygon(w["outline"]).buffer(0)
    _x0, y_lo, _x1, y_hi = poly.bounds

    def middle(y):                             # the outline's middle across, at height y
        got = _across(poly, min(max(y, y_lo + 1.0), y_hi - 1.0))
        return (got[0][0] + got[-1][1]) / 2 if got else None
    key = _key_of(mark)
    mine = defaultdict(list)
    for e in verticals:
        if _key_of(str(e.final("mark") or e.mark or "")) == key and shifts.get(e.sheet) is not None \
                and _centre_of(e) is not None:
            mine[floor_of[e.sheet]].append(e)

    def at(e):                                 # its middle, the plans moved onto each other
        c, sh = _centre_of(e), shifts[e.sheet]
        return c[0] + sh[0], c[1] + sh[1]
    a = min(bands) - 1
    if a < 0 or len(mine.get(a, [])) != 1 or ys[a] is None or middle(ys[a]) is None:
        return no(f"the plan under its lean does not draw {mark} once, to start it from")
    pa, ca = at(mine[a][0]), middle(ys[a])
    vx, vy = plane

    def spot(y):                               # where the section puts its middle at height y
        m = middle(y)
        d = (m - ca) / s_ if m is not None else 0.0
        return pa[0] + vx * d, pa[1] + vy * d
    # at each floor's level it stands where that floor's plan draws it - its beams meet it there
    # (user 2026-10-28: the load must travel) - the plan's difference from the section spread up
    # the storey; a floor whose plan does not draw it once takes the one below's
    offs = {}
    for f in range(a, max(bands) + 1):
        es = mine.get(f) or []
        if len(es) == 1 and ys[f] is not None:
            p, q = at(es[0]), spot(ys[f])
            offs[f] = (p[0] - q[0], p[1] - q[1])

    def off_at(f):
        g = max((k for k in offs if k <= f), default=a)
        return offs.get(g, (0.0, 0.0))
    why, off, kinds = {}, {}, set()
    for fi in bands:
        es = mine.get(fi) or []
        if len(es) != 1 or ys[fi - 1] is None or ys[fi] is None:
            why[fi] = f"the plan does not draw {mark} once on that floor"
            continue
        e, p = es[0], at(es[0])
        runs = e.extra.get("runs") or []
        r = runs[0] if e.type == "wall" and len(runs) == 1 else None
        L = math.dist(r["start"], r["end"]) if r else 0.0
        if r and L > 1.0 and abs((r["end"][0] - r["start"][0]) * vx + (r["end"][1] - r["start"][1]) * vy) > 0.9 * L:
            # along the elevation: its drawn outline as a profile wall, as _profile_walls
            cut = poly.intersection(box(poly.bounds[0] - 1, ys[fi - 1], poly.bounds[2] + 1, ys[fi]))
            if cut.geom_type != "Polygon":
                cut = max(getattr(cut, "geoms", []), key=lambda g: g.area, default=None)
            if cut is None or cut.geom_type != "Polygon" or cut.area <= 0:
                why[fi] = "the elevation draws nothing of it in that storey"
                continue
            rs, re_ = r["start"], r["end"]
            if (re_[0] - rs[0]) * vx + (re_[1] - rs[1]) * vy < 0:
                rs, re_ = re_, rs
            ux, uy = (re_[0] - rs[0]) / L, (re_[1] - rs[1]) / L
            sh = shifts[e.sheet]
            t0 = (pa[0] - rs[0] - sh[0]) * ux + (pa[1] - rs[1] - sh[1]) * uy
            o0, o1 = off_at(fi - 1), offs[fi]
            d0, d1 = o0[0] * ux + o0[1] * uy, o1[0] * ux + o1[1] * uy
            H = (ys[fi] - ys[fi - 1]) / s_

            def along(x, y):
                z = min(max((y - ys[fi - 1]) / s_ / H, 0.0), 1.0)
                return t0 + (x - ca) / s_ + d0 + (d1 - d0) * z
            e.extra["profile_wall"] = {
                "section": title, "origin": [rs[0], rs[1]], "dir": [ux, uy], "thickness": float(r["thickness"]),
                "points": _up_to_level([[round(along(x, y), 1), round((y - ys[fi - 1]) / s_, 1)]
                                        for x, y in cut.exterior.coords[:-1]], H)}
            kinds.add("a profile wall")
        else:
            (lx, ly), (hx, hy) = spot(ys[fi - 1]), spot(ys[fi])
            o0, o1 = off_at(fi - 1), offs[fi]
            lo, hi = (lx + o0[0], ly + o0[1]), (hx + o1[0], hy + o1[1])
            e.extra["lean"] = {"section": title, "base": [round(lo[0] - p[0], 1), round(lo[1] - p[1], 1)],
                               "top": [round(hi[0] - p[0], 1), round(hi[1] - p[1], 1)]}
            kinds.add("a slanted column" if e.type == "column" else "a slanted wall")
        off[fi] = math.dist(spot(ys[fi]), p)
        why[fi] = ""
    fit = ", ".join(f"{keys[fi]} {off[fi]:.0f}" for fi in sorted(off))
    if off and max(off.values()) > 300.0:
        worst = max(off, key=off.get)
        b.issues.append(Issue(WARNING, WALL_ELEV, f"{mark}: the plans and {title} disagree where it stands - the "
                              f"{keys[worst]} plan draws it {off[worst]:.0f} mm from where the section's lean puts it "
                              f"(mm, by floor: {fit}). Built to meet each floor's beams where the plans draw it, "
                              "the section's shape between; correct the drawing that is wrong", mark=mark,
                              extra={"from_linker": True}))
    return why, (f"{' / '.join(sorted(kinds))}: the section's shape, meeting each floor where its plan draws it "
                 f"(its beams there); the plans draw it off the section's lean by, mm: {fit}")


def _raft_level(b: Building, r, z, drawn_t, heights: dict, keys: list, src: str, why: str, stats) -> None:
    """A raft's founding level (its bottom) from the top a wall elevation draws (z: floor index,
    mm above that floor's level line): that top less its thickness - the one decided in the
    review, else the one drawn under the wall (the elevation's scale confirmed by its written
    storey heights). Nothing when that floor has no elevation (read or decided), or when a
    founding level was decided - for this raft in the review, or for all (the Foundation height
    on the Floors page, "founding level for all"): the person's level wins."""
    i, off = z
    fv = r.resolved.get(FOUNDING)
    if i is None or (fv is not None and fv.status == OVERRIDE):
        return                                       # decided: for this raft, or for all on the Floors page
    rmark = r.final("mark") or r.mark or "raft"
    if heights.get(keys[i]) is None:
        b.issues.append(Issue(INFO, WALL_ELEV, f"raft {rmark}: {why} - give {keys[i]} an elevation (Floors page) "
                              "and its founding level follows", sheet=r.sheet, elements=[r.id],
                              extra={"from_linker": True}))
        return
    top = heights[keys[i]] + off
    put = []
    tv = r.resolved.get("thickness")
    if tv is None or tv.status not in (READ, OVERRIDE) or not tv.value:
        if not drawn_t:
            return
        r.resolved["thickness"] = Value(float(round(drawn_t)), READ, 0.6, [Evidence(
            reader=READER, sheet=src, source=f"{WALL_ELEV}: drawn {drawn_t:.0f} thick under the wall - {why}")])
        put.append("thickness")
    t = float(r.resolved["thickness"].value)
    r.resolved[FOUNDING] = Value(float(round(top - t)), READ, 0.8, [Evidence(
        reader=READER, sheet=src, source=f"{WALL_ELEV}: top {top:+.0f} less {t:.0f} thick - {why}")])
    put.append(FOUNDING)
    r.extra["wall_height"] = {"set": put}
    # no longer missing: out of the 'no founding level' / 'no thickness' issues
    for iss in list(b.issues):
        if r.id in iss.elements and iss.category == "missing" and ("founding level" in iss.message or
                                                                    (put[0] == "thickness" and "thickness" in iss.message)):
            iss.elements.remove(r.id)
            if not iss.elements:
                b.issues.remove(iss)
    note = (f"the elevation draws it {drawn_t:.0f} thick; {t:.0f}, decided in the review, is used"
            if drawn_t and tv is not None and tv.status == OVERRIDE and abs(t - drawn_t) > 1 else f"{t:.0f} thick")
    b.issues.append(Issue(INFO, WALL_ELEV, f"raft {rmark}: top at {top:+.0f} ({why}); {note} - founding level "
                          f"{top - t:+.0f}", sheet=r.sheet, elements=[r.id], extra={"from_linker": True}))
    stats["rafts given a founding level by a wall elevation"] += 1


# a column that ends at this floor, however the drawing words it: COLUMN STOP, STOP COLUMN,
# COLUMN TERMINATES, COLUMN ENDS HERE, DISCONTINUED, NOT CONTINUED, UP TO THIS LEVEL. Never "SC"
# alone: a stub column to one office, a steel column to another
# (letters around a word, not \b: layer names join words with _ as often as with a space)
COLUMN_ENDS = re.compile(r"(?<![A-Z])STOP|TERMINAT|(?<![A-Z])END(?:S|ING)?(?![A-Z])|DISCONTINU|NOT[\s_-]*CONTINU"
                         r"|UP[\s_-]*TO[\s_-]*THIS", re.I)
COLUMN_STARTS = re.compile(r"(?<![A-Z])STUB|FLOAT|(?<![A-Z])START(?:S|ING)?(?![A-Z])", re.I)


def _column_stops(b: Building, stats) -> None:
    """Columns and walls drawn on a plan's 'column stop' layer (COLUMN_ENDS: stop, terminates,
    ends, discontinued ...) end at that floor: built up to it and none above, as the plans
    above leave them out. Said per plan, so it shows - and a plan above that still draws one
    of those marks is pointed out."""
    for sid, s in b.sheets.items():
        if s.reader != "plan.legacy":
            continue
        stop = [e for e in b.elements if e.sheet == sid and e.type in ("column", "wall") and not gone(e)
                and COLUMN_ENDS.search(str(e.extra.get("_layer") or ""))
                and not COLUMN_STARTS.search(str(e.extra.get("_layer") or ""))]
        if not stop:
            continue
        r = level_rank(s.extra.get("level_key") or "")
        above = [o for o, os_ in b.sheets.items() if os_.reader == "plan.legacy" and r is not None
                 and os_.extra.get("set") == s.extra.get("set")
                 and (level_rank(os_.extra.get("level_key") or "") or -1e9) > r]
        nxt = min(above, key=lambda o: level_rank(b.sheets[o].extra["level_key"])) if above else None
        # a column's mark drawn again above (a wall carries its mark on, piece by piece)
        up = {_key_of(str(o.final("mark") or o.mark or "")) for o in b.elements
              if o.sheet == nxt and o.type == "column" and not gone(o)} if nxt else set()
        marks = sorted({str(e.final("mark") or e.mark or "unmarked") for e in stop})
        again = sorted({str(e.final("mark") or e.mark) for e in stop if e.type == "column"
                        and (e.final("mark") or e.mark) and _key_of(str(e.final("mark") or e.mark)) in up})
        for e in stop:
            e.extra["stops_here"] = True
        lv = s.level or s.extra.get("level_key") or sid
        b.issues.append(Issue(WARNING if again else INFO, "column stop",
                              f"{len(stop)} columns/walls drawn on the column-stop layer end at {lv}: "
                              f"{', '.join(marks)} - built up to {lv}, none above"
                              + (f". The {b.sheets[nxt].level or b.sheets[nxt].extra.get('level_key')} plan still "
                                 f"draws {', '.join(again)}: check whether that is the same column" if again else ""),
                              sheet=sid, elements=[e.id for e in stop], extra={"from_linker": True}))
        stats["columns/walls ending at their floor (column-stop layer)"] += len(stop)


def _section_heights(b: Building, stats) -> None:
    """A building section (readers/building_sections.py) put on the plan by its beam marks:
    the marked beams in the section are matched to plan beams of the same marks, parallel,
    at the section's spacings. Then a beam whose top the section shows off its level gets
    that height, and a slab the section draws sloping from one beam to another slopes
    between them - with the beams that cross that strip (Main Block terrace, SECTION C-C:
    B19's top 2270 below the terrace, slab S6 rising from it to B7)."""
    from .sheets.levels import levels_in
    for e in b.elements:
        for k in ("section_top", "slope", "slope_profile"):
            e.extra.pop(k, None)
    own = _own_sections(b)
    for sec in b.of("building_section"):
        set_ = _set_of(b, sec)
        scale = float(sec.get("scale") or 1.0)
        boxes = sec.extra.get("beams") or []
        for sid, s in b.sheets.items():
            if s.reader != "plan.legacy" or s.extra.get("set", "") != set_:
                continue
            if _drawing_of(sid) != _drawing_of(sec.sheet) and \
                    (sec.mark or "").upper() in own.get((set_, _drawing_of(sid)), ()):
                continue                    # the plan's own drawing has a section of that name
            key = s.extra.get("level_key") or ""
            lvl = next((l for l in sec.extra.get("levels") or [] if key and key in levels_in(l["name"])), None)
            if lvl is None:
                continue
            beams = [e for e in b.elements if e.sheet == sid and e.type in ("beam", "beam_span") and not gone(e)
                     and not e.geometry.get("mid") and e.geometry.get("start")]
            got = _place_section(beams, boxes, scale)
            if not got:
                continue
            placed, origin, (ux, uy), (nx, ny), sign = got
            name = f"{sec.mark}-{sec.mark}"
            # the scale, checked on the beams' depths where known (box height / depth)
            ratios = [(boxes[i]["top"] - boxes[i]["bottom"]) / float(e.final("depth"))
                      for i, e in placed.items() if e.final("depth")]
            if ratios and all(abs(r - scale) > 0.1 * scale for r in ratios):
                scale = sorted(ratios)[len(ratios) // 2]
            height = {i: round((boxes[i]["top"] - lvl["y"]) / scale) for i in placed}

            def across(e):                                 # a beam's place across the section, on the plan
                g = e.geometry
                m = ((g["start"][0] + g["end"][0]) / 2.0, (g["start"][1] + g["end"][1]) / 2.0)
                return (m[0] - origin[0]) * nx + (m[1] - origin[1]) * ny
            pos = {i: across(e) for i, e in placed.items()}
            ev = Evidence(reader=READER, sheet=sec.sheet, source=f"section {name}", text=sec.extra.get("title", ""))
            for i, e in placed.items():
                if abs(height[i]) > 25:
                    e.extra["section_top"] = height[i]
                    e.resolved["top_mm"] = Value(float(height[i]), READ, 0.9, [Evidence(
                        reader=READER, sheet=sec.sheet, source=f"section {name}: top {height[i]:+.0f} from "
                        f"{lvl['name']} level", text=sec.extra.get("title", ""))])
            def columns_under():
                # a column under a lowered beam ends at that beam (section C-C draws B19's column
                # stopping under it) - unless a beam reaches it higher up
                for i, e in placed.items():
                    if abs(height[i]) <= 25:
                        continue
                    (ax, ay), (bx, by) = e.geometry["start"][:2], e.geometry["end"][:2]
                    L = math.hypot(bx - ax, by - ay) or 1.0
                    for c in b.elements:
                        if c.sheet != sid or c.type != "column" or c.geometry.get("x") is None:
                            continue
                        cx, cy = c.geometry["x"], c.geometry["y"]
                        t = ((cx - ax) * (bx - ax) + (cy - ay) * (by - ay)) / (L * L)
                        off = abs((cx - ax) * -(by - ay) / L + (cy - ay) * (bx - ax) / L)
                        half = 0.5 * max(float(c.get("width") or 0), float(c.get("depth") or 0))
                        reach = (half + 50.0) / L                    # a beam ends inside the column
                        if not (-reach <= t <= 1.0 + reach and off <= 0.5 * float(e.final("width") or 230) + half):
                            continue
                        higher = False
                        for o in beams:
                            if o is e:
                                continue
                            for end, frac in ((o.geometry["start"], 0.0), (o.geometry["end"], 1.0)):
                                if math.hypot(end[0] - cx, end[1] - cy) > half + 300:
                                    continue
                                prof = o.extra.get("slope_profile")
                                zo = (prof[0][1] if frac == 0.0 else prof[-1][1]) if prof else \
                                    (o.extra.get("section_top") or 0)
                                if zo > height[i] + 50:
                                    higher = True
                        if not higher:
                            c.extra["section_top"] = height[i]
                            c.resolved["top_mm"] = Value(float(height[i]), READ, 0.9, [Evidence(
                                reader=READER, sheet=sec.sheet, source=f"section {name}: ends under "
                                f"{e.final('mark') or e.mark}, {height[i]:+.0f} from {lvl['name']} level",
                                text=sec.extra.get("title", ""))])
            sloped = []
            for sl in sec.extra.get("slopes") or []:
                i, j = sl["between"]
                if i not in placed or j not in placed:
                    continue
                pi, pj, hi, hj = pos[i], pos[j], height[i], height[j]
                lo_p, hi_p = sorted((pi, pj))
                # and along them: only as far as the two beams the slope runs between
                along = [(q[0] - origin[0]) * ux + (q[1] - origin[1]) * uy
                         for e in (placed[i], placed[j]) for q in (e.geometry["start"], e.geometry["end"])]
                lo_u, hi_u = min(along) - 300.0, max(along) + 300.0

                def in_u(pt):
                    return lo_u <= (pt[0] - origin[0]) * ux + (pt[1] - origin[1]) * uy <= hi_u

                def z_at(x, y):
                    p = (x - origin[0]) * nx + (y - origin[1]) * ny
                    p = min(max(p, lo_p), hi_p)
                    return hi + (p - pi) * (hj - hi) / (pj - pi)

                def p_of(pt):
                    return (pt[0] - origin[0]) * nx + (pt[1] - origin[1]) * ny
                for e in b.elements:
                    if e.sheet != sid or gone(e):
                        continue
                    if e.type == "slab" and len(e.geometry.get("outline") or []) >= 3:
                        ps = [p_of(q) for q in e.geometry["outline"]]
                        if min(ps) >= lo_p - 400 and max(ps) <= hi_p + 400 and max(ps) - min(ps) > 0.5 * (hi_p - lo_p) \
                                and all(in_u(q) for q in e.geometry["outline"]):
                            e.extra["slope"] = {"origin": list(origin), "dir": [nx, ny], "p0": pi, "z0": hi,
                                                "p1": pj, "z1": hj}
                            e.resolved["slope"] = Value(f"{hi:+.0f} to {hj:+.0f} mm", READ, 0.9, [ev])
                            sloped.append(e)
                    elif e.type in ("beam", "beam_span") and e not in placed.values() and e.geometry.get("start") \
                            and not e.geometry.get("mid"):
                        a, c = e.geometry["start"], e.geometry["end"]
                        pa, pc = p_of(a), p_of(c)
                        if max(pa, pc) <= lo_p + 1 or min(pa, pc) >= hi_p - 1 or abs(pa - pc) < 100 \
                                or not (in_u(a) and in_u(c)):
                            continue                       # outside the strip, or along it
                        cuts = sorted({0.0, 1.0} | {(q - pa) / (pc - pa) for q in (lo_p, hi_p)
                                                    if 0.0 < (q - pa) / (pc - pa) < 1.0})
                        prof = [[round(t, 4), round(z_at(a[0] + (c[0] - a[0]) * t, a[1] + (c[1] - a[1]) * t))]
                                for t in cuts]
                        if any(abs(z) > 25 for _, z in prof):
                            e.extra["slope_profile"] = prof
                            sloped.append(e)
            columns_under()                    # after the slopes: a sloping beam may reach a column higher
            stats["beams and slabs given heights by a building section"] += len(sloped) + \
                sum(1 for i in placed if abs(height[i]) > 25)
            marks = ", ".join(sorted({e.final("mark") or e.mark for e in placed.values()}))
            low = [f"{placed[i].final('mark') or placed[i].mark} {height[i]:+.0f}" for i in placed if abs(height[i]) > 25]
            b.issues.append(Issue(INFO, "section", f"section {name} placed on the plan by its beams {marks}"
                                  + (f"; tops off the {lvl['name']} level: {', '.join(low)} mm" if low else "")
                                  + (f"; {sum(1 for e in sloped if e.type == 'slab')} slab(s) and "
                                     f"{sum(1 for e in sloped if e.type != 'slab')} beam(s) slope with it" if sloped else ""),
                                  sheet=sid, elements=[e.id for e in list(placed.values()) + sloped][:200],
                                  extra={"from_linker": True}))


def _place_section(beams, boxes, scale):
    """Match a section's marked beam boxes to plan beams: same marks, parallel, at the
    section's spacings (3 %, 150 mm), viewed either way. Returns ({box index: beam}, origin,
    along, across, sign) for the best fit with at least two beams, else None."""
    marked = [(i, _key_of(x["mark"]), (x["x0"] + x["x1"]) / 2.0) for i, x in enumerate(boxes) if x["mark"]]
    if len(marked) < 2:
        return None
    by_mark = defaultdict(list)
    for e in beams:
        by_mark[_key_of(e.final("mark") or e.mark)].append(e)
    best = None
    i0, k0, x0 = marked[0]
    for ref in by_mark.get(k0, []):
        (ax, ay), (bx, by) = ref.geometry["start"][:2], ref.geometry["end"][:2]
        L = math.hypot(bx - ax, by - ay) or 1.0
        ux, uy = (bx - ax) / L, (by - ay) / L
        nx, ny = -uy, ux
        for sign in (1.0, -1.0):
            got = {i0: ref}
            for i, k, x in marked[1:]:
                want = (x - x0) / scale * sign
                for e in by_mark.get(k, []):
                    (cx, cy), (dx, dy) = e.geometry["start"][:2], e.geometry["end"][:2]
                    l2 = math.hypot(dx - cx, dy - cy) or 1.0
                    if abs(ux * (dy - cy) / l2 - uy * (dx - cx) / l2) > 0.05:
                        continue                                  # not parallel
                    m = ((cx + dx) / 2.0, (cy + dy) / 2.0)
                    s = (m[0] - ax) * nx + (m[1] - ay) * ny
                    t = (m[0] - ax) * ux + (m[1] - ay) * uy
                    if abs(s - want) <= max(150.0, 0.03 * abs(want)) and -3000.0 <= t <= L + 3000.0:
                        got[i] = e
                        break
            if len(got) >= 2 and (best is None or len(got) > len(best[0])):
                best = (got, (ax, ay), (ux, uy), (nx, ny), sign)
    return best


def _supports_and_beams(b: Building, sid: str):
    """The plan's straight beams [(element, start, end, width)] and its supports
    (columns, RC walls) as one shape, for the panel and run rules below."""
    from shapely.geometry import Polygon
    from shapely.ops import unary_union
    beams, sup = [], []
    for e in b.elements:
        if e.sheet != sid or gone(e):
            continue
        g = e.geometry
        if e.type in ("beam", "beam_span") and g.get("start") and g.get("end") and not g.get("mid"):
            w = e.get("width") or 230.0
            beams.append((e, tuple(g["start"][:2]), tuple(g["end"][:2]), float(w)))
        elif e.type in ("column", "wall") and len(g.get("outline") or []) >= 3:
            if e.type == "wall" and e.final("role") == "masonry":
                continue
            try:
                sup.append(Polygon([p[:2] for p in g["outline"]]).buffer(0))
            except Exception:
                continue
    return beams, (unary_union(sup) if sup else None)


def _panel_marks(b: Building) -> dict[str, tuple[str, str]]:
    """Slab bays with no mark written in them that are part of a marked panel.

    A slab mark names the panel between two beams. A beam dividing that panel in two
    ends on those two beams, and the bays either side of it both run along them: New
    block writes NS4 once per row and its SB beams, each ending on NB3 and NB4, split
    the row into three or four bays. Two bays separated by such a beam are one panel;
    an unmarked bay joined that way to marked bays (through other unmarked bays too),
    all of them carrying one mark, takes that mark. Across a beam that does not divide
    a panel so (NB4 ends on B32 and NB5, which no NS4 bay runs along both of), or when
    the marks reached differ, nothing is taken: the nearest mark stays a working value.
    {slab id: (mark, the marked bay's id)}"""
    from shapely.geometry import LineString, Point, Polygon
    out = {}
    for sid in b.sheets:
        slabs = []
        for e in b.elements:
            if e.sheet == sid and e.type == "slab" and not gone(e) and len(e.geometry.get("outline") or []) >= 3:
                try:
                    p = Polygon([q[:2] for q in e.geometry["outline"]]).buffer(0)
                except Exception:
                    continue
                unmarked = not e.mark or any("no slab mark in this" in str(n) for n in e.notes)
                slabs.append((e, p, "" if unmarked else _key_of(e.mark)))
        if not any(not m for _e, _p, m in slabs) or not any(m for _e, _p, m in slabs):
            continue
        beams, _sup = _supports_and_beams(b, sid)
        bands = [LineString([a, c]).buffer(w / 2.0, cap_style=2) for _e, a, c, w in beams]

        def resting_on(k):
            """The beam lines beam k ends on, one shape per end (its spans together).
            None when an end is on a column or wall: that beam is part of the frame and
            divides panels (Stilt A wing B11A, 230 x 750 on two columns, between the sunk
            S1 strip and the bays above it)."""
            _e, a, c, _w = beams[k]
            if _sup is not None and any(_sup.buffer(50.0).contains(Point(p)) for p in (a, c)):
                return None
            ends = []
            for p in (a, c):
                on = [bands[j] for j in range(len(beams)) if j != k and bands[j].buffer(50.0).contains(Point(p))]
                if not on:
                    return None
                ends.append(on[0].union(on[1]) if len(on) > 1 else on[0])
            return ends

        def runs_along(p, band):
            return p.buffer(100.0).intersection(band).area > 0.05e6

        # beam lines (spans of one beam in line, end to end) and their order: 0 on a
        # column or wall, else one more than the lines they rest on. A beam of order 2+
        # rests on beams that rest on beams: a secondary beam inside a panel (New block's
        # SB grid in the NS4 rows, an SB resting on the SBs across it)
        line_of = list(range(len(beams)))

        def root(k):
            while line_of[k] != k:
                line_of[k] = line_of[line_of[k]]
                k = line_of[k]
            return k

        for i in range(len(beams)):
            _e, a, c, w = beams[i]
            L = math.dist(a, c) or 1.0
            u = ((c[0] - a[0]) / L, (c[1] - a[1]) / L)
            for j in range(i + 1, len(beams)):
                _f, p2, q2, w2 = beams[j]
                if abs(w - w2) > 10.0:
                    continue
                M = math.dist(p2, q2) or 1.0
                if abs(u[0] * (q2[1] - p2[1]) / M - u[1] * (q2[0] - p2[0]) / M) > 0.035:
                    continue
                if any(math.dist(x, y) <= 30.0 for x in (a, c) for y in (p2, q2)):
                    line_of[root(i)] = root(j)
        members = defaultdict(list)
        for k in range(len(beams)):
            members[root(k)].append(k)
        order: dict[int, int] = {}

        def line_order(r, depth=0):
            if r in order:
                return order[r]
            if depth > 6:
                return 0
            pts = [p for k in members[r] for p in beams[k][1:3]]
            k0 = members[r][0]
            _e, a, c, _w = beams[k0]
            L = math.dist(a, c) or 1.0
            u = ((c[0] - a[0]) / L, (c[1] - a[1]) / L)
            ends = (min(pts, key=lambda p: p[0] * u[0] + p[1] * u[1]), max(pts, key=lambda p: p[0] * u[0] + p[1] * u[1]))
            if _sup is not None and any(_sup.buffer(50.0).contains(Point(p)) for p in ends):
                order[r] = 0
                return 0
            under = []
            for p in ends:
                on = {root(j) for j in range(len(beams)) if root(j) != r and bands[j].buffer(50.0).contains(Point(p))}
                if not on:
                    order[r] = 0
                    return 0
                under.append(max(line_order(x, depth + 1) for x in on))
            order[r] = 1 + max(under)
            return order[r]

        joined = defaultdict(set)
        for i in range(len(slabs)):
            for j in range(i + 1, len(slabs)):
                between = slabs[i][1].buffer(300.0).intersection(slabs[j][1].buffer(300.0))
                if between.is_empty or between.area < 0.05e6:
                    continue
                across = [k for k, bd in enumerate(bands) if bd.intersection(between).area > 0.3 * between.area]
                if len(across) != 1:
                    continue
                ends = resting_on(across[0])
                if (ends and all(runs_along(slabs[n][1], e) for n in (i, j) for e in ends)) or \
                        line_order(root(across[0])) >= 2:
                    joined[i].add(j)
                    joined[j].add(i)
        for i, (e, _p, m) in enumerate(slabs):
            if m:
                continue
            seen, todo, found = {i}, [i], {}
            while todo:
                for j in joined[todo.pop()]:
                    if j in seen:
                        continue
                    seen.add(j)
                    if slabs[j][2]:
                        found.setdefault(slabs[j][2], slabs[j][0].id)     # a marked bay: stop there
                    else:
                        todo.append(j)
            if len(found) == 1:
                out[e.id] = next(iter(found.items()))
    return out


def _run_marks(b: Building) -> dict[str, tuple[str, str]]:
    """Beam spans with no mark that carry on a marked beam.

    A beam's mark names it from support to support. The plan reader cuts a beam line
    at every beam crossing it, so the spans between a crossing and the next carry no
    tag of their own (New block: the column line marked NB5a..NB5e is cut by every
    cross beam). An unmarked span running straight on from a marked one - same width,
    end to end, with no column or wall at the joint - takes its mark, through other
    unmarked spans too; when the run reaches two different marks, nothing is taken.
    {beam id: (mark, the marked span's id)}"""
    from shapely.geometry import Point
    out = {}
    for sid in b.sheets:
        beams, sup = _supports_and_beams(b, sid)
        if not beams:
            continue
        marked = {k: _key_of(e.mark) for k, (e, *_r) in enumerate(beams)
                  if e.mark and (parse_mark(e.mark) is None or parse_mark(e.mark).family() in ("beam", "unknown"))}
        if not marked or len(marked) == len(beams):
            continue

        def unit(k):
            _e, a, c, _w = beams[k]
            L = math.dist(a, c) or 1.0
            return ((c[0] - a[0]) / L, (c[1] - a[1]) / L)

        nxt = defaultdict(set)
        for i in range(len(beams)):
            ui = unit(i)
            for j in range(i + 1, len(beams)):
                if abs(beams[i][3] - beams[j][3]) > 10.0:
                    continue
                uj = unit(j)
                if abs(ui[0] * uj[1] - ui[1] * uj[0]) > 0.035:
                    continue                                   # not in line
                for p in beams[i][1:3]:
                    for q in beams[j][1:3]:
                        if math.dist(p, q) <= 30.0 and \
                                abs((q[0] - beams[i][1][0]) * -ui[1] + (q[1] - beams[i][1][1]) * ui[0]) <= 30.0:
                            if sup is not None and sup.buffer(30.0).contains(Point(p)):
                                continue                       # a column between: another beam
                            nxt[i].add(j)
                            nxt[j].add(i)
        for i, (e, *_r) in enumerate(beams):
            if i in marked or e.mark:
                continue
            seen, todo, found = {i}, [i], {}
            while todo:
                for j in nxt[todo.pop()]:
                    if j in seen:
                        continue
                    seen.add(j)
                    if j in marked:
                        found.setdefault(marked[j], beams[j][0].id)
                    else:
                        todo.append(j)
            if len(found) == 1:
                out[e.id] = next(iter(found.items()))
    return out


def _detail_span_marks(b: Building, from_captions) -> dict[str, tuple[str, str]]:
    """Unmarked spans of a beam its detail shows running over several grids.

    NIFT writes 2B5 once on the plan, on one span; its beam detail draws it as one beam
    from grid (A)Y11' to (A)Y17, over the columns between (beam_sections._detail_grid_rows).
    The plan's grid lines are named by the labels at their ends; an unmarked span in line
    with the marked one, as wide, and between the first and last of those grids, is that
    beam. Two beams' details claiming one span: nothing taken. Only where no other source
    marks the span (it is an "inherited" mark). {beam id: (mark, the marked span's id)}"""
    from .readers.beam_sections import _axis as grid_axis, grid_key
    spans = defaultdict(list)                       # (set, mark) -> [[grid label, ...], ...]
    for s in b.of("spec"):
        if s.extra.get("grid_span") and s.get("family") == "beam" and s.mark:
            spans[(_set_of(b, s), _key_of(s.mark))].append([grid_key(g) for g in s.extra["grid_span"]])
    if not spans:
        return {}
    out = {}
    for sid, s in b.sheets.items():
        view = b.sheets.get(s.extra.get("view_sheet", ""))
        if s.reader != "plan.legacy" or view is None or not view.extra.get("grid_labels") or not s.grids:
            continue
        # each label names the grid line ending nearest it (its bubble beside the line's end:
        # NIFT's labels stand 600 off the line); a label at both ends naming two lines: neither
        named = {}
        for text, x, y, h in view.extra["grid_labels"]:
            best = None
            for g in s.grids:
                (ax, ay), (cx, cy) = g["start"][:2], g["end"][:2]
                L = math.hypot(cx - ax, cy - ay)
                if L <= 0 or min(math.hypot(x - ax, y - ay), math.hypot(x - cx, y - cy)) > 8.0 * h:
                    continue
                across = abs((x - ax) * (cy - ay) - (y - ay) * (cx - ax)) / L
                if across <= 4.0 * h and (best is None or across < best[0]):
                    best = (across, ((ax, ay), (cx, cy)))
            if best:
                named.setdefault(grid_key(text), set()).add(best[1])
        named = {k: next(iter(v)) for k, v in named.items() if len(v) == 1}
        # a grid shown only by its labelled bubble (NIFT 4th floor: 13 lines drawn for 23
        # grids) runs through its label, as the drawn lines of its own family run; with no
        # line of its family drawn, it is not placed
        fam = {}
        for k, ((ax, ay), (cx, cy)) in named.items():
            L = math.hypot(cx - ax, cy - ay)
            fam.setdefault(grid_axis(k), ((cx - ax) / L, (cy - ay) / L))
        for text, x, y, h in view.extra["grid_labels"]:
            k = grid_key(text)
            d = fam.get(grid_axis(k))
            if k not in named and d is not None:
                named[k] = ((x, y), (x + 1000.0 * d[0], y + 1000.0 * d[1]))
        beams = [e for e in b.elements if e.sheet == sid and e.type in ("beam", "beam_span") and not gone(e)
                 and not e.geometry.get("mid") and e.geometry.get("start") and e.geometry.get("end")]

        def marks_of(e):
            # its own tag, and the captions beside it (NIFT tags its beams by captions)
            return {m for m, _ in from_captions.get(e.id, [])} | ({_key_of(e.mark)} if e.mark else set())
        claims = defaultdict(set)
        for m in beams:
            mine = marks_of(m)
            ranges = spans.get((s.extra.get("set", ""), next(iter(mine)))) if len(mine) == 1 else None
            if not ranges:
                continue
            mark = next(iter(mine))
            (px, py), (qx, qy) = m.geometry["start"][:2], m.geometry["end"][:2]
            L = math.hypot(qx - px, qy - py) or 1.0
            ux, uy = (qx - px) / L, (qy - py) / L

            def along(pt):
                return (pt[0] - px) * ux + (pt[1] - py) * uy

            def cross_at(line):
                """Where a grid line crosses this beam's line: distance along it, or None."""
                (ax, ay), (cx, cy) = line
                dx, dy = cx - ax, cy - ay
                den = ux * dy - uy * dx
                if abs(den) < 0.3 * math.hypot(dx, dy):
                    return None                     # (nearly) parallel: not across it
                t = ((ax - px) * dy - (ay - py) * dx) / den
                return t
            for labels in ranges:
                ts = [t for g in labels if g in named and (t := cross_at(named[g])) is not None]
                if len(ts) < 2:
                    continue
                lo, hi = min(ts) - 300.0, max(ts) + 300.0
                if not lo <= along(((px + qx) / 2.0, (py + qy) / 2.0)) <= hi:
                    continue                        # the marked span is not in that stretch
                w = float(m.get("width") or 0.0)
                for e in beams:
                    if e is m or marks_of(e):
                        continue
                    (ex, ey), (fx, fy) = e.geometry["start"][:2], e.geometry["end"][:2]
                    le = math.hypot(fx - ex, fy - ey) or 1.0
                    if abs(((fx - ex) / le) * uy - ((fy - ey) / le) * ux) > 0.035:
                        continue                    # not in line
                    if max(abs((ex - px) * -uy + (ey - py) * ux), abs((fx - px) * -uy + (fy - py) * ux)) > 60.0:
                        continue
                    if w and e.get("width") and abs(float(e.get("width")) - w) > 10.0:
                        continue
                    if lo <= min(along((ex, ey)), along((fx, fy))) and max(along((ex, ey)), along((fx, fy))) <= hi:
                        claims[e.id].add((mark, m.id))
        for eid, got in claims.items():
            if len({_key_of(mk) for mk, _ in got}) == 1:
                out[eid] = sorted(got)[0]
    return out


def _size_captions(b: Building, readings, cands, stats) -> None:
    """A size written along a beam with no mark beside it gives that beam its size.

    aldoot: 9"X21" / 12"X24" written on each beam, B-0 on a few. The size goes to the
    beam it runs along and sits on (as a plan caption does); when two beams are about
    as close, or the written width is not the beam's drawn width (within 15 mm - then
    it names some other member), it is left alone and said so."""
    for sc in b.of("size_caption"):
        beams = [e for sid in readings.get(sc.sheet, []) for e in b.of(sheet=sid)
                 if e.type in ("beam", "beam_span") and not gone(e) and e.geometry.get("start")]
        if not beams:
            continue
        p = sc.geometry["at"]
        th = max(sc.geometry.get("text_height") or 100.0, 1.0)
        rot = sc.geometry.get("rotation")
        reach = max(4 * th, 600.0)
        near = sorted(((_beam_dist(p, e), e) for e in beams if rot is None or _parallel(rot, e)),
                      key=lambda x: (not x[0][1], x[0][0]))
        near = [(d, e) for (d, ins), e in near if d <= reach and ins] or \
               [(d, e) for (d, ins), e in near if d <= reach]
        why = ""
        if not near:
            why = "no beam runs along it"
        elif len(near) > 1 and near[1][0] < 1.25 * max(near[0][0], th * 0.5):
            why = f"two beams almost equally close ({near[0][0]:.0f} and {near[1][0]:.0f} mm)"
        else:
            hit = near[0][1]
            drawn = hit.get("width")
            w, d = sc.get("width"), sc.get("depth")
            if drawn and abs(float(drawn) - float(w)) > 15.0:
                why = f"its width {w:.0f} is not the beam's drawn width {float(drawn):.0f}"
            else:
                ev = sc.props["width"].evidence[0]
                cands[hit.id]["width"].append(Candidate(w, "plan caption", [ev]))
                cands[hit.id]["depth"].append(Candidate(d, "plan caption", [ev]))
                sc.extra["attached_to"] = hit.id
                stats["sizes written on beams (no mark) attached"] += 1
                continue
        stats["sizes written on beams (no mark) not attached"] += 1
        b.issues.append(Issue(INFO, "caption", f"size '{sc.extra.get('raw', '')}' written on the plan not given to a "
                              f"beam: {why}", sheet=sc.sheet, elements=[sc.id], at=list(p),
                              extra={"from_linker": True}))


def _line_marks(b: Building, from_captions) -> dict[str, str]:
    """Beam spans with no mark lying between two spans of one mark on a straight line.

    Some consultants write a continuous beam's mark at each end of it only (NIFT: 4B7 at
    both ends of a line of three spans over two columns); the spans between carry none.
    An unmarked span whose line - neighbours in line, end to end, same width, over a
    column too - reaches the SAME mark first in both directions takes it. A mark on one
    side only, two different marks, or a joint where the line forks gives nothing.
    {beam id: mark}"""
    from shapely.geometry import Point
    out = {}
    for sid in b.sheets:
        beams, sup = _supports_and_beams(b, sid)
        if len(beams) < 3:
            continue
        sup30 = sup.buffer(30.0) if sup is not None else None

        def mark_of(e):
            # "" unmarked; None marked with something that is not one beam mark (a stop)
            ms = {m for m, _ in from_captions.get(e.id, [])} | ({_key_of(e.mark)} if e.mark else set())
            if not ms:
                return ""
            if len(ms) > 1:
                return None
            m = next(iter(ms))
            mk = parse_mark(m)
            return m if mk is None or mk.family() in ("beam", "unknown") else None

        marks = [mark_of(e) for e, *_r in beams]
        if not any(marks) or all(marks):
            continue

        def unit(k):
            _e, a, c, _w = beams[k]
            L = math.dist(a, c) or 1.0
            return ((c[0] - a[0]) / L, (c[1] - a[1]) / L)

        joins = defaultdict(list)          # (beam, end 0/1) -> [(beam, its end there)]
        for i in range(len(beams)):
            ui = unit(i)
            for j in range(i + 1, len(beams)):
                if abs(beams[i][3] - beams[j][3]) > 10.0:
                    continue
                uj = unit(j)
                if abs(ui[0] * uj[1] - ui[1] * uj[0]) > 0.035:
                    continue                                   # not in line
                for si in (0, 1):
                    p = beams[i][1 + si]
                    for sj in (0, 1):
                        q = beams[j][1 + sj]
                        d = math.dist(p, q)
                        off = abs((q[0] - beams[i][1][0]) * -ui[1] + (q[1] - beams[i][1][1]) * ui[0])
                        over_column = sup30 is not None and d <= 1500.0 and \
                            sup30.contains(Point(p)) and sup30.contains(Point(q))
                        if off > 30.0 or not (d <= 30.0 or over_column):
                            continue
                        # end to end, not overlapping: j's far end lies beyond i's end p
                        far, near_i = beams[j][2 - sj], beams[i][2 - si]
                        if math.dist(far, near_i) <= math.dist(p, near_i):
                            continue
                        joins[(i, si)].append((j, sj))
                        joins[(j, sj)].append((i, si))

        def first_mark(i, side):
            seen = {i}
            while True:
                nxt = joins.get((i, side), [])
                if len(nxt) != 1:
                    return ""                                  # line ends, or forks
                j, sj = nxt[0]
                if j in seen:
                    return ""
                seen.add(j)
                if marks[j] is None or marks[j]:
                    return marks[j] or ""
                i, side = j, 1 - sj

        for i, (e, *_r) in enumerate(beams):
            if marks[i] != "" or e.mark:
                continue
            m0, m1 = first_mark(i, 0), first_mark(i, 1)
            if m0 and m0 == m1:
                out[e.id] = m0
    return out


def _working_values(b: Building, e, mark: str, stats) -> None:
    """A slab bay with no mark written in it: what the plan reader proposed (the nearest
    mark, its thickness) is kept as a working value - status missing, so it is built
    marked and a person confirms it - unless a real source (a caption inside the bay, the
    plan's general note) gives it."""
    if "mark" not in e.resolved:
        e.resolved["mark"] = Value(mark, MISSING, 0.0, [Evidence(
            reader=READER, sheet=e.sheet, source=f"nearest slab mark, none is written in this bay: {mark}")])
        stats["slab marks not written (nearest kept as a working value)"] += 1
        b.issues.append(Issue(WARNING, "missing", f"slab bay with no mark: none is written in it; the nearest "
                              f"mark {mark} is used as a working value - confirm or set the mark", sheet=e.sheet,
                              elements=[e.id], mark=mark, extra={"from_linker": True}))
    t = e.get("thickness")
    if "thickness" not in e.resolved and t:
        e.resolved["thickness"] = Value(t, MISSING, 0.0, [Evidence(
            reader=READER, sheet=e.sheet, source=f"thickness of the nearest slab mark {mark}: {t}")])


def _layer_name_check(b: Building, e, cs, hints) -> None:
    """A depth only the layer's name gives, on a layer holding blocks too: shown for
    review, the value is not changed (New block B32b on BEAM-230X1325, the layer the
    stair block is on). A layer name that disagrees with another source is already a
    conflict (_resolve)."""
    v, lst = e.resolved.get("depth"), cs.get("depth", [])
    if v is None or v.status != READ or v.value is None or not lst or \
            any(c.kind != "layer name" for c in lst):
        return
    n = max((h.extra.get("blocks", 0) for h in hints), default=0)
    if n:
        mark = e.final("mark") or e.mark or e.id.rsplit("/", 1)[-1]
        b.issues.append(Issue(WARNING, "confirm", f"beam {mark}: depth {v.value:.0f} comes only from the "
                              f"name of layer '{e.extra.get('_layer') or ''}', and that layer also holds {n} "
                              f"block(s) drawn on it (not beams) - confirm the depth", sheet=e.sheet,
                              elements=[e.id], mark=str(mark), extra={"from_linker": True}))


STRUCTURE_WORDS = r"PROJECTION|CHH?AJJA|CANOPY|CANTILEVER|LOFT"


def _unmodelled_layers(b: Building) -> None:
    """A layer the plan reader skipped whose name says it is structure (New block:
    4 RCC projections on 'Projection'): nothing is built there, so ask, don't guess."""
    import re
    for i in list(b.issues):
        if i.category != "layers" or i.extra.get("from_linker"):
            continue
        m = re.match(r"Layer '(.+)' carries (\d+) entities but is not in the layer map", i.message)
        if not m or not re.search(STRUCTURE_WORDS, m.group(1), re.I):
            continue
        b.issues.append(Issue(WARNING, "not modelled", f"layer '{m.group(1)}' draws {m.group(2)} shapes and its "
                              f"name says it is structure, but nothing on it is modelled - no slab or beam is "
                              f"built there. Decide whether to model them; their sizes are not set", sheet=i.sheet,
                              extra={"from_linker": True}))


def _written_elsewhere(b: Building, stats) -> None:
    """A size still missing for a mark the drawings write elsewhere - a schedule row,
    a list, a note (readers/labels.mentions) - is most likely a reading fault, not a
    gap in the drawings: say so, and where, instead of only asking for the value."""
    where = defaultdict(list)
    for sid, s in b.sheets.items():
        for key, lst in (s.extra.get("mentions") or {}).items():
            where[(s.extra.get("set", ""), key)] += lst
    if not where:
        return
    for e in b.elements:
        need = REQUIRED.get(e.type)
        if not need:
            continue
        e.extra.pop("written_elsewhere", None)
        gone = [p for p in need if e.resolved.get(p) is not None and e.resolved[p].status == MISSING]
        mark = e.final("mark") or e.mark
        if not gone or not mark:
            continue
        seen = where.get((_set_of(b, e), _key_of(mark)))
        if not seen:
            continue
        e.extra["written_elsewhere"] = seen[0]["text"]
        stats["missing values whose mark the drawings write elsewhere"] += 1
        b.issues.append(Issue(WARNING, "reading", f"{e.type} {mark}: no {' or '.join(gone)} was read, yet the "
                              f"drawings write {mark} in '{seen[0]['text']}' - probably a reading fault: check "
                              f"that row and report it", sheet=seen[0]["sheet"], elements=[e.id], mark=mark,
                              at=seen[0]["at"], extra={"from_linker": True}))


def _wall_section_by_size(b: Building, e, lv: str, my_set: str):
    """The one unmarked wall section of a schedule (same drawing set, a level range
    holding this plan) whose size is this wall's outline: thickness x length for a
    straight wall, the outside size for an L / U. None if no section, or several
    different ones, fit - never a guess."""
    outline = e.geometry.get("outline") or []
    if len(outline) < 3:
        return None
    xs, ys = [p[0] for p in outline], [p[1] for p in outline]
    lo, hi = sorted((max(xs) - min(xs), max(ys) - min(ys)))
    fits = {}
    for s in b.of("spec"):
        if not s.extra.get("unmarked") or s.get("family") != "wall":
            continue
        if my_set and _set_of(b, s) and _set_of(b, s) != my_set:
            continue
        if not s.extra.get("level_range") or not in_range(lv, s.extra["level_range"]):
            continue
        a, c = sorted((s.get("width") or 0, s.get("length") or 0))
        if abs(lo - a) <= 25 and abs(hi - c) <= max(25.0, 0.03 * c):
            fits.setdefault((a, c, s.get("thickness")), s)
    return next(iter(fits.values())) if len(fits) == 1 else None


def _stairs_and_ramps(b: Building, cands) -> None:
    """A stair's going, width and treads as measured on its plan; its riser and
    tread from the stair notes of the same drawing set (Villa 5: "RISER = 0.160M
    THREAD = 0.30M"), which confirm - or contradict - the going measured."""
    notes = defaultdict(list)
    for s in b.of("spec"):
        if s.get("family") == "stair":
            notes[_set_of(b, s)].append(s)
    for e in b.elements:
        if e.type == "stair":
            for prop in ("going", "width", "flights"):
                v = e.props.get(prop)
                if v is not None:
                    cands[e.id][prop].append(Candidate(v.value, "plan geometry", v.evidence[:1]))
            for s in notes.get(_set_of(b, e), []):
                if s.get("tread"):
                    cands[e.id]["going"].append(Candidate(s.get("tread"), "detail caption",
                                                          s.props["tread"].evidence[:1], note="stair notes"))
                if s.get("riser"):
                    cands[e.id]["riser"].append(Candidate(s.get("riser"), "detail caption",
                                                          s.props["riser"].evidence[:1], note="stair notes"))
                if s.get("landing_levels") and "landing_levels" not in e.resolved:
                    e.resolved["landing_levels"] = Value(s.get("landing_levels"), READ, 0.7,
                                                         s.props["landing_levels"].evidence[:3])
            for prop in ("treads", "direction_text"):
                if e.props.get(prop) is not None:
                    e.resolved[prop] = e.props[prop]
        elif e.type == "ramp":
            for prop in ("width", "length", "slope"):
                v = e.props.get(prop)
                if v is not None:
                    cands[e.id][prop].append(Candidate(v.value, "plan geometry", v.evidence[:1]))


def _same(a, b, tol) -> bool:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(a - b) <= tol
    return a == b


def _resolve(prop: str, lst: list[Candidate], etype: str) -> Value:
    """Group agreeing values; one group is a reading, several are a conflict."""
    tol = 15.0 if any(c.kind == "plan geometry" for c in lst) else 5.0
    groups: list[list[Candidate]] = []
    for c in sorted(lst, key=lambda c: -c.weight):
        for g in groups:
            if _same(g[0].value, c.value, tol):
                g.append(c)
                break
        else:
            groups.append([c])
    best = max(groups, key=lambda g: sum(c.weight for c in g))
    lead = max(best, key=lambda c: c.weight)
    value = lead.value
    if isinstance(value, float) and lead.kind == "plan geometry":
        value = round(value / 5.0) * 5.0          # a measured 229.6 is a 230 member
    evidence = [Evidence(reader=READER, sheet=ev.sheet, source=f"{c.kind}: {c.value}", layer=ev.layer,
                         handle=ev.handle, at=ev.at, text=ev.text)
                for g in groups for c in g for ev in (c.evidence[:1] or [Evidence(reader=READER)])]
    if len(groups) == 1:
        conf = min(1.0, lead.weight + 0.1 * (len({c.kind for c in best}) - 1))
        return Value(value, READ, round(conf, 2), evidence)
    # Any source that disagrees - a layer name or a measured size too - is a conflict
    # a person sees (external review 2026-10-10, point E: NB3 on a 230X900 layer but
    # captioned 750 showed as plain "read"). The value kept is still the strongest
    # sources' one, so what is built does not change; it is marked until decided.
    return Value(value, CONFLICT, round(lead.weight * 0.5, 2), evidence)


def _column_sides(cs: dict[str, list[Candidate]]) -> None:
    """A column's size compared as narrow side and long side, whichever way it was
    written: C(500X230) and a measured 230 x 500 outline agree."""
    w, d = cs.get("width", []), cs.get("depth", [])
    if not w or not d:
        return
    pairs = defaultdict(dict)
    def key(c):
        return c.pair or (id(c.evidence[0]) if c.evidence else id(c))
    for c in w:
        pairs[key(c)]["w"] = c
    for c in d:
        pairs[key(c)]["d"] = c
    for p in pairs.values():
        if "w" in p and "d" in p and isinstance(p["w"].value, (int, float)) and \
                isinstance(p["d"].value, (int, float)) and p["w"].value > p["d"].value:
            p["w"].value, p["d"].value = p["d"].value, p["w"].value


def apply_overrides(b: Building, overrides: dict) -> int:
    """Review decisions. Two ways to say one:

        "elements": {"<element id>": {"depth": 600}}           one element
        "marks": {"B63@B/L1": {"depth": 600}, "GB4": {...}}    every element with
                                                               that mark (and wing/level)
        "exclude": ["<element id>", ...]                        not to be modelled
        "sheets": {"<sheet id>": {"kind": ..., "level": ...}}  applied by readset,
                                                               before reading
        "project": {                                           decisions for the whole job
            "founding_level_mm": -1500,                        every footing and raft the
                                                               drawings give none (a set's
                                                               own: {"<set>": -1500, ...})
            "levels": {"Level 1": 3150, ...},                  Revit level elevations, used
                                                               by the export and the build
            "units": {"<file name>": "m"}}                     applied by readset
    Project decisions go first, so one made for a single element always wins.
    """
    n = 0
    ev = Evidence(reader="review", source="review decision")
    fl = (overrides.get("project") or {}).get("founding_level_mm")
    if fl not in (None, ""):
        pev = Evidence(reader="review", source="project decision: founding level for every footing "
                                               "the drawings give none")
        for e in b.elements:
            if e.type not in ("footing", "raft"):
                continue
            v = fl.get(_set_of(b, e)) if isinstance(fl, dict) else fl
            have = e.resolved.get(FOUNDING)
            if v in (None, "") or (have is not None and have.status != MISSING):
                continue                # read, decided, or in conflict: that needs its own decision
            e.resolved[FOUNDING] = Value(float(v), OVERRIDE, 1.0, [pev] + (have.evidence if have else []))
            n += 1
    # no founding level given, but a height for the footings' own level ("Foundation" -2000,
    # typed with the floor heights): that height is their bottom
    lv = (overrides.get("project") or {}).get("levels") or {}
    if fl in (None, "") and lv:
        lev = Evidence(reader="review", source="project decision: the height given to the foundation level, "
                                               "used as the bottom of every footing the drawings give none")
        for e in b.elements:
            if e.type not in ("footing", "raft"):
                continue
            have = e.resolved.get(FOUNDING)
            if have is not None and have.status != MISSING:
                continue
            s = b.sheets.get(e.sheet)
            names = [s.extra.get("revit_level")] if s else []
            if s is None or (s.level or e.level or "").upper() == "FDN":
                names += ["Foundation", "FDN", "Footing", "Footings", "Foundation Level"]   # the Floors step's names
            v = next((lv[x] for x in names if x and lv.get(x) not in (None, "")), None)
            if v is None:
                continue
            e.resolved[FOUNDING] = Value(float(v), OVERRIDE, 1.0, [lev] + (have.evidence if have else []))
            n += 1
    for eid, props in (overrides.get("elements") or {}).items():
        for e in b.elements:
            if e.id == eid:
                for k, v in props.items():
                    e.resolved[k] = Value(v, OVERRIDE, 1.0, [ev] + (e.resolved[k].evidence if k in e.resolved else []))
                    n += 1
    for spec, props in (overrides.get("marks") or {}).items():
        mark, _, scope = spec.partition("@")
        wing, _, level = scope.partition("/")
        for e in b.elements:
            if e.type not in FAMILY_OF_TYPE or _key_of(str(e.final("mark") or e.mark)) != _key_of(mark):
                continue
            if wing and e.wing != wing:
                continue
            if level and _level_key(b, e) != level:
                continue
            for k, v in props.items():
                e.resolved[k] = Value(v, OVERRIDE, 1.0, [ev] + (e.resolved[k].evidence if k in e.resolved else []))
                n += 1
    for eid in overrides.get("exclude") or []:
        for e in b.elements:
            if e.id == eid:
                e.resolved["excluded"] = Value(True, OVERRIDE, 1.0, [ev])
                n += 1
    return n
