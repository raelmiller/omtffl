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

import os
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import engine, fetcher

HERE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(HERE / "templates"))

app = FastAPI(title="Matchweek", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

scheduler = BackgroundScheduler(timezone="UTC")


@app.on_event("startup")
def startup():
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


def _context(request):
    return {
        "request": request,
        "mode": fetcher.mode(),
        "season": engine.season(),
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
