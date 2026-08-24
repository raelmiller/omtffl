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
  - A does not need the points banked — they're mortgaging future score,
    which is what makes the gamble interesting.

BANK
  Credited by receiving points in a trade. Spendable in any later gameweek,
  declared before it starts, in whole or in part. Can also fund the points
  side of a later trade. Cannot go negative.

WAIVER
  A free transfer: drop one player, add an unowned one of the same position.

MANAGER BOOST
  Each team drafts one real Premier League manager and may use the boost
  THREE times a season, declared before a gameweek.
  - Size scales with the manager's club's league position going into that
    gameweek: 1st gets the smallest boost, 20th the largest. Backing a
    struggling side is the high-variance play.
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


def validate_trade(tx, squads_now):
    """Why a trade is illegal, or None if it's fine."""
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

    if tx.get("points", 0) < 0:
        return "points offered cannot be negative"
    return None


def apply_transactions(base_squads, transactions, upto_gameweek):
    """Squad state and per-gameweek adjustments up to and including a gameweek.

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
    ORDER = {"trade": 0, "waiver": 1, "bank_use": 2, "boost": 3}
    for tx in sorted(transactions,
                     key=lambda t: (t.get("gameweek", 0), ORDER.get(t.get("type"), 9))):
        gw = tx.get("gameweek")
        if gw is None or gw > upto_gameweek:
            continue
        kind = tx.get("type")

        if kind == "trade":
            problem = validate_trade(tx, squads)
            if problem:
                problems.append(f"GW{gw} trade {tx.get('from')}→{tx.get('to')}: {problem}")
                continue
            a, b = tx["from"], tx["to"]
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
            if boosts_used[team] >= BOOST_USES_PER_SEASON:
                problems.append(
                    f"GW{gw} boost by {team}: already used all "
                    f"{BOOST_USES_PER_SEASON}")
                continue
            # Whether it pays, and how much, is resolved later against the
            # real result — recorded here so the caller can price it.
            boost_log.append({"gameweek": gw, "team": team})
            boosts_used[team] += 1

    return squads, adjustments, boost_log, bank, problems


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
    """Boost size for a club sitting in `position`. 1st smallest, 20th largest."""
    pos = max(1, min(20, position))
    span = BOOST_MAX_PCT - BOOST_MIN_PCT
    return BOOST_MIN_PCT + (pos - 1) * span / 19.0


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
