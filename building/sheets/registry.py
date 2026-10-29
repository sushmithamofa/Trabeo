"""The sheet registry: what every view in a drawing set is.

For each drawing: split it into views, find each view's title, decide what kind
of drawing it is, which level and wing it shows, and read any notes on it. The
result goes into the building model as Sheets, one per view, with the evidence
for every decision - so the review screen can show why a view was called a
"framing plan at L1" and a person can correct it.

Nothing here reads elements. It only says what each view is, so the right
reader can be pointed at the right view.
"""
from __future__ import annotations

import os
import re
from collections import Counter
from dataclasses import asdict, dataclass, field

from .classify import (SECTION_TITLE, STRUCTURAL_PLANS, WORDS, Verdict, classify,
                       features, is_plan)
from .levels import level_rank, levels_in, next_up, roof_over, title_elevations, wing_in
from .notes import Fact, read_notes, summary
from .source import TextItem, open_drawing, texts_in
from .views import _DETAIL, View, find_views

TITLE_NEAR = 0.25     # paper text this far (in viewport heights) below/above it is its title
# what a section or detail drawn inside blocks may be (views.inside_blocks)
_HELD_KINDS = ("beam_details", "column_details", "slab_details", "footing_details", "stair_details")


@dataclass
class SheetRecord:
    file: str
    view: View
    title: str = ""
    title_from: str = ""                 # "model text", "paper text", "layout name", "file name"
    prominent: list[str] = field(default_factory=list)
    verdict: Verdict | None = None
    levels: list[str] = field(default_factory=list)
    levels_from: str = ""
    level_sources: list[tuple[str, str, list[str]]] = field(default_factory=list)
    wing: str = ""
    wing_from: str = ""
    notes: list[Fact] = field(default_factory=list)
    features: dict = field(default_factory=dict)
    # kept for the readers, so they need not open the drawing again
    texts: list[TextItem] = field(default_factory=list, repr=False)
    layers: Counter = field(default_factory=Counter, repr=False)
    doc: object = field(default=None, repr=False)      # the drawing, shared by its views

    @property
    def level_conflict(self) -> bool:
        """The title and the file name name different levels, e.g. a 1st-floor
        schedule copied into the 3rd-floor drawing."""
        sets = [set(found) for _, _, found in self.level_sources]
        return len(sets) > 1 and any(not (sets[0] & s) for s in sets[1:])

    @property
    def id(self) -> str:
        stem = re.sub(r"[^A-Za-z0-9]+", "_", os.path.splitext(os.path.basename(self.file))[0]).strip("_")
        return f"{stem[:32]}:{self.view.id}"

    def row(self) -> dict:
        """One line for a table or the review screen."""
        w, h = self.view.size
        return {
            "sheet": self.id, "layout": self.view.layout, "source": self.view.source,
            "size_m": f"{w / 1000:.0f} x {h / 1000:.0f}",
            "scale": f"1:{self.view.scale:.0f}" if self.view.source == "viewport" else "",
            "entities": len(self.view.handles),
            "title": self.title, "kind": self.verdict.kind if self.verdict else "",
            "confidence": self.verdict.confidence if self.verdict else 0,
            "levels": ",".join(self.levels), "wing": self.wing,
            "level_conflict": self.level_conflict,
            "also": ",".join(self.verdict.also) if self.verdict else "",
            "same_as": self.view.same_as,
        }


_HEADING = re.compile(r"^\s*(?:GENERAL\s+)?NOTES?\b|^\s*LEGENDS?\b|^\s*KEY\s*PLAN\b|"
                      r"^\s*\*|\bIMP\.?\s*NOTE\b|"
                      r"^\s*(?:DRAWING\s+TITLE|SCALE|SHEET|REVISION(?:\s+NO)?|DATE|CLIENT|PROJECT|"
                      r"DRAWN\s+BY|CHECKED\s+BY|APPROVED\s+BY|DESIGNED\s+BY)\s*\.?\s*[:\-]")
# a sentence giving an instruction is a note, not a title
_SENTENCE = re.compile(r"\b(?:MUST|SHALL|SHOULD|TO\s+BE|PLEASE|REFER|AS\s+PER|UNLESS|OTHERWISE|"
                       r"WHEREVER|PROVIDE[DS]?|CONSIDER)\b")
# a plan of something standing above the floors: its sheet's floor is not its own
_ABOVE_FLOORS = re.compile(r"@\s*TOP\b|\bAT\s+TOP\b|\bTOP\s+OF\b|\bMUMTY\b|\bHEAD\s*ROOM\b|\bMACHINE\s+ROOM\b|"
                           r"\bOHT\b|\bOVER\s*HEAD\s+(?:WATER\s+)?TANK\b|\bSHAFT\b")


def _title_score(t: TextItem, height: float, hmax: float, big: bool) -> float:
    s = 3.0 * (height / hmax if hmax else 0)
    u = t.text.upper()
    if t.underlined:
        s += 2.0
    if any(rx.search(u) for k, rx, w in WORDS if w >= 2 and k not in ("notes", "legend")):
        s += 2.0
    if levels_in(u):
        s += 1.0
    if _HEADING.search(u):
        s -= 3.0          # a notes or title-block heading, not the drawing's name
    if _SENTENCE.search(u):
        s -= 3.0
    if big and SECTION_TITLE.search(u):
        s -= 2.5          # an inset section's caption on a plan-sized view
    if len(u) < 4 or re.fullmatch(r"[\d\s.,+\-X×()]+", u):
        s -= 3.0
    elif len(u.split()) == 1:
        s -= 1.0          # one word, e.g. a table header: weak as a drawing title
    if len(u) > 90:
        s -= 1.5
    return s


def _names_a_plan(text: str) -> bool:
    u = text.upper()
    return any(rx.search(u) for k, rx, w in WORDS if k in STRUCTURAL_PLANS and w >= 2) or \
        bool(re.search(r"\bPLAN\b|\bLAY\s*-?\s*OUT\b", u))


def _circles(ents, depth: int = 0) -> list[tuple[float, float, float]]:
    out = []
    for e in ents:
        t = e.dxftype()
        if t == "CIRCLE":
            c = e.dxf.center
            out.append((c.x, c.y, float(e.dxf.radius)))
        elif t == "INSERT" and depth < 2:
            try:
                out += _circles(e.virtual_entities(), depth + 1)
            except Exception:
                continue
    return out


def _candidates(view: View, model_texts, paper_texts) -> list[tuple[float, TextItem, str]]:
    """Texts that could be this view's title, with paper-equivalent heights."""
    out = []
    # a view split out of a region by its title: only its own text, not the
    # neighbouring drawing's that happens to fall inside its rectangle
    own = set(view.handles) if view.split_from else None
    for t in model_texts:
        if own is not None and t.handle and t.handle not in own:
            continue
        if view.contains(t.x, t.y):
            out.append((t.height / (view.scale or 1.0), t, "model text"))
    if view.source == "viewport" and view.paper:
        x0, y0, x1, y1 = view.paper
        pad = TITLE_NEAR * (y1 - y0)
        for t in paper_texts.get(view.layout, []):
            if x0 <= t.x <= x1 and y0 - pad <= t.y <= y1 + pad:
                out.append((t.height, t, "paper text"))
    return out


def register(path: str, work_dir: str | None = None, log=None) -> list[SheetRecord]:
    """Every view in one drawing, identified."""
    doc = open_drawing(path, work_dir, log)
    msp = doc.modelspace()
    by_handle = {e.dxf.handle: e for e in msp}
    model_texts = texts_in(msp, "model")
    paper_texts = {lay.name: texts_in(lay, lay.name) for lay in doc.layouts if lay.name != "Model"}
    fname = os.path.basename(path)
    stem = os.path.splitext(fname)[0]

    records = []
    for v in find_views(doc):
        rec = SheetRecord(file=path, view=v)
        w, h = v.size
        big = max(w, h) >= 12000.0
        cands = _candidates(v, model_texts, paper_texts)
        texts = [t for _, t, _ in cands]
        ents = [by_handle[h_] for h_ in v.handles if h_ in by_handle]
        layers = Counter(e.dxf.layer for e in ents)
        rec.features = features(texts, layers, _circles(ents))
        plan = is_plan(rec.features, (w / 1000.0, h / 1000.0))

        hmax = max((h_ for h_, _, _ in cands), default=0.0)
        ranked = sorted(cands, key=lambda c: -_title_score(c[1], c[0], hmax, big))
        usable = [c for c in ranked if _title_score(c[1], c[0], hmax, big) > 1.0]
        wf = None
        if v.twin == "plan" and v.whole is not None:
            # the framing plan of a sheet window holding the floor twice: titled as that
            # whole window is (its title is the sheet's, not a note inside one half)
            wc = _candidates(v.whole, model_texts, paper_texts)
            wmax = max((h_ for h_, _, _ in wc), default=0.0)
            wbig = max(v.whole.size) >= 12000.0
            ranked = sorted(wc, key=lambda c: -_title_score(c[1], c[0], wmax, wbig))
            usable = [c for c in ranked if _title_score(c[1], c[0], wmax, wbig) > 1.0]
            wf = features([t for _, t, _ in wc], Counter(by_handle[h_].dxf.layer for h_ in v.whole.handles
                                                          if h_ in by_handle), [])
            if is_plan(wf, (v.whole.size[0] / 1000.0, v.whole.size[1] / 1000.0)):
                plan = True
        if plan:
            # a plan's title names a plan; an inset's caption ("SECTION 2-2",
            # "SCHEDULE OF BEAMS") on the same sheet is not its title
            usable = [c for c in usable if _names_a_plan(c[1].text)]
        hint = next((c for c in cands if v.title_hint and c[1].text.upper() == v.title_hint), None)
        if hint is not None:
            # split out under this title: it is the title, whatever else is large
            rec.title, rec.title_from = hint[1].text, "model text (the title it was split by)"
        elif usable:
            rec.title, rec.title_from = usable[0][1].text, usable[0][2]
        elif v.layout:
            rec.title, rec.title_from = v.layout, "layout name"
        else:
            rec.title, rec.title_from = stem, "file name"
        seen = {rec.title}
        for _, t, _ in ranked:
            if t.text not in seen and len(t.text) >= 4:
                rec.prominent.append(t.text)
                seen.add(t.text)
            if len(rec.prominent) >= 8:
                break

        if v.source in ("overview", "stray"):
            why = ("shows the whole site or several other views; not read"
                   if v.source == "overview" else
                   f"only {len(v.handles)} entities away from everything else; listed, not read")
            rec.verdict = Verdict(v.source, 1.0, [why])
        else:
            rec.verdict = classify(rec.title, v.layout, stem, rec.prominent, rec.features,
                                   (w / 1000.0, h / 1000.0))
            if wf is not None:
                # judged as the whole window was before it was cut in two
                rec.verdict = classify(rec.title, v.layout, stem, rec.prominent, wf,
                                       (v.whole.size[0] / 1000.0, v.whole.size[1] / 1000.0))
            if v.twin == "reinforcement":
                # the same floor drawn again beside its framing plan, with its slab bars
                rec.verdict = Verdict("slab_details", 0.8,
                                      rec.verdict.reasons + ["the same grid drawn twice side by side: this copy "
                                                             "carries the slab reinforcement, the other the framing"],
                                      rec.verdict.scores, [rec.verdict.kind] + list(rec.verdict.also))
            elif v.split_from and _DETAIL.match(rec.title.upper()):
                # cut out of its sheet under a beam's mark and size ("PB4 (300X600)"): that
                # beam drawn out with its bars, whatever the sheet's title block says
                rec.verdict = Verdict("beam_details", max(rec.verdict.confidence, 0.7),
                                      rec.verdict.reasons + [f"split out under a beam detail's title '{rec.title[:50]}'"],
                                      rec.verdict.scores, [rec.verdict.kind] + list(rec.verdict.also))
            elif v.split_from and rec.verdict.kind not in STRUCTURAL_PLANS and _names_a_plan(rec.title) \
                    and [k for k in levels_in(rec.title) if k != "TYPICAL"]:
                # cut out of its sheet under its own plan title ("STRUCTURAL LAYOUT AT
                # GF LINTEL LEVEL"): it is that plan, however many size captions it carries
                rec.verdict = Verdict("framing_plan", max(rec.verdict.confidence, 0.7),
                                      rec.verdict.reasons + [f"split out under its plan title '{rec.title[:50]}'"],
                                      rec.verdict.scores, [rec.verdict.kind] + list(rec.verdict.also))
            if v.inside_blocks:
                # a section or detail drawn inside blocks: the kind its title says, never a plan,
                # a schedule or a legend - so no floor is read from it
                kind = next((k for k, pat, _ in WORDS if k in _HELD_KINDS and pat.search(rec.title.upper())),
                            "section")
                rec.verdict = Verdict(kind, min(rec.verdict.confidence, 0.6) or 0.6,
                                      rec.verdict.reasons + [f"a {kind.replace('_', ' ')} drawn inside blocks: "
                                                             "read as one"],
                                      rec.verdict.scores, [rec.verdict.kind] + list(rec.verdict.also))

        # every source that names a level, title first; disagreement is kept. A plan titled as
        # the top of something ("FRAMING PLAN @ TOP" of the shaft, a mumty, a machine room) is
        # above the floors: its sheet's floor (Pirgal THIRD FLOOR) is not its own
        above = rec.verdict is not None and rec.verdict.kind in STRUCTURAL_PLANS and \
            not levels_in(rec.title) and bool(_ABOVE_FLOORS.search(rec.title.upper()))
        for src, text in (("title", rec.title), ("layout name", v.layout), ("file name", stem)):
            if above and src != "title":
                continue
            found = levels_in(text)
            if not found and src == "title" and rec.verdict is not None and rec.verdict.kind in STRUCTURAL_PLANS \
                    and rec.title_from.startswith(("model text", "paper text")):
                # a plan named by its level's value ("FRAMING PLAN @(+)6600 LVL."): that level
                found = title_elevations(text)
            if found:
                rec.level_sources.append((src, text[:60], found))
        if rec.level_sources:
            rec.levels = rec.level_sources[0][2]
            rec.levels_from = f"{rec.level_sources[0][0]}: {rec.level_sources[0][1]}"
        for src, text in [("title", rec.title), ("layout name", v.layout), ("file name", stem)] + \
                [("text", p) for p in rec.prominent]:
            wing = wing_in(text)
            if wing:
                rec.wing, rec.wing_from = wing, f"{src}: {text[:60]}"
                break
        rec.notes = read_notes(texts)
        rec.texts, rec.layers, rec.doc = texts, layers, doc
        records.append(rec)
    _roofs_one_floor_up(records)
    return records


def _roofs_one_floor_up(records: list[SheetRecord]) -> None:
    """Plans titled "<storey> roof" belong one floor up: the slab over that storey.

    Aldott: "basement roof", "stilt floor roof", "u.g. floor roof", "first floor roof",
    "second floor roof" side by side. Each goes to the next storey this drawing names
    (basement roof -> stilt), the top one to the floor above it (second -> L3). The
    drawing's order breaks ties between storeys of one rank (stilt, then upper ground)."""
    roofs = [(r, roof_over(r.title)) for r in records if r.title_from.startswith("model text") or
             r.title_from.startswith("paper text")]
    roofs = [(r, k) for r, k in roofs if k and r.levels and r.levels[0] == k]
    if not roofs:
        return
    # the drawing's order: along the row the plans are laid out in (left to right, or
    # top to bottom when they are stacked)
    xs = [(r.view.window[0] + r.view.window[2]) / 2 for r, _ in roofs]
    ys = [(r.view.window[1] + r.view.window[3]) / 2 for r, _ in roofs]
    along_x = max(xs) - min(xs) >= max(ys) - min(ys)
    first_at = {}
    for r, k in sorted(roofs, key=lambda rk: (rk[0].view.window[0] + rk[0].view.window[2]) / 2 if along_x
                       else -(rk[0].view.window[1] + rk[0].view.window[3]) / 2):
        first_at.setdefault(k, len(first_at))
    seq = sorted(first_at, key=lambda k: (level_rank(k) if level_rank(k) is not None else 0.0, first_at[k]))
    for r, k in roofs:
        i = seq.index(k)
        up = seq[i + 1] if i + 1 < len(seq) else next_up(k)
        if not up:
            continue
        r.levels = [up]
        r.levels_from = f"title: '{r.title[:40]}' is the slab over {k}, so {up}"


def to_building(records: list[SheetRecord], building):
    """Add the registry to a Building as Sheets (one per view)."""
    from ..model import Sheet
    for r in records:
        w, h = r.view.size
        building.add_sheet(Sheet(
            id=r.id, file=r.file, layout=r.view.layout, kind=r.verdict.kind,
            title=r.title, wing=r.wing, level=r.levels[0] if len(r.levels) == 1 else "",
            reader="sheets.registry",
            extra={
                "view": {"source": r.view.source, "window": list(r.view.window),
                         "scale": r.view.scale, "entities": len(r.view.handles),
                         "same_as": r.view.same_as,
                         **({"twin": r.view.twin, "split_from": r.view.split_from} if r.view.twin else {}),
                         **({"inside_blocks": True} if r.view.inside_blocks else {})},
                "title_from": r.title_from, "prominent": r.prominent,
                "kind": {"value": r.verdict.kind, "confidence": r.verdict.confidence,
                         "reasons": r.verdict.reasons, "scores": r.verdict.scores},
                "levels": {"candidates": r.levels, "from": r.levels_from,
                           "all_sources": [list(s) for s in r.level_sources],
                           "conflict": r.level_conflict},
                "kind_also": r.verdict.also,
                "wing_from": r.wing_from,
                "notes": summary(r.notes),
                "features": r.features,
            }))
    return building
