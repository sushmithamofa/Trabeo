# CAD to Revit — structural extractor

Drop in a structural CAD plan. The tool cleans the layers, repairs what it safely
can, extracts the geometry and marks needed to build the Revit model, and reports
everything it could not resolve.

The source drawing is never modified. It is copied, converted and read.

---

## Running it

**Double-click `run.bat`.** Pick the drawing, press Run.

For a whole set of floors:

```
python cli.py "Z:\...\*.dwg" --out "C:\extracted"
```

The command line exits with code 1 if any drawing produced a blocker, so it can
gate an automated step.

### Running it on a drawing you have not seen before

The tool only reads layers it recognises. A drawing from a different consultant,
or a different job, will use different layer names, so there is one step first.

**1. Inspect the layers.** Press **Inspect layers** in the app, or:

```
python cli.py "new-drawing.dwg" --inspect
```

Nothing is extracted. You get every layer in use, how many entities it holds, how
the tool currently reads it, and a guess where it does not:

```
LAYER                            ENTS  READ AS        SUGGESTION     CONTENTS
STR_BEAMS_RCC                     151  unmapped       beam           LINE:151
TEXT-BM-NUM                       112  unmapped       beam_tag       TEXT:112
STR-COLUMNS                        59  unmapped       column         LWPOLYLINE:59
```

**2. Check the guesses and paste them in.** The inspection ends with a ready-made
block. Open `config.json` (the **Edit** button), merge it into `"layer_roles"`, and
correct anything the guess got wrong. The guess is only a guess — it reads layer
names, not geometry.

**3. Run.** If the mapping is right, everything extracts as normal.

Keep one config per consultant rather than editing the same file back and forth:

```
python cli.py "their-plan.dwg" --config "consultant-b.json"
```

If nothing is recognised at all, the run stops with a blocker saying how many
entities were skipped and on how many layers, rather than quietly producing an
empty model.

### The roles a layer can be given

`column`, `column_stop`, `column_float`, `wall`, `beam`, `girder`, `slab`, `sunk`,
`opening`, `stair`, `grid`, `column_tag`, `beam_tag`, `slab_tag`, `ignore`.

Put anything that is not structure — title block, schedules, dimensions, hatching,
details — under `ignore`, so it stops being reported as unmapped.

### What it needs

| | |
|---|---|
| Python | 3.10 or later |
| ezdxf | installed automatically by `run.bat` |
| shapely | installed automatically by `run.bat`; finds the slab panels between the beams |
| A DWG converter | only for `.dwg` input — ODA File Converter, ZWCAD, or BricsCAD. Feed it a `.dxf` and none is needed. |
| Units | millimetres. Any other unit is reported as a warning, and every size will be wrong until the drawing is fixed. |

On this machine ZWCAD is used, which opens briefly while it converts.

---

## What comes out

One folder per run:

| File | What it is |
|---|---|
| `cleaned.dxf` | The plan rebuilt on standard `S-` layers, flat at Z = 0, every outline closed, beams as **true centrelines**, origin moved to the project base point |
| `columns.csv` | Mark, centre point, width, depth, rotation |
| `beams.csv` | Mark, both end points, width, **depth and where the depth came from**, and how the centreline was obtained |
| `walls.csv` | Mark, thickness, centreline, outline vertex count |
| `slab_tags.csv` | Slab marks, positions and thickness from the slab schedule |
| `sunk_zones.csv` | Sunk depth, centre, vertex count |
| `issues.csv` | Every finding, machine-readable |
| `model.json` | All of the above including full polygon outlines, for a Revit or Dynamo importer |
| `<drawing name> QUERIES.dxf` | **The consultant's own drawing with the audit marked on it** — revision clouds, numbered bubbles, the comments, and a query schedule clear of the sheet, all on `S-QUERY-*` layers. Opens as a normal drawing and can be sent straight back. Their original file is untouched. |
| `report.html` | The readable audit — **open this first**. It opens with a plan of everything extracted, with every beam whose end touches nothing picked out in red |

Every CSV carries both `_local` coordinates (measured from the project base point)
and, for columns, the original drawing coordinates.

---

## What it fixes on the way through

| Problem | What the tool does |
|---|---|
| Multilines justified Top or Bottom | Recovers the true centreline from both drawn faces. No guessing which side — it works for any justification. |
| Entities off Z = 0 | Flattens to zero and reports how many there were |
| Outlines left open by a small gap | Closes them if the gap is under `close_gap_tolerance_mm` (50 mm), and reports it |
| Outlines genuinely open | Refuses to invent a shape; raises a blocker with the gap size and location |
| The same line drawn twice | Drops the duplicate so it does not become a double beam |
| A face line whose partner merged without it | Dropped. With no width and no mark of its own, sitting exactly half a beam width off a beam already in the model, it is a leftover face, not a beam |
| A beam drawn as two face lines | Merges the pair into one beam and measures its width from the gap between the faces. A girder wider than the usual limit is merged too, but only where the mark written between the faces has that width in the schedule |
| Corners pulled apart by the centreline shift | Extends or trims each beam to where it crosses its neighbour, and walks any end still floating out **along its own line** to the column, wall or beam that carries it |
| Lift walls sitting on the column layer | Re-classifies anything whose mark reads `LW…` as a wall |
| Notes and legends on tag layers | Skipped — a mark has to look like a mark |
| Rotated rectangles | De-rotates them, so a column reads as its true `230 x 950`, not its bounding box |
| A column drawn as four separate lines | Stitched back into one outline. Identical on the sheet, invisible to anything that only reads polylines |
| Shading drawn as lines on a structural layer | Recognised as a fill and left out. Every line of it otherwise becomes a beam |
| Debris far from the building | Left out. A fragment dragged off the sheet is not structure |
| The drawing legend | Left out. A legend draws a sample column and a sample beam on the very layers it describes, so they would otherwise become real elements standing beside the building |
| The same outline drawn twice | The repeat is dropped, so one column does not become two standing inside one another. If the two copies carry different marks, both are kept and it is raised as a blocker |
| Beam depth, which is in no plan geometry | Read from the beam schedule printed on the same sheet |
| Structure drawn inside a block | Opens the block out, so a plan inserted as one block is read like any other. Details and sections inserted at another scale are left shut — see below |

It does **not** invent geometry. If the drawing has no closed slab outlines, each
panel is taken as the bay its beams, columns and walls enclose, with the slab mark
inside it and that mark's thickness from the slab schedule. A mark the schedule's
remarks call sunk ("400mm SUNK", printed under its row) is built that far below
the level; "partly sunk" is reported and left at the level.
Cut-outs drawn as an X on the void / cut-out layers (no outline needed; a
diagonal broken for its label is joined) are cut out of the slab they fall in.

Checked on every Reis Magos floor plan (Wings A, B, C, 1st-3rd floors, and the
stilt plans), which is where these rules come from:

- A closed outline on the slab layer is used as drawn for the one mark inside
  it; every other mark still gets its bay. An outline round several different
  marks is not a panel and is ignored (reported).
- The slab schedule is also read off the beam schedule's layer, slab marks only,
  as some sheets print both tables there. Rows like `S | - | 4A`, `SC-1`, `ST-2,4`
  and a bare `SC` are all read.
- A sunk remark belongs to the first real row above it in its own table; a
  remark under a row with no mark is not guessed.
- A mark whose insertion point sits on the beam line (within 100 mm,
  `slab_tag_snap_mm`) is taken for the empty bay beside it. Anything further
  out is reported, not moved.

Sunk depths come from the drawing's own legend, by hatch pattern, not from the
layer name (on Wing B 1st floor the layer '300mm SUNK' holds the 100 mm hatch).
A plan hatch is matched to a legend sample by pattern, then layer, then scale
(allowing for a legend drawn at a different hatch scale, as on Wing A 1st
floor); if it is still ambiguous its depth is left unknown and reported. A
hatch the legend does not show is not given its layer name's number. Where a
sunk hatch covers only part of a slab bay, the slab is split along it: the
hatched part is its own panel at the sunk depth, the rest stays at the level.
A mark the slab schedule calls sunk keeps the schedule's depth, and a hatch
that disagrees is reported.

Slab folds / hidden beams: the red lines on the slab layer, drawn as two
lines about 200 mm apart. Each pair is one fold on the centreline between them.
A fold divides the slab bay it crosses (so S8A and S8 either side of one are
separate slabs; a piece with no mark keeps its bay's mark), and is built in
Revit as framing ("FOLD 200x225"), from the top of the higher slab to the
underside of the lower one: depth = step + lower slab thickness. With no step
either side it is a hidden beam as deep as the slab. A fold without a slab on
both sides is reported, not built. A single red line with no partner divides
slabs only; on these plans those are outer slab edges beyond the edge beam.

Tables on the sheet, and what each is used for:

| Table | Used for |
|---|---|
| Beam schedule | width and depth per mark; remarks set the beam's level: "250MM SUNK" drops its top 250, "BT@ +0.200M" raises it 200. PARTLY SUNK, INVERTED, two levels for one beam and absolute levels ("BT@ 12.5M") are reported, not placed |
| Slab schedule | thickness per mark; "N MM SUNK" in its remarks sinks the slab |
| Column schedule (`Col. sch.`) | cross-check only: each marked column's drawn size against the table for this drawing's wing; differences listed, columns built as drawn. A sheet with a table per wing is matched by the wing in the drawing's name; a drawing covering several wings is not checked |
| Legend | sunk depth of each hatch pattern |
| Beam marks | written along their beam (BEAM NO (H) / (V)); a mark goes to a beam running the same way |

Not handled: retaining walls on the stilt and basement plans (`RET WALL`, open
lines) are not extracted, so bays against them stay open; marks written in the
margin outside the building (Wing A stilt draft, Dec 2025); X's drawn as four
arms that do not meet. Panels run to the beam
faces (beam depth is overall, so nothing is counted twice). A bay with no mark is
left open (lift, stair, shafts). A mark in no closed bay, such as a cantilever, is
reported, not guessed. Two marks in one bay are built once at the thicker and
reported, as a beam between them is probably missing.

---

## Where a beam depth comes from

A beam's **width** is in the plan. Its **depth is not, anywhere in the geometry** -
a centreline is a line. So a model built from the plan alone has every beam the
same size, which is what it looks like in a 3D view.

The depth is in the schedule. On these drawings the schedule is **printed on the
same sheet**, as 1,762 text objects on `beam sch. text`:

```
-- | 150 | 150 | - | -- | 8 | -- | 2 | 16 | -- | 2 | 16 | -- | -- | 230 | 450 | B | 1,1A
                                                                  ^^^   ^^^   ^   ^^^^
                                                                width depth pre marks
```

So the tool reads it. Text is grouped back into rows by its Y position, each row
is ordered by X, and the mark, the prefix and the two size cells are picked out.
Everything else on the row is reinforcement, which Revit does not need.

On the 3rd floor plan that gave **121 marks their real width and depth**, and
**110 of the 143 beams** a depth instead of a default: 450, 550, 600, 750, 900 and
300 deep rather than one flat number. The slab schedule reads the same way and
gave 56 of the 58 slab tags a thickness.

Point it at your own layers:

```json
"schedules": {
  "read": true,
  "beam_layers": ["beam sch. text"],
  "slab_layers": ["slab sch. text"]
}
```

A row that is not in that shape - a continuation line carrying a second layer of
bars, a header, a note - is **skipped and counted, never guessed at**. A mark the
schedule does not cover keeps no depth at all, and the report names it. On the
3rd floor two did: B5 and B6.

`sizes.csv` from the consultant's filled workbook still wins where it exists,
because it is the later and more authoritative source.

### One warning worth reading

If a beam measures one width on the plan and the schedule says another, the plan
width is kept and you get a warning naming both. That is a real disagreement in
the drawing, not a tool problem.

---

## A column drawn as four lines is still a column

Some drafters draw a column as a closed polyline. Others draw the same column as
**four separate lines**. On the sheet the two are indistinguishable. To an
extractor that only understands polylines, the second one does not exist.

That is not hypothetical. On `1ST FLOOR PLAN ( B WING )` the COLUMN layer carries
**127 lines and 15 polylines**, and only the 15 came through - 15 columns in a
model that should have 34. Every one of those 127 endpoints turned out to be
shared with another line, so the shapes were there all along, just never stitched.

Now they are. Endpoints are snapped to a tolerance and used as graph nodes, and
the closed faces of that graph are walked out. Face traversal, not naive cycle
following, because columns often share a corner: at each node the walk takes the
next edge clockwise from the one it arrived on, which traces the smallest face on
the left and so separates two columns meeting at a point instead of merging them
into one ring.

On that drawing it recovered **19 columns and the lift shaft**, and 33 of the 34
columns now carry their mark.

Nothing is invented. A line that closes no face is left exactly where it is, and
an assembled outline has to pass the same checks a drawn polyline does.

```json
"assemble_loops": {
  "roles": ["column", "column_stop", "column_float", "wall", "slab", "opening", "stair"],
  "snap_mm": 25,
  "max_lines": 4000
}
```

`max_lines` is a guard: a sunk layer carrying its own hatching can hold two
thousand lines, and stitching those would produce thousands of meaningless
slivers. Over the limit the tool says so and leaves them alone.

---

## The face that got left behind

A beam drawn as two faces normally merges into one. Sometimes only one of the two
finds its partner - the other face is a different length, or was drawn in two
segments, or a third line runs alongside - and the loser survives as a beam in its
own right: **no width, no mark, lying exactly half a beam width off the centreline
of a beam that is already in the model.**

In Revit that is a thin extra beam pressed against a real one, ending in mid air.
Nine of the nineteen open beam ends on Wing B were these, every one of them
115 mm - half of 230 - from its neighbour.

The test is deliberately narrow: no width of its own, parallel, overlapping, and
offset by half the other beam's width to within a few millimetres. A beam that
measured its own width is never dropped, whatever it sits beside.

**And never one that carries a mark.** This runs before the marks are matched, so
a line's own mark is still empty and cannot be used - the first version of this
check looked at it and was therefore looking at nothing, and a test caught it
deleting a tagged beam. It now compares the tags directly: a tag that sits closer
to this line than to the beam it would be merged into means the consultant is
calling it an element, and it stays. That guard saved a real tagged girder on
Wing C.

```
Wing B open beam ends   19 -> 8     (96% of ends landing)
Wing C open beam ends   18 -> 17
marked beams            unchanged, both drawings
```

The marked count not moving is the check that matters: raw counts fall when the
tool learns to leave something out, but a mark is the only thing tying an element
to its schedule row, so no marked element may ever be dropped. That is its own
test now, for beams as well as columns.

---

## A girder too wide to look like one beam

Two parallel lines close together are the two faces of one beam. Two parallel
lines far apart are two beams. The dividing line is `face_pair_max_width_mm`, and
it is 700 mm, because welding two real beams into one wide one is worse than
leaving one beam as two faces.

A job with **1500 mm girders** breaks that honestly. On Wing B each girder stayed
as its two faces: two beams 750 mm either side of where the girder actually is.
Nothing ran down the middle - and the **floating columns, which sit on those
girders, touched nothing at all.** That is what "no connection between columns and
beams" turned out to be.

Widening the limit for everyone is not the fix. The Wing C schedule lists 750 and
900 mm beams, and beams on that plan really do run 750 mm apart, so a raised
ceiling welds them together.

So the evidence has to be **local**: a mark written *between* the two lines, whose
own row in the schedule gives a width matching the gap. The drawing is then
saying, in as many words, that this band is one beam of this width.

```
FC1, FC3, FC4, FC5, FC6   750 mm off their girder  ->  0 mm, sitting on it
```

It found two on Wing C as well, which had gone unnoticed: **BP4 at 1500 mm and
GB4 at 900 mm**, each matching its schedule row to a tenth of a millimetre. Both
were two beams at their faces before.

---

## Hatching drawn as ordinary lines

A drafter shades a region - a drop, a sunk panel, a girder zone - by filling it
with closely spaced parallel lines. Often those go on the structural layer being
shaded rather than a hatch layer, and then each one looks exactly like a beam:
straight, right layer, right sort of length.

On the Wing B sheet, **162 of the 186 lines on `GIRDER BEAM` are at 45 degrees,
258 to 367 mm apart**. They are the fill of the girder zones. Seventy-nine became
girders, with widths measured from the gap between neighbouring hatch lines, and
in Revit they arrive as a thicket of diagonal beams crossing the real structure.

The signature has nothing to do with the angle being 45: **many parallel lines,
evenly spaced, far too close together to be structure.** Nothing real is eight
parallel beams three hundred millimetres apart. That is what is tested, so the
check works whatever angle the drafter hatched at, and a genuine pair of beams
4 m apart is never touched.

```
99 girders  ->  20 girders, all of them real
79 diagonal beams  ->  0
```

Stair layers are deliberately left out of the check: treads look identical.

---

## Debris a long way from the building

A floor plan is one connected body: beams frame into columns, columns carry
beams, and nothing real is a hundred metres from everything else.

But CAD files collect debris - a fragment dragged off the sheet, a paste that
landed in the wrong place, the remains of an edit nobody finished. The Wing B
sheet carries **seven beam lines 2.4 kilometres from the plan**, with no columns,
no tags and no text anywhere near them.

In Revit that debris is what you notice first: a handful of beams alone in space,
forcing every view to zoom out until the building is a smudge.

Elements are grouped by single linkage, and a group is dropped only when it is
**both** far from the main body and a small share of everything. A genuinely
detached wing is neither, so it stays - and is reported, so you can check.

```json
"islands": { "drop": true, "link_mm": 10000, "min_distance_mm": 50000, "max_share": 0.15 }
```

---

## Look at the plan before you build

`report.html` now opens with **a plan of what was extracted**, drawn as plain SVG so
it works from disk with no libraries. Beams are blue; ones under 1.2 m are orange;
any beam with an end touching nothing is **red with its free ends ringed**. Hover
any beam for its mark, length, width and depth.

This exists because a geometry mistake is obvious at a glance and nearly
invisible in a table of numbers. A beam left hanging in the middle of a room, or
one stopping short of its support, shows up here in a second - before the model is
built, instead of after.

---

## One beam, one width

A beam that splays round an obstruction is drawn as two face polylines that bend
together: diagonal, straight, diagonal. Each bend becomes its own segment, and
each segment measures its own width from the gap between the faces.

That works on the straight run and fails on the diagonals, because the second
face is offset **along an axis rather than square to the line**. A 200 mm beam
turning through 45 degrees then measures 200/&radic;2 = 141 mm, and the model gets
a beam that mysteriously narrows at both ends. The 3rd floor had beams reading
139.6, 140.0, 141.4, 163.8 and 167.8 mm - none of them a width anyone ever built.

The segments came from one polyline, so they are one beam and take one width: the
widest reading in the run, which is the one taken where the faces are genuinely
parallel. Every correction is reported with the before and after.

---

## Closing the frame up again

Moving a beam from its drawn face to its true centreline shifts it sideways by
half its width, which pulls every corner apart by that much. Revit cannot join a
beam to something it does not touch, so the frame has to be closed again. Two
passes do it, and the order matters.

**1. Where two centrelines cross**, each end is walked to the crossing.

The crossing has to be **on the neighbour's actual length**, not on the infinite
line through it. That sounds pedantic and is not: on an orthogonal grid, two
beams thirty metres apart still have a mathematical intersection, and snapping an
end to one of those sends a beam off pointing at nothing. Before this was fixed,
**152 of 250 joins on the 3rd floor landed more than 50 mm from any real beam**,
the worst 1,834 mm out. A 300 mm overshoot is still allowed, so a beam can frame
into the very end of another.

**2. Whatever is left floating** is walked outward **along its own line** until it
enters a column or wall or reaches another beam.

This is the case the first pass cannot help with: a beam that lands on a column
has no crossing to find, and the centreline shift has left it short of its own
support.

Never sideways. An end is only ever moved along the line the beam was drawn on,
because moving it across that line would be inventing a position. If nothing is
found within `beam_support_snap_mm`, the end is **left exactly as drawn** and
reported: a gap that cannot be closed along the beam's own line is a gap in the
drawing.

Measured as the share of beam ends that touch a beam or sit inside a support:

| | before | after |
|---|---|---|
| 1st floor | 73% | **99%** |
| 3rd floor | 69% | **95%** |

The 15 ends still adrift on the 3rd floor are named in the report.

```json
"beam_join_snap_mm": 800,        // how far an end may be walked to a crossing
"beam_join_overshoot_mm": 300,   // how far past a neighbour a crossing may sit
"beam_support_snap_mm": 800      // how far an end may be walked to its support
```

---

## Two things that are drawn like structure but are not

### The legend

A legend draws a little sample box captioned COLUMN, another captioned BEAM, one
per sunk depth - **on the layers it is describing**. To a layer-driven extractor
they are structure. On the Wing C sheets they arrived in Revit as a tight stack
of seven columns standing on their own beside the building.

The block is found from the drawing, not from a hardcoded rectangle: a heading
such as `LEGEND`, the column of captions beneath it, and the bounding box those
texts occupy, padded to take in the samples beside them. On the 3rd floor that
left out exactly 17 entities - 7 on `COLUMN`, 2 on `BEAM`, 8 captions - and
nothing else.

It is reported, never silent. If it ever takes something real:

```json
"legend": { "detect": false }
```

`exclude_zones` takes `[[x1, y1, x2, y2], ...]` in drawing coordinates for
anything else on the sheet that should be ignored.

### The same outline drawn twice

Two identical outlines give two Revit elements standing inside one another:
invisible in a 3D view, a phantom extra row in every schedule, double the
concrete in a take-off. The 1st floor had 9 of them, the 3rd floor 9.

Two shapes count as the same only when their outlines **match point for point**.
Comparing centres and sizes is not enough - a lift shaft is often two different
outlines about one centre, and a wall carries no width or depth to tell them
apart, so LW1 and LW2 would collapse into one wall.

The check runs **after the marks have been matched**, which decides what to keep:

- a **marked** copy is kept over an unmarked one, because the mark is the only
  thing tying the element to its schedule row;
- where both copies carry a mark and **the two marks differ**, neither is dropped.
  The drawing is saying two different things about one outline, and only the
  consultant can say which is right. That is raised as a blocker. The 1st floor
  has one: the lift shaft is drawn twice, tagged `LW1` and `LW2`.

---

## Structure drawn inside a block

Consultants often draw the whole column grid once and insert it as a **single
block**. Reading model space alone then finds almost nothing: the layers are
right and the geometry is right, but it all sits one level down.

That is what happened on `3RD FLOOR PLAN ( C WING )`. Every column, and every
column mark, lived inside one block called `COLUMN2`. The extraction found
**7 columns** — seven stray polylines at the edge of the sheet — instead of 56,
and no marks at all. In Revit the columns landed in a line down one side while
the beams sat 25 m away, which is exactly what it looked like.

So blocks are now opened out before anything is extracted.

**Not all of them.** The same sheet carries sections, details and schedules, also
as blocks, usually inserted at a scale other than 1:1. Exploding those would give
you extra columns and walls at four times life size.

The rule: **a block is opened only if it is inserted at a uniform scale of 1.0**,
rotated or mirrored as much as it likes. Anything else is treated as a detail and
left shut — but never silently. If a shut block holds entities on structural
layers you get a warning naming it, its scale, and how many:

```
Block 'COLUMN2' is inserted at plan scale and was opened out; 139 structural
entities inside it were extracted. Without this they would have been missed.

Block 'zw$CD88' holds 16 entities on structural layers but is inserted at 4x
scale, so it was treated as a detail or section and left out.
```

If a block really is part of the plan at another scale, force it:

```json
"blocks": {
  "explode": "auto",
  "explode_names": ["MY-PLAN-BLOCK"],
  "skip_names": ["TITLE-*"]
}
```

`"explode": true` opens everything regardless of scale; `false` reads model space
only, as the tool did before. Both name lists take `*` wildcards.

**`--inspect` now counts inside blocks too**, in an `INBLK` column, so a drawing
built this way shows its real contents before you run it:

```
LAYER                            ENTS  INBLK  READ AS        CONTENTS
COLUMN                             56     49  column         LWPOLYLINE:56
colno                              42     42  column_tag     TEXT:42
```

A layer whose entities are *all* in blocks is the tell.

---

## Reading the report

- **Blocker** — that element cannot be modelled until the drawing changes.
- **Warning** — it was extracted, but on an assumption worth checking.
- **Note** — information only.

Every issue carries a drawing coordinate, so it can be found in CAD.

---

## Pointing it at a different consultant

All layer rules live in `config.json`. No code changes.

```json
"layer_roles": {
  "column": ["COLUMN", "COLUMNS", "S-COL"],
  "beam":   ["BEAM", "S-BEAM"]
}
```

Patterns are case-insensitive and `*` is a wildcard. Roles are tested in the order
listed, so put specific patterns before general ones. Any layer that carries
entities but matches nothing is reported, never silently dropped.

Use `--inspect` to get a starting block rather than typing layer names by hand.

Two settings usually need attention on a new file:

- **`base_point`** — `"auto"` puts the local origin at the south-west corner of the
  columns. Replace with `[x, y]` once a shared point is agreed with the architect,
  and every floor will then line up.
- **`sunk_depth_override`** — where layer names disagree with the drawing legend, for
  example `{"300mm SUNK": 150, "900MM SUNK": 400}`. Until a layer is listed here the
  depth is read from its name and a warning is raised.

---

## Results on the Wing B GFC file

`01_08_2026_1153 - 1ST FLOOR PLAN ( B WING ) 29-07-2026 (6).dwg`. This one is
drawn differently from Wing C and is what the loose-line stitching was built for:

```
34 columns, 1 wall, 105 beams, 20 girders, 72 slab tags, 6 grid lines
5 of the 7 floating columns sit exactly on the girder that carries them
96% of beam ends land on a beam or inside a support
33 of the 34 columns carry their mark
108 of the beams took their depth from the schedule printed on the drawing
```

Before the stitching it gave **15 columns**. Nineteen of them, and the lift
shaft, were drawn as loose lines. Seven beams 2.4 km off the sheet were dropped as
debris, which took the beam ends touching nothing from 14 down to 2 - and both of
those are real 1.6 m gaps in the drawing.

This is also the first drawing in the job that carries a grid, on
`GRIDLINE_MOFA`.

---

## Results on the Wing C GFC file

`1153 - 1ST FLOOR PLAN ( C WING ) 14-07-2026.dwg`, 4,202 entities, 51 layers:

```
48 columns, 7 walls, 116 beams, 16 girders, 65 slab tags, 20 sunk zones
16 blockers, 94 warnings, 16 notes
all 44 column marks and all 4 wall marks placed
99% of beam ends touch a beam or sit inside a support
```

The column count was 65 until 2026-09-25, when the tool learned to leave out the
legend and the repeats. Eight of those 65 were the legend sample boxes and nine
were exact repeats of another outline. **All 44 marks are unchanged**, which is
what says the 17 that went were not real structure - and is now its own test.

All 115 multilines were corrected from their drawn face to their centreline, 30
beams drawn as two face lines were merged into one, 6 lines lying on top of another
beam were dropped, and 129 centrelines were extended or trimmed to meet their
neighbours.

The blockers are the ones written up in
`../standard/Structural-CAD-Standard-J1153.md`: column outlines left open, no
closed slab outlines, no grid, outlines carrying no mark, and six beam marks
(B68, B90, B96, BP1, BP2, BP3) whose beam is not drawn or already carries another
mark.

---

## How it is put together

```
app.py        desktop front end (tkinter)
cli.py        command line, batch and exit codes
config.json   layer rules and tolerances
core/
  dwgconv.py  finds an installed converter and drives it
  reader.py   layer name -> role, plus the guesser used by --inspect
  inspect_layers.py  the layer listing and the config block it suggests
  geometry.py rectangles, centroids, distances, de-rotation
  extract.py  the actual extraction and mark matching
  cleanout.py writes the cleaned DXF
  blocks.py   opens out plan-scale block references
  loops.py    stitches loose lines back into closed outlines
  hatchlines.py  spots shading drawn as lines on a structural layer
  legend.py   keeps the drawing legend out of the model
  schedule.py reads the beam and slab schedules printed on the sheet
  report.py   CSV, JSON, the SVG plan and the HTML report
  markup.py   writes queries.dxf, the clouded and commented markup
  pipeline.py ties it together
```

---

## Testing

```
python selftest.py           96 tests, about 18 seconds
python selftest.py -v        show each one
```

Twelve groups:

- **Geometry and marks** — rectangle recovery from a rotated shape, point-to-line
  distance, what counts as a mark. Pure maths, instant.
- **Extraction** — builds a drawing in memory whose answers are known exactly (one
  `230 x 950` column at 2 degrees marked `5`, one Top-justified 230 multiline marked
  `B1`, one closed slab, one grid line) and checks the extractor returns precisely
  those numbers. A failure here means the tool is wrong, not that a sample changed.
- **Blocks** — a drawing whose only column exists inside a 1:1 block, beside a
  section detail on the same layers inserted at 4x. The first must come out as a
  column with its mark; the second must not, and must be reported rather than
  dropped. Also checks `explode_names` and `explode: false`.
- **Schedules, legends and repeats** — a plan with its beam schedule printed
  beside it, whose cells are deliberately scattered in Y the way the real
  drawing's are. The depths must come out 450 and 750, not 230; a legend's sample
  boxes must not become columns; an outline drawn twice must make one column, and
  two copies with different marks must make a blocker instead.
- **Beam joining** — a beam falling short of a column at one end and of a
  crossing beam at the other, both of which must be closed, beside a beam whose
  neighbour does not reach it but whose *infinite line* crosses 600 mm past its
  end. That one must be left exactly where it is.
- **Chain widths and the plan view** — a face pair bending through 45 degrees,
  whose diagonal runs must come out 200 mm wide and not 141; and that the report
  really draws an SVG plan and marks the free ends on it.
- **Loose lines and debris** — a column drawn as four separate lines, which must
  come out as one marked column; a dangling line beside it, which must invent
  nothing; and a fragment two kilometres away, which must be left out and said so.
- **Hatching** — a real beam beside twenty parallel lines 300 mm apart on the
  same layer. The fill must not become beams, the beam must survive, and beams
  genuinely 4 m apart must be left alone.
- **Wide girders** — a 1500 mm girder drawn as two faces with its mark between
  them and a floating column on its centreline. The faces must merge, the column
  must end up on the beam, and neither must happen when the mark is elsewhere or
  the gap matches no scheduled width.
- **Leftover faces** — a beam drawn as two faces with a third line along one of
  them. The orphan must go, a line at any other offset must stay, and a tagged
  one must stay whatever its offset.
- **Round trip** — the cleaned DXF is re-read and must give the same elements, be
  flat at Z = 0, have every outline closed, and keep the corrected beam position.
- **The real drawing** — runs the Wing C GFC file end to end and checks the counts
  against `REAL_EXPECTED` in `selftest.py`, that every multiline beam was offset by
  exactly half its width, that the known blockers are still reported, and that the
  source DWG is byte-identical afterwards. Skipped automatically if the DWG is not
  on the machine.

To see the synthetic drawing in CAD:

```
python selftest.py --fixture check
```

### Checking the tests can actually fail

Worth doing after any change to the geometry. Break something on purpose and
confirm the suite notices — for example, take the drawn multiline face as the
centreline instead of the midpoint of both faces, and three tests should fail with
`0.0 != 115.0`. Use the bounding box instead of the true rectangle sides, and two
should fail with `263.0 != 230.0`. Set `"explode": false` in `config.json` and
four block tests should fail; `"scale_tolerance": 99`, two; `"read": false` under
`schedules`, three; `"detect": false` under `legend`, three. Stop sorting each
schedule row by X and three fail with the width and depth swapped. Drop the
on-segment test in `_join_beam_network` and the phantom-intersection test fails;
drop the call to `_extend_beams_to_supports` and two more do; drop
`_reconcile_chain_widths` and two more. Empty `assemble_loops.roles` and five
fail; set `islands.drop` to false and two do; set `hatch_lines.detect` to false
and two do. Make `_scheduled_wide_beam` always refuse, or always accept, and the
wide-girder tests fail each way. Skip `_drop_orphan_faces` and three fail; drop
its tag guard and a tagged beam is deleted, which two more catch. If they pass, the tests are not
testing.

### Trying it by hand

1. Run it on a drawing you already know, and open `report.html`.
2. Open `cleaned.dxf` next to the original in CAD, with the original underneath.
   Beam lines should sit on the **centre** of each beam, not on a face.
3. Spot-check three rows of `columns.csv` against the column schedule.
4. Check a mark you know is problematic appears in `issues.csv` with a coordinate
   you can find in CAD.

### After changing the layer rules

`config.json` is not covered by the tests, because it changes per consultant. After
editing it, run `--inspect` again and confirm no layer holding structure is still
`unmapped`.

---

## Not built yet

- **Cantilever slabs.** A slab past the edge beam has no beam on its far side,
  so no panel edge to take. These are reported and modelled by hand.
- **Reinforcement.** Bar sizes and spacings are read off the schedule rows and
  then thrown away - Revit does not need them to build the frame, but they are
  sitting right there if a rebar model is ever wanted.
- **Placing the elements in Revit.** `model.json` and the CSVs are the hand-off;
  a Dynamo graph or pyRevit script reads them and creates the elements.
