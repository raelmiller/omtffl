#!/usr/bin/env python3
"""Weekly lineup submission — the XI a manager actually picked.

Everything else in the shadow league scores the *best* legal XI in hindsight,
which tests the engine but flatters everyone: nobody picks perfectly. This
module is the other half — managers declare eleven before the deadline and
live with it.

Rules as implemented
--------------------
SUBMISSION
  Eleven starters and four ordered substitutes, from the fifteen owned at
  that gameweek (so trades and waivers change what's legal to pick).
  Formation must be legal: 1 GK, 3-5 DEF, 2-5 MID, 1-3 FWD.

DEADLINE
  A lineup counts if it was submitted before the gameweek's deadline. Where
  a submission carries no timestamp we accept it and say so, rather than
  silently trusting or silently binning it.

ROLLOVER
  Forget to submit and last week's team plays again — the same courtesy FPL
  Draft extends. Players no longer owned drop out and the bench covers them.

AUTOSUBS
  A starter who doesn't play at all is replaced by the first substitute who
  did, provided the formation stays legal. Keepers only replace keepers.
  This is what stops one postponed fixture wrecking a week.

Usage
-----
    python3 shadow/lineups.py --check          # validate every submission
    python3 shadow/lineups.py --template 2     # blank submission for GW2
    python3 shadow/lineups.py --template 2 --suggest
                                               # pre-filled on form, no hindsight
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data"

GK_COUNT = 1
LIMITS = {"GK": (1, 1), "DEF": (3, 5), "MID": (2, 5), "FWD": (1, 3)}
XI_SIZE = 11
BENCH_SIZE = 4


# ── Loading ────────────────────────────────────────────────────────────────
def load_lineups(path=None):
    """Submitted lineups, keyed by gameweek then team."""
    p = Path(path) if path else DATA / "lineups.json"
    if not p.exists():
        return {}
    raw = json.loads(p.read_text()).get("lineups", {})
    return {int(gw): teams for gw, teams in raw.items()}


def position_counts(players):
    counts = {"GK": 0, "DEF": 0, "MID": 0, "FWD": 0}
    for p in players:
        counts[p["position"]] = counts.get(p["position"], 0) + 1
    return counts


def legal_formation(players):
    """Why an XI is illegal, or None if it's fine."""
    if len(players) != XI_SIZE:
        return f"{len(players)} players, needs {XI_SIZE}"
    counts = position_counts(players)
    for pos, (lo, hi) in LIMITS.items():
        n = counts.get(pos, 0)
        if not lo <= n <= hi:
            return f"{n} {pos}, needs {lo}-{hi}"
    return None


# ── Validation ─────────────────────────────────────────────────────────────
def validate(entry, squad, deadline=None):
    """Check one team's submission against the squad they owned.

    Returns (errors, warnings). Errors mean the lineup can't be used;
    warnings are things worth saying out loud but not fatal.
    """
    errors, warnings = [], []
    by_id = {p["id"]: p for p in squad}

    xi_ids = list(entry.get("xi", []))
    if len(set(xi_ids)) != len(xi_ids):
        errors.append("the same player is named twice in the XI")
        xi_ids = list(dict.fromkeys(xi_ids))

    unowned = [i for i in xi_ids if i not in by_id]
    if unowned:
        errors.append(f"not in the squad that gameweek: {sorted(unowned)}")

    xi = [by_id[i] for i in xi_ids if i in by_id]
    problem = legal_formation(xi)
    if problem:
        errors.append(f"illegal XI — {problem}")

    bench_ids = list(entry.get("bench", []))
    if bench_ids:
        overlap = set(bench_ids) & set(xi_ids)
        if overlap:
            errors.append(f"named both starting and benched: {sorted(overlap)}")
        missing = [i for i in bench_ids if i not in by_id]
        if missing:
            errors.append(f"benched players not in the squad: {sorted(missing)}")
        left_out = [p["id"] for p in squad if p["id"] not in xi_ids and p["id"] not in bench_ids]
        if left_out and len(bench_ids) < BENCH_SIZE:
            warnings.append(
                f"bench has {len(bench_ids)} of {BENCH_SIZE}; "
                f"{len(left_out)} squad player(s) unordered and will sub on last")
    else:
        warnings.append("no bench order given — substitutes come on in squad order")

    submitted = entry.get("submitted_at")
    if deadline and submitted:
        if submitted > deadline:
            errors.append(f"submitted {submitted}, after the {deadline} deadline")
    elif deadline and not submitted:
        warnings.append("no submission time recorded, so the deadline can't be checked")

    return errors, warnings


# ── Choosing which lineup applies ──────────────────────────────────────────
def lineup_source_gameweek(team, gameweek, lineups):
    """Which round the eleven applying to `gameweek` was actually picked in.

    The same fallback `effective_lineup` walks — this round if there is a pick
    for it, otherwise the most recent earlier one — exposed on its own so a
    caller can ask *whether a human chose this team* without parsing the
    sentence `effective_lineup` writes for the page.

    Returns None when nobody has ever picked for this team.
    """
    if lineups.get(gameweek, {}).get(team) is not None:
        return gameweek
    earlier = [gw for gw in sorted(lineups) if gw < gameweek and team in lineups[gw]]
    return earlier[-1] if earlier else None


def effective_lineup(team, gameweek, lineups, squad):
    """The XI and bench that actually apply, and how we got there.

    Falls back through: this week's submission → the most recent earlier
    submission (rolled over) → nothing. The third case is the caller's to
    handle; there is no sensible way to invent a team someone never picked.
    """
    entry = lineups.get(gameweek, {}).get(team)
    source = "submitted"
    if entry is None:
        earlier = [gw for gw in sorted(lineups) if gw < gameweek and team in lineups[gw]]
        if not earlier:
            return None, None, "none"
        entry = lineups[earlier[-1]][team]
        source = f"rolled over from GW{earlier[-1]}"

    by_id = {p["id"]: p for p in squad}
    xi = [by_id[i] for i in entry.get("xi", []) if i in by_id]
    named = {p["id"] for p in xi}
    bench = [by_id[i] for i in entry.get("bench", []) if i in by_id and i not in named]
    named |= {p["id"] for p in bench}
    # Anyone unaccounted for — squad churn since the lineup was picked, or an
    # incomplete bench — goes to the back of the bench rather than vanishing.
    bench += [p for p in squad if p["id"] not in named]

    if len(xi) < XI_SIZE:
        xi, bench, filled = _fill_to_legal(xi, bench)
        if filled:
            source += f", {filled} slot(s) filled from the bench"

    return xi, bench, source


def _fill_to_legal(xi, bench):
    """Top a short XI back up to eleven, keeping the formation legal.

    Happens when a rolled-over lineup names someone since traded away.
    """
    xi, bench = list(xi), list(bench)
    filled = 0
    while len(xi) < XI_SIZE:
        pick = None
        for cand in bench:
            if legal_formation(xi + [cand]) is None or _could_reach_legal(xi + [cand]):
                pick = cand
                break
        if pick is None:
            break
        xi.append(pick)
        bench.remove(pick)
        filled += 1
    return xi, bench, filled


def _could_reach_legal(partial):
    """Whether a partial XI can still be completed into a legal one."""
    counts = position_counts(partial)
    if len(partial) > XI_SIZE:
        return False
    for pos, (_, hi) in LIMITS.items():
        if counts.get(pos, 0) > hi:
            return False
    # Enough slots left to reach every minimum?
    shortfall = sum(max(0, lo - counts.get(pos, 0)) for pos, (lo, _) in LIMITS.items())
    return shortfall <= XI_SIZE - len(partial)


# ── Automatic substitutions ────────────────────────────────────────────────
def round_is_over(gw_data) -> bool:
    """Whether every match in a gameweek has been played.

    `finished` is FPL's own flag for the last whistle; `data_checked` marks
    the bonus double-check that follows a day or so later. Either means the
    football is done, which is what a settlement rule needs to know.
    """
    return bool(gw_data.get("data_checked") or gw_data.get("finished"))


def apply_autosubs(xi, bench, minutes, settled=True):
    """Swap out starters who didn't play, keeping the formation legal.

    Returns (final_xi, substitutions) where each substitution is
    (player_off, player_on). Keepers only replace keepers, and an outfield
    swap only happens if the XI is still legal afterwards — which is why a
    lone forward can't be replaced by a fifth defender.

    `settled` says the minutes are final. Mid-round they are not, and this
    rule cannot tell "didn't play" from "hasn't kicked off yet" — both are
    zero minutes. Run on Saturday evening it would bench a starter whose
    match is on Monday and hand his shirt to whoever happened to play first,
    which is not what the manager picked and not what FPL does: autosubs are
    an end-of-round settlement. So an unsettled round makes no substitutions
    at all, and the eleven stands as picked until the last whistle.
    """
    xi, bench = list(xi), list(bench)
    subs = []
    if not settled:
        return xi, subs

    blanks = [p for p in xi if not minutes.get(p["id"], 0)]
    for off in blanks:
        for on in bench:
            if not minutes.get(on["id"], 0):
                continue
            if (off["position"] == "GK") != (on["position"] == "GK"):
                continue  # keepers are their own market
            trial = [on if p["id"] == off["id"] else p for p in xi]
            if legal_formation(trial) is None:
                xi = trial
                bench.remove(on)
                subs.append((off, on))
                break
    return xi, subs


def minutes_from_gameweek(gw_data):
    return {el["id"]: (el.get("stats", {}) or {}).get("minutes") or 0
            for el in gw_data.get("elements", [])}


# ── Helping managers pick ──────────────────────────────────────────────────
def suggest_lineup(squad, form):
    """A plausible XI from prior form — deliberately not hindsight.

    `form` maps player id to points from gameweeks already played, so this
    only ever knows what a manager would have known. Used to pre-fill a
    submission template and to model "what if everyone picked sensibly".
    """
    ranked = sorted(squad, key=lambda p: -form.get(p["id"], 0))
    xi = []
    for p in ranked:
        if len(xi) == XI_SIZE:
            break
        if _could_reach_legal(xi + [p]):
            xi.append(p)
    # Guarantee legality even if form ordering painted us into a corner.
    if legal_formation(xi) is not None:
        rest = [p for p in ranked if p not in xi]
        xi, _, _ = _fill_to_legal(xi, rest)
    named = {p["id"] for p in xi}
    bench = [p for p in ranked if p["id"] not in named]
    # A keeper on the bench is only ever cover, so put them last.
    bench.sort(key=lambda p: (p["position"] == "GK", -form.get(p["id"], 0)))
    return xi, bench


def form_before(gameweek, positions):
    """Points each player scored in gameweeks before this one."""
    from scoring import score_entry

    form = {}
    for f in sorted(DATA.glob("gw*.json")):
        n = int(f.stem[2:])
        if n >= gameweek:
            continue
        gw = json.loads(f.read_text())
        for el in gw["elements"]:
            pos = positions.get(el["id"])
            if pos is not None:
                form[el["id"]] = form.get(el["id"], 0) + score_entry(el, pos)
    return form


# ── CLI ────────────────────────────────────────────────────────────────────
def _load(name, required=True):
    p = DATA / name
    if not p.exists():
        if required:
            sys.exit(f"No data/{name}")
        return None
    return json.loads(p.read_text())


def _squads_at(gameweek):
    """Squads as they stood for a gameweek, with trades and waivers applied."""
    squads_base = _load("squads.json")
    try:
        from mechanics import load_transactions, squads_at
        txs = load_transactions()
    except Exception:
        txs = []
    if txs:
        return squads_at(squads_base, txs, gameweek), squads_base
    return {t["key"]: list(t["squad"]) for t in squads_base["teams"]}, squads_base


def cmd_check():
    lineups = load_lineups()
    if not lineups:
        print("No data/lineups.json yet — nothing submitted.")
        print("Generate one with: python3 shadow/lineups.py --template 1 --suggest")
        return 0
    squads_base = _load("squads.json")
    names = {t["key"]: t["team"] for t in squads_base["teams"]}
    deadlines = {}
    for f in sorted(DATA.glob("gw*.json")):
        gw = json.loads(f.read_text())
        deadlines[gw["gameweek"]] = gw.get("deadline_time")

    bad = 0
    for gw in sorted(lineups):
        squads, _ = _squads_at(gw)
        print(f"\nGameweek {gw}")
        missing = [k for k in squads if k not in lineups[gw]]
        for team, entry in sorted(lineups[gw].items()):
            errors, warnings = validate(entry, squads.get(team, []), deadlines.get(gw))
            label = names.get(team, team)
            if errors:
                bad += 1
                print(f"  ✗ {label}")
                for e in errors:
                    print(f"      {e}")
            else:
                print(f"  ✓ {label}")
            for w in warnings:
                print(f"      note: {w}")
        if missing:
            print(f"  — no submission: {', '.join(names.get(m, m) for m in sorted(missing))}"
                  f" (last week's team rolls over)")
    print()
    print("All submissions legal." if not bad else f"{bad} submission(s) rejected.")
    return 1 if bad else 0


def cmd_template(gameweek, suggest=False):
    squads, squads_base = _squads_at(gameweek)
    names = {t["key"]: t["team"] for t in squads_base["teams"]}
    positions = {}
    if suggest:
        from score_league import load_positions
        pos_map = load_positions()
        positions = form_before(gameweek, pos_map)

    out = {}
    for key in sorted(squads):
        squad = squads[key]
        if suggest:
            ranking = positions
            if not ranking:
                # No football played yet, so there is no form to go on. Draft
                # price is the next best thing and, crucially, was known
                # before the deadline — this stays a no-hindsight suggestion.
                ranking = {p["id"]: p.get("price", 0) for p in squad}
            xi, bench = suggest_lineup(squad, ranking)
        else:
            xi, bench = [], []
        out[key] = {
            "team": names.get(key, key),
            "xi": [p["id"] for p in xi],
            "bench": [p["id"] for p in bench],
            "_names": {
                "xi": [f"{p['position']} {p['name']}" for p in xi],
                "bench": [f"{p['position']} {p['name']}" for p in bench],
            },
        }
    print(json.dumps({"lineups": {str(gameweek): out}}, indent=2))
    return 0


def main():
    argv = sys.argv[1:]
    if "--check" in argv or not argv:
        return cmd_check()
    if "--template" in argv:
        i = argv.index("--template")
        if i + 1 >= len(argv):
            sys.exit("--template needs a gameweek number")
        return cmd_template(int(argv[i + 1]), suggest="--suggest" in argv)
    sys.exit(__doc__)


if __name__ == "__main__":
    sys.exit(main())
