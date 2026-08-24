#!/usr/bin/env python3
"""Work out what the manager boost should pay at each league position.

The boost multiplies your XI by a percentage set by your manager's club
position, and only pays if their club gets a result. Those two things pull in
opposite directions: backing a struggling club pays a bigger percentage, but
struggling clubs lose more often. Whether the mechanic is interesting depends
entirely on whether the second effect cancels the first.

This prints the scale and, more usefully, the **expected** value at each
position — percentage times the chance of actually getting paid. A flat
expected-value curve means every choice is equally good, which is boring. A
curve that falls away at the bottom means nobody ever backs a relegation
club, which wastes half the scale.

The win rates are league-position averages, not this season's clubs, and they
are assumptions rather than measured data — edit `WIN_RATES` if you'd rather
use different ones. The shape of the conclusion is not sensitive to small
changes in them.

Usage
-----
    python3 shadow/boost_scale.py              # the agreed stepped scale
    python3 shadow/boost_scale.py --ceiling 70 # try a higher top band
    python3 shadow/boost_scale.py --linear     # compare against a smooth ramp
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


def payout_chance(position):
    """Expected share of the headline percentage that actually gets paid."""
    win, draw = WIN_RATES[position]
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
    # You get three boosts across 38 gameweeks, so you never spend one on an
    # average fixture — you wait for a good one. This models that: the club
    # keeps its own band, but plays with the outcome rates of a side N places
    # higher. It's a proxy for a favourable draw, not a fixture model.
    lift = 0
    if "--good-fixture" in argv:
        i = argv.index("--good-fixture")
        lift = int(argv[i + 1]) if i + 1 < len(argv) else 6

    pct_of = ((lambda p: linear_pct(p, ceiling=ceiling)) if use_linear
              else (lambda p: scaled(p, ceiling)))

    label = "linear ramp" if use_linear else "stepped bands"
    note = f", used in a fixture worth {lift} places" if lift else ""
    print(f"Manager boost — {label}, 10% floor to {ceiling:.0f}% ceiling{note}\n")
    print(f"{'Pos':>3}  {'Boost':>6}  {'W':>5} {'D':>5}  {'Pays':>6}  "
          f"{'Expected':>9}   {'On a 50-pt XI':>14}")
    print("-" * 62)

    evs = []
    for pos in range(1, 21):
        pct = pct_of(pos)
        form_pos = max(1, pos - lift)
        win, draw = WIN_RATES[form_pos]
        chance = payout_chance(form_pos)
        ev = pct * chance
        evs.append((pos, ev))
        print(f"{pos:>3}  {pct:>5.1f}%  {win:>5.0%} {draw:>5.0%}  {chance:>5.0%}   "
              f"{ev:>8.1f}%   {50 * ev / 100:>13.1f} pts")

    best = max(evs, key=lambda x: x[1])
    worst_half = min(evs[10:], key=lambda x: x[1])
    top = evs[0][1]
    print()
    print(f"Best position to back:   {best[0]}th, worth {best[1]:.1f}% of your XI")
    print(f"Backing the leaders:     {top:.1f}%")
    print(f"Worst in the bottom half: {worst_half[0]}th, worth {worst_half[1]:.1f}%")
    spread = max(e for _, e in evs) - min(e for _, e in evs)
    print(f"Spread across the table: {spread:.1f} percentage points")
    if evs[19][1] < evs[9][1]:
        need = evs[9][1] / payout_chance(max(1, 20 - lift))
        print(f"\n20th is worth less than 10th, so nobody would ever back the "
              f"bottom club.\nIt would need a {need:.0f}% band to match "
              f"mid-table.")


if __name__ == "__main__":
    main()
