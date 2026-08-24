#!/usr/bin/env python3
"""Pull one or more finished gameweeks from the FPL API.

Uses `event/{gw}/live/`, which returns every player's stats for that gameweek
in a single request — far cheaper than ~600 element-summary calls, and it also
carries FPL's own `total_points` per player, which is what validate.py checks
our engine against.

Runs in GitHub Actions: the Claude sandbox has no egress to
fantasy.premierleague.com, but Actions does (same arrangement as players-feed).

Usage
-----
    python3 shadow/fetch_gw.py            # every finished gameweek not yet saved
    python3 shadow/fetch_gw.py 1 2 3      # specific gameweeks
    python3 shadow/fetch_gw.py --refetch  # re-pull even if already saved,
                                          # which is how stat corrections land
"""
import json
import sys
import time
import urllib.request
from pathlib import Path

BASE = "https://fantasy.premierleague.com/api"
HEADERS = {"User-Agent": "Mozilla/5.0 (omtffl-shadow-league)"}
DATA = Path(__file__).resolve().parent / "data"

# Stats we keep. Everything the scoring engine needs, plus total_points and
# bps so we can check ourselves against FPL.
KEEP = [
    "minutes", "goals_scored", "assists", "clean_sheets", "goals_conceded",
    "own_goals", "penalties_saved", "penalties_missed", "yellow_cards",
    "red_cards", "saves", "bonus", "bps", "total_points",
    "defensive_contribution", "clearances_blocks_interceptions", "recoveries",
    "tackles", "starts", "expected_goals", "expected_assists",
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


def finished_gameweeks(bootstrap):
    """Gameweeks with results in. `data_checked` means FPL considers the
    gameweek's stats final; `finished` alone can still be mid-revision."""
    out = []
    for e in bootstrap.get("events", []):
        if e.get("finished"):
            out.append({
                "id": e["id"],
                "name": e.get("name"),
                "finished": True,
                "data_checked": bool(e.get("data_checked")),
                "deadline_time": e.get("deadline_time"),
            })
    return out


def fetch_gameweek(gw, meta):
    print(f"Fetching gameweek {gw}...")
    live = get_json(f"{BASE}/event/{gw}/live/")
    elements = []
    for el in live.get("elements", []):
        stats = el.get("stats", {}) or {}
        elements.append({
            "id": el["id"],
            "stats": {k: stats.get(k) for k in KEEP if k in stats},
        })
    played = sum(1 for e in elements if (e["stats"].get("minutes") or 0) > 0)
    print(f"  {len(elements)} players, {played} with minutes")
    return {
        "gameweek": gw,
        "name": meta.get("name"),
        "deadline_time": meta.get("deadline_time"),
        "data_checked": meta.get("data_checked"),
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "elements": elements,
    }


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    refetch = "--refetch" in sys.argv

    print("Fetching bootstrap-static for gameweek state...")
    bs = get_json(f"{BASE}/bootstrap-static/")

    # Player positions change rarely but do change; save them alongside so
    # scoring a historical gameweek uses the positions we knew at the time.
    positions = {str(el["id"]): el["element_type"] for el in bs["elements"]}
    names = {str(el["id"]): el["web_name"] for el in bs["elements"]}
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / "players.json").write_text(
        json.dumps({"positions": positions, "names": names}, separators=(",", ":"))
    )
    print(f"  saved positions for {len(positions)} players")

    finished = finished_gameweeks(bs)
    by_id = {g["id"]: g for g in finished}
    print(f"  {len(finished)} finished gameweek(s): {sorted(by_id)}")

    if args:
        wanted = [int(a) for a in args]
    else:
        wanted = sorted(by_id)

    written = 0
    for gw in wanted:
        if gw not in by_id:
            print(f"Gameweek {gw} is not finished — skipping.")
            continue
        out = DATA / f"gw{gw:02d}.json"
        if out.exists() and not refetch:
            # Re-pull anything FPL hasn't finalised, since those stats move.
            try:
                existing = json.loads(out.read_text())
                if existing.get("data_checked"):
                    print(f"Gameweek {gw} already saved and final — skipping.")
                    continue
                print(f"Gameweek {gw} saved but not final — re-fetching.")
            except Exception:
                pass
        data = fetch_gameweek(gw, by_id[gw])
        out.write_text(json.dumps(data, separators=(",", ":")))
        print(f"  wrote {out.name} ({out.stat().st_size / 1024:.0f} KB)")
        written += 1
        time.sleep(0.4)  # be polite

    print(f"Done. {written} gameweek file(s) written.")


if __name__ == "__main__":
    main()
