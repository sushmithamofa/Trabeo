"""General facts written as notes: grades, units, cover, bearing capacity, levels.

Every fact keeps the text it was read from. Where notes say different things
(M25 in one note, M30 in another), every value is kept - deciding which applies
to which element is the linker's job, and an unresolved one is a review item.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .levels import elevations_in

CONCRETE = re.compile(r"\bM\s?-?\s?(10|15|20|25|30|35|40|45|50|55|60)\b")
STEEL = re.compile(r"\b(?:FE|HYSD|TMT)\s?-?\s?(250|415|500|550|600)\s?(D)?\b")
UNITS = re.compile(r"ALL\s+(?:THE\s+)?DIMENSIONS?\s+(?:ARE\s+)?(?:IN\s+)?(MM|MILLIMET(?:RE|ER)S?|M\b|MET(?:RE|ER)S?|CM)")
COVER = re.compile(r"(?:CLEAR\s+)?COVER\b[^0-9]{0,40}?(\d{2})\s*MM")
SBC = re.compile(r"\bS\.?B\.?C\.?\b[^0-9]{0,30}(\d{2,3}(?:\.\d+)?)\s*(T/M2|T/SQ\.?M|KN/M2|TONNES?)")
LEVEL_LABEL = re.compile(r"\b(?:LVL|LEVEL|EL|FFL|SFL|TOS|TOC|BOF)\b")


@dataclass
class Fact:
    name: str            # concrete_grade, steel_grade, units, cover_mm, sbc, level_mm
    value: object
    text: str            # the text it was read from
    handle: str = ""
    space: str = ""
    at: tuple[float, float] | None = None


def read_notes(texts) -> list[Fact]:
    out: list[Fact] = []
    for t in texts:
        u = t.text.upper()
        where = dict(text=t.text[:200], handle=t.handle, space=t.space, at=(t.x, t.y))
        for m in CONCRETE.finditer(u):
            out.append(Fact("concrete_grade", f"M{m.group(1)}", **where))
        for m in STEEL.finditer(u):
            out.append(Fact("steel_grade", f"Fe{m.group(1)}{m.group(2) or ''}", **where))
        m = UNITS.search(u)
        if m:
            unit = m.group(1)
            name = "mm" if unit == "MM" or unit.startswith("MILLI") else "cm" if unit == "CM" else "m"
            out.append(Fact("units", name, **where))
        m = COVER.search(u)
        if m:
            out.append(Fact("cover_mm", int(m.group(1)), **where))
        m = SBC.search(u)
        if m:
            out.append(Fact("sbc", f"{m.group(1)} {m.group(2)}", **where))
        if LEVEL_LABEL.search(u):
            for mm in elevations_in(u):
                out.append(Fact("level_mm", mm, **where))
    return out


def summary(facts: list[Fact]) -> dict[str, list]:
    """Distinct values per fact, most frequent first."""
    from collections import Counter
    by: dict[str, Counter] = {}
    for f in facts:
        by.setdefault(f.name, Counter())[str(f.value)] += 1
    return {k: [v for v, _ in c.most_common()] for k, c in by.items()}
