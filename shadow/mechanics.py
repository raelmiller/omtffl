#!/usr/bin/env python3
"""League mechanics that don't exist in FPL: trades-with-points, a points
bank, waivers, and manager boosts.

These are the rules being prototyped, so they live in one place and are
parameterised — the point of the simulation is to find out whether they're
balanced, which means expecting to change the numbers.

The big structural change from plain FPL is that **squads are no longer
static**. Trades and waivers move players between teams mid-season, so a
squad is a function of gameweek, not a fixed list. Everything here is built
around `squad_at(team, gameweek)`.

Rules as implemented
--------------------
TRADE
  A gives B one or more players plus optionally N points, for players back.
  - Position counts must be preserved on both sides, otherwise a squad stops
    being a legal 2/5/5/3. A FWD-for-FWD swap is fine; FWD-for-MID isn't.
  - The N points are deducted from A's score in the gameweek the trade takes
    effect, and credited to B's bank.
  - N cannot exceed what A has actually scored this season so far, counting
    debts already promised away. A single gameweek can go negative — that's
    the gamble, and it's allowed to look ugly — but a **season total** can't.
    You can mortgage what you've earned; you can't play with house money.
  - Trades are bilateral and final. No veto, no commissioner.

BANK
  Credited by receiving points in a trade. Spendable in any later gameweek,
  declared before it starts, in whole or in part. Cannot go negative.
  Bank balances do NOT fund the points side of a trade — trade points are
  always mortgaged against your score — and don't count towards the offer
  cap above.

WAIVER
  Drop one player, add an unowned one of the same position. Unlimited, but
  claims are processed in a single run before the gameweek, and priority is
  a snake from the bottom of the table upwards: last place claims first in
  round one, first place claims first in round two, and so on. Each manager
  submits claims in their own priority order and lands at most one per round.

MANAGER BOOST
  Each team drafts one real Premier League manager and may use the boost
  THREE times a season, declared before a gameweek.
  - Size scales with the manager's club's league position going into that
    gameweek: 1st gets the smallest boost, 20th the largest. Backing a
    struggling side is the high-variance play. The table used is the live one
    — early-season volatility is part of the tactics, not a flaw to smooth.
  - Payout is decided by the club's real result that gameweek: a win pays in
    full, a draw pays half, a defeat pays nothing.
  - If the club doesn't play, nothing is paid and the use is NOT consumed.

Final gameweek score
--------------------
    XI points + boost + bank spent - trade debt
"""
from __future__ import annotations

import json
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data"

# ── Tunables ───────────────────────────────────────────────────────────────
# Boost size runs linearly from BOOST_MIN at 1st to BOOST_MAX at 20th. The
# league wanted "about 10% for Arteta, about 50% for a struggling side".
BOOST_MIN_PCT = 10.0
BOOST_MAX_PCT = 50.0
# Five bands of four places. Round numbers beat a smooth ramp here: "Leeds are
# 15th so that's 40%" is a thing you can work out in the pub, where 34.7% is
# not. See boost_scale.py for what each band is actually worth once you
# account for how often the club wins.
BOOST_BANDS = [
    (1, 4, 10.0),
    (5, 8, 20.0),
    (9, 12, 30.0),
    (13, 16, 40.0),
    (17, 20, 50.0),
]
BOOST_USES_PER_SEASON = 3
# Result multiplier: win pays in full, draw half, defeat nothing.
BOOST_RESULT = {"W": 1.0, "D": 0.5, "L": 0.0}
# Before any football has been played there is no table, so a first-gameweek
# boost is priced at mid-table rather than guessing.
NEUTRAL_POSITION = 10

SQUAD_SHAPE = {"GK": 2, "DEF": 5, "MID": 5, "FWD": 3}


# ── Transactions ───────────────────────────────────────────────────────────
def load_transactions(path=None):
    p = Path(path) if path else DATA / "transactions.json"
    if not p.exists():
        return []
    return json.loads(p.read_text()).get("transactions", [])


def position_counts(squad):
    counts = {}
    for p in squad:
        counts[p["position"]] = counts.get(p["position"], 0) + 1
    return counts


def validate_trade(tx, squads_now, accumulated=None):
    """Why a trade is illegal, or None if it's fine.

    `accumulated` is what the offering manager has scored so far this season.
    Pass it to enforce the offer cap; without it the cap can't be checked and
    isn't guessed at.
    """
    a, b = tx["from"], tx["to"]
    for t in (a, b):
        if t not in squads_now:
            return f"unknown team {t}"

    out_ids = {p["id"] for p in tx.get("players_out", [])}
    in_ids = {p["id"] for p in tx.get("players_in", [])}
    a_ids = {p["id"] for p in squads_now[a]}
    b_ids = {p["id"] for p in squads_now[b]}

    missing_a = out_ids - a_ids
    if missing_a:
        return f"{a} doesn't own player(s) {sorted(missing_a)}"
    missing_b = in_ids - b_ids
    if missing_b:
        return f"{b} doesn't own player(s) {sorted(missing_b)}"

    # Position counts must balance or a squad stops being legal.
    out_pos = position_counts(tx.get("players_out", []))
    in_pos = position_counts(tx.get("players_in", []))
    if out_pos != in_pos:
        return (f"positions don't balance: {a} sends {out_pos}, receives {in_pos} "
                "— a trade must preserve each squad's shape")

    pts = tx.get("points", 0)
    if pts < 0:
        return "points offered cannot be negative"
    if accumulated is not None and pts > accumulated:
        return (f"offers {pts} points but has only scored {accumulated} this season "
                "— you can't offer more than you've accumulated")
    return None


# ── Waivers ────────────────────────────────────────────────────────────────
def snake_order(table_order, rounds):
    """Claim order for each round, snaking from the bottom of the table up.

    `table_order` is the standings, best team first. Round one runs bottom to
    top, round two top to bottom, and so on — so the team propping up the
    table gets first pick of the week, but doesn't get first pick of every
    round as well.
    """
    bottom_up = list(reversed(table_order))
    return [bottom_up if r % 2 == 0 else list(table_order) for r in range(rounds)]


def process_waivers(claims, squads, table_order):
    """Run a week's waiver claims and report what happened.

    `claims` maps a team to its claims in the manager's own priority order,
    each `{"drop": player, "add": player}`. A manager attempts exactly one
    claim per round. **Losing a race costs you the round** — your next choice
    waits until the snake comes back to you, rather than being taken off the
    rank immediately. That's what stops the bottom club hoovering up the whole
    free-agent list in one pass.

    A claim that was never contested — a malformed one, or one whose drop has
    already been used — is discarded without costing the round, since nobody
    beat you to anything.

    Returns (results, problems) and mutates `squads` in place.
    """
    results, problems = [], []
    pending = {t: list(cs) for t, cs in claims.items() if cs}
    if not pending:
        return results, problems
    # Managers usually name the same player to drop against several claims —
    # "whoever I land, this is the one going". Once that player is gone the
    # later claims are spent, not illegal.
    dropped_already = set()

    rounds = max(len(cs) for cs in pending.values())
    for round_no, order in enumerate(snake_order(table_order, rounds), start=1):
        for team in order:
            queue = pending.get(team)
            if not queue:
                continue
            if team not in squads:
                problems.append(f"waiver claim by unknown team {team}")
                pending[team] = []
                continue

            owned = {p["id"] for s in squads.values() for p in s}
            spent_round = False
            while queue and not spent_round:
                claim = queue.pop(0)
                drop_id = claim["drop"]["id"]
                add = claim["add"]
                mine = {p["id"] for p in squads[team]}
                if drop_id not in mine:
                    if drop_id in dropped_already:
                        results.append({"round": round_no, "team": team, "add": add,
                                        "drop": claim["drop"], "landed": False,
                                        "why": "already used that drop"})
                    else:
                        problems.append(
                            f"waiver by {team}: doesn't own "
                            f"{claim['drop'].get('name', drop_id)}")
                    continue
                if add["id"] in owned:
                    # Not an error — someone earlier in the snake got there
                    # first, which is exactly what priority is for. It costs
                    # the round: the next choice waits for the snake to come
                    # back round.
                    results.append({"round": round_no, "team": team, "add": add,
                                    "drop": claim["drop"], "landed": False,
                                    "why": "already claimed"})
                    spent_round = True
                    continue
                dropped = next(p for p in squads[team] if p["id"] == drop_id)
                if dropped["position"] != add["position"]:
                    problems.append(
                        f"waiver by {team}: {add['position']} for {dropped['position']} "
                        "would break the squad shape")
                    continue
                squads[team] = [p for p in squads[team] if p["id"] != drop_id] + [add]
                dropped_already.add(drop_id)
                spent_round = True
                results.append({"round": round_no, "team": team, "add": add,
                                "drop": claim["drop"], "landed": True, "why": None})
    return results, problems


def apply_transactions(base_squads, transactions, upto_gameweek,
                       points_to_date=None, standings=None, managers=None):
    """Squad state and per-gameweek adjustments up to and including a gameweek.

    `points_to_date[gw][team]` is what a team had scored going into that
    gameweek; supply it to enforce the trade offer cap. `standings[gw]` is the
    table going into that gameweek, best first, which sets waiver priority.
    `managers[team]` describes the drafted manager, and a `sacked_from`
    gameweek there kills that team's remaining boosts. All three are optional
    — without them those rules can't be checked, and the caller is told so
    rather than the rule being silently skipped.

    Returns (squads, adjustments, boosts_used, bank, problems) where
    `adjustments[gw][team]` is the net points change for that gameweek.
    """
    squads = {t["key"]: list(t["squad"]) for t in base_squads["teams"]}
    bank = {k: 0 for k in squads}
    boosts_used = {k: 0 for k in squads}
    adjustments = {}
    boost_log = []
    problems = []

    def adjust(gw, team, delta):
        adjustments.setdefault(gw, {}).setdefault(team, 0)
        adjustments[gw][team] += delta

    # Within a gameweek, order matters: everything is declared in the same
    # pre-deadline window, so a trade must credit the bank before a spend can
    # draw on it. Sorting alphabetically would put "bank_use" first and
    # wrongly reject spending points received the same week.
    ORDER = {"trade": 0, "waiver": 1, "waiver_run": 1, "bank_use": 2, "boost": 3}
    waiver_log = []
    committed = {}  # (gameweek, team) -> points already promised away that week
    for tx in sorted(transactions,
                     key=lambda t: (t.get("gameweek", 0), ORDER.get(t.get("type"), 9))):
        gw = tx.get("gameweek")
        if gw is None or gw > upto_gameweek:
            continue
        kind = tx.get("type")

        if kind == "trade":
            a = tx.get("from")
            accumulated = (points_to_date or {}).get(gw, {}).get(a)
            if accumulated is not None:
                # Earlier gameweeks' debts are already netted into the running
                # total; this gameweek's aren't yet, so subtract them here.
                # Two 40-point offers in one week off a 50-point season would
                # otherwise both pass and leave the season total at -30.
                accumulated -= committed.get((gw, a), 0)
            problem = validate_trade(tx, squads, accumulated)
            if problem:
                problems.append(f"GW{gw} trade {a}→{tx.get('to')}: {problem}")
                continue
            b = tx["to"]
            out_ids = {p["id"] for p in tx["players_out"]}
            in_ids = {p["id"] for p in tx["players_in"]}
            moving_out = [p for p in squads[a] if p["id"] in out_ids]
            moving_in = [p for p in squads[b] if p["id"] in in_ids]
            squads[a] = [p for p in squads[a] if p["id"] not in out_ids] + moving_in
            squads[b] = [p for p in squads[b] if p["id"] not in in_ids] + moving_out

            pts = tx.get("points", 0)
            if pts:
                adjust(gw, a, -pts)   # mortgaged against this gameweek
                bank[b] += pts        # spendable whenever B likes
                committed[(gw, a)] = committed.get((gw, a), 0) + pts

        elif kind == "bank_use":
            team, pts = tx["team"], tx["points"]
            if pts <= 0:
                problems.append(f"GW{gw} bank use by {team}: must be positive")
                continue
            if pts > bank[team]:
                problems.append(
                    f"GW{gw} bank use by {team}: wants {pts}, only {bank[team]} banked")
                continue
            bank[team] -= pts
            adjust(gw, team, pts)

        elif kind == "waiver_run":
            order = (standings or {}).get(gw)
            if order is None:
                order = sorted(squads)
                problems.append(
                    f"GW{gw} waiver run: no standings for that gameweek, so priority "
                    "fell back to alphabetical instead of snaking from the bottom")
            res, probs = process_waivers(tx.get("claims", {}), squads, order)
            for r in res:
                r["gameweek"] = gw
            waiver_log.extend(res)
            problems.extend(f"GW{gw} {p}" for p in probs)

        elif kind == "waiver":
            team = tx["team"]
            drop_id = tx["drop"]["id"]
            add = tx["add"]
            owned = {p["id"] for s in squads.values() for p in s}
            if drop_id not in {p["id"] for p in squads[team]}:
                problems.append(f"GW{gw} waiver by {team}: doesn't own {drop_id}")
                continue
            if add["id"] in owned:
                problems.append(f"GW{gw} waiver by {team}: {add['name']} is already owned")
                continue
            dropped = next(p for p in squads[team] if p["id"] == drop_id)
            if dropped["position"] != add["position"]:
                problems.append(
                    f"GW{gw} waiver by {team}: {add['position']} for {dropped['position']} "
                    "would break the squad shape")
                continue
            squads[team] = [p for p in squads[team] if p["id"] != drop_id] + [add]

        elif kind == "boost":
            team = tx["team"]
            manager = (managers or {}).get(team) or {}
            sacked_from = manager.get("sacked_from")
            if sacked_from is not None and gw >= sacked_from:
                # Drafting a manager under pressure is the gamble. If they go,
                # the remaining boosts go with them — no replacement, no
                # re-draft. The use isn't consumed because it can't be used.
                who = manager.get("name", "their manager")
                problems.append(
                    f"GW{gw} boost by {team}: {who} was sacked in GW{sacked_from} "
                    "— the boost goes with them")
                continue
            if boosts_used[team] >= BOOST_USES_PER_SEASON:
                problems.append(
                    f"GW{gw} boost by {team}: already used all "
                    f"{BOOST_USES_PER_SEASON}")
                continue
            # Whether it pays, and how much, is resolved later against the
            # real result — recorded here so the caller can price it.
            boost_log.append({"gameweek": gw, "team": team})
            boosts_used[team] += 1

    return squads, adjustments, boost_log, bank, waiver_log, problems


def squads_at(base_squads, transactions, gameweek):
    """Everyone's roster as it stood for a given gameweek."""
    squads, *_ = apply_transactions(base_squads, transactions, gameweek)
    return squads


# ── Manager boosts ─────────────────────────────────────────────────────────
def league_table(pl_fixtures, upto_gameweek):
    """Premier League standings from results before `upto_gameweek`.

    Derived from played fixtures rather than read from a snapshot, so the
    boost is priced on the table as it actually stood at the time.
    """
    stats = {}
    for f in pl_fixtures:
        if not f["finished"] or f["event"] is None or f["event"] >= upto_gameweek:
            continue
        h, a = f["team_h"], f["team_a"]
        hs, as_ = f["team_h_score"], f["team_a_score"]
        if hs is None or as_ is None:
            continue
        for t in (h, a):
            stats.setdefault(t, {"pts": 0, "gd": 0, "gf": 0})
        stats[h]["gf"] += hs; stats[h]["gd"] += hs - as_
        stats[a]["gf"] += as_; stats[a]["gd"] += as_ - hs
        if hs > as_:
            stats[h]["pts"] += 3
        elif as_ > hs:
            stats[a]["pts"] += 3
        else:
            stats[h]["pts"] += 1; stats[a]["pts"] += 1

    ranked = sorted(stats.items(), key=lambda kv: (-kv[1]["pts"], -kv[1]["gd"], -kv[1]["gf"]))
    return {team: i + 1 for i, (team, _) in enumerate(ranked)}


def boost_pct(position):
    """Boost size for a club sitting in `position`. 1st smallest, 20th largest.

    Stepped in bands rather than a smooth ramp, so the numbers are round
    enough to hold in your head — and so crossing a band boundary is a real
    event during the week rather than a rounding difference.
    """
    pos = max(1, min(20, position))
    for lo, hi, pct in BOOST_BANDS:
        if lo <= pos <= hi:
            return pct
    return BOOST_MAX_PCT


def club_result(pl_fixtures, club_id, gameweek):
    """'W'/'D'/'L' for a club in a gameweek, or None if they didn't play.

    Double gameweeks are averaged rather than taking the first match, so a
    week of one win and one defeat pays out at half.
    """
    results = []
    for f in pl_fixtures:
        if f["event"] != gameweek or not f["finished"]:
            continue
        hs, as_ = f["team_h_score"], f["team_a_score"]
        if hs is None or as_ is None:
            continue
        if f["team_h"] == club_id:
            results.append("W" if hs > as_ else "D" if hs == as_ else "L")
        elif f["team_a"] == club_id:
            results.append("W" if as_ > hs else "D" if as_ == hs else "L")
    return results or None


def boost_value(xi_points, club_id, gameweek, pl_fixtures):
    """What a boost is worth, and why.

    Returns (points, detail). Zero points and `played=False` means the club
    had no fixture, in which case the caller should not consume a use.
    """
    results = club_result(pl_fixtures, club_id, gameweek)
    table = league_table(pl_fixtures, gameweek)
    position = table.get(club_id, NEUTRAL_POSITION) if table else NEUTRAL_POSITION
    pct = boost_pct(position)

    if not results:
        return 0, {"played": False, "position": position, "pct": pct,
                   "result": None, "multiplier": 0.0}

    multiplier = sum(BOOST_RESULT[r] for r in results) / len(results)
    points = round(xi_points * (pct / 100.0) * multiplier)
    return points, {
        "played": True, "position": position, "pct": pct,
        "result": "/".join(results), "multiplier": multiplier,
    }
