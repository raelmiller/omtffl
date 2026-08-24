#!/usr/bin/env python3
"""Unit tests for the league mechanics.

These pin down the rules with worked examples, including the ones that are
easy to get wrong: a trade that would break a squad's shape, spending more
than is banked, boosting a club that didn't play, and the boost scale's ends.

Run: python3 shadow/test_mechanics.py
"""
import sys

from mechanics import (
    BOOST_MAX_PCT, BOOST_MIN_PCT, BOOST_USES_PER_SEASON,
    apply_transactions, boost_pct, boost_value, club_result, league_table,
    process_waivers, snake_order, validate_trade,
)

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


def p(pid, pos, name=None):
    return {"id": pid, "position": pos, "name": name or f"P{pid}"}


def base(a_squad, b_squad):
    return {"teams": [{"key": "A", "team": "A", "squad": a_squad},
                      {"key": "B", "team": "B", "squad": b_squad}]}


A = [p(1, "FWD", "Solanke"), p(2, "MID"), p(3, "DEF")]
B = [p(10, "FWD", "Haaland"), p(11, "MID"), p(12, "DEF")]

print("── Trades ──────────────────────────────────────────────")

trade = {"type": "trade", "gameweek": 5, "from": "A", "to": "B",
         "players_out": [p(1, "FWD")], "players_in": [p(10, "FWD")], "points": 40}
squads, adj, boosts, bank, _, problems = apply_transactions(base(A, B), [trade], 10)

check("A receives Haaland", sorted(x["id"] for x in squads["A"]), [2, 3, 10])
check("B receives Solanke", sorted(x["id"] for x in squads["B"]), [1, 11, 12])
check("A is docked the points that gameweek", adj[5]["A"], -40)
check("the points land in B's bank, not their score", bank["B"], 40)
check_true("B's gameweek score is untouched by the trade", 5 not in adj or "B" not in adj.get(5, {}))
check("no problems", problems, [])

# A cross-position trade would leave both squads illegal
bad = {"type": "trade", "gameweek": 5, "from": "A", "to": "B",
       "players_out": [p(1, "FWD")], "players_in": [p(11, "MID")], "points": 0}
check_true("cross-position trade rejected",
           "positions don't balance" in (validate_trade(bad, {"A": A, "B": B}) or ""),
           validate_trade(bad, {"A": A, "B": B}))

# Trading a player you don't own
notmine = {"type": "trade", "gameweek": 5, "from": "A", "to": "B",
           "players_out": [p(99, "FWD")], "players_in": [p(10, "FWD")], "points": 0}
check_true("can't trade a player you don't own",
           "doesn't own" in (validate_trade(notmine, {"A": A, "B": B}) or ""))

# Points are mortgaged, not spent from the bank — A had nothing banked
check_true("points can be offered without a bank balance", bank["A"] == 0 and adj[5]["A"] == -40)

print("\n── Points bank ─────────────────────────────────────────")

spend = [trade, {"type": "bank_use", "gameweek": 9, "team": "B", "points": 25}]
_, adj, _, bank, _, problems = apply_transactions(base(A, B), spend, 10)
check("bank credit is spendable later", adj[9]["B"], 25)
check("bank reduces by what was spent", bank["B"], 15)
check("spending within balance is fine", problems, [])

over = [trade, {"type": "bank_use", "gameweek": 9, "team": "B", "points": 100}]
_, adj, _, bank, _, problems = apply_transactions(base(A, B), over, 10)
check_true("can't spend more than is banked", any("only 40 banked" in x for x in problems), str(problems))
check("bank untouched by a rejected spend", bank["B"], 40)
check_true("no points credited by a rejected spend", 9 not in adj)

# Bank persists across gameweeks and can be spent in pieces
split = [trade,
         {"type": "bank_use", "gameweek": 7, "team": "B", "points": 10},
         {"type": "bank_use", "gameweek": 12, "team": "B", "points": 30}]
_, adj, _, bank, _, problems = apply_transactions(base(A, B), split, 20)
check("bank can be spent in pieces", (adj[7]["B"], adj[12]["B"]), (10, 30))
check("bank empties exactly", bank["B"], 0)

print("\n── Waivers ─────────────────────────────────────────────")

wv = [{"type": "waiver", "gameweek": 4, "team": "A",
       "drop": p(2, "MID"), "add": p(50, "MID", "NewMid")}]
squads, _, _, _, _, problems = apply_transactions(base(A, B), wv, 10)
check("waiver swaps the player in", sorted(x["id"] for x in squads["A"]), [1, 3, 50])
check("waiver accepted", problems, [])

wv_bad = [{"type": "waiver", "gameweek": 4, "team": "A",
           "drop": p(2, "MID"), "add": p(51, "FWD", "NewFwd")}]
_, _, _, _, _, problems = apply_transactions(base(A, B), wv_bad, 10)
check_true("waiver across positions rejected",
           any("break the squad shape" in x for x in problems), str(problems))

wv_owned = [{"type": "waiver", "gameweek": 4, "team": "A",
             "drop": p(2, "MID"), "add": p(11, "MID")}]
_, _, _, _, _, problems = apply_transactions(base(A, B), wv_owned, 10)
check_true("can't waiver in a player someone owns",
           any("already owned" in x for x in problems), str(problems))

print("\n── Offer cap ───────────────────────────────────────────")

# You can mortgage your season, but not more than you've actually scored.
cap_trade = dict(trade)
check_true("offering more than you've scored is rejected",
           "can't offer more than you've accumulated"
           in (validate_trade(cap_trade, {"A": A, "B": B}, accumulated=39) or ""),
           str(validate_trade(cap_trade, {"A": A, "B": B}, accumulated=39)))
check("offering exactly what you've scored is allowed",
      validate_trade(cap_trade, {"A": A, "B": B}, accumulated=40), None)
check("offering less is allowed",
      validate_trade(cap_trade, {"A": A, "B": B}, accumulated=200), None)
check_true("with nothing scored, no points can be offered at all",
           validate_trade(cap_trade, {"A": A, "B": B}, accumulated=0) is not None)
check("without a season total the cap isn't guessed at",
      validate_trade(cap_trade, {"A": A, "B": B}), None)

# End to end: the same trade passes or fails on the season total alone.
_, adj, _, bank, _, problems = apply_transactions(
    base(A, B), [trade], 10, points_to_date={5: {"A": 100}})
check("a covered offer goes through", problems, [])
check("and the points move", bank["B"], 40)

_, adj, _, bank, _, problems = apply_transactions(
    base(A, B), [trade], 10, points_to_date={5: {"A": 10}})
check_true("an uncovered offer is blocked", len(problems) == 1, str(problems))
check("no points move", bank["B"], 0)
check_true("and the players stay put", 5 not in adj)

# A gameweek can go negative, but a season can't: two offers in one week
# that jointly exceed the season total must not both go through.
two = [dict(trade, points=30),
       dict(trade, gameweek=5, points=30, players_out=[p(2, "MID")],
            players_in=[p(11, "MID")])]
_, _, _, bank, _, problems = apply_transactions(
    base(A, B), two, 10, points_to_date={5: {"A": 50}})
check_true("a second offer can't take the season below zero",
           len(problems) == 1, str(problems))
check("only the affordable offer lands", bank["B"], 30)

print("\n── Waiver priority ─────────────────────────────────────")

# Standings best-first; round one runs bottom-up, round two back down.
TABLE = ["A", "B", "C", "D"]
rounds = snake_order(TABLE, 3)
check("round one starts with the bottom club", rounds[0], ["D", "C", "B", "A"])
check("round two turns around", rounds[1], ["A", "B", "C", "D"])
check("round three turns back", rounds[2], ["D", "C", "B", "A"])

FREE_1 = p(70, "MID", "Wanted")
FREE_2 = p(71, "MID", "Backup")


def four_teams():
    return {k: [p(100 + i, "MID", f"{k}mid{i}") for i in range(2)]
            for k in ("A", "B", "C", "D")}


squads4 = four_teams()
claims = {
    "A": [{"drop": squads4["A"][0], "add": FREE_1}],
    "D": [{"drop": squads4["D"][0], "add": FREE_1},
          {"drop": squads4["D"][1], "add": FREE_2}],
}
results, problems = process_waivers(claims, squads4, TABLE)
first_round = {r["team"]: r["add"]["name"]
               for r in results if r["landed"] and r["round"] == 1}
check("the bottom club wins the contested claim", first_round.get("D"), "Wanted")
check_true("and the top club doesn't get him", "A" not in first_round, str(first_round))
check_true("the top club is told it lost the race",
           any(r["team"] == "A" and not r["landed"] and r["why"] == "already claimed"
               for r in results), str(results))
check("losing a race isn't an error", problems, [])
check_true("the winner's squad actually changes",
           FREE_1["id"] in {x["id"] for x in squads4["D"]})

# Losing a race costs you the round. Your second choice waits for the snake
# to come back to you — it is not taken off the rank immediately.
squads4 = four_teams()
claims = {
    "D": [{"drop": squads4["D"][0], "add": FREE_1}],
    "A": [{"drop": squads4["A"][0], "add": FREE_1},
          {"drop": squads4["A"][0], "add": FREE_2}],
}
results, problems = process_waivers(claims, squads4, TABLE)
a_landed = [r for r in results if r["team"] == "A" and r["landed"]]
check_true("a beaten manager gets nothing in the round they lost",
           not any(r["round"] == 1 for r in a_landed), str(results))
check("their next choice waits for the snake to come back", a_landed[0]["round"], 2)
check("and then it lands", a_landed[0]["add"]["name"], "Backup")
check("still no error", problems, [])

# The bottom club can't sweep the list in one pass: one claim per round each.
squads4 = four_teams()
FREE_3 = p(73, "MID", "Third")
claims = {"D": [{"drop": squads4["D"][0], "add": FREE_1},
                {"drop": squads4["D"][1], "add": FREE_3}],
          "A": [{"drop": squads4["A"][0], "add": FREE_3}]}
results, _ = process_waivers(claims, squads4, TABLE)
round_one = [r for r in results if r["round"] == 1 and r["landed"]]
check("only one claim lands per team per round", len(round_one), 2)
check_true("and the top club gets the round-two turn first",
           {x["id"] for x in squads4["A"]} & {FREE_3["id"]} != set(),
           "A should have taken Third in round one")

# Naming the same drop against several claims is normal, not illegal.
squads4 = four_teams()
claims = {"D": [{"drop": squads4["D"][0], "add": FREE_1},
                {"drop": squads4["D"][0], "add": FREE_2}]}
results, problems = process_waivers(claims, squads4, TABLE)
check("only one claim lands per drop", sum(1 for r in results if r["landed"]), 1)
check("reusing a spent drop is not an error", problems, [])
check_true("it's reported as spent",
           any(r["why"] == "already used that drop" for r in results), str(results))

# A claim that would break the squad shape is refused.
squads4 = four_teams()
claims = {"D": [{"drop": squads4["D"][0], "add": p(72, "FWD", "WrongPos")}]}
results, problems = process_waivers(claims, squads4, TABLE)
check_true("a cross-position claim is refused",
           any("break the squad shape" in x for x in problems), str(problems))

print("\n── Boost limits ────────────────────────────────────────")

many = [{"type": "boost", "gameweek": g, "team": "A"} for g in (2, 3, 4, 5)]
_, _, boost_log, _, _, problems = apply_transactions(base(A, B), many, 38)
check(f"only {BOOST_USES_PER_SEASON} boosts allowed", len(boost_log), BOOST_USES_PER_SEASON)
check_true("the fourth is rejected", any("already used all" in x for x in problems), str(problems))

print("\n── Sacked managers ─────────────────────────────────────")

# Drafting a manager under pressure is the gamble: if they go, the remaining
# boosts go with them.
MANAGERS = {"A": {"name": "Fictional Boss", "club": 1, "sacked_from": 6}}
uses = [{"type": "boost", "gameweek": g, "team": "A"} for g in (3, 6, 9)]
_, _, boost_log, _, _, problems = apply_transactions(
    base(A, B), uses, 38, managers=MANAGERS)
check("boosts before the sacking still count", len(boost_log), 1)
check("the surviving one is the early one", boost_log[0]["gameweek"], 3)
check_true("boosts from the sacking gameweek on are refused",
           sum(1 for x in problems if "sacked" in x) == 2, str(problems))
check_true("and the manager is named", any("Fictional Boss" in x for x in problems))

# Without a sacking on record nothing changes.
_, _, boost_log, _, _, problems = apply_transactions(base(A, B), uses, 38)
check("an unsacked manager keeps all three", len(boost_log), 3)

print("\n── Boost scale ─────────────────────────────────────────")

check("top of the table gets the smallest boost", boost_pct(1), BOOST_MIN_PCT)
check("bottom gets the largest", boost_pct(20), BOOST_MAX_PCT)
check_true("mid-table sits in between", BOOST_MIN_PCT < boost_pct(10) < BOOST_MAX_PCT,
           f"10th = {boost_pct(10):.1f}%")
check_true("scale is monotonic", all(boost_pct(i) < boost_pct(i + 1) for i in range(1, 20)))

print("\n── Boost payout ────────────────────────────────────────")

# Club 1 beat club 2 in GW1; club 3 drew with club 4.
FIX = [
    {"event": 1, "finished": True, "team_h": 1, "team_a": 2, "team_h_score": 2, "team_a_score": 0},
    {"event": 1, "finished": True, "team_h": 3, "team_a": 4, "team_h_score": 1, "team_a_score": 1},
    {"event": 2, "finished": True, "team_h": 2, "team_a": 1, "team_h_score": 3, "team_a_score": 0},
]
check("win in GW1", club_result(FIX, 1, 1), ["W"])
check("defeat in GW1", club_result(FIX, 2, 1), ["L"])
check("draw in GW1", club_result(FIX, 3, 1), ["D"])
check("no fixture returns None", club_result(FIX, 99, 1), None)

# After GW1, club 1 leads on 3 points; club 2 is bottom on 0.
table = league_table(FIX, upto_gameweek=2)
check_true("table built from results", table.get(1) == 1, f"table={table}")

# A boost on the winning club in GW2 (they lost) pays nothing
pts, detail = boost_value(100, club_id=1, gameweek=2, pl_fixtures=FIX)
check("defeat pays nothing", pts, 0)
check("and is recorded as a defeat", detail["result"], "L")

# A boost on club 2 in GW2 (they won 3-0) pays in full
pts, detail = boost_value(100, club_id=2, gameweek=2, pl_fixtures=FIX)
check_true("a win pays the full percentage", pts > 0, f"{pts} pts at {detail['pct']:.1f}%")
check_true("the struggling club is worth more",
           detail["position"] > 1, f"club 2 sits {detail['position']}")

# A draw pays half
DRAW_FIX = FIX + [{"event": 3, "finished": True, "team_h": 1, "team_a": 2,
                   "team_h_score": 1, "team_a_score": 1}]
full, _ = boost_value(100, 2, 2, DRAW_FIX)
half, d = boost_value(100, 2, 3, DRAW_FIX)
check("a draw pays half the multiplier", d["multiplier"], 0.5)

# No fixture: nothing paid, and the caller is told not to consume a use
pts, detail = boost_value(100, club_id=99, gameweek=1, pl_fixtures=FIX)
check("blank gameweek pays nothing", pts, 0)
check("and flags that the club didn't play", detail["played"], False)

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
    sys.exit(1)
print("ALL MECHANICS TESTS PASSED")
