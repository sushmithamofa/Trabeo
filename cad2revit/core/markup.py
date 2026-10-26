"""Mark the audit up on a COPY of the consultant's own drawing.

The drawing comes through untouched, with revision clouds, numbered bubbles, the
comments and a query schedule added on their own `S-QUERY-*` layers. It opens as a
normal drawing with the markup on top, so it can be read, printed or sent straight
back to the consultant. Their original file is never modified.
"""
from __future__ import annotations

import math
import textwrap

import ezdxf

from .model import BLOCKER, INFO, WARNING, Result

LAYERS = {
    BLOCKER: ("S-QUERY-BLOCKER", 1),     # red
    WARNING: ("S-QUERY-WARNING", 30),    # orange
    INFO: ("S-QUERY-NOTE", 4),           # cyan
}
TEXT_LAYER = "S-QUERY-TEXT"
LABELS = {BLOCKER: "MUST FIX", WARNING: "CHECK", INFO: "NOTE"}

CLOUD_RADIUS = 900.0        # mm, sized to read at plan scale
BUBBLE_RADIUS = 450.0
NUM_HEIGHT = 400.0
SCHED_HEIGHT = 320.0
SCHED_LINE = 520.0
SCHED_WRAP = 95             # characters per line in the schedule

COMMENT_HEIGHT = 260.0      # on-plan comment text
COMMENT_WRAP = 46           # characters per line beside the cloud
COMMENT_MAX_LINES = 4       # the rest goes to the schedule
COMMENT_STEP = 1600.0       # how far to move when looking for clear space


def _cloud(msp, cx: float, cy: float, r: float, layer: str, arcs: int = 11) -> None:
    """A revision cloud: a closed polyline whose every segment bulges outward."""
    pts = []
    for i in range(arcs):
        a = 2 * math.pi * i / arcs
        # bulge 0.55 gives a rounded scallop rather than a half circle
        pts.append((cx + r * math.cos(a), cy + r * math.sin(a), 0.0, 0.0, 0.55))
    msp.add_lwpolyline(pts, format="xyseb", close=True, dxfattribs={"layer": layer})


def _text(msp, s: str, x: float, y: float, h: float, layer: str, *, bold_gap=0.0):
    t = msp.add_text(s, dxfattribs={"layer": layer, "height": h})
    t.set_placement((x, y))
    return t


def write_markup_dxf(res: Result, out_path: str, source_dxf: str,
                     comment_on: tuple[str, ...] = (BLOCKER,)) -> str:
    """Add the markup to a copy of `source_dxf` and save it as `out_path`.

    comment_on picks which severities get their text written out on the plan.
    Everything gets a cloud and a number regardless, and the query schedule
    carries the full wording for all of them.
    """
    doc = ezdxf.readfile(source_dxf)          # the consultant's drawing, as drawn
    msp = doc.modelspace()

    for sev, (name, colour) in LAYERS.items():
        if name not in doc.layers:
            doc.layers.add(name, color=colour)
    if TEXT_LAYER not in doc.layers:
        doc.layers.add(TEXT_LAYER, color=7)

    order = {BLOCKER: 0, WARNING: 1, INFO: 2}
    issues = sorted(res.issues, key=lambda i: (order.get(i.severity, 3), i.category))

    located = [i for i in issues if i.x is not None and i.y is not None]
    general = [i for i in issues if i.x is None or i.y is None]

    # ---- clouds, numbered bubbles and the comment beside each one ----
    placed: list[tuple[float, float, float, float]] = []   # boxes already used

    def free_spot(x: float, y: float, w: float, h: float):
        """Find a clear place for a comment near (x, y), spiralling outwards."""
        for ring in range(0, 14):
            for dx, dy in ((1, 1), (1, -1), (-1, 1), (-1, -1), (1, 0), (-1, 0), (0, 1), (0, -1)):
                cx = x + dx * (CLOUD_RADIUS * 1.6 + ring * COMMENT_STEP)
                cy = y + dy * (CLOUD_RADIUS * 1.2 + ring * COMMENT_STEP * 0.6)
                bx = cx if dx > 0 else cx - w
                box = (bx, cy - h, bx + w, cy)
                if not any(box[0] < p[2] and p[0] < box[2]
                           and box[1] < p[3] and p[1] < box[3] for p in placed):
                    placed.append(box)
                    return bx, cy
        placed.append((x, y - h, x + w, y))
        return x, y

    # Every located issue gets a cloud and a number, which is small and never
    # clutters. Written-out comments go only on the ones worth reading in place:
    # a plan carrying a hundred paragraphs cannot be read at all, and the rest are
    # the same rule broken repeatedly, which the schedule states once.
    for n, issue in enumerate(located, start=1):
        layer = LAYERS.get(issue.severity, LAYERS[INFO])[0]
        _cloud(msp, issue.x, issue.y, CLOUD_RADIUS, layer)
        msp.add_circle((issue.x, issue.y), BUBBLE_RADIUS, dxfattribs={"layer": layer})
        t = msp.add_text(str(n), dxfattribs={"layer": layer, "height": NUM_HEIGHT})
        t.set_placement((issue.x, issue.y), align=ezdxf.enums.TextEntityAlignment.MIDDLE_CENTER)

        if issue.severity not in comment_on:
            continue

        head = f"{n}. {LABELS.get(issue.severity, '')}" + (f" - {issue.mark}" if issue.mark else "")
        wrapped = textwrap.wrap(issue.message, COMMENT_WRAP)
        lines = [head] + wrapped[:COMMENT_MAX_LINES]
        if len(wrapped) > COMMENT_MAX_LINES:
            lines[-1] = lines[-1].rstrip(".,;") + " ... (see schedule)"

        w = COMMENT_WRAP * COMMENT_HEIGHT * 0.62
        h = len(lines) * COMMENT_HEIGHT * 1.55
        tx, ty = free_spot(issue.x, issue.y, w, h)

        # leader from the cloud to the comment
        msp.add_lwpolyline(
            [(issue.x, issue.y), (tx if tx > issue.x else tx + w, ty - h * 0.5)],
            dxfattribs={"layer": layer})

        yy = ty
        for k, line in enumerate(lines):
            _text(msp, line, tx, yy, COMMENT_HEIGHT,
                  layer if k == 0 else TEXT_LAYER)
            yy -= COMMENT_HEIGHT * 1.55

    # ---- query schedule, clear of everything already on the sheet ----
    try:
        ext = doc.header.get("$EXTMAX"), doc.header.get("$EXTMIN")
        sx = float(ext[0][0]) + 6000.0
        sy = float(ext[0][1])
    except Exception:
        xs = [i.x for i in located] or [0.0]
        ys = [i.y for i in located] or [0.0]
        sx, sy = max(xs) + 8000.0, max(ys)

    y = sy
    _text(msp, "STRUCTURAL CAD QUERY SCHEDULE", sx, y, SCHED_HEIGHT * 2.0, TEXT_LAYER)
    y -= SCHED_LINE * 2.2
    counts = res.issue_counts()
    _text(msp, f"{counts.get(BLOCKER, 0)} MUST FIX   "
               f"{counts.get(WARNING, 0)} CHECK   {counts.get(INFO, 0)} NOTE",
          sx, y, SCHED_HEIGHT * 1.3, TEXT_LAYER)
    y -= SCHED_LINE * 1.6
    _text(msp, "MUST FIX = cannot be modelled until the drawing changes.", sx, y,
          SCHED_HEIGHT, TEXT_LAYER)
    y -= SCHED_LINE
    _text(msp, "CHECK    = modelled on an assumption that needs confirming.", sx, y,
          SCHED_HEIGHT, TEXT_LAYER)
    y -= SCHED_LINE
    _text(msp, "NOTE     = for information.", sx, y, SCHED_HEIGHT, TEXT_LAYER)
    y -= SCHED_LINE * 2.0

    if located:
        _text(msp, "CLOUDED ON THE PLAN", sx, y, SCHED_HEIGHT * 1.4, TEXT_LAYER)
        y -= SCHED_LINE * 1.6
    for n, issue in enumerate(located, start=1):
        layer = LAYERS.get(issue.severity, LAYERS[INFO])[0]
        head = f"{n}.  [{LABELS.get(issue.severity, '')}]" + (f"  {issue.mark}" if issue.mark else "")
        _text(msp, head, sx, y, SCHED_HEIGHT, layer)
        y -= SCHED_LINE
        for line in textwrap.wrap(issue.message, SCHED_WRAP):
            _text(msp, "     " + line, sx, y, SCHED_HEIGHT, TEXT_LAYER)
            y -= SCHED_LINE
        y -= SCHED_LINE * 0.4

    if general:
        y -= SCHED_LINE
        _text(msp, "GENERAL, NOT TIED TO ONE PLACE", sx, y, SCHED_HEIGHT * 1.4, TEXT_LAYER)
        y -= SCHED_LINE * 1.6
        for n, issue in enumerate(general, start=len(located) + 1):
            layer = LAYERS.get(issue.severity, LAYERS[INFO])[0]
            head = f"{n}.  [{LABELS.get(issue.severity, '')}]" + (f"  {issue.layer}" if issue.layer else "")
            _text(msp, head, sx, y, SCHED_HEIGHT, layer)
            y -= SCHED_LINE
            for line in textwrap.wrap(issue.message, SCHED_WRAP):
                _text(msp, "     " + line, sx, y, SCHED_HEIGHT, TEXT_LAYER)
                y -= SCHED_LINE
            y -= SCHED_LINE * 0.4

    doc.saveas(out_path)
    return out_path
