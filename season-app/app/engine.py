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
from mechanics import (                             # noqa: E402
    BOOST_RESULT, BOOST_USES_PER_SEASON, apply_transactions, boost_pct,
    boost_value, league_table, process_waivers, setting, snake_order,
    validate_trade,
)
from scoring import entry_breakdown, score_entry    # noqa: E402


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
def _season(version, lineup_key, lineups_json, real_keys,
            transactions_json, managers_json):
    """Score the whole season. Cached on the data fingerprint, not on time.

    `lineups_json` is passed in rather than read here so the app can serve
    submissions from the database while the shadow scripts keep using the
    file. Both arrive in the same shape, so the engine can't tell them apart.
    """
    lineups_in = json.loads(lineups_json) if lineups_json else None
    real = set(real_keys or ())
    transactions = json.loads(transactions_json) if transactions_json else []
    drafted = json.loads(managers_json) if managers_json else {}
    pl_fixtures = _read("pl_fixtures.json") or []
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

    managers_arg = {k: {"name": v.get("name"), "club": v.get("club"),
                        "sacked_from": v.get("sacked_from")}
                    for k, v in drafted.items()}
    # Standings drive waiver priority, and the standings going into a gameweek
    # are the table after the one before — so this is resolved as the season is
    # scored, not once up front. Everything is recomputed each round because a
    # waiver in round three changes who owns whom in round four.
    standings = {}
    boosts_allowed = set()
    squads_at = {}

    for path in gameweek_files():
        gw = json.loads(path.read_text())
        n = gw["gameweek"]

        if transactions:
            ranked_now = sorted(
                table.values(),
                key=lambda r: (-r["Pts"], -(r["PF"] - r["PA"]), -r["PF"]))
            standings[n] = [r["key"] for r in ranked_now]
            moved, _, boost_log, _, _, _ = apply_transactions(
                squads, transactions, n, managers=managers_arg,
                standings=standings)
            boosts_allowed = {(b["gameweek"], b["team"]) for b in boost_log}
            squads_at = moved

        _, hindsight = gameweek_scores(path, squads, positions)

        # Where a manager submitted an XI, that's their score. Where nobody
        # has, the best available XI stands in — and the page says which.
        pts = {}
        for el in gw["elements"]:
            pos = positions.get(el["id"])
            if pos is not None:
                pts[el["id"]] = score_entry(el, pos)
        minutes = minutes_from_gameweek(gw)

        scores, sources, boosts = {}, {}, {}
        for team in squads["teams"]:
            key = team["key"]
            roster = squads_at.get(key, team["squad"])
            picked, bench, how = effective_lineup(key, n, lineups, roster)
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

            # A boost multiplies the XI that actually played, so it is priced
            # after the eleven is settled and the substitutions resolved.
            if (n, key) in boosts_allowed:
                club = (drafted.get(key) or {}).get("club")
                if club is not None:
                    gained, detail = boost_value(scores[key], club, n, pl_fixtures)
                    scores[key] += gained
                    boosts[key] = {"points": gained, "club": clubs().get(club, {}).get("name"),
                                   **detail}

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
                "home_boost": boosts.get(h), "away_boost": boosts.get(a),
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
            "boosts": boosts,
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


def season(stored=None, transactions=None, drafted=None):
    """Score the season: submitted elevens, plus whatever was declared.

    `stored` is what managers saved as lineups, `transactions` everything else
    they declared, `drafted` which club each team's manager runs. All three
    are in the cache key, so a boost played a moment ago is reflected on the
    next page load without a restart.
    """
    tx = json.dumps(transactions or [], sort_keys=True) if transactions else None
    mg = json.dumps(drafted or {}, sort_keys=True) if drafted else None
    if not stored:
        return _season(data_version(), 0, None, (), tx, mg)
    merged = merge_lineups(stored)
    real = tuple(sorted((int(gw), key)
                        for gw, teams in stored.items() for key in teams))
    payload = json.dumps({str(k): v for k, v in sorted(merged.items())},
                         sort_keys=True)
    return _season(data_version(), len(payload), payload, real, tx, mg)


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


def fixture_list(scored=None):
    """Every round of the season, played or not, oldest first.

    The table can only speak for rounds that have been scored. The other
    twenty-odd are already fixed, and who you play in gameweek 30 is a reason
    to make a trade in gameweek 12 — so the whole list goes on the page, with
    scores filled in as far as they go.
    """
    fixtures = _read("fixtures.json")
    squads = _read("squads.json")
    if not fixtures or not squads:
        return []

    names = {t["key"]: t.get("team", t["key"]) for t in squads["teams"]}
    played = {r["gameweek"]: r for r in (scored or [])}
    meta = {g["gameweek"]: g for g in calendar()}

    by_gw = {}
    for fx in fixtures["fixtures"]:
        by_gw.setdefault(fx["gameweek"], []).append(fx)

    rounds = []
    for n in sorted(by_gw):
        if n in played:
            rounds.append({**played[n], "played": True})
            continue
        info = meta.get(n, {})
        rounds.append({
            "gameweek": n,
            "name": info.get("name") or f"Gameweek {n}",
            "state": info.get("state") or "upcoming",
            "deadline": info.get("deadline"),
            "played": False,
            "matches": [{"home": names.get(fx["home"], fx["home"]),
                         "away": names.get(fx["away"], fx["away"]),
                         "home_key": fx["home"], "away_key": fx["away"]}
                        for fx in by_gw[n]],
        })
    return rounds


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


def current_club(player_id):
    """A player's club now, not on draft day.

    Squad entries carry the club a player was at when they were drafted, and
    that goes stale the moment anyone transfers — a defender listed at their
    old club is genuinely confusing when you're picking a team. The fetched
    player data is refreshed every gameweek, so it wins.
    """
    meta = _read("players.json") or {}
    club_id = (meta.get("player_clubs") or {}).get(str(player_id))
    return clubs().get(club_id, {}).get("short")


def refresh_clubs(squad):
    """Squad entries with their club brought up to date."""
    out = []
    for player in squad:
        now = current_club(player["id"])
        out.append({**player, "club": now} if now else dict(player))
    return out


def squad_for(key):
    """A manager's fifteen, as drafted."""
    squads = _read("squads.json")
    if not squads:
        return []
    for team in squads["teams"]:
        if team["key"] == key:
            return refresh_clubs(team["squad"])
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


def clubs():
    """Premier League clubs, id -> name, from the fetched player data."""
    meta = _read("players.json") or {}
    return {int(k): v for k, v in (meta.get("clubs") or {}).items()}


def boost_status(key, gameweek, drafted, used, declared=False):
    """What a boost is worth to this manager this week, and whether it's on.

    Everything a manager needs to decide with: which club, where they sit,
    what that band pays, how many uses are left, and why it might be refused.
    """
    entry = (drafted or {}).get(key)
    if not entry:
        return {"available": False,
                "why": "no manager drafted — the league hasn't assigned one yet"}

    club_id = entry["club"]
    club = clubs().get(club_id, {})
    sacked = entry.get("sacked_from")
    if sacked is not None and gameweek >= sacked:
        return {"available": False, "club": club.get("name", "?"),
                "why": (f"your manager left {club.get('name', 'the club')} in "
                        f"gameweek {sacked} — the boost went with them")}

    fixtures = _read("pl_fixtures.json") or []
    table = league_table(fixtures, gameweek)
    position = table.get(club_id)
    settled = position is not None
    if not settled:
        from mechanics import NEUTRAL_POSITION
        position = NEUTRAL_POSITION

    left = BOOST_USES_PER_SEASON - used
    return {
        "available": left > 0,
        "declared": declared,
        "club": club.get("name", "?"),
        "short": club.get("short", ""),
        "position": position,
        "provisional_position": not settled,
        "pct": boost_pct(position),
        "used": used,
        "left": left,
        "total": BOOST_USES_PER_SEASON,
        "why": None if left > 0 else
               f"all {BOOST_USES_PER_SEASON} boosts used this season",
    }


# ── Trades ─────────────────────────────────────────────────────────────────
def trade_outcome(record, config=None):
    """What a trade's stored status actually means right now.

    A published points trade is neither done nor dead while its window is
    open: the league can still object. Rather than a scheduled job flipping
    rows at the deadline, the outcome is derived whenever anyone looks, so a
    trade is never in one state in the database and another on the page.
    """
    status = record["status"]
    if status != "published":
        return {"state": status, "open": False,
                "vetoes": len(record["vetoes"]), "needed": None}

    threshold = setting(config, "veto_threshold")
    target = next((g for g in calendar()
                   if g["gameweek"] == record["gameweek"]), None)
    window = deadline_state(target) if target else {"open": False}
    count = len(record["vetoes"])

    if count >= threshold:
        return {"state": "vetoed", "open": False,
                "vetoes": count, "needed": threshold}
    if window["open"]:
        return {"state": "published", "open": True, "vetoes": count,
                "needed": threshold, "deadline": window.get("deadline")}
    # The window closed without enough objections, so it stands.
    return {"state": "accepted", "open": False, "vetoes": count,
            "needed": threshold}


def effective_trades(records, config=None):
    """Only the trades that actually happened, as engine transactions."""
    out = []
    for record in records:
        outcome = trade_outcome(record, config)
        if outcome["state"] != "accepted":
            continue
        out.append({
            "type": "trade", "gameweek": record["gameweek"],
            "from": record["proposer"], "to": record["receiver"],
            "players_out": record["players_out"],
            "players_in": record["players_in"],
            "points": record["points"],
        })
    return out


def check_trade(record, squads_at_gw, accumulated=None, received=0, config=None):
    """Why a proposed trade couldn't happen, or None.

    Calls the same validator the scoring uses, so a trade the app accepts can
    never be one the engine later refuses.
    """
    return validate_trade({
        "from": record["proposer"], "to": record["receiver"],
        "players_out": record["players_out"],
        "players_in": record["players_in"],
        "points": record["points"],
        "vetoes": record.get("vetoes", []),
    }, squads_at_gw, accumulated=accumulated, received=received, config=config)


def squads_for_gameweek(gameweek, stored_trades=None, config=None):
    """Everyone's fifteen as they stand for a gameweek, trades applied."""
    base = _read("squads.json")
    if not base:
        return {}
    txs = effective_trades(stored_trades or [], config)
    if not txs:
        return {t["key"]: refresh_clubs(t["squad"]) for t in base["teams"]}
    squads, *_ = apply_transactions(base, txs, gameweek)
    return {k: refresh_clubs(v) for k, v in squads.items()}


def accumulated_points(key, gameweek, season_data):
    """What a manager had scored going into a gameweek — the offer cap."""
    total = 0
    for rnd in season_data.get("rounds", []):
        if rnd["gameweek"] >= gameweek:
            continue
        for m in rnd["matches"]:
            if m["home_key"] == key:
                total += m["home_score"]
            elif m["away_key"] == key:
                total += m["away_score"]
    return total


# ── The points bank ────────────────────────────────────────────────────────
def bank_status(key, gameweek, transactions, drafted=None):
    """What a manager has banked, and what they've already declared to spend.

    The balance comes from the rules engine rather than a running total in a
    column, so it can never disagree with the trades that produced it — and a
    trade voted down leaves nothing behind.
    """
    base = _read("squads.json")
    if not base:
        return {"balance": 0, "spending": 0, "available": False,
                "why": "no squads yet"}

    # Everything up to but not including this gameweek settles the balance;
    # a spend declared for this round is what we're deciding about.
    earlier = [t for t in transactions
               if not (t.get("type") == "bank_use"
                       and t.get("gameweek") == gameweek
                       and t.get("team") == key)]
    managers_arg = {k: {"club": v.get("club"), "sacked_from": v.get("sacked_from")}
                    for k, v in (drafted or {}).items()}
    _, _, _, bank, _, problems = apply_transactions(
        base, earlier, 38, managers=managers_arg)

    spending = next((t.get("points", 0) for t in transactions
                     if t.get("type") == "bank_use"
                     and t.get("gameweek") == gameweek
                     and t.get("team") == key), 0)
    balance = bank.get(key, 0)
    return {
        "balance": balance,
        "spending": spending,
        "available": balance > 0 or spending > 0,
        "why": None if balance or spending else
               "nothing banked — points arrive by accepting a trade that "
               "carries them",
        "problems": [p for p in problems if key in p],
    }


# ── Waivers ────────────────────────────────────────────────────────────────
def free_agents(gameweek, stored_trades=None):
    """Players nobody owns, with their name, position and club."""
    squads = squads_for_gameweek(gameweek, stored_trades)
    owned = {p["id"] for squad in squads.values() for p in squad}
    meta = _read("players.json") or {}
    positions = {int(k): v for k, v in (meta.get("positions") or {}).items()}
    names = meta.get("names") or {}
    player_clubs = {int(k): v for k, v in (meta.get("player_clubs") or {}).items()}
    club_names = clubs()
    POS = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}

    out = []
    for pid, element_type in positions.items():
        if pid in owned:
            continue
        out.append({
            "id": pid,
            "name": names.get(str(pid), f"#{pid}"),
            "position": POS.get(element_type, "?"),
            "club": club_names.get(player_clubs.get(pid), {}).get("short", ""),
        })
    return out


def waiver_order(gameweek, season_data):
    """Standings going into a gameweek, best first — waivers snake from the
    bottom of this list upwards."""
    table = season_data.get("table") or []
    ranked = [r["key"] for r in table]
    return ranked


def run_waivers(gameweek, claims, stored_trades=None, season_data=None):
    """Resolve a gameweek's claims exactly as the rules engine would.

    Used to show managers what would happen if the run went now, and by the
    scoring once the deadline has passed — the same function either way, so
    the preview cannot promise something the real run won't deliver.
    """
    squads = squads_for_gameweek(gameweek, stored_trades)
    order = waiver_order(gameweek, season_data or {}) or sorted(squads)
    results, problems = process_waivers(claims, squads, order)
    return {"results": results, "problems": problems, "order": order,
            "squads": squads}


# ── Player statistics ──────────────────────────────────────────────────────
# What a manager judges a claim on. `sum` accumulates across gameweeks;
# `last` takes the most recent round only.
METRICS = [
    ("total_points",  "Pts",      "sum",  "Points this season"),
    ("event_points",  "GW",       "last", "Points last gameweek"),
    ("form",          "Form",     "form", "Average points over recent gameweeks"),
    ("minutes",       "Mins",     "sum",  "Minutes played"),
    ("starts",        "Starts",   "sum",  "Games started"),
    ("goals_scored",  "Goals",    "sum",  "Goals scored"),
    ("assists",       "Assists",  "sum",  "Assists"),
    ("clean_sheets",  "CS",       "sum",  "Clean sheets"),
    ("bonus",         "Bonus",    "sum",  "Bonus points"),
    ("defensive_contribution", "DefCon", "sum", "Defensive contributions"),
    ("expected_goals", "xG",      "sum",  "Expected goals"),
    ("expected_assists", "xA",    "sum",  "Expected assists"),
    ("expected_goal_involvements", "xGI", "sum", "Expected goal involvements"),
    ("expected_goals_conceded", "xGC", "sum", "Expected goals conceded"),
    ("creativity",    "Creat",    "sum",  "Creativity"),
    ("influence",     "Infl",     "sum",  "Influence"),
    ("threat",        "Threat",   "sum",  "Threat"),
    ("ict_index",     "ICT",      "sum",  "ICT index"),
]

FORM_GAMEWEEKS = 4


@lru_cache(maxsize=4)
def _player_stats(version):
    """Season totals per player, aggregated from the saved gameweeks.

    A metric the fetched data never carried comes back as None rather than
    zero. The difference matters: nought minutes is a fact about a player,
    while a missing expected-goals column is a fact about our data, and
    sorting on it as though it were nought would quietly rank everyone equal.
    """
    files = gameweek_files()
    totals, seen, points_by_gw = {}, {}, {}

    for path in files:
        gw = json.loads(path.read_text())
        n = gw["gameweek"]
        for el in gw["elements"]:
            pid = el["id"]
            stats = el.get("stats") or {}
            row = totals.setdefault(pid, {})
            present = seen.setdefault(pid, set())
            for key, _, how, _ in METRICS:
                if how == "sum":
                    value = stats.get(key)
                    if value is None:
                        continue
                    present.add(key)
                    row[key] = round(row.get(key, 0) + float(value), 2)
            points_by_gw.setdefault(pid, {})[n] = stats.get("total_points")

    latest = max((int(f.stem[2:]) for f in files), default=None)
    for pid, row in totals.items():
        history = points_by_gw.get(pid, {})
        row["event_points"] = history.get(latest)
        recent = [history[g] for g in sorted(history)[-FORM_GAMEWEEKS:]
                  if history.get(g) is not None]
        row["form"] = round(sum(recent) / len(recent), 1) if recent else None
        for key, _, how, _ in METRICS:
            if how == "sum" and key not in seen.get(pid, set()):
                row[key] = None
    return totals


def player_stats():
    return _player_stats(data_version())


def available_metrics():
    """Which metrics the data actually supports, so the page offers no column
    that would be blank for everyone."""
    stats = player_stats()
    if not stats:
        return [m for m in METRICS if m[2] != "sum"]
    have = set()
    for row in stats.values():
        have |= {k for k, v in row.items() if v is not None}
    return [m for m in METRICS if m[0] in have]


def free_agent_pool(gameweek, stored_trades=None):
    """Free agents with their season numbers attached."""
    stats = player_stats()
    pool = []
    for player in free_agents(gameweek, stored_trades):
        pool.append({**player, "stats": stats.get(player["id"], {})})
    return pool


# ── One team's gameweek ────────────────────────────────────────────────────
def team_gameweek(key, gameweek, lineups=None, transactions=None, drafted=None):
    """How a team's round actually went, player by player.

    The same eleven the scoring used, with each player's points, the
    substitutions that were made for them, and whatever the boost and the bank
    added on top — so the total on this page and the total in the table are
    the same number arrived at the same way.
    """
    path = DATA / f"gw{gameweek:02d}.json"
    if not path.exists():
        return None
    gw = json.loads(path.read_text())
    positions = load_positions()
    squads_base = _read("squads.json")
    if not squads_base:
        return None

    points = {}
    for el in gw["elements"]:
        pos = positions.get(el["id"])
        if pos is not None:
            points[el["id"]] = score_entry(el, pos)
    minutes = minutes_from_gameweek(gw)

    transactions = transactions or []
    drafted = drafted or {}
    managers_arg = {k: {"club": v.get("club"), "sacked_from": v.get("sacked_from")}
                    for k, v in drafted.items()}
    squads, adjustments, boost_log, _, _, _ = apply_transactions(
        squads_base, transactions, gameweek, managers=managers_arg)
    roster = squads.get(key) or next(
        (t["squad"] for t in squads_base["teams"] if t["key"] == key), [])
    if not roster:
        # No such manager. The caller turns this into a 404 rather than an
        # empty pitch that looks like a team who scored nothing.
        return None
    roster = refresh_clubs(roster)

    # The same basis the table uses: real submissions laid over the committed
    # placeholder. Reading the database alone would fall back to a best-XI
    # here while the table used the placeholder, and the two pages would show
    # different totals for the same round.
    picked, bench, source = effective_lineup(
        key, gameweek, merge_lineups(lineups or {}), roster)
    if not picked:
        total, _, picked = best_xi(roster, points)
        bench = [p for p in roster if p not in picked]
        source = "best available"
    final_xi, subs = apply_autosubs(picked, bench, minutes)
    swapped_in = {on["id"] for _, on in subs}
    swapped_out = {off["id"] for off, _ in subs}

    def card(player, played=True):
        return {**player, "points": points.get(player["id"], 0),
                "minutes": minutes.get(player["id"], 0),
                "came_on": player["id"] in swapped_in,
                "went_off": player["id"] in swapped_out}

    xi_total = sum(points.get(p["id"], 0) for p in final_xi)

    # Everyone in the fifteen who didn't play for you: the bench that stayed
    # there, then the starters an autosub took off. A player who was replaced
    # otherwise disappears from the page entirely, which reads as a bug — and
    # their points are the ones genuinely left unused.
    sat_out = ([p for p in bench if p["id"] not in swapped_in]
               + [p for p in picked if p["id"] in swapped_out])

    boost = None
    if (gameweek, key) in {(b["gameweek"], b["team"]) for b in boost_log}:
        club = (drafted.get(key) or {}).get("club")
        if club is not None:
            gained, detail = boost_value(xi_total, club, gameweek,
                                         _read("pl_fixtures.json") or [])
            boost = {"points": gained,
                     "club": clubs().get(club, {}).get("name"), **detail}

    adjustment = adjustments.get(gameweek, {}).get(key, 0)
    lines = {}
    for player in final_xi:
        lines.setdefault(player["position"], []).append(card(player))

    names = {t["key"]: t.get("team", t["key"]) for t in squads_base["teams"]}
    return {
        "key": key, "team": names.get(key, key), "gameweek": gameweek,
        "state": state(gw), "source": source,
        "lines": [(pos, lines.get(pos, [])) for pos in ("GK", "DEF", "MID", "FWD")],
        "bench": [card(p) for p in sat_out],
        "subs": [{"off": off["name"], "on": on["name"],
                  "points": points.get(on["id"], 0)} for off, on in subs],
        "xi_total": xi_total,
        "boost": boost,
        "adjustment": adjustment,
        "total": xi_total + (boost["points"] if boost else 0) + adjustment,
        "bench_points": sum(points.get(p["id"], 0) for p in sat_out),
    }


# ── One player ─────────────────────────────────────────────────────────────
def player_detail(player_id, ahead=5):
    """Everything worth knowing about a player, for the popup.

    A headline of the season so far, what they did in each gameweek, and who
    they play next — the three things a manager weighs before starting someone
    or claiming them.
    """
    meta = _read("players.json") or {}
    positions = {int(k): v for k, v in (meta.get("positions") or {}).items()}
    if player_id not in positions:
        return None
    POS = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}
    club_id = (meta.get("player_clubs") or {}).get(str(player_id))
    club_names = clubs()

    history = []
    for path in gameweek_files():
        gw = json.loads(path.read_text())
        entry = next((e for e in gw["elements"] if e["id"] == player_id), None)
        if entry is None:
            continue
        stats = entry.get("stats") or {}
        history.append({
            "gameweek": gw["gameweek"],
            "points": score_entry(entry, positions[player_id]),
            "breakdown": entry_breakdown(entry, positions[player_id]),
            "minutes": stats.get("minutes") or 0,
            "goals": stats.get("goals_scored") or 0,
            "assists": stats.get("assists") or 0,
            "clean_sheet": bool(stats.get("clean_sheets")),
            "bonus": stats.get("bonus") or 0,
            "defcon": stats.get("defensive_contribution"),
            "xg": stats.get("expected_goals"),
            "xa": stats.get("expected_assists"),
            "opponent": None, "home": None,
        })

    # Who they played, and who they play next.
    fixtures = _read("pl_fixtures.json") or []
    played = {h["gameweek"] for h in history}
    upcoming = []
    for fx in sorted((f for f in fixtures if f.get("event")),
                     key=lambda f: (f["event"], f.get("kickoff_time") or "")):
        if club_id not in (fx["team_h"], fx["team_a"]):
            continue
        home = fx["team_h"] == club_id
        other = fx["team_a"] if home else fx["team_h"]
        row = {"gameweek": fx["event"], "home": home,
               "opponent": club_names.get(other, {}).get("short", "?")}
        if fx["event"] in played:
            for h in history:
                if h["gameweek"] == fx["event"]:
                    h["opponent"], h["home"] = row["opponent"], home
        elif not fx["finished"] and len(upcoming) < ahead:
            upcoming.append(row)

    totals = player_stats().get(player_id, {})
    owner = None
    squads = _read("squads.json") or {"teams": []}
    for team in squads["teams"]:
        if any(p["id"] == player_id for p in team["squad"]):
            owner = team.get("team", team["key"])

    return {
        "id": player_id,
        "name": (meta.get("names") or {}).get(str(player_id), f"#{player_id}"),
        "position": POS.get(positions[player_id], "?"),
        "club": club_names.get(club_id, {}).get("name", ""),
        "club_short": club_names.get(club_id, {}).get("short", ""),
        "owner": owner,
        "totals": totals,
        "history": history,
        "upcoming": upcoming,
        "best": max((h["points"] for h in history), default=None),
    }
