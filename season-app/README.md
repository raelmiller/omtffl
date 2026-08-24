# Matchweek — season app

Phase one: a read-only league table, served straight from the rules engine.

There is no sign-in and nothing to submit yet. The point of this phase is to
prove the deploy, the data pipeline and the scoring job all work *before*
anything depends on them.

## What it does

| Route | |
|---|---|
| `/` | the head-to-head table and every round's results |
| `/gameweek/N` | one round in detail |
| `/health` | what data is on disk, how settled it is, and whether this host can reach the FPL API |
| `POST /admin/refresh` | pull new gameweek data by hand |

## The one architectural rule

`app/engine.py` is the only file that talks to `shadow/`, and nothing in
`season-app/` computes a point, decides a formation or resolves a fixture. The
web layer loads data, calls the engine, and renders the answer.

If a scoring rule changes it changes in `shadow/`, and this app doesn't move.

## Live or archive

Every FPL fetch so far has run inside GitHub Actions, because the development
sandbox has no route to `fantasy.premierleague.com`. Whether the host has one
is the open question this phase answers, so the app probes it at boot and
reports the result on `/health`:

- **live** — the host can reach the API, so data refreshes on a schedule
  (Monday and Tuesday mornings, matching the Actions workflow).
- **archive** — it can't, so the committed files stand and Actions keeps them
  current. The header says `archive data` and nothing breaks.

Refreshes are non-destructive: a failed fetch leaves the last good data alone.
A table that goes blank because an upstream API had a bad minute is worse than
one that is a few hours stale.

## Running it

```bash
pip install -r season-app/requirements.txt
cd season-app
DISABLE_SCHEDULER=1 python3 -m uvicorn app.main:app --reload --port 8801
python3 test_app.py          # smoke tests
```

`SHADOW_DIR` overrides where the engine and its data are found; it defaults to
`../shadow` and is set to `/srv/shadow` in the container.

## Deploying

The Dockerfile builds **from the repository root**, not from this directory,
because it copies `shadow/` in alongside the app — one image, one source of
truth for scoring. `railway.json` points at it and health-checks `/health`.

## What phase one deliberately leaves out

Sign-in, the Declare page, transactions and live scoring. Scores currently come
from submitted lineups where they exist and the best available XI where they
don't, and the page says which — a hindsight XI flatters everyone equally, so
it is comparative rather than a replay.
