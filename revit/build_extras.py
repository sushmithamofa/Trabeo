r"""Build footings, their steps and rafts - and set up levels - from the export (stage 8).

This runs INSIDE Revit, in the same hosts as build_model.py (Rhino.Inside.Revit,
Dynamo CPython3, pyRevit). It is a separate script on purpose: build_model.py,
which builds columns, beams, slabs and walls and is proven, is not touched.

Input: the foundations.json that `python -m building.export RUN` writes into the
folder of the lowest plan of a drawing set (IN[0], or `extras_json`, or a file
picker). levels.json, in the export folder above it, is read too.

What it does
  LEVELS     every level the drawings give an elevation for is created if the
             model has no level of that name; an existing level at a different
             elevation is REPORTED, never moved (MOVE_LEVELS = True moves it).
  FOOTINGS   isolated foundations, one type per size ("Footing 2400x2700x750"),
             duplicated from a loaded rectangular footing family
             (M_Footing-Rectangular or similar). Each one's bottom is its own
             founding level (bottom_mm), read from the drawings or decided in the
             review - never one depth for the whole building. It is measured from
             the level the footings hang from: that level's elevation in the
             drawings (level_elevation_mm) against where it stands in the model.
  STEPS      the upper step of a stepped footing, as a second footing on top.
  RAFTS      structural floors of the raft's thickness, bottom at its founding level.

Not built (reported): a footing or raft with no founding level, or when the level
it hangs from has no elevation; PCC beds; a stair whose going or width is not given;
footings whose size or depth is in conflict or missing (export_report.txt).

Running it again first takes out what the previous run of THIS script placed
(placed_extras.json beside the input), so it never duplicates. One undo reverts it.
Everything is in mm in the files; Revit works in feet, so all lengths go through MM().
"""

import io
import json
import os
import traceback

MOVE_LEVELS = False             # True: move an existing level to the drawings' elevation
REPLACE_PREVIOUS = True
BUILD_FOOTINGS = True
BUILD_RAFTS = True
BUILD_LEVELS = True
BUILD_PEDESTALS = True          # footing top up to the plan's level, as a concrete column
BUILD_STAIRS = True             # every revit_build\<plan>\stairs.json, in its own edit session
FOUNDATION_SIZE_FILTERS = True  # a view filter per footing size, each its own colour (as the beams)


# ---------------------------------------------------------------- the plan (pure)
# Everything below this line up to "the Revit side" is plain Python with no Revit
# in it, so revit\selftest_extras.py can check it outside Revit.

def bottom_in_model(item, level_mm, drawn_level_mm):
    """Where a footing's or raft's bottom goes in the model (mm), or None.

    bottom_mm is its founding level against the drawings' datum, read from the
    drawings or decided in the review. The level it hangs from stands at level_mm in
    the model and at drawn_level_mm in the drawings, so a model on another datum
    (a site datum, sea level) still gets the footing at the right depth below it.
    Without both, nothing is assumed: None."""
    b = item.get("bottom_mm")
    if b is None or drawn_level_mm is None:
        return None
    return level_mm + (float(b) - float(drawn_level_mm))


def footing_boxes(items, level_mm, drawn_level_mm):
    """Each footing and step as a box: bottom and top elevation in mm.

    A footing's bottom is its own founding level (bottom_in_model); one without
    any is left out. A step stands on the footing it belongs to (same mark, same
    centre).
    """
    out = []
    tops = {}
    for it in items:
        if it.get("type") != "footing":
            continue
        bottom = bottom_in_model(it, level_mm, drawn_level_mm)
        if bottom is None:
            continue
        top = bottom + float(it["thickness"])
        tops[(it.get("mark"), round(it["x"]), round(it["y"]))] = top
        out.append({"kind": "footing", "mark": it.get("mark") or "", "x": it["x"], "y": it["y"],
                    "size_x": float(it["size_x"]), "size_y": float(it["size_y"]),
                    "bottom": bottom, "top": top, "thickness": float(it["thickness"]),
                    # degrees: a foundation sheet drawn turned against the plan (0 = as drawn)
                    "rotation": float(it.get("rotation") or 0.0)})
    for it in items:
        if it.get("type") != "step":
            continue
        base = tops.get((it.get("on") or it.get("mark"), round(it["x"]), round(it["y"])))
        if base is None:
            continue
        out.append({"kind": "step", "mark": it.get("mark") or "", "x": it["x"], "y": it["y"],
                    "size_x": float(it["size_x"]), "size_y": float(it["size_y"]),
                    "bottom": base, "top": base + float(it["thickness"]), "thickness": float(it["thickness"]),
                    "rotation": float(it.get("rotation") or 0.0)})
    return out


def raft_slabs(items, level_mm, drawn_level_mm):
    """Each raft: its outline, thickness and top elevation (bottom at its founding level)."""
    out = []
    for it in items:
        if it.get("type") != "raft":
            continue
        bottom = bottom_in_model(it, level_mm, drawn_level_mm)
        if bottom is None:
            continue
        t = float(it["thickness"])
        out.append({"mark": it.get("mark") or "RAFT", "outline": it["outline"], "thickness": t,
                    "top": bottom + t})
    return out


def plan_levels(entries, existing, move=False):
    """What to do with each level of levels.json.

    entries:  [{"revit_name", "elevation_mm", "status"}]
    existing: {name: elevation_mm} of the levels in the model
    Returns [(action, name, detail)] with action one of
      create / move / same / differs / skip
    A level is never created where another level already stands at that elevation.
    """
    out = []
    taken = dict(existing)
    for e in entries:
        name = (e.get("revit_name") or e.get("level") or "").strip()
        z = e.get("elevation_mm")
        if not name:
            continue
        if e.get("status") not in ("read", "override") or z is None:
            out.append(("skip", name, "no elevation in the drawings"))
            continue
        z = float(z)
        if name in taken:
            if abs(taken[name] - z) <= 1.0:
                out.append(("same", name, z))
            elif move:
                out.append(("move", name, (taken[name], z)))
                taken[name] = z
            else:
                out.append(("differs", name, (taken[name], z)))
            continue
        clash = [n for n, v in taken.items() if abs(v - z) <= 1.0]
        if clash:
            out.append(("skip", name, "level '%s' already stands at %+.0f" % (clash[0], z)))
            continue
        out.append(("create", name, z))
        taken[name] = z
    return out


def pedestal_box(item, footings, level_mm):
    """A pedestal from the top of the footing it stands on up to the level, or None."""
    on = [f for f in footings if f["kind"] == "footing" and f["mark"] == item.get("on")
          and abs(f["x"] - item["on_xy"][0]) < 1.0 and abs(f["y"] - item["on_xy"][1]) < 1.0]
    if not on:
        return None
    steps = [f for f in footings if f["kind"] == "step" and abs(f["x"] - on[0]["x"]) < 1.0
             and abs(f["y"] - on[0]["y"]) < 1.0]
    bottom = max([on[0]["top"]] + [s["top"] for s in steps])
    if bottom >= level_mm:
        return None
    return {"mark": item.get("mark") or "", "x": item["x"], "y": item["y"], "size_x": float(item["size_x"]),
            "size_y": float(item["size_y"]), "bottom": bottom, "top": level_mm}


def stair_runs(stair, base_mm, top_mm):
    """A stair's flights as Revit runs: chained end to start, rising from base to top.

    Each flight's line goes from its first tread line to its last, extended back by
    one going so it covers every tread. The flights are taken in the order drawn
    and each turned so it starts at the end nearest the previous flight's end (the
    landing of a dog-leg). Every flight has one riser more than tread lines; the
    height is shared equally among all risers.
    Returns (runs, riser_mm, note).
    """
    import math
    runs = [dict(r) for r in stair.get("runs") or []]
    if not runs or top_mm <= base_mm:
        return [], None, "no flights, or no height to climb"
    for r in runs:
        r["start"], r["end"] = list(r["start"]), list(r["end"])
    def dist(p, q):
        return math.hypot(p[0] - q[0], p[1] - q[1])
    for prev, r in zip(runs, runs[1:]):
        if dist(prev["end"], r["end"]) < dist(prev["end"], r["start"]):
            r["start"], r["end"] = r["end"], r["start"]
    for r in runs:
        if not (r.get("going") or stair.get("going")) or not (r.get("width") or stair.get("width")):
            return [], None, "its going or width is not given - decide it in the review"
    risers = sum(int(r["treads"]) + 1 for r in runs)
    h = (top_mm - base_mm) / float(risers)
    note = ""
    if stair.get("riser") and abs(float(stair["riser"]) - h) > 10.0:
        note = "the notes give a %.0f riser; the floor height gives %.0f over %d risers" % (
            float(stair["riser"]), h, risers)
    z = base_mm
    out = []
    for r in runs:
        dx, dy = r["end"][0] - r["start"][0], r["end"][1] - r["start"][1]
        L = math.hypot(dx, dy) or 1.0
        g = float(r.get("going") or stair.get("going"))
        start = [r["start"][0] - dx / L * g, r["start"][1] - dy / L * g]
        n = int(r["treads"]) + 1
        out.append({"start": start, "end": r["end"], "width": float(r.get("width") or stair.get("width")),
                    "base": z, "top": z + n * h, "risers": n})
        z += n * h
    return out, h, note


def type_name(kind, size_x, size_y, thickness):
    a, b = sorted((size_x, size_y))
    return "%s %dx%dx%d" % ("Footing" if kind == "footing" else "Footing step",
                            round(a), round(b), round(thickness))


# ---------------------------------------------------------------- the Revit side
log = []


def say(s):
    log.append(str(s))


def _revit():
    """Everything that needs Revit. Not run by the tests."""
    import clr
    clr.AddReference("RevitAPI")
    g = globals()
    from Autodesk.Revit.DB import (BuiltInCategory, BuiltInParameter, CurveLoop, ElementId,
                                   FamilySymbol, FilteredElementCollector, Floor, Level, Line,
                                   Structure, Transaction, XYZ)
    for k, v in list(locals().items()):
        g[k] = v
    try:
        from Autodesk.Revit.DB import UnitTypeId, UnitUtils

        def MM(v):
            return UnitUtils.ConvertToInternalUnits(float(v), UnitTypeId.Millimeters)

        def to_mm(ft):
            return UnitUtils.ConvertFromInternalUnits(float(ft), UnitTypeId.Millimeters)
    except ImportError:
        def MM(v):
            return float(v) / 304.8

        def to_mm(ft):
            return float(ft) * 304.8
    g["MM"], g["to_mm"] = MM, to_mm


def _find_doc():
    """The document, as build_model.py finds it in each host."""
    g = globals()
    p = g.get("__revit__")
    if p is not None:
        try:
            return p.ActiveUIDocument.Document, "pyRevit"
        except Exception:
            pass
    try:
        import System
        for asm in System.AppDomain.CurrentDomain.GetAssemblies():
            try:
                if asm.GetName().Name in ("RhinoInside.Revit", "RhinoInside.Revit.AddIn"):
                    t = asm.GetType("RhinoInside.Revit.Revit")
                    d = t.GetProperty("ActiveDBDocument").GetValue(None) if t is not None else None
                    if d is not None and d.IsValidObject:
                        return d, "Rhino.Inside.Revit"
            except Exception:
                continue
    except Exception:
        pass
    try:
        import clr
        clr.AddReference("RevitServices")
        from RevitServices.Persistence import DocumentManager
        d = DocumentManager.Instance.CurrentDBDocument
        if d is not None and d.IsValidObject:
            return d, "Dynamo"
    except Exception:
        pass
    return None, None


def _lift_helpers():
    """Floor types, marks and offsets: the proven helpers of build_model.py, loaded
    from that file rather than copied, so a fix there is a fix here."""
    import ast
    src = os.path.join(os.path.dirname(os.path.abspath(globals().get("__file__") or ".")), "build_model.py")
    want = ("collect", "levels", "neighbour_level", "_setp", "set_floor_offset", "set_comment", "set_mark",
            "_concrete_first", "NOT_CAST_CONCRETE", "concrete_material", "_single_layer", "_is_single_layer",
            "floor_type_for", "slab_loop_points", "set_size", "base_symbol", "collect_same_family",
            "activate", "TypeCache", "SIZE_PARAMS",
            "eid", "BEAM_COLOURS", "_solid_fill_id", "_equals_rule", "_filter_views",
            "_apply_colour_filters", "footing_size_key", "make_foundation_size_filters",
            "column_size_groups", "make_column_size_filters", "_as_list")
    with io.open(src, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    nodes = [n for n in tree.body
             if (isinstance(n, (ast.FunctionDef, ast.ClassDef)) and n.name in want)
             or (isinstance(n, ast.Assign) and any(getattr(t, "id", "") in want for t in n.targets))]
    try:
        mod = ast.Module(body=nodes, type_ignores=[])
    except TypeError:
        mod = ast.Module(body=nodes)
    exec(compile(mod, src, "exec"), globals())
    missing = [w for w in want if w not in globals()]
    if missing:
        raise RuntimeError("build_model.py no longer has: " + ", ".join(missing))


def _input(i, name):
    v = globals().get(name)
    if v not in (None, ""):
        return v
    dyn = globals().get("IN")
    if dyn is not None:
        try:
            return dyn[i] or None
        except Exception:
            return None
    return None


def _pick():
    try:
        import clr
        clr.AddReference("System.Windows.Forms")
        from System.Windows.Forms import DialogResult, OpenFileDialog
        dlg = OpenFileDialog()
        dlg.Title = "Choose foundations.json from the export (python -m building.export)"
        dlg.Filter = "Foundations (foundations.json)|foundations.json|JSON files (*.json)|*.json"
        if dlg.ShowDialog() == DialogResult.OK:
            return dlg.FileName
    except Exception:
        pass
    return None


def _footing_family(doc):
    """A loaded isolated footing family with Width / Length / thickness parameters."""
    syms = list(FilteredElementCollector(doc).OfCategory(BuiltInCategory.OST_StructuralFoundation)
                .WhereElementIsElementType().ToElements())
    for s in syms:
        if not isinstance(s, FamilySymbol):
            continue
        names = set(p.Definition.Name for p in s.Parameters)
        if "Width" in names and "Length" in names and ("Foundation Thickness" in names or "Thickness" in names):
            return s
    return None


def _set_num(el, names, mm):
    for n in names:
        p = el.LookupParameter(n)
        if p is not None and not p.IsReadOnly:
            p.Set(MM(mm))
            return True
    return False


def _footing_type(doc, base, b, cache):
    name = type_name(b["kind"], b["size_x"], b["size_y"], b["thickness"])
    if name in cache:
        return cache[name]
    sym = None
    for s in base.Family.GetFamilySymbolIds():
        t = doc.GetElement(s)
        if t.Name == name:
            sym = t
            break
    if sym is None:
        sym = base.Duplicate(name)
    lo, hi = sorted((b["size_x"], b["size_y"]))
    ok = _set_num(sym, ("Width",), lo) and _set_num(sym, ("Length",), hi) and \
        _set_num(sym, ("Foundation Thickness", "Thickness"), b["thickness"])
    if not ok:
        say("WARNING  type '%s': could not set its width, length or thickness" % name)
    if not sym.IsActive:
        sym.Activate()
        doc.Regenerate()
    cache[name] = sym
    return sym


def _write(path, text):
    """Text to a file, in IronPython 2.7 (bytes str) as in Python 3."""
    if not isinstance(text, type(u"")):
        text = text.decode("utf-8")
    with io.open(path, "w", encoding="utf-8") as f:
        f.write(text)


def raft_type_for(thickness_mm, cache):
    """A raft is a foundation slab (Structural Foundations), not a floor: a foundation slab
    type exactly this thick, one concrete layer, named "RCC Raft 250 mm" - reused if in the
    model, else made from a foundation slab type there. A model with no foundation slab type
    at all gets the structural floor type (floor_type_for), said once."""
    key = ("raft", int(round(thickness_mm)))
    if key in cache:
        return cache[key]
    name = "RCC Raft %d mm" % key[1]
    found = []
    for ft in collect(BuiltInCategory.OST_StructuralFoundation) + collect(BuiltInCategory.OST_Floors):
        try:
            if getattr(ft, "IsFoundationSlab", False):
                found.append(ft)
        except Exception:
            continue
    concrete = concrete_material()
    existing = next((ft for ft in found if ft.Name == name), None)
    if existing is not None:
        if not _is_single_layer(existing, thickness_mm, concrete):
            _single_layer(existing, thickness_mm, concrete)
        cache[key] = existing
        return existing
    for ft in _concrete_first(found, lambda f: f.Name):
        new = None
        try:
            new = ft.Duplicate(name)
            if _single_layer(new, thickness_mm, concrete):
                cache[key] = new
                return new
        except Exception:
            pass
        if new is not None:
            try:
                doc.Delete(new.Id)
            except Exception:
                pass
    if not cache.get("said"):
        say("NOTE     No foundation slab type in this model (Structure > Foundation: Slab): the rafts are built "
            "as structural floors. Load or make one and build again to have them as foundations.")
        cache["said"] = True
    cache[key] = floor_type_for(thickness_mm, cache)
    return cache[key]


def _placed_file(src):
    return os.path.join(os.path.dirname(src), "placed_extras.json")


def _doc_id(doc):
    """This very model's fingerprint (its Project Information's UniqueId), or ""."""
    try:
        return str(doc.ProjectInformation.UniqueId or "")
    except Exception:
        return ""


def _delete_earlier_runs(doc, src):
    """Take out the footings, rafts, pedestals and stairs earlier runs of this project placed
    for this plan IN THIS VERY MODEL (their record carries this model's fingerprint): each
    Analyse makes a new run folder, so the previous build is often recorded in another one.
    Levels are never taken out; records with no fingerprint, or another model's, are left."""
    import glob as _glob
    me = _doc_id(doc)
    if not me:
        return 0
    here = os.path.dirname(os.path.abspath(src))
    runs = os.path.dirname(os.path.dirname(os.path.dirname(here)))
    own = os.path.normcase(os.path.abspath(_placed_file(src)))
    key = doc.PathName or doc.Title
    kinds = set()
    for bic in ("OST_StructuralFoundation", "OST_Floors", "OST_StructuralColumns", "OST_Stairs", "OST_StairsRailing"):
        try:
            kinds.add(int(getattr(BuiltInCategory, bic)))
        except Exception:
            pass
    gone = 0
    for path in _glob.glob(os.path.join(runs, "*", "revit_build", os.path.basename(here), "placed_extras.json")):
        if os.path.normcase(os.path.abspath(path)) == own:
            continue
        try:
            with io.open(path, encoding="utf-8") as f:
                rec = json.load(f)
        except Exception:
            continue
        if (rec.get("doc_ids") or {}).get(key) != me:
            continue
        for uid in rec.get(key) or []:
            el = doc.GetElement(uid)
            if el is None:
                continue
            try:
                cid = el.Category.Id
                cnum = int(cid.Value) if hasattr(cid, "Value") else int(cid.IntegerValue)
            except Exception:
                continue
            if cnum in kinds:
                doc.Delete(el.Id)
                gone += 1
    if gone:
        say("Took out %d foundation elements earlier runs of this project built in this model." % gone)
    return gone


def level_at(name, entries, existing):
    """The model level standing where the drawings' level `name` stands, when the model has
    none of that name: the only level within 1 mm of its elevation, and not itself another
    of the drawings' levels (that one is its own floor). None otherwise."""
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


def build(doc, src):
    with io.open(src, encoding="utf-8") as f:
        fd = json.load(f)
    ox, oy = [float(v) for v in fd.get("base_point") or [0.0, 0.0]]
    say("Foundations of plan %s (level '%s')." % (fd.get("plan"), fd.get("level_name")))
    say("Lined up by: %s" % (fd.get("how") or "-"))

    # take out what the previous run of this script placed
    pf = _placed_file(src)
    if REPLACE_PREVIOUS and os.path.exists(pf):
        with io.open(pf, encoding="utf-8") as f:
            prev = json.load(f).get(doc.PathName or doc.Title, [])
        gone = 0
        for uid in prev:
            el = doc.GetElement(uid)
            if el is not None:
                doc.Delete(el.Id)
                gone += 1
        say("Took out %d elements the previous run of this script placed." % gone)
    if REPLACE_PREVIOUS:
        _delete_earlier_runs(doc, src)
    placed = []

    # levels
    lv_file = os.path.join(os.path.dirname(os.path.dirname(src)), "levels.json")
    if BUILD_LEVELS and os.path.exists(lv_file):
        with io.open(lv_file, encoding="utf-8") as f:
            entries = json.load(f)
        have = dict((l.Name, to_mm(l.Elevation)) for l in levels())
        for action, name, detail in plan_levels(entries, have, MOVE_LEVELS):
            if action == "create":
                lv = Level.Create(doc, MM(detail))
                lv.Name = name
                placed.append(lv.UniqueId)
                say("LEVEL    created '%s' at %+.0f mm" % (name, detail))
            elif action == "move":
                lv = next(l for l in levels() if l.Name == name)
                lv.Elevation = MM(detail[1])
                say("LEVEL    moved '%s' from %+.0f to %+.0f mm" % (name, detail[0], detail[1]))
            elif action == "differs":
                say("LEVEL    '%s' is at %+.0f mm in the model, %+.0f mm in the drawings - left as it is "
                    "(MOVE_LEVELS = True moves it)" % (name, detail[0], detail[1]))
            elif action == "skip":
                say("LEVEL    '%s' not set up: %s" % (name, detail))

    level = next((l for l in levels() if l.Name.strip().upper() == (fd.get("level_name") or "").strip().upper()), None)
    if level is None and os.path.exists(lv_file):
        with io.open(lv_file, encoding="utf-8") as f:
            stand = level_at(fd.get("level_name") or "", json.load(f), dict((l.Name, to_mm(l.Elevation)) for l in levels()))
        if stand:
            level = next(l for l in levels() if l.Name == stand)
            say("LEVEL    no level is named '%s'; '%s' stands at its height, so the foundations hang from it. "
                "Rename it '%s' in Revit if you like." % (fd.get("level_name"), stand, fd.get("level_name")))
    if level is None:
        say("STOP: the model has no level named '%s', which the footings hang from. Create it (or "
            "rename the right one) and run again." % fd.get("level_name"))
        return placed
    level_mm = to_mm(level.Elevation)
    drawn_mm = fd.get("level_elevation_mm")
    founded = [it for it in fd.get("items") or [] if it.get("type") in ("footing", "raft")]
    if drawn_mm is None and founded:
        say("NOT BUILT  %d footings and rafts: level '%s' has no elevation in the drawings or the review, so "
            "their founding levels cannot be measured from it. Give it one (Export page, floor levels) and "
            "export again." % (len(founded), level.Name))
    else:
        for it in founded:
            if it.get("bottom_mm") is None:
                say("NOT BUILT  %s %s: no founding level - decide it in the review" % (it["type"], it.get("mark") or ""))
        if founded:
            say("Footings and rafts: each at its own founding level, measured from level '%s' (%+.0f mm in "
                "the drawings, %+.0f mm in this model)." % (level.Name, drawn_mm, level_mm))

    made = {"footings": 0, "steps": 0, "rafts": 0, "pedestals": 0}
    boxes = footing_boxes(fd.get("items") or [], level_mm, drawn_mm)
    if BUILD_FOOTINGS:
        base = _footing_family(doc)
        if base is None and boxes:
            say("STOP footings: no rectangular isolated footing family is loaded (Insert > Load Family > "
                "Structural Foundations > M_Footing-Rectangular). %d footings not built." % len(boxes))
        elif boxes:
            cache = {}
            for b in boxes:
                try:
                    sym = _footing_type(doc, base, b, cache)
                    pt = XYZ(MM(b["x"] - ox), MM(b["y"] - oy), level.Elevation)
                    inst = doc.Create.NewFamilyInstance(pt, sym, level, Structure.StructuralType.Footing)
                    # the footing's top on the level, moved down to where it belongs
                    off = b["top"] - level_mm
                    p = inst.LookupParameter("Height Offset From Level") or \
                        inst.get_Parameter(BuiltInParameter.INSTANCE_ELEVATION_PARAM)
                    if p is not None and not p.IsReadOnly:
                        p.Set(MM(off))
                    else:
                        say("WARNING  footing %s: its height offset could not be set (%+.0f mm wanted)"
                            % (b["mark"], off))
                    if b["size_x"] > b["size_y"] + 1.0:
                        # the family's Width runs along x: turn a footing that is longer along x
                        from Autodesk.Revit.DB import ElementTransformUtils
                        axis = Line.CreateBound(pt, XYZ(pt.X, pt.Y, pt.Z + 1.0))
                        ElementTransformUtils.RotateElement(doc, inst.Id, axis, 3.14159265358979 / 2.0)
                    if abs(b.get("rotation") or 0.0) > 1e-6:
                        # its foundation sheet is drawn turned against the plan: turned with it
                        from Autodesk.Revit.DB import ElementTransformUtils
                        axis = Line.CreateBound(pt, XYZ(pt.X, pt.Y, pt.Z + 1.0))
                        ElementTransformUtils.RotateElement(doc, inst.Id, axis, b["rotation"] * 3.14159265358979 / 180.0)
                    set_mark(inst, b["mark"])
                    set_comment(inst, "%s | %s | from CAD (stage 8)" % (b["mark"], b["kind"]))
                    placed.append(inst.UniqueId)
                    made["footings" if b["kind"] == "footing" else "steps"] += 1
                except Exception as exc:
                    say("footing %s failed: %s" % (b["mark"], exc))

    if BUILD_PEDESTALS:
        peds = [p for p in (pedestal_box(it, boxes, level_mm) for it in fd.get("items") or []
                            if it.get("type") == "pedestal") if p]
        if peds:
            cols = TypeCache(BuiltInCategory.OST_StructuralColumns, "PD")
            if cols.base is None:
                say("STOP pedestals: no rectangular concrete column family is loaded; %d not built." % len(peds))
            for p in peds if cols.base is not None else []:
                try:
                    sym = cols.get(p["size_x"], p["size_y"])
                    activate(sym)
                    pt = XYZ(MM(p["x"] - ox), MM(p["y"] - oy), level.Elevation)
                    inst = doc.Create.NewFamilyInstance(pt, sym, level, Structure.StructuralType.Column)
                    # from the footing's top (below the level) up to the level
                    _setp(inst, BuiltInParameter.FAMILY_BASE_LEVEL_PARAM, level.Id)
                    _setp(inst, BuiltInParameter.FAMILY_BASE_LEVEL_OFFSET_PARAM, MM(p["bottom"] - level_mm))
                    _setp(inst, BuiltInParameter.FAMILY_TOP_LEVEL_PARAM, level.Id)
                    _setp(inst, BuiltInParameter.FAMILY_TOP_LEVEL_OFFSET_PARAM, 0.0)
                    set_mark(inst, p["mark"])
                    set_comment(inst, "PEDESTAL %s | from CAD (stage 8)" % p["mark"])
                    placed.append(inst.UniqueId)
                    made["pedestals"] += 1
                except Exception as exc:
                    say("pedestal %s failed: %s" % (p["mark"], exc))

    if BUILD_RAFTS:
        cache = {}
        for r in raft_slabs(fd.get("items") or [], level_mm, drawn_mm):
            try:
                pts = slab_loop_points(r["outline"])
                ft = raft_type_for(r["thickness"], cache)
                if not pts or ft is None:
                    say("raft %s: no usable outline or floor type" % r["mark"])
                    continue
                loop = CurveLoop()
                xyz = [XYZ(MM(p[0] - ox), MM(p[1] - oy), level.Elevation) for p in pts]
                for i in range(len(xyz)):
                    loop.Append(Line.CreateBound(xyz[i], xyz[(i + 1) % len(xyz)]))
                try:
                    fl = Floor.Create(doc, [loop], ft.Id, level.Id, True, None, 0.0)
                except Exception:
                    fl = Floor.Create(doc, [loop], ft.Id, level.Id)
                _setp(fl, BuiltInParameter.FLOOR_PARAM_IS_STRUCTURAL, 1)
                if not set_floor_offset(fl, MM(r["top"] - level_mm)):
                    say("WARNING  raft %s: could not lower it to %+.0f mm" % (r["mark"], r["top"] - level_mm))
                set_mark(fl, r["mark"])
                set_comment(fl, "RAFT %s | %d mm | from CAD (stage 8)" % (r["mark"], round(r["thickness"])))
                say("RAFT     %s: %d mm, type '%s'" % (r["mark"], round(r["thickness"]), ft.Name))
                placed.append(fl.UniqueId)
                made["rafts"] += 1
            except Exception as exc:
                say("raft %s failed: %s" % (r["mark"], exc))

    if FOUNDATION_SIZE_FILTERS:
        try:
            doc.Regenerate()
            make_foundation_size_filters()
        except Exception as exc:
            say("Footing size filters skipped (%s)." % exc)
        if made["pedestals"]:
            try:
                make_column_size_filters()          # pedestals in their size's column colour
            except Exception as exc:
                say("Pedestal size filters skipped (%s)." % exc)

    say("Built: %d footings, %d footing steps, %d pedestals, %d rafts."
        % (made["footings"], made["steps"], made["pedestals"], made["rafts"]))
    for line in fd.get("not_placed") or []:
        say("NOT BUILT  " + line)
    return placed


def build_stairs(doc, src):
    """Every plan's stairs (revit_build\\<plan>\\stairs.json), each in its own stair edit
    session after the foundations are committed: a stair Revit refuses is reported and
    undoes nothing else. Returns the UniqueIds placed."""
    from Autodesk.Revit.DB.Architecture import StairsEditScope, StairsLanding, StairsRun, StairsRunJustification
    root = os.path.dirname(os.path.dirname(src))
    placed = []
    for folder in sorted(os.listdir(root)):
        sj = os.path.join(root, folder, "stairs.json")
        if not os.path.exists(sj):
            continue
        with io.open(sj, encoding="utf-8") as f:
            sd = json.load(f)
        top = next((l for l in levels() if l.Name.strip().upper() == (sd.get("level_name") or "").strip().upper()),
                   None)
        base = neighbour_level(top, -1) if top is not None else None
        if top is None or base is None:
            say("STAIRS %s: no level '%s' with a level below it in the model - not built"
                % (folder, sd.get("level_name")))
            continue
        ox, oy = [float(v) for v in sd.get("base_point") or [0.0, 0.0]]
        for n, stair in enumerate(sd.get("stairs") or []):
            runs, riser, note = stair_runs(stair, to_mm(base.Elevation), to_mm(top.Elevation))
            if not runs:
                say("STAIRS %s #%d: %s" % (folder, n + 1, note))
                continue
            scope = StairsEditScope(doc, "Stair from CAD")
            try:
                sid = scope.Start(base.Id, top.Id)
                t = Transaction(doc, "Stair runs from CAD")
                t.Start()
                made_runs = []
                for r in runs:
                    z = MM(r["base"] - to_mm(base.Elevation))
                    line = Line.CreateBound(XYZ(MM(r["start"][0] - ox), MM(r["start"][1] - oy), z),
                                            XYZ(MM(r["end"][0] - ox), MM(r["end"][1] - oy), z))
                    run = StairsRun.CreateStraightRun(doc, sid, line, StairsRunJustification.Center)
                    p = run.LookupParameter("Actual Run Width")
                    if p is not None and not p.IsReadOnly:
                        p.Set(MM(r["width"]))
                    made_runs.append(run)
                for a, c in zip(made_runs, made_runs[1:]):
                    try:
                        StairsLanding.CreateAutomaticLanding(doc, a.Id, c.Id)
                    except Exception as exc:
                        say("STAIRS %s #%d: landing not made (%s)" % (folder, n + 1, exc))
                t.Commit()
                scope.Commit(_Quiet())
                placed.append(doc.GetElement(sid).UniqueId)
                say("STAIRS %s #%d: %d flights, %d risers of %.0f mm, %s to %s%s" % (
                    folder, n + 1, len(runs), sum(r["risers"] for r in runs), riser, base.Name, top.Name,
                    (" - " + note) if note else "")
                    + " (direction of travel taken from the drawing order: flip it in Revit if UP is at "
                      "the other end)")
            except Exception as exc:
                try:
                    scope.Cancel()
                except Exception:
                    pass
                say("STAIRS %s #%d failed: %s" % (folder, n + 1, exc))
        for line in sd.get("not_built") or []:
            say("NOT BUILT  " + line)
    return placed


def _Quiet():
    """A failures handler that lets warnings through (stairs raise several) and
    stops on errors, which are then reported."""
    from Autodesk.Revit.DB import FailureProcessingResult, FailureSeverity, IFailuresPreprocessor

    class Quiet(IFailuresPreprocessor):
        __namespace__ = "C2R.Stairs"           # pythonnet 3 needs it to implement an interface

        def PreprocessFailures(self, fa):
            for f in list(fa.GetFailureMessages()):
                if f.GetSeverity() == FailureSeverity.Warning:
                    fa.DeleteWarning(f)
            return FailureProcessingResult.Continue
    return Quiet()


def main():
    _revit()
    doc, host = _find_doc()
    if doc is None:
        say("No usable Revit document found. Open a project and run this from Rhino.Inside.Revit, "
            "Dynamo or pyRevit, as build_model.py.")
        return
    _lift_helpers()
    globals()["doc"] = doc
    say("Host: %s, document: %s" % (host, doc.Title))
    src = _input(0, "extras_json") or _pick()
    if not src or not os.path.exists(src):
        say("No foundations.json chosen - nothing built.")
        return
    t = None
    if host != "Dynamo":
        t = Transaction(doc, "Build foundations from CAD (stage 8)")
        t.Start()
    else:
        from RevitServices.Transactions import TransactionManager
        TransactionManager.Instance.EnsureInTransaction(doc)
    try:
        placed = build(doc, src)
        if t is not None:
            t.Commit()
        else:
            TransactionManager.Instance.TransactionTaskDone()
    except Exception:
        if t is not None:
            t.RollBack()
        raise
    if BUILD_STAIRS and host != "Dynamo":
        try:
            placed += build_stairs(doc, src)
        except Exception as exc:
            say("Stairs not built: %s" % exc)
    elif BUILD_STAIRS:
        say("Stairs: run this from pyRevit or Rhino.Inside to build them (Dynamo keeps its own transaction).")
    pf = _placed_file(src)
    rec = {}
    if os.path.exists(pf):
        with io.open(pf, encoding="utf-8") as f:
            rec = json.load(f)
    rec[doc.PathName or doc.Title] = placed
    rec.setdefault("doc_ids", {})[doc.PathName or doc.Title] = _doc_id(doc)
    _write(pf, json.dumps(rec, indent=1))
    _write(os.path.join(os.path.dirname(src), "build_extras_log.txt"), "\n".join(log))


if __name__ == "__main__" or globals().get("__revit__") is not None or globals().get("IN") is not None:
    if not globals().get("_SELFTEST"):
        try:
            main()
        except Exception:
            say("FAILED:")
            say(traceback.format_exc())
        OUT = "\n".join(log)
        try:
            print(OUT)
        except Exception:
            pass
