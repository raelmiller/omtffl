#!/usr/bin/env python3
"""Simulate the league mechanics on real data.

Runs a scenario of trades, bank spends, waivers and manager boosts against
the real squads and real gameweek scores, and shows what each does to a
team's total. The point is to find out whether these rules are balanced
before anyone plays a season under them.

Where real results aren't available yet (a gameweek that hasn't finished),
that's stated rather than papered over. `--assume-results` lets a boost be
priced against hypothetical outcomes so the mechanic can be demonstrated
before the season provides real ones — clearly labelled as hypothetical.

Usage
-----
    python3 shadow/simulate.py                        # run data/scenario.json
    python3 shadow/simulate.py path/to/scenario.json
    python3 shadow/simulate.py --assume-results W     # price boosts as if won
"""
import json
import sys
from pathlib import Path

from mechanics import (
    BOOST_RESULT, BOOST_USES_PER_SEASON, apply_transactions, boost_pct,
    boost_value, league_table,
)
from score_league import best_xi, load_positions
from scoring import score_player

DATA = Path(__file__).resolve().parent / "data"


def load(name, required=True):
    p = DATA / name
    if not p.exists():
        if required:
            sys.exit(f"No data/{name}")
        return None
    return json.loads(p.read_text())


def player_points(gw_file, positions):
    gw = json.loads(gw_file.read_text())
    pts = {}
    for el in gw["elements"]:
        pos = positions.get(el["id"])
        if pos is not None:
            pts[el["id"]] = score_player(el.get("stats", {}), pos)
    return gw, pts


def main():
    argv = sys.argv[1:]
    assume = None
    if "--assume-results" in argv:
        i = argv.index("--assume-results")
        assume = argv[i + 1] if i + 1 < len(argv) else "W"
        if assume not in BOOST_RESULT:
            sys.exit(f"--assume-results must be one of {sorted(BOOST_RESULT)}")
        # Drop the flag and the value it consumed, so neither is mistaken for
        # a scenario path.
        argv = argv[:i] + argv[i + 2:]
    args = [a for a in argv if not a.startswith("-")]

    scenario_path = Path(args[0]) if args else DATA / "scenario.json"
    if not scenario_path.exists():
        sys.exit(f"No scenario at {scenario_path}")
    scenario = json.loads(scenario_path.read_text())
    transactions = scenario.get("transactions", [])
    manager_clubs = scenario.get("manager_clubs", {})  # team key -> PL club id

    squads_base = load("squads.json")
    positions = load_positions()
    meta = load("players.json")
    clubs = {int(k): v for k, v in meta.get("clubs", {}).items()}
    pl_fixtures = load("pl_fixtures.json", required=False) or []
    names = {t["key"]: t["team"] for t in squads_base["teams"]}

    files = sorted(DATA.glob("gw*.json"))
    if not files:
        sys.exit("No gameweek data — run the fetch workflow first.")

    print(f"Scenario: {scenario.get('name', scenario_path.name)}")
    if scenario.get("description"):
        print(f"  {scenario['description']}")
    print()

    for f in files:
        gw_num = int(f.stem[2:])
        gw, pts = player_points(f, positions)

        squads, adjustments, boost_log, bank, problems = apply_transactions(
            squads_base, transactions, gw_num)

        if problems:
            print("Rule violations in this scenario:")
            for p in problems:
                print(f"  ✗ {p}")
            print()

        state = ("final" if gw.get("data_checked")
                 else "PROVISIONAL" if gw.get("finished")
                 else "IN PROGRESS — round not complete")
        print(f"{'='*76}\nGameweek {gw_num} ({state})\n{'='*76}")

        boosts_this_gw = {b["team"]: b for b in boost_log if b["gameweek"] == gw_num}

        rows = []
        for team in squads_base["teams"]:
            key = team["key"]
            xi_total, _, _ = best_xi(squads[key], pts)

            boost_pts, boost_detail = 0, None
            if key in boosts_this_gw:
                club = manager_clubs.get(key)
                if club is None:
                    boost_detail = {"error": "no manager drafted"}
                else:
                    boost_pts, boost_detail = boost_value(
                        xi_total, club, gw_num, pl_fixtures)
                    if not boost_detail["played"] and assume:
                        # Hypothetical: price it as if the club got `assume`.
                        pct = boost_detail["pct"]
                        boost_pts = round(xi_total * (pct / 100.0) * BOOST_RESULT[assume])
                        boost_detail.update(result=f"{assume} (assumed)",
                                            multiplier=BOOST_RESULT[assume],
                                            hypothetical=True)

            adj = adjustments.get(gw_num, {}).get(key, 0)
            rows.append({
                "key": key, "team": names[key], "xi": xi_total,
                "boost": boost_pts, "boost_detail": boost_detail,
                "adj": adj, "total": xi_total + boost_pts + adj,
                "bank": bank[key],
            })

        rows.sort(key=lambda r: -r["total"])
        changed = [r for r in rows if r["boost"] or r["adj"] or r["bank"]]

        print(f"{'':>3} {'Team':<24} {'XI':>4} {'Boost':>6} {'Trade/Bank':>11} {'Total':>6}")
        for i, r in enumerate(rows, 1):
            b = f"{r['boost']:+d}" if r["boost"] else ""
            a = f"{r['adj']:+d}" if r["adj"] else ""
            mark = " ←" if (r["boost"] or r["adj"]) else ""
            print(f"{i:>3} {r['team']:<24} {r['xi']:>4} {b:>6} {a:>11} {r['total']:>6}{mark}")

        if changed:
            print("\nWhat moved:")
            for r in changed:
                if r["adj"]:
                    word = "docked for a trade" if r["adj"] < 0 else "spent from the bank"
                    print(f"  {r['team']}: {r['adj']:+d} — {word}")
                d = r["boost_detail"]
                if d and d.get("error"):
                    print(f"  {r['team']}: boost declared but {d['error']}")
                elif d:
                    club = clubs.get(manager_clubs.get(r["key"]), {}).get("name", "?")
                    if not d["played"] and not d.get("hypothetical"):
                        print(f"  {r['team']}: boost on {club} — no fixture played, "
                              f"nothing paid and the use is not consumed")
                    else:
                        tag = " [hypothetical]" if d.get("hypothetical") else ""
                        print(f"  {r['team']}: boost on {club} ({d['position']}th, "
                              f"{d['pct']:.1f}%) result {d['result']} "
                              f"×{d['multiplier']} → {r['boost']:+d}{tag}")
                if r["bank"]:
                    print(f"  {r['team']}: {r['bank']} points still banked")
        print()


if __name__ == "__main__":
    main()
