r"""What of a drawing the app read, and what it left: every layer and every text.

    python tools\coverage.py RUN_FOLDER DRAWING.dxf [--all]

For each layer: how many entities and texts it holds, and how many of them the
app used - as an element (its outline or line), a mark, a size, a schedule cell,
a level, a note. Then the texts nobody used, grouped by layer, so a reader that
misses something shows up. Read-only: it changes nothing.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def used_handles(building: dict) -> set:
    """Every DXF handle the building's elements, values and issues point to."""
    out = set()

    def walk(x):
        if isinstance(x, dict):
            for k, v in x.items():
                if k in ("handle", "_handle") and isinstance(v, str) and v:
                    out.add(v.upper())
                else:
                    walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    walk(building.get("elements") or [])
    walk(building.get("issues") or [])
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run")
    ap.add_argument("dxf")
    ap.add_argument("--all", action="store_true", help="list every unused text, not the first 25 per layer")
    a = ap.parse_args(argv)
    from ezdxf import recover
    with open(os.path.join(a.run, "building.json"), encoding="utf-8") as f:
        b = json.load(f)
    used = used_handles(b)
    doc, _ = recover.readfile(a.dxf)
    ents = Counter()
    texts = defaultdict(list)                        # layer -> [(handle, text, used)]
    geo_used = Counter()
    for e in doc.modelspace():
        lay = e.dxf.layer
        ents[lay] += 1
        h = e.dxf.handle.upper()
        if e.dxftype() in ("TEXT", "MTEXT", "ATTRIB"):
            t = e.plain_text() if e.dxftype() == "MTEXT" else e.dxf.text
            if t.strip():
                texts[lay].append((h, " ".join(t.split())[:70], h in used))
        elif h in used:
            geo_used[lay] += 1
        if e.dxftype() == "INSERT":
            for at in e.attribs:
                if at.dxf.text.strip():
                    texts[at.dxf.layer].append((at.dxf.handle.upper(), at.dxf.text.strip()[:70],
                                                at.dxf.handle.upper() in used))
    print(f"{'LAYER':32s} {'ENTITIES':>8s} {'SHAPES USED':>11s} {'TEXTS':>6s} {'TEXTS USED':>10s}")
    for lay in sorted(ents, key=lambda l: -ents[l]):
        tl = texts.get(lay, [])
        nt, nu = len(tl), sum(1 for x in tl if x[2])
        print(f"{lay[:32]:32s} {ents[lay]:8d} {geo_used[lay]:11d} {nt:6d} {nu:10d}")
    tot = sum(len(v) for v in texts.values())
    tu = sum(1 for v in texts.values() for x in v if x[2])
    print(f"\nTexts: {tot}, used {tu} ({100 * tu / max(tot, 1):.0f}%)")
    print("\nTEXTS NOT USED, by layer:")
    for lay, tl in sorted(texts.items(), key=lambda kv: -sum(1 for x in kv[1] if not x[2])):
        un = [x[1] for x in tl if not x[2]]
        if un:
            shown = un if a.all else un[:25]
            print(f"  [{lay}] {len(un)}: " + " | ".join(shown) + (" ..." if len(un) > len(shown) else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
