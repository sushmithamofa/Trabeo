"""Look inside block references, so structure drawn in a block is not missed.

Consultants often draw a whole column grid once and insert it as a single block.
Reading model space alone then finds nothing: the layers are right, the geometry
is right, but it all sits one level down inside the block definition.

Exploding everything is not the answer either. The same sheet carries sections,
details and schedules, also as blocks, usually inserted at a scale other than
1:1. Those would arrive as extra columns and walls at four times life size.

So by default a block is opened only if it is inserted at plan scale - a uniform
scale of 1.0, mirrored or rotated as much as it likes. Anything else is left
shut and reported, with what it would have contributed, so a plan that really is
at another scale is never silently dropped.
"""
from __future__ import annotations

import fnmatch

from .model import INFO, WARNING, Result
from .reader import GEOMETRY_ROLES, TAG_ROLES, LayerMap

DEFAULTS = {
    "explode": "auto",          # "auto" | true | false
    "scale_tolerance": 0.02,    # how far from 1.0 still counts as plan scale
    "max_depth": 4,
    "min_report": 3,            # structural entities before a skip is worth a warning
    "explode_names": [],        # always open these, whatever the scale
    "skip_names": [],           # never open these
}


def settings(cfg: dict) -> dict:
    s = dict(DEFAULTS)
    s.update(cfg.get("blocks") or {})
    return s


def _matches(name: str, patterns) -> bool:
    up = str(name).upper()
    return any(fnmatch.fnmatchcase(up, str(p).upper()) for p in patterns)


def _scale_of(insert) -> tuple[float, float, float]:
    d = insert.dxf
    return (abs(float(getattr(d, "xscale", 1.0) or 1.0)),
            abs(float(getattr(d, "yscale", 1.0) or 1.0)),
            abs(float(getattr(d, "zscale", 1.0) or 1.0)))


def _at_plan_scale(insert, tol: float) -> bool:
    return all(abs(s - 1.0) <= tol for s in _scale_of(insert))


def _structural(entities, lmap: LayerMap) -> int:
    """How many of these entities sit on a layer we would actually extract."""
    wanted = GEOMETRY_ROLES | TAG_ROLES
    n = 0
    for e in entities:
        try:
            if lmap.role_of(e.dxf.layer) in wanted:
                n += 1
        except AttributeError:
            continue
    return n


def block_of(e) -> str | None:
    """The block (outermost) an entity was opened out of; None if drawn loose."""
    return getattr(e, "_c2r_block", None)


def upright(e) -> None:
    """Turn an entity drawn on an upside-down plane the right way up, in place.

    A polyline, arc or circle stores its points on its own drawing plane. One that was
    mirrored, or comes out of a mirrored block, has that plane facing down, and its
    stored X is the drawing's X negated: read as written it lands mirrored across the
    origin (ALCAZAR 1st floor: a slab outline in a mirrored block came out 3.3 km away).
    This rewrites it facing up, at the same place on the drawing. Entities already
    facing up, and texts (which cannot be turned this way), are left as they are."""
    try:
        ext = e.dxf.get("extrusion")
    except AttributeError:
        return
    if ext is None or ext[2] >= 0:
        return
    from ezdxf.upright import upright as _upright
    try:
        _upright(e)
    except Exception:
        pass


def _attribute_texts(insert, top=None) -> list:
    """An insert's visible attributes as plain TEXT entities (not added to the drawing)."""
    from ezdxf.entities import Text
    out = []
    for a in getattr(insert, "attribs", []) or []:
        try:
            if a.is_invisible or not (a.dxf.get("text", "") or "").strip():
                continue
            keys = ("text", "insert", "height", "rotation", "layer", "style", "halign", "valign",
                    "align_point", "width", "oblique", "color")
            t = Text.new(doc=insert.doc, dxfattribs={k: a.dxf.get(k) for k in keys if a.dxf.hasattr(k)})
            # what the mark is follows the layer the block was placed on, not the layer inside
            # the block (Pirgal reuses its grid bubble, attribute on GRID MARK, placed on SLAB_MARK)
            il = insert.dxf.get("layer", "0") or "0"
            if il != "0":
                t.dxf.layer = il
            if top is not None:
                t._c2r_block = top
            out.append(t)
        except Exception:
            continue
    return out


def flatten(msp, cfg: dict, res: Result | None = None, log=print) -> list:
    """Model space with every plan-scale block reference opened out.

    Returns a flat list of entities. Block contents come back already placed -
    rotated, mirrored and moved to where they appear on the drawing - so
    everything downstream can treat them as if they had been drawn loose.
    """
    s = settings(cfg)
    if s["explode"] is False:
        loose = list(msp)
        for e in loose:
            upright(e)
        return loose

    lmap = LayerMap(cfg["layer_roles"])
    force_all = s["explode"] is True
    out: list = []
    opened: dict[str, int] = {}
    shut: dict[str, tuple[float, int]] = {}
    failed: dict[str, str] = {}

    def walk(container, depth: int, top: str | None = None) -> None:
        for e in container:
            if top is not None:
                e._c2r_block = top        # which block it was opened out of (block_of)
            if e.dxftype() != "INSERT":
                upright(e)
                out.append(e)
                continue
            # a mark drawn as a block with its text in an attribute (Pirgal's slab-mark
            # bubbles: S5, S6 ...): the attribute is what the drawing shows - handed on as
            # text, at its place, on its layer; opening the block alone would lose it
            out.extend(_attribute_texts(e, top))

            name = str(e.dxf.name)
            if depth >= s["max_depth"] or _matches(name, s["skip_names"]):
                out.append(e)
                continue

            forced = _matches(name, s["explode_names"])
            if not forced and not force_all and not _at_plan_scale(e, s["scale_tolerance"]):
                try:
                    inner = list(e.virtual_entities())
                except Exception:
                    inner = []
                n = _structural(inner, lmap)
                if n:
                    sc = _scale_of(e)[0]
                    was, tot = shut.get(name, (sc, 0))
                    shut[name] = (was, tot + n)
                out.append(e)
                continue

            try:
                inner = list(e.virtual_entities())
            except Exception as exc:                     # a block we cannot open
                failed[name] = str(exc)[:120]
                out.append(e)
                continue

            opened[name] = opened.get(name, 0) + _structural(inner, lmap)
            walk(inner, depth + 1, top or name)

    walk(msp, 0)

    if res is not None:
        for name, n in sorted(opened.items(), key=lambda kv: -kv[1]):
            if n:
                res.add(INFO, "blocks",
                        f"Block '{name}' is inserted at plan scale and was opened out; "
                        f"{n} structural entities inside it were extracted. Without this "
                        "they would have been missed entirely.")
        for name, (sc, n) in sorted(shut.items(), key=lambda kv: -kv[1][1]):
            if n < s["min_report"]:
                continue
            res.add(WARNING, "blocks",
                    f"Block '{name}' holds {n} entities on structural layers but is "
                    f"inserted at {sc:g}x scale, so it was treated as a detail or section "
                    "and left out. If it is part of the plan, add its name to "
                    "\"blocks\": {\"explode_names\": [...]} in config.json.")
        for name, why in failed.items():
            res.add(WARNING, "blocks",
                    f"Block '{name}' could not be opened out ({why}). Anything structural "
                    "inside it has not been extracted; explode it in CAD and run again.")

    n_open = sum(1 for v in opened.values())
    if n_open:
        log(f"  Opened {n_open} block references "
            f"({sum(opened.values())} structural entities inside them)")
    return out
