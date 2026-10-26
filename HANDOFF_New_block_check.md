# Handoff: New block (L2) – what the app modelled wrong (checked 2026-10-10)

Written for Claude working in this folder (VS Code). Read `SESSION_NOTES.md` (the 2026-10-10 entries)
for background. Keep the usual working rules: everything happens in `New version`; run
`run_tests.bat` (or `python tools\run_all_tests.py --baselines`) before and after every change;
nothing that works today may change unnoticed; explain findings to the user in plain terms.

## What was checked

* Drawing: `samples\New block.dwg` (identical, same MD5, to `C:\Users\Admin\Downloads\New block.dwg`,
  which the user's app project uses).
* App project: `C:\Users\Admin\Downloads\res\res` (project "res", `L2=Level 2`, suggested profiles on).
* Run checked: `C:\Users\Admin\Downloads\res\res\runs\2026-10-10_123311`
  (`revit_build\New_block_M1a_plan\model.json`, `export_report.txt`, `building.json`).
  DXF used: `C:\Users\Admin\Downloads\res\res\dxf_cache\New block.dxf`.
* Method: every structural entity in the DXF was drawn under the app's model (columns, beams with
  their widths, slabs with marks, openings) and compared one by one.
  Picture with numbered problems: `New_block_check.png` (in this folder). The numbers below match it.

The run has 19 columns, 78 beams and 35 slabs. Coordinates below are DXF model space (mm).

## A. App bugs (the app reads the CAD wrongly)

### 1. The "Main Block" area is modelled as part of this block
* The DXF has an ANSI31 hatch on layer **`Main Block`** covering exactly
  x 1913986–1919016, y −1619114 to −1600754 (the whole left strip: lifts, stair, corridor).
* That strip has **no slab marks** at all, and its edge beams use the old numbering **B32a–e**
  (this block's own beams are NB…, slabs NS…).
* The app still built ~30 beams, 8 columns and 7 slabs there. The 7 slabs got made-up marks from
  `cad2revit\core\slabs.py:506` ("no slab mark in this bay: mark and thickness of the nearest"):
  NS5, NS2, NS4 ×4, and **NS1 on the stair landing box** (x 1915946–1917056, y −1614909 to −1613944).
* Likely fix: treat an area hatched on a layer named like `Main Block` / existing / another block as
  "not this drawing's structure" (left out, or kept as reference only), and at least never copy a
  nearby slab mark into a bay that lies inside it. **Confirm with the user first** that the left strip
  belongs to the Main Block drawing.

### 2. Six real columns dropped as "open outlines" (confirmed root cause)
* Handles **246, 24C, 251** (375×750, left edge), **258** (230×2460, top left), **256, 259**
  (230×750, between the lift shafts). Each is an LWPOLYLINE with 5 points where the **last point
  equals the first**, but the polyline's closed flag is off. Example 246:
  (1913986,−1618364) → (1914361,−1618364) → (1914361,−1619114) → (1913986,−1619114) → (1913986,−1618364).
* `cad2revit\core\extract.py:432` calls `G.dedupe_points`, which (`cad2revit\core\geometry.py:132`)
  **drops the closing point equal to the first**. Then `extract.py:437-438` sees `closed == False` and
  measures `gap = dist(pts[0], pts[-1])`, which is now a whole side (750 / 2460). Gap > 600, so it
  is a BLOCKER and the column is skipped (`extract.py:452-458`). The 230×600 ones drawn the same way
  are only "repaired" because their side (600) happens to be ≤ `close_wall_end_mm` (600).
  So the 7 "closed across a 600 mm gap" warnings for 248, 24A, 24E, 24F, 250, 257, 25A are false too.
* Fix: decide "closed" **before** dedupe (closed flag OR first point ≈ last point within
  `duplicate_tolerance_mm`). Add a selftest with a 5-point open-flag rectangle of side > 600.

### 3. Middle column line: 70×600 slivers built as separate columns
* On x ≈ 1923200–1923460 each column is drawn as **two touching rectangles**: 70×600 (handles 222,
  228, 22A, 22C at x 1923196) + 230×600 (22E, 22F, 230, 231 at x 1923346). Together that is
  **300×600**, which matches the 300-wide NB5a–e beams on that line and the 300×600 end columns on
  the same line (232 and 239, drawn as single 600×300 rectangles).
* The app built 8 columns (four of them 70×600, only warned "outside the expected range").
* Fix: merge column rectangles on the same layer that share a full edge (same length, touching)
  into one column, before the size check.

### 4. Three concrete columns get the mark "SC1"
* "SC1" comes from block **G$CECFCBF78** (inserted on layer `STEEL` at (1927456,−1601354)): 22
  polylines on layer `Steel Col` (300×550 / 280×530) at y −1600353 and −1623328, x from 1914100 to
  **1936681**. That is 9 m past the right edge of this plan, so the block doesn't fit this plan
  (pasted from elsewhere, or steel posts outside the slab). Its 11 TEXTs "SC1" (layer 0) were
  attached by the linker to the nearest RCC columns 238, 239, 25A (`column SC1 ... [read (plan caption)]`
  in `export_report.txt`).
* Fix: a column caption should not attach to a column when it sits next to an outline of its own
  block/layer (here `Steel Col`) that is nearer, or when the caption comes from a block whose
  geometry lies mostly outside the plan. At the least, flag it instead of reading it.

### 5. Half of an NB2 becomes "NB1"
* The drawing has the texts "NB2" and "NB1" (layer `BEAM NO`) at **exactly the same point**
  (1925495, −1605359) on the right-hand NB2 (y ≈ −1605589). A drafting slip.
* The extractor rightly did not place NB1 (blocker at `extract.py:2547`), but the linker
  (`building\linker.py` ~140-191, the span splitter) still used both captions and cut the beam
  midway. The right half is now a span "NB1" with width and depth guessed (230×600, red, provisional).
* Fix: captions at the same spot (or a caption the extractor rejected as stacked) must not split a
  beam. Report them as ambiguous and keep the beam whole with the mark the extractor gave (NB2,
  230×450, matching the mirror NB2 on the left).
* Quick workaround for the user: in Review set that span's mark to NB2.

### 6. One toilet slab piece has the wrong mark (still open from earlier)
* Slab x 1926743–1927606, y −1608504 to −1607524 (far right, upper toilet row, sunk 600):
  modelled **NS2**, should be **NS4** like the rest of that row (the NS2 bay above it leaks down).
  Already noted in SESSION_NOTES "(later)" entry. Quick workaround: set mark NS4 in Review.

## B. Drawing gaps / conflicts (need a decision; show them in Review, don't guess)

7. **RCC projections not modelled**: 4 polylines on layer `Projection` (balconies above and below the
   plan, ~2.9 × 1.25 m each). Legend: "*THUS MARKED 150 mm DOWN RCC PROJECTION". The 150 mm sunk
   zones are read, but there is no slab under them. Needs a slab-from-outline path for `Projection`
   (thickness from the general note, 125, dropped 150).
8. **SB depth is given nowhere**: every TEXT/MTEXT in the file was searched, including inside
   blocks. 10 SB beams are built at a guessed 600. The user must ask the engineer. Width 230 is measured.
9. **NB3a / NB3b**: drawn on layer `BEAM-230X900`, but the beam sections say 230×750. The app used 750
   and marked it "read". The layer-name size should go in as a disagreeing source, so it shows as a
   conflict in Review (today the 900 is dropped without telling the user).
10. **Stair beams (inside the Main Block area)**: `BEAM-300X450` faces at y −1613494 / −1613944 are
    450 apart, so the beam was built 450×450. `BEAM-150X450` faces at y −1614909 / −1615024 are 115
    apart, so it was built 115×450. A measured width that disagrees with the layer-name width should be
    flagged, not taken silently.
11. **B32b built 1325 deep**: it is on layer `BEAM-230X1325` (faces x 1918786 / 1919016). That is
    what the drawing says, but the whole stair block (G$CA5CD48FA) was also put on that layer, so it
    needs confirming.

Also still open: no grid in the drawing (setting-out blocker); 19 columns have no mark.

## Suggested order

1. #2 (root cause known, small, safe), then #3, #5, #4. Each one: run the baselines before and after,
   and add a selftest that fails before the fix.
2. Ask the user about #1 (is the left strip the Main Block?) before changing anything for it.
3. #9/#10: add the layer-name size as a disagreeing source, so these appear as conflicts.
4. Re-run the user's project, regenerate the comparison picture, check the counts
   (expect 19 + 6 − 4 = 21 columns if the slivers merge and the 6 come back), and add a SESSION_NOTES entry.
5. After any change to `building\*.py`, restart the app (close the "CAD to Revit" window, run `run_app.bat`).

## Reproducing the comparison

Run the scripts with `python -I` (they only read files):
* A survey of layers and texts, and the overlay picture, came from small ezdxf + matplotlib scripts.
  To redo it: draw every LINE/LWPOLYLINE on the beam, column, cut-out, stair and Projection layers
  (opening plan-scale INSERTs as virtual entities, except the `S-B5-230X600` rebar symbols), then draw
  `model.json` beams as rectangles (centreline ± width/2), columns from `outline`, slabs from `outline`
  with their marks.
