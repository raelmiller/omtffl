#!/usr/bin/env python3
"""Score the league for each saved gameweek.

Each team owns 15 players but only 11 score. Real FPL Draft asks managers to
pick that XI before the deadline; for the shadow season we instead pick the
best legal XI in hindsight, which tests the *engine* without anyone having to
set a team every week.

That is deliberately not the same thing as what a manager would have scored —
nobody picks perfectly — so these totals run slightly hot. They flatter every
team equally, so the table is still meaningful for comparison, but they are
not "what I would have got".

`--submitted` scores the XI managers actually named instead, with automatic
substitutions applied, and shows the difference. That difference is the skill
the hindsight table hides.

Usage
-----
    python3 shadow/score_league.py            # all saved gameweeks + table
    python3 shadow/score_league.py 1          # one gameweek, with detail
    python3 shadow/score_league.py 1 --xi     # also print each chosen XI
    python3 shadow/score_league.py --submitted   # score the picked XIs
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


def score_one_gameweek(path, squads, positions, lineups=None):
    """Score every team for a gameweek.

    With `lineups`, a team that submitted an XI is scored on that XI (after
    automatic substitutions) rather than the best one available. Both numbers
    are returned either way, so the cost of picking badly is visible.
    """
    gw = json.loads(path.read_text())
    pts = {}
    for el in gw["elements"]:
        pos = positions.get(el["id"])
        if pos is not None:
            pts[el["id"]] = score_player(el.get("stats", {}), pos)

    n = gw["gameweek"]
    minutes = None
    if lineups:
        from lineups import apply_autosubs, effective_lineup, minutes_from_gameweek
        minutes = minutes_from_gameweek(gw)

    results = []
    for team in squads["teams"]:
        squad = team["squad"]
        best_total, best_formation, best_eleven = best_xi(squad, pts)
        total, formation, xi = best_total, best_formation, best_eleven
        source, subs = "best available (hindsight)", []

        if lineups:
            picked, bench, how = effective_lineup(team["key"], n, lineups, squad)
            if picked:
                xi, subs = apply_autosubs(picked, bench, minutes)
                total = sum(pts.get(p["id"], 0) for p in xi)
                counts = {}
                for p in xi:
                    counts[p["position"]] = counts.get(p["position"], 0) + 1
                formation = (counts.get("DEF", 0), counts.get("MID", 0), counts.get("FWD", 0))
                source = how

        bench_players = [p for p in squad if p not in xi]
        results.append({
            "key": team["key"],
            "team": team.get("team", team["key"]),
            "points": total,
            "best_points": best_total,
            "source": source,
            "subs": [(off["name"], on["name"]) for off, on in subs],
            "formation": "-".join(str(x) for x in formation),
            "xi": [(p["name"], p["position"], pts.get(p["id"], 0)) for p in xi],
            "bench_points": sum(pts.get(p["id"], 0) for p in bench_players),
        })
    results.sort(key=lambda r: -r["points"])
    return gw, results


def main():
    show_xi = "--xi" in sys.argv
    use_submitted = "--submitted" in sys.argv
    args = [a for a in sys.argv[1:] if not a.startswith("-")]

    lineups = None
    if use_submitted:
        from lineups import load_lineups
        lineups = load_lineups()
        if not lineups:
            sys.exit("No data/lineups.json — nothing has been submitted.\n"
                     "Create one with: python3 shadow/lineups.py --template 1 --suggest")

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

    cumulative = {t["key"]: 0 for t in squads["teams"]}
    names = {t["key"]: t.get("team", t["key"]) for t in squads["teams"]}

    for f in files:
        gw, results = score_one_gameweek(f, squads, positions, lineups)
        state = ("final" if gw.get("data_checked")
                 else "PROVISIONAL" if gw.get("finished")
                 else "IN PROGRESS — round not complete")
        print(f"\n{'='*70}\nGameweek {gw['gameweek']} ({state})\n{'='*70}")
        for i, r in enumerate(results, 1):
            line = (f"{i:>2}. {r['key']:<4} {r['team']:<24} {r['points']:>4}  "
                    f"({r['formation']}, bench {r['bench_points']})")
            if use_submitted:
                left = r["best_points"] - r["points"]
                line += f"   best {r['best_points']:>3}, left out {left:>3}"
            print(line)
            cumulative[r["key"]] += r["points"]
            if use_submitted and r["source"] != "submitted":
                print(f"        {r['source']}")
            for off, on in r["subs"]:
                print(f"        autosub: {on} for {off}")
            if show_xi:
                for name, pos, p in sorted(r["xi"], key=lambda x: -x[2]):
                    print(f"        {pos:<4} {name:<18} {p:>3}")

    if len(files) > 1:
        print(f"\n{'='*58}\nSeason table ({len(files)} gameweeks)\n{'='*58}")
        for i, (k, pts) in enumerate(sorted(cumulative.items(), key=lambda x: -x[1]), 1):
            print(f"{i:>2}. {k:<4} {names[k]:<24} {pts:>5}")

    if use_submitted:
        gap = sum(r["best_points"] - r["points"] for r in results)
        print(f"\nScored on submitted lineups. The league left {gap} points on "
              f"the bench this gameweek —\nthat gap is the whole point of "
              "asking managers to pick.")
    else:
        print("\nNote: XI chosen in hindsight, so totals run slightly hot for "
              "everyone. Comparative, not 'what I would have scored'."
              "\nAdd --submitted to score the XIs managers actually picked.")


if __name__ == "__main__":
    main()
