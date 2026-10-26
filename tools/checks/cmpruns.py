"""Compare two runall outputs element by element (by position, not by id).
python cmpruns.py BEFORE AFTER"""
import json
import os
import sys


def r(v):
    return None if v is None else round(float(v))


def key_beam(b):
    pts = sorted([tuple(r(x) for x in b["start"][:2]), tuple(r(x) for x in b["end"][:2])])
    return ("beam", tuple(pts), tuple(r(x) for x in (b.get("mid") or [0, 0])[:2]))


def val_beam(b):
    return (b.get("mark"), r(b.get("width")), r(b.get("depth")), b.get("kind"), r(b.get("top_mm")))


def key_poly(t, e):
    ol = e.get("outline") or []
    if not ol:
        return (t, (r(e.get("x")), r(e.get("y"))))
    xs, ys = [p[0] for p in ol], [p[1] for p in ol]
    return (t, (r(sum(xs) / len(xs)), r(sum(ys) / len(ys)), len(ol)))


def items(d):
    out = {}
    for b in d.get("beams", []):
        out[key_beam(b)] = val_beam(b)
    for t, f in (("column", ("mark", "width", "depth")), ("wall", ("mark", "thickness")),
                 ("slab", ("mark", "thickness", "sunk_mm")), ("fold", ("width", "depth")),
                 ("opening", ("kind",)), ("sunk", ("depth",))):
        for e in d.get(t + "s", d.get(t, [])) or []:
            out[key_poly(t, e)] = tuple(e.get(k) if not isinstance(e.get(k), (int, float)) else r(e.get(k)) for k in f)
    return out


def main():
    a, b = sys.argv[1], sys.argv[2]
    same = changed = 0
    for proj in sorted(os.listdir(a)):
        pa = os.path.join(a, proj)
        if not os.path.isdir(pa):
            continue
        for plan in sorted(os.listdir(pa)):
            fa, fb = os.path.join(pa, plan, "model.json"), os.path.join(b, proj, plan, "model.json")
            if not os.path.exists(fa):
                continue
            if not os.path.exists(fb):
                print("MISSING AFTER", proj, plan)
                changed += 1
                continue
            A, B = items(json.load(open(fa, encoding="utf-8"))), items(json.load(open(fb, encoding="utf-8")))
            gone = sorted(set(A) - set(B))
            new = sorted(set(B) - set(A))
            diff = sorted(k for k in set(A) & set(B) if A[k] != B[k])
            if not (gone or new or diff):
                same += 1
                continue
            changed += 1
            print(f"== {proj} | {plan}: {len(gone)} gone, {len(new)} new, {len(diff)} changed")
            for k in gone[:12]:
                print("   - ", k, A[k])
            for k in new[:12]:
                print("   + ", k, B[k])
            for k in diff[:12]:
                print("   ~ ", k, A[k], "->", B[k])
    print(f"\n{same} plans identical, {changed} with differences")


if __name__ == "__main__":
    main()
