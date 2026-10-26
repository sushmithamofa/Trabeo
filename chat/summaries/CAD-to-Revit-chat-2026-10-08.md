# Chat: Making CAD to Revit a sellable product
8 October 2026. Planning, a code review, then changes to the original app on the Desktop.

---

## User
(Pasted advice from another assistant on what the app needs before it can be sold: dependable workflow, international units and conventions, revisions, Revit installation, recovery, licensing, a pre-build check, project coordinates, evidence-based quality reporting.) Don't make changes, just tell your inputs.

## Claude
Agreed with the advice. The must-haves before charging money:
- **A review step before building**, with a confidence level for each element. The app should never quietly guess.
- **A normal Revit add-in with an installer**, instead of the live Revit MCP build.
- **Measured accuracy on drawings the app has never seen**: 5 to 10 projects from other consultants.

Additions:
- Saved per-consultant profiles as the advantage over competitors.
- The Excel file as the review step, with each row linked back to its drawing.
- DWG reading is a cost decision: ODA costs money each year, LibreDWG is GPL, so start with DXF or AutoCAD Core Console.
- Rhino.Inside as an extra, not a requirement.
- Check every dependency's licence.
- Predictable AI costs and an offline mode.
- Liability wording: "a model for engineer review".
- A support bundle button.
- Schedules and sheets matter for sales.

Proposed order: review screen → Revit add-in → accuracy test set → pilot with 3 to 5 firms → licensing and support → revision updates → wider scope.

## User
It must be sold in the market. It must not override itself with anything else and must be independent. It must model any consultant's DWG for all units. After importing and selecting a level, floor-to-floor heights can be set, but some designs have stepped beams and several levels within one floor. Don't assume any beam size; everything must match the CAD.

## Claude
Restated the requirements:
- Independent of any office template.
- Nothing overrides the drawing.
- No assumptions about sizes.
- Works on any consultant's drawings, in any units.
- Stepped beams and several levels within one floor, read from per-element level marks.
- Missing information becomes a question for the user, and every value shows where it came from.

## User
Independent means: it's an online tool, but it must not depend only on the training CAD files.

## Claude
Understood: the requirement is that it isn't overfitted to the sample files. That rules out:
- hardcoded layer names, block names and tag formats from the samples
- sizes learned from the samples
- fixes written for one particular file

Instead: read structure from geometry and context, detect each file's conventions when it's imported, and test on drawings from consultants never used during development.

## User
If the hardcoded parts are removed, will it affect modelling, e.g. column locations or beam locations and angles?

## Claude
Read the code (read-only). Column and beam positions and angles come from the drawn geometry, so they won't move. But:
- **Layer names** in `config.json` are how beams and columns are found. Removing them without a replacement means elements go missing.
- **Schedule layer names** are how beam depths are read.
- **Default sizes** (230/350/230 and slab 150) are assumptions and should go.
- **The 230 mm beam width used when cutting slab edges** would move slab edges if removed.
- **Mark prefixes** decide element types only.
- **The MOFA hatch→depth table** is still active.
- **Beam ends can move up to 800 mm when beams are joined.**

Safe order: automatic layer detection → schedule detection → remove defaults → remove the MOFA table, checking against `selftest.py` after each step.

## User
(Pasted a review of that answer.) What is your review?

## Claude
Checked the 800 mm question in the code:
- An end moves only along its own line, trimming back is limited to 300 mm, and an end never leaves its column.
- The beam-to-beam join only writes a total, not a record for each beam. That's the real gap.

Also confirmed the MOFA table was still active ([extract.py:1629](cad2revit/core/extract.py), [extract.py:1653](cad2revit/core/extract.py)).

Agreed with the review:
- Detection should suggest, not decide.
- Layer names are fine as saved profiles.
- Unknown widths should leave slab edges unresolved.
- "Nothing breaks" was too strong.
- Step zero is saving baseline results.

## User
Don't change anything?

## Claude
Confirmed: nothing had been changed; only reading and searching so far.

## User / Claude (further review rounds)
Agreed details:
- Flag trims, changed supports and nearly-equal targets, not only large moves.
- Keep the original endpoints alongside the adjusted ones.
- Remove the MOFA table completely.
- Baselines should hold both the as-read and the adjusted geometry.
- Convert to millimetres at import; ask when units are uncertain.
- Keep a separate decision log.
- Identify each drawing by a content hash and each code version by a source fingerprint.
- Compare runs by meaning, ignoring only fields that always change.
- Acceptance check: adding baseline output must leave existing results unchanged.

## User
OK, please do all necessary changes based on the discussion, and don't break anything.

## Claude
Done in the Desktop folder (`cad2revit/` and `revit/`). Verified on 6 sample B-wing drawings: element geometry and sizes are identical to the original code. Tests: 164 extractor tests and 44 Revit-script tests pass.
1. **`baseline.py`** saves runs and compares them by meaning. The original output was saved first, and two runs of the original code compare as identical.
2. **`decisions.csv` and `audit.json`** record every correction, with the geometry before and after. Unusual ones become "adjustment" queries, about 37 to 48 per drawing.
3. **Default sizes removed** from `config.json` and from the Revit builder. Elements with no size are not built; they're listed in `needs_decision.csv`.
4. **MOFA hatch table replaced** by reading legends without a heading from the drawing itself. Same depths as before.
5. **`core/units.py`** converts to mm, or stops and asks. There's a Units box in the app and a `--units` option on the command line.
6. **Shape-based layer suggestions** in `--inspect`, marked "?".

Notes:
- `build_model.py` was not run inside Revit.
- One joint tie: B68/BC2 close from the other side after an inch round trip.
- Still open: the 3000 mm storey height, stepped beam levels, and drawings from other consultants.
- A backup of the original code was saved in the session scratchpad.

## User
Push the new version to https://github.com/sushmithamofa/Structure-workflow.git, using GitHub Desktop.

## Claude
Git on the command line can't log in. Plan:
1. You clone the repo in GitHub Desktop.
2. Claude copies the new version in and commits.
3. You push.

Client data (outputs, DWGs, J1153 workbooks) to be left out.

## User
`C:\Users\Admin\Documents\Structure CAD to Revit\New version` — this is the latest, right?

## Claude
Yes, that's the universal app (stages 0 to 10, web app) and it's what should go to GitHub. Correction: today's changes went into the Desktop original by mistake, because the Documents folder wasn't found. `New version` doesn't have them. Offered to restore the Desktop original from the backup and to bring the changes into `New version` later.

## User
Keep 2 folders: rename one as the app, the older one for local testing, and open the correct folder in VS Code.

## Claude
Plan:
- `Documents\Structure CAD to Revit\New version` → `...\App`, updating the fallback path in `revit\rir_cad2revit.py` and the project notes.
- `Desktop\...\Sushmitha\Structure CAD to Revit` → `Structure CAD to Revit (local testing)`.
- Then open `App` in VS Code.

The rename was blocked: the web app (`python webapp.py`, process 37804) is running from `New version`, and the Codex app may have it open. Waiting for the user to close them.

## User
Give location?

## Claude
- Current: `C:\Users\Admin\Documents\Structure CAD to Revit\New version` (latest) and `C:\Users\Admin\Desktop\Shushmita\Shushmita\Sushmitha\Structure CAD to Revit` (older).
- After renaming: `C:\Users\Admin\Documents\Structure CAD to Revit\App` and `C:\Users\Admin\Desktop\Shushmita\Shushmita\Sushmitha\Structure CAD to Revit (local testing)`.

## User
Please save the chat.

---

## Still to do
1. Close the web app and Codex, then rename the two folders and open `App` in VS Code.
2. Clone the GitHub repo in GitHub Desktop, copy `App` in (without client data), commit, then push.
3. Optional: bring today's changes into `App`, checked against its own baselines, and restore the Desktop original from the backup.
