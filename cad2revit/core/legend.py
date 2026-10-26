"""Keep the drawing's own legend out of the model, and read what it says.

There is no built-in legend of any office or consultant: every legend is read
from the drawings themselves - a block under a heading (find_boxes), a column of
captions with no heading (find_caption_columns), or a legend sheet imported with
the drawings (samples_from_file, used for the plans whose own legend does not
explain a hatch).

A legend is a column of little sample shapes - a box captioned COLUMN, another
captioned BEAM, one per sunk depth - drawn on the very layers it is describing.
To a layer-driven extractor they are indistinguishable from structure, so they
arrive in Revit as a tight stack of columns standing on their own beside the
building, which is exactly what they look like in a 3D view.

On the Reis Magos C-wing sheet the legend contributed 7 columns and 1 beam.

The block is found from the drawing rather than from a hardcoded rectangle: a
heading such as LEGEND, the column of captions beneath it, and the bounding box
those texts occupy, padded to take in the samples beside them. If no heading is
found nothing is excluded. Whatever is excluded is reported with its position
and counts, so it can be checked and, if it was wrong, switched off.
"""
from __future__ import annotations

import collections
import re

from .model import INFO, WARNING, Result

DEFAULTS = {
    "detect": True,
    "headings": ["LEGEND", "KEY", "SYMBOLS", "ABBREVIATION", "ABBREVIATIONS"],
    "label_x_window_mm": 8000.0,    # how far right of the heading a caption may sit
    "label_x_left_mm": 2000.0,      # and how far left, which is much less
    "label_y_reach_mm": 30000.0,    # how far down the captions may run
    "label_gap_mm": 5000.0,         # a bigger vertical gap ends the legend
    "heading_max_chars": 25,        # a heading is a word, not a sentence
    "min_labels": 3,                # fewer than this is not a legend
    "pad_mm": 1200.0,               # to take in the samples beside the captions
    "max_box_mm": 40000.0,          # a box bigger than this is not a legend
    "min_sunk_captions": 3,         # a legend with no heading: this many 'N MM SUNK' captions
    "caption_align_mm": 50.0,       #   lined up within this, one above another
    "sample_reach_mm": 7000.0,      #   and its samples within this of the captions
    "exclude_zones": [],            # [[x1, y1, x2, y2], ...] drawing coordinates
}


def settings(cfg: dict) -> dict:
    s = dict(DEFAULTS)
    s.update(cfg.get("legend") or {})
    return s


def _text_of(e) -> str:
    try:
        t = e.dxf.text if e.dxftype() == "TEXT" else e.plain_text()
    except Exception:
        return ""
    # %%u and friends are CAD formatting codes, not part of the words
    return re.sub(r"%%[a-zA-Z]", "", str(t)).strip()


def _point(e):
    """One representative point for an entity, or None if it has no obvious one."""
    t = e.dxftype()
    try:
        if t in ("TEXT", "MTEXT", "INSERT"):
            p = e.dxf.insert
            return float(p[0]), float(p[1])
        if t == "LINE":
            a, b = e.dxf.start, e.dxf.end
            return (float(a[0]) + float(b[0])) / 2.0, (float(a[1]) + float(b[1])) / 2.0
        if t in ("CIRCLE", "ARC"):
            p = e.dxf.center
            return float(p[0]), float(p[1])
        if t == "HATCH":            # a legend's sample hatch must go with its legend
            b = hatch_box(e)
            return ((b[0] + b[2]) / 2.0, (b[1] + b[3]) / 2.0) if b else None
        if t == "LWPOLYLINE":
            pts = [(float(p[0]), float(p[1])) for p in e.get_points("xy")]
        elif t == "POLYLINE":
            pts = [(float(v.dxf.location[0]), float(v.dxf.location[1])) for v in e.vertices]
        elif t == "MLINE":
            pts = [(float(v[0]), float(v[1])) for v in e.get_locations()]
        else:
            return None
        if not pts:
            return None
        return (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))
    except Exception:
        return None


def find_boxes(entities, s: dict) -> list[tuple[float, float, float, float, str]]:
    """Bounding boxes of the legend blocks on the sheet, from their own text."""
    texts = []
    for e in entities:
        if e.dxftype() not in ("TEXT", "MTEXT"):
            continue
        t = _text_of(e)
        if not t:
            continue
        p = _point(e)
        if p:
            texts.append((p[0], p[1], t.upper()))

    # A heading is a word on its own - 'LEGEND-:' - not a sentence that happens to
    # contain it. Without that, a note saying "see the legend" starts a legend.
    wanted = {str(h).upper() for h in s["headings"]}
    heads = []
    for x, y, t in texts:
        if len(t) > s["heading_max_chars"]:
            continue
        words = set(re.split(r"[^A-Z]+", t)) - {""}
        if words & wanted:
            heads.append((x, y, t))

    boxes = []
    for hx, hy, ht in heads:
        near = [(x, y) for x, y, _t in texts
                if hx - s["label_x_left_mm"] <= x <= hx + s["label_x_window_mm"]
                and hy - s["label_y_reach_mm"] <= y <= hy]
        # walk down from the heading and stop at the first real gap, so a
        # schedule further down the sheet is not swept in with the captions
        labels, last = [], hy
        for p in sorted(near, key=lambda p: -p[1]):
            if last - p[1] > s["label_gap_mm"]:
                break
            labels.append(p)
            last = p[1]
        if len(labels) < s["min_labels"]:
            continue
        xs = [hx] + [p[0] for p in labels]
        ys = [hy] + [p[1] for p in labels]
        pad = s["pad_mm"]
        x1, y1 = min(xs) - pad, min(ys) - pad
        x2, y2 = max(xs) + pad, max(ys) + pad
        if (x2 - x1) > s["max_box_mm"] or (y2 - y1) > s["max_box_mm"]:
            continue
        boxes.append((x1, y1, x2, y2, ht.strip(" -:")))
    return boxes


_SUNK_CAPTION = re.compile(r"(\d+(?:\.\d+)?)\s*MM\s*SUNK", re.I)


def find_caption_columns(entities, s: dict) -> list[tuple[float, float, float, float, str]]:
    """Legend blocks with no heading, read from the drawing itself.

    Many legends - an office's standard sheet pasted beside the plan, a
    consultant's key - carry no LEGEND heading: just a column of captions
    (100MM SUNK, 150MM SUNK ... COLUMN, BEAM), one above another, with a sample
    drawn beside each. find_boxes() cannot see them, and their samples would be
    read as a real sunk hatch each, their sample beam as a beam.

    Found by what a legend is, not by whose it is: at least min_sunk_captions
    'N MM SUNK' captions of different depths, lined up one above another (same
    left edge), then the other short captions in line with them. The samples sit
    on one side of the captions; that side, and how far the samples reach, is
    read from where the drawn things on those rows actually are.
    """
    texts = []
    for e in entities:
        if e.dxftype() not in ("TEXT", "MTEXT"):
            continue
        t = _text_of(e)
        p = _point(e)
        if p and t:
            texts.append((p[0], p[1], t.upper()))
    sunk = [(x, y, t) for x, y, t in texts if _SUNK_CAPTION.fullmatch(t.strip())]
    others = []
    fills = []          # what a sample is: a hatch, or the short lines of an exploded one
    for e in entities:
        if e.dxftype() in ("TEXT", "MTEXT"):
            continue
        p = hatch_box(e) if e.dxftype() == "HATCH" else None
        if e.dxftype() == "HATCH":
            if p:
                others.append(((p[0] + p[2]) / 2.0, (p[1] + p[3]) / 2.0, p[0], p[2]))
                fills.append(((p[0] + p[2]) / 2.0, (p[1] + p[3]) / 2.0, "hatch"))
            continue
        q = _point(e)
        if q:
            others.append((q[0], q[1], q[0], q[0]))
            if e.dxftype() == "LINE":
                a, b = e.dxf.start, e.dxf.end
                if ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5 < 1500.0:
                    fills.append((q[0], q[1], "line"))
    align = float(s.get("caption_align_mm", 50.0))
    gap = float(s["label_gap_mm"])
    reach = float(s.get("sample_reach_mm", 7000.0))
    boxes, used = [], set()
    for i, (x0, _y0, _t0) in enumerate(sunk):
        if i in used:
            continue
        col = sorted((j for j, (x, _y, _t) in enumerate(sunk) if abs(x - x0) <= align),
                     key=lambda j: -sunk[j][1])
        used.update(col)
        # a run of captions one above another, split at a real gap
        runs, cur = [], [col[0]]
        for a, b in zip(col, col[1:]):
            if sunk[a][1] - sunk[b][1] > gap:
                runs.append(cur)
                cur = []
            cur.append(b)
        runs.append(cur)
        for run in runs:
            depths = {float(_SUNK_CAPTION.search(sunk[j][2]).group(1)) for j in run}
            if len(depths) < int(s.get("min_sunk_captions", 3)):
                continue
            ys = [sunk[j][1] for j in run]
            # A legend draws a sample beside each caption; a schedule's remarks column
            # ('400mm SUNK' in the slab schedule) has words and table rules, no sample.
            gaps_y = [a - b for a, b in zip(sorted(ys, reverse=True), sorted(ys, reverse=True)[1:]) if a - b > 1]
            half = (min(gaps_y) if gaps_y else 1000.0) * 0.45

            def has_sample(y):
                near = [f for f in fills if abs(f[1] - y) <= half and 0 < abs(f[0] - x0) <= reach]
                return any(f[2] == "hatch" for f in near) or sum(1 for f in near if f[2] == "line") >= 6
            if sum(1 for y in ys if has_sample(y)) < int(s.get("min_sunk_captions", 3)):
                continue
            top, bot = max(ys), min(ys)
            # the other captions in the same column, carrying on above or below
            more = sorted((y for x, y, t in texts if abs(x - x0) <= align and len(t) <= s["heading_max_chars"]
                           and not _SUNK_CAPTION.fullmatch(t.strip())), key=lambda y: -y)
            for y in more:
                if bot - gap <= y < bot:
                    bot = y
            for y in sorted(more):
                if top < y <= top + gap:
                    top = y
            step = min([a - b for a, b in zip(sorted(ys, reverse=True), sorted(ys, reverse=True)[1:])
                        if a - b > 1] or [1200.0])
            rows = [o for o in others if bot - step / 2 <= o[1] <= top + step / 2
                    and abs(o[0] - x0) <= reach]
            left = [o for o in rows if o[0] < x0]
            right = [o for o in rows if o[0] > x0]
            pad = 300.0

            def cluster(xs):
                """How far out from the captions the samples reach: from the first
                thing drawn beside them (past the caption words themselves) up to
                the first real gap - the samples, and not a plan drawn close by."""
                xs = sorted(d for d in xs if d > 0)
                if not xs:
                    return 1000.0
                out = last = xs[0]
                for d in xs[1:]:
                    if d - last > s.get("sample_gap_mm", 2000.0):
                        break
                    out = last = d
                return out
            if len(right) >= len(left):
                far = cluster([o[3] - x0 for o in right])
                box = (x0 - 500.0, bot - step / 2, x0 + far + pad, top + step / 2)
            else:
                far = cluster([x0 - o[2] for o in left])
                box = (x0 - far - pad, bot - step / 2, x0 + 500.0, top + step / 2)
            boxes.append(box + ("legend (no heading)",))
    return boxes


# "*THUS MARKED 200 mm SUNK SLAB WITH ..." beside a sample: a legend written as notes
_THUS = re.compile(r"\bTHUS\s+(?:MARKED|HATCHED|SHOWN)\b", re.I)
# in such a row a drop is said either way: "200 mm SUNK", "150 mm DOWN RCC PROJECTION"
_DROP_CAPTION = re.compile(r"(\d+(?:\.\d+)?)\s*MM\s*(?:SUNK|DOWN)\b", re.I)


def _depth_caption(t: str):
    """A legend caption's sunk depth match: 'N MM SUNK', or in a THUS MARKED row
    'N MM DOWN' too. None for anything else."""
    return _SUNK_CAPTION.search(t) or (_DROP_CAPTION.search(t) if _THUS.search(t) else None)


def find_thus_marked(entities, s: dict) -> list[tuple[float, float, float, float, str]]:
    """Legend rows written as notes, each with its sample beside it.

    New block (Sasan Gir hotel, 2nd floor) has no LEGEND heading and no column of
    'N MM SUNK' captions: its key is in the notes - "*THUS MARKED 200 mm SUNK SLAB
    WITH LIGHT WEIGHTED FILLING", "...600 mm SUNK...", "...150 mm DOWN RCC
    PROJECTION", "*THUS MARKED CUT OUT" - each with a little hatched box to its
    left. Unread, the depths were unknown and the sample boxes became sunk zones.

    A row counts only with a sample on it: a hatch (or a closed outline) beside the
    caption, within sample_reach_mm and on its row. One box per row, round the
    caption and its sample."""
    reach = float(s.get("sample_reach_mm", 7000.0))
    samples = []
    for e in entities:
        if e.dxftype() == "HATCH":
            b = hatch_box(e)
            if b:
                samples.append(b)
    boxes = []
    for e in entities:
        if e.dxftype() not in ("TEXT", "MTEXT"):
            continue
        t = _text_of(e)
        if not _THUS.search(t):
            continue
        p = _point(e)
        if p is None:
            continue
        try:
            h = float(e.dxf.height if e.dxftype() == "TEXT" else e.dxf.char_height) or 200.0
        except Exception:
            h = 200.0
        cx, cy = p
        row = [b for b in samples if b[2] <= cx + h and cx - reach <= b[0]
               and abs((b[1] + b[3]) / 2.0 - cy) <= 3.0 * h]
        if not row:
            row = [b for b in samples if b[0] >= cx and b[0] - cx <= reach
                   and abs((b[1] + b[3]) / 2.0 - cy) <= 3.0 * h]
        if not row:
            continue
        b = min(row, key=lambda b: abs((b[1] + b[3]) / 2.0 - cy) + 0.1 * min(abs(cx - b[2]), abs(b[0] - cx)))
        boxes.append((min(b[0], cx) - 100.0, min(b[1], cy - h), max(b[2], cx) + 100.0, max(b[3], cy + h),
                      "legend (THUS MARKED)"))
    return boxes


def _boxes(entities, s: dict) -> list[tuple[float, float, float, float, str]]:
    """Every legend block on the sheet: with a heading, and without one (a column
    of captions inside a headed legend is that legend, not a second one), and
    legend rows written as notes (THUS MARKED) outside both."""
    headed = find_boxes(entities, s)

    def overlaps(a, b):
        return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]
    out = headed + [c for c in find_caption_columns(entities, s)
                    if not any(overlaps(c, h) for h in headed)]
    return out + [c for c in find_thus_marked(entities, s) if not any(overlaps(c, h) for h in out)]


def hatch_box(e):
    """(x1, y1, x2, y2) of a hatch's boundary, or None."""
    xs, ys = [], []
    try:
        for path in e.paths:
            for v in getattr(path, "vertices", []) or []:
                xs.append(float(v[0]))
                ys.append(float(v[1]))
            for edge in getattr(path, "edges", []) or []:
                for attr in ("start", "end", "center"):
                    q = getattr(edge, attr, None)
                    if q is not None:
                        xs.append(float(q[0]))
                        ys.append(float(q[1]))
    except Exception:
        return None
    return (min(xs), min(ys), max(xs), max(ys)) if xs else None


def sunk_key(entities, cfg: dict, res: Result | None = None, log=print) -> list[tuple]:
    """The legend's sunk samples: [(layer, pattern, scale, depth)].

    The legend is the only place a sunk depth is stated for the plan's hatches,
    and it goes by the look of the hatch, not the layer name. On Wing B 1st floor
    the layer '300mm SUNK' carries the pattern captioned 100MM SUNK, and the 150
    and one 400 sit on a layer called HATCH. The same pattern means 100 mm on one
    sheet and 250 mm on the next, so it is read from each drawing's own legend.

    Each caption is paired with the sample hatch on its own row, left of it.
    Must run before exclude(), which takes the legend out. depth_for() then says
    what a plan hatch means.
    """
    s = settings(cfg)
    if not s["detect"]:
        return []
    boxes = _boxes(entities, s)
    if not boxes:
        return []
    hatches, captions = [], []
    for e in entities:
        t = e.dxftype()
        if t == "HATCH":
            b = hatch_box(e)
            if b:
                hatches.append(((b[0] + b[2]) / 2, (b[1] + b[3]) / 2, e))
        elif t in ("TEXT", "MTEXT"):
            m = _depth_caption(_text_of(e))
            p = _point(e)
            if m and p and not re.search(r"PART", _text_of(e), re.I):
                captions.append((p[0], p[1], float(m.group(1))))

    samples = []
    for x1, y1, x2, y2, name in boxes:
        caps = sorted([c for c in captions if x1 <= c[0] <= x2 and y1 <= c[1] <= y2],
                      key=lambda c: -c[1])
        if not caps:
            continue
        gaps = [a[1] - b[1] for a, b in zip(caps, caps[1:]) if a[1] - b[1] > 1]
        reach = max(300.0, min(gaps) * 0.6) if gaps else 600.0
        for cx, cy, depth in caps:
            row = [h for h in hatches
                   if x1 - s["pad_mm"] <= h[0] < cx and abs(h[1] - cy) <= reach]
            if not row and name == "legend (no heading)":     # samples right of the captions
                row = [h for h in hatches if cx < h[0] <= x2 and abs(h[1] - cy) <= reach]
            if row:
                h = min(row, key=lambda h: abs(h[1] - cy) + 0.1 * abs(cx - h[0]))[2]
                q = (str(h.dxf.layer).upper(), str(h.dxf.pattern_name).upper(),
                     float(h.dxf.pattern_scale or 1.0), depth)
                if q not in samples:
                    samples.append(q)
    if samples and res is not None:
        res.add(INFO, "legend",
                "Sunk depths read from the legend: " + ", ".join(
                    f"{d:.0f} mm = {pat} (scale {sc:g}) on {lay}"
                    for lay, pat, sc, d in samples) + ".")
        log(f"  Legend gives {len(samples)} sunk hatch samples")
    return samples


def sunk_key_lines(entities, cfg: dict, res: Result | None = None, log=print) -> list[tuple]:
    """The legend's sunk samples drawn as exploded hatch: [(layer, look, angle, depth)].

    sunk_key() for a legend whose sample hatches have been exploded into lines,
    as on the team's working copy of Wing B 2nd floor. Each line left of a caption
    goes to the caption row nearest it; the lines of a row give its look (see
    sunklines.look). Must run before exclude().
    """
    from . import sunklines
    s = settings(cfg)
    ls = sunklines.settings(cfg)
    if not s["detect"] or not ls["detect"]:
        return []
    boxes = _boxes(entities, s)
    if not boxes:
        return []
    captions = []
    for e in entities:
        if e.dxftype() in ("TEXT", "MTEXT"):
            m = _depth_caption(_text_of(e))
            p = _point(e)
            if m and p and not re.search(r"PART", _text_of(e), re.I):
                captions.append((p[0], p[1], float(m.group(1))))
    segs = sunklines.segments(entities)

    samples = []
    for x1, y1, x2, y2, name in boxes:
        caps = sorted([c for c in captions if x1 <= c[0] <= x2 and y1 <= c[1] <= y2],
                      key=lambda c: -c[1])
        if not caps:
            continue
        gaps = [a[1] - b[1] for a, b in zip(caps, caps[1:]) if a[1] - b[1] > 1]
        reach = max(300.0, min(gaps) * 0.6) if gaps else 600.0
        rows = collections.defaultdict(list)
        for g in segs:
            mx, my = (g[1][0] + g[2][0]) / 2, (g[1][1] + g[2][1]) / 2
            if not (x1 - s["pad_mm"] <= mx <= x2 and y1 - reach <= my <= y2 + reach):
                continue
            left = [i for i, c in enumerate(caps) if mx < c[0] and abs(my - c[1]) <= reach]
            if not left and name == "legend (no heading)":     # samples right of the captions
                left = [i for i, c in enumerate(caps) if mx > c[0] and abs(my - c[1]) <= reach]
            if left:
                rows[min(left, key=lambda i: abs(my - caps[i][1]))].append(g)
        for i, row in rows.items():
            # the sample is the fill, not the box drawn round it: keep the layer
            # with the most lines on the row
            lay = collections.Counter(g[0] for g in row).most_common(1)[0][0]
            fill = [g for g in row if g[0] == lay]
            got = sunklines.look(fill, ls)
            if got and len(fill) >= 6:
                samples.append((lay, got[0], got[1], caps[i][2]))
    if samples and res is not None:
        res.add(INFO, "legend",
                "The legend's sunk samples are exploded into lines; read by their look: "
                + ", ".join(f"{d:.0f} mm = {kind} at {ang:.0f} deg on {lay}"
                            for lay, kind, ang, d in samples) + ".")
        log(f"  Legend gives {len(samples)} sunk samples drawn as lines")
    return samples


def legend_scale(samples, plan_hatches) -> float:
    """How much bigger the legend draws its hatches than the plan does.

    Wing A 1st floor draws its legend at 1.866 times the plan's hatch scale, so a
    sample's scale can only be compared with a plan hatch's after allowing for
    it. Taken from the patterns that appear in just one sample.
    """
    import statistics
    ratios = []
    for lay, pat, sc, _d in samples:
        if sum(1 for q in samples if q[1] == pat) != 1:
            continue
        plan = [float(h.dxf.pattern_scale or 1.0) for h in plan_hatches
                if str(h.dxf.pattern_name).upper() == pat]
        if plan:
            ratios.append(sc / statistics.median(plan))
    return statistics.median(ratios) if ratios else 1.0


def depth_for(hatch, samples, factor: float = 1.0) -> float | None:
    """The depth a plan hatch means by the legend, or None if it cannot say.

    By pattern first - that is what a reader of the drawing goes by. Where two
    samples share the pattern, the one on the same layer, then the one nearest
    in scale (after the legend's own scale), decides; if that still leaves two
    depths, nothing is guessed.
    """
    import math
    pat = str(hatch.dxf.pattern_name).upper()
    cands = [q for q in samples if q[1] == pat]
    if not cands:
        return None
    if len({q[3] for q in cands}) == 1:
        return cands[0][3]
    lay = str(hatch.dxf.layer).upper()
    same = [q for q in cands if q[0] == lay]
    if same and len({q[3] for q in same}) == 1:
        return same[0][3]
    cands = same or cands
    sc = float(hatch.dxf.pattern_scale or 1.0) * factor
    off = sorted((abs(math.log(q[2] / sc)), q[3]) for q in cands if q[2] > 0 and sc > 0)
    if len(off) >= 2 and off[1][0] - off[0][0] > math.log(2.0) and off[0][1] != off[1][1]:
        return off[0][1]
    return None


def samples_from_file(dxf_path: str, cfg: dict) -> dict:
    """What a legend sheet says, for the other drawings of its set:
    {"samples": sunk_key(), "lines": sunk_key_lines()}, read exactly as a plan's
    own legend is (blocks opened first)."""
    import ezdxf
    from . import blocks
    doc = ezdxf.readfile(dxf_path)
    quiet = Result(dxf_file=dxf_path)
    ents = blocks.flatten(doc.modelspace(), cfg, quiet, log=lambda *a, **k: None)
    return {"samples": [list(q) for q in sunk_key(ents, cfg)],
            "lines": [list(q) for q in sunk_key_lines(ents, cfg)]}


def exclude(entities, cfg: dict, res: Result | None = None, log=print) -> list:
    """Drop everything inside a legend block, and inside any configured zone."""
    s = settings(cfg)
    boxes: list[tuple[float, float, float, float, str]] = []

    for z in s["exclude_zones"] or []:
        try:
            x1, y1, x2, y2 = (float(v) for v in z)
            boxes.append((min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2),
                          "an exclude_zone from config.json"))
        except Exception:
            if res is not None:
                res.add(WARNING, "legend",
                        f"Could not read the exclude_zone {z!r} in config.json. It must be "
                        "four numbers: [x1, y1, x2, y2] in drawing coordinates.")

    if s["detect"]:
        boxes.extend(_boxes(entities, s))

    if not boxes:
        return list(entities)

    kept, dropped = [], {}
    for e in entities:
        p = _point(e)
        hit = None
        if p is not None:
            for x1, y1, x2, y2, name in boxes:
                if x1 <= p[0] <= x2 and y1 <= p[1] <= y2:
                    hit = name
                    break
        if hit is None:
            kept.append(e)
        else:
            d = dropped.setdefault(hit, {})
            d[str(e.dxf.layer)] = d.get(str(e.dxf.layer), 0) + 1

    if res is not None:
        for name, layers in dropped.items():
            total = sum(layers.values())
            top = ", ".join(f"{k} ({v})" for k, v in
                            sorted(layers.items(), key=lambda kv: -kv[1])[:6])
            res.add(INFO, "legend",
                    f"{total} entities inside '{name}' were left out, because a legend is "
                    "drawn on the same layers as the structure it describes and would "
                    f"otherwise become real elements. Layers: {top}. If any of it was real "
                    "structure, set \"legend\": {\"detect\": false} in config.json.")
    n = sum(sum(v.values()) for v in dropped.values())
    if n:
        log(f"  Left out {n} entities inside {len(dropped)} legend or excluded block(s)")
    return kept
