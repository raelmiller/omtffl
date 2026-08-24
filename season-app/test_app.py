#!/usr/bin/env python3
"""Smoke tests for phase one.

Deliberately shallow. The scoring rules are tested exhaustively in shadow/;
what needs proving here is that the web layer boots, finds the engine, and
degrades honestly when data or the FPL API is missing.

Run: python3 season-app/test_app.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient          # noqa: E402

from app import auth, db, engine, fetcher           # noqa: E402
from app.main import app                           # noqa: E402

FAILS = []


def check(name, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")
    if not ok:
        FAILS.append(name)


def check_true(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}{(' — ' + detail) if detail else ''}")
    if not cond:
        FAILS.append(name)


client = TestClient(app)

print("── Routes ──────────────────────────────────────────────")
r = client.get("/")
check("the table renders", r.status_code, 200)
check_true("and contains the standings", "Pts" in r.text and "Team" in r.text.replace("TEAM", "Team"))

h = client.get("/health")
check("health responds", h.status_code, 200)
body = h.json()
check_true("health reports whether the FPL API is reachable",
           "reachable" in body["fpl_api"], json.dumps(body["fpl_api"]))
check_true("and which mode that puts us in", body["mode"] in ("live", "archive", "unknown"),
           body["mode"])
check_true("and how much data is on disk", body["data"]["gameweeks_on_disk"] >= 0)

first = engine.season()
if first.get("ready") and first["rounds"]:
    n = first["rounds"][0]["gameweek"]
    g = client.get(f"/gameweek/{n}")
    check(f"gameweek {n} renders", g.status_code, 200)
check("an unscored gameweek 404s", client.get("/gameweek/99").status_code, 404)

print("\n── Engine bridge ───────────────────────────────────────")
season = engine.season()
check_true("the season scores", season.get("ready") is True, str(season.get("reason")))
if season.get("ready"):
    check_true("every team is in the table", len(season["table"]) >= 2)
    check_true("ranks run from 1", season["table"][0]["rank"] == 1)
    check_true("the table is sorted by points",
               all(a["Pts"] >= b["Pts"]
                   for a, b in zip(season["table"], season["table"][1:])))
    # Every match hands out three points, or two when it's drawn.
    expected = sum(3 if m["home_score"] != m["away_score"] else 2
                   for r in season["rounds"] for m in r["matches"])
    check("league points awarded match the results",
          sum(r["Pts"] for r in season["table"]), expected)
    # Points for and against must mirror each other exactly.
    check("points for equals points against across the league",
          sum(r["PF"] for r in season["table"]),
          sum(r["PA"] for r in season["table"]))

print("\n── Degrading honestly ──────────────────────────────────")
# A host with no route to the FPL API must still serve, and must say so.
fetcher.STATUS["reachable"] = False
check("no egress means archive mode", fetcher.mode(), "archive")
check("a refresh in archive mode is refused, not attempted", fetcher.refresh(), False)
check_true("and explains itself",
           "cannot reach" in (fetcher.STATUS["last_refresh_detail"] or ""),
           str(fetcher.STATUS["last_refresh_detail"]))
check("the table still renders without a live API", client.get("/").status_code, 200)
check("and health still reports ok", client.get("/health").json()["ok"], True)

print("\n── Signing in ──────────────────────────────────────────")

db.init()
me = [m for m in db.managers() if m["key"] == "RM"][0]

check("a stranger can read the table", client.get("/").status_code, 200)
check("but can't reach the picker", client.get("/declare").status_code, 401)
check("and admin doesn't announce itself", client.get("/admin").status_code, 404)

bad = client.get("/m/not-a-real-token", follow_redirects=False)
check("a dead link redirects rather than erroring", bad.status_code, 303)
check_true("and doesn't say which part was wrong",
           "bad_link" in bad.headers["location"], bad.headers["location"])

signed = TestClient(app)
r = signed.get(f"/m/{me['token']}", follow_redirects=False)
check("a good link signs you in", r.status_code, 303)
check("and lands on the picker", r.headers["location"], "/declare")
check("which now loads", signed.get("/declare").status_code, 200)

print("\n── Picking a team ──────────────────────────────────────")

gw = engine.current_gameweek()
check_true("the round on offer is still open", engine.deadline_state(gw)["open"],
           f"GW{gw['gameweek']} closes {gw['deadline']}")

squad = engine.squad_for("RM")
by = {}
for pl in squad:
    by.setdefault(pl["position"], []).append(pl["id"])
legal = by["GK"][:1] + by["DEF"][:4] + by["MID"][:4] + by["FWD"][:2]
bench = [pl["id"] for pl in squad if pl["id"] not in legal]

def post(gameweek, xi, bench_ids):
    return signed.post(f"/declare/{gameweek}",
                       data={"xi": ",".join(map(str, xi)),
                             "bench": ",".join(map(str, bench_ids))})

ok = post(gw["gameweek"], legal, bench)
check("a legal 4-4-2 saves", ok.status_code, 200)
check("and says so", ok.json()["ok"], True)

stored = db.get_lineup("RM", gw["gameweek"])
check("eleven are stored", len(stored["xi"]), 11)
check("and four on the bench", len(stored["bench"]), 4)

two_keepers = by["GK"][:2] + by["DEF"][:3] + by["MID"][:4] + by["FWD"][:2]
r = post(gw["gameweek"], two_keepers, [])
check("two keepers is refused", r.status_code, 422)
check_true("with a reason a manager can act on",
           "GK" in r.json()["errors"][0], str(r.json()["errors"]))

thin = by["GK"][:1] + by["DEF"][:2] + by["MID"][:5] + by["FWD"][:3]
r = post(gw["gameweek"], thin, [])
check("so is a back three that isn't", r.status_code, 422)

closed = [g for g in engine.calendar()
          if not engine.deadline_state(g)["open"]]
if closed:
    r = post(closed[0]["gameweek"], legal, bench)
    check("a passed deadline refuses the save", r.status_code, 409)
    check_true("and says why", "deadline" in r.json()["errors"][0].lower())
    check_true("leaving the old team alone",
               db.get_lineup("RM", closed[0]["gameweek"]) is None)

print("\n── A save can't rewrite a played round ─────────────────")

# Saving for a future gameweek must not disturb one already scored: the
# placeholder file has to keep standing behind the rounds it was scoring.
before = engine.season()["rounds"][-1]["matches"][0]
after = engine.season(db.all_lineups())["rounds"][-1]["matches"][0]
check("gameweek 1 scores the same either way",
      (after["home_score"], after["away_score"]),
      (before["home_score"], before["away_score"]))
check_true("and is still labelled a placeholder",
           engine.season(db.all_lineups())["seeded_lineups"])

print("\n── Admin ───────────────────────────────────────────────")

check("a normal manager gets no admin page", signed.get("/admin").status_code, 404)
import os
os.environ["ADMIN_KEYS"] = "RM"
check("naming them in the environment grants it",
      signed.get("/admin").status_code, 200)
old = me["token"]
db.rotate_token("RM")
check_true("rotating issues a different link",
           db.manager_by_key("RM")["token"] != old)
check("and the old one stops working",
      TestClient(app).get(f"/m/{old}", follow_redirects=False)
      .headers["location"].endswith("bad_link=1"), True)
os.environ.pop("ADMIN_KEYS", None)

print("\n── Viewing as another manager ──────────────────────────")

import os
os.environ["ADMIN_KEYS"] = "RM"
boss = TestClient(app)
boss.get(f"/m/{db.manager_by_key('RM')['token']}")
other = [m for m in db.managers() if m["key"] != "RM"][0]

r = boss.post(f"/admin/view-as/{other['key']}", follow_redirects=False)
check("an admin can view as another manager", r.status_code, 303)
page = boss.get("/declare")
check_true("their team is the one on screen", other["team"] in page.text, other["team"])
check_true("and a banner says whose it is", "Viewing as" in page.text)

# A borrowed identity must not carry admin rights with it.
check("no admin page while viewing as someone else",
      boss.get("/admin").status_code, 404)

squad = engine.squad_for(other["key"])
by = {}
for pl in squad:
    by.setdefault(pl["position"], []).append(pl["id"])
xi = by["GK"][:1] + by["DEF"][:4] + by["MID"][:4] + by["FWD"][:2]
gwn = engine.current_gameweek()["gameweek"]
saved = boss.post(f"/declare/{gwn}", data={"xi": ",".join(map(str, xi)), "bench": ""})
check("a team saved while viewing lands on their record", saved.status_code, 200)
check_true("stored against them, not the admin",
           db.get_lineup(other["key"], gwn) is not None)

boss.get("/stop-viewing")
check("stopping restores the admin", boss.get("/admin").status_code, 200)
check_true("and their own team is back",
           db.manager_by_key("RM")["team"] in boss.get("/declare").text)

# Nobody else can borrow an identity.
plain = TestClient(app)
plain.get(f"/m/{other['token']}")
check("a normal manager can't view as anyone",
      plain.post(f"/admin/view-as/RM", follow_redirects=False).status_code, 404)
plain.cookies.set("matchweek_as", "RM")
check_true("and setting the cookie by hand achieves nothing",
           db.manager_by_key("RM")["team"] not in plain.get("/declare").text)
os.environ.pop("ADMIN_KEYS", None)

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
    sys.exit(1)
print("ALL APP TESTS PASSED")
