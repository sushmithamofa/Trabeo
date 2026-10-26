"""What the tool can read, and what it can build in Revit - kept apart on purpose.

Reading a footing detail and being able to place a footing in Revit are two
different achievements. The Excel and the review screen show both, so nobody
mistakes "found in the drawing" for "will appear in the model".

Levels:
    supported   works, and is covered by baselines or tests
    partial     works for the common case; the note says what is left out
    planned     designed for, not built yet (the stage that delivers it)
    no          not intended (e.g. rebar is listed, never modelled)
    n/a         not something that is modelled at all (annotations)
"""
from __future__ import annotations

SUPPORTED, PARTIAL, PLANNED, NO, NA = "supported", "partial", "planned", "no", "n/a"

# type -> (read, model, note)
SUPPORT: dict[str, tuple[str, str, str]] = {
    "column":    (SUPPORTED, SUPPORTED, "position, size, rotation; size per floor from stage 3"),
    "beam":      (SUPPORTED, SUPPORTED, "straight and curved; beams and girders"),
    "beam_span": (SUPPORTED, PLANNED, "one marked span of a beam line the plan reader joined; built in stage 8"),
    "slab":      (SUPPORTED, SUPPORTED, "panels with cut-outs, thickness, sunk depth"),
    "fold":      (SUPPORTED, SUPPORTED, "slab downstand at a sunk step, built as framing"),
    "wall":      (SUPPORTED, PARTIAL, "runs, thickness, shape (straight/L/T/U/core) as the Revit build splits "
                                      "them; columns drawn as odd shapes and masonry flagged; marks SW/LW"),
    "sunk_zone": (SUPPORTED, PARTIAL, "built through the slab panel it lowers"),
    "opening":   (SUPPORTED, PARTIAL, "built as holes in the slab panel"),
    "slab_tag":  (SUPPORTED, NA, "annotation: carries the slab mark and schedule values"),
    "caption":   (SUPPORTED, NA, "a mark (and size) written on a plan; attached to its element by the linker"),
    "spec":      (SUPPORTED, NA, "what a detail or schedule says about a mark: size, steel, level"),
    "size_caption": (SUPPORTED, NA, "a size written on a plan with no mark beside it (9\"X21\" along a beam): "
                                    "gives its size to the beam it runs along"),
    "layer_size": (SUPPORTED, NA, "a size in a layer name; an optional hint"),
    "column_position": (SUPPORTED, NA, "a marked column on a column layout: gives plan columns their marks"),
    "level_mark": (PARTIAL, NA, "a level written with its elevation (sections, elevations, notes): feeds the "
                                "level table; floor / landing / beam / foundation / local"),
    "level":     (PARTIAL, PLANNED, "the level table: elevation per level and floor-to-floor heights, with "
                                    "sources and conflicts; used to set Revit levels in stage 8"),
    "footing":   (SUPPORTED, PLANNED, "outline, PCC bed, steps, mark, columns on it; sizes and depth "
                                      "from the schedule; built in stage 8"),
    "pedestal":  (PARTIAL, PLANNED, "from blocks marked PD/PED; built in stage 8"),
    "raft":      (PARTIAL, PLANNED, "outline labelled RAFT, thickness from notes; built in stage 8"),
    "pile":      (PLANNED, PLANNED, "stage 5"),
    "pile_cap":  (PLANNED, PLANNED, "stage 5"),
    "stair":     (PARTIAL, PLANNED, "flights from tread lines (width, going, treads), UP/DN; riser, tread "
                                    "and landing levels from notes; built in stage 8"),
    "ramp":      (PARTIAL, PLANNED, "outline on a RAMP layer / block or labelled RAMP, slope if written; "
                                    "built in stage 8"),
    "rebar":     (PLANNED, NO, "stage 3: listed in the Excel and parameters, never modelled"),
}
ELEMENT_TYPES = tuple(SUPPORT)


def support_of(type_: str) -> dict[str, str]:
    read, model, note = SUPPORT.get(type_, (NO, NO, "unknown element type"))
    return {"read": read, "model": model, "note": note}
