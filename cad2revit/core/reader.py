"""Decide what each layer in the drawing is for."""
from __future__ import annotations

import fnmatch

# Roles are tested in this order, so a specific pattern wins over a general one.
ROLE_ORDER = [
    "column_float", "column_stop", "column", "wall",
    "girder", "beam", "slab", "sunk", "opening", "stair", "grid",
    "column_tag", "beam_tag", "slab_tag",
    "ignore",
]

GEOMETRY_ROLES = {"column", "column_stop", "column_float", "wall",
                  "beam", "girder", "slab", "sunk", "opening", "stair", "grid"}
TAG_ROLES = {"column_tag", "beam_tag", "slab_tag"}


class LayerMap:
    """Maps a drawing layer name to one of the roles in ROLE_ORDER."""

    def __init__(self, rules: dict[str, list[str]]):
        self.rules = {role: [p.upper() for p in rules.get(role, [])]
                      for role in ROLE_ORDER}
        self._cache: dict[str, str] = {}

    def role_of(self, layer: str) -> str:
        key = layer.upper()
        if key in self._cache:
            return self._cache[key]
        role = self._match(key)
        if role == "unmapped" and key.rstrip(" ").endswith("."):
            # a full stop at the end is the same name: Sasan Gir UGWT's slab marks are on
            # 'SLAB NO.', the 'SLAB NO' every other drawing uses, and were all missed
            role = self._match(key.rstrip(". "))
        self._cache[key] = role
        return role

    def _match(self, key: str) -> str:
        for r in ROLE_ORDER:
            for pat in self.rules[r]:
                if key == pat or fnmatch.fnmatchcase(key, pat):
                    return r
        return "unmapped"

    def classify_drawing(self, layers: list[str]) -> dict[str, str]:
        return {lay: self.role_of(lay) for lay in sorted(layers)}


# Keyword guesses for a layer whose name the config does not know yet. Order
# matters: the first hit wins, so tag keywords are tested before element ones.
_GUESSES: list[tuple[str, tuple[str, ...], tuple[str, ...]]] = [
    ("column_tag", ("COL", "CLM"), ("TAG", "NO", "NUM", "TEXT", "MARK", "LABEL")),
    ("beam_tag", ("BEAM", "BM", "GIRDER"), ("TAG", "NO", "NUM", "TEXT", "MARK", "LABEL")),
    ("slab_tag", ("SLAB",), ("TAG", "NO", "NUM", "TEXT", "MARK", "LABEL")),
    ("ignore", ("HATCH", "DIM", "SCH", "SHEET", "ANNO", "LEGEND", "TITLE",
                "DETAIL", "NOTE", "LOGO", "REINF", "DEFPOINT"), ()),
    ("column_float", ("FLOAT", "FL COL", "FL_COL"), ()),
    ("column_stop", ("COL STOP", "COLSTOP", "STOP"), ()),
    ("wall", ("WALL", "PARDI", "SHEAR"), ()),
    ("girder", ("GIRDER",), ()),
    ("beam", ("BEAM",), ()),
    ("sunk", ("SUNK",), ()),
    ("slab", ("SLAB",), ()),
    ("column", ("COLUMN", "COL", "CLM"), ()),
    ("grid", ("GRID",), ()),
    ("stair", ("STAIR",), ()),
    ("opening", ("OPEN", "CUT", "VOID"), ()),
]


def guess_role(layer: str) -> str:
    """Best guess at what an unmapped layer holds, from its name alone.

    This only ever suggests. Nothing is extracted on a guess - the layer still
    has to be written into config.json by a person.
    """
    up = layer.upper()
    for role, words, qualifiers in _GUESSES:
        if any(w in up for w in words):
            if qualifiers and not any(q in up for q in qualifiers):
                continue
            return role
    return "unmapped"


def sunk_depth_from_layer(layer: str, override: dict[str, int]) -> tuple[float | None, str]:
    """Read a sunk depth out of a layer name, e.g. '150 SUNK' -> 150.

    An entry in `override` always wins, because layer names in the wild often
    disagree with the drawing's own legend.
    """
    import re
    for k, v in override.items():
        if k.upper() == layer.upper():
            return float(v), "override"
    # A layer from an attached drawing is named 'file$0$layer'; only the layer's
    # own name says anything. The whole name gave 13 mm for
    # '13_03_2026_REIS MARINA ... ARCHITECTURAL$0$SUNK' - the date.
    own = layer.split("$")[-1]
    # and only a number standing next to SUNK or MM is a depth
    m = (re.search(r"(\d+(?:\.\d+)?)\s*(?:MM)?\s*[-_ ]*SUNK", own, re.I)
         or re.search(r"SUNK\s*[-_ ]*(\d+(?:\.\d+)?)", own, re.I)
         or re.search(r"(\d+(?:\.\d+)?)\s*MM", own, re.I))
    if m:
        return float(m.group(1)), "layer-name"
    return None, "unknown"
