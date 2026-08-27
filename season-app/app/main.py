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

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import auth, db, engine, fetcher, live

HERE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(HERE / "templates"))


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
    return {
        "running": True,
        "next": nxt.isoformat(timespec="seconds") if nxt else None,
        "cron": str(REFRESH_SCHEDULE),
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
    squad = engine.market(target["gameweek"], db.trades(), db.transactions()
                          )["squads"].get(me["key"]) or engine.squad_for(me["key"])
    saved = db.get_lineup(me["key"], target["gameweek"])
    rolled = None
    if not saved:
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

    ctx.update({
        "squad_json": json.dumps(squad),
        "saved_json": json.dumps(saved or {"xi": [], "bench": []}),
        "gw": target,
        "lock": engine.deadline_state(target),
        "squad": squad,
        "saved": saved,
        "rolled_from": rolled,
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
    gw = engine.current_gameweek()
    records = db.trades()
    squads = engine.squads_for_gameweek(gw["gameweek"], records)

    def decorate(r):
        r = dict(r)
        r["outcome"] = engine.trade_outcome(r)
        r["proposer_team"] = (db.manager_by_key(r["proposer"]) or {}).get("team")
        r["receiver_team"] = (db.manager_by_key(r["receiver"]) or {}).get("team")
        r["i_vetoed"] = me["key"] in r["vetoes"]
        return r

    all_trades = [decorate(r) for r in records]
    ctx.update({
        "gw": gw,
        "lock": engine.deadline_state(gw),
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
    gw = engine.current_gameweek()
    if not engine.deadline_state(gw)["open"]:
        return JSONResponse({"ok": False, "errors": [
            "The deadline has passed — propose this for the next gameweek."]},
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

    db.propose_trade(gw["gameweek"], me["key"], receiver, out, back, points,
                     note.strip() or None)
    return JSONResponse({"ok": True})


@app.post("/trade/{trade_id}/{action}")
def respond(request: Request, trade_id: int, action: str):
    me = auth.current(request)
    if not me:
        raise HTTPException(401, "sign in first")
    record = db.trade(trade_id)
    if not record:
        raise HTTPException(404, "no such trade")

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


@app.get("/team/{key}", response_class=HTMLResponse)
@app.get("/team/{key}/{gameweek}", response_class=HTMLResponse)
def team_page(request: Request, key: str, gameweek: int = None):
    """How one team's gameweek went, laid out the way they picked it."""
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
    return templates.TemplateResponse("team.html", ctx)


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
