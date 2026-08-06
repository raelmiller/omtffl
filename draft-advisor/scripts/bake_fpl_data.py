#!/usr/bin/env python3
"""Bake the FPL stats dataset used by the draft advisor factsheets.

Pulls from the official FPL API:
  - bootstrap-static: current player list, clubs, per-player season stats,
    set-piece order fields (penalties/corners/direct free kicks)
  - element-summary/{id}: past-season totals per player (history_past)
  - team/set-piece-notes: club-level set-piece notes

Output: draft-advisor/data/fpl_data.json — fully self-contained so the
advisor needs zero network on draft night.

Designed to run in GitHub Actions (the Claude Code container has no
egress to fantasy.premierleague.com).
"""
import json
import sys
import time
import urllib.request
from pathlib import Path

BASE = "https://fantasy.premierleague.com/api"
HEADERS = {"User-Agent": "Mozilla/5.0 (omtffl-draft-advisor data bake)"}
OUT = Path(__file__).resolve().parent.parent / "data" / "fpl_data.json"

# bootstrap element fields worth keeping for factsheets
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

HISTORY_FIELDS = [
    "season_name", "total_points", "minutes", "goals_scored", "assists",
    "clean_sheets", "goals_conceded", "yellow_cards", "red_cards", "saves",
    "bonus", "starts", "expected_goals", "expected_assists",
    "expected_goal_involvements", "expected_goals_conceded",
    "defensive_contribution", "start_cost", "end_cost",
]


def get_json(url, retries=3):
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except Exception as e:
            if attempt == retries - 1:
                raise
            print(f"  retry {attempt + 1} for {url}: {e}", file=sys.stderr)
            time.sleep(2 * (attempt + 1))


def main():
    print("Fetching bootstrap-static...")
    bs = get_json(f"{BASE}/bootstrap-static/")

    teams = {
        t["id"]: {"id": t["id"], "name": t["name"], "short_name": t["short_name"]}
        for t in bs["teams"]
    }
    current_events = [e for e in bs["events"] if e.get("is_current") or e.get("is_next")]
    season_label = "pre-season" if not any(e.get("is_current") for e in bs["events"]) else "in-season"

    players = []
    for el in bs["elements"]:
        p = {k: el.get(k) for k in ELEMENT_FIELDS}
        p["team_short"] = teams.get(el["team"], {}).get("short_name")
        players.append(p)

    print(f"  {len(players)} players, {len(teams)} clubs ({season_label})")

    print("Fetching set-piece notes...")
    try:
        spn = get_json(f"{BASE}/team/set-piece-notes/")
        set_piece_notes = {
            t["id"]: [n.get("info_message") for n in t.get("notes", [])]
            for t in spn.get("teams", [])
        }
    except Exception as e:
        print(f"  set-piece notes unavailable: {e}", file=sys.stderr)
        set_piece_notes = {}

    print(f"Fetching past-season history for {len(players)} players...")
    history = {}
    for i, p in enumerate(players):
        pid = p["id"]
        try:
            es = get_json(f"{BASE}/element-summary/{pid}/")
            history[pid] = [
                {k: h.get(k) for k in HISTORY_FIELDS} for h in es.get("history_past", [])
            ]
        except Exception as e:
            print(f"  history failed for {pid} ({p['web_name']}): {e}", file=sys.stderr)
            history[pid] = []
        if (i + 1) % 50 == 0:
            print(f"  {i + 1}/{len(players)}")
        time.sleep(0.05)  # be polite to the API

    # attach Championship output for promoted-club players (kept across bakes)
    champ_file = OUT.parent / "player_research.json"
    if champ_file.exists():
        champ = json.loads(champ_file.read_text()).get("players", [])
        by_key = {(c["web_name"], c.get("club")): c for c in champ}
        for p in players:
            c = by_key.get((p["web_name"], p.get("team_short")))
            if c:
                p["champ"] = {k: c.get(k) for k in ("goals", "assists", "starts", "note", "league", "proj")}

    out = {
        "baked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "season_state": season_label,
        "next_events": [
            {"name": e["name"], "deadline_time": e["deadline_time"]}
            for e in current_events[:2]
        ],
        "teams": list(teams.values()),
        "players": players,
        "history_past": history,
        "set_piece_notes": set_piece_notes,
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, separators=(",", ":")))
    size_mb = OUT.stat().st_size / 1e6
    print(f"Wrote {OUT} ({size_mb:.1f} MB)")


if __name__ == "__main__":
    main()
