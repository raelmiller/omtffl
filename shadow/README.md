# Shadow league

Scores the league in parallel with the official FPL game, to prove the scoring
engine is correct **before** anything depends on it. Nothing here affects the
real draft app; it's a private validation exercise for one season.

## Why score it ourselves

The engine computes points from **raw match stats**, not from FPL's
`total_points`. Copying their number would prove nothing. Deriving it means:

- we can check ourselves against them, player by player, every gameweek; and
- next season's rule changes (the 2/5/3/5 structure, custom scoring) are a
  config change rather than a dependency on FPL agreeing with us.

## Files

| Path | What it is |
|---|---|
| `scoring.py` | the rules engine — raw stats → points, driven by the `RULES` table |
| `test_scoring.py` | hand-worked unit tests pinning down each rule |
| `fetch_gw.py` | pulls finished gameweeks, PL fixtures and club data from the FPL API |
| `validate.py` | our points vs FPL's, for every player in every saved gameweek |
| `import_squads.py` | draft results spreadsheet → `data/squads.json` |
| `build_fixtures.py` | the 14-team H2H fixture list, with a round-robin check |
| `score_league.py` | per-gameweek team scores, hindsight XI or submitted XI |
| `h2h.py` | the head-to-head table, and a check against the league's own scores |
| `lineups.py` | weekly XI submission: validation, deadlines, rollover, autosubs |
| `mechanics.py` | trades-with-points, the points bank, waivers, manager boosts |
| `simulate.py` | runs a scenario of those mechanics against real data |
| `boost_scale.py` | what the boost is worth at each league position, and why |
| `data/` | fetched stats, PL fixtures, squads, league fixtures, lineups |

## Running it

```bash
python3 shadow/test_scoring.py       # unit tests — no network needed
python3 shadow/test_lineups.py
python3 shadow/test_mechanics.py
python3 shadow/validate.py           # engine vs FPL across saved gameweeks
python3 shadow/validate.py 3 -v      # one gameweek, listing every mismatch
python3 shadow/h2h.py --compare      # the table, vs the league's own scores
python3 shadow/lineups.py --check    # validate every submitted lineup
python3 shadow/score_league.py --submitted   # score the XIs actually picked
python3 shadow/simulate.py           # trades, bank and boosts on real squads
```

Scripts import each other by module name, so run them from inside `shadow/`
or with it on the path.

`fetch_gw.py` needs the FPL API, which the dev sandbox can't reach — it runs in
the **Shadow league — fetch gameweeks** workflow (Mondays and Tuesdays, or on
demand). That workflow fetches, validates, and commits the data.

## Stat corrections

FPL revises stats for a day or two after matches — assists get reassigned,
bonus is finalised. Gameweeks are marked `data_checked` once FPL considers them
settled; until then the fetcher re-pulls them on every run. This is the single
most underrated source of "why did my score change?", and it's the reason the
schedule runs on both Monday *and* Tuesday.

## Scoring rules

Mirrors official FPL for 2025/26 onwards, including defensive contributions
(DEF: 2pts at 10+ CBIT; MID/FWD: 2pts at 12+ including recoveries). Bonus
points are taken as awarded rather than recomputed from BPS.

The one rule we don't derive is bonus itself — reproducing the BPS ranking
would add a lot of surface area for no benefit, since FPL publishes the result.

## Lineups

Two ways to score a team, and the difference between them matters.

**Hindsight** (the default) picks the best legal XI from each squad after the
fact. It tests the engine without anyone having to set a team, and it flatters
everyone equally — so the table is comparative, not a replay.

**Submitted** (`--submitted`) scores the eleven a manager actually named, with
automatic substitutions for anyone who didn't play. Lineups live in
`data/lineups.json`, keyed by gameweek then team. Miss a deadline and last
week's team rolls over; a rolled-over lineup naming someone since traded away
has the slot filled from the bench.

**Substitutions only happen once the round is over.** They are a settlement,
not a running total: mid-round, a starter with a Monday fixture has zero
minutes and is indistinguishable from one who didn't play, so applying the
rule early benches him and hands his shirt to whoever kicked off first — then
unwinds it when he does play. `apply_autosubs(..., settled=False)` makes no
substitutions at all and leaves the eleven as picked; `round_is_over(gw)`
reads FPL's `finished` or `data_checked` to decide. Every caller that has a
gameweek to hand passes it.

`lineups.py --template N --suggest` prints a pre-filled submission for
gameweek N. The suggestion is built from prior gameweeks' points only — or, in
gameweek 1 where no football has been played, from draft price — so it never
uses information a manager wouldn't have had before the deadline.

## Mechanics that FPL doesn't have

Prototyped in `mechanics.py` and demonstrated by `simulate.py`:

- **Trades with points.** A manager can sweeten a swap with points, deducted
  from their score that gameweek and credited to the other manager's bank.
  Position counts must balance so both squads stay a legal 2/5/5/3. A single
  gameweek can go below zero — that's the gamble — but a season total can't:
  you can't offer more than you've scored, counting what you've already
  promised away. So no points change hands in gameweek 1.

  A straight player-for-player swap is between the two managers and takes
  effect immediately. A trade carrying **points** is published to the league
  first and can be voted down — enough objections and it never happens. There
  is also a season cap on what any manager may receive in trade points, which
  is what stops a friendly pair drip-feeding ten points a week. Both the cap
  and the veto threshold are league settings rather than engine constants, so
  they belong in the admin panel.
- **The points bank.** Spendable in any later gameweek, declared beforehand,
  in whole or in part. It doesn't fund trade offers and doesn't raise your
  offer cap — trade points are always mortgaged against your score.
- **Waivers.** Drop one player, add an unowned one of the same position.
  Unlimited, but claims run in one batch before the gameweek and priority
  snakes from the bottom of the table upwards: last place claims first in
  round one, first place leads round two, and so on. Each manager submits
  claims in their own priority order and attempts one per round. Losing a race
  costs you the round: your next choice waits for the snake to come back to
  you rather than coming off the rank immediately, which is what stops the
  bottom club sweeping the free-agent list in a single pass.
- **Manager boosts.** Eight a season, at most one per gameweek, declared
  before kick-off. When you spend them is yours — all eight in the opening
  weeks, spread out, or held back for the run-in. That timing is a small
  lever rather than a big one (backing them against your strongest opponents
  beats spreading them by about a quarter of a league point over a season),
  but it's a nuance worth having. Size is stepped in five bands by the
  drafted manager's club position going into that gameweek — top four 10%,
  then 20/30/40%, relegation places 50% — and the
  club's real result decides the payout: a win pays in full, a draw half, a
  defeat nothing. No fixture means no payout and the use isn't consumed. Sack
  risk is part of the draft: if your manager loses their job, the remaining
  boosts go with them — no replacement, no re-draft.

The manager is drafted at the auction and kept all season, so the band isn't
a weekly choice — it's the hand you were dealt, and the only decision is when
to spend your uses. That makes the scale a fairness question, and
`boost_scale.py` answers it: what each band is worth once you account for how
often a club at that position actually wins.

`boost_scale.py` also prices the number of uses, which is what decides
whether a manager is worth bidding on at all. That's measured against 304
real head-to-head matches from a completed season, kept as bare score pairs
in `data/season_results.json` — margins are bunched much tighter than a model
predicts, and that's what decides whether a boost ever changes a result.

Three uses would have been worth 1.3 league points across a season and about
74p of a £50 budget: bench-player territory, and nobody would bid. Eight is
worth a full win, which is why that's the allowance.

The short version on the scale is that the very bottom is a genuine risk
rather than a free win. A 17th-place manager is the best hand at about 38 points a season; a 20th
-place one is worth 27, because they blank more than half the time. A top-four
manager is worth 14 but pays out almost every time. That spread is what the
auction has to price.

Three of these rules depend on the season having a history: the offer cap
needs your accumulated points, waiver priority needs a table, and the boost
needs league positions. In gameweek 1 none of those exist, so no points can be
traded, waiver priority falls back to alphabetical, and every boost prices at
mid-table. The simulation says so out loud rather than inventing a table. The
unit tests are where the multi-gameweek behaviour is pinned down.

## Not yet built

- **Submission UI.** Lineups and waiver claims are hand-edited JSON. A form
  belongs with the draft app, not here.
- **Sacking feed.** The rule is implemented, but a scenario has to state the
  gameweek a manager was sacked in by hand — the FPL API doesn't carry it.
