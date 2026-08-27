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
| `logo.png` | — | The league crest, 192×192, sat top-right in the bar |

The crest is the league's own mark — a crowned paschal lamb in deep purple,
"OMTFFL" set around the ring. It sits furthest right in the top bar, past the
manager chip, drawn at 34px (30px on the phone) with the image's own white
ground doing the work of a coin. The chrome moved with it: the bar, tab rail
and dark-mode grounds shifted from cool slate to aubergine (`#171122`,
`#1E1730`, `#0D0916`) so the badge belongs to the surface it sits on rather
than being dropped onto it. Lime on aubergine is a deliberate complementary
pair, not a new colour in the palette. The left-hand `OMTFFL.` wordmark stays:
at 34px the crest's ring lettering reads as texture, so the lamb works as a
badge rather than a second wordmark.

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
