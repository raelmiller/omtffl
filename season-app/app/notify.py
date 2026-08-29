"""What is worth interrupting someone for, and making sure it happens once.

`push.py` knows how to encrypt and post a notification. This decides whether
one should exist at all.

The bar is deliberately high. Fourteen people agreed to notifications for the
things they would be annoyed to have missed — a deadline they were going to
pick a team before, an offer sitting unanswered — and every notification
beyond those spends the goodwill that keeps the useful ones switched on.
So: nothing about other people's transactions, nothing about results (the
table is not going anywhere), and nothing a manager did themselves.
"""
from __future__ import annotations

import threading

from . import db, engine, live, push

# How long before a deadline to warn. Long enough to do something about it
# from wherever you are, short enough that "later" doesn't mean "forgotten".
DEADLINE_WARNING_HOURS = 3
WAIVER_WARNING_HOURS = 3


def _deliver(subscriptions, message):
    """Send to every app, pruning the ones the service says are gone.

    404 and 410 are the push service telling us the app is uninstalled or its
    data cleared. Keeping those rows means every future send has a guaranteed
    failure in it, and the failure column stops meaning anything.
    """
    sent = 0
    for sub in subscriptions:
        status, detail = push.send(sub, message)
        if status in (404, 410):
            db.unsubscribe_push(sub["endpoint"])
            continue
        ok = 200 <= status < 300
        db.mark_push(sub["endpoint"], ok, None if ok else f"{status} {detail}")
        sent += ok
    return sent


def to_manager(key, title, body, url="/", tag=None, background=True,
               wanting="want_deadlines"):
    """Notify one manager on every app they have allowed.

    Sent on a thread by default. A push service having a slow minute must
    never be why accepting a trade took eight seconds, and there is nothing
    the caller could usefully do with the result anyway — delivery is the push
    service's problem the moment it takes the message.
    """
    subscriptions = db.push_subscriptions(key, wanting=wanting)
    if not subscriptions or not push.configured():
        return 0
    message = {"title": title, "body": body, "url": url, "tag": tag or url}
    if not background:
        return _deliver(subscriptions, message)
    threading.Thread(target=_deliver, args=(subscriptions, message),
                     daemon=True).start()
    return len(subscriptions)


def once(kind, gameweek, key, title, body, url="/"):
    """Send a notice that must not repeat, claiming it before sending.

    The claim is written first. A reminder that arrives twice because a send
    failed halfway is a worse outcome than one that never arrives: the second
    is a bug someone reports, the first is the app crying wolf until people
    turn it off.
    """
    if db.notice_already_sent(kind, gameweek, key):
        return False
    # Nothing to send to, so nothing to remember. Claiming it here would mean
    # a manager who turns notifications on ten minutes later has already
    # "had" the warning and silently never gets it, while the deadline it
    # was about is still hours away.
    if not db.push_subscriptions(key, wanting="want_deadlines"):
        return False
    db.record_notice(kind, gameweek, key)
    to_manager(key, title, body, url=url, tag=f"{kind}-{gameweek}")
    return True


# ── The events themselves ──────────────────────────────────────────────────
def trade_offered(trade, proposer_team):
    """Someone has offered you a trade. Immediate, and only to the receiver.

    The proposer knows: they just did it.
    """
    out = ", ".join(p.get("name", "?") for p in trade["players_out"]) or "nobody"
    back = ", ".join(p.get("name", "?") for p in trade["players_in"]) or "nobody"
    points = f" and {trade['points']} points" if trade["points"] else ""
    to_manager(
        trade["receiver"],
        f"{proposer_team} has offered you a trade",
        f"{out}{points} for {back}. It expires when the trade window shuts.",
        url="/trade", tag=f"trade-{trade['id']}")


def deadlines_due():
    """The scheduled sweep: who still needs warning about what.

    Runs often and sends rarely. Every send is claimed in `notice_sent` first,
    so running it four times an hour and running it once an hour produce the
    same notifications.
    """
    if not push.configured():
        return {"notices": 0, "why": "push not configured"}

    gw = engine.current_gameweek()
    if not gw:
        return {"notices": 0, "why": "no gameweek"}
    number = gw["gameweek"]
    sent = 0

    lock = engine.deadline_state(gw)
    waivers = engine.waiver_state(gw)

    # The team sheet — but only when there is something wrong with it.
    #
    # A pick rolls over until it is changed, so "you haven't picked" describes
    # most managers most weeks and is not news. Sending it anyway is how a
    # notification earns itself a place in the settings a manager turns off.
    # So this asks the team instead: is anyone in the eleven injured,
    # suspended, doubtful, or without a fixture to play in?
    #
    # Note this is not conditioned on whether they submitted. Someone who
    # picked on Tuesday and had a striker pull up on Thursday is precisely who
    # needs telling, and a "did you submit" check would skip exactly them.
    hours = (lock.get("seconds") or 0) / 3600
    if lock.get("open") and 0 < hours <= DEADLINE_WARNING_HOURS:
        transactions = db.transactions() + engine.effective_trades(db.trades())
        for manager in db.managers():
            problems = engine.needs_attention(
                manager["key"], number, db.all_lineups(), transactions)
            if not problems:
                continue
            # Three names is a notification; eleven is a wall of text nobody
            # reads on a lock screen. The page has the rest.
            shown = "; ".join(problems[:3])
            if len(problems) > 3:
                shown += f", and {len(problems) - 3} more"
            sent += once(
                "deadline", number, manager["key"],
                f"{gw['name']} deadline in {round(hours)}h",
                f"{shown}. Your team plays as it stands.", url="/declare")

    # The waiver window, which shuts a day earlier and is the one people
    # forget exists. Only to managers with claims in, since it is a reminder
    # that the window is closing rather than an instruction to use it.
    wh = (waivers.get("waiver_seconds") or 0) / 3600
    if waivers.get("waivers_open") and 0 < wh <= WAIVER_WARNING_HOURS:
        claimed = {d["manager"] for d in db.declarations("waiver")
                   if d["gameweek"] == number}
        for key in claimed:
            sent += once(
                "waivers", number, key,
                f"Waivers run in {round(wh)}h",
                "Your claims are locked in when the window shuts. Change the "
                "order while you still can.", url="/waivers")

    # Notices raised, not notifications delivered — delivery is the push
    # service's business the moment it takes the message.
    return {"notices": sent, "gameweek": number}


# ── What your players are doing, while they are doing it ───────────────────
# Only the events that move points enough to be worth a buzz. Bonus is
# deliberately absent: it moves all match and is not settled until well after
# full time, so it would notify repeatedly and be wrong most of those times.
WATCHED_EVENTS = [
    ("goals_scored", "Goal"),
    ("assists", "Assist"),
    ("penalties_saved", "Penalty saved"),
    ("penalties_missed", "Penalty missed"),
    ("own_goals", "Own goal"),
    ("red_cards", "Red card"),
]


def _kicked_off():
    """Whether any match is in progress — the engine's answer, not a second one.

    The point is to not ask FPL for fixtures at four in the morning. A round
    with nothing on gets no request at all, which is the difference between
    polling every minute and polling every minute *during matches*.
    """
    return engine.matches_in_progress()


def _events_now(fixtures):
    """Every event in the round as (player, label, count, fixture).

    FPL reports cumulative totals, not events: `goals_scored` says Salah has
    two, and says it again on the next poll. So this returns the running
    count and the caller decides which counts it has not seen — "Salah has
    two" is not news, "Salah's second" is.
    """
    out = []
    for fixture in fixtures:
        stats = {s.get("identifier"): s for s in (fixture.get("stats") or [])}
        for identifier, label in WATCHED_EVENTS:
            block = stats.get(identifier) or {}
            for side in ("h", "a"):
                for row in block.get(side) or []:
                    if "element" in row and (row.get("value") or 0) > 0:
                        out.append((row["element"], label, row["value"],
                                    fixture.get("id")))
    return out


def match_events():
    """Tell each manager what their squad has just done. Batched, once each.

    Runs on a timer during matches. Every event is claimed in `notice_sent`
    under a key naming the fixture, the player and *which* goal it was, so a
    poll that repeats an event it has already reported sends nothing — which
    is every poll after the first, since FPL keeps reporting the total.

    Batched on purpose: two things in the same minute are one notification,
    not two a few seconds apart. A manager owns fifteen players and a busy
    Saturday would otherwise be a phone that will not stop.
    """
    if not push.configured():
        return {"notices": 0, "why": "push not configured"}
    if not _kicked_off():
        return {"notices": 0, "why": "no match in progress"}

    gw = engine.live_gameweek()
    if not gw:
        return {"notices": 0, "why": "no live gameweek"}
    number = gw["gameweek"]

    fixtures, error = live.fetch(number)
    if error and not fixtures:
        return {"notices": 0, "why": error}

    events = _events_now(fixtures)
    if not events:
        return {"notices": 0, "gameweek": number}

    names = engine.player_names()
    squads = engine.market(number, db.trades(), db.transactions())["squads"]
    sent = 0

    for key, squad in squads.items():
        # The whole squad, not the eleven: a manager wants to know their bench
        # forward scored, both because substitutes come on and because owning
        # him is the thing that makes it interesting.
        owned = {p["id"] for p in squad}
        if not db.push_subscriptions(key, wanting="want_events"):
            continue

        fresh = []
        for player, label, count, fixture in events:
            if player not in owned:
                continue
            # The count is part of the key, so a second goal is a new notice
            # and the first one is not re-sent.
            kind = f"ev:{fixture}:{player}:{label}:{count}"
            if db.notice_already_sent(kind, number, key):
                continue
            db.record_notice(kind, number, key)
            fresh.append((player, label, count))

        if not fresh:
            continue

        lines = []
        for player, label, count in fresh:
            who = names.get(player, f"player {player}")
            # "Goal" for the first, "2nd goal" after that — the number is the
            # news once there has already been one.
            nth = "" if count == 1 else f" ({count})"
            lines.append(f"{who} — {label}{nth}")
        title = lines[0] if len(lines) == 1 else f"{len(lines)} for your squad"
        to_manager(key, title, " · ".join(lines), url="/live",
                   tag=f"events-{number}", wanting="want_events")
        sent += 1

    return {"notices": sent, "gameweek": number}
