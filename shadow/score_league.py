#!/usr/bin/env python3
"""Score the league for each saved gameweek.

Each team owns 15 players but only 11 score. Real FPL Draft asks managers to
pick that XI before the deadline; for the shadow season we instead pick the
best legal XI in hindsight, which tests the *engine* without anyone having to
set a team every week.

That is deliberately not the same thing as what a manager would have scored —
nobody picks perfectly — so these totals run slightly hot. They flatter every
team equally, so the table is still meaningful for comparison, but they are
not "what I would have got". Once lineups are a real feature this switches to
the submitted XI.

Usage
-----
    python3 shadow/score_league.py            # all saved gameweeks + table
    python3 shadow/score_league.py 1          # one gameweek, with detail
    python3 shadow/score_league.py 1 --xi     # also print each chosen XI
"""
import json
import sys
from itertools import combinations
from pathlib import Path

from scoring import score_player

DATA = Path(__file__).resolve().parent / "data"

# FPL formation rules: always 1 keeper, then 3-5 / 2-5 / 1-3 making up 11.
GK_COUNT = 1
LIMITS = {"DEF": (3, 5), "MID": (2, 5), "FWD": (1, 3)}
XI_SIZE = 11


def valid_formations():
    out = []
    for d in range(LIMITS["DEF"][0], LIMITS["DEF"][1] + 1):
        for m in range(LIMITS["MID"][0], LIMITS["MID"][1] + 1):
            for f in range(LIMITS["FWD"][0], LIMITS["FWD"][1] + 1):
                if GK_COUNT + d + m + f == XI_SIZE:
                    out.append((d, m, f))
    return out


FORMATIONS = valid_formations()


def best_xi(squad, points):
    """Highest-scoring legal XI from a 15-man squad.

    Brute force over the eight legal formations, taking the top scorers at
    each position. Exact, and small enough that cleverness would be wasted.
    """
    by_pos = {"GK": [], "DEF": [], "MID": [], "FWD": []}
    for p in squad:
        by_pos.setdefault(p["position"], []).append(p)
    for pos in by_pos:
        by_pos[pos].sort(key=lambda p: points.get(p["id"], 0), reverse=True)

    best = None
    for d, m, f in FORMATIONS:
        if (len(by_pos["GK"]) < GK_COUNT or len(by_pos["DEF"]) < d
                or len(by_pos["MID"]) < m or len(by_pos["FWD"]) < f):
            continue
        xi = (by_pos["GK"][:GK_COUNT] + by_pos["DEF"][:d]
              + by_pos["MID"][:m] + by_pos["FWD"][:f])
        total = sum(points.get(p["id"], 0) for p in xi)
        if best is None or total > best[0]:
            best = (total, (d, m, f), xi)
    return best


def load_positions():
    raw = json.loads((DATA / "players.json").read_text())
    return {int(k): v for k, v in raw["positions"].items()}


def score_one_gameweek(path, squads, positions):
    gw = json.loads(path.read_text())
    pts = {}
    for el in gw["elements"]:
        pos = positions.get(el["id"])
        if pos is not None:
            pts[el["id"]] = score_player(el.get("stats", {}), pos)

    results = []
    for team in squads["teams"]:
        total, formation, xi = best_xi(team["squad"], pts)
        bench = [p for p in team["squad"] if p not in xi]
        results.append({
            "manager": team["manager"],
            "points": total,
            "formation": "-".join(str(x) for x in formation),
            "xi": [(p["name"], p["position"], pts.get(p["id"], 0)) for p in xi],
            "bench_points": sum(pts.get(p["id"], 0) for p in bench),
        })
    results.sort(key=lambda r: -r["points"])
    return gw, results


def main():
    show_xi = "--xi" in sys.argv
    args = [a for a in sys.argv[1:] if not a.startswith("-")]

    sq = DATA / "squads.json"
    if not sq.exists():
        sys.exit("No data/squads.json — run import_squads.py first.")
    squads = json.loads(sq.read_text())
    positions = load_positions()

    files = sorted(DATA.glob("gw*.json"))
    if args:
        wanted = {int(a) for a in args}
        files = [f for f in files if int(f.stem[2:]) in wanted]
    if not files:
        sys.exit("No gameweek files — run the fetch workflow first.")

    cumulative = {t["manager"]: 0 for t in squads["teams"]}

    for f in files:
        gw, results = score_one_gameweek(f, squads, positions)
        state = ("final" if gw.get("data_checked")
                 else "PROVISIONAL" if gw.get("finished")
                 else "IN PROGRESS — round not complete")
        print(f"\n{'='*58}\nGameweek {gw['gameweek']} ({state})\n{'='*58}")
        for i, r in enumerate(results, 1):
            print(f"{i:>2}. {r['manager']:<12} {r['points']:>4}  ({r['formation']}"
                  f", bench {r['bench_points']})")
            cumulative[r["manager"]] += r["points"]
            if show_xi:
                for name, pos, p in sorted(r["xi"], key=lambda x: -x[2]):
                    print(f"        {pos:<4} {name:<18} {p:>3}")

    if len(files) > 1:
        print(f"\n{'='*58}\nSeason table ({len(files)} gameweeks)\n{'='*58}")
        for i, (mgr, pts) in enumerate(sorted(cumulative.items(), key=lambda x: -x[1]), 1):
            print(f"{i:>2}. {mgr:<12} {pts:>5}")

    print("\nNote: XI chosen in hindsight, so totals run slightly hot for "
          "everyone. Comparative, not 'what I would have scored'.")


if __name__ == "__main__":
    main()
