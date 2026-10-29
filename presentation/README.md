# CAD to Revit Automation — J-1153 presentation

Deck for the Reis Magos Goa Housing structural CAD-to-Revit discussion. 22 slides,
built from the audit of `1153 - 1ST FLOOR PLAN ( C WING ) 14-07-2026.dwg` and its
GFC layout and link-detail sheets. Written for our own team and management first,
with the structural consultant as the second audience in the same run-through.

## Files

| File | What it is |
|---|---|
| `CAD-to-Revit-Presentation.html` | The deck. Double-click to open in a browser and present. |
| `CAD-to-Revit-Presentation.pdf` | The same 22 slides as a 16:9 PDF, for emailing or printing. |
| `source/project/slides/*.html` | One file per slide. Edit these to change content. |
| `source/project/deck.json` | Slide order, sections and typefaces. |
| `source/_head.html`, `source/_tail.html` | Page shell, styling and the presenter script. |
| `build.sh` | Rebuilds the HTML from `source/`. |

## Presenting

Open `CAD-to-Revit-Presentation.html` in Chrome or Edge, then press F11 for full screen.

- **Left / Right arrow**, Page Up / Page Down, Space, or click — move between slides
- **N** — show or hide the speaker notes for the current slide
- **P** — print, which is also how you re-export the PDF
- **Home / End** — first or last slide

Slides scale to whatever the screen or projector gives them, so it works on any
resolution. Typefaces come from Google Fonts, so with no internet the deck falls
back to Arial and Courier; everything else still renders. Each slide has a
permalink, for example `CAD-to-Revit-Presentation.html#centreline`.

## Editing

1. Edit the slide file in `source/project/slides/`, or add a new one and put its
   id into the `order` list in `source/project/deck.json`.
2. Run `bash build.sh` from this folder.
3. To refresh the PDF, open the HTML and press **P**, or run:

```
"C:/Program Files/Google/Chrome/Application/chrome.exe" --headless=new --disable-gpu \
  --no-pdf-header-footer --virtual-time-budget=20000 \
  --print-to-pdf="CAD-to-Revit-Presentation.pdf" \
  "file:///C:/Users/TWO/Desktop/Sushmitha/Structure CAD to Revit/presentation/CAD-to-Revit-Presentation.html"
```

## Figures still to fill in

Three numbers were left as bracketed placeholders because we do not have them yet:

- Slide 3 — effort today `[__ hrs per floor]` and the target `[__ hrs]`, from our Wing C record
- Slide 4 — `[__]` floors across `[__]` wings on J-1153
- Slide 21 — durations for the three phases

## Structure

| Section | Slides | Covers |
|---|---|---|
| Why | 1–4 | The case for generating the model instead of redrawing it |
| How | 5 | The five-step pipeline, ending on the audit |
| The Wing C audit | 6–12 | What the DWG does right, the three blockers, the 21 findings |
| The standard | 13–18 | Setup, layers, element rules, marks, our Revit template |
| What's next | 19–22 | The eight deliverables, seven open questions, the pilot |

## Online copy

The same deck is also published as a private artifact, which exports to PowerPoint
and PDF directly: https://claude.ai/artifact/T4f5TekVmyvpRRdCjypVe7

It is private until shared from that page's Share menu. The files in this folder
are the working copy; if you edit the deck there, the two will drift apart.
