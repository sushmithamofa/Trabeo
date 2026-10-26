"""The existing extractor (cad2revit), as the first plan reader.

It runs cad2revit exactly as it is, unchanged, and turns its model.json into
building-model elements with evidence. to_legacy() turns them back into a
model.json the existing Revit builder and Excel generator read, field for field.

The two directions must be exact inverses: Stage 1 is only accepted when
model.json -> Building -> model.json reproduces every value, which is what
building/selftest.py and `tools/baseline.py compare` check. That is the proof the
new model loses nothing the working tool produces today.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any

from ..model import (Building, Element, Evidence, Issue, Sheet, Value,
                     DEFAULT, MISSING, READ)

READER = "plan.legacy"

# How each list in model.json maps onto elements.
#   type      element type
#   geometry  fields copied into Element.geometry
#   props     field -> the field naming its source ("" if none); becomes a Value
#   extra     reader-specific fields kept as they are
SPEC: dict[str, dict[str, Any]] = {
    "columns": {"type": "column", "geometry": ["x", "y", "rotation", "outline"],
                "props": {"kind": "", "width": "", "depth": ""},
                "extra": ["tag_distance", "repaired"]},
    "walls": {"type": "wall", "geometry": ["start", "end", "outline"],
              "props": {"thickness": ""}, "extra": ["tag_distance"]},
    "beams": {"type": "beam", "geometry": ["start", "end", "mid"],
              "props": {"kind": "", "width": "width_source", "depth": "depth_source",
                        "top_mm": ""},
              "extra": ["centreline_source", "offset_applied", "tag_distance"]},
    "slabs": {"type": "slab", "geometry": ["outline", "holes"],
              "props": {"thickness": "thickness_source", "sunk_mm": ""}, "extra": []},
    "slab_tags": {"type": "slab_tag", "geometry": ["x", "y"],
                  "props": {"thickness": "thickness_source", "sunk_mm": ""}, "extra": []},
    "folds": {"type": "fold", "geometry": ["start", "end"],
              "props": {"width": "", "depth": "", "top_mm": ""}, "extra": []},
    "sunk": {"type": "sunk_zone", "geometry": ["outline"],
             "props": {"depth_mm": "depth_source"}, "extra": []},
    "openings": {"type": "opening", "geometry": ["outline"],
                 "props": {"kind": ""}, "extra": []},
}
SHEET_FIELDS = ["source_file", "level_name", "dxf_file", "units", "dxf_version",
                "base_point", "stats", "layer_map", "grids"]
DERIVED = ["counts", "issue_counts"]          # recomputed, never stored


def _mapped(spec: dict[str, Any]) -> set[str]:
    """Fields with a home of their own in an Element; the rest go to extra."""
    return ({"mark", "source_layer", "handle", "notes"} | set(spec["geometry"])
            | set(spec["props"]) | {f for f in spec["props"].values() if f})


def _status(value, source: str) -> str:
    if value is None:
        return MISSING
    if source and source.strip().lower().startswith("default"):
        return DEFAULT
    return READ


def from_legacy(d: dict[str, Any], sheet_id: str, building: Building | None = None,
                wing: str = "") -> Building:
    """Add one extractor run (a model.json, as a dict) to a Building as one sheet."""
    b = building if building is not None else Building()
    sheet = Sheet(
        id=sheet_id, file=d.get("source_file", ""), kind="framing_plan",
        level=d.get("level_name", "") or "", wing=wing, reader=READER,
        dxf_file=d.get("dxf_file", ""), units=d.get("units", ""),
        dxf_version=d.get("dxf_version", ""),
        base_point=list(d.get("base_point") or [0.0, 0.0]),
        stats=d.get("stats") or {}, layer_map=d.get("layer_map") or {},
        grids=list(d.get("grids") or []),
        extra={"_keys": list(d.keys()),
               # an older extraction counted fewer things; keep to what it counted
               "_count_keys": list((d.get("counts") or {}).keys()) or None,
               **{k: v for k, v in d.items()
                  if k not in SHEET_FIELDS and k not in DERIVED and k not in SPEC
                  and k != "issues"}})
    b.add_sheet(sheet)

    by_handle: dict[str, str] = {}
    for key, spec in SPEC.items():
        for n, row in enumerate(d.get(key) or []):
            eid = f"{sheet_id}/{spec['type']}/{n}"
            layer, handle = row.get("source_layer", ""), row.get("handle", "")
            props = {}
            for p, src_field in spec["props"].items():
                if p not in row:
                    continue
                src = row.get(src_field, "") if src_field else ""
                props[p] = Value(row[p], _status(row[p], src), 1.0, [Evidence(
                    reader=READER, sheet=sheet_id, source=src, layer=layer, handle=handle)])
            el = Element(
                id=eid, type=spec["type"], sheet=sheet_id, level=sheet.level, wing=wing,
                mark=row.get("mark", "") or "",
                geometry={g: row[g] for g in spec["geometry"] if g in row},
                props=props, notes=list(row.get("notes") or []),
                # anything not mapped above travels untouched, so nothing is lost
                extra={**{x: v for x, v in row.items() if x not in _mapped(spec)},
                       "_keys": list(row.keys()), "_layer": layer, "_handle": handle})
            b.add(el)
            if handle:
                by_handle.setdefault(handle, eid)

    # Issue fields are kept exactly as written (a handle can be null), so the
    # issue list comes back unchanged.
    issue_fields = {"severity", "category", "message", "mark", "layer", "x", "y", "handle"}
    for i in d.get("issues") or []:
        x, y = i.get("x"), i.get("y")
        h = i.get("handle")
        b.issues.append(Issue(
            severity=i.get("severity"), category=i.get("category"),
            message=i.get("message"), sheet=sheet_id,
            elements=[by_handle[h]] if h in by_handle else [],
            mark=i.get("mark"), layer=i.get("layer"),
            at=None if x is None and y is None else [x, y], handle=h,
            extra={**{k: v for k, v in i.items() if k not in issue_fields},
                   "_keys": list(i.keys())}))
    return b


def to_legacy(b: Building, sheet_id: str) -> dict[str, Any]:
    """The model.json the existing Revit builder and Excel generator expect."""
    s = b.sheets[sheet_id]
    out: dict[str, Any] = {
        "source_file": s.file, "level_name": s.level, "dxf_file": s.dxf_file,
        "units": s.units, "dxf_version": s.dxf_version, "base_point": list(s.base_point),
        "stats": s.stats, "layer_map": s.layer_map, "grids": s.grids,
    }
    out.update({k: v for k, v in s.extra.items() if not k.startswith("_")})
    by_type = {spec["type"]: key for key, spec in SPEC.items()}
    for key in SPEC:
        out[key] = []
    for el in b.of(sheet=sheet_id):
        key = by_type.get(el.type)
        if key is None or el.extra.get("generated"):
            continue                      # an element type the old tool never had, or one made later
        spec = SPEC[key]
        row: dict[str, Any] = {}
        for k in el.extra.get("_keys", []):
            if k == "mark":
                row[k] = el.mark
            elif k == "source_layer":
                row[k] = el.extra.get("_layer", "")
            elif k == "handle":
                row[k] = el.extra.get("_handle", "")
            elif k == "notes":
                row[k] = list(el.notes)
            elif k in spec["geometry"]:
                row[k] = el.geometry.get(k)
            elif k in spec["props"]:
                row[k] = el.props[k].value
            elif k in el.extra:
                row[k] = el.extra[k]
            else:                         # a source field, e.g. depth_source
                owner = next((p for p, f in spec["props"].items() if f == k), None)
                ev = el.props[owner].evidence if owner and owner in el.props else []
                row[k] = ev[0].source if ev else ""
        out[key].append(row)

    out["issues"] = []
    for i in b.issues:
        if i.sheet != sheet_id:
            continue
        row = {"severity": i.severity, "category": i.category, "message": i.message,
               "mark": i.mark, "layer": i.layer,
               "x": i.at[0] if i.at else None, "y": i.at[1] if i.at else None,
               "handle": i.handle,
               **{k: v for k, v in i.extra.items() if k != "_keys"}}
        keys = i.extra.get("_keys")
        out["issues"].append({k: row.get(k) for k in keys} if keys else row)
    out["counts"] = _counts(out)
    if s.extra.get("_count_keys"):
        out["counts"] = {k: out["counts"].get(k, 0) for k in s.extra["_count_keys"]}
    out["issue_counts"] = _issue_counts(out["issues"])
    keys = s.extra.get("_keys")
    return {k: out[k] for k in keys if k in out} if keys else out


def _counts(d: dict[str, Any]) -> dict[str, int]:
    """Exactly as cad2revit's Result.counts()."""
    return {
        "columns": len(d["columns"]), "walls": len(d["walls"]),
        "beams": len([b for b in d["beams"] if b.get("kind") == "beam"]),
        "girders": len([b for b in d["beams"] if b.get("kind") == "girder"]),
        "slab panels": len(d["slabs"]), "slab tags": len(d["slab_tags"]),
        "slab folds": len([f for f in d["folds"] if f.get("width")]),
        "sunk zones": len(d["sunk"]), "openings and stairs": len(d["openings"]),
        "grid lines": len(d["grids"]),
    }


def _issue_counts(issues: list[dict[str, Any]]) -> dict[str, int]:
    out = {"blocker": 0, "warning": 0, "info": 0}
    for i in issues:
        out[i["severity"]] = out.get(i["severity"], 0) + 1
    return out


def read(src: str, out_dir: str, sheet_id: str, level: str | None = None,
         building: Building | None = None, wing: str = "", log=None,
         config_path: str | None = None) -> Building:
    """Run the existing extractor on one drawing and add it to a Building.

    config_path: a config with a consultant profile applied (building.profiles);
    None runs it exactly as it always has.
    """
    here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    sys.path.insert(0, os.path.join(here, "cad2revit"))
    from core import pipeline                                  # noqa: E402
    pipeline.run(src, out_dir=out_dir, config_path=config_path, level_name=level,
                 log=log or (lambda *a, **k: None))
    with open(os.path.join(out_dir, "model.json"), encoding="utf-8") as f:
        return from_legacy(json.load(f), sheet_id, building, wing)
