#!/usr/bin/env python3
"""Pull gameweek data from the FPL API.

Uses `event/{gw}/live/`, which returns every player's stats for that gameweek
in a single request — far cheaper than ~600 element-summary calls, and it also
carries FPL's own `total_points` per player, which is what validate.py checks
our engine against.

Runs in GitHub Actions: the Claude sandbox has no egress to
fantasy.premierleague.com, but Actions does (same arrangement as players-feed).

Usage
-----
    python3 shadow/fetch_gw.py            # every round that has kicked off,
                                          # skipping any FPL has marked final
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
    # Not used by the scoring engine, but managers judge a waiver claim on
    # them, so the app needs them to sort by.
    "influence", "creativity", "threat", "ict_index",
    "expected_goal_involvements", "expected_goals_conceded",
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


def gameweek_states(bootstrap):
    """Every gameweek that has at least kicked off, with its state.

    `finished` means all matches are played; `data_checked` means FPL
    considers the stats final. A gameweek that has started but is neither is
    fetched anyway: the live endpoint's numbers are real, just not settled, and
    a table that shows nothing at all until Tuesday is worse than one that says
    plainly it is still being played.
    """
    out = {}
    for e in bootstrap.get("events", []):
        started = bool(e.get("finished") or e.get("is_current") or e.get("is_previous"))
        if not started:
            continue
        out[e["id"]] = {
            "id": e["id"],
            "name": e.get("name"),
            "finished": bool(e.get("finished")),
            "data_checked": bool(e.get("data_checked")),
            "deadline_time": e.get("deadline_time"),
        }
    return out


def fetch_gameweek(gw, meta):
    print(f"Fetching gameweek {gw}...")
    live = get_json(f"{BASE}/event/{gw}/live/")
    elements = []
    for el in live.get("elements", []):
        stats = el.get("stats", {}) or {}
        row = {
            "id": el["id"],
            "stats": {k: stats.get(k) for k in KEEP if k in stats},
        }
        # In a double gameweek the aggregate stats quietly break anything
        # counted per match — two 90-minute games look like one 180-minute
        # one and score appearance points once. FPL's own breakdown carries
        # the raw values per fixture, so keep those and let the engine score
        # each match separately. We take `value`, never `points`.
        per_fixture = []
        for block in el.get("explain") or []:
            lines = block[0] if isinstance(block, (list, tuple)) and block else []
            per_fixture.append({ln["stat"]: ln.get("value", 0)
                                for ln in lines if "stat" in ln})
        if len(per_fixture) > 1:
            row["fixtures"] = per_fixture
        elements.append(row)
    played = sum(1 for e in elements if (e["stats"].get("minutes") or 0) > 0)
    print(f"  {len(elements)} players, {played} with minutes")
    return {
        "gameweek": gw,
        "name": meta.get("name"),
        "deadline_time": meta.get("deadline_time"),
        "finished": meta.get("finished"),
        "data_checked": meta.get("data_checked"),
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "elements": elements,
    }


def fetch_pl_fixtures():
    """Every Premier League match, with gameweek and score.

    Needed for the manager-boost mechanic: the result decides whether a boost
    pays out in full, half or not at all, and the running league table decides
    how big it is. One call returns the whole season.
    """
    print("Fetching Premier League fixtures...")
    raw = get_json(f"{BASE}/fixtures/")
    out = []
    for f in raw:
        out.append({
            "id": f.get("id"),
            "event": f.get("event"),
            "finished": bool(f.get("finished")),
            "kickoff_time": f.get("kickoff_time"),
            "team_h": f.get("team_h"),
            "team_a": f.get("team_a"),
            "team_h_score": f.get("team_h_score"),
            "team_a_score": f.get("team_a_score"),
        })
    played = sum(1 for f in out if f["finished"])
    print(f"  {len(out)} fixtures, {played} played")
    # The per-fixture stats are not saved — they are what the live scores page
    # asks FPL for directly, since they change by the minute and committing
    # them would bloat this file for no gain. Logged so the shape they arrive
    # in is on the record rather than taken on trust.
    sample = next((f for f in raw if f.get("stats")), None)
    if sample:
        kinds = [s.get("identifier") for s in sample["stats"]]
        one = next((s for s in sample["stats"] if s.get("h") or s.get("a")), None)
        print(f"  live stats available per fixture: {kinds}")
        print(f"  example entry: {one}")
    (DATA / "pl_fixtures.json").write_text(json.dumps(out, separators=(",", ":")))
    return out


def should_fetch(gw, by_id, path, refetch=False):
    """Whether to pull a gameweek, and why — in words, for the log.

    Two rules, and the second is the one that matters to anyone reading a
    table:

    A round that has KICKED OFF is worth having even though it is unfinished.
    This used to take finished rounds only, to keep the repo from churning
    with half-played ones, and that was the wrong trade: it meant a gameweek
    in progress showed no result at all until FPL declared it over, so the
    table sat on last week's news for the whole weekend.

    A round FPL has marked `data_checked` is never pulled again. Bonus and
    assists move for a day or two after the whistle, so anything short of
    final is re-fetched until it settles — but once it has settled it is
    finished with, and no later run can move a result out from under anyone.
    """
    if gw not in by_id:
        return False, "hasn't kicked off yet — skipping"
    live = not by_id[gw]["finished"]
    if not path.exists():
        return True, ("in progress — fetching provisional data" if live
                      else "finished and not yet saved — fetching")
    if refetch:
        return True, "re-fetching, because --refetch was asked for"
    try:
        existing = json.loads(path.read_text())
    except Exception:
        return True, "saved copy is unreadable — fetching again"
    if existing.get("data_checked"):
        return False, "already saved and final — skipping"
    return True, ("in progress — re-fetching for the latest scores" if live
                  else "saved but not final — re-fetching")


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
    clubs = {
        str(t["id"]): {"name": t["name"], "short": t["short_name"]}
        for t in bs["teams"]
    }
    player_clubs = {str(el["id"]): el["team"] for el in bs["elements"]}

    # Who is injured, suspended or doubtful. Only the players with something
    # wrong are stored: everyone else is available, and saying so 600 times
    # over would be most of the file.
    availability = {}
    for el in bs["elements"]:
        status = el.get("status") or "a"
        chance = el.get("chance_of_playing_next_round")
        if status == "a" and chance in (None, 100):
            continue
        availability[str(el["id"])] = {
            "status": status,
            "chance": chance,
            "news": (el.get("news") or "").strip(),
        }

    (DATA / "players.json").write_text(
        json.dumps({"positions": positions, "names": names,
                    "clubs": clubs, "player_clubs": player_clubs,
                    "availability": availability},
                   separators=(",", ":"))
    )
    print(f"  saved positions for {len(positions)} players, "
          f"{len(availability)} of them carrying a doubt")

    fetch_pl_fixtures()

    by_id = gameweek_states(bs)
    finished = sorted(g for g, m in by_id.items() if m["finished"])
    print(f"  started: {sorted(by_id)} | finished: {finished}")

    wanted = [int(a) for a in args] if args else sorted(by_id)

    written = 0
    for gw in wanted:
        verdict, why = should_fetch(gw, by_id, DATA / f"gw{gw:02d}.json", refetch)
        print(f"Gameweek {gw}: {why}")
        if not verdict:
            continue
        out = DATA / f"gw{gw:02d}.json"
        data = fetch_gameweek(gw, by_id[gw])
        out.write_text(json.dumps(data, separators=(",", ":")))
        print(f"  wrote {out.name} ({out.stat().st_size / 1024:.0f} KB)")
        written += 1
        time.sleep(0.4)  # be polite

    print(f"Done. {written} gameweek file(s) written.")


if __name__ == "__main__":
    main()
