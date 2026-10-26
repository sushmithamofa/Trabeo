r"""Lining the architect's model up with the structure (Review's 3D view). Display only.

The line-up maps the architect's model (in mm, Z up, after its units and up axis) onto a
drawing set's frame - the frame Review's 3D view draws that set in:

    x' = cos(rot) x - sin(rot) y + dx
    y' = sin(rot) x + cos(rot) y + dy
    z' = z + dz

Nothing is assumed; every proposal is shown to the user, who saves it or not:
  1. as it is: when the structure's columns already stand in the architect's columns
     (both drawn on the same background, "origin to origin") - nothing to move;
  2. by the columns: the one turn and shift that puts the most structural columns on
     the architect's columns (pairs of columns the same distance apart vote for it);
     a fit that is as good in two places is not used - the page asks for two points;
  3. two points picked by the user (two_points), when the columns do not decide it.
Heights: the architect's floors (IFC) matched by name to the structure's levels; the
difference per floor is listed and a single offset is proposed only when they agree.

Columns of the architect's model: its column objects (IFC IfcColumn, a layer or name
saying column) and anything shaped like one - an upright piece at most 1.5 m across and
taller than 1.8 m. Doors, windows, furniture, railings, services and the site are never
taken for columns.
"""
from __future__ import annotations

import math
import re

import numpy as np

TOL = 150.0              # mm: a structural column this near an architectural one is on it
MIN_PAIR = 1500.0        # mm: shorter pairs say little about the turn
NOT_COLUMNS = ("Doors", "Windows", "Furniture", "Railings", "Services", "Site", "Curtain walls and frames",
               "Stairs and ramps", "Finishes and ceilings")
_COLWORD = re.compile(r"(?i)(?<![a-z])(col(?:umn)?s?|clm)(?![a-z])")
_WALLWORD = re.compile(r"(?i)wall|parapet|railing|glass|glazing|window|door|furn|tree|plant")


# ---------------------------------------------------------------- the architect's columns
def to_mm_zup(b, unit_mm: float, up: str) -> tuple[np.ndarray, np.ndarray]:
    """An element's box (file units, file axes) in mm with Z up: (lo, hi)."""
    lo, hi = np.array(b[:3], dtype=float) * unit_mm, np.array(b[3:], dtype=float) * unit_mm
    if up == "y":                                   # (x, y up, z towards you) -> (x, -z, y)
        lo, hi = np.array([lo[0], -hi[2], lo[1]]), np.array([hi[0], -lo[2], hi[1]])
    return lo, hi


def columns(meta: dict, unit_mm: float | None = None, up: str | None = None) -> list[tuple[float, float]]:
    """Centres (x, y mm, Z up) of the architect's columns, one per place."""
    unit = float(unit_mm or meta.get("unit_mm") or 1.0)
    up = up or meta.get("up") or "z"
    groups = meta.get("groups") or []
    named_pts, shaped = [], []
    for e in meta.get("elements") or []:
        g = groups[e["g"]] if 0 <= e.get("g", -1) < len(groups) else ""
        if g in NOT_COLUMNS:
            continue
        lo, hi = to_mm_zup(e["b"], unit, up)
        w, d, h = hi - lo
        named = e.get("c") in ("IfcColumn", "IfcColumnStandardCase") or g == "Columns" or \
            bool(_COLWORD.search(g)) or bool(_COLWORD.search(e.get("n") or ""))
        if named and h >= 1000.0 and max(w, d) <= 3000.0:
            named_pts.append(((lo[0] + hi[0]) / 2.0, (lo[1] + hi[1]) / 2.0))
        elif (150.0 <= min(w, d) and max(w, d) <= 1500.0 and max(w, d) <= 3.0 * min(w, d) and h >= 1800.0
              and h >= 1.5 * max(w, d) and not _WALLWORD.search(g) and not _WALLWORD.search(e.get("n") or "")):
            shaped.append((lo, hi))
    # a piece touching another such piece is part of a wall drawn in pieces (a curved wall
    # as short straight lengths), not a column standing on its own
    cell, cells = 2000.0, {}
    for i, (lo, hi) in enumerate(shaped):
        for cx in range(int(math.floor(lo[0] / cell)), int(math.floor(hi[0] / cell)) + 1):
            for cy in range(int(math.floor(lo[1] / cell)), int(math.floor(hi[1] / cell)) + 1):
                cells.setdefault((cx, cy), []).append(i)
    alone = []
    for i, (lo, hi) in enumerate(shaped):
        near = {j for cx in range(int(math.floor((lo[0] - 50) / cell)), int(math.floor((hi[0] + 50) / cell)) + 1)
                for cy in range(int(math.floor((lo[1] - 50) / cell)), int(math.floor((hi[1] + 50) / cell)) + 1)
                for j in cells.get((cx, cy), ())}
        if not any(j != i and lo[0] - 50 <= shaped[j][1][0] and shaped[j][0][0] <= hi[0] + 50
                   and lo[1] - 50 <= shaped[j][1][1] and shaped[j][0][1] <= hi[1] + 50
                   and lo[2] < shaped[j][1][2] and shaped[j][0][2] < hi[2] for j in near):
            alone.append(((lo[0] + hi[0]) / 2.0, (lo[1] + hi[1]) / 2.0))
    return dedupe(named_pts + alone)


def dedupe(pts, within: float = 100.0) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    grid: dict[tuple[int, int], list[int]] = {}
    for x, y in pts:
        k = (int(math.floor(x / within)), int(math.floor(y / within)))
        near = [i for dx in (-1, 0, 1) for dy in (-1, 0, 1) for i in grid.get((k[0] + dx, k[1] + dy), [])
                if math.hypot(out[i][0] - x, out[i][1] - y) <= within]
        if near:
            continue
        grid.setdefault(k, []).append(len(out))
        out.append((float(x), float(y)))
    return out


# ---------------------------------------------------------------- fitting
class _Near:
    """Points looked up by place (a grid of cells TOL wide)."""

    def __init__(self, pts: np.ndarray, tol: float):
        self.p, self.t = pts, tol
        self.cells: dict[tuple[int, int], list[int]] = {}
        for i, (x, y) in enumerate(pts):
            self.cells.setdefault((int(math.floor(x / tol)), int(math.floor(y / tol))), []).append(i)

    def nearest(self, x: float, y: float) -> tuple[int, float]:
        kx, ky = int(math.floor(x / self.t)), int(math.floor(y / self.t))
        best, bd = -1, self.t + 1.0
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for i in self.cells.get((kx + dx, ky + dy), ()):
                    d = math.hypot(self.p[i][0] - x, self.p[i][1] - y)
                    if d < bd:
                        best, bd = i, d
        return best, bd


def apply(al: dict, pts) -> np.ndarray:
    P = np.asarray(pts, dtype=float).reshape(-1, 2)
    c, s = math.cos(math.radians(al["rot"])), math.sin(math.radians(al["rot"]))
    return np.c_[c * P[:, 0] - s * P[:, 1] + al["dx"], s * P[:, 0] + c * P[:, 1] + al["dy"]]


def score(al: dict, A: np.ndarray, near_s: _Near) -> tuple[int, float, list]:
    """How many of the architect's columns land on a structural column (each structural
    column used once), their median miss (mm), and the pairs (arch i, structure j)."""
    T = apply(al, A)
    used, pairs, miss = set(), [], []
    for i, (x, y) in enumerate(T):
        j, d = near_s.nearest(x, y)
        if j >= 0 and d <= near_s.t and j not in used:
            used.add(j)
            pairs.append((i, j))
            miss.append(d)
    return len(pairs), (float(np.median(miss)) if miss else 0.0), pairs


def _refine(A, S, pairs) -> dict:
    """The turn and shift that fit matched pairs best (least squares, no mirror, no scale)."""
    a = np.array([A[i] for i, _ in pairs])
    s = np.array([S[j] for _, j in pairs])
    ca, cs = a.mean(axis=0), s.mean(axis=0)
    H = (a - ca).T @ (s - cs)
    rot = math.atan2(H[0, 1] - H[1, 0], H[0, 0] + H[1, 1])
    c, sn = math.cos(rot), math.sin(rot)
    t = cs - np.array([c * ca[0] - sn * ca[1], sn * ca[0] + c * ca[1]])
    return {"rot": math.degrees(rot), "dx": float(t[0]), "dy": float(t[1])}


def _candidates(A: np.ndarray, S: np.ndarray, tol: float, rng) -> list[dict]:
    """Turns and shifts that pairs of columns the same distance apart vote for, most votes first."""
    if len(S) < 2 or len(A) < 2:
        return []
    # structural pairs: each column with its nearest columns at least MIN_PAIR away
    seen = {}
    for i in range(len(S)):
        d = np.hypot(*(S - S[i]).T)
        for j in [j for j in np.argsort(d) if d[j] >= MIN_PAIR][:6]:
            seen[(min(i, j), max(i, j))] = float(d[j])
    pairs_s = [(i, j, d) for (i, j), d in sorted(seen.items())]
    if len(pairs_s) > 500:
        pairs_s = [pairs_s[k] for k in sorted(rng.choice(len(pairs_s), 500, replace=False))]
    maxd = max(p[2] for p in pairs_s) + tol
    # every pair of the architect's columns up to that distance, by distance
    ia, ja, da = [], [], []
    for i in range(len(A)):
        d = np.hypot(*(A[i + 1:] - A[i]).T)
        k = np.nonzero((d >= MIN_PAIR - tol) & (d <= maxd))[0]
        ia.append(np.full(len(k), i))
        ja.append(k + i + 1)
        da.append(d[k])
    ia, ja, da = np.concatenate(ia), np.concatenate(ja), np.concatenate(da)
    order = np.argsort(da)
    ia, ja, da = ia[order], ja[order], da[order]
    keys = []
    for i, j, dist in pairs_s:
        lo, hi = np.searchsorted(da, dist - tol), np.searchsorted(da, dist + tol, side="right")
        if hi <= lo:
            continue
        sv = S[j] - S[i]
        ang_s = math.atan2(sv[1], sv[0])
        for a0, a1 in ((ia[lo:hi], ja[lo:hi]), (ja[lo:hi], ia[lo:hi])):
            av = A[a1] - A[a0]
            rot = ang_s - np.arctan2(av[:, 1], av[:, 0])
            c, s = np.cos(rot), np.sin(rot)
            tx = S[i][0] - (c * A[a0][:, 0] - s * A[a0][:, 1])
            ty = S[i][1] - (s * A[a0][:, 0] + c * A[a0][:, 1])
            deg = (np.degrees(rot) + 180.0) % 360.0 - 180.0
            keys.append(np.c_[np.round(deg / 0.5), np.round(tx / 250.0), np.round(ty / 250.0)])
    if not keys:
        return []
    K = np.concatenate(keys).astype(np.int64)
    uniq, counts = np.unique(K, axis=0, return_counts=True)
    top = np.argsort(-counts)[:40]
    return [{"rot": float(uniq[k][0] * 0.5), "dx": float(uniq[k][1] * 250.0), "dy": float(uniq[k][2] * 250.0)}
            for k in top]


def _best(A, S, tol, rng):
    near_s = _Near(S, tol)
    found = []
    for c in _candidates(A, S, tol, rng):
        n, med, pairs = score(c, A, _Near(S, tol * 3))   # the vote's grid is coarse: a wider net first
        if n < 3:
            continue
        al = _refine(A, S, pairs)
        for _ in range(2):
            n, med, pairs = score(al, A, near_s)
            if n < 3:
                break
            al = _refine(A, S, pairs)
        n, med, pairs = score(al, A, near_s)
        if n >= 3:
            found.append((n, med, al))
    found.sort(key=lambda f: (-f[0], f[1]))
    return found


def _differs(a, b) -> bool:
    return abs(((a["rot"] - b["rot"]) + 180) % 360 - 180) > 1.0 or math.hypot(a["dx"] - b["dx"], a["dy"] - b["dy"]) > 1000


def fit(arch_cols, struct_cols, tol: float = TOL) -> dict:
    """The line-up the columns say, as a proposal for the page:
    {ok, how, rot, dx, dy, matched, arch, struct, miss_mm, off:[[x, y], ...], text}."""
    A = np.array(dedupe(arch_cols), dtype=float).reshape(-1, 2)
    S = np.array(dedupe(struct_cols), dtype=float).reshape(-1, 2)
    base = {"arch": len(A), "struct": len(S)}
    if len(S) < 3:
        return {**base, "ok": False, "how": "none", "text": "This drawing set has fewer than 3 columns in 3D: "
                "line the model up by two points."}
    if len(A) < 3:
        return {**base, "ok": False, "how": "none", "text": "No columns were found in the architect's model: "
                "line it up by two points."}
    rng = np.random.default_rng(7)
    near_s = _Near(S, tol)
    need = max(4, int(math.ceil(0.5 * min(len(A), len(S)))))
    ident = {"rot": 0.0, "dx": 0.0, "dy": 0.0}
    n0, m0, p0 = score(ident, A, near_s)
    found = _best(A, S, tol, rng)
    best = found[0] if found else None
    if n0 >= need and (best is None or n0 >= 0.9 * best[0]):
        return {**base, **_report(ident, A, S, near_s, n0, m0, "as it is"), "ok": True, "how": "as it is",
                "text": f"Already lined up: {n0} of {len(S)} structural columns stand in the architect's columns "
                        f"as both files are drawn (median miss {m0:.0f} mm). Nothing is moved."}
    if best is None or best[0] < need:
        mirrored = _best(A * np.array([-1.0, 1.0]), S, tol, rng)
        if mirrored and mirrored[0][0] >= need:
            return {**base, "ok": False, "how": "mirrored",
                    "text": "The columns fit only if the architect's model is mirrored - check the file "
                            "(it may be a mirrored copy). Not lined up."}
        return {**base, "ok": False, "how": "none",
                "text": f"The columns did not give a clear fit (best: {best[0] if best else 0} of {len(S)}). "
                        "Line the model up by two points."}
    rival = next((f for f in found[1:] if _differs(f[2], best[2])), None)
    if rival and rival[0] >= best[0]:
        return {**base, "ok": False, "how": "ambiguous",
                "text": f"The columns fit equally well in more than one place ({best[0]} columns each). "
                        "Line the model up by two points."}
    n, med, al = best
    out = {**base, **_report(al, A, S, near_s, n, med, "by the columns"), "ok": True, "how": "by the columns",
           "text": f"Lined up by the columns: {n} of {len(S)} structural columns stand in the architect's "
                   f"columns (median miss {med:.0f} mm) - turned {al['rot']:.2f} deg, moved "
                   f"{al['dx'] / 1000:+.3f} m, {al['dy'] / 1000:+.3f} m."}
    if rival and rival[0] >= 0.85 * best[0]:
        # a symmetrical grid fits turned round too, with a column or two fewer: proposed,
        # but said - the user looks at the preview, and can try the other one
        r_al = rival[2]
        out["alt"] = {**_report(r_al, A, S, near_s, rival[0], rival[1], "by the columns"),
                      "text": f"The other fit: {rival[0]} of {len(S)} columns, turned {r_al['rot']:.2f} deg, moved "
                              f"{r_al['dx'] / 1000:+.3f} m, {r_al['dy'] / 1000:+.3f} m."}
        out["text"] += (f" Check the preview: another fit puts {rival[0]} columns on columns (turned "
                        f"{r_al['rot']:.1f} deg) - the grid is nearly symmetrical.")
    return out


def _report(al, A, S, near_s, n, med, how) -> dict:
    """The proposal with the structural columns that have no architectural column under them."""
    T = apply(al, A)
    near_a = _Near(T, near_s.t)
    off = [[round(float(x)), round(float(y))] for x, y in S if near_a.nearest(x, y)[1] > near_s.t]
    return {"rot": round(al["rot"], 4), "dx": round(al["dx"], 1), "dy": round(al["dy"], 1),
            "matched": n, "miss_mm": round(med, 1), "off": off[:200], "n_off": len(off)}


def refine(arch_cols, struct_cols, start: dict, tol: float = TOL) -> dict:
    """From a line-up made by hand (near, not exact): the architect's columns near the
    structure's taken as pairs and the turn and shift fitted to them, the net drawn in from
    4x to 1x the tolerance. Nothing searched far away - the hand placement decides where."""
    A = np.array(dedupe(arch_cols), dtype=float).reshape(-1, 2)
    S = np.array(dedupe(struct_cols), dtype=float).reshape(-1, 2)
    if len(A) < 3:
        return {"ok": False, "text": "The architect's model has no columns to fine-tune with - the position "
                                     "by hand is kept. Pin it there, or use Two points."}
    if len(S) < 3:
        return {"ok": False, "text": "This drawing set has fewer than 3 columns in 3D - the position by hand is kept."}
    al = {"rot": float(start.get("rot") or 0.0), "dx": float(start.get("dx") or 0.0), "dy": float(start.get("dy") or 0.0)}
    for t in (tol * 8, tol * 4, tol * 2, tol):
        n, med, pairs = score(al, A, _Near(S, t))
        if n < 3:
            return {"ok": False, "text": f"Fewer than 3 of the architect's columns are within {t:.0f} mm of a "
                                         "structural column - move the model closer by hand first."}
        al = _refine(A, S, pairs)
    near_s = _Near(S, tol)
    n, med, _ = score(al, A, near_s)
    turned = ((al["rot"] - (start.get("rot") or 0.0)) + 180) % 360 - 180
    mid = A.mean(axis=0, keepdims=True)                 # how far the building itself moved
    moved = float(np.hypot(*(apply(al, mid) - apply({"rot": float(start.get("rot") or 0.0), "dx": float(start.get("dx") or 0.0),
                                                       "dy": float(start.get("dy") or 0.0)}, mid))[0]))
    return {**_report(al, A, S, near_s, n, med, "fine-tuned"), "ok": True,
            "text": f"Fine-tuned by the columns: {n} of {len(S)} structural columns stand in the architect's columns "
                    f"(median miss {med:.0f} mm) - from where you put it, turned {turned:+.2f} deg and moved "
                    f"{moved:.0f} mm."}


def two_points(s1, s2, a1, a2) -> dict:
    """The line-up taking the architect's points a1, a2 onto the structure's s1, s2
    (x, y mm): turn and shift from the pair; the two distances are compared, not scaled."""
    sv, av = np.subtract(s2, s1), np.subtract(a2, a1)
    ls, la = float(np.hypot(*sv)), float(np.hypot(*av))
    if ls < 100 or la < 100:
        raise ValueError("Pick two points further apart.")
    rot = math.atan2(sv[1], sv[0]) - math.atan2(av[1], av[0])
    c, s = math.cos(rot), math.sin(rot)
    dx = s1[0] - (c * a1[0] - s * a1[1])
    dy = s1[1] - (s * a1[0] + c * a1[1])
    deg = (math.degrees(rot) + 180.0) % 360.0 - 180.0
    out = {"rot": round(deg, 4), "dx": round(dx, 1), "dy": round(dy, 1), "how": "two points",
           "text": f"Lined up by two points: turned {deg:.2f} deg, moved {dx / 1000:+.3f} m, {dy / 1000:+.3f} m."}
    if abs(ls - la) > max(50.0, 0.005 * ls):
        out["text"] += (f" The two points are {ls / 1000:.3f} m apart in the structure but {la / 1000:.3f} m in "
                        "the architect's model - check the units, or that the same points were picked.")
        out["warn"] = True
    return out


# ---------------------------------------------------------------- heights
def heights(storeys: list[dict], levels: list[dict], unit_mm: float = 1.0) -> dict:
    """The architect's floors against the structure's levels, matched by name:
    {pairs: [{arch, elev, level, z, diff}], dz (only when they agree within 25 mm), text}."""
    from building.sheets.levels import levels_in
    by_key = {lv["key"]: lv for lv in levels or []}
    pairs = []
    for s in storeys or []:
        if s.get("elev") is None:
            continue
        keys = [k for k in levels_in(s.get("name") or "") if k in by_key]
        if not keys:
            continue
        lv = by_key[keys[0]]
        elev = float(s["elev"]) * unit_mm
        pairs.append({"arch": s["name"], "elev": round(elev, 1), "level": lv["key"], "z": round(float(lv["z"]), 1),
                      "diff": round(float(lv["z"]) - elev, 1), "assumed": bool(lv.get("assumed"))})
    if not pairs:
        return {"pairs": [], "dz": None, "text": "No floor of the architect's model could be matched to the "
                "structure's levels by name: heights are as in the file (type an offset if its 0 is elsewhere)."}
    real = [p for p in pairs if not p["assumed"]] or pairs
    diffs = sorted(p["diff"] for p in real)
    med = diffs[len(diffs) // 2]
    agree = all(abs(d - med) <= 25.0 for d in diffs)
    txt = "; ".join(f"{p['arch']} {p['elev'] / 1000:+.3f} m - {p['level']} {p['z'] / 1000:+.3f} m "
                    f"(difference {p['diff']:+.0f} mm)" for p in pairs)
    if agree:
        return {"pairs": pairs, "dz": med, "text": f"Floors matched by name: {txt}. Proposed height offset "
                                                     f"{med:+.0f} mm."}
    return {"pairs": pairs, "dz": None, "text": f"Floors matched by name, but the differences do not agree: {txt}. "
                                                "No offset proposed - type one, or check the levels."}


def propose(meta: dict, model: dict, struct_cols, levels) -> dict:
    """Everything the page shows when the user asks to line a model up with a drawing set."""
    unit, up = float(model.get("unit_mm") or meta.get("unit_mm") or 1.0), model.get("up") or meta.get("up") or "z"
    plan = fit(columns(meta, unit, up), struct_cols)
    h = heights(meta.get("storeys") or [], levels, unit)
    plan["heights"] = h
    plan["dz"] = h["dz"] if h["dz"] is not None else 0.0
    return plan
