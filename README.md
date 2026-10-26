# Trabeo

Reads structural consultants' CAD drawings (DWG / DXF) floor by floor and builds the structural model - columns, beams, slabs, walls, foundations - for Revit, IFC and Rhino. Runs on your own PC; nothing is sent anywhere.

*Developed as "CAD to Revit"; file names inside still use that name.*

**Testers:** double-click `run_app.bat` (Windows, Python 3.10 or later). DWG files need ZWCAD or the free ODA File Converter installed; DXF files work without either.

The universal version of the CAD-to-Revit tool is developed here. The original
folder on the Desktop is left untouched and keeps working as it is.

## Using it

Double-click **run_app.bat**. The app opens in the browser (it runs on this PC only, at
127.0.0.1; `webapp.py`). The first time it shows a welcome screen: **New project**, **Open
project**, recent projects, or **Try a sample** (a small made-up drawing set). Then four steps
along the top:

1. **Import CAD**: add drawings or a whole folder (Windows' own pickers), any consultant, DWG/DXF.
2. **Analyse**: tick the floors and pick each one's Revit name from a list, then analyse. Progress
   is shown stage by stage, and the technical log is under "Show details".
3. **Review**: the review page with its Plan/3D switch. Every decision is saved into the project as
   it is made.
4. **Export to Revit**: writes the Revit files. Then choose one of four ways:
   * **Build in Revit now**: builds straight into the project open in Revit, through the Revit
     MCP plug-in. Switch on its service in Revit first.
   * **Create a Revit file (.rvt)**: Revit, through the same connection, makes a new project
     from its default template, builds into it, and the app offers the `.rvt` to download.
   * Build it yourself, by hand or with the Rhino.Inside component (below).
   * **Create Revit package (.zip)**: for a colleague who builds it.

   Both live options need Revit open, with the MCP service on, and Rhino 8 installed (for its
   IronPython). Packages and `.rvt` files are kept in `<project>\packages\`.

A project keeps its drawings list and floors in `c2r_project.json`, and its decisions in
`overrides.json`.

**Inside Revit (Rhino.Inside.Revit):** in Grasshopper, add a Python 3 script component and
paste in `revit\rir_cad2revit.py` (the instructions are at the top of the file). It finds the
project's latest exported run, builds every floor in order with `build_model.py`, then the
foundations, levels and stairs with `build_extras.py`, all in the open model. It can also open
the app inside Revit.

**Tests:** double-click `run_tests.bat` (or run `python tools\run_all_tests.py --baselines`).
It prints PASS or FAIL for every suite and baseline.

The older Tk window (`app.py`) still works. Each run writes, in
`<project>\runs\<date_time>\`: `review.html` and `review.xlsx` (check every value and its
source), `revit_build\` (the files the Revit scripts build from) and `SUMMARY.txt` (what needs a
decision, in order). Save your review decisions as `<project>\overrides.json` and confirmed layer
profiles in `<project>\profiles\`; every later run applies them. Same from the command line:

```
python -m building.workflow DRAWINGS_FOLDER --project PROJECT_FOLDER [--levels "L1=Level 1"] [--use-suggested]
```

Converted drawings are kept in `<project>\dxf_cache\` and reused while the DWG is unchanged, and
converter windows a stopped run left open are closed before a run.

## Measuring it on new drawings (Stage 10)

```
python tools\scorecard.py RUN_FOLDER ANSWERS.json
```

Write down a few marks and sizes you read off a new consultant's drawings (format in the
script), run the workflow, and the scorecard says per answer: correct, flagged for review,
SILENTLY WRONG (must never happen) or not found. `tools\make_synthetic_set.py` writes a set in a
style none of the training drawings use, with its answers; `tools\blind_baseline.py` locks its score.

## Rule: nothing that works today may change unnoticed

Before and after every change:

```
python tools\baseline.py compare        every locked drawing must say "same"
python tools\sheets_baseline.py compare every training view identified as before
python tools\readers_baseline.py compare every mark's size read as before
python building\selftest.py             building model: lossless round trip
python -m building.sheets.selftest      sheet registry rules
python -m building.marks_selftest       mark and label grammar
python -m building.readers.selftest     Stage 3 readers and profiles
python -m building.linker_selftest      linker, Excel, review page
python -m building.foundations_selftest Stage 5: foundations, column schedules, column marks
python -m building.stage6_selftest      Stage 6: walls and cores, stairs, ramps
python -m building.stage7_selftest      Stage 7: level markers, level table
python -m building.stage8_selftest      Stage 8: export for the Revit build
python -m building.stage9_selftest      Stage 9: the workflow and its window
python tools\blind_baseline.py compare  Stage 10: blind set in an unseen style - 0 silently wrong, no loss
cd revit & python selftest_extras.py    Stage 8: foundations/levels script logic
cd cad2revit & python selftest.py       extractor tests
cd revit     & python selftest.py       Revit builder tests
```

`compare` also passes every run through the building model and back, and
reports it if anything is lost on the way.

## Reading drawings into one building (Stage 1)

```
python -m building.cli OUT "1st floor.dwg=Level 1" "2nd floor.dwg=Level 2" --wing B
```

Writes `OUT\building.json` (every sheet, level and element, each value with the
sheet, layer and CAD handle it came from, its status - read / default / missing -
and each element's read/model support), plus the usual `model.json` per sheet in
`OUT\<sheet>\`, so the Revit build script and the Excel work exactly as before.

If `compare` reports a difference, it lists it element by element (`-` was, `+` now).
A difference is either a bug to fix, or an agreed improvement. Only once it is
agreed is the baseline re-captured:

```
python tools\baseline.py capture --case <name>
```

## What is locked (Stage 0, 2026-10-06)

| Case | Drawing | Level |
|---|---|---|
| J1153_B_L1 | 1ST FLOOR PLAN ( B WING ) | Level 1 |
| J1153_B_L2 | 2ND FLOOR PLAN ( B WING ) | Level 2 |
| J1153_B_L3 | 3ST FLOOR PLAN ( B WING ) | Level 3 |
| J1153_B_L4 | 4ND FLOOR PLAN ( B WING ) | Level 4 |

Each baseline holds every column, beam, slab panel, fold, sunk zone, opening
and wall, plus the issue counts and the rows the schedule workbook gets. Add a case to
`baselines\cases.json` for every new drawing that is confirmed to work.

The C-wing drawing (`1153 - 1ST FLOOR PLAN ( C WING ) 14-07-2026.dwg`) is not on
this machine. Put it in this folder to switch on the 6 extractor tests that use it,
and add it as a baseline case.

## What is in a drawing set (Stage 2)

```
python -m building.sheets FOLDER_OR_DRAWINGS [--why]
```

Splits every drawing into views and says what each one is: framing plan,
foundation plan, column layout, beam/column/footing details, schedules, legend,
architectural, overview - with its title, level (L1, GF, TERRACE, ...), wing,
notes (grades, units, cover), and the reasons (`--why`).

* Views come from paper-space viewports where the drawing has layouts
  (including viewports offset by a view target), otherwise from model-space
  regions. Overviews and stray scraps are listed, not read.
* A title that names a different level from the file name is flagged as a
  LEVEL CONFLICT (the J1153 "3ST FLOOR" file is titled 1ST FLOOR).

```
python -m building.readset OUT FOLDER_OR_DRAWINGS [--levels "L1=Level 1,GF=Ground"]
```

Reads a whole set into `OUT\building.json`: every view as a sheet, and every
framing plan through the plan reader. From drawings with layouts only the plan's
own view is read (details beside it are left out); drawings without layouts are
read whole, exactly as before.

## Sizes from any source (Stage 3)

No one source of sizes is required; each reader reports what it finds, with evidence:

| Reader | Reads | Example |
|---|---|---|
| `readers\labels.py` | marks and sizes as text, in any style | `B4a(230X600)`, `GB 1(230x450)`, `GB1:230x300`, `C(230X500)`, `S2(200THK)`, `B12` + `230X600` beside it |
| `readers\tables.py` | schedule tables by their headers | multi-row headers, split marks, rows on two lines, tables side by side, `850/750` (varying depth: kept, flagged) |
| `readers\details.py` | detail sheets | a size confirmed by a matching dimension; bar callouts per mark |
| `readers\layer_sizes.py` | a size in a layer name, if any | `BEAM-230X600` (an optional hint only) |

Plan captions are kept at their position; the linker (Stage 4) attaches them to
elements. A table's own title decides its wing and part ("WING C REVISED",
"CLUB HOUSE"), not the sheet it sits on.

### Consultant profiles

`building\profiles\` - a consultant's layer names and mark prefixes, laid over the
base config without editing it. `readset` suggests one for every drawing from what
is drawn on each unknown layer (parallel beam faces, column boxes, mark texts),
writes it to `OUT\_profiles\` as **unconfirmed**, and uses it only when:

```
python -m building.readset OUT FOLDER --profile confirmed_profile.json
python -m building.readset OUT FOLDER --use-suggested        # testing only
```

To confirm a suggestion: check it, set `"confirmed": true`, keep it per consultant.

## Linking, review and decisions (Stage 4)

`readset` now ends by linking everything and writing, next to `building.json`:

* **review.html** - open in a browser. Every plan drawn element by element,
  coloured by status (read / low confidence / conflict / missing / decided).
  Click an element: every property with every source. Use a source's value, type
  one, or exclude the element - for that element or every element of that mark on
  that level - then *Export overrides.json*.
* **review.xlsx** - the same per element, plus Marks, Issues, Sheets, Support,
  and an **Overrides** sheet for decisions (`python -m building.excel --overrides
  review.xlsx overrides.json`).

Next run: `python -m building.readset OUT FOLDER --overrides overrides.json`.
Decisions win, marked OVERRIDE; the sources they overrule stay listed. A sheet's
kind or level can be corrected the same way (scope = sheet id from the Sheets tab).

The linker (`building\linker.py`):
* attaches plan captions to the element they run along (direction checked,
  measured to the beam's faces); ambiguous ones are reported, never guessed;
* splits a beam line that carries several marks into spans at the crossing beam
  or column between them (midway only if none, and then flagged);
* applies specs by mark within the same wing and level, never from another wing
  or building part;
* resolves each value: agreeing sources -> read; strong sources disagreeing ->
  conflict; only hints disagreeing -> low confidence; nothing -> missing.

## Folders

| Folder | What |
|---|---|
| `building\` | Shared building model (`model.py`), read/model support table (`support.py`), readers (`readers\`), sheet registry (`sheets\`), set reader (`readset.py`) |
| `cad2revit\` | The extractor (desktop app `run.bat`, `cli.py`), unchanged; used as the first plan reader |
| `revit\` | Revit build script and `make_sizes_csv.py` |
| `standard\` | Consultant standard, schedule workbook generator |
| `samples\training\` | Reference drawings per project: J1153_ReisMagos (locked baselines), Sasangir_MainBlock, Villa5. Hidden test sets are kept outside this folder |
| `baselines\` | The locked results, one folder per case |
| `tools\` | `baseline.py` |

## Stages

| Stage | Delivers | Status |
|---|---|---|
| 0 | Baselines and comparison | **done** |
| 1 | Shared building model with evidence and support status; current extractor wrapped as the first plan reader; must equal the baseline | **done** |
| 2 | Sheet registry, sheet classification, notes and level reader, multi-sheet input | **done** |
| 3 | Mark/label grammar; caption, table, detail and layer-size readers; consultant profiles with suggestions | **done** |
| 4 | Linker, conflicts, Excel v2, review screen, project overrides | **done** |
| 5 | Foundations, column schedules, columns across floors | **done** |
| 6 | Walls and cores, stairs, ramps (read and reviewed) | **done** |
| 7 | Level markers, floor heights, level table | **done** |
| 8 | Whole-building Revit build: export + foundations/levels script (needs a first test in Revit) | built, untested in Revit |

## Building in Revit (Stage 8)

```
python -m building.export runs\<run>          -> runs\<run>\revit_build\
```

* `revit_build\<plan>\model.json` - run `revit\build_model.py` on it exactly as on an extractor
  model.json (columns, beams, slabs, walls, with the review's values). Read `export_report.txt` first.
* `revit_build\<lowest plan>\foundations.json` - run `revit\build_extras.py` on it: footings (a
  rectangular isolated footing family must be loaded), their steps, rafts, and the levels in
  `revit_build\levels.json`. Each footing sits at its own founding level (read from the footing
  schedule, or decided in the review or on the Export page); one without any is not built. Existing
  levels are never moved unless `MOVE_LEVELS = True`.
* Nothing unsettled is built: an element whose size is in conflict or missing is left out of
  model.json and listed under "NEEDS A DECISION" (export_report.txt, `revit_build\check.json`, the
  app's Export page). `python -m building.prebuild RUN` prints the pre-build check.
* `cd revit & python selftest_extras.py` checks its logic outside Revit.
