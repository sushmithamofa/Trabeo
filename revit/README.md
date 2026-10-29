# Building the model in Revit

The extractor gives you the geometry. This step places it as real Revit elements.

**It has to run inside Revit.** No script outside Revit can create Revit elements —
there is no way around that, so the last step is a few clicks rather than a
double-click. Everything before it is automated.

The same script runs from **Rhino.Inside.Revit**, **Dynamo** or **pyRevit**. It
detects which one it is in and reports it. If you have Rhino.Inside.Revit, use
that: it is the least fiddly of the three.

---

## The order things happen in

```
the consultant's DWG
        |
        |   run.bat          (cad2revit)
        v
extracted\model.json         geometry, marks, positions
        +
extracted\sizes.csv          depths from the schedule   <- make_sizes_csv.py
        |
        |   build_model.py   (inside Revit: Rhino.Inside / Dynamo / pyRevit)
        v
the Revit model
```

---

## Step 1 — sizes from the schedule (only if the workbook is filled in)

A beam's **width** is in the CAD. Its **depth is not, anywhere in the geometry.**

Since 2026-09-25 the extractor reads the **beam schedule printed on the drawing
itself**, so `model.json` usually already carries a real depth per mark. On the
3rd floor plan that covered 110 of 143 beams. Check `report.html`: it says how
many marks it read and which ones it could not.

This step is for the consultant's **filled workbook**, which wins over the
drawn schedule because it is the later source. If you do not have one, skip
straight to Step 2 — the beams will still be the right depth.

The workbook itself is made from the analysis, so the consultant starts from what
the drawing already says (one folder per floor, any number of floors):

```
cd standard
python make_schedules.py --from "..\..\New folder\1" "..\..\New folder\2"
```

That writes `J1153_<Wing>_<Levels>_STR_<drawing date>.xlsx` with every mark, size,
beam top offset, slab thickness and sunk depth, sunk zone, opening and slab fold
the analysis found, shaded grey. Yellow cells are what the drawing did not give.

Once the consultant returns the filled workbook:

```
cd revit
python make_sizes_csv.py "..\standard\J1153_B_01-04_STR_2026-09-08.xlsx" --out "<floor folder>\sizes.csv"
```

That writes `sizes.csv` beside that floor's `model.json` (copy the same file to every
floor folder: each row carries its level, and the build only uses the rows for the
level it is building). It carries sizes, the beams' **Top of Beam Offset** and the
slabs' **thickness and sunk depth**. A slab's sunk depth only corrects panels the
drawing already shows as sunk; it never sinks or raises a whole mark.

Marks are matched loosely on purpose: the CAD tags a column `1` and the schedule
calls it `C-1`, the CAD says `B107` and the schedule says `B-107`. Both forms match.

## Step 1b — look at the plan in `report.html`

It opens with a plan of everything extracted. Any beam with an end touching
nothing is red with its free ends ringed. Thirty seconds there saves building a
model and finding it in 3D.

## Step 2 — open a Revit model that is ready to receive it

`build_model.py` places elements; it does not invent the things Revit needs first.
Before running, the model must have:

| | Why |
|---|---|
| **At least one level** | Every element sits on a level. The script uses the lowest one unless you name another. |
| **A rectangular concrete column family** | The script duplicates it to make a type per size. |
| **A rectangular concrete framing family** | Same, for beams and girders. |

On this machine both are already installed, under the Revit 2025 UK library:

- `Structural Columns\Concrete\Concrete Rectangular.rfa`
- `Structural Framing\Concrete\Concrete - Rectangular Beam.rfa`

The script will **not** fall back to a steel section. A universal beam happily
accepts a width and a depth into parameters that share those names, and you get a
model full of I-sections matching nothing in the drawing. If no concrete family is
loaded it stops and lists what it found instead.

Insert > Load Family, from your office template or the Revit library. If either is
missing the script stops and says so rather than half-building.

Start from an **empty** model the first time, so an undo puts you back cleanly.
After that, re-running is safe — see below.

## Step 3 — run it

The script finds its own host. It works from Rhino.Inside.Revit, Dynamo or
pyRevit, and prints which one it used. Pick whichever you have.

### A. Rhino.Inside.Revit — the best option if you have it

Full Revit API, real CPython 3, and no engine settings to get wrong.

**Simplest, no Grasshopper:**

1. Revit → **Rhino.Inside** tab → **Start** (wait for it to load).
2. Click **Rhino**, then in Rhino open the **script editor**: type `ScriptEditor` at
   the command line, or Tools → **Python 3 Script** → Edit.
3. Open `build_model.py`, copy all of it, paste, press **Run**.

**It asks you which extraction to build.** A file dialog opens; pick the
`model.json` from the run you want.

That matters because the extractor writes to whatever **Output folder** you gave
it, and that changes run to run. A fixed path in the script is how you quietly
rebuild last week's drawing.

To stop it asking, set `ASK_FOR_FILE = False` near the top of `build_model.py` and
point `DEFAULT_MODEL_JSON` at the folder you always use.

**With Grasshopper, if you want to point it at different files:**

1. Rhino.Inside → **Grasshopper**.
2. Drop in a **Python 3 Script** component.
3. Right-click the component → add two inputs, named exactly `model_json` and
   `level_name`, both set to type **string**.
4. Paste the script in.
5. Wire a **Panel** with the full path to `model.json` into `model_json`, and
   optionally a level name into `level_name`.

Both inputs are optional — unwired, it falls back to the default path and the
lowest level.

### B. Dynamo — if you would rather not start Rhino

1. Revit → **Manage → Dynamo → New**.
2. Drag in a **Python Script** node. Right-click → engine → **CPython3**.
3. Paste `build_model.py` in, click **Save Changes**.
4. Optional: a **String** node with the path to `model.json` wired to `IN[0]`, and
   a level name wired to `IN[1]`.
5. Set the graph to **Manual** and press **Run**.

Save the graph and Dynamo Player can run it later without opening Dynamo.

### Either way

The result is written to **`build_log.txt`, in the same folder as the `model.json`
you chose**, as well as printed. Each host puts printed output somewhere different,
so the file is the reliable one.

Its opening lines prove what was built from:

```
Host: Rhino.Inside.Revit
Document: Project1
Folder used:    C:\...\New folder\1
Extracted from: Z:\163. REIS MAGOS GOA HOUSING\...\C Block.dwg
Extracted on:   25 Sep 2026, 16:40
That extraction held: 58 columns, 144 beams, ...
Columns from family 'Concrete Rectangular'.
Column types created (15): 230x750, 230x900, 230x950, ...
   sized through the family parameters 'b' and 'h'
```

So the folder, the source drawing, the date and the counts are all in one place.
If the DWG name is not the one you expect, you picked the wrong folder.

If the drawing has been edited **since** that extraction, it warns in capitals and
tells you to run the extractor again rather than building stale geometry.

If anything throws, the whole transaction is rolled back, so you never get a
half-built model.

---

## What it builds

| From | Becomes | Notes |
|---|---|---|
| `columns` | Structural columns | Placed at the centre point, rotated to match, one type per size |
| `beams`, girders | Structural framing | Placed on the corrected **centrelines** |
| `walls` | Structural walls | Only the straight ones — see below |
| `slabs` | Structural floors | Top at the level, like the beam tops. One type per thickness, named `RCC Slab 150 mm` etc.; a mark not in the slab schedule gets `DEFAULT_SLAB_MM` (150) - except from a reviewed export (`"strict": true` in model.json), where such a slab, a beam with no depth, or a column with no level below is reported NOT BUILT instead of guessed. Sunk panels drop by their depth when it is known |

Every element gets its **Mark** set, so the model can be scheduled against the
consultant's drawing straight away.

Types are named `C 230x950`, `B 230x600` and so on, made once and reused.

### Re-extracting does not fix a model you have already built

This is the one that wastes an afternoon.

Fixing something in the extractor changes **what the next build places**. It can
do nothing whatever about elements already sitting in your Revit model. Build
three times and the model holds three builds worth of elements, the first two of
them from older, worse extractions - and the thing you just fixed is still there,
on screen, looking exactly as broken as before.

So every run records what it placed, in **`placed.json` beside `model.json`**,
and there is a switch at the **top of `build_model.py`** that takes it back out:

```python
REPLACE_PREVIOUS = True
```

Until you set that, a rebuild changes nothing you can see. The log says so
plainly - `ALREADY 34 columns, 195 beams (left alone, not duplicated)` means
exactly that: the old, wrong elements are still there.

or wire `mode` = `"replace"` as the third input. On the next run it deletes the
elements the previous run placed, then builds. One undo puts everything back.

It will only ever delete:

- ids **this script recorded**, never anything you drew;
- in the **same document** they were recorded from - if `placed.json` came from
  another model it refuses and says so;
- elements that **still exist**.

If the model already holds elements and a `placed.json` is present, the log says
so in capitals, because that is almost always the explanation for a fix that
appears not to have worked.

The record **accumulates**. An update run places only what was missing, and
writing just those ids would throw away the record of everything already there,
after which nothing could remove them. (That is not theoretical: a run that added
9 beams once cut a record of 238 elements down to 9.)

### Starting over completely

When the record no longer matches the model - or you just want a clean floor:

```python
CLEAR_ALL_STRUCTURE = True
```

That deletes **every** structural column, framing element, wall and floor in the
document, whether this script made it or not, then builds. One undo puts it all
back. Set it to False again afterwards.

### Running it more than once is safe

Before it builds anything it takes a snapshot of what is already in the model. Any
element that matches something in that snapshot — same Mark, or within 50 mm of the
same position — is left alone. So:

- **First run** on an empty model places everything.
- **Second run** places nothing and reports `ALREADY 202 elements (left alone, not duplicated)`.
- **After the consultant reissues the drawing**, re-extract and run again: only the
  genuinely new elements are added. Changed ones are *not* moved — delete those and
  re-run, or start the floor again.

This was checked by replaying three consecutive runs against the real
`model.json`: 202 placed, then 0, then 0.

If you do want a second copy, add a third input `mode` set to `add`.

---

## What it will not do, and why

- **Beam depths the schedule does not cover** — a depth is in no plan geometry.
  Where the drawing's own schedule has no row for a mark, and no `sizes.csv`
  covers it either, the beam is built 450 deep and the log names it.
- **L-shaped and lift-shaft walls** — those are one outline with no single
  centreline. Revit needs one wall per straight run. The script reports how many it
  skipped; model those by hand, or ask for them as separate straight walls.
- **Cantilever slabs** — no panel edge in the drawing, so none is built; the
  extractor lists them in `report.html`.
- **Levels and grids** — it does not create them. There is no grid in the drawing
  and no level table has been issued yet.

It never deletes anything and never guesses a dimension it does not have.

---

## If something goes wrong

| What you see | What it means |
|---|---|
| A problem you already fixed in the extractor is still in the model | The build did not remove the old elements, because nothing removes them automatically. Set `REPLACE_PREVIOUS = True` and run again. |
| `STOP: this model has no levels` | Make a level first. |
| `STOP: no structural column family is loaded` | Insert > Load Family. |
| `Could not set the size on '<family>'` | That family names its dimensions something the script does not know. Add the pair to `SIZE_PARAMS` near the top of `build_model.py` — it already tries `b`/`h`, `Width`/`Depth`, `Width`/`Height`, `b`/`d`, `Section Width`/`Section Depth`. |
| Every beam the same size | The schedule was not read. Check `report.html` for how many marks the drawing's schedule gave a depth. If none, name the schedule's text layer under `"schedules"` in `cad2revit\config.json`. |
| A beam narrower than the rest of its run | It bends, and the drawn faces are offset along an axis rather than square to the line. Re-extract; each run is now given one width. |
| Beams not joined, ending in mid-air | Re-extract. Beam ends are now walked to their support along the beam own line, which took the 1st floor from 73% to 99% of ends touching something. What is left is listed in `report.html` as gaps in the drawing. |
| A tight stack of columns standing beside the building | The drawing legend. Re-extract — the tool now leaves it out. |
| Two columns inside one another | The outline is drawn twice. Re-extract; the repeat is now dropped. |
| Columns all facing the same way, or turned 90 degrees from the drawing | The family starts with its long side up the Y axis, so the instance is turned by the drawing angle LESS 90. The script does this and then measures the first column placed to confirm it; the log says which offset it used. |
| Columns bunched in a line at one edge, beams correct and far away | The extractor missed them. They are drawn inside a block. Re-extract — the tool now opens plan-scale blocks — and check the column count in `report.html` before building. |
| Everything lands in the wrong place | The base point. `model.json` carries the one the extractor used; set a fixed `base_point` in `cad2revit\config.json` so every floor lines up. |
| Elements are 1000x too big or small | The drawing was not in millimetres. The extractor warns about this. |

---

## Which way a column faces

`model.json` gives a column's rotation as the direction of its **long side**,
measured from the X axis. A rectangular column family does not start at zero: its
size parameters are `b` across and `h` along, so an unrotated instance already has
its long side pointing up the Y axis, at 90 degrees.

So the angle to turn the instance through is the drawing angle **minus 90**.
Feeding the drawing angle straight in turns every column a further quarter turn.
On Wing B, 26 of the 34 columns are at 92 degrees in the plan and every one of
them was built lying on its side.

The offset is a property of the family, not of the drawing, so the script does
not simply trust it: after placing the first column whose width and depth
actually differ it **measures the result** and compares the bounding box against
what that angle should produce. A family whose `b` and `h` run the other way is
detected, corrected, and reported. The log always says which offset was used:

```
Columns were turned to the angle in the drawing less 90 degrees, which is where
the family starts. A column at 92 degrees in the plan is therefore a 2 degree
instance in Revit.
```

---

## Testing the delete

`build_model.py` only runs inside Revit, but the code that decides **what to
delete** is plain logic, and getting that wrong means losing someone's model. So
it is lifted out of the file and run against a stubbed Revit API:

```
cd revit
python selftest.py        19 tests, instant
```

It checks that a replace deletes exactly the ids it recorded and nothing else,
refuses a document the record did not come from, skips ids already gone, and does
nothing at all when the record is missing or corrupt. It also checks the column
rotation maths, including the two real Wing B cases.

The constants come out of `build_model.py` with the functions, never from the
test: a test that supplies its own `ROT_OFFSET` passes happily while the shipped
one is wrong, which is the bug those tests exist to catch.

Worth confirming it can fail: drop the document check in `delete_previous` and
`test_it_refuses_a_different_document` fails; set `ROT_OFFSET` back to `[0.0]`
and four rotation tests fail; make `write_placed` write only this run's ids and
two accumulation tests fail.

---

## Honest limits

This script has been written against the Revit API and checked for syntax and for
its handling of `model.json`, but **it has not been run inside a live Revit session
on this machine** — there was no model open to test against. Run it on an empty
model first and check a few elements against the drawing before trusting it on real
work.

If it throws, the transaction is rolled back and nothing is left behind, so a
failed run costs you nothing but the error message.

The parts most likely to need adjusting are the family parameter names in
`SIZE_PARAMS` and the wall height, which is currently a flat 3000 mm because no
level table has been issued.
