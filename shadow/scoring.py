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


def score_player(stats: dict, position: int, rules: dict | None = None) -> int:
    """Points for one player in one gameweek.

    `stats` is a raw FPL stat dict (from event/{gw}/live or element-summary).
    Returns an integer — fantasy points are always whole numbers.
    """
    r = rules or RULES
    minutes = int(_stat(stats, "minutes"))

    # No minutes, no points. FPL awards nothing at all to an unused player,
    # including no bonus and no card deductions (they can't be booked).
    if minutes <= 0:
        return 0

    pts = 0

    # Appearance
    if minutes >= r["minutes_long"]["threshold"]:
        pts += r["minutes_long"]["points"]
    elif minutes >= r["minutes_short"]["threshold"]:
        pts += r["minutes_short"]["points"]

    # Attacking returns
    pts += int(_stat(stats, "goals_scored")) * r["goal"][position]
    pts += int(_stat(stats, "assists")) * r["assist"][position]

    # Clean sheet — needs a full-ish game
    if (
        int(_stat(stats, "clean_sheets")) > 0
        and minutes >= r["clean_sheet_min_minutes"]
    ):
        pts += r["clean_sheet"][position]

    # Goals conceded, in whole blocks of N
    conceded = int(_stat(stats, "goals_conceded"))
    if conceded and r["conceded_points"][position]:
        pts += (conceded // r["conceded_per"]) * r["conceded_points"][position]

    # Goalkeeping
    saves = int(_stat(stats, "saves"))
    if saves:
        pts += (saves // r["saves_per"]) * r["saves_points"]
    pts += int(_stat(stats, "penalties_saved")) * r["penalty_save"]

    # Misses and discipline
    pts += int(_stat(stats, "penalties_missed")) * r["penalty_miss"]
    pts += int(_stat(stats, "own_goals")) * r["own_goal"]
    pts += int(_stat(stats, "yellow_cards")) * r["yellow_card"]
    pts += int(_stat(stats, "red_cards")) * r["red_card"]

    # Defensive contribution
    threshold = r["defcon_threshold"].get(position)
    if threshold is not None:
        if defensive_actions(stats, position) >= threshold:
            pts += r["defcon_points"]

    # Bonus is awarded by FPL from the BPS system; we take it as given rather
    # than trying to recompute the bonus algorithm.
    pts += int(_stat(stats, "bonus"))

    return pts


def score_gameweek(elements: list[dict], positions: dict[int, int]) -> dict[int, int]:
    """Score every player in a gameweek.

    `elements` is the list from event/{gw}/live (each {id, stats}).
    `positions` maps player id -> element_type.
    """
    out = {}
    for el in elements:
        pid = el["id"]
        pos = positions.get(pid)
        if pos is None:
            continue
        out[pid] = score_player(el.get("stats", {}), pos)
    return out
