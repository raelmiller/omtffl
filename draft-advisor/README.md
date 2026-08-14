# OMTFFL Draft Advisor

A private, auction-night companion. When a player is nominated it shows a
factsheet, a fair-price range that reacts to how much money is left in the
room, the rivals most likely to bid, and a clear verdict on whether *you*
should — plus a live "gems" board of the best value still available.

**This is for your eyes only.** Run it on your own device. It is a separate
page from the shared auction app — never host it where the room can see it.

## Files

| Path | What it is |
|---|---|
| `advisor.html` | the tool (open in a browser) |
| `dossiers.html` | every manager's 3-year auction record |
| `data/fpl_data.json` | baked FPL stats (players, history, set-piece notes) |
| `data/price_model.json` | league price bands + manager bidding priors |
| `data/*.json` | draft history, enriched sales, manager dossiers |
| `scripts/*.py` | the data pipeline (see below) |

## Running it

The page loads its data with `fetch`, so it must be served over http, not
opened as a `file://`:

```bash
cd draft-advisor
python3 -m http.server 8099
# then open http://localhost:8099/advisor.html
```

### On the night
1. Start the auction app (see `exec-search/fpl-draft`).
2. Open the advisor, paste the auction server URL, hit **connect**.
3. In the **Teams** tab, map each auction team to a league manager (it
   auto-maps by name where it can) so rival predictions use their history.
4. Set **"I am team…"** in the header so verdicts know your budget and gaps.
   Check the **League** tab reads the right shape — team count and budget per
   team come straight from the auction server, but if the roster format isn't
   2 GK / 5 DEF / 5 MID / 3 FWD you must set the slots there yourself.
5. As players are nominated the factsheet appears automatically. Between
   nominations, work the **Gems** tab.

No auction connection? The tool still works fully offline — use **Search**
to pull up any player, and **demo** to replay the 2025-26 draft as a dry run.

## How the numbers are built

- **Projection**: `0.7 × last season + 0.3 × season before`, but only when
  those two seasons are consecutive and the earlier one had real minutes;
  otherwise just the last season. Each season is **minutes-adjusted** toward
  a full 3000-minute rate (upward only, capped 1.6×, 900-min floor) so a
  player who missed time isn't understated. Players new to the PL estimate
  from FPL's own price (median of same-position peers within £0.5m). Any
  projection can be **manually overridden** on the factsheet — your call
  beats the model for transfers, role changes, new managers.
- **Fair range**: the league's historical price for that position and
  projection band (25th/median/75th percentile of what was actually paid),
  **interpolated smoothly** between adjacent bands (no cliffs at boundaries),
  scaled by a **room-liquidity multiplier** — `sqrt(money-per-slot-left ÷
  the starting money per slot)`, clamped 0.6–1.6. Early with full budgets it
  sits at ×1.0; it rises when the room is flush and falls when everyone's
  skint — and by a **league-size multiplier** (below) when the room isn't the
  16 teams the price model was trained on.
- **League size**: the price bands come from 16-team drafts. Team count
  doesn't change money per slot — every team still brings one budget for one
  squad — but it changes how many players get drafted, and so how good the
  best *undrafted* player is. That replacement-level player is what you fall
  back on when you lose an auction, so he sets what a target is really worth.
  In a smaller room the mid-tier collapses toward the minimum bid (why pay
  £4m for a 130-pt mid when a 124-pt one goes unsold?) while elites hold
  their price, having no replacement at any size. The multiplier is
  `sqrt(surplus-over-replacement now ÷ surplus at 16 teams)`, renormalised so
  total spend stays in proportion to the room's money — a redistribution
  across tiers, not a blanket discount. See the **League** tab.
- **Marquee premium**: a high projection alone doesn't earn the £20m+ tier —
  that premium tapers by how far a player's FPL price reaches into the top
  tail of his own position. Keeps genuine big hitters elite; prices
  consistent-but-modest scorers as strong non-marquees. Flagged on the card.
- **Rivals**: each team scored on open slots at the position, spare budget,
  their manager's historical position-spend share, club affinity, marquee
  appetite, and whether they've bought this player before.
- **Verdict**: compares the live bid (or the expected market price when
  browsing) to the fair range → VALUE / FAIR / OVERPRICED, gated by your
  budget and open slots. Positional **scarcity** (starter-grade players left
  vs open slots) upgrades a fair price to PRIORITY, or an over-market price
  to STRETCH.

## Rebuilding the data

```bash
# 1. FPL stats — runs in GitHub Actions (container can't reach the FPL API)
#    workflow: .github/workflows/bake-fpl-data.yml  (or run locally)
python3 scripts/bake_fpl_data.py

# 2. everything downstream (safe to run anytime)
python3 scripts/ingest_draft_history.py     # workbooks -> draft_history
python3 scripts/build_player_breakdown.py    # + FPL name join, enriched_sales
python3 scripts/build_manager_dossiers.py    # dossiers.html
python3 scripts/fit_price_model.py           # price_model.json
```

## Tunable dials (in advisor.html)

- **Minutes adjustment**: `FULL_MIN` (3000), `MAX_UP` (1.6), `MIN_M` (900).
- **Marquee taper**: `marqueeBand` uses each position's 80th→97th price
  percentile as the premium window.
- **Scarcity threshold**: `scarce = strong <= open+1` in `myVerdict`.
- **Liquidity multiplier**: clamp + curve in `liquidity()`.

## Known limits

- **Price bands** are league-wide; a few positions/bands have thin samples
  (GK 180+, DEF 180+). Smoothing interpolates across neighbours.
- Promoted-club and new-signing players have thin PL history — estimated
  from FPL price and flagged, not invented. Use the override.
