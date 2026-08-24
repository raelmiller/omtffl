"""Bridge to the rules engine in `shadow/`.

The engine is the source of truth for every number this app shows. Nothing
here computes points, decides a formation or resolves a fixture — it loads
data, calls the engine, and hands back plain dictionaries for the templates.

That boundary is the whole architecture. If a scoring rule needs to change it
changes in `shadow/`, and this file doesn't move.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

SHADOW = Path(os.environ.get("SHADOW_DIR")
              or Path(__file__).resolve().parents[2] / "shadow")
DATA = SHADOW / "data"

# The shadow modules import each other by bare module name, so the directory
# itself has to be importable rather than the package above it.
if str(SHADOW) not in sys.path:
    sys.path.insert(0, str(SHADOW))

from h2h import WIN, DRAW, gameweek_scores          # noqa: E402
from score_league import best_xi, load_positions    # noqa: E402
from lineups import (                               # noqa: E402
    apply_autosubs, effective_lineup, form_before, legal_formation,
    load_lineups, minutes_from_gameweek, suggest_lineup,
    validate as validate_lineup,
)
from scoring import score_entry                     # noqa: E402


def _read(name):
    path = DATA / name
    if not path.exists():
        return None
    return json.loads(path.read_text())


def gameweek_files():
    return sorted(DATA.glob("gw*.json"))


def data_version():
    """A cheap fingerprint of the data on disk, for cache invalidation.

    Scoring a season is fast, but it isn't free, and the data only changes
    when the fetcher writes. Keying the cache on file mtimes means a refresh
    is picked up immediately without a restart.
    """
    files = gameweek_files() + [DATA / "squads.json", DATA / "fixtures.json"]
    return tuple((f.name, f.stat().st_mtime_ns) for f in files if f.exists())


def state(gw: dict) -> str:
    """How settled a gameweek's numbers are — never dress one up as final."""
    if gw.get("data_checked"):
        return "final"
    if gw.get("finished"):
        return "provisional"
    return "in progress"


def merge_lineups(stored):
    """Real submissions laid over the committed placeholder file.

    Replacing the file outright would let a team picked for one gameweek
    silently rewrite an earlier, already-played round: the placeholder would
    vanish from gameweek 1 and its scores would change under everyone. Merging
    per manager per gameweek means a submission only ever affects its own
    round, and the placeholder retires quietly as real teams replace it.
    """
    merged = {}
    for gw, teams in (load_lineups() or {}).items():
        merged[int(gw)] = dict(teams)
    for gw, teams in (stored or {}).items():
        merged.setdefault(int(gw), {}).update(teams)
    return merged


@lru_cache(maxsize=8)
def _season(version, lineup_key, lineups_json, real_keys):
    """Score the whole season. Cached on the data fingerprint, not on time.

    `lineups_json` is passed in rather than read here so the app can serve
    submissions from the database while the shadow scripts keep using the
    file. Both arrive in the same shape, so the engine can't tell them apart.
    """
    lineups_in = json.loads(lineups_json) if lineups_json else None
    real = set(real_keys or ())
    squads = _read("squads.json")
    fixtures = _read("fixtures.json")
    if not squads or not fixtures:
        return {"ready": False, "reason": "no squads or fixture list yet"}

    positions = load_positions()
    from_file = lineups_in is None
    lineups = ({int(k): v for k, v in lineups_in.items()} if not from_file
               else load_lineups() or {})
    # The committed lineups file is a worked example — teams filled in from
    # draft price so the engine had something to score. Nobody picked those
    # elevens, and the page must not imply anyone did.
    placeholder_file = _lineups_are_seeded()
    names = {t["key"]: t.get("team", t["key"]) for t in squads["teams"]}

    by_gw = {}
    for fx in fixtures["fixtures"]:
        by_gw.setdefault(fx["gameweek"], []).append(fx)

    table = {t["key"]: dict(key=t["key"], team=names[t["key"]], P=0, W=0, D=0,
                            L=0, PF=0, PA=0, Pts=0)
             for t in squads["teams"]}
    rounds = []

    for path in gameweek_files():
        gw = json.loads(path.read_text())
        n = gw["gameweek"]
        _, hindsight = gameweek_scores(path, squads, positions)

        # Where a manager submitted an XI, that's their score. Where nobody
        # has, the best available XI stands in — and the page says which.
        pts = {}
        for el in gw["elements"]:
            pos = positions.get(el["id"])
            if pos is not None:
                pts[el["id"]] = score_entry(el, pos)
        minutes = minutes_from_gameweek(gw)

        scores, sources = {}, {}
        for team in squads["teams"]:
            key = team["key"]
            picked, bench, how = effective_lineup(key, n, lineups, team["squad"])
            if picked:
                if (n, key) not in real:
                    # Resolved from the placeholder file, not from anything a
                    # manager actually chose.
                    how = "placeholder"
                final_xi, _ = apply_autosubs(picked, bench, minutes)
                scores[key] = sum(pts.get(p["id"], 0) for p in final_xi)
                sources[key] = how
            else:
                scores[key] = hindsight.get(key, 0)
                sources[key] = "best available"

        matches = []
        for fx in by_gw.get(n, []):
            h, a = fx["home"], fx["away"]
            hs, as_ = scores.get(h, 0), scores.get(a, 0)
            for t, sf, sa in ((h, hs, as_), (a, as_, hs)):
                row = table[t]
                row["P"] += 1
                row["PF"] += sf
                row["PA"] += sa
                if sf > sa:
                    row["W"] += 1
                    row["Pts"] += WIN
                elif sf == sa:
                    row["D"] += 1
                    row["Pts"] += DRAW
                else:
                    row["L"] += 1
            matches.append({
                "home": names[h], "away": names[a],
                "home_key": h, "away_key": a,
                "home_score": hs, "away_score": as_,
                "home_source": sources.get(h), "away_source": sources.get(a),
            })

        rounds.append({
            "gameweek": n,
            "name": gw.get("name") or f"Gameweek {n}",
            "state": state(gw),
            "deadline": gw.get("deadline_time"),
            "matches": matches,
            "high": max(scores.values()) if scores else 0,
            "low": min(scores.values()) if scores else 0,
            "average": round(sum(scores.values()) / len(scores)) if scores else 0,
        })

    ranked = sorted(table.values(),
                    key=lambda r: (-r["Pts"], -(r["PF"] - r["PA"]), -r["PF"]))
    for i, row in enumerate(ranked, 1):
        row["rank"] = i
        row["diff"] = row["PF"] - row["PA"]

    submitted = sum(1 for r in rounds for m in r["matches"]
                    for k in (m["home_key"], m["away_key"])
                    if (r["gameweek"], k) in real)
    # The placeholder label belongs on the page for as long as the rounds on
    # show are still standing on it — a team saved for a future gameweek
    # doesn't change what gameweek 1 was scored from.
    seeded = placeholder_file and submitted == 0
    total_slots = sum(len(r["matches"]) * 2 for r in rounds)

    return {
        "ready": True,
        "table": ranked,
        "rounds": list(reversed(rounds)),
        "played": len(rounds),
        "scheduled": len(by_gw),
        "submitted_share": (submitted, total_slots),
        "seeded_lineups": seeded,
    }


def _lineups_are_seeded():
    """Whether the lineups on disk are the worked example rather than real.

    The file says so itself. Trusting its own note beats guessing from the
    shape of the data, and it stops being true the moment real submissions
    replace it.
    """
    raw = _read("lineups.json")
    if not raw:
        return False
    return "worked example" in (raw.get("note") or "").lower()


def season(stored=None):
    """Score the season, with any real submissions laid over the placeholder.

    `stored` is what managers have actually saved. The cache key includes it,
    so a team saved a moment ago shows on the next page load without a restart
    or a manual flush.
    """
    if not stored:
        return _season(data_version(), 0, None, ())
    merged = merge_lineups(stored)
    real = tuple(sorted((int(gw), key)
                        for gw, teams in stored.items() for key in teams))
    payload = json.dumps({str(k): v for k, v in sorted(merged.items())},
                         sort_keys=True)
    return _season(data_version(), len(payload), payload, real)


def freshness():
    """What data is on disk and how settled it is — the health page's job."""
    files = gameweek_files()
    latest = None
    if files:
        gw = json.loads(files[-1].read_text())
        latest = {
            "gameweek": gw["gameweek"],
            "state": state(gw),
            "fetched_at": gw.get("fetched_at"),
            "players": len(gw.get("elements", [])),
        }
    return {
        "gameweeks_on_disk": len(files),
        "latest": latest,
        "squads": bool((DATA / "squads.json").exists()),
        "fixtures": bool((DATA / "fixtures.json").exists()),
        "shadow_dir": str(SHADOW),
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


# FPL locks a gameweek 90 minutes before its first kick-off. Deriving the
# deadline that way lets the app know about rounds that haven't happened yet,
# where no gameweek file exists — which is exactly the round a manager wants
# to pick a team for.
DEADLINE_BEFORE_KICKOFF = timedelta(minutes=90)


def calendar():
    """Every gameweek of the season, newest first.

    Built from the Premier League fixture list, which covers all 38 rounds
    from day one, and overlaid with real data wherever a round has been
    downloaded. A season app that only knew about finished rounds could never
    show you the team sheet you actually need.
    """
    rounds = {}
    for fx in _read("pl_fixtures.json") or []:
        event, kickoff = fx.get("event"), fx.get("kickoff_time")
        if not event or not kickoff:
            continue
        first = rounds.setdefault(event, {"gameweek": event, "kickoff": kickoff})
        if kickoff < first["kickoff"]:
            first["kickoff"] = kickoff

    for entry in rounds.values():
        entry["name"] = f"Gameweek {entry['gameweek']}"
        entry["deadline"] = (_parse(entry["kickoff"]) - DEADLINE_BEFORE_KICKOFF
                             ).isoformat().replace("+00:00", "Z")
        entry["state"] = "upcoming"

    # A downloaded round knows better than the fixture list: it carries FPL's
    # own deadline and how settled the stats are.
    for path in gameweek_files():
        gw = json.loads(path.read_text())
        entry = rounds.setdefault(gw["gameweek"], {"gameweek": gw["gameweek"]})
        entry.update(name=gw.get("name") or f"Gameweek {gw['gameweek']}",
                     deadline=gw.get("deadline_time") or entry.get("deadline"),
                     state=state(gw), has_data=True)

    return sorted(rounds.values(), key=lambda g: -g["gameweek"])


def gameweeks():
    """Only the rounds we hold data for, newest first."""
    return [g for g in calendar() if g.get("has_data")]


def current_gameweek():
    """The round managers should be picking a team for.

    The next one still open, or failing that the most recent — a manager
    arriving mid-week wants this week's team sheet, not last week's result.
    """
    rounds = calendar()
    if not rounds:
        return None
    now = datetime.now(timezone.utc)
    upcoming = [g for g in rounds
                if g.get("deadline") and _parse(g["deadline"]) > now]
    return min(upcoming, key=lambda g: g["gameweek"]) if upcoming else rounds[0]


def _parse(stamp):
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def deadline_state(gw):
    """Whether a gameweek is still open, and how long is left.

    The lock is the deadline itself — the same instant FPL uses — so there is
    never a question of whose clock is right.
    """
    if not gw or not gw.get("deadline"):
        return {"open": False, "reason": "no deadline on record", "seconds": 0}
    now = datetime.now(timezone.utc)
    closes = _parse(gw["deadline"])
    left = int((closes - now).total_seconds())
    return {
        "open": left > 0,
        "deadline": gw["deadline"],
        "seconds": max(0, left),
        "reason": None if left > 0 else "the deadline has passed",
    }


def squad_for(key):
    """A manager's fifteen. Phase two has no trades, so this is the draft."""
    squads = _read("squads.json")
    if not squads:
        return []
    for team in squads["teams"]:
        if team["key"] == key:
            return list(team["squad"])
    return []


def suggest_for(key, gameweek, squad=None):
    """A plausible starting eleven for a manager who has never picked.

    Ranked on points from gameweeks already played, or on draft price before
    any football has happened. Never on hindsight — this only ever knows what
    a manager could have known before the deadline.
    """
    squad = squad if squad is not None else squad_for(key)
    if not squad:
        return [], []
    form = form_before(gameweek, load_positions())
    if not form:
        form = {p["id"]: p.get("price", 0) for p in squad}
    return suggest_lineup(squad, form)
