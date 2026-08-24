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
    python3 shadow/simulate.py --best-xi              # ignore submitted lineups
"""
import json
import sys
from pathlib import Path

from lineups import (
    apply_autosubs, effective_lineup, load_lineups, minutes_from_gameweek,
)
from mechanics import (
    BOOST_RESULT, BOOST_USES_PER_SEASON, apply_transactions, boost_pct,
    boost_value, league_table,
)
from h2h import standings_before
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


def team_total(key, squad, pts, lineups, gw_num, minutes):
    """One team's XI score, from their submitted lineup where there is one."""
    if lineups:
        picked, bench, _ = effective_lineup(key, gw_num, lineups, squad)
        if picked:
            final_xi, _ = apply_autosubs(picked, bench, minutes)
            return sum(pts.get(p["id"], 0) for p in final_xi), "submitted lineups"
    total, _, _ = best_xi(squad, pts)
    return total, "best available (hindsight)"


def season_context(files, squads_base, transactions, positions, lineups):
    """What each team had scored, and where they sat, going into each gameweek.

    Both are inputs to rules — the trade offer cap is capped by what you've
    accumulated, and waiver priority snakes from the bottom of the table. They
    can only be known by scoring the season up to that point, so this is a
    first pass with those two rules switched off. Running the cap check
    against a total that the cap itself helped produce would be circular.
    """
    points_to_date = {}
    # Everyone starts on nothing, which is a real number rather than a gap —
    # it's what makes a points offer in gameweek 1 illegal rather than
    # unknowable.
    running = {t["key"]: 0 for t in squads_base["teams"]}
    for f in files:
        gw_num = int(f.stem[2:])
        gw, pts = player_points(f, positions)
        points_to_date[gw_num] = dict(running)
        squads, adjustments, _, _, _, _ = apply_transactions(
            squads_base, transactions, gw_num)
        minutes = minutes_from_gameweek(gw)
        for team in squads_base["teams"]:
            key = team["key"]
            total, _ = team_total(key, squads[key], pts, lineups, gw_num, minutes)
            total += adjustments.get(gw_num, {}).get(key, 0)
            running[key] = running.get(key, 0) + total

    standings = {}
    played = sorted(int(f.stem[2:]) for f in files)
    for gw_num in played:
        if not any(g < gw_num for g in played):
            # Nothing has been played, so there is no table to snake from.
            # Left out deliberately, so the run says so rather than dressing
            # up an all-square table as priority.
            continue
        try:
            standings[gw_num] = standings_before(gw_num)
        except Exception:
            pass  # no fixture list yet; the caller reports the fallback
    return points_to_date, standings


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
    # By default the simulation scores the XI managers submitted, because a
    # boost is a percentage of that XI — pricing it off a hindsight team would
    # overstate every boost in the league.
    use_best = "--best-xi" in argv
    args = [a for a in argv if not a.startswith("-")]
    lineups = {} if use_best else load_lineups()

    scenario_path = Path(args[0]) if args else DATA / "scenario.json"
    if not scenario_path.exists():
        sys.exit(f"No scenario at {scenario_path}")
    scenario = json.loads(scenario_path.read_text())
    transactions = scenario.get("transactions", [])
    # A manager entry is either a bare club id or a record that can also carry
    # a name and the gameweek they were sacked in.
    managers = {}
    for key, val in scenario.get("manager_clubs", {}).items():
        managers[key] = {"club": val} if isinstance(val, int) else dict(val)
    manager_clubs = {k: v["club"] for k, v in managers.items()}

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

    points_to_date, standings = season_context(
        files, squads_base, transactions, positions, lineups)

    for f in files:
        gw_num = int(f.stem[2:])
        gw, pts = player_points(f, positions)

        squads, adjustments, boost_log, bank, waiver_log, problems = apply_transactions(
            squads_base, transactions, gw_num, points_to_date=points_to_date,
            standings=standings, managers=managers)

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
        minutes = minutes_from_gameweek(gw)

        claims = [w for w in waiver_log if w["gameweek"] == gw_num]
        if claims:
            print("Waiver run (priority snakes from the bottom of the table):")
            for w in claims:
                mark = "✓" if w["landed"] else "·"
                why = "" if w["landed"] else f"  — {w['why']}"
                print(f"  {mark} round {w['round']}  {names[w['team']]:<24} "
                      f"{w['add']['name']} for {w['drop']['name']}{why}")
            print()

        rows = []
        xi_source = "best available (hindsight)"
        for team in squads_base["teams"]:
            key = team["key"]
            xi_total, xi_source = team_total(
                key, squads[key], pts, lineups, gw_num, minutes)

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

        print(f"XI scored from: {xi_source}")
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
