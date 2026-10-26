"""Run the whole job: DWG in, cleaned DXF and data files out."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from datetime import datetime

from . import cleanout, dwgconv, extract, markup, report
from .model import BLOCKER, Result

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CONFIG = os.path.join(HERE, "config.json")


def load_config(path: str | None = None) -> dict:
    with open(path or DEFAULT_CONFIG, "r", encoding="utf-8") as f:
        return json.load(f)


FLOOR_WORDS = {"GROUND": 0, "PLINTH": 0, "STILT": 0, "FIRST": 1, "SECOND": 2,
               "THIRD": 3, "FOURTH": 4, "FIFTH": 5, "SIXTH": 6, "SEVENTH": 7,
               "EIGHTH": 8, "NINTH": 9, "TENTH": 10}
ROOF_WORDS = ("TERRACE", "ROOF", "TOP SLAB", "OHT", "HEAD ROOM", "MUMTY")


def guess_level(src: str) -> str:
    """A Revit level name from the drawing's name, for the user to confirm.

    '1ST FLOOR PLAN' -> 'Level 1', 'GROUND FLOOR' -> 'Level 0', 'TERRACE' ->
    'Terrace'. The same reading build_model.py falls back on, offered up front
    so the level is chosen with the drawing rather than discovered in Revit.
    """
    import re
    t = os.path.basename(src or "").upper()
    if any(w in t for w in ROOF_WORDS):
        return "Terrace" if "TERRACE" in t else "Roof"
    m = re.search(r"\b(\d+)\s*(?:ST|ND|RD|TH)?\s*(?:FLOOR|FLR|SLAB)\b", t)
    if m:
        return f"Level {int(m.group(1))}"
    for word, n in FLOOR_WORDS.items():
        if re.search(rf"\b{word}\s*(?:FLOOR|FLR|SLAB)\b", t):
            return f"Level {n}"
    return ""


def run(src: str, out_dir: str | None = None, config_path: str | None = None,
        log=print, level_name: str | None = None) -> tuple[Result, str, list[str]]:
    """Returns (result, output folder, list of files written).

    level_name is the Revit level this floor goes on; it is written into
    model.json for the Revit script to build on.
    """
    src = os.path.abspath(src)
    if not os.path.exists(src):
        raise FileNotFoundError(src)

    cfg = load_config(config_path)
    stem = os.path.splitext(os.path.basename(src))[0]
    if out_dir is None:
        out_dir = os.path.join(os.path.dirname(src),
                               f"{stem}_revit_{datetime.now():%Y-%m-%d_%H%M}")
    os.makedirs(out_dir, exist_ok=True)

    log(f"Source: {os.path.basename(src)}")
    log(f"Output: {out_dir}")
    level_name = (level_name or "").strip()
    log(f"Revit level: {level_name}" if level_name else
        "Revit level: not set, so the Revit script will guess it from the drawing's name")

    tmp = tempfile.mkdtemp(prefix="c2r_")
    try:
        dxf = dwgconv.to_dxf(src, os.path.join(tmp, stem + ".dxf"), log=log)
        res = extract.extract(dxf, cfg, log=log)
        res.source_file = src
        res.level_name = level_name

        log("  Writing the cleaned DXF")
        clean = cleanout.write_clean_dxf(res, os.path.join(out_dir, "cleaned.dxf"), cfg)

        log("  Marking the queries up on a copy of the drawing")
        marks = markup.write_markup_dxf(
            res, os.path.join(out_dir, f"{stem} QUERIES.dxf"), dxf,
            comment_on=tuple(cfg.get("markup_comment_on", ["blocker"])))

        log("  Writing data files")
        files = [clean, marks]
        files += report.write_csvs(res, out_dir)
        files.append(report.write_json(res, out_dir))
        files.append(report.write_html(res, out_dir, files))

        counts = res.counts()
        ic = res.issue_counts()
        log("")
        log("Done. " + ", ".join(f"{v} {k}" for k, v in counts.items() if v))
        log(f"      {ic.get(BLOCKER, 0)} blockers, {ic.get('warning', 0)} warnings, "
            f"{ic.get('info', 0)} notes")
        return res, out_dir, files
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
