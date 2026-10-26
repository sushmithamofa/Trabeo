r"""The review screen: look at every plan, see why each value is what it is, decide.

    python -m building.review OUT\building.json [review.html]

Writes one self-contained HTML file (no server, no installation): open it in a
browser. Each plan is drawn element by element, coloured by status; clicking an
element shows every property with every source that gave it. Decisions - use
one source's value, type a value, exclude an element - apply to that element or
to every element with that mark on that level, and are exported as
overrides.json for the next run:

    python -m building.readset OUT DRAWINGS --overrides overrides.json

The same decisions can be made in the Excel's Overrides sheet; both produce the
same overrides.json.
"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from building import Building, support_of        # noqa: E402

LOW = 0.6
DRAWN = ("beam", "beam_span", "fold", "column", "slab", "wall", "footing", "raft", "pedestal", "stair", "ramp")
SHOWN_AS = {"fold": "hidden_beam"}     # a slab fold is the concealed / hidden beam at a sunk step
PROPS = {"beam": ("mark", "width", "depth"), "beam_span": ("mark", "width", "depth"), "fold": ("width", "depth"),
         "column": ("mark", "width", "depth"), "slab": ("mark", "thickness"), "wall": ("mark", "width"),
         "footing": ("mark", "width", "length", "depth"), "raft": ("thickness",), "pedestal": ("mark",),
         "stair": ("going", "riser", "width"), "ramp": ("width", "length")}
FOUNDATION = ("footing", "raft", "pedestal")
RANK = {"conflict": 4, "missing": 3, "low": 2, "override": 1, "read": 0}


def _key(v) -> str:
    if v is None:
        return "missing"
    if v.status == "read" and v.confidence < LOW:
        return "low"
    return v.status if v.status in RANK else "read"


def _r(x):
    return round(float(x)) if x is not None else None


def _geom(e):
    g = e.geometry
    if e.type in ("beam", "beam_span", "fold", "wall") and g.get("start") and g.get("end"):
        if g.get("mid"):                     # a curved beam: along its arc
            from building.ifc_export import _arc_points
            pts = _arc_points(tuple(g["start"][:2]), tuple(g["mid"][:2]), tuple(g["end"][:2]), 16)
        else:
            pts = [g["start"], g["end"]]
        if e.type == "wall" and g.get("outline") and not g.get("start"):
            pts = g["outline"]
        return {"k": "line", "p": [[_r(p[0]), _r(p[1])] for p in pts],
                "w": _r(e.final("width") or e.get("width") or e.get("thickness") or 230)}
    if g.get("outline"):
        return {"k": "poly", "p": [[_r(p[0]), _r(p[1])] for p in g["outline"]]}
    if g.get("x") is not None:
        return {"k": "point", "p": [[_r(g["x"]), _r(g["y"])]]}
    return None


UNDERLAY_MAX = 60000         # line segments; a busier plan keeps only its mapped layers
_UNDER: dict = {}            # dxf path -> (modified time, linework, outlines)


def _cad(path: str, roles: dict):
    """The plan's own CAD linework (lines, polylines, arcs, block contents) as segments,
    and its closed column-sized outlines, read once per file."""
    import math
    try:
        mt = os.path.getmtime(path)
    except OSError:
        return None
    if _UNDER.get(path, (None,))[0] == mt:
        return _UNDER[path][1:]
    import ezdxf
    try:
        doc = ezdxf.readfile(path)
    except Exception:
        return None
    segs, boxes = [], []

    from ezdxf.upright import upright

    def add(e, depth=0):
        t = e.dxftype()
        lay = e.dxf.get("layer", "0")
        try:
            upright(e)              # drawn mirrored (plane facing down): read where it is on the drawing
        except Exception:
            pass
        if (t == "INSERT" and depth < 2) or t == "MLINE":
            # a block's contents; a multiline's lines (J1153 draws its beams as MLINEs)
            try:
                for v in e.virtual_entities():
                    add(v, depth + 1)
            except Exception:
                pass
        elif t == "LINE":
            segs.append((lay, e.dxf.start.x, e.dxf.start.y, e.dxf.end.x, e.dxf.end.y))
        elif t in ("LWPOLYLINE", "POLYLINE"):
            pts = [(p[0], p[1]) for p in (e.get_points("xy") if t == "LWPOLYLINE" else
                                         [v.dxf.location for v in e.vertices])]
            closed = bool(e.closed if t == "LWPOLYLINE" else e.is_closed)
            for a, c in zip(pts, pts[1:] + (pts[:1] if closed else [])):
                segs.append((lay, a[0], a[1], c[0], c[1]))
            if len(pts) >= 4 and (closed or math.dist(pts[0], pts[-1]) < 1.0):
                boxes.append((lay, pts[:4]))
        elif t in ("ARC", "CIRCLE"):
            c, r = e.dxf.center, e.dxf.radius
            a0 = math.radians(e.dxf.start_angle) if t == "ARC" else 0.0
            a1 = math.radians(e.dxf.end_angle) if t == "ARC" else 2 * math.pi
            if a1 <= a0:
                a1 += 2 * math.pi
            n = max(4, min(24, int((a1 - a0) / 0.3)))
            pts = [(c.x + r * math.cos(a0 + (a1 - a0) * i / n), c.y + r * math.sin(a0 + (a1 - a0) * i / n))
                   for i in range(n + 1)]
            segs.extend((lay, a[0], a[1], q[0], q[1]) for a, q in zip(pts, pts[1:]))

    for e in doc.modelspace():
        add(e)
    if len(segs) > UNDERLAY_MAX:
        segs = [s for s in segs if roles.get(s[0], "unmapped") not in ("unmapped", "ignore")]
    segs = segs[:UNDERLAY_MAX]
    _UNDER[path] = (mt, segs, boxes)
    return segs, boxes


def _unused(boxes, els, roles):
    """Closed column-sized outlines on a column layer (or one named for columns) with no
    modelled column or wall on them: drawn, and not in the model."""
    import math
    import re
    have = []
    for e in els:
        if e["t"] in ("column", "wall") and e["g"]["p"]:
            pts = e["g"]["p"]
            have.append((sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts), pts))
    out = []
    for lay, pts in boxes:
        role = roles.get(lay, "")
        # the column / wall layers themselves, or an unmapped one named for columns -
        # never a tag layer ('colno' boxes round the column numbers)
        if not (role in ("column", "column_stop", "column_float", "wall")
                or (role in ("", "unmapped") and re.search(r"COL", lay, re.I))):
            continue
        sides = sorted(math.dist(pts[i], pts[(i + 1) % 4]) for i in range(4))
        if not (150.0 <= sides[0] and sides[-1] <= 3000.0):
            continue
        cx, cy = sum(p[0] for p in pts) / 4, sum(p[1] for p in pts) / 4
        if any(math.hypot(cx - x, cy - y) < 300.0 or _in(cx, cy, q) for x, y, q in have):
            continue
        out.append({"p": [[round(p[0]), round(p[1])] for p in pts], "l": lay})
    return out


def _in(x, y, poly) -> bool:
    hit = False
    for i in range(len(poly)):
        (x1, y1), (x2, y2) = poly[i][:2], poly[i - 1][:2]
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            hit = not hit
    return hit


def _underlay(s, els, run_dir=""):
    """What the drawing draws under what the model holds (external review, point G):
    the CAD as faint lines, and the outlines the model did not use, highlighted. Only
    what lies on the plan's own extent: a whole drawing holds its sections beside it."""
    path = s.dxf_file or ""
    if path and not os.path.exists(path) and run_dir:
        # a plan read from the whole drawing: its DXF was a temporary copy; the same
        # conversion is kept in the run's work folder and the project's cache
        name = os.path.basename(path)
        path = next((p for p in (os.path.join(run_dir, "_work", name),
                                 os.path.join(os.path.dirname(os.path.dirname(run_dir)), "dxf_cache", name))
                     if os.path.exists(p)), "")
    if not path or not els or not os.path.exists(path):
        return None
    roles = s.layer_map or {}
    got = _cad(path, roles)
    if not got or not got[0]:
        return None
    segs, boxes = got
    xs = [p[0] for e in els for p in e["g"]["p"]]
    ys = [p[1] for e in els for p in e["g"]["p"]]
    m = 0.15 * max(max(xs) - min(xs), max(ys) - min(ys)) + 2000.0     # a dropped edge column stays in
    x0, x1, y0, y1 = min(xs) - m, max(xs) + m, min(ys) - m, max(ys) + m

    def on(x, y):
        return x0 <= x <= x1 and y0 <= y <= y1
    segs = [sg for sg in segs if on(sg[1], sg[2]) or on(sg[3], sg[4])]
    boxes = [bx for bx in boxes if all(on(p[0], p[1]) for p in bx[1])]
    if not segs:
        return None                        # not in the model's coordinates: drawn nothing
    ox, oy = round(min(xs)), round(min(ys))
    flat = []
    for _l, x1, y1, x2, y2 in segs:
        flat += [round(x1) - ox, round(y1) - oy, round(x2) - ox, round(y2) - oy]
    return {"o": [ox, oy], "s": flat, "u": _unused(boxes, els, roles)}


def data(b: Building, decisions: dict | None = None, run_dir: str = "") -> dict:
    from building.export import left_out
    sheets = []
    titles = {sid: s.title for sid, s in b.sheets.items()}
    with_foundations = {e.sheet for e in b.elements if e.type in FOUNDATION}
    for sid, s in b.sheets.items():
        if s.reader != "plan.legacy" and sid not in with_foundations:
            continue
        els = []
        view_of = s.extra.get("view_sheet", "")
        for e in b.elements:
            # stairs and ramps are read from the plan's view itself, not by the plan reader
            here = e.sheet == sid or (view_of and e.sheet == view_of and e.type in ("stair", "ramp"))
            if not here or e.type not in DRAWN or e.extra.get("split_into") or e.extra.get("merged_into"):
                continue
            if e.type == "stair" and not (decisions or {}).get("model_stairs"):
                continue                    # stairs switched off: their lines are in the CAD underneath
            g = _geom(e)
            if g is None:
                continue
            props, worst = {}, "read"
            for p in PROPS[e.type] + tuple(x for x in ("scope", "section", "top_mm", "slope") if x in e.resolved):
                v = e.resolved.get(p)
                if v is None and e.type == "fold":    # not linked: as read off the drawing
                    v = e.props.get(p)
                k = _key(v)
                if RANK[k] > RANK[worst]:
                    worst = k
                props[p] = {"v": v.value if v is not None else None, "s": k,
                            "c": v.confidence if v is not None else 0,
                            "src": [{"k": ev.source, "t": ev.text, "sh": ev.sheet, "h": ev.handle}
                                    for ev in (v.evidence if v is not None else [])][:12]}
            masonry = e.type == "wall" and e.final("role") == "masonry"
            # masonry is never built (export, 3D view): drawn grey and dashed like everything
            # left out, not green under the beam that sits on it as if it were structure
            out = left_out(e) or masonry
            if masonry:
                worst = "read"          # shown, never built: nothing to decide about it
            dv = e.resolved.get("depth_varies")
            els.append({"id": e.id, "t": SHOWN_AS.get(e.type, e.type), "m": e.final("mark") or "", "w": e.wing,
                        "lv": s.extra.get("level_key") or e.level, "st": worst, "p": props, "g": g,
                        "ex": bool(e.resolved.get("excluded")), "out": out,
                        "var": dv.value if dv is not None else None,
                        "note": "; ".join(x for x in (
                            ("masonry: not structure, not built" if masonry else "") or
                            ("left out of the model: " + str(e.resolved["scope"].value).split(":", 1)[-1].strip()
                             + " ('this block' builds it)" if out else "") or
                            e.extra.get("cut_before") or e.extra.get("cut_after") or
                            e.extra.get("tag_set_aside") or "",
                            ("faces drawn " + ", ".join(f"{k} {v}" for k, v in sorted(e.extra["faces"].items()))
                             + " (dashed: under the slab; solid: seen from above)") if e.extra.get("faces") else "",
                            (f"one beam drawn with {len(e.extra['merged_from'])} lines, joined")
                            if e.extra.get("merged_from") else "",
                            e.extra.get("legend") or "") if x),
                        "model": support_of(e.type)["model"]})
        # beams turning an L corner are drawn on to fill it (their ends are not changed)
        from building.beam_ends import corner_extensions, carried
        straight = [x for x in els if x["t"] in ("beam", "beam_span", "hidden_beam")
                    and x["g"]["k"] == "line" and len(x["g"]["p"]) == 2]
        for x, ext in zip(straight, corner_extensions(
                [(x["g"]["p"][0], x["g"]["p"][1], x["g"]["w"]) for x in straight])):
            if ext[0] or ext[1]:
                a, c = carried(x["g"]["p"][0], x["g"]["p"][1], ext)
                x["g"]["p"] = [[_r(a[0]), _r(a[1])], [_r(c[0]), _r(c[1])]]
        view = s.extra.get("view_sheet", "")
        title = titles.get(view, view) if view else s.title
        sheets.append({"id": sid, "lv": s.extra.get("level_key") or s.level or
                       ("FDN" if sid in with_foundations else ""), "title": title,
                       "file": os.path.basename(s.file), "els": els, "set": s.extra.get("set", ""),
                       "cad": _underlay(s, els, run_dir) if s.reader == "plan.legacy" else None})
    # an area marked as another block lists all its elements: decided together
    issues = [{"sev": i.severity, "cat": i.category, "msg": i.message,
               "els": i.elements if i.category == "scope" else i.elements[:6],
               "sheet": i.sheet, **({"prefix": i.extra["prefix"]} if i.extra.get("prefix") else {})}
              for i in b.issues if i.extra.get("from_linker")]
    levels = []
    for s in sheets:
        if s["lv"] and s["lv"] not in levels:
            levels.append(s["lv"])
    # the 3D view of each drawing set (the same scene as model3d.html), its items
    # carrying the ids of the elements above so a click in 3D selects them here
    from building import viewer3d
    d3 = []
    for set_key in sorted({s["set"] for s in sheets if s["set"]}):
        try:
            sc = viewer3d.scene(b, set_key, decisions)        # the floor heights decided, as the build
        except Exception:
            sc = None
        if sc and sc.get("items"):
            d3.append(sc)
    return {"project": b.project, "store": _store(run_dir), "levels": levels, "sheets": sheets,
            "issues": issues, "d3": d3}


def _store(run_dir: str) -> str:
    """Which job this browser's saved decisions belong to: the project folder for a run
    in its 'runs' (so a new analysis of the same job keeps them), else the run folder.
    Two jobs never share it, whatever their names or sheets."""
    if not run_dir:
        return ""
    run = os.path.normcase(os.path.abspath(run_dir))
    parent = os.path.dirname(run)
    return os.path.dirname(parent) if os.path.basename(parent) == "runs" else run


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Structure Review</title>
<style>
:root{--bg:#f6f5f1;--panel:#fff;--ink:#1d2730;--mute:#5f6c77;--line:#dcd9d0;--accent:#1f6fb2;
--read:#2e8b57;--low:#d98a1c;--conflict:#c0392b;--missing:#c9a600;--override:#7b5fb4;--chip:#eceae4}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:13px/1.45 system-ui,'Segoe UI',sans-serif}
header{display:flex;gap:16px;align-items:center;padding:10px 16px;border-bottom:1px solid var(--line);background:var(--panel);flex-wrap:wrap}
header h1{font-size:16px;margin:0}.chips{display:flex;gap:6px;flex-wrap:wrap}
.chip{padding:2px 8px;border-radius:10px;background:var(--chip);font-size:12px}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:4px;vertical-align:middle}
main{display:grid;grid-template-columns:260px 1fr 360px;height:calc(100vh - 92px)}
aside,section{overflow-y:auto;overflow-x:hidden;border-right:1px solid var(--line)}aside{background:var(--panel)}
.list{padding:8px}.item{padding:6px 8px;border-radius:6px;cursor:pointer;margin-bottom:2px}
.item:hover{background:var(--chip)}.item.on{background:#dfe9f4}
.small{font-size:11px;color:var(--mute)}h2{font-size:12px;text-transform:uppercase;letter-spacing:.5px;color:var(--mute);margin:12px 8px 4px}
#canvas{position:relative;background:#fbfaf7}svg{width:100%;height:100%;display:block;cursor:grab}
#tip{position:absolute;pointer-events:none;background:#1d2730;color:#fff;padding:4px 8px;border-radius:4px;font-size:12px;display:none;white-space:nowrap}
#detail{padding:12px;background:var(--panel)}table{width:100%;border-collapse:collapse}
td,th{border-bottom:1px solid var(--line);padding:5px 4px;text-align:left;vertical-align:top}
th{font-size:11px;color:var(--mute);font-weight:600}.src{font-size:11px;color:var(--mute)}
button{font:inherit;border:1px solid var(--line);background:#fff;border-radius:5px;padding:3px 8px;cursor:pointer}
button.primary{background:var(--accent);color:#fff;border-color:var(--accent)}button:hover{filter:brightness(.96)}
input[type=text]{font:inherit;width:80px;padding:2px 4px;border:1px solid var(--line);border-radius:4px}
select,input[type=search]{font:inherit;padding:3px;border:1px solid var(--line);border-radius:4px;width:100%}
footer{display:flex;gap:10px;align-items:center;padding:8px 16px;border-top:1px solid var(--line);background:var(--panel);height:46px}
.badge{font-size:11px;padding:1px 6px;border-radius:8px;color:#fff}
@media (max-width:900px){main{grid-template-columns:1fr;height:auto}aside,section{max-height:60vh}}
.seg button{border-radius:0;margin-left:-1px}.seg button:first-child{border-radius:5px 0 0 5px}
.seg button:last-child{border-radius:0 5px 5px 0}.seg button.on{background:var(--ink);color:#fff;border-color:var(--ink)}
#gl{position:absolute;inset:0;display:none}#gl canvas{display:block}
#d3note{position:absolute;left:10px;bottom:8px;max-width:70%;font-size:11px;color:var(--mute);display:none;
 background:rgba(255,255,255,.85);padding:4px 6px;border-radius:4px}
#d3types label{font-size:12px;margin-right:6px;white-space:nowrap}
</style>
<script src="https://cdnjs.cloudflare.com/ajax/libs/three.js/0.160.0/three.min.js"></script>
</head><body>
<header><h1>Structure review</h1><span id="proj" class="small"></span><div class="chips" id="chips"></div>
<span class="seg"><button id="m2d" class="on">Plan</button><button id="m3d">3D</button></span>
<span id="lvbar" class="small">Level <select id="lvsel" style="width:auto"></select>
<label title="the drawing's own lines under the model; outlines the model did not use in orange"><input type="checkbox" id="cadon" checked> CAD underneath</label></span>
<span id="d3bar" style="display:none"><select id="d3set" style="width:auto"></select> <button id="d3all">All levels</button>
<span id="d3types"></span></span>
<span class="small" id="hint" style="margin-left:auto">click an element to see every source &middot; scroll to zoom &middot; drag to pan</span></header>
<main>
<aside><h2>Plans</h2><div class="list" id="plans"></div>
<h2>Issues</h2><div style="padding:0 8px"><select id="cat"></select></div><div class="list" id="issues"></div></aside>
<section id="canvas"><svg id="svg"></svg><div id="gl"></div><div id="d3note"></div><div id="tip"></div></section>
<aside id="detail"><p class="small">Select an element on the plan, or an issue on the left.</p></aside>
</main>
<footer><b id="ndec">0 decisions</b><span class="small">saved in this browser until you export them</span>
<button class="primary" id="export">Export overrides.json</button><label class="small"><input type="file" id="import" accept=".json" style="display:none">
<button onclick="document.getElementById('import').click()">Import overrides.json</button></label>
<button id="clear">Clear decisions</button></footer>
<script id="data" type="application/json">__DATA__</script>
<script>
const D=JSON.parse(document.getElementById('data').textContent);
const COL={read:'var(--read)',low:'var(--low)',conflict:'var(--conflict)',missing:'var(--missing)',override:'var(--override)',out:'#9aa3ab'};
const LBL={read:'read',low:'read (low)',conflict:'conflict',missing:'missing',override:'decided',out:'left out'};
const KEY='review:'+(D.store||'')+':'+(D.project||'')+':'+D.sheets.map(s=>s.id).join('|');
let dec={elements:{},marks:{},exclude:[],prefixes:{}};
try{const s=localStorage.getItem(KEY);if(s)dec=JSON.parse(s)}catch(e){}
dec.prefixes=dec.prefixes||{};
const byId={};D.sheets.forEach(s=>s.els.forEach(e=>{e.sheet=s.id;byId[e.id]=e}));
let cur=null,sel=null,vb=null;
const $=id=>document.getElementById(id);
function save(){try{localStorage.setItem(KEY,JSON.stringify(dec))}catch(e){}
 const n=Object.keys(dec.elements).length+Object.keys(dec.marks).length+dec.exclude.length+Object.keys(dec.prefixes||{}).length;$('ndec').textContent=n+' decision'+(n==1?'':'s')}
function decided(e,p){const a=dec.elements[e.id];if(a&&p in a)return a[p];
 for(const k of [e.m+'@'+e.w+'/'+e.lv,e.m+'@'+e.w,e.m]){const m=dec.marks[k];if(m&&p in m)return m[p]}return undefined}
function status(e){if(dec.exclude.includes(e.id))return 'override';
 if(e.out&&!/^this/i.test(String(decided(e,'scope')??'')))return 'out';   // not built: nothing to fill in
 let w='read',r={read:0,override:1,low:2,missing:3,conflict:4};
 for(const p in e.p){const s=decided(e,p)!==undefined?'override':e.p[p].s;if(r[s]>r[w])w=s}return w}
function chips(){const c={read:0,low:0,conflict:0,missing:0,override:0,out:0};D.sheets.forEach(s=>s.els.forEach(e=>c[status(e)]++));
 $('chips').innerHTML=Object.keys(c).map(k=>`<span class="chip"><span class="dot" style="background:${COL[k]}"></span>${LBL[k]} ${c[k]}</span>`).join('')}
function plans(){$('plans').innerHTML=D.sheets.map(s=>{const c={};s.els.forEach(e=>{const k=status(e);c[k]=(c[k]||0)+1});
 return `<div class="item${cur&&cur.id==s.id?' on':''}" data-id="${s.id}"><b>${s.lv||'?'}</b> &middot; ${esc(s.title||s.file)}<div class="small">${s.els.length} elements`
 +['conflict','missing','low'].filter(k=>c[k]).map(k=>` &middot; <span style="color:${COL[k]}">${c[k]} ${LBL[k]}</span>`).join('')
 +(s.cad&&s.cad.u.length?` &middot; <span style="color:#e67e22">${s.cad.u.length} drawn outlines not in the model</span>`:'')+`</div></div>`}).join('');
 document.querySelectorAll('#plans .item').forEach(n=>n.onclick=()=>show(D.sheets.find(s=>s.id==n.dataset.id)));
 $('lvsel').innerHTML=D.sheets.map(s=>`<option value="${esc(s.id)}"${cur&&cur.id==s.id?' selected':''}>${esc(s.lv||'?')} - ${esc(s.title||s.file)}</option>`).join('')}
$('lvsel').onchange=ev=>show(D.sheets.find(s=>s.id==ev.target.value));
function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
// A plan drawn a few degrees off square (a turned UCS) is shown square: the
// display is turned by the angle most of the beam length runs at. Display only.
function domAng(segs){const h={};let tot=0,best=0,bw=0;for(const [a,b] of segs){const dx=b[0]-a[0],dy=b[1]-a[1],L=Math.hypot(dx,dy);if(L<1)continue;
 let t=((Math.atan2(dy,dx)*180/Math.PI)%90+90)%90;if(t>45)t-=90;const k=Math.round(t*2)/2;h[k]=(h[k]||0)+L;tot+=L;if(h[k]>bw){bw=h[k];best=k}}
 return (Math.abs(best)<0.25||bw<0.4*tot)?0:best}
let ang=0,ca=1,sa=0;
function setAng(a){ang=a;ca=Math.cos(-a*Math.PI/180);sa=Math.sin(-a*Math.PI/180)}
function P(p){return [p[0]*ca-p[1]*sa,p[0]*sa+p[1]*ca]}
function bounds(s){let x0=1e18,y0=1e18,x1=-1e18,y1=-1e18;s.els.forEach(e=>e.g.p.map(P).forEach(p=>{x0=Math.min(x0,p[0]);x1=Math.max(x1,p[0]);y0=Math.min(y0,p[1]);y1=Math.max(y1,p[1])}));
 const pad=Math.max(x1-x0,y1-y0)*0.03+500;return [x0-pad,-(y1+pad),x1-x0+2*pad,y1-y0+2*pad]}
function show(s,focus){cur=s;setAng(domAng(s.els.filter(e=>e.g.k=='line'&&e.g.p.length>=2).map(e=>[e.g.p[0],e.g.p[e.g.p.length-1]])));plans();vb=bounds(s);draw();if(focus)zoomTo(focus)}
function cad(){const u=cur.cad;if(!u||!$('cadon').checked)return '';const o=u.o,s=u.s;let d='';
 for(let i=0;i<s.length;i+=4){const a=P([s[i]+o[0],s[i+1]+o[1]]),b=P([s[i+2]+o[0],s[i+3]+o[1]]);d+=`M${a[0]|0} ${-a[1]|0}L${b[0]|0} ${-b[1]|0}`}
 let h=`<path d="${d}" fill="none" stroke="#8d969e" stroke-opacity=".55" stroke-width="0.6" vector-effect="non-scaling-stroke" pointer-events="none"/>`;
 u.u.forEach(x=>{const pts=x.p.map(P).map(p=>p[0]+','+(-p[1])).join(' ');
  h+=`<polygon points="${pts}" fill="#e67e22" fill-opacity=".25" stroke="#e67e22" stroke-width="2.5" vector-effect="non-scaling-stroke" stroke-dasharray="6 3"><title>drawn on layer ${esc(x.l)} - not in the model</title></polygon>`});
 return h}
$('cadon').onchange=()=>draw();
function draw(){if(!cur)return;const svg=$('svg');svg.setAttribute('viewBox',vb.join(' '));let h=cad();
 const order={raft:0,slab:0,ramp:0,stair:1,footing:1,wall:1,beam:2,beam_span:2,hidden_beam:2,pedestal:3,column:3};
 [...cur.els].sort((a,b)=>order[a.t]-order[b.t]).forEach(e=>{const st=status(e),c=COL[st],on=sel&&sel.id==e.id;
  const gp=e.g.p.map(P),pts=gp.map(p=>p[0]+','+(-p[1])).join(' ');
  if(e.g.k=='line')h+=`<polyline data-id="${e.id}" points="${pts}" fill="none" stroke="${c}" stroke-width="${Math.max(e.g.w||230,120)}" stroke-opacity="${on?1:e.t=='hidden_beam'?.45:.75}" stroke-linecap="butt"${on?' style="filter:drop-shadow(0 0 300px #1f6fb2)"':''}/>`;
  else if(e.g.k=='poly')h+=`<polygon data-id="${e.id}" points="${pts}" fill="${c}" fill-opacity="${st=='out'?(on?.35:.12):e.t=='slab'||e.t=='raft'||e.t=='ramp'||e.t=='stair'?(on?.35:.12):e.t=='footing'?(on?.5:.3):.9}" stroke="${on?'#1f6fb2':c}" stroke-width="${on?120:30}"${st=='out'?' stroke-dasharray="150 100"':''}/>`;
  else h+=`<circle data-id="${e.id}" cx="${gp[0][0]}" cy="${-gp[0][1]}" r="300" fill="${c}"/>`});
 svg.innerHTML=h;svg.querySelectorAll('[data-id]').forEach(n=>{n.onclick=ev=>{ev.stopPropagation();pick(byId[n.dataset.id])};
  n.onmousemove=ev=>tip(ev,byId[n.dataset.id]);n.onmouseleave=()=>$('tip').style.display='none'})}
function tip(ev,e){const t=$('tip'),r=$('canvas').getBoundingClientRect();const v=p=>e.p[p]?(decided(e,p)??e.p[p].v??'?'):'';
 t.textContent=`${e.m||'(no mark)'} · ${e.t.replace('_',' ')} · `+(e.t=='slab'?v('thickness')+' thk':v('width')+' × '+v('depth'))+` · ${LBL[status(e)]}`;
 t.style.left=(ev.clientX-r.left+12)+'px';t.style.top=(ev.clientY-r.top+12)+'px';t.style.display='block'}
function zoomTo(e){const gp=e.g.p.map(P),xs=gp.map(p=>p[0]),ys=gp.map(p=>-p[1]);const cx=(Math.min(...xs)+Math.max(...xs))/2,cy=(Math.min(...ys)+Math.max(...ys))/2;
 const w=Math.max(8000,(Math.max(...xs)-Math.min(...xs))*3);vb=[cx-w/2,cy-w/2*0.7,w,w*0.7];draw()}
function pick(e){sel=e;draw();const parts=[];
 parts.push(`<h3 style="margin:0 0 4px">${esc(e.m||'(no mark)')} <span class="badge" style="background:${COL[status(e)]}">${LBL[status(e)]}</span></h3>
 <div class="small">${e.t.replace('_',' ')} &middot; level ${esc(e.lv)}${e.w?' &middot; wing '+esc(e.w):''} &middot; Revit: ${e.model}</div>
 <div class="small" style="word-break:break-all">${esc(e.id)}</div>${e.note?`<p class="small">${esc(e.note)}</p>`:''}
 ${e.var?`<p class="small" style="color:var(--conflict)">Depth written ${e.var.join('/')}: deeper at one end - confirm.</p>`:''}
 <table><tr><th>Property</th><th>Value</th><th>Sources</th></tr>`);
 for(const p in e.p){const x=e.p[p],d=decided(e,p);const st=d!==undefined?'override':x.s;
  const srcs=x.src.map(s=>{const val=(s.k||'').split(': ').slice(1).join(': ');
   return `<div class="src">${esc(s.k)}${s.t?' &ldquo;'+esc(s.t)+'&rdquo;':''}${val&&p!='mark'?` <button data-p="${p}" data-v="${esc(val)}">use</button>`:val&&p=='mark'?` <button data-p="${p}" data-v="${esc(val)}">use</button>`:''}</div>`}).join('')||'<span class="src">no source</span>';
  parts.push(`<tr><td><b>${p}</b><br><span class="badge" style="background:${COL[st]}">${LBL[st]}</span></td>
  <td>${esc(d!==undefined?d:x.v)}<br><input type="text" data-set="${p}" placeholder="set"></td><td>${srcs}</td></tr>`)}
 parts.push(`</table><p><label class="small">Apply to <select id="scope"><option value="el">this element only</option>
 ${e.m?`<option value="mark">every ${esc(e.m)} on level ${esc(e.lv)}${e.w?' wing '+esc(e.w):''}</option>`:''}</select></label></p>
 <p><button class="primary" id="setbtn">Save typed values</button> <button id="exbtn">${dec.exclude.includes(e.id)?'Include again':'Exclude from model'}</button> <button id="undo">Undo decisions here</button></p>`);
 $('detail').innerHTML=parts.join('');
 $('detail').querySelectorAll('button[data-p]').forEach(b=>b.onclick=()=>decide(e,b.dataset.p,b.dataset.v));
 $('setbtn').onclick=()=>$('detail').querySelectorAll('input[data-set]').forEach(i=>{if(i.value.trim())decide(e,i.dataset.set,i.value.trim())});
 $('exbtn').onclick=()=>{const i=dec.exclude.indexOf(e.id);if(i>=0)dec.exclude.splice(i,1);else dec.exclude.push(e.id);after(e)};
 $('undo').onclick=()=>{delete dec.elements[e.id];delete dec.marks[e.m+'@'+e.w+'/'+e.lv];const i=dec.exclude.indexOf(e.id);if(i>=0)dec.exclude.splice(i,1);after(e)}}
const SIZES=['width','depth','thickness','diameter','length','depth_edge','going','riser'];
function decide(e,p,v){const num=parseFloat(v);const val=(p!='mark'&&!isNaN(num))?num:v;
 if(SIZES.includes(p)&&!(isFinite(num)&&num>0)){alert('"'+v+'" is not a '+p+': type a number above 0 (mm). Not saved.');return}
 if($('scope')&&$('scope').value=='mark'){const k=e.m+'@'+e.w+'/'+e.lv;(dec.marks[k]=dec.marks[k]||{})[p]=val}
 else (dec.elements[e.id]=dec.elements[e.id]||{})[p]=val;after(e)}
function after(e){save();chips();plans();draw();pick(e)}
function issues(){const cats=[...new Set(D.issues.map(i=>i.cat))];$('cat').innerHTML='<option value="">all issues ('+D.issues.length+')</option>'+cats.map(c=>`<option>${esc(c)}</option>`).join('');
 const f=()=>{const c=$('cat').value;$('issues').innerHTML=D.issues.filter(i=>!c||i.cat==c).slice(0,400).map((i,n)=>`<div class="item" data-n="${D.issues.indexOf(i)}"><span class="badge" style="background:${i.cat=='missing'?COL.missing:i.cat=='conflict'||i.cat=='scope'?COL.conflict:COL.low}">${esc(i.cat)}</span> <span class="small">${esc(i.msg)}</span>`
   +(i.cat=='scope'&&i.els.length?`<div><button data-all="this block">all ${i.els.length}: this block</button> <button data-all="other block">all ${i.els.length}: other block</button></div>`:'')
   +(i.prefix?`<div class="small">${esc(i.prefix)} marks are: `+['beam','column','slab','wall','footing'].map(f=>`<button data-pfx="${f}"${dec.prefixes[i.prefix]==f?' style="background:var(--override);color:#fff"':''}>${f}</button>`).join(' ')
     +(dec.prefixes[i.prefix]?` <button data-pfx="">undo</button><br>decided: ${esc(dec.prefixes[i.prefix])} - press Analyse again to use it`:'')+`</div>`:'')+`</div>`).join('');
  document.querySelectorAll('#issues .item').forEach(n=>n.onclick=ev=>{const i=D.issues[n.dataset.n];const all=ev.target.dataset&&ev.target.dataset.all;
   const pfx=ev.target.dataset?ev.target.dataset.pfx:undefined;
   if(pfx!==undefined&&i.prefix){if(pfx)dec.prefixes[i.prefix]=pfx;else delete dec.prefixes[i.prefix];save();f();return}
   if(all){i.els.forEach(x=>{if(byId[x])(dec.elements[x]=dec.elements[x]||{}).scope=all});save();chips();plans();draw();return}
   const e=i.els.map(x=>byId[x]).find(Boolean);
   if(e){show(D.sheets.find(s=>s.id==e.sheet),e);pick(e)}})};$('cat').onchange=f;f()}
const svg=$('svg');let drag=null;
svg.addEventListener('wheel',ev=>{if(!vb)return;ev.preventDefault();const r=svg.getBoundingClientRect(),k=ev.deltaY>0?1.2:1/1.2;
 const mx=vb[0]+(ev.clientX-r.left)/r.width*vb[2],my=vb[1]+(ev.clientY-r.top)/r.height*vb[3];vb=[mx-(mx-vb[0])*k,my-(my-vb[1])*k,vb[2]*k,vb[3]*k];svg.setAttribute('viewBox',vb.join(' '))},{passive:false});
svg.addEventListener('mousedown',ev=>{drag=[ev.clientX,ev.clientY,vb.slice()]});
window.addEventListener('mouseup',()=>drag=null);
window.addEventListener('mousemove',ev=>{if(!drag||!vb)return;const r=svg.getBoundingClientRect();
 vb=[drag[2][0]-(ev.clientX-drag[0])/r.width*drag[2][2],drag[2][1]-(ev.clientY-drag[1])/r.height*drag[2][3],drag[2][2],drag[2][3]];svg.setAttribute('viewBox',vb.join(' '))});
$('export').onclick=()=>{const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([JSON.stringify(dec,null,1)],{type:'application/json'}));a.download='overrides.json';a.click()};
$('import').onchange=ev=>{const f=ev.target.files[0];if(!f)return;f.text().then(t=>{const o=JSON.parse(t);dec={elements:o.elements||{},marks:o.marks||{},exclude:o.exclude||[],prefixes:o.prefixes||{}};save();chips();plans();draw();issues()})};
$('clear').onclick=()=>{if(confirm('Clear every decision made here?')){dec={elements:{},marks:{},exclude:[],prefixes:{}};save();chips();plans();draw();issues()}};
$('proj').textContent=(D.project||'')+(D.levels.length?' · levels '+D.levels.join(', '):'');
save();chips();plans();issues();
// ---------------------------------------------------------------- 3D view
// The same elements, the same selection, the same decisions: a click in 3D opens
// the element on the right; a decision recolours it in 3D at once.
const D3=D.d3||[],TY=['footing','column','wall','beam','slab','stair'];
let mode='2d',r3=null,s3=null,cam=null,grp=null,meshes=[],cur3=-1,focusLv=null;
const shown3=Object.fromEntries(TY.map(t=>[t,true]));
const CSS=getComputedStyle(document.documentElement),HEX={};
['read','low','conflict','missing','override'].forEach(k=>HEX[k]=CSS.getPropertyValue('--'+k).trim()||'#888');
const tgt={x:0,y:0,z:0};let dist=50,theta=1.1,phi=0.9;
function st3(it){const e=byId[it.id];return e?status(e):(it.s||'read')}
function place3(){cam.position.set(tgt.x+dist*Math.sin(phi)*Math.cos(theta),tgt.y+dist*Math.cos(phi),tgt.z+dist*Math.sin(phi)*Math.sin(theta));
 cam.lookAt(tgt.x,tgt.y,tgt.z)}
function size3(){const r=$('canvas').getBoundingClientRect();if(r3.domElement.width!==Math.round(r.width*devicePixelRatio)||
 r3.domElement.height!==Math.round(r.height*devicePixelRatio)){r3.setSize(r.width,r.height);cam.aspect=r.width/Math.max(r.height,1);cam.updateProjectionMatrix()}}
const MM3=v=>v/1000;
function mesh3(geo,it){const m=new THREE.Mesh(geo,new THREE.MeshLambertMaterial({color:HEX.read,transparent:true}));m.userData=it;return m}
function box3(cx,cy,w,d,rot,z0,z1,it){const m=mesh3(new THREE.BoxGeometry(MM3(w),Math.max(MM3(z1-z0),0.01),MM3(d)),it);
 m.position.set(MM3(cx),MM3((z0+z1)/2),-MM3(cy));m.rotation.y=(rot||0)*Math.PI/180;return m}
function cyl3(x,y,dia,z0,z1,it){const h=Math.max(MM3(z1-z0),0.01),m=mesh3(new THREE.CylinderGeometry(MM3(dia/2),MM3(dia/2),h,32),it);  // a round column
 m.position.set(MM3(x),MM3(z0)+h/2,-MM3(y));return m}
function bar3(a,b,w,z0,z1,it){const dx=b[0]-a[0],dy=b[1]-a[1];const m=box3((a[0]+b[0])/2,(a[1]+b[1])/2,Math.hypot(dx,dy),w,0,z0,z1,it);
 m.rotation.y=Math.atan2(dy,dx);return m}
function geo3(v,idx){const g=new THREE.BufferGeometry();g.setAttribute('position',new THREE.Float32BufferAttribute(v,3));
 g.setIndex(idx);g.computeVertexNormals();return g}
function slant3(a,b,w,za,zb,d,it){const dx=b[0]-a[0],dy=b[1]-a[1],L=Math.hypot(dx,dy)||1,nx=-dy/L*w/2,ny=dx/L*w/2;  // a sloping beam
 const c=[[a[0]-nx,a[1]-ny,za-d],[a[0]+nx,a[1]+ny,za-d],[b[0]+nx,b[1]+ny,zb-d],[b[0]-nx,b[1]-ny,zb-d],
  [a[0]-nx,a[1]-ny,za],[a[0]+nx,a[1]+ny,za],[b[0]+nx,b[1]+ny,zb],[b[0]-nx,b[1]-ny,zb]],v=[];
 c.forEach(p=>v.push(MM3(p[0]),MM3(p[2]),-MM3(p[1])));
 const m=mesh3(geo3(v,[0,2,1,0,3,2,4,5,6,4,6,7,0,1,5,0,5,4,1,2,6,1,6,5,2,3,7,2,7,6,3,0,4,3,4,7]),it);m.material.side=THREE.DoubleSide;return m}
function slab3(pts,sl,top,th,it){const lo=Math.min(sl.p0,sl.p1),hi=Math.max(sl.p0,sl.p1);   // a sloping slab
 const z=p=>{let q=(p[0]-sl.o[0])*sl.d[0]+(p[1]-sl.o[1])*sl.d[1];q=Math.min(Math.max(q,lo),hi);return top+sl.z0+(q-sl.p0)*(sl.z1-sl.z0)/(sl.p1-sl.p0)};
 const n=pts.length,v=[];pts.forEach(p=>v.push(MM3(p[0]),MM3(z(p)),-MM3(p[1])));pts.forEach(p=>v.push(MM3(p[0]),MM3(z(p)-th),-MM3(p[1])));
 const idx=[];THREE.ShapeUtils.triangulateShape(pts.map(p=>new THREE.Vector2(p[0],p[1])),[]).forEach(t=>{idx.push(t[0],t[1],t[2],t[0]+n,t[2]+n,t[1]+n)});
 for(let i=0;i<n;i++){const j=(i+1)%n;idx.push(i,j,j+n,i,j+n,i+n)}
 const m=mesh3(geo3(v,idx),it);m.material.side=THREE.DoubleSide;return m}
function swept3(a,b,sec,z,it){const dx=b[0]-a[0],dy=b[1]-a[1],L=Math.hypot(dx,dy);       // a beam's drawn section along it
 const m=mesh3(new THREE.ExtrudeGeometry(new THREE.Shape(sec.map(p=>new THREE.Vector2(MM3(p[0]),MM3(p[1])))),{depth:Math.max(MM3(L),0.01),bevelEnabled:false}),it);
 const f=new THREE.Vector3(dx,0,-dy).normalize(),up=new THREE.Vector3(0,1,0);
 m.quaternion.setFromRotationMatrix(new THREE.Matrix4().makeBasis(new THREE.Vector3().crossVectors(up,f),up,f));
 m.position.set(MM3(a[0]),MM3(z),-MM3(a[1]));return m}
function prism3(pts,z0,z1,it){const sh=new THREE.Shape(pts.map(p=>new THREE.Vector2(MM3(p[0]),MM3(p[1]))));
 const m=mesh3(new THREE.ExtrudeGeometry(sh,{depth:Math.max(MM3(z1-z0),0.01),bevelEnabled:false}),it);m.rotation.x=-Math.PI/2;m.position.y=MM3(z0);return m}
function build3(i){if(grp){s3.remove(grp);grp.traverse(o=>{if(o.geometry)o.geometry.dispose();if(o.material)o.material.dispose()})}
 grp=new THREE.Group();meshes=[];cur3=i;const S=D3[i];$('d3set').value=i;
 for(const it of S.items){let m=null;
  // a column's rotation is the direction of its long side (depth); a footing's sizes are x and y
  if(it.t==='column')m=it.rd?cyl3(it.x,it.y,it.rd,it.z0,it.z1,it):box3(it.x,it.y,it.d,it.w,it.rot,it.z0,it.z1,it);
  else if(it.t==='footing')m=it.p?prism3(it.p,it.z0,it.z1,it):box3(it.x,it.y,it.w,it.d,it.rot,it.z0,it.z1,it);
  else if(it.t==='beam')m=it.sec?swept3(it.a,it.b,it.sec,it.z,it):it.p?prism3(it.p,it.z-it.d,it.z,it):
   (it.z2!==undefined&&Math.abs(it.z2-it.z)>1)?slant3(it.a,it.b,it.w,it.z,it.z2,it.d,it):bar3(it.a,it.b,it.w,it.z-it.d,it.z,it);
  else if(it.t==='wall')m=bar3(it.a,it.b,it.w,it.z0,it.z1,it);
  else if(it.t==='slab')m=it.sl?slab3(it.p,it.sl,it.z,it.th,it):prism3(it.p,it.z-it.th,it.z,it);
  else if(it.t==='stair')m=prism3(it.p,it.z0,it.z0+(it.z1-it.z0)/2,it);
  if(m){grp.add(m);meshes.push(m)}}
 grp.rotation.y=-domAng(S.items.filter(it=>it.t==='beam').map(it=>[it.a,it.b]))*Math.PI/180;
 s3.add(grp);grp.updateMatrixWorld(true);const bb=new THREE.Box3().setFromObject(grp),c=bb.getCenter(new THREE.Vector3());
 tgt.x=c.x;tgt.y=c.y;tgt.z=c.z;dist=(bb.getSize(new THREE.Vector3()).length()||10)*1.1;place3();paint3();
 $('d3note').innerHTML=(S.notes||[]).map(n=>'&bull; '+esc(n)).join('<br>')+'<br>Levels: '+S.levels.map(l=>esc(l.key)+' '+(l.z/1000).toFixed(2)+' m').join(', ')}
function paint3(){if(!r3)return;for(const m of meshes){const it=m.userData,on=sel&&sel.id===it.id;
 m.visible=shown3[it.t];m.material.color.set(HEX[st3(it)]||HEX.read);m.material.emissive.setHex(on?0x1f6fb2:0x000000);
 const base=it.t==='slab'?0.55:it.t==='footing'?0.85:1,dim=focusLv&&it.lv!==focusLv;
 m.material.opacity=dim?0.07:base;m.material.depthWrite=!dim&&base>=1}}
function init3(){if(r3)return true;
 if(typeof THREE==='undefined'){alert('The 3D view loads three.js from the internet - connect once and reopen this page.');return false}
 r3=new THREE.WebGLRenderer({antialias:true});r3.setPixelRatio(devicePixelRatio);$('gl').appendChild(r3.domElement);
 s3=new THREE.Scene();s3.background=new THREE.Color(0xfbfaf7);cam=new THREE.PerspectiveCamera(45,1,0.1,5000);
 s3.add(new THREE.HemisphereLight(0xffffff,0x9aa4ad,1.1));const sun=new THREE.DirectionalLight(0xffffff,0.9);sun.position.set(1,2,1.5);s3.add(sun);
 const el=r3.domElement,ray=new THREE.Raycaster(),mouse=new THREE.Vector2();let dr=null,moved=0;
 const hit=ev=>{const r=el.getBoundingClientRect();mouse.set((ev.clientX-r.left)/r.width*2-1,-(ev.clientY-r.top)/r.height*2+1);
  ray.setFromCamera(mouse,cam);const h=ray.intersectObjects(meshes.filter(m=>m.visible&&m.material.opacity>0.2))[0];return h?h.object.userData:null};
 el.addEventListener('contextmenu',ev=>ev.preventDefault());
 el.addEventListener('pointerdown',ev=>{dr={x:ev.clientX,y:ev.clientY,b:ev.button};moved=0});
 el.addEventListener('pointerup',ev=>{if(dr&&moved<4){const it=hit(ev);if(it&&byId[it.id])pick(byId[it.id])}dr=null});
 el.addEventListener('pointermove',ev=>{if(dr){const dx=ev.clientX-dr.x,dy=ev.clientY-dr.y;dr.x=ev.clientX;dr.y=ev.clientY;moved+=Math.abs(dx)+Math.abs(dy);
   if(dr.b===2||ev.shiftKey){const k=dist/600,f=new THREE.Vector3();cam.getWorldDirection(f);
    const rt=new THREE.Vector3().crossVectors(f,cam.up).normalize(),up=new THREE.Vector3().crossVectors(rt,f).normalize();
    tgt.x+=-dx*k*rt.x+dy*k*up.x;tgt.y+=-dx*k*rt.y+dy*k*up.y;tgt.z+=-dx*k*rt.z+dy*k*up.z}
   else{theta+=dx*0.005;phi=Math.min(Math.max(phi-dy*0.005,0.05),3.09)}place3();$('tip').style.display='none';return}
  const it=hit(ev);if(!it){$('tip').style.display='none';return}
  if(byId[it.id])tip(ev,byId[it.id]);else{const t=$('tip'),r=$('canvas').getBoundingClientRect();
   t.textContent=`${it.m||'(no mark)'} · ${it.t} · ${it.info}`;t.style.left=(ev.clientX-r.left+12)+'px';t.style.top=(ev.clientY-r.top+12)+'px';t.style.display='block'}});
 el.addEventListener('wheel',ev=>{ev.preventDefault();dist*=Math.exp(ev.deltaY*0.001);place3()},{passive:false});
 $('d3set').innerHTML=D3.map((s,i)=>`<option value="${i}">${esc(s.set)}</option>`).join('');
 $('d3set').onchange=ev=>{focusLv=null;build3(+ev.target.value)};
 $('d3types').innerHTML=TY.map(t=>`<label><input type="checkbox" checked data-t="${t}"> ${t}s</label>`).join('');
 document.querySelectorAll('#d3types input').forEach(c=>c.onchange=()=>{shown3[c.dataset.t]=c.checked;paint3()});
 $('d3all').onclick=()=>{focusLv=null;paint3()};
 (function loop(){requestAnimationFrame(loop);if(mode==='3d'){size3();r3.render(s3,cam)}})();
 return true}
function setMode(m){if(m==='3d'){if(!D3.length){alert('No plan in these drawings could be put together in 3D.');return}
  if(!init3())return}
 mode=m;$('m2d').classList.toggle('on',m==='2d');$('m3d').classList.toggle('on',m==='3d');
 $('svg').style.display=m==='3d'?'none':'block';$('gl').style.display=m==='3d'?'block':'none';
 $('d3note').style.display=$('d3bar').style.display=m==='3d'?'':'none';$('lvbar').style.display=m==='3d'?'none':'';
 $('hint').textContent=m==='3d'?'click an element to see every source · drag: orbit · right-drag: pan · wheel: zoom · pick a level on the left to isolate it'
  :'click an element to see every source · scroll to zoom · drag to pan';
 if(m==='3d'){const i=D3.findIndex(s=>cur&&s.set===cur.set);if(i!==cur3)build3(i>=0?i:0);else paint3()}}
$('m2d').onclick=()=>setMode('2d');$('m3d').onclick=()=>setMode('3d');
// plans, picks and decisions also drive the 3D view
const show2d=show,pick2d=pick,after2d=after;
show=function(s,f){show2d(s,f);if(mode==='3d'){const i=D3.findIndex(x=>x.set===s.set);if(i>=0&&i!==cur3)build3(i);focusLv=s.lv;paint3()}};
pick=function(e){pick2d(e);if(mode==='3d')paint3()};
after=function(e){after2d(e);if(mode==='3d')paint3()};
const want=decodeURIComponent((location.hash.match(/^#el=(.+)$/)||[])[1]||'');
if(want&&byId[want]){const e=byId[want];show(D.sheets.find(s=>s.id==e.sheet),e);pick(e)}
else if(D.sheets.length)show(D.sheets[0]);
</script></body></html>"""


def write(b: Building, path: str, decisions: dict | None = None) -> str:
    """decisions: the project decisions (overrides.json "project"); its floor heights
    place the 3D view's levels as the Revit build does."""
    payload = json.dumps(data(b, decisions, os.path.dirname(os.path.abspath(path))),
                         separators=(",", ":")).replace("</", "<\\/")
    with open(path, "w", encoding="utf-8") as f:
        f.write(PAGE.replace("__DATA__", payload))
    return path


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("building")
    ap.add_argument("out", nargs="?")
    a = ap.parse_args(argv)
    b = Building.load(a.building)
    out = a.out or os.path.join(os.path.dirname(a.building), "review.html")
    print(f"Wrote {write(b, out)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
