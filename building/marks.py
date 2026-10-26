"""Element marks and size labels, as consultants actually write them.

Measured on the training sets, the same idea is written many ways:

    B12   B-12   GB 1   B4a   B55A   FC2A   B107/BP   Bk1   STB1   SB   C
    B4a(230X600)   GB 1(230x450)   B16 (230X600)   GB1:230x300   C(230X500)
    GB2 (230x450)/STB1 (230x600)          two labels in one text
    provided to support GB3 (230x450)    a label inside a sentence
    S2(200THK)                           a slab and its thickness
    B-52,54   S3,S5                      several marks sharing one label

and some text looks like a mark but is not one: bar callouts (T8 @ 100,
T16-100mm C/C), grades (M25:Fe415), job numbers (J-1153), footing sizes
(3300 X 3600), steel sections (172X92X5.4mm).

A mark's key ignores the way it was typed: B-12, B12 and b 12 are all "B12";
B4a and B4A are both "B4A". Its family (beam, column, footing, slab, wall...)
comes from the prefix by default - a consultant profile can override that,
because prefixes do not mean the same thing everywhere (GB is a girder beam to
one consultant and a ground beam to another; both are beams).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# Default prefix families. A consultant profile can add or override any of them.
FAMILY = {
    "beam": {"B", "GB", "PB", "LB", "TB", "RB", "SB", "EB", "HB", "MB", "CB", "BK", "BKT",
             "BC", "BP", "MLB", "STB", "FB", "TIB", "DB", "BM", "CTB"},
    "column": {"C", "FC", "SC", "COL", "CL"},
    "wall": {"SW", "LW", "RW", "W", "CW"},
    "footing": {"F", "IF", "CF", "PF", "RF", "FTG", "PC"},
    "pedestal": {"PD", "PED"},
    "slab": {"S", "TS", "RS", "CS", "WS"},
    "stair": {"ST", "STR", "FL", "LND"},
}
# Prefixes that are never marks: bar sizes (T8, Y12), and words drafters abbreviate.
# Kept short on purpose - D, M, R, Z are real marks for some consultants.
NOT_MARKS = {"T", "Y", "J", "MM", "THK", "NO", "NOS", "LVL", "EL", "RAFT", "STEP", "SEC", "DWG",
             "REV", "SH", "FIG", "TYP", "FE", "HYSD", "TOR", "UP", "DN", "TO", "OF", "AT",
             "REF", "MAX", "MIN", "TOP", "BOT", "AND", "FOR", "ALL", "THE"}
GRADE_MARK = re.compile(r"^M\s?-?\s?(?:1[05]|[2-6][05])$", re.I)    # M20, M25 ... a grade

BAR = re.compile(r"\b\d+\s*-\s*[TY#]\s?\d{1,2}\b|\b[TY#]\s?\d{1,2}\s*[-@]\s*\d{2,3}|"
                 r"\b\d+\s*#\s*\d{0,2}\b|Ø\s?\d{1,2}\b|\b\d{1,2}\s*[TY]\s*@\s*\d{2,3}|C\s*/\s*C", re.I)
GRADE = re.compile(r"\bM\s?\d{2}\b\s*[:/]\s*FE|\bFE\s?\d{3}", re.I)

# a floor number may come first: 4B3 is beam B3 of the 4th floor (NIFT: 2B5, 3B3A, 4B9A)
_MARK = r"(?:\d{1,2}(?=[A-Za-z]))?[A-Za-z]{1,4}\s?-?\s?\d{0,3}[A-Za-z]?"
_SIZE = r"(\d{2,4})\s*[xX×*]\s*(\d{2,4})"
# MARK followed by a size in brackets, after a colon, or just a space:
# B4a(230X600), GB 1(230x450), GB1:230x300, B16 (230X600); or a thickness S2(200THK)
LABEL = re.compile(
    r"(?<![A-Za-z0-9])(" + _MARK + r")\s*(?:\(\s*" + _SIZE + r"\s*\)|:\s*" + _SIZE +
    r"(?![\d.])|\(\s*(\d{2,3})\s*THK\s*\))", re.I)
# the whole text is a mark, a space and its size: B01 230x450, GB 2 230 X 600 MM
SPACED = re.compile(r"^\s*(" + _MARK + r")\s+" + _SIZE + r"\s*(?:MM)?\s*$", re.I)
SIZE_ONLY = re.compile(r"^\(?\s*" + _SIZE + r"\s*\)?(?:\s*(?:MM|DEEP|DP))?$", re.I)
MARK_ONLY = re.compile(r"^(" + _MARK + r")(?:\s*/\s*([A-Za-z]{1,3}\d*))?$")


@dataclass(frozen=True)
class Mark:
    raw: str
    prefix: str          # upper case
    number: str          # "" for marks with none (SB, C)
    suffix: str          # upper case; B4a -> "A"
    also: str = ""       # second tag on a compound mark: B107/BP -> "BP"
    floor: str = ""      # the floor number written first: 4B3 -> "4" (the prefix stays B)

    @property
    def key(self) -> str:
        return f"{self.floor}{self.prefix}{self.number}{self.suffix}"

    def family(self, overrides: dict[str, str] | None = None) -> str:
        return family_of(self.prefix, overrides)


def family_of(prefix: str, overrides: dict[str, str] | None = None) -> str:
    p = prefix.upper()
    if overrides and p in overrides:
        return overrides[p]
    for fam, prefixes in FAMILY.items():
        if p in prefixes:
            return fam
    if p.endswith("B") and len(p) <= 4:
        return "beam"                       # most consultants end beam prefixes in B
    if len(p) >= 2 and p[0] == "N":
        # N for "new" before an ordinary prefix: NB beside NS (slab) and NC (column) on
        # the Sasan Gir hotel's New block, whose slab marks NS1-NS5 were taken for beams
        for fam, prefixes in FAMILY.items():
            if p[1:] in prefixes:
                return fam
    return "unknown"


def parse_mark(text: str) -> Mark | None:
    """A whole text that is one mark, or None.

    A bare text must carry a number: on its own, "C" or "B" is far more likely a
    grid label, and "VOID" a word. Marks without a number (SB, C, Bn) are only
    taken with a size beside them - see labels_in.
    """
    s = (text or "").strip()
    if not s or BAR.search(s) or GRADE.search(s) or GRADE_MARK.match(s):
        return None
    m = MARK_ONLY.match(s)
    if not m:
        return None
    mk = _mark(m.group(1), m.group(2) or "")
    return mk if mk and mk.number else None


def bare_mark(text: str) -> Mark | None:
    """A mark written without a number - ST, SB, C - or None.

    Only for a place that already says the text is a mark: a schedule table's mark
    column. On a plan a lone "C" or "B" is a grid label, so parse_mark keeps
    requiring a number there. Upper-case letters only, and a prefix whose family
    is known (ST stair, SB beam, C column); a word or an unknown prefix is not one.
    """
    s = (text or "").strip().rstrip(".")
    if not re.fullmatch(r"[A-Z]{1,3}", s):
        return None
    mk = _mark(s)
    if mk is None or mk.number or family_of(mk.prefix) == "unknown":
        return None
    return mk


def _mark(raw: str, also: str = "") -> Mark | None:
    if GRADE_MARK.match(raw.strip()):
        return None
    r = raw.replace(" ", "").replace("-", "")
    fl = re.match(r"^(\d{1,2})(?=[A-Za-z])", r)
    if fl:
        # a floor number first (4B3): only before a prefix whose family is known and a
        # number - 2T16, 4D12, 8Y10 are bars, 3RD a word
        mk = _mark(r[fl.end():], also)
        if mk is None or not mk.number or family_of(mk.prefix) == "unknown":
            return None
        return Mark(raw.strip(), mk.prefix, mk.number, mk.suffix, mk.also, fl.group(1))
    m = re.match(r"^([A-Za-z]+?)(\d*)([A-Za-z]?)$", r)
    if not m:
        return None
    prefix, number, suffix = m.group(1), m.group(2), m.group(3)
    if not number:
        # no number: SB, C, Bn - upper-case letters are the prefix, a trailing
        # lower-case letter is a suffix (Bn is B, variant n)
        mm = re.match(r"^([A-Z]+)([a-z]?)$", prefix + suffix)
        if not mm or len(mm.group(1)) > 3:
            return None                       # a word, not a mark
        prefix, suffix = mm.group(1), mm.group(2)
    p = prefix.upper()
    if p in NOT_MARKS or len(p) > 4:
        return None
    return Mark(raw.strip(), p, number, suffix.upper(), also.upper())


@dataclass
class Label:
    """A mark with what is written beside it: a size, a thickness, or nothing."""
    mark: Mark | None
    width: float | None = None
    depth: float | None = None
    thickness: float | None = None
    text: str = ""
    marks: list[Mark] = field(default_factory=list)    # several marks sharing it


def labels_in(text: str) -> list[Label]:
    """Every mark+size label in one text; a text can hold several."""
    s = (text or "").strip()
    out: list[Label] = []
    if not s or GRADE.search(s):
        return out
    for m in LABEL.finditer(s):
        mk = _mark(m.group(1))
        if mk is None:
            continue
        w = m.group(2) or m.group(4)
        d = m.group(3) or m.group(5)
        out.append(Label(mk, float(w) if w else None, float(d) if d else None,
                         float(m.group(6)) if m.group(6) else None, s, [mk]))
    if out:
        return out
    m = SPACED.match(s)
    if m and not BAR.search(s):
        mk = _mark(m.group(1))
        if mk is not None:
            return [Label(mk, float(m.group(2)), float(m.group(3)), None, s, [mk])]
    if BAR.search(s):
        return out
    marks = grouped(s)
    if marks:
        return [Label(marks[0], text=s, marks=marks)]
    mk = parse_mark(s)
    if mk:
        out.append(Label(mk, text=s, marks=[mk]))
    return out


def grouped(text: str) -> list[Mark]:
    """Several marks sharing one label: B-52,54  S3,S5  B2,4 & 7  SW3,SW5.

    A part that is only a number takes the prefix of the part before it. Every
    part must be a mark, or nothing is returned.
    """
    parts = [p for p in re.split(r"\s*[,&]\s*", (text or "").strip()) if p]
    if len(parts) < 2:
        return []
    out, prefix = [], ""
    for p in parts:
        if re.fullmatch(r"\d{1,3}[A-Za-z]?", p) and prefix:
            mk = _mark(prefix + p)
        else:
            mk = parse_mark(p)
        if mk is None or not mk.number:
            return []
        out.append(mk)
        prefix = mk.floor + mk.prefix
    return out


def size_only(text: str) -> tuple[float, float] | None:
    """A text that is just a size, e.g. 230X600 written beside a mark."""
    m = SIZE_ONLY.match((text or "").strip())
    if not m:
        return None
    return float(m.group(1)), float(m.group(2))
