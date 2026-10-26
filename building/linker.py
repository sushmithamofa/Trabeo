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
from .model import (Building, Evidence, Issue, Value, CONFLICT, MISSING, OVERRIDE, READ,
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
    _section_heights(b, stats)        # building sections: heights off the level, slopes
    _legend_zones(b, stats)           # what the legend marks: steel roofs, columns through / ending
    levels_table.build(b, stats)
    if overrides:
        stats["overrides applied"] = apply_overrides(b, overrides)
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
        for k in ("section_profile", "section_profile_mirrored", "section_part"):
            e.extra.pop(k, None)
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
            used_sec[c.mark] = sec
            hit = [e for e in beams if "section" not in e.resolved
                   and _crosses(c.geometry["a"], c.geometry["b"], e.geometry["start"], e.geometry["end"])]
            for grp in _parallel_groups(hit):
                done = _give_section(grp, sec, c, slabs)
                used[c.mark] += done
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
