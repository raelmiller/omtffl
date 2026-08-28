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

from . import db, engine, push

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


def to_manager(key, title, body, url="/", tag=None, background=True):
    """Notify one manager on every app they have allowed.

    Sent on a thread by default. A push service having a slow minute must
    never be why accepting a trade took eight seconds, and there is nothing
    the caller could usefully do with the result anyway — delivery is the push
    service's problem the moment it takes the message.
    """
    subscriptions = db.push_subscriptions(key)
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
    if not db.push_subscriptions(key):
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
    # all_lineups is {gameweek: {manager: ...}}, the shape the engine reads.
    picked = set(db.all_lineups().get(number, {}))

    # The team sheet. Only to managers who have not picked — a reminder to do
    # something you have already done is the fastest way to be muted.
    hours = (lock.get("seconds") or 0) / 3600
    if lock.get("open") and 0 < hours <= DEADLINE_WARNING_HOURS:
        for manager in db.managers():
            if manager["key"] in picked:
                continue
            sent += once(
                "deadline", number, manager["key"],
                f"{gw['name']} deadline in {round(hours)}h",
                "You haven't picked a team yet. Last year's XI rolls over if "
                "you don't.", url="/declare")

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
