# OMTFFL Draft Advisor — Project Scope

A live auction-day advisor for the OMTFFL £50m draft. When a player goes up
for auction, it shows an instant factsheet, a recommended bidding range that
accounts for the state of the room, a prediction of which rivals will be in
on the player, and a clear bid / pass verdict — plus a "remaining gems"
panel to fix the weak late-draft phase.

**Feasibility verdict: fully feasible, including the live hookup to the
auction app.** No unknowns remain after reviewing the auction app source.

---

## Why the live connection is easy

The auction app (`fpl-draft/` in the `exec-search` repo) is an Express +
Socket.io server we control. It already broadcasts everything the advisor
needs to every connected client, with no changes required:

| Broadcast | Contents | Advisor use |
|---|---|---|
| `stateUpdate.currentAuction` | player on the block (FPL id, name, position, club), current bid, leading team | triggers the factsheet + live verdict |
| `stateUpdate.teams` | all 16 budgets and squads | money left in the room, positional gaps per rival |
| `stateUpdate.soldPlayers` | every sale: player, buyer, price | live inflation tracking, rival tendency updates |
| `playersLoaded` | full FPL player list (bootstrap-static ids) | joins auction players to the stats dataset |

The advisor connects as one more Socket.io client. Because player ids come
from the same FPL `bootstrap-static` feed the advisor's dataset is built
from, matching is exact by id (fuzzy name match only needed for the rare
`startCustomAuction` manual entries).

**Privacy note:** the advisor must NOT be a route on the shared auction
server — everyone in the room connects to that. It runs as a separate page
open only on Rael's device, connecting outbound to the auction server's
socket.

---

## Components

### 1. Data foundation (pre-draft, offline)
- **FPL stats bake**: pull `bootstrap-static` + per-player `element-summary`
  (past-season history) + set-piece notes in the days before the draft;
  bake into a static JSON dataset. Factsheets then need zero network on the
  night.
- **Draft history ingest**: normalise 3 years of OMTFFL auction results
  (player, position, buyer, price, draft order if available) into one file.
- Known gaps to flag, not solve: promoted-club players have thin PL
  history; summer transfers can make last-season club context misleading.

### 2. Factsheet
Per player: last-season points, goals, assists, minutes, xG, xA, xGI,
defensive contributions, set-piece duty (pens / corners / free kicks),
past-season totals, price paid in previous OMTFFL drafts (and by whom).

### 3. Pricing + rivals model
- ~720 historical sales (3 yrs × 16 teams × 15 players) → enough for
  explainable heuristics + regression; deliberately not ML.
- **Bid range** = base value (projected points vs positional replacement
  level) × room inflation (money remaining vs slots remaining) × scarcity
  at position.
- **Rival prediction**: per-drafter priors from history (club bias,
  early-spender vs hoarder, position timing) × live constraints (budget,
  open slots at this position).
- **Verdict**: bid range crossed with our own budget, gaps, and value above
  the best likely alternative still in the pool.

### 4. Remaining gems panel
Live ranking of undrafted players by projected value per £ — the answer to
the late-draft "just grab last year's points" problem.

### 5. Deployment
- Auction app: Railway free trial expired. Options: original LAN mode
  (laptop + room WiFi, zero cost), Railway Hobby (~$5/mo), or Render/Fly
  free tier. Decision needed before draft night.
- Note: auction server state is in-memory — a mid-draft server restart
  loses everything. Worth adding a small state-snapshot-to-disk to the
  auction app as insurance (optional, ~20 lines).
- Advisor: static page + baked JSON, hosted anywhere (GitHub Pages works),
  configured with the auction server URL.

---

## Build phases

1. **Data foundation** — FPL bake pipeline + draft-history ingest.
2. **Factsheet UI** — type-ahead search, works standalone with no auction
   connection (also the fallback if WiFi misbehaves on the night).
3. **Draft engine** — socket connection, live state, bid ranges, rival
   read, verdict, gems panel.
4. **Polish + dry run** — replay a past draft from history data as a full
   rehearsal before the real night.

---

## Needed from Rael

- [ ] 3 years of draft data (any format — spreadsheet, screenshots, etc.)
- [ ] Confirm rules were the same all 3 years (16 teams, £50m, 2/5/5/3) —
      the inflation model needs to know if not
- [ ] Nomination rules (who puts players up, in what order)
- [ ] Deployment choice for the auction app on draft night (LAN vs hosted)
