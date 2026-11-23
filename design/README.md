# Design prototypes

## Open the prototype
**`web/prototype/explorer.html`**: the Explorer prototype as a single standalone file. Double-click it to open in any browser; no server or install is needed (an internet connection loads the fonts).

It uses **sample, illustrative data**: the companies are real, but every relationship, year, importance flag and country shown is made up to demonstrate the interface.

What to try:
- Search a company (NVDA, SYM, RKLB) or a component (lidar, harmonic reducers); switch depth between This, 1 hop and 2 hops
- Drag the **As of** year slider; use Hide stale / Show stale
- **By country** layout and the **Supply exposure to** Taiwan / Japan / US filter
- **Chokepoints** tab; click industry tags or components in the right panel

## Online version
The same prototype, plus the earlier screens and the three style directions, lives on the design canvas:
https://claude.ai/artifact/Fpw12P98t7xP9XiC2iy91H

Planning whiteboard: https://claude.ai/artifact/8rmKaSXTQujxNPTdbuAyAe

## `canvas-source/`
The canvas's source files, kept for reference. They use the canvas's own component format (`*.dc.html`) and **won't run on their own**. Use `web/prototype/explorer.html` instead.

| File | Screen |
|---|---|
| `Explorer.dc.html` | **Current design:** map canvas, relationships table, year slider, importance, geography, components |
| `Company.dc.html` | Earlier company-search screen (dark) |
| `Main.dc.html` | Earlier industry-graph screen (dark) |
| `DirA.dc.html`, `DirB.dc.html`, `DirC.dc.html` | Style directions: research desk, pro terminal, map canvas |
| `canvas.json` | Canvas layout |

The standalone `explorer.html` reuses the exact logic from `Explorer.dc.html`. If you change one, rebuild the other, or treat `explorer.html` as the source of truth from now on.

## Turning it into the real viewer
`web/index.html` is the working viewer for real pipeline output. The plan (see `docs/ROADMAP.md`) is to rebuild it in the Explorer style: same layout, but it reads `web/data/*.json` instead of the sample data inside the prototype.
