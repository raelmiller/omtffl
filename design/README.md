# Design

Working files for the season app's UI directions. Each `.dc.html` is one
artboard on a shared canvas; `canvas.json` places them and picks the opening
view.

| File | What it is |
| --- | --- |
| `Main.dc.html` | Direction A, *Floodlight* — app shell, dense tables, Archivo + IBM Plex Mono on an electric-lime accent |
| `Matchday.dc.html` | Direction B, *Matchday* — editorial, Bricolage Grotesque on ink-navy, amber |
| `Terrace.dc.html` | Direction C, *Terrace* — programme print, Anton caps, bone on near-black, signal orange |
| `Mobile.dc.html` | Direction A at 390×844, navigation moved to a bottom bar |
| `States.dc.html` | Direction A's controls in every state: rest, hover, focus, active, loading, empty, error, disabled |

All three are built from the values already in `season-app/app/static/style.css`
rather than invented: the same panel radii, cell padding, uppercase label
tracking and accent-on-ink relationship. What changes between them is the
idiom, not the identity.

None of them reproduce Premier League branding — the palette, typefaces and
marks are original. The shared conventions (dark chrome, a tab rail, dense
tables, score chips) are the genre, not the brand.

`omtffl-matchday-ui.html` is generated from these files and is not committed:
it carries the whole canvas editor and runs to about 2.5 MB. Reseed it with
the `/design` skill's helper rather than editing it.
