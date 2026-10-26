# Test checklist

Everything below has been built and checked outside Revit. These steps are the first real use.
Note what happens at each step, good or bad, and send the files named under each part.

## A. The app (no Revit needed), about 20 minutes

1. Double-click `run_app.bat` in this folder.
2. Drawings folder: `samples\training\Sasangir_MainBlock`. Project folder: leave the suggested one.
   Revit level names: `GF=GF,L1=Level 1,L2=Level 2,TERRACE=TERRACE` (or your Revit level names).
   Tick "Use suggested layer profiles". Press **Run**.
   * First run converts the DWGs with ZWCAD (5-10 min). A ZWCAD window opening and closing is normal.
3. When it finishes, look at the cards and the NEXT steps in the log, then press:
   * **Review page**: click a plan on the left; click a red element; does each source make sense?
     Try choosing a value for one conflict, then **Export overrides.json** into the project folder.
   * **Review workbook**: sheets Columns, Beams, Walls, Stairs & ramps, Levels, Issues.
   * **Summary**.
4. Press **Run** again: it should reuse the converted drawings (much faster) and apply your decision.
   Check that element now shows "override".

Send: the project folder's latest `runs\<date>\SUMMARY.txt`, and anything that looked wrong
(screenshot of the review page is fine).

## B. Revit: columns, beams, slabs, walls, about 15 minutes

Use a test copy of a Revit project with a rectangular concrete column family, a concrete
beam family and a rectangular isolated footing family loaded, and levels named as above.

1. Run `revit\build_model.py` (pyRevit, Dynamo or Rhino.Inside, as you do today) and pick
   `runs\<date>\revit_build\PLINTH_BEAM_LAYOUT_COL_LAY_1_plan\model.json`.
2. Then the same for `...\rev_SF_lvl_slab_sasan_28_5_25_FRAMING_1_plan\model.json` (Level 2).
3. Compare with what today's extractor route builds for the same drawing.

Send: the `build_log.txt` written beside each `model.json`.

## C. Revit: foundations, pedestals, stairs, levels, about 10 minutes

0. First, on the app's Export page under **Before you build**: enter a founding level for the
   footings (Sasan Gir's drawings say only "minimum 1500 mm", so e.g. -1500) and the floor levels'
   elevations, then **Save and update the Revit files**. Without them nothing is placed at a guessed depth.
1. Run `revit\build_extras.py` and pick
   `runs\<date>\revit_build\PLINTH_BEAM_LAYOUT_COL_LAY_1_plan\foundations.json`.
   * Each footing sits at its own founding level (read from the footing schedule, or your decision),
     measured from level GF's elevation.
   * It also builds the stairs of every plan that has a `stairs.json` (from pyRevit or
     Rhino.Inside; not from Dynamo).
2. Look at: footings under the columns? right size and depth? steps on top? stairs between the
   right levels, flights joined by a landing, going the right way (flip in Revit if not)?

Send: `build_extras_log.txt` (beside foundations.json) and a 3D screenshot.

## Known in advance

* Villa 5 foundations are not built: its raft layout has nothing to line it up with the plans.
* Ramps and PCC beds are listed, not built.
* Stair direction is taken from the drawing order; the log says so.
* An element with a size in conflict or missing is **not built** until you decide it (listed on the
  Export page under "Needs a decision" and in each floor's export_report.txt). Nothing is built at a
  default beam depth, slab thickness, storey height or founding depth.
* Building into a new Revit file stops until every floor level has an elevation (from the drawings
  or entered on the Export page). Building into your own model uses levels you already have.
