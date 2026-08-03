# Players feed — data source for the draft auction app

The Railway auction app (**https://omtffl.up.railway.app/**, source in
`raelmiller/exec-search` at `fpl-draft/`) needs the current Premier League
player list — id, name, position, club — to run the auction. It used to pull
the full FPL `bootstrap-static` feed live at every boot, which is unreliable in
the off-season (the official feed lags the new season's squads and prices), so
a hand-downloaded static file kept having to be dropped in by hand.

This folder replaces that with **one stable, auto-refreshed URL**.

## What it is

| File | What it is |
|---|---|
| `players.json` | the feed the app fetches — a small subset of FPL `bootstrap-static` (`teams` + `elements`, ~60 KB) |
| `build_players_feed.py` | builds `players.json` from the live FPL API or a committed snapshot |
| `../.github/workflows/publish-players-feed.yml` | rebuilds and commits `players.json` daily |

## The URL the app uses

```
https://raw.githubusercontent.com/raelmiller/omtffl/main/players-feed/players.json
```

The auction app fetches this first and keeps the live FPL API only as a
fallback (see `fpl-draft/server/index.js`). The URL is overridable on Railway
via the `PLAYERS_FEED_URL` environment variable — handy for pointing at a
branch copy before this is merged to `main`.

> **Before merge:** `schedule` triggers only run from the default branch, so
> the daily refresh starts once this is on `main`. Until then the feed lives at
> the branch URL (`.../omtffl/<branch>/players-feed/players.json`) — set
> `PLAYERS_FEED_URL` to that on Railway to test, or just run the workflow
> manually (`workflow_dispatch`).

## How it stays fresh

`publish-players-feed.yml` runs daily (05:30 UTC) and on demand. In GitHub
Actions — which, unlike the local sandbox, can reach `fantasy.premierleague.com`
— it pulls the live bootstrap, slims it, and commits `players.json` if it
changed.

**Safety valve:** if the live source returns fewer than 300 players (an empty
or transitional off-season feed), the existing `players.json` is kept rather
than overwritten with a thin list. So a good feed never gets clobbered by a bad
API day; once the real season data goes live, the daily refresh takes over on
its own.

## Manual rebuilds

```bash
# from the live FPL API (needs egress to fantasy.premierleague.com)
python players-feed/build_players_feed.py

# from a committed snapshot — off-season override, or offline seeding
python players-feed/build_players_feed.py path/to/bootstrap_or_fpl_data.json
```

The builder accepts either an FPL `bootstrap-static` export
(`{"teams":…,"elements":…}`) or the advisor's `fpl_data.json`
(`{"teams":…,"players":…}`) — the current `players.json` was seeded from the
latter. To force a build from a committed snapshot in CI, pass its repo path as
the `bootstrap_path` input when running the workflow manually.
