"""List a drawing's layers with a suggested role.

This is the first thing to run on a drawing from a consultant whose layer names
you have not seen before. It does not extract anything; it tells you what to put
in config.json so that extraction will work.
"""
from __future__ import annotations

import collections
import json
import os

import ezdxf

from . import blocks
from .reader import LayerMap, guess_role


def inspect(dxf_path: str, cfg: dict) -> list[dict]:
    doc = ezdxf.readfile(dxf_path)
    msp = doc.modelspace()
    lmap = LayerMap(cfg["layer_roles"])

    # Count what extraction will actually see, which means looking inside any
    # block inserted at plan scale. A drawing whose whole column grid is one
    # block otherwise reads as having no columns at all.
    ents = blocks.flatten(msp, cfg, None, log=lambda *a: None)
    loose = collections.Counter(e.dxf.layer for e in msp)

    count = collections.Counter()
    kinds = collections.defaultdict(collections.Counter)
    for e in ents:
        count[e.dxf.layer] += 1
        kinds[e.dxf.layer][e.dxftype()] += 1

    rows = []
    for layer in sorted({l.dxf.name for l in doc.layers} | set(count)):
        n = count[layer]
        current = lmap.role_of(layer)
        rows.append({
            "layer": layer,
            "entities": n,
            "types": ", ".join(f"{t}:{c}" for t, c in kinds[layer].most_common(3)),
            "current_role": current,
            "suggested_role": guess_role(layer) if current == "unmapped" else current,
            "in_blocks": n - loose[layer],
        })
    rows.sort(key=lambda r: (-r["entities"], r["layer"]))
    return rows


def format_table(rows: list[dict], dxf_path: str) -> str:
    out = [f"\nLayers in {os.path.basename(dxf_path)}", "=" * 96,
           f"{'LAYER':<30} {'ENTS':>6} {'INBLK':>6}  {'READ AS':<14} {'SUGGESTION':<14} CONTENTS",
           "-" * 104]
    for r in rows:
        if r["entities"] == 0:
            continue
        cur = r["current_role"]
        sug = r["suggested_role"] if cur == "unmapped" else ""
        blk = str(r.get("in_blocks") or "")
        out.append(f"{r['layer'][:30]:<30} {r['entities']:>6} {blk:>6}  "
                   f"{cur:<14} {sug:<14} {r['types']}")

    nested = sum(r.get("in_blocks") or 0 for r in rows)
    if nested:
        out.append("")
        out.append(f"INBLK = of those entities, how many are inside a block reference "
                   f"({nested} in total).")
        out.append("They are counted because blocks inserted at plan scale are opened out "
                   "before extraction.")

    todo = [r for r in rows if r["current_role"] == "unmapped" and r["entities"] > 0]
    out.append("-" * 96)
    if not todo:
        out.append("Every layer in use is mapped. Run the extraction.")
    else:
        out.append(f"{len(todo)} layers in use are not mapped yet. Paste the block below into")
        out.append("config.json under \"layer_roles\", merging it with what is already there,")
        out.append("then run the extraction.\n")
        out.append(suggest_config(rows))
    return "\n".join(out)


def suggest_config(rows: list[dict]) -> str:
    """A ready-made layer_roles fragment covering everything still unmapped."""
    by_role: dict[str, list[str]] = collections.defaultdict(list)
    for r in rows:
        if r["current_role"] == "unmapped" and r["entities"] > 0:
            by_role[r["suggested_role"]].append(r["layer"])
    if not by_role:
        return "{}"

    unknown = by_role.pop("unmapped", [])
    lines = ["{"]
    for role in sorted(by_role):
        lines.append(f'  "{role}": {json.dumps(sorted(by_role[role]))},')
    if lines[-1].endswith(","):
        lines[-1] = lines[-1][:-1]
    lines.append("}")
    text = "\n".join(lines)
    if unknown:
        text += ("\n\nNo guess for these - decide yourself, or add them to \"ignore\":\n  "
                 + ", ".join(unknown))
    return text
