# Structural Drawing Guidelines (for consultants)

What structural consultants need to put in their CAD drawings so the app can build
the Revit model without queries. The main rule: every column, beam, slab, wall and
footing has a mark on the plan, and every mark has a size somewhere in the set
(plan label, schedule, detail or section). Written from how the app actually reads
drawings: sheet titles, marks, size formats, schedule headers, slabs, sunk areas
and openings.

| File | What it is |
|---|---|
| `Structural-Drawing-Guidelines.pdf` | The guideline, A4, to send to consultants |
| `Structural-Drawing-Guidelines.html` | The same, to open in a browser or edit. Ctrl+P saves a new PDF |

It does not ask consultants to change their layer names or drafting style. The app
adapts to each consultant. It replaces nothing: the older J1153-specific standard in
`standard\` stays as it was.
