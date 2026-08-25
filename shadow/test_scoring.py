#!/usr/bin/env python3
"""Unit tests for the scoring rules.

These pin down the rules with hand-worked examples. They're the first line of
defence; validate.py then checks the same engine against FPL's own totals
across every real player in a gameweek, which is the harder test.

Run: python3 shadow/test_scoring.py
"""
import sys
from scoring import (breakdown, entry_breakdown, score_entry,
                     score_player, GK, DEF, MID, FWD)

FAILS = []


def check(name, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {name}: got {got}, want {want}")
    if not ok:
        FAILS.append(name)


def check_true(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}{' — ' + detail if detail else ''}")
    if not cond:
        FAILS.append(name)


def s(**kw):
    """A stat dict with sensible zeros for everything unset."""
    base = dict(
        minutes=0, goals_scored=0, assists=0, clean_sheets=0, goals_conceded=0,
        saves=0, penalties_saved=0, penalties_missed=0, own_goals=0,
        yellow_cards=0, red_cards=0, bonus=0, defensive_contribution=0,
    )
    base.update(kw)
    return base


# ── Appearance ─────────────────────────────────────────────────────────────
check("unused player scores nothing", score_player(s(minutes=0), MID), 0)
check("sub appearance (1-59 mins) = 1", score_player(s(minutes=20), MID), 1)
check("59 minutes is still the short appearance", score_player(s(minutes=59), MID), 1)
check("60 minutes = 2", score_player(s(minutes=60), MID), 2)
check("90 minutes = 2", score_player(s(minutes=90), MID), 2)

# An unused player can't accrue anything else, even if odd data appears
check("no minutes beats any other stat", score_player(s(minutes=0, goals_scored=2, bonus=3), FWD), 0)

# ── Goals by position ──────────────────────────────────────────────────────
check("GK goal = 6 (+2 apps)", score_player(s(minutes=90, goals_scored=1), GK), 8)
check("DEF goal = 6 (+2 apps)", score_player(s(minutes=90, goals_scored=1), DEF), 8)
check("MID goal = 5 (+2 apps)", score_player(s(minutes=90, goals_scored=1), MID), 7)
check("FWD goal = 4 (+2 apps)", score_player(s(minutes=90, goals_scored=1), FWD), 6)
check("two FWD goals = 8 (+2 apps)", score_player(s(minutes=90, goals_scored=2), FWD), 10)

# ── Assists ────────────────────────────────────────────────────────────────
check("assist = 3 for any position", score_player(s(minutes=90, assists=1), FWD), 5)
check("two assists = 6", score_player(s(minutes=90, assists=2), DEF), 8)

# ── Clean sheets ───────────────────────────────────────────────────────────
check("GK clean sheet = 4 (+2 apps)", score_player(s(minutes=90, clean_sheets=1), GK), 6)
check("DEF clean sheet = 4 (+2 apps)", score_player(s(minutes=90, clean_sheets=1), DEF), 6)
check("MID clean sheet = 1 (+2 apps)", score_player(s(minutes=90, clean_sheets=1), MID), 3)
check("FWD clean sheet = 0 (+2 apps)", score_player(s(minutes=90, clean_sheets=1), FWD), 2)
check(
    "clean sheet needs 60 mins — 45 mins gets nothing extra",
    score_player(s(minutes=45, clean_sheets=1), DEF),
    1,
)

# ── Goals conceded ─────────────────────────────────────────────────────────
check("DEF conceding 2 = -1 (+2 apps)", score_player(s(minutes=90, goals_conceded=2), DEF), 1)
check("DEF conceding 3 = -1 (rounds down)", score_player(s(minutes=90, goals_conceded=3), DEF), 1)
check("DEF conceding 4 = -2", score_player(s(minutes=90, goals_conceded=4), DEF), 0)
check("DEF conceding 1 = no deduction", score_player(s(minutes=90, goals_conceded=1), DEF), 2)
check("MID is not docked for concessions", score_player(s(minutes=90, goals_conceded=4), MID), 2)
check("FWD is not docked for concessions", score_player(s(minutes=90, goals_conceded=4), FWD), 2)

# ── Goalkeeping ────────────────────────────────────────────────────────────
check("3 saves = 1 (+2 apps)", score_player(s(minutes=90, saves=3), GK), 3)
check("5 saves = 1 (rounds down)", score_player(s(minutes=90, saves=5), GK), 3)
check("6 saves = 2", score_player(s(minutes=90, saves=6), GK), 4)
check("2 saves = 0 extra", score_player(s(minutes=90, saves=2), GK), 2)
check("penalty save = 5 (+2 apps)", score_player(s(minutes=90, penalties_saved=1), GK), 7)

# ── Misses and discipline ──────────────────────────────────────────────────
check("penalty miss = -2", score_player(s(minutes=90, penalties_missed=1), FWD), 0)
check("own goal = -2", score_player(s(minutes=90, own_goals=1), DEF), 0)
check("yellow = -1", score_player(s(minutes=90, yellow_cards=1), MID), 1)
check("red = -3", score_player(s(minutes=90, red_cards=1), MID), -1)

# ── Bonus ──────────────────────────────────────────────────────────────────
check("bonus is added as given", score_player(s(minutes=90, bonus=3), MID), 5)

# ── Defensive contributions (2025/26+) ─────────────────────────────────────
check(
    "DEF hitting 10 defensive actions = +2",
    score_player(s(minutes=90, defensive_contribution=10), DEF),
    4,
)
check(
    "DEF just short at 9 = no bonus",
    score_player(s(minutes=90, defensive_contribution=9), DEF),
    2,
)
check(
    "MID needs 12, so 10 is not enough",
    score_player(s(minutes=90, defensive_contribution=10), MID),
    2,
)
check(
    "MID hitting 12 = +2",
    score_player(s(minutes=90, defensive_contribution=12), MID),
    4,
)
check(
    "FWD hitting 12 = +2",
    score_player(s(minutes=90, defensive_contribution=12), FWD),
    4,
)
check(
    "GK gets no defensive-contribution bonus",
    score_player(s(minutes=90, defensive_contribution=20), GK),
    2,
)

# Falling back to raw counts when the precomputed field is absent
raw_def = dict(minutes=90, clearances_blocks_interceptions=8, tackles=2, recoveries=5)
check("DEF counts CBI+tackles when no precomputed field", score_player(raw_def, DEF), 4)
raw_mid = dict(minutes=90, clearances_blocks_interceptions=5, tackles=2, recoveries=5)
check("MID also counts recoveries (12 total)", score_player(raw_mid, MID), 4)

# ── A realistic combined line ──────────────────────────────────────────────
# Defender: 90 mins, a goal, a clean sheet, 10 defensive actions, 2 bonus
check(
    "combined defender line",
    score_player(s(minutes=90, goals_scored=1, clean_sheets=1, defensive_contribution=10, bonus=2), DEF),
    2 + 6 + 4 + 2 + 2,
)
# Keeper: 90 mins, clean sheet, 6 saves, penalty save, 3 bonus
check(
    "combined keeper line",
    score_player(s(minutes=90, clean_sheets=1, saves=6, penalties_saved=1, bonus=3), GK),
    2 + 4 + 2 + 5 + 3,
)



print("\n── Found by a full season of real data ────────────────")

# A player can be booked without the clock recording a minute — on the bench,
# or in stoppage time after coming on. The engine used to return a flat 0 for
# anyone on no minutes, which cost this exact case a point.
check("a booking on zero minutes still costs a point",
      score_player({"minutes": 0, "yellow_cards": 1}, FWD), -1)
check("a red card on zero minutes too",
      score_player({"minutes": 0, "red_cards": 1}, MID), -3)
check("but an unused player still scores nothing",
      score_player({"minutes": 0}, DEF), 0)
check("and no appearance point sneaks in",
      score_player({"minutes": 0, "yellow_cards": 1, "bonus": 3}, DEF), -1)

# Double gameweeks: a gameweek's stats are aggregated across both matches,
# so anything counted per match has to be scored per match.
two_nineties = {
    "id": 1,
    "stats": {"minutes": 180, "clean_sheets": 2, "goals_conceded": 0},
    "fixtures": [
        {"minutes": 90, "clean_sheets": 1, "goals_conceded": 0},
        {"minutes": 90, "clean_sheets": 1, "goals_conceded": 0},
    ],
}
check("two clean sheets in a double gameweek score twice",
      score_entry(two_nineties, DEF), 12)          # (2 + 4) x 2
check("scoring the aggregate instead undercounts it — 6 not 12",
      score_player(two_nineties["stats"], DEF), 6)

# A double where the player only featured in one of the two matches.
one_of_two = {
    "id": 2,
    "stats": {"minutes": 90, "goals_scored": 1},
    "fixtures": [
        {"minutes": 90, "goals_scored": 1},
        {"minutes": 0},
    ],
}
check("a blank in the second match adds nothing",
      score_entry(one_of_two, FWD), 6)             # 2 appearance + 4 goal

# Single gameweeks must be untouched by any of this.
single = {"id": 3, "stats": {"minutes": 90, "goals_scored": 1}}
check("an ordinary gameweek is unaffected",
      score_entry(single, FWD), score_player(single["stats"], FWD))

print("\n── Where the points came from ─────────────────────────")

# The breakdown is what a manager is shown; score_player is what their team
# is scored on. They are the same arithmetic, and this is what keeps them so.
CASES = [
    ({"minutes": 90, "clean_sheets": 1, "goals_conceded": 0,
      "defensive_contribution": 11, "yellow_cards": 1}, DEF),
    ({"minutes": 90, "goals_conceded": 4, "saves": 5, "bonus": 1}, GK),
    ({"minutes": 22, "goals_scored": 1, "assists": 2}, MID),
    ({"minutes": 0, "yellow_cards": 1}, FWD),
    ({"minutes": 90, "penalties_missed": 1, "own_goals": 1, "red_cards": 1}, FWD),
]
for stats, pos in CASES:
    rows = breakdown(stats, pos)
    check(f"the rows add up to the score ({stats.get('minutes')}' as {pos})",
          sum(r["points"] for r in rows), score_player(stats, pos))

booked = breakdown({"minutes": 0, "yellow_cards": 1}, FWD)
check("a blank still shows the minutes", booked[0]["what"], "Minutes")
check("and the booking that made it worse", booked[-1]["points"], -1)

keeper = breakdown({"minutes": 90, "goals_conceded": 1, "saves": 2}, GK)
check_true("a nought worth explaining is still a row — one conceded",
           any(r["what"] == "Goals conceded" and r["points"] == 0 for r in keeper))
check_true("and two saves, which is one short of a point",
           any(r["what"] == "Saves" and r["points"] == 0 for r in keeper))

check("a double gameweek is broken down match by match",
      sum(r["points"] for r in entry_breakdown(two_nineties, DEF)),
      score_entry(two_nineties, DEF))
check_true("with each row saying which match it was",
           all("match" in r for r in entry_breakdown(two_nineties, DEF)))
check_true("and an ordinary gameweek saying nothing of the sort",
           not any("match" in r for r in entry_breakdown(single, FWD)))

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
    sys.exit(1)
print("ALL SCORING RULE TESTS PASSED")
