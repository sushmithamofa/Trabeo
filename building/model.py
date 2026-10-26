"""The shared building model every reader feeds and every output reads from.

One Building holds the whole structure: its sheets, levels and elements. Readers
never write to each other's data; each adds elements (or values on elements) with
the evidence for them, and later stages link and reconcile what they found.

Three ideas run through it:

* Every property is a Value: the value itself, where it came from (Evidence),
  how sure we are of it, and its status - read from the drawing, a default the
  tool assumed, missing, in conflict between two sources, or overridden by a
  person. Nothing is a bare number.
* Every element remembers the sheet it was read from, the layer and the CAD
  handle, so anything in the Excel or the review screen can be traced back to
  the line in the drawing it came from.
* Reading and modelling are tracked separately (see support.py). A detail the
  tool can read is not automatically something it can build in Revit.

Coordinates are in drawing units (mm) of the sheet they came from. Each sheet
carries its own base point; joining sheets into one building happens later, in
the linker, never inside a reader.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

SCHEMA_VERSION = 1

# Value statuses
READ = "read"            # read from the drawing (or another source document)
DEFAULT = "default"      # the tool assumed it; nothing in the drawing said so
MISSING = "missing"      # needed, and no source gave it
CONFLICT = "conflict"    # sources disagree; see the evidence
OVERRIDE = "override"    # set by a person: project override or review decision
STATUSES = (READ, DEFAULT, MISSING, CONFLICT, OVERRIDE)

# Issue severities, the same words the extractor uses
BLOCKER, WARNING, INFO = "blocker", "warning", "info"


@dataclass
class Evidence:
    """Where one piece of information came from."""
    reader: str                      # which reader produced it, e.g. "plan.legacy"
    sheet: str = ""                  # Sheet.id
    source: str = ""                 # how it was found: "drawing schedule", "multiline", ...
    layer: str = ""
    handle: str = ""                 # CAD handle of the entity, for tracing back
    at: list[float] | None = None    # drawing coordinates, where useful
    text: str = ""                   # raw text read, e.g. "B4a(230X600)"


@dataclass
class Value:
    value: Any = None
    status: str = READ
    confidence: float = 1.0
    evidence: list[Evidence] = field(default_factory=list)


@dataclass
class Element:
    id: str
    type: str                        # see support.ELEMENT_TYPES
    sheet: str                       # the sheet it was read from
    level: str = ""                  # level name, once known
    wing: str = ""
    mark: str = ""
    geometry: dict[str, Any] = field(default_factory=dict)
    props: dict[str, Value] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    # reader-specific detail kept so nothing is lost, e.g. how far the tag was
    extra: dict[str, Any] = field(default_factory=dict)
    # what the linker decided from every source (props stay as each reader read
    # them); a value here lists every source that gave it, agreeing or not
    resolved: dict[str, Value] = field(default_factory=dict)

    def get(self, name: str, default=None):
        v = self.props.get(name)
        return default if v is None else v.value

    def final(self, name: str, default=None):
        """The linker's value if there is one, else the reader's."""
        v = self.resolved.get(name) or self.props.get(name)
        return default if v is None else v.value


@dataclass
class Sheet:
    id: str
    file: str                        # the source drawing
    layout: str = ""                 # paper-space layout or region, once split
    kind: str = ""                   # framing_plan, foundation_plan, beam_details, ...
    title: str = ""
    wing: str = ""
    level: str = ""
    reader: str = ""                 # reader that produced its elements
    dxf_file: str = ""
    units: str = ""
    dxf_version: str = ""
    base_point: list[float] = field(default_factory=lambda: [0.0, 0.0])
    stats: dict[str, Any] = field(default_factory=dict)
    layer_map: dict[str, str] = field(default_factory=dict)
    grids: list[dict[str, Any]] = field(default_factory=list)
    refs: list[str] = field(default_factory=list)   # drawing references found on it
    extra: dict[str, Any] = field(default_factory=dict)   # reader output not mapped above


@dataclass
class Issue:
    severity: str
    category: str
    message: str
    sheet: str = ""
    elements: list[str] = field(default_factory=list)   # Element ids it concerns
    mark: str = ""
    layer: str = ""
    at: list[float] | None = None
    handle: str | None = ""
    extra: dict[str, Any] = field(default_factory=dict)   # reader output not mapped above


@dataclass
class Building:
    project: str = ""
    levels: list[str] = field(default_factory=list)
    sheets: dict[str, Sheet] = field(default_factory=dict)
    elements: list[Element] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    schema: int = SCHEMA_VERSION

    # ------------------------------------------------------------ building it
    def add_sheet(self, sheet: Sheet) -> Sheet:
        if sheet.id in self.sheets:
            raise ValueError(f"sheet id already used: {sheet.id}")
        self.sheets[sheet.id] = sheet
        if sheet.level and sheet.level not in self.levels:
            self.levels.append(sheet.level)
        return sheet

    def add(self, el: Element) -> Element:
        self.elements.append(el)
        return el

    def of(self, type_: str | None = None, sheet: str | None = None,
           level: str | None = None) -> list[Element]:
        return [e for e in self.elements
                if (type_ is None or e.type == type_)
                and (sheet is None or e.sheet == sheet)
                and (level is None or e.level == level)]

    # ------------------------------------------------------------ saving it
    def to_dict(self) -> dict[str, Any]:
        from .support import support_of
        d = asdict(self)
        for el, row in zip(self.elements, d["elements"]):
            row["support"] = support_of(el.type)
        return d

    def save(self, path: str) -> str:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=1)
        return path

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Building":
        def ev(rows):
            return [Evidence(**r) for r in rows or []]

        def val(r):
            return Value(r.get("value"), r.get("status", READ), r.get("confidence", 1.0),
                         ev(r.get("evidence")))

        b = cls(project=d.get("project", ""), levels=list(d.get("levels") or []),
                schema=d.get("schema", SCHEMA_VERSION))
        for sid, s in (d.get("sheets") or {}).items():
            b.sheets[sid] = Sheet(**s)
        for e in d.get("elements") or []:
            e = {k: v for k, v in e.items() if k != "support"}
            e["props"] = {k: val(v) for k, v in (e.get("props") or {}).items()}
            e["resolved"] = {k: val(v) for k, v in (e.get("resolved") or {}).items()}
            b.elements.append(Element(**e))
        b.issues = [Issue(**i) for i in d.get("issues") or []]
        return b

    @classmethod
    def load(cls, path: str) -> "Building":
        with open(path, encoding="utf-8") as f:
            return cls.from_dict(json.load(f))
