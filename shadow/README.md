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
| `fetch_gw.py` | pulls finished gameweeks from the FPL API |
| `validate.py` | our points vs FPL's, for every player in every saved gameweek |
| `data/` | fetched per-gameweek stats + a player id → position/name map |

## Running it

```bash
python3 shadow/test_scoring.py     # unit tests — no network needed
python3 shadow/validate.py         # engine vs FPL across saved gameweeks
python3 shadow/validate.py 3 -v    # one gameweek, listing every mismatch
```

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

## Not yet built

- **Squads.** The engine scores players; mapping them to the league's 16 teams
  needs the draft results exported from the auction app.
- **Lineups.** Real FPL Draft asks managers to pick an XI. For validation the
  plan is to score a best-valid-XI automatically, which tests the engine
  without needing anyone to set a team each week — and to be explicit that
  this is *not* the same as what a manager would actually have scored.
- **H2H / standings**, once squads and lineups exist.
