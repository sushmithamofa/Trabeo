"""Read a DXF and pull out the structural elements.

The drawing is never modified. Everything is read, corrected in memory, and
written out fresh by cleanout.py.
"""
from __future__ import annotations

import collections
import math
import re

import ezdxf

from . import blocks
from . import hatchlines
from . import sunklines
from . import legend
from . import units
from . import loops
from . import schedule as sched
from . import slabs as slab_bays
from . import geometry as G
from .model import (BLOCKER, INFO, WARNING, Beam, Column, Fold, GridLine, Issue,
                    Opening, Result, SlabPanel, SlabTag, SunkZone, Wall)
from .reader import GEOMETRY_ROLES, LayerMap, guess_role, sunk_depth_from_layer

MARK_CLEAN = re.compile(r"\s+")

# A mark is short and looks like C-12, 12, FC1, S4A, ST2A, B107/BP or BKT.
# Anything else on a tag layer is a note or a legend, not a mark.
MARK_OK = re.compile(
    r"^(?:[A-Za-z]{0,4}[-]?\d{1,3}[A-Za-z]?(?:/[A-Za-z0-9]{1,6})?|[A-Za-z]{2,4})$")
MARK_MAX_LEN = 14


def _clean_mark(s: str) -> str:
    s = MARK_CLEAN.sub("", s or "")
    for junk in ("%%U", "%%u", "%%C", "{", "}"):
        s = s.replace(junk, "")
    return s.strip()


def is_mark(s: str) -> bool:
    return bool(s) and len(s) <= MARK_MAX_LEN and MARK_OK.match(s) is not None


# A caption that writes its own size: GB1(1100X1200), B7 (230 X 600), GB13(1100X1300/1200).
SIZE_CAPTION = re.compile(r"\b([A-Z]{1,4}-?\d{1,3}[A-Z]?)\s*[\(\[]\s*(\d{2,4})\s*[X×*]\s*(\d{2,4})")


def _size_captions(ents) -> list[tuple[str, float, list[G.Point]]]:
    """(mark, written width, points of the text) for every text on the plan, on any
    layer, that writes a member's size after its mark. ALCAZAR 1st floor writes its
    girders' captions on the girder layer itself and on TXT, not on a tag layer."""
    from ezdxf import bbox as _bbox
    out = []
    for e in ents:
        if e.dxftype() not in ("TEXT", "MTEXT"):
            continue
        try:
            t = e.dxf.text if e.dxftype() == "TEXT" else e.plain_text()
        except Exception:
            continue
        m = SIZE_CAPTION.search(_clean_mark(t).upper())
        if not m:
            continue
        pts = []
        if e.dxf.hasattr("insert"):
            pts.append((e.dxf.insert.x, e.dxf.insert.y))
        try:
            b = _bbox.extents([e], fast=True)
            if b.has_data and b.size.x < 10000 and b.size.y < 10000:
                x0, y0, x1, y1 = b.extmin.x, b.extmin.y, b.extmax.x, b.extmax.y
                pts += [(x0, y0), (x1, y0), (x0, y1), (x1, y1), ((x0 + x1) / 2, (y0 + y1) / 2)]
        except Exception:
            pass
        if pts:
            out.append((m.group(1), float(m.group(2)), pts))
    return out


def _pts_of_lwpolyline(e) -> list[G.Point]:
    return [(p[0], p[1]) for p in e.get_points("xy")]


def _pts_of_polyline(e) -> list[G.Point]:
    return [(v.dxf.location.x, v.dxf.location.y) for v in e.vertices]


def _entity_z(e) -> float | None:
    """The Z a plan reader would trip over, if the entity has one."""
    for attr in ("elevation", "insert", "location", "start", "center"):
        if e.dxf.hasattr(attr):
            v = e.dxf.get(attr)
            if isinstance(v, (int, float)):
                return float(v)
            try:
                return float(v.z)
            except AttributeError:
                continue
    return None


# A slab thickness written as a note: "135 Thk", "140 MM THK.", "125mm thick".
THICK_NOTE = re.compile(r"^\s*(\d{2,3}(?:\.\d+)?)\s*(?:MM)?\.?\s*TH(?:I?C)?KN?E?S?S?\.?\s*$", re.I)
# Marks that name a wall or a column are never taken for slab marks.
WALL_OR_COLUMN = re.compile(r"^(?:SW|LW|RW|W|COL|C)\d", re.I)


def _slab_marks_on_beam_layers(tags: dict) -> list[tuple[str, float, float, float]]:
    """Slab marks written on a beam mark layer, moved to the slab marks.

    Sasan Gir terrace: "S4" with "135 Thk" under it, 14 times, in a block on the layer
    BEAM NO, so they were read as beam marks - S4 was given to beams on a best guess,
    and the rooms they name had no slab mark, so no slab. A mark with a slab thickness
    written right under it says what it is, whatever its layer. Only that: a mark
    with no thickness note under it stays where its layer puts it, and a mark naming a
    wall or column (SW7, C3) is never moved. Returns (mark, x, y, thickness) of each."""
    raw = tags.get("beam_tag") or []

    def text_of(e):
        try:
            return e.plain_text() if e.dxftype() == "MTEXT" else e.dxf.text
        except Exception:
            return ""

    def frame(e):
        h = float((e.dxf.get("height", 0) if e.dxftype() == "TEXT" else e.dxf.get("char_height", 0)) or 0)
        rot = math.radians(float(e.dxf.get("rotation", 0) or 0))
        return (e.dxf.insert.x, e.dxf.insert.y), h, rot

    notes = []
    for e in raw:
        m = THICK_NOTE.match(re.sub(r"%%[UuOo]", "", text_of(e) or "").strip())
        if m and e.dxf.hasattr("insert"):
            notes.append((float(m.group(1)), frame(e)[0]))
    if not notes:
        return []
    keep, moved = [], []
    for e in raw:
        mark = _clean_mark(text_of(e))
        if not (is_mark(mark) and e.dxf.hasattr("insert")) or WALL_OR_COLUMN.match(mark):
            keep.append(e)
            continue
        p, h, rot = frame(e)
        h = h or 150.0
        c, s = math.cos(-rot), math.sin(-rot)
        under = None
        for thk, q in notes:
            dx, dy = q[0] - p[0], q[1] - p[1]
            u, v = dx * c - dy * s, dx * s + dy * c          # along / across the mark's own line
            if abs(u) <= 3.0 * h and -3.5 * h <= v <= -0.4 * h and 75.0 <= thk <= 500.0:
                under = thk
                break
        if under is None:
            keep.append(e)
        else:
            moved.append((mark, p[0], p[1], under))
            tags.setdefault("slab_tag", []).append(e)
    if moved:
        tags["beam_tag"] = keep
    return moved


# --------------------------------------------------------------------------
# main entry
# --------------------------------------------------------------------------

def extract(dxf_path: str, cfg: dict, log=print) -> Result:
    tol = cfg["tolerances"]
    res = Result(dxf_file=dxf_path)

    log("  Opening the DXF")
    doc = ezdxf.readfile(dxf_path)
    msp = doc.modelspace()
    res.dxf_version = doc.dxfversion
    # everything below is in millimetres: other units are converted, or the drawing
    # is stopped when its units cannot be known (units.py)
    try:
        k, res.units, note, _ = units.to_mm(doc, cfg.get("units"))
    except units.UnitsError as exc:
        res.units = "unknown"
        res.add(BLOCKER, "setup", f"{exc} Nothing was read from it.")
        return res
    if note:
        res.add(WARNING, "setup", f"Drawing units: {note}.")

    lmap = LayerMap(cfg["layer_roles"])
    layer_names = [l.dxf.name for l in doc.layers]
    res.layer_map = lmap.classify_drawing(layer_names)

    log("  Looking inside block references")
    ents = blocks.flatten(msp, cfg, res, log=log)
    # read what the legend says about sunk hatches before it is taken out
    sunk_key = legend.sunk_key(ents, cfg, res, log=log)
    sunk_key_lines = legend.sunk_key_lines(ents, cfg, res, log=log)
    ents = legend.exclude(ents, cfg, res, log=log)
    ents = hatchlines.drop(ents, cfg, lmap.role_of, res, log=log)

    unmapped = sorted({lay for lay, role in res.layer_map.items() if role == "unmapped"})
    used_layers = {e.dxf.layer for e in ents}
    unmapped_used = [l for l in unmapped if l in used_layers]
    skipped_entities = 0
    for lay in unmapped_used:
        n = sum(1 for e in ents if e.dxf.layer == lay)
        skipped_entities += n
        hint = guess_role(lay)
        suffix = (f" Its name suggests it holds '{hint}'; add it under that role in "
                  "config.json to use it.") if hint != "unmapped" else \
                 " Add it to config.json if it holds structure."
        res.add(WARNING if n >= 5 else INFO, "layers",
                f"Layer '{lay}' carries {n} entities but is not in the layer map, so it "
                f"was skipped.{suffix}", layer=lay)

    empty = [l for l in layer_names if l not in used_layers]
    if empty:
        res.add(INFO, "layers",
                f"{len(empty)} of {len(layer_names)} layers carry no entities: "
                + ", ".join(empty[:12]) + ("..." if len(empty) > 12 else ""))

    # ---- bucket the entities ------------------------------------------
    buckets: dict[str, list] = {r: [] for r in GEOMETRY_ROLES}
    tags: dict[str, list] = {"column_tag": [], "beam_tag": [], "slab_tag": []}
    off_z = 0

    log("  Sorting entities by layer role")
    for e in ents:
        role = lmap.role_of(e.dxf.layer)
        if role in ("ignore", "unmapped"):
            continue
        z = _entity_z(e)
        if z is not None and abs(z) > tol["flat_z_tolerance_mm"]:
            off_z += 1
        if role in tags:
            if e.dxftype() in ("TEXT", "MTEXT"):
                tags[role].append(e)
        elif role in buckets:
            buckets[role].append(e)

    if off_z:
        res.add(WARNING, "setup",
                f"{off_z} structural entities sit at a Z other than zero. They have "
                "been flattened to Z = 0 for extraction; confirm none of them was "
                "meant to be at a different level.")

    # ---- tags ----------------------------------------------------------
    tag_rot: dict[str, float] = {}
    res.stats["_tag_rot"] = tag_rot

    def tag_points(role):
        out, skipped = [], []
        for t in tags[role]:
            mark = _clean_mark(t.dxf.text if t.dxftype() == "TEXT" else t.text)
            if not mark:
                continue
            ip = t.dxf.insert if t.dxf.hasattr("insert") else None
            if ip is None:
                continue
            if not is_mark(mark):
                skipped.append(mark)
                continue
            p = (ip.x, ip.y)
            if role == "slab_tag":
                # the middle of the mark, not its insert point: a slab mark written from
                # the beam face ("%%UNS4" on New block) starts on the beam, outside its bay
                try:
                    from ezdxf import bbox as _bbox
                    b = _bbox.extents([t], fast=True)
                    if b.has_data and b.size.x < 5000 and b.size.y < 5000:
                        p = ((b.extmin.x + b.extmax.x) / 2.0, (b.extmin.y + b.extmax.y) / 2.0)
                except Exception:
                    pass
            out.append((mark, p, t.dxf.handle, t.dxf.layer))
            try:                          # which way the text runs, for the beam match
                tag_rot[t.dxf.handle] = float(t.dxf.rotation or 0.0)
            except Exception:
                pass
        if skipped:
            sample = "; ".join(s[:40] + ("..." if len(s) > 40 else "") for s in skipped[:3])
            res.add(INFO, "tags",
                    f"{len(skipped)} text objects on the {role.replace('_', ' ')} layers do "
                    f"not read as marks and were skipped, e.g. \"{sample}\". Keep notes and "
                    "legends off the tag layers.")
        return out

    moved_thk = _slab_marks_on_beam_layers(tags)

    col_tags = tag_points("column_tag")
    beam_tags = tag_points("beam_tag")
    slab_tags_raw = tag_points("slab_tag")

    for mark, p, h, lay in slab_tags_raw:
        res.slab_tags.append(SlabTag(mark=mark, x=p[0], y=p[1], source_layer=lay, handle=h))
    if moved_thk:
        for mark, x, y, thk in moved_thk:
            near = [t for t in res.slab_tags if t.mark == mark and G.dist((t.x, t.y), (x, y)) <= 1000.0]
            if near:
                t = min(near, key=lambda t: G.dist((t.x, t.y), (x, y)))
                t.thickness, t.thickness_source = thk, "written under the mark"
        res.add(INFO, "tags",
                f"{len(moved_thk)} marks on the beam mark layers have a slab thickness written under "
                f"them (e.g. \"{moved_thk[0][0]} / {moved_thk[0][3]:.0f} thk\"), so they were read as "
                "slab marks, with that thickness.")

    compound = [(m, p, h) for m, p, h, _ in (col_tags + beam_tags + slab_tags_raw) if "/" in m]
    for m, p, h in compound:
        res.add(WARNING, "tags",
                f"Mark '{m}' names more than one element in a single text object, so "
                "it cannot be assigned reliably. Split it into separate text objects.",
                mark=m, x=p[0], y=p[1], handle=h)

    # ---- columns, walls, slabs, sunk, openings -------------------------
    log("  Extracting columns and walls")
    _extract_closed_shapes(ents, buckets, res, cfg, tol)
    _drop_outlines_round_columns(res)

    # The printed schedule is read before the geometry is interpreted, because
    # the merging of face pairs has to know what beam widths actually exist in
    # this job before it can decide whether two parallel lines are one beam.
    sched_pre = sched.read_all(ents, cfg)

    log("  Extracting beam and girder centrelines")
    _extract_beams(doc, buckets, res, cfg, tol)

    log("  Extracting slabs, sunk zones, openings and grid")
    _extract_slabs_and_zones(buckets, res, cfg, tol, ents, sunk_key, sunk_key_lines)
    _extract_grid(buckets, res)

    # ---- tag matching ---------------------------------------------------
    log("  Merging beams drawn as two faces")
    _split_long_faces(res, tol)
    _merge_face_pairs(res, tol, sched_pre[0], beam_tags, _size_captions(ents))
    _reconcile_chain_widths(res, tol)
    _drop_stacked_beams(res, tol)
    _drop_orphan_faces(res, tol, beam_tags)
    _drop_end_caps(res, tol, beam_tags)
    _bridge_staggered_faces(res, tol)
    _align_face_continuations(res, tol)

    log("  Joining the beam network")
    _join_beam_network(res, tol)
    _extend_beams_to_supports(res, tol)
    _carry_past_side_beams(res, tol)
    _trim_over_neighbour(res, tol)

    log("  Splitting beams into spans")
    _split_beams_at_crossings(res, tol)

    _drop_detached_islands(res, cfg, tol)

    log("  Matching marks to elements")
    _match_column_tags(col_tags, res, cfg, tol)
    _drop_duplicate_shapes(res, tol)
    # After the column marks, so a floating column (FC) is known not to be a support.
    _split_beams_at_columns(res, tol, beam_tags, cfg)
    _match_beam_tags(beam_tags, res, tol, cfg)
    _trim_over_neighbour(res, tol, marked_only=True)
    # After the marks: a marked line is a beam, whatever it outlines.
    _drop_opening_outlines(res, tol)
    # After the cut-outs: a join never makes a beam that is then taken for a cut-out's edge
    _join_overlapping_spans(res, tol)
    _drop_label_boxes(res, ents)

    # After every straight beam is merged, joined and tagged: the curved ones,
    # which take any mark the straight beams left unclaimed.
    _add_curved_beams(res, tol, beam_tags)

    log("  Reading the schedules printed on the drawing")
    sched.apply(ents, res, cfg, log=log, pre=sched_pre)
    # An unmarked curve is the end of the straight beam it runs into: its size.
    _size_curved_beams(res)

    _reclassify_walls(res, cfg)
    # Last of the geometry: the bays are only right once every beam is final.
    log("  Finding slab panels between the beams")
    slab_bays.derive_panels(res, cfg, log=log)
    res.stats.pop("_stair_points", None)
    res.stats.pop("_stair_segments", None)
    res.stats.pop("_opening_edges", None)
    slab_bays.check_drawn(res, res.stats.pop("_open_slab_fragments", 0))
    # After the slabs, which run to the column faces either way.
    _carry_floating_columns(res)
    _check_duplicates(res, tol)
    _report_justification(res)
    _check_alignment(res, tol)
    _check_beam_alignment(res, tol)
    _base_point(res, cfg)

    if not (res.columns or res.beams or res.walls or res.slabs):
        if skipped_entities:
            res.add(BLOCKER, "layers",
                    f"No structural elements were extracted. {skipped_entities} entities sit "
                    f"on {len(unmapped_used)} layers this tool does not recognise. Run with "
                    "--inspect to list them with a suggested role, then add them to "
                    "config.json and run again.")
        else:
            res.add(BLOCKER, "layers",
                    "No structural elements were extracted and no unrecognised layers were "
                    "found either. Check that the drawing holds the plan in model space.")

    res.stats = {
        "entities_read": len(ents),
        "entities_in_model_space": len(msp),
        "layers": len(layer_names),
        "layers_unmapped_in_use": len(unmapped_used),
        "entities_off_z": off_z,
    }
    return res


# --------------------------------------------------------------------------


def _drop_duplicate_shapes(res: Result, tol) -> None:
    """Drop a column or wall outline that is drawn twice in the same place.

    The same outline drawn on top of itself gives two Revit elements standing
    inside one another: invisible in a 3D view, a phantom extra row in every
    schedule, and double the concrete in a take-off.

    Two shapes count as the same only when their outlines match point for point.
    Comparing centres and sizes is not enough: a lift shaft is often two
    different outlines about one centre, and a wall carries no width or depth to
    tell them apart. The points are sorted before comparison, so a shape traced
    clockwise matches the same shape traced anticlockwise, and one whose polyline
    starts at a different corner matches too.

    This runs after the marks have been matched, which decides what to keep. A
    marked copy is kept over an unmarked one, because the mark is the only thing
    tying the element to its schedule row. Where both copies carry a mark and the
    two marks differ, neither is dropped: the drawing is saying two different
    things about one outline, and only the consultant can say which is right.
    """
    t = max(tol["duplicate_tolerance_mm"], 2.0)

    def shape_key(outline):
        """A tuple that is equal for two outlines drawn on top of one another."""
        if not outline:
            return None
        return tuple(sorted((round(p[0] / t), round(p[1] / t)) for p in outline))

    def drop(items, kind):
        kept, groups, order = [], {}, []
        for it in items:
            key = shape_key(it.outline)
            if key is None:
                kept.append(it)
                continue
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append(it)

        dropped, clashes = 0, []
        for key in order:
            group = groups[key]
            if len(group) == 1:
                kept.append(group[0])
                continue
            marks = sorted({g.mark for g in group if g.mark})
            if len(marks) > 1:
                kept.extend(group)                  # a real contradiction: keep both
                clashes.append((marks, group[0]))
                continue
            keep = next((g for g in group if g.mark), group[0])
            keep.notes.append(f"{len(group) - 1} identical outline(s) drawn on top of "
                              "this one have been dropped")
            kept.append(keep)
            dropped += len(group) - 1

        for marks, where in clashes:
            p = G.centroid(where.outline) if where.outline else (None, None)
            res.add(BLOCKER, "geometry",
                    f"One {kind} outline is drawn twice in the same place and the two "
                    f"copies carry different marks ({', '.join(marks)}). Both have been "
                    "kept because the drawing does not say which is right. Delete the "
                    "repeat, or draw them as two separate elements.",
                    mark=marks[0], x=p[0], y=p[1], handle=where.handle)
        return kept, dropped

    res.columns, n_col = drop(res.columns, "column")
    res.walls, n_wall = drop(res.walls, "wall")

    if n_col or n_wall:
        parts = []
        if n_col:
            parts.append(f"{n_col} column")
        if n_wall:
            parts.append(f"{n_wall} wall")
        res.add(WARNING, "geometry",
                " and ".join(parts) + " outlines are drawn twice in the same place, one "
                "on top of the other. The repeats have been dropped so they do not become "
                "duplicate elements stacked at one point in Revit. Ask for them to be "
                "removed from the drawing, and run OVERKILL before issuing.")



def _shape_notes(repaired: bool, assembled: bool) -> list[str]:
    n = []
    if repaired:
        n.append("outline repaired")
    if assembled:
        n.append("outline assembled from separate lines, not drawn as one polyline")
    return n


def _shapes_in(role, buckets, res: Result, cfg, tol):
    """Every outline on this role: drawn as a polyline, or built from loose lines.

    Yields (points, closed, layer, handle, assembled, block it came out of or None,
    linetype as set on the entity).
    """
    for e in buckets[role]:
        t = e.dxftype()
        lt = (e.dxf.get("linetype", "BYLAYER") or "BYLAYER").upper()
        if t == "LWPOLYLINE":
            yield (_pts_of_lwpolyline(e), bool(e.closed), e.dxf.layer, e.dxf.handle, False,
                   blocks.block_of(e), lt)
        elif t == "POLYLINE":
            yield (_pts_of_polyline(e), bool(e.is_closed), e.dxf.layer, e.dxf.handle, False,
                   blocks.block_of(e), lt)

    ls = loops.settings(cfg)
    if role not in (ls.get("roles") or []):
        return
    lines = [e for e in buckets[role] if e.dxftype() == "LINE"]
    if not lines:
        return
    if len(lines) > ls["max_lines"]:
        res.add(INFO, "geometry",
                f"{len(lines)} loose lines sit on the {role} layers, too many to stitch "
                "into outlines safely (that is usually hatching on a structural layer). "
                "They have been left alone.")
        return

    _segs, faces = loops.from_entities(lines, ls["snap_mm"], ls["min_area_mm2"],
                                       ls["max_vertices"])
    if not faces:
        return
    layer = lines[0].dxf.layer
    res.add(INFO, "geometry",
            f"{len(faces)} closed outlines were assembled from {len(lines)} loose lines "
            f"on the {role} layers. They are drawn as separate lines rather than as one "
            "closed polyline, which looks identical on the sheet but is not a shape until "
            "it is stitched back together. Draw one closed polyline per element.",
            layer=layer)
    for k, pts in enumerate(faces):
        yield pts, True, layer, "assembled-%s-%d" % (role, k), True, None, "BYLAYER"


def _drop_outlines_round_columns(res: Result) -> None:
    """A "wall" outline on a column layer with two or more columns inside it is the outline
    of something round them - the floor's edge, the building - not a wall: Pirgal's third
    floor has its 11 x 17 m slab outline on 'RCE COL', built as one wall over the whole
    floor. A real wall never stands round columns. Walls on wall layers are left alone."""
    if not res.walls or not res.columns:
        return
    from shapely.geometry import Point, Polygon
    centres = []
    for c in res.columns:
        if len(c.outline) >= 3:
            p = Polygon(c.outline)
            if p.is_valid:
                q = p.representative_point()
                centres.append((q.x, q.y))
    keep = []
    for w in res.walls:
        layer = (w.source_layer or "")
        if len(w.outline) < 3 or not re.search(r"COL|CLM", layer, re.I):
            keep.append(w)
            continue
        try:
            p = Polygon(w.outline).buffer(0)
        except Exception:
            keep.append(w)
            continue
        inside = sum(1 for x, y in centres if p.contains(Point(x, y)))
        if inside >= 2 and p.area >= 20e6:
            c = p.representative_point()
            res.add(INFO, "geometry",
                    f"A {p.area / 1e6:.0f} m2 outline on '{layer}' has {inside} columns inside it: "
                    "an outline round them (a floor or building edge), not a wall - not built.",
                    layer=layer, x=c.x, y=c.y, handle=w.handle)
            continue
        keep.append(w)
    res.walls = keep


def _extract_closed_shapes(msp, buckets, res: Result, cfg, tol) -> None:
    """Columns and walls come from closed outlines, drawn or assembled."""
    slivers = []            # rectangles too thin for a column: part of the one beside?
    from_block = {}         # id(column) -> the block it was opened out of
    dashed = {}             # id(column) -> drawn dashed (hidden)
    doc = next((e.doc for r in ("column", "column_stop", "column_float") for e in buckets[r]
                if getattr(e, "doc", None) is not None), None)
    dashed_types = _dashed_linetypes(doc)
    for role in ("column", "column_stop", "column_float", "wall"):
        for pts, closed, e_layer, e_handle, assembled, blk, e_lt in _shapes_in(
                role, buckets, res, cfg, tol):

            # last point back on the first = closed, flag or not (New block 246):
            # dedupe drops that point, and a whole side would then read as the gap
            if not closed and len(pts) > 3 and \
                    G.dist(pts[0], pts[-1]) <= tol["duplicate_tolerance_mm"]:
                closed = True
            pts = G.dedupe_points(pts, tol["duplicate_tolerance_mm"])
            if len(pts) < 3:
                continue

            repaired = False
            if not closed and len(pts) >= 5:
                # the closing side drawn too long, on past the first corner (Sasan Gir
                # terrace, the column under B25c: its left side runs 570 mm on up to B6's
                # face). Closed across that "gap" it was a 5-point L, built as a wall. The
                # first point lies on the last side: that is the corner, the rest overshoot.
                # (And the same with the first side drawn on past the last point.)
                on = max(tol.get("rect_side_tolerance_mm", 5.0), 1.0)
                if G.point_to_segment(pts[0], pts[-2], pts[-1]) <= on and \
                        G.dist(pts[0], pts[-2]) > on and G.dist(pts[0], pts[-1]) > on:
                    pts, closed, repaired = pts[:-1], True, True
                elif G.point_to_segment(pts[-1], pts[0], pts[1]) <= on and \
                        G.dist(pts[-1], pts[0]) > on and G.dist(pts[-1], pts[1]) > on:
                    pts, closed, repaired = pts[1:], True, True
            if not closed:
                gap = G.dist(pts[0], pts[-1])
                if gap <= tol["close_gap_tolerance_mm"]:
                    repaired = True          # the shape was open only by a rounding gap
                elif len(pts) >= 4 and gap <= tol.get("close_wall_end_mm", 600.0):
                    # Open by about a wall's thickness: the drafter stopped one
                    # short edge from closing it. On the Wing B 2nd floor that was
                    # the stair / lift wall, open by 230 mm; dropped, the floor lost
                    # its walls and the beams framing into them stopped short.
                    repaired = True
                    res.add(WARNING, "geometry",
                            f"An outline on '{e_layer}' was open by {gap:.0f} mm, about one "
                            "wall thickness, and has been closed across the gap. Check the "
                            "shape in the report, and close the polyline in the drawing.",
                            layer=e_layer, x=pts[0][0], y=pts[0][1], handle=e_handle)
                else:
                    res.add(BLOCKER, "geometry",
                            f"An outline on '{e_layer}' is open by {gap:.0f} mm and "
                            "cannot be turned into a solid. Close the polyline.",
                            layer=e_layer, x=pts[0][0], y=pts[0][1],
                            handle=e_handle)
                    continue

            rect = G.rectangle_of(pts, tol["rect_side_tolerance_mm"],
                                  tol["rect_angle_tolerance_deg"])
            if role == "wall" or rect is None:
                start = end = None
                thick = None
                if rect is not None:
                    c, short, long_, rot = rect
                    thick = short
                    start, end = G.long_axis_of_rect(pts)
                res.walls.append(Wall(
                    mark="", thickness=thick, start=start, end=end, outline=pts,
                    source_layer=e_layer, handle=e_handle,
                    notes=_shape_notes(repaired, assembled)))
                if rect is None and role != "wall":
                    res.add(INFO, "geometry",
                            f"A closed {len(pts)}-point outline on '{e_layer}' is not a "
                            "rectangle. It has been carried through as a wall outline.",
                            layer=e_layer, x=pts[0][0], y=pts[0][1],
                            handle=e_handle)
                continue

            c, short, long_, rot = rect
            if short < tol["min_column_side_mm"]:
                slivers.append((pts, short, long_, e_layer, e_handle, c))
                continue
            if not (tol["min_column_side_mm"] <= short <= tol["max_column_side_mm"]):
                res.add(WARNING, "geometry",
                        f"A rectangle on '{e_layer}' measures {short:.0f} x {long_:.0f} mm, "
                        "which is outside the expected range for a column. Confirm what it is.",
                        layer=e_layer, x=c[0], y=c[1], handle=e_handle)
                continue

            res.columns.append(Column(
                mark="", kind=role if role != "column_stop" else "column_stop",
                x=c[0], y=c[1], width=round(short, 1), depth=round(long_, 1),
                rotation=round(rot, 3), outline=pts, source_layer=e_layer,
                handle=e_handle, repaired=repaired,
                notes=_shape_notes(repaired, assembled)))
            if blk is not None:
                from_block[id(res.columns[-1])] = blk
            dashed[id(res.columns[-1])] = _is_dashed(e_lt, e_layer, dashed_types, doc)

    _round_columns(msp, buckets, res, tol, dashed, from_block, dashed_types, doc)
    _one_column_per_stack(res, dashed, from_block, tol)
    for pts, short, long_, e_layer, e_handle, c in slivers:
        if _merge_sliver(pts, e_layer, e_handle, res, tol):
            continue
        res.add(WARNING, "geometry",
                f"A rectangle on '{e_layer}' measures {short:.0f} x {long_:.0f} mm, "
                "which is outside the expected range for a column. Confirm what it is.",
                layer=e_layer, x=c[0], y=c[1], handle=e_handle)
    _drop_block_copies(res, from_block, tol)


ROUND_SIDES = 32            # points on a round column's outline


def _round_columns(ents, buckets, res: Result, tol, dashed, from_block, dashed_types, doc) -> None:
    """A circle on a column layer, as wide as a column and with nothing written in it,
    is a round column (Akanistha C6: 650 across, its mark beside it). It was never
    read - only closed polylines were - and the floor lost those columns. A donut (a
    closed polyline of two half-circles) is the same. A circle with a mark written
    in it is a label bubble (VILLA5 draws its slab marks in circles on the column
    layer), and one on the same centre inside a bigger one is the inside of a tube
    (Sasangir steel columns: 420 outside, 400 in): neither is a column of its own.
    One standing inside a column already read is a detail of that column."""
    found = []
    for role in ("column", "column_stop", "column_float"):
        for e in buckets[role]:
            t = e.dxftype()
            if t == "CIRCLE":
                c, r = (e.dxf.center.x, e.dxf.center.y), float(e.dxf.radius)
            elif t == "LWPOLYLINE" and e.closed and len(e) == 2:
                (x0, y0, _s, _e, b0), (x1, y1, _s2, _e2, b1) = e.get_points("xyseb")
                if abs(abs(b0) - 1.0) > 0.01 or abs(abs(b1) - 1.0) > 0.01:
                    continue
                w = float(e.dxf.get("const_width", 0.0) or 0.0)
                c, r = ((x0 + x1) / 2.0, (y0 + y1) / 2.0), (G.dist((x0, y0), (x1, y1)) + w) / 2.0
            else:
                continue
            if r > 0:
                found.append((r, c, role, e))
    if not found:
        return

    from ezdxf import bbox as _bbox
    texts = []
    for e in ents:
        if e.dxftype() not in ("TEXT", "MTEXT") or not e.dxf.hasattr("insert"):
            continue
        ip = (e.dxf.insert.x, e.dxf.insert.y)
        if not any(G.dist(ip, f[1]) <= f[0] + 3000.0 for f in found):
            continue
        try:
            s = (e.dxf.text if e.dxftype() == "TEXT" else e.plain_text()).strip()
        except Exception:
            continue
        if not s:
            continue
        pts = [ip]
        try:
            b = _bbox.extents([e], fast=True)
            if b.has_data:
                pts.append(((b.extmin.x + b.extmax.x) / 2.0, (b.extmin.y + b.extmax.y) / 2.0))
        except Exception:
            pass
        texts += pts

    from shapely.geometry import Point as _Pt, Polygon
    drawn = []
    for col in res.columns:
        try:
            drawn.append(Polygon(col.outline).buffer(0))
        except Exception:
            continue
    for w in res.walls:
        try:
            drawn.append(Polygon(w.outline).buffer(0))
        except Exception:
            continue

    lo, hi = tol["min_column_side_mm"], tol["max_column_side_mm"]
    same = max(tol["duplicate_tolerance_mm"], 5.0)
    kept, bubbles, inside, odd = [], 0, 0, 0
    for r, c, role, e in sorted(found, key=lambda f: -f[0]):
        d = 2.0 * r
        outer = next((k for k in kept if G.dist(k[1], c) <= same), None)
        if outer is not None:
            if outer[0] - r > same:
                col = outer[2]
                col.notes = list(col.notes) + [f"drawn as a tube: {d:.0f} across inside"]
            continue
        if any(G.dist(p, c) <= r for p in texts):
            bubbles += 1
            continue
        if not (lo <= d <= hi):
            odd += 1
            continue
        if any(p.contains(_Pt(c)) for p in drawn):
            inside += 1
            continue
        lay = e.dxf.layer
        col = Column(
            mark="", kind=role, x=c[0], y=c[1], width=round(d, 1), depth=round(d, 1),
            rotation=0.0, source_layer=lay, handle=e.dxf.handle,
            outline=[(c[0] + r * math.cos(2 * math.pi * k / ROUND_SIDES),
                      c[1] + r * math.sin(2 * math.pi * k / ROUND_SIDES)) for k in range(ROUND_SIDES)],
            notes=[f"round, {d:.0f} across"], diameter=round(d, 1))
        res.columns.append(col)
        kept.append((r, c, col))
        blk = blocks.block_of(e)
        if blk is not None:
            from_block[id(col)] = blk
        lt = (e.dxf.get("linetype", "BYLAYER") or "BYLAYER").upper()
        dashed[id(col)] = _is_dashed(lt, lay, dashed_types, doc)

    if kept:
        sizes = collections.Counter(f"{k[2].diameter:.0f}" for k in kept)
        res.add(INFO, "geometry",
                f"{len(kept)} round columns are drawn as circles on the column layers ("
                + ", ".join(f"{n} x {s} across" for s, n in sizes.most_common()) + "). Each is "
                "built as a round column of that size.")
    if bubbles:
        res.add(INFO, "geometry",
                f"{bubbles} circles on the column layers have a mark written inside them. "
                "They are label bubbles, not columns, and have been left alone.")
    if inside:
        res.add(INFO, "geometry",
                f"{inside} circles on the column layers stand inside a column or wall already "
                "drawn. They are read as part of that element, not as columns of their own.")
    if odd:
        res.add(INFO, "geometry",
                f"{odd} circles on the column layers are too small or too big for a column "
                f"({lo:.0f} to {hi:.0f} mm across). They have been left alone.")


def _dashed_linetypes(doc) -> set:
    """Names of the linetypes whose pattern has gaps, whatever they are called."""
    out = set()
    for lt in (doc.linetypes if doc is not None else ()):
        try:
            if any(t.code == 49 and float(t.value) < 0 for t in lt.pattern_tags.tags):
                out.add(lt.dxf.name.upper())
        except Exception:
            continue
    return out


def _is_dashed(lt: str, layer: str, dashed_types: set, doc) -> bool:
    if lt == "BYLAYER" and doc is not None:
        try:
            lt = (doc.layers.get(layer).dxf.linetype or "CONTINUOUS").upper()
        except Exception:
            lt = "CONTINUOUS"
    return lt in dashed_types


def _one_column_per_stack(res: Result, dashed: dict, from_block: dict, tol) -> None:
    """A column drawn twice in one place, dashed and solid, is one column at two
    storeys. On a framing plan the column below the floor is hidden by the slab and
    drawn dashed; the column above is cut and drawn solid (smaller, as columns step
    down going up). ALCAZAR 1st floor draws all 66 so - dashed 300 x 700, solid
    230 x 700 - and both were built, side by side.

    The plan's columns are the ones holding its floor up (they run from the level
    below up to it), so the dashed one is kept and the solid one's size noted on it;
    the floor above draws that one as its own. ALCAZAR keeps the two layouts in two
    blocks (columns below, 'COL-1' above). Only outlines both from blocks or both on
    the plan pair: a block's copy under the plan's own is _drop_block_copies' business."""
    from shapely.geometry import Polygon
    dash = [c for c in res.columns if dashed.get(id(c))]
    if not dash:
        return
    solid = [c for c in res.columns if not dashed.get(id(c))]
    polys = {}

    def poly(c):
        if id(c) not in polys:
            try:
                p = Polygon(c.outline).buffer(0)
                polys[id(c)] = p if p.area > 0 else None
            except Exception:
                polys[id(c)] = None
        return polys[id(c)]

    drop, sizes = set(), collections.Counter()
    for d in dash:
        pd = poly(d)
        if pd is None:
            continue
        for s in solid:
            if id(s) in drop or (id(s) in from_block) != (id(d) in from_block) or \
                    abs(s.x - d.x) > 3000 or abs(s.y - d.y) > 3000:
                continue
            ps = poly(s)
            if ps is None or pd.intersection(ps).area < 0.5 * min(pd.area, ps.area):
                continue
            drop.add(id(s))
            d.notes = list(d.notes) + [f"drawn dashed: the column below this floor; the column "
                                       f"above is drawn solid on it, {s.width:.0f} x {s.depth:.0f}"]
            sizes[(f"{d.width:.0f} x {d.depth:.0f}", f"{s.width:.0f} x {s.depth:.0f}")] += 1
            break
    if not drop:
        return
    res.columns = [c for c in res.columns if id(c) not in drop]
    said = "; ".join(f"{n} x {a} below / {b} above" for (a, b), n in sizes.most_common(4))
    res.add(INFO, "geometry",
            f"{len(drop)} columns are drawn twice, dashed and solid in the same place: the "
            "column below this floor (hidden under the slab) and the column above it. Each "
            f"is built once, as the column holding this floor up ({said}).")


def _drop_block_copies(res: Result, from_block: dict, tol) -> None:
    """A copy of the column layout in a block under the plan (ALCAZAR plinth: the
    'Grid 5' block holds the grids and the columns at 230 x 1050, inserted 1:1;
    the plan draws its own hatched columns, 400 x 1050, over them) made every
    column twice, the copy taking the mark. Where a block's column lies on one
    drawn on the plan itself, the plan's own is kept and the copy's size noted on
    it. A block whose columns mostly lie on the plan's own is a copy: what only it
    has is not drawn on this plan, so it is left out and said, not built.
    A block that is the only drawing of the columns is read as before."""
    if not from_block:
        return
    from shapely.geometry import Polygon
    own = [c for c in res.columns if id(c) not in from_block]
    if not own:
        return
    t = tol["rect_side_tolerance_mm"]
    polys = {}

    def poly(c):
        if id(c) not in polys:
            try:
                p = Polygon(c.outline).buffer(0)
                polys[id(c)] = p if p.area > 0 else None
            except Exception:
                polys[id(c)] = None
        return polys[id(c)]

    copies, alone = collections.defaultdict(list), collections.defaultdict(list)
    for c in res.columns:
        name = from_block.get(id(c))
        if name is None:
            continue
        pc, hit = poly(c), None
        for o in own if pc is not None else ():
            if abs(o.x - c.x) > 5000 or abs(o.y - c.y) > 5000:
                continue
            po = poly(o)
            if po is not None and pc.intersection(po).area >= 0.5 * min(pc.area, po.area):
                hit = o
                break
        (copies[name].append((c, hit)) if hit is not None else alone[name].append(c))

    drop = set()
    for name, pairs in copies.items():
        drop |= {id(c) for c, _ in pairs}
        sizes = collections.Counter()
        for c, o in pairs:
            if abs(c.width - o.width) > t or abs(c.depth - o.depth) > t:
                o.notes = list(o.notes) + [f"block '{name}' under the plan draws it "
                                           f"{c.width:.0f} x {c.depth:.0f}"]
                sizes[(f"{o.width:.0f} x {o.depth:.0f}", f"{c.width:.0f} x {c.depth:.0f}")] += 1
        said = "; ".join(f"{n} drawn {a} on the plan, {b} in the block"
                         for (a, b), n in sorted(sizes.items(), key=lambda kv: -kv[1]))
        res.add(WARNING if sizes else INFO, "geometry",
                f"Block '{name}' (inserted 1:1 under the plan) holds a copy of {len(pairs)} "
                "columns the plan draws itself. Each is built once, from the plan's own "
                "outline" + (f". The sizes differ - {said}: check which is right." if sizes
                             else "."))
        rest = alone.get(name, [])
        if rest and len(pairs) >= len(rest):
            drop |= {id(c) for c in rest}
            for c in rest:
                res.add(WARNING, "geometry",
                        f"A {c.width:.0f} x {c.depth:.0f} column is only in block '{name}', "
                        "a copy of the column layout; it is not drawn on the plan, so it has "
                        "been left out. If it belongs on this floor, draw it on the plan.",
                        layer=c.source_layer, x=c.x, y=c.y)
    if drop:
        res.columns = [c for c in res.columns if id(c) not in drop]


def _merge_sliver(pts, layer, handle, res: Result, tol) -> bool:
    """A strip too thin for a column, sharing one whole side with a column on its
    layer, is part of that column drawn in two pieces (New block: 70x600 + 230x600
    = the 300x600 column the 300-wide beams on that line sit on). Merges it."""
    t = tol["rect_side_tolerance_mm"]

    def same_edge(e1, e2):
        return (G.dist(e1[0], e2[0]) <= t and G.dist(e1[1], e2[1]) <= t) or \
               (G.dist(e1[0], e2[1]) <= t and G.dist(e1[1], e2[0]) <= t)

    for col in res.columns:
        if col.source_layer != layer or len(col.outline) != 4:
            continue
        for i in range(4):
            s_edge = (pts[i], pts[(i + 1) % 4])
            for j in range(4):
                c_edge = (col.outline[j], col.outline[(j + 1) % 4])
                if not same_edge(s_edge, c_edge):
                    continue
                far = [pts[(i + 2) % 4], pts[(i + 3) % 4],
                       col.outline[(j + 2) % 4], col.outline[(j + 3) % 4]]
                cx = sum(p[0] for p in far) / 4.0
                cy = sum(p[1] for p in far) / 4.0
                far.sort(key=lambda p: math.atan2(p[1] - cy, p[0] - cx))
                rect = G.rectangle_of(far, t, tol["rect_angle_tolerance_deg"])
                if rect is None or rect[1] > tol["max_column_side_mm"]:
                    return False
                (x, y), short, long_, rot = rect
                old = f"{col.width:.0f} x {col.depth:.0f}"
                col.x, col.y = x, y
                col.width, col.depth = round(short, 1), round(long_, 1)
                col.rotation = round(rot, 3)
                col.outline = far
                col.notes = list(col.notes) + [f"merged with the strip drawn against it ({handle})"]
                res.add(INFO, "geometry",
                        f"A {old} column on '{layer}' has a thin strip drawn against one whole "
                        f"side; together they are one {short:.0f} x {long_:.0f} column, built as one.",
                        layer=layer, x=x, y=y, handle=col.handle)
                return True
    return False


def _extract_beams(doc, buckets, res: Result, cfg, tol) -> None:
    seen: list[tuple] = []
    dup_layers: dict[str, int] = {}

    for role in ("beam", "girder"):
        default_w = cfg["defaults"]["beam_width_mm" if role == "beam" else "girder_width_mm"]
        shorts: list[tuple] = []            # pieces too short to be a beam: maybe a curve
        for e in buckets[role]:
            t = e.dxftype()
            segs: list[tuple[G.Point, G.Point, float | None, str, str, float]] = []

            if t == "MLINE":
                segs = _mline_centreline(e, res)
            elif t == "LINE":
                a = (e.dxf.start.x, e.dxf.start.y)
                b = (e.dxf.end.x, e.dxf.end.y)
                segs = [(a, b, None, "schedule-required", "as-drawn", 0.0)]
            elif t == "LWPOLYLINE":
                pts = _pts_of_lwpolyline(e)
                bulges = [p[4] for p in e.get_points("xyseb")]
                for i in range(len(pts) - 1):
                    if abs(bulges[i]) > 1e-6:
                        # A curved segment. Taken as a straight chord it cuts the
                        # corner; it goes with the arcs instead.
                        _note_arc(res, role, e, *_bulge_arc(pts[i], pts[i + 1], bulges[i]))
                        continue
                    segs.append((pts[i], pts[i + 1], None, "schedule-required", "as-drawn", 0.0))
                # A beam drawn as a closed rectangle is one beam: its centreline down
                # the middle, its short side the width. Read side by side it lost its
                # closing side - Wing B 1st floor GB7 sat on one face, 750 mm off.
                rect = _beam_rectangle(pts, bulges, bool(e.closed))
                if rect:
                    segs = [(rect[0], rect[1], rect[2], "outline", "outline", 0.0)]
            elif t == "ARC":
                c = e.dxf.center
                _note_arc(res, role, e, (c.x, c.y), float(e.dxf.radius),
                          math.radians(e.dxf.start_angle), math.radians(e.dxf.end_angle))
                continue
            else:
                continue

            for a, b, width, wsrc, csrc, off in segs:
                # A short piece is a stray line - unless it is a multiline at least as long as
                # it is wide: both faces drawn, a beam. ALCAZAR 1st floor BC17, 256 x 230 under
                # column 64, closes the corner with B63; dropped, it left the corner open and
                # the slab leaking round it.
                if G.dist(a, b) < tol["beam_min_length_mm"] and not (
                        csrc == "multiline-faces" and width and G.dist(a, b) >= width):
                    if csrc == "as-drawn":
                        shorts.append((a, b, e))
                    continue
                if any(G.segments_equal((a, b), s, tol["duplicate_tolerance_mm"]) for s in seen):
                    dup_layers[e.dxf.layer] = dup_layers.get(e.dxf.layer, 0) + 1
                    continue
                seen.append((a, b))

                notes = []
                if csrc == "as-drawn":
                    notes.append("drawn as a plain line: centreline assumed, width from schedule")
                    # the line as drawn, before joining stretches it (_drop_opening_outlines)
                    res.stats.setdefault("_drawn_lines", {}).setdefault(
                        e.dxf.handle, []).append((a, b))
                res.beams.append(Beam(
                    mark="", kind=role, start=a, end=b,
                    width=width if width else None,
                    width_source=wsrc if width else "schedule-required",
                    centreline_source=csrc, offset_applied=round(off, 1),
                    source_layer=e.dxf.layer, handle=e.dxf.handle, notes=notes))

        _arcs_from_short_lines(res, role, shorts)
        # kept for the straight ends of curved beams (_curve_end_pieces)
        res.stats.setdefault("_short_lines", []).extend((role, a, b) for a, b, _e in shorts)

    for layer, n in dup_layers.items():
        res.add(WARNING, "geometry",
                f"{n} lines on '{layer}' are drawn twice in the same place. The duplicates "
                "have been dropped so they do not become double beams in Revit. Run "
                "OVERKILL before issuing.", layer=layer)

    n_assumed = sum(1 for b in res.beams if b.centreline_source == "as-drawn")
    if n_assumed:
        res.add(WARNING, "geometry",
                f"{n_assumed} beam or girder segments are drawn as plain lines or "
                "polylines rather than multilines. Their width is unknown and their "
                "line has been taken as the centreline without checking.")


def _bulge_arc(p, q, bulge):
    """The arc of a curved polyline segment: (centre, radius, start, end), ccw."""
    from ezdxf.math import bulge_to_arc
    c, a0, a1, r = bulge_to_arc(p, q, bulge)
    return (c[0], c[1]), float(r), float(a0), float(a1)


def _note_arc(res: Result, role, e, centre, r, a0, a1) -> None:
    """Keep a curved beam face aside until the straight beams are settled.

    Every step that merges, joins, splits and tags beams works on straight lines.
    Curves are kept out of all of it and turned into beams at the very end, so
    adding them cannot change a single straight beam.
    """
    span = (a1 - a0) % (2 * math.pi) or 2 * math.pi
    res.stats.setdefault("_arcs", []).append({
        "role": role, "c": (float(centre[0]), float(centre[1])), "r": float(r),
        "a0": a0 % (2 * math.pi), "span": span,
        "layer": e.dxf.layer, "handle": e.dxf.handle})


def _circle_3p(p, q, s):
    """Centre and radius of the circle through three points, or None if in line."""
    ax, ay = p; bx, by = q; cx, cy = s
    d = 2.0 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-9:
        return None
    a2, b2, c2 = ax * ax + ay * ay, bx * bx + by * by, cx * cx + cy * cy
    ux = (a2 * (by - cy) + b2 * (cy - ay) + c2 * (ay - by)) / d
    uy = (a2 * (cx - bx) + b2 * (ax - cx) + c2 * (bx - ax)) / d
    return (ux, uy), math.hypot(ax - ux, ay - uy)


def _arcs_from_short_lines(res: Result, role, shorts) -> None:
    """A curve drawn as a chain of short straight lines is an arc.

    The Wing B 2nd floor balcony beams are drawn that way: each face is a dozen
    lines of about 100 mm. Every one is shorter than a beam can be, so they were
    all dropped as scraps and the curved beams vanished. A chain of three or
    more that follows one circle, turning the same way at every joint, is taken
    as the arc it is drawn to be. Anything else is dropped as before.
    """
    if len(shorts) < 3:
        return
    tol_join = 5.0

    def near(p, q):
        return abs(p[0] - q[0]) <= tol_join and abs(p[1] - q[1]) <= tol_join

    left = list(range(len(shorts)))
    while left:
        i = left.pop(0)
        a, b, e = shorts[i]
        chain = [a, b]
        grown = True
        while grown:
            grown = False
            for k in list(left):
                p, q, _ = shorts[k]
                for x, y in ((p, q), (q, p)):
                    if near(x, chain[-1]):
                        chain.append(y)
                    elif near(y, chain[0]):
                        chain.insert(0, x)
                    else:
                        continue
                    left.remove(k)
                    grown = True
                    break
        if len(chain) < 4:                 # three segments at least
            continue
        fit = _circle_3p(chain[0], chain[len(chain) // 2], chain[-1])
        if fit is None:
            continue
        c, r = fit
        if not (150.0 <= r <= 20000.0):
            continue
        if any(abs(G.dist(p, c) - r) > max(15.0, 0.02 * r) for p in chain):
            continue
        turns = [(chain[k][0] - chain[k - 1][0]) * (chain[k + 1][1] - chain[k][1]) -
                 (chain[k][1] - chain[k - 1][1]) * (chain[k + 1][0] - chain[k][0])
                 for k in range(1, len(chain) - 1)]
        if not (all(t > 0 for t in turns) or all(t < 0 for t in turns)):
            continue
        # A curve bends a little at every joint; a box turns a corner. Three sides of
        # a small rectangle lie on one circle and turn the same way, so the squared-off
        # end of a beam (J1153 Wing C 3rd floor: B30 at B87, the B101A and B107A ends)
        # was built as a 230 mm "curved beam" across it.
        if any(_turn_deg(chain[k - 1], chain[k], chain[k + 1]) > 60.0
               for k in range(1, len(chain) - 1)):
            continue
        ang = lambda p: math.atan2(p[1] - c[1], p[0] - c[0])
        first, last = (chain[0], chain[-1]) if turns[0] > 0 else (chain[-1], chain[0])
        a0 = ang(first)
        span = (ang(last) - a0) % (2 * math.pi)
        _note_arc(res, role, e, c, r, a0, a0 + span)


def _turn_deg(p, q, s) -> float:
    """How far a path p -> q -> s turns at q, in degrees (0 = straight on)."""
    u = (q[0] - p[0], q[1] - p[1])
    v = (s[0] - q[0], s[1] - q[1])
    return abs(math.degrees(math.atan2(u[0] * v[1] - u[1] * v[0], u[0] * v[0] + u[1] * v[1])))


def _arc_point(c, r, a):
    return (c[0] + r * math.cos(a), c[1] + r * math.sin(a))


def _add_curved_beams(res: Result, tol, beam_tags=()) -> None:
    """Curved beams from the arcs on the beam layers.

    Until now an ARC on a beam layer was skipped without a word, and a curved
    polyline segment was taken as its straight chord, so the curved beams at the
    balconies on the Wing B 2nd floor were not in the model at all. A curved beam
    is drawn like a straight one, as its two faces: two arcs on one centre, a
    beam width apart. They become one beam on the arc between them.

    Most curves carry no mark; they are the end of the straight beam they run
    into and are sized as it (_size_curved_beams). One that does carry a mark
    (MLB2 on Wing C) takes it from the marks no straight beam claimed.
    """
    arcs = res.stats.pop("_arcs", [])
    short_lines = res.stats.pop("_short_lines", [])
    if not arcs:
        return
    min_len = tol["beam_min_length_mm"]
    limit = tol.get("tag_assign_limit_mm", 700.0)
    straight = [b for b in res.beams if b.mid is None]

    def overlap(a, b):
        """Shared angular range of two arcs on one centre, as (start, span)."""
        best = (0.0, 0.0)
        for shift in (-2 * math.pi, 0.0, 2 * math.pi):
            s = max(a["a0"], b["a0"] + shift)
            e = min(a["a0"] + a["span"], b["a0"] + shift + b["span"])
            if e - s > best[1]:
                best = (s, e - s)
        return best

    # faces: two arcs on the same centre, a plausible beam width apart
    pairs = []
    for i in range(len(arcs)):
        for j in range(i + 1, len(arcs)):
            a, b = arcs[i], arcs[j]
            if a["role"] != b["role"] or G.dist(a["c"], b["c"]) > 50.0:
                continue
            gap = abs(a["r"] - b["r"])
            if not (100.0 <= gap <= 800.0):
                continue
            s, span = overlap(a, b)
            if span < 0.5 * min(a["span"], b["span"]):
                continue
            pairs.append((gap, i, j, s, span))
    pairs.sort()

    used, curves = set(), []
    paired = {}                              # arc -> the angular ranges it is a face of
    for gap, i, j, s, span in pairs:
        if i in used or j in used:
            continue
        used |= {i, j}
        paired.setdefault(i, []).append((s, span))
        paired.setdefault(j, []).append((s, span))
        a, b = arcs[i], arcs[j]
        curves.append((a["role"], ((a["c"][0] + b["c"][0]) / 2, (a["c"][1] + b["c"][1]) / 2),
                       (a["r"] + b["r"]) / 2, s, span, round(gap, 1), a["layer"], a["handle"]))
    # A face broken where another beam meets it (Akanistha Winter Villa: a curve's inner
    # face in two arcs, its outer face one): the whole face paired with one piece, and the
    # other piece, finding it taken, was built on itself - half a beam width off its
    # centreline. A piece left over takes the face it runs beside over the stretch they
    # share, unless that stretch is already paired (a face drawn twice).
    for gap, i, j, s, span in pairs:
        for k, o in ((i, j), (j, i)):
            if k in used or o not in used:
                continue
            if any(overlap({"a0": s, "span": span}, {"a0": s2, "span": sp2})[1] > 0.1 * span
                   for s2, sp2 in paired.get(o, [])):
                continue
            used.add(k)
            paired.setdefault(k, []).append((s, span))
            paired[o].append((s, span))
            a, b = arcs[k], arcs[o]
            # the width as the faces stand apart there, not their radii apart: J1153 Wing C
            # 2nd floor BC7A's outer face is two arcs on centres 40 mm off the inner one's
            # (r 647 and 708 round r 438) - 270 by radii, 231 across, 230 at its straight end
            p = _arc_point(a["c"], a["r"], s + span / 2.0)
            across = abs(G.dist(p, b["c"]) - b["r"])
            curves.append((a["role"], ((a["c"][0] + b["c"][0]) / 2, (a["c"][1] + b["c"][1]) / 2),
                           (a["r"] + b["r"]) / 2, s, span, round(across, 1), a["layer"], a["handle"]))
    curves, joints = _join_curve_pieces(res, curves, arcs, overlap)
    for k, a in enumerate(arcs):
        if k not in used:
            curves.append((a["role"], a["c"], a["r"], a["a0"], a["span"], None,
                           a["layer"], a["handle"]))

    def nearest_end(p):
        best = None
        for sb in straight:
            for q in (sb.start, sb.end):
                d = G.dist(p, q)
                if best is None or d < best[0]:
                    best = (d, sb)
        return best

    def same_curve(a, b):
        # A curve drawn twice (Wing C has several) must not become two beams.
        return (G.dist(a.mid, b.mid) <= 20.0 and
                ((G.dist(a.start, b.start) <= 20.0 and G.dist(a.end, b.end) <= 20.0) or
                 (G.dist(a.start, b.end) <= 20.0 and G.dist(a.end, b.start) <= 20.0)))

    def on_wall(p):
        # a curve ending on a wall is carried by it, as one ending on a column (Akanistha
        # Winter Villa 2nd floor: the curved edge beam B1 runs on past shear wall SW16 - an
        # outline that follows the curve, so not a rectangle, so read as a wall - to a
        # free end; it touched no beam or column and was left out)
        for w in res.walls:
            ol = w.outline or []
            if len(ol) >= 3 and (G.point_in_polygon(p, ol) or
                                 min(G.point_to_segment(p, ol[i - 1], ol[i]) for i in range(len(ol))) <= 300.0):
                return True
        return False

    hosts = res.stats.setdefault("_curve_hosts", {})
    made: list[tuple] = []                  # (beam, centre, radius, a0, span)
    stray = repeats = 0
    for cv in curves:
        role, c, r, a0, span, width, layer, handle = cv
        if r * span < min_len:
            continue
        p0, p1 = _arc_point(c, r, a0), _arc_point(c, r, a0 + span)
        # A curve that meets no beam, column or wall is not part of the frame: an
        # arc on the beam layer in a section or a detail. Leave it out. A curve joined
        # from pieces is judged on its pieces' ends too: they met the beams coming in.
        ends = [p0, p1] + list(joints.get(id(cv), ()))
        near = [n for n in (nearest_end(p0), nearest_end(p1)) if n]
        touches = any(n[0] <= 800.0 for n in near) or any(
            n and n[0] <= 800.0 for n in (nearest_end(p) for p in ends[2:])) or any(
            G.dist(p, (col.x, col.y)) <= max(col.width, col.depth) for p in ends
            for col in res.columns) or any(on_wall(p) for p in ends)
        if not touches:
            stray += 1
            continue

        host = None
        if near and min(n[0] for n in near) <= 800.0:
            host = min(near, key=lambda n: n[0])[1]
        if width is not None and host is not None and host.width \
                and abs(width - host.width) <= 25.0:
            width = host.width            # drafting slack, as for straight beams
        b = Beam(
            mark="", kind=role, start=p0, end=p1, width=width,
            width_source="measured between the drawn faces" if width else "schedule-required",
            centreline_source="arc faces" if width else "as-drawn",
            offset_applied=0.0, source_layer=layer, handle=handle,
            notes=["curved beam, built as an arc through its ends and midpoint"],
            mid=_arc_point(c, r, a0 + span / 2))
        if any(same_curve(b, m[0]) for m in made):
            repeats += 1
            continue
        made.append((b, c, r, a0, span))
        if host is not None:
            hosts[id(b)] = host

    # marks no straight beam took, nearest first, to the curve they sit on
    taken = {x.mark for x in res.beams if x.mark}

    def to_arc(p, c, r, a0, span, w):
        ang = (math.atan2(p[1] - c[1], p[0] - c[0]) - a0) % (2 * math.pi)
        if ang <= span:
            d = abs(G.dist(p, c) - r)
        else:
            d = min(G.dist(p, _arc_point(c, r, a0)), G.dist(p, _arc_point(c, r, a0 + span)))
        return max(0.0, d - (w or 0.0) / 2.0)

    cand = []
    for ti, (mark, p, h, lay) in enumerate(beam_tags):
        if not mark or mark in taken:
            continue
        for k, (b, c, r, a0, span) in enumerate(made):
            d = to_arc(p, c, r, a0, span, b.width)
            if d <= limit:
                cand.append((d, ti, k))
    cand.sort()
    got_tag, got_curve = set(), set()
    for d, ti, k in cand:
        mark = beam_tags[ti][0]
        if ti in got_tag or k in got_curve or mark in taken:
            continue
        got_tag.add(ti); got_curve.add(k); taken.add(mark)
        b = made[k][0]
        b.mark, b.tag_distance = mark, round(d, 1)
        # it was reported as a mark with no beam; it has one now
        res.issues[:] = [i for i in res.issues if not (
            i.category == "tags" and i.mark == mark and "was not placed" in i.message)]

    for b, *_ in made:
        res.beams.append(b)
    _hand_ends_to_curves(res, made, straight)
    ends = _curve_end_pieces(made, straight, short_lines, hosts)
    ends += _curve_ends_to_supports(res, made, ends, hosts, tol)
    res.beams.extend(ends)
    if made:
        res.add(INFO, "geometry",
                f"{len(made)} curved beams were built from arcs on the beam layers, each as "
                "one beam on the arc midway between its two drawn faces. Unmarked ones are "
                "sized as the straight beam they continue.")
    if repeats:
        res.add(WARNING, "geometry",
                f"{repeats} curved beams are drawn twice in the same place; the repeats "
                "have been dropped. Run OVERKILL before issuing.")
    if stray:
        res.add(WARNING, "geometry",
                f"{stray} arcs on the beam layers touch no beam, column or wall and were left "
                "out. If one is a real curved beam, draw it meeting its supports.")


def _hand_ends_to_curves(res: Result, made, straight) -> None:
    """A straight beam carried on past where it turns into a curve: back to the curve.

    J1153 Wing C 3rd floor, B61: both faces stop 400 mm short of BC6 and carry on as two
    arcs bending into BC6's face. The straight beams are carried to their supports before
    the curves are read, so B61 was walked on straight up to BC6 through the corner, and
    the curve was built beside it from B61's drawn end: two beams where the drawing has one
    that bends.

    Narrow on purpose: a curve drawn as two faces, as wide as the straight beam (25 mm),
    starting on its centreline (15 mm) inside its length and running on along it (20 deg: B61's
    arcs leave its faces 8-10 deg off, a flare);
    the straight beam's own drawn faces end there (30 mm). The stretch past it was only
    carried, never drawn: it goes, and the curve carries the beam on."""
    cut = []
    for b, c, r, a0, span in made:
        if not b.width:
            continue
        for a, p, sgn in ((a0, b.start, -1.0), (a0 + span, b.end, 1.0)):
            # into the curve, along its tangent at this end
            tx, ty = math.sin(a) * sgn, -math.cos(a) * sgn
            for k in straight:
                fe = getattr(k, "_face_ends", None)
                L = k.length
                if not fe or not k.width or abs(k.width - b.width) > 25.0 or L < 1:
                    continue
                ux, uy = (k.end[0] - k.start[0]) / L, (k.end[1] - k.start[1]) / L
                tp = (p[0] - k.start[0]) * ux + (p[1] - k.start[1]) * uy
                if abs(-(p[0] - k.start[0]) * uy + (p[1] - k.start[1]) * ux) > 15.0 or not 50.0 < tp < L - 50.0:
                    continue
                dot = tx * ux + ty * uy
                if abs(dot) < math.cos(math.radians(20.0)):
                    continue
                ts = [(q[0] - k.start[0]) * ux + (q[1] - k.start[1]) * uy for q in fe]
                q = (k.start[0] + ux * tp, k.start[1] + uy * tp)
                if dot > 0 and max(ts) <= tp + 30.0:
                    gone, k.end = L - tp, q
                elif dot < 0 and min(ts) >= tp - 30.0:
                    gone, k.start = tp, q
                else:
                    continue
                k.notes.append(f"ends where it turns into the curved beam its faces become; the {gone:.0f} mm "
                               "it had been carried on past that, never drawn, was taken off")
                cut.append(k)
    if cut:
        m = cut[0].end
        res.add(INFO, "geometry",
                f"{len(cut)} straight beams turn into a curved beam where their faces end. They had been carried "
                "on straight past that to their support; they now end where the curve begins, and the curve "
                "carries the beam on.", x=m[0], y=m[1])


def _join_curve_pieces(res: Result, curves, arcs, overlap) -> list:
    """One curved beam, not a row of pieces with gaps between them.

    A curve's face is broken wherever another beam comes into it, and each piece of
    it makes a piece of beam with the face opposite; between them the beam stopped
    (Akanistha Winter Villa: the first floor's curved edge beams in two to four pieces,
    a gap at every beam coming in). Where the other face runs on across the gap, the
    beam does too, as it does for a straight beam (_bridge_staggered_faces): two
    pieces on one circle, as wide, at most 1.5 m apart, the gap covered by one face
    arc of theirs and no column or wall in it. A curve of two radii (J1153 Wing C
    BC7A: 647 and 708 round 438) is two circles and is left as it is."""
    def supported(pts):
        for o in list(res.columns) + list(res.walls):
            ol = getattr(o, "outline", None) or []
            if len(ol) >= 3 and any(G.point_in_polygon(p, ol) or
                                    min(G.point_to_segment(p, ol[i - 1], ol[i]) for i in range(len(ol))) <= 30.0
                                    for p in pts):
                return True
        return False

    def covered(c, r, w, g0, d):
        """one face of theirs runs the whole gap - as one arc, or arcs end to end"""
        if d <= 0.0:
            return True
        tol = 5.0 / max(r, 1.0)                        # 5 mm along the curve
        for side in (1.0, -1.0):
            ivs = []
            for a in arcs:
                if G.dist(a["c"], c) <= 50.0 and abs((a["r"] - r) * side - w / 2.0) <= 30.0:
                    s0 = ((a["a0"] - g0 + math.pi) % (2 * math.pi)) - math.pi
                    ivs.append((s0, s0 + a["span"]))
            reach = None
            for s0, s1 in sorted(ivs):
                if s0 <= tol and s1 > 0.0 and reach is None:
                    reach = s1
                elif reach is not None and s0 <= reach + tol:
                    reach = max(reach, s1)
            if reach is not None and reach >= d - tol:
                return True
        return False

    out = list(curves)
    joints = {}                     # id(joined curve) -> where its pieces ended
    joined = 0
    changed = True
    while changed:
        changed = False
        for i, A in enumerate(out):
            for j, B in enumerate(out):
                if i == j or A[5] is None or B[5] is None or A[0] != B[0]:
                    continue
                if G.dist(A[1], B[1]) > 30.0 or abs(A[2] - B[2]) > 20.0 or abs(A[5] - B[5]) > 25.0:
                    continue
                end = A[3] + A[4]
                d = ((B[3] - end + math.pi) % (2 * math.pi)) - math.pi      # B starts d after A ends
                if d < -0.5 * min(A[4], B[4]) or d * A[2] > 1500.0:
                    continue
                c, r, w = A[1], (A[2] + B[2]) / 2.0, max(A[5], B[5])
                if not covered(c, r, w, end, d):
                    continue
                if d > 0.0 and supported([_arc_point(c, r, end + d * f) for f in (0.0, 0.5, 1.0)]):
                    continue
                span = max(A[4], A[4] + d + B[4])
                if span >= 2 * math.pi - 1e-3:
                    continue
                keep = A if A[4] * A[2] >= B[4] * B[2] else B
                new = (A[0], A[1], keep[2], A[3], span, keep[5], keep[6], keep[7])
                joints[id(new)] = (joints.pop(id(A), []) + joints.pop(id(B), []) +
                                   [_arc_point(A[1], A[2], end), _arc_point(B[1], B[2], B[3])])
                out[i] = new
                del out[j]
                joined += 1
                changed = True
                break
            if changed:
                break
    if joined:
        res.add(INFO, "geometry",
                f"{joined} gaps in curved beams were closed: a face broken where another beam comes in, "
                "the other face running on across it - one curved beam, not pieces.")
    return out, joints


def _curve_end_pieces(made, straight, short_lines, hosts) -> list:
    """The short straight end of a curved beam, drawn as its two faces.

    J1153 Wing C 3rd floor, B101A: both face polylines carry on straight for 220 mm
    past the curve into the face of B101 (a line across marks where the curve
    ends). Each leg is shorter than a beam can be, so both were dropped, the curve
    stopped 330 mm short of B101 and the slab bay beside it was left open.

    Both faces must be drawn, a leg from each face end of the curve, along its
    tangent, the same length. The piece is carried on to the centreline of the beam
    it runs into (300 mm at most). A curve that already ends on a beam gets none:
    its legs lie inside that beam.
    """
    out = []

    def on_beam(p, sb):
        return G.point_to_segment(p, sb.start, sb.end) <= (sb.width or 230.0) / 2.0 + 5.0

    for b, c, r, a0, span in made:
        if not b.width:
            continue
        w = b.width
        for a, p, sgn in ((a0, b.start, -1.0), (a0 + span, b.end, 1.0)):
            # outward along the tangent at this end
            t = (-math.sin(a) * sgn, math.cos(a) * sgn)
            n = (math.cos(a), math.sin(a))
            if any(on_beam(p, sb) for sb in straight):
                continue
            legs = []
            for side in (1.0, -1.0):
                f = (p[0] + side * n[0] * w / 2.0, p[1] + side * n[1] * w / 2.0)
                best = None
                for role, u, v in short_lines:
                    if role != b.kind:
                        continue
                    for q0, q1 in ((u, v), (v, u)):
                        if G.dist(q0, f) > 15.0:
                            continue
                        d = (q1[0] - q0[0], q1[1] - q0[1])
                        L = d[0] * t[0] + d[1] * t[1]
                        if L > 20.0 and abs(d[0] * t[1] - d[1] * t[0]) <= 0.09 * L:
                            best = L if best is None else max(best, L)
                legs.append(best)
            if None in legs or abs(legs[0] - legs[1]) > 15.0:
                continue
            L = (legs[0] + legs[1]) / 2.0
            e = (p[0] + t[0] * L, p[1] + t[1] * L)
            # on to the centreline of the beam it runs into
            reach = None
            for sb in straight:
                hit = G.line_intersection(e, (e[0] + t[0], e[1] + t[1]), sb.start, sb.end)
                if hit is None:
                    continue
                d = (hit[0] - e[0]) * t[0] + (hit[1] - e[1]) * t[1]
                if -1.0 <= d <= 300.0 and G.point_to_segment(hit, sb.start, sb.end) <= 5.0:
                    reach = d if reach is None else min(reach, d)
            if reach:
                e = (e[0] + t[0] * reach, e[1] + t[1] * reach)
            piece = Beam(
                mark="", kind=b.kind, start=p, end=e, width=w,
                width_source=b.width_source, centreline_source="face-pair",
                offset_applied=0.0, source_layer=b.source_layer, handle=b.handle,
                notes=["the straight end of a curved beam, drawn as its two faces"])
            piece._curve_end = True
            if id(b) in hosts:
                hosts[id(piece)] = hosts[id(b)]
            out.append(piece)
    return out


def _curve_ends_to_supports(res: Result, made, pieces, hosts, tol) -> list:
    """A curved beam's free end, carried along its tangent to what carries it.

    Sasan Gir terrace, B32 by the stair: both face arcs start where the curve turns,
    300 mm short of the column-wall it runs into - nothing drawn between - and the
    curve stopped there, short of its support. A straight beam's end is walked out
    along its own line to its support (_extend_beams_to_supports); this is the same
    for a curve, along its tangent.

    Only a curve drawn as two faces, at an end touching no beam, column or wall and
    with no straight end of its own drawn (_curve_end_pieces); the piece reaches the
    first column, wall or beam along the tangent within the straight beams' snap
    distance. Nothing met: left as it was.
    """
    snap = tol.get("beam_support_snap_mm", tol.get("beam_join_snap_mm", 800.0))
    supports = [o.outline for o in list(res.columns) + list(res.walls) if len(o.outline) >= 3]
    edges = [(o[i - 1], o[i]) for o in supports for i in range(len(o))]
    straight = [b for b in res.beams if b.mid is None]
    out = []
    for b, c, r, a0, span in made:
        if not b.width:
            continue
        for a, p, sgn in ((a0, b.start, -1.0), (a0 + span, b.end, 1.0)):
            if any(G.dist(x.start, p) <= 1.0 for x in pieces):
                continue                       # its drawn straight end
            # it rests on something already: its own concrete touches it (J1153 Wing C 3rd floor,
            # B61 flaring into BC6: the curve ends 180 mm off BC6's centreline; Akanistha's curves
            # end 30-90 mm short of a wall face - no piece for those)
            if any(G.point_to_segment(p, sb.start, sb.end) <= ((sb.width or 230.0) + b.width) / 2.0 + 5.0
                   for sb in straight) or \
                    any(G.point_in_polygon(p, o) or
                        min(G.point_to_segment(p, o[i - 1], o[i]) for i in range(len(o))) <= b.width / 2.0 + 5.0
                        for o in supports):
                continue
            dx, dy = -math.sin(a) * sgn, math.cos(a) * sgn
            stop = None
            for u0, u1 in edges + [(sb.start, sb.end) for sb in straight]:
                vx, vy = u1[0] - u0[0], u1[1] - u0[1]
                den = dx * vy - dy * vx
                if abs(den) < 1e-12:
                    continue
                wx, wy = u0[0] - p[0], u0[1] - p[1]
                t = (wx * vy - wy * vx) / den
                u = (wx * dy - wy * dx) / den
                if 1.0 < t <= snap and -1e-9 <= u <= 1.0 + 1e-9 and (stop is None or t < stop):
                    stop = t
            if stop is None:
                continue
            piece = Beam(
                mark="", kind=b.kind, start=p, end=(p[0] + dx * stop, p[1] + dy * stop), width=b.width,
                width_source=b.width_source, centreline_source="face-pair",
                offset_applied=0.0, source_layer=b.source_layer, handle=b.handle,
                notes=[f"the straight end of a curved beam, carried {stop:.0f} mm along its tangent "
                       "to its support (not drawn)"])
            piece._curve_end = True
            if id(b) in hosts:
                hosts[id(piece)] = hosts[id(b)]
            out.append(piece)
    if out:
        m = out[0].end
        res.add(INFO, "geometry",
                f"{len(out)} curved beams stopped short of the column, wall or beam they run into; "
                "each was carried on along its tangent to it.", x=m[0], y=m[1])
    return out


def _size_curved_beams(res: Result) -> None:
    """An unmarked curve takes the size of the straight beam it continues.

    Run after the schedule, so that beam's depth is the scheduled one.
    """
    hosts = res.stats.pop("_curve_hosts", {})
    for b in res.beams:
        host = hosts.get(id(b))
        if (b.mid is None and not getattr(b, "_curve_end", False)) or host is None:
            continue
        if b.width is None and host.width:
            b.width, b.width_source = host.width, f"same as {host.mark or 'the beam it continues'}"
        if b.depth is None and host.depth:
            b.depth = host.depth
            b.depth_source = f"same as {host.mark}" if host.mark else host.depth_source
        if host.mark and not b.mark:
            b.notes.append(f"the {'curved' if b.mid else 'straight'} end of {host.mark}, "
                           f"sized as {host.mark}")


def _mline_centreline(e, res: Result):
    """The true centreline of a multiline, from its two drawn faces.

    Taking the midpoint of the face lines removes the justification problem: it
    works whether the multiline is justified Top, Zero or Bottom, and it needs no
    guess about which side of the line the beam sits on.
    """
    just = e.dxf.justification          # 0 top, 1 zero, 2 bottom
    scale = float(e.dxf.scale_factor or 0.0)
    try:
        faces = [v for v in e.virtual_entities() if v.dxftype() == "LINE"]
    except Exception:
        faces = []

    locs = [(p.x, p.y) for p in e.get_locations()]
    out = []

    if len(faces) >= 2 and len(locs) > 2:
        # A multiline with a corner: ezdxf gives the faces segment by segment (both
        # faces of the first leg, then both of the next), so halving the list paired
        # a face of one leg with a face of the next - ALCAZAR's L-shaped BKT brackets
        # came out as two diagonal beams across the corner. Each leg takes the two
        # outermost faces running along it.
        for i in range(len(locs) - 1):
            p, q = locs[i], locs[i + 1]
            L = G.dist(p, q)
            if L < 1.0:
                continue
            ux, uy = (q[0] - p[0]) / L, (q[1] - p[1]) / L
            along = []
            for f in faces:
                s, t = (f.dxf.start.x, f.dxf.start.y), (f.dxf.end.x, f.dxf.end.y)
                fl = G.dist(s, t)
                if fl < 1.0 or abs((t[0] - s[0]) * uy - (t[1] - s[1]) * ux) / fl > 0.02:
                    continue                          # not along this leg
                m = G.midpoint(s, t)
                k = (m[0] - p[0]) * ux + (m[1] - p[1]) * uy
                off = (m[0] - p[0]) * -uy + (m[1] - p[1]) * ux
                if -1.0 <= k <= L + 1.0 and abs(off) <= abs(scale) + 1.0:
                    if (t[0] - s[0]) * ux + (t[1] - s[1]) * uy < 0:
                        s, t = t, s
                    along.append((off, s, t))
            if len(along) < 2:
                continue
            along.sort(key=lambda x: x[0])
            (_o1, s1, t1), (_o2, s2, t2) = along[0], along[-1]
            a, b = G.midpoint(s1, s2), G.midpoint(t1, t2)
            out.append((a, b, scale, "multiline", "multiline-faces", G.dist(a, p)))
        if len(out) != len(locs) - 1:
            out = []                                  # not understood: as before
    if not out and len(faces) >= 2 and len(locs) >= 2:
        # Faces come out grouped per style element, in drawing order.
        half = len(faces) // 2
        left, right = faces[:half], faces[half:]
        for lf, rf in zip(left, right):
            a = G.midpoint((lf.dxf.start.x, lf.dxf.start.y), (rf.dxf.start.x, rf.dxf.start.y))
            b = G.midpoint((lf.dxf.end.x, lf.dxf.end.y), (rf.dxf.end.x, rf.dxf.end.y))
            drawn_a = (lf.dxf.start.x, lf.dxf.start.y)
            off = G.dist(a, drawn_a)
            out.append((a, b, scale, "multiline", "multiline-faces", off))

    if not out:                                   # fall back to the drawn line
        for i in range(len(locs) - 1):
            out.append((locs[i], locs[i + 1], scale, "multiline", "as-drawn", 0.0))

    if just != 1 and out and out[0][4] == "multiline-faces":
        # Counted here and reported once per layer by _report_justification.
        key = (e.dxf.layer, just, scale)
        res.stats.setdefault("_just", {})
        res.stats["_just"][key] = res.stats["_just"].get(key, 0) + 1
    return out


def _own_layer_partner(b, a, candidates, used, lo, hi, min_ov, max_ang) -> bool:
    """Has face b an unpaired partner on its own layer (other than a)?

    New block: NB4's single drawn face on BEAM-230X600 came first and took the top
    face of the SB beam beside it, 609 mm off, so the SB's bottom face then paired
    with NB3's face: two 600-wide 'SB' beams over the toilet slab instead of one
    230 SB. A face from another layer may only take b when b has no partner of its own."""
    for c in candidates:
        if c is b or c is a or id(c) in used or c.source_layer != b.source_layer:
            continue
        if _angle_between(b, c) > max_ang:
            continue
        if not (lo <= G.point_to_line(_mid(b), c.start, c.end) <= hi):
            continue
        if _overlap_fraction(b, c) >= min_ov:
            return True
    return False


def _split_long_faces(res: Result, tol) -> None:
    """One face drawn whole, the other broken where cross beams meet it: cut the whole
    one to the pieces opposite it, so each piece pairs with its own part.

    Main Block terrace, BEAM-300X600: the south face is one line 8.4 m long, the north
    face four pieces. Paired with one piece, the beam ran from the middle of their two
    starts - 3.4 m short - and the other pieces were dropped as leftovers: the bay
    stayed open and the slab leaked through with a rounded edge. Only where a line has
    two or more such pieces on one side, on its own layer, together covering most of it,
    none of them covering most of it alone (that one already pairs as it is), and each
    break between them a gap of a beam's width (where a cross beam meets the face)."""
    lo = tol.get("face_pair_min_width_mm", 100.0)
    hi = tol.get("face_pair_max_width_mm", 700.0)
    max_ang = tol.get("face_pair_max_angle_deg", 3.0)
    lines = [b for b in res.beams if b.centreline_source == "as-drawn" and not b.mid]
    out = []
    for a in lines:
        L = a.length
        if L < 3000.0:
            continue
        ux, uy = (a.end[0] - a.start[0]) / L, (a.end[1] - a.start[1]) / L
        sides: dict[int, list] = {}
        for b in lines:
            if b is a or b.source_layer != a.source_layer or _angle_between(a, b) > max_ang:
                continue
            m = _mid(b)
            s = (m[0] - a.start[0]) * -uy + (m[1] - a.start[1]) * ux
            if not (lo <= abs(s) <= hi):
                continue
            t = sorted(((p[0] - a.start[0]) * ux + (p[1] - a.start[1]) * uy) for p in (b.start, b.end))
            if t[1] - t[0] >= 0.7 * L:
                continue                                  # it pairs with a as it is
            # a piece lying within a - or running on a little past a's end, over the
            # column (New block NB3b: the last top-face piece runs 150 mm further than
            # the bottom face; left out, the beam stopped 1.3 m short of the column)
            if min(t[1], L) - max(t[0], 0.0) < 0.8 * (t[1] - t[0]) or t[0] < -hi or t[1] > L + hi:
                continue
            sides.setdefault(1 if s > 0 else -1, []).append((max(t[0], 0.0), min(t[1], L), abs(s)))
        for sign, pieces in sides.items():
            # a further line along a, beyond the pieces on the same side (Main Block edge:
            # outer face, the slope line 230 in, the inner face 750 in): the pieces are a
            # line inside one beam, not its other face - left to the pairing as it is
            off = sorted(p[2] for p in pieces)[len(pieces) // 2]
            if any(b is not a and b.source_layer == a.source_layer and _angle_between(a, b) <= max_ang
                   and off + 20.0 < sign * ((_mid(b)[0] - a.start[0]) * -uy + (_mid(b)[1] - a.start[1]) * ux)
                   <= off + hi and _overlap_fraction(b, a) >= 0.5 for b in lines):
                continue
            pieces = [(t0, t1) for t0, t1, _ in pieces]
            pieces.sort()
            if len(pieces) < 2 or sum(t1 - t0 for t0, t1 in pieces) < 0.7 * L:
                continue
            if any(not (lo <= p[0] - q[1] <= hi) for p, q in zip(pieces[1:], pieces)):
                # broken where cross beams meet it: gaps of a beam's width between the
                # pieces. Pieces that just meet (J1153 B42: one face in two pieces) or
                # overlap pair as they always have
                continue
            cuts = [(q[0] + p[1]) / 2.0 for p, q in zip(pieces, pieces[1:])]
            out.append((a, ux, uy, cuts))
            break
    if not out:
        return
    import dataclasses
    for a, ux, uy, cuts in out:
        at = [a.start] + [(a.start[0] + ux * t, a.start[1] + uy * t) for t in cuts] + [a.end]
        parts = [dataclasses.replace(a, start=p, end=q, notes=list(a.notes) + [
            "one face drawn whole, cut to the pieces of the other face"]) for p, q in zip(at, at[1:])]
        i = res.beams.index(a)
        res.beams[i:i + 1] = parts
    res.add(INFO, "geometry",
            f"{len(out)} beam faces drawn in one line, opposite a face broken where cross beams meet it, "
            "were cut to the pieces opposite them, so every piece pairs into its beam.")


def _side_of(a, b) -> int:
    """Which side of line a the middle of line b lies on: +1 left, -1 right, 0 on it."""
    m = _mid(b)
    c = float((a.end[0] - a.start[0]) * (m[1] - a.start[1]) - (a.end[1] - a.start[1]) * (m[0] - a.start[0]))
    return 1 if c > 0 else -1 if c < 0 else 0


def _lines_along_a_beam(lines, lo, hi, min_ov, max_ang, slab_pts, beam_tags=(), tag_limit=700.0) -> dict:
    """{id(a): (a, b, c)}: a shorter line lying along a beam whose two faces b and c are drawn.

    aldoot (every "roof" plan): a 4.8 x 5.4 m box with a cross from corner to corner is drawn on
    the beam layer, its sides inside or right along the beams round it. The bottom side lies
    between the two faces of the 230 row under it and, read first, paired with the top face:
    a 130 "beam", the real bottom face left over. The right side, 35 mm inside a 300 beam,
    paired with its far face: 265, corrected to 330, and the beam stopped 2.2 m short of its
    column. The top side, 100 below a 230 beam, paired across its near face: a 330 beam over it.

    Narrow on purpose: b and c pair with each other (same layer, a beam's width apart, running
    along each other nearly end to end, nothing else between them), each covers a and runs at
    least a beam's least width further; a has no
    other partner than b and c; and a lies between them, or outside within that least width."""
    out = {}
    slack = 15.0
    for a in lines:
        La = a.length
        if a.mid is not None or La < 1:
            continue
        ux, uy = (a.end[0] - a.start[0]) / La, (a.end[1] - a.start[1]) / La

        def side(p):
            return -(p[0] - a.start[0]) * uy + (p[1] - a.start[1]) * ux

        def cover(o):
            t = sorted(((p[0] - a.start[0]) * ux + (p[1] - a.start[1]) * uy) for p in (o.start, o.end))
            return max(0.0, min(t[1], La) - max(t[0], 0.0)) / La

        par = [o for o in lines if o is not a and o.mid is None and abs(side(_mid(o))) <= 2 * hi
               and cover(o) > 0 and _angle_between(a, o) <= max_ang]
        partners = [o for o in par if lo <= G.point_to_line(_mid(a), o.start, o.end) <= hi
                    and _overlap_fraction(a, o) >= min_ov]
        if not partners or len(partners) > 2:
            continue
        near = [(side(_mid(o)), o) for o in par
                if abs(side(_mid(o))) <= hi and cover(o) >= 0.9 and o.length >= La + lo]
        found = None
        for i, (sb, b) in enumerate(near):
            for sc, c in near[i + 1:]:
                if b.source_layer != c.source_layer or not (lo <= abs(sb - sc) <= hi):
                    continue
                if any(p is not b and p is not c for p in partners):
                    continue
                s0, s1 = min(sb, sc), max(sb, sc)
                if not (s0 - lo - slack <= 0.0 <= s1 + lo + slack):
                    continue
                if min(abs(sb), abs(sc)) <= slack:
                    continue                  # on a face: a leftover copy (_drop_orphan_faces)
                if _overlap_fraction(b, c) < min_ov or _slab_mark_between(b, c, s1 - s0, slab_pts):
                    continue
                # one beam's two faces run along each other nearly end to end; a long edge line
                # beside a short face piece is not a pair (NIFT level 5: the 20 m outer line and
                # one span's 4.7 m face set that span's own face, drawn in two pieces, aside)
                if _overlap_fraction(b, c) * min(b.length, c.length) < 0.8 * max(b.length, c.length):
                    continue
                # nothing else between the two faces, along a: they are one beam's faces
                if any(o is not b and o is not c and s0 + slack < side(_mid(o)) < s1 - slack and cover(o) >= 0.5
                       for o in par):
                    continue
                found = (a, b, c)
                break
            if found:
                break
        # a mark nearer to it than to the beam: the drawing says it is an element of its own
        if found and not any(G.point_to_segment(pt, a.start, a.end) <= min(
                tag_limit, G.point_to_segment(pt, found[1].start, found[1].end),
                G.point_to_segment(pt, found[2].start, found[2].end)) for _m, pt, _h, _l in beam_tags):
            out[id(a)] = found
    return out


def _merge_face_pairs(res: Result, tol, sched_sizes=None, beam_tags=(), size_caps=()) -> None:
    """Collapse two plain lines drawn as the two faces of one beam into one beam.

    A multiline carries both faces in a single entity, so its centreline is easy.
    Where a beam was instead drawn as two ordinary parallel lines, each line looks
    like a beam of its own, and the model ends up with two beams where there is
    one. The separation between them is the beam's width, which is also the only
    place that width can be read from.
    """
    lo = tol.get("face_pair_min_width_mm", 100.0)
    hi = tol.get("face_pair_max_width_mm", 700.0)
    min_ov = tol.get("face_pair_min_overlap", 0.5)
    max_ang = tol.get("face_pair_max_angle_deg", 3.0)

    candidates = [b for b in res.beams if b.centreline_source == "as-drawn"]
    used: set[int] = set()
    merged: list[Beam] = []
    slab_pts = [(t.x, t.y) for t in res.slab_tags]
    aside = _lines_along_a_beam(candidates, lo, hi, min_ov, max_ang, slab_pts, beam_tags,
                                tol.get("tag_assign_limit_mm", 700.0))
    paired: dict[int, Beam] = {}             # id of each face -> the beam it went into

    def pair(a, pool) -> None:
        best = None
        valid = []
        for b in pool:
            if id(b) in used or (id(b) in aside and id(a) not in aside):
                continue
            if _angle_between(a, b) > max_ang:
                continue
            gap = G.point_to_line(_mid(a), b.start, b.end)
            if not (lo <= gap <= hi) and not _scheduled_wide_beam(
                    a, b, gap, sched_sizes, beam_tags, tol, size_caps):
                continue
            ov = _overlap_fraction(a, b)
            if ov < min_ov:
                continue
            if _slab_mark_between(a, b, gap, slab_pts):
                continue
            if a.source_layer != b.source_layer and _own_layer_partner(b, a, candidates, used, lo, hi,
                                                                         min_ov, max_ang):
                continue                          # b's other face is on b's own layer: leave b to it
            # the fuller overlap wins; on a tie (both overlap it wholly), the face on
            # the same layer: New block's SB face paired with the next beam's face (on
            # BEAM-230X600) 609 mm off, not its own SB partner 230 mm off, and made a
            # 600-wide SB that ate the toilet slab. Otherwise the first found, as ever
            # (B42 on Wing B 2nd floor depends on it).
            # Overlap is measured on the SHORTER line, so any short line beside a long
            # face overlaps it "wholly": J1153 Wing C 3rd floor B47's top face (2871)
            # paired with the 432 bottom edge of a duct box under it, 460 off, over its own
            # 2871 face 230 off (a hair under 1 from drafting skew), and B47 was built on
            # its lower face, 115 mm out of place; the same gave 650 "beams" across the
            # A wing stair cut-outs and a 690 BC6/B33 on Wing C 2nd floor. So when another
            # line ties it (overlap to 0.1 %, same layer standing), lies at a different
            # distance - a different line, not the next piece of the same broken face
            # (Sasan Gir plinth GB8: that choice and the carrying-on after it stay as they
            # were) - and runs along much more of this one, that line is the face.
            rank = (ov, a.source_layer == b.source_layer)
            cover = ov * min(a.length, b.length) / a.length if a.length else 0.0
            valid.append((ov, gap, b, rank, cover))
            if best is None or rank > best[3]:
                best = (ov, gap, b, rank, cover)
        if best is None:
            return
        # A line on the OTHER side of this one is a different line too, however near the
        # same distance: J1153 Wing C 2nd floor 08-09-2026, the BC6/B33 row's top face
        # paired with the 432 top edge of a duct box on it, 229 above, over its own 8 m
        # bottom face 230 below - the row was built 115 mm up, off the curve it meets.
        alt = [v for v in valid if v[2] is not best[2]
               and round(v[0], 3) >= round(best[0], 3) and v[3][1] >= best[3][1]
               and (abs(v[1] - best[1]) > 20.0 or _side_of(a, v[2]) != _side_of(a, best[2]))
               and v[4] > best[4] + 0.2]
        if alt:
            best = max(alt, key=lambda v: v[4])

        ov, gap, b, _rank, _cover = best
        used.add(id(a))
        used.add(id(b))
        # the ends of the two drawn faces, for _extend_over_leftover
        a._face_ends = [a.start, a.end, b.start, b.end]
        s2, e2 = (b.start, b.end)
        if ((e2[0] - s2[0]) * (a.end[0] - a.start[0])
                + (e2[1] - s2[1]) * (a.end[1] - a.start[1])) < 0:
            s2, e2 = e2, s2                      # run both faces the same way

        a.start = G.midpoint(a.start, s2)
        a.end = G.midpoint(a.end, e2)
        a.width = round(gap, 1)
        a.width_source = "measured between the drawn faces"
        a.centreline_source = "face-pair"
        a.offset_applied = round(gap / 2.0, 1)
        a.mark = a.mark or b.mark
        a.notes = [n for n in a.notes if "plain line" not in n]
        a.notes.append("two drawn faces merged into one beam")
        merged.append(a)
        res.beams.remove(b)
        paired[id(a)] = paired[id(b)] = a

    for i, a in enumerate(candidates):
        if id(a) not in used and id(a) not in aside:
            pair(a, candidates[i + 1:])
    # A line set aside lies along a beam whose two faces are drawn longer than it. When
    # those two faces did make one beam, it is not a face of that beam: it goes. If they
    # did not, it pairs as it always has.
    gone = []
    for a, b, c in aside.values():
        k = paired.get(id(b))
        if k is not None and paired.get(id(c)) is k:
            res.beams.remove(a)
            k.notes.append("a shorter line along this beam (another outline drawn on the beam layer) "
                           "was not taken as one of its faces")
            gone.append(a)
        elif id(a) not in used:
            pair(a, [o for o in candidates if o is not a])
    if gone:
        m = _mid(gone[0])
        res.add(INFO, "geometry",
                f"{len(gone)} shorter lines on the beam layer lie along a beam whose two faces are both "
                "drawn (e.g. the edge of a box drawn over the beam). They are not its faces and were not "
                "built; the beam was read from its own two faces.", x=m[0], y=m[1])

    if merged:
        widths = sorted({round(m.width) for m in merged if m.width})
        res.add(WARNING, "geometry",
                f"{len(merged)} beams were drawn as two separate face lines rather than one "
                f"centreline. Each pair has been merged into a single beam and its width "
                f"measured from the gap between the faces ({', '.join(str(w) for w in widths[:6])}"
                f"{' mm' if widths else ''}). Draw one centreline per beam instead.")



def _reconcile_chain_widths(res: Result, tol) -> None:
    """Give every run of one beam the same width.

    A beam that splays round an obstruction is drawn as two face polylines that
    bend together: diagonal, straight, diagonal. Each bend becomes its own
    segment, and each segment measures its own width from the gap between the
    faces.

    That works on the straight run and fails on the diagonals, because drafters
    offset the second face along an axis rather than perpendicular to the line.
    A 200 mm beam turning through 45 degrees then measures 200/root 2 = 141 mm,
    and the model gets one beam that mysteriously narrows at both ends.

    The segments came from one polyline, so they are one beam and take one width:
    the widest reading in the run, which is the one taken where the two faces are
    genuinely parallel. Anything reconciled is reported.
    """
    t = max(tol["duplicate_tolerance_mm"], 2.0)
    groups: dict[str, list[Beam]] = {}
    for b in res.beams:
        if b.centreline_source == "face-pair" and b.handle and b.width:
            groups.setdefault(b.handle, []).append(b)

    fixed = []
    for handle, run in groups.items():
        if len(run) < 2:
            continue
        widest = max(b.width for b in run)
        wb = max(run, key=lambda b: b.width)
        for b in run:
            # only the legs at an angle to it: a piece lying straight in line with the widest
            # is a face line shared by two beams of different widths, each read rightly
            # (a long face cut to the pieces opposite it, _split_long_faces)
            if widest - b.width > t and _angle_between(b, wb) > tol.get("face_pair_max_angle_deg", 3.0):
                was = b.width
                b.width = widest
                b.offset_applied = round(widest / 2.0, 1)
                b.width_source = "measured between the drawn faces, along the run"
                b.notes.append(f"width read as {was:.0f} mm on this bend and corrected to "
                               f"{widest:.0f} mm, the width of the rest of the same beam")
                fixed.append((was, widest, b))

    if fixed:
        worst = min(fixed, key=lambda f: f[0])
        res.add(WARNING, "geometry",
                f"{len(fixed)} beam segments measured narrower than the rest of the beam "
                "they belong to, because the two drawn faces are offset along an axis "
                "rather than square to the line where the beam changes direction. Each has "
                "been widened to match its own run (the narrowest was "
                f"{worst[0]:.0f} mm, corrected to {worst[1]:.0f} mm). Confirm the width "
                "against the schedule.",
                x=worst[2].start[0], y=worst[2].start[1])




SLAB_MARK_INSIDE_MM = 150.0    # a slab mark this far inside both faces is in the band, not on a face


def _slab_mark_between(a, b, gap: float, slab_pts) -> bool:
    """Is a slab mark written in the band between these two lines?

    Then the band is slab, not one wide beam: Stilt A wing's sunk S1 strip runs
    600 mm wide between the B10A and B7 rows, its S1 marks in it, and the facing
    faces of the two rows were merged into a 600 mm beam that covered the slab.
    The mark has to sit well inside both faces, so a mark on a beam line (Wing A
    2nd floor S8, 34 mm off) never counts, and no 230 beam can be affected.
    """
    if not slab_pts or gap <= 2 * SLAB_MARK_INSIDE_MM:
        return False
    s2, e2 = b.start, b.end
    if ((e2[0] - s2[0]) * (a.end[0] - a.start[0])
            + (e2[1] - s2[1]) * (a.end[1] - a.start[1])) < 0:
        s2, e2 = e2, s2
    mid_s, mid_e = G.midpoint(a.start, s2), G.midpoint(a.end, e2)
    return any(G.point_to_segment(p, mid_s, mid_e) <= gap / 2.0 - SLAB_MARK_INSIDE_MM for p in slab_pts)


def _scheduled_wide_beam(a, b, gap: float, sched_sizes, beam_tags, tol, size_caps=()) -> bool:
    """Are these two lines the faces of one unusually wide beam?

    The ordinary limit on how far apart two faces may be exists to stop two
    genuinely separate beams being welded into one. A job with 1500 mm girders
    breaks that limit honestly: on the Wing B plan each girder stayed as its two
    faces, giving two beams 750 mm either side of where the girder really is, and
    the floating columns that sit on it touching nothing.

    Widening the limit for everyone is not the answer either. The Wing C schedule
    happens to list 750 and 900 mm beams, and beams on that plan really do run
    750 mm apart, so a raised ceiling welds them together.

    So the evidence has to be local: a mark written BETWEEN these two lines, whose
    own schedule row gives a width matching the gap. The drawing is then saying,
    in as many words, that this band is one beam of this width.

    Or a caption beside the band that writes the width itself, "GB1(1100X1200)",
    equal to the gap. ALCAZAR 1st floor writes its girders that way, on the girder
    layer: the two faces of each stayed two beams - the girder centred on one face,
    half its width outside the real girder, and the other face a 230 beam beside it.
    """
    if not (sched_sizes and beam_tags) and not size_caps:
        return False
    slack = max(tol.get("rect_side_tolerance_mm", 5.0), 25.0)

    # The two faces are not necessarily drawn in the same direction. Pairing the
    # ends without checking gives a midline that runs diagonally across the band
    # instead of down it, and the mark inside the band then measures as far
    # outside it. That is why only two of the seven Wing B girders merged on the
    # first attempt.
    s2, e2 = b.start, b.end
    if ((e2[0] - s2[0]) * (a.end[0] - a.start[0])
            + (e2[1] - s2[1]) * (a.end[1] - a.start[1])) < 0:
        s2, e2 = e2, s2
    mid_s = G.midpoint(a.start, s2)
    mid_e = G.midpoint(a.end, e2)
    # The mark sits in the band or just beside it, along its length: J1153 Wing C 1st
    # floor writes GB1, GB2, GB3, GB5 270-350 mm outside the band's face (insertion
    # point), and each girder stayed two beams, one centred on a face and the other an
    # extra beam beside it. A band drawn a little off its scheduled width (GB2A: 736
    # drawn, 700 scheduled) is still that girder; the width difference is then
    # reported by the linker, not hidden.
    reach = gap / 2.0 + tol.get("wide_beam_mark_reach_mm", 450.0)
    for mark, pt, _h, _lay in (beam_tags if sched_sizes else ()):
        if G.point_to_segment(pt, mid_s, mid_e) > reach:
            continue
        v = sched_sizes.get(sched._key(mark))
        if v and v.get("width") and abs(float(v["width"]) - gap) <= max(slack, 0.1 * float(v["width"])):
            return True
    # a written size is captioned beside its beam, so it may sit outside the band,
    # as far from its edge as a mark may sit from the beam it names
    near = gap / 2.0 + tol.get("tag_assign_limit_mm", 700.0)
    for _mark, width, pts in size_caps:
        if abs(width - gap) <= slack and min(G.point_to_segment(p, mid_s, mid_e) for p in pts) <= near:
            return True
    return False


def _angle_between(a: Beam, b: Beam) -> float:
    d1 = G.angle_deg(a.start, a.end) % 180.0
    d2 = G.angle_deg(b.start, b.end) % 180.0
    d = abs(d1 - d2)
    return min(d, 180.0 - d)


def _mid(b: Beam) -> G.Point:
    return G.midpoint(b.start, b.end)


def _overlap_fraction(a: Beam, b: Beam, pad: float = 0.0) -> float:
    """How much of the shorter beam runs alongside the other, 0 to 1.

    `pad` lengthens b at both ends before measuring, for callers that run before
    the network has been joined and so before b has been stretched to meet its
    neighbours.
    """
    ax, ay = b.end[0] - b.start[0], b.end[1] - b.start[1]
    L = math.hypot(ax, ay)
    if L == 0:
        return 0.0
    ux, uy = ax / L, ay / L
    ts = [((p[0] - b.start[0]) * ux + (p[1] - b.start[1]) * uy) for p in (a.start, a.end)]
    lo_, hi_ = min(ts), max(ts)
    ov = max(0.0, min(hi_, L + pad) - max(lo_, -pad))
    shorter = min(a.length, L)
    return ov / shorter if shorter else 0.0


def _drop_stacked_beams(res: Result, tol) -> None:
    """Remove a beam that lies along another one, keeping the longer of the two."""
    dropped = 0
    beams = sorted(res.beams, key=lambda b: -b.length)
    keep: list[Beam] = []
    for b in beams:
        clash = False
        for k in keep:
            if _angle_between(b, k) > 2.0:
                continue
            if G.point_to_line(_mid(b), k.start, k.end) > tol.get("stacked_tolerance_mm", 5.0):
                continue
            if _overlap_fraction(b, k) < 0.8:
                continue
            clash = True
            break
        if clash:
            dropped += 1
        else:
            keep.append(b)
    if dropped:
        res.beams[:] = keep
        res.add(WARNING, "geometry",
                f"{dropped} beam lines lie on top of another beam along their whole length "
                "and have been dropped, so they do not become duplicate Revit beams.")


def _join_overlapping_spans(res: Result, tol) -> None:
    """One beam, not two lying part over part.

    Sasan Gir terrace, B29c / B30c: each drawn as two face pairs on one centreline,
    the first from B2 down to 127710, the second from 126183 on to B3 - 1.5 m over
    each other. Neither covers 80 % of the other (_drop_stacked_beams), so both were
    kept: two beams, one on top of the other for 1.5 m, the second without a mark.

    Run after the beams are split at their columns and crossings, and after the
    marks: each piece is then a span with nothing in it, so the two together have
    nothing in them either; and two pieces with marks of their own (J1153 Wing C
    2nd floor, B5 and B6 drawn overlapping either side of the lift wall) are two
    beams and are left as they are.
    """
    supports = [o.outline for o in list(res.columns) + list(res.walls) if len(o.outline) >= 3]

    def at_support(k, b):
        """the stretch they share has a column or wall in it: two beams meeting on it,
        drawn on past each other (Villa 5: MB at the mid landing and GB19 either side
        of C4, 892 mm over each other)"""
        L = k.length
        ux, uy = (k.end[0] - k.start[0]) / L, (k.end[1] - k.start[1]) / L
        ts = sorted(((p[0] - k.start[0]) * ux + (p[1] - k.start[1]) * uy) for p in (b.start, b.end))
        t0, t1 = max(0.0, ts[0]), min(L, ts[1])
        n = max(2, int((t1 - t0) / 50.0))
        pts = [(k.start[0] + ux * (t0 + (t1 - t0) * i / n), k.start[1] + uy * (t0 + (t1 - t0) * i / n))
               for i in range(n + 1)]
        return any(G.point_in_polygon(p, o) or
                   min(G.point_to_segment(p, o[j - 1], o[j]) for j in range(len(o))) <= 30.0
                   for o in supports for p in pts)

    # Two beams meeting at a crossing beam, each drawn on past it (Sasan Gir terrace,
    # B27d / B27c at the B7 rows: 768 and 428 mm over each other): where one of them
    # ends on a crossing beam inside the other's length, the other is cut back to
    # that crossing. Copies of one beam (B29c) end on no crossing and are joined below.
    met = 0
    straight = [b for b in res.beams if b.mid is None and b.width and b.length > 0]
    for x in straight:
        for y in straight:
            if x is y or _angle_between(x, y) > 2.0 or \
                    G.point_to_line(_mid(y), x.start, x.end) > tol.get("stacked_tolerance_mm", 5.0) or \
                    abs(x.width - y.width) > 5.0 or _shared_length(y, x) < max(x.width, 1.0):
                continue
            L = x.length
            ux, uy = (x.end[0] - x.start[0]) / L, (x.end[1] - x.start[1]) / L
            ty = [((p[0] - x.start[0]) * ux + (p[1] - x.start[1]) * uy) for p in (y.start, y.end)]
            for e, te, other in ((y.start, ty[0], ty[1]), (y.end, ty[1], ty[0])):
                if not (x.width < te < L - x.width):
                    continue                          # y's end is not inside x's length
                cross = next((o for o in res.beams if o is not x and o is not y and o.mid is None and
                              _angle_between(o, x) >= 60.0 and
                              G.point_to_segment(e, o.start, o.end) <= (o.width or 0.0) / 2.0 + 30.0), None)
                if cross is None:
                    continue
                c = _cross_point(x, cross)
                if c is None:
                    continue
                tc = (c[0] - x.start[0]) * ux + (c[1] - x.start[1]) * uy
                # cut x on y's side of the crossing (y's body lies towards `other`)
                if other > te and L - tc > x.width:
                    x.end = (x.start[0] + ux * tc, x.start[1] + uy * tc)
                elif other < te and tc > x.width:
                    x.start = (x.start[0] + ux * tc, x.start[1] + uy * tc)
                else:
                    continue
                x.notes.append("ends at the crossing beam where the next beam along its line begins "
                               "(they were drawn on past each other)")
                met += 1
                break
    if met:
        res.add(INFO, "geometry",
                f"{met} beams were drawn on past the crossing beam where the next beam along their "
                "line begins, lying over it; each now ends at that crossing.")

    joined = 0
    changed = True
    while changed:
        changed = False
        beams = sorted(res.beams, key=lambda b: -b.length)
        for i, k in enumerate(beams):
            if k.length <= 0:
                continue
            for b in beams[i + 1:]:
                if _angle_between(b, k) > 2.0:
                    continue
                if G.point_to_line(_mid(b), k.start, k.end) > tol.get("stacked_tolerance_mm", 5.0):
                    continue
                if _shared_length(b, k) < max(k.width or 0.0, 1.0) or at_support(k, b):
                    continue
                # never over a third beam on the line (J1153 Wing C 2nd floor: B6 and an
                # unmarked copy lying on B5 - joined, B6 ran on over B5)
                if any(c is not k and c is not b and _angle_between(c, k) <= 2.0 and
                       G.point_to_line(_mid(c), k.start, k.end) <= max(k.width or 0.0, 230.0) / 2.0 and
                       (_shared_length(c, k) > 1.0 or _shared_length(c, b) > 1.0)
                       for c in res.beams):
                    continue
                if _join_overlapping_piece(k, b):
                    res.beams.remove(b)
                    joined += 1
                    changed = True
                    break
            if changed:
                break
    if joined:
        res.add(INFO, "geometry",
                f"{joined} beams were drawn in two overlapping pieces on one centreline, as wide (the "
                "same beam drawn twice, part over part). Each pair was joined into one beam.")


def _drop_label_boxes(res: Result, ents) -> None:
    """A box drawn round a note on the beam layer is not a beam.

    Villa 5: each "MUD FILLING" note has a 1384 x 234 box round it on RCC BEAMS. Its
    two long sides paired into a 234 mm "beam", seven of them on each floor, each
    floating in mid-air.

    Narrow on purpose: a straight unmarked beam that rests on nothing at either end
    (no beam, column or wall) - a beam is carried at one end at least - with a text
    written inside its own band. Anything touching the frame is never dropped.
    """
    from ezdxf import bbox as _bbox
    size_like = re.compile(r"\d{2,4}\s*[xX×*]\s*\d{2,4}")
    pts = []
    for e in ents:
        if e.dxftype() not in ("TEXT", "MTEXT"):
            continue
        try:
            # a size written in the box ("GB29(230X500)", a style the marks may not read) is
            # the beam's own caption: never a reason to drop it
            if size_like.search(e.dxf.text if e.dxftype() == "TEXT" else e.plain_text()):
                continue
            b = _bbox.extents([e], fast=True)
            if b.has_data:
                pts.append(((b.extmin.x + b.extmax.x) / 2.0, (b.extmin.y + b.extmax.y) / 2.0))
            else:
                pts.append((e.dxf.insert.x, e.dxf.insert.y))
        except Exception:
            continue
    if not pts:
        return
    supports = [o.outline for o in list(res.columns) + list(res.walls) if len(o.outline) >= 3]

    def rests(p, b):
        if any(o is not b and G.point_to_segment(p, o.start, o.end) <= (o.width or 0.0) / 2.0 + 30.0
               for o in res.beams):
            return True
        return any(G.point_in_polygon(p, o) or
                   min(G.point_to_segment(p, o[i - 1], o[i]) for i in range(len(o))) <= 30.0 for o in supports)

    drop = []
    for b in res.beams:
        if b.mark or b.mid is not None or not b.width or b.length < 1:
            continue
        L = b.length
        ux, uy = (b.end[0] - b.start[0]) / L, (b.end[1] - b.start[1]) / L
        if not any(0.0 <= (p[0] - b.start[0]) * ux + (p[1] - b.start[1]) * uy <= L and
                   abs(-(p[0] - b.start[0]) * uy + (p[1] - b.start[1]) * ux) <= b.width / 2.0 for p in pts):
            continue
        if rests(b.start, b) or rests(b.end, b):
            continue
        drop.append(b)
    if not drop:
        return
    ids = {id(b) for b in drop}
    res.beams[:] = [b for b in res.beams if id(b) not in ids]
    m = _mid(drop[0])
    res.add(INFO, "geometry",
            f"{len(drop)} boxes on the beam layer with a note written inside, touching no beam, column "
            "or wall, are frames round the note, not beams, and were not built. If one is a beam, "
            "give it its mark on the drawing.", x=m[0], y=m[1])


def _shared_length(a: Beam, b: Beam) -> float:
    """How far a and b (parallel) run alongside each other, in mm."""
    L = b.length
    if L <= 0:
        return 0.0
    ux, uy = (b.end[0] - b.start[0]) / L, (b.end[1] - b.start[1]) / L
    ts = sorted(((p[0] - b.start[0]) * ux + (p[1] - b.start[1]) * uy) for p in (a.start, a.end))
    return max(0.0, min(L, ts[1]) - max(0.0, ts[0]))


def _join_overlapping_piece(k: Beam, b: Beam) -> bool:
    """Join b into k when they are one beam drawn in two pieces that overlap.

    Only two beams measured from their faces, as wide, on the same layer and of the
    same kind, not with two different marks, sharing at least a beam's width of
    their length (pieces that only touch end to end are not this). Two such beams
    are the same concrete. k (the longer) is carried over b's length and takes b's
    mark if it has none.
    """
    if not k.width or not b.width or abs(k.width - b.width) > max(5.0, 0.02 * k.width):
        return False
    if k.mid is not None or b.mid is not None or k.source_layer != b.source_layer or k.kind != b.kind:
        return False
    if k.mark and b.mark and k.mark != b.mark:
        return False
    L = k.length
    if L <= 0:
        return False
    ux, uy = (k.end[0] - k.start[0]) / L, (k.end[1] - k.start[1]) / L
    ts = sorted(((p[0] - k.start[0]) * ux + (p[1] - k.start[1]) * uy) for p in (b.start, b.end))
    if min(L, ts[1]) - max(0.0, ts[0]) < k.width:
        return False
    lo, hi = min(0.0, ts[0]), max(L, ts[1])
    k.start, k.end = (k.start[0] + ux * lo, k.start[1] + uy * lo), (k.start[0] + ux * hi, k.start[1] + uy * hi)
    k.mark = k.mark or b.mark
    fe = list(getattr(k, "_face_ends", None) or []) + list(getattr(b, "_face_ends", None) or [])
    if fe:
        k._face_ends = fe
    k.notes.append(f"joined with a second piece drawn over it ({min(L, ts[1]) - max(0.0, ts[0]):.0f} mm "
                   "overlap): one beam, drawn twice part over part")
    return True



def _continues_face(b: Beam, faces, tol: float = 10.0) -> bool:
    """Is leftover b the rest of one merged face, broken where a crossing beam meets it?

    New block NB2: its top face is drawn in two pieces, broken for 230 mm where an
    SB beam meets it, and its bottom face in one. The second top piece neither
    starts nor ends at a merged face's end, so it was not taken as a continuation
    and the beam stopped 1.3 m short of the column line, leaving the bays on either
    side open to each other. It is one when it lies on the line of one merged face
    and ends exactly where the other face ends: both faces then say where the beam
    stops."""
    pairs = ((faces[0], faces[1], faces[2], faces[3]), (faces[2], faces[3], faces[0], faces[1]))
    for f0, f1, g0, g1 in pairs:
        L = G.dist(f0, f1)
        if L <= 0:
            continue
        if G.point_to_line(b.start, f0, f1) > tol or G.point_to_line(b.end, f0, f1) > tol:
            continue
        ux, uy = (f1[0] - f0[0]) / L, (f1[1] - f0[1]) / L

        def t(p):
            return (p[0] - f0[0]) * ux + (p[1] - f0[1]) * uy
        if any(abs(t(p) - t(q)) <= tol for p in (b.start, b.end) for q in (g0, g1)):
            return True
    return False


def _extend_over_leftover(k: Beam, b: Beam, overhang: float = 200.0) -> None:
    """Carry beam k on over the stretch its leftover face b still covers.

    A face drawn in two pieces pairs with the other face through only one of
    them. The beam then ends at the average of the paired pieces' ends, and the
    second piece is dropped as a leftover. B42 on the Wing B 2nd floor lost its
    last 760 mm to B95 that way: its top face is split where B93 joins it.
    Where the leftover runs on past the beam's end, the beam is drawn that far,
    so its centreline goes that far too.

    Only a real continuation counts: same layer, and starting where one of the
    beam's own merged faces ends. The end edge of a girder also lies along a
    beam's face, and taking it as a leftover pushed eight 1st-floor beams half a
    girder width into the girders they end on.
    """
    faces = getattr(k, "_face_ends", None)
    if not faces and k.centreline_source == "multiline-faces" and k.width and k.length > 1:
        # a multiline's faces end half its width either side of its own ends
        nx, ny = -(k.end[1] - k.start[1]) / k.length, (k.end[0] - k.start[0]) / k.length
        h = k.width / 2.0
        faces = [(p[0] + s * h * nx, p[1] + s * h * ny) for p in (k.start, k.end) for s in (1, -1)]
    if not faces or b.source_layer != k.source_layer or b.kind != k.kind:
        return
    if not any(G.dist(p, q) <= 10.0 for p in (b.start, b.end) for q in faces) and \
            not _continues_face(b, faces):
        return
    L = k.length
    if L <= 0:
        return
    ux, uy = (k.end[0] - k.start[0]) / L, (k.end[1] - k.start[1]) / L
    ts = [(p[0] - k.start[0]) * ux + (p[1] - k.start[1]) * uy for p in (b.start, b.end)]
    hi, lo = max(ts), min(ts)
    if hi > L + overhang:
        k.end = (k.start[0] + ux * hi, k.start[1] + uy * hi)
        k.notes.append(f"carried on {hi - L:.0f} mm, as far as its face line is drawn")
    if lo < -overhang:
        k.start = (k.start[0] + ux * lo, k.start[1] + uy * lo)
        k.notes.append(f"carried back {-lo:.0f} mm, as far as its face line is drawn")


def _bridge_staggered_faces(res: Result, tol) -> None:
    """Join two pieces of one beam whose faces are broken at staggered places.

    New block, the column line by the stair: the left face is broken where a beam
    comes in from the left, the right face lower down where NB2 comes in from the
    right. Each pair of face pieces merged into its own beam, with 560 mm between
    them, and the stair bay ran through that gap into the slab beside it.

    Only where the drawn faces together cover the whole gap: wherever one face is
    broken the other runs on, so the beam does too. Same layer, same width,
    parallel, on one centreline. A beam really broken - neither face drawn across
    the gap - is left as drawn.
    """
    slack = max(tol.get("rect_side_tolerance_mm", 5.0), 15.0)
    max_ang = tol.get("face_pair_max_angle_deg", 3.0)
    pairs = [b for b in res.beams if b.centreline_source == "face-pair" and b.width
             and getattr(b, "_face_ends", None)]
    joined = 0
    changed = True
    while changed:
        changed = False
        for a in pairs:
            for b in pairs:
                if a is b or a.source_layer != b.source_layer or abs(a.width - b.width) > slack:
                    continue
                if _angle_between(a, b) > max_ang:
                    continue
                if G.point_to_line(b.start, a.start, a.end) > slack or \
                        G.point_to_line(b.end, a.start, a.end) > slack:
                    continue
                L = a.length
                if L <= 0:
                    continue
                ux, uy = (a.end[0] - a.start[0]) / L, (a.end[1] - a.start[1]) / L

                def t(p):
                    return (p[0] - a.start[0]) * ux + (p[1] - a.start[1]) * uy
                ta, tb = sorted((t(a.start), t(a.end))), sorted((t(b.start), t(b.end)))
                if ta[1] > tb[0]:
                    continue                      # b is not after a, or they overlap
                gap = (ta[1], tb[0])
                if gap[1] - gap[0] > max(4.0 * a.width, 1000.0):
                    continue
                spans = []
                for fe in (a._face_ends, b._face_ends):
                    for p, q in ((fe[0], fe[1]), (fe[2], fe[3])):
                        spans.append(tuple(sorted((t(p), t(q)))))
                spans.sort()
                reach = gap[0]
                for s0, s1 in spans:
                    if s0 <= reach + 1.0:
                        reach = max(reach, s1)
                if reach < gap[1] - 1.0:
                    continue                      # a real break: neither face crosses it
                lo = a.start if t(a.start) < t(a.end) else a.end
                hi = b.end if t(b.end) > t(b.start) else b.start
                a.start, a.end = lo, hi
                a._face_ends = list(a._face_ends) + list(b._face_ends)
                a.mark = a.mark or b.mark
                a.notes.append("joined with the next piece of the same beam: its faces are broken at "
                               "staggered places, but between them run the whole way")
                res.beams.remove(b)
                pairs.remove(b)
                joined += 1
                changed = True
                break
            if changed:
                break
    if joined:
        res.add(INFO, "geometry",
                f"{joined} beams were drawn with their two faces broken at different places (where "
                "other beams meet them from either side). Each was in two pieces with a gap between; "
                "the faces between them cover the gap, so the pieces were joined into one beam.")


def _align_face_continuations(res: Result, tol) -> None:
    """Put a lone face line that carries on a beam onto that beam's centreline.

    New block, the right-hand column line: the inner face is broken where NB4 and
    NB3 come in, and its middle piece starts 900 mm past the end of the beam the
    faces merged into - too far to be dropped as its leftover - so it stayed a beam
    of its own, on the face line, 150 mm off the beam it belongs to.

    Only a line with no width of its own, parallel, on the same layer, lying half
    the beam's width off its centreline (on its face line) and in line with it
    within max(4 widths, 1 m). Two real beams never sit half a width apart: they
    would overlap. The line keeps its own ends, moved onto the centreline, and
    takes the beam's width."""
    slack = max(tol.get("rect_side_tolerance_mm", 5.0), 15.0)
    max_ang = tol.get("face_pair_max_angle_deg", 3.0)
    moved = absorbed = 0
    gone = set()
    moved_ids = set()
    for b in res.beams:
        if b.width or b.centreline_source != "as-drawn":
            continue
        for k in res.beams:
            if k is b or not k.width or k.centreline_source != "face-pair" or k.source_layer != b.source_layer:
                continue
            if _angle_between(b, k) > max_ang:
                continue
            off = G.point_to_line(_mid(b), k.start, k.end)
            if abs(off - k.width / 2.0) > slack:
                continue
            L = k.length
            if L <= 0:
                continue
            ux, uy = (k.end[0] - k.start[0]) / L, (k.end[1] - k.start[1]) / L
            ts = sorted(((p[0] - k.start[0]) * ux + (p[1] - k.start[1]) * uy) for p in (b.start, b.end))
            gap = max(ts[0] - L, -ts[1], 0.0)
            if gap > max(4.0 * k.width, 1000.0):
                continue
            fe = list(getattr(k, "_face_ends", None) or [])
            spans = [sorted(((p[0] - k.start[0]) * ux + (p[1] - k.start[1]) * uy) for p in fe[i:i + 2])
                     for i in range(0, len(fe) - 1, 2)]

            def bridged(g0, g1):
                """a gap the beam's other face runs across (as left by the leftover step's 200)"""
                return g1 - g0 <= 200.0 + slack and any(s[0] <= g0 + 1.0 and s[1] >= g1 - 1.0 for s in spans)

            def supported(t0, t1):
                """a column or wall where the piece takes over: the beam ends there, the piece is
                the next span (J1153 Terrace: B4 | B7 and B10D | B10e meet on columns, their
                face lines drawn on past them)"""
                pts = [(k.start[0] + ux * t, k.start[1] + uy * t) for t in (t0 - 50.0, (t0 + t1) / 2.0, t1 + 50.0)]
                for o in list(res.columns) + list(res.walls):
                    ol = getattr(o, "outline", None) or []
                    if len(ol) >= 3 and any(G.point_in_polygon(p, ol) or
                                            min(G.point_to_segment(p, ol[i - 1], ol[i]) for i in range(len(ol))) <= 30.0
                                            for p in pts):
                        return True
                return False
            # only a piece that carries the beam on past an end of it; one lying within it
            # (a face line with a mark of its own beside it) is left as it was
            ext_end, ext_start = ts[1] > L + slack, ts[0] < -slack
            # and lying on the beam's face line from end to end: a face that bends a few
            # degrees at a time (Sasan Gir plinth, 135 mm over 5.5 m) is not straightened
            on_face = all(abs(G.point_to_line(p, k.start, k.end) - k.width / 2.0) <= slack for p in (b.start, b.end))
            if on_face and (ext_end or ext_start) and \
                    (ts[0] <= L + slack or bridged(L, ts[0])) and (ts[1] >= -slack or bridged(ts[1], 0.0)) and \
                    not (ext_end and supported(min(L, ts[0]), ts[0])) and \
                    not (ext_start and supported(ts[1], max(0.0, ts[1]))):
                # It runs alongside the beam, or on from its very end: the beam is the rest
                # of this face, so it goes as far as the face does. Akanistha Winter Villa
                # beside SW1: one face whole (5600), the other a 323 stub and a 4977 piece
                # either side of a beam coming in; the stub paired with the whole face and the
                # beam ended at their ends' average, half way; the 4977 piece, put on its
                # centreline, became a second beam lying over the first for 2.5 m. On the
                # ground floor that piece is two lines end to end, and the second became an
                # unmarked beam of its own. (A beam crossing there still splits it later.)
                lo_t, hi_t = min(0.0, ts[0]), max(L, ts[1])
                k._face_ends = list(getattr(k, "_face_ends", None) or []) + [b.start, b.end]
                k.start, k.end = (k.start[0] + ux * lo_t, k.start[1] + uy * lo_t), \
                                 (k.start[0] + ux * hi_t, k.start[1] + uy * hi_t)
                k.mark = k.mark or b.mark
                k.notes.append(f"carried {hi_t - lo_t - L:.0f} mm further along its face line, broken "
                               "where another beam meets it")
                gone.add(id(b))
                absorbed += 1
                break

            def onto(p):
                t = (p[0] - k.start[0]) * ux + (p[1] - k.start[1]) * uy
                return (k.start[0] + ux * t, k.start[1] + uy * t)
            b.start, b.end = onto(b.start), onto(b.end)
            b.width = k.width
            b.width_source = "the beam whose face it carries on"
            b.centreline_source = "face-pair"
            b.offset_applied = round(k.width / 2.0, 1)
            b.notes = [n for n in b.notes if "plain line" not in n] + [
                "a face line carrying on a beam: put on that beam's centreline"]
            moved += 1
            # a face drawn as two lines end to end (Sasan Gir terrace, B17c between B7 and B19:
            # the line style changes half way) - each moved here became a beam, both were later
            # stretched to B7 and B19, and two beams lay in one place. The second piece, in
            # line with one moved before it and touching or overlapping it, joins that one.
            for o in res.beams:
                if id(o) not in moved_ids or id(o) in gone or _angle_between(o, b) > 2.0 or \
                        G.point_to_line(_mid(b), o.start, o.end) > tol.get("stacked_tolerance_mm", 5.0):
                    continue
                Lo = o.length
                if Lo <= 0:
                    continue
                vx, vy = (o.end[0] - o.start[0]) / Lo, (o.end[1] - o.start[1]) / Lo
                tb = sorted(((p[0] - o.start[0]) * vx + (p[1] - o.start[1]) * vy) for p in (b.start, b.end))
                if tb[0] > Lo + slack or tb[1] < -slack:
                    continue                      # a gap between them: left as they are
                lo_t, hi_t = min(0.0, tb[0]), max(Lo, tb[1])
                o.start, o.end = (o.start[0] + vx * lo_t, o.start[1] + vy * lo_t), \
                                 (o.start[0] + vx * hi_t, o.start[1] + vy * hi_t)
                o.mark = o.mark or b.mark
                gone.add(id(b))
                break
            moved_ids.add(id(b))
            break
    if gone:
        res.beams[:] = [x for x in res.beams if id(x) not in gone]
    if moved:
        res.add(INFO, "geometry",
                f"{moved} lone face lines carry on a beam whose faces were broken further along; each "
                "was put on that beam's centreline with its width, instead of half a width off it.")
    if absorbed:
        res.add(INFO, "geometry",
                f"{absorbed} beams were carried on along a face line drawn beside them, broken where "
                "another beam meets it, instead of a second beam being laid over them.")


def _drop_end_caps(res: Result, tol, beam_tags=()) -> None:
    """Remove the line closing the end of a beam drawn as two faces.

    A wide girder drawn as two face lines is often closed at its end by a short
    line across the band (J1153 Wing C 1st floor, GB2: a 750 line across its
    750 band, drawn as one polyline with a face). Once the faces are one beam,
    that line is left as a beam of its own: 750 long, no width, no mark, lying
    across the girder's end - an extra beam in the model.

    Narrow on purpose: no width or mark of its own, across a beam merged from two
    faces, as long as that beam is wide (within a tenth), its middle on the beam's
    end, and no mark nearer to it than to the beam.
    """
    slack = max(tol.get("rect_side_tolerance_mm", 5.0), 25.0)
    tag_limit = tol.get("tag_assign_limit_mm", 700.0)
    merged = [k for k in res.beams if k.width and getattr(k, "_face_ends", None)]
    drop = []
    for b in res.beams:
        if b.width or b.mark or b.length < 1:
            continue
        for k in merged:
            if k is b or _angle_between(b, k) < 60.0:
                continue
            if abs(b.length - k.width) > max(slack, 0.1 * k.width):
                continue
            m = _mid(b)
            if min(G.dist(m, k.start), G.dist(m, k.end)) > max(slack, 0.25 * k.width):
                continue
            if any(G.point_to_segment(pt, b.start, b.end) <= tag_limit and
                   G.point_to_segment(pt, b.start, b.end) < G.point_to_segment(pt, k.start, k.end)
                   for _m, pt, _h, _l in beam_tags):
                continue
            drop.append(b)
            k.notes.append("the line closing this beam's end was dropped (not a beam of its own)")
            break
    if not drop:
        return
    ids = {id(b) for b in drop}
    res.beams[:] = [b for b in res.beams if id(b) not in ids]
    res.add(INFO, "geometry",
            f"{len(drop)} short lines closing the end of a beam drawn as two faces were not "
            "read as beams of their own.", x=_mid(drop[0])[0], y=_mid(drop[0])[1])


def _drop_opening_outlines(res: Result, tol) -> None:
    """Remove the outline of a cut-out drawn on the beam layer.

    A shaft or duct is drawn as a box with a cross from corner to corner; the cross
    makes it an opening. Some drafters draw the box on the beam layer (J1153 Wing C
    3rd floor: a 740 x 1200 box open against B73, a 500 x 323 box on B16), and its
    sides were built as beams - plain lines as unsized beams, two opposite sides as
    one 500 mm "beam" lying in the hole.

    Narrow on purpose: run after the marks, a marked beam is never touched; only
    straight beams; only openings (not stairs). A beam goes when the lines it was
    read from lie along the opening's outline for most of their length: the line
    itself if it has no width, both drawn faces if it has one. A beam framing a
    cut-out shares one face with it at most, and is kept.

    The same for a concrete wall drawn again on the beam layer: J1153 Wing C 3rd floor
    draws the lift core's walls (LW1/LW2, a block on COLUMN) and also lines on BEAM
    along them - the middle wall's two faces became a 230 "beam", the top wall's inner
    face and the right wall's outer face plain-line beams, a ring of beams over the
    lift. A beam whose lines lie within the wall is that wall. Brick walls are left
    out: a beam over a brick wall is a beam.
    """
    from shapely.geometry import LineString, Point, Polygon
    from .slabs import MASONRY
    drawn_lines = res.stats.pop("_drawn_lines", {})
    holes = []                      # (bounds, area its lines must lie in, "opening" / "wall")
    for o in res.openings:
        if o.kind != "opening" or len(o.outline) < 3:
            continue
        try:
            poly = Polygon(o.outline)
            if poly.area > 0:
                holes.append((poly.bounds, poly.exterior.buffer(30.0), "opening"))
        except Exception:
            continue
    for w in res.walls:
        # only a wall the drawing names (LW1...): an unnamed outline on a column layer
        # holds real beams - Villa 5's lift beams GB15/GB29, Sasan Gir plinth GB8
        if not w.mark or len(w.outline) < 3 or MASONRY.search(w.source_layer or ""):
            continue
        try:
            poly = Polygon(w.outline).buffer(0)
            # a "wall" with a slab mark in it is an area, not a wall (slabs.derive_panels)
            if poly.area > 0 and not any(poly.contains(Point(t.x, t.y)) for t in res.slab_tags):
                holes.append((poly.bounds, poly.buffer(30.0, join_style=2), "wall"))
        except Exception:
            continue
    if not holes:
        return

    def on_edge(a, b, bounds, edge):
        line = LineString([a, b])
        if line.length < 1:
            return False
        x0, y0, x1, y1 = line.bounds
        if x1 < bounds[0] - 50 or x0 > bounds[2] + 50 or y1 < bounds[1] - 50 or y0 > bounds[3] + 50:
            return False
        return line.intersection(edge).length >= 0.6 * line.length

    drop = {"opening": [], "wall": []}
    edge_lines = []                 # (layer, a, b): the dropped cut-out edges, as drawn
    for b in res.beams:
        if b.mark or b.mid is not None or b.length < 1:
            continue
        if b.width:
            ux, uy = b.end[0] - b.start[0], b.end[1] - b.start[1]
            n = math.hypot(ux, uy)
            ox, oy = -uy / n * b.width / 2.0, ux / n * b.width / 2.0
            lines = [((b.start[0] + s * ox, b.start[1] + s * oy), (b.end[0] + s * ox, b.end[1] + s * oy))
                     for s in (1, -1)]
        else:
            lines = [(b.start, b.end)]
        kind = next((k for bounds, edge, k in holes
                     if all(on_edge(a, c, bounds, edge) for a, c in lines)), None)
        if kind is None and not b.width:
            # a plain line joining stretched to the next beam (Wing C 3rd floor: a duct's
            # 432 edge under B47, carried 790 mm on to B58): the line as drawn, in line with it
            own = [(p, q) for p, q in drawn_lines.get(b.handle, ())
                   if G.point_to_line(p, b.start, b.end) <= 5.0 and G.point_to_line(q, b.start, b.end) <= 5.0]
            kind = next((k for bounds, edge, k in holes if k == "opening"
                         and own and all(on_edge(p, q, bounds, edge) for p, q in own)), None)
            if kind is not None:
                lines = own
        if kind is None:
            continue
        drop[kind].append(b)
        if kind == "opening":
            # still drawn lines: the slab bays stop at them (slabs.derive_panels), so a bay
            # with no mark is not joined to the marked one across the cut-out
            res.stats.setdefault("_opening_edges", []).extend(lines)
            edge_lines.extend((b.source_layer, p, q) for p, q in lines)
    ids = {id(b) for k in drop for b in drop[k]}
    if not ids:
        return
    res.beams[:] = [b for b in res.beams if id(b) not in ids]
    _carry_beams_along_cutout_edges(res, tol, edge_lines)
    if drop["opening"]:
        m = _mid(drop["opening"][0])
        res.add(INFO, "geometry",
                f"{len(drop['opening'])} unmarked lines on the beam layer are the outline of a "
                "cut-out (a box with a cross from corner to corner). They are its edge, not beams, "
                "and were not built. If one is a beam, give it its mark on the drawing.",
                x=m[0], y=m[1])
    if drop["wall"]:
        m = _mid(drop["wall"][0])
        res.add(INFO, "geometry",
                f"{len(drop['wall'])} unmarked lines on the beam layer lie within a concrete wall "
                "(a lift or stair core drawn on the beam layer as well). They are the wall, not "
                "beams, and were not built. If one is a beam, give it its mark on the drawing.",
                x=m[0], y=m[1])


def _carry_beams_along_cutout_edges(res: Result, tol, edge_lines) -> None:
    """Carry a beam on along a cut-out edge that is also its own face.

    Sasan Gir terrace, the duct between B12 and B7 at column B17c: the duct's two
    long sides are B12's lower face and B7's upper face. Each of those faces is
    broken where an SB comes in; past the break, the face piece paired with the
    duct's other side into a 610 "beam" lying in the hole (dropped above as the
    cut-out's outline). B12 and B7 were left with one face whole to the column and
    the other face short, and ended half way between - 1.3 m short of B17c.

    Narrow on purpose: a straight beam merged from two faces, whose end touches
    nothing (no beam, column or wall); a dropped edge on the same layer lying on
    that beam's face line; and the beam is carried on only as far as BOTH its faces
    are drawn without a break past its end (the edge on one side, its own other
    face on the other). It stops at the first column, wall or beam it meets, and
    never where another beam already lies in line.
    """
    if not edge_lines:
        return
    slack = max(tol.get("rect_side_tolerance_mm", 5.0), 15.0)
    max_ang = tol.get("face_pair_max_angle_deg", 3.0)
    supports = [o.outline for o in list(res.columns) + list(res.walls) if len(o.outline) >= 3]
    edges = [(o[i - 1], o[i]) for o in supports for i in range(len(o))]
    carried = []
    for k in res.beams:
        fe = list(getattr(k, "_face_ends", None) or [])
        if not k.width or k.mid is not None or k.centreline_source != "face-pair" or len(fe) < 4:
            continue
        L = k.length
        if L < 1:
            continue
        ux, uy = (k.end[0] - k.start[0]) / L, (k.end[1] - k.start[1]) / L
        h = k.width / 2.0

        def t_of(p):
            return (p[0] - k.start[0]) * ux + (p[1] - k.start[1]) * uy

        def side(p):
            return -(p[0] - k.start[0]) * uy + (p[1] - k.start[1]) * ux

        def on_face(p, q):
            return all(abs(abs(side(x)) - h) <= slack for x in (p, q)) and side(p) * side(q) > 0

        mine = [(fe[i], fe[i + 1]) for i in range(0, len(fe) - 1, 2) if on_face(fe[i], fe[i + 1])]
        cut = [(p, q) for lay, p, q in edge_lines if lay == k.source_layer and on_face(p, q)
               and abs(math.degrees(math.atan2(abs(side(q) - side(p)), abs(t_of(q) - t_of(p)) or 1e-9))) <= max_ang]
        if not cut:
            continue
        for which in ("end", "start"):
            pt = k.end if which == "end" else k.start
            sgn = 1.0 if which == "end" else -1.0
            t0 = L if which == "end" else 0.0
            # the end already rests on something: it is where the drawing ends it
            if any(o is not k and G.point_to_segment(pt, o.start, o.end) <= 30.0 for o in res.beams) or \
                    any(G.point_in_polygon(pt, o) or
                        min(G.point_to_segment(pt, o[i - 1], o[i]) for i in range(len(o))) <= 30.0
                        for o in supports):
                continue

            def reach(lines):
                """how far past t0 a face drawn without a break runs (0 if none at t0)"""
                spans = sorted(tuple(sorted((sgn * t_of(p), sgn * t_of(q)))) for p, q in lines)
                at = sgn * t0
                got = None
                for s0, s1 in spans:
                    if s0 <= at + slack and s1 > at - slack:
                        got = s1 if got is None else max(got, s1)
                if got is None:
                    return 0.0
                for s0, s1 in spans:          # pieces end to end
                    if s0 <= got + slack:
                        got = max(got, s1)
                return got - at

            best = 0.0
            for cs in (1, -1):                # the cut-out edge on one side, own face on the other
                same = [(p, q) for p, q in cut if side(p) * cs > 0]
                other = [(p, q) for p, q in mine if side(p) * cs < 0]
                if same and other:
                    best = max(best, min(reach(same + [(p, q) for p, q in mine if side(p) * cs > 0]), reach(other)))
            if best <= max(slack, 50.0):
                continue
            # stop at the first column, wall or beam the carried stretch meets
            dx, dy = sgn * ux, sgn * uy
            stop = best
            for a, c in edges + [(o.start, o.end) for o in res.beams if o is not k]:
                vx, vy = c[0] - a[0], c[1] - a[1]
                den = dx * vy - dy * vx
                if abs(den) < 1e-12:
                    continue
                wx, wy = a[0] - pt[0], a[1] - pt[1]
                t = (wx * vy - wy * vx) / den
                u = (wx * dy - wy * dx) / den
                if 1.0 < t < stop and -1e-9 <= u <= 1.0 + 1e-9:
                    stop = t
            # another beam already lying in line along the stretch: leave it to that beam
            if any(o is not k and o.mid is None and _angle_between(o, k) <= max_ang and
                   G.point_to_line(_mid(o), k.start, k.end) <= h and
                   max(sgn * t_of(o.start), sgn * t_of(o.end)) > sgn * t0 + slack and
                   min(sgn * t_of(o.start), sgn * t_of(o.end)) < sgn * t0 + stop - slack
                   for o in res.beams):
                continue
            new = (pt[0] + dx * stop, pt[1] + dy * stop)
            if which == "end":
                k.end = new
            else:
                k.start = new
            k.notes.append(f"carried on {stop:.0f} mm along a cut-out's edge that is also its face, "
                           "as far as both its faces are drawn")
            carried.append(k)
    if carried:
        m = _mid(carried[0])
        res.add(INFO, "geometry",
                f"{len(carried)} beams frame a cut-out whose edge is also the beam's face. They were "
                "carried on as far as both their faces are drawn, instead of stopping short where "
                "the face was broken by a beam coming in.", x=m[0], y=m[1])


def _drop_orphan_faces(res: Result, tol, beam_tags=()) -> None:
    """Remove a face line whose partner was merged into a beam without it.

    A beam drawn as two faces normally merges into one. Sometimes only one of the
    two finds its partner - the other face is a different length, or was drawn as
    two segments, or a third line runs alongside - and the loser survives as a
    beam in its own right: no width, no mark, lying exactly half a beam width off
    the centreline of a beam that is already in the model.

    In Revit that is a thin extra beam pressed against a real one, ending in mid
    air. Nine of the nineteen open beam ends on the Wing B plan were these, every
    one of them 115 mm - half of 230 - from its neighbour.

    The test is deliberately narrow: no width of its own, parallel, overlapping,
    and offset by half the other beam's width to within a few millimetres. A beam
    that measured its own width is never dropped, whatever it sits next to.
    """
    slack = max(tol.get("rect_side_tolerance_mm", 5.0), 15.0)
    max_ang = tol.get("face_pair_max_angle_deg", 3.0)
    min_ov = tol.get("face_pair_min_overlap", 0.5)
    # This runs before the network is joined, so the beam an orphan belongs to has
    # not been stretched to its neighbours yet and the two spans need not yet
    # coincide. Allow for the stretch that is about to happen, or every orphan
    # measures zero overlap and none is ever recognised.
    pad = tol.get("beam_join_snap_mm", 800.0)
    tag_limit = tol.get("tag_assign_limit_mm", 700.0)

    def is_tagged(b, k):
        """Is there a mark that belongs to this line rather than to its neighbour?

        This runs before the marks are matched, so a line's own `mark` is still
        empty and cannot be used. But the tags are already read, and a tag that
        sits closer to this line than to the beam it would be merged into is the
        consultant saying this line is an element in its own right. Dropping it
        would delete a tagged beam, which is never acceptable.
        """
        # b carried on past k's end along k's face, touching it (ALCAZAR 1st floor: a
        # 300 mm line taking B26's face on to the outer corner was kept as a beam of
        # its own because BC7, written out on the balcony, was a little nearer to it,
        # and stood off the corner as a stub): it is k's face, so a mark protects it
        # only when it is nearer to it by more than k's width
        L = k.length
        ux, uy = ((k.end[0] - k.start[0]) / L, (k.end[1] - k.start[1]) / L) if L > 1 else (0.0, 0.0)
        tb = sorted(((p[0] - k.start[0]) * ux + (p[1] - k.start[1]) * uy) for p in (b.start, b.end))
        beyond = L > 1 and (tb[0] >= L - slack and tb[0] - L <= 50.0 or tb[1] <= slack and -tb[1] <= 50.0)
        margin = k.width if beyond else 0.0
        ks, ke = k.start, k.end
        if beyond:                         # k as it will be, carried on over b
            lo, hi = min(0.0, tb[0]), max(L, tb[1])
            ks = (k.start[0] + ux * lo, k.start[1] + uy * lo)
            ke = (k.start[0] + ux * hi, k.start[1] + uy * hi)
        for _mark, pt, _h, _lay in beam_tags:
            d_self = G.point_to_segment(pt, b.start, b.end)
            if d_self > tag_limit:
                continue
            if d_self + margin >= G.point_to_segment(pt, ks, ke):
                continue
            # nearer still to a third beam: the tag is that beam's (New block: NB7 written
            # beside the vertical NB7, 388 mm off, kept a leftover face 623 mm off as a
            # beam of its own, half a width off the beam it belongs to)
            if any(o is not b and o is not k and G.point_to_segment(pt, o.start, o.end) < d_self
                   for o in res.beams):
                continue
            return True
        return False

    sized = [b for b in res.beams if b.width]
    drop: list[Beam] = []
    for b in res.beams:
        if b.width or b.mark:
            continue                      # it knows its own width, or it is tagged
        for k in sized:
            if k is b:
                continue
            if _angle_between(b, k) > max_ang:
                continue
            off = G.point_to_line(_mid(b), k.start, k.end)
            if abs(off - k.width / 2.0) > slack:
                continue
            if _overlap_fraction(b, k, pad) < min_ov:
                continue
            if is_tagged(b, k):
                continue
            drop.append(b)
            k.notes.append("a leftover face line alongside this beam was dropped")
            _extend_over_leftover(k, b)
            break

    if not drop:
        return
    ids = {id(b) for b in drop}
    res.beams[:] = [b for b in res.beams if id(b) not in ids]
    p = drop[0]
    res.add(WARNING, "geometry",
            f"{len(drop)} lines are one face of a beam whose other face has already been "
            "merged into it. They carry no width and no mark, and sit exactly half a beam "
            "width off the centreline of a beam that is already in the model, so they have "
            "been dropped. Left in, each becomes a thin extra beam pressed against a real "
            "one, ending in mid air.",
            x=p.start[0], y=p.start[1])


def _join_beam_network(res: Result, tol) -> None:
    """Extend or trim each beam centreline to where it meets its neighbours.

    Moving a beam from its drawn face to its true centreline shifts it sideways by
    half its width, which pulls every corner apart by that much. The centrelines
    still cross, but the ENDS no longer sit on the crossing. This walks each end to
    the nearest intersection so the frame closes up again, which is what Revit needs
    to join a beam to its supports.
    """
    snap = tol.get("beam_join_snap_mm", 400.0)
    min_angle = tol.get("beam_join_min_angle_deg", 15.0)
    overshoot = tol.get("beam_join_overshoot_mm", 300.0)
    beams = res.beams
    if len(beams) < 2:
        return

    original = [(b.start, b.end) for b in beams]
    moved = 0
    total_shift = 0.0

    # An end that already sits on a column or wall is on its support and stays
    # there. Without this a beam drawn down to the top of a column, with a cross
    # beam 560 mm short of it, was trimmed back to the cross beam - leaving the
    # column (Wing B: 21, 23, 25, 3A) hanging off one beam line instead of two.
    supports = [c.outline for c in res.columns if len(c.outline) >= 3]
    supports += [w.outline for w in res.walls if len(w.outline) >= 3]

    def support_of(p):
        return next((o for o in supports if G.point_in_polygon(p, o)), None)

    def face_of(p, tol_mm=25.0):
        # drawn up to a column's face (ALCAZAR B98 to column 9): tested as inside, the
        # face is outside by a hair, and B98 was cut back 238 mm to the cross beam - a
        # gap under the column the drawing does not have. Such an end is not pulled
        # back off the column; moving it out to a crossing stays as it always was
        return next((o for o in supports if any(
            G.point_to_segment(p, o[k], o[(k + 1) % len(o)]) <= tol_mm for k in range(len(o)))), None)

    for i, b in enumerate(beams):
        a1, b1 = original[i]
        L1 = G.dist(a1, b1)
        new = {}
        for which, pt in (("start", a1), ("end", b1)):
            # it may still move to a crossing INSIDE the same column, which is
            # where two beams meet over it, but never off it
            held = support_of(pt)
            at_face = face_of(pt) if held is None else None
            best = None
            for j, (a2, b2) in enumerate(original):
                if j == i:
                    continue
                x = G.line_intersection(a1, b1, a2, b2, min_angle)
                if x is None:
                    continue
                gap = G.dist(pt, x)
                if gap > snap:
                    continue
                # The crossing has to be on the neighbour's actual length, not on
                # the infinite line through it. Two beams on the same gridline
                # thirty metres apart still have a mathematical intersection, and
                # snapping an end to one of those is how a beam ends up pointing
                # at nothing. A small overshoot is allowed so a beam can still
                # frame into the very end of another.
                if G.point_to_segment(x, a2, b2) > overshoot:
                    continue
                # the crossing has to be at THIS end, not the far one
                if G.dist(x, b1 if which == "start" else a1) < G.dist(x, pt):
                    continue
                # Pulling an end BACK along the beam is only ever undoing the
                # half-width overshoot left by moving it to its centreline. A
                # crossing further inside than that is a beam framing in part way
                # along this one, and cutting back to it throws away real beam.
                if L1 > 1e-9:
                    far = b1 if which == "start" else a1
                    inward = ((x[0] - pt[0]) * (far[0] - pt[0])
                              + (x[1] - pt[1]) * (far[1] - pt[1])) > 0
                    if inward and gap > overshoot:
                        continue
                    if inward and at_face is not None and not G.point_in_polygon(x, at_face):
                        continue
                if held is not None and not G.point_in_polygon(x, held):
                    continue
                if best is None or gap < best[0]:
                    best = (gap, x)
            if best and best[0] > 1e-6:
                new[which] = best

        if not new:
            continue
        ns = new.get("start", (0, a1))[1]
        ne = new.get("end", (0, b1))[1]
        if G.dist(ns, ne) < tol["beam_min_length_mm"]:
            continue                       # refuse to collapse a beam
        # direction must not reverse
        if ((ne[0] - ns[0]) * (b1[0] - a1[0]) + (ne[1] - ns[1]) * (b1[1] - a1[1])) <= 0:
            continue
        shift = G.dist(b.start, ns) + G.dist(b.end, ne)
        b.start, b.end = ns, ne
        moved += 1
        total_shift += shift

    if moved:
        res.add(INFO, "geometry",
                f"{moved} beam and girder centrelines were extended or trimmed to meet "
                f"their neighbours, closing gaps of up to {snap:.0f} mm left by moving "
                "each beam from its drawn face to its centre.")



def _extend_beams_to_supports(res: Result, tol) -> None:
    """Walk a beam end that still floats free out to whatever carries it.

    The join before this one closes a corner where two centrelines cross. It
    cannot help a beam that lands on a column rather than on another beam: there
    is no crossing to find, and moving from the drawn face to the centreline has
    left the end short of its own support.

    So each free end is walked OUTWARD ALONG ITS OWN AXIS - never sideways, which
    would be inventing a position - until it enters a column or wall outline or
    reaches another beam. If it reaches nothing inside the snap distance it is
    left exactly where it was and reported, because a gap that cannot be closed
    along the beam's own line is a gap in the drawing, not something to paper
    over.
    """
    snap = tol.get("beam_support_snap_mm", tol.get("beam_join_snap_mm", 800.0))
    outlines = [c.outline for c in res.columns if len(c.outline) >= 3]
    outlines += [w.outline for w in res.walls if len(w.outline) >= 3]
    segs = [(b.start, b.end) for b in res.beams]

    edges = []
    for o in outlines:
        for k in range(len(o)):
            edges.append((o[k], o[(k + 1) % len(o)]))

    def landed(p, skip):
        for k, (a, c) in enumerate(segs):
            if k != skip and G.point_to_segment(p, a, c) < 1.0:
                return True
        return any(G.point_in_polygon(p, o) for o in outlines)

    def ray_hit(p, ux, uy, skip):
        """Distance along the ray to the first thing it meets, or None.

        Exact, rather than walking out in steps: a step walk has to choose a
        stride, and whatever it chooses it strides straight over any gap smaller
        than that. A 6 mm gap is invisible on the drawing and is still a beam
        that will not join in Revit.
        """
        best = None
        targets = [(a, c) for k, (a, c) in enumerate(segs) if k != skip] + edges
        for a, c in targets:
            vx, vy = c[0] - a[0], c[1] - a[1]
            den = ux * vy - uy * vx
            if abs(den) < 1e-12:
                continue                      # parallel: no single crossing
            wx, wy = a[0] - p[0], a[1] - p[1]
            t = (wx * vy - wy * vx) / den      # along the ray
            u = (wx * uy - wy * ux) / den      # along the target segment
            if t <= 1e-9 or t > snap:
                continue
            if u < -1e-9 or u > 1.0 + 1e-9:
                continue                      # crossing is off the end of it
            if best is None or t < best:
                best = t
        return best

    moved, stuck = 0, []
    for i, b in enumerate(res.beams):
        L = G.dist(b.start, b.end)
        if L < 1e-6:
            continue
        ux = (b.end[0] - b.start[0]) / L
        uy = (b.end[1] - b.start[1]) / L
        for which in ("start", "end"):
            pt = b.start if which == "start" else b.end
            if landed(pt, i):
                continue
            sgn = -1.0 if which == "start" else 1.0
            d = ray_hit(pt, sgn * ux, sgn * uy, i)
            if d is None:
                stuck.append((b, pt))
                continue
            hit = (pt[0] + sgn * ux * d, pt[1] + sgn * uy * d)
            if which == "start":
                b.start = hit
            else:
                b.end = hit
            segs[i] = (b.start, b.end)
            b.notes.append(f"end extended {d:.0f} mm along its own line to reach "
                           "its support")
            moved += 1

    if moved:
        res.add(INFO, "geometry",
                f"{moved} beam ends were extended along their own line to reach the "
                "column, wall or beam that carries them. Moving a beam from its drawn "
                "face to its centreline leaves it short of its own support, and Revit "
                "cannot join a beam to something it does not touch.")
    if stuck:
        worst = max(stuck, key=lambda sp: 0.0)[1]
        res.add(WARNING, "geometry",
                f"{len(stuck)} beam ends still touch nothing. Extending them along their "
                f"own line finds no column, wall or beam within {snap:.0f} mm, so they "
                "have been left exactly as drawn rather than moved sideways to the "
                "nearest thing. Each one is a gap in the drawing: check them in CAD.",
                x=worst[0], y=worst[1])


def _carry_past_side_beams(res: Result, tol) -> None:
    """Carry a beam on past a beam that comes in from one side, to its support.

    Sasan Gir terrace, B25c: its left face stops where B6 comes in from the left;
    its right face runs on 570 mm past B6, to the column under it. The faces' ends
    averaged, and the join then took the end back to B6's centreline - the last
    450 mm to the column were lost and slab S12 ran round through the gap.

    Narrow on purpose: a straight beam merged from two faces, ending at a beam
    that comes in from ONE side (not across it); the face on that side ends at the
    side beam's near face, the other face runs on clearly past its far face. The
    beam is carried on only to the first column, wall or beam it meets within the
    face's length - none met, it is left alone - and never where another beam
    already lies in line. An L corner (the outer face ending at the far face of
    the beam it turns into) is not touched.
    """
    slack = max(tol.get("rect_side_tolerance_mm", 5.0), 15.0)
    max_ang = tol.get("face_pair_max_angle_deg", 3.0)
    supports = [o.outline for o in list(res.columns) + list(res.walls) if len(o.outline) >= 3]
    edges = [(o[i - 1], o[i]) for o in supports for i in range(len(o))]
    carried = []
    for k in res.beams:
        fe = list(getattr(k, "_face_ends", None) or [])
        if not k.width or k.mid is not None or k.centreline_source != "face-pair" or len(fe) < 4:
            continue
        L = k.length
        if L < 1:
            continue
        ux, uy = (k.end[0] - k.start[0]) / L, (k.end[1] - k.start[1]) / L
        h = k.width / 2.0

        def t_of(p):
            return (p[0] - k.start[0]) * ux + (p[1] - k.start[1]) * uy

        def side(p):
            return -(p[0] - k.start[0]) * uy + (p[1] - k.start[1]) * ux

        faces = {1: [], -1: []}
        for i in range(0, len(fe) - 1, 2):
            p, q = fe[i], fe[i + 1]
            if side(p) * side(q) > 0 and all(abs(abs(side(x)) - h) <= slack for x in (p, q)):
                faces[1 if side(p) > 0 else -1].append((t_of(p), t_of(q)))
        if not faces[1] or not faces[-1]:
            continue
        for which in ("end", "start"):
            sgn = 1.0 if which == "end" else -1.0
            pt = k.end if which == "end" else k.start
            at = sgn * (L if which == "end" else 0.0)
            reach = {s: max(max(sgn * a, sgn * b) for a, b in faces[s]) for s in (1, -1)}
            far_s = 1 if reach[1] > reach[-1] else -1
            far, near = reach[far_s], reach[-far_s]
            if far - at <= max(slack, 50.0):
                continue
            # the beam coming in on the short face's side, its near face where that face stops
            o = tc = None
            for b in res.beams:
                if b is k or b.mid is not None or not b.width or _angle_between(b, k) < 60.0:
                    continue
                x = _cross_point(k, b)
                if x is None:
                    continue
                t = sgn * t_of(x)
                if abs(t - b.width / 2.0 - near) <= slack and abs(t - at) <= b.width / 2.0 + slack and \
                        min(side(b.start) * far_s, side(b.end) * far_s) < -h and \
                        max(side(b.start) * far_s, side(b.end) * far_s) <= h + slack:
                    o, tc = b, t
                    break
            if o is None:
                continue
            if far <= tc + o.width / 2.0 + 100.0:
                continue                      # an L corner: the outer face ends at o's far face
            dx, dy = sgn * ux, sgn * uy
            stop = None
            for a, c in edges + [(b.start, b.end) for b in res.beams if b is not k and b is not o]:
                vx, vy = c[0] - a[0], c[1] - a[1]
                den = dx * vy - dy * vx
                if abs(den) < 1e-12:
                    continue
                wx, wy = a[0] - pt[0], a[1] - pt[1]
                t = (wx * vy - wy * vx) / den
                u = (wx * dy - wy * dx) / den
                if 1.0 < t <= far - at + slack and -1e-9 <= u <= 1.0 + 1e-9 and (stop is None or t < stop):
                    stop = t
            if stop is None:
                continue                      # nothing carries it there: left as it was
            if any(b is not k and b.mid is None and _angle_between(b, k) <= max_ang and
                   G.point_to_line(_mid(b), k.start, k.end) <= h and
                   max(sgn * t_of(b.start), sgn * t_of(b.end)) > at + slack and
                   min(sgn * t_of(b.start), sgn * t_of(b.end)) < at + stop - slack
                   for b in res.beams):
                continue
            new = (pt[0] + dx * stop, pt[1] + dy * stop)
            if which == "end":
                k.end = new
            else:
                k.start = new
            k.notes.append(f"carried on {stop:.0f} mm past {o.mark or 'the beam'} coming in from one side, "
                           "as far as its other face runs, to its support")
            carried.append(k)
    if carried:
        m = _mid(carried[0])
        res.add(INFO, "geometry",
                f"{len(carried)} beams have one face broken where a beam comes in from that side while "
                "the other face runs on to a column or beam. They were carried on to it instead of "
                "stopping at the beam coming in.", x=m[0], y=m[1])


def _trim_over_neighbour(res: Result, tol, marked_only=False) -> None:
    """End a beam where the beam sharing its long face takes over.

    Sasan Gir terrace, the B3 / B10 line: the bottom face is one 35 m line; the top
    face is 300 mm above it right of the column at 803725 (B3a, 300 x 750) and 230
    above it to the left (B10f, 230 x 600). B3a paired the long bottom face with its
    own top face, ended half way between their ends - 4.4 m past its column - and
    lay over B10f there, two beams in one place.

    Narrow on purpose: a straight beam merged from two faces, one of which stops
    short of the beam's end; along the stretch past that face's end lies another
    beam, parallel, whose own edge is this beam's long face, from (within this
    beam's width of) the face's end on. This beam then ends where that one begins.

    Before the marks, only a beam of another width counts: one as wide may be the
    same beam drawn in pieces (B29c / B30c), joined later. After the marks
    (marked_only), one as wide counts when the two carry different marks - two beams
    drawn on past each other (J1153 Wing C 3rd BC3 / B88, Wing C 2nd B6 / B5).
    """
    slack = max(tol.get("rect_side_tolerance_mm", 5.0), 15.0)
    max_ang = tol.get("face_pair_max_angle_deg", 3.0)
    trimmed = 0
    for k in res.beams:
        fe = list(getattr(k, "_face_ends", None) or [])
        if not k.width or k.mid is not None or k.centreline_source != "face-pair" or len(fe) < 4:
            continue
        L = k.length
        if L < 1:
            continue
        ux, uy = (k.end[0] - k.start[0]) / L, (k.end[1] - k.start[1]) / L
        h = k.width / 2.0

        def t_of(p):
            return (p[0] - k.start[0]) * ux + (p[1] - k.start[1]) * uy

        def side(p):
            return -(p[0] - k.start[0]) * uy + (p[1] - k.start[1]) * ux

        faces = {1: [], -1: []}
        for i in range(0, len(fe) - 1, 2):
            p, q = fe[i], fe[i + 1]
            if side(p) * side(q) > 0 and all(abs(abs(side(x)) - h) <= slack for x in (p, q)):
                faces[1 if side(p) > 0 else -1].append(sorted((t_of(p), t_of(q))))
        if not faces[1] or not faces[-1]:
            continue
        for which in ("end", "start"):
            sgn = 1.0 if which == "end" else -1.0
            at = sgn * (L if which == "end" else 0.0)
            reach = {s: max(max(sgn * a, sgn * b) for a, b in faces[s]) for s in (1, -1)}
            long_s = 1 if reach[1] > reach[-1] else -1
            short = reach[-long_s]
            if at - short <= max(k.width, 100.0):
                continue                      # it does not run on past its short face
            for b in res.beams:
                if b is k or b.mid is not None or not b.width or _angle_between(b, k) > max_ang:
                    continue
                # a different beam, of another width; one as wide is the same beam drawn
                # in pieces (B29c / B30c), joined later by _join_overlapping_spans - unless
                # both carry marks, and different ones
                as_wide = abs(b.width - k.width) <= max(slack, 0.05 * k.width)
                if marked_only:
                    if not (as_wide and k.mark and b.mark and k.mark != b.mark):
                        continue
                elif as_wide:
                    continue
                off = side(_mid(b))
                # b's edge on k's long face, and b lying on k's side of it (over k's band)
                if abs(off - long_s * (h - b.width / 2.0)) > slack:
                    continue
                tb = sorted((sgn * t_of(b.start), sgn * t_of(b.end)))
                # b starts at k's short face end (within k's width) and covers the stretch beyond
                if abs(tb[0] - short) > max(k.width, 150.0) + slack or tb[1] < at - slack:
                    continue
                if at - tb[0] <= slack:
                    break                             # it already ends there (J1153 B wing B32)
                new_t = sgn * tb[0]
                p = (k.start[0] + ux * new_t, k.start[1] + uy * new_t)
                if which == "end":
                    k.end = p
                else:
                    k.start = p
                k.notes.append(f"ends where the beam sharing its long face takes over ({at - tb[0]:.0f} mm "
                               "less): its other face stops there")
                trimmed += 1
                break
    if trimmed:
        res.add(INFO, "geometry",
                f"{trimmed} beams ran on past the end of one of their faces, over the next beam along "
                "the same line (a different width sharing the other face). Each now ends where that "
                "beam begins.")


def _cross_point(k, o):
    """Where o's centreline meets k's line, if o reaches it (within half k's width), else None."""
    x = G.line_intersection(k.start, k.end, o.start, o.end, 30.0)
    if x is None or G.point_to_segment(x, o.start, o.end) > k.width / 2.0 + 30.0:
        return None
    return x


def _split_beams_at_crossings(res: Result, tol) -> None:
    """Cut each beam line into spans wherever another beam crosses it.

    A drafter draws one continuous line across several bays; the schedule treats
    each bay as its own beam with its own depth and reinforcement, and gives each
    a mark. Revit needs the same, one element per span between supports. Without
    this, a line carrying two marks can only keep one of them.
    """
    if not tol.get("split_beams_at_crossings", True) or len(res.beams) < 2:
        return

    min_span = tol["beam_min_length_mm"]
    out: list[Beam] = []
    split_count = 0

    segs = [(b.start, b.end) for b in res.beams]
    for i, b in enumerate(res.beams):
        a1, b1 = segs[i]
        L = G.dist(a1, b1)
        if L <= 0:
            continue
        ux, uy = (b1[0] - a1[0]) / L, (b1[1] - a1[1]) / L

        cuts: list[float] = []
        for j, (a2, b2) in enumerate(segs):
            if i == j:
                continue
            x = G.line_intersection(a1, b1, a2, b2, 15.0)
            if x is None:
                continue
            # must fall inside BOTH beams, not past an end
            t = (x[0] - a1[0]) * ux + (x[1] - a1[1]) * uy
            if not (min_span <= t <= L - min_span):
                continue
            if G.point_to_segment(x, a2, b2) > 1.0:
                continue
            cuts.append(t)

        if not cuts:
            out.append(b)
            continue

        cuts = sorted(set(round(c, 1) for c in cuts))
        stops = [0.0] + cuts + [L]
        pieces = []
        for s, e in zip(stops, stops[1:]):
            if e - s < min_span:
                continue
            pieces.append(((a1[0] + ux * s, a1[1] + uy * s),
                           (a1[0] + ux * e, a1[1] + uy * e)))
        if len(pieces) < 2:
            out.append(b)
            continue

        split_count += 1
        for k, (ps, pe) in enumerate(pieces):
            if k == 0:
                b.start, b.end = ps, pe
                out.append(b)
            else:
                out.append(Beam(
                    mark="", kind=b.kind, start=ps, end=pe, width=b.width,
                    width_source=b.width_source, centreline_source=b.centreline_source,
                    offset_applied=b.offset_applied, source_layer=b.source_layer,
                    handle=b.handle, notes=list(b.notes) + ["span of a longer drawn line"]))

    if split_count:
        res.beams[:] = out
        res.add(INFO, "geometry",
                f"{split_count} beam lines ran across other beams and were cut into spans, "
                f"giving {len(out)} beams in total. A line drawn across several bays is one "
                "beam per bay in the schedule and in Revit.")


def _girder_mark_test(cfg):
    """A test for girder marks: GB2 is one; B2 or BC2 is not, though they start alike."""
    pre = tuple(p.upper() for p in (cfg or {}).get("mark_prefixes", {}).get("girder", []))

    def test(mark):
        m = (mark or "").upper()
        return any(m.startswith(p) and m[len(p):len(p) + 1].isdigit() for p in pre)
    return test


def _split_beams_at_columns(res: Result, tol, beam_tags, cfg=None) -> None:
    """Cut a beam into spans where a supporting column stands part-way along it.

    Beams that meet a wide girder stop at its face, so they never cross its
    centreline and _split_beams_at_crossings leaves the girder whole. On the
    Wing B plan GB2 and GB3 were one line over the column between them, as were
    GB4 and GB5, and a single line can keep only one of the two marks. Floating
    columns (FC) stand on the girder rather than hold it up, so they are not a
    reason to cut.

    Splitting every beam is off in config (split_beams_at_crossings), because it
    makes spans with no mark. So a line is cut only where it carries marks in two
    or more of the bays the columns divide it into: each piece then has its own.
    """
    limit = tol.get("tag_assign_limit_mm", 700.0)
    min_span = tol["beam_min_length_mm"]
    supports = [c for c in res.columns
                if c.kind != "column_float" and not (c.mark or "").upper().startswith("FC")]
    if not supports:
        return

    # Each mark counts only for the line of its own kind whose face it is nearest,
    # so a small beam's mark beside a girder does not look like a mark on the girder,
    # and GB4, written where beam B62 meets the girder's face, still counts for it.
    def face(p, k):
        return max(0.0, G.point_to_segment(p, k.start, k.end) - (k.width or 0.0) / 2.0)

    is_girder_mark = _girder_mark_test(cfg)
    owner = {}
    for mark, p, _h, _lay in beam_tags:
        gm = is_girder_mark(mark)
        same = [i for i, k in enumerate(res.beams) if (k.kind == "girder") == gm]
        best = min(same, key=lambda i: face(p, res.beams[i]), default=None)
        if best is not None and face(p, res.beams[best]) <= limit:
            owner.setdefault(best, []).append(p)

    out: list[Beam] = []
    split_count = 0
    for bi, b in enumerate(res.beams):
        a1, b1 = b.start, b.end
        L = G.dist(a1, b1)
        if L <= 0:
            out.append(b)
            continue
        ux, uy = (b1[0] - a1[0]) / L, (b1[1] - a1[1]) / L
        half = max(b.width or 0.0, 230.0) / 2.0

        cuts = []
        for c in supports:
            t = (c.x - a1[0]) * ux + (c.y - a1[1]) * uy
            if not (min_span <= t <= L - min_span):
                continue
            if abs((c.x - a1[0]) * uy - (c.y - a1[1]) * ux) > half:
                continue
            cuts.append(round(t, 1))
        if not cuts:
            out.append(b)
            continue

        # Keep only the cuts that separate two marks written along this line.
        along = sorted((p[0] - a1[0]) * ux + (p[1] - a1[1]) * uy for p in owner.get(bi, []))
        cuts = sorted(c for c in set(cuts)
                      if any(t < c for t in along) and any(t > c for t in along))
        stops = [0.0]
        for c in cuts:
            if any(stops[-1] < t < c for t in along):
                stops.append(c)
        stops.append(L)
        pieces = [((a1[0] + ux * s, a1[1] + uy * s), (a1[0] + ux * e, a1[1] + uy * e))
                  for s, e in zip(stops, stops[1:]) if e - s >= min_span]
        if len(pieces) < 2:
            out.append(b)
            continue

        split_count += 1
        for k, (ps, pe) in enumerate(pieces):
            if k == 0:
                b.start, b.end = ps, pe
                out.append(b)
            else:
                out.append(Beam(
                    mark="", kind=b.kind, start=ps, end=pe, width=b.width,
                    width_source=b.width_source, centreline_source=b.centreline_source,
                    offset_applied=b.offset_applied, source_layer=b.source_layer,
                    handle=b.handle, notes=list(b.notes) + ["span either side of a column"]))

    if split_count:
        res.beams[:] = out
        res.add(INFO, "geometry",
                f"{split_count} beam lines ran over a column part-way along and were cut "
                "there into spans, because the line carried a separate mark for each bay.")


def _report_justification(res: Result) -> None:
    for (layer, just, scale), n in (res.stats.pop("_just", {}) or {}).items():
        name = {0: "Top", 2: "Bottom"}.get(just, str(just))
        res.add(WARNING, "geometry",
                f"{n} multilines on '{layer}' are justified {name}, so the drawn line is a "
                f"beam face rather than the centreline. Each centreline has been recovered "
                f"{scale / 2:.0f} mm off the drawn line. Set multiline justification to Zero "
                "so this correction is not needed.", layer=layer)


def _extract_slabs_and_zones(buckets, res: Result, cfg, tol, ents=(), sunk_key=None,
                             sunk_key_lines=None) -> None:
    open_slab_fragments = 0

    fold_lines = []
    for e in buckets["slab"]:
        if e.dxftype() == "LINE":
            fold_lines.append(((e.dxf.start.x, e.dxf.start.y), (e.dxf.end.x, e.dxf.end.y),
                               e.dxf.layer, e.dxf.handle))
            continue
        if e.dxftype() != "LWPOLYLINE":
            continue
        pts = G.dedupe_points(_pts_of_lwpolyline(e), tol["duplicate_tolerance_mm"])
        if len(pts) < 3 or (not e.closed and
                            G.dist(pts[0], pts[-1]) > tol["close_gap_tolerance_mm"]):
            open_slab_fragments += 1
            fold_lines += [(p, q, e.dxf.layer, e.dxf.handle) for p, q in zip(pts, pts[1:])]
            continue
        res.slabs.append(SlabPanel(mark="", outline=pts, source_layer=e.dxf.layer,
                                   handle=e.dxf.handle))

    # The open lines on the slab layer are the slab folds at the sunk edges.
    for p, q, w, lay, h in slab_bays.folds_from_lines(fold_lines):
        res.folds.append(Fold(start=p, end=q, width=w, source_layer=lay, handle=h))

    # Judged once the panels have been looked for between the beams as well.
    res.stats["_open_slab_fragments"] = open_slab_fragments

    override = cfg.get("sunk_depth_override", {})
    samples = sunk_key or []
    legend_src = "legend"
    # The drawing's own legend first. A plan with none (or none of sunk hatches)
    # reads the legend sheet imported with it, if the set has one - still a legend
    # in the drawings, never one built into the tool.
    set_legend = cfg.get("set_legend") or {}
    if not samples and set_legend.get("samples"):
        samples = [tuple(q) for q in set_legend["samples"]]
        legend_src = f"legend sheet {set_legend.get('sheet') or ''}".strip()
        res.add(INFO, "legend",
                f"This drawing has no legend of sunk hatches of its own; the legend sheet "
                f"'{set_legend.get('sheet')}' imported with it is used: " + ", ".join(
                    f"{q[3]:.0f} mm = {q[1]}" for q in samples) + ".")
    if not sunk_key_lines and set_legend.get("lines") and not sunk_key:
        sunk_key_lines = [tuple(q) for q in set_legend["lines"]]
    unknown_depth_layers, not_in_legend, ambiguous = set(), set(), 0
    texts = _sunk_texts(ents)
    from_text = collections.Counter()

    # Sunk areas: hatches on the sunk layers, and on any layer the legend's sunk
    # samples use (Wing B puts its 150 and 400 mm hatches on a layer called
    # HATCH). Not every hatch of a sunk pattern: a column hatch can share one.
    in_role = {id(e) for e in buckets["sunk"]}
    sample_layers = {q[0] for q in samples}
    extra = [e for e in ents if e.dxftype() == "HATCH" and id(e) not in in_role
             and str(e.dxf.layer).upper() in sample_layers]
    zone_hatches = [e for e in list(buckets["sunk"]) + extra if e.dxftype() == "HATCH"]
    factor = legend.legend_scale(samples, zone_hatches) if samples else 1.0
    for e in list(buckets["sunk"]) + extra:
        outline: list[G.Point] = []
        if e.dxftype() == "HATCH":
            for path in e.paths:
                pts = []
                for v in getattr(path, "vertices", []) or []:      # polyline path
                    pts.append((v[0], v[1]))
                if not pts:                                        # edge path
                    pts = _edge_path_points(path)
                if len(pts) >= 3:
                    outline = G.dedupe_points(pts, tol["duplicate_tolerance_mm"])
                    break
        elif e.dxftype() == "LWPOLYLINE":
            outline = G.dedupe_points(_pts_of_lwpolyline(e), tol["duplicate_tolerance_mm"])
        if len(outline) < 3:
            continue
        depth, src = sunk_depth_from_layer(e.dxf.layer, override)
        if src != "override" and samples and e.dxftype() == "HATCH":
            got = legend.depth_for(e, samples, factor)
            if got is not None:
                depth, src = got, legend_src
            elif any(q[1] == str(e.dxf.pattern_name).upper() for q in samples):
                # the legend has this pattern for more than one depth
                depth, src = None, "legend-ambiguous"
                ambiguous += 1
            elif id(e) not in in_role:
                continue                  # a hatch on HATCH the legend does not explain
            else:
                # The drawing has a legend and it does not show this hatch. Its
                # layer name is no guide - on Wing A 2nd floor '900MM SUNK' would
                # have sunk S10A by 900, where the schedule says 400.
                depth, src = None, "not in legend"
        if depth is None:
            # no legend says it: the words written inside the area may ("SUNK 150", "-0.150")
            got = _depth_written_inside(outline, texts)
            if got is not None:
                if src == "legend-ambiguous":
                    ambiguous -= 1
                depth, src = got, "text in the area"
                from_text[depth] += 1
        if src == "not in legend":
            not_in_legend.add(e.dxf.layer)
        elif src not in ("override", legend_src, "text in the area", "legend-ambiguous"):
            unknown_depth_layers.add(e.dxf.layer)
        res.sunk.append(SunkZone(depth_mm=depth, depth_source=src, outline=outline,
                                 source_layer=e.dxf.layer, handle=e.dxf.handle))
    _sunk_from_lines(ents, res, cfg, sunk_key_lines or [], texts, from_text)
    if from_text:
        res.add(INFO, "slabs",
                "Sunk depths read from the words written inside the sunk areas (no legend "
                "explains their hatch): " + ", ".join(
                    f"{n} at {d:.0f} mm" for d, n in sorted(from_text.items())) + ".")

    if ambiguous:
        res.add(WARNING, "slabs",
                f"{ambiguous} sunk hatches use a pattern the legend shows for more than one "
                "depth, and neither layer nor scale tells them apart, so their depth is "
                "unknown and they are built at the level. Set it in config.json under "
                "'sunk_depth_override'.")
    for lay in sorted(not_in_legend):
        res.add(WARNING, "slabs",
                f"A sunk hatch on layer '{lay}' is not shown in the drawing's legend, so its "
                "depth is unknown and the slab under it is built at the level. The layer "
                "name is not used: on these drawings it often disagrees with the legend. "
                "Set the depth in config.json under 'sunk_depth_override' if it is known.",
                layer=lay)
    for lay in sorted(unknown_depth_layers):
        res.add(WARNING, "slabs",
                f"Sunk depth for layer '{lay}' was read from its name, because the drawing "
                "has no legend of sunk hatches. Check it against the drawing, and set the "
                "real depth in config.json under 'sunk_depth_override' if it differs.",
                layer=lay)

    # where a staircase is drawn: a bay with no slab mark holding it is the stair
    # well, left open (slab_bays.derive_panels). Its lines' middles are enough.
    stair_pts, stair_segs = [], []
    for e in buckets["stair"]:
        try:
            if e.dxftype() == "LINE":
                stair_pts.append(((e.dxf.start.x + e.dxf.end.x) / 2.0, (e.dxf.start.y + e.dxf.end.y) / 2.0))
                stair_segs.append(((e.dxf.start.x, e.dxf.start.y), (e.dxf.end.x, e.dxf.end.y)))
            elif e.dxftype() == "LWPOLYLINE":
                pts = [(float(p[0]), float(p[1])) for p in e.get_points("xy")]
                stair_pts += pts
                stair_segs += list(zip(pts, pts[1:] + ([pts[0]] if e.closed else [])))
        except Exception:
            continue
    res.stats["_stair_points"] = stair_pts
    # ...and its lines, to take the staircase out of a marked bay (slab_bays._cut_stairs)
    res.stats["_stair_segments"] = stair_segs

    for role in ("opening", "stair"):
        for e in buckets[role]:
            if e.dxftype() != "LWPOLYLINE":
                continue
            pts = G.dedupe_points(_pts_of_lwpolyline(e), tol["duplicate_tolerance_mm"])
            if len(pts) >= 3:
                res.openings.append(Opening(kind=role, outline=pts,
                                            source_layer=e.dxf.layer, handle=e.dxf.handle))

    # A cut-out drawn the usual way: a cross from corner to corner, no outline.
    lines = [((e.dxf.start.x, e.dxf.start.y), (e.dxf.end.x, e.dxf.end.y),
              e.dxf.layer, e.dxf.handle)
             for e in buckets["opening"] if e.dxftype() == "LINE"]
    for outline, layer, handle in slab_bays.openings_from_crosses(lines):
        res.openings.append(Opening(kind="opening", outline=outline,
                                    source_layer=layer, handle=handle))


_WRITTEN_DEPTH = [
    re.compile(r"^\(?\s*(\d{2,4})\s*MM\.?\s*SUNK\s*\)?$"),                 # 150MM SUNK
    re.compile(r"^SUNK\s*(?:BY\s*)?[-:]?\s*(\d{2,4})\s*(?:MM)?\.?$"),        # SUNK 150, SUNK BY 150MM
    re.compile(r"^\(\s*[-−]\s*(\d{2,4})\s*(?:MM)?\s*\)$"),             # (-150)
    re.compile(r"^(?:[A-Z]{2,4}\.?\s*[:=]?\s*)?[-−]\s*0?\.(\d{3})$"),   # -0.150, SFL -0.150 (m)
]


def _sunk_texts(ents) -> list:
    """[(x, y, depth mm)] for every text that states a sunk depth on its own."""
    out = []
    for e in ents:
        if e.dxftype() not in ("TEXT", "MTEXT"):
            continue
        try:
            t = e.dxf.text if e.dxftype() == "TEXT" else e.plain_text()
            p = e.dxf.insert
        except Exception:
            continue
        t = re.sub(r"%%[a-zA-Z]", "", str(t)).strip().upper()
        for rx in _WRITTEN_DEPTH:
            m = rx.match(t)
            if m:
                d = float(m.group(1))
                if 25.0 <= d <= 1500.0:
                    out.append((float(p[0]), float(p[1]), d))
                break
    return out


def _depth_written_inside(outline, texts) -> float | None:
    """The sunk depth written inside an area, when every such text there agrees."""
    if not texts or len(outline) < 3:
        return None
    from shapely.geometry import Point, Polygon
    try:
        poly = Polygon(outline).buffer(0)
    except Exception:
        return None
    got = {d for x, y, d in texts if poly.contains(Point(x, y))}
    return got.pop() if len(got) == 1 else None


def _sunk_from_lines(ents, res: Result, cfg, samples, texts=(), from_text=None) -> None:
    """Sunk areas whose hatch was exploded into lines, depth by the legend's look.

    Only where the legend itself is exploded (samples), and only on the layers
    it uses or a layer named SUNK. A shaded area on a SUNK layer whose look is
    not in the legend is kept with its depth unknown (unless the words written
    inside it give the depth), and reported.
    """
    if not samples:
        return
    s = sunklines.settings(cfg)
    sample_layers = {q[0] for q in samples}
    layers = {str(e.dxf.layer).upper() for e in ents if e.dxftype() == "LINE"}
    layers = {l for l in layers if l in sample_layers or "SUNK" in l}
    segs = sunklines.segments(ents, layers)
    by_layer = collections.defaultdict(list)
    for g in segs:
        by_layer[g[0]].append(g)
    found = collections.Counter()
    by_look, unknown = {}, collections.Counter()
    for lay, lsegs in sorted(by_layer.items()):
        for poly, members in sunklines.areas(lsegs, s):
            got = sunklines.look(members, s)
            depth, src = None, "not in legend"
            if got:
                same = {q[3] for q in samples if q[0] == lay and q[1] == got[0]}
                alike = {q[3] for q in samples if q[1] == got[0]}
                if len(same) == 1:
                    depth, src = same.pop(), "legend (exploded hatch)"
                elif not same and len(alike) == 1:
                    depth, src = alike.pop(), "legend (exploded hatch, by look)"
                    by_look[(lay, got[0], round(got[1]))] = depth
            forced, how = sunk_depth_from_layer(lay, cfg.get("sunk_depth_override", {}))
            if how == "override":
                depth, src = forced, "override"
            if depth is None:
                if "SUNK" not in lay:
                    continue          # lines on HATCH the legend does not explain
                outline = [(x, y) for x, y in poly.exterior.coords][:-1]
                written = _depth_written_inside(outline, texts)
                if written is not None:
                    depth, src = written, "text in the area"
                    if from_text is not None:
                        from_text[depth] += 1
            if depth is None:
                unknown[(lay, got[0] if got else "unrecognised")] += 1
            found[(lay, depth)] += 1
            res.sunk.append(SunkZone(
                depth_mm=depth, depth_source=src,
                outline=[(round(x, 1), round(y, 1))
                         for x, y in poly.simplify(10.0).exterior.coords][:-1],
                source_layer=lay, handle=""))
    if found:
        res.add(INFO, "slabs",
                "Sunk areas shaded with exploded hatch lines (not real hatches), read by "
                "their look against the legend: " + ", ".join(
                    f"{n} on {lay} at {d:.0f} mm" if d else f"{n} on {lay} of unknown depth"
                    for (lay, d), n in sorted(found.items(), key=str)) + ".")
    for (lay, kind, ang), d in sorted(by_look.items()):
        sam = [q for q in samples if q[1] == kind and q[3] == d][0]
        res.add(WARNING, "slabs",
                f"The {kind} shading on layer '{lay}' ({ang} deg) was taken as {d:.0f} mm "
                f"sunk because the legend's {d:.0f} MM SUNK sample is the only one drawn as "
                f"{kind}, though the sample is on '{sam[0]}' at {sam[2]:.0f} deg. The hatches "
                "in this drawing have been exploded into lines, so their pattern names are "
                "lost; extract from a copy with real hatches to be sure.", layer=lay)
    for (lay, kind), n in sorted(unknown.items()):
        res.add(WARNING, "slabs",
                f"{n} areas on layer '{lay}' are shaded ({kind}) in a way the legend does "
                "not show, so their depth is unknown and the slab there is built at the "
                "level. Set it in config.json under 'sunk_depth_override' if it is sunk.",
                layer=lay)


def _edge_path_points(path, step_mm: float = 50.0) -> list:
    """The points round a hatch boundary made of edges, curves followed.

    A line edge gives its ends. An arc, ellipse or spline edge is walked along
    its curve. It used to give its centre instead: on Wing B 2nd floor (MOFA's
    copy) the curved 150 hatch at the chajja, with arcs centred far inside the
    building, became a 246 m2 shape that sank half of two S1 bays.
    """
    pts = []
    for edge in getattr(path, "edges", []) or []:
        kind = type(edge).__name__
        try:
            if kind == "LineEdge":
                pts += [(edge.start[0], edge.start[1]), (edge.end[0], edge.end[1])]
            elif kind == "ArcEdge":
                c, r = edge.center, float(edge.radius)
                a0, a1 = float(edge.start_angle), float(edge.end_angle)
                if not edge.ccw:                       # stored mirrored for a clockwise arc
                    a0, a1 = 360.0 - a0, 360.0 - a1
                    sweep = -((a0 - a1) % 360.0 or 360.0)
                else:
                    sweep = (a1 - a0) % 360.0 or 360.0
                n = max(2, int(abs(math.radians(sweep)) * r / step_mm) + 1)
                for i in range(n + 1):
                    a = math.radians(a0 + sweep * i / n)
                    pts.append((c[0] + r * math.cos(a), c[1] + r * math.sin(a)))
            else:                                       # ellipse, spline: ezdxf flattens
                from ezdxf import path as ezpath
                pth = ezpath.from_hatch_edge_path(path)
                return [(v[0], v[1]) for v in pth.flattening(5.0)]
        except Exception:
            continue
    return pts


def _beam_rectangle(pts, bulges, closed, max_width: float = 1600.0):
    """(start, end, width) of a beam drawn as a closed rectangle, else None.

    Four straight sides at right angles, the short side beam-sized and at most
    half the long one. A closed square on a beam layer (Wing C 2nd floor, 3.4 x
    3.6 m) is an outline of something else and is read as before.
    """
    if not closed or len(pts) != 4 or any(abs(b) > 1e-6 for b in bulges[:4]):
        return None
    sides = [(pts[i], pts[(i + 1) % 4]) for i in range(4)]
    lens = [G.dist(p, q) for p, q in sides]
    for i in range(4):
        u = (sides[i][1][0] - sides[i][0][0], sides[i][1][1] - sides[i][0][1])
        v = (sides[(i + 1) % 4][1][0] - sides[(i + 1) % 4][0][0],
             sides[(i + 1) % 4][1][1] - sides[(i + 1) % 4][0][1])
        if abs(u[0] * v[0] + u[1] * v[1]) > 0.02 * lens[i] * lens[(i + 1) % 4] + 1e-9:
            return None
    if abs(lens[0] - lens[2]) > 2.0 or abs(lens[1] - lens[3]) > 2.0:
        return None
    k = 0 if lens[0] <= lens[1] else 1          # sides k and k+2 are the ends
    w, L = lens[k], lens[k + 1]
    if w > max_width or w > 0.5 * L:
        return None
    s = ((sides[k][0][0] + sides[k][1][0]) / 2.0, (sides[k][0][1] + sides[k][1][1]) / 2.0)
    e = ((sides[k + 2][0][0] + sides[k + 2][1][0]) / 2.0,
         (sides[k + 2][0][1] + sides[k + 2][1][1]) / 2.0)
    return s, e, round(w, 1)


def _carry_floating_columns(res: Result, reach: float = 150.0) -> None:
    """Run the beams under each floating column through it, not up to its faces.

    A floating column (FC) starts on the slab, so what is under its footprint is
    the beam it stands on. The drawing stops the beams at the FC's faces, as it
    does at any column, and under an ordinary column the gap is inside the
    column. Under an FC it is open: Wing B 1st floor, B53 and B54 end 1050 mm
    apart either side of FC2 on the west edge, and in Revit FC2 stood on nothing.

    A beam ending at an FC with another in line on the far side is taken to the
    FC's centre, so the two meet; a beam ending at one alone is taken to its far
    face. A beam already running past the centre is left as it is.
    """
    from shapely.geometry import Point, Polygon
    fcs = [c for c in res.columns
           if c.kind == "column_float" or (c.mark or "").upper().startswith("FC")]
    moved = []
    for c in fcs:
        if len(c.outline) < 3:
            continue
        poly = Polygon(c.outline)
        zone = poly.buffer(reach)
        ends = []
        for b in res.beams:
            if getattr(b, "mid", None):
                continue
            for which in ("start", "end"):
                if zone.contains(Point(getattr(b, which))):
                    ends.append((b, which))
        # already standing on a beam or girder that runs through it: nothing to do
        mine = {id(b) for b, _w in ends}
        if any(id(b) not in mine and not getattr(b, "mid", None)
               and G.point_to_segment((c.x, c.y), b.start, b.end) <= (b.width or 230) / 2
               for b in res.beams):
            continue
        for b, which in ends:
            p = getattr(b, which)
            q = b.end if which == "start" else b.start
            L = G.dist(q, p)
            if L < 1.0:
                continue
            u = ((p[0] - q[0]) / L, (p[1] - q[1]) / L)       # along the beam, toward the FC

            def along(pt):
                return (pt[0] - q[0]) * u[0] + (pt[1] - q[1]) * u[1]

            def parallel(o):
                v = (o.end[0] - o.start[0], o.end[1] - o.start[1])
                n = math.hypot(*v) or 1.0
                return abs(u[0] * v[1] - u[1] * v[0]) / n < 0.05
            partner = any(o is not b and parallel(o) for o, _w in ends)
            target = along((c.x, c.y)) if partner else max(along(v) for v in c.outline)
            if target <= L + 1.0:
                continue
            new = (q[0] + u[0] * target, q[1] + u[1] * target)
            setattr(b, which, (round(new[0], 1), round(new[1], 1)))
            b.notes.append(f"run under floating column {c.mark or '?'} "
                           f"({target - L:.0f} mm longer)")
            moved.append(f"{b.mark or '?'} under {c.mark or '?'} +{target - L:.0f}")
    if moved:
        res.add(INFO, "beams",
                f"{len(moved)} beam ends stopped at the face of a floating column, which "
                "starts on the slab and would have stood on nothing. They now run under "
                "it: " + ", ".join(moved) + ".")


def _extract_grid(buckets, res: Result) -> None:
    for e in buckets["grid"]:
        if e.dxftype() == "LINE":
            res.grids.append(GridLine(
                label="", start=(e.dxf.start.x, e.dxf.start.y),
                end=(e.dxf.end.x, e.dxf.end.y), handle=e.dxf.handle))
    if not res.grids:
        res.add(BLOCKER, "setting out",
                "There is no grid in the drawing. Without one the model cannot be set "
                "out or checked against the architectural file.")


# --------------------------------------------------------------------------
# tag matching
# --------------------------------------------------------------------------


def _drop_detached_islands(res: Result, cfg, tol) -> None:
    """Leave out structure that sits on its own, miles from the building.

    A floor plan is one connected body: beams frame into columns, columns carry
    beams, and nothing real is a hundred metres from everything else. But CAD
    files collect debris - a fragment dragged off the sheet, a paste that landed
    in the wrong place, the remains of an edit nobody finished. On the Wing B
    first floor, seven beam lines sit 2.4 KILOMETRES from the plan with nothing
    else around them.

    In Revit that debris is what you notice first: a handful of beams alone in
    space, forcing every view to zoom out until the building is a smudge.

    Elements are grouped by single linkage, and only a group that is both small
    and far is dropped. A genuinely detached wing is neither, so it stays.
    """
    s = cfg.get("islands") or {}
    if s.get("drop") is False:
        return
    link = float(s.get("link_mm", 10000.0))
    min_away = float(s.get("min_distance_mm", 50000.0))
    max_share = float(s.get("max_share", 0.15))

    items = []           # (owner_list, object, [points])
    for c in res.columns:
        items.append((res.columns, c, [(c.x, c.y)]))
    for b in res.beams:
        items.append((res.beams, b, [b.start, b.end]))
    for w in res.walls:
        if w.outline:
            items.append((res.walls, w, [G.centroid(w.outline)]))
    if len(items) < 8:
        return

    parent = list(range(len(items)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i, j):
        a, b = find(i), find(j)
        if a != b:
            parent[a] = b

    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            if find(i) == find(j):
                continue
            if any(G.dist(p, q) <= link for p in items[i][2] for q in items[j][2]):
                union(i, j)

    groups: dict[int, list[int]] = {}
    for i in range(len(items)):
        groups.setdefault(find(i), []).append(i)
    if len(groups) < 2:
        return

    main = max(groups.values(), key=len)
    main_pts = [p for i in main for p in items[i][2]]
    mx0 = min(p[0] for p in main_pts)
    mx1 = max(p[0] for p in main_pts)
    my0 = min(p[1] for p in main_pts)
    my1 = max(p[1] for p in main_pts)

    def away(pts):
        """How far outside the main body's extent this group sits."""
        d = 0.0
        for x, y in pts:
            dx = max(mx0 - x, 0.0, x - mx1)
            dy = max(my0 - y, 0.0, y - my1)
            d = max(d, math.hypot(dx, dy))
        return d

    dropped = 0
    for key, idx in groups.items():
        if idx is main or idx == main:
            continue
        pts = [p for i in idx for p in items[i][2]]
        d = away(pts)
        share = len(idx) / float(len(items))
        if d < min_away or share > max_share:
            res.add(WARNING, "geometry",
                    f"{len(idx)} elements sit {d / 1000.0:.0f} m away from the rest of the "
                    "structure, as a group of their own. They have been kept, because a "
                    "group that large or that close may be a real part of the building, "
                    "but check the drawing.",
                    x=pts[0][0], y=pts[0][1])
            continue
        for i in idx:
            owner, obj, _p = items[i]
            try:
                owner.remove(obj)
            except ValueError:
                pass
            dropped += 1
        res.add(WARNING, "geometry",
                f"{len(idx)} elements were left out: they sit {d / 1000.0:.0f} m from the "
                "building, on their own, with no columns or tags around them. That is "
                "debris in the CAD file - a fragment dragged off the sheet or a stray "
                "paste - not structure. In Revit it would be a handful of beams alone in "
                "space. Delete them from the drawing.",
                x=pts[0][0], y=pts[0][1])

    if dropped:
        res.add(INFO, "geometry",
                f"{dropped} detached elements in total were left out of the model.")


def _match_column_tags(col_tags, res: Result, cfg, tol) -> None:
    targets = [(c, (c.x, c.y)) for c in res.columns]
    targets += [(w, G.centroid(w.outline)) for w in res.walls]
    if not targets:
        return
    used: set[int] = set()

    for mark, p, h, lay in sorted(col_tags, key=lambda t: t[0]):
        best = None
        for idx, (obj, c) in enumerate(targets):
            d = G.dist(p, c)
            if best is None or d < best[0]:
                best = (d, idx, obj)
        d, idx, obj = best
        if d > tol["tag_max_distance_mm"]:
            res.add(BLOCKER, "tags",
                    f"Mark '{mark}' is {d:.0f} mm from the nearest column or wall, too far "
                    "to assign. Move the tag inside or within 300 mm of its element.",
                    mark=mark, x=p[0], y=p[1], handle=h)
            continue
        if idx in used:
            res.add(WARNING, "tags",
                    f"Mark '{mark}' matches an element that is already tagged. One of the "
                    "two tags is on the wrong element or is a duplicate.",
                    mark=mark, x=p[0], y=p[1], handle=h)
            continue
        used.add(idx)
        obj.mark = mark
        obj.tag_distance = round(d, 1)
        if d > tol["tag_warn_distance_mm"]:
            res.add(WARNING, "tags",
                    f"Mark '{mark}' sits {d:.0f} mm from its element. Anything beyond "
                    f"{tol['tag_warn_distance_mm']:.0f} mm is matched on a best guess.",
                    mark=mark, x=p[0], y=p[1], handle=h)

    untagged = [t for i, t in enumerate(targets) if i not in used]
    if untagged:
        res.add(BLOCKER, "tags",
                f"{len(untagged)} column or wall outlines carry no mark, so nothing "
                "connects them to a schedule row. Tag every element.")


def _marks_follow_beams(rots) -> bool:
    """True if the beam marks are written in two directions at right angles,
    each for a real share of them - i.e. each mark runs along its beam."""
    rs = [r % 180.0 for r in rots if r is not None]
    if len(rs) < 10:
        return False
    import collections
    groups = collections.Counter(round(r / 10.0) * 10 % 180 for r in rs)
    (a, na), *rest = groups.most_common()
    other = sum(n for g, n in groups.items()
                if 70 <= min(abs(g - a), 180 - abs(g - a)) <= 110)
    return na >= 0.2 * len(rs) and other >= 0.2 * len(rs)


def _split_for_second_mark(beam_tags, res: Result, tol, along, limit) -> None:
    """Split a line that two marks both belong to, at a joint between them.

    On Wing B 2nd floor one vertical line runs three bays; its middle span is
    marked B89 and its lower span BP, both written along it. Kept whole it can
    carry one mark, and B89 was reported "not drawn on this plan". A line is only
    cut where another beam or a column meets it between the two marks, so a
    single span with a repeated mark is never split.
    """
    import dataclasses
    for _round in range(6):
        near: dict[int, list] = {}
        for ti, (mark, p, h, lay) in enumerate(beam_tags):
            best = None
            for bi, b in enumerate(res.beams):
                if not along(ti, b):
                    continue
                d = max(0.0, G.point_to_segment(p, b.start, b.end) - (b.width or 0.0) / 2.0)
                if d <= limit and (best is None or d < best[0]):
                    best = (d, bi)
            if best:
                near.setdefault(best[1], []).append(ti)
        cut = None
        for bi, tis in near.items():
            b = res.beams[bi]
            if b.mid or len({beam_tags[t][0] for t in tis}) < 2:
                continue
            L = b.length
            if L < 1:
                continue
            ux, uy = (b.end[0] - b.start[0]) / L, (b.end[1] - b.start[1]) / L

            def t_of(pt):
                return (pt[0] - b.start[0]) * ux + (pt[1] - b.start[1]) * uy

            def off(pt):
                return abs((pt[0] - b.start[0]) * -uy + (pt[1] - b.start[1]) * ux)

            joints = []
            for o in res.beams:
                if o is b:
                    continue
                for e in (o.start, o.end):
                    if off(e) <= (b.width or 230) / 2 + (o.width or 230) / 2 + 150 \
                            and 300 < t_of(e) < L - 300:
                        joints.append(t_of(e))
            for c in res.columns:
                if off((c.x, c.y)) <= (b.width or 230) / 2 + c.depth / 2 and 300 < t_of((c.x, c.y)) < L - 300:
                    joints.append(t_of((c.x, c.y)))
            ts = sorted((t_of(beam_tags[t][1]), beam_tags[t][0]) for t in tis)
            for (t1, m1), (t2, m2) in zip(ts, ts[1:]):
                if m1 == m2:
                    continue
                between = [j for j in joints if t1 < j < t2]
                if between:
                    mid = (t1 + t2) / 2.0
                    cut = (bi, min(between, key=lambda j: abs(j - mid)))
                    break
            if cut:
                break
        if not cut:
            return
        bi, t = cut
        b = res.beams[bi]
        L = b.length
        pm = (b.start[0] + (b.end[0] - b.start[0]) * t / L, b.start[1] + (b.end[1] - b.start[1]) * t / L)
        second = dataclasses.replace(b, start=pm, notes=list(b.notes) + ["split where a second mark begins"])
        b.end = pm
        b.notes = list(b.notes) + ["split where a second mark begins"]
        res.beams.insert(bi + 1, second)
        res.add(INFO, "geometry",
                "A beam line carried two different marks, one per span; it was split at "
                "the joint between them so each span keeps its own mark.",
                x=pm[0], y=pm[1])


def _match_beam_tags(beam_tags, res: Result, tol, cfg=None) -> None:
    """Give every mark the nearest beam that has not already been claimed.

    Beams sit close together, so the nearest line to a mark is often one that a
    previous mark already took. Falling back to the next nearest free beam, rather
    than giving up, is what stops marks being dropped on a crowded plan.
    """
    if not res.beams:
        return
    limit = tol.get("tag_assign_limit_mm", 700.0)
    is_girder_mark = _girder_mark_test(cfg)

    # A mark is written along its beam - on the Reis Magos plans 'BEAM NO (H)'
    # at 2 degrees for the horizontal beams and 'BEAM NO (V)' at 92 for the
    # vertical ones. Ignoring that gave B7, written horizontally, to a vertical
    # beam on Wing B 2nd floor, and left B82 - the vertical beam's own mark -
    # with nowhere to go. Only used where the drawing's marks really do run both
    # ways; a drawing that writes every mark level keeps the old matching.
    rot = res.stats.get("_tag_rot", {}) or {}
    follow = _marks_follow_beams([rot.get(h) for _m, _p, h, _l in beam_tags])

    def along(ti, b):
        r = rot.get(beam_tags[ti][2])
        if not follow or r is None or b.mid:
            return True
        d = abs((G.angle_deg(b.start, b.end) % 180.0) - (r % 180.0)) % 180.0
        return min(d, 180.0 - d) <= 20.0

    _split_for_second_mark(beam_tags, res, tol, along, limit)

    # Every mark-to-beam pair within reach, closest first. Taking them in that
    # order means the confident pairs claim each other before the doubtful ones
    # are considered, which a per-mark search cannot do: there, an early mark can
    # take a beam that a later, much closer mark needed, and the error cascades.
    pairs = []
    #
    # Distance is to the beam's face, not its centreline. A 1500 mm girder's face
    # is 750 mm off its centreline, so a mark written against it (GB2 to GB7 on
    # Wing B) measured beyond the limit and the girder fell back to a default depth.
    # A girder mark prefers a girder and a beam mark a beam, so a wide girder does
    # not draw in the marks of the small beams that frame into its face.
    for ti, (mark, p, h, lay) in enumerate(beam_tags):
        gm = is_girder_mark(mark)
        for bi, b in enumerate(res.beams):
            d = max(0.0, G.point_to_segment(p, b.start, b.end) - (b.width or 0.0) / 2.0)
            if d <= limit:
                rank = d if gm == (b.kind == "girder") else d + limit
                # a mark written across a beam is only a last resort: every pair
                # that runs the right way is settled first. Short brackets (BKT)
                # are not always written along their stub, and must still place.
                if not along(ti, b):
                    rank += 4 * limit
                pairs.append((rank, d, ti, bi))
    pairs.sort()

    taken_tag: set[int] = set()
    taken_beam: set[int] = set()
    for _rank, d, ti, bi in pairs:
        if ti in taken_tag or bi in taken_beam:
            continue
        taken_tag.add(ti)
        taken_beam.add(bi)
        mark, p, h, lay = beam_tags[ti]
        b = res.beams[bi]
        b.mark = mark
        b.tag_distance = round(d, 1)
        if d > tol["tag_warn_distance_mm"]:
            res.add(WARNING, "tags",
                    f"Beam mark '{mark}' sits {d:.0f} mm from the line it was given and was "
                    "matched on a best guess.", mark=mark, x=p[0], y=p[1], handle=h)

    for ti, (mark, p, h, lay) in enumerate(beam_tags):
        if ti in taken_tag:
            continue
        nearest = min((max(0.0, G.point_to_segment(p, b.start, b.end) - (b.width or 0.0) / 2.0)
                       for b in res.beams),
                      default=float("inf"))
        res.add(BLOCKER, "tags",
                f"Beam mark '{mark}' was not placed. The nearest beam face is {nearest:.0f} mm "
                f"away and every line within {limit:.0f} mm already carries a mark, so the "
                "beam it names is probably not drawn on this plan. Confirm whether it belongs "
                "to another level.", mark=mark, x=p[0], y=p[1], handle=h)

    untagged = [b for i, b in enumerate(res.beams) if i not in taken_beam]
    if untagged:
        res.add(WARNING, "tags",
                f"{len(untagged)} beam or girder segments carry no mark. Some will be spans "
                "of an already-tagged beam; the rest need tagging.")


def _reclassify_walls(res: Result, cfg) -> None:
    """A rectangle on a column layer whose mark reads LW is a wall, not a column."""
    prefixes = tuple(p.upper() for p in cfg.get("wall_mark_prefixes", []))
    if not prefixes:
        return
    moved = []
    for c in list(res.columns):
        if c.mark.upper().startswith(prefixes):
            res.columns.remove(c)
            res.walls.append(Wall(
                mark=c.mark, thickness=c.width,
                start=G.long_axis_of_rect(c.outline)[0] if len(c.outline) == 4 else None,
                end=G.long_axis_of_rect(c.outline)[1] if len(c.outline) == 4 else None,
                outline=c.outline, source_layer=c.source_layer, handle=c.handle,
                tag_distance=c.tag_distance,
                notes=["moved off the column layer: mark reads as a wall"]))
            moved.append(c.mark)
    if moved:
        res.add(WARNING, "layers",
                f"{len(moved)} shapes on a column layer are marked as walls "
                f"({', '.join(sorted(moved))}). They have been re-classified. Put them on "
                "a wall layer in the drawing.")


def _check_duplicates(res: Result, tol) -> None:
    seen: dict[tuple, str] = {}
    for c in res.columns:
        key = (round(c.x, 1), round(c.y, 1), round(c.width), round(c.depth))
        if key in seen:
            res.add(WARNING, "geometry",
                    f"Two identical column outlines sit on top of each other at "
                    f"({c.x:.0f}, {c.y:.0f}). One would become a duplicate Revit column.",
                    mark=c.mark or seen[key], x=c.x, y=c.y, handle=c.handle)
        else:
            seen[key] = c.mark


def _check_beam_alignment(res: Result, tol) -> None:
    """Flag beams that do not run along any of the plan's main directions.

    A building is drawn on a small number of bearings. A short, untagged line at
    45 degrees to all of them is usually hatching, a chamfer or a detail that has
    landed on the beam layer, not a beam. It is reported rather than deleted,
    because a genuine skewed beam looks exactly the same to a program.
    """
    if len(res.beams) < 8:
        return
    import collections

    # Weight each bearing by total length, so a few stray lines cannot define one.
    by_angle: dict[int, float] = collections.defaultdict(float)
    for b in res.beams:
        by_angle[round(G.angle_deg(b.start, b.end) % 180.0)] += b.length
    total = sum(by_angle.values()) or 1.0

    mains: list[int] = []
    for ang, length in sorted(by_angle.items(), key=lambda kv: -kv[1]):
        if length / total < 0.03:
            break
        if all(_angle_gap(ang, m) > 10 for m in mains):
            mains.append(ang)
    if not mains:
        return

    tolerance = tol.get("beam_alignment_tolerance_deg", 5.0)
    odd = [b for b in res.beams
           if all(_angle_gap(round(G.angle_deg(b.start, b.end) % 180.0), m) > tolerance
                  for m in mains)]
    if not odd:
        return

    untagged = [b for b in odd if not b.mark]
    res.add(WARNING, "geometry",
            f"The plan runs at {', '.join(f'{m} deg' for m in sorted(mains))}. "
            f"{len(odd)} beam lines run at none of those bearings, {len(untagged)} of them "
            f"untagged and totalling {sum(b.length for b in odd) / 1000:.1f} m. They are "
            "usually hatching, chamfers or detail linework left on the beam layer. Check "
            "them before modelling; they have been kept, not deleted.",
            x=odd[0].start[0], y=odd[0].start[1])


def _angle_gap(a: float, b: float) -> float:
    d = abs(a - b) % 180.0
    return min(d, 180.0 - d)


def _check_alignment(res: Result, tol) -> None:
    """Most plans are drawn at one angle. Shapes off that angle are usually either
    a different wing or a drafting slip, and both are worth knowing about."""
    if len(res.columns) < 5:
        return
    import collections
    angles = collections.Counter(round(c.rotation % 90.0) for c in res.columns)
    dominant, n_dom = angles.most_common(1)[0]
    tolerance = tol.get("alignment_tolerance_deg", 1.0)

    odd = [c for c in res.columns
           if min(abs((c.rotation % 90.0) - dominant),
                  90 - abs((c.rotation % 90.0) - dominant)) > tolerance]
    if not odd:
        return

    untagged = [c for c in odd if not c.mark]
    sizes = collections.Counter(f"{c.width:.0f} x {c.depth:.0f}" for c in odd)
    res.add(WARNING, "geometry",
            f"The plan is drawn at {dominant} degrees ({n_dom} of {len(res.columns)} "
            f"rectangles), but {len(odd)} are not, {len(untagged)} of them untagged. "
            f"Sizes: {', '.join(f'{s} x{n}' for s, n in sizes.most_common(4))}. "
            "Confirm whether these are structure or drafting left in by mistake.",
            x=odd[0].x, y=odd[0].y)


def _base_point(res: Result, cfg) -> None:
    bp = cfg.get("base_point", "auto")
    if isinstance(bp, (list, tuple)) and len(bp) == 2:
        res.base_point = (float(bp[0]), float(bp[1]))
        return
    pts = [(c.x, c.y) for c in res.columns] or [b.start for b in res.beams]
    if pts:
        res.base_point = (min(p[0] for p in pts), min(p[1] for p in pts))
        res.add(INFO, "setting out",
                f"No shared base point is set, so local coordinates are measured from the "
                f"south-west corner of the columns at ({res.base_point[0]:.0f}, "
                f"{res.base_point[1]:.0f}). Set 'base_point' in config.json once the "
                "shared point is agreed with the architect.")
