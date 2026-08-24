#!/usr/bin/env python3
"""Work out what the manager boost is worth to whoever drafted that manager.

Managers are drafted at the auction and you keep yours all season, so this is
not a menu you pick from each week — it's the hand you were dealt. The only
choice you have is *when* to spend your uses — and how many uses there are
decides whether a manager is worth real money at the auction or an
afterthought. The last section prices that.

That makes the question a fairness one rather than a strategy one. The boost
multiplies your XI by a percentage set by your manager's club position, and
only pays if their club gets a result. Those two pull against each other: a
struggling club pays a bigger percentage but wins less often. If they cancel
exactly, the scale is decorative. If they don't cancel at all, the auction
just becomes a race for relegation-threatened managers.

So the table below is read down, not across: each row is a different drafter's
season, and the spread between the rows is what the auction has to price.

Two things the raw expectation misses, both of which favour the top of the
table more than the numbers suggest:

- **Variance.** A top-four manager pays out four times in five. A relegation
  manager pays nothing more often than not. In a head-to-head league a
  reliable small boost wins more weeks than a volatile large one.
- **Fixture choice.** A handful of uses across 38 gameweeks are never spent on an
  average fixture, which lifts the bottom of the table far more than the top
  (a title club is already near its ceiling). `--good-fixture` models it as
  a fixture that cuts your chance of *not* winning: p -> 1 - (1-p)**k. That
  saturates near the top instead of pretending a title club can improve
  without limit, which a "treat them as N places higher" fudge does not.

The win rates are league-position averages, not this season's clubs, and they
are assumptions rather than measured data — edit `WIN_RATES` if you'd rather
use different ones. The shape of the conclusion is not sensitive to small
changes in them.

Usage
-----
    python3 shadow/boost_scale.py                 # the agreed stepped scale
    python3 shadow/boost_scale.py --good-fixture 1.6  # spent on a good week
    python3 shadow/boost_scale.py --ceiling 70    # try a higher top band
    python3 shadow/boost_scale.py --linear        # compare against a ramp
"""
import sys

from mechanics import BOOST_RESULT, boost_pct

# Rough per-match outcome rates by final league position, averaged over recent
# Premier League seasons: (win, draw). Defeat is the remainder.
WIN_RATES = {
    1: (0.76, 0.14), 2: (0.68, 0.18), 3: (0.63, 0.18), 4: (0.58, 0.21),
    5: (0.53, 0.22), 6: (0.50, 0.23), 7: (0.47, 0.24), 8: (0.44, 0.24),
    9: (0.41, 0.25), 10: (0.39, 0.26), 11: (0.37, 0.26), 12: (0.35, 0.26),
    13: (0.33, 0.27), 14: (0.32, 0.26), 15: (0.30, 0.26), 16: (0.29, 0.26),
    17: (0.27, 0.26), 18: (0.25, 0.25), 19: (0.21, 0.24), 20: (0.17, 0.22),
}


# A typical gameweek XI in this league, and how much team scores vary, both
# measured from real gameweek data where it exists (see league_shape()).
XI_POINTS = 38
XI_SPREAD = 11

BUDGET = 50.0        # auction budget per team
SQUAD_SIZE = 15
SEASON_WEEKS = 38


def league_shape():
    """Mean and spread of a real gameweek's team scores, if we have any.

    Falls back to the constants above when there's no data yet. Everything
    downstream is sensitive to these two numbers, so they're measured rather
    than assumed wherever possible.
    """
    try:
        import json
        from pathlib import Path

        from h2h import gameweek_scores
        from score_league import load_positions

        data = Path(__file__).resolve().parent / "data"
        squads = json.loads((data / "squads.json").read_text())
        positions = load_positions()
        totals = []
        for f in sorted(data.glob("gw*.json")):
            _, scores = gameweek_scores(f, squads, positions)
            totals.extend(scores.values())
        if len(totals) < 5:
            return XI_POINTS, XI_SPREAD, 0
        mean = sum(totals) / len(totals)
        var = sum((t - mean) ** 2 for t in totals) / (len(totals) - 1)
        return mean, var ** 0.5, len(totals)
    except Exception:
        return XI_POINTS, XI_SPREAD, 0


def _phi(z):
    """Standard normal CDF, via the error function."""
    import math
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def flip_chance(boost_points, spread):
    """Chance one boost turns a head-to-head defeat into a win.

    Two things have to be true: you have to be behind, and behind by less
    than the boost is worth. Team scores are near enough normal, so the
    margin between two of them is a half-normal with spread sqrt(2) times
    a single team's.
    """
    if spread <= 0:
        return 0.0
    margin_spread = spread * (2 ** 0.5)
    within_reach = 2 * _phi(boost_points / margin_spread) - 1
    return 0.5 * within_reach  # you're the one losing about half the time


def outcome(position, quality=1.0):
    """(win, draw) rates for a club, optionally in a favourable fixture.

    `quality` above 1 shrinks the chance of not winning, which is what a good
    draw actually does. It saturates: a club already winning 76% of the time
    has far less room to improve than one winning 17%.
    """
    win, draw = WIN_RATES[position]
    if quality == 1.0:
        return win, draw
    better = 1 - (1 - win) ** quality
    spare = 1 - better
    loss = 1 - win - draw
    # Split what's left between draw and defeat in their original proportion.
    share = draw / (draw + loss) if (draw + loss) else 0
    return better, spare * share


def payout_chance(position, quality=1.0):
    """Expected share of the headline percentage that actually gets paid."""
    win, draw = outcome(position, quality)
    return win * BOOST_RESULT["W"] + draw * BOOST_RESULT["D"]


def linear_pct(position, floor=10.0, ceiling=50.0):
    return floor + (position - 1) * (ceiling - floor) / 19.0


def scaled(position, ceiling):
    """The stepped scale, rescaled to a different ceiling."""
    base = boost_pct(position)
    if ceiling == 50.0:
        return base
    return 10.0 + (base - 10.0) * (ceiling - 10.0) / 40.0


def main():
    argv = sys.argv[1:]
    ceiling = 50.0
    if "--ceiling" in argv:
        ceiling = float(argv[argv.index("--ceiling") + 1])
    use_linear = "--linear" in argv
    # Boosts are scarce, so you never spend one on an average fixture — you
    # wait for a good one. Quality above 1 shrinks the chance of not winning.
    # It's a proxy for a favourable draw, not a fixture model.
    quality = 1.0
    if "--good-fixture" in argv:
        i = argv.index("--good-fixture")
        quality = float(argv[i + 1]) if i + 1 < len(argv) else 1.6

    pct_of = ((lambda p: linear_pct(p, ceiling=ceiling)) if use_linear
              else (lambda p: scaled(p, ceiling)))

    label = "linear ramp" if use_linear else "stepped bands"
    note = f", spent in a fixture of quality {quality}" if quality != 1.0 else ""
    print(f"Manager boost — {label}, 10% floor to {ceiling:.0f}% ceiling{note}\n")
    print(f"{'Pos':>3}  {'Boost':>6}  {'Wins':>5}  {'Pays':>6} {'Blanks':>7}  "
          f"{'Per use':>8}  {'Season':>7}")
    print("-" * 62)

    evs = []
    for pos in range(1, 21):
        pct = pct_of(pos)
        win, draw = outcome(pos, quality)
        chance = payout_chance(pos, quality)
        ev = pct * chance
        blank = 1 - win - draw
        evs.append((pos, ev, blank))
        # A season's worth: three uses on a typical 50-point XI.
        season = 3 * XI_POINTS * ev / 100
        print(f"{pos:>3}  {pct:>5.1f}%  {win:>5.0%}  {chance:>5.0%}  {blank:>6.0%}   "
              f"{XI_POINTS * ev / 100:>5.1f} pts  {season:>4.0f} pts")

    best = max(evs, key=lambda x: x[1])
    top, bottom = evs[0], evs[19]
    print()
    print("Read down, not across — each row is a different drafter's season.")
    print(f"  Top-four manager:   {top[1]:.1f}% a use, "
          f"{3 * XI_POINTS * top[1] / 100:.0f} pts a season, "
          f"blanks {top[2]:.0%} of the time")
    print(f"  Relegation manager: {bottom[1]:.1f}% a use, "
          f"{3 * XI_POINTS * bottom[1] / 100:.0f} pts a season, "
          f"blanks {bottom[2]:.0%} of the time")
    print(f"  Best hand to draw:  {best[0]}th, worth "
          f"{3 * XI_POINTS * best[1] / 100:.0f} pts a season")

    ratio = best[1] / top[1] if top[1] else float("inf")
    print(f"\nSpread the auction has to price: {ratio:.1f}x between the best and "
          f"worst hand.")
    if ratio > 3.0:
        print("That's wide enough that top-four managers go unsold — worth "
              "narrowing the bands.")
    elif ratio < 1.5:
        print("That's narrow enough that the scale barely matters — the "
              "mechanic is decorative.")
    else:
        print("Wide enough to be worth bidding on, narrow enough that a "
              "reliable manager still has\na buyer — especially in H2H, where "
              "blanking loses you the week outright.")

    how_many_uses(evs, quality)


def how_many_uses(evs, quality):
    """What a manager is worth at different numbers of boosts a season.

    The raw points understate it, because in a head-to-head league points
    only matter when they change a result. So this also prices the boost in
    the currency that decides the title: matches turned from a defeat into
    a win.
    """
    mean, spread, sample = league_shape()
    source = (f"measured from {sample} real team-gameweeks"
              if sample else "assumed — no gameweek data yet")
    print(f"\n{'=' * 62}")
    print(f"How many boosts make a manager worth drafting?")
    print(f"{'=' * 62}")
    print(f"Typical XI {mean:.0f} pts, spread {spread:.0f} ({source}).")

    # Price the median hand rather than the best or worst one.
    median_ev = sorted(e for _, e, _ in evs)[len(evs) // 2]
    per_use = mean * median_ev / 100
    # A squad's whole season, which is what the whole budget buys.
    season_squad = mean * SEASON_WEEKS
    flip = flip_chance(per_use, spread)

    print(f"A median manager's boost is worth {per_use:.1f} pts when spent, "
          f"and turns a\ndefeat into a win {flip:.0%} of the time it's used.\n")
    print(f"{'Uses':>5}  {'Points/season':>14}  {'Results flipped':>16}  "
          f"{'Worth':>7}")
    print("-" * 52)
    for uses in (1, 2, 3, 5, 8, 12, 19, 38):
        pts = uses * per_use
        flips = uses * flip
        # What that's worth out of a 50-budget that buys a whole season's
        # points: the same share of the budget as it is of the points.
        worth = BUDGET * pts / season_squad
        gap = SEASON_WEEKS / uses
        every = ("once" if uses == 1
                 else "every week" if gap < 1.5
                 else f"every {gap:.0f} weeks")
        print(f"{uses:>5}  {pts:>10.0f} pts  {flips:>13.1f}    "
              f"£{worth:>5.2f}   ({every})")

    print("\nA manager is worth bidding on when they cost less than they "
          "return. At three\nuses that's about £1 — the price of a bench "
          "player, so they'd go for scraps.")
    target = next((u for u in range(1, 39)
                   if BUDGET * u * per_use / season_squad >= 3.0), None)
    if target:
        print(f"To make one worth around £3 — a real squad slot, something "
              f"people fight over —\ntakes {target} uses, roughly one every "
              f"{SEASON_WEEKS / target:.0f} gameweeks.")
    one_flip = next((u for u in range(1, 39) if u * flip >= 1.0), None)
    if one_flip:
        print(f"To expect one flipped result a season takes {one_flip} uses.")


if __name__ == "__main__":
    main()
