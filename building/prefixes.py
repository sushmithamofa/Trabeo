"""What a consultant's mark prefixes mean, read from their own drawings.

Consultants write marks their own way: SL1 or P1 for a slab, SB for a slab band
to one office and a secondary beam to another. The built-in list (marks.FAMILY)
covers the common prefixes; this module weighs what the drawing set itself says
about each prefix, strongest first:

    schedule   5   a mark listed in a table titled SLAB SCHEDULE (or on a sheet the
                   registry called a slab schedule / slab details) is a slab mark
    position   3   on the plan the marks sit inside slab bays, inside column outlines,
                   along beams (the linker adds this once the plan is read)
    layer      2   the marks are written on a layer whose name says SLAB / BEAM / COL
    hint       2   what was confirmed for an earlier project - rechecked every time:
                   the drawing wins where it says otherwise
    built-in   1   marks.FAMILY

Each source gives its weight split by the share of that prefix's marks pointing
each way. A decision made in Review for the project is final. A close call is
never decided here: the prefix keeps its built-in meaning and goes to Review. A
prefix the built-in list knows is only re-read when the schedule or the plan
itself says otherwise (a layer name or a hint alone is not enough).
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict

from .marks import FAMILY, family_of

WEIGHTS = {"schedule": 5.0, "position": 3.0, "layer": 2.0, "hint": 2.0, "built-in": 1.0}
STRONG = ("schedule", "position")      # what may re-read a prefix the built-in list knows
CLOSE = 1.0                            # a lead smaller than this is a close call
LEARN_SHARE = 0.75                     # an unknown prefix is learned only when this share of its marks agree
FAMILIES = ("beam", "column", "slab", "wall", "footing", "stair")

# what a schedule's title says it lists; a title naming two ("BEAM & SLAB DETAILS") says nothing
_TITLE = [("footing", re.compile(r"FOOTING|FOUNDATION|\bFTG\b|\bRAFT\b", re.I)),
          ("column", re.compile(r"COLUMN|\bCOL\b", re.I)),
          ("slab", re.compile(r"SLAB", re.I)),
          ("wall", re.compile(r"\bWALL|SHEAR|\bCORE\b", re.I)),
          ("stair", re.compile(r"STAIR", re.I)),
          ("beam", re.compile(r"BEAM|GIRDER|LINTEL", re.I))]
_SHEET_KIND = {"beam_schedule": "beam", "beam_details": "beam", "column_schedule": "column",
               "column_details": "column", "slab_schedule": "slab", "slab_details": "slab",
               "footing_schedule": "footing", "footing_details": "footing", "stair_details": "stair"}
# a layer's name: one family named in it, and not a schedule / legend / detail text layer
_LAYER = [("column", re.compile(r"COL|CLM", re.I)), ("slab", re.compile(r"SLAB", re.I)),
          ("wall", re.compile(r"WALL|PARDI|SHEAR", re.I)), ("stair", re.compile(r"STAIR", re.I)),
          ("beam", re.compile(r"BEAM|\bBM\b|GIRDER", re.I))]
_NOT_TAG_LAYER = re.compile(r"SCH|LEGEND|DETAIL|NOTE|TITLE|DIM|HATCH", re.I)
_PREFIX = re.compile(r"^([A-Z]{1,4})")


def prefix_of(mark: str) -> str:
    m = _PREFIX.match((mark or "").upper())
    return m.group(1) if m else ""


def title_family(title: str) -> str:
    hits = {fam for fam, rx in _TITLE if rx.search(title or "")}
    return hits.pop() if len(hits) == 1 else ""


def layer_family(layer: str) -> str:
    if not layer or _NOT_TAG_LAYER.search(layer):
        return ""
    hits = {fam for fam, rx in _LAYER if rx.search(layer)}
    return hits.pop() if len(hits) == 1 else ""


def _set_of(b, el) -> str:
    s = b.sheets.get(el.sheet)
    return s.extra.get("set", "") if s else ""


def drawing_evidence(b, set_key: str | None = None) -> dict[str, dict[str, dict[str, Counter]]]:
    """{set: {prefix: {source: Counter(family -> marks)}}} from the schedules and
    the plan captions read so far (the plan position is added by the linker)."""
    out: dict = defaultdict(lambda: defaultdict(lambda: defaultdict(Counter)))
    for s in b.of("spec"):
        if s.props.get("by_column") is not None or (set_key is not None and _set_of(b, s) != set_key):
            continue                         # a footing listed by the column it carries names a column
        p = prefix_of(s.mark)
        if not p:
            continue
        fam = title_family(str(s.extra.get("table") or ""))
        if not fam:
            sh = b.sheets.get(s.sheet)
            fam = _SHEET_KIND.get(sh.kind, "") if sh else ""
        if fam:
            out[_set_of(b, s)][p]["schedule"][fam] += 1
    for c in b.of("caption"):
        if set_key is not None and _set_of(b, c) != set_key:
            continue
        p = prefix_of(c.mark)
        if not p:
            continue
        ev = c.props.get("family").evidence if c.props.get("family") else []
        fam = layer_family(ev[0].layer if ev else "")
        if fam:
            out[_set_of(b, c)][p]["layer"][fam] += 1
    return out


def decide(prefix: str, ev: dict[str, Counter], hint: str = "", decided: str = "") -> dict:
    """One prefix's meaning from its weighted evidence.

    status: decided (in Review) / built-in (nothing else said anything) / agreed /
    learned (a prefix the built-in list does not know) / drawing (the drawing
    re-reads a known prefix) / close (a close call: built-in kept, to Review)."""
    builtin = family_of(prefix)
    ev = {k: Counter(v) for k, v in (ev or {}).items() if v and sum(v.values())}
    if hint:
        ev["hint"] = Counter({hint: 1})
    score: Counter = Counter()
    for src, cnt in ev.items():
        tot = sum(cnt.values())
        for fam, n in cnt.items():
            score[fam] += WEIGHTS[src] * n / tot
    if builtin != "unknown":
        score[builtin] += WEIGHTS["built-in"]
    out = {"prefix": prefix, "builtin": builtin, "score": {k: round(v, 2) for k, v in score.items()},
           "evidence": {k: dict(v) for k, v in ev.items()}}
    if decided:
        return {**out, "family": decided, "status": "decided"}
    if not score:
        return {**out, "family": "unknown", "status": "built-in"}
    ranked = score.most_common()
    f1, s1 = ranked[0]
    s2 = ranked[1][1] if len(ranked) > 1 else 0.0
    strong_for = {fam for src in STRONG for fam, n in ev.get(src, {}).items()
                  if n / sum(ev[src].values()) >= 0.5}
    drawing_says = {fam for src in ("schedule", "position", "layer") for fam in ev.get(src, {})}
    # the schedule or the plan itself split (S1 in a slab table, S2 in a beam table)
    split = any(len(ev.get(src, {})) > 1 and sorted(ev[src].values())[-2] / sum(ev[src].values()) >= 1 / 3
                for src in STRONG)
    if f1 == hint and hint != builtin and drawing_says <= {hint}:
        # an earlier decision, and nothing in this drawing says otherwise
        return {**out, "family": f1, "status": "hint"}
    if prefix in KNOWN:
        # a usual prefix (B, S, C, LW, NC ...) keeps its meaning: never re-read by itself.
        # Measured on 36 plans: J1153 lists LW in its column schedule, Sasan's 'SC1' steel
        # posts met J1153's SC slabs - a re-read there broke what worked. Only where the
        # schedule or the plan clearly says otherwise, Review is asked.
        if f1 != builtin and f1 in strong_for and not split and s1 - s2 >= CLOSE and \
                {f1, builtin} != {"column", "wall"}:
            return {**out, "family": builtin, "status": "check", "suggested": f1}
        said = [s for s in ev if s != "hint"]
        return {**out, "family": builtin, "status": "agreed" if said else "built-in"}
    # a prefix the built-in list does not have (SL, P, MLBC), or only guesses (SLAB ends in B)
    if s1 - s2 < CLOSE or split:
        return {**out, "family": builtin, "status": "close"}
    # learned only where its marks agree: Pirgal's IP (insert plates, per its own legend) stood
    # 33 in slab bays, 14 on beams, 3 at columns - two thirds is no meaning, and taken as slab
    # marks they fought every slab's own mark
    mixed = any(sum(ev[src].values()) and ev[src].get(f1, 0) / sum(ev[src].values()) < LEARN_SHARE
                for src in STRONG if ev.get(src))
    if builtin == "unknown" and mixed:
        # not an element mark until a person says so: it names no beam, column or slab
        # (left unknown, its marks along beams were taken as beam names and split them)
        return {**out, "family": "other", "status": "close"}
    if f1 == builtin:
        said = [s for s in ev if s != "hint"]
        return {**out, "family": f1, "status": "agreed" if said else "built-in"}
    return {**out, "family": f1, "status": "learned"}


def decide_all(evidence: dict[str, dict[str, Counter]], hints: dict | None = None,
               decisions: dict | None = None) -> dict[str, dict]:
    hints = {k.upper(): v for k, v in (hints or {}).items()}
    decisions = {k.upper(): v for k, v in (decisions or {}).items() if v}
    prefixes = set(evidence) | set(decisions)
    return {p: decide(p, evidence.get(p, {}), hints.get(p, ""), decisions.get(p, "")) for p in sorted(prefixes)}


def families_map(decided: dict[str, dict]) -> dict[str, str]:
    """{prefix: family} for the readers - only where it differs from the built-in list."""
    return {p: d["family"] for p, d in decided.items()
            if d["family"] not in ("", "unknown") and d["family"] != d["builtin"]}


def describe(d: dict) -> str:
    """The evidence in words, for Review."""
    words = {"schedule": "in a {} schedule / details", "position": "on the plan, at {}",
             "layer": "on a {} mark layer", "hint": "your earlier decision: {}"}
    where = {"beam": "beams", "column": "columns", "slab": "slab bays", "wall": "walls",
             "footing": "footings", "stair": "stairs"}
    parts = []
    for src in ("schedule", "position", "layer", "hint"):
        cnt = d["evidence"].get(src)
        if not cnt:
            continue
        for fam, n in sorted(cnt.items(), key=lambda x: -x[1]):
            what = words[src].format(where.get(fam, fam) if src == "position" else fam)
            parts.append(f"{n} {what}" if src != "hint" else what)
    if d["builtin"] != "unknown":
        parts.append(f"built-in list: {d['builtin']}")
    return "; ".join(parts)


# the usual prefixes, and N ("new") before one (NB, NS, NC on the Sasan Gir New block)
KNOWN = frozenset(p for ps in FAMILY.values() for p in ps) | frozenset("N" + p for ps in FAMILY.values() for p in ps)
