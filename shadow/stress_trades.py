#!/usr/bin/env python3
"""Stress-test trades and the points bank against a completed season.

Trades-with-points and the bank are the two genuinely new mechanics, and both
move value between managers in ways FPL has no equivalent for. This checks
they hold up over 38 real gameweeks rather than the one worked example.

Three things are tested, in increasing order of how much they should worry
you.

INVARIANTS
  Randomised trades and spends, thousands of them, asserting the rules can't
  be broken: a bank never goes negative, a season total never goes negative,
  and points are conserved — every point deducted from one manager arrives in
  another's bank, so the mechanic moves value around without creating any.

CONCENTRATION
  Whether the bank lets one manager hoard. A bank that can be filled faster
  than it's spent would let someone bank a season and cash it in the run-in.

COLLUSION
  The one that matters. Trades are bilateral and final, with no veto, so two
  managers can agree anything. In a head-to-head league, points spent winning
  a week you'd have won anyway are worth nothing to you — but handed to an
  ally, they can flip a match. This measures how much a friendly pair can
  extract, using the real season's margins.

Usage
-----
    python3 shadow/stress_trades.py
    python3 shadow/stress_trades.py --trials 5000
"""
import json
import random
import sys
from pathlib import Path

from mechanics import apply_transactions

DATA = Path(__file__).resolve().parent / "data" / "season2025-26"


def season_scores():
    """{gameweek: {manager: (own score, opponent, opponent score)}}."""
    fixtures = json.loads((DATA / "fixtures.json").read_text())["fixtures"]
    out = {}
    for fx in fixtures:
        gw = fx["gameweek"]
        h, a = fx["home"], fx["away"]
        hs, as_ = fx["actual"]["home"], fx["actual"]["away"]
        out.setdefault(gw, {})[h] = (hs, a, as_)
        out.setdefault(gw, {})[a] = (as_, h, hs)
    return out


def league_points(mine, theirs):
    return 3 if mine > theirs else 1 if mine == theirs else 0


# ── Invariants ─────────────────────────────────────────────────────────────
def fake_squads(keys):
    """Minimal legal squads — the shape is all these tests need."""
    teams = []
    for i, k in enumerate(sorted(keys)):
        squad = []
        for j, pos in enumerate(["GK", "DEF", "MID", "FWD"]):
            squad.append({"id": i * 10 + j, "position": pos, "name": f"{k}{pos}"})
        teams.append({"key": k, "team": k, "squad": squad})
    return {"teams": teams}


def test_invariants(scores, trials, rng):
    keys = sorted(next(iter(scores.values())).keys())
    base = fake_squads(keys)
    squads_by_key = {t["key"]: t["squad"] for t in base["teams"]}

    # What each manager had scored going into each gameweek, which is what
    # the offer cap is measured against.
    points_to_date, running = {}, {k: 0 for k in keys}
    for gw in sorted(scores):
        points_to_date[gw] = dict(running)
        for k, (own, _, _) in scores[gw].items():
            running[k] += own

    failures = []
    for trial in range(trials):
        txs = []
        for _ in range(rng.randint(1, 12)):
            gw = rng.randint(1, 38)
            a, b = rng.sample(keys, 2)
            pa = next(p for p in squads_by_key[a] if p["position"] == "FWD")
            pb = next(p for p in squads_by_key[b] if p["position"] == "FWD")
            txs.append({
                "type": "trade", "gameweek": gw, "from": a, "to": b,
                "players_out": [pa], "players_in": [pb],
                # Deliberately absurd offers as well as sane ones, so the cap
                # gets tested rather than just tiptoed around.
                "points": rng.choice([0, 5, 25, 60, 200, 5000]),
            })
        for _ in range(rng.randint(0, 8)):
            txs.append({"type": "bank_use", "gameweek": rng.randint(1, 38),
                        "team": rng.choice(keys),
                        "points": rng.choice([1, 10, 40, 150])})

        _, adjustments, _, bank, _, _ = apply_transactions(
            base, txs, 38, points_to_date=points_to_date)

        for k, v in bank.items():
            if v < 0:
                failures.append(f"trial {trial}: {k} bank went to {v}")

        # No season total may end below zero.
        for k in keys:
            total = sum(scores[gw][k][0] for gw in scores)
            total += sum(adjustments.get(gw, {}).get(k, 0) for gw in adjustments)
            if total < 0:
                failures.append(f"trial {trial}: {k} finished the season on {total}")

        # Conservation: every point deducted lands somewhere.
        debts = -sum(v for gw in adjustments for v in adjustments[gw].values() if v < 0)
        credits = sum(v for gw in adjustments for v in adjustments[gw].values() if v > 0)
        held = sum(bank.values())
        if debts != credits + held:
            failures.append(
                f"trial {trial}: {debts} deducted but {credits} spent + {held} banked")

    return failures


# ── Collusion ──────────────────────────────────────────────────────────────
def spare_points(own, opp):
    """Points a manager could give away this week and still win."""
    return max(0, own - opp - 1)


def spend_pot(scores, taker, pot):
    """Upper bound: flip the narrowest defeats first, knowing the margins.

    Unreachable, and included only to bound the problem. A bank spend is
    declared before the gameweek like everything else, so nobody gets to see
    that they lost by two and pay exactly three.
    """
    losses = sorted(opp - own + 1
                    for gw in scores
                    for own, _, opp in [scores[gw][taker]] if own < opp)
    gained = 0
    for need in losses:
        if need <= pot:
            pot -= need
            gained += 3          # a defeat becomes a win
    return gained, pot


def spend_declared(scores, taker, pot, per_week):
    """What a bank is actually worth, spent blind.

    The taker commits the same amount every gameweek without knowing how the
    match will go. Most of it lands on weeks they were winning anyway or
    losing by more than they can cover — the same structural waste that makes
    a boost worth less than its headline number.
    """
    gained = 0
    for gw in sorted(scores):
        if pot <= 0:
            break
        spend = min(per_week, pot)
        own, _, opp = scores[gw][taker]
        before = league_points(own, opp)
        after = league_points(own + spend, opp)
        pot -= spend
        gained += after - before
    return gained, pot


def collusion_oracle(scores, giver, taker, cap_by_gw):
    """Upper bound, and deliberately unreachable.

    Assumes the giver knows their margin before donating, so they never give
    away a point that costs them. Nobody can do this: a trade is declared
    before the gameweek, same as a boost. Kept only to bound the problem.
    """
    pot = sum(min(spare_points(*[scores[gw][giver][0], scores[gw][giver][2]]),
                  cap_by_gw[gw][giver])
              for gw in scores if scores[gw][giver][0] > scores[gw][giver][2])
    return spend_pot(scores, taker, pot)


def collusion_realistic(scores, giver, taker, cap_by_gw, per_week):
    """What a colluding pair actually nets, declaring in advance.

    The giver commits the same donation every gameweek without knowing how
    their own match will go, so some of it comes out of weeks they needed.
    Returns the pair's NET league points: what the taker gains minus what the
    giver loses by weakening themselves.
    """
    pot = 0
    giver_cost = 0
    for gw in sorted(scores):
        own, _, opp = scores[gw][giver]
        give = min(per_week, cap_by_gw[gw][giver])
        if give <= 0:
            continue
        pot += give
        giver_cost += league_points(own, opp) - league_points(own - give, opp)
    # The taker spends blind too, at whatever weekly rate empties the pot
    # over the rest of the season.
    best = (0, 0, 0)
    for spend_rate in (5, 10, 15, 20, 30, 50):
        gained, left = spend_declared(scores, taker, pot, spend_rate)
        if gained > best[0]:
            best = (gained, left, spend_rate)
    gained, left, _ = best
    return gained - giver_cost, gained, giver_cost, left


def main():
    argv = sys.argv[1:]
    trials = 2000
    if "--trials" in argv:
        trials = int(argv[argv.index("--trials") + 1])

    if not (DATA / "fixtures.json").exists():
        sys.exit(f"No season archive at {DATA}")
    scores = season_scores()
    keys = sorted(next(iter(scores.values())).keys())
    rng = random.Random(11)

    print(f"Stress-testing against {len(scores)} real gameweeks, "
          f"{len(keys)} managers\n")

    print("=" * 66)
    print(f"INVARIANTS — {trials} randomised trade/spend scenarios")
    print("=" * 66)
    failures = test_invariants(scores, trials, rng)
    if failures:
        print(f"{len(failures)} violation(s):")
        for f in failures[:10]:
            print(f"  ✗ {f}")
    else:
        print("  ✓ no bank ever went negative")
        print("  ✓ no season total ever went negative")
        print("  ✓ points conserved: everything deducted was banked or spent")

    # ── Concentration ──
    points_to_date, running = {}, {k: 0 for k in keys}
    for gw in sorted(scores):
        points_to_date[gw] = dict(running)
        for k, (own, _, _) in scores[gw].items():
            running[k] += own
    final = dict(running)

    print(f"\n{'=' * 66}")
    print("CONCENTRATION — how big can a bank get?")
    print("=" * 66)
    biggest = max(final.values())
    print(f"  A season's points-for runs {min(final.values())} to {biggest}.")
    print(f"  The cap is what you've scored so far, so the most one manager")
    print(f"  could ever hand over across a season is their own final total.")
    print(f"  A bank can't outgrow the game — it's a transfer, not a printer.")

    # ── Collusion ──
    print(f"\n{'=' * 66}")
    print("COLLUSION — what a friendly pair can extract")
    print("=" * 66)
    pairs = [(g, t) for g in keys for t in keys if g != t]

    oracle = sorted((collusion_oracle(scores, g, t, points_to_date)[0], g, t)
                    for g, t in pairs)[::-1]
    print("  With hindsight on both sides — the giver knows their margin")
    print("  before donating, the taker knows theirs before spending:\n")
    print(f"    best pair {oracle[0][1]}→{oracle[0][2]}: "
          f"{oracle[0][0]} league points to the taker")
    print("    An upper bound nobody can reach: trades AND bank spends are")
    print("    both declared before the gameweek, exactly like a boost.\n")

    print("  Declared in advance, which is the real rule. Neither side sees")
    print("  a margin before committing — the giver weakens themselves blind,")
    print("  the taker spends blind:\n")
    print(f"  {'per week':>9} {'taker gains':>12} {'giver loses':>12} "
          f"{'pair net':>10}")
    best_net = None
    for per_week in (5, 10, 15, 20, 30, 40):
        rows = [collusion_realistic(scores, g, t, points_to_date, per_week)
                for g, t in pairs]
        net = max(r[0] for r in rows)
        row = max(rows, key=lambda r: r[0])
        print(f"  {per_week:>9} {row[1]:>12} {row[2]:>12} {net:>10}")
        if best_net is None or net > best_net[0]:
            best_net = (net, per_week)
    print(f"\n  Best realistic collusion: {best_net[0]} net league points to a")
    print(f"  pair, donating {best_net[1]} a week all season.")
    print("  For scale, the title was won on 70 points last season.")

    print(f"\n{'=' * 66}")
    print("MITIGATION — a season cap on points received")
    print("=" * 66)
    print("  The exploit isn't the size of any one trade, it's the drip: ten")
    print("  points a week is small enough to almost never cost the giver a")
    print("  result, and by spring it's a bank of several hundred.")
    print("  Capping what a manager may RECEIVE across a season kills the")
    print("  drip while leaving a single headline trade untouched.\n")
    print(f"  {'season cap':>11} {'worst-case collusion':>22}")
    for cap in (25, 50, 75, 100, 150, 250, None):
        worst = 0
        for g, t in pairs:
            pot = 0
            for gw in sorted(scores):
                give = min(10, points_to_date[gw][g])
                if cap is not None:
                    give = min(give, max(0, cap - pot))
                pot += give
            gained = max(spend_declared(scores, t, pot, r)[0]
                         for r in (5, 10, 15, 20, 30, 50))
            worst = max(worst, gained)
        label = "none" if cap is None else str(cap)
        print(f"  {label:>11} {worst:>19} pts")
    print("\n  A cap of 50 is about one Solanke-plus-40 trade a season, which")
    print("  is the mechanic you actually wanted, and holds collusion to a")
    print("  few points rather than a title.")


if __name__ == "__main__":
    main()
