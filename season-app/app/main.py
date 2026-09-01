"""Matchweek — phase one.

A read-only league table, served straight from the rules engine. No sign-in,
nothing to submit: the point of this phase is to prove the deploy, the data
pipeline and the scoring job all work before anything depends on them.

Routes
------
  /            the head-to-head table and recent results
  /gameweek/N  one round in detail
  /health      what data is on disk, how settled it is, and whether this host
               can reach the FPL API
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import (HTMLResponse, JSONResponse, RedirectResponse,
                               Response)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import auth, db, engine, fetcher, live, notify, push

HERE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(HERE / "templates"))
STARTED = datetime.now(timezone.utc).isoformat(timespec="seconds")


def asset_version():
    """A stamp that changes whenever the stylesheet does.

    Without it a browser keeps the CSS it already has while happily taking the
    new HTML, which renders new markup against old rules — points sitting
    unstyled at the top-left of a shirt instead of centred in it, and every
    card's layout pushed out with them. Deploys are the moment that breaks, so
    the file's own modification time is the stamp.
    """
    try:
        return str(int((HERE / "static" / "style.css").stat().st_mtime))
    except OSError:
        return "0"

app = FastAPI(title="Matchweek", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

scheduler = BackgroundScheduler(timezone="UTC")

# Every morning. Results settle over Monday and Tuesday, but injury news and
# prices move all week — and a manager picking on Friday afternoon is reading
# whatever the last fetch brought in. Kept early enough that a fetch has been
# and gone before anyone is awake to look at it.
REFRESH_SCHEDULE = CronTrigger(hour=7, minute=45)

# And every few minutes while the football is actually on. The daily job is
# right for injury news and prices, which move slowly; it is useless for a
# table on a Saturday afternoon, which is exactly when anyone is looking.
LIVE_REFRESH_MINUTES = 5


def refresh_if_live():
    """Pull fresh gameweek data, but only while matches are being played.

    Guarded rather than simply scheduled often, so this costs nothing at four
    in the morning: `matches_in_progress` reads the fixture list already on
    disk, so deciding not to fetch needs no fetch. The guard is the same one
    the match-event notifications use — one answer to "is there football on",
    not two that can disagree.

    `fetch_gw.should_fetch` decides what actually gets pulled, and it will
    re-fetch a round in progress and never re-fetch one FPL has marked final.
    So this cannot churn settled results however often it runs.
    """
    if not engine.matches_in_progress():
        return {"refreshed": False, "why": "no match in progress"}
    ok = fetcher.refresh()
    return {"refreshed": bool(ok), "why": fetcher.STATUS.get("last_refresh_detail")}


# And every half hour between the last whistle and FPL finalising the round.
# Slower than the live cadence, because nothing is happening minute to minute;
# far faster than the daily job, because a round that settles on Sunday night
# should not read "provisional" until Monday morning.
SETTLE_REFRESH_MINUTES = 30


def refresh_if_settling():
    """Go back for a round FPL has finished but not yet checked.

    The live cadence stops at the last whistle, which is right — there are no
    more goals to collect. But the numbers are not final there: bonus is still
    ours, computed from live BPS, and FPL's own data check can move a point.
    Until that check lands the round shows as provisional, and without this it
    stayed that way until the next morning's refresh.

    `engine.awaiting_final_data` reads the files already on disk, so deciding
    not to fetch costs nothing, and it stops asking once a round is checked or
    too old to be waiting on — so this is silent every day but Sunday night.
    """
    gameweek = engine.awaiting_final_data()
    if gameweek is None:
        return {"refreshed": False, "why": "nothing waiting to be finalised"}
    ok = fetcher.refresh()
    return {"refreshed": bool(ok), "gameweek": gameweek,
            "why": fetcher.STATUS.get("last_refresh_detail")}


# Often enough that a manager who opens the app after a trade settles finds a
# team already put right, rather than one being put right in front of them.
MEND_MINUTES = 10


def mend_lineup(key, gameweek, squad, saved):
    """Bring one saved team back in line with the squad, and store it.

    A lineup is a list of ids and the squad under it moves. Reading it back is
    `engine.reconcile`'s job; this is the decision to make that reading the
    real one. A team that only looks right on the page is still an ineligible
    team in the database — ten on the pitch, six on the bench — and leaving
    that lying around waiting for someone to notice is the quirk, not the fix.

    Refuses one case: if the bench had nothing legal to bring on, the mended
    side is still short, and storing a team that cannot play would be worse
    than leaving the manager's own. The page asks them to fill it by hand.

    The mark goes on the row rather than being worked out on each load: the
    sweep normally mends a team before its manager next opens the app, so a
    notice derived from "did we change anything just now" would be one they
    never saw. It clears when they save a team of their own.

    Returns (saved, filled, stored) — the team to show, how many places were
    filled from the bench, and whether the database was written.
    """
    picked, bench, filled = engine.reconcile(saved, squad)
    fresh = {"xi": [p["id"] for p in picked], "bench": [p["id"] for p in bench]}
    # Whatever the row already carries stands until the manager saves: the
    # mend that earned the notice may have happened on an earlier sweep.
    standing = saved.get("mended", 0)
    unchanged = (fresh["xi"] == list(saved.get("xi") or [])
                 and fresh["bench"] == list(saved.get("bench") or []))
    if unchanged or len(fresh["xi"]) != engine.XI_SIZE:
        return fresh, standing, False
    db.save_lineup(key, gameweek, fresh["xi"], fresh["bench"],
                   mended=max(filled, standing))
    return fresh, max(filled, standing), True


def mend_lineups():
    """Put right every team the squad has moved out from under.

    Done on a sweep rather than only when someone opens the page, because the
    manager who never opens it is exactly the one who would be left with an
    ineligible team. Only while the round is open: a trade window shuts before
    the gameweek deadline, so there is nothing to mend after it, and rewriting
    a locked team would be changing a submission after the fact.
    """
    target = engine.current_gameweek()
    if target is None or not engine.deadline_state(target).get("open"):
        return {"mended": [], "why": "no round open for picking"}
    n = target["gameweek"]
    squads = engine.market(n, db.trades(), db.transactions())["squads"]
    mended = []
    for manager in db.managers():
        key = manager["key"]
        saved, squad = db.get_lineup(key, n), squads.get(key)
        if not saved or not squad:
            continue
        _, filled, stored = mend_lineup(key, n, squad, saved)
        if stored:
            mended.append({"manager": key, "filled": filled})
    if mended:
        print(f"[matchweek] mended {len(mended)} team(s) for GW{n}: "
              + ", ".join(m["manager"] for m in mended))
    return {"mended": mended, "gameweek": n}


@app.on_event("startup")
def startup():
    db.init()
    _log_admin_links()
    # Probe once at boot so /health can answer the egress question straight
    # away rather than waiting for the first scheduled run.
    fetcher.probe()

    if os.environ.get("DISABLE_SCHEDULER"):
        return
    scheduler.add_job(fetcher.refresh, REFRESH_SCHEDULE,
                      id="refresh", replace_existing=True)
    # Deadline reminders. Every ten minutes, because a warning is only useful
    # while there is still time to act on it and a warning that is an hour
    # late is worse than none. Sending is idempotent — each notice is claimed
    # in the database before it goes — so running often costs nothing.
    scheduler.add_job(notify.deadlines_due, "interval", minutes=10,
                      id="notices", replace_existing=True)
    # Match events, while matches are on. The job itself checks whether
    # anything has kicked off before asking FPL for anything, so this is a
    # cheap no-op at four in the morning and a real poll on a Saturday.
    scheduler.add_job(notify.match_events, "interval", minutes=1,
                      id="events", replace_existing=True)
    # The table and the fixtures, while the games are on. max_instances=1 so a
    # slow fetch is skipped rather than stacked — two copies of fetch_gw.py
    # writing the same file is the one way this could make things worse — and
    # coalesce so a container that was asleep runs it once on waking, not once
    # for every interval it missed.
    scheduler.add_job(refresh_if_live, "interval",
                      minutes=LIVE_REFRESH_MINUTES, id="live-refresh",
                      replace_existing=True, max_instances=1, coalesce=True)
    # And the round's final numbers, once the football is over. Same guards,
    # for the same reasons: one at a time, and coalesced so a container that
    # was asleep runs it once on waking.
    scheduler.add_job(refresh_if_settling, "interval",
                      minutes=SETTLE_REFRESH_MINUTES, id="settle-refresh",
                      replace_existing=True, max_instances=1, coalesce=True)
    # Teams a settled trade has moved the squad out from under. Touches only
    # the database, so it costs nothing and never waits on FPL — and it has to
    # be a sweep, because the manager who never opens the app is the one who
    # would otherwise be left with a team that cannot play.
    scheduler.add_job(mend_lineups, "interval", minutes=MEND_MINUTES,
                      id="mend", replace_existing=True,
                      max_instances=1, coalesce=True)
    # A daily job only fires if the process happens to be alive at that minute,
    # and this one isn't: the container is replaced on every deploy and recycled
    # besides. Restart at 08:00 and the next refresh was a whole day away, which
    # is how the app ends up serving last week's injury news.
    #
    # So catch up on boot when the data is older than a refresh cycle. Twenty
    # seconds out, and on the scheduler's own thread, so it never holds up the
    # port opening — a slow FPL response must not look like a failed deploy.
    if engine.stale():
        scheduler.add_job(
            fetcher.refresh, "date", id="catchup", replace_existing=True,
            run_date=datetime.now(timezone.utc) + timedelta(seconds=20))
        print("[matchweek] data is "
              f"{engine.data_age()['hours_ago']}h old — catching up on boot")
    scheduler.start()


@app.on_event("shutdown")
def shutdown():
    if scheduler.running:
        scheduler.shutdown(wait=False)


def _log_admin_links():
    """Print the admin's own sign-in link to the deploy log.

    Every sign-in link lives on /admin, and /admin needs a signed-in admin —
    so on a fresh database there is no way in at all. The deploy log is
    already privileged, which makes it the right place to hand over the first
    key. Only admins are printed, and only their own link.
    """
    keys = auth.admin_keys()
    if not keys:
        print("[matchweek] No ADMIN_KEYS set — /admin is unreachable. "
              "Set it to a manager's initials, e.g. ADMIN_KEYS=RM")
        return
    domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN")
    base = f"https://{domain}" if domain else "<your app url>"
    for key in sorted(keys):
        manager = db.manager_by_key(key)
        if manager:
            # Links are single use and expire, so the one on file has usually
            # been spent by now. Issue a fresh one: this log line is the only
            # way into /admin on a database nobody has a session for.
            token = db.rotate_token(key, days=db.LINK_DAYS)
            print(f"[matchweek] Admin sign-in for {manager['team']} ({key}), "
                  f"good once within {db.LINK_DAYS} days: {base}/m/{token}")
        else:
            print(f"[matchweek] ADMIN_KEYS names {key}, which is not a manager "
                  f"in this league. Known: "
                  f"{', '.join(m['key'] for m in db.managers())}")


def _context(request):
    me = auth.current(request)
    # Real submissions live in the database; the committed file is only a
    # placeholder, so once anyone has picked a team the database wins.
    stored = db.all_lineups()
    # What managers call their teams now, rather than what the draft file
    # called them. Read once and handed to everything that shows a name.
    names = db.team_names()
    return {
        "request": request,
        "mode": fetcher.mode(),
        "asset_version": asset_version(),
        "me": me,
        "names": names,
        "season": engine.season(
            stored if stored else None,
            transactions=db.transactions() + engine.effective_trades(db.trades()),
            drafted=db.manager_clubs(),
            names=names),
    }


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    """The week for a manager, the table for a visitor.

    A signed-in manager opening the app wants their own score, which is why
    it is also the first tab. A signed-out one has no rail and no team, so
    the league table is the only thing the root can usefully be — and it
    stays public, which it has always been.

    Rendered rather than redirected: a redirect would bounce the wordmark and
    every bookmark through a second request, and leave `/` matching no tab.
    """
    if auth.current(request):
        return templates.TemplateResponse("team.html", _team_context(
            request, auth.current(request)["key"]))
    return table(request)


@app.get("/table", response_class=HTMLResponse)
def table(request: Request):
    ctx = _context(request)
    season = ctx["season"]
    ctx["fixtures"] = engine.fixture_list(season.get("rounds") or [],
                                          names=ctx["names"])
    now = engine.current_gameweek()
    ctx["now_gw"] = now["gameweek"] if now else None
    # Open on the last round that was scored — that's the news. Before a ball
    # is kicked there isn't one, so open on the round being picked for.
    played = [r["gameweek"] for r in ctx["fixtures"] if r["played"]]
    ctx["show_gw"] = played[-1] if played else ctx["now_gw"]
    return templates.TemplateResponse("table.html", ctx)


@app.get("/stats", response_class=HTMLResponse)
def stats(request: Request):
    ctx = _context(request)
    if not ctx["season"].get("ready"):
        raise HTTPException(404, "no season data yet")
    ctx["stats"] = engine.analytics(ctx["season"])
    return templates.TemplateResponse("stats.html", ctx)


@app.get("/gameweek/{number}", response_class=HTMLResponse)
def gameweek(request: Request, number: int):
    ctx = _context(request)
    season = ctx["season"]
    if not season.get("ready"):
        raise HTTPException(404, "no season data yet")
    match = next((r for r in season["rounds"] if r["gameweek"] == number), None)
    if match is None:
        raise HTTPException(404, f"gameweek {number} has not been scored")
    ctx["round"] = match
    return templates.TemplateResponse("gameweek.html", ctx)


def build():
    """Which code is actually running.

    `/health` has always reported how fresh the *data* is and never how fresh
    the *app* is, which makes "I can't see the change you just made" an
    unanswerable question: nobody can tell a bug from a container still
    serving last week's image. Railway injects the commit it built from, so
    the running app can simply say.

    `built` is the fallback that needs nothing injected: the Dockerfile copies
    `app/` in as its last layer, so that directory's mtime is when this image
    was built. It survives running outside Railway, where the git variables
    are absent.
    """
    sha = os.environ.get("RAILWAY_GIT_COMMIT_SHA") or None
    try:
        built = datetime.fromtimestamp(
            HERE.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")
    except OSError:
        built = None
    return {
        "commit": sha[:7] if sha else None,
        "branch": os.environ.get("RAILWAY_GIT_BRANCH") or None,
        "message": os.environ.get("RAILWAY_GIT_COMMIT_MESSAGE") or None,
        "deployment": os.environ.get("RAILWAY_DEPLOYMENT_ID") or None,
        "built": built,
        "started": STARTED,
    }


def _next_refresh():
    """When the next automatic refresh is due, or why there isn't one.

    Worth reporting plainly: a scheduler that never starts leaves `last`
    permanently null, which looks the same as a deploy that happened five
    minutes ago.
    """
    if os.environ.get("DISABLE_SCHEDULER"):
        return {"running": False, "why": "DISABLE_SCHEDULER is set"}
    if not scheduler.running:
        return {"running": False, "why": "the scheduler is not running"}
    job = scheduler.get_job("refresh")
    nxt = getattr(job, "next_run_time", None) if job else None
    live = scheduler.get_job("live-refresh")
    live_next = getattr(live, "next_run_time", None) if live else None
    playing = engine.matches_in_progress()
    settle = scheduler.get_job("settle-refresh")
    settle_next = getattr(settle, "next_run_time", None) if settle else None
    settling = engine.awaiting_final_data()
    return {
        "running": True,
        "next": nxt.isoformat(timespec="seconds") if nxt else None,
        "cron": str(REFRESH_SCHEDULE),
        # Which cadence is actually in force. "The table isn't moving" has a
        # different answer depending on whether the app thinks football is on,
        # and that is not otherwise visible from outside.
        "live": {
            "every_minutes": LIVE_REFRESH_MINUTES,
            "matches_in_progress": playing,
            "next": live_next.isoformat(timespec="seconds") if live_next else None,
            "why": None if playing else "no match in progress — daily only",
        },
        # The third cadence: between the last whistle and FPL's data check.
        # Worth reporting for the same reason as the others — "the scores are
        # still provisional" needs to be answerable without guessing.
        "settling": {
            "every_minutes": SETTLE_REFRESH_MINUTES,
            "gameweek": settling,
            "next": (settle_next.isoformat(timespec="seconds")
                     if settle_next else None),
            "why": (f"gameweek {settling} is played but not yet final"
                    if settling else "nothing waiting to be finalised"),
        },
    }


# ── Live scores ────────────────────────────────────────────────────────────
def _live_context(request, gameweek=None):
    ctx = _context(request)
    rounds = engine.calendar()
    target = (next((g for g in rounds if g["gameweek"] == gameweek), None)
              if gameweek else engine.live_gameweek())
    if target is None:
        return ctx, None
    n = target["gameweek"]
    me = ctx["me"]
    # Ownership is the whole point of showing this here rather than on Sky:
    # every name carries whose team it is in, and the reader's own stand out.
    squads = engine.market(n, db.trades(), db.transactions())["squads"]
    ctx.update({
        "gw": target,
        "board": live.board(n, squads=squads, me=me["key"] if me else None),
        "played": sorted(g["gameweek"] for g in rounds
                         if g.get("deadline") and g["gameweek"] <= n),
        "next_gw": next((g["gameweek"] for g in sorted(
            rounds, key=lambda g: g["gameweek"]) if g["gameweek"] > n), None),
        "prev_gw": n - 1 if n > 1 else None,
        # Matched to the cache in live.py: polling faster than the cache only
        # re-renders the same answer, and slower leaves it stale on screen.
        "refresh_seconds": live.TTL_SECONDS,
    })
    return ctx, target


@app.get("/live", response_class=HTMLResponse)
@app.get("/live/{gameweek}", response_class=HTMLResponse)
def live_scores(request: Request, gameweek: int = None):
    ctx, target = _live_context(request, gameweek)
    if target is None:
        raise HTTPException(404, "no fixture list yet")
    return templates.TemplateResponse("live.html", ctx)


@app.get("/live/{gameweek}/board", response_class=HTMLResponse)
def live_board(request: Request, gameweek: int):
    """Just the matches, for the page to swap in while it is left open.

    A fragment rather than JSON so there is one template and no chance of the
    polled version drifting from the rendered one.
    """
    ctx, target = _live_context(request, gameweek)
    if target is None:
        raise HTTPException(404, "no fixture list yet")
    return templates.TemplateResponse("_board.html", ctx)


# ── Installing it ──────────────────────────────────────────────────────────
# The colours here are the light palette's --bg and the chrome aubergine, and
# they are duplicated from style.css on purpose: a manifest cannot read CSS
# custom properties, and the splash screen is painted before any stylesheet
# loads. If the palette moves, move these with it.
MANIFEST = {
    "id": "/",
    "name": "OMTFFL Matchweek",
    "short_name": "OMTFFL",
    "description": "The OMTFFL league table, team sheet and transfer market.",
    "start_url": "/",
    "scope": "/",
    "display": "standalone",
    "background_color": "#F3F5EF",
    "theme_color": "#171122",
    "icons": [
        {"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png",
         "purpose": "any"},
        {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png",
         "purpose": "any"},
        {"src": "/static/icon-maskable-512.png", "sizes": "512x512",
         "type": "image/png", "purpose": "maskable"},
    ],
}


@app.get("/manifest.webmanifest")
def manifest():
    """What a phone needs to install this to a home screen.

    Served as a route rather than a static file so the media type is certainly
    right — some servers guess `.webmanifest` wrong, and a manifest served as
    plain text is silently ignored, which looks exactly like not having one.
    """
    return JSONResponse(MANIFEST, media_type="application/manifest+json")


@app.get("/sw.js")
def service_worker():
    """The service worker, served from the root so its scope is the whole app.

    At `/static/sw.js` it could only control `/static/*`, which is not enough
    for Chrome to treat the app as installable.

    It deliberately caches **nothing**. Every page here is a live answer — the
    table, the pitch, who owns whom — and a cached shell would show a manager
    yesterday's squad with no sign it was stale. Chrome wants a fetch handler
    before it offers to install; it does not require that handler to do
    anything, so this one passes everything straight through. If offline
    support is ever wanted it should start from the data, not from the HTML.
    """
    return Response(
        "self.addEventListener('install', () => self.skipWaiting());\n"
        "self.addEventListener('activate', e => e.waitUntil(clients.claim()));\n"
        "// Network only, on purpose — see the docstring in main.py.\n"
        "self.addEventListener('fetch', () => {});\n"
        "\n"
        "// A notification arrived. showNotification is not optional: the\n"
        "// subscription was made with userVisibleOnly, and a push that shows\n"
        "// nothing has the browser revoke permission after a few offences.\n"
        "self.addEventListener('push', event => {\n"
        "  let m = {};\n"
        "  try { m = event.data ? event.data.json() : {}; } catch (e) { m = {}; }\n"
        "  event.waitUntil(self.registration.showNotification(\n"
        "    m.title || 'OMTFFL', {\n"
        "      body: m.body || '',\n"
        "      icon: '/static/icon-192.png',\n"
        "      badge: '/static/icon-192.png',\n"
        "      // Same tag replaces rather than stacks, so a reminder sent\n"
        "      // twice is one notification, not a pile of them.\n"
        "      tag: m.tag || 'omtffl',\n"
        "      data: { url: m.url || '/' },\n"
        "    }));\n"
        "});\n"
        "\n"
        "// Tapping it should land on the page it is about, and reuse a window\n"
        "// that is already open rather than starting a second copy of the app.\n"
        "self.addEventListener('notificationclick', event => {\n"
        "  event.notification.close();\n"
        "  const url = (event.notification.data && event.notification.data.url) || '/';\n"
        "  event.waitUntil(clients.matchAll({ type: 'window',\n"
        "      includeUncontrolled: true }).then(list => {\n"
        "    for (const c of list) {\n"
        "      if ('focus' in c) { c.navigate(url); return c.focus(); }\n"
        "    }\n"
        "    return clients.openWindow(url);\n"
        "  }));\n"
        "});\n",
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})


@app.get("/push/key")
def push_key():
    """The public half a browser needs to subscribe. Safe to hand to anyone."""
    return JSONResponse({"key": push.public_key(),
                         "configured": push.configured()})


@app.post("/push/subscribe")
async def push_subscribe(request: Request):
    me = auth.current(request)
    if not me:
        raise HTTPException(401, "sign in first")
    body = await request.json()
    keys = body.get("keys") or {}
    if not body.get("endpoint") or not keys.get("p256dh") or not keys.get("auth"):
        raise HTTPException(422, "not a push subscription")
    db.subscribe_push(me["key"], body["endpoint"], keys["p256dh"], keys["auth"])
    # Preferences ride along when the page sends them, so turning goal alerts
    # on is one request rather than a subscribe followed by an update that
    # might not arrive.
    db.set_push_prefs(body["endpoint"],
                      want_deadlines=body.get("want_deadlines"),
                      want_events=body.get("want_events"))
    return JSONResponse({"ok": True})


@app.post("/push/prefs")
async def push_prefs(request: Request):
    me = auth.current(request)
    if not me:
        raise HTTPException(401, "sign in first")
    body = await request.json()
    endpoint = body.get("endpoint", "")
    # Only your own devices: an endpoint is unguessable, but a stolen one
    # should not let anyone else change what it receives.
    mine = {s["endpoint"] for s in db.push_subscriptions(me["key"])}
    if endpoint not in mine:
        raise HTTPException(404, "not one of your apps")
    db.set_push_prefs(endpoint, want_deadlines=body.get("want_deadlines"),
                      want_events=body.get("want_events"))
    return JSONResponse({"ok": True})


@app.post("/push/unsubscribe")
async def push_unsubscribe(request: Request):
    body = await request.json()
    # No sign-in check on purpose: this only ever deletes, and the endpoint is
    # the browser's own. A manager clearing notifications from a device they
    # have since been signed out of should still succeed.
    return JSONResponse({"ok": True,
                         "removed": db.unsubscribe_push(body.get("endpoint", ""))})


@app.post("/account/push-test")
def push_test(request: Request):
    """Send yourself one, and report exactly what the push service said.

    Nothing in this app has ever reached a real push service — it was written
    somewhere that cannot — so this is the first proof that any of it works,
    and it is worth showing the raw status rather than "sent".
    """
    me = auth.current(request)
    if not me:
        raise HTTPException(401, "sign in first")
    subscriptions = db.push_subscriptions(me["key"])
    if not subscriptions:
        return JSONResponse({"ok": False,
                             "detail": "no apps are subscribed for you yet"},
                            status_code=409)
    results = []
    for sub in subscriptions:
        status, detail = push.send(sub, {
            "title": "OMTFFL", "body": "Notifications are working.",
            "url": "/", "tag": "test"})
        if status in (404, 410):
            db.unsubscribe_push(sub["endpoint"])
        else:
            db.mark_push(sub["endpoint"], 200 <= status < 300,
                         None if 200 <= status < 300 else f"{status} {detail}")
        results.append({"status": status, "detail": detail,
                        "service": urlparse(sub["endpoint"]).netloc})
    return JSONResponse({"ok": any(200 <= r["status"] < 300 for r in results),
                         "results": results})


@app.get("/health")


@app.get("/health")
def health():
    """Deployment truth: what's on disk, and whether we can refresh it.

    Returns 200 whenever the app can serve a table. Being unable to reach the
    FPL API is reported, not treated as a failure — the committed data is a
    perfectly good fallback and the Actions workflow keeps it current.
    """
    season = engine.season()
    body = {
        "ok": bool(season.get("ready")),
        "build": build(),
        "mode": fetcher.mode(),
        "fpl_api": {
            "reachable": fetcher.STATUS["reachable"],
            "probed_at": fetcher.STATUS["probed_at"],
            "detail": fetcher.STATUS.get("probe_detail"),
        },
        "refresh": {
            "last": fetcher.STATUS["last_refresh"],
            "ok": fetcher.STATUS["last_refresh_ok"],
            "detail": fetcher.STATUS["last_refresh_detail"],
            "scheduled": _next_refresh(),
        },
        "data": engine.freshness(),
        "push": {**push.status(),
                 "subscriptions": len(db.push_subscriptions())},
        "database": db.stats(),
        "admin": {
            "configured": bool(auth.admin_keys()),
            "found_as": auth.admin_source(),
            "managers": sorted(auth.admin_keys()),
            "note": ("set ADMIN_KEYS to a manager's initials to reach /admin"
                     if not auth.admin_keys() else None),
        },
        "scored": {
            "gameweeks": season.get("played", 0),
            "scheduled": season.get("scheduled", 0),
        },
    }
    if not body["ok"]:
        body["reason"] = season.get("reason")
    return JSONResponse(body, status_code=200 if body["ok"] else 503)


@app.post("/admin/refresh")
def manual_refresh(request: Request):
    """Kick a refresh by hand, from the button on the admin page.

    What it does is harmless — pull public FPL data into this container's own
    copy, leaving the previous data alone if it fails. What it costs is a
    minute of outbound fetching, which is not something a passer-by should be
    able to start, so it asks who you are like everything else that acts.
    """
    me = auth.current(request)
    if not me or not me["is_admin"]:
        raise HTTPException(404)
    ok = fetcher.refresh(reprobe=True)
    return JSONResponse({
        "ok": ok,
        "mode": fetcher.mode(),
        "detail": fetcher.STATUS["last_refresh_detail"],
    })


# ── Signing in ─────────────────────────────────────────────────────────────
@app.get("/m/{token}")
def sign_in(token: str):
    """A manager's personal link. Spends it, starts a session, gets out of
    the way."""
    manager, secret = db.spend_token(token)
    if not manager:
        # Deliberately vague: a wrong token shouldn't confirm which part was
        # wrong, and a spent or expired link should read the same as a typo.
        return RedirectResponse("/?bad_link=1", status_code=303)
    return auth.sign_in(RedirectResponse("/declare", status_code=303), secret)


@app.get("/signout")
def signout(request: Request):
    return auth.sign_out(RedirectResponse("/", status_code=303), request)


@app.post("/declare/name")
def rename(request: Request, team: str = Form("")):
    """Rename your own team. Nobody else's, and nobody vets the name."""
    me = auth.current(request)
    if not me:
        raise HTTPException(401, "sign in first")
    name, problem = db.rename_team(me["key"], team)
    if problem:
        return JSONResponse({"ok": False, "errors": [problem]}, status_code=422)
    return JSONResponse({"ok": True, "team": name})


@app.get("/account", response_class=HTMLResponse)
def account(request: Request):
    """Where a manager can see and revoke their own sessions.

    The point of the page is that losing a link is no longer a reason to ask
    the admin for anything: a manager who is signed in anywhere can mint a
    fresh one for another device themselves.
    """
    ctx = _context(request)
    me = ctx["me"]
    if not me:
        return templates.TemplateResponse("signin.html", ctx, status_code=401)
    ctx["sessions"] = db.sessions_for(me["key"], request.cookies.get(auth.COOKIE))
    ctx["link_minutes"] = db.LINK_MINUTES
    ctx["session_days"] = db.SESSION_DAYS
    ctx["pair_minutes"] = db.PAIR_MINUTES
    return templates.TemplateResponse("account.html", ctx)


@app.post("/account/link")
def new_device_link(request: Request):
    """Mint a single-use link for another of your own devices.

    Minutes, not days: you are about to open it. Issuing it also spends
    whatever link was outstanding, so there is only ever one live at a time.
    """
    me = auth.current(request)
    if not me:
        raise HTTPException(401, "sign in first")
    token = db.rotate_token(me["key"], minutes=db.LINK_MINUTES)
    return JSONResponse({"ok": True,
                         "link": f"{str(request.base_url).rstrip('/')}/m/{token}",
                         "minutes": db.LINK_MINUTES})


@app.post("/account/pair")
def pair_code(request: Request):
    """Mint a short code for signing the installed app in.

    A link cannot do this job. Installed to a home screen the app has its own
    cookie jar and no address bar, so a link tapped in a messaging app signs
    that app's browser in and leaves the installed one with nowhere to paste
    anything. A code is read off one screen and typed into the other.
    """
    me = auth.current(request)
    if not me:
        raise HTTPException(401, "sign in first")
    code, expires = db.pair_code(me["key"])
    return JSONResponse({"ok": True, "code": code, "expires": expires,
                         "minutes": db.PAIR_MINUTES})


@app.post("/pair")
def use_pair_code(request: Request, code: str = Form("")):
    """Spend a pairing code and sign this app in.

    Worth being clear about the power of this: it is exactly a sign-in link,
    with the same reach and the same single use. It is not a second factor
    and does not pretend to be — it is the same front door, shaped so it fits
    through a doorway a link cannot.
    """
    key = db.spend_pair_code(code)
    if not key:
        ctx = _context(request)
        ctx["pair_error"] = ("That code has expired or isn't right. Codes last "
                             f"{db.PAIR_MINUTES} minutes and work once — make "
                             "a fresh one and try again.")
        return templates.TemplateResponse("signin.html", ctx, status_code=400)
    return auth.sign_in(RedirectResponse("/", status_code=303),
                        db.start_session(key))


@app.post("/account/signout-all")
def signout_everywhere(request: Request):
    me = auth.current(request)
    if not me:
        raise HTTPException(401, "sign in first")
    db.end_all_sessions(me["key"])
    return auth.sign_out(RedirectResponse("/", status_code=303), request)


# ── Declaring a team ───────────────────────────────────────────────────────
def _declare_context(request, gameweek=None):
    ctx = _context(request)
    ctx["name_max"] = db.TEAM_NAME_MAX
    me = ctx["me"]
    if not me:
        return ctx, None

    rounds = engine.calendar()
    target = (next((g for g in rounds if g["gameweek"] == gameweek), None)
              if gameweek else engine.current_gameweek())
    if target is None:
        return ctx, None

    # The fifteen a manager actually owns right now, not the fifteen they were
    # dealt on draft night: a trade they accepted, a claim the run landed and
    # anyone they picked up in the free period all have to be on the pitch, or
    # they cannot be picked.
    mkt = engine.market(target["gameweek"], db.trades(), db.transactions(),
                        for_manager=me["key"])
    squad = mkt["squads"].get(me["key"]) or engine.squad_for(me["key"])
    # Who each of them plays this round, so the decision can be made on the
    # pitch rather than in another tab.
    squad = engine.with_fixtures(squad, target["gameweek"])
    # If the engine threw one of this manager's transactions out, this page is
    # where they notice — a player they were told they had is not on the
    # pitch. Say why here rather than leaving them to hunt for it.
    ctx["refused"] = [p for p in mkt["problems"] if f" {me['key']}→" in p
                      or f"→{me['key']}:" in p or f" by {me['key']}:" in p]
    saved = db.get_lineup(me["key"], target["gameweek"])
    rolled = None
    mended = 0
    if saved:
        # A saved lineup is a list of ids, and the squad under it moves. A
        # trade that settles after you pick takes two of your eleven and hands
        # you two more, and reading the entry back raw puts nine on the pitch
        # and seven on the bench — a team that cannot be saved, and that no
        # swap can repair, because every swap keeps the counts.
        #
        # Put right and stored, not just displayed: a team that only looks
        # right on the page is still an ineligible team in the database. The
        # sweep normally gets there first, so this is usually a no-op — but a
        # manager opening the app a minute after a trade settles should find
        # it already done rather than watch it happen.
        if engine.deadline_state(target).get("open"):
            saved, mended, _ = mend_lineup(
                me["key"], target["gameweek"], squad, saved)
        else:
            picked, bench, mended = engine.reconcile(saved, squad)
            saved = {"xi": [p["id"] for p in picked],
                     "bench": [p["id"] for p in bench]}
    else:
        # Nothing submitted for this round, so show what would actually play:
        # the most recent team, which is what rollover will use. The
        # placeholder file counts here, since it is what the engine would fall
        # back to as well.
        merged = engine.merge_lineups(db.all_lineups())
        picked, bench, how = engine.effective_lineup(
            me["key"], target["gameweek"], merged, squad)
        if picked:
            saved = {"xi": [p["id"] for p in picked],
                     "bench": [p["id"] for p in bench]}
            rolled = how
        else:
            # Nobody has ever picked for this team. An empty pitch is a poor
            # welcome, so open on a legal side built from what was knowable
            # before the deadline — never from hindsight.
            xi, bench = engine.suggest_for(me["key"], target["gameweek"], squad)
            saved = {"xi": [p["id"] for p in xi], "bench": [p["id"] for p in bench]}
            rolled = "a suggested eleven — change anything you like"

    used = sum(1 for d in db.declarations("boost", me["key"])
               if d["gameweek"] != target["gameweek"])
    all_tx = db.transactions() + engine.effective_trades(db.trades())
    ctx["bank"] = engine.bank_status(
        me["key"], target["gameweek"], all_tx, db.manager_clubs())
    ctx["boost"] = engine.boost_status(
        me["key"], target["gameweek"], db.manager_clubs(), used,
        declared=bool(db.declaration(me["key"], target["gameweek"], "boost")))

    # Advice, computed only while there is still time to act on it.
    ctx["advice"] = (engine.suggestions(
        me["key"], target["gameweek"], db.all_lineups(),
        db.transactions() + engine.effective_trades(db.trades()))
        if engine.deadline_state(target).get("open") else {"swaps": [], "rounds": 0})
    ctx.update({
        "squad_json": json.dumps(squad),
        "saved_json": json.dumps(saved or {"xi": [], "bench": []}),
        "gw": target,
        "lock": engine.deadline_state(target),
        "squad": squad,
        "saved": saved,
        "rolled_from": rolled,
        "mended": mended,
        "calendar": [g for g in rounds if g["gameweek"] <= target["gameweek"] + 3],
    })
    return ctx, target


@app.get("/declare", response_class=HTMLResponse)
@app.get("/declare/{gameweek}", response_class=HTMLResponse)
def declare(request: Request, gameweek: int = None):
    ctx, target = _declare_context(request, gameweek)
    if not ctx["me"]:
        return templates.TemplateResponse("signin.html", ctx, status_code=401)
    if target is None:
        raise HTTPException(404, "no such gameweek")
    return templates.TemplateResponse("declare.html", ctx)


@app.post("/declare/{gameweek}")
def save_declaration(request: Request, gameweek: int,
                     xi: str = Form(""), bench: str = Form("")):
    """Save an XI. The deadline and the rules are both enforced here.

    Client-side validation is a convenience; this is the check that counts,
    and it calls the same engine the scoring does so the two can't disagree.
    """
    me = auth.current(request)
    if not me:
        raise HTTPException(401, "sign in first")

    target = next((g for g in engine.calendar() if g["gameweek"] == gameweek), None)
    if target is None:
        raise HTTPException(404, "no such gameweek")

    lock = engine.deadline_state(target)
    if not lock["open"]:
        return JSONResponse({"ok": False, "errors": [
            "The deadline for this gameweek has passed."]}, status_code=409)

    def ids(raw):
        return [int(x) for x in raw.split(",") if x.strip().isdigit()]

    # Validated against the live fifteen for the same reason the page draws
    # it: an XI is only legal if every man in it is actually yours today.
    squad = engine.market(gameweek, db.trades(), db.transactions()
                          )["squads"].get(me["key"]) or engine.squad_for(me["key"])
    entry = {"xi": ids(xi), "bench": ids(bench)}
    errors, warnings = engine.validate_lineup(entry, squad)
    if errors:
        return JSONResponse({"ok": False, "errors": errors}, status_code=422)

    db.save_lineup(me["key"], gameweek, entry["xi"], entry["bench"])
    return JSONResponse({"ok": True, "warnings": warnings,
                         "saved_at": db.now()})


@app.post("/declare/{gameweek}/boost")
def declare_boost(request: Request, gameweek: int, on: str = Form("")):
    """Play or withdraw the manager boost for a gameweek.

    Withdrawable right up to the deadline: a boost costs a use, and a manager
    who changes their mind before kick-off has not used anything.
    """
    me = auth.current(request)
    if not me:
        raise HTTPException(401, "sign in first")

    target = next((g for g in engine.calendar() if g["gameweek"] == gameweek), None)
    if target is None:
        raise HTTPException(404, "no such gameweek")
    if not engine.deadline_state(target)["open"]:
        return JSONResponse({"ok": False, "errors": [
            "The deadline for this gameweek has passed."]}, status_code=409)

    if on != "1":
        db.withdraw(me["key"], gameweek, "boost")
        return JSONResponse({"ok": True, "declared": False})

    used = sum(1 for d in db.declarations("boost", me["key"])
               if d["gameweek"] != gameweek)
    status = engine.boost_status(me["key"], gameweek, db.manager_clubs(), used)
    if not status["available"]:
        return JSONResponse({"ok": False, "errors": [status["why"]]},
                            status_code=409)

    db.declare(me["key"], gameweek, "boost")
    return JSONResponse({"ok": True, "declared": True,
                         "club": status["club"], "pct": status["pct"]})


@app.post("/declare/{gameweek}/bank")
def declare_bank(request: Request, gameweek: int, points: int = Form(0)):
    """Spend banked points on a gameweek, or change how many.

    Declared before kick-off like everything else, and adjustable right up to
    the deadline — the points are only committed when the round starts.
    """
    me = auth.current(request)
    if not me:
        raise HTTPException(401, "sign in first")
    target = next((g for g in engine.calendar() if g["gameweek"] == gameweek), None)
    if target is None:
        raise HTTPException(404, "no such gameweek")
    if not engine.deadline_state(target)["open"]:
        return JSONResponse({"ok": False, "errors": [
            "The deadline for this gameweek has passed."]}, status_code=409)

    if points <= 0:
        db.withdraw(me["key"], gameweek, "bank_use")
        return JSONResponse({"ok": True, "spending": 0})

    all_tx = db.transactions() + engine.effective_trades(db.trades())
    status = engine.bank_status(me["key"], gameweek, all_tx, db.manager_clubs())
    if points > status["balance"]:
        return JSONResponse({"ok": False, "errors": [
            f"You have {status['balance']} banked, so you can't spend {points}."]},
            status_code=422)

    db.declare(me["key"], gameweek, "bank_use", {"points": points})
    return JSONResponse({"ok": True, "spending": points,
                         "left": status["balance"] - points})


# ── Trades ─────────────────────────────────────────────────────────────────
def _trade_context(request):
    ctx = _context(request)
    me = ctx["me"]
    if not me:
        return ctx
    # The round trades are FOR, which moves on when the window shuts — not the
    # round a team is being picked for, which runs a day longer. For that last
    # day the page used to offer a round it would refuse a trade for.
    gw = engine.trading_gameweek()
    ctx["picking"] = engine.current_gameweek()
    records = db.trades()
    # market() rather than squads_for_gameweek() because it also hands back
    # what the engine refused to do. Those were being dropped on the floor,
    # which is how a trade could read "done" on a squad that never moved.
    mkt = engine.market(gw["gameweek"], records, db.transactions(),
                        for_manager=me["key"])
    squads, problems = mkt["squads"], mkt["problems"]

    # A trade row stores whatever it was given as JSON, and Jinja prints a
    # missing key as an empty string — so a player without a `name` renders as
    # nothing at all, leaving "X gave" with a blank after it and no hint why.
    # Resolve to a display name here instead, falling back to the id lookup and
    # finally to the raw id, so a name can be wrong but never silently absent.
    lookup = engine.player_names()

    def named(players):
        out = []
        for p in players or []:
            if isinstance(p, dict):
                out.append(p.get("name") or lookup.get(p.get("id"))
                           or f"player {p.get('id', '?')}")
            else:
                out.append(lookup.get(p) or f"player {p}")
        return out

    def decorate(r):
        r = dict(r)
        r["outcome"] = engine.trade_outcome(r)
        r["proposer_team"] = (db.manager_by_key(r["proposer"]) or {}).get("team")
        r["receiver_team"] = (db.manager_by_key(r["receiver"]) or {}).get("team")
        r["i_vetoed"] = me["key"] in r["vetoes"]
        r["out_names"] = named(r["players_out"])
        r["in_names"] = named(r["players_in"])
        # Agreed is not the same as performed. Only ask once it is agreed —
        # a trade still waiting on a reply was never handed to the engine.
        r["problem"] = (engine.trade_problem(r, problems)
                        if r["outcome"]["state"] == "accepted" else None)
        return r

    all_trades = [decorate(r) for r in records]
    ctx.update({
        "gw": gw,
        # Trades close when waivers do, not at kick-off, so the page counts
        # down to that and every gate below reads the same clock.
        "lock": engine.trade_window(gw),
        "gwlock": engine.deadline_state(gw),
        # Where the points actually are. Without this there is no way to tell
        # a trade that has settled from one that only looks like it has.
        "banks": [{"key": k, "team": ctx["names"].get(k, k), "points": v}
                  for k, v in sorted(engine.banks(
                      db.transactions() + engine.effective_trades(records),
                      db.manager_clubs()).items(),
                      key=lambda kv: (-kv[1], kv[0])) if v],
        "incoming": [t for t in all_trades
                     if t["receiver"] == me["key"] and t["status"] == "proposed"],
        "outgoing": [t for t in all_trades
                     if t["proposer"] == me["key"] and t["status"] == "proposed"],
        "open_to_veto": [t for t in all_trades
                         if t["outcome"]["open"]
                         and me["key"] not in (t["proposer"], t["receiver"])],
        # A published trade of your own shows on everyone else's page as
        # something to object to, and used to show on yours nowhere at all.
        "mine_open": [t for t in all_trades
                      if t["outcome"]["open"]
                      and me["key"] in (t["proposer"], t["receiver"])],
        "settled": [t for t in all_trades
                    if t["outcome"]["state"] in ("accepted", "vetoed", "declined")][:12],
        "my_squad": squads.get(me["key"], []),
        "others": [{"key": m["key"], "team": m["team"],
                    "squad": squads.get(m["key"], [])}
                   for m in db.managers() if m["key"] != me["key"]],
        # Whoever they play this round can't be sold points, so the page says
        # which manager that is before anyone fills the form in.
        "facing": engine.opponents(gw["gameweek"]).get(me["key"]),
        "received": sum(t["points"] for t in engine.effective_trades(records)
                        if t["to"] == me["key"]),
        "cap": engine.setting(None, "points_received_cap"),
        "accumulated": engine.accumulated_points(
            me["key"], gw["gameweek"], ctx["season"]),
    })
    return ctx


@app.get("/trade", response_class=HTMLResponse)
def trade_page(request: Request):
    ctx = _trade_context(request)
    if not ctx["me"]:
        return templates.TemplateResponse("signin.html", ctx, status_code=401)
    ctx["squads_json"] = json.dumps({
        "mine": ctx["my_squad"],
        "others": {o["key"]: {"team": o["team"], "squad": o["squad"]}
                   for o in ctx["others"]},
    })
    return templates.TemplateResponse("trade.html", ctx)


@app.post("/trade/propose")
def propose(request: Request, receiver: str = Form(...), give: str = Form(""),
            take: str = Form(""), points: int = Form(0), note: str = Form("")):
    me = auth.current(request)
    if not me:
        raise HTTPException(401, "sign in first")
    # Whatever round the page was offering — the same call, so the form and
    # the route cannot disagree about which round an offer lands in.
    gw = engine.trading_gameweek()
    if not engine.trade_window(gw)["open"]:
        return JSONResponse({"ok": False, "errors": [
            "No round is open for trading. The window shuts a day before "
            "each deadline, so that anyone you take can still be picked."]},
            status_code=409)
    if receiver == me["key"]:
        return JSONResponse({"ok": False, "errors": ["Pick another manager."]},
                            status_code=422)

    records = db.trades()
    squads = engine.squads_for_gameweek(gw["gameweek"], records)
    ids = lambda raw: [int(x) for x in raw.split(",") if x.strip().isdigit()]
    by_id = {p["id"]: p for squad in squads.values() for p in squad}
    out = [by_id[i] for i in ids(give) if i in by_id]
    back = [by_id[i] for i in ids(take) if i in by_id]
    if not out or not back:
        return JSONResponse({"ok": False, "errors": [
            "Choose at least one player on each side."]}, status_code=422)

    season = engine.season(db.all_lineups() or None, db.transactions(),
                           db.manager_clubs())
    record = {"proposer": me["key"], "receiver": receiver,
              "gameweek": gw["gameweek"],
              "players_out": out, "players_in": back, "points": points,
              "vetoes": []}
    problem = engine.check_trade(
        record, squads,
        accumulated=engine.accumulated_points(me["key"], gw["gameweek"], season),
        received=sum(t["points"] for t in engine.effective_trades(records)
                     if t["to"] == receiver))
    if problem:
        return JSONResponse({"ok": False, "errors": [problem]}, status_code=422)

    trade_id = db.propose_trade(gw["gameweek"], me["key"], receiver, out, back,
                                points, note.strip() or None)
    # An offer nobody looks at expires when the window shuts, which is the one
    # failure the receiver cannot do anything about after the fact.
    notify.trade_offered({"id": trade_id, "receiver": receiver,
                          "players_out": out, "players_in": back,
                          "points": points}, me["team"])
    return JSONResponse({"ok": True})


@app.post("/trade/{trade_id}/{action}")
def respond(request: Request, trade_id: int, action: str):
    me = auth.current(request)
    if not me:
        raise HTTPException(401, "sign in first")
    record = db.trade(trade_id)
    if not record:
        raise HTTPException(404, "no such trade")

    # Everything that MOVES a player is gated on the same window. Without
    # this an offer could be accepted after kick-off and the engine would
    # apply it to a round already under way, changing a score after the fact.
    moves_players = action in ("accept", "decline", "withdraw")
    if moves_players:
        target = next((g for g in engine.calendar()
                       if g["gameweek"] == record["gameweek"]), None)
        if not engine.trade_window(target)["open"]:
            raise HTTPException(
                409, "trades for that gameweek have closed — the window shuts "
                     "when waivers do, so anyone you take can still be picked")

    if action == "accept" and record["receiver"] == me["key"]:
        # A straight swap is between the two of them. Points change the league,
        # so the league gets a say before it takes effect.
        db.set_trade_status(trade_id,
                            "published" if record["points"] else "accepted")
    elif action == "decline" and record["receiver"] == me["key"]:
        db.set_trade_status(trade_id, "declined")
    elif action == "withdraw" and record["proposer"] == me["key"]:
        db.set_trade_status(trade_id, "withdrawn")
    elif action in ("veto", "unveto"):
        outcome = engine.trade_outcome(record)
        if not outcome["open"]:
            raise HTTPException(409, "that trade is no longer open to objection")
        if me["key"] in (record["proposer"], record["receiver"]):
            raise HTTPException(403, "you can't object to your own trade")
        (db.veto_trade if action == "veto" else db.unveto_trade)(trade_id, me["key"])
    else:
        raise HTTPException(403, "not yours to do that with")
    return RedirectResponse("/trade", status_code=303)


def _team_context(request, key, gameweek=None):
    """Everything the team page needs, for whichever team and round.

    Factored out so "this week" is the same page rather than a second one
    that looks like it: two routes rendering the same view from two builds of
    the context is how they drift apart, and this one carries the boost, the
    substitutions and the rest of the league's scores.
    """
    ctx = _context(request)
    season = ctx["season"]
    played = [r["gameweek"] for r in season.get("rounds", [])]
    if not played:
        raise HTTPException(404, "no gameweek has been scored yet")
    target = gameweek if gameweek in played else max(played)

    detail = engine.team_gameweek(
        key, target, db.all_lineups(),
        db.transactions() + engine.effective_trades(db.trades()),
        db.manager_clubs(), names=ctx["names"])
    if detail is None:
        raise HTTPException(404, "no such team or gameweek")

    rnd = next(r for r in season["rounds"] if r["gameweek"] == target)
    opponent = None
    for m in rnd["matches"]:
        if m["home_key"] == key:
            opponent = {"key": m["away_key"], "team": m["away"], "total": m["away_score"]}
        elif m["away_key"] == key:
            opponent = {"key": m["home_key"], "team": m["home"], "total": m["home_score"]}

    scores = {}
    for m in rnd["matches"]:
        scores[m["home_key"]] = m["home_score"]
        scores[m["away_key"]] = m["away_score"]
    others = sorted(
        ({"key": k, "team": t, "score": scores.get(k, 0)}
         for k, t in ((r["key"], r["team"]) for r in season["table"])),
        key=lambda x: -x["score"])

    earlier = [g for g in played if g < target]
    later = [g for g in played if g > target]
    ctx.update({
        "detail": detail, "opponent": opponent, "others": others,
        "round_name": rnd["name"],
        "prev_gw": max(earlier) if earlier else None,
        "next_gw": min(later) if later else None,
    })
    return ctx


@app.get("/team/{key}", response_class=HTMLResponse)
@app.get("/team/{key}/{gameweek}", response_class=HTMLResponse)
def team_page(request: Request, key: str, gameweek: int = None):
    """How one team's gameweek went, laid out the way they picked it."""
    return templates.TemplateResponse(
        "team.html", _team_context(request, key, gameweek))


@app.get("/week", response_class=HTMLResponse)
def this_week(request: Request):
    """Your own points for the round being played, in one tap.

    The same view as `/team/<you>`, which was previously reachable only by
    opening the table, finding your own row and clicking it — three steps to
    the one number a manager checks most often during a gameweek.

    No gameweek in the path on purpose: this is always the latest round that
    has been scored, so the tab means the same thing every week and a
    bookmark of it never goes stale.
    """
    me = auth.current(request)
    if not me:
        return templates.TemplateResponse("signin.html", _context(request),
                                          status_code=401)
    return templates.TemplateResponse("team.html", _team_context(request, me["key"]))


@app.get("/api/player/{player_id}")
def player_api(player_id: int):
    """Stats for the player popup. Public — it is all public FPL data."""
    detail = engine.player_detail(player_id, names=db.team_names())
    if detail is None:
        raise HTTPException(404, "no such player")
    return JSONResponse(detail)


# ── Waivers ────────────────────────────────────────────────────────────────
def _claims_from_declarations(gameweek):
    """Everyone's claims for a gameweek, in each manager's priority order."""
    claims = {}
    for row in db.declarations("waiver"):
        if row["gameweek"] != gameweek:
            continue
        payload = json.loads(row["payload"])
        if payload.get("claims"):
            claims[row["manager"]] = payload["claims"]
    return claims


@app.get("/waivers", response_class=HTMLResponse)
def waivers(request: Request):
    ctx = _context(request)
    me = ctx["me"]
    if not me:
        return templates.TemplateResponse("signin.html", ctx, status_code=401)

    gw = engine.current_gameweek()
    trades = db.trades()
    txs = db.transactions()
    now = engine.market(gw["gameweek"], trades, txs, for_manager=me["key"])
    squads = now["squads"]
    claims = _claims_from_declarations(gw["gameweek"])
    # A claim is a blind bid. Nothing resolved is built here while the window
    # is open — not even to be thrown away — because the only reliable way to
    # keep another manager's bid off this page is not to put it in the context
    # in the first place. The order comes from the standings, which are public.
    order = engine.waiver_order(gw["gameweek"], ctx["season"]) or sorted(squads)
    names = {m["key"]: m["team"] for m in db.managers()}

    ctx.update({
        "gw": gw,
        "lock": engine.deadline_state(gw),
        "phase": now["state"],
        "my_squad": squads.get(me["key"], []),
        "my_claims": claims.get(me["key"], []),
        "free_json": json.dumps(engine.free_agent_pool(
            gw["gameweek"], squads=squads, exclude=now["shut_out"])),
        "metrics_json": json.dumps([
            {"key": k, "short": short, "label": label}
            for k, short, _, label in engine.available_metrics()]),
        "squad_json": json.dumps(engine.with_stats(squads.get(me["key"], []))),
        "claims_json": json.dumps(claims.get(me["key"], [])),
        "order": [{"key": k, "team": names.get(k, k)} for k in reversed(order)],
        "my_place": (list(reversed(order)).index(me["key"]) + 1
                     if me["key"] in order else None),
        # What the run actually did, once it has happened, and who it put out
        # of reach — the pool is short by exactly these players and should
        # say so rather than leaving a manager hunting for a name.
        "settled": [{**m, "team": names.get(m["team"], m["team"])}
                    for m in now["moves"]],
        "frozen": [{**f, "team": names.get(f["by"], f["by"])}
                   for f in engine.frozen_detail(now)],
        "my_moves": [m for m in now["moves"]
                     if m["team"] == me["key"] and m.get("kind") == "free_agent"],
    })
    return templates.TemplateResponse("waivers.html", ctx)


@app.post("/waivers/{gameweek}")
def save_claims(request: Request, gameweek: int, claims: str = Form("")):
    """Save a manager's ranked claims. Each is 'dropId:addId'."""
    me = auth.current(request)
    if not me:
        raise HTTPException(401, "sign in first")
    target = next((g for g in engine.calendar() if g["gameweek"] == gameweek), None)
    if target is None:
        raise HTTPException(404, "no such gameweek")
    now = engine.market(gameweek, db.trades(), db.transactions(),
                        for_manager=me["key"])
    if now["state"]["phase"] == "closed":
        return JSONResponse({"ok": False, "errors": [
            "The deadline for this gameweek has passed."]}, status_code=409)
    if not now["state"]["waivers_open"]:
        return JSONResponse({"ok": False, "errors": [
            "The waiver run has already happened. The pool is open to "
            "everyone until the deadline — take someone directly instead."]},
            status_code=409)

    squads = now["squads"]
    mine = {p["id"]: p for p in squads.get(me["key"], [])}
    free = {p["id"]: p for p in engine.free_agents(
        gameweek, squads=squads, exclude=now["shut_out"])}

    parsed, errors = [], []
    for pair in [c for c in claims.split(",") if c.strip()]:
        try:
            drop_id, add_id = (int(x) for x in pair.split(":"))
        except ValueError:
            errors.append(f"couldn't read the claim '{pair}'")
            continue
        if drop_id not in mine:
            errors.append("you can only drop a player you own")
        elif add_id not in free:
            errors.append(f"{free.get(add_id, {}).get('name', 'that player')} "
                          "is already owned")
        elif mine[drop_id]["position"] != free[add_id]["position"]:
            errors.append(f"{free[add_id]['name']} is a "
                          f"{free[add_id]['position']} and "
                          f"{mine[drop_id]['name']} is a "
                          f"{mine[drop_id]['position']} — that breaks the squad")
        else:
            parsed.append({"drop": mine[drop_id], "add": free[add_id]})

    if errors:
        return JSONResponse({"ok": False, "errors": errors}, status_code=422)
    if parsed:
        db.declare(me["key"], gameweek, "waiver", {"claims": parsed})
    else:
        db.withdraw(me["key"], gameweek, "waiver")
    return JSONResponse({"ok": True, "claims": len(parsed)})


@app.post("/waivers/{gameweek}/take")
def take_free_agent(request: Request, gameweek: int,
                    drop: int = Form(...), add: int = Form(...)):
    """Take a free agent, there and then.

    The window between the waiver run and the deadline is first come, first
    served, so this settles immediately rather than joining a queue. Whoever
    posts first gets the player; everyone else is told he has gone.
    """
    me = auth.current(request)
    if not me:
        raise HTTPException(401, "sign in first")
    target = next((g for g in engine.calendar() if g["gameweek"] == gameweek), None)
    if target is None:
        raise HTTPException(404, "no such gameweek")

    now = engine.market(gameweek, db.trades(), db.transactions(),
                        for_manager=me["key"])
    state = now["state"]
    if state["phase"] == "closed":
        return JSONResponse({"ok": False, "errors": [
            "The deadline for this gameweek has passed."]}, status_code=409)
    if not state["free_agency"]:
        return JSONResponse({"ok": False, "errors": [
            "The waiver run hasn't happened yet — put a claim in and it will "
            "be settled by priority."]}, status_code=409)

    mine = {p["id"]: p for p in now["squads"].get(me["key"], [])}
    pool = {p["id"]: p for p in engine.free_agents(
        gameweek, squads=now["squads"], exclude=now["shut_out"])}

    errors = []
    if drop not in mine:
        errors.append("you can only drop a player you own")
    elif add in now["shut_out"]:
        errors.append("that player was dropped this gameweek — nobody else can "
                      "pick him up until the next one")
    elif add not in pool:
        errors.append("somebody has taken him already")
    elif mine[drop]["position"] != pool[add]["position"]:
        errors.append(f"{pool[add]['name']} is a {pool[add]['position']} and "
                      f"{mine[drop]['name']} is a {mine[drop]['position']} — "
                      "that breaks the squad")
    if errors:
        return JSONResponse({"ok": False, "errors": errors}, status_code=422)

    move_id = db.take_free_agent(gameweek, me["key"], mine[drop], pool[add])
    # Two managers can post inside the same second, and both will have read a
    # pool that still had him in it. The engine settles that race by the clock,
    # so the only honest answer is to ask it who won rather than to assume the
    # write succeeding means the claim did.
    landed = any(m.get("kind") == "free_agent" and m["team"] == me["key"]
                 and m["add"]["id"] == add
                 for m in engine.market(gameweek, db.trades(),
                                        db.transactions())["moves"])
    if not landed:
        db.undo_free_agent(move_id)
        return JSONResponse({"ok": False, "errors": [
            "somebody got there first"]}, status_code=409)
    return JSONResponse({"ok": True, "added": pool[add]["name"],
                         "dropped": mine[drop]["name"]})


# ── Admin ──────────────────────────────────────────────────────────────────
@app.get("/admin", response_class=HTMLResponse)
def admin(request: Request):
    """The sign-in links, so they can be handed out."""
    ctx = _context(request)
    me = ctx["me"]
    if not me or not me["is_admin"]:
        raise HTTPException(404)
    ctx["managers"] = db.managers()
    ctx["clubs"] = engine.clubs()
    ctx["drafted"] = db.manager_clubs()
    ctx["base"] = str(request.base_url).rstrip("/")
    ctx["submitted"] = db.all_lineups()
    ctx["current_gw"] = engine.current_gameweek()
    ctx["nowish"] = db.now()
    ctx["signed_in"] = {m["key"]: len(db.sessions_for(m["key"]))
                        for m in ctx["managers"]}
    ctx["data"] = engine.freshness()
    ctx["build"] = build()
    ctx["refresh"] = dict(fetcher.STATUS)
    job = scheduler.get_job("refresh") if scheduler.running else None
    ctx["next_refresh"] = (job.next_run_time.isoformat()
                           if job and job.next_run_time else None)
    return templates.TemplateResponse("admin.html", ctx)


@app.post("/admin/view-as/{key}")
def view_as(request: Request, key: str):
    """Admin only: see the app as another manager, to test both sides."""
    me = auth.real(request)
    if not me or not me["is_admin"]:
        raise HTTPException(404)
    target = "" if key == me["key"] else key
    if target and not db.manager_by_key(target):
        raise HTTPException(404, "no such manager")
    return auth.view_as(RedirectResponse("/declare", status_code=303), target)


@app.get("/stop-viewing")
def stop_viewing(request: Request):
    return auth.view_as(RedirectResponse("/admin", status_code=303), "")


@app.post("/admin/assign-clubs")
def assign_clubs(request: Request):
    """Hand every team a random club's manager, for testing before a draft."""
    me = auth.real(request)
    if not me or not me["is_admin"]:
        raise HTTPException(404)
    clubs = engine.clubs()
    if len(clubs) < len(db.managers()):
        raise HTTPException(409, "not enough clubs to go round")
    db.assign_clubs_randomly(sorted(clubs))
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/club/{key}")
def set_club(request: Request, key: str, club: int = Form(...),
             sacked_from: str = Form("")):
    me = auth.real(request)
    if not me or not me["is_admin"]:
        raise HTTPException(404)
    gw = int(sacked_from) if sacked_from.strip().isdigit() else None
    db.set_manager_club(key, club, gw)
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/rotate/{key}")
def rotate(request: Request, key: str):
    me = auth.current(request)
    if not me or not me["is_admin"]:
        raise HTTPException(404)
    db.rotate_token(key, days=db.LINK_DAYS)
    return RedirectResponse("/admin", status_code=303)
