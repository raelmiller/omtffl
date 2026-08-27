"""Real matches, as they are being played.

Everything else in this app reads data from disk, refreshed once a day. That
is the wrong shape for live scores, so this one module goes to FPL directly
when someone opens the page and holds the answer for a few seconds.

`/api/fixtures/?event=N` returns every match in a round with a `stats` block
per fixture — goals, assists, cards, saves, penalties, and BPS. The stats
arrive already attributed to a fixture and a side, which is why nothing here
has to work out who played where. That matters more than it sounds: deriving
it from a player's club goes wrong for anyone who moved in January and cannot
be done at all for a double gameweek. Clubs are looked up here only to badge
a name on screen.

Two things this is NOT:

- It is not a source of truth for the league. Scores here are FPL's raw match
  events; the engine still scores the league from the saved gameweek files.
- Bonus is PROVISIONAL until FPL settles it. BPS moves with every touch, and
  the ranking can change after the whistle. The board says which it is showing
  and the page has to keep saying so.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

from .engine import clubs, player_clubs, player_names, provisional_bonus

BASE = "https://fantasy.premierleague.com/api"
HEADERS = {"User-Agent": "Mozilla/5.0 (omtffl-season-app)"}

# Long enough that a room full of managers refreshing on a Saturday is one
# request rather than fifty, short enough that a goal shows up while people are
# still talking about it.
TTL_SECONDS = 45
# A live page must never hang on a slow upstream: better to say "couldn't
# reach FPL" quickly and let the reader retry.
TIMEOUT = 8

_cache: dict[int, tuple[float, list]] = {}
STATUS = {"last_fetch": None, "last_ok": None, "last_detail": None}

# What FPL calls each thing, and what it means to a person watching. Ordered
# the way they would be read out: what happened, then what it cost.
EVENTS = [
    ("goals_scored", "goal", "Goal"),
    ("assists", "assist", "Assist"),
    ("own_goals", "own", "Own goal"),
    ("penalties_saved", "pensave", "Penalty saved"),
    ("penalties_missed", "penmiss", "Penalty missed"),
    ("red_cards", "red", "Red card"),
]


def fetch(gameweek, force=False):
    """Every match in a round, straight from FPL. Cached for a few seconds.

    Returns (fixtures, error). On a failure the last good answer is served
    with the error alongside it, because a board that goes blank mid-match is
    worse than one that is thirty seconds behind and says so.
    """
    now = time.time()
    hit = _cache.get(gameweek)
    if hit and not force and now - hit[0] < TTL_SECONDS:
        return hit[1], None
    try:
        req = urllib.request.Request(
            f"{BASE}/fixtures/?event={int(gameweek)}", headers=HEADERS)
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            data = json.loads(r.read())
        _cache[gameweek] = (now, data)
        STATUS.update(last_fetch=now, last_ok=True, last_detail=None)
        return data, None
    except (urllib.error.URLError, OSError, TimeoutError, ValueError) as exc:
        detail = f"{type(exc).__name__}: {exc}"
        STATUS.update(last_fetch=now, last_ok=False, last_detail=detail)
        # Stale beats blank.
        return (hit[1] if hit else []), detail


def _by_identifier(fixture):
    return {s.get("identifier"): s for s in (fixture.get("stats") or [])}


def _side_rows(block, side):
    """FPL gives each stat as {'h': [...], 'a': [...]} of {value, element}."""
    return [(row["element"], row.get("value") or 0)
            for row in (block.get(side) or []) if "element" in row]


def board(gameweek, squads=None, names=None, me=None, force=False):
    """Every match in the round, shaped for the page.

    `squads` is {manager: [player, ...]} so each name can carry who owns it —
    the thing the official game doesn't tell you. `me` is the reader, whose
    own players are marked out from everyone else's.
    """
    raw, error = fetch(gameweek, force=force)
    owner = {}
    for key, squad in (squads or {}).items():
        for p in squad:
            owner.setdefault(p["id"], []).append(key)
    who = player_names()
    at = player_clubs()
    sides = clubs()
    short = {cid: c.get("short") for cid, c in sides.items()}
    full = {cid: c.get("name") for cid, c in sides.items()}

    matches = []
    for f in sorted(raw, key=lambda x: (x.get("kickoff_time") or "", x.get("id"))):
        stats = _by_identifier(f)

        def tag(pid):
            owners = owner.get(pid, [])
            return {
                "id": pid,
                "name": who.get(pid, f"#{pid}"),
                "club": short.get(at.get(pid)),
                "owners": owners,
                "mine": bool(me and me in owners),
            }

        events = []
        for identifier, kind, label in EVENTS:
            block = stats.get(identifier) or {}
            for side in ("h", "a"):
                for pid, value in _side_rows(block, side):
                    for _ in range(max(1, int(value))):
                        events.append({**tag(pid), "kind": kind,
                                       "label": label, "side": side})

        # Bonus: FPL's own once it has settled, ours from live BPS until then.
        bps_block = stats.get("bps") or {}
        bps = {pid: v for side in ("h", "a")
               for pid, v in _side_rows(bps_block, side)}
        bps_side = {pid: side for side in ("h", "a")
                    for pid, _ in _side_rows(bps_block, side)}
        awarded_block = stats.get("bonus") or {}
        awarded = {pid: v for side in ("h", "a")
                   for pid, v in _side_rows(awarded_block, side)}
        settled = bool(awarded)
        points = awarded if settled else provisional_bonus(bps)
        bonus = sorted(
            ({**tag(pid), "bps": bps.get(pid, 0), "points": pts,
              "side": bps_side.get(pid, "h")}
             for pid, pts in points.items() if pts > 0),
            key=lambda r: (-r["points"], -r["bps"], r["name"]))

        started = bool(f.get("started"))
        matches.append({
            "id": f.get("id"),
            "kickoff": f.get("kickoff_time"),
            "started": started,
            "finished": bool(f.get("finished")),
            "minutes": f.get("minutes") or 0,
            "home": {"id": f.get("team_h"), "short": short.get(f.get("team_h")),
                     "name": full.get(f.get("team_h")),
                     "score": f.get("team_h_score")},
            "away": {"id": f.get("team_a"), "short": short.get(f.get("team_a")),
                     "name": full.get(f.get("team_a")),
                     "score": f.get("team_a_score")},
            "events": events,
            "bonus": bonus,
            "bonus_settled": settled,
            # Which of the reader's players are involved at all, so a match
            # they have a stake in can be found without reading every line.
            "mine": sorted({e["name"] for e in events if e["mine"]}
                           | {b["name"] for b in bonus if b["mine"]}),
        })

    playing = [m for m in matches if m["started"] and not m["finished"]]
    return {
        "gameweek": gameweek,
        "matches": matches,
        "kicked_off": any(m["started"] for m in matches),
        "in_play": bool(playing),
        "error": error,
        "fetched_at": STATUS["last_fetch"],
    }
