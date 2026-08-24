#!/usr/bin/env python3
"""Build the season's H2H fixture list.

With 14 teams a full round robin is 13 rounds, and the season simply repeats
that cycle: GW1-13, GW14-26, then GW27-38 (the first 12 of a third cycle). So
only 13 rounds are written out here and the rest are generated — transcribing
all 266 fixtures by hand would be 266 chances to make a typo.

The round robin is verified rather than assumed: every round must contain all
14 teams exactly once, and across the 13 rounds every pair must meet exactly
once. A transcription slip breaks one of those and the script fails.

Output: data/fixtures.json
"""
import json
import sys
from itertools import combinations
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data"

SOURCE = DATA / "fixtures_source.json"


def load_source():
    """Fixture data lives in a local file, not in this script.

    The rounds are just manager names, and omtffl is a public repo — keeping
    them in data/ (gitignored) means the code can be versioned without
    publishing who plays in the league. It's also better structured: a fixture
    list is data, not code.
    """
    if not SOURCE.exists():
        sys.exit(
            f"No {SOURCE.name} — the fixture list is kept out of git.\n"
            "It should contain: cycle_length, gameweeks, rounds "
            "(list of rounds, each a list of [home, away]), known_results."
        )
    raw = json.loads(SOURCE.read_text())
    rounds = [[tuple(fx) for fx in rnd] for rnd in raw["rounds"]]
    known = {
        int(gw): {m: tuple(v) for m, v in res.items()}
        for gw, res in raw.get("known_results", {}).items()
    }
    return raw["cycle_length"], raw["gameweeks"], rounds, known


def verify(managers, ROUNDS):
    problems = []
    for i, rnd in enumerate(ROUNDS, 1):
        seen = [t for fx in rnd for t in fx]
        if len(seen) != len(managers):
            problems.append(f"round {i}: {len(seen)} slots, expected {len(managers)}")
        if len(set(seen)) != len(seen):
            dupes = {t for t in seen if seen.count(t) > 1}
            problems.append(f"round {i}: team(s) appearing twice: {sorted(dupes)}")
        unknown = set(seen) - managers
        if unknown:
            problems.append(f"round {i}: unknown manager(s): {sorted(unknown)}")

    pairs = [frozenset(fx) for rnd in ROUNDS for fx in rnd]
    for pair in set(pairs):
        if pairs.count(pair) != 1:
            problems.append(f"pair {sorted(pair)} meets {pairs.count(pair)}x in one cycle")
    expected = set(map(frozenset, combinations(sorted(managers), 2)))
    missing = expected - set(pairs)
    if missing:
        problems.append(f"{len(missing)} pair(s) never meet, e.g. {sorted(map(sorted, list(missing)[:3]))}")
    return problems


def main():
    squads_path = DATA / "squads.json"
    if not squads_path.exists():
        sys.exit("No data/squads.json — run import_squads.py first.")
    managers = {t["key"] for t in json.loads(squads_path.read_text())["teams"]}
    CYCLE, TOTAL_GAMEWEEKS, ROUNDS, KNOWN_RESULTS = load_source()

    problems = verify(managers, ROUNDS)
    if problems:
        print("Fixture list is not a valid round robin:")
        for p in problems:
            print(f"  - {p}")
        return 1
    print(f"✓ verified: {len(ROUNDS)} rounds, {len(managers)} teams, "
          f"every pair meets exactly once per cycle")

    fixtures = []
    for gw in range(1, TOTAL_GAMEWEEKS + 1):
        rnd = ROUNDS[(gw - 1) % CYCLE]
        for home, away in rnd:
            fx = {"gameweek": gw, "home": home, "away": away}
            known = KNOWN_RESULTS.get(gw, {}).get(home)
            if known:
                fx["actual"] = {"home": known[0], "away": known[1]}
            fixtures.append(fx)

    out = {
        "teams": sorted(managers),
        "cycle_length": CYCLE,
        "gameweeks": TOTAL_GAMEWEEKS,
        "fixtures": fixtures,
    }
    (DATA / "fixtures.json").write_text(json.dumps(out, indent=2))
    print(f"Wrote data/fixtures.json — {len(fixtures)} fixtures across {TOTAL_GAMEWEEKS} gameweeks")

    # Each team should play every gameweek and end with a balanced schedule.
    from collections import Counter
    played = Counter(t for fx in fixtures for t in (fx["home"], fx["away"]))
    if len(set(played.values())) != 1:
        print(f"⚠ uneven schedule: {dict(played)}")
        return 1
    print(f"  each team plays {played.most_common(1)[0][1]} matches")
    return 0


if __name__ == "__main__":
    sys.exit(main())
