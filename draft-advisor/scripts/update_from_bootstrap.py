#!/usr/bin/env python3
"""Refresh the player dataset from an FPL bootstrap-static JSON file.

Use this to update to the latest players/prices without waiting for the
daily GitHub bake (or when you have a specific snapshot). Feed it any
FPL `bootstrap-static` export:

    python scripts/update_from_bootstrap.py data/raw/bootstrap_latest.json

It rebuilds data/fpl_data.json, preserving each player's MULTI-SEASON
history from the previous fpl_data.json (matched by name), since a
bootstrap only carries last season. Players with no prior match get a
single synthesised season from the bootstrap totals.

After this, run the rest of the pipeline (build_player_breakdown,
build_manager_dossiers, fit_price_model, build_standalone) — or just use
scripts/refresh_all.sh which chains them.
"""
import json
import re
import sys
import time
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

ELEMENT_FIELDS = [
    "id", "web_name", "first_name", "second_name", "element_type", "team",
    "status", "news", "now_cost", "total_points", "minutes", "starts",
    "goals_scored", "assists", "clean_sheets", "goals_conceded",
    "yellow_cards", "red_cards", "saves", "bonus", "bps", "form",
    "points_per_game", "selected_by_percent",
    "expected_goals", "expected_assists", "expected_goal_involvements",
    "expected_goals_conceded",
    "defensive_contribution", "clearances_blocks_interceptions_tackles",
    "recoveries", "tackles",
    "penalties_order", "corners_and_indirect_freekicks_order",
    "direct_freekicks_order",
    "penalties_text", "corners_and_indirect_freekicks_text",
    "direct_freekicks_text",
]
# fields to lift from a bootstrap element into a synthesised history season
HIST_FROM_ELEMENT = [
    "total_points", "minutes", "goals_scored", "assists", "clean_sheets",
    "goals_conceded", "yellow_cards", "red_cards", "saves", "bonus", "starts",
    "expected_goals", "expected_assists", "expected_goal_involvements",
    "expected_goals_conceded", "defensive_contribution",
]


def norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z ]", "", s.lower()).strip()


def completed_season(bs):
    """The season the element totals represent = the one before is_next."""
    nxt = [e for e in bs.get("events", []) if e.get("is_next")]
    yr = None
    if nxt and nxt[0].get("deadline_time"):
        yr = int(nxt[0]["deadline_time"][:4])
    if not yr:
        yr = time.gmtime().tm_year
    return f"{yr - 1}/{str(yr)[2:]}"


def main():
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else DATA / "raw" / "bootstrap_latest.json"
    bs = json.loads(src.read_text())
    season = completed_season(bs)

    teams = {
        t["id"]: {"id": t["id"], "name": t["name"], "short_name": t["short_name"]}
        for t in bs["teams"]
    }

    # carry multi-season history from the previous dataset, keyed by name
    old_hist_by_name = {}
    prev = DATA / "fpl_data.json"
    carried = 0
    if prev.exists():
        old = json.loads(prev.read_text())
        old_hp = old.get("history_past", {})
        for p in old.get("players", []):
            key = norm(p.get("first_name", "") + " " + p.get("second_name", "")) or norm(p.get("web_name", ""))
            h = old_hp.get(str(p["id"])) or old_hp.get(p["id"])
            if key and h:
                old_hist_by_name[key] = h

    players, history = [], {}
    synthesised = 0
    for el in bs["elements"]:
        p = {k: el.get(k) for k in ELEMENT_FIELDS}
        p["team_short"] = teams.get(el["team"], {}).get("short_name")
        players.append(p)

        key = norm(el.get("first_name", "") + " " + el.get("second_name", "")) or norm(el.get("web_name", ""))
        if key in old_hist_by_name:
            history[el["id"]] = old_hist_by_name[key]
            carried += 1
        else:
            # no prior history — synthesise one season from the bootstrap totals
            if (el.get("minutes") or 0) > 0 or (el.get("total_points") or 0) > 0:
                season_row = {k: el.get(k) for k in HIST_FROM_ELEMENT}
                season_row["season_name"] = season
                history[el["id"]] = [season_row]
                synthesised += 1
            else:
                history[el["id"]] = []

    out = {
        "baked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": f"bootstrap file: {src.name}",
        "season_state": "pre-season/updated",
        "completed_season": season,
        "teams": list(teams.values()),
        "players": players,
        "history_past": history,
        "set_piece_notes": {},   # not in a bootstrap; per-player order fields cover set-pieces
    }
    (DATA / "fpl_data.json").write_text(json.dumps(out, separators=(",", ":")))
    print(f"fpl_data.json refreshed from {src.name}")
    print(f"  {len(players)} players, {len(teams)} teams, completed season {season}")
    print(f"  history: {carried} carried multi-season, {synthesised} single-season from bootstrap")


if __name__ == "__main__":
    main()
