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

`lineups.py --template N --suggest` prints a pre-filled submission for
gameweek N. The suggestion is built from prior gameweeks' points only — or, in
gameweek 1 where no football has been played, from draft price — so it never
uses information a manager wouldn't have had before the deadline.

## Mechanics that FPL doesn't have

Prototyped in `mechanics.py` and demonstrated by `simulate.py`:

- **Trades with points.** A manager can sweeten a swap with points, deducted
  from their score that gameweek and credited to the other manager's bank.
  Position counts must balance so both squads stay a legal 2/5/5/3.
- **The points bank.** Spendable in any later gameweek, declared beforehand,
  in whole or in part, including to fund a later trade.
- **Waivers.** A free transfer: drop one player, add an unowned one of the
  same position.
- **Manager boosts.** Three a season. Size scales with the drafted manager's
  club position going into that gameweek (1st smallest, 20th largest), and the
  club's real result decides the payout: a win pays in full, a draw half, a
  defeat nothing. No fixture means no payout and the use isn't consumed.

The numbers — the 10–50% boost range, three uses, whether trade debt can push
a gameweek score below zero — are all tunables at the top of `mechanics.py`.
Finding out whether they're balanced is the point of running a shadow season.

## Not yet built

- **Submission UI.** Lineups are hand-edited JSON. A form would come with the
  draft app, not here.
- **Waiver priority.** Right now a waiver is first-come; a real league needs an
  order (reverse standings, or a rolling priority list).
