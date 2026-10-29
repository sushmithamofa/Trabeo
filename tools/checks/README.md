# Drawing-wide checks (developer tools, this PC)

Used on 2026-10-19 to make sure beam fixes are general rules, not one-off fixes.
They read the sample drawings found under Downloads / Documents / Desktop\Shushmita
(paths at the top of `runall.py`).

- `runall.py CODE_DIR OUT_DIR` – run the extractor on every plan (69) and save the results.
- `cmpruns.py OLD_OUT NEW_OUT` – compare two runs element by element, by position.
- `audit_all.py CODE_DIR OUT_DIR` – look for each mistake kind on every plan view:
  beams overlapping beams, slabs under beams, beam ends resting on nothing,
  drawn lines left uncovered, odd wall shapes. Uses `audit_one.py` per plan.
- `tiles_flags.py` – close-up pictures of each flagged spot over the CAD.
- `plot_review.py` – a building.json drawn over its CAD.

Before any extractor change: run `runall.py` on the old code, and again on the new code,
then `cmpruns.py`. Every difference must be explained, with the drawing open.
After that, run `tools\run_all_tests.py --baselines`.
