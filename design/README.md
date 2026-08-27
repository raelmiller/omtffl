# Design

Working files for the season app's UI directions. Each `.dc.html` is one
artboard on a shared canvas; `canvas.json` places them and picks the opening
view.

**Floodlight** is the chosen direction, light by default. The chrome stays
night-dark and the body is lit paper: the direction is the contrast between
the two rather than a palette that flips wholesale, which is also why the
dark toggle costs the app nothing structural — only the body moves.

The accent does two jobs. As a **fill** (`--a`, lime) it is always paired with
ink text and reads on both grounds, so buttons, the active tab and the "yours"
row need no light/dark variant. As **ink** (`--ai`, deep grass green) it
carries text on paper: links, positive numbers, the countdown. `Main.dc.html`
derives the second from the first with `color-mix`, so the accent tweak moves
both together.

| File | Page | What it is |
| --- | --- | --- |
| `Main.dc.html` | Floodlight | The table, light — the default |
| `Dark.dc.html` | Floodlight | The same screen with the theme toggle thrown |
| `Mobile.dc.html` | Floodlight | 390×844, navigation moved to a bottom bar |
| `States.dc.html` | Floodlight | Every control in every state: rest, hover, focus, active, loading, empty, error, disabled |
| `Matchday.dc.html` | Not taken | Direction B — editorial, Bricolage Grotesque on ink-navy |
| `Terrace.dc.html` | Not taken | Direction C — programme print, Anton caps, signal orange |

Everything is built from the values already in
`season-app/app/static/style.css` rather than invented: the same panel radii,
cell padding, uppercase label tracking and accent-on-ink relationship. What
changed is the idiom, not the identity.

None of them reproduce Premier League branding — the palette, typefaces and
marks are original. The shared conventions (dark chrome, a tab rail, dense
tables, score chips) are the genre, not the brand.

`omtffl-matchday-ui.html` is generated from these files and is not committed:
it carries the whole canvas editor and runs to about 2.5 MB. Reseed it with
the `/design` skill's helper rather than editing it.
