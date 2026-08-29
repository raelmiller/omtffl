# Matchweek — season app

Phase one: a read-only league table, served straight from the rules engine.

There is no sign-in and nothing to submit yet. The point of this phase is to
prove the deploy, the data pipeline and the scoring job all work *before*
anything depends on them.

## What it does

| Route | |
|---|---|
| `/` | your own points for the round being played, or the table when signed out |
| `/week` | the same page, at a stable path — the first tab |
| `/table` | the head-to-head table and every round's results |
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

- **live** — the host can reach the API, so data refreshes daily, matching the
  Actions workflow.
- **archive** — it can't, so the committed files stand and Actions keeps them
  current. The header says `archive data` and nothing breaks.

Both paths take **every round that has kicked off**, not only finished ones: a
gameweek in progress is fetched with the scores it has so far, and the page
says "in progress" rather than pretending it is settled. A round FPL has marked
`data_checked` is never pulled again, so a settled result cannot move under
anyone. `fetch_gw.should_fetch` is that rule, on its own and tested.

Refreshes are non-destructive: a failed fetch leaves the last good data alone.
A table that goes blank because an upstream API had a bad minute is worse than
one that is a few hours stale.

**During matches it refreshes every few minutes instead.** A daily job is
right for injury news and prices, which move slowly, and useless for a table
on a Saturday afternoon — which is exactly when anyone is looking at it. So a
second job runs every `LIVE_REFRESH_MINUTES` and asks
`engine.matches_in_progress()` first: that reads the fixture list already on
disk, so deciding *not* to fetch costs nothing and the app is silent overnight.
It is the same question the goal notifications ask, answered in one place
rather than two that could disagree.

**It waits on `finished`, not on a clock**, and that distinction was learned
the hard way. The first version stopped 2.5 hours after kick-off, which is
comfortably past the whistle — but FPL sets `finished` some time *later*, and
that flag is what the boost and the table wait on. So the app watched the
goals go in, stopped, and never saw the round settle: a manager's boost read
"still playing" the next morning, hours after the match everyone had watched
end. Now anything kicked off and not yet marked finished keeps the fast
cadence, so the round switches it off itself at the moment there is nothing
left to collect. `SETTLE_WINDOW_HOURS` is only an outer bound, so a fixture
FPL never flags cannot poll for ever.

Running often is safe because the rule about what to pull lives in
`fetch_gw.should_fetch`, not in the caller: a round in progress is re-fetched
every time, and a round FPL has marked `data_checked` is never pulled again.
No cadence can move a settled result. `/health` reports which cadence is in
force and whether the app currently thinks football is on, because "the table
isn't moving" has a different answer depending on that.

**Two things have to be true for a refresh to reach a page**, and each has bitten
once:

1. *Something has to run it.* The in-app job is daily at 07:45 UTC, which only
   fires if the process is alive at that minute — and it often isn't, because
   the container is replaced on every deploy. So the app also catches up on
   boot when the data is older than `STALE_AFTER_HOURS`. `/health` reports
   `refresh.scheduled`, which says whether anything is scheduled at all and
   why not if it isn't.
2. *The cache has to notice.* Scoring is cached on a fingerprint of the data
   files' mtimes, and **any file the fetcher writes that isn't in
   `engine.WATCHED` is a file whose new contents never reach a page.**
   `players.json` was missing from that list, which is the one that matters:
   between rounds it is the only file that changes, so injury news, suspensions
   and club transfers went stale until a restart. `/health` reports
   `data.written`, so "when did this last change" is answerable without
   guessing at the numbers.

**Which code is running is reported too**, on `/health` under `build` and in a
panel at the top of `/admin`. Data freshness was answerable and code freshness
wasn't, which makes "the change isn't on the page" impossible to diagnose from
outside: a bug and a container still serving the previous image look identical.
Railway injects `RAILWAY_GIT_COMMIT_SHA`, so the app reports the commit,
branch and message it was built from; off Railway those are absent and it
falls back to the mtime of `app/`, which the Dockerfile copies in as its last
layer and is therefore the image's build time. Check it before hunting a bug —
a redeploy takes a few minutes.

The container's copy of the data is ephemeral — a redeploy resets it to
whatever is committed — so **the committed files are the floor the app falls
back to, and how often they are refreshed is how stale the app can get.** That
is why the Actions workflow runs daily rather than twice a week: it commits to
the default branch, which redeploys the app with the new data already in it.
Leave that on a Monday schedule and the app reverts to Monday every time
anything redeploys.

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

## Live scores

`/live` shows the real Premier League matches in the round being played, with
goals, assists, red cards and penalties, and **who owns each name** — which
the official game does not tell you. The reader's own players are marked out
and each match says which of theirs is in it.

Three things make it work:

- **It fetches on demand.** Everything else reads from disk once a day, which
  is the wrong shape for live scores, so `live.py` goes to
  `/api/fixtures/?event=N` when someone opens the page and holds the answer for
  `TTL_SECONDS`. Fourteen people refreshing is one request a minute, and the
  page polls a fragment rather than reloading so the explainer boxes stay open.
- **Attribution is FPL's, not ours.** The stats arrive already grouped per
  fixture and per side, so nothing has to work out who played where — which
  cannot be derived from a player's club for anyone who moved in January, and
  cannot be done at all for a double gameweek.
- **Bonus is computed from live BPS** by `scoring.provisional_bonus`, and is
  labelled provisional until FPL settles it, at which point their figure is
  shown instead. The rule is standard competition ranking: players level on
  BPS share the higher place and use up the places below it, so three tied
  second take two each and the single point goes unawarded. It reproduces
  FPL's own awards for all 310 players who featured in gameweek 1, which the
  test suite checks.

**Nothing here feeds the league.** The table is scored from the saved gameweek
files by the same engine as always; this is a window onto the football.

## Picking a team

Every player on the pitch carries his club and, under it, **who he plays this
round and whether it is home or away** — `BHA` / `CHE (a)` — so the decision
is made on the pitch rather than in another tab.

`engine.club_fixtures` keys the round's fixtures by club id and records both
sides of each one, so the home side and the away side can never disagree about
which is which. `with_fixtures` attaches them to a squad, and it resolves the
club through `player_clubs` rather than the squad entry: an entry carries the
club its player was at on draft night, so anyone who moved in January would
otherwise be given their old club's fixture.

It is a **list**, never a single fixture. A club can have two games in a double
gameweek and none in a blank, and both are exactly what a manager needs to see
before picking — a double shows both opponents, a blank says `blank`. Neither
case appears in the current round, so the test suite manufactures them.

**A result is known at the whistle, not when FPL finishes checking it.** The
fixtures endpoint carries two flags hours apart: `finished_provisional` goes
up at full time, `finished` waits until bonus is added and the stats checked —
which on a Friday night match can still be false the next morning. Reading
only `finished` meant a boost sat on "still playing" twelve hours after the
match ended, on data that had been refreshed two minutes earlier. Goals do not
change in between, so `mechanics.decided()` takes either flag, and both the
boost and the Premier League table that sets its band use it. The refresh
cadence still waits for `finished`, because bonus is exactly what it is
collecting. Files written before the field existed fall back to `finished`.

**A round still being played has no result, and the header must not claim
one.** It read "lost to ThunderBijol" beside its own "in progress" pill, for a
gameweek whose matches had not all kicked off. Past tense now needs
`final` or `provisional` — the football being over — and anything earlier says
*ahead of*, *behind* or *level with*.

**A boost on a match still being played pays nothing *yet*, and says so.**
`boost_value` returns `played: False` in two situations that mean opposite
things to a manager: the club had no fixture at all, or the fixture has kicked
off and is not final. Both used to render as "they didn't play, so nothing was
paid" — so someone watching their boosted club win 1–0 was told their boost
had been thrown away. The `pending` flag separates them. Paying nothing until
the match is final is right, since the result can still change; the boost pays
in full, and the use is consumed, the moment FPL marks it settled.

**A boost pays a whole number of points**, rounded before it is added, so no
fraction ever reaches a total. That matters because a fixture is decided by
comparing two scores for equality: a boost that paid 0.2 of a point would turn
a genuine draw into a win, in a table showing whole numbers that could never
show why.

The half goes **away from zero** — 2.5 pays 3 — via `ROUND_HALF_UP` rather
than the built-in `round`, which is banker's rounding and sends a half to the
*even* neighbour: 1.5 pays 2, and so does 2.5. Exact halves are common here
rather than exotic (a 20% band on a draw is a tenth of the XI, and twenty of
them turn up in the first 200 points of XI score), so which way they went was
decided by the parity of the number below — not a rule anyone could hold in
their head, and a bad one to lose a fixture to.

**The root is your week.** A signed-in manager opening the app wants their own
score; a signed-out visitor has no team, so the root serves them the league
table, which it has always done publicly. Rendered rather than redirected — a
redirect would bounce the wordmark and every bookmark through a second request
and leave `/` matching no tab. A bare `/` in a tab's extra paths therefore
means the root *and only* the root: `startswith("/")` is true of every path
there has ever been.

**A pick rolls over until it is changed, and nothing should call that a
failure.** The pages said "No eleven was submitted for this round", which
reads as an omission when it is usually a decision — a squad already set up
the way you want needs no weekly ceremony. Worse, the engine agreed with the
wording: it asked whether *this* round had a database row and, finding none,
labelled the score a **placeholder** — so a manager who picked in gameweek one
and left it alone was told every later score came from a made-up eleven, and
the table counted those slots as "best available". `lineup_source_gameweek`
now answers which round the eleven was actually picked in, and the placeholder
label belongs only to an eleven no human ever chose.

**`/week` is `/team/<you>` without the hunting.**
The one number a manager checks most often during a gameweek was three steps
away: open the table, find your own row, click it. The route resolves *you*
and the latest scored round itself, so the tab means the same thing every week
and a bookmark of it never goes stale. It renders the team page from the same
`_team_context` the team route uses, rather than a second view that looks like
it — two builds of the same page is how they drift apart, and this one carries
the boost, the substitutions and the rest of the league's scores.

A team's week (`/team/RM/3`) now lights that tab; a whole round
(`/gameweek/3`) still belongs to the Table. Eight tabs fit the bottom bar
without clipping or sideways scroll from 320px up, which was measured rather
than hoped.

## Suggested substitutions

The pick page offers up to three swaps under **Worth a look**, each with the
reason it is being offered — *"Madjo — injured · 1.5 a game lately against 0 ·
Robertson has the kinder fixture"*.

Four signals are compared between the bench and the eleven: whether a player
can play at all, recent scoring, the opponent's league position (with home
advantage worth about two places), and the expected-goal numbers per 90 —
involvements for an attacker, goals conceded for a defender, and only ever
like for like.

**They are kept as named signals rather than blended into one projected
score.** "Expected 4.7 points" would have to be invented from coefficients
nobody agreed and would read as authority it has not earned. A manager can
weigh "he's injured" against "the other one has the better fixture"
themselves; what they cannot do quickly is notice both.

Three rules keep it quiet. **"He cannot play" stands alone**; every other
signal needs a second to agree with it, so a form number wobbling by a point
does not fill the page. **Nothing is offered when as much points the other
way.** And **every swap returned is legal** — the eleven it leaves you with
still fits the formation, so nothing is suggested that could not be made.

It also says what it is standing on: early in a season the panel prints how
many rounds have been played, because form over two games is a very short
story and the page should say so rather than imply otherwise.

## The transfer week

A gameweek has two windows, not one.

**Waivers** run until the waiver deadline — by default 24 hours before the
gameweek deadline, set by `waiver_hours_before`. Claims are collected in each
manager's own priority order and nothing moves: until the run they are claims,
not transfers, and the page says so. At the deadline they are resolved in one
pass, snaking from the bottom of the table upwards.

**Claims are blind, and that is a rule rather than a nicety.** If you could see
what the manager ahead of you was going for you would simply pick someone else,
and the whole decision — how to rank bids you might lose — disappears. So while
the window is open the waivers route never builds a resolved run at all, not
even to discard it: another manager's claim cannot leak from a page it was
never put on. Nothing about anyone else's claims is shown, including how many
they have lodged. After the run everybody sees all of it at once.

**Trades close at the same moment**, and for the same reason a manager cares
about: a player you trade for has to be pickable for the round you traded him
for. On the old clock — trades open until kick-off — a points trade is
published for the league to object to and settles when its window shuts, so it
delivered the player at the very instant lineups locked. He could never play
for the round he was traded for. Straight swaps were barely better: agreed at
17:29 for a 17:30 deadline. `engine.trade_window` is now the waiver window,
and every route that moves a player is gated on it, including accepting an
offer — which previously had no clock on it at all and would have applied a
trade to a round already being played.

**A trade carrying points is agreed but not done, and the page has to say so
in those words.** It said "agreed", which a manager reads as finished — so the
squads not having moved looked like a bug rather than the rule working. The
pending block now states plainly that neither squad changes until it settles,
when that is, and how many objections would stop it. The points line says
where the points are going rather than only how many, and **every manager's
bank is listed on the page**: previously a bank was only ever shown to the
manager who owned it, so the one number that proves a points trade landed was
visible to nobody but the recipient. `engine.banks()` computes all fourteen in
one `apply_transactions` pass, and the suite checks it agrees with the
per-manager figure.

The settled history names the players, one line a side — `Quantum of Szobos
gave Steele` / `License to Kelleher gave Kelleher` — rather than only the two
managers, which said a trade happened without ever saying what it was. **The
verb carries the outcome**: a trade that was voted down moved nobody, so its
line says `offered` and its points say `never sent`. A history where a
rejected offer looks identical to a completed deal is worse than one that says
less, so the test suite asserts the word `gave` never appears against a vetoed
trade.

**A rule binds when the deal is struck, not on every page load.** Nothing is
recorded when a trade settles — the engine re-derives every transaction from
the draft each time a page is drawn. That is what keeps one source of truth,
and it meant a rule written on Thursday was applied to a trade agreed on
Tuesday: the squads silently reverted and a manager went looking for a player
the league had told them they owned. The head-to-head rule did exactly this.

So `validate_trade` now splits in two. Above the line are facts that must
still hold — the players owned, the shapes balanced — and objections, which
keep arriving after acceptance and stay live. Below it are conditions on the
*deal*: the offer cap, the received cap, the head-to-head rule. A trade
carrying `agreed` has already passed those, at proposal, through
`check_trade` — the same validator with the same settings — so they are not
re-litigated. `engine.effective_trades` sets that flag, because every trade
the app applies came through `propose`. A caller that has not vetted a trade
leaves it unset and gets the full check, which is what `shadow/` does on its
own. **A new rule governs new trades.**

**"Agreed" and "performed" are two different questions, and the page used to
ask only the first.** `trade_outcome` decides whether a trade stands — its
status, the objections against it, the clock. Whether it can actually be
*carried out* only `apply_transactions` knows: a manager may not own the
player by the time the trade is reached, or the positions may not balance.
`_settle` has always returned those refusals and `squads_for_gameweek` threw
them away, so a trade the engine had rejected printed **done** in the history
while the squads correctly never moved — and the manager went looking for a
player nobody had sent. `engine.trade_problem` matches each refusal back to
its trade; a refused one now reads **not applied** with the engine's own
sentence beside it, and says `offered` rather than `gave`. The pick-team page
carries the same warning, because that is where a manager notices the player
is missing.

**The settled table stops being a table below 40rem.** `table` carries a 32rem
`min-width` and scrolls inside `.scroll`, which on a 390px screen puts Points
and Outcome off the right edge — so the one cell that says whether a trade was
performed, and why not, was invisible on a phone unless you thought to swipe a
table sideways. Stacked, every cell is on screen. The diagnosis being in a
column nobody can see is the same failure as not printing it at all.

**The Settled section is always on the page, empty or not.** It used to live
inside `{% if settled %}`, so a league with nothing settled yet saw no section
at all — which looks exactly like a page that doesn't list trades, and sent
someone hunting for a bug that wasn't there. It now says "nothing has settled
yet" and where a trade goes when it does. The same reasoning as `/health`
reporting the truth rather than the intent: an absent thing and an empty thing
have to look different.

**Trading moves on when the window shuts**, rather than waiting for the round
to finish. The page named `current_gameweek()` — the round a team is being
picked for, which runs a full day longer than the trade window — so for the
last 24 hours of every round it offered a round it would then refuse a trade
for, told the reader to propose for the next one, and gave them no way to do
it. `engine.trading_gameweek()` is the round trades are actually for: this one
while its window is open, otherwise the next one whose window is. The page and
the propose route call it, so the form and the route cannot disagree about
where an offer lands, and the page says plainly when trading has moved ahead
of the round being picked.

**Free agency** runs from then until the gameweek deadline. Whoever is left is
first come, first served, as many moves as a manager likes, still one out for
one in of the same position so a squad stays a legal 2/5/5/3. A take settles
immediately rather than joining a queue, and because two managers can post
inside the same second the route asks the engine who actually won before
telling anyone they got him.

**Anyone dropped once the run has finished is frozen for the rest of that
gameweek** — the run's own drops and free-agency drops alike, because a rule
covering only the run would be bypassed by not using waivers. The freeze is
against everyone *else*: whoever let a player go may take him back, which
costs a move, gains them nothing they did not already have, and means a drop
made in error is recoverable rather than costing a week. Narrow it to the run
alone with `freeze_drops: "waivers"`.

`engine.market()` is the one place any of this is decided. It returns the
phase, every squad as it actually stands, the pool for a given manager and who
is frozen out of it — so no two pages can disagree about who owns whom.

## How it looks

The UI is **Floodlight**, drawn in `design/` and applied here. Two things are
worth knowing before editing `static/style.css`.

**Light is the default and the fallback.** The light palette is defined on a
bare `:root`, so a device that has never expressed a preference gets it. Dark
is reached two ways and two ways only: `prefers-color-scheme: dark` (guarded
by `:root:not([data-theme="light"])` so a manager's explicit "light" wins), and
`:root[data-theme="dark"]` from the toggle in the bar. The toggle stores
`matchweek-theme`; right-click or long-press it to hand control back to the
device. The two dark blocks must define the same tokens as each other and as
the light block — a token light sets and dark forgets keeps its light value
against a dark ground, which is how you get black text on a black panel. The
test suite checks this.

**The accent is two tokens, because one colour cannot do both jobs.** `--fill`
is the lime; it is only ever a background, always carrying `--on-fill` ink, and
it needs no light/dark variant because it reads on paper and on slate alike.
`--accent` is the accent as ink — a deep grass green on paper, the lime again
after dark — and it is what carries text: links, positive numbers, the
countdown. Reaching for `--fill` as a text colour on a light ground will not
read. The lime is also rationed: it marks the one primary action on a page, so
a per-row action is `.btn.ghost`, not a fourteenth highlight.

The chrome — the bar and the tab rail — is aubergine in both themes and has
its own `--chrome-*` tokens, because the crest is a deep purple and the bar is
the surface it sits on rather than a ground that moves under it. Below 40rem
the rail becomes a bottom navigation bar in the thumb's reach.

**Rules go behind an info icon**, via the `info` macro in `_info.html`. They
matter, but once read twice they are paragraphs standing between a manager and
the thing they came for. Hover opens the box on a pointer; a click pins it, so
it survives a tap and can be read on a phone without vanishing as the mouse
travels into it; Escape or a click outside dismisses it. Below 34rem it stops
being a tooltip and becomes a sheet across the bottom, because a 32rem box
anchored to an icon runs off one edge or the other on a 390px screen. The text
is always in the served HTML, and a `noscript` rule prints it inline — folded
away, never withheld.

It carries the waiver and free-agency rules, the five stat definitions, why a
points trade is published and open to objection, and how substitutes come on.
Each box needs an id unique to its page; the test suite checks every icon opens
a box that exists, that no two share an id, and that they all ship shut.

## Installing it to a home screen

It is a PWA, so a manager can add it to their home screen and get an icon that
opens full-screen with no browser chrome. Nothing about how it works changes —
this is the same server-rendered pages in a window without an address bar.

- **The manifest is a route, not a static file.** A manifest served as
  `text/plain` is silently ignored, which is indistinguishable from not having
  one, and `.webmanifest` is exactly the extension servers guess wrong.
- **The Apple tags are not duplicates of it.** iOS reads almost none of the
  manifest: without `apple-mobile-web-app-capable` it opens in Safari with its
  chrome, which is the whole thing being avoided. `apple-touch-icon` must be a
  real 180px square — iOS neither scales nor rounds it.
- **Two icon shapes.** `any` is the crest cropped to its own ink, shown as
  given. `maskable` is the same crest at 70% on white, because Android crops
  an icon to a circle, a squircle or a rounded square depending on the
  launcher and only the central 80% is guaranteed — an `any` icon used as
  maskable gets its ring shaved off. `tools/make_icons.py` generates all of
  them from `tools/crest-1024.png`, so they can be regenerated rather than
  being blobs nobody can change. The Dockerfile copies `app/` only, so the
  source never reaches the image.
- **The service worker caches nothing, deliberately.** Registering one is what
  makes Chrome offer to install; it is not obliged to do anything. Every page
  here is a live answer, and a cached shell would show a manager yesterday's
  squad with no sign it was stale. It is served from `/sw.js` rather than
  `/static/` because a worker's scope is its own directory.
- **`viewport-fit=cover`** is what makes `env(safe-area-inset-*)` report
  anything. The bottom rail already padded itself by that inset; until this it
  always read 0, and installed to a home screen the rail sat under the
  iPhone's home indicator.

The honest limit: on iOS installing is Safari-only, via Share → Add to Home
Screen, which is obscure enough to be worth a sentence of instruction.

## Notifications

Two kinds, chosen per device, because they are completely different
appetites — and a bad Saturday must not be why someone turns off the reminder
that stops them missing a deadline.

**Deadlines and trades**, on by default: someone in your eleven injured or
without a fixture as the deadline nears, the waiver window closing when you
have claims in, and a trade offered to you. Nothing about results, nothing
about other people's transactions, nothing you did yourself.

The deadline one asks the **team**, not the manager. It used to fire for
anyone who had not submitted for the round — which, since a pick rolls over
until it is changed, is most managers most weeks, and a notification that
arrives every week saying nothing has gone wrong is one that gets switched
off. `engine.needs_attention` reports who in the eleven is injured, suspended,
doubtful or has no fixture to play in; a team with none of those is left
alone. It is deliberately not conditioned on submitting: a manager who picked
on Tuesday and had a striker pull up on Thursday is exactly who needs telling,
and a "did you submit" check skips precisely them.

**What your players are doing**, off by default: goals, assists, red cards
and penalties, for the **whole fifteen** rather than the eleven — a bench
forward scoring is news, both because substitutes come on and because owning
him is what makes it interesting. Bonus is deliberately excluded: it moves all
match and settles long after, so it would notify repeatedly and be wrong most
of those times.

**FPL reports cumulative totals, not events.** `goals_scored` says Salah has
two, and says it again on every poll for the rest of the match. Turning that
into "Salah's second" is the whole feature: the count is part of the key each
notice is claimed under, so a goal already reported is never re-sent and a
second goal is a new one. Get that wrong and you have either silence or a
notification every sixty seconds.

**Sends are batched per poll.** Two things in the same minute are one
notification listing both, not two a few seconds apart. Fifteen players on a
busy Saturday is otherwise a phone that will not stop.

The poll checks whether anything has actually kicked off before asking FPL for
anything, so it is a free no-op overnight and a real request only while
matches are on. Two honest limits: FPL updates a minute or two after the
event, so if you are watching, your phone buzzes after you have already
celebrated — it earns its keep when you are *not* watching. And provisional
stats get revised, so a goal reassigned an hour later cannot be un-sent.

**Set `VAPID_PRIVATE_KEY`** to turn it on; `python3 tools/vapid_keys.py`
generates one, and `VAPID_SUBJECT` should be a mailto: some push service can
use to reach you. The key has to stay the same for the life of the league:
every subscription is bound to its public half, so changing it silently
unsubscribes everybody and the only symptom is notifications quietly stopping.
`/health` reports whether push is available, configured, and how many apps are
subscribed.

**The encryption is written here rather than taken from a library.** The
obvious choice, `pywebpush`, pulls in `http-ece`, whose `setup.py` no longer
builds against a current setuptools — a dependency that fails to build is a
deploy that fails at the worst possible moment. `app/push.py` implements
RFC 8291 and RFC 8292 on `cryptography`, which ships wheels, and imports it
defensively so a broken crypto install turns push off rather than taking the
app down.

That is only a defensible trade because the result is checked against
something other than its author's reading of the spec: the same inputs given
to `http_ece`, an independent implementation, produce a **byte-identical**
block, and each decrypts the other's output. The suite also pins the header
layout, that the salt is fresh per message, that the wrong `auth` secret
cannot decrypt, and that the VAPID signature is a raw r‖s pair rather than
DER — a DER signature is accepted by nothing and rejected with a 401 that
explains nothing.

**Sending is idempotent.** A notice is claimed in `notice_sent` before it is
sent, keyed by what makes it unique — "the gameweek 3 deadline warning for
RM" — so the sweep running every ten minutes and running once an hour produce
the same notifications. A manager with no app subscribed is *not* claimed,
because otherwise turning notifications on ten minutes later would silently
consume a warning about a deadline still hours away. A push service answering
404 or 410 means the app is gone, and the subscription is dropped rather than
left to fail on every future send.

**Nothing here has ever spoken to a real push service.** This was written
somewhere that cannot reach `fcm.googleapis.com` or `web.push.apple.com`, so
the first real send is the first proof — which is why `/account` has a **Send
a test** button that reports the push service's raw status rather than
"sent".

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

**A link cannot sign in a second app, and a code can.** Installed to a home
screen the app has its own cookie jar and no address bar: a link tapped in a
messaging app opens in *that* app's browser, signs it in, and leaves the
installed one signed out with nowhere to paste anything. A second browser has
the same problem. So `/account` mints an eight-character code, read off one
screen and typed into the other — the app's front door, shaped to fit through
a doorway a link cannot.

It is worth being plain that this is **exactly as powerful as a sign-in link**
and is not a second factor. What keeps it proportionate: single use, five
minutes, only one live per manager (minting retires the last), and only the
hash is stored. The alphabet is Crockford's base32 — no I, L, O or U — so 32⁸
against a five-minute window is out of reach of guessing without leaning on
rate limiting, and the characters that get misread aren't in it. Input is
normalised the other way too: `O` becomes `0`, lowercase and the displayed
hyphen are forgiven, because the code is being copied by eye.

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
