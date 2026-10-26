"""Write the outputs: CSV per element type, one JSON, and an HTML audit."""
from __future__ import annotations

import csv
import html
import json
import os
from datetime import datetime

from . import geometry as G
from .model import BLOCKER, INFO, WARNING, Result


def _w(path: str, rows: list[dict], fields: list[str]) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        wr = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        wr.writeheader()
        wr.writerows(rows)


def write_csvs(res: Result, out_dir: str) -> list[str]:
    ox, oy = res.base_point
    made = []

    rows = [{
        "mark": c.mark, "type": c.kind,
        "x_local": round(c.x - ox, 1), "y_local": round(c.y - oy, 1),
        "width_mm": c.width, "depth_mm": c.depth, "rotation_deg": c.rotation,
        "x_drawing": round(c.x, 1), "y_drawing": round(c.y, 1),
        "source_layer": c.source_layer, "tag_distance_mm": c.tag_distance,
        "outline_repaired": "yes" if c.repaired else "",
        "notes": "; ".join(c.notes), "handle": c.handle,
    } for c in sorted(res.columns, key=lambda c: c.mark)]
    p = os.path.join(out_dir, "columns.csv")
    _w(p, rows, ["mark", "type", "x_local", "y_local", "width_mm", "depth_mm",
                 "rotation_deg", "x_drawing", "y_drawing", "source_layer",
                 "tag_distance_mm", "outline_repaired", "notes", "handle"])
    made.append(p)

    rows = [{
        "mark": b.mark, "type": b.kind,
        "x1_local": round(b.start[0] - ox, 1), "y1_local": round(b.start[1] - oy, 1),
        "x2_local": round(b.end[0] - ox, 1), "y2_local": round(b.end[1] - oy, 1),
        "length_mm": round(b.length, 1),
        "width_mm": b.width if b.width else "",
        "width_source": b.width_source,
        "depth_mm": b.depth if b.depth else "",
        "depth_source": b.depth_source,
        "centreline_source": b.centreline_source,
        "offset_applied_mm": b.offset_applied,
        "source_layer": b.source_layer, "tag_distance_mm": b.tag_distance,
        "notes": "; ".join(b.notes), "handle": b.handle,
    } for b in sorted(res.beams, key=lambda b: (b.kind, b.mark))]
    p = os.path.join(out_dir, "beams.csv")
    _w(p, rows, ["mark", "type", "x1_local", "y1_local", "x2_local", "y2_local",
                 "length_mm", "width_mm", "width_source", "depth_mm", "depth_source",
                 "centreline_source",
                 "offset_applied_mm", "source_layer", "tag_distance_mm", "notes",
                 "handle"])
    made.append(p)

    rows = []
    for w in sorted(res.walls, key=lambda w: w.mark):
        rows.append({
            "mark": w.mark, "thickness_mm": w.thickness if w.thickness else "",
            "x1_local": round(w.start[0] - ox, 1) if w.start else "",
            "y1_local": round(w.start[1] - oy, 1) if w.start else "",
            "x2_local": round(w.end[0] - ox, 1) if w.end else "",
            "y2_local": round(w.end[1] - oy, 1) if w.end else "",
            "vertices": len(w.outline), "source_layer": w.source_layer,
            "tag_distance_mm": w.tag_distance, "notes": "; ".join(w.notes),
            "handle": w.handle})
    p = os.path.join(out_dir, "walls.csv")
    _w(p, rows, ["mark", "thickness_mm", "x1_local", "y1_local", "x2_local",
                 "y2_local", "vertices", "source_layer", "tag_distance_mm",
                 "notes", "handle"])
    made.append(p)

    rows = [{"mark": t.mark, "x_local": round(t.x - ox, 1),
             "y_local": round(t.y - oy, 1),
             "thickness_mm": t.thickness if t.thickness else "",
             "thickness_source": t.thickness_source,
             "source_layer": t.source_layer,
             "handle": t.handle} for t in sorted(res.slab_tags, key=lambda t: t.mark)]
    p = os.path.join(out_dir, "slab_tags.csv")
    _w(p, rows, ["mark", "x_local", "y_local", "thickness_mm", "thickness_source",
                 "source_layer", "handle"])
    made.append(p)

    rows = []
    for i, z in enumerate(res.sunk, 1):
        cx = sum(p_[0] for p_ in z.outline) / len(z.outline)
        cy = sum(p_[1] for p_ in z.outline) / len(z.outline)
        rows.append({"id": i, "depth_mm": z.depth_mm if z.depth_mm else "",
                     "depth_source": z.depth_source,
                     "centre_x_local": round(cx - ox, 1),
                     "centre_y_local": round(cy - oy, 1),
                     "vertices": len(z.outline), "source_layer": z.source_layer,
                     "handle": z.handle})
    p = os.path.join(out_dir, "sunk_zones.csv")
    _w(p, rows, ["id", "depth_mm", "depth_source", "centre_x_local",
                 "centre_y_local", "vertices", "source_layer", "handle"])
    made.append(p)

    p = os.path.join(out_dir, "issues.csv")
    _w(p, [i.as_row() for i in res.issues],
       ["severity", "category", "mark", "layer", "message", "x", "y", "handle"])
    made.append(p)
    return made


def write_json(res: Result, out_dir: str) -> str:
    p = os.path.join(out_dir, "model.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(res.to_dict(), f, indent=1)
    return p


# --------------------------------------------------------------------------

CSS = """
:root{--ink:#16202B;--paper:#F7F6F2;--panel:#FDFCF9;--line:#DCD9D0;
--blue:#1F6FB2;--ochre:#A4500F;--mute:#4C5A66}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--paper);color:var(--ink);font:15px/1.55 'Segoe UI',system-ui,sans-serif;padding:40px}
.wrap{max-width:1100px;margin:0 auto}
h1{font-size:28px;margin-bottom:6px}
h2{font-size:19px;margin:34px 0 12px;color:var(--blue)}
.sub{color:var(--mute);margin-bottom:24px;font-size:14px}
.cards{display:flex;flex-wrap:wrap;gap:14px;margin-bottom:10px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;
padding:16px 20px;min-width:140px}
.card .n{font-size:28px;font-weight:600;font-variant-numeric:tabular-nums}
.card .l{font-size:13px;color:var(--mute);text-transform:uppercase;letter-spacing:.5px}
.card.blocker .n{color:#B3261E}.card.warning .n{color:var(--ochre)}
.card.info .n{color:var(--blue)}
table{border-collapse:collapse;width:100%;background:var(--panel);
border:1px solid var(--line);border-radius:8px;overflow:hidden;font-size:14px}
th{background:var(--ink);color:var(--paper);text-align:left;padding:9px 12px;font-weight:600}
td{padding:9px 12px;border-bottom:1px solid var(--line);vertical-align:top}
tr:last-child td{border-bottom:0}
.sev{font-weight:600;white-space:nowrap}
.sev.blocker{color:#B3261E}.sev.warning{color:var(--ochre)}.sev.info{color:var(--blue)}
code{background:#EFEEE9;padding:1px 5px;border-radius:3px;font-size:13px}
.ok{background:#E8F3EA;border:1px solid #BBD9C2;border-radius:8px;padding:14px 18px}
.foot{margin-top:40px;color:var(--mute);font-size:13px;border-top:1px solid var(--line);padding-top:14px}
"""


def plan_svg(res, width_px: int = 1040) -> str:
    """A plan of what was extracted, with every doubtful beam picked out.

    The point of this is that a mistake in the geometry is obvious at a glance
    and almost invisible in a table of numbers. A beam left hanging in the middle
    of a room, or one that stops short of its support, shows up here immediately,
    so it can be found before the model is built rather than after.

    Plain SVG, no libraries, so it works in any browser straight from disk.
    """
    import math

    beams, cols, walls = res.beams, res.columns, res.walls
    pts = [p for b in beams for p in (b.start, b.end)]
    pts += [p for c in cols for p in (c.outline or [])]
    pts += [p for w in walls for p in (w.outline or [])]
    if not pts:
        return ""

    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    pad = max(x1 - x0, y1 - y0) * 0.03 + 500.0
    x0, x1, y0, y1 = x0 - pad, x1 + pad, y0 - pad, y1 + pad
    w_mm, h_mm = x1 - x0, y1 - y0
    k = width_px / w_mm
    h_px = h_mm * k

    def X(x):
        return (x - x0) * k

    def Y(y):
        return (y1 - y) * k           # SVG y grows downward

    # which ends touch nothing: the same test the extractor reports on
    def seg_d(p, a, b):
        dx, dy = b[0] - a[0], b[1] - a[1]
        L = dx * dx + dy * dy
        t = 0.0 if L == 0 else max(0.0, min(1.0, ((p[0]-a[0])*dx + (p[1]-a[1])*dy) / L))
        return math.hypot(p[0] - (a[0] + t*dx), p[1] - (a[1] + t*dy))

    outlines = [c.outline for c in cols if len(c.outline or []) >= 3]
    outlines += [w.outline for w in walls if len(w.outline or []) >= 3]
    segs = [(b.start, b.end) for b in beams]

    def free(p, skip):
        for k2, (a, b) in enumerate(segs):
            if k2 != skip and seg_d(p, a, b) < 1.0:
                return False
        return not any(G.point_in_polygon(p, o) for o in outlines)

    out = [f'<svg viewBox="0 0 {width_px:.0f} {h_px:.0f}" width="100%" '
           f'style="max-width:{width_px}px;background:#FDFCF9;border:1px solid #DCD9D0;'
           'border-radius:8px">']

    for w in walls:
        if len(w.outline or []) >= 3:
            d = " ".join(f"{X(p[0]):.1f},{Y(p[1]):.1f}" for p in w.outline)
            out.append(f'<polygon points="{d}" fill="#B9B4A8" stroke="#6E6960" '
                       'stroke-width="0.6"/>')
    for c in cols:
        if len(c.outline or []) >= 3:
            d = " ".join(f"{X(p[0]):.1f},{Y(p[1]):.1f}" for p in c.outline)
            out.append(f'<polygon points="{d}" fill="#16202B" stroke="#16202B" '
                       'stroke-width="0.6"/>')

    loose = 0
    for i, b in enumerate(beams):
        wdt = max((b.width or 230.0) * k, 1.2)
        f1, f2 = free(b.start, i), free(b.end, i)
        if f1 or f2:
            colr, loose = "#B3261E", loose + 1
        elif b.length < 1200.0:
            colr = "#A4500F"
        else:
            colr = "#1F6FB2"
        out.append(f'<line x1="{X(b.start[0]):.1f}" y1="{Y(b.start[1]):.1f}" '
                   f'x2="{X(b.end[0]):.1f}" y2="{Y(b.end[1]):.1f}" stroke="{colr}" '
                   f'stroke-width="{wdt:.2f}" stroke-linecap="butt" opacity="0.92">'
                   f'<title>{html.escape(b.mark or "untagged")} &#183; '
                   f'{b.length:.0f} long &#183; {b.width or "?"} wide &#183; '
                   f'{b.depth or "no depth"} deep</title></line>')
        for p, isfree in ((b.start, f1), (b.end, f2)):
            if isfree:
                out.append(f'<circle cx="{X(p[0]):.1f}" cy="{Y(p[1]):.1f}" r="4" '
                           'fill="none" stroke="#B3261E" stroke-width="1.4"/>')

    out.append("</svg>")
    legend = (
        '<p class="sub" style="margin-top:10px">'
        '<span style="color:#1F6FB2">&#9632;</span> beam &middot; '
        '<span style="color:#A4500F">&#9632;</span> under 1.2 m &middot; '
        f'<span style="color:#B3261E">&#9632;</span> an end touching nothing ({loose} '
        'beams, ringed) &middot; '
        '<span style="color:#16202B">&#9632;</span> column &middot; '
        '<span style="color:#B9B4A8">&#9632;</span> wall. '
        'Hover any beam for its mark and size.</p>')
    return "".join(out) + legend

def write_html(res: Result, out_dir: str, outputs: list[str]) -> str:
    e = html.escape
    counts = res.counts()
    ic = res.issue_counts()

    def cards(d, cls=""):
        return "".join(
            f'<div class="card {cls}"><div class="n">{v}</div>'
            f'<div class="l">{e(k)}</div></div>' for k, v in d.items())

    rows = []
    order = {BLOCKER: 0, WARNING: 1, INFO: 2}
    for i in sorted(res.issues, key=lambda i: (order.get(i.severity, 3), i.category)):
        loc = f"<code>{i.x:.0f}, {i.y:.0f}</code>" if i.x is not None else ""
        rows.append(
            f"<tr><td class='sev {i.severity}'>{i.severity}</td>"
            f"<td>{e(i.category)}</td><td>{e(i.mark)}</td>"
            f"<td>{e(i.message)}</td><td>{loc}</td></tr>")

    lay = "".join(
        f"<tr><td><code>{e(k)}</code></td><td>{e(v)}</td></tr>"
        for k, v in sorted(res.layer_map.items(), key=lambda kv: (kv[1], kv[0]))
        if v != "ignore")

    files = "".join(f"<li><code>{e(os.path.basename(p))}</code></li>" for p in outputs)

    plan = plan_svg(res)

    body = f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<title>CAD to Revit extraction report</title><style>{CSS}</style></head><body><div class="wrap">
<h1>CAD to Revit extraction report</h1>
<div class="sub">{e(os.path.basename(res.source_file))} &middot; {e(res.dxf_version)},
{e(res.units)} &middot; run {datetime.now():%d %B %Y, %H:%M}</div>

<h2>What came out</h2>
<div class="cards">{cards(counts)}</div>

<h2>The plan as extracted</h2>
<p class="sub">Check this before building. A beam left hanging in the middle of a room,
or one stopping short of its support, is obvious here and nearly invisible in a table.</p>
{plan}

<h2>What needs attention</h2>
<div class="cards">{cards({'blockers': ic.get(BLOCKER,0)}, 'blocker')}
{cards({'warnings': ic.get(WARNING,0)}, 'warning')}
{cards({'notes': ic.get(INFO,0)}, 'info')}</div>
<p class="sub">A <strong>blocker</strong> means that element cannot be modelled until the
drawing changes. A <strong>warning</strong> means it was modelled on an assumption that
should be checked. A <strong>note</strong> is for information.</p>
{'<div class="ok">Nothing was flagged. Every element resolved cleanly.</div>' if not rows else
 '<table><thead><tr><th>Severity</th><th>Area</th><th>Mark</th><th>What it means</th><th>At</th></tr></thead><tbody>' + ''.join(rows) + '</tbody></table>'}

<h2>How the layers were read</h2>
<table><thead><tr><th>Drawing layer</th><th>Read as</th></tr></thead><tbody>{lay}</tbody></table>
<p class="sub">Layers shown as <code>unmapped</code> were skipped. Add them to
<code>config.json</code> if they hold structure.</p>

<h2>Files written</h2>
<ul>{files}</ul>

<div class="foot">Local coordinates are measured from the project base point at
({res.base_point[0]:.0f}, {res.base_point[1]:.0f}) in drawing units. The source drawing
was not modified.</div>
</div></body></html>"""

    p = os.path.join(out_dir, "report.html")
    with open(p, "w", encoding="utf-8") as f:
        f.write(body)
    return p
