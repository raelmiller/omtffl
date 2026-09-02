"""What the app knew when someone said something was wrong.

The whole value of a report is the state around it, and that state has to be
gathered here rather than accepted from the browser. A manager saying "I saved
my team in time" is a claim; the stored timestamp is a fact, and the two are
worth telling apart. Anything a reporter could dress up — who they are, when
it was, what the deadline was, whether the save actually landed — comes from
the session and the database, never from the request body.

Kept to a summary. This is read by a person and by a triage agent working from
a short prompt, so it answers the questions a first reply would otherwise have
to ask for, and stops there: no squad dumps, no full league state, nothing
belonging to another manager.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from . import db, engine, fetcher, push


def _round(target):
    """The gameweek being picked for, and how much of it is still open."""
    if target is None:
        return None
    lock = engine.deadline_state(target)
    return {
        "gameweek": target["gameweek"],
        "deadline": target.get("deadline"),
        "open": bool(lock.get("open")),
        "seconds_left": lock.get("seconds"),
        "reason_shut": None if lock.get("open") else lock.get("reason"),
    }


def _team(key, target):
    """Their side for that round, and whether it would pass a save."""
    if target is None:
        return None
    n = target["gameweek"]
    squad = (engine.market(n, db.trades(), db.transactions(),
                           for_manager=key)["squads"].get(key)
             or engine.squad_for(key))
    stored = db.get_lineup(key, n)
    out = {
        "squad_size": len(squad),
        "has_saved_a_team": stored is not None,
        "saved_at": (stored or {}).get("updated_at"),
        # Non-zero means the app filled a place after a trade moved someone
        # off their pitch — the first thing to check when a team looks wrong.
        "places_filled_by_app": (stored or {}).get("mended", 0),
    }
    if stored:
        xi, bench, filled = engine.reconcile(stored, squad)
        entry = {"xi": [p["id"] for p in xi], "bench": [p["id"] for p in bench]}
        errors, warnings = engine.validate_lineup(entry, squad)
        out.update({
            "xi_size": len(xi),
            "bench_size": len(bench),
            "formation": engine.legal_formation(xi) or "legal",
            # Empty means a save would be accepted right now. Anything here is
            # the actual reason "I can't save my team".
            "would_be_refused_because": errors,
            "warnings": warnings,
            "names_players_no_longer_owned": filled > 0,
        })
    return out


def _standing(key, target):
    """The two numbers people ask about most, and where to find them."""
    if target is None:
        return None
    n = target["gameweek"]
    tx = db.transactions() + engine.effective_trades(db.trades())
    used = sum(1 for d in db.declarations("boost", key) if d["gameweek"] != n)
    try:
        bank = engine.bank_status(key, n, tx, db.manager_clubs())
        boost = engine.boost_status(key, n, db.manager_clubs(), used,
                                    declared=bool(db.declaration(key, n, "boost")))
    except Exception as exc:                       # noqa: BLE001
        # A report about something being broken must still record itself.
        return {"unavailable": f"{type(exc).__name__}: {exc}"}
    return {
        "bank_balance": bank.get("balance"),
        "bank_shown_on": "/declare",
        "boost_available": boost.get("available"),
        "boost_left": boost.get("left"),
        "boost_declared": boost.get("declared"),
    }


def _scoring(key):
    """How the last scored round was arrived at, for "my points are wrong".

    The engine's own working, not a summary of it: the eleven that played,
    what each of them scored, which substitutions fired, and what the boost
    and any traded points added. A dispute is answered by showing this, so it
    has to be in the evidence rather than fetched later on trust.
    """
    played = [r for r in engine.calendar() if r.get("has_data")]
    if not played:
        return None
    n = max(r["gameweek"] for r in played)
    tx = db.transactions() + engine.effective_trades(db.trades())
    try:
        detail = engine.team_gameweek(key, n, db.all_lineups(), tx,
                                      db.manager_clubs())
    except Exception as exc:                       # noqa: BLE001
        return {"gameweek": n, "unavailable": f"{type(exc).__name__}: {exc}"}
    if detail is None:
        return {"gameweek": n, "unavailable": "no such team that round"}
    return {
        "gameweek": n,
        "state": detail["state"],
        "eleven_came_from": detail["source"],
        "xi_total": detail["xi_total"],
        "total": detail["total"],
        "boost": detail.get("boost"),
        "traded_points": detail.get("adjustment"),
        "substitutions": detail.get("subs"),
        "players": [{"name": p["name"], "points": p["points"],
                     "minutes": p["minutes"]}
                    for _pos, line in detail["lines"] for p in line],
        "bench": [{"name": p["name"], "points": p["points"],
                   "minutes": p["minutes"]} for p in detail["bench"]],
    }


def _waivers(key, target):
    """Where they claim in the run, and why there.

    Asked on the first real report the agent ever saw — "why am I so low in
    the waiver list" — which it correctly refused to answer, because nothing
    in the evidence said. It is not a hard question: priority is the table
    upside down, and the app already computes it.
    """
    if target is None:
        return None
    try:
        season = engine.season(db.all_lineups(),
                               db.transactions() + engine.effective_trades(db.trades()),
                               db.manager_clubs())
        order = engine.waiver_order(target["gameweek"], season)
    except Exception as exc:                       # noqa: BLE001
        return {"unavailable": f"{type(exc).__name__}: {exc}"}
    if key not in order:
        return None
    # waiver_order is the table best-first; claims run from the bottom up.
    return {"claims": len(order) - order.index(key), "of": len(order),
            "table_position": order.index(key) + 1}


def gather(key, page=None):
    """Everything worth knowing about one manager's report.

    `page` is the only thing taken from the browser, and it is a hint about
    where they were rather than evidence of anything — recorded as such.
    """
    target = engine.current_gameweek()
    manager = db.manager_by_key(key) or {}
    return {
        "manager": key,
        "team": manager.get("team"),
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "reported_from_page": page,          # a hint, not evidence
        "round": _round(target),
        "team_sheet": _team(key, target),
        "standing": _standing(key, target),
        "waivers": _waivers(key, target),
        "last_scored_round": _scoring(key),
        # "I'm not getting notifications" needs this, and nothing else does.
        "apps_subscribed": len(db.push_subscriptions(key)),
        "push_configured": push.configured(),
        # Which code and which data — half of "it's broken for me" is one of
        # these being older than the person assumes.
        "build": os.environ.get("RAILWAY_GIT_COMMIT_SHA", "")[:7] or None,
        "data": engine.data_age(),
        "mode": fetcher.mode(),
    }
