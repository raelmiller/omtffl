#!/usr/bin/env python3
"""Smoke tests for phase one.

Deliberately shallow. The scoring rules are tested exhaustively in shadow/;
what needs proving here is that the web layer boots, finds the engine, and
degrades honestly when data or the FPL API is missing.

Run: python3 season-app/test_app.py
"""
import json
import re
import sys
from datetime import datetime, timedelta, timezone as _tz
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


# The waiver window — and now the trade window with it — closes a fixed number
# of hours before kick-off, so whether either is open depends on the hour the
# suite happens to be run at. Left alone, these tests pass in the morning and
# fail after tea. Hold the window open for the whole run; the sections that
# test what happens once it shuts wind it forward themselves and put it back.
_real_waiver_deadline = engine.waiver_deadline
# Fixed instants, not "now + 2h" evaluated per call: a moving deadline gives
# the page one timestamp and the assertion checking it another, microseconds
# apart, and the mismatch looks like a bug in the page.
_WINDOW_OPEN = (datetime.now(_tz.utc) + timedelta(hours=2)
                ).isoformat(timespec="seconds")
_WINDOW_SHUT = (datetime.now(_tz.utc) - timedelta(minutes=5)
                ).isoformat(timespec="seconds")


def _hold_window_open():
    engine.waiver_deadline = lambda g, config=None: _WINDOW_OPEN


def _shut_window():
    engine.waiver_deadline = lambda g, config=None: _WINDOW_SHUT


_hold_window_open()

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
# The band comes from the club's league position, and before any football
# there is no table to read it from. Which state the data happens to be in
# depends on the day, so the test pins the rule rather than the day.
_pl = engine._read("pl_fixtures.json") or []
_played_matches = sum(1 for f in _pl if f.get("finished"))
check("the band is provisional exactly while there is no table yet",
      bool(st["provisional_position"]), _played_matches == 0)

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

check_true("the trade window is the waiver window, not the gameweek's",
           engine.trade_window(engine.current_gameweek())["deadline"]
           < engine.deadline_state(engine.current_gameweek())["deadline"])

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

# The manager RM plays this round can be sent players but not points.
rival = engine.opponents(gwt)["RM"]
theirs = [p for p in sq[rival] if p["position"] == "FWD"][0]
r = offer(points=10, to=rival, take=theirs)
check("no points to the team you play this round", r.status_code, 422)
check_true("and it says which team that is",
           "about to face" in r.json()["errors"][0], str(r.json()["errors"]))
check("but a straight swap with them is fine",
      offer(to=rival, take=theirs).status_code, 200)
check_true("the page names them before anyone tries",
           "you play them this round" in a.get("/trade").text)

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

print("\n── Trades close when waivers do ────────────────────────")

# The rule, and the reason for it: a player you trade for has to be pickable
# for the round you traded him for. On the old clock a published points trade
# settled at the very instant lineups locked, which delivered a player nobody
# could play.
tw_gw = engine.current_gameweek()
tw_n = tw_gw["gameweek"]
check_true("the trade window closes before the gameweek does",
           engine.trade_window(tw_gw)["deadline"]
           < engine.deadline_state(tw_gw)["deadline"])
check("and it is the waiver deadline, not a third clock of its own",
      engine.trade_window(tw_gw)["deadline"],
      engine.waiver_state(tw_gw)["waiver_deadline"])

tsq = engine.market(tw_n, db.trades(), db.transactions())["squads"]
one = "RM"
two = next(k for k in tsq if k not in (one, engine.opponents(tw_n).get(one)))
give = next(p for p in tsq[one] if p["position"] == "MID")
get = next(p for p in tsq[two] if p["position"] == "MID")
paid = db.propose_trade(tw_n, one, two, [give], [get], 10)
db.set_trade_status(paid, "published")

check("while the window is open it is still open to objection",
      engine.trade_outcome(db.trade(paid))["state"], "published")
check_true("so nobody's squad has moved yet",
           not any(p["id"] == get["id"] for p in engine.market(
               tw_n, db.trades(), db.transactions())["squads"][one]))

# Wind past the waiver deadline — a full day before kick-off.
_shut_window()
check("once it shuts, an unobjected trade stands",
      engine.trade_outcome(db.trade(paid))["state"], "accepted")
after_trade = engine.market(tw_n, db.trades(), db.transactions())["squads"]
check_true("the player has moved", any(p["id"] == get["id"] for p in after_trade[one]))
check_true("AND THE ROUND IS STILL OPEN, so he can actually be picked",
           engine.deadline_state(tw_gw)["open"],
           "this is the whole point of the change")

# The pitch is the real test: he has to be on it, not merely in a dict.
tclient = TestClient(app)
tclient.get(f"/m/{db.manager_by_key(one)['token']}")
pitch = tclient.get("/declare").text
check_true("and the pick-team page draws him", f'"id": {get["id"]},' in pitch
           or f'"id":{get["id"]},' in pitch)

# Nothing may move once the window has shut.
late = db.propose_trade(tw_n, one, two, [get], [give], 0)
check("a late offer cannot be accepted",
      tclient.post(f"/trade/{late}/withdraw").status_code, 409)
other = TestClient(app)
other.get(f"/m/{db.manager_by_key(two)['token']}")
check("nor accepted by the other side", other.post(f"/trade/{late}/accept").status_code, 409)
check("and a new proposal is refused",
      tclient.post("/trade/propose", data={
          "receiver": two, "give": str(give["id"]), "take": str(get["id"]),
          "points": 0}).status_code, 409)
# Whitespace-normalised: the copy is wrapped across lines in the template,
# so matching the raw HTML tests the line breaks rather than the words.
_closed_page = re.sub(r"\s+", " ", tclient.get("/trade").text)
check_true("the page says why, rather than blaming the gameweek deadline",
           "shuts a day before each deadline" in _closed_page)
check_true("and points at the pool, which is still open",
           "free-agent pool is open until kick-off" in _closed_page)
db.set_trade_status(late, "withdrawn")

_hold_window_open()
check_true("with the window open again, offers are taken as normal",
           tclient.post("/trade/propose", data={
               "receiver": two, "give": str(give["id"]), "take": str(get["id"]),
               "points": 0}).status_code == 200)

print("\n── Saying whether a trade has actually happened ────────")

# Reported from the running app: "it says the trade has gone through, but the
# player isn't in my team and I can't see the bank of points in the other
# team". Nothing was broken — the trade carried points, so it was published
# and genuinely pending — but the page said "agreed" and listed it as theirs,
# which reads as done, and there was nowhere to see where points had landed.
_hold_window_open()

pg = engine.current_gameweek(); pn = pg["gameweek"]
psq = engine.market(pn, db.trades(), db.transactions())["squads"]
P1 = "RM"
P2 = next(k for k in psq if k not in (P1, engine.opponents(pn).get(P1)))
p_give = next(p for p in psq[P1] if p["position"] == "DEF")
p_take = next(p for p in psq[P2] if p["position"] == "DEF")
c1 = TestClient(app); c1.get(f"/m/{db.manager_by_key(P1)['token']}")
c2 = TestClient(app); c2.get(f"/m/{db.manager_by_key(P2)['token']}")
c1.post("/trade/propose", data={"receiver": P2, "give": str(p_give["id"]),
                                "take": str(p_take["id"]), "points": 7})
# db.trades() is newest-first, so [0] is the offer just made. [-1] is the
# oldest one in the database, which is a different trade entirely.
ptid = db.trades("proposed")[0]["id"]
check("the offer just made is the one being accepted",
      db.trade(ptid)["points"], 7)
c2.post(f"/trade/{ptid}/accept")

flat = lambda t: re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", t))
pending_page = flat(c1.get("/trade").text)
check_true("a pending trade says so before anything else",
           "Not done yet" in pending_page)
check_true("and says the squads have not moved",
           "Neither squad changes until this settles" in pending_page)
check_true("and where the points will go when it does",
           f"points reach {db.manager_by_key(P2)['team']}" in pending_page)
check_true("the heading no longer just says 'agreed'",
           "nothing has moved yet" in pending_page)
check_true("and the player really is still theirs, as the page now says",
           not any(x["id"] == p_take["id"] for x in engine.market(
               pn, db.trades(), db.transactions())["squads"][P1]))

_shut_window()
after = engine.market(pn, db.trades(), db.transactions())["squads"]
check_true("once it settles the player moves", any(x["id"] == p_take["id"]
                                                   for x in after[P1]))
settled_page = flat(c1.get("/trade").text)
check_true("the page stops saying it is pending", "Not done yet" not in settled_page)
check_true("and shows where the points went",
           f"to {db.manager_by_key(P2)['team']}" in settled_page)

# A history that only names the two managers doesn't say what the trade WAS.
check_true("the settled row names the player who went one way",
           p_give["name"] in settled_page)
check_true("and the player who came back",
           p_take["name"] in settled_page)
check_true("attributed to the manager who let him go",
           re.search(rf"{re.escape(db.manager_by_key(P1)['team'])} gave "
                     rf"{re.escape(p_give['name'])}", settled_page) is not None)
check_true("and the other side to the other manager",
           re.search(rf"{re.escape(db.manager_by_key(P2)['team'])} gave "
                     rf"{re.escape(p_take['name'])}", settled_page) is not None)

# The whole point of the report: being able to see the other team's bank.
check_true("every manager's bank is on the page", "Banked points" in settled_page)
check_true("including the manager who received them",
           db.manager_by_key(P2)["team"] in settled_page)

# One pass for everyone must agree with the per-manager answer, or the list
# and the pick-team page would show different numbers for the same thing.
_tx = db.transactions() + engine.effective_trades(db.trades())
_all = engine.banks(_tx, db.manager_clubs())
check("the league-wide banks agree with the one-at-a-time answer",
      {k: v for k, v in _all.items() if v},
      {m["key"]: engine.bank_status(m["key"], pn, _tx, db.manager_clubs())["balance"]
       for m in db.managers()
       if engine.bank_status(m["key"], pn, _tx, db.manager_clubs())["balance"]})
_hold_window_open()

# A trade that was voted down moved nobody, so the history must not say a
# manager "gave" anyone. Same two lines, one different verb — the alternative
# is a settled list where a rejected offer is indistinguishable from a deal.
vsq = engine.market(pn, db.trades(), db.transactions())["squads"]
v_give = next(p for p in vsq[P1] if p["position"] == "FWD")
v_take = next(p for p in vsq[P2] if p["position"] == "FWD")
c1.post("/trade/propose", data={"receiver": P2, "give": str(v_give["id"]),
                                "take": str(v_take["id"]), "points": 3})
vtid = db.trades("proposed")[0]["id"]
check("the offer about to be voted down is the one just made",
      db.trade(vtid)["points"], 3)
c2.post(f"/trade/{vtid}/accept")
for m in db.managers():
    if m["key"] in (P1, P2):
        continue
    vc = TestClient(app); vc.get(f"/m/{m['token']}")
    vc.post(f"/trade/{vtid}/veto")
check("enough managers objected to stop it",
      engine.trade_outcome(db.trade(vtid))["state"], "vetoed")

_shut_window()
vetoed_page = flat(c1.get("/trade").text)
check_true("a voted-down trade still names the players in it",
           v_give["name"] in vetoed_page and v_take["name"] in vetoed_page)
check_true("but says they were offered, not given",
           re.search(rf"{re.escape(db.manager_by_key(P1)['team'])} offered "
                     rf"{re.escape(v_give['name'])}", vetoed_page) is not None)
check_true("and nobody is recorded as having given him away",
           re.search(rf"gave {re.escape(v_give['name'])}", vetoed_page) is None)
check_true("its points are marked as never sent", "never sent" in vetoed_page)
check_true("and they really did not move",
           not any(x["id"] == v_take["id"] for x in engine.market(
               pn, db.trades(), db.transactions())["squads"][P1]))
_hold_window_open()

print("\n── Telling an empty list from a missing feature ────────")

# "That isn't showing in the view." The settled table was inside {% if
# settled %}, so a league with nothing settled yet got no section at all —
# indistinguishable from a page that simply doesn't list trades.
check_true("the Settled section is on the page even when it has rows",
           "Settled" in flat(c1.get("/trade").text))

_empty = TestClient(app)
_empty.get(f"/m/{db.manager_by_key(P1)['token']}")
import app.main as _m
_real_settled = _m.db.trades
try:
    _m.db.trades = lambda *a, **k: []          # a league that has traded nothing
    _blank = flat(_empty.get("/trade").text)
finally:
    _m.db.trades = _real_settled
check_true("with nothing settled the section is still there",
           "Settled" in _blank)
check_true("and says so in words rather than vanishing",
           "Nothing has settled yet" in _blank)

# Which code is running was unanswerable, so "I can't see your change" and
# "the container is still on the old image" looked the same from outside.
_b = _m.build()
check_true("the build reports when the image was made", bool(_b["built"]))
check_true("and when this process started", bool(_b["started"]))
check("health carries it", "build" in c1.get("/health").json(), True)

os.environ["RAILWAY_GIT_COMMIT_SHA"] = "0123456789abcdef0123456789abcdef01234567"
os.environ["RAILWAY_GIT_BRANCH"] = "main"
check("a Railway commit is reported short", _m.build()["commit"], "0123456")
check("with its branch", _m.build()["branch"], "main")
del os.environ["RAILWAY_GIT_COMMIT_SHA"], os.environ["RAILWAY_GIT_BRANCH"]
check("and off Railway it reports no commit rather than a wrong one",
      _m.build()["commit"], None)

# Jinja prints a missing key as an empty string, so a stored player without a
# `name` would render as nothing at all — "X gave" followed by a blank, with
# no way to tell a broken row from an empty one. Write both bad shapes
# straight into the database and check the page still names somebody.
_lookup = engine.player_names()
_pid = next(iter(_lookup))
with db.connect() as _conn:
    _conn.execute(
        "INSERT INTO trade (gameweek, proposer, receiver, players_out,"
        " players_in, points, status, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (pn, P1, P2, json.dumps([{"id": _pid, "position": "MID"}]),
         json.dumps([{"id": _pid, "position": "MID"}]), 0, "accepted", db.now()))
_odd = flat(c1.get("/trade").text)
check_true("a player stored without a name still shows one, from the id",
           _lookup[_pid] in _odd)
with db.connect() as _conn:
    _conn.execute("DELETE FROM trade WHERE id = (SELECT MAX(id) FROM trade)")

print("\n── A trade the engine refused must not read as done ────")

# Reported from the running app: the settled history said a trade was done,
# but the player never arrived and the one given away was still there. Two
# different answers to "did this happen" — trade_outcome (status, objections,
# the clock) and apply_transactions (can it actually be performed) — were
# never compared, and only the first reached the page.
_shut_window()
_rsq = engine.market(pn, db.trades(), db.transactions())["squads"]
_mine = next(p for p in _rsq[P1] if p["position"] == "MID")
_ghost = next(p for p in _rsq[P2] if p["position"] == "MID")
with db.connect() as _conn:
    # Agreed and past its window, but naming a player P2 does not own: the
    # engine refuses it, exactly as it refuses a player traded away earlier.
    _conn.execute(
        "INSERT INTO trade (gameweek, proposer, receiver, players_out,"
        " players_in, points, status, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (pn, P1, P2, json.dumps([_mine]),
         json.dumps([dict(_ghost, id=987654)]), 0, "accepted", db.now()))
_rid = db.trades()[0]["id"]

_probs = engine.market(pn, db.trades(), db.transactions())["problems"]
_why = engine.trade_problem(db.trade(_rid), _probs)
check_true("the engine says why it refused the trade", bool(_why))
check_true("and names the player nobody owns", "987654" in (_why or ""))

# Unescaped: the engine's sentence contains an apostrophe ("doesn't own"),
# which Jinja writes as &#39; — stripping tags is not enough to compare prose.
import html as _html
_refused_page = _html.unescape(flat(c1.get("/trade").text))
check_true("the history no longer calls it done", "not applied" in _refused_page)
check_true("and prints the engine's reason", (_why or "!") in _refused_page)
check_true("the player was not moved, as the page now agrees",
           not any(p["id"] == 987654 for p in engine.market(
               pn, db.trades(), db.transactions())["squads"][P1]))
check_true("and the one offered is still where he was",
           any(p["id"] == _mine["id"] for p in engine.market(
               pn, db.trades(), db.transactions())["squads"][P1]))

# The pick-team page is where a manager notices, so it has to say so too.
check_true("the team page warns that a transaction was thrown out",
           "could not be applied" in flat(c1.get("/declare").text))

# `table` carries a 32rem min-width and scrolls inside .scroll, which on a
# 390px screen pushes Points and Outcome off the right edge — so the cell
# saying whether a trade happened, and why not, was invisible on a phone.
_css = (Path(__file__).parent / "app/static/style.css").read_text()
check_true("the settled table has its own class to hang a phone layout on",
           'class="settled"' in (Path(__file__).parent
                                 / "app/templates/trade.html").read_text())
check_true("and stops being a table on a narrow screen",
           "table.settled thead { display: none; }" in _css)
check_true("shedding the min-width that caused the sideways scroll",
           "table.settled { min-width: 0; }" in _css)

with db.connect() as _conn:
    _conn.execute("DELETE FROM trade WHERE id = ?", (_rid,))
check_true("with it gone the warning goes too",
           "could not be applied" not in flat(c1.get("/declare").text))

# A rule binds when the deal is struck. Every trade the app hands the engine
# went through check_trade at proposal — the same validator, the same caps,
# the same head-to-head rule — so it must be marked as vetted, or a rule
# written later silently reverts a squad that settled days ago.
check_true("every trade the app applies is marked as already vetted",
           all(t.get("agreed") for t in
               engine.effective_trades(db.trades())) if db.trades() else True)
_hold_window_open()

print("\n── Trading moves on when a window shuts ────────────────")

# For the last 24 hours of every round the page offered a round it would
# refuse a trade for: it named current_gameweek(), whose window had shut,
# told the reader to propose for the next one, and gave them no way to.
_here = engine.current_gameweek()
_next = next(g for g in engine.calendar()
             if g["gameweek"] == _here["gameweek"] + 1)

engine.waiver_deadline = (lambda g, config=None:
                          _WINDOW_SHUT if g["gameweek"] == _here["gameweek"]
                          else _WINDOW_OPEN)
check("with this round shut, trading moves to the next",
      engine.trading_gameweek()["gameweek"], _next["gameweek"])
_moved = flat(c1.get("/trade").text)
check_true("and the page says which round it is now offering",
           f"For {_next['name']}" in _moved)
check_true("and why it differs from the one being picked",
           "trading has moved on" in _moved)

# The offer must land in the round the page named, not the one being picked.
_tsq = engine.market(_next["gameweek"], db.trades(), db.transactions())["squads"]
_t2 = next(k for k in _tsq if k != P1)
_g = next(p for p in _tsq[P1] if p["position"] == "DEF")
_t = next(p for p in _tsq[_t2] if p["position"] == "DEF")
_r = c1.post("/trade/propose", data={"receiver": _t2, "give": str(_g["id"]),
                                     "take": str(_t["id"]), "points": 0})
check("an offer is accepted while this round is shut", _r.status_code, 200)
check("and lands in the round the page offered",
      db.trades("proposed")[0]["gameweek"], _next["gameweek"])
with db.connect() as _conn:
    _conn.execute("DELETE FROM trade WHERE id = ?", (db.trades("proposed")[0]["id"],))

_shut_window()
check("with nothing open, trading falls back to this round",
      engine.trading_gameweek()["gameweek"], _here["gameweek"])
check("and an offer is refused rather than misfiled",
      c1.post("/trade/propose", data={"receiver": _t2, "give": str(_g["id"]),
                                      "take": str(_t["id"]), "points": 0}
              ).status_code, 409)
_hold_window_open()
check("with everything open, trading is for the round being picked",
      engine.trading_gameweek()["gameweek"], _here["gameweek"])

print("\n── Who each player faces this round ────────────────────")

_fgw = engine.current_gameweek()["gameweek"]
_fx = engine.club_fixtures(_fgw)
_short = {cid: c.get("short") for cid, c in engine.clubs().items()}

# Both sides of every fixture, and the two sides must agree about which of
# them is at home — a mirrored pair is the whole content of the feature.
_raw = [f for f in engine._read("pl_fixtures.json") if f.get("event") == _fgw]
check("every club playing this round has a fixture",
      len(_fx), len({c for f in _raw for c in (f["team_h"], f["team_a"])}))
for _f in _raw[:5]:
    _h = [x for x in _fx[_f["team_h"]] if x["opponent"] == _short[_f["team_a"]]]
    _a = [x for x in _fx[_f["team_a"]] if x["opponent"] == _short[_f["team_h"]]]
    check_true(f"{_short[_f['team_h']]} is at home to {_short[_f['team_a']]}, "
               "and the away side agrees",
               bool(_h) and _h[0]["home"] and bool(_a) and not _a[0]["home"])

# The club must come from the player data, not the squad entry: an entry
# carries the club its player was at on draft night.
_sq = engine.with_fixtures(engine.refresh_clubs(engine.squad_for("RM")), _fgw)
_at = engine.player_clubs()
check_true("every player's fixture is his current club's",
           all(not p["fixtures"]
               or p["fixtures"] == _fx.get(_at.get(p["id"]), [])
               for p in _sq))
check_true("and matches the club shown beside his name",
           all(p.get("club") == _short.get(_at.get(p["id"])) for p in _sq
               if p.get("club")))

# A blank and a double are the two cases the current round cannot show, and
# both have to survive: a list, never a single fixture.
_fake = [{"event": 99, "team_h": 1, "team_a": 2, "finished": False},
         {"event": 99, "team_h": 3, "team_a": 1, "finished": False}]
_real_read = engine._read
engine._read = lambda n: _fake if n == "pl_fixtures.json" else _real_read(n)
engine._club_fixtures.cache_clear()
try:
    _d = engine.club_fixtures(99)
    check("a club with two games this round carries both", len(_d[1]), 2)
    check_true("one home and one away",
               sorted(x["home"] for x in _d[1]) == [False, True])
    check_true("a club with no game is simply absent", 4 not in _d)
    # A player at a club that isn't in the fake fixtures at all — the blank.
    _blank_player = next(pid for pid, club in _at.items() if club not in (1, 2, 3))
    check("and a player there gets an empty list, not a missing key",
          engine.with_fixtures([{"id": _blank_player, "name": "x"}], 99)[0]
          ["fixtures"], [])
finally:
    engine._read = _real_read
    engine._club_fixtures.cache_clear()

# And it has to reach the pitch, which builds its chips from this payload.
_pitch = c1.get("/declare").text
check_true("the pick-team page ships each player's fixtures",
           '"fixtures":' in _pitch or '"fixtures" :' in _pitch)
check_true("and the chip renders an opponent with home or away",
           "opposition(p)" in _pitch and "f.home ? 'h' : 'a'" in _pitch)
check_true("with a blank round saying so rather than going empty",
           "return 'blank'" in _pitch)

print("\n── Signing a second app in ─────────────────────────────")

# Installed to a home screen the app has its own cookie jar and no address
# bar, so a sign-in link cannot reach it: one tapped in a messaging app signs
# THAT app's browser in and leaves the installed one with nowhere to paste
# anything. A code read off one screen and typed into another crosses it.
_signed_in = TestClient(app)
_signed_in.get(f"/m/{db.manager_by_key(P1)['token']}")
_r = _signed_in.post("/account/pair")
check("a signed-in manager can mint a code", _r.status_code, 200)
_code = _r.json()["code"]
check("the code is the length the page formats for", len(_code), db.PAIR_LENGTH)
check_true("and avoids the characters that get misread",
           not set(_code) & set("ILOU"))

# The whole point: a browser with no cookie at all becomes that manager.
_fresh = TestClient(app)
check("a fresh app is signed out to begin with", _fresh.get("/account").status_code, 401)
check("the code signs it in", _fresh.post("/pair", data={"code": _code},
                                          follow_redirects=False).status_code, 303)
check("as the manager who minted it",
      db.session_manager(_fresh.cookies.get(auth.COOKIE))["key"], P1)
check("and it can now reach its own account page",
      _fresh.get("/account").status_code, 200)

# Single use, or a code read over someone's shoulder stays good.
_again = TestClient(app)
check("the same code cannot be used twice",
      _again.post("/pair", data={"code": _code}).status_code, 400)
check_true("and the page says why rather than just refusing",
           "expired" in _again.post("/pair", data={"code": _code}).text)

# Read off a screen, so a misread character must not cost a round trip.
_r2 = _signed_in.post("/account/pair").json()["code"]
_typo = (_r2.replace("0", "O").replace("1", "I")).lower()
_c3 = TestClient(app)
check("O for zero and lowercase are forgiven",
      _c3.post("/pair", data={"code": _typo}, follow_redirects=False).status_code, 303)
_r3 = _signed_in.post("/account/pair").json()["code"]
_c4 = TestClient(app)
check("and so is the hyphen the page displays",
      _c4.post("/pair", data={"code": _r3[:4] + "-" + _r3[4:]},
               follow_redirects=False).status_code, 303)

# Minting replaces, so only one code per manager is ever live.
_old = _signed_in.post("/account/pair").json()["code"]
_new = _signed_in.post("/account/pair").json()["code"]
check("minting a code retires the one before it",
      TestClient(app).post("/pair", data={"code": _old}).status_code, 400)
check("while the newest one works",
      TestClient(app).post("/pair", data={"code": _new},
                           follow_redirects=False).status_code, 303)

# Expiry is enforced server-side, not by the countdown on the page.
_stale = _signed_in.post("/account/pair").json()["code"]
with db.connect() as _conn:
    _conn.execute("UPDATE pair_code SET expires_at = ?",
                  ((datetime.now(_tz.utc) - timedelta(minutes=1))
                   .isoformat(timespec="seconds"),))
check("an expired code is refused",
      TestClient(app).post("/pair", data={"code": _stale}).status_code, 400)
check_true("and is dropped rather than left lying in the table",
           db.spend_pair_code(_stale) is None)

check("a code nobody minted is refused",
      TestClient(app).post("/pair", data={"code": "ZZZZZZZZ"}).status_code, 400)
check("and so is an empty one",
      TestClient(app).post("/pair", data={"code": ""}).status_code, 400)
check("signing out is still required to mint one",
      TestClient(app).post("/account/pair").status_code, 401)

# Only the hash is stored, for the same reason sessions only keep theirs.
_kept = _signed_in.post("/account/pair").json()["code"]
with db.connect() as _conn:
    _stored = [dict(r) for r in _conn.execute("SELECT * FROM pair_code")]
check_true("the code itself is never written down",
           all(_kept not in str(v) for r in _stored for v in r.values()))

check_true("the sign-in page offers somewhere to type one",
           'action="/pair"' in TestClient(app).get("/account").text)
check_true("and the account page explains why a link won't do",
           "Sign in another app" in _signed_in.get("/account").text)

print("\n── Installing it to a home screen ──────────────────────")

# PNG width and height live at a fixed offset in the header, so the size of an
# icon can be checked without making Pillow a dependency of the app.
def _png_size(raw):
    assert raw[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    return (int.from_bytes(raw[16:20], "big"), int.from_bytes(raw[20:24], "big"))

_mf = c1.get("/manifest.webmanifest")
check("the manifest is served", _mf.status_code, 200)
# A manifest served as text/plain is silently ignored, which looks exactly
# like not having one — so the media type is the assertion, not a detail.
check_true("as a manifest, not as plain text",
           _mf.headers["content-type"].startswith("application/manifest+json"))
_m = _mf.json()
for _k in ("name", "short_name", "start_url", "scope", "icons"):
    check_true(f"it carries {_k}", bool(_m.get(_k)))
check("it opens without browser chrome", _m["display"], "standalone")

# The icons are the half that rots: the manifest keeps promising a file and a
# size long after either has changed, and nothing complains at runtime.
_sizes = {i["sizes"] for i in _m["icons"]}
check_true("it offers both sizes a phone asks for",
           {"192x192", "512x512"} <= _sizes)
check_true("including one that survives being masked to a circle",
           any(i.get("purpose") == "maskable" for i in _m["icons"]))
for _i in _m["icons"]:
    _r = c1.get(_i["src"])
    check(f'{_i["src"]} is really there', _r.status_code, 200)
    _w, _h = _png_size(_r.content)
    check(f'{_i["src"]} is the size the manifest claims', f"{_w}x{_h}", _i["sizes"])

# Scope: at /static/sw.js a worker can only control /static/*, which is not
# enough for the app to be installable.
_sw = c1.get("/sw.js")
check("the service worker is served from the root", _sw.status_code, 200)
check("with the whole app in scope", _sw.headers.get("Service-Worker-Allowed"), "/")
check_true("and caches nothing, since every page here is a live answer",
           "caches" not in _sw.text and "cache" not in _sw.text.lower()
           or "no-cache" in _sw.headers.get("Cache-Control", ""))

_home = c1.get("/").text
check_true("the page links the manifest", '<link rel="manifest"' in _home)
# iOS reads almost none of the manifest; without these it opens in Safari.
for _tag in ("apple-mobile-web-app-capable", "apple-mobile-web-app-title",
             "apple-mobile-web-app-status-bar-style"):
    check_true(f"and carries {_tag} for iOS", _tag in _home)
check_true("with a real 180px touch icon, which iOS will not scale for you",
           "icon-180.png" in _home)
check("and that icon is 180 square",
      _png_size(c1.get("/static/icon-180.png").content), (180, 180))
# The rail already pads itself by env(safe-area-inset-bottom); that reports 0
# unless the viewport opts in, and the rail then sits under the home indicator.
check_true("the viewport opts into the safe-area insets the rail relies on",
           "viewport-fit=cover" in _home)
check_true("which the rail is written to use",
           "env(safe-area-inset-bottom" in _css)

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
wpage = wc.get("/waivers")
check("the waivers page loads", wpage.status_code, 200)

# Deciding who to claim is really deciding who to drop, so a manager's own
# players go in the same table, measured the same way.
check_true("the pool can be switched to your own squad",
           'id="fshow"' in wpage.text)
own = json.loads(re.search(r'id="squad-data"[^>]*>(.*?)</script>',
                           wpage.text, re.S).group(1))
check("all fifteen of them are there", len(own), 15)
check_true("each carrying the same numbers the pool does",
           all("stats" in p for p in own),
           str([p["name"] for p in own if "stats" not in p]))
metrics = {k for k, _, _, _ in engine.available_metrics()}
check_true("every sortable metric among them",
           all(metrics <= set(p["stats"]) for p in own),
           str(sorted(metrics - set(own[0]["stats"]))))

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

print("\n── The free-agent pool ─────────────────────────────────")

gwp = engine.current_gameweek()["gameweek"]
pool = engine.free_agent_pool(gwp, db.trades())
check_true("free agents carry their season numbers",
           pool and "stats" in pool[0], str(pool[:1]))

metrics = engine.available_metrics()
check_true("metrics are offered", len(metrics) >= 10, f"{len(metrics)}")
keys = {m[0] for m in metrics}
for wanted in ("total_points", "form", "minutes", "goals_scored",
               "assists", "clean_sheets", "bonus", "defensive_contribution",
               "expected_goals", "expected_assists", "starts"):
    check_true(f"  can sort by {wanted}", wanted in keys)

# A metric the data doesn't carry must not be offered as a column that would
# read nought for every player.
stats = engine.player_stats()
sample = next(iter(stats.values()))
missing = [k for k, v in sample.items() if v is None]
check_true("missing data is None rather than nought",
           all(k not in keys for k in missing),
           f"offered but absent: {[k for k in missing if k in keys]}")

totals = [p["stats"].get("total_points") or 0 for p in pool]
check_true("season points aggregate to something", max(totals) > 0, f"max {max(totals)}")
check_true("nobody in the pool is owned",
           not ({p["id"] for p in pool} &
                {p["id"] for sq in engine.squads_for_gameweek(gwp, db.trades()).values()
                 for p in sq}))

print("\n── A team's gameweek ───────────────────────────────────")

vc = TestClient(app)
gwv = engine.season()["rounds"][0]["gameweek"]
detail = engine.team_gameweek("RM", gwv, db.all_lineups(), db.transactions(),
                              db.manager_clubs())
check_true("a team's round can be read back", detail is not None)
check("eleven on the pitch",
      sum(len(players) for _, players in detail["lines"]), 11)
check_true("laid out in position lines",
           [pos for pos, _ in detail["lines"]] == ["GK", "DEF", "MID", "FWD"])
check_true("every player carries their points",
           all("points" in p for _, players in detail["lines"] for p in players))

# The total here and the total in the table have to be the same number.
season_now = engine.season(db.all_lineups() or None, db.transactions(),
                           db.manager_clubs())
rnd = next(r for r in season_now["rounds"] if r["gameweek"] == gwv)
from_table = next((m["home_score"] if m["home_key"] == "RM" else m["away_score"])
                  for m in rnd["matches"]
                  if "RM" in (m["home_key"], m["away_key"]))
detail2 = engine.team_gameweek("RM", gwv, db.all_lineups(), db.transactions(),
                               db.manager_clubs())
check("the team page agrees with the table", detail2["total"], from_table)

page = vc.get(f"/team/RM/{gwv}")
check("the page renders", page.status_code, 200)
# Every shirt has to name a club, or it falls back to a blank tile and the
# manager loses the quickest read on the pitch: who plays for whom.
shirts = re.findall(r'<span class="shirt"[^>]*>', page.text)
check("a shirt for the eleven and the bench behind them",
      len(shirts), 11 + len(detail["bench"]))
check_true("every shirt names a club",
           all(re.search(r'data-club="[A-Z]{3}"', s) for s in shirts),
           next((s for s in shirts if not re.search(r'data-club="[A-Z]{3}"', s)), ""))
kits = open("app/static/style.css").read()
check_true("and the stylesheet dresses it",
           all(f'data-club="{c["short"]}"' in kits for c in engine.clubs().values()),
           ", ".join(c["short"] for c in engine.clubs().values()
                     if f'data-club="{c["short"]}"' not in kits))

check("without a gameweek it shows the latest", vc.get("/team/RM").status_code, 200)
check("an unknown manager is a 404", vc.get(f"/team/NOBODY/{gwv}").status_code, 404)
check_true("results on the table link into it",
           '/team/' in vc.get("/").text)

# A player an autosub replaced is still one of your fifteen. Dropping them
# from the page made the bench look short, and counted points as "unused"
# that had in fact been used.
subbed = next((engine.team_gameweek(t["key"], gwv, db.all_lineups(),
                                    db.transactions(), db.manager_clubs())
               for t in engine.season()["table"]
               if engine.team_gameweek(t["key"], gwv, db.all_lineups(),
                                       db.transactions(),
                                       db.manager_clubs())["subs"]), None)
if subbed:
    check("the whole fifteen is accounted for",
          sum(len(p) for _, p in subbed["lines"]) + len(subbed["bench"]), 15)
    taken_off = {s["off"] for s in subbed["subs"]}
    check("the ones taken off are on the bench",
          sorted(b["name"] for b in subbed["bench"] if b["went_off"]),
          sorted(taken_off))
    check_true("and they come after the ones never called on",
               [b["name"] for b in subbed["bench"][-len(taken_off):]]
               == [b["name"] for b in subbed["bench"] if b["went_off"]])
    check("unused points exclude anyone who came on",
          subbed["bench_points"],
          sum(b["points"] for b in subbed["bench"]))

print("\n── Where a player's points came from ───────────────────")

# The best scorer on the pitch, so the breakdown has something in it beyond
# "didn't play" — a nought itemises to one line and proves very little.
scorer = max((p for _, players in detail["lines"] for p in players),
             key=lambda p: p["points"])
detail_p = engine.player_detail(scorer["id"])
pid = scorer["id"]
week = detail_p["history"][-1]
check_true("the latest round is itemised", bool(week["breakdown"]),
           str(week["breakdown"]))
check("and the items add up to the score",
      sum(r["points"] for r in week["breakdown"]), week["points"])
check_true("every row says what it was for",
           all(r["what"] for r in week["breakdown"]))
check_true("a scorer's week is more than one line",
           len(week["breakdown"]) > 1, str(week["breakdown"]))
check_true("starting with the minutes they played",
           week["breakdown"][0]["what"] == "Minutes")
check("the popup serves it too",
      vc.get(f"/api/player/{pid}").json()["history"][-1]["breakdown"],
      week["breakdown"])

print("\n── Whose team is that ──────────────────────────────────")

front = vc.get("/").text
check_true("the table names the manager as well as the team",
           "<th>Manager</th>" in front)
check_true("with everyone's initials against their row",
           all(f'<td class="who">{m["key"]}</td>' in front
               for m in db.managers()),
           ", ".join(m["key"] for m in db.managers()
                     if f'<td class="who">{m["key"]}</td>' not in front))
check_true("and against both sides of every fixture",
           front.count('class="mgr"') >= 2 * 7)
check_true("a round of its own says so too",
           'class="mgr"' in vc.get(f"/gameweek/{gwv}").text)
check_true("as does a team's page",
           'class="mgr"' in vc.get(f"/team/RM/{gwv}").text)

print("\n── The whole fixture list ──────────────────────────────")

scored = engine.season(db.all_lineups() or None, db.transactions(),
                       db.manager_clubs())["rounds"]
allfx = engine.fixture_list(scored)
check("every round of the season", len(allfx), 38)
check_true("in order, one to thirty-eight",
           [r["gameweek"] for r in allfx] == list(range(1, 39)))
check("every fixture", sum(len(r["matches"]) for r in allfx), 38 * 7)
check("the scored ones are marked played",
      sorted(r["gameweek"] for r in allfx if r["played"]),
      sorted(r["gameweek"] for r in scored))
future = next(r for r in allfx if not r["played"])
check_true("an unplayed round still names both teams",
           all(m["home"] and m["away"] for m in future["matches"]))
check_true("and carries its deadline", bool(future["deadline"]),
           str(future.get("deadline")))
check_true("but no score to show",
           not any("home_score" in m for m in future["matches"]))

front = vc.get("/").text
check_true("the table page carries all 38 rounds",
           all(f'id="gw{n}"' in front for n in range(1, 39)),
           ", ".join(str(n) for n in range(1, 39) if f'id="gw{n}"' not in front))
check("with a chip apiece to click through", front.count('class="gwchip'), 38)
check("and only one of them open", front.count('<div class="round" id="gw')
      - front.count("hidden>"), 1)

print("\n── Points that changed hands ───────────────────────────")

# A trade paid for in points has to land on the table as well as on the team
# page, or a manager tops the league on a score they spent.
sq = engine.squads_for_gameweek(gwv, db.trades())
# Not each other's opponent, or the head-to-head rule refuses it — which is
# the point of that rule, and tested where it belongs.
buyer = "EE"
seller = next(k for k in sq
              if k != buyer and engine.opponents(gwv).get(buyer) != k)
out = next(p for p in sq[buyer] if p["position"] == "FWD")
back = next(p for p in sq[seller] if p["position"] == "FWD")
paid_id = db.propose_trade(gwv, buyer, seller, [out], [back], 20)
db.set_trade_status(paid_id, "accepted")

# And a boost played in the same round, so the fixture can mark both.
booster = next((k for k, v in db.manager_clubs().items()
                if v.get("club") and k not in (buyer, seller)), None)
if booster:
    db.declare(booster, gwv, "boost")

tx = db.transactions() + engine.effective_trades(db.trades())
after = engine.season(db.all_lineups() or None, tx, db.manager_clubs())
rnd = next(r for r in after["rounds"] if r["gameweek"] == gwv)


def side_of(key, match):
    return "home" if match["home_key"] == key else "away"


m = next(m for m in rnd["matches"] if buyer in (m["home_key"], m["away_key"]))
side = side_of(buyer, m)
page = engine.team_gameweek(buyer, gwv, db.all_lineups(), tx, db.manager_clubs())
check("the team page still knows what was paid", page["adjustment"], -20)
check("the table now agrees with it", m[f"{side}_score"], page["total"])
check("and the fixture says how much moved", m[f"{side}_move"], -20)
check("the standings count it too",
      next(r["PF"] for r in after["table"] if r["key"] == buyer),
      sum(x[f"{side_of(buyer, x)}_score"] for r in after["rounds"]
          for x in r["matches"] if buyer in (x["home_key"], x["away_key"])))
check("the seller banks it rather than scoring it",
      next(m[f"{side_of(seller, m)}_move"] for m in rnd["matches"]
           if seller in (m["home_key"], m["away_key"])), 0)

if booster:
    bm = next(m for m in rnd["matches"]
              if booster in (m["home_key"], m["away_key"]))
    check_true("a boost played shows on the fixture",
               bm[f"{side_of(booster, bm)}_boost"] is not None)
    check_true("with the club it was played on",
               bool(bm[f"{side_of(booster, bm)}_boost"]["club"]))
    other = next(m for m in rnd["matches"]
                 if booster not in (m["home_key"], m["away_key"])
                 and buyer not in (m["home_key"], m["away_key"]))
    check("and nothing on a fixture where nobody played one",
          (other["home_boost"], other["away_boost"], other["home_move"],
           other["away_move"]), (None, None, 0, 0))

front = vc.get("/").text
check_true("the table page draws the markers", 'class="tag' in front)

print("\n── The season read sideways ────────────────────────────")

# Four teams, two rounds, worked by hand. Luck is the only figure here that
# isn't obvious by eye, so it's the one worth pinning:
#
#   GW1  A 50 B 40 C 30 D 20     GW2  A 10 B 40 C 20 D 40
#
# A outscored everyone in GW1 (a whole expected win) and nobody in GW2 (none),
# so A is owed 1.0 and won 1 — dead level. C outscored one of three in each
# round, so is owed 0.67, but the fixture list handed it two wins.
def _round(gw, pairs):
    return {"gameweek": gw, "name": f"Gameweek {gw}", "state": "final",
            "matches": [{"home": h, "away": a, "home_key": h, "away_key": a,
                         "home_score": hs, "away_score": as_,
                         "home_boost": None, "away_boost": None,
                         "home_move": 0, "away_move": 0}
                        for h, hs, a, as_ in pairs]}


worked = {
    "ready": True,
    "rounds": [_round(2, [("A", 10, "C", 20), ("B", 40, "D", 40)]),
               _round(1, [("A", 50, "B", 40), ("C", 30, "D", 20)])],
    "table": [{"key": k, "team": k, "rank": i}
              for i, k in enumerate("ABCD", 1)],
}
an = engine.analytics(worked)
by = {t["key"]: t for t in an["teams"]}

check("both rounds counted", an["played"], 2)
check("a team's scores come back in order", by["A"]["scores"], [50, 10])
check("and their results with them", by["A"]["results"], ["W", "L"])
check("expected wins for the team that topped one week and propped the other",
      by["A"]["expected_wins"], 1.0)
check("so its luck is nil", by["A"]["luck"], 0.0)
check("the team owed least got most", by["C"]["expected_wins"], 0.7)
check("which is luck of", by["C"]["luck"], 1.3)
check("and a draw pays half an expected win", by["D"]["expected_wins"], 0.8)
check("spread of fifty and ten", by["A"]["spread"], 20.0)
check("no spread at all for two identical scores", by["B"]["spread"], 0.0)
check("low and high", (by["A"]["low"], by["A"]["high"], by["A"]["range"]),
      (10, 50, 40))
check("what they won by", by["A"]["won_by"], 10.0)
check("and lost by", by["A"]["lost_by"], 10.0)
check("a draw is neither", (by["B"]["won_by"], by["B"]["lost_by"]), (0.0, 10.0))

# A single round has no spread to speak of, and the page must say so rather
# than print a confident 0.0.
one = engine.analytics({"ready": True, "rounds": [worked["rounds"][1]],
                        "table": worked["table"]})
check("one round is not a spread",
      [t["spread"] for t in one["teams"]], [None] * 4)

tight = engine.analytics({
    "ready": True, "table": worked["table"],
    "rounds": [_round(1, [("A", 41, "B", 40), ("C", 60, "D", 20)])]})
tby = {t["key"]: t for t in tight["teams"]}
check("a one-point win is a close one",
      (tby["A"]["close_wins"], tby["B"]["close_losses"]), (1, 1))
check("and forty points is a thrashing",
      (tby["C"]["blowout_wins"], tby["D"]["blowout_losses"]), (1, 1))
check("neither is the other",
      (tby["A"]["blowout_wins"], tby["C"]["close_wins"]), (0, 0))

sp = vc.get("/stats")
check("the stats page renders", sp.status_code, 200)
check_true("without signing in", TestClient(app).get("/stats").status_code == 200)
check_true("with every analysis on it",
           all(f'data-panel="{p}"' in sp.text
               for p in ("form", "spread", "luck", "margins", "returns")))
blocks = re.findall(r'<div class="panelblock"[^>]*>', sp.text)
check("all five are there", len(blocks), 5)
check("and only one of them open",
      sum(1 for b in blocks if "hidden" not in b), 1)
nav = TestClient(app)
nav.get(f"/m/{db.manager_by_key('RM')['token']}")
check_true("and the nav offers it", 'href="/stats"' in nav.get("/").text)

# Returns are credited to the eleven that played, so they should add up to
# what the squads actually did rather than to who owns whom now.
live = engine.analytics(engine.season(db.all_lineups() or None, tx,
                                      db.manager_clubs()))
check("five things to flip between", len(live["returns"]), 5)
check_true("each with a total for every team",
           all(set(m for m, _, _ in live["returns"]) <= set(t["returns"])
               for t in live["teams"]))
check_true("and nobody has returned a negative number of anything",
           all(t["returns"][m] >= 0 for t in live["teams"]
               for m, _, _ in live["returns"]))
scored = [t for t in live["teams"] if t["returns"]["clean_sheets"]]
check_true("somebody kept a clean sheet in the round that has been played",
           bool(scored))
if scored:
    t = scored[0]
    check_true("and the player who kept most of them is named",
               (t["returns"]["best"]["clean_sheets"] or {}).get("name"))
check_true("the panel is on the page",
           'data-panel="returns"' in sp.text)
check("with one list showing and the rest waiting on the dropdown",
      sp.text.count('class="panel metricblock"'), 5)
check_true("and the dropdown names all five",
           all(f'value="{m}"' in sp.text for m, _, _ in live["returns"]))

# Every answer from boost_status has to carry the same keys. Two of them
# didn't, and the page rendered a bare "of left" where the numbers belong.
for label, drafted in [("no manager drafted", {}),
                       ("a manager who was sacked",
                        {"RM": {"club": 1, "sacked_from": 1}})]:
    st = engine.boost_status("RM", 5, drafted, used=0)
    check(f"{label}: still says how many boosts there are",
          (st.get("used"), st.get("left"), st.get("total")), (0, 0, 8))
    check_true(f"{label}: and says why there is nothing to play", bool(st["why"]))

print("\n── Naming your own team ────────────────────────────────")

namer = TestClient(app)
namer.get(f"/m/{db.manager_by_key('DP')['token']}")
was = db.manager_by_key("DP")["team"]

r = namer.post("/declare/name", data={"team": "  Salah  Dressing  "})
check("a manager can rename their own team", r.status_code, 200)
check("with the spacing tidied but the words left alone",
      r.json()["team"], "Salah Dressing")
check("and it sticks", db.manager_by_key("DP")["team"], "Salah Dressing")

check("an empty name is refused",
      namer.post("/declare/name", data={"team": "   "}).status_code, 422)
check_true("and says why",
           "needs a name" in namer.post("/declare/name", data={"team": ""})
           .json()["errors"][0])
check("the name survives a refusal", db.manager_by_key("DP")["team"],
      "Salah Dressing")

long_one = "x" * (db.TEAM_NAME_MAX + 1)
r = namer.post("/declare/name", data={"team": long_one})
check("one character too long is refused", r.status_code, 422)
check_true("and says how long is too long",
           str(db.TEAM_NAME_MAX) in r.json()["errors"][0],
           r.json()["errors"][0])
check("exactly the limit is fine",
      namer.post("/declare/name",
                 data={"team": "y" * db.TEAM_NAME_MAX}).status_code, 200)

# Newlines would break every row the name sits in, so they are folded away
# rather than refused — nobody types one on purpose.
r = namer.post("/declare/name", data={"team": "Two\nLines"})
check("a name with a line break becomes one line", r.json()["team"], "Two Lines")

check("a stranger can't rename anyone",
      TestClient(app).post("/declare/name",
                           data={"team": "Hostile Takeover"}).status_code, 401)
check("and DP is still DP", db.manager_by_key("DP")["team"], "Two Lines")

# The engine takes names from the squad file, so a rename has to reach it or
# the table would still show what the team was called on draft day.
renamed = db.team_names()
league = engine.season(db.all_lineups() or None, db.transactions(),
                       db.manager_clubs(), names=renamed)
check("the table shows the new name",
      next(r["team"] for r in league["table"] if r["key"] == "DP"), "Two Lines")
check_true("and so do the fixtures",
           any(m["home"] == "Two Lines" or m["away"] == "Two Lines"
               for r in engine.fixture_list(league["rounds"], names=renamed)
               for m in r["matches"]))
check("the stats page too",
      next(t["team"] for t in engine.analytics(league)["teams"]
           if t["key"] == "DP"), "Two Lines")
gwn = league["rounds"][0]["gameweek"]
check("and a team's own page",
      engine.team_gameweek("DP", gwn, db.all_lineups(), db.transactions(),
                           db.manager_clubs(), names=renamed)["team"],
      "Two Lines")
check_true("the front page renders it",
           "Two Lines" in namer.get("/").text)
db.rename_team("DP", was)

print("\n── A link, and the hour after someone opens it ─────────")

# A manager the rest of the suite leaves alone, so the session counts below
# mean what they say.
WHO = "YG"
db.end_all_sessions(WHO)

link = db.rotate_token(WHO, days=db.LINK_DAYS)
browser_a = TestClient(app)
r = browser_a.get(f"/m/{link}", follow_redirects=False)
check("the link signs you in", r.status_code, 303)
check("and the app opens", browser_a.get("/declare").status_code, 200)

# Tapping a link in a messaging app opens it in that app's browser. The
# manager who then opens the app properly must not find the link already
# dead — that is the whole reason for the hour.
again = TestClient(app)
r = again.get(f"/m/{link}", follow_redirects=False)
check("the same link still works in a second browser", r.status_code, 303)
check("which is now signed in too", again.get("/declare").status_code, 200)
check("two browsers off one link", len(db.sessions_for(WHO)), 2)

# ...but the window is measured from the first use, not the last, so opening
# it repeatedly can't keep it alive.
after_first = db.manager_by_key(WHO)["token_expires"]
TestClient(app).get(f"/m/{link}")
check("using it again doesn't push the hour back",
      db.manager_by_key(WHO)["token_expires"], after_first)
check_true("and the hour is an hour, not the week it started with",
           after_first < (datetime.now(_tz.utc)
                          + timedelta(minutes=db.LINK_GRACE_MINUTES + 5)
                          ).isoformat(timespec="seconds"), after_first)

# Once the hour is up it is gone, whoever is holding it.
with db.connect() as conn:
    conn.execute("UPDATE manager SET token_expires = ? WHERE key = ?",
                 ((datetime.now(_tz.utc) - timedelta(minutes=1))
                  .isoformat(timespec="seconds"), WHO))
late = TestClient(app)
r = late.get(f"/m/{link}", follow_redirects=False)
check_true("an hour later the link is dead",
           "bad_link" in r.headers["location"], r.headers["location"])
check("so a latecomer is still a stranger",
      late.get("/declare").status_code, 401)
check("and the browsers already signed in are untouched",
      browser_a.get("/declare").status_code, 200)
db.end_all_sessions(WHO)
db.rotate_token(WHO, days=db.LINK_DAYS)

# A short link a manager mints for themselves is not lengthened by using it.
own = db.rotate_token(WHO, minutes=db.LINK_MINUTES)
TestClient(app).get(f"/m/{own}")
check_true("a fifteen-minute link stays a fifteen-minute link",
           db.manager_by_key(WHO)["token_expires"]
           < (datetime.now(_tz.utc) + timedelta(minutes=db.LINK_MINUTES + 2)
              ).isoformat(timespec="seconds"),
           db.manager_by_key(WHO)["token_expires"])

# Clean slate for the session tests below, with one browser signed in.
db.end_all_sessions(WHO)
browser_a = TestClient(app)
browser_a.get(f"/m/{db.rotate_token(WHO, days=db.LINK_DAYS)}")
check("one browser to start from", len(db.sessions_for(WHO)), 1)

# The cookie is a session secret, not the sign-in link. A copy of the
# database should not be a set of working logins.
cookie = browser_a.cookies.get(auth.COOKIE)
check_true("the cookie is not the link that made it", cookie != link)
check_true("nor anybody's link",
           cookie not in {m["token"] for m in db.managers()})
with db.connect() as conn:
    stored = [r[0] for r in conn.execute("SELECT id FROM session")]
check_true("and it is not sitting in the session table either",
           cookie not in stored, "the cookie value is stored verbatim")
check_true("only its fingerprint is", db._fingerprint(cookie) in stored)

expired = db.rotate_token(WHO, minutes=-1)
r = TestClient(app).get(f"/m/{expired}", follow_redirects=False)
check_true("a link past its expiry is refused too",
           "bad_link" in r.headers["location"], r.headers["location"])
check_true("and refused the same way a wrong one is",
           TestClient(app).get("/m/nonsense", follow_redirects=False)
           .headers["location"] == r.headers["location"])
db.rotate_token(WHO, days=db.LINK_DAYS)   # a good one again, for what follows

print("\n── Sessions you can see and end ────────────────────────")

browser_b = TestClient(app)
browser_b.get(f"/m/{db.manager_by_key(WHO)['token']}")
check("two browsers, two sessions", len(db.sessions_for(WHO)), 2)
check("one of which knows it is the one asking",
      sum(1 for x in db.sessions_for(WHO, browser_a.cookies.get(auth.COOKIE))
          if x["this_one"]), 1)

apage3 = browser_a.get("/account")
check("the account page renders", apage3.status_code, 200)
check_true("and says where you're signed in", "Where you" in apage3.text)

made = browser_b.post("/account/link")
check("a manager can mint their own link", made.json()["ok"], True)
token3 = made.json()["link"].rsplit("/", 1)[-1]
browser_c = TestClient(app)
browser_c.get(f"/m/{token3}")
check("which signs in a third browser",
      browser_c.get("/declare").status_code, 200)
check("three sessions now", len(db.sessions_for(WHO)), 3)

browser_a.get("/signout")
check("signing out ends one session", len(db.sessions_for(WHO)), 2)
check("and that browser specifically",
      browser_a.get("/declare").status_code, 401)
check("while the others carry on", browser_b.get("/declare").status_code, 200)

browser_b.post("/account/signout-all", follow_redirects=False)
check("signing out everywhere ends the lot", len(db.sessions_for(WHO)), 0)
check("including browsers that weren't asking",
      browser_c.get("/declare").status_code, 401)

# A session nobody has used inside the window is dropped, whether or not the
# browser still holds the cookie.
stale = TestClient(app)
stale.get(f"/m/{db.manager_by_key(WHO)['token']}")
check("signed in again", stale.get("/declare").status_code, 200)
with db.connect() as conn:
    conn.execute("UPDATE session SET last_seen = ? WHERE manager = ?",
                 (datetime(2020, 1, 1, tzinfo=_tz.utc).isoformat(), WHO))
check("a session left unused for months stops working",
      stale.get("/declare").status_code, 401)
check("and is listed as gone", len(db.sessions_for(WHO)), 0)
check("pruning clears it out", db.prune_sessions(), 1)

print("\n── Bringing data in by hand ────────────────────────────")

fresh = engine.freshness()
check_true("what is on disk is countable", fresh["gameweeks_on_disk"] >= 1)
check_true("along with how many players we know of", fresh["players"] > 0)
# None and 0 mean different things here and the difference matters: no
# availability key at all means the file predates the fetcher asking, while
# zero would mean it asked and nobody is hurt. Report the file, never a guess.
_raw_av = (engine._read("players.json") or {}).get("availability")
check("the injury count is read off the file rather than assumed",
      fresh["availability"], None if _raw_av is None else len(_raw_av))
check_true("a file that was never asked reads as unknown, not as nobody hurt",
           (fresh["availability"] is None) == (_raw_av is None))

from app.main import REFRESH_SCHEDULE                       # noqa: E402

# Injury news and prices move all week, and a manager picking on Friday is
# reading whatever the last fetch brought in — so the fetch runs every day.
runs = []
prev, now = None, datetime(2026, 8, 26, 0, 0, tzinfo=_tz.utc)
for _ in range(4):
    nxt = REFRESH_SCHEDULE.get_next_fire_time(prev, now)
    runs.append(nxt)
    prev, now = nxt, nxt
check("four runs, four consecutive days",
      [r.day for r in runs], [26, 27, 28, 29])
check_true("all at the same hour", all((r.hour, r.minute) == (7, 45) for r in runs))
check_true("including a Friday, which is when most deadlines fall",
           any(r.weekday() == 4 for r in runs))

adm = TestClient(app)
os.environ["ADMIN_KEYS"] = "RM"
adm.get(f"/m/{db.manager_by_key('RM')['token']}")
apage = adm.get("/admin")
check("the admin page still renders", apage.status_code, 200)
check_true("with a button to fetch", 'id="refresh"' in apage.text)
check_true("and it says what is on disk now",
           str(fresh["gameweeks_on_disk"]) in apage.text
           and "Rounds on disk" in apage.text)
check_true("and when the next scheduled run is, or that there isn't one",
           "scheduled run" in apage.text)
check("and a manager who isn't an admin can't see any of it",
      TestClient(app).get("/admin").status_code, 404)

# The button is behind the admin page, so the route it calls has to be too.
# It is harmless in what it does and expensive in what it costs to run.
check("a passer-by can't start a fetch",
      TestClient(app).post("/admin/refresh").status_code, 404)
plain = TestClient(app)
plain.get(f"/m/{db.manager_by_key('AJ')['token']}")
check("nor can a manager who isn't an admin",
      plain.post("/admin/refresh").status_code, 404)

# A probe that failed once must not refuse every refresh after it. Pressing
# the button is someone saying they think the answer has changed.
fetcher.STATUS["reachable"] = False
probes = []
was_probe = fetcher.probe
fetcher.probe = lambda *a, **k: (probes.append(1), False)[1]
try:
    r = adm.post("/admin/refresh")
    check("a refresh that can't reach FPL answers rather than hanging",
          r.status_code, 200)
    check("and says it pulled nothing", r.json()["ok"], False)
    check("having tested the route again first", len(probes), 1)
    check_true("with a reason worth reading",
               "cannot reach" in (r.json()["detail"] or ""), str(r.json()))
finally:
    fetcher.probe = was_probe
os.environ.pop("ADMIN_KEYS", None)

print("\n── Who might not play ──────────────────────────────────")

# Two levels only. Red is "he isn't playing", amber is "he might not", and a
# manager picking a team needs to tell those apart at a glance.
cases = [
    ({"status": "i", "chance": 0, "news": "Knee injury"}, "red"),
    ({"status": "s", "chance": None, "news": "Suspended"}, "red"),
    ({"status": "u", "chance": None, "news": ""}, "red"),
    ({"status": "n", "chance": None, "news": ""}, "red"),
    ({"status": "i", "chance": 25, "news": "Ankle"}, "amber"),
    ({"status": "d", "chance": 50, "news": ""}, "amber"),
    ({"status": "d", "chance": 75, "news": ""}, "amber"),
]
for entry, want in cases:
    check(f"{entry['status']} at {entry['chance']}",
          (engine.flag(entry) or {}).get("level"), want)
check("nothing against a player is no flag at all", engine.flag(None), None)
check("a suspension says so without needing FPL's wording",
      engine.flag({"status": "s", "chance": None, "news": ""})["text"],
      "Suspended")
check("but FPL's own note wins when there is one, rather than stuttering",
      engine.flag({"status": "i", "chance": 25,
                   "news": "Ankle injury - 25% chance of playing"})["text"],
      "Ankle injury - 25% chance of playing")

# The data only arrives with a fetch. Until then nobody is flagged, and
# nobody is wrongly cleared either — the absence has to be silent.
check("no availability data means no flags",
      [p for p in engine.squad_for("RM") if p.get("flag")], [])

hurt = engine.squad_for("RM")[0]
was = engine.availability
engine.availability = lambda pid=None: (
    {str(hurt["id"]): {"status": "i", "chance": 0, "news": "Knee"}}
    if pid is None else
    ({"status": "i", "chance": 0, "news": "Knee"}
     if str(pid) == str(hurt["id"]) else None))
try:
    flagged = engine.refresh_clubs(engine.squad_for("RM"))
    check("a squad carries the doubt",
          [p["name"] for p in flagged if p.get("flag")], [hurt["name"]])
    check("at the right level",
          next(p["flag"]["level"] for p in flagged if p.get("flag")), "red")
    check("everyone else is left clean",
          sum(1 for p in flagged if p.get("flag")), 1)
    pooled = engine.with_stats([hurt])
    check_true("and so does the waiver pool", bool(pooled[0].get("flag")))
    check_true("the popup carries it too",
               bool(engine.player_detail(hurt["id"])["flag"]))
finally:
    engine.availability = was

print("\n── Deadlines and offers you can see ────────────────────")

wv = TestClient(app)
wv.get(f"/m/{db.manager_by_key('RM')['token']}")
wpage2 = wv.get("/waivers").text
check_true("the waivers page counts down to the deadline",
           'class="clock" data-deadline=' in wpage2)
# There are two deadlines in a week now, and the page counts down to whichever
# one is actually next: the run while claims are open, the gameweek after it.
_wstate = engine.waiver_state(engine.current_gameweek())
check_true("and says when that is in words",
           _wstate["next_deadline"] in wpage2,
           f"{_wstate['phase']}: expected {_wstate['next_deadline']}")
check_true("counting down to the run, not past it, while claims are open",
           f'data-deadline="{_wstate["waiver_deadline"]}"' in wpage2
           if _wstate["waivers_open"] else True)

# A published trade of your own showed on everyone else's page as something
# to object to, and on your own page nowhere at all.
gwp = engine.current_gameweek()["gameweek"]
sqp = engine.squads_for_gameweek(gwp, db.trades())
mate = next(k for k in sqp
            if k not in ("RM", engine.opponents(gwp).get("RM")))
pid2 = db.propose_trade(gwp, "RM", mate,
                        [next(p for p in sqp["RM"] if p["position"] == "GK")],
                        [next(p for p in sqp[mate] if p["position"] == "GK")], 5)
db.set_trade_status(pid2, "published")
tpage = wv.get("/trade").text
check_true("your own open trade is on your own page",
           'id="mine-open"' in tpage)
check_true("with the objections it would take to stop it",
           "unless 4 managers object" in re.sub(r"\s+", " ", tpage),
           tpage[tpage.find("object") - 60:][:110])
other = TestClient(app)
other.get(f"/m/{db.manager_by_key(mate)['token']}")
check_true("the other side sees it as theirs too",
           'id="mine-open"' in other.get("/trade").text)
third = next(k for k in sqp if k not in ("RM", mate))
bystander = TestClient(app)
bystander.get(f"/m/{db.manager_by_key(third)['token']}")
btext = bystander.get("/trade").text
check_true("a bystander still sees it as something to object to",
           "Open to objection" in btext)
check_true("and not as one of theirs", 'id="mine-open"' not in btext)

print("\n── Two windows: waivers, then free agency ──────────────")

# The week has two halves and they behave differently, so the tests wind the
# clock rather than waiting for it. Everything here is restored afterwards.
gw_two = engine.current_gameweek()["gameweek"]
real_waiver_deadline = engine.waiver_deadline


def wind_past_the_run():
    _shut_window()


state = engine.waiver_state(engine.current_gameweek())
check("before it, the waiver window is what's open", state["phase"], "waivers")
check_true("and it shuts a day before the gameweek does",
           state["waiver_deadline"] < state["deadline"],
           f"{state['waiver_deadline']} vs {state['deadline']}")

wv2 = TestClient(app)
wv2.get(f"/m/{db.manager_by_key('RM')['token']}")
before = engine.market(gw_two, db.trades(), db.transactions(), for_manager="RM")
drop_a = next(p for p in before["squads"]["RM"] if p["position"] == "FWD")
add_a = next(p for p in before["pool"] if p["position"] == "FWD")
check("a claim can be lodged while the window is open",
      wv2.post(f"/waivers/{gw_two}", data={
          "claims": f"{drop_a['id']}:{add_a['id']}"}).status_code, 200)
check_true("but nothing has moved yet — a claim is not a transfer",
           any(p["id"] == drop_a["id"] for p in
               engine.market(gw_two, db.trades(), db.transactions())["squads"]["RM"]))
check_true("and taking someone directly is refused until the run has happened",
           wv2.post(f"/waivers/{gw_two}/take",
                    data={"drop": drop_a["id"], "add": add_a["id"]}
                    ).status_code == 409)

# A claim is a blind bid: yours is on your page, nobody else's is anywhere.
nosy = TestClient(app)
nosy_key = next(k for k in before["squads"] if k != "RM")
nosy.get(f"/m/{db.manager_by_key(nosy_key)['token']}")
spy = nosy.get("/waivers").text
check_true("your own claim is on your own page",
           json.loads(re.search(r'id="claims-data"[^>]*>(.*?)</script>',
                                wv2.get("/waivers").text, re.S).group(1)))
# The free agent alone proves nothing — he is in everybody's pool by
# definition. What must not travel is the PAIR: whose claim it is, and the man
# they are giving up for him. That player is in RM's squad and has no business
# on anyone else's page at all.
check_true("but nobody else's claims are on yours",
           json.loads(re.search(r'id="claims-data"[^>]*>(.*?)</script>',
                                spy, re.S).group(1)) == [])
check_true("and the man they would drop for him never leaves their own page",
           drop_a["name"] not in spy)
check_true("nor does a resolved run anyone could read it out of",
           "If the run happened now" not in spy and "provisional" not in spy)
check_true("the page says why it is empty rather than looking broken",
           "Nobody sees anyone else" in spy)

# The rules are folded away behind an icon rather than deleted: still served,
# still readable without scripting, just not eating half the page.
check_true("the explainer is behind an icon",
           'class="infobtn"' in spy and 'aria-controls="howrun"' in spy)
check_true("shut to begin with",
           re.search(r'<span class="infobox" id="howrun"[^>]*\shidden>', spy)
           is not None)
check_true("but the words are still on the page for anyone without scripting",
           "processed together at the waiver deadline" in spy)
check_true("and there is a rule that shows them when there is no scripting",
           "<noscript>" in spy and ".infobtn { display: none; }" in spy)

# Every page that folds rules away, checked the same way: an icon that opens
# nothing is worse than the paragraph it replaced, and two boxes sharing an id
# would have the button open the wrong one.
for path in ("/waivers", "/trade", "/declare", "/stats"):
    page = wv2.get(path)
    if page.status_code != 200:
        continue
    opens = re.findall(r'aria-controls="([^"]+)"', page.text)
    boxes = re.findall(r'<span class="infobox" id="([^"]+)"', page.text)
    check(f"{path}: every icon opens a box that is there",
          sorted(opens), sorted(boxes))
    check(f"{path}: and no two boxes share an id", len(boxes), len(set(boxes)))
    check_true(f"{path}: every box ships shut",
               all(re.search(r'id="%s"[^>]*\shidden>' % b, page.text) for b in boxes),
               ", ".join(b for b in boxes
                         if not re.search(r'id="%s"[^>]*\shidden>' % b, page.text)))
    check_true(f"{path}: and every icon says what it opens",
               'aria-label=""' not in page.text
               and page.text.count('class="infobtn"') == len(boxes))

wind_past_the_run()
after = engine.market(gw_two, db.trades(), db.transactions(), for_manager="RM")
check("now the free window is open", after["state"]["phase"], "free_agency")
check_true("the run settled the claim", any(
    m["landed"] and m["team"] == "RM" and m["add"]["id"] == add_a["id"]
    for m in after["moves"]), str(after["moves"]))
check_true("so the squad has actually changed",
           any(p["id"] == add_a["id"] for p in after["squads"]["RM"]))
check_true("and the pick-team page draws the man they won, not the man they lost",
           str(add_a["id"]) in wv2.get("/declare").text
           and f'"id": {drop_a["id"]},' not in wv2.get("/declare").text)
check_true("claims are no longer taken",
           wv2.post(f"/waivers/{gw_two}", data={
               "claims": f"{drop_a['id']}:{add_a['id']}"}).status_code == 409)

# The freeze, which is the point of the whole exercise.
mate = TestClient(app)
other_key = next(k for k in after["squads"] if k != "RM")
mate.get(f"/m/{db.manager_by_key(other_key)['token']}")
theirs = engine.market(gw_two, db.trades(), db.transactions(),
                       for_manager=other_key)
their_fwd = next(p for p in theirs["squads"][other_key] if p["position"] == "FWD")
grab = mate.post(f"/waivers/{gw_two}/take",
                 data={"drop": their_fwd["id"], "add": drop_a["id"]})
check("a player the run dropped is out of reach", grab.status_code, 422)
check_true("and is told so plainly",
           "nobody else can pick him up" in grab.json()["errors"][0],
           str(grab.json()))
check_true("the pool they see is short by exactly him",
           not any(p["id"] == drop_a["id"] for p in theirs["pool"]))
check_true("but the manager who let him go still sees him",
           any(p["id"] == drop_a["id"] for p in after["pool"]))
check_true("and the page says who is out of reach and why",
           "Out of reach this week" in mate.get("/waivers").text)

# Free agency proper: first come, first served, as often as you like.
free_fwd = [p for p in theirs["pool"] if p["position"] == "FWD"]
took = mate.post(f"/waivers/{gw_two}/take",
                 data={"drop": their_fwd["id"], "add": free_fwd[0]["id"]})
check("a free agent can be taken there and then", took.status_code, 200)
now2 = engine.market(gw_two, db.trades(), db.transactions(), for_manager=other_key)
check_true("and lands immediately rather than joining a queue",
           any(p["id"] == free_fwd[0]["id"] for p in now2["squads"][other_key]))
second = mate.post(f"/waivers/{gw_two}/take",
                   data={"drop": free_fwd[0]["id"], "add": free_fwd[1]["id"]})
check("there is no limit on how many", second.status_code, 200)
check_true("what free agency drops is frozen too",
           mate.post(f"/waivers/{gw_two}/take",
                     data={"drop": free_fwd[1]["id"], "add": their_fwd["id"]}
                     ).status_code == 200,
           "the manager who dropped him may take him back")
racer = TestClient(app)
third_key = next(k for k in after["squads"] if k not in ("RM", other_key))
racer.get(f"/m/{db.manager_by_key(third_key)['token']}")
their_own = next(p for p in engine.market(
    gw_two, db.trades(), db.transactions())["squads"][third_key]
    if p["position"] == "FWD")
check_true("but nobody else may",
           racer.post(f"/waivers/{gw_two}/take",
                      data={"drop": their_own["id"], "add": free_fwd[1]["id"]}
                      ).status_code == 422)

check_true("a swap that breaks the squad shape is refused either way",
           mate.post(f"/waivers/{gw_two}/take", data={
               "drop": their_own["id"],
               "add": next(p for p in now2["pool"] if p["position"] == "GK")["id"]}
               ).status_code == 422)

# Put the world back: later readers of this file should not inherit a
# half-transferred league, and the clock belongs to the real season.
engine.waiver_deadline = real_waiver_deadline
for row in db.free_agent_moves(gameweek=gw_two):
    db.undo_free_agent(row["id"])
db.withdraw("RM", gw_two, "waiver")
check("and the league is left as it was found",
      len(db.free_agent_moves(gameweek=gw_two)), 0)

print("\n── Refreshes actually reaching the page ────────────────")

# The bug this pins: the cache fingerprint watched the gameweek files but not
# players.json, which between rounds is the ONLY file a refresh rewrites. So
# the fetch ran, the disk updated, and the app went on serving the injury news
# it had booted with until something restarted it.
watched = set(engine.WATCHED)
check_true("every file a refresh writes is watched for changes",
           {"players.json", "pl_fixtures.json"} <= watched,
           f"missing {sorted({'players.json', 'pl_fixtures.json'} - watched)}")

players_file = engine.DATA / "players.json"
kept = players_file.read_text()
try:
    before_flags = len(engine.availability() or {})
    before_version = engine.data_version()
    blob = json.loads(kept)
    blob.setdefault("availability", {})["999999"] = {
        "status": "i", "chance": 0, "news": "Broken leg"}
    players_file.write_text(json.dumps(blob, separators=(",", ":")))

    check_true("a write to players.json moves the fingerprint",
               engine.data_version() != before_version)
    check_true("so new injury news reaches the app without a restart",
               engine.availability(999999) is not None)
    check("and the old flags are still there too",
          len(engine.availability() or {}), before_flags + 1)
finally:
    players_file.write_text(kept)
check_true("and it goes again when the file does",
           engine.availability(999999) is None)
# This section writes to a file that is committed, so say out loud that it put
# it back. Leaving an "availability" key behind where there was none is the
# difference between "nobody is hurt" and "we have never asked".
check("and the committed file is left exactly as it was found",
      players_file.read_text(), kept)

# Whether a refresh is due, and whether anything is going to run it, are
# separate questions and /health answers both.
age = engine.data_age()
check_true("the data says when it was last written", bool(age and age["written_at"]))
check_true("and how old that makes it", isinstance(age["hours_ago"], float))
hp = client.get("/health").json()
check_true("health reports the refresh schedule", "scheduled" in hp["refresh"])
sched = hp["refresh"]["scheduled"]
check("and says plainly when nothing is scheduled", sched["running"], False)
# The point is that a dead scheduler is never silent. Which reason it gives
# depends on how the app was started, so the test pins that there IS one
# rather than which — a stopped scheduler and a disabled one both need saying.
check_true("and always says why, rather than leaving it to be guessed at",
           isinstance(sched.get("why"), str) and len(sched["why"]) > 10,
           str(sched))
check_true("health carries the data's age", "written" in hp["data"])

print("\n── Which rounds a refresh brings in ────────────────────")

# The rule the daily fetch runs on. Two things have to hold: a round being
# played is worth having before it ends, and a round that has settled must
# never move again.
import fetch_gw                                             # noqa: E402
import tempfile                                             # noqa: E402

LIVE = {2: {"finished": False, "data_checked": False}}
DONE = {2: {"finished": True, "data_checked": False}}
FINAL = {2: {"finished": True, "data_checked": True}}
missing = Path(tempfile.gettempdir()) / "no-such-gameweek-file.json"
if missing.exists():
    missing.unlink()


def saved(**flags):
    """A gameweek file already on disk, in whatever state."""
    f = Path(tempfile.mkdtemp()) / "gw02.json"
    f.write_text(json.dumps({"gameweek": 2, **flags}))
    return f


check("a round in progress is fetched, not waited on",
      fetch_gw.should_fetch(2, LIVE, missing)[0], True)
check("a finished round nobody has saved is fetched",
      fetch_gw.should_fetch(2, DONE, missing)[0], True)
check("a round that hasn't kicked off is left alone",
      fetch_gw.should_fetch(9, LIVE, missing)[0], False)

# The one that matters: results stay put once they are final.
check("a saved round FPL calls final is never pulled again",
      fetch_gw.should_fetch(2, FINAL, saved(data_checked=True))[0], False)
check_true("and the log says why",
           "final" in fetch_gw.should_fetch(2, FINAL, saved(data_checked=True))[1])
check("but one that is finished and not yet final still is — bonus moves",
      fetch_gw.should_fetch(2, DONE, saved(data_checked=False))[0], True)
check("and one still being played is re-pulled for the latest scores",
      fetch_gw.should_fetch(2, LIVE, saved(data_checked=False))[0], True)
check("--refetch overrides even a final round, since that is what it is for",
      fetch_gw.should_fetch(2, FINAL, saved(data_checked=True), refetch=True)[0], True)
corrupt = Path(tempfile.mkdtemp()) / "gw02.json"
corrupt.write_text("{ not json")
check("a corrupt saved file is replaced rather than trusted",
      fetch_gw.should_fetch(2, DONE, corrupt)[0], True)

# The committed data is the floor the deployed app falls back to on every
# redeploy, so how often it is refreshed IS how stale the app can get.
wf = Path(__file__).resolve().parents[1] / ".github/workflows/shadow-fetch-gw.yml"
cron = re.search(r'cron:\s*"([^"]+)"', wf.read_text()).group(1)
check("the committed data is refreshed daily, not twice a week", cron, "30 7 * * *")

print("\n── Live scores ─────────────────────────────────────────")

import time as _time                                        # noqa: E402
from app import live                                        # noqa: E402

# The round on the television is not the round you are picking for: on a
# Saturday afternoon those are different weeks, and this page wants the former.
lg, cg = engine.live_gameweek(), engine.current_gameweek()
check_true("the round being played is the last one whose deadline went by",
           lg["gameweek"] <= cg["gameweek"], f"{lg['gameweek']} vs {cg['gameweek']}")

# A match in the exact shape FPL returns, verified against the real API.
live_gw = lg["gameweek"]
lsquads = engine.market(live_gw, db.trades(), db.transactions())["squads"]
rm_ids = [p["id"] for p in lsquads["RM"]]
ch_ids = [p["id"] for p in lsquads["CH"]]
scorer, helper, sent_off = rm_ids[10], rm_ids[11], ch_ids[3]


def _stat(kind, h=(), a=()):
    return {"identifier": kind,
            "h": [{"value": v, "element": e} for e, v in h],
            "a": [{"value": v, "element": e} for e, v in a]}


def _fixture(bonus=()):
    return [{
        "id": 901, "event": live_gw, "started": True, "finished": False,
        "minutes": 67, "kickoff_time": "2026-08-29T14:00:00Z",
        "team_h": 1, "team_a": 6, "team_h_score": 2, "team_a_score": 1,
        "stats": [
            _stat("goals_scored", h=[(scorer, 2)]),
            _stat("assists", h=[(helper, 1)]),
            _stat("red_cards", a=[(sent_off, 1)]),
            # Two men level on 28 behind the scorer: they take second and
            # third between them and the single point goes unawarded.
            _stat("bps", h=[(scorer, 44), (helper, 28)], a=[(sent_off, 28)]),
            _stat("bonus", h=bonus),
        ],
    }]


real_fetch = live.fetch
live.fetch = lambda gw, force=False: (_fixture() if gw == live_gw else [], None)
b = live.board(live_gw, squads=lsquads, me="RM")
m = b["matches"][0]

check("the round is in play", b["in_play"], True)
check("a brace is two lines, not one",
      sum(1 for e in m["events"] if e["kind"] == "goal"), 2)
check("the sending off is there too",
      [e["label"] for e in m["events"] if e["kind"] == "red"], ["Red card"])
check_true("every name says who owns it",
           all(e["owners"] for e in m["events"]), str(m["events"][:1]))
check_true("and the reader's own are marked out",
           all(e["mine"] for e in m["events"] if e["kind"] in ("goal", "assist"))
           and not any(e["mine"] for e in m["events"] if e["kind"] == "red"))
check("the match lists which of yours are in it", m["mine"],
       sorted({engine.player_names()[scorer], engine.player_names()[helper]}))

check("bonus is provisional while the match is on", m["bonus_settled"], False)
check("and follows the rule: 3, then two men sharing second",
      [(x["id"], x["points"]) for x in m["bonus"]],
      [(scorer, 3), (helper, 2), (sent_off, 2)])

# Once FPL settles it, their figure is what shows.
live.fetch = lambda gw, force=False: (
    _fixture(bonus=[(scorer, 3), (helper, 1)]) if gw == live_gw else [], None)
settled_board = live.board(live_gw, squads=lsquads, me="RM")["matches"][0]
check("once confirmed, FPL's own award is used", settled_board["bonus_settled"], True)
check("even where it differs from ours",
      [(x["id"], x["points"]) for x in settled_board["bonus"]],
      [(scorer, 3), (helper, 1)])

# A page that goes blank mid-match is worse than one that is a minute behind.
live._cache[live_gw] = (_time.time(), _fixture())
live.fetch = real_fetch
stale_fixtures, why = live.fetch(live_gw)
check_true("a cached round is served without going out again", bool(stale_fixtures))

live.fetch = lambda gw, force=False: ([], "URLError: unreachable")
broke = live.board(live_gw, squads=lsquads, me="RM")
check("an unreachable FPL is reported rather than hidden",
      bool(broke["error"]), True)
check("and the page still renders", broke["matches"], [])

live.fetch = lambda gw, force=False: (_fixture() if gw == live_gw else [], None)
page = wv2.get("/live")
check("the live page renders", page.status_code, 200)
check_true("with the match on it", "2" in page.text and "67" in page.text)
check_true("and says plainly that bonus is provisional",
           "provisional" in page.text.lower())
check_true("it is reachable by gameweek too",
           wv2.get(f"/live/{live_gw}").status_code == 200)
frag = wv2.get(f"/live/{live_gw}/board")
check("the polled fragment is served on its own", frag.status_code, 200)
check_true("and is a fragment, not a whole page",
           "<html" not in frag.text and "games" in frag.text)
live.fetch = real_fetch

print("\n── The chrome ──────────────────────────────────────────")

# The bar and the tab rail are the only navigation there is now, so they get
# the same treatment as anything else that would strand a manager if it broke.
sheet = open("app/static/style.css").read()

bar = wv.get("/").text
check_true("the bar carries the crest", 'src="/static/logo.png' in bar)
check("and the crest is actually served", wv.get("/static/logo.png").status_code, 200)
check_true("the manager's initials sit next to their team",
           '<span class="av">RM</span>' in bar)
check_true("and the team name with them", "Quantum of Szobos" in bar)

# The current section, including the pages that belong to one without being it.
def tab_on(html):
    return re.findall(r'<a class="tab on" href="([^"]+)"', html)

check("the table page marks the table tab", tab_on(bar), ["/"])
check("a team's gameweek belongs to the table too",
      tab_on(wv.get("/team/RM").text), ["/"])
gw_now = engine.season()["rounds"][0]["gameweek"]
check("and so does a round",
      tab_on(wv.get(f"/gameweek/{gw_now}").text), ["/"])
check("the waivers page marks its own", tab_on(wv.get("/waivers").text), ["/waivers"])
check("and the trades page marks its own", tab_on(wv.get("/trade").text), ["/trade"])
rail_hrefs = re.findall(r'<a class="tab[^"]*" href="([^"]+)"', bar)
check("the rail carries every section", rail_hrefs,
      ["/", "/live", "/stats", "/declare", "/trade", "/waivers"])
check_true("and every one of them opens",
           all(wv.get(h).status_code == 200 for h in rail_hrefs),
           ", ".join(f"{h} -> {wv.get(h).status_code}" for h in rail_hrefs
                     if wv.get(h).status_code != 200))

# Admin is a section, not a secret: it is only on the rail for an admin, and
# the page itself is what actually stops anyone else.
check_true("a manager who is not an admin has no admin tab", ">Admin<" not in bar)
os.environ["ADMIN_KEYS"] = "RM"
admin_bar = wv.get("/").text
check_true("an admin does", ">Admin<" in admin_bar)
check("and it is the last of them",
      re.findall(r'<a class="tab[^"]*" href="([^"]+)"', admin_bar)[-1], "/admin")
check("the admin page marks its own tab",
      re.findall(r'<a class="tab on" href="([^"]+)"', wv.get("/admin").text), ["/admin"])
os.environ.pop("ADMIN_KEYS", None)
check_true("and the tab goes with the rights", ">Admin<" not in wv.get("/").text)
check_true("a signed-out visitor has no rail at all",
           '<nav class="rail"' not in client.get("/").text)

# The thing that was actually asked for: light unless the device says dark.
light = sheet[:sheet.find("@media (prefers-color-scheme: dark)")]
check_true("the light palette is defined on a bare :root, so it is the fallback",
           "--bg: #F3F5EF" in light and "--accent: #3E6B12" in light)
check_true("dark is only reached by the device asking for it",
           "@media (prefers-color-scheme: dark)" in sheet
           and ':root:not([data-theme="light"])' in sheet)
check_true("or by the toggle asking for it",
           ':root[data-theme="dark"]' in sheet)
check_true("and the toggle can still be handed back to the device",
           'removeItem("matchweek-theme")' in bar)

# Throwing the toggle swaps one block of tokens for another. Anything the
# light block names and the dark block forgets keeps its light value against a
# dark ground, which is how you get black text on a black panel — so the two
# have to name exactly the same things.
def token_names(block):
    return set(re.findall(r"(--[a-z0-9-]+):", block))

dark = sheet[sheet.find(':root[data-theme="dark"]'):]
dark = dark[:dark.find("\n}")]
chrome = {"--chrome", "--chrome-2", "--chrome-3", "--chrome-ink",
          "--chrome-muted", "--chrome-line", "--fill", "--on-fill",
          "--display", "--body", "--mono"}
missing = (token_names(light) - chrome) - token_names(dark)
check_true("dark redefines every token light sets, bar the ones that never move",
           not missing, ", ".join(sorted(missing)))
check_true("and the media-query block agrees with the toggle",
           token_names(dark) == token_names(
               sheet[sheet.find('@media (prefers-color-scheme: dark)'):
                     sheet.find(':root[data-theme="dark"]')]))

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
    sys.exit(1)
print("ALL APP TESTS PASSED")
