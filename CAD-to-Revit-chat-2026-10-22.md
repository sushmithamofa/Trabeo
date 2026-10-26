# Chat: aldoot beams 130 wide / short, J1153 B102 corner, B61 bending into BC6
22 October 2026. All work in `Documents\Structure CAD to Revit\New version`. Technical details are in SESSION_NOTES.md there. Continues from CAD-to-Revit-chat-2026-10-19.md.

---

## User
(Screenshot: aldoot, 9"X21" row and the beam by "UP".) Again a few beams are not modelled completely. Please don't break anything.

## Claude
This was plan M3; M2 and M4 had the same faults. The drawing has a 4.8 x 5.4 m box with an X drawn on the beam layer, and its sides lie inside or right next to the real beams. The app took the box sides for beam faces:
- **9"X21" row**: the box's bottom side lies between the row's two faces and paired with the top face, giving a 130 mm "beam". It now reads 230 mm, full length.
- **Vertical by "UP"**: the box's right side, 35 mm inside the beam, paired with the far face. That gave 330 mm, and the beam stopped 2.2 m short of its column. It now reads 300 mm, to the 12"X24" beam.
- **Top row**: the box's top side paired across the beam, giving a 330 beam over the 230. It is now one 230 beam.

New rule: a shorter line lying along a beam whose two faces are both drawn (longer, end to end, nothing between) is not one of its faces. A first version wrongly changed a NIFT Level 5 edge beam (300 to 450) and broke an existing test. Both were caught, and the rule was tightened. 106 plans: 103 identical, only aldoot M2/M3/M4 changed. Tests +4. Not changed, to ask: the box with an X is probably an opening, but its diagonals are still built as two beams.

## User
(Screenshot: J1153 3rd floor, B102 / BC5 corner, curve and green sliver.) Incomplete beams are still there - why is this happening?

## Claude
Nothing changed. The project had not been analysed since 15 October, and Review shows the saved result. This corner (the curve 280 mm short, S8 leaking down beside B102) was fixed on 19 October. Today's code gives a complete beam and S8 stays in its bay. Press Analyse. Offered: a notice when a result is older than the app.

## User
(Screenshot: B61 / BC6 corner, red line along B61 into BC6.) Some minor model error - why is this happening? Without breaking anything.

## Claude
B61's faces stop 400 mm below BC6 and bend as two arcs into BC6. Straight beams are joined to their supports before curves are read, so B61 was carried straight on to BC6 and the curve was built beside it. Now a straight beam whose drawn faces end where a curve of the same width starts and carries on stops there, and the curve carries the beam on. Every plan checked: 101 identical. The same mistake was fixed on A wing 2nd floor BC3 (5 copies) and Stilt floor B40, all drawn over the CAD. Test added (fails on the old code). 665 tests; the readers baseline shows only the 5 Sasan differences from 21-10.

## User
Restart my app. / Save.

## Claude
Apps restarted (8765, office 8770). Saved: this chat file, SESSION_NOTES.md, and backups\App_2026-10-22.zip.
