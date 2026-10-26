"""Data the extractor produces. These objects are what the CSV, JSON and the
cleaned DXF are all written from."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any

Point = tuple[float, float]

# Issue severities. BLOCKER means the element cannot be modelled at all.
BLOCKER = "blocker"
WARNING = "warning"
INFO = "info"


@dataclass
class Issue:
    severity: str
    category: str
    message: str
    mark: str = ""
    layer: str = ""
    x: float | None = None
    y: float | None = None
    handle: str = ""

    def as_row(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "category": self.category,
            "mark": self.mark,
            "layer": self.layer,
            "message": self.message,
            "x": round(self.x, 1) if self.x is not None else "",
            "y": round(self.y, 1) if self.y is not None else "",
            "handle": self.handle,
        }


@dataclass
class Column:
    mark: str
    kind: str                # column | column_stop | column_float
    x: float
    y: float
    width: float             # short side
    depth: float             # long side
    rotation: float          # degrees, direction of the long side
    outline: list[Point]
    source_layer: str
    handle: str
    tag_distance: float | None = None
    repaired: bool = False   # outline was open and has been closed
    notes: list[str] = field(default_factory=list)
    # A round column: its diameter (width and depth are both this). None for a
    # rectangular one, and then left out of model.json, which stays as it was.
    diameter: float | None = None


@dataclass
class Wall:
    mark: str
    thickness: float | None
    start: Point | None
    end: Point | None
    outline: list[Point]
    source_layer: str
    handle: str
    tag_distance: float | None = None
    notes: list[str] = field(default_factory=list)


@dataclass
class Beam:
    mark: str
    kind: str                # beam | girder
    start: Point
    end: Point
    width: float | None
    width_source: str        # multiline | schedule-required | default
    centreline_source: str   # multiline-faces | as-drawn
    offset_applied: float    # mm the drawn line was moved to reach the centre
    source_layer: str
    handle: str
    depth: float | None = None
    depth_source: str = ""   # drawing schedule | workbook | default
    tag_distance: float | None = None
    notes: list[str] = field(default_factory=list)
    # A curved beam: a point on its centreline arc, midway between the ends. None
    # for a straight beam. Revit builds the arc through start, mid and end.
    mid: Point | None = None
    # Top of the beam relative to the level, from the beam schedule's remarks
    # ("250MM SUNK" -> -250, "BT@ +0.200M" -> +200). 0 is flush with the slab.
    top_mm: float = 0.0

    @property
    def length(self) -> float:
        import math
        return math.hypot(self.end[0] - self.start[0], self.end[1] - self.start[1])


@dataclass
class SlabPanel:
    mark: str
    outline: list[Point]
    source_layer: str
    handle: str
    notes: list[str] = field(default_factory=list)
    thickness: float | None = None
    thickness_source: str = ""
    sunk_mm: float = 0.0     # top of this panel sits this far below the level
    holes: list[list[Point]] = field(default_factory=list)   # openings inside it


@dataclass
class Fold:
    """A slab fold: the downturn where a slab steps down to a sunk slab.

    Drawn on the slab layer as two parallel lines about 200 mm apart (on every
    Reis Magos plan), red, one often dashed. Unmarked and in no schedule; its
    depth is the step it closes plus the lower slab's thickness.
    """
    start: Point
    end: Point
    width: float | None          # gap between the drawn pair; None for a lone line
    source_layer: str
    handle: str
    depth: float | None = None   # set once the slabs either side are known
    top_mm: float = 0.0          # top of the fold relative to the level
    notes: list[str] = field(default_factory=list)


@dataclass
class SlabTag:
    mark: str
    x: float
    y: float
    source_layer: str
    handle: str
    thickness: float | None = None
    thickness_source: str = ""
    sunk_mm: float = 0.0     # from the slab schedule's remarks, e.g. "400mm SUNK"


@dataclass
class SunkZone:
    depth_mm: float | None
    depth_source: str        # layer-name | override | unknown
    outline: list[Point]
    source_layer: str
    handle: str


@dataclass
class Opening:
    kind: str                # opening | stair
    outline: list[Point]
    source_layer: str
    handle: str


@dataclass
class GridLine:
    label: str
    start: Point
    end: Point
    handle: str


@dataclass
class Result:
    source_file: str = ""
    # The Revit level this floor's beams sit on, as chosen when extracting. The
    # Revit script builds on the level of exactly this name. Blank: it guesses
    # from the drawing's name.
    level_name: str = ""
    dxf_file: str = ""
    units: str = ""
    dxf_version: str = ""
    base_point: Point = (0.0, 0.0)
    columns: list[Column] = field(default_factory=list)
    walls: list[Wall] = field(default_factory=list)
    beams: list[Beam] = field(default_factory=list)
    slabs: list[SlabPanel] = field(default_factory=list)
    slab_tags: list[SlabTag] = field(default_factory=list)
    folds: list[Fold] = field(default_factory=list)
    sunk: list[SunkZone] = field(default_factory=list)
    openings: list[Opening] = field(default_factory=list)
    grids: list[GridLine] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    layer_map: dict[str, str] = field(default_factory=dict)

    def add(self, severity: str, category: str, message: str, **kw) -> None:
        self.issues.append(Issue(severity, category, message, **kw))

    def counts(self) -> dict[str, int]:
        return {
            "columns": len(self.columns),
            "walls": len(self.walls),
            "beams": len([b for b in self.beams if b.kind == "beam"]),
            "girders": len([b for b in self.beams if b.kind == "girder"]),
            "slab panels": len(self.slabs),
            "slab tags": len(self.slab_tags),
            "slab folds": len([f for f in self.folds if f.width]),
            "sunk zones": len(self.sunk),
            "openings and stairs": len(self.openings),
            "grid lines": len(self.grids),
        }

    def issue_counts(self) -> dict[str, int]:
        out = {BLOCKER: 0, WARNING: 0, INFO: 0}
        for i in self.issues:
            out[i.severity] = out.get(i.severity, 0) + 1
        return out

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for c in d["columns"]:
            if c.get("diameter") is None:
                c.pop("diameter", None)
        d["counts"] = self.counts()
        d["issue_counts"] = self.issue_counts()
        return d
