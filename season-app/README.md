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

## Storage

The database holds what managers declare, and a container's filesystem is
wiped on every redeploy — so it has to live on a volume. **Attaching a volume
in Railway is the only step.** Railway sets `RAILWAY_VOLUME_MOUNT_PATH` itself
when you do, and the app puts the database there. `DB_PATH` overrides it if you
ever want the file somewhere specific.

`/health` reports the truth rather than the intent: it reads the kernel's mount
table, so it can tell "a volume is configured" apart from "a volume is actually
mounted". Check the `verdict` line before handing sign-in links out.

Set `ADMIN_KEYS` to a manager's initials (e.g. `RM`) to reach `/admin`, where
the sign-in links live. Without it `/admin` returns 404 and `/health` says so.
The admin's own link is printed to the deploy log on every start, freshly
issued, because on a database nobody holds a session for that log line is the
only way in.

## Signing in

There is no password. A manager opens an unguessable link, and opening it
**starts a one-hour clock** rather than consuming the link outright. That hour
exists because of how links are really opened: tapped in a messaging app, they
open in that app's own browser, and the manager who then opens the app in
Safari must not find a link that is already dead. An hour is long enough for
that and short enough that a leaked link is exposed for an evening rather than
a season. It runs from the first use, so opening the link again cannot extend
it.

Links expire on their own too — a week for one an admin hands out, fifteen
minutes for one a manager mints for their own second device from `/account`.

The cookie carries a session secret whose hash is all the database keeps, so a
copy of the database is not a set of working logins. Sessions are listed on
`/account` with when they were last used, can be ended one at a time or all at
once, and one nobody has used for ninety days is dropped.

The honest limit: anyone holding an unspent link, or a live cookie, is that
manager. There is no second factor. That is proportionate for fourteen people
who know each other and would not be for anything with money or strangers in
it.

## Deploying

The Dockerfile builds **from the repository root**, not from this directory,
because it copies `shadow/` in alongside the app — one image, one source of
truth for scoring.

That is why `railway.json` lives at the repository root rather than here.
Railway reads its config from the service's root directory, and setting the
service root to `season-app/` would put `shadow/` outside the build context
where `COPY` cannot reach it. **Leave the service root unset.**

A `.dockerignore` keeps the image to what actually runs: the 11MB season
archive and every `__pycache__` stay out, leaving a payload under a megabyte.

In *live* mode the container writes fetched gameweek data into its own
filesystem, which Railway discards on redeploy. That is fine here, because the
Actions workflow commits the same data and the next build picks it up — but it
does mean the container's copy and the repository's can drift by a few hours.
Phase two, which stores things people typed, will need a volume.

## What phase one deliberately leaves out

Sign-in, the Declare page, transactions and live scoring. Scores currently come
from submitted lineups where they exist and the best available XI where they
don't, and the page says which — a hindsight XI flatters everyone equally, so
it is comparative rather than a replay.
