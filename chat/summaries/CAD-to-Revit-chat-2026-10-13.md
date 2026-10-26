# Chat: reading drawings more generally, Stilt slabs, Wing C girders
13 October 2026. All work in `Documents\Structure CAD to Revit\New version`. Technical details are in SESSION_NOTES.md there.

---

## User
(Pasted a 12-point list for reading any consultant's drawings: tables as grids of cells, marks without numbers in tables, one table reader, weak or disagreeing values to Review, follow leaders, read hatches and X boxes, slabs only between beams, recognise layers by shape, match column schedule headers so 1050/2050/DETAIL are never column sizes, check sizes against sections, read text however drawn, flag what doesn't make sense.) What do you think? Without breaking anything.

## Claude
Mostly a good list; about half is already in the app and only needs finishing (hatches and X openings, slabs from beam bays, layers by shape, drawn beam sections, most text types). Real gaps: merged table cells, bare marks like ST, column table headers, sanity flags, leaders.

Two points should change:
- **One table reader only (3):** removing the old reader is the biggest risk of breaking things. Keep it as a check; the new reader wins; disagreement is a conflict.
- **One weak source to Review (4):** taken literally, it would undo the slab and beam marks filled in on 13 Oct at the user's request. Show those values as "read from around" instead.

Suggested order: 2, 1, 9, then 12, 10, 5, 11, 6, 4, 3.

## User
Don't break anything please.

## Claude
Did 1, 2 and 9 in the table reader:
- A table's drawn lines now divide its cells: a cell holding several mark lines with one size printed (Stilt A: B-10 ... B-38,39 with one "230 450") gives that size to every line. About 100 beam marks on Stilt A gained their sizes.
- In a table's mark column, marks without a number (ST, HB, BKT, RW, MLB, CS) are rows of their own. Their values no longer fall into the row above (ST's 200 had gone into S-1). On plans a mark still needs a number.
- Sizes written in pieces ("230" "X" "1050") are read as one size. Two different sizes in one cell: neither is taken.
- Item 9's premise was wrong: 1050 and 2050 are real column sizes on Stilt A (230 x 1050), and in B BLOCK the 1050 is the footing depth under "FOOTING SIZE D". Nothing needed rejecting.

## User
Slabs are not modelled properly according to the slab shape. Please don't break anything. Please restart the app after changes.

## Claude
On Stilt A, the sunk S1 strip by the retaining wall had scalloped edges. Beam B89 along the wall is drawn there only as short cross pieces, so the app rounded off the open side. The strip is now built from the drawing's sunk hatch: straight where the drawing is straight, following the wall's real bulge lower down. The middle bays keep the same placeholder mark as before.

Checked by running 12 earlier drawings on the old and the new code and comparing every element by position. 7 identical; the rest only gained correct readings. One real finding: ALCAZAR 1st floor's table says HB is 230 wide but the plan draws three HB beams 150 wide; they now show as a conflict in Review. 21 of 21 test suites pass.

Open question: on Stilt A the side strips run past the top beam up to the break line, as far as the hatch is drawn. Stop at the last beam, or follow the hatch?

## User
I am not going to use those 12 drawings, can I test on a new CAD file?

## Claude
Yes. The 12 drawings are only my check that nothing breaks. Restarted the app (http://127.0.0.1:8765).

## User
The slabs, columns and beams are fine, but a few extra beams have been created. Why? Please don't break anything.

## Claude
New drawing: J1153 Wing C 1st floor. The wide girders GB1, GB2, GB2A, GB3, GB5 are each drawn as two side lines, with the mark written just outside the girder. The app joined the lines only when the mark was inside or very close, so each girder became two beams: one centred on a side line (half a width off) and one extra beam.

Fixed as a general rule:
- Two side lines become one girder when the girder's own mark is within 450 mm of its edge and the gap matches that mark's scheduled width (within 10%). GB2A is drawn 736 but scheduled 700: built at 735, with the difference shown in Review.
- The short line closing a girder's end is not built as a beam.

Left as drawn: the beam along the left edge inside a consultant's comment cloud, one unmarked beam at the top right, and the thin red lines round the sunk areas (slab steps, not beams).

Not one element changed on the 12 earlier drawings. 21 of 21 test suites pass, 552 tests, including three new ones that fail on the old code. App restarted.

## User
OK, this is good, please save.

## Claude
Saved this chat (Desktop and New version) and the session notes.

## User
Now a new issue in export: not all the elements are getting exported, but it builds properly. Only correct the export. Don't break anything.

## Claude
The Rhino export of the A wing 1st floor had only 2 of 71 columns and no walls. Cause: the Export page's floor levels were Level 0 = 4000 and Level 1 = 4000, so Level 1's columns and walls had no height. The walls were even counted as written while coming out 0 mm tall.

The Revit build only looked right: the new file's template already has Level 1 at 0, so Revit kept Level 1 at 0, not the 4000 entered.

With Level 0 = 0 and Level 1 = 4000 the same export writes everything: 71 columns, 283 beams, 113 slabs, all walls, 46 folds.

Changed in the export only (IFC and Rhino):
- If a floor's level is not above the level below, the export stops with a clear message naming both levels, instead of writing a file with columns missing.
- A wall with no height is listed as left out, never counted.

With correct levels the export is unchanged. 21 of 21 test suites pass (554 tests). App restarted.

To do: set Level 0 = 0, Level 1 = 4000 on the Export page and export again. Possible next step: warn about such levels on the Export page before building.

## User
Start app.

## Claude
Started at http://127.0.0.1:8765.

## User
Save the chat and project.

---

## Still open
- Export page: set Level 0 = 0 and Level 1 = 4000 (both are 4000 now), then export again.
- Stilt A: should sunk strips stop at the last beam on a cut plan, or follow the hatch to the break line?
- ALCAZAR 1st floor: HB width, 230 (table) or 150 (plan)?
- Next from the list: sense checks (12), sizes against section drawings (10), leader lines and arrows (5), AutoCAD table objects and split marks on plans (11).
