# Chat: beams that are not in the drawing
15 October 2026. All work in `Documents\Structure CAD to Revit\New version`. Technical details are in SESSION_NOTES.md there. Continues from CAD-to-Revit-chat-2026-10-14.md.

---

## User
Start my app.

## Claude
Started the app at http://127.0.0.1:8765/ (`run_app.bat`, this PC only). `run_office.bat` for colleagues.

## User
Continuing the debugging: a few beams are created even though they are not in the CAD. Don't break anything.

## Claude
J1153 Wing C 3rd floor (09-09-2026), drawn over the CAD: 7 beams that are not in the drawing.
- **Shaft and duct boxes drawn on the beam layer**, with an X: the box beside B73 and a small box on B16. Their sides were built as beams. Rule: an unmarked line along the edge of a cut-out (a box with an X) is the cut-out, not a beam. A beam with a mark is always kept. The cut-out's sides still separate the slab bays, so a bay with no slab mark is not joined to a marked one.
- **230 mm stubs across beam ends** (B30 at B87, the B101A and B107A ends): the app took the small square end of a beam for a curve. A curve now never turns a sharp corner.
- Removing the B101A stub would have lost slab SC (the stub closed it by accident). The real shape: both faces of the curved B101A carry on 220 mm straight into B101. That straight end is now built, so SC stays, with a cleaner edge.

Checked on every drawing on this PC (36 plans, before and after): the other changes were all corrections (C wing 2nd: a fake stub had taken mark BC7A; Sasan Gir: a fake 610 mm "beam" inside a cut-out; a cut-out edge on Sasan Gir SF). A first version gave slabs to two bays with no slab mark (Sasan Gir); caught and fixed before release. 23 of 23 suites pass.

## User
(3D screenshot) Why are there beams that are not in the drawing but in the model? (the ring of beams over the lift)

## Claude
The lift core walls (LW1/LW2) are drawn on the wall layer and again on the beam layer; the middle wall's two lines became a 230 beam, the top and right walls' faces single-line beams. Rule: an unmarked line inside a wall the drawing names (LW1, LW2, LW7 …) is that wall. A first version used every wall outline and removed real beams (Villa 5 lift beams GB15/GB29, Sasan Gir plinth GB8 - labels the app does not read), so it was limited to named walls. Also fixed: the same lift walls on C wing 2nd floor and LW7 on J1153 First Floor and Stilt A. 23 of 23 suites pass.

## User
(Review and Revit screenshots) The beams are not in the correct place (B47). Why is it reading the CAD wrongly, and why in Revit? Fix without breaking anything.

## Claude
B47 is drawn as two lines 230 apart. A small duct box sits just under it, and the app paired B47's top line with the duct's bottom edge (460 apart), threw that away, and built B47 on its bottom line, 115 mm out of place. Revit is built from the same data. Cause: overlap was measured on the shorter line, so any short line beside a long beam looked like a perfect partner. Fix: when another line ties, lies at a different distance and runs along much more of the beam, it is the beam's real other face.

The same bug is fixed on: C wing 3rd (B107 115 mm off and a fake 440 mm BC8 - now BC8 is the curve as drawn, and the SC slab beside it is built like the mirrored side); A wing 1st and 2nd, J1153 First Floor, Stilt (650 mm "beams" across the stair cut-outs, now 230 and centred); Sasan Gir Terrace (two beams side by side, now one 300). Two earlier attempts were rejected: one shortened Sasan Gir plinth GB8 by 540 mm, the other made C wing 2nd BC6/B33 two overlapping beams. All 36 plans compared, every change drawn over the CAD; 23 of 23 suites pass, 598 tests. Press Analyse again, then re-export to Revit.

## User
Save.

## Claude
Saved the chat, the notes and `backups\App_2026-10-15.zip`.

## User
Start my app. (Twice; the app had stopped.)

## Claude
Started the app at http://127.0.0.1:8765/.

## User
(Rhino message "Unable to load RhinoRenderCycles.rhp plug-in: initialization failed") Help me fix the Rhino issue, but don't touch the app.

## Claude
Not the app, and nothing broken for good. The Rhino window open at the time had started at 14:52; Rhino 8 was uninstalled at 14:53 and reinstalled at 15:03 while it stayed open, so its plug-in files were replaced underneath it. (A repair at 13:16 had also run with five Rhino windows open.) Graphics card (RTX 3050), driver and .NET runtimes are fine; no restart pending. Fix: click OK without ticking "Never load this plug-in again", save and close Rhino, start it again. If the message comes back: close Rhino, Settings > Apps > Rhino 8 > Modify > Repair, restart the PC. Rhino has since been closed.

## User
Save.

## Claude
Saved.

## User
Start office app. (Started for colleagues at http://DESKTOP-1B6U4I7:8770/.)

## User
Is all the analysed data stored?

## Claude
Yes, on this PC, nothing deleted by itself: each colleague's projects in `Documents\CAD to Revit office\<e-mail>\` (drawings, converted drawings, every analysis in `runs\<date_time>\`, decisions in overrides.json, exports). Not in the app backup - back that folder up too.

## User
A team guide (no admin details) in `presentation\team presentation`; links clickable; a "Need help?" slide with the team issues Google Sheet (own tab per person). A checklist / guideline for structural consultants.

## Claude
Team guide: 15 slides (HTML + PDF) + README with a message to send. Consultant guidelines: `presentation\consultant guidelines\` (HTML + 7-page PDF): every element marked, every mark sized; how the app reads drawings; marks, sizes, schedules, sections, plan, layers; checklist.

## User
Are mark prefixes hard-coded? Different consultants write marks differently (S1 on the plan, S-1 in the table). Compare two approaches and fix it, with: S01 = S1 only as a suggestion; match within floor and schedule; weighted evidence, schedule title strongest; saved mappings are hints, the drawing wins; S1-2 never S12; ambiguous cases to Review; verify on all drawings. Don't break anything - team demo tomorrow.

## Claude
Checked first: S1 = S-1 was already handled, S01 and S1 already apart, schedules already matched within the floor. Built: prefix meanings learned from each set's own schedules, plan positions, layers, saved hints and the built-in list (weighted); unclear cases go to Review with buttons; "did you mean" suggestions (never matched automatically); S1-2 kept apart from S12. 17 new tests pass, Review buttons tested in a browser. Guidelines and team guide updated.

## User
Save.

## Claude
Saved the notes, this chat and a backup (`backups\App_2026-10-15b.zip`). At the time of saving, the before/after comparison on all drawings and the full test run were still running - not yet verified; the app was still running the old code.

---

## Still open
- **Before the demo:** finish the before/after comparison on all drawings and `run_tests.bat`, then restart the app (and the office app).
- C wing 2nd floor: BC6/B33 built as one 690 mm band (as before today); needs its own fix.
- LW1 and LW2 share one wall outline in the drawing, so the wall is built twice (Review warns).
- From before: rounded slab edges where a bay has no beam on a side (J1153 1st floor); 4 S1 marks with no edges; "Start a new drawing set" also clearing the floor list / Export settings; rename "old extractor"; UGWT RB1 beams; Export levels; Stilt A sunk strips; ALCAZAR HB width; office mode from a second PC; keep a copy of `C:\Users\Admin\.c2r_admin`.
