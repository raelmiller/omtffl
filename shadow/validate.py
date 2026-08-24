#!/usr/bin/env python3
"""Check the scoring engine against FPL's own totals.

For every player in every saved gameweek, compute points from raw stats and
compare with FPL's `total_points`. Any mismatch means our rule table is wrong
(or FPL changed something), and the offending players are printed so the rule
can be found rather than guessed at.

This is the real test of the shadow season. The unit tests prove the rules do
what I *think* the rules are; this proves what I think is actually right.

Usage
-----
    python3 shadow/validate.py           # all saved gameweeks
    python3 shadow/validate.py 1         # one gameweek
    python3 shadow/validate.py -v        # list every mismatch, not a sample
"""
import json
import sys
from collections import Counter
from pathlib import Path

from scoring import score_entry, POSITION_NAMES

DATA = Path(__file__).resolve().parent / "data"


def load_players():
    p = DATA / "players.json"
    if not p.exists():
        sys.exit("No data/players.json — run fetch_gw.py first (in GitHub Actions).")
    raw = json.loads(p.read_text())
    positions = {int(k): v for k, v in raw["positions"].items()}
    names = {int(k): v for k, v in raw["names"].items()}
    return positions, names


def validate_gameweek(path, positions, names, verbose=False):
    data = json.loads(path.read_text())
    gw = data["gameweek"]
    elements = data["elements"]

    checked = 0
    mismatches = []
    for el in elements:
        pid = el["id"]
        stats = el.get("stats", {})
        if "total_points" not in stats:
            continue
        pos = positions.get(pid)
        if pos is None:
            continue
        # Players who neither featured nor scored are trivially zero on both
        # sides; counting them would flatter the result. A no-minutes player
        # with points is a real case though — a booking on the bench — so
        # those stay in.
        if (stats.get("minutes") or 0) <= 0 and not stats.get("total_points"):
            continue

        ours = score_entry(el, pos)
        theirs = int(stats["total_points"])
        checked += 1
        if ours != theirs:
            mismatches.append({
                "id": pid,
                "name": names.get(pid, f"#{pid}"),
                "pos": POSITION_NAMES.get(pos, pos),
                "ours": ours,
                "theirs": theirs,
                "diff": ours - theirs,
                "stats": stats,
            })

    if data.get("data_checked"):
        final = "final"
    elif data.get("finished"):
        final = "PROVISIONAL — FPL still revising"
    else:
        final = "IN PROGRESS — round not complete"
    print(f"\nGameweek {gw} ({final}) — {checked} players with minutes")
    if not mismatches:
        print(f"  ✓ all {checked} match FPL exactly")
        return checked, []

    print(f"  ✗ {len(mismatches)} mismatch(es) ({len(mismatches)/checked:.1%})")
    by_diff = Counter(m["diff"] for m in mismatches)
    print(f"  difference spread: {dict(sorted(by_diff.items()))}")
    by_pos = Counter(m["pos"] for m in mismatches)
    print(f"  by position: {dict(by_pos)}")

    show = mismatches if verbose else mismatches[:8]
    for m in show:
        interesting = {
            k: v for k, v in m["stats"].items()
            if v not in (0, None) and k != "total_points"
        }
        print(f"    {m['name']:<18} {m['pos']:<4} ours={m['ours']:>3} fpl={m['theirs']:>3} "
              f"diff={m['diff']:+d}  {interesting}")
    if not verbose and len(mismatches) > len(show):
        print(f"    … and {len(mismatches) - len(show)} more (-v to see all)")
    return checked, mismatches


def main():
    verbose = "-v" in sys.argv or "--verbose" in sys.argv
    args = [a for a in sys.argv[1:] if not a.startswith("-")]

    positions, names = load_players()
    files = sorted(DATA.glob("gw*.json"))
    if args:
        wanted = {int(a) for a in args}
        files = [f for f in files if int(f.stem[2:]) in wanted]
    if not files:
        sys.exit("No gameweek files in shadow/data — run fetch_gw.py first.")

    total_checked = 0
    total_bad = 0
    for f in files:
        checked, bad = validate_gameweek(f, positions, names, verbose)
        total_checked += checked
        total_bad += len(bad)

    print("\n" + "=" * 60)
    if total_bad == 0:
        print(f"ENGINE VALIDATED: {total_checked} player-gameweeks, 0 mismatches")
        return 0
    print(f"{total_bad} mismatch(es) across {total_checked} player-gameweeks "
          f"({total_bad/total_checked:.2%}) — rules need work")
    return 1


if __name__ == "__main__":
    sys.exit(main())
