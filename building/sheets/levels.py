"""Which level and wing a title, layout name or file name refers to.

Titles say the same thing many ways: "1ST FLOOR PLAN", "STR. LAYOUT AT 1st FLOOR
LVL", "FIRST FLOOR FRAMING PLAN", "1F", "3ST FLOOR" (a typo, kept on purpose),
"GROUND/PLINTH", "TERRACE LVL". All of them reduce to one key per level, so
sheets from different consultants and different files can be joined:

    B2, B1      basements          GF          ground
    STILT       stilt / podium     PLINTH      plinth
    L1, L2 ...  numbered floors    MEZZ        mezzanine
    TERRACE     terrace            ROOF        roof
    OHT         overhead tank      MUMTY       mumty / lift machine room
    FDN         foundation

A title naming several ("GROUND/PLINTH", "2ND TO 5TH FLOOR", "TYPICAL") gives
all of them; deciding between them is the linker's job, not this module's.
"""
from __future__ import annotations

import re

_ORD = {
    "FIRST": 1, "SECOND": 2, "THIRD": 3, "FOURTH": 4, "FIFTH": 5, "SIXTH": 6,
    "SEVENTH": 7, "EIGHTH": 8, "NINTH": 9, "TENTH": 10, "ELEVENTH": 11,
    "TWELFTH": 12, "THIRTEENTH": 13, "FOURTEENTH": 14, "FIFTEENTH": 15,
}
_FLOORWORD = r"(?:FLOOR|FLR|FL|LVL|LEVEL|SLAB|ROOF)"
# 1ST / 2ND / 3RD / 4TH, and the typos that turn up in titles: 3ST, 4ND
_NUM_FLOOR = re.compile(r"\b(\d{1,2})\s*(?:ST|ND|RD|TH)?\s*\.?\s*" + _FLOORWORD + r"\b")
_NUM_ONLY = re.compile(r"\b(\d{1,2})\s*(?:ST|ND|RD|TH)\b")
_WORD_FLOOR = re.compile(r"\b(" + "|".join(_ORD) + r")\b")
_RANGE = re.compile(r"\b(\d{1,2})\s*(?:ST|ND|RD|TH)?\s*(?:TO|-|–|&)\s*(\d{1,2})\s*(?:ST|ND|RD|TH)?\s*"
                    + _FLOORWORD)
_LEVEL_N = re.compile(r"\b(?:LEVEL|LVL|L)\s*[-_ ]?\s*(\d{1,2})\b")
# FF / SF / TF SLAB LVL (first, second, third floor). Only before a floor word:
# SF alone is a footing mark to some consultants, and FFL is a finished floor level.
_ABBR = {"FF": 1, "SF": 2, "TF": 3}
_ABBR_FLOOR = re.compile(r"\b(FF|SF|TF)\s*\.?\s*" + _FLOORWORD + r"\b")

_NAMED = [
    ("B2", re.compile(r"\b(?:2ND\s+BASEMENT|BASEMENT\s*-?\s*2|LOWER\s+BASEMENT|B2)\b")),
    ("B1", re.compile(r"\b(?:(?<!2ND )(?<!LOWER )BASEMENT(?!\s*-?\s*2)|1ST\s+BASEMENT|B1)\b")),
    ("FDN", re.compile(r"\b(?:FOUNDATION|FOOTING|FDN|RAFT)\b")),
    ("PLINTH", re.compile(r"\bPLINTH\b")),
    ("STILT", re.compile(r"\b(?:STILT|PODIUM)\b")),
    ("GF", re.compile(r"\b(?:GROUND|G\.?\s?F\.?|UPPER\s+GROUND|LOWER\s+GROUND|U\.?\s?G(?=\.?\s*(?:FLOOR|FLR|FL)\b))\b")),
    ("MEZZ", re.compile(r"\bMEZZ(?:ANINE)?\b")),
    ("TERRACE", re.compile(r"\bTERRACE\b")),
    ("ROOF", re.compile(r"\bROOF\b(?!\s+BEAMS?\b)")),
    ("OHT", re.compile(r"\b(?:OHT|O\.H\.T|OVERHEAD\s+(?:WATER\s+)?TANK)\b")),
    ("MUMTY", re.compile(r"\b(?:MUMTY|MUMTEE|LMR|MACHINE\s+ROOM)\b")),
]
_TYPICAL = re.compile(r"\bTYPICAL\b")


def levels_in(text: str) -> list[str]:
    """Level keys a piece of text refers to, in the order they appear."""
    t = " " + (text or "").upper().replace("_", " ") + " "
    found: list[tuple[int, str]] = []

    for m in _RANGE.finditer(t):
        a, b = int(m.group(1)), int(m.group(2))
        if 0 < a < b <= 60:
            found += [(m.start(), f"L{n}") for n in range(a, b + 1)]
    for m in _NUM_FLOOR.finditer(t):
        n = int(m.group(1))
        # "BASEMENT 2 FLOOR PLAN": the 2 numbers the basement, not a floor
        if re.search(r"BASEMENT\s*-?\s*$", t[:m.start()]):
            continue
        if 0 < n <= 60:
            found.append((m.start(), f"L{n}"))
    if not found:
        for m in _NUM_ONLY.finditer(t):
            n = int(m.group(1))
            # "2ND BASEMENT" is the lower basement (B2), not the 2nd floor
            if re.match(r"\s*BASEMENT\b", t[m.end():]):
                continue
            if 0 < n <= 60 and re.search(r"FLOOR|FLR|LVL|LEVEL|SLAB|PLAN", t):
                found.append((m.start(), f"L{n}"))
    for m in _WORD_FLOOR.finditer(t):
        if re.match(r"\s*" + _FLOORWORD, t[m.end():]):
            found.append((m.start(), f"L{_ORD[m.group(1)]}"))
    for m in _LEVEL_N.finditer(t):
        found.append((m.start(), f"L{int(m.group(1))}"))
    for m in _ABBR_FLOOR.finditer(t):
        found.append((m.start(), f"L{_ABBR[m.group(1)]}"))
    for key, rx in _NAMED:
        for m in rx.finditer(t):
            found.append((m.start(), key))
    if _TYPICAL.search(t) and not found:
        found.append((_TYPICAL.search(t).start(), "TYPICAL"))

    out: list[str] = []
    for _, k in sorted(found):
        if k not in out:
            out.append(k)
    # "BASEMENT 2" also matched the bare BASEMENT rule's exclusion; keep both rules honest

    # A lintel level is a level of its own inside the storey: "GF LINTEL LEVEL" is
    # not the ground floor's slab, and its beams are not at GF. Keyed GF_LINTEL.
    if _LINTEL.search(t):
        out = [f"{k}_LINTEL" if k in _STOREYS or re.fullmatch(r"L\d{1,2}", k) else k for k in out]
    # "GF ROOF SLAB LEVEL" is the roof over the ground floor: the roof comes first
    if "ROOF" in out and len(out) > 1 and _ROOF_SLAB.search(t):
        out.remove("ROOF")
        out.insert(0, "ROOF")
    return out


_LINTEL = re.compile(r"\bLINTEL\b")
_ROOF_SLAB = re.compile(r"\bROOF\s+(?:SLAB|LEVEL|LVL)\b")
_STOREYS = ("B2", "B1", "PLINTH", "STILT", "GF", "MEZZ")


def roof_over(text: str) -> str:
    """The storey a title names the roof of: "basement roof" -> B1, "stilt floor roof"
    -> STILT, "first floor roof" -> L1. That plan is the slab over the storey, i.e.
    the next floor up. "" for anything else ("GF ROOF SLAB" stays the ROOF level)."""
    found = levels_in(text)
    if len(found) != 2 or found[1] != "ROOF" or _ROOF_SLAB.search((text or "").upper()):
        return ""
    k = found[0]
    return k if k in _STOREYS or re.fullmatch(r"L\d{1,2}", k) else ""


def next_up(key: str) -> str:
    """The floor above a storey when nothing else says which: B2 -> B1 -> GF -> L1 -> L2."""
    if key == "B2":
        return "B1"
    if key in ("B1", "PLINTH"):
        return "GF"
    if key in ("GF", "STILT", "MEZZ"):
        return "L1"
    m = re.fullmatch(r"L(\d{1,2})", key or "")
    return f"L{int(m.group(1)) + 1}" if m else ""


_RANK = {"FDN": -10.0, "B2": -3.0, "B1": -2.0, "PLINTH": -0.5, "GF": 0.0, "STILT": 0.0,
         "MEZZ": 0.5, "TERRACE": 100.0, "ROOF": 101.0, "MUMTY": 102.0, "OHT": 103.0}


# a plan titled by its level's value, not a floor's name: "FRAMING PLAN @(+)6600 LVL.",
# "LAYOUT AT +6.600 LEVEL", "PLAN AT EL. 9.600", "SLAB @ (-)1.200". The value follows "@", "AT"
# or a level word; a bare number is taken only with a sign or a level word beside it
_TITLE_ELEV = re.compile(
    r"(@|\bAT\b|\b(?:LVL|LEVEL|EL|ELEV|SFL|TOS|TOC)\b\.?)\s*:?\s*"
    r"(?:\(\s*([+\-±])\s*\)|([+\-±]))?\s*(\d{1,5}(?:\.\d{1,3})?)\s*(MM|M)?(?![\d.:/])"
    r"(\s*\.?\s*(?:LVL|LEVEL|LEV)\b)?")
_EL_KEY = re.compile(r"EL([+\-])(\d{1,6})")


def title_elevations(text: str) -> list[str]:
    """Level keys ("EL+6600") for the level values a plan's title is named by, in order.
    Only for a title naming no floor (levels_in finds none): the app's own key for a
    level the drawings call by its elevation."""
    out = []
    for m in _TITLE_ELEV.finditer((text or "").upper()):
        sign = m.group(2) or m.group(3)
        worded = not m.group(1).startswith(("@", "AT")) or bool(m.group(6))
        if not sign and not worded:
            continue                                # "PLAN AT 1" / "@ 150 C/C": not a level
        num, unit = m.group(4), m.group(5)
        if unit == "MM" or (unit is None and "." not in num):
            if float(num) < 100 and float(num) != 0:
                continue                            # "+3", "LVL 2": metres or a floor number - not said
            mm = float(num)
        else:
            mm = float(num) * 1000.0
        if mm > 300000:
            continue
        key = f"EL{'-' if sign == '-' and mm else '+'}{round(mm)}"
        if key not in out:
            out.append(key)
    return out


def key_elevation(key: str) -> float | None:
    """The elevation in mm a title-elevation key names ("EL+6600" -> 6600), else None."""
    m = _EL_KEY.fullmatch(key or "")
    return (-1.0 if m.group(1) == "-" else 1.0) * float(m.group(2)) if m else None


# a floor the person added for a project, one the app has no name for ("Service floor"
# below the ground floor): key "X_SERVICE_FLOOR_UNDER_GF" - its words, then where it sits
_CUSTOM_KEY = re.compile(r"X_([A-Z0-9]+(?:_[A-Z0-9]+)*?)_(OVER|UNDER)_([A-Z0-9_+\-]+)")


def custom_key(name: str, above: bool, base: str) -> str:
    """The key of an added floor: its name's words, above or below a floor the app knows."""
    words = "_".join(re.findall(r"[A-Z0-9]+", (name or "").upper()))
    return f"X_{words}_{'OVER' if above else 'UNDER'}_{base}" if words and base else ""


def custom_floor(key: str) -> tuple[str, bool, str] | None:
    """(name words, above?, the floor it sits against) of an added floor's key, else None."""
    m = _CUSTOM_KEY.fullmatch(key or "")
    return (m.group(1).replace("_", " "), m.group(2) == "OVER", m.group(3)) if m else None


def custom_floors_in(text: str, keys, renamed: dict | None = None) -> list[str]:
    """The added floors (keys) whose name a text says: "SERVICE FLOOR LAYOUT PLAN" names
    the added "Service floor". renamed: {key: its name now on the Floors page} - a floor
    added as "xzzcs" and renamed "Service Floor" is found by either. A new name that is a
    floor the app knows ("Ground Floor", "Level 0") is not used: it would take that floor's plan."""
    t = " " + (text or "").upper().replace("_", " ") + " "
    short = {"FLOOR": r"(?:FLOOR|FLR|FL)\.?", "LEVEL": r"(?:LEVEL|LVL|LEV)\.?"}   # as titles shorten them
    out = []
    for k in keys:
        cf = custom_floor(k)
        if not cf:
            continue
        names = [cf[0]]
        now = " ".join(re.findall(r"[A-Z0-9]+", str((renamed or {}).get(k) or "").upper()))
        if now and now != cf[0] and not levels_in(now) and not title_elevations(now):
            names.append(now)
        if any(re.search(r"\b" + r"\W*".join(short.get(w, re.escape(w)) for w in n.split()) + r"(?!\w)", t)
               for n in names) and k not in out:
            out.append(k)
    return out


# the words a title names a floor with: "THIRD FLOOR", "1ST FLR.", "TERRACE LEVEL", "LEVEL 3"
_FLOOR_PHRASES = [re.compile(p) for p in (
    r"\b\d{1,2}\s*(?:ST|ND|RD|TH)?\s*\.?\s*(?:FLOOR|FLR|FL)\b",
    r"\b(?:" + "|".join(_ORD) + r")\s+(?:FLOOR|FLR|FL)\b",
    r"\b(?:UPPER\s+|LOWER\s+)?GROUND(?:\s+(?:FLOOR|FLR|FL|LEVEL|LVL)\b)?",
    r"\b(?:U\.?\s?G|G)\.?\s?F\b\.?|\bU\.?\s?G\.?\s*(?:FLOOR|FLR|FL)\b",
    r"\b(?:\d(?:ST|ND)\s+|LOWER\s+|UPPER\s+)?BASEMENT(?:\s*-?\s*\d)?(?:\s+(?:FLOOR|FLR|FL)\b)?",
    r"\b(?:STILT|PODIUM|PLINTH|MEZZ(?:ANINE)?|TERRACE|ROOF|MUMTY|MUMTEE|FOUNDATION|RAFT|OHT|LMR|MACHINE\s+ROOM|"
    r"OVERHEAD\s+(?:WATER\s+)?TANK)(?:\s+(?:FLOOR|FLR|FL|LEVEL|LVL)\b)?",
    r"\b(?:LEVEL|LVL)\s*[-_ ]?\s*\d{1,2}\b",
)]
_LONG = {"FLR": "FLOOR", "FL": "FLOOR", "LVL": "LEVEL"}


def _ordinal(n: int) -> str:
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def _tidy(phrase: str) -> str:
    """'3ST  FLR.' -> '3rd Floor', 'TERRACE LVL' -> 'Terrace Level', 'LMR' -> 'LMR'."""
    words = re.findall(r"[A-Z0-9]+", phrase.upper())
    out = []
    for i, w in enumerate(words):
        if len(w) == 1 and w.isalpha():
            # initials as written with dots: U.G. FLOOR, G.F.
            if out and re.fullmatch(r"(?:[A-Z]\.)+", out[-1]):
                out[-1] += w + "."
            else:
                out.append(w + ".")
            continue
        w = _LONG.get(w, w)
        m = re.fullmatch(r"(\d{1,2})(ST|ND|RD|TH)?", w)
        nxt = _LONG.get(words[i + 1], words[i + 1]) if i + 1 < len(words) else ""
        if m and (m.group(2) or nxt == "FLOOR"):
            out.append(_ordinal(int(m.group(1))))
        elif m:
            out.append(m.group(1))
        elif not re.search(r"[AEIOU]", w):
            out.append(w)                    # OHT, LMR: as written
        else:
            out.append(w.capitalize())
    return " ".join(out)


def floor_name(texts, key: str) -> str:
    """A floor's name as the drawings write it, from the texts naming it (plan titles first):
    "THIRD FLOOR LAYOUT PLAN" -> "Third Floor", "TERRACE LEVEL LAYOUT PLAN" -> "Terrace Level",
    "FRAMING PLAN @(+)6600 LVL." -> "+6600 Level". Only words that name this very floor; ""
    when none does (the caller keeps its own name)."""
    cf = custom_floor(key)
    if cf is not None:
        return _tidy(cf[0])
    el = key_elevation(key)
    if el is not None:
        return f"{'-' if el < 0 else '+'}{abs(round(el))} Level"
    for text in texts:
        t = " " + (text or "").upper().replace("_", " ") + " "
        for rx in _FLOOR_PHRASES:
            for m in rx.finditer(t):
                if levels_in(m.group(0)) == [key]:
                    return _tidy(m.group(0))
        # "BASEMENT ROOF", "U.G. FLOOR ROOF": the slab over a storey - the floor above it,
        # named as the drawing names that slab
        under = roof_over(text)
        if under and next_up(under) and key not in (under,):
            for rx in _FLOOR_PHRASES:
                for m in rx.finditer(t):
                    tail = re.match(r"\s*(?:(?:FLOOR|FLR|FL)\b\.?\s*)?ROOF\b", t[m.end():])
                    if tail and levels_in(m.group(0)) == [under]:
                        return _tidy(m.group(0) + tail.group(0))
    return ""


def level_rank(key: str) -> float | None:
    """Where a level key sits, bottom to top: FDN < B1 < GF < L1 < L2 < TERRACE < OHT.
    None for keys with no fixed place (TYPICAL)."""
    if key in _RANK:
        return _RANK[key]
    cf = custom_floor(key)
    if cf is not None:
        # an added floor: just above the floor it sits on, or just below the one over it
        base = level_rank(cf[2])
        return None if base is None else base + (0.6 if cf[1] else -0.3)
    el = key_elevation(key)
    if el is not None:
        # a level named by its value: about where a 3 m storey of that height sits, among
        # the floors named by number (always under the terrace, over the foundation)
        return max(-9.0, min(99.0, el / 3000.0))
    if (key or "").endswith("_LINTEL"):
        # within its storey, above the floor and below the next one (and MEZZ)
        base = level_rank(key[:-len("_LINTEL")])
        return None if base is None else base + 0.4
    m = re.fullmatch(r"L(\d{1,2})", key or "")
    return float(m.group(1)) if m else None


_WING = [
    re.compile(r"\(\s*([A-Z0-9]{1,3})\s+WING\s*\)"),            # ( B WING )
    re.compile(r"\bWING\s*[-:]?\s*([A-Z0-9]{1,3})\b"),          # WING - B
    re.compile(r"\b([A-Z])\s*-?\s*WING\b"),                      # B-WING
    re.compile(r"\b(?:BLOCK|TOWER|BLDG|BUILDING)\s*[-:]?\s*([A-Z0-9]{1,3})\b"),
    re.compile(r"\b(MAIN|CLUB|SERVICE)\s+BLOCK\b"),              # MAIN BLOCK
]


def wing_in(text: str) -> str:
    t = (text or "").upper().replace("_", " ")
    for rx in _WING:
        m = rx.search(t)
        if m:
            return m.group(1)
    return ""


# +3.150, (+)3.15 M, SFL +6.300, LVL. + 9.45, EL 3150
_ELEV = re.compile(
    r"(?:\b(?:LVL|LEVEL|EL|ELEV|FFL|SFL|TOS|TOC|BOF|BOS)\b\.?\s*:?\s*)?"
    r"\(?\s*([+\-±])\s*\)?\s*(\d{1,3}(?:\.\d{1,3})?)\s*(MM|M)?\b")


def elevations_in(text: str) -> list[float]:
    """Level values in mm written as +3.150, (+) 3.15 M, -1.500 ..."""
    out = []
    for m in _ELEV.finditer((text or "").upper()):
        sign = -1.0 if m.group(1) == "-" else 1.0
        num = float(m.group(2))
        unit = m.group(3)
        if unit == "MM" or (unit is None and "." not in m.group(2) and num >= 100):
            mm = num
        else:
            mm = num * 1000.0
        if mm <= 200000:
            out.append(sign * mm)
    return out
