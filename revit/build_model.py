r"""Build the structural model in Revit from model.json.

This runs INSIDE Revit. Nothing outside Revit can create Revit elements, so it is
written to work in either host:

  * Rhino.Inside.Revit - a Grasshopper Python 3 component, or the Rhino script
               editor with Rhino.Inside.Revit loaded. Inputs are named
               `model_json` and `level_name`.
  * Dynamo   - a Python Script node set to CPython3. IN[0] is the path to
               model.json; IN[1] is optional, the level name.
  * pyRevit  - run it straight from the pyRevit console.

With no inputs wired it reads ..\extracted\model.json next to this script.

It places columns, beams, girders and walls, makes a type for every distinct size,
writes the Mark on each element, and reports what it could not do rather than
guessing. It never deletes anything.

Running it twice is safe. Before building it takes a snapshot of what is already
in the model and skips anything that matches, by Mark or by position, so a second
run fills in what is missing instead of duplicating the lot. Pass mode="add" if you
actually want a second copy.

Everything in model.json is in millimetres. Revit works in feet internally, so all
lengths go through MM().
"""

import json
import os
import sys
import traceback

import clr

clr.AddReference("RevitAPI")
from Autodesk.Revit.DB import (Arc, BuiltInCategory, BuiltInParameter, ElementId,
                               ElementTransformUtils, FilteredElementCollector,
                               Floor, Line, Structure, Transaction, Wall, XYZ,
                               CurveLoop)

try:                       # Revit 2021 and later
    from Autodesk.Revit.DB import UnitTypeId, UnitUtils
    def MM(v):
        return UnitUtils.ConvertToInternalUnits(float(v), UnitTypeId.Millimeters)
except ImportError:        # older API
    def MM(v):
        return float(v) / 304.8

# ---------------------------------------------------------------- the host
# Three ways in. Rhino.Inside.Revit and pyRevit both hand over the document and
# let us run our own transaction; Dynamo wants its own transaction manager.
HOST = None
doc = None

# pyRevit injects a global called __revit__; read it out of globals() so no
# editor flags it as an undefined name before the host has created it.
_pyrevit = globals().get("__revit__")
if _pyrevit is not None:
    try:
        doc = _pyrevit.ActiveUIDocument.Document
        HOST = "pyRevit"
    except Exception:
        doc = None

def _usable(d):
    """A document handle that Revit will actually accept.

    Dynamo's DocumentManager hands back a stale wrapper when Dynamo is installed
    but not the thing running the script, and that only fails later, deep inside
    a transaction. Check it here instead.
    """
    try:
        return d is not None and d.IsValidObject and d.Title is not None
    except Exception:
        return False


RIR_TYPE = None

if doc is None:
    # Rhino.Inside.Revit is already loaded in this process when it is the host, so
    # find it among the loaded assemblies. clr.AddReference by name is unreliable
    # here: the assembly is not on the probing path and the name has changed
    # between Rhino versions.
    try:
        import System
        for _asm in System.AppDomain.CurrentDomain.GetAssemblies():
            try:
                if _asm.GetName().Name in ("RhinoInside.Revit", "RhinoInside.Revit.AddIn"):
                    _t = _asm.GetType("RhinoInside.Revit.Revit")
                    if _t is not None:
                        _p = _t.GetProperty("ActiveDBDocument")
                        _d = _p.GetValue(None) if _p is not None else None
                        if _usable(_d):
                            doc, RIR_TYPE, HOST = _d, _t, "Rhino.Inside.Revit"
                            break
            except Exception:
                continue
    except Exception:
        pass

if doc is None:
    try:                                                           # Dynamo
        clr.AddReference("RevitServices")
        from RevitServices.Persistence import DocumentManager
        from RevitServices.Transactions import TransactionManager
        _d = DocumentManager.Instance.CurrentDBDocument
        if _usable(_d):
            doc, HOST = _d, "Dynamo"
    except Exception:
        pass

if doc is None:
    raise RuntimeError(
        "No usable Revit document found.\n"
        "Rhino.Inside.Revit: press Start on the Revit ribbon first, then run this "
        "from the Rhino script editor or a Grasshopper Python component.\n"
        "Dynamo: run it from a Python Script node.\n"
        "Either way, a Revit project must be open.")


def _in(i, name, default=None):
    """An input, however this host supplies them.

    Grasshopper and Rhino give named variables; Dynamo injects a list called IN.
    Both are read out of globals() rather than by name, so no editor reports an
    undefined name for a variable its own host is going to create.
    """
    v = globals().get(name)
    if v not in (None, ""):
        return v
    dynamo_in = globals().get("IN")
    if dynamo_in is not None:
        try:
            v = dynamo_in[i]
            if v not in (None, ""):
                return v
        except Exception:
            pass
    return default


# Where model.json comes from, in order:
#   1. a `model_json` input, or Dynamo's IN[0]
#   2. a file picker, if ASK_FOR_FILE is on and this host can show one
#   3. DEFAULT_MODEL_JSON below
#
# Extractions go to a different folder each run, so the picker is on by default:
# a fixed path is how you end up quietly building last week's drawing. Turn it off
# if you always extract to the same place.
ASK_FOR_FILE = True
DEFAULT_MODEL_JSON = r"C:\Users\TWO\Desktop\Sushmitha\Structure CAD to Revit\extracted\model.json"

# ---------------------------------------------------------------------------
#  REPLACE_PREVIOUS - the one you will want after fixing anything.
#
#  False  adds only what is missing. Anything this script placed before is left
#         exactly as it was, wrong rotation and all, and the log will say
#         ALREADY n columns (left alone).
#  True   removes everything the previous run placed, then builds again.
#         One undo puts it all back.
#
#  Re-extracting the drawing changes what the NEXT build places. It can do
#  nothing about elements already in the model. If a fix looks like it did not
#  work, this switch is almost always why.
# ---------------------------------------------------------------------------
REPLACE_PREVIOUS = True

#  CLEAR_ALL_STRUCTURE - the bigger hammer, for starting over.
#
#  True deletes EVERY structural column, framing element, wall and floor in the
#  document, whether this script made it or not, and then builds. Use it when the
#  record in placed.json no longer covers what is in the model - or when you just
#  want a clean floor. It is one transaction, so a single undo puts it all back.
#
#  Set it back to False afterwards. It does not care what you drew by hand, and
#  it does not care which floor: with several floors in one model it takes out
#  ALL of them. To redo one floor, use REPLACE_PREVIOUS - each floor's folder
#  keeps its own placed.json, so only that floor's elements are removed.
CLEAR_ALL_STRUCTURE = False

#  LEVEL_NAME - the level this drawing's beams sit on, e.g. "Level 2" or "Terrace".
#
#  Normally left as "": the level is chosen in the extractor's "Revit level" box
#  and saved in model.json. Failing that it is read from the drawing's name:
#  "1ST FLOOR PLAN" goes on the level named 1, a "TERRACE" or "ROOF" plan on the
#  level named Terrace or Roof. Set it here only to override both.
#  Its columns always run from the level below up to this one.
LEVEL_NAME = ""

#  ALIGN_TO_FLOOR_BELOW - each floor is its own drawing, and the drawings do not
#  share an origin. When most of this floor's columns miss the columns below by
#  one and the same amount, the whole floor is moved by it so they stack.
ALIGN_TO_FLOOR_BELOW = True

#  BEAM_SIZE_FILTERS - a view filter per beam size ("Beam 230x450"), each in
#  its own colour, added to every 3D, plan, section and elevation view (or to
#  the view template that controls it). The same size keeps the same colour on
#  every floor and every re-run; a new size takes the next unused colour.
BEAM_SIZE_FILTERS = True

#  SUNK_SLAB_FILTERS - the same for sunk slabs: a view filter per sunk depth
#  ("Sunk slab 150 mm", on the slab's Height Offset From Level), each in its own
#  colour. Flat slabs and rafts keep their own look. Footings are coloured per
#  size the same way by build_extras.py (FOUNDATION_SIZE_FILTERS there).
SUNK_SLAB_FILTERS = True

#  COLUMN_SIZE_FILTERS - and for columns: a view filter per column size
#  ("Column 300x600", whichever way the column faces; pedestals of that size too).
COLUMN_SIZE_FILTERS = True

#  FC_CARRY_ON - False (the user's rule, 2026-10-09): every floating column (FC) on a
#  floor's plan stands on that floor's girder and rises to the next level, on every
#  plan - the 3rd-floor FCs stand on Level 3, they are not the 1st-floor ones again.
#  True brings back the earlier reading: an FC with a floating column already under
#  it is that column carrying on, built from the level below.
FC_CARRY_ON = False


def _pick_json():
    """Ask for the file. Returns None where no dialog is possible (Dynamo, GH)."""
    try:
        import clr as _clr
        _clr.AddReference("System.Windows.Forms")
        from System.Windows.Forms import DialogResult, OpenFileDialog
        dlg = OpenFileDialog()
        dlg.Title = "Choose the model.json from the extraction you want to build"
        dlg.Filter = "Extracted model (model.json)|model.json|JSON files (*.json)|*.json"
        start = DEFAULT_MODEL_JSON
        if os.path.isfile(start):
            dlg.InitialDirectory = os.path.dirname(start)
            dlg.FileName = os.path.basename(start)
        if dlg.ShowDialog() == DialogResult.OK:
            return dlg.FileName
        return ""                       # cancelled on purpose
    except Exception:
        return None                     # no dialog available in this host


def _default_json():
    here = globals().get("__file__")
    if here:
        beside = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(here))),
                              "extracted", "model.json")
        if os.path.exists(beside):
            return beside
    return DEFAULT_MODEL_JSON


MODEL_JSON = _in(0, "model_json")
CANCELLED = False
if not MODEL_JSON and ASK_FOR_FILE:
    picked = _pick_json()
    if picked:
        MODEL_JSON = picked
    elif picked == "":
        CANCELLED = True
if not MODEL_JSON:
    MODEL_JSON = _default_json()
LEVEL_NAME = _in(1, "level_name") or LEVEL_NAME
# How a run treats what is already in the model:
#
#   "update"   (default) adds only what is missing, so running twice is safe.
#   "replace"  takes out everything the PREVIOUS run of this script placed, then
#              builds. Use this after fixing something in the extractor: a
#              re-extraction changes what the next build places and can do
#              nothing about elements already in the model. One undo reverts it.
#   "add"      places everything again, duplicates and all.
#
# REPLACE_PREVIOUS is set at the top of this file.
MODE = str(_in(2, "mode", "replace" if REPLACE_PREVIOUS else "update")).strip().lower()

log = []
def say(s):
    log.append(str(s))


def load_sizes(level_names=()):
    """Optional sizes.csv beside model.json, written from the schedule workbook by
    make_sizes_csv.py. The workbook wins over the drawing for what it fills in.

    Returns (sizes, extra):
      sizes  mark -> (width, depth), as sized() wants it
      extra  mark -> {sheet: {"depth_mm", "top_mm", "sunk_mm"}}, for a beam's
             top-of-beam offset and a slab's thickness and sunk depth

    A row whose level is one of `level_names` beats a row with no level; a row for
    some other level is ignored, so one workbook can cover every floor. An older
    sizes.csv with no level column applies everywhere, as it always did.
    """
    import csv
    path = os.path.join(os.path.dirname(MODEL_JSON), "sizes.csv")
    sizes, extra = {}, {}
    if not os.path.exists(path):
        return sizes, extra
    wanted = set(norm(n) for n in level_names if n)
    try:
        with open(path, "r") as f:
            rows = list(csv.DictReader(f))
        # rows for no level first, so the ones for this level overwrite them
        rows.sort(key=lambda r: 1 if (r.get("level") or "").strip() else 0)
        for row in rows:
            mark = (row.get("mark") or "").strip()
            lv = (row.get("level") or "").strip()
            if not mark or (lv and norm(lv) not in wanted):
                continue
            def num(k):
                try:
                    return float(row.get(k) or "")
                except ValueError:
                    return None
            key = norm(mark)
            w, d = num("width_mm"), num("depth_mm")
            if w is not None or d is not None:
                sizes[key] = (w, d)
            extra.setdefault(key, {})[(row.get("from") or "").strip()] = {
                "depth_mm": d, "top_mm": num("top_mm"), "sunk_mm": num("sunk_mm")}
    except Exception as exc:
        say("sizes.csv could not be read: %s" % exc)
    return sizes, extra


def from_book(extra, mark, sheet, field):
    """One value the workbook gives this mark on this sheet, or None."""
    return (extra.get(norm(mark)) or {}).get(sheet, {}).get(field)


def norm(mark):
    """'B-107' and 'B107' are the same mark; so are 'c 3' and 'C3'."""
    return "".join(ch for ch in str(mark or "").upper() if ch.isalnum())


DEFAULT_BEAM_DEPTH = 450.0      # only used where no schedule gives one
DEFAULT_SLAB_MM = 150.0         # a slab mark the slab schedule does not cover

# The plan is a FLOOR framing plan: its beams sit with their tops at the chosen
# level and hang below it. The columns drawn on it hold that floor up, so they
# must rise from the level below and stop AT this level, where the beams frame
# into them. Placing a column "on" the level makes it stand up from the slab
# instead - above the beams, never touching them - which is what every build so
# far did. Floating columns (FC marks) are the exception: they start on a girder
# at this level and carry the floor above.
#
# Where the model has a level below (or above) it is used. Where it does not,
# the column is given this height instead.
STOREY_HEIGHT_MM = 3000.0


def sized(mark, sizes, w_default, d_default, prefixes=()):
    """Schedule size for this mark where there is one, else the CAD/default.

    The workbook writes marks in full ('C-1', 'B-107'); the CAD tags them the way
    the drafter did ('1', 'B107'). Both are reduced to letters and digits, and for
    columns the missing prefix is tried too, or nothing would ever match.
    """
    key = norm(mark)
    if not key:
        return w_default, d_default
    for k in (key,) + tuple(p + key for p in prefixes):
        if k in sizes:
            w, d = sizes[k]
            return (w or w_default), (d or d_default)
    return w_default, d_default


# ---------------------------------------------------------------- helpers
def collect(bic):
    return list(FilteredElementCollector(doc).OfCategory(bic)
                .WhereElementIsElementType().ToElements())


def levels():
    return sorted(FilteredElementCollector(doc).OfCategory(BuiltInCategory.OST_Levels)
                  .WhereElementIsNotElementType().ToElements(),
                  key=lambda l: l.Elevation)


def neighbour_level(level, direction):
    """The next level below (direction -1) or above (+1), or None."""
    tol = MM(100.0)
    if direction < 0:
        cands = [l for l in levels() if l.Elevation < level.Elevation - tol]
        return cands[-1] if cands else None
    cands = [l for l in levels() if l.Elevation > level.Elevation + tol]
    return cands[0] if cands else None


def check_alignment(data, level, ox, oy):
    """Say whether this floor's columns stand on the columns of the floor below.

    Each floor is its own drawing, and each extraction measures from its own base
    point. If two floors' drawings do not share CAD coordinates, or 'base_point' in
    config.json is left on "auto" and the floors' column sets differ, the whole
    floor lands shifted. That is invisible in a plan view of one level, so it is
    measured here: most columns continue straight up, and the typical offset
    between each new column and the nearest one below is the shift.
    """
    import math as _m
    below = neighbour_level(level, -1)
    if below is None:
        return
    below2 = neighbour_level(below, -1)
    lo = below2.Elevation if below2 is not None else below.Elevation - MM(STOREY_HEIGHT_MM)
    old = []
    for el in (FilteredElementCollector(doc).OfCategory(BuiltInCategory.OST_StructuralColumns)
               .WhereElementIsNotElementType()):
        try:
            bb = el.get_BoundingBox(None)
            loc = el.Location.Point
        except Exception:
            continue
        if bb is None or loc is None:
            continue
        if lo < (bb.Min.Z + bb.Max.Z) / 2.0 <= below.Elevation:
            old.append((loc.X, loc.Y))
    new = [(MM(c["x"] - ox), MM(c["y"] - oy)) for c in data.get("columns", [])
           if not is_floating(c)]
    if not old or not new:
        return
    near, offs = 0, []
    for x, y in new:
        dx, dy = min(((ox_ - x, oy_ - y) for ox_, oy_ in old), key=lambda d: d[0] ** 2 + d[1] ** 2)
        offs.append((dx, dy))
        if _m.hypot(dx, dy) <= MM(SAME_PLACE):
            near += 1
    say("Alignment: %d of %d columns on this floor stand on a column of '%s'."
        % (near, len(new), below.Name))
    if near * 2 >= len(new):
        return
    # Mostly missed: see whether they all missed by the same amount.
    mx = sorted(o[0] for o in offs)[len(offs) // 2]
    my = sorted(o[1] for o in offs)[len(offs) // 2]
    same = sum(1 for dx, dy in offs if _m.hypot(dx - mx, dy - my) <= MM(SAME_PLACE))
    if same * 2 >= len(new) and same >= 3:
        # One shift for most columns is the two drawings' different origins
        # (Wing B 1st floor at x 2,900,000, 2nd floor at x 50,000), not a design
        # change: 29 of 32 marks moved by exactly (113, -274). Moved back here.
        dx_mm, dy_mm = mx * 304.8, my * 304.8
        say("SHIFTED  This floor's drawing has a different origin from the floor below: %d of %d"
            % (same, len(new)))
        say("         columns were %.0f mm, %.0f mm (x, y) off the columns of '%s'. The whole"
            % (-dx_mm, -dy_mm, below.Name))
        say("         floor (columns, beams, slabs, folds, walls) was moved by that to sit on it.")
        say("         Set ALIGN_TO_FLOOR_BELOW = False at the top of this file to switch this off.")
        return dx_mm, dy_mm
    elif same * 2 >= len(new):
        say("    Too few columns to tell a shift from a design change; check the floors")
        say("    line up in a 3D view.")
    else:
        say("    Few columns continue from the floor below. That can be right (a podium,")
        say("    a transfer floor), but check the two floors line up in a 3D view.")


def columns_between(band):
    """(x, y) of the structural columns whose middle is inside band (z_lo, z_hi]."""
    out = []
    for el in (FilteredElementCollector(doc).OfCategory(BuiltInCategory.OST_StructuralColumns)
               .WhereElementIsNotElementType()):
        try:
            bb = el.get_BoundingBox(None)
            loc = el.Location.Point
        except Exception:
            continue
        if bb is not None and loc is not None and band[0] < (bb.Min.Z + bb.Max.Z) / 2.0 <= band[1]:
            out.append((loc.X, loc.Y))
    return out


def is_floating(c):
    return c.get("kind") == "column_float" or norm(c.get("mark")).startswith("FC")


def _setp(el, bip, value):
    p = el.get_Parameter(bip)
    if p is not None and not p.IsReadOnly:
        p.Set(value)


def set_floor_offset(fl, offset_ft):
    """Drop a floor below its level, and check that it took.

    Setting it once and trusting it left every sunk slab flush on Wing B 2nd
    floor with nothing in the log. The parameter is found by its id, then by
    its name; if Revit still has it locked straight after creation, the model
    is regenerated and it is tried again. The value is read back.
    """
    def param():
        p = fl.get_Parameter(BuiltInParameter.FLOOR_HEIGHTABOVELEVEL_PARAM)
        if p is None or p.IsReadOnly:
            p = fl.LookupParameter("Height Offset From Level") or p
        return p

    for attempt in (0, 1):
        try:
            p = param()
            if p is not None and not p.IsReadOnly:
                p.Set(float(offset_ft))
                if abs(p.AsDouble() - offset_ft) < 1e-6:
                    return True
        except Exception:
            pass
        try:
            doc.Regenerate()
        except Exception:
            pass
    return False


def set_comment(el, text):
    p = el.get_Parameter(BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
    if p is not None and not p.IsReadOnly:
        try:
            p.Set(str(text))
        except Exception:
            pass


SLAB_SCHEDULE_NAME = "CAD Slabs - mark, thickness, sunk"


def make_slab_schedule():
    """A Revit schedule of every floor: Mark, Type (its thickness), Level,
    Height Offset From Level (the sunk depth), Area, Volume and Comments.
    Made once; later builds add their slabs to the same schedule."""
    try:
        from Autodesk.Revit.DB import ViewSchedule, ScheduleSortGroupField
        for v in FilteredElementCollector(doc).OfClass(ViewSchedule):
            if v.Name == SLAB_SCHEDULE_NAME:
                return
        vs = ViewSchedule.CreateSchedule(doc, ElementId(BuiltInCategory.OST_Floors))
        vs.Name = SLAB_SCHEDULE_NAME
        d = vs.Definition
        want = [BuiltInParameter.ALL_MODEL_MARK, BuiltInParameter.ELEM_TYPE_PARAM,
                BuiltInParameter.LEVEL_PARAM, BuiltInParameter.FLOOR_HEIGHTABOVELEVEL_PARAM,
                BuiltInParameter.HOST_AREA_COMPUTED, BuiltInParameter.HOST_VOLUME_COMPUTED,
                BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS]
        schedulable = list(d.GetSchedulableFields())
        added = []
        for bip in want:
            wid = ElementId(bip)
            sf = next((f for f in schedulable if f.ParameterId.Equals(wid)), None)
            if sf is not None:
                added.append((bip, d.AddField(sf)))
        for bip, fld in added:
            if bip in (BuiltInParameter.LEVEL_PARAM, BuiltInParameter.ALL_MODEL_MARK):
                d.AddSortGroupField(ScheduleSortGroupField(fld.FieldId))
        say("Made the Revit schedule '%s' (%d columns)." % (SLAB_SCHEDULE_NAME, len(added)))
    except Exception as exc:
        say("Could not make the slab schedule: %s" % exc)


def set_column_span(inst, level, floating):
    """Make the column meet the beams: up TO this level, or up FROM it if floating.

    The order of the Set calls matters. Revit refuses, at every step, a column
    whose top is not above its base, so the offset that opens up the height is
    always set before the level that would otherwise close it.
    """
    storey = MM(STOREY_HEIGHT_MM)
    if floating:
        above = neighbour_level(level, +1)
        _setp(inst, BuiltInParameter.FAMILY_BASE_LEVEL_OFFSET_PARAM, 0.0)
        if above is not None:
            _setp(inst, BuiltInParameter.FAMILY_TOP_LEVEL_PARAM, above.Id)
            _setp(inst, BuiltInParameter.FAMILY_TOP_LEVEL_OFFSET_PARAM, 0.0)
        else:
            _setp(inst, BuiltInParameter.FAMILY_TOP_LEVEL_OFFSET_PARAM, storey)
            _setp(inst, BuiltInParameter.FAMILY_TOP_LEVEL_PARAM, level.Id)
        return
    below = neighbour_level(level, -1)
    if below is not None:
        _setp(inst, BuiltInParameter.FAMILY_BASE_LEVEL_PARAM, below.Id)
        _setp(inst, BuiltInParameter.FAMILY_BASE_LEVEL_OFFSET_PARAM, 0.0)
    else:
        _setp(inst, BuiltInParameter.FAMILY_BASE_LEVEL_OFFSET_PARAM, -storey)
    _setp(inst, BuiltInParameter.FAMILY_TOP_LEVEL_PARAM, level.Id)
    _setp(inst, BuiltInParameter.FAMILY_TOP_LEVEL_OFFSET_PARAM, 0.0)


def wall_runs(outline, t_min=100.0, t_max=600.0):
    """Split a wall drawn as one outline (an L, a U, a lift shaft) into straight runs.

    Each pair of opposite, parallel faces a wall-thickness apart is one run; its
    centreline is the midline over the stretch where both faces exist. Runs on
    the same centreline that nearly meet are merged, and each run end is then
    carried to the centreline of the run it butts into, which is where Revit
    wants two walls to meet to join them cleanly.

    Returns [(start, end, thickness)] in drawing millimetres.
    """
    import math
    pts = [tuple(p) for p in outline]
    if len(pts) > 2 and pts[0] == pts[-1]:
        pts = pts[:-1]
    edges = [(pts[i], pts[(i + 1) % len(pts)]) for i in range(len(pts))]
    runs = []
    for i, (a, b) in enumerate(edges):
        L = math.hypot(b[0] - a[0], b[1] - a[1])
        if L < t_min:
            continue
        ux, uy = (b[0] - a[0]) / L, (b[1] - a[1]) / L
        for j in range(i + 1, len(edges)):
            c, d = edges[j]
            L2 = math.hypot(d[0] - c[0], d[1] - c[1])
            if L2 < t_min:
                continue
            vx, vy = (d[0] - c[0]) / L2, (d[1] - c[1]) / L2
            if abs(ux * vy - uy * vx) > 0.02:          # not parallel
                continue
            # perpendicular distance between the two face lines
            dist = abs((c[0] - a[0]) * -uy + (c[1] - a[1]) * ux)
            if not (t_min <= dist <= t_max):
                continue
            s = sorted([0.0, L])
            tc = sorted([(c[0] - a[0]) * ux + (c[1] - a[1]) * uy,
                         (d[0] - a[0]) * ux + (d[1] - a[1]) * uy])
            lo, hi = max(s[0], tc[0]), min(s[1], tc[1])
            if hi - lo < t_min:
                continue
            side = 1.0 if ((c[0] - a[0]) * -uy + (c[1] - a[1]) * ux) > 0 else -1.0
            nx, ny = -uy * side * dist / 2.0, ux * side * dist / 2.0
            runs.append([[a[0] + ux * lo + nx, a[1] + uy * lo + ny],
                         [a[0] + ux * hi + nx, a[1] + uy * hi + ny], dist])

    def direction(r):
        L = math.hypot(r[1][0] - r[0][0], r[1][1] - r[0][1])
        return (r[1][0] - r[0][0]) / L, (r[1][1] - r[0][1]) / L

    # merge runs that lie on one centreline with a gap no wider than a wall
    merged = True
    while merged:
        merged = False
        for i in range(len(runs)):
            for j in range(i + 1, len(runs)):
                r, q = runs[i], runs[j]
                ux, uy = direction(r)
                qx, qy = direction(q)
                if abs(ux * qy - uy * qx) > 0.02:
                    continue
                if abs((q[0][0] - r[0][0]) * -uy + (q[0][1] - r[0][1]) * ux) > 20.0:
                    continue
                ts = [(p[0] - r[0][0]) * ux + (p[1] - r[0][1]) * uy
                      for p in (r[0], r[1], q[0], q[1])]
                if max(ts[0], ts[1]) + r[2] * 1.5 < min(ts[2], ts[3]) or \
                   max(ts[2], ts[3]) + r[2] * 1.5 < min(ts[0], ts[1]):
                    continue
                lo, hi = min(ts), max(ts)
                runs[i] = [[r[0][0] + ux * lo, r[0][1] + uy * lo],
                           [r[0][0] + ux * hi, r[0][1] + uy * hi], max(r[2], q[2])]
                del runs[j]
                merged = True
                break
            if merged:
                break

    # carry each end on to the centreline of a crossing run it stops just short of
    for r in runs:
        ux, uy = direction(r)
        for k, sgn in ((0, -1.0), (1, 1.0)):
            p = r[k]
            for q in runs:
                if q is r:
                    continue
                qx, qy = direction(q)
                den = ux * qy - uy * qx
                if abs(den) < 0.5:
                    continue
                # step along r's direction from p until q's centreline
                wx, wy = q[0][0] - p[0], q[0][1] - p[1]
                step = (wx * qy - wy * qx) / den
                if not (0.0 < step * sgn <= q[2] * 1.1):
                    continue
                hit = (p[0] + ux * step, p[1] + uy * step)
                qt = (hit[0] - q[0][0]) * qx + (hit[1] - q[0][1]) * qy
                qlen = math.hypot(q[1][0] - q[0][0], q[1][1] - q[0][1])
                if -q[2] <= qt <= qlen + q[2]:
                    r[k] = [hit[0], hit[1]]
                    break
    return [(r[0], r[1], r[2]) for r in runs]


NOT_A_SHEAR_WALL = ("FND", "FOUND", "FOOTING", "RETAIN", "BRICK", "MASONRY", "CMU", "BLOCK", "STUD",
                    "PARTITION", "CURTAIN", "GLAZ", "PARAPET", "SOFFIT", "INSUL", "CLAD")


def wall_bases(names):
    """Wall types to make an RCC shear wall type from, best first: concrete ones, then
    generic, never first a footing, retaining or masonry type ('Wall-Fnd_300Con_Footing'
    had been given to every 300 mm shear wall, which Revit then showed as a foundation
    wall). Plain arithmetic on names, so it is tested without Revit."""
    def rank(n):
        u = (n or "").upper()
        bad = any(w in u for w in NOT_A_SHEAR_WALL)
        conc = "RCC" in u or "CONCRETE" in u or "CONC" in u
        return (1 if bad else 0, 0 if conc else 1 if "GENERIC" in u else 2)
    return sorted(names, key=rank)


def wall_type_for(thickness_mm, cache):
    """An RCC shear wall type exactly this thick: one concrete layer, nothing else.

    Named "RCC Shear Wall 300 mm" and so on, reused if already in the model (repaired if it
    is not one layer that thick), else made from the best base (wall_bases). A footing or
    retaining type of the same thickness is never taken as it is; a base whose function
    is foundation / retaining / soffit is set to an ordinary wall in the new type.
    """
    key = int(round(thickness_mm))
    if key in cache:
        return cache[key]
    from Autodesk.Revit.DB import WallKind
    name = "RCC Shear Wall %d mm" % key
    basic = []
    for wt in collect(BuiltInCategory.OST_Walls):
        try:
            if wt.Kind == WallKind.Basic:
                basic.append(wt)
        except Exception:
            continue
    concrete = concrete_material()
    existing = next((wt for wt in basic if wt.Name == name), None)
    if existing is not None:
        if not _is_single_layer(existing, thickness_mm, concrete) and                 not _single_layer(existing, thickness_mm, concrete) and not _is_single_layer(existing, thickness_mm):
            say("WARNING  Wall type '%s' is not %d mm thick and could not be corrected. Set its "
                "structure in Edit Type." % (name, key))
        cache[key] = existing
        return existing
    by_name = dict((wt.Name, wt) for wt in basic)
    errors = []
    for n in wall_bases(list(by_name)):
        new = None
        try:
            new = by_name[n].Duplicate(name)
            if _single_layer(new, thickness_mm, concrete):
                try:
                    p = new.get_Parameter(BuiltInParameter.FUNCTION_PARAM)
                    if p is not None and not p.IsReadOnly and p.AsInteger() in (2, 3, 4):
                        p.Set(1)           # foundation / retaining / soffit -> an ordinary (exterior) wall
                except Exception:
                    pass
                if concrete is None:
                    say("NOTE     No material named concrete in this model; '%s' took its material "
                        "from '%s'. Check it in Edit Type." % (name, n))
                cache[key] = new
                return new
            errors.append("%s: structure not accepted" % n)
        except Exception as ex:
            errors.append("%s: %s" % (n, str(ex).splitlines()[0]))
        if new is not None:            # leave no half-made type behind with the name
            try:
                doc.Delete(new.Id)
            except Exception:
                pass
    fallback = wall_bases(list(by_name))
    cache[key] = by_name[fallback[0]] if fallback else None
    say("WARNING  Could not make the wall type '%s'; its walls were given '%s' instead. Change "
        "their type by hand. %s" % (name, fallback[0] if fallback else "nothing", "; ".join(errors[:3])))
    return cache[key]


NOT_CAST_CONCRETE = ("MASONRY", "CMU", "BLOCK", "BRICK", "PRECAST", "PAVER", "TILE")


def _concrete_first(items, name_of):
    """Concrete-looking names first, cast-in-place / RCC before any other concrete:
    duplicating 'Floor_Timber_22Cbd-225Joist' gave the RCC slabs a timber material in
    their project, and 'Concrete Masonry Units' gave the shear walls a block pattern in
    Winter Villa's. Masonry, block and precast go last with timber and steel."""
    def rank(x):
        try:
            n = (name_of(x) or "").upper()
        except Exception:
            n = ""
        if any(w in n for w in NOT_CAST_CONCRETE):
            return 4
        if "RCC" in n or ("CAST" in n and "CONC" in n):
            return 0
        if "CONCRETE" in n or "CONC" in n:
            return 1
        if "GENERIC" in n:
            return 2
        if any(w in n for w in ("TIMBER", "WOOD", "JOIST", "STEEL", "METAL", "DECK")):
            return 4
        return 3
    return sorted(items, key=rank)


def concrete_material():
    """A material called concrete in this model - cast-in-place first, never a masonry /
    block / precast one ('Concrete Masonry Units') - or None."""
    try:
        from Autodesk.Revit.DB import Material
        mats = list(FilteredElementCollector(doc).OfClass(Material).ToElements())
    except Exception:
        return None
    for m in _concrete_first(mats, lambda m: m.Name):
        n = (m.Name or "").upper()
        if ("CONCRETE" in n or "RCC" in n) and not any(w in n for w in NOT_CAST_CONCRETE):
            return m.Id
    return None


def _single_layer(ft, thickness_mm, mat):
    """Make floor type `ft` one structural layer, thickness_mm of `mat`. True if done.

    Edits the type's own compound structure rather than building a new one:
    CreateSingleLayerCompoundStructure() carries a default end cap that Revit
    refuses for a floor ("wrong EndCap condition"). Checked afterwards by width.
    """
    from Autodesk.Revit.DB import (CompoundStructure, CompoundStructureLayer,
                                   MaterialFunctionAssignment, ShellLayerType)
    from System.Collections.Generic import List
    cs = ft.GetCompoundStructure()
    if cs is None:
        return False
    if mat is None:
        layers = list(cs.GetLayers())
        i = cs.StructuralMaterialIndex
        if not (0 <= i < len(layers)):
            i = max(range(len(layers)), key=lambda k: layers[k].Width)
        mat = layers[i].MaterialId
    tries = []
    try:
        cs.SetNumberOfShellLayers(ShellLayerType.Exterior, 0)
        cs.SetNumberOfShellLayers(ShellLayerType.Interior, 0)
        lst = List[CompoundStructureLayer]()
        lst.Add(CompoundStructureLayer(MM(thickness_mm), MaterialFunctionAssignment.Structure, mat))
        cs.SetLayers(lst)
        cs.StructuralMaterialIndex = 0
        tries.append(cs)
    except Exception:
        pass
    try:
        one = CompoundStructure.CreateSingleLayerCompoundStructure(
            MaterialFunctionAssignment.Structure, MM(thickness_mm), mat)
        try:
            one.EndCap = ft.GetCompoundStructure().EndCap
        except Exception:
            pass
        tries.append(one)
    except Exception:
        pass
    for s in tries:
        try:
            ft.SetCompoundStructure(s)
        except Exception:
            continue
        if _is_single_layer(ft, thickness_mm, mat):
            return True
    return False


def _is_single_layer(ft, thickness_mm, mat=None):
    """One layer, thickness_mm thick (and of `mat` if given)."""
    try:
        cs = ft.GetCompoundStructure()
        layers = list(cs.GetLayers())
        return (len(layers) == 1 and abs(cs.GetWidth() - MM(thickness_mm)) < MM(0.5)
                and (mat is None or layers[0].MaterialId == mat))
    except Exception:
        return False


def floor_type_for(thickness_mm, cache):
    """A structural floor type exactly this thick: one RCC layer, nothing else.

    Named "RCC Slab 150 mm" and so on, and reused if it is already in the model -
    after checking it really is one layer that thick, and repairing it if not.
    A failed repair had made the script skip the type and give every slab
    'Floor-Grnd-Bearing_65Scr-90Ins-125Conc-50SBld-150Hcore', 480 mm thick, which
    buried the 450 mm beams and hung the sunk slabs far below them.
    """
    key = int(round(thickness_mm))
    if key in cache:
        return cache[key]
    name = "RCC Slab %d mm" % key
    types = collect(BuiltInCategory.OST_Floors)
    concrete = concrete_material()

    existing = None
    for ft in types:
        try:
            if ft.Name == name:
                existing = ft
                break
        except Exception:
            continue
    if existing is not None:
        if not _is_single_layer(existing, thickness_mm, concrete):
            if _single_layer(existing, thickness_mm, concrete):
                say("FIXED    Floor type '%s' was not one %d mm concrete layer; it is now."
                    % (name, key))
            elif not _is_single_layer(existing, thickness_mm):
                say("WARNING  Floor type '%s' is not %d mm thick and could not be "
                    "corrected. Set its structure in Edit Type." % (name, key))
        cache[key] = existing
        return existing

    errors = []
    for ft in _concrete_first(types, lambda f: f.Name):
        new = None
        try:
            new = ft.Duplicate(name)
            if _single_layer(new, thickness_mm, concrete):
                if concrete is None:
                    say("NOTE     No material named concrete in this model; '%s' took its "
                        "material from '%s'. Check it in Edit Type." % (name, ft.Name))
                cache[key] = new
                return new
            errors.append("%s: structure not accepted" % ft.Name)
        except Exception as ex:
            errors.append("%s: %s" % (getattr(ft, "Name", "?"), str(ex).splitlines()[0]))
        if new is not None:            # leave no half-made type behind with the name
            try:
                doc.Delete(new.Id)
            except Exception:
                pass
    fallback = _concrete_first(types, lambda f: f.Name)
    cache[key] = fallback[0] if fallback else None
    say("WARNING  Could not make the floor type '%s'; its slabs were given '%s' "
        "instead. Change their type by hand. %s" % (
            name, getattr(cache[key], "Name", "nothing"), "; ".join(errors[:3])))
    return cache[key]


def slab_loop_points(outline, min_mm=1.0):
    """The outline with repeated points taken out, the closing one included.

    Every edge of a floor's boundary has to be there for the loop to close. Skipping
    a near-zero edge while drawing it, as this used to, leaves a gap Revit refuses.
    """
    pts = []
    for p in outline or []:
        if not pts or abs(p[0] - pts[-1][0]) + abs(p[1] - pts[-1][1]) > min_mm:
            pts.append((float(p[0]), float(p[1])))
    while len(pts) > 1 and abs(pts[0][0] - pts[-1][0]) + abs(pts[0][1] - pts[-1][1]) <= min_mm:
        pts.pop()
    return pts if len(pts) >= 3 else []


FLOOR_WORDS = {"GROUND": 0, "PLINTH": 0, "FIRST": 1, "SECOND": 2, "THIRD": 3,
               "FOURTH": 4, "FIFTH": 5, "SIXTH": 6, "SEVENTH": 7, "EIGHTH": 8,
               "NINTH": 9, "TENTH": 10}


def floor_number(text):
    """The floor a drawing is for, from its file name: '1ST FLOOR PLAN' is 1."""
    import re
    t = os.path.basename(str(text or "")).upper()
    m = re.search(r"\b(\d+)\s*(?:ST|ND|RD|TH)?\s*(?:FLOOR|FLR|SLAB)\b", t)
    if m:
        return int(m.group(1))
    for word, n in FLOOR_WORDS.items():
        if re.search(r"\b%s\s*(?:FLOOR|FLR|SLAB)\b" % word, t):
            return n
    return None


def level_for_floor(n, all_levels):
    """'Level 1', 'L1', '1st Floor', 'First Floor' ... for floor n, or None."""
    import re
    for l in all_levels:
        name = l.Name.upper()
        nums = [int(x) for x in re.findall(r"\d+", name)]
        if nums and nums[0] == n and not re.search(r"\bB\d|BASEMENT", name):
            return l
    for word, k in FLOOR_WORDS.items():
        if k == n:
            for l in all_levels:
                if word in l.Name.upper():
                    return l
    return None


ROOF_WORDS = ("TERRACE", "ROOF", "TOP SLAB", "OHT", "HEAD ROOM", "MUMTY")


def is_roof(text):
    """A roof or terrace plan: framed above the top floor, and named for no number."""
    t = os.path.basename(str(text or "")).upper()
    return any(w in t for w in ROOF_WORDS)


def drawing_levels():
    """levels.json of the export (the folder above this floor's), or []."""
    try:
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(MODEL_JSON))), "levels.json")
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return []


def level_at(name, entries, existing):
    """The model level standing where the drawings' level `name` stands, when the model has
    none of that name: the only level within 1 mm of its elevation, and not itself another
    of the drawings' levels (that one is its own floor). None otherwise. Plain arithmetic,
    so it is tested without Revit."""
    mine = [e for e in entries if (e.get("revit_name") or "").strip().lower() == str(name).strip().lower()
            and e.get("elevation_mm") is not None and e.get("status") in ("read", "override")]
    if len(mine) != 1:
        return None
    z = float(mine[0]["elevation_mm"])
    named = set((e.get("revit_name") or "").strip().lower() for e in entries)
    at = [n for n, v in existing.items() if abs(float(v) - z) <= 1.0]
    if len(at) != 1 or at[0].strip().lower() in named:
        return None
    return at[0]


def pick_level(name, drawing=None):
    """The level the BEAMS of this plan sit on.

    Named explicitly, that one. Otherwise the floor is read off the drawing's
    name: a '1ST FLOOR PLAN' is framed at Level 1, and its columns then stand on
    Level 0 below it. Falling back to the lowest level put a 1st floor's beams on
    Level 0 with its columns 3 m underground.
    """
    all_levels = levels()
    if not all_levels:
        return None
    if name:
        for l in all_levels:
            if l.Name.strip().lower() == str(name).strip().lower():
                return l
        entries = drawing_levels()
        stand = level_at(name, entries, dict((l.Name, l.Elevation * 304.8) for l in all_levels)) if entries else None
        if stand:
            say("No level in this model is named '%s'; '%s' stands at its height, so this floor is "
                "built on it. Rename it '%s' in Revit if you like." % (name, stand, name))
            return next(l for l in all_levels if l.Name == stand)
        say("No level in this model is named '%s'. The levels here are: %s. Rename one "
            "in Revit, or type the right name in the extractor's Revit level box (or "
            "LEVEL_NAME at the top of this file), and run again."
            % (name, ", ".join("'%s'" % l.Name for l in all_levels)))
        return None
    if is_roof(drawing):
        for l in all_levels:
            if any(w in l.Name.upper() for w in ROOF_WORDS):
                say("The drawing is a roof / terrace plan, so its beams go on '%s'." % l.Name)
                return l
        say("The drawing is a roof / terrace plan but no level is named Roof or Terrace. "
            "Add one above '%s' (Architecture > Level, in an elevation) and run again, or "
            "set LEVEL_NAME." % all_levels[-1].Name)
        return None
    n = floor_number(drawing)
    if n is not None:
        l = level_for_floor(n, all_levels)
        if l is not None:
            say("The drawing is a floor %d plan, so its beams go on '%s'. Set "
                "LEVEL_NAME to choose another." % (n, l.Name))
            return l
        # Falling back to the lowest level used to be harmless with one floor in the
        # model. With several it builds this floor straight through the one below.
        say("The drawing is a floor %d plan but no level in this model is named for "
            "it. Add 'Level %d' (Architecture > Level, in an elevation) and run again, "
            "or set LEVEL_NAME." % (n, n))
        return None
    if len(all_levels) > 1:
        say("Cannot tell which floor this drawing is from its name. Set LEVEL_NAME at "
            "the top of this file to the level its beams sit on, and run again.")
        return None
    return all_levels[0]


SIZE_PARAMS = [("b", "h"), ("Width", "Depth"), ("Width", "Height"),
               ("b", "d"), ("Section Width", "Section Depth")]


def set_size(symbol, width_mm, depth_mm):
    """Try the usual parameter names until one pair takes the values.

    Returns the pair that worked, so the log can say which one was used and you
    can check it is the pair you expect for that family.
    """
    for wn, dn in SIZE_PARAMS:
        pw = symbol.LookupParameter(wn)
        pd = symbol.LookupParameter(dn)
        if pw and pd and not pw.IsReadOnly and not pd.IsReadOnly:
            pw.Set(MM(width_mm))
            pd.Set(MM(depth_mm))
            return (wn, dn)
    return None


def base_symbol(bic):
    """A family that can actually take a width and a depth.

    Deliberately strict. A steel universal section will happily accept values
    into parameters that happen to be called b and d, and you get 137 identical
    I-beams that look like a working model but are nothing like the drawing. It
    is better to stop and ask for the right family.
    """
    syms = collect(bic)
    if not syms:
        return None, []
    named = []
    for s in syms:
        try:
            named.append(((s.FamilyName or "") + " " + (s.Name or ""), s))
        except Exception:
            continue

    def looks_right(name):
        n = name.lower()
        if any(bad in n for bad in ("universal", "wide flange", "hss", "angle",
                                    "channel", "hollow", "tee", "pipe", "ub-", "uc-",
                                    "w-", "hp-", "steel", "round", "circular")):
            return False
        return "concrete" in n or "rectangular" in n or "rect" in n

    for name, s in named:
        if looks_right(name) and "concrete" in name.lower():
            return s, named
    for name, s in named:
        if looks_right(name):
            return s, named
    return None, named


class TypeCache(object):
    """One Revit type per distinct size, made once and reused."""

    def __init__(self, bic, prefix):
        self.base, self.loaded = base_symbol(bic)
        self.param_pair = None
        self.prefix = prefix
        self.made = {}
        self.failed = False

    def get(self, width_mm, depth_mm):
        if self.base is None:
            return None
        key = (int(round(width_mm)), int(round(depth_mm)))
        if key in self.made:
            return self.made[key]
        name = "%s %dx%d" % (self.prefix, key[0], key[1])

        for s in collect_same_family(self.base):      # already in the model?
            if s.Name == name:
                self.made[key] = s
                return s
        try:
            sym = self.base.Duplicate(name)
            pair = set_size(sym, width_mm, depth_mm)
            if pair and not self.param_pair:
                self.param_pair = pair
            if not pair:
                if not self.failed:
                    say("Could not set the size on '%s'. Its family does not use any of "
                        "the parameter names this script knows (%s). Types were made but "
                        "all at the base size." %
                        (self.base.FamilyName, ", ".join(a + "/" + b for a, b in SIZE_PARAMS)))
                    self.failed = True
            self.made[key] = sym
            return sym
        except Exception as exc:
            say("Could not create type %s: %s" % (name, exc))
            return None


def collect_same_family(symbol):
    fam = symbol.Family
    return [doc.GetElement(i) for i in fam.GetFamilySymbolIds()]


# Revit's own round concrete column, loaded from its content library when a plan has
# round columns and the model has no round column family yet.
ROUND_COLUMN_FILES = ["English/US/Structural Columns/Concrete/M_Concrete-Round-Column.rfa",
                      "English-Imperial/US/Structural Columns/Concrete/Concrete-Round-Column.rfa",
                      "*/Structural Columns/Concrete/*Round*.rfa",
                      "*/*/Structural Columns/Concrete/*Round*.rfa"]
DIAMETER_PARAMS = ["b", "Diameter", "D", "d"]


def _round_family_name(name):
    n = name.lower()
    if any(bad in n for bad in ("steel", "pipe", "hss", "hollow", "tube")):
        return False
    return "round" in n or "circular" in n


class RoundTypeCache(object):
    """One Revit type per diameter of a round concrete column family, made once."""

    def __init__(self):
        self.base = None
        self.tried = False
        self.loaded_from = ""
        self.made = {}
        self.failed = False

    def _find(self):
        for s in collect(BuiltInCategory.OST_StructuralColumns):
            try:
                if _round_family_name((s.FamilyName or "") + " " + (s.Name or "")):
                    return s
            except Exception:
                continue
        return None

    def _load(self):
        import glob
        root = os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), "Autodesk",
                            "RVT %s" % doc.Application.VersionNumber, "Libraries")
        for pat in ROUND_COLUMN_FILES:
            for f in sorted(glob.glob(os.path.join(root, *pat.split("/")))):
                n = os.path.basename(f).lower()
                if "drop cap" in n or "precast" in n:
                    continue
                try:
                    if doc.LoadFamily(f):
                        self.loaded_from = f
                        return self._find()
                except Exception:
                    continue
        return None

    def get(self, diameter_mm):
        if not self.tried:
            self.tried = True
            self.base = self._find() or self._load()
        if self.base is None:
            return None
        key = int(round(diameter_mm))
        if key in self.made:
            return self.made[key]
        name = "C %d dia" % key
        for s in collect_same_family(self.base):
            if s.Name == name:
                self.made[key] = s
                return s
        try:
            sym = self.base.Duplicate(name)
            ok = False
            for pn in DIAMETER_PARAMS:
                p = sym.LookupParameter(pn)
                if p and not p.IsReadOnly:
                    p.Set(MM(diameter_mm))
                    ok = True
                    break
            if not ok and not self.failed:
                say("Could not set the diameter on '%s' (none of %s). Round columns were "
                    "made at its base size." % (self.base.FamilyName, ", ".join(DIAMETER_PARAMS)))
                self.failed = True
            self.made[key] = sym
            return sym
        except Exception as exc:
            say("Could not create type %s: %s" % (name, exc))
            return None


def activate(symbol):
    if symbol and not symbol.IsActive:
        symbol.Activate()
        doc.Regenerate()


def set_mark(el, mark):
    if not mark:
        return
    p = el.get_Parameter(BuiltInParameter.ALL_MODEL_MARK)
    if p and not p.IsReadOnly:
        p.Set(str(mark))


SAME_PLACE = 50.0           # mm: nearer than this counts as the same element


class Already(object):
    """What is already in the model, so running twice does not build it twice.

    An element is treated as already present if something in the same category
    and the same storey carries the same Mark, or, for untagged elements, sits
    within SAME_PLACE of the same spot. That makes a re-run safe: it fills in what
    is missing and leaves the rest alone.

    The storey matters once a second floor goes into the same model. Every floor
    has a B1 and a column at grid A-1; checked by mark or plan position alone, the
    2nd floor's would all be taken for the 1st floor's and skipped. So each element
    is kept with the height of its middle, and only one inside the storey being
    built counts.
    """

    def __init__(self, bic):
        self.items = []                 # (mark, x, y, z of its middle)
        self.segs = []                  # (mark, ax, ay, bx, by, z) of curve elements
        for el in (FilteredElementCollector(doc).OfCategory(bic)
                   .WhereElementIsNotElementType()):
            p = el.get_Parameter(BuiltInParameter.ALL_MODEL_MARK)
            m = norm(p.AsString()) if p else ""
            pt = self._point(el)
            z = self._mid_z(el)
            if m or pt:
                self.items.append((m, pt[0] if pt else None, pt[1] if pt else None, z))
            try:
                c = el.Location.Curve
                a, b = c.GetEndPoint(0), c.GetEndPoint(1)
                self.segs.append((m, a.X, a.Y, b.X, b.Y, z))
            except Exception:
                pass

    @property
    def points(self):
        return [i for i in self.items if i[1] is not None]

    @staticmethod
    def _point(el):
        try:
            loc = el.Location
            if hasattr(loc, "Point") and loc.Point:
                return (loc.Point.X, loc.Point.Y)
            if hasattr(loc, "Curve") and loc.Curve:
                a = loc.Curve.GetEndPoint(0)
                b = loc.Curve.GetEndPoint(1)
                return ((a.X + b.X) / 2.0, (a.Y + b.Y) / 2.0)
        except Exception:
            pass
        try:                                   # a floor has no location point
            bb = el.get_BoundingBox(None)
            if bb is not None:
                return ((bb.Min.X + bb.Max.X) / 2.0, (bb.Min.Y + bb.Max.Y) / 2.0)
        except Exception:
            pass
        return None

    @staticmethod
    def _mid_z(el):
        try:
            bb = el.get_BoundingBox(None)
            if bb is not None:
                return (bb.Min.Z + bb.Max.Z) / 2.0
        except Exception:
            pass
        return None

    def has(self, mark, x, y, band=None):
        """band is (z_low, z_high), the storey the new element occupies."""
        m = norm(mark)
        tol = MM(SAME_PLACE)
        for im, px, py, z in self.items:
            if band is not None and z is not None and not (band[0] < z <= band[1]):
                continue
            if m and im == m:
                return True
            if px is not None and abs(px - x) <= tol and abs(py - y) <= tol:
                return True
        return False

    def on_line(self, mark, x, y, band=None):
        """An element with this mark whose line passes within SAME_PLACE of (x, y).
        For unmarked-in-the-drawing pieces such as slab folds: Revit's joins trim or
        extend a beam's ends, so its middle moves and has() alone misses it on a re-run."""
        m = norm(mark)
        tol = MM(SAME_PLACE)
        for im, ax, ay, bx, by, z in self.segs:
            if im != m or (band is not None and z is not None and not (band[0] < z <= band[1])):
                continue
            dx, dy = bx - ax, by - ay
            L2 = dx * dx + dy * dy
            t = 0.0 if L2 <= 0 else max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / L2))
            if ((ax + t * dx - x) ** 2 + (ay + t * dy - y) ** 2) ** 0.5 <= tol:
                return True
        return False

    # Deliberately no add(): this index is a snapshot of the model as it was when
    # the run started. Elements placed by this run must not suppress each other -
    # two columns can genuinely stack, and the CAD sometimes repeats a tag. On the
    # next run they are found by the collector like anything else.


# ---------------------------------------------------------------- build

# ---------------------------------------------------------------- orientation
# model.json reports a column's rotation as the direction of its LONG side,
# measured from the X axis. A rectangular column family does not start at zero:
# its size parameters are b across and h along, so an unrotated instance already
# has its long side pointing up the Y axis, at 90 degrees.
#
# Applying the reported angle straight to the instance therefore turns every
# column a further 90 degrees, and a plan whose columns should run north-south
# comes out running east-west. That is what happened on Wing B: 26 of the 34
# columns are at 92 degrees and every one of them was built lying on its side.
#
# So the angle to give Revit is the reported one MINUS the 90 the family starts
# with. The offset is a constant of the family, not of the drawing, and it is
# checked against the first column actually placed rather than trusted - see
# calibrate_rotation.
ROT_OFFSET = [-90.0]


def column_rotation_deg(rot_deg, offset=None):
    """The angle to turn a column instance through, in degrees.

    A rectangle looks the same after half a turn, so the answer is normalised to
    between -90 and +90: the model is identical either way and small numbers are
    easier to check by eye in the Properties palette.
    """
    off = ROT_OFFSET[0] if offset is None else offset
    r = (float(rot_deg or 0.0) + off) % 180.0
    if r > 90.0:
        r -= 180.0
    return r


def calibrate_rotation(inst, width_mm, depth_mm, rot_deg):
    """Confirm the family really does start with its long side along Y.

    Measures the placed column instead of trusting the convention. A family
    whose b and h run the other way would put every column 90 degrees out, and
    the whole model would be wrong in a way that looks plausible at a glance.

    Returns True if the offset held, False if it was corrected.
    """
    try:
        bb = inst.get_BoundingBox(None)
        if bb is None:
            return True
        import math as _m
        dx = abs(bb.Max.X - bb.Min.X)
        dy = abs(bb.Max.Y - bb.Min.Y)
        a = _m.radians(float(rot_deg or 0.0))
        w, d = MM(width_mm), MM(depth_mm)
        # what the bounding box should measure if the long side really is at rot
        want_x = abs(d * _m.cos(a)) + abs(w * _m.sin(a))
        want_y = abs(d * _m.sin(a)) + abs(w * _m.cos(a))
        good = abs(dx - want_x) + abs(dy - want_y)
        swapped = abs(dx - want_y) + abs(dy - want_x)
        if swapped < good - MM(20.0):
            ROT_OFFSET[0] += 90.0
            say("The column family starts with its long side along X, not Y, so the "
                "rotation offset has been corrected to %+.0f degrees. This was measured "
                "from the first column placed, not assumed." % ROT_OFFSET[0])
            return False
    except Exception:
        pass
    return True


# ---------------------------------------------------------------- what we placed
# Every element this script creates is recorded, and the list is written to
# placed.json beside model.json. That is the only reliable way to undo a build:
# re-extracting fixes what the NEXT run produces and can do nothing about what a
# previous run already put in the model, so a model built three times holds three
# builds worth of elements, the first two of them from older, worse extractions.
PLACED = []
PLACED_UID = []   # Revit UniqueIds of the same: never reused, unlike the numbers
REMOVED = []      # ids a replace took out during this run


def eid(el):
    """An element id as a plain number, across Revit API versions."""
    try:
        return int(el.Id.Value)                # Revit 2024 and later
    except AttributeError:
        return int(el.Id.IntegerValue)


def _solid_volume(el):
    """Volume (cubic feet) of what Revit actually draws for el, joins applied."""
    from Autodesk.Revit.DB import GeometryInstance, Options, Solid
    opt = Options()
    opt.ComputeReferences = False
    total = 0.0
    try:
        geo = el.get_Geometry(opt)
    except Exception:
        return None
    if geo is None:
        return 0.0
    stack = list(geo)
    while stack:
        g = stack.pop()
        if isinstance(g, Solid):
            try:
                total += g.Volume
            except Exception:
                pass
        elif isinstance(g, GeometryInstance):
            try:
                stack.extend(list(g.GetInstanceGeometry()))
            except Exception:
                pass
    return total


def _framing_strength(el):
    """Which of two beams cuts the other: deeper, then wider, then longer.
    A slab fold always gives way to a beam."""
    try:
        sym = el.Symbol
        h = sym.LookupParameter("h").AsDouble()
        b = sym.LookupParameter("b").AsDouble()
        fold = sym.Name.upper().startswith("FOLD")
    except Exception:
        h = b = 0.0
        fold = False
    try:
        L = el.Location.Curve.Length
    except Exception:
        L = 0.0
    return (0 if fold else 1, round(h, 4), round(b, 4), L)


def join_framing(beams):
    """Join beams that run into each other, the supporting one cutting.

    Beams come from centrelines, so one framing into another runs to its
    centreline and its end sits inside it: two solids in one place, drawn as an
    overlap (1 Oct). Revit's answer is a geometry join; the centrelines stay
    connected, the overlap goes. Deeper beam cuts shallower.
    """
    from Autodesk.Revit.DB import BoundingBoxIntersectsFilter, JoinGeometryUtils, Outline
    from Autodesk.Revit.DB.Structure import StructuralFramingUtils
    # Revit's own beam-end join first, as when a beam is drawn by hand onto
    # another: the end is cut back to the face of the beam it meets.
    ends = 0
    for b in beams:
        for k in (0, 1):
            try:
                if not StructuralFramingUtils.IsJoinAllowedAtEnd(b, k):
                    StructuralFramingUtils.AllowJoinAtEnd(b, k)
                    ends += 1
            except Exception:
                pass
    if ends:
        doc.Regenerate()
        say("ENDS     %d beam ends set to join the beams they meet (cut back to the face)."
            % ends)
    ids = set(eid(b) for b in beams)
    joined, turned = 0, 0
    done = set()
    for b in beams:
        try:
            bb = b.get_BoundingBox(None)
            flt = BoundingBoxIntersectsFilter(Outline(bb.Min, bb.Max))
            near = (FilteredElementCollector(doc)
                    .OfCategory(BuiltInCategory.OST_StructuralFraming)
                    .WhereElementIsNotElementType().WherePasses(flt).ToElements())
        except Exception:
            continue
        for o in near:
            pair = tuple(sorted((eid(b), eid(o))))
            if eid(o) == eid(b) or pair in done:
                continue
            done.add(pair)
            if eid(o) not in ids and eid(b) not in ids:
                continue
            try:
                if not JoinGeometryUtils.AreElementsJoined(doc, b, o):
                    JoinGeometryUtils.JoinGeometry(doc, b, o)
                    joined += 1
            except Exception:
                continue           # touching only, nothing to join
            strong, weak = (b, o) if _framing_strength(b) >= _framing_strength(o) else (o, b)
            try:
                if not JoinGeometryUtils.IsCuttingElementInJoin(doc, strong, weak):
                    JoinGeometryUtils.SwitchJoinOrder(doc, strong, weak)
                    turned += 1
            except Exception:
                pass
    if joined or turned:
        doc.Regenerate()
        say("BEAMS    %d beam-to-beam overlaps joined (the deeper beam cuts the shallower; "
            "the centrelines still meet), %d turned round." % (joined, turned))


def fix_joins_and_check(ids):
    """Revit RC join order, then find framing Revit draws with no body.

    Revit auto-joins each new floor to the beams it touches, and when the floor
    is the cutting element the beam can lose its body: still there, selectable,
    not drawn (Wing B 2nd floor, 1 Oct). The rule in concrete modelling is
    column cuts beam, beam cuts slab, so every join is put that way round. Any
    beam or fold still drawn at under a third of its size is listed by mark and
    element id.
    """
    from Autodesk.Revit.DB import JoinGeometryUtils
    cat_rank = {int(BuiltInCategory.OST_StructuralColumns): 0,
                int(BuiltInCategory.OST_StructuralFraming): 1,
                int(BuiltInCategory.OST_Floors): 2}

    def rank(el):
        try:
            c = el.Category.Id
            v = int(c.Value) if hasattr(c, "Value") else int(c.IntegerValue)
            return cat_rank.get(v)
        except Exception:
            return None

    els = []
    for i in ids:
        el = None
        try:
            el = doc.GetElement(ElementId(i))
        except Exception:
            try:                    # Revit 2024+ wants a 64-bit id
                from System import Int64
                el = doc.GetElement(ElementId(Int64(i)))
            except Exception:
                el = None
        if el is not None and rank(el) is not None:
            els.append(el)
    switched = 0
    for el in els:
        r = rank(el)
        try:
            joined = list(JoinGeometryUtils.GetJoinedElements(doc, el))
        except Exception:
            continue
        for jid in joined:
            other = doc.GetElement(jid)
            ro = rank(other) if other is not None else None
            if ro is None or ro == r:
                continue
            strong, weak = (el, other) if r < ro else (other, el)
            try:
                if not JoinGeometryUtils.IsCuttingElementInJoin(doc, strong, weak):
                    JoinGeometryUtils.SwitchJoinOrder(doc, strong, weak)
                    switched += 1
            except Exception:
                continue
    if switched:
        doc.Regenerate()
        say("JOINS    %d joins turned round so columns cut beams and beams cut slabs "
            "(a slab cutting a beam can leave the beam with no body)." % switched)

    join_framing([el for el in els if rank(el) == 1])

    hidden = []
    for el in els:
        if rank(el) != 1:
            continue
        try:
            curve = el.Location.Curve
            sym = el.Symbol
            b = sym.LookupParameter("b").AsDouble()
            h = sym.LookupParameter("h").AsDouble()
            expected = curve.Length * b * h
        except Exception:
            continue
        vol = _solid_volume(el)
        if vol is None or expected <= 0:
            continue
        if vol < expected / 3.0:
            p = el.get_Parameter(BuiltInParameter.ALL_MODEL_MARK)
            hidden.append(((p.AsString() if p else "") or "?", eid(el), vol / expected))
    if hidden:
        say("HIDDEN   %d beams/folds have almost no body in Revit (selectable, not drawn):"
            % len(hidden))
        for mark, i, f in hidden:
            say("         %s  id %d  drawn at %d%% of its size" % (mark, i, round(100 * f)))
    else:
        say("CHECKED  every beam and fold placed has its full body in Revit.")


BEAM_COLOURS = [            # strong, easy to tell apart
    (230, 25, 75), (60, 180, 75), (0, 130, 200), (245, 130, 48), (145, 30, 180),
    (70, 240, 240), (240, 50, 230), (210, 245, 60), (250, 190, 212), (0, 128, 128),
    (220, 190, 255), (170, 110, 40), (255, 225, 25), (128, 0, 0), (170, 255, 195),
    (128, 128, 0), (255, 215, 180), (0, 0, 128), (128, 128, 128), (0, 0, 0),
    (255, 99, 71), (46, 139, 87), (65, 105, 225), (218, 165, 32), (199, 21, 133),
    (0, 206, 209), (139, 69, 19), (154, 205, 50), (112, 128, 144), (255, 140, 200)]


def _solid_fill_id():
    from Autodesk.Revit.DB import FillPatternElement
    for fp in FilteredElementCollector(doc).OfClass(FillPatternElement):
        try:
            if fp.GetFillPattern().IsSolidFill:
                return fp.Id
        except Exception:
            continue
    return None


def _equals_rule(value, bip=BuiltInParameter.ALL_MODEL_TYPE_NAME):
    from Autodesk.Revit.DB import ParameterFilterRuleFactory
    pid = ElementId(bip)
    try:
        return ParameterFilterRuleFactory.CreateEqualsRule(pid, value)          # 2023+
    except Exception:
        return ParameterFilterRuleFactory.CreateEqualsRule(pid, value, True)    # older


def _filter_views():
    """The views to colour: 3D, plans, sections, elevations - templates instead
    of the views they control, since a templated view refuses its own filters."""
    from Autodesk.Revit.DB import View, ViewType
    kinds = [ViewType.ThreeD, ViewType.FloorPlan, ViewType.EngineeringPlan,
             ViewType.CeilingPlan, ViewType.Section, ViewType.Elevation]
    out, seen = [], set()
    for v in FilteredElementCollector(doc).OfClass(View):
        try:
            if not any(v.ViewType == k for k in kinds):
                continue
            target = v
            if not v.IsTemplate and v.ViewTemplateId != ElementId.InvalidElementId:
                target = doc.GetElement(v.ViewTemplateId)
            if target is None or eid(target) in seen:
                continue
            if not target.IsTemplate and not target.AreGraphicsOverridesAllowed():
                continue
            seen.add(eid(target))
            out.append(target)
        except Exception:
            continue
    return out


def make_beam_size_filters():
    """One view filter per beam size, same size = same colour, in every view."""
    from Autodesk.Revit.DB import (Color, ElementParameterFilter,
                                   OverrideGraphicSettings, ParameterFilterElement)
    from System.Collections.Generic import List

    sizes = set()
    for el in (FilteredElementCollector(doc).OfCategory(BuiltInCategory.OST_StructuralFraming)
               .WhereElementIsNotElementType()):
        try:
            n = doc.GetElement(el.GetTypeId()).Name
        except Exception:
            continue
        if n and (n.startswith("B ") or n.startswith("FOLD ")) and "x" in n:
            sizes.add(n)
    if not sizes:
        return

    def size_key(n):
        try:
            w, d = n.split(" ", 1)[1].split("x")
            return (n.startswith("FOLD"), int(d), int(w))
        except Exception:
            return (True, 0, 0)
    sizes = sorted(sizes, key=size_key)

    existing = {f.Name: f for f in FilteredElementCollector(doc).OfClass(ParameterFilterElement)}
    views = _filter_views()
    solid = _solid_fill_id()

    # colours already given, read back from any view that shows the filter
    colour_of = {}
    for name, f in existing.items():
        if not (name.startswith("Beam ") or name.startswith("Fold ")):
            continue
        for v in views:
            try:
                if v.IsFilterApplied(f.Id):
                    c = v.GetFilterOverrides(f.Id).ProjectionLineColor
                    if c.IsValid:
                        colour_of[name] = (c.Red, c.Green, c.Blue)
                        break
            except Exception:
                continue
    free = [c for c in BEAM_COLOURS if c not in colour_of.values()]

    cats = List[ElementId]()
    cats.Add(ElementId(BuiltInCategory.OST_StructuralFraming))
    made, added = 0, 0
    legend = []
    for tname in sizes:
        # slab folds are framing too, coloured by their own size
        fname = ("Fold " if tname.startswith("FOLD") else "Beam ") + tname.split(" ", 1)[1]
        f = existing.get(fname)
        if f is None:
            try:
                f = ParameterFilterElement.Create(
                    doc, fname, cats, ElementParameterFilter(_equals_rule(tname)))
            except Exception:       # a Revit where Type Name cannot be filtered on
                f = ParameterFilterElement.Create(
                    doc, fname, cats, ElementParameterFilter(
                        _equals_rule(tname, BuiltInParameter.SYMBOL_NAME_PARAM)))
            existing[fname] = f
            made += 1
        if fname not in colour_of:
            colour_of[fname] = free.pop(0) if free else BEAM_COLOURS[len(colour_of) % len(BEAM_COLOURS)]
        r, g, b = colour_of[fname]
        col = Color(r, g, b)
        ogs = OverrideGraphicSettings()
        ogs.SetProjectionLineColor(col)
        ogs.SetCutLineColor(col)
        if solid is not None:
            ogs.SetSurfaceForegroundPatternId(solid)
            ogs.SetSurfaceForegroundPatternColor(col)
            ogs.SetCutForegroundPatternId(solid)
            ogs.SetCutForegroundPatternColor(col)
        for v in views:
            try:
                if not v.IsFilterApplied(f.Id):
                    v.AddFilter(f.Id)
                    added += 1
                v.SetFilterVisibility(f.Id, True)
                v.SetFilterOverrides(f.Id, ogs)
            except Exception:
                continue
        legend.append("%s = RGB %d,%d,%d" % (fname, r, g, b))
    say("FILTERS  %d beam and slab-fold sizes, each its own colour, in %d views/templates (%d filters new):"
        % (len(sizes), len(views), made))
    for line in legend:
        say("         " + line)


def sunk_depths(offsets_mm):
    """The sunk depths (whole mm) among slabs' offsets from their level, shallowest
    first: a slab 1 mm or more below its level is sunk, one on or above it is not."""
    return sorted(set(int(round(-float(o))) for o in offsets_mm if o is not None and float(o) <= -1.0))


def sunk_filter_name(depth_mm):
    return "Sunk slab %d mm" % depth_mm


def footing_size_key(name):
    """Colour order of the footing types ("Footing 1800x2100x600", "Footing step
    1200x1500x300"): footings before steps, then by size."""
    try:
        a, b, t = name.rsplit(" ", 1)[1].split("x")
        return (name.startswith("Footing step"), int(a), int(b), int(t))
    except Exception:
        return (True, 0, 0, 0)


def _apply_colour_filters(cats, wanted, refresh=False):
    """One view filter per entry of `wanted`, each its own colour, in every view.

    wanted: [(filter name, [make_rules, ...])] in colour order. make_rules returns
    the FilterRules that must all hold (or a whole ElementFilter); the first one
    this Revit accepts is used. A filter already in the model keeps its colour, and
    its rules unless refresh is set. A new one takes a
    colour no other filter in the views has yet (beams, footings, sunk slabs read
    apart), and only once those run out one another group already uses.
    Returns (views coloured, filters made, legend lines)."""
    from Autodesk.Revit.DB import (Color, ElementParameterFilter, FilterRule,
                                   OverrideGraphicSettings, ParameterFilterElement)
    from System.Collections.Generic import List

    existing = {f.Name: f for f in FilteredElementCollector(doc).OfClass(ParameterFilterElement)}
    views = _filter_views()
    solid = _solid_fill_id()
    names = set(n for n, _ in wanted)
    colour_of, taken = {}, set()
    for name, f in existing.items():
        for v in views:
            try:
                if v.IsFilterApplied(f.Id):
                    c = v.GetFilterOverrides(f.Id).ProjectionLineColor
                    if c.IsValid:
                        if name in names:
                            colour_of[name] = (c.Red, c.Green, c.Blue)
                        else:
                            taken.add((c.Red, c.Green, c.Blue))
                        break
            except Exception:
                continue
    mine = set(colour_of.values())
    free = [c for c in BEAM_COLOURS if c not in mine and c not in taken] + \
        [c for c in BEAM_COLOURS if c not in mine and c in taken]

    cat_ids = List[ElementId]()
    for c in cats:
        cat_ids.Add(ElementId(c))
    made = 0
    legend = []
    def element_filter(make_rules):
        got = make_rules()
        if not isinstance(got, list):
            return got                          # a whole filter (a LogicalOrFilter)
        rules = List[FilterRule]()
        for r in got:
            rules.Add(r)
        return ElementParameterFilter(rules)

    for name, makers in wanted:
        f = existing.get(name)
        if f is not None and refresh:
            for make_rules in makers:
                try:
                    f.SetElementFilter(element_filter(make_rules))
                    break
                except Exception:
                    continue
        if f is None:
            for make_rules in makers:
                try:
                    f = ParameterFilterElement.Create(doc, name, cat_ids, element_filter(make_rules))
                    break
                except Exception:
                    f = None
            if f is None:
                legend.append("%s - Revit would not take this filter, left uncoloured" % name)
                continue
            existing[name] = f
            made += 1
        if name not in colour_of:
            colour_of[name] = free.pop(0) if free else BEAM_COLOURS[len(colour_of) % len(BEAM_COLOURS)]
        r, g, b = colour_of[name]
        col = Color(r, g, b)
        ogs = OverrideGraphicSettings()
        ogs.SetProjectionLineColor(col)
        ogs.SetCutLineColor(col)
        if solid is not None:
            ogs.SetSurfaceForegroundPatternId(solid)
            ogs.SetSurfaceForegroundPatternColor(col)
            ogs.SetCutForegroundPatternId(solid)
            ogs.SetCutForegroundPatternColor(col)
        for v in views:
            try:
                if not v.IsFilterApplied(f.Id):
                    v.AddFilter(f.Id)
                v.SetFilterVisibility(f.Id, True)
                v.SetFilterOverrides(f.Id, ogs)
            except Exception:
                continue
        legend.append("%s = RGB %d,%d,%d" % (name, r, g, b))
    return len(views), made, legend


def column_size_groups(type_names):
    """{filter name: [type names]}: a column's size whichever way it faces, so "C
    300x600" and "C 600x300" - and a pedestal "PD 300x600" - are one colour."""
    out = {}
    for n in type_names:
        if not n or not (n.startswith("C ") or n.startswith("PD ")):
            continue
        try:
            a, b = sorted(int(v) for v in n.split(" ", 1)[1].split("x"))
        except Exception:
            continue
        out.setdefault("Column %dx%d" % (a, b), []).append(n)
    return dict((k, sorted(v)) for k, v in out.items())


def make_column_size_filters():
    """One view filter per column size, same size = same colour, in every view
    (columns, floating columns and pedestals). A column that needs a decision stays
    red: its own override wins over every filter."""
    from Autodesk.Revit.DB import ElementParameterFilter, LogicalOrFilter
    names = set()
    for el in (FilteredElementCollector(doc).OfCategory(BuiltInCategory.OST_StructuralColumns)
               .WhereElementIsNotElementType()):
        try:
            names.add(doc.GetElement(el.GetTypeId()).Name)
        except Exception:
            continue
    groups = column_size_groups(names)
    if not groups:
        return

    def either(tnames, bip=None):
        def make():
            parts = [ElementParameterFilter(_equals_rule(t, bip) if bip else _equals_rule(t)) for t in tnames]
            return parts[0] if len(parts) == 1 else LogicalOrFilter(parts[0], parts[1]) if len(parts) == 2 \
                else LogicalOrFilter(_as_list(parts))
        return make
    wanted = []
    for fname in sorted(groups, key=lambda k: tuple(int(v) for v in k.split(" ")[1].split("x"))):
        wanted.append((fname, [either(groups[fname]),
                               either(groups[fname], BuiltInParameter.SYMBOL_NAME_PARAM)]))
    n_views, made, legend = _apply_colour_filters([BuiltInCategory.OST_StructuralColumns], wanted,
                                                  refresh=True)
    say("FILTERS  %d column sizes, each its own colour, in %d views/templates (%d filters new):"
        % (len(groups), n_views, made))
    for line in legend:
        say("         " + line)


def _as_list(filters):
    from Autodesk.Revit.DB import ElementFilter
    from System.Collections.Generic import List
    out = List[ElementFilter]()
    for f in filters:
        out.Add(f)
    return out


def make_sunk_slab_filters():
    """One view filter per sunk depth, same depth = same colour, in every view. A
    raft (Comments "RAFT ...", from build_extras.py) sits below its level too but is
    not a sunk slab, so the filter leaves it out."""
    from Autodesk.Revit.DB import ParameterFilterRuleFactory as PRF
    offsets = []
    for fl in (FilteredElementCollector(doc).OfCategory(BuiltInCategory.OST_Floors)
               .WhereElementIsNotElementType()):
        try:
            c = fl.get_Parameter(BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
            if c is not None and (c.AsString() or "").upper().startswith("RAFT"):
                continue
            p = fl.get_Parameter(BuiltInParameter.FLOOR_HEIGHTABOVELEVEL_PARAM)
            if p is not None:
                offsets.append(p.AsDouble() * 304.8)
        except Exception:
            continue
    depths = sunk_depths(offsets)
    if not depths:
        return
    off_id = ElementId(BuiltInParameter.FLOOR_HEIGHTABOVELEVEL_PARAM)
    note_id = ElementId(BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
    wanted = []
    for d in depths:
        def at(d=d):
            return PRF.CreateEqualsRule(off_id, -MM(d), MM(0.5))
        wanted.append((sunk_filter_name(d), [
            lambda at=at: [at(), PRF.CreateNotBeginsWithRule(note_id, "RAFT")],         # 2023+
            lambda at=at: [at(), PRF.CreateNotBeginsWithRule(note_id, "RAFT", False)],  # older
            lambda at=at: [at()]]))
    n_views, made, legend = _apply_colour_filters([BuiltInCategory.OST_Floors], wanted)
    say("FILTERS  %d sunk depths, each its own colour, in %d views/templates (%d filters new):"
        % (len(depths), n_views, made))
    for line in legend:
        say("         " + line)


def make_foundation_size_filters():
    """One view filter per footing type ("Footing 1800x2100x600", one type per size,
    from build_extras.py), same size = same colour, in every view. Run by
    build_extras.py after it places the footings."""
    sizes = set()
    for el in (FilteredElementCollector(doc).OfCategory(BuiltInCategory.OST_StructuralFoundation)
               .WhereElementIsNotElementType()):
        try:
            n = doc.GetElement(el.GetTypeId()).Name
        except Exception:
            continue
        if n and n.startswith("Footing ") and "x" in n:
            sizes.add(n)
    if not sizes:
        return
    wanted = []
    for tname in sorted(sizes, key=footing_size_key):
        wanted.append((tname, [
            lambda t=tname: [_equals_rule(t)],
            lambda t=tname: [_equals_rule(t, BuiltInParameter.SYMBOL_NAME_PARAM)]]))
    n_views, made, legend = _apply_colour_filters([BuiltInCategory.OST_StructuralFoundation], wanted)
    say("FILTERS  %d footing sizes, each its own colour, in %d views/templates (%d filters new):"
        % (len(sizes), n_views, made))
    for line in legend:
        say("         " + line)


# Elements built at a working size because nobody has settled them yet (model.json
# rows with a "provisional" note, from building.export): each carries the note in
# its Comments, is coloured PROVISIONAL_RGB in every view, and gets a text note on
# its level's plans, so nobody takes it for a drawn size.
PROVISIONAL = []          # (element id, label, note, point or None)
PROVISIONAL_RGB = (255, 0, 0)
PROVISIONAL_TAG = "NEEDS DECISION: "


def slope_z_mm(slope, x, y):
    """A sloping slab's top at (x, y), mm from its level (model.json "slope": its top runs
    from z0 at p0 to z1 at p1, p measured along dir from origin). Plain arithmetic."""
    p = (x - slope["origin"][0]) * slope["dir"][0] + (y - slope["origin"][1]) * slope["dir"][1]
    lo, hi = sorted((slope["p0"], slope["p1"]))
    p = min(max(p, lo), hi)
    return slope["z0"] + (p - slope["p0"]) * (slope["z1"] - slope["z0"]) / (slope["p1"] - slope["p0"])


def slope_floor(fl, slope, ox, oy):
    """Lift a floor's corners to its slope (building section): the slab shape editor
    moves each corner by its height - Revit 2016 and later, no slope arrow needed."""
    doc.Regenerate()
    ed = fl.SlabShapeEditor
    ed.Enable()
    unit = MM(1.0)
    n = 0
    for v in list(ed.SlabShapeVertices):
        x = v.Position.X / unit + ox
        y = v.Position.Y / unit + oy
        ed.ModifySubElement(v, MM(slope_z_mm(slope, x, y)))
        n += 1
    return n


def section_points(prof, start, end):
    """The corners of a beam's drawn section at its start, in mm: [u, v] (u to the left
    of start -> end from the centreline, v down from the top) -> (x, y, z) offsets from
    the start point. Plain arithmetic, so it is tested without Revit."""
    import math as _m
    dx, dy = end[0] - start[0], end[1] - start[1]
    L = _m.hypot(dx, dy) or 1.0
    nx, ny = -dy / L, dx / L
    return [(nx * float(u), ny * float(u), float(v)) for u, v in prof]


def shaped_beam(prof, p1, p2, start, end):
    """A beam built to its drawn section (model.json "section", e.g. SECTION X-X: 750
    wide, sloped top): the profile carried from p1 to p2 as one solid - a DirectShape
    in the Structural Framing category. Raises if Revit refuses it; the caller then
    builds the usual rectangular beam, marked."""
    from Autodesk.Revit.DB import DirectShape, GeometryCreationUtilities, GeometryObject
    from System.Collections.Generic import List as _L
    f = p2.Subtract(p1)
    length = f.GetLength()
    f = f.Normalize()
    pts = [p1.Add(XYZ(MM(x), MM(y), MM(z))) for x, y, z in section_points(prof, start, end)]
    loop = CurveLoop()
    for i in range(len(pts)):
        a, c = pts[i], pts[(i + 1) % len(pts)]
        if a.DistanceTo(c) > MM(1.0):
            loop.Append(Line.CreateBound(a, c))
    loops = _L[CurveLoop]()
    loops.Add(loop)
    solid = GeometryCreationUtilities.CreateExtrusionGeometry(loops, f, length)
    ds = DirectShape.CreateElement(doc, ElementId(BuiltInCategory.OST_StructuralFraming))
    shapes = _L[GeometryObject]()
    shapes.Add(solid)
    ds.SetShape(shapes)
    return ds


def profile_points(prof):
    """A wall's drawn shape (export: wall "profile") as points in the drawing, mm: (x, y, z)
    with z up from the level below - each [along, up] laid from the origin along dir.
    Plain arithmetic, so it is tested without Revit."""
    (x0, y0), (ux, uy) = prof["origin"][:2], prof["dir"][:2]
    return [(x0 + ux * float(a), y0 + uy * float(a), float(z)) for a, z in prof["points"]]


def profile_wall(pts, wt, level):
    """A structural wall whose outline in its own plane is the closed loop pts (XYZ): an arch
    cut out of its foot, a lean. Raises if Revit refuses it; the caller builds it straight."""
    from Autodesk.Revit.DB import Curve
    from System.Collections.Generic import List as _L
    curves = _L[Curve]()
    for i in range(len(pts)):
        a, c = pts[i], pts[(i + 1) % len(pts)]
        if a.DistanceTo(c) > MM(1.0):
            curves.Add(Line.CreateBound(a, c))
    return Wall.Create(doc, curves, wt.Id, level.Id, True)


def lean_points(a, b, ln, per_mm):
    """A leaning column's two ends (export: "lean", linker._section_lean): its lower end moved
    across by ln "base", its upper by ln "top" (mm; per_mm: model units per mm). a, b: (x, y, z)
    as Revit has them, either way round; returned in the same order. Plain arithmetic, so it
    is tested without Revit."""
    lo_first = a[2] <= b[2]
    (bx, by), (tx, ty) = ln["base"][:2], ln["top"][:2]
    lo, hi = (a, b) if lo_first else (b, a)
    lo = (lo[0] + bx * per_mm, lo[1] + by * per_mm, lo[2])
    hi = (hi[0] + tx * per_mm, hi[1] + ty * per_mm, hi[2])
    return (lo, hi) if lo_first else (hi, lo)


def slant_angle(ln, orient, height_mm):
    """A leaning wall's slant (radians from vertical) and how much of its lean is along it, not
    across (mm): Revit slants a wall only across itself. orient: the wall's (x, y) facing."""
    import math
    dx = float(ln["top"][0]) - float(ln["base"][0])
    dy = float(ln["top"][1]) - float(ln["base"][1])
    across = dx * orient[0] + dy * orient[1]
    along = abs(-dx * orient[1] + dy * orient[0])
    return math.atan2(across, float(height_mm)), along


def lean_column(inst, ln, mark):
    """A column leaning as a wall elevation draws it: made end-point driven (slanted), its ends
    moved across. False, said, when Revit refuses it: it stays upright."""
    try:
        _setp(inst, BuiltInParameter.SLANTED_COLUMN_TYPE_PARAM, 2)       # end point driven
        doc.Regenerate()
        crv = inst.Location.Curve
        p0, p1 = crv.GetEndPoint(0), crv.GetEndPoint(1)
        a, b = lean_points((p0.X, p0.Y, p0.Z), (p1.X, p1.Y, p1.Z), ln, MM(1.0))
        inst.Location.Curve = Line.CreateBound(XYZ(*a), XYZ(*b))
        return True
    except Exception as exc:
        say("column %s: could not lean it as the wall elevation draws (%s) - built upright" % (mark or "?", exc))
        return False


def lean_wall(wall, ln, height_mm, mark):
    """A wall leaning across itself as a wall elevation draws it (its base line already moved by
    ln "base"): slanted, Revit 2021 and later. Which way Revit tilts a positive angle is read
    back from the wall's box. False, said, when it cannot: it stays upright."""
    try:
        from Autodesk.Revit.DB import WallCrossSection
        o = wall.Orientation
        ang, along = slant_angle(ln, (o.X, o.Y), height_mm)
        if abs(ang) < 1e-4:
            return True
        bb0 = wall.get_BoundingBox(None)
        c0 = (bb0.Min + bb0.Max) * 0.5
        wall.CrossSection = WallCrossSection.Slanted
        _setp(wall, BuiltInParameter.WALL_SINGLE_SLANT_ANGLE_FROM_VERTICAL, abs(ang))
        doc.Regenerate()
        bb1 = wall.get_BoundingBox(None)
        c1 = (bb1.Min + bb1.Max) * 0.5
        moved = (c1.X - c0.X) * o.X + (c1.Y - c0.Y) * o.Y
        if abs(moved) < MM(1.0):
            raise Exception("Revit left it upright")
        if moved * ang < 0:
            _setp(wall, BuiltInParameter.WALL_SINGLE_SLANT_ANGLE_FROM_VERTICAL, -abs(ang))
        if along > 50.0:
            say("wall %s: its lean along itself (%.0f mm) is not built - Revit slants a wall only "
                "across itself" % (mark or "?", along))
        return True
    except Exception as exc:
        say("wall %s: could not slant it as the wall elevation draws (%s) - built upright" % (mark or "?", exc))
        return False


def mark_provisional(el, label, note, pt=None):
    set_comment(el, PROVISIONAL_TAG + note)
    PROVISIONAL.append((el.Id, label, note, pt))


def highlight_provisional(level):
    """Colour the provisional elements in every view, over the size filters, and note
    each on this level's plans."""
    if not PROVISIONAL:
        return
    from Autodesk.Revit.DB import (Color, OverrideGraphicSettings, TextNote, TextNoteType,
                                   View, ViewPlan, ViewType)
    col = Color(*PROVISIONAL_RGB)
    ogs = OverrideGraphicSettings()
    ogs.SetProjectionLineColor(col)
    ogs.SetCutLineColor(col)
    solid = _solid_fill_id()
    if solid is not None:
        ogs.SetSurfaceForegroundPatternId(solid)
        ogs.SetSurfaceForegroundPatternColor(col)
        ogs.SetCutForegroundPatternId(solid)
        ogs.SetCutForegroundPatternColor(col)
    kinds = [ViewType.ThreeD, ViewType.FloorPlan, ViewType.EngineeringPlan,
             ViewType.CeilingPlan, ViewType.Section, ViewType.Elevation]
    n_views = 0
    for v in FilteredElementCollector(doc).OfClass(View):
        try:
            if v.IsTemplate or not any(v.ViewType == k for k in kinds):
                continue
            # an element's own override wins over every view filter
            for el_id, _, _, _ in PROVISIONAL:
                v.SetElementOverrides(el_id, ogs)
            n_views += 1
        except Exception:
            continue
    tnt = FilteredElementCollector(doc).OfClass(TextNoteType).FirstElementId()
    n_notes = 0
    for v in FilteredElementCollector(doc).OfClass(ViewPlan):
        try:
            if v.IsTemplate or v.GenLevel is None or v.GenLevel.Id != level.Id:
                continue
            for _, label, note, pt in PROVISIONAL:
                if pt is None:
                    continue
                tn = TextNote.Create(doc, v.Id, XYZ(pt.X, pt.Y, level.Elevation),
                                     "NEEDS DECISION - %s: %s" % (label, note), tnt)
                remember(tn)
                n_notes += 1
        except Exception:
            continue
    say("NEEDS DECISION %d elements built at a working size, coloured red in %d views, the reason in "
        "each one's Comments ('%s...')%s. Settle them in the review and build again."
        % (len(PROVISIONAL), n_views, PROVISIONAL_TAG.strip(),
           (" and %d text notes on the plans of '%s'" % (n_notes, level.Name)) if n_notes else ""))
    for _, label, note, _ in PROVISIONAL[:60]:
        say("         %s: %s" % (label, note))


def remember(el):
    try:
        PLACED.append(eid(el))
        PLACED_UID.append(str(el.UniqueId))
    except Exception:
        pass


PLACED_FILE = os.path.join(os.path.dirname(MODEL_JSON) or ".", "placed.json")


def doc_key():
    try:
        return str(doc.PathName or doc.Title)
    except Exception:
        return ""


def doc_id():
    """A fingerprint of this very model, or "" if Revit gives none.

    Every unsaved model is called "Project1", so the name cannot tell last
    session's model from this one; the first build of a floor in a new session
    then took the old record's id numbers to mean elements here, and deleted
    the floor built just before it. The Project Information element's UniqueId
    is made new with each model.
    """
    try:
        return str(doc.ProjectInformation.UniqueId or "")
    except Exception:
        return ""


def _is_saved():
    try:
        return bool(doc.PathName)
    except Exception:
        return False


def write_placed():
    """Record what this run put in the model, so it can be taken out again.

    This ADDS to the record rather than replacing it. An update run places only
    the few elements that were missing, and writing just those would throw away
    the record of the hundreds already there - after which nothing could remove
    them and the model would have to be cleared by hand.

    Ids removed by a replace earlier in the same run are dropped first, so the
    record always describes what is actually in the model.
    """
    try:
        import datetime
        # Only what this floor placed and is still in the model. The old record
        # kept every id ever written (1562 for a 243-element floor), and an id
        # number can be handed to a new element after an undo - the other floor's.
        keep_uid = []
        if os.path.exists(PLACED_FILE):
            try:
                with open(PLACED_FILE, "r") as f:
                    old = json.load(f)
                same = str(old.get("document") or "") == doc_key()
                if same and old.get("doc_id") and doc_id():
                    same = old.get("doc_id") == doc_id()
                elif same and not old.get("doc_id") and doc_id() and not _is_saved():
                    same = False      # an old unsaved record: may be another Project1
                if same:
                    if old.get("uids") is not None:
                        keep_uid = [str(u) for u in old.get("uids") or []]
                    else:
                        # the old form: carry its numbers over as UniqueIds, but not
                        # one a replace judged to be another floor's (reused number)
                        gone = set(REMOVED) | set(LEGACY_OTHER)
                        for n in old.get("ids") or []:
                            if int(n) in gone:
                                continue
                            try:
                                el = doc.GetElement(ElementId(int(n)))
                            except Exception:
                                el = None
                            if el is not None:
                                keep_uid.append(str(el.UniqueId))
            except Exception:
                keep_uid = []
        uids, ids, seen = [], [], set()
        for u in keep_uid + list(PLACED_UID):
            if u in seen:
                continue
            seen.add(u)
            try:
                el = doc.GetElement(u)
            except Exception:
                el = None
            if el is None:
                continue
            uids.append(u)
            ids.append(eid(el))
        with open(PLACED_FILE, "w") as f:
            json.dump({"document": doc_key(), "doc_id": doc_id(),
                       # strftime, not isoformat(timespec=): IronPython 2.7 / 3.4 lack it
                       "when": datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                       "model_json": os.path.abspath(MODEL_JSON),
                       "level": LEVEL_USED[0] if LEVEL_USED else "",
                       "uids": uids, "ids": ids}, f, indent=1)
        return len(uids)
    except Exception as exc:
        say("Could not write placed.json (%s), so mode=\"replace\" will not be able "
            "to remove this build later." % exc)
        return 0



def delete_all_structure():
    """Delete every structural column, beam, wall and floor in the document.

    Everything, not only what this script placed. That is the point of it: it is
    the way back when the record no longer matches the model. One transaction, so
    one undo restores it.
    """
    from System.Collections.Generic import List as _List
    cats = [(BuiltInCategory.OST_StructuralColumns, "columns"),
            (BuiltInCategory.OST_StructuralFraming, "beams"),
            (BuiltInCategory.OST_Walls, "walls"),
            (BuiltInCategory.OST_Floors, "floors")]
    coll = _List[ElementId]()
    counts = []
    for bic, label in cats:
        found = list(FilteredElementCollector(doc).OfCategory(bic)
                     .WhereElementIsNotElementType().ToElements())
        for el in found:
            coll.Add(el.Id)
        if found:
            counts.append("%d %s" % (len(found), label))
    if coll.Count == 0:
        say("CLEAR_ALL_STRUCTURE: the model holds no structural elements already.")
        return 0
    removed = doc.Delete(coll)
    n = len(removed) if removed is not None else coll.Count
    for i in coll:
        try:
            REMOVED.append(int(i.Value))
        except AttributeError:
            REMOVED.append(int(i.IntegerValue))
    say("CLEAR_ALL_STRUCTURE: deleted everything structural in this model - "
        + ", ".join(counts) + " (%d elements). One undo puts them back." % n)
    return n


LEVEL_USED = []
LEGACY_OTHER = []    # old-record ids a replace left alone as another floor's


def _belongs_to_floor(el, level, fc_marks):
    """For an old record (id numbers only): is el this floor's element?

    This floor holds what sits between the level below and its own level, plus
    its floating columns rising above it. Another floor's beam, slab or column
    whose number turned up in the record is left alone.
    """
    try:
        bb = el.get_BoundingBox(None)
        mid = (bb.Min.Z + bb.Max.Z) / 2.0
    except Exception:
        return False
    below = neighbour_level(level, -1)
    lo = below.Elevation if below is not None else level.Elevation - MM(STOREY_HEIGHT_MM)
    if lo < mid <= level.Elevation + MM(5.0):
        return True
    above = neighbour_level(level, +1)
    hi = above.Elevation if above is not None else level.Elevation + MM(STOREY_HEIGHT_MM)
    try:
        is_col = el.Category.Id == ElementId(BuiltInCategory.OST_StructuralColumns)
        p = el.get_Parameter(BuiltInParameter.ALL_MODEL_MARK)
        mark = norm(p.AsString()) if p else ""
    except Exception:
        return False
    return is_col and mark in fc_marks and level.Elevation < mid <= hi


def earlier_records(placed_file):
    """placed.json of this floor from the project's earlier runs - runs/<time>/revit_build/
    <floor>/placed.json beside this run's - newest first. Each Analyse makes a new run folder,
    so the previous build of this floor is often recorded in another one."""
    import glob as _glob
    here = os.path.dirname(os.path.abspath(placed_file))
    runs = os.path.dirname(os.path.dirname(os.path.dirname(here)))
    me = os.path.normcase(os.path.abspath(placed_file))
    found = _glob.glob(os.path.join(runs, "*", "revit_build", os.path.basename(here), "placed.json"))
    return sorted((p for p in found if os.path.normcase(os.path.abspath(p)) != me), reverse=True)


def delete_earlier_runs():
    """Take out what earlier runs of this project placed for this floor IN THIS VERY MODEL
    (their record's model fingerprint, doc_id, is this model's): left in, they stood where
    this build's elements go, so this build skipped those places and the model kept the old
    ones. Records from any other model, or with no fingerprint, are left alone."""
    me = doc_id()
    if not me:
        return 0
    ours = set()
    for bic in ("OST_StructuralColumns", "OST_StructuralFraming", "OST_Walls", "OST_Floors", "OST_TextNotes"):
        try:
            ours.add(int(getattr(BuiltInCategory, bic)))
        except Exception:
            pass
    ids, runs = [], 0
    for path in earlier_records(PLACED_FILE):
        try:
            with open(path, "r") as f:
                rec = json.load(f)
        except Exception:
            continue
        if rec.get("doc_id") != me or rec.get("uids") is None:
            continue
        n0 = len(ids)
        for u in rec["uids"]:
            try:
                el = doc.GetElement(str(u))
            except Exception:
                el = None
            if el is None:
                continue
            try:
                cid = el.Category.Id
                cnum = int(cid.Value) if hasattr(cid, "Value") else int(cid.IntegerValue)
            except Exception:
                continue
            if cnum in ours and el.Id not in ids:
                ids.append(el.Id)
        runs += 1 if len(ids) > n0 else 0
    if not ids:
        return 0
    from System.Collections.Generic import List as _List
    coll = _List[ElementId]()
    for i in ids:
        coll.Add(i)
    doc.Delete(coll)
    say("mode=\"replace\": also took out %d elements that %d earlier run(s) of this project built for this "
        "floor in this model - they would have kept this build from placing its own." % (len(ids), runs))
    return len(ids)


def delete_previous(level=None, fc_marks=()):
    """Remove the elements a previous run of this script placed. Returns a count.

    Only ids this script recorded, only in the document they were recorded from,
    and only elements that still exist. Everything happens inside the build
    transaction, so one undo puts it all back.
    """
    if not os.path.exists(PLACED_FILE):
        say("mode=\"replace\": no placed.json in this folder, so there is no record "
            "of a previous build to remove. Nothing was deleted.")
        return 0
    try:
        with open(PLACED_FILE, "r") as f:
            rec = json.load(f)
    except Exception as exc:
        say("mode=\"replace\": placed.json could not be read (%s). Nothing was "
            "deleted." % exc)
        return 0

    was = str(rec.get("document") or "")
    now = doc_key()
    if was and now and was != now:
        say("mode=\"replace\": placed.json was written from a different model")
        say("      it recorded: " + was)
        say("      this one is: " + now)
        say("      Nothing was deleted. Open that model, or delete the old elements "
            "by hand.")
        return 0

    if rec.get("doc_id") and doc_id() and rec.get("doc_id") != doc_id():
        say("mode=\"replace\": placed.json was written from another model with the same "
            "name (an unsaved 'Project1' from an earlier session). Nothing was deleted, so "
            "no other floor is touched; this floor is built fresh and recorded anew.")
        return 0
    if not rec.get("doc_id") and doc_id() and not _is_saved():
        say("mode=\"replace\": placed.json is an old record from an unsaved model, so it "
            "cannot be told apart from another 'Project1'. Nothing was deleted, so no other "
            "floor is touched. If this floor's previous build is still in this model, "
            "delete it by hand once (or save the model as a .rvt), then build again.")
        return 0

    # Unsaved models are all called "Project1", so the record can come from a
    # different model with the same name, and its ids then point at whatever
    # happens to carry them here - a view, a level. Only ever take out the kinds
    # of element this script places.
    ours = set()
    for bic in ("OST_StructuralColumns", "OST_StructuralFraming", "OST_Walls",
                "OST_Floors", "OST_TextNotes"):
        try:
            ours.add(int(getattr(BuiltInCategory, bic)))
        except Exception:
            pass
    ids = []
    uids = rec.get("uids")
    legacy = uids is None
    if legacy:
        say("mode=\"replace\": placed.json is in the old form (id numbers only). Only "
            "elements on this floor's storey are taken out; it is rewritten in the new form.")
    skipped_other = 0
    for n in (rec.get("ids") or []) if legacy else uids:
        try:
            if legacy:
                i = ElementId(int(n))
                el = doc.GetElement(i)
            else:
                el = doc.GetElement(str(n))
                i = el.Id if el is not None else None
        except Exception:
            continue
        if el is None:
            continue
        if legacy and level is not None and not _belongs_to_floor(el, level, set(fc_marks)):
            skipped_other += 1
            LEGACY_OTHER.append(int(n))
            continue
        try:
            cat = el.Category
            cid = cat.Id
            cnum = int(cid.Value) if hasattr(cid, "Value") else int(cid.IntegerValue)
            if ours and cnum not in ours:
                continue
        except Exception:
            if ours:
                continue
        ids.append(i)

    if skipped_other:
        say("mode=\"replace\": %d elements in the old record belong to another floor "
            "(their id numbers were reused) and were LEFT ALONE." % skipped_other)
    if not ids:
        say("mode=\"replace\": the %d elements from the previous build are already "
            "gone from this model." % len((rec.get("ids") if legacy else uids) or []))
        return 0

    from System.Collections.Generic import List as _List
    coll = _List[ElementId]()
    for i in ids:
        coll.Add(i)
    removed = doc.Delete(coll)
    n = len(removed) if removed is not None else len(ids)
    for i in ids:
        try:
            REMOVED.append(int(i.Value))
        except AttributeError:
            REMOVED.append(int(i.IntegerValue))
    say("mode=\"replace\": removed %d elements placed by the previous build "
        "(recorded %s). One undo puts them back."
        % (n, rec.get("when") or "at an unknown time"))
    return n


def build():
    if CANCELLED:
        say("Cancelled: no file chosen, so nothing was built.")
        return
    if not os.path.exists(MODEL_JSON):
        say("STOP: could not find model.json at")
        say("      " + str(MODEL_JSON))
        say("")
        say("Either wire the full path into the 'model_json' input, or edit "
            "DEFAULT_MODEL_JSON near the top of this script.")
        return
    say("Reading " + str(MODEL_JSON))
    with open(MODEL_JSON, "r") as f:
        data = json.load(f)

    say("Folder used:    " + os.path.dirname(os.path.abspath(MODEL_JSON)))
    src = data.get("source_file") or ""
    say("Extracted from: " + (str(src) or "(not recorded)"))
    try:
        import datetime
        when = datetime.datetime.fromtimestamp(os.path.getmtime(MODEL_JSON))
        say("Extracted on:   %s" % when.strftime("%d %b %Y, %H:%M"))
        # if the drawing has moved on since the extraction, say so loudly
        if src and os.path.exists(src) and os.path.getmtime(src) > os.path.getmtime(MODEL_JSON):
            say("")
            say("*** WARNING: the drawing has been changed since this extraction. ***")
            say("*** Run the extractor again before building, or this model will  ***")
            say("*** be built from out-of-date geometry.                          ***")
            say("")
    except Exception:
        pass
    c = data.get("counts") or {}
    if c:
        say("That extraction held: "
            + ", ".join("%s %s" % (v, k) for k, v in c.items() if v))

    bp = data.get("base_point") or [0.0, 0.0]
    ox, oy = float(bp[0]), float(bp[1])

    # The level: LEVEL_NAME here if set, else the one chosen in the extractor and
    # saved in model.json, else read from the drawing's name.
    chosen = LEVEL_NAME or data.get("level_name") or ""
    if chosen and not LEVEL_NAME:
        say("model.json says this floor goes on level '%s' (set in the extractor)." % chosen)
    level = pick_level(chosen, data.get("source_file") or MODEL_JSON)
    if level is not None:
        LEVEL_USED[:] = [level.Name]
    if level is None:
        say("STOP: no level to build on (see above). Nothing was placed.")
        return
    say("Building on level '%s'." % level.Name)
    # A reviewed export (building.export) says "strict": nothing is placed on an
    # assumed size or height - no DEFAULT_BEAM_DEPTH, DEFAULT_SLAB_MM, 230 mm width or
    # STOREY_HEIGHT_MM. What would need one is listed as NOT BUILT instead. An
    # extractor model.json (no "strict") is built exactly as before.
    strict = bool(data.get("strict"))
    # the project builds what is unsettled too, at a working size, coloured and noted
    prov_ok = strict and bool(data.get("provisional_ok"))
    held = []
    if prov_ok:
        say("Reviewed export: sizes come from the drawings and the review. What still needs a "
            "decision is built at a working size and marked red - see NEEDS DECISION below.")
    elif strict:
        say("Reviewed export: nothing is placed on an assumed size or height.")

    # read after the level is known: the workbook's rows are per level
    sizes, extra = load_sizes((chosen, level.Name))
    if sizes or extra:
        say("sizes.csv read: %d marks carry a scheduled size, %d a scheduled level or "
            "sunk depth, for level '%s' or for every level."
            % (len(sizes), sum(1 for v in extra.values()
                               if any(x.get("top_mm") is not None or x.get("sunk_mm") is not None
                                      for x in v.values())), level.Name))
    else:
        say("No sizes.csv found. Sizes, beam levels and sunk depths come from the drawing.")

    col_types = TypeCache(BuiltInCategory.OST_StructuralColumns, "C")
    round_types = RoundTypeCache()
    beam_types = TypeCache(BuiltInCategory.OST_StructuralFraming, "B")
    fold_types = TypeCache(BuiltInCategory.OST_StructuralFraming, "FOLD")

    for cache, what, example in (
            (col_types, "column", "M_Concrete-Rectangular-Column.rfa"),
            (beam_types, "beam", "M_Concrete-Rectangular Beam.rfa")):
        if cache.base is None:
            say("STOP: no rectangular concrete %s family is loaded." % what)
            if cache.loaded:
                say("      What is loaded: "
                    + ", ".join(sorted({n.strip() for n, _ in cache.loaded})[:8]))
                say("      Those are steel or non-rectangular sections. Forcing sizes "
                    "into them produces elements that look built but match nothing "
                    "in the drawing, so nothing has been placed.")
            else:
                say("      Nothing at all is loaded for this category.")
            say("      Insert > Load Family, then %s (or your office equivalent)."
                % example)
            return

    say("Columns from family '%s'." % col_types.base.FamilyName)
    n_round = sum(1 for c in data.get("columns", []) if c.get("diameter"))
    if n_round:
        if round_types.get(float(next(c["diameter"] for c in data["columns"] if c.get("diameter")))) is not None:
            say("Round columns (%d) from family '%s'%s." % (
                n_round, round_types.base.FamilyName,
                ", loaded from Revit's library" if round_types.loaded_from else ""))
        else:
            say("%d round columns, but no round column family is loaded or in Revit's library: "
                "they are built square, marked red." % n_round)
    say("Beams from family '%s'." % beam_types.base.FamilyName)

    if CLEAR_ALL_STRUCTURE:
        delete_all_structure()
    elif MODE == "replace":
        delete_previous(level, [norm(c.get("mark")) for c in data.get("columns", [])
                                if is_floating(c)])
        delete_earlier_runs()

    calibrated = [False]
    made = {"columns": 0, "beams": 0, "folds": 0, "walls": 0, "floors": 0}
    already = {"columns": 0, "beams": 0, "folds": 0, "walls": 0, "floors": 0}
    seen = {}
    if MODE != "add":
        seen["columns"] = Already(BuiltInCategory.OST_StructuralColumns)
        seen["beams"] = Already(BuiltInCategory.OST_StructuralFraming)
        seen["walls"] = Already(BuiltInCategory.OST_Walls)
        seen["floors"] = Already(BuiltInCategory.OST_Floors)
        n_found = sum(len(v.points) for v in seen.values())
        if n_found:
            say("%d structural elements are already in this model. Only what is "
                "missing will be added; nothing is duplicated." % n_found)
            if os.path.exists(PLACED_FILE):
                say("")
                say("*** Some of those came from an earlier run of this script.      ***")
                say("*** Re-extracting the drawing cannot remove them: it only       ***")
                say("*** changes what the NEXT build places. If the model still      ***")
                say("*** shows a problem you have already fixed in the extractor,    ***")
                say("*** this is why. Set REPLACE_PREVIOUS = True at the TOP of this  ***")
                say("*** file and run again. Nothing below will change until you do. ***")
                say("*** again to take the previous build out first.                 ***")
                say("")
    # The storey each kind of element occupies, for the duplicate check: what this
    # floor holds up sits between the level below and this one; a floating column
    # stands between this level and the one above.
    _lo = neighbour_level(level, -1)
    _hi = neighbour_level(level, +1)
    lo_z = _lo.Elevation if _lo is not None else level.Elevation - MM(STOREY_HEIGHT_MM)
    hi_z = _hi.Elevation if _hi is not None else level.Elevation + MM(STOREY_HEIGHT_MM)
    band_below = (lo_z, level.Elevation)
    band_above = (level.Elevation, hi_z)

    no_depth = set()
    odd_walls = 0
    curved = 0
    shaped = 0              # beams built to their drawn section (not a rectangle)
    skipped = {"columns": 0, "beams": 0, "folds": 0, "walls": 0, "floors": 0}

    # ---- columns ----------------------------------------------------
    try:
        shift = check_alignment(data, level, ox, oy)
        if shift and ALIGN_TO_FLOOR_BELOW:
            ox, oy = ox - shift[0], oy - shift[1]
    except Exception as exc:
        say("Alignment check skipped (%s)." % exc)
    spans = {"supporting": 0, "floating": 0}
    # An FC on this floor's plan that already has a column under it (the floor
    # below's FC, Level 1 to Level 2) is that column carrying on, not a new
    # floating one: Wing B 2nd floor FC1-FC6 stood up 3000 mm above Level 2.
    below_cols = columns_between(band_below) if FC_CARRY_ON else []
    continuing = set()
    for c in data.get("columns", []):
        if is_floating(c):
            x, y = MM(c["x"] - ox), MM(c["y"] - oy)
            if any(abs(x - bx) + abs(y - by) <= MM(SAME_PLACE) for bx, by in below_cols):
                continuing.add(id(c))
    if continuing:
        say("Floating columns: %d FCs on this plan stand on a column already in the model "
            "below '%s', so they are built from the level below up to it, like any column."
            % (len(continuing), level.Name))

    def floating(c):
        return is_floating(c) and id(c) not in continuing

    for c in data.get("columns", []):
        col_note = c.get("provisional") or ""
        if strict and neighbour_level(level, +1 if floating(c) else -1) is None:
            # its height would be STOREY_HEIGHT_MM, which nothing in the drawings says
            why = "the model has no level %s '%s' for it to %s" % (
                "above" if floating(c) else "below", level.Name, "rise to" if floating(c) else "rise from")
            if not prov_ok:
                held.append("column %s: %s" % (c.get("mark") or "?", why))
                continue
            col_note = "; ".join(x for x in (col_note, "%s - built %.0f mm %s it" % (
                why, STOREY_HEIGHT_MM, "above" if floating(c) else "below")) if x)
        # a column tagged "1" in the CAD is "C-1" in the schedule
        w, d = sized(c.get("mark"), sizes, c["width"], c["depth"],
                     prefixes=("FC",) if c.get("kind") == "column_float" else ("C", "FC"))
        # The schedule writes the narrow side first; the drawing's outline says
        # which way the column faces. Keep the drawn orientation, take the size.
        cw, cd = float(c["width"] or 0), float(c["depth"] or 0)
        if abs(w - d) > 1.0 and abs(cw - cd) > 1.0 and (w > d) != (cw > cd):
            w, d = d, w
        sym = None
        if c.get("diameter") and abs(w - d) <= 1.0:
            # drawn as a circle: a round column of that diameter
            sym = round_types.get(w)
            if sym is None:
                col_note = "; ".join(x for x in (col_note, "round in the drawing (%.0f across) - no round "
                                     "column family loaded or in Revit's library, so built square; load "
                                     "one (Insert > Load Family, Concrete-Round-Column) and build again" % w) if x)
        if sym is None:
            sym = col_types.get(w, d)
        if sym is None:
            skipped["columns"] += 1
            continue
        activate(sym)
        try:
            pt = XYZ(MM(c["x"] - ox), MM(c["y"] - oy), level.Elevation)
            if "columns" in seen and seen["columns"].has(c.get("mark"), pt.X, pt.Y,
                                                             band_above if floating(c) else band_below):
                already["columns"] += 1
                continue
            inst = doc.Create.NewFamilyInstance(
                pt, sym, level, Structure.StructuralType.Column)
            try:
                set_column_span(inst, level, floating(c))
                spans["floating" if floating(c) else "supporting"] += 1
            except Exception as exc:
                say("column %s: could not set its height (%s)" % (c.get("mark") or "?", exc))
            if c.get("top_mm") and not floating(c):
                # ends under a lowered beam (building section: B19 2270 below the terrace)
                try:
                    _setp(inst, BuiltInParameter.FAMILY_TOP_LEVEL_OFFSET_PARAM, MM(float(c["top_mm"])))
                except Exception as exc:
                    say("column %s: could not lower its top (%s)" % (c.get("mark") or "?", exc))
            if c.get("base_mm") and not floating(c) and neighbour_level(level, -1) is not None:
                # starts above the level below (a wall elevation: it stands on a raft higher up)
                try:
                    _setp(inst, BuiltInParameter.FAMILY_BASE_LEVEL_OFFSET_PARAM, MM(float(c["base_mm"])))
                except Exception as exc:
                    say("column %s: could not raise its base (%s)" % (c.get("mark") or "?", exc))
            rot = float(c.get("rotation") or 0.0)
            turn = column_rotation_deg(rot)
            axis = Line.CreateBound(pt, XYZ(pt.X, pt.Y, pt.Z + 1.0))
            if abs(turn) > 1e-6:
                ElementTransformUtils.RotateElement(
                    doc, inst.Id, axis, turn * 3.14159265358979 / 180.0)
            if not calibrated[0] and abs(d - w) > 100.0:
                calibrated[0] = True
                if not calibrate_rotation(inst, w, d, rot):
                    # the offset was wrong: put this one right, the rest follow
                    ElementTransformUtils.RotateElement(
                        doc, inst.Id, axis, 90.0 * 3.14159265358979 / 180.0)
            if c.get("lean") and not floating(c) and neighbour_level(level, -1) is not None:
                # leaning as a wall elevation draws it (C12 beside SW13 - SW15)
                lean_column(inst, c["lean"], c.get("mark"))
            set_mark(inst, c.get("mark"))
            if col_note:
                mark_provisional(inst, "column %s" % (c.get("mark") or "?"), col_note, pt)
            remember(inst)
            made["columns"] += 1
        except Exception as exc:
            skipped["columns"] += 1
            say("column %s failed: %s" % (c.get("mark") or "?", exc))

    # ---- beams and girders -----------------------------------------
    from_drawing = 0
    for b in data.get("beams", []):
        # A beam's depth is never in the plan geometry. It comes from a schedule:
        # the one printed on the drawing, which the extractor now reads into
        # model.json, or the consultant's workbook via sizes.csv, which wins
        # because it is the later and more authoritative source.
        drawn_depth = b.get("depth")
        w, d = sized(b.get("mark"), sizes, b.get("width") or (None if strict else 230.0),
                     drawn_depth or (None if strict else DEFAULT_BEAM_DEPTH),
                     prefixes=("B", "GB") if b.get("kind") == "beam" else ("GB", "B"))
        if strict and not (w and d):
            held.append("beam %s: no %s given" % (b.get("mark") or "(untagged)",
                                                  "width or depth" if not (w or d) else "width" if not w else "depth"))
            continue
        sym = beam_types.get(w, d)
        scheduled = b.get("mark") and norm(b["mark"]) in sizes
        if drawn_depth and not scheduled:
            from_drawing += 1
        elif not drawn_depth and not scheduled:
            no_depth.add(str(b.get("mark") or "(untagged)"))
        if sym is None:
            skipped["beams"] += 1
            continue
        activate(sym)
        try:
            # a beam the schedule's remarks drop ("250MM SUNK") or raise ("BT@ +0.200M");
            # the workbook's Top of Beam Offset wins where it gives one
            top = from_book(extra, b.get("mark"), "Beams", "top_mm")
            if top is None:
                top = b.get("top_mm") or 0.0
            bz = level.Elevation + MM(top)
            # a beam sloping with a slab (building section): its end at its own height
            bz2 = bz if b.get("top_end_mm") is None else level.Elevation + MM(b["top_end_mm"])
            p1 = XYZ(MM(b["start"][0] - ox), MM(b["start"][1] - oy), bz)
            p2 = XYZ(MM(b["end"][0] - ox), MM(b["end"][1] - oy), bz2)
            if p1.DistanceTo(p2) < MM(1.0):
                skipped["beams"] += 1
                continue
            mx, my = (p1.X + p2.X) / 2.0, (p1.Y + p2.Y) / 2.0
            if "beams" in seen and seen["beams"].has(b.get("mark"), mx, my, band_below):
                already["beams"] += 1
                continue
            if b.get("mid"):
                # a curved beam: the arc through its ends and a point midway along
                pm = XYZ(MM(b["mid"][0] - ox), MM(b["mid"][1] - oy), bz)
                curve = Arc.Create(p1, p2, pm)
                curved += 1
            else:
                curve = Line.CreateBound(p1, p2)
            inst, note = None, b.get("provisional") or ""
            sec = b.get("section") or {}
            if sec.get("profile") and not b.get("mid"):
                # built to its drawn section; if Revit refuses the solid, the usual beam, marked
                try:
                    inst = shaped_beam(sec["profile"], p1, p2, b["start"], b["end"])
                    shaped += 1
                except Exception as exc:
                    say("beam %s: section %s could not be built as its shape (%s) - built as a "
                        "rectangle" % (b.get("mark") or "?", sec.get("name") or "", exc))
                    note = "; ".join(x for x in (note, "drawn section %s not built as its shape: a %s x %s "
                                                 "rectangle instead" % (sec.get("name") or "", w, d)) if x)
            if inst is None:
                inst = doc.Create.NewFamilyInstance(
                    curve, sym, level, Structure.StructuralType.Beam)
            set_mark(inst, b.get("mark"))
            if note:
                mark_provisional(inst, "beam %s" % (b.get("mark") or "(untagged)"), note,
                                 XYZ(mx, my, bz))
            remember(inst)
            made["beams"] += 1
        except Exception as exc:
            skipped["beams"] += 1
            say("beam %s failed: %s" % (b.get("mark") or "?", exc))

    # ---- slab folds ---------------------------------------------------
    # The downturn where a slab steps down to a sunk slab: the paired red lines
    # on the slab layer. Built as framing from the top of the higher slab down to
    # the underside of the lower one; the extractor works out both from the slabs
    # either side. One with no depth had no slab on one side and is left out.
    for f in data.get("folds", []):
        if not f.get("width") or not f.get("depth"):
            continue
        sym = fold_types.get(f["width"], f["depth"])
        if sym is None:
            skipped["folds"] += 1
            continue
        activate(sym)
        try:
            z = level.Elevation + MM(f.get("top_mm") or 0.0)
            p1 = XYZ(MM(f["start"][0] - ox), MM(f["start"][1] - oy), z)
            p2 = XYZ(MM(f["end"][0] - ox), MM(f["end"][1] - oy), z)
            if p1.DistanceTo(p2) < MM(1.0):
                skipped["folds"] += 1
                continue
            mx, my = (p1.X + p2.X) / 2.0, (p1.Y + p2.Y) / 2.0
            # by position only: every fold would carry the same mark
            if "beams" in seen and (seen["beams"].has(None, mx, my, band_below)
                                    or seen["beams"].on_line("SLAB FOLD", mx, my, band_below)):
                already["folds"] += 1
                continue
            inst = doc.Create.NewFamilyInstance(
                Line.CreateBound(p1, p2), sym, level, Structure.StructuralType.Beam)
            set_mark(inst, "SLAB FOLD")
            if f.get("provisional"):
                mark_provisional(inst, "hidden beam", f["provisional"], XYZ(mx, my, z))
            remember(inst)
            made["folds"] += 1
        except Exception as exc:
            skipped["folds"] += 1
            say("slab fold failed: %s" % exc)

    # ---- walls -------------------------------------------------------
    wall_type = next(iter(FilteredElementCollector(doc)
                          .OfCategory(BuiltInCategory.OST_Walls)
                          .WhereElementIsElementType().ToElements()), None)
    # Walls are RC walls holding this floor up, like the columns: they rise from
    # the level below to this one, so the beams that land on them meet them.
    below = neighbour_level(level, -1)
    if below is not None:
        wall_base, wall_off = below, 0.0
        wall_h = level.Elevation - below.Elevation
    else:
        wall_base, wall_off = level, -MM(STOREY_HEIGHT_MM)
        wall_h = MM(STOREY_HEIGHT_MM)
    wall_types = {}
    split_walls = 0
    for w in data.get("walls", []):
        wall_note = w.get("provisional") or ""
        if strict and below is None:
            why = "the model has no level below '%s' for it to rise from" % level.Name
            if not prov_ok:
                held.append("wall %s: %s" % (w.get("mark") or "", why))
                continue
            wall_note = "; ".join(x for x in (wall_note, "%s - built %.0f mm tall" % (why, STOREY_HEIGHT_MM)) if x)
        if strict and w.get("start") and w.get("end") and not w.get("thickness"):
            held.append("wall %s: no thickness given" % (w.get("mark") or ""))
            continue
        if w.get("profile") and below is not None and wall_type is not None:
            # the shape a wall elevation draws (an arch, a lean): one profile wall
            try:
                wt = wall_type_for(w["profile"]["thickness"], wall_types)
                pts = [XYZ(MM(x - ox), MM(y - oy), below.Elevation + MM(z)) for x, y, z in profile_points(w["profile"])]
                wall = profile_wall(pts, wt, below)
                set_mark(wall, w.get("mark"))
                if wall_note:
                    mark_provisional(wall, "wall %s" % (w.get("mark") or ""), wall_note, pts[0])
                remember(wall)
                made["walls"] += 1
                continue
            except Exception as exc:
                say("wall %s: its drawn shape could not be built (%s) - built straight" % (w.get("mark") or "?", exc))
        if w.get("start") and w.get("end"):
            runs = [(w["start"], w["end"], w.get("thickness") or 230.0)]
        elif w.get("outline"):
            # an L, U or lift shaft drawn as one outline: one wall per straight run
            runs = wall_runs(w["outline"])
            if runs:
                split_walls += 1
            else:
                odd_walls += 1
        else:
            runs = []
        if not runs:
            skipped["walls"] += 1
            continue
        for start, end, thick in runs:
            try:
                wt = wall_type_for(thick, wall_types) if wall_type is not None else None
                if wt is None:
                    skipped["walls"] += 1
                    continue
                p1 = XYZ(MM(start[0] - ox), MM(start[1] - oy), level.Elevation)
                p2 = XYZ(MM(end[0] - ox), MM(end[1] - oy), level.Elevation)
                if p1.DistanceTo(p2) < MM(1.0):
                    skipped["walls"] += 1
                    continue
                mx, my = (p1.X + p2.X) / 2.0, (p1.Y + p2.Y) / 2.0
                if "walls" in seen and seen["walls"].has(None, mx, my, band_below):
                    already["walls"] += 1
                    continue
                # a wall elevation's base / top off the levels (only with a level below to measure from)
                lift = MM(float(w.get("base_mm") or 0.0)) if below is not None else 0.0
                drop = MM(float(w.get("top_mm") or 0.0)) if below is not None else 0.0
                ln = w.get("lean") if below is not None else None
                if ln:
                    # leaning as a wall elevation draws it (SW13): its foot where the lean puts it
                    p1 = XYZ(p1.X + MM(ln["base"][0]), p1.Y + MM(ln["base"][1]), p1.Z)
                    p2 = XYZ(p2.X + MM(ln["base"][0]), p2.Y + MM(ln["base"][1]), p2.Z)
                wall = Wall.Create(doc, Line.CreateBound(p1, p2), wt.Id,
                                   wall_base.Id, wall_h - lift + drop, wall_off + lift, False, True)
                if ln:
                    lean_wall(wall, ln, (wall_h - lift + drop) / MM(1.0), w.get("mark"))
                set_mark(wall, w.get("mark"))
                if wall_note:
                    mark_provisional(wall, "wall %s" % (w.get("mark") or ""), wall_note, XYZ(mx, my, level.Elevation))
                remember(wall)
                made["walls"] += 1
            except Exception as exc:
                skipped["walls"] += 1
                say("wall %s failed: %s" % (w.get("mark") or "?", exc))

    # ---- floors ------------------------------------------------------
    # One per slab panel, its top at the level like the beams' tops, so slab and
    # beams read as one flat top. The extractor gives each panel its mark and its
    # thickness from the slab schedule; a sunk panel drops by its sunk depth.
    floor_types = {}
    sloped_floors = [0]         # floors lifted to a building section's slope
    slab_sizes = {}
    unsized_slabs = set()
    sunk_done, sunk_failed = [], []
    for s in data.get("slabs", []):
        pts = slab_loop_points(s.get("outline"))
        # The workbook's Slabs sheet wins over the drawn slab schedule for the
        # thickness. Its sunk depth is per mark, but the drawing says panel by panel
        # which ones are sunk (S7 is flat in one room and sunk in the next), so it
        # only corrects the depth of a panel the drawing already shows as sunk.
        sch_th = from_book(extra, s.get("mark"), "Slabs", "depth_mm")
        sch_sunk = from_book(extra, s.get("mark"), "Slabs", "sunk_mm")
        if sch_th or (sch_sunk and s.get("sunk_mm")):
            s = dict(s)
            if sch_th:
                s["thickness"] = sch_th
            if sch_sunk and s.get("sunk_mm"):
                s["sunk_mm"] = sch_sunk
        if strict and not s.get("thickness"):
            held.append("slab %s: no thickness given" % (s.get("mark") or "(unmarked)"))
            continue
        th = s.get("thickness") or DEFAULT_SLAB_MM
        if not s.get("thickness"):
            unsized_slabs.add(str(s.get("mark") or "(unmarked)"))
        ft = floor_type_for(th, floor_types)
        if not pts or ft is None:
            skipped["floors"] += 1
            continue
        try:
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            cx = MM((min(xs) + max(xs)) / 2.0 - ox)
            cy = MM((min(ys) + max(ys)) / 2.0 - oy)
            # By position only: S3 is the mark of fifteen panels on one floor.
            if "floors" in seen and seen["floors"].has(None, cx, cy, band_below):
                already["floors"] += 1
                continue
            # The outline, then one inner loop per cut-out inside it.
            loops = []
            for ring in [pts] + [slab_loop_points(h) for h in s.get("holes") or []]:
                if not ring:
                    continue
                loop = CurveLoop()
                xyz = [XYZ(MM(p[0] - ox), MM(p[1] - oy), level.Elevation) for p in ring]
                for i in range(len(xyz)):
                    loop.Append(Line.CreateBound(xyz[i], xyz[(i + 1) % len(xyz)]))
                loops.append(loop)
            try:                                           # Revit 2022 and later
                fl = Floor.Create(doc, loops, ft.Id, level.Id, True, None, 0.0)
            except Exception:
                fl = Floor.Create(doc, loops, ft.Id, level.Id)
            _setp(fl, BuiltInParameter.FLOOR_PARAM_IS_STRUCTURAL, 1)
            if s.get("slope"):
                # sloping (building section): its corners lifted to the section's heights;
                # if Revit refuses, it stays flat at the level, marked
                try:
                    slope_floor(fl, s["slope"], ox, oy)
                    sloped_floors[0] += 1
                except Exception as exc:
                    say("floor %s: could not be sloped (%s) - left flat" % (s.get("mark") or "?", exc))
                    s = dict(s, provisional="; ".join(x for x in (s.get("provisional"), "sloping slab (building "
                                                                  "section) built flat: Revit refused the slope") if x))
            if s.get("sunk_mm"):
                if set_floor_offset(fl, -MM(s["sunk_mm"])):
                    sunk_done.append("%s -%d" % (s.get("mark") or "?", round(s["sunk_mm"])))
                else:
                    sunk_failed.append(str(s.get("mark") or "?"))
            set_mark(fl, s.get("mark"))
            set_comment(fl, "%s | %d mm%s | from CAD" % (
                s.get("mark") or "(no mark)", round(th),
                (" | sunk %d mm" % round(s["sunk_mm"])) if s.get("sunk_mm") else ""))
            if s.get("provisional"):
                mark_provisional(fl, "slab %s" % (s.get("mark") or "(no mark)"), s["provisional"],
                                 XYZ(cx, cy, level.Elevation))
            remember(fl)
            made["floors"] += 1
            key = int(round(th))
            slab_sizes[key] = slab_sizes.get(key, 0) + 1
        except Exception as exc:
            skipped["floors"] += 1
            say("floor %s failed: %s" % (s.get("mark") or "?", exc))

    make_slab_schedule()
    if sloped_floors[0]:
        say("         %d floor(s) sloping, as the building section draws them." % sloped_floors[0])

    try:
        doc.Regenerate()
        fix_joins_and_check(list(PLACED))
    except Exception as exc:
        say("Join / visibility check skipped (%s)." % exc)
    if BEAM_SIZE_FILTERS:
        try:
            make_beam_size_filters()
        except Exception as exc:
            say("Beam size filters skipped (%s)." % exc)
    if COLUMN_SIZE_FILTERS:
        try:
            make_column_size_filters()
        except Exception as exc:
            say("Column size filters skipped (%s)." % exc)
    if SUNK_SLAB_FILTERS:
        try:
            make_sunk_slab_filters()
        except Exception as exc:
            say("Sunk slab filters skipped (%s)." % exc)
    try:
        highlight_provisional(level)
    except Exception as exc:
        say("Marking the elements that need a decision skipped (%s). Their Comments still say so." % exc)

    say("")
    say("PLACED   " + (", ".join("%s %s" % (v, k) for k, v in made.items() if v) or "nothing"))
    if sunk_done:
        say("SUNK     %d slabs dropped below the level and checked in Revit: %s"
            % (len(sunk_done), ", ".join(sunk_done)))
    if sunk_failed:
        say("NOT SUNK %d slabs should be sunk but Revit would not take the offset: %s. "
            "Set 'Height Offset From Level' on them by hand."
            % (len(sunk_failed), ", ".join(sunk_failed)))
    if curved:
        say("         %d of the beams are curved, built as arcs." % curved)
    if shaped:
        say("         %d of the beams are built to their drawn section (not a rectangle)." % shaped)
    if any(already.values()):
        say("ALREADY  " + ", ".join("%s %s" % (v, k) for k, v in already.items() if v)
            + "  (left alone, not duplicated)")
    if any(skipped.values()):
        say("SKIPPED  " + ", ".join("%s %s" % (v, k) for k, v in skipped.items() if v))
    if held:
        say("NOT BUILT %d - each needs a decision; nothing was assumed in its place:" % len(held))
        for h in held[:60]:
            say("         " + h)
        if len(held) > 60:
            say("         ... and %d more" % (len(held) - 60))
    say("")
    if made["columns"]:
        say("Columns were turned to the angle in the drawing less %.0f degrees, which is "
            "where the family starts. A column at 92 degrees in the plan is therefore a "
            "%.0f degree instance in Revit."
            % (-ROT_OFFSET[0], column_rotation_deg(92.0)))

    for cache, what in ((col_types, "Column"), (beam_types, "Beam"), (fold_types, "Slab fold")):
        if cache.made:
            sizes_made = sorted(cache.made.keys())
            say("%s types created (%d): %s" % (
                what, len(sizes_made),
                ", ".join("%dx%d" % s for s in sizes_made[:12])
                + (" ..." if len(sizes_made) > 12 else "")))
            if cache.param_pair:
                say("   sized through the family parameters '%s' and '%s'"
                    % cache.param_pair)
    if from_drawing:
        say("%d beams took their depth from the schedule printed on the drawing, as read "
            "into model.json by the extractor." % from_drawing)
    if no_depth:
        say("%d beams have no scheduled depth and were placed at %.0f mm. A beam depth is "
            "not in the plan geometry and can only come from a schedule. Check "
            "report.html for the marks the drawing's own schedule did not cover, or fill "
            "the workbook and run make_sizes_csv.py."
            % (len(no_depth), DEFAULT_BEAM_DEPTH))
    if spans["supporting"] or spans["floating"]:
        below = neighbour_level(level, -1)
        above = neighbour_level(level, +1)
        say("Column heights: %d columns rise from %s up to '%s', where the beams frame "
            "into them. %d floating columns (FC) start on their girder at '%s' and rise "
            "to %s."
            % (spans["supporting"],
               ("'%s'" % below.Name) if below else "%.0f mm below" % STOREY_HEIGHT_MM,
               level.Name, spans["floating"], level.Name,
               ("'%s'" % above.Name) if above else "%.0f mm above" % STOREY_HEIGHT_MM))
    if split_walls:
        say("%d walls drawn as one outline (an L, U or lift shaft) were split into "
            "straight runs, which Revit joins at the corners." % split_walls)
    if odd_walls:
        say("%d walls are drawn as a shape the script could not split into straight "
            "runs. Model those by hand." % odd_walls)
    if slab_sizes:
        say("Slabs by thickness: " + ", ".join(
            "%d at %d mm" % (n, t) for t, n in sorted(slab_sizes.items()))
            + ". Each is a floor type named 'RCC Slab <thickness> mm'.")
    if unsized_slabs:
        say("Slabs %s are not in the slab schedule and were built at %.0f mm. Check "
            "their thickness." % (", ".join(sorted(unsized_slabs)), DEFAULT_SLAB_MM))
    if not data.get("slabs"):
        say("No floors were placed: model.json has no slab panels. Extract the drawing "
            "again with the current extractor, which finds them between the beams.")


# ---------------------------------------------------------------- run
LOG_FILE = os.path.join(os.path.dirname(MODEL_JSON) or ".", "build_log.txt")


def write_log():
    """Always leave the result on disk. Each host shows printed output somewhere
    different, and in Rhino it is easy to be looking at the wrong panel."""
    try:
        with open(LOG_FILE, "w") as f:
            f.write("\n".join(log))
    except Exception:
        pass


def run_build():
    t = Transaction(doc, "Build structure from CAD")
    t.Start()
    try:
        build()
        st = t.Commit()
    except Exception:
        t.RollBack()
        raise
    if str(st) != "Committed":
        # Revit refused the transaction (an error nobody resolved): nothing above stands
        say("STOP: Revit rolled the whole build back (%s) - nothing was placed. Build again with "
            "Revit open, so its error message can be seen." % st)
        del PLACED[:], PLACED_UID[:], REMOVED[:]
        return
    # only after the transaction has committed: if it rolled back, nothing was
    # placed and the old record is still the true one
    if CLEAR_ALL_STRUCTURE:
        say("Set CLEAR_ALL_STRUCTURE back to False now, or the next run will wipe "
            "the model again.")
    if PLACED or REMOVED:
        total = write_placed()
        say("placed.json now records %d elements in this model (%d added, %d removed "
            "this run), so they can be taken out later with REPLACE_PREVIOUS = True."
            % (total, len(PLACED), len(REMOVED)))


def finish():
    write_log()
    out = "\n".join(log)
    try:
        print(out)
    except Exception:
        pass
    return out


try:
    say("Host: %s" % HOST)
    try:
        say("Document: %s" % doc.Title)
    except Exception:
        say("Document: (could not read its title)")
    if HOST == "Dynamo":
        TransactionManager.Instance.EnsureInTransaction(doc)
        build()
        TransactionManager.Instance.TransactionTaskDone()
    else:
        try:
            run_build()
        except Exception as exc:
            # Revit only accepts changes from its own API context. Rhino's script
            # editor is usually inside it, but not always; a Grasshopper Python
            # component always is, so say so rather than fail obscurely.
            if "API context" in str(exc) or "read-only" in str(exc).lower():
                say("Revit would not accept a change from here: this thread is outside "
                    "its API context.")
                say("Run the same script from a Grasshopper Python 3 component instead "
                    "(Rhino.Inside > Grasshopper), which always runs inside it.")
            else:
                raise
except Exception:
    say("FAILED:")
    say(traceback.format_exc())

OUT = finish()
