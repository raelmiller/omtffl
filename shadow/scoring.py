#!/usr/bin/env python3
"""Fantasy scoring rules engine.

Computes a player's points for a gameweek from their raw stats, rather than
reading FPL's own `total_points`. That distinction is the whole point of the
shadow season: copying FPL's number would prove nothing, whereas deriving it
lets us (a) check our engine against theirs player-by-player, and (b) change
the rules next season without depending on them at all.

The rules live in RULES as data, not code, so the league's own variants —
different position structure, different clean-sheet values, whatever — are a
config change rather than a rewrite.

Positions are FPL's element_type: 1=GK, 2=DEF, 3=MID, 4=FWD.
"""
from __future__ import annotations

import itertools

GK, DEF, MID, FWD = 1, 2, 3, 4
POSITION_NAMES = {GK: "GK", DEF: "DEF", MID: "MID", FWD: "FWD"}

# ── Rule table ─────────────────────────────────────────────────────────────
# Mirrors the official FPL rules for 2025/26 onwards, including the defensive
# contribution points introduced that season. Values are per position where
# the position matters.
RULES = {
    # Appearance
    "minutes_short": {"threshold": 1, "points": 1},    # played at all
    "minutes_long": {"threshold": 60, "points": 2},    # 60+ minutes
    # Attacking returns
    "goal": {GK: 6, DEF: 6, MID: 5, FWD: 4},
    "assist": {GK: 3, DEF: 3, MID: 3, FWD: 3},
    # Clean sheets — only counted if the player managed 60+ minutes
    "clean_sheet": {GK: 4, DEF: 4, MID: 1, FWD: 0},
    "clean_sheet_min_minutes": 60,
    # Goals conceded: -1 per N conceded, GK and DEF only
    "conceded_per": 2,
    "conceded_points": {GK: -1, DEF: -1, MID: 0, FWD: 0},
    # Goalkeeping
    "saves_per": 3,
    "saves_points": 1,
    "penalty_save": 5,
    # Misses and discipline
    "penalty_miss": -2,
    "own_goal": -2,
    "yellow_card": -1,
    "red_card": -3,
    # Defensive contributions (2025/26+): a flat bonus for hitting a
    # threshold of defensive actions. Defenders count clearances, blocks,
    # interceptions and tackles; everyone else also counts recoveries.
    "defcon_points": 2,
    "defcon_threshold": {DEF: 10, MID: 12, FWD: 12},  # GK: not applicable
}


def _stat(stats: dict, *names, default=0):
    """First present stat among `names`.

    The live endpoint and element-summary don't always agree on field names
    across seasons, so callers pass the aliases they know about.
    """
    for n in names:
        if n in stats and stats[n] is not None:
            return stats[n]
    return default


def defensive_actions(stats: dict, position: int) -> int:
    """Count of defensive actions relevant to this position.

    FPL sometimes exposes a precomputed `defensive_contribution`; when it's
    there we trust it, since that's the number their own scoring used.
    """
    precomputed = _stat(stats, "defensive_contribution", default=None)
    if precomputed is not None:
        return int(precomputed)

    cbit = (
        _stat(stats, "clearances_blocks_interceptions")
        + _stat(stats, "tackles")
    )
    if position == DEF:
        return int(cbit)
    return int(cbit + _stat(stats, "recoveries"))


def breakdown(stats: dict, position: int, rules: dict | None = None) -> list[dict]:
    """Where a player's points came from, line by line.

    Each row is {what, detail, points}. score_player is this summed, so a
    breakdown shown to a manager can never disagree with the score on their
    pitch.

    A line is kept when it scored something, and also when it explains a
    nought a manager would otherwise wonder about: one goal conceded (it
    takes two), two saves (it takes three), eight defensive actions (it takes
    ten). Minutes are always there, since a nought in that row answers most
    questions about a blank.
    """
    r = rules or RULES
    minutes = int(_stat(stats, "minutes"))
    rows = []

    def add(what, detail, points, keep=False):
        if points or keep:
            rows.append({"what": what, "detail": detail, "points": points})

    yellows = int(_stat(stats, "yellow_cards"))
    reds = int(_stat(stats, "red_cards"))
    owns = int(_stat(stats, "own_goals"))

    # No minutes, almost no points — but discipline still counts. A player
    # can be booked without the clock recording a minute: shown a card on the
    # bench, or in stoppage time after coming on. A full season of real data
    # turned up exactly that (a forward on 0 minutes, one yellow, -1), which
    # a single gameweek never would have.
    if minutes <= 0:
        add("Minutes", "0", 0, keep=True)
        add("Yellow card", "", yellows * r["yellow_card"])
        add("Red card", "", reds * r["red_card"])
        add("Own goals", str(owns), owns * r["own_goal"])
        return rows

    # Appearance
    if minutes >= r["minutes_long"]["threshold"]:
        add("Minutes", str(minutes), r["minutes_long"]["points"])
    else:
        add("Minutes", str(minutes), r["minutes_short"]["points"], keep=True)

    # Attacking returns
    goals = int(_stat(stats, "goals_scored"))
    assists = int(_stat(stats, "assists"))
    add("Goals", str(goals), goals * r["goal"][position])
    add("Assists", str(assists), assists * r["assist"][position])

    # Clean sheet — needs a full-ish game
    if (
        int(_stat(stats, "clean_sheets")) > 0
        and minutes >= r["clean_sheet_min_minutes"]
    ):
        add("Clean sheet", "", r["clean_sheet"][position])

    # Goals conceded, in whole blocks of N
    conceded = int(_stat(stats, "goals_conceded"))
    if conceded and r["conceded_points"][position]:
        add("Goals conceded", str(conceded),
            (conceded // r["conceded_per"]) * r["conceded_points"][position],
            keep=True)

    # Goalkeeping
    saves = int(_stat(stats, "saves"))
    if saves:
        add("Saves", str(saves), (saves // r["saves_per"]) * r["saves_points"],
            keep=True)
    pens_saved = int(_stat(stats, "penalties_saved"))
    add("Penalties saved", str(pens_saved), pens_saved * r["penalty_save"])

    # Misses and discipline
    missed = int(_stat(stats, "penalties_missed"))
    add("Penalties missed", str(missed), missed * r["penalty_miss"])
    add("Own goals", str(owns), owns * r["own_goal"])
    add("Yellow card", "", yellows * r["yellow_card"])
    add("Red card", "", reds * r["red_card"])

    # Defensive contribution
    threshold = r["defcon_threshold"].get(position)
    if threshold is not None:
        actions = defensive_actions(stats, position)
        hit = actions >= threshold
        if actions:
            add("Defensive actions", f"{actions} of {threshold}",
                r["defcon_points"] if hit else 0, keep=True)

    # Bonus is awarded by FPL from the BPS system; we take it as given rather
    # than trying to recompute the bonus algorithm.
    add("Bonus", "", int(_stat(stats, "bonus")))

    return rows


def contributions(entry: dict, position: int, rules: dict | None = None) -> dict:
    """What a player returned in a gameweek, counted rather than scored.

    Goals, assists and bonus are as recorded. A clean sheet is only counted
    where the rules pay for one — the minutes were there and the position
    earns it — so a forward whose side kept one doesn't collect it. A
    defensive contribution counts on the round it cleared the threshold, not
    per action, since that is the thing that happened.

    Per match in a double gameweek, for the same reason score_entry is.
    """
    r = rules or RULES
    per_fixture = entry.get("fixtures")
    games = (per_fixture if per_fixture and len(per_fixture) > 1
             else [entry.get("stats", {})])

    out = {"goals": 0, "assists": 0, "clean_sheets": 0, "defcon": 0, "bonus": 0}
    for stats in games:
        out["goals"] += int(_stat(stats, "goals_scored"))
        out["assists"] += int(_stat(stats, "assists"))
        out["bonus"] += int(_stat(stats, "bonus"))
        if (int(_stat(stats, "clean_sheets")) > 0
                and int(_stat(stats, "minutes")) >= r["clean_sheet_min_minutes"]
                and r["clean_sheet"][position]):
            out["clean_sheets"] += 1
        threshold = r["defcon_threshold"].get(position)
        if (threshold is not None
                and defensive_actions(stats, position) >= threshold):
            out["defcon"] += 1
    return out


def score_player(stats: dict, position: int, rules: dict | None = None) -> int:
    """Points for one player in one gameweek.

    `stats` is a raw FPL stat dict (from event/{gw}/live or element-summary).
    Returns an integer — fantasy points are always whole numbers.
    """
    return sum(row["points"] for row in breakdown(stats, position, rules))


def score_entry(entry: dict, position: int, rules: dict | None = None) -> int:
    """Points for one player, handling double gameweeks correctly.

    A gameweek's `stats` are aggregated across every match the player's club
    played, which quietly breaks anything counted per match. Appearance points
    are the obvious one: two 90-minute games is 4 points, not 2, but the
    aggregate says 180 minutes and scores it once. Clean sheets and goals
    conceded have the same problem.

    So when per-fixture stats are present, each match is scored on its own and
    the results added. Ordinary single gameweeks are unaffected.
    """
    per_fixture = entry.get("fixtures")
    if per_fixture and len(per_fixture) > 1:
        return sum(score_player(f, position, rules) for f in per_fixture)
    return score_player(entry.get("stats", {}), position, rules)


def entry_breakdown(entry: dict, position: int, rules: dict | None = None) -> list[dict]:
    """score_entry, itemised — and in a double gameweek, per match.

    The rows carry a `match` number when there was more than one, because
    "90 minutes" twice in a list is otherwise a puzzle rather than an answer.
    """
    per_fixture = entry.get("fixtures")
    if per_fixture and len(per_fixture) > 1:
        return [{**row, "match": i}
                for i, f in enumerate(per_fixture, 1)
                for row in breakdown(f, position, rules)]
    return breakdown(entry.get("stats", {}), position, rules)


def score_gameweek(elements: list[dict], positions: dict[int, int]) -> dict[int, int]:
    """Score every player in a gameweek.

    `elements` is the list from event/{gw}/live (each {id, stats}), optionally
    carrying per-fixture stats under "fixtures" for double gameweeks.
    `positions` maps player id -> element_type.
    """
    out = {}
    for el in elements:
        pid = el["id"]
        pos = positions.get(pid)
        if pos is None:
            continue
        out[pid] = score_entry(el, pos)
    return out


def provisional_bonus(bps_by_player: dict[int, int]) -> dict[int, int]:
    """Bonus points from BPS, the way FPL awards them.

    `bps_by_player` maps player id to their BPS **in one match**. Returns the
    same keys mapped to 3, 2, 1 or 0.

    Standard competition ranking: everyone level shares the higher place, and
    together they use up the places below it. So three players tied behind a
    leader take second, third and fourth between them — they get two points
    each and the single point is gone, rather than the next man down taking
    it. That is the case that catches you out, and it is why this is a rank
    over places rather than over distinct values.

    While a match is being played this is PROVISIONAL: BPS moves with every
    touch and FPL only settles bonus after the whistle. Nothing here feeds
    scoring — `score_player` takes bonus as awarded — this exists so the live
    page can show people what is happening.

    Verified against real data: reproduces FPL's own awards for all 310
    players who featured in gameweek 1.
    """
    out: dict[int, int] = {}
    place = 1
    ranked = sorted(bps_by_player.items(), key=lambda kv: -kv[1])
    for _, tied in itertools.groupby(ranked, key=lambda kv: kv[1]):
        tied = list(tied)
        award = {1: 3, 2: 2, 3: 1}.get(place, 0)
        for pid, _ in tied:
            out[pid] = award
        place += len(tied)
    return out
