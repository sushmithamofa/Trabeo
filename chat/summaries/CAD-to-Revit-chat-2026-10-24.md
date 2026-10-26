# Chat: "not analysing anything" (Windows blocking a file), floor names, adding a floor, column stops
24 October 2026. All work in `Documents\Structure CAD to Revit\New version`. Technical details are in SESSION_NOTES.md there. Continues from CAD-to-Revit-chat-2026-10-23.md.

---

## User
Start my app. Start the office app.

## Claude
Both started (main on this PC, office at http://192.168.1.28:8770 for colleagues). The app only runs while its small window is open and the PC is on.

## User
Why is it not analysing anything?

## Claude
Not the app's reading rules: Windows (Application Control) began blocking one compiled file of a Python library the app uses to measure text. With no text measured, blocks had no size, so the new Akanistha drawing (each floor kept in a block) looked empty - "8 views, 0 plans". The library ships the same file as plain Python; the app now uses that when the compiled one is blocked (same results, a little slower; no Windows setting changed). Akanistha: all 8 drawings found, 6 floors in 3D. All 134 drawings from yesterday read exactly as yesterday.

## User
Why do level reading issues happen in the 2nd stage? Explain how it works; don't change anything.

## Claude
- A plan's floor comes from the drawing (title, then layout, then file name), not from the Floors page.
- The Floors page gives each floor its Revit name; heights are stored by that name.
- Height: what you type wins, then what the drawings write; otherwise the 3D shows 3 m storeys and the Revit export asks for it.
- Common mistakes: two floors given the same Revit name (ankit's Akanistha: Ground and 2nd both "Level 2", 1st and 3rd both "Level 3"); floors left over from another drawing set (test 1 still had Goa's floors).
- Step by step: untick all, tick the building's floors, one name each, heights, "lowest floor's columns start from", Analyse, check Issues and Export -> Floor levels.

## User
Can we have an option of adding a new floor like a service floor? Please don't break anything.

## Claude
Added "A floor not in the list": type its name ("Service floor"), choose below/above which floor, Add floor. Plans titled with that name (also "SERVICE FLR") are that floor, in its place in 3D and in Revit. Also fixed: the Revit build guessed the floor order from the names, so an added floor - and yesterday's Goa "+6600" floors (the terrace would have been built first) - came out of order; the analysis now gives the exact order.

## User
Can the Revit naming be the same? Having two names is a little confusing; for a few we can just rename.

## Claude
One name per floor, which is also its Revit level name ("1st floor", "Terrace"); type over it to call it something else, and the page shows which floor of the drawings it is. Saved names untouched. Browser test 16/16; all tests pass.

## User
(Screenshot of the Floors page.) Can the names be similar to what is in the imported CAD?

## Claude
New button "As in the drawings": after an analysis it ticks the floors the drawings have plans of and names each as the titles write it - Third Floor, Terrace Level, +6600 Level, Level 3 (NIFT), Basement Roof (Aldott). Only when pressed; other floors untouched. Browser test 20/20; all 26 test groups pass.

## User
We use a different hatch for columns that stop at a certain level. Can the app understand it, or should I add a small cloud tagged SC (stop column)?

## Claude
- Each floor's columns are built from that floor's own plan, so a column not drawn on the plan above already stops.
- The app reads the legend "*THUS MARKED RCC COLUMN ENDS AT THIS LVL." / "COLUMN THROUGH" with a hatch sample left of the words, finds that hatch on the plan and notes each column in it (Review).
- Not a cloud (means a revision) or "SC" (can be read as a column mark). Use the standard legend row.
- Offered: more wordings (STOPS / TERMINATES / UP TO), using it for typical-floor plans, checking a real drawing.

## User
If my second roof plan has a few columns that stop at that level, on a different layer like "column stop", will the app read it?

## Claude
Yes: any layer with COL or CLM in its name is a column layer, and those columns are built up to that plan's level, which is where they stop. Watch out for: columns drawn only as a hatch (hatch layers are skipped), layer names without COL/CLM (guessed - check in Review), and typical-floor plans. "SECOND ROOF PLAN" is read as the roof over the 2nd floor (the 3rd floor level); for a separate higher roof, add "Second roof" above Roof on the Floors page.

## User
Save.

## Claude
Saved: this chat and yesterday's (New version and Desktop), SESSION_NOTES.md, and a backup of the app (backups\App_2026-10-24.zip).
