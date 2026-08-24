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
from app.main import _claims_from_declarations as _claims_for  # noqa: E402
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

print("\n── Drafted managers ────────────────────────────────────")

os.environ["ADMIN_KEYS"] = "RM"
boss2 = TestClient(app)
boss2.get(f"/m/{db.manager_by_key('RM')['token']}")

clubs = engine.clubs()
check_true("the club list came through with the player data", len(clubs) >= 20,
           f"{len(clubs)} clubs")

r = boss2.post("/admin/assign-clubs", follow_redirects=False)
check("an admin can assign clubs at random", r.status_code, 303)
drafted = db.manager_clubs()
check("every team gets one", len(drafted), len(db.managers()))
check_true("and they're all different",
           len({d["club"] for d in drafted.values()}) == len(drafted))
check_true("each is a real club",
           all(d["club"] in clubs for d in drafted.values()))

r = boss2.post("/admin/club/AF", data={"club": "1", "sacked_from": "12"},
               follow_redirects=False)
check("one can be set by hand", r.status_code, 303)
check("including a sacking", db.manager_clubs()["AF"]["sacked_from"], 12)

r = boss2.post("/admin/club/AF", data={"club": "1", "sacked_from": ""},
               follow_redirects=False)
check("and cleared again", db.manager_clubs()["AF"]["sacked_from"], None)

# The engine already refuses a boost once the manager has gone; this is the
# join between that rule and the data the app stores.
_, _, boost_log, _, _, problems = engine.apply_transactions(
    {"teams": [{"key": "AF", "team": "AF", "squad": []}]},
    [{"type": "boost", "gameweek": 20, "team": "AF"}], 38,
    managers={"AF": {"name": "test", "sacked_from": 12}})
check("a boost after the sacking is refused", len(boost_log), 0)
check_true("and says so", any("sacked" in x for x in problems), str(problems))

check("a normal manager can't reassign clubs",
      plain.post("/admin/assign-clubs", follow_redirects=False).status_code, 404)
os.environ.pop("ADMIN_KEYS", None)

print("\n── Manager boost ───────────────────────────────────────")

os.environ["ADMIN_KEYS"] = "RM"
bc = TestClient(app)
bc.get(f"/m/{db.manager_by_key('RM')['token']}")
bc.post("/admin/assign-clubs")
gwn = engine.current_gameweek()["gameweek"]

st = engine.boost_status("RM", gwn, db.manager_clubs(), used=0)
check_true("a club is drafted", st["available"], str(st.get("why")))
check_true("with a band from the table", st["pct"] in (10.0, 20.0, 30.0, 40.0, 50.0),
           str(st["pct"]))
check_true("flagged as provisional while no football has been played",
           st["provisional_position"])

r = bc.post(f"/declare/{gwn}/boost", data={"on": "1"})
check("playing a boost is accepted", r.json()["ok"], True)
check("and recorded", bool(db.declaration("RM", gwn, "boost")), True)

# Withdrawable before the deadline: changing your mind costs nothing.
bc.post(f"/declare/{gwn}/boost", data={"on": "0"})
check("withdrawing removes it", db.declaration("RM", gwn, "boost"), None)
bc.post(f"/declare/{gwn}/boost", data={"on": "1"})

# The allowance is a season limit, counted across gameweeks.
from mechanics import BOOST_USES_PER_SEASON
# Eight in *other* gameweeks, so the allowance is spent without counting the
# one already declared for this round.
for extra in range(1, BOOST_USES_PER_SEASON + 1):
    db.declare("RM", gwn + extra, "boost")
used = sum(1 for d in db.declarations("boost", "RM") if d["gameweek"] != gwn)
st = engine.boost_status("RM", gwn, db.manager_clubs(), used)
check(f"{BOOST_USES_PER_SEASON} used leaves none", st["left"], 0)
check_true("and it stops being available", not st["available"])
check_true("with a reason", "used this season" in st["why"], st["why"])

r = bc.post(f"/declare/{gwn + 40}/boost", data={"on": "1"})
check("a gameweek that doesn't exist is refused", r.status_code, 404)

# A sacked manager takes the remaining boosts with them.
club = db.manager_clubs()["RM"]["club"]
db.set_manager_club("RM", club, sacked_from=gwn)
st = engine.boost_status("RM", gwn, db.manager_clubs(), used=0)
check_true("a sacking ends it", not st["available"])
check_true("and says which club and when",
           "went with them" in st["why"], st["why"])
db.set_manager_club("RM", club, sacked_from=None)

# A team with no drafted manager can't boost at all.
st = engine.boost_status("NOBODY", gwn, db.manager_clubs(), used=0)
check_true("no drafted manager, no boost", not st["available"])
check_true("and it says so plainly", "no manager drafted" in st["why"], st["why"])

closed = [g for g in engine.calendar() if not engine.deadline_state(g)["open"]]
if closed:
    r = bc.post(f"/declare/{closed[0]['gameweek']}/boost", data={"on": "1"})
    check("a passed deadline refuses a boost", r.status_code, 409)
os.environ.pop("ADMIN_KEYS", None)

print("\n── Trades ──────────────────────────────────────────────")

os.environ["ADMIN_KEYS"] = "RM"
a = TestClient(app)
a.get(f"/m/{db.manager_by_key('RM')['token']}")
gwt = engine.current_gameweek()["gameweek"]
sq = engine.squads_for_gameweek(gwt, [])
mine = [p for p in sq["RM"] if p["position"] == "FWD"][0]
hers = [p for p in sq["AF"] if p["position"] == "FWD"][0]

def offer(points=0, give=None, take=None, to="AF"):
    return a.post("/trade/propose", data={
        "receiver": to, "give": str((give or mine)["id"]),
        "take": str((take or hers)["id"]), "points": str(points)})

check("the trade page loads", a.get("/trade").status_code, 200)
check("a straight swap can be proposed", offer().status_code, 200)

wrong_pos = [p for p in sq["AF"] if p["position"] == "MID"][0]
r = offer(take=wrong_pos)
check("positions must balance", r.status_code, 422)
check_true("and it says why", "balance" in r.json()["errors"][0], str(r.json()["errors"]))

r = offer(points=99999)
check("you can't offer more than you've scored", r.status_code, 422)
check("trading with yourself is refused", offer(to="RM").status_code, 422)

# The receiver decides, and nobody else can decide for them.
pending = [t for t in db.trades("proposed") if t["receiver"] == "AF"][0]
b = TestClient(app)
b.get(f"/m/{db.manager_by_key('AF')['token']}")
outsider = TestClient(app)
outsider.get(f"/m/{db.manager_by_key('CH')['token']}")
check("an outsider can't accept someone else's trade",
      outsider.post(f"/trade/{pending['id']}/accept", follow_redirects=False).status_code, 403)
check("nor can the proposer accept their own",
      a.post(f"/trade/{pending['id']}/accept", follow_redirects=False).status_code, 403)

b.post(f"/trade/{pending['id']}/accept")
check("a straight swap takes effect at once",
      db.trade(pending["id"])["status"], "accepted")
check_true("and moves the players",
           mine["id"] in {p["id"] for p in
                          engine.squads_for_gameweek(gwt, db.trades())["AF"]})

# A points trade goes to the league instead.
mine2 = [p for p in sq["RM"] if p["position"] == "DEF"][0]
hers2 = [p for p in sq["AF"] if p["position"] == "DEF"][0]
check("a points offer within the cap is allowed",
      offer(points=10, give=mine2, take=hers2).status_code, 200)
pts_trade = [t for t in db.trades("proposed") if t["points"] == 10][0]
b.post(f"/trade/{pts_trade['id']}/accept")
check("accepting publishes it rather than settling it",
      db.trade(pts_trade["id"])["status"], "published")
out = engine.trade_outcome(db.trade(pts_trade["id"]))
check("the objection window is open", out["open"], True)
check_true("and it is not yet a transaction",
           pts_trade["id"] not in
           {t.get("id") for t in engine.effective_trades(db.trades())})

check("the two sides can't object to their own trade",
      a.post(f"/trade/{pts_trade['id']}/veto", follow_redirects=False).status_code, 403)
check("but anyone else can",
      outsider.post(f"/trade/{pts_trade['id']}/veto", follow_redirects=False).status_code, 303)
check("objecting twice counts once", len(db.trade(pts_trade["id"])["vetoes"]), 1)
outsider.post(f"/trade/{pts_trade['id']}/unveto")
check("and can be withdrawn", len(db.trade(pts_trade["id"])["vetoes"]), 0)

from mechanics import DEFAULTS
for key in [m["key"] for m in db.managers()
            if m["key"] not in ("RM", "AF")][:DEFAULTS["veto_threshold"]]:
    db.veto_trade(pts_trade["id"], key)
check("enough objections vote it down",
      engine.trade_outcome(db.trade(pts_trade["id"]))["state"], "vetoed")
check_true("so it never reaches the engine",
           not [t for t in engine.effective_trades(db.trades()) if t["points"] == 10])
os.environ.pop("ADMIN_KEYS", None)

print("\n── The bank ────────────────────────────────────────────")

os.environ["ADMIN_KEYS"] = "RM"
bk = TestClient(app)
bk.get(f"/m/{db.manager_by_key('RM')['token']}")
gwb = engine.current_gameweek()["gameweek"]

# A bank only fills from a trade that carried points.
sqb = engine.squads_for_gameweek(gwb, db.trades())
give = [p for p in sqb["AF"] if p["position"] == "MID"][0]
back = [p for p in sqb["RM"] if p["position"] == "MID"][0]
tid = db.propose_trade(gwb, "AF", "RM", [give], [back], 25)
db.set_trade_status(tid, "accepted")
alltx = db.transactions() + engine.effective_trades(db.trades())

st = engine.bank_status("RM", gwb + 1, alltx, db.manager_clubs())
check("receiving points fills the bank", st["balance"], 25)
check("the manager who paid has nothing banked",
      engine.bank_status("AF", gwb + 1, alltx, db.manager_clubs())["balance"], 0)

r = bk.post(f"/declare/{gwb}/bank", data={"points": "10"})
check("spending within the balance is allowed", r.json()["ok"], True)
check("and says what's left", r.json()["left"], 15)
r = bk.post(f"/declare/{gwb}/bank", data={"points": "999"})
check("spending more than you have is refused", r.status_code, 422)
r = bk.post(f"/declare/{gwb}/bank", data={"points": "0"})
check("and it can be withdrawn", r.json()["spending"], 0)

print("\n── Waivers ─────────────────────────────────────────────")

wc = TestClient(app)
wc.get(f"/m/{db.manager_by_key('RM')['token']}")
check("the waivers page loads", wc.get("/waivers").status_code, 200)

gww = engine.current_gameweek()["gameweek"]
sqw = engine.squads_for_gameweek(gww, db.trades())
freew = engine.free_agents(gww, db.trades())
check_true("there are free agents to claim", len(freew) > 50, f"{len(freew)}")
check_true("and none of them is owned",
           not ({p["id"] for p in freew} &
                {p["id"] for sq in sqw.values() for p in sq}))

wanted = [p for p in freew if p["position"] == "MID"][0]

def claim(who, drop_pos="MID", add=None):
    drop = [p for p in sqw[who] if p["position"] == drop_pos][0]
    target = add or wanted
    return db.declare(who, gww, "waiver",
                      {"claims": [{"drop": drop, "add": target}]})

# The two managers at opposite ends of the table both want the same player.
season_now = engine.season(db.all_lineups() or None, db.transactions(),
                           db.manager_clubs())
standings = engine.waiver_order(gww, season_now)
top, bottom = standings[0], standings[-1]
claim(top)
claim(bottom)
run = engine.run_waivers(gww, _claims_for(gww), db.trades(), season_now)
landed = [r for r in run["results"] if r["landed"]]
check("only one of them gets him", len(landed), 1)
check("and it's the one nearer the bottom", landed[0]["team"], bottom)
lost = [r for r in run["results"] if not r["landed"]]
check_true("the other is told they lost the race",
           lost and lost[0]["why"] == "already claimed", str(run["results"]))
check("losing a race isn't an error", run["problems"], [])
db.withdraw(top, gww, "waiver")
db.withdraw(bottom, gww, "waiver")
os.environ.pop("ADMIN_KEYS", None)

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
    sys.exit(1)
print("ALL APP TESTS PASSED")
