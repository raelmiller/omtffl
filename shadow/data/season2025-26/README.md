# 2025/26 season archive

A complete season of this league, captured from the FPL Draft API and used as
the engine's regression corpus. Everything here is keyed by **manager initials
only** — no names, since this repository is public.

| File | What it is |
|---|---|
| `gw01..gw38.json` | every player's stats for each gameweek, plus per-fixture stats where a club played twice |
| `lineups.json` | the XI each manager actually submitted, every gameweek |
| `fixtures.json` | the head-to-head schedule with real results |
| `pl_fixtures.json` | all 380 Premier League matches with scores |
| `players.json` | player id → position, name, club |

## Why it's here

One gameweek of live data validated the engine against 279 players. This
validates it against **11,362 player-gameweeks across 38 gameweeks** — and
that difference immediately found two real bugs that a single week could not:

- **Double gameweeks.** A gameweek's stats are aggregated across both matches,
  so two 90-minute games looked like one 180-minute game and scored appearance
  points once instead of twice. Clean sheets and goals conceded had the same
  flaw. 105 player-gameweeks were wrong, always in FPL's favour.
- **Bookings on zero minutes.** A player can be shown a card without the clock
  recording a minute. The engine short-circuited to 0 for anyone who didn't
  play, and a source comment confidently asserted they "can't be booked".

With both fixed, the engine reproduces FPL's own totals **exactly, 11,362 out
of 11,362**.

## Note on the season label

The source capture labels itself 2024/25 in its metadata. Its gameweek 1
deadline is 2025-08-15, so it is 2025/26. That matters: 2025/26 is when
defensive contributions entered the scoring rules, which is why this data is
scoreable by the engine as written.
