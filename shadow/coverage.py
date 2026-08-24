#!/usr/bin/env python3
"""Which scoring rules the saved gameweek data actually exercised.

A clean validate.py run only means something if the rules were used. A rule no
player triggered is untested by that data, however green the run looks — so
this reports what real matches actually covered, and what is still resting on
the unit tests alone.

Usage: python3 shadow/coverage.py
"""
import json
import sys
from pathlib import Path

from scoring import defensive_actions, POSITION_NAMES, RULES

DATA = Path(__file__).resolve().parent / "data"

# Rare events worth calling out explicitly when nothing triggered them.
RARE = ["penalty saved", "penalty missed", "own goal", "red card"]


def main():
    pfile = DATA / "players.json"
    if not pfile.exists():
        sys.exit("No data/players.json — run the fetch workflow first.")
    positions = {int(k): v for k, v in json.loads(pfile.read_text())["positions"].items()}

    files = sorted(DATA.glob("gw*.json"))
    if not files:
        sys.exit("No gameweek files — run the fetch workflow first.")

    cov, total = {}, 0
    def bump(k):
        cov[k] = cov.get(k, 0) + 1

    for f in files:
        gw = json.loads(f.read_text())
        for e in gw["elements"]:
            s = e["stats"]
            p = positions.get(e["id"])
            m = s.get("minutes") or 0
            if p is None or m <= 0:
                continue
            total += 1
            bump("appearance 60+" if m >= 60 else "appearance 1-59")
            if s.get("goals_scored"):
                bump(f"goal ({POSITION_NAMES[p]})")
            if s.get("assists"):
                bump("assist")
            if s.get("clean_sheets") and m >= 60:
                bump(f"clean sheet ({POSITION_NAMES[p]})")
            if (s.get("goals_conceded") or 0) >= RULES["conceded_per"] and p in (1, 2):
                bump("conceded 2+ (GK/DEF)")
            if (s.get("saves") or 0) >= RULES["saves_per"]:
                bump("3+ saves")
            if s.get("penalties_saved"):
                bump("penalty saved")
            if s.get("penalties_missed"):
                bump("penalty missed")
            if s.get("own_goals"):
                bump("own goal")
            if s.get("yellow_cards"):
                bump("yellow card")
            if s.get("red_cards"):
                bump("red card")
            if s.get("bonus"):
                bump("bonus awarded")
            t = RULES["defcon_threshold"].get(p)
            if t is not None and defensive_actions(s, p) >= t:
                bump(f"defensive contribution ({POSITION_NAMES[p]})")

    print(f"{total} player-gameweeks with minutes across {len(files)} gameweek(s)\n")
    print("Rules exercised by real data:")
    for k in sorted(cov, key=lambda x: -cov[x]):
        print(f"  {cov[k]:>5}  {k}")

    missing = [r for r in RARE if r not in cov]
    if missing:
        print("\nNot yet seen in real data (covered only by unit tests):")
        for mname in missing:
            print(f"  - {mname}")


if __name__ == "__main__":
    main()
