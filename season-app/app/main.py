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
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import auth, db, engine, fetcher

HERE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(HERE / "templates"))

app = FastAPI(title="Matchweek", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

scheduler = BackgroundScheduler(timezone="UTC")


@app.on_event("startup")
def startup():
    db.init()
    _log_admin_links()
    # Probe once at boot so /health can answer the egress question straight
    # away rather than waiting for the first scheduled run.
    fetcher.probe()

    if os.environ.get("DISABLE_SCHEDULER"):
        return
    # Monday and Tuesday mornings, matching the Actions workflow: after the
    # weekend's matches, and again once FPL has settled bonus and corrections.
    scheduler.add_job(fetcher.refresh, CronTrigger(day_of_week="mon,tue", hour=7,
                                                   minute=45),
                      id="refresh", replace_existing=True)
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
            print(f"[matchweek] Admin sign-in for {manager['team']} ({key}): "
                  f"{base}/m/{manager['token']}")
        else:
            print(f"[matchweek] ADMIN_KEYS names {key}, which is not a manager "
                  f"in this league. Known: "
                  f"{', '.join(m['key'] for m in db.managers())}")


def _context(request):
    me = auth.current(request)
    # Real submissions live in the database; the committed file is only a
    # placeholder, so once anyone has picked a team the database wins.
    stored = db.all_lineups()
    return {
        "request": request,
        "mode": fetcher.mode(),
        "me": me,
        "season": engine.season(stored if stored else None),
    }


@app.get("/", response_class=HTMLResponse)
def table(request: Request):
    ctx = _context(request)
    return templates.TemplateResponse("table.html", ctx)


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
def manual_refresh():
    """Kick a refresh by hand. Unauthenticated, and safe because it is:

    it can only pull public FPL data into the container's own copy, and a
    failure leaves the previous data untouched.
    """
    ok = fetcher.refresh()
    return JSONResponse({
        "ok": ok,
        "mode": fetcher.mode(),
        "detail": fetcher.STATUS["last_refresh_detail"],
    })


# ── Signing in ─────────────────────────────────────────────────────────────
@app.get("/m/{token}")
def sign_in(token: str):
    """A manager's personal link. Sets the cookie and gets out of the way."""
    manager = db.manager_by_token(token)
    if not manager:
        # Deliberately vague: a wrong token shouldn't confirm which part was
        # wrong, and a rotated link should read as expired rather than broken.
        return RedirectResponse("/?bad_link=1", status_code=303)
    return auth.sign_in(RedirectResponse("/declare", status_code=303), token)


@app.get("/signout")
def signout():
    return auth.sign_out(RedirectResponse("/", status_code=303))


# ── Declaring a team ───────────────────────────────────────────────────────
def _declare_context(request, gameweek=None):
    ctx = _context(request)
    me = ctx["me"]
    if not me:
        return ctx, None

    rounds = engine.calendar()
    target = (next((g for g in rounds if g["gameweek"] == gameweek), None)
              if gameweek else engine.current_gameweek())
    if target is None:
        return ctx, None

    squad = engine.squad_for(me["key"])
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

    squad = engine.squad_for(me["key"])
    entry = {"xi": ids(xi), "bench": ids(bench)}
    errors, warnings = engine.validate_lineup(entry, squad)
    if errors:
        return JSONResponse({"ok": False, "errors": errors}, status_code=422)

    db.save_lineup(me["key"], gameweek, entry["xi"], entry["bench"])
    return JSONResponse({"ok": True, "warnings": warnings,
                         "saved_at": db.now()})


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
    db.rotate_token(key)
    return RedirectResponse("/admin", status_code=303)
