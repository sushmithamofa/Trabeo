# Structural CAD Standard for Automated Revit Modelling

| | |
|---|---|
| **Project** | J-1153, Reis Magos Goa Housing |
| **Issued by** | MOFA Studios Pvt. Ltd. |
| **For** | The structural consultant |
| **File audited** | `1153 - 1ST FLOOR PLAN ( C WING ) 14-07-2026.dwg` |
| **Version** | 1.0, 25 September 2026 |

---

## 1. What this document is for

We generate the structural Revit model from your CAD drawing instead of redrawing it
by hand. A script reads the DWG layers, matches each shape to its mark, joins the
mark to the schedule row, and places the Revit element. Anything the script cannot
read with certainty is not guessed; it comes back to you as a written query.

This only works if the drawing is consistent in a few specific ways. This document
sets out those ways.

**What we are not asking for.** We are not asking you to model in Revit, to change
how the drawing looks when printed, to redraw existing work, or to adopt a new
CAD package. Every rule below is about how entities are placed on layers in the
DWG you already produce.

**What changes for you.** Roughly six drafting habits, listed in section 5, plus
three schedules delivered as Excel alongside the DWG. Nothing in section 5 takes
longer to draw than what is being drawn today.

**Why it is worth it.** On the audited file, 4,200 entities carry the whole first
floor of one wing. Reading them automatically takes minutes. Redrawing them takes
days, and takes days again at every reissue.

---

## 2. Status of the current file

We read `1153 - 1ST FLOOR PLAN ( C WING ) 14-07-2026.dwg` in full. Appendix A lists
what we found, with counts. In summary:

**Already correct and requiring no change**

- AutoCAD 2010 format, `INSUNITS` = 4 (millimetres), `MEASUREMENT` = 1 (metric), drawn 1:1.
- Columns, beams, girders, floating columns and the sunk zones are already on their own layers.
- Column outlines are real closed rectangles: de-rotated, they give exact schedule sizes (230 x 950, 230 x 1200, 350 x 750 and so on).
- Beams are multilines at a true 230 width, girders at 350.
- All marks are live text, not exploded geometry, at consistent heights (250 mm on columns and beams, 300 mm on slabs).
- No external references. Nothing is xref'd in, so the file is self-contained.
- The plan is orthogonal to itself: beam multilines run at 2 degrees and 92 degrees with no drift.

**Blocking automation today**

| # | What we found | Section that fixes it |
|---|---|---|
| 1 | The `SLAB` layer holds 6 open fragments. There is no closed outline anywhere to build a Revit floor from, against 65 slab tags. | 5.5 |
| 2 | Beam multilines use **top** justification, so every drawn line is a beam face, not a centreline: 115 mm off for beams, 175 mm for girders. | 5.3 |
| 3 | There is no grid. No grid lines, no bubbles, no grid layer anywhere in the file. | 4.5 |
| 4 | The plan sits about 2.9 km from the drawing origin with no point agreed against the architectural model. | 4.4 |
| 5 | All three schedules are loose text, not tables. The beam schedule alone is 1,934 separate text objects. | 6 |
| 6 | Sunk layers are named `300mm SUNK` and `900MM SUNK`; the legend on the same sheet calls for 100, 150 and 400 sunk. | 4.6, 5.6 |
| 7 | 375 entities sit at a Z other than zero, and the drawing extents report a Z of 30,544,084 mm. | 4.3 |

---

## 3. Deliverables, per floor, per issue

| # | Deliverable | Format |
|---|---|---|
| 1 | Structural plan, one floor per file | DWG, AutoCAD 2010 or later |
| 2 | Column schedule | XLSX, one row per mark |
| 3 | Beam and girder schedule | XLSX, one row per mark |
| 4 | Slab schedule | XLSX, one row per mark |
| 5 | Level table, plinth to terrace | XLSX or PDF |
| 6 | Sections through sunk areas, pool, deck, stairs and lift | PDF |
| 7 | Revision note listing what changed since the previous issue | PDF or in the DWG title block |

**File naming**

```
J1153_<Wing>_<Level>_STR_<YYYY-MM-DD>.dwg
J1153_<Wing>_<Level>_STR_<YYYY-MM-DD>_COL.xlsx
J1153_<Wing>_<Level>_STR_<YYYY-MM-DD>_BEAM.xlsx
J1153_<Wing>_<Level>_STR_<YYYY-MM-DD>_SLAB.xlsx
```

Example: `J1153_C_01_STR_2026-07-14.dwg`

---

## 4. Drawing setup

### 4.1 Units and scale

Millimetres, drawn 1:1. `INSUNITS` = 4, `MEASUREMENT` = 1. Nothing scaled, stretched
or exploded. *The current file already complies.*

### 4.2 One floor per file

Model space holds the structural plan for one floor only. Schedules, legends,
details, title block and key plan may sit beside the plan in model space, but on
their own layers (section 6) so they can be ignored by the reader.

### 4.3 Flat at zero

Every entity at Z = 0. Run `FLATTEN` before issue, then check that `EXTMIN` and
`EXTMAX` both report Z = 0.

**In the audited file:** 375 entities carry a Z other than zero — 244 of them at Z = −229,
and others at −11,425, −11,197, −8,962, +7,506, +14,784. Drawing extents report a
Z range of −22,493 to 30,544,084 mm. A script reading plan coordinates has to
discard Z entirely, which also means it cannot trust any Z it does find.

### 4.4 Origin and setting out

- One base point, shared with the architectural model, agreed once for the whole project.
- The same XY position for every floor and every wing. A floor must sit directly above the floor below with no shift.
- Never re-rotate the plan between issues. The current 2 degree rotation is fine and can stay; it simply must not change.
- Mark the shared base point with a single point or small cross on layer `S-REF`.

**In the audited file:** the plan occupies about 25.4 m x 56.3 m, positioned roughly 2.9 km
from the drawing origin, with nothing identifying a shared point.

### 4.5 Grid

The structural grid goes on `S-GRID`: grid lines as lines, bubbles as circles with
the grid label as text inside. If the structural grid is the architect's grid, copy
it in from the architectural file rather than redrawing it.

**In the audited file:** there is no grid of any kind. Columns are numbered 1 to 32 with no
lines to locate them against. There are 34 dimensions on the `DIM` layer, but with
no grid there is nothing for them to dimension from.

### 4.6 Housekeeping before issue

- `OVERKILL` run, so no entity is drawn twice on top of another.
- `PURGE` run, so empty layers, unused blocks and unused styles are gone.
- `AUDIT` run, with errors fixed.
- No structural element inside a block. Blocks are fine for title blocks, logos, section marks and stair symbols; not for columns, beams, slabs or walls.
- Layer names spelled one way only.

**In the audited file:** 10 of 51 layers carry no entities; `RCC PARDI` and `R.C.C. PARDI`
both exist as layers with only the second one used; 2 column outlines are exact
duplicates lying on top of each other. 69 block definitions exist, none of which
contain structural elements, which is correct.

---

## 5. How each element is drawn

### 5.1 Columns

- One **closed** rectangle per column, drawn at the scheduled size, on `S-COL`.
- Columns that stop at this floor go on `S-COL-STOP` instead, so we know not to carry them up.
- Floating columns go on `S-COL-FLOAT`.
- Nothing else on these layers. No hatch, no text, no detail linework.

**In the audited file:** 68 outlines on `COLUMN`, of which 61 are closed rectangles that
de-rotate to exact schedule sizes, 2 are closed 17-point outlines (the lift walls),
and 5 are **open** 5-point polylines that a script cannot fill. A further 8
rectangles of 495 x 1141 sit on the `COLUMN` layer at 0 degrees, a size that appears
nowhere in the column schedule; please confirm what these are. The 6 outlines on
`FL COL` are all correctly closed.

### 5.2 Walls

Lift walls (LW-1 to LW-4) and the RCC pardi walls go on `S-WALL` as closed
outlines, with their thickness as drawn. They are not columns and must not sit on
the column layer.

**In the audited file:** the lift wall pieces are drawn on `COLUMN` as 230 x 2110 and
230 x 1950 rectangles and as two 17-point outlines. The RCC pardi is on
`R.C.C. PARDI`, one closed and one open outline.

### 5.3 Beams and girders — the most important rule

- **One centreline per span**, on `S-BEAM` for beams and `S-GIRDER` for girders.
- If drawn as a multiline, set justification to **Zero**, not Top or Bottom.
- One entity type throughout. Pick multiline or polyline and stay with it.
- Girders drawn at the scheduled width, not at a single nominal width for all of them.
- Every line parallel to the grid, or at the true angle of that part of the building.

**In the audited file:**

| Finding | Count |
|---|---|
| Beam multilines, width 230, justification **TOP** | 113 |
| Girder multilines, width 350, justification **TOP** | 2 |
| Beams drawn as plain lines instead | 49 |
| Beams drawn as open polylines instead | 8 |
| Girders drawn as plain lines | 12 |
| Girder tags on the plan, against 2 girder multilines | 9 |

Top justification means the line in the file lies on one face of the beam, 115 mm
from its centre (175 mm for a 350 girder). A script can shift a line by 115 mm, but
nothing in the file says which side to shift it to, and guessing wrong puts a beam a
full width away from its columns.

Beam multilines are clean on angle: 59 at 92 degrees and 56 at 2 degrees. The plain
lines are not: 13 run at 69 degrees, which is the angled wing and correct, but 2 run
at 0 degrees. On `GIRDER BEAM`, 2 lines run at 0 degrees, 2 at 85 and 2 at 6, where
2 or 92 is expected.

### 5.4 Slabs

- **One closed outline per slab panel**, on `S-SLAB`.
- Edges land on the beam centrelines, or on the slab edge where there is no beam.
- One tag per panel, even where several panels share the same slab type.

**In the audited file:** the `SLAB` layer holds 6 open polylines of 3 and 4 points. There is
nothing closed to convert into a Revit floor. The 65 slab tags cover 19 unique
marks, so several panels share a mark: S1 is used 14 times, S3 12 times, S4 8 times,
S8 6 times. Each of those placements needs its own outline.

### 5.5 Sunk areas, openings and stairs

- A **closed outline** for each sunk zone on `S-SUNK-100`, `S-SUNK-150` or `S-SUNK-400`, named for its depth in millimetres. Add layers for other depths as the same pattern.
- Pool and deck slabs on `S-SUNK-POOL` and `S-SUNK-DECK`.
- A hatch may sit on top for printing, but the closed outline must exist. A hatch alone is not enough.
- Openings and cut-outs as closed outlines on `S-OPEN`.
- Stairs and landings as closed outlines on `S-STAIR`.

**In the audited file:** sunk zones exist only as hatches, 12 on `300mm SUNK` (pattern ANSI33)
and 7 on `900MM SUNK` (pattern FLEX), with 1 more on `SUNK` and 10 on `HATCH`. The
legend on the same sheet lists 100MM SUNK, 150MM SUNK, 400MM SUNK, POOL SLAB and
DECK SLAB. The layer names and the legend do not agree, and we cannot tell which
hatch is which depth.

### 5.6 What must not be mixed

One layer, one kind of thing. Do not place tags, hatches, dimensions or detail
linework on an element layer, and do not place two element types on one layer.

---

## 6. Layer standard

Element geometry and its tag are always on separate layers, so the reader can pick
up shapes and text independently.

| Layer | Contents | Entity type |
|---|---|---|
| `S-COL` | Columns continuing above this floor | Closed polyline |
| `S-COL-STOP` | Columns stopping at this floor | Closed polyline |
| `S-COL-FLOAT` | Floating columns | Closed polyline |
| `S-WALL` | Lift walls, RCC pardi, retaining walls | Closed polyline |
| `S-BEAM` | Beam centrelines | Line or polyline |
| `S-GIRDER` | Girder centrelines | Line or polyline |
| `S-SLAB` | One closed outline per slab panel | Closed polyline |
| `S-SUNK-100` | Sunk 100 zones | Closed polyline |
| `S-SUNK-150` | Sunk 150 zones | Closed polyline |
| `S-SUNK-400` | Sunk 400 zones | Closed polyline |
| `S-SUNK-POOL` | Pool slab | Closed polyline |
| `S-SUNK-DECK` | Deck slab | Closed polyline |
| `S-OPEN` | Openings and cut-outs | Closed polyline |
| `S-STAIR` | Stair flights and landings | Closed polyline |
| `S-GRID` | Grid lines and bubbles | Line, circle, text |
| `S-REF` | Shared base point, level marks | Point, text |
| `S-COL-TAG` | Column marks | Text |
| `S-WALL-TAG` | Wall marks | Text |
| `S-BEAM-TAG` | Beam marks | Text |
| `S-GIRDER-TAG` | Girder marks | Text |
| `S-SLAB-TAG` | Slab marks | Text |
| `S-HATCH` | Hatches for printing only | Hatch |
| `S-DIM` | Dimensions | Dimension |
| `S-ANNO` | Notes, legend, schedules, title block, details | Any |

If you already run a layer standard of your own, send it to us. Mapping our reader
onto your names is a ten-minute job and is preferable to you changing how you draft.
What matters is that the mapping is one-to-one and does not change between issues.

**Mapping from the current file**

| Current layer | Becomes |
|---|---|
| `COLUMN` | `S-COL`, with the lift wall pieces moved to `S-WALL` |
| `FL COL` | `S-COL-FLOAT` |
| `BEAM` | `S-BEAM` |
| `GIRDER BEAM` | `S-GIRDER` |
| `SLAB` | `S-SLAB` |
| `R.C.C. PARDI`, `RCC PARDI` | `S-WALL` (one layer, not two) |
| `300mm SUNK`, `900MM SUNK`, `SUNK`, `HATCH` | `S-SUNK-<depth>`, named for the real depth |
| `colno` | `S-COL-TAG` |
| `BEAM NO (H)`, `BEAM NO (V)` | `S-BEAM-TAG` (one layer, not two) |
| `SLAB NO` | `S-SLAB-TAG` |
| `COL HATCH`, `GIRDER-HATCH`, `RET WALL HATCH` | `S-HATCH` |
| `DIM` | `S-DIM` |
| `SHEET`, `MAIN TEXT`, `Col. sch.`, `beam sch. text`, `slab sch. text`, `beam & slab detail` | `S-ANNO` |

---

## 7. Marks and tags

### 7.1 Format

| Element | Prefix | Example |
|---|---|---|
| Column | `C-` | `C-12` |
| Floating column | `FC-` | `FC-3` |
| Lift wall | `LW-` | `LW-2` |
| Beam | `B-` | `B-34` |
| Cantilever beam | `BC-` | `BC-6` |
| Plinth beam | `BP-` | `BP-1` |
| Mid-landing beam | `MLB-` | `MLB-1` |
| Girder beam | `GB-` | `GB-5` |
| Slab | `S-` | `S-4A` |
| Staircase slab | `ST-` | `ST-2A` |

### 7.2 Placement

1. **One mark per text object.** A text object reading `B107/BP` or `/B60` describes two elements and cannot be split reliably. Use two text objects.
2. **Within 300 mm** of the element it labels, or inside it. Where a leader is needed for clarity in print, keep the text within 300 mm and let the leader run the other way.
3. **One tag per element**, once. No duplicated tags, no tag without an element, no element without a tag.
4. Real text or mtext, never exploded to lines.

**In the audited file:** tag placement is the single most uneven area.

| | Column tags | Beam tags |
|---|---|---|
| Tags found | 48 | 123 |
| Nearest element, minimum | 191 mm | 22 mm |
| Nearest element, median | 593 mm | 90 mm |
| Nearest element, maximum | 1,446 mm | 2,705 mm |
| More than 300 mm away | 41 of 48 | 27 of 123 |
| More than 1,000 mm away | 3 of 48 | 4 of 123 |

Beam tagging is mostly good — the median beam tag is 90 mm from its beam. The
outliers are the problem: B87 at 2,705 mm, B54 at 2,225 mm, BP3 at 1,299 mm and
BP2 at 1,279 mm are closer to a different beam than to their own.

Column tagging fails the rule almost everywhere, with a median of 593 mm. There are
also 48 tags against 74 column and floating-column outlines, so 26 outlines carry no
mark at all.

Compound marks currently in use that need splitting: `/B60`, `B33/BP`, `B35/BP`,
`B91/BP`, `B107/BP`, `BC6/BP`.

---

## 8. Schedules as data

Send the column, beam and slab schedules as Excel, in addition to drawing them on
the sheet. The drawn schedule stays exactly as it is for printing; the Excel is a
parallel delivery, not a replacement.

**Rules**

1. One row per mark. Grouped rows are split before delivery: `B-2,4,7 | 230x600` becomes three rows.
2. One value per cell. No units inside the cell, no ranges, no "as above", no merged cells.
3. A header row with stable column names that do not change between issues.
4. Every row complete. A blank depth or an unreadable size stops that element being modelled.

**Use the workbook issued with this document.** `J1153_C_01_STR_2026-07-14.xlsx`
carries one sheet per element type with the columns already set out, the marks we
read from your drawing already entered in the Mark column, and dropdowns on the
fields that must match a fixed list. Its Read Me sheet repeats these rules.

The columns are, per sheet, with the required ones marked *:

| Sheet | Columns |
|---|---|
| **Levels** | Level Name* / Structural Level SSL (mm)* / Floor to Floor (mm) / Datum Note |
| **Columns** | Mark* / CAD tag / Type* / Width (mm)* / Depth (mm)* / Base Level* / Top Level* / Concrete Grade* / Main Steel / Links / Continues Above / Remark |
| **Beams** | Mark* / CAD tag / Type* / Width (mm)* / Depth (mm)* / Level* / Top of Beam Offset (mm) / Concrete Grade* / Top Steel / Bottom Steel / Extra Top / Stirrups / Sunk Depth (mm) / Remark |
| **Slabs** | Mark* / CAD tag / Thickness (mm)* / Level* / Top Offset (mm) / Concrete Grade* / Span Type / Sunk Depth (mm) / Top Steel / Bottom Steel / Remark |
| **Walls** | Mark* / CAD tag / Thickness (mm)* / Base Level* / Top Level* / Concrete Grade* / Vertical Steel / Horizontal Steel / Remark |
| **Sunk and Openings** | ID* / Type* / Depth (mm)* / Level* / In Slab Panel / Where / Remark |
| **Stairs** | Mark* / CAD tag / Waist (mm)* / Level From* / Level To* / Concrete Grade* / Main Steel / Remark |
| **Revisions** | Rev* / Date* / Issued By / What Changed* / Marks Affected |

The **CAD tag** column shows the tag exactly as it appears in your drawing, so a
mark that reads differently in the schedule and on the plan is visible at a glance.
Correct it if we have read it wrongly.

The **Level** columns are the one addition to what is drawn on the sheet today. A
Revit element has to sit on a level, and the drawn schedules carry no level
information at all.

**In the audited file:** all three schedules exist only as free text placed on the sheet —
1,934 separate text objects for the beam schedule, 230 for the column schedule and
161 for the slab schedule. Rows can only be rebuilt by guessing which texts line up
horizontally, which is not reliable enough to model from. Grouped entries such as
`2,4,7`, `10,14,22,26`, `107,109,111` and `55,56,58,79` are already in use and each
needs splitting.

---

## 9. Levels

Send a level table for the wing, once, and reissue it only when it changes.

| Column | Meaning |
|---|---|
| `Level Name` | Plinth, 1st Floor, 2nd Floor, Terrace |
| `Structural Level (mm)` | Top of slab, against project datum |
| `Floor to Floor (mm)` | To the level above |
| `Datum Note` | What the project zero refers to |

Where a beam or slab does not sit at the floor level — a sunk beam, a stepped beam,
a partly sunk beam such as B-34 or GB-5 — give the drop in the schedule's `Sunk`
column, and give one section through it.

**In the audited file:** there is no level information in the DWG. The `S- LEVEL` layer
exists but is empty.

---

## 10. Submission checklist

To be confirmed before each issue.

| | Check |
|---|---|
| 1 | Units are millimetres, drawing is 1:1, nothing exploded |
| 2 | `FLATTEN` run; `EXTMIN` and `EXTMAX` both report Z = 0 |
| 3 | Plan sits on the agreed base point, same XY as the floor below, rotation unchanged |
| 4 | `S-GRID` present, matching the architectural grid |
| 5 | Every column, wall, slab, sunk zone, opening and stair outline is **closed** |
| 6 | Every beam and girder is a single centreline, justification Zero |
| 7 | Girders drawn at their scheduled width |
| 8 | One closed outline per slab panel, including repeated panels |
| 9 | Sunk layers named for their real depth, matching the legend |
| 10 | One mark per text object, within 300 mm of its element, one tag per element |
| 11 | Every mark on the plan has a schedule row, and every schedule row has a mark on the plan |
| 12 | `OVERKILL`, `PURGE` and `AUDIT` run; no duplicated entities; no empty layers |
| 13 | Column, beam and slab schedules attached as Excel, one row per mark, no blanks |
| 14 | Level table attached or confirmed unchanged |
| 15 | Revision note listing what changed since the previous issue |

---

## Appendix A — Audit of the 14-07-2026 Wing C file

Read from the DWG itself, converted to DXF without altering the original. Raw output
is in `Wing-C-Audit-Evidence.txt` in this folder.

**File**

| Item | Value |
|---|---|
| Format | AutoCAD 2010 (AC1024) |
| `INSUNITS` / `MEASUREMENT` | 4 (millimetres) / 1 (metric) |
| Entities | 4,202 — 4,200 in model space, 2 in paper space |
| Layers | 51 defined, 10 of them empty |
| Block definitions | 69, no external references |
| Text styles | 7; 2,772 of 2,873 texts use `arial` |
| Plan extent | about 25.4 m x 56.3 m |
| Distance from origin | about 2.9 km |
| Plan rotation | 2.0 degrees |
| Entities with Z not zero | 375 |
| Reported Z extents | −22,493 to 30,544,084 mm |

**Columns**

| Item | Value |
|---|---|
| Outlines on `COLUMN` | 68: 61 closed rectangles, 2 closed 17-point, 5 open |
| Outlines on `FL COL` | 6, all closed |
| Exact duplicate outlines | 2 |
| Rotation | 53 rectangles at 2 degrees, 8 at 0 degrees |
| Sizes recovered | 230x950 (11), 230x2110 (8), 230x1050 (5), 230x900 (5), 230x1200 (5), 350x750 (5), 230x1950 (4), 230x1500 (4), 230x1350, 230x750, 230x230, 300x1050, 350x1480, 350x2000 |
| Unexplained | 8 rectangles of 495 x 1141, at 0 degrees, matching no schedule size |
| Tags | 48, against 74 outlines |

**Beams and girders**

| Item | Value |
|---|---|
| `BEAM` | 113 multilines (width 230, justification TOP), 49 lines, 8 open polylines, 3 hatches |
| `GIRDER BEAM` | 2 multilines (width 350, justification TOP), 12 lines, 4 polylines |
| Multiline angles | 92 degrees (59), 2 degrees (56) |
| Beam line angles | 2 (17), 92 (17), 69 (13), 0 (2) |
| Girder line angles | 92 (6), 0 (2), 85 (2), 6 (2) |
| Tags | 123 on two layers, 122 unique marks |

**Slabs**

| Item | Value |
|---|---|
| `SLAB` | 6 open polylines, no closed outline |
| `SLAB NO` | 65 tags, 19 unique marks |
| Most repeated marks | S1 (14), S3 (12), S4 (8), S8 (6) |
| `SLAB MARK` | 95 open 4-point polylines, tag boxes |
| Sunk hatches | `300mm SUNK` 12 (ANSI33), `900MM SUNK` 7 (FLEX), `SUNK` 1, `HATCH` 10 |
| Legend calls for | 100MM SUNK, 150MM SUNK, 400MM SUNK, POOL SLAB, DECK SLAB |
| Slab thicknesses in the schedule | 125, 150, 175, 200 |

**Schedules and setting out**

| Item | Value |
|---|---|
| Beam schedule | 1,934 text objects, 142 unique strings |
| Column schedule | 230 text objects |
| Slab schedule | 161 text objects |
| Dimensions | 34 |
| Grid | none — no grid lines, bubbles or grid layer |
| Level information | none; `S- LEVEL` layer is empty |

---

## Appendix B — Queries on the current drawing

These are needed before Wing C first floor can be modelled. Please answer against
the mark, not in general terms.

| # | Query |
|---|---|
| 1 | Should beam centrelines run column face to face, or column centre to column centre? We will follow whichever you use, but it must be the same on every floor. |
| 2 | Which hatch is 100, 150 and 400 sunk? The layers are named `300mm SUNK` and `900MM SUNK`, the legend lists 100, 150 and 400. |
| 3 | Marks 11, 15 and 19 appear on the plan. We could not find a matching row in the column schedule. Are they column stops, or is the row missing? |
| 4 | Eight rectangles of 495 x 1141 sit on the `COLUMN` layer at 0 degrees. What are they? |
| 5 | C-30 is drawn 230 x 827 but scheduled 230 x 950, with an untagged 230 x 950 outline 0.9 m away. Which is correct? |
| 6 | 26 column and floating-column outlines carry no mark. Please tag them. |
| 7 | B-103 and B-106 carry no depth. GB-5 reads `1480 800/`. B-34 reads `700/` and is noted as partly sunk. Please complete these rows and give the step. |
| 8 | B-60 appears on the plan only inside the compound text `/B60`. B-94 and B-97 are scheduled but we found no tag. GB-3A is tagged twice. |
| 9 | Floor-to-floor heights and structural levels from plinth to terrace. |
| 10 | Which point do we share with the architectural model as the common origin? |
| 11 | Girder widths run from 350 to 1500 in the schedule but only two girders are drawn with a width. Confirm we take girder widths from the schedule until 5.3 is applied. |
| 12 | Grade is M30 for beams and slabs and M40 for girders. Confirm this holds for every floor, or tell us where it changes. |

---

## Appendix C — Contact

Queries on this document to MOFA Studios Pvt. Ltd. We are happy to run the checklist
in section 10 against a trial file before you adopt it, at no cost to your programme,
so that any disagreement surfaces on one floor rather than on the whole project.
