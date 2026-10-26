#!/usr/bin/env bash
# Rebuilds CAD-to-Revit-Presentation.html from the slide files in source/.
# Slide order is read from source/project/deck.json, so edit that to reorder.
set -e
cd "$(dirname "$0")"

OUT="CAD-to-Revit-Presentation.html"
ORDER=$(sed -n '/"order"/,/\]/p' source/project/deck.json | grep -o '"[a-z][a-z-]*"' | grep -v '"order"' | tr -d '"')

cat source/_head.html > "$OUT"
for id in $ORDER; do
  f="source/project/slides/$id.html"
  if [ ! -f "$f" ]; then
    echo "missing slide file: $f" >&2
    exit 1
  fi
  cat "$f" >> "$OUT"
  printf '\n' >> "$OUT"
done
cat source/_tail.html >> "$OUT"

echo "built $OUT from $(echo "$ORDER" | wc -w | tr -d ' ') slides"
