"""Level markers and floor heights, from sections, elevations and notes (stage 7).

How consultants write a level, and what is read:

    FIRST FLOOR LVL +3.150      TERRACE LVL. (+) 12.600     + 600 LVL
    FFL +3.150 / SFL +3.100     MIDLANDING UFL. LVL. (+)1440
    a name with its value beside or under it: "2ND FLOOR SLAB LVL"  "+6.300"

A value is read only when it is written as a level: a signed decimal (+3.150),
a bracketed sign ((+) 1250), or a signed number followed by LVL. Marks like
B-39, bar callouts, footing depths (750+300) and dates are never levels - the
old loose pattern took "B-39" for -39 m.

Each marker becomes a level_mark element with its elevation in mm, the level it
names (GF, L1 ... TERRACE, from the same vocabulary as titles), and its kind:

    floor       a floor or slab level - goes into the level table
    landing     a stair landing
    beam        a beam top / bottom (BT@ +0.200M)
    foundation  footing, PCC, raft bottom
    local       a level with no level named (a detail's +000 / +600)

and whether it is a finished (FFL) or structural (SFL / TOS / slab) level, when
the text says so.

Notes giving a floor height ("HEIGHT REVISED TO 3150MM", "FLOOR TO FLOOR HT
3.15M") are kept as a height note on the level of the sheet they are on - never
turned into elevations: a note does not say which floor it starts from.
"""
from __future__ import annotations

import re

from ..marks import BAR
from ..model import Building, Element, Evidence, Value, READ
from ..sheets.levels import level_rank, levels_in

READER = "elevations"
ELEV = re.compile(
    r"\(\s*(?P<ps>[+\-±])\s*\)\s*(?P<pv>\d+(?:\.\d+)?)\s*(?P<pu>MM|M)?(?![\d])"          # (+) 1250, (-)1910
    r"|(?<![\w.])(?P<ds>[+\-±])\s*(?P<dv>\d{1,3}\.\d{1,3})\s*(?P<du>MM|M)?(?![\d])"       # +3.150, + 0.200M
    r"|(?<![\w.])(?P<is>[+\-±])\s*(?P<iv>\d{2,5})\s*(?:MM)?\s*(?=(?:LVL|LEVEL)\b)"         # + 600 LVL
    r"|\b(?:LVL|LEVEL)\.?\s*(?P<ls>[+\-±])\s*(?P<lv>\d{2,5})(?![\d.])"                      # LVL. +1440
    r"|\b(?:LVL|LEVEL)\.?\s+(?:[A-Z]+\s+){1,3}(?P<ws>[+\-±])\s*(?P<wv>\d{3,5})(?![\d.])",  # LVL B BLOCK +9500
    re.I)
HEIGHT = re.compile(r"(?:FLOOR\s*(?:TO\s*FLOOR\s*)?(?:HEIGHT|HT)\.?|STOREY\s*(?:HEIGHT|HT)\.?|"
                    r"HEIGHT\s*(?:REVISED\s*)?(?:TO|=|:|-))\s*[=:-]?\s*(\d+(?:\.\d+)?)\s*(MM|M)?\b", re.I)
KIND = (("landing", re.compile(r"LANDING", re.I)),
        ("beam", re.compile(r"\bBEAM|\bBT\s*@|\bB\.?T\.?\b|BOTTOM\s+OF\s+BEAM|TOP\s+OF\s+BEAM", re.I)),
        ("foundation", re.compile(r"FOOTING|\bFDN\b|\bPCC\b|FOUNDATION|RAFT", re.I)))
FINISHED = re.compile(r"\bFFL\b|FINISH|\bF\.F\.L|\bUFL\b", re.I)
STRUCTURAL = re.compile(r"\bSFL\b|\bTOS\b|T\.O\.S|STRUCTURAL|\bSLAB\b", re.I)
NAMEWORD = re.compile(r"\b(?:LVL|LEVEL|FLOOR|SLAB|FFL|SFL|TOS|ROOF|TERRACE|PLINTH|GROUND|LANDING)\b", re.I)


def values_in(text: str) -> list[float]:
    """Levels in mm written in a text, read strictly (see the module notes)."""
    out = []
    for m in ELEV.finditer(text or ""):
        if m.group("pv") is not None:
            sign, num, unit = m.group("ps"), m.group("pv"), m.group("pu")
            v = float(num) * (1000.0 if (unit or "").upper() == "M" or ("." in num and float(num) < 50) else 1.0)
        elif m.group("dv") is not None:
            sign, num, unit = m.group("ds"), m.group("dv"), m.group("du")
            v = float(num) * (1.0 if (unit or "").upper() == "MM" else 1000.0)
        elif m.group("iv") is not None:
            sign, v = m.group("is"), float(m.group("iv"))
        elif m.group("wv") is not None:
            sign, v = m.group("ws"), float(m.group("wv"))
        else:
            sign, v = m.group("ls"), float(m.group("lv"))
        out.append(round(-v if sign == "-" else v))
    return out


def _kind(text: str, keys: list[str]) -> str:
    for name, rx in KIND:
        if rx.search(text):
            return name
    return "floor" if keys else "local"


def read(b: Building, rec) -> dict[str, int]:
    if rec.view.source in ("stray", "overview") or rec.view.same_as:
        return {}
    texts = [t for t in rec.texts if t.space == "model"]
    made = {"level marks": 0, "height notes": 0}
    used = set()
    for t in texts:
        if BAR.search(t.text) and not NAMEWORD.search(t.text):
            continue                                    # a bar callout, not a level
        vals = values_in(t.text)
        if not vals:
            continue
        name_t, keys = t, [k for k in levels_in(ELEV.sub(" ", t.text)) if level_rank(k) is not None]
        if not keys and not NAMEWORD.search(t.text) and not any(rx.search(t.text) for _, rx in KIND):
            # a bare value: the level it belongs to is the name written beside or above it
            h = max(t.height, 1.0)

            def gap(u):
                # from the end of the name to the value (a long name ends far from where it starts)
                end = u.x + 0.8 * max(u.height, 1.0) * len(u.text)
                return min(abs(t.x - end), abs(t.x - u.x))
            near = [u for u in texts if u is not t and not values_in(u.text) and NAMEWORD.search(u.text)
                    and ((abs(u.y - t.y) <= 1.2 * h and gap(u) <= 20 * h) or
                         (0 < u.y - t.y <= 2.5 * h and abs(u.x - t.x) <= 8 * h))]
            if near:
                name_t = min(near, key=lambda u: abs(u.y - t.y) * 3 + gap(u))
                keys = [k for k in levels_in(name_t.text) if level_rank(k) is not None]
            elif not re.search(r"LVL|LEVEL", t.text, re.I):
                continue                                # a lone +3.150 with nothing naming it
        words = name_t.text + " " + t.text
        kind = _kind(words, keys)
        basis = "finished" if FINISHED.search(words) else "structural" if STRUCTURAL.search(words) else ""
        ev = Evidence(reader=READER, sheet=rec.id, source="level marker", layer=t.layer, handle=t.handle,
                      at=[t.x, t.y], text=(name_t.text + " | " + t.text if name_t is not t else t.text)[:90])
        for v in vals[:1]:
            key = (round(t.x), round(t.y), v)
            if key in used:
                continue
            used.add(key)
            props = {"elevation": Value(float(v), READ, 0.85, [ev]), "kind": Value(kind, READ, 0.8, [ev])}
            if keys:
                props["level"] = Value(keys[-1] if kind == "floor" else keys[0], READ, 0.8, [ev])
            if basis:
                props["basis"] = Value(basis, READ, 0.8, [ev])
            b.add(Element(id=f"{rec.id}/level_mark/{len(b.elements)}", type="level_mark", sheet=rec.id,
                          level=keys[0] if keys else "", wing=rec.wing,
                          geometry={"x": t.x, "y": t.y}, props=props,
                          extra={"view_kind": rec.verdict.kind, "name_text": name_t.text[:80]}))
            made["level marks"] += 1
    for t in texts:
        m = HEIGHT.search(t.text)
        if not m:
            continue
        v = float(m.group(1)) * (1000.0 if (m.group(2) or "").upper() == "M" or float(m.group(1)) < 20 else 1.0)
        if not 2000 <= v <= 8000:
            continue
        ev = Evidence(reader=READER, sheet=rec.id, source="floor height note", layer=t.layer, handle=t.handle,
                      at=[t.x, t.y], text=t.text[:90])
        keys = [k for k in rec.levels if level_rank(k) is not None]
        b.add(Element(id=f"{rec.id}/spec/{len(b.elements)}", type="spec", sheet=rec.id, wing=rec.wing, mark="",
                      level=keys[0] if len(keys) == 1 else "",
                      props={"family": Value("level", READ, 1.0, [ev]), "storey_height": Value(round(v), READ, 0.7, [ev])},
                      extra={"from": "floor height note", "part": ""}))
        made["height notes"] += 1
    return {k: v for k, v in made.items() if v}
