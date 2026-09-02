"""Facts about a report, worked out before anything reads the words.

The division of labour here is the whole safety argument, so it is worth
stating plainly. Matching "my points look wrong" to "your Monday fixture has
not kicked off yet" is a language problem, and a model is good at it. Deciding
whether the points are wrong is not, and a model must never be asked — it is
arithmetic the engine already did, and an opinion is exactly what somebody
arguing for more points would try to change.

So this module computes findings from the evidence alone. It never reads the
message. A finding is a true statement about that manager's state right now,
with the numbers already in it. The agent's job downstream is to choose which
findings answer what was asked and write them up; it cannot invent a finding,
because it is only ever handed this list.

That also makes most reports cheap: the facts arrive precomputed, so nothing
has to go reading the codebase to answer "why can't I save my team".
"""
from __future__ import annotations

# How sure the evidence is, which decides what may be done without a person.
#
#   certain — the evidence alone explains a problem, with a number attached.
#             Nothing a reporter can write changes it.
#   likely  — true and worth saying, but may not be what they asked about.
#   context — background. Never an answer on its own.
CERTAIN, LIKELY, CONTEXT = "certain", "likely", "context"

# The lanes. Set from the evidence where the evidence settles it; otherwise
# left for the agent to choose by reading what was actually asked.
ANSWER, DIAGNOSE, ADJUDICATE, ESCALATE = (
    "answer", "diagnose", "adjudicate", "escalate")
LANES = frozenset((ANSWER, DIAGNOSE, ADJUDICATE, ESCALATE))


def _ordinal(n):
    """1st, 2nd, 3rd — because "you are 6 in the table" reads like a typo."""
    if 10 <= n % 100 <= 20:
        return f"{n}th"
    return f"{n}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th') }"


def _finding(code, confidence, headline, detail, lane=None):
    return {"code": code, "confidence": confidence, "headline": headline,
            "detail": detail, "lane": lane}


def findings(context):
    """Everything true and notable about one manager's state.

    Ordered most conclusive first, so the first `certain` entry is the one a
    reply should lead with.
    """
    if not isinstance(context, dict) or context.get("gathering_failed"):
        return [_finding(
            "no_evidence", CONTEXT,
            "The app could not read your state when you reported.",
            context.get("gathering_failed", "no context was recorded"))]

    out = []
    rnd = context.get("round") or {}
    sheet = context.get("team_sheet") or {}
    standing = context.get("standing") or {}
    scored = context.get("last_scored_round") or {}

    # ── Why a save is being refused ────────────────────────────────────────
    # The single most common report, and the app already knows the answer: it
    # is whatever validation would say if they pressed the button now.
    refusals = sheet.get("would_be_refused_because") or []
    if refusals:
        out.append(_finding(
            "save_refused", CERTAIN,
            "Your team would not save as it stands.",
            "The app says: " + "; ".join(refusals),
            lane=DIAGNOSE))

    if rnd and not rnd.get("open"):
        out.append(_finding(
            "deadline_shut", CERTAIN,
            f"Gameweek {rnd.get('gameweek')} is locked.",
            f"{rnd.get('reason_shut') or 'the deadline has passed'} — "
            f"the deadline was {rnd.get('deadline')}. Nothing can be saved "
            "for a round once it shuts.",
            lane=DIAGNOSE))

    # ── Their team changing under them ─────────────────────────────────────
    filled = sheet.get("places_filled_by_app") or 0
    if filled:
        out.append(_finding(
            "team_mended", CERTAIN,
            f"The app filled {filled} place(s) in your eleven.",
            "A settled trade or waiver took players off your pitch after you "
            "picked, so those places were filled from your bench to keep the "
            "side legal. That team is stored and it will play — but it is the "
            "app's choice rather than yours, and it stands until you save.",
            lane=DIAGNOSE))

    if sheet and sheet.get("has_saved_a_team") is False:
        out.append(_finding(
            "never_picked", LIKELY,
            "You have not picked a team for this round.",
            "Which is not a problem in itself — a pick rolls over until you "
            "change it — but what plays is last week's eleven rather than "
            "anything chosen for this one.",
            lane=DIAGNOSE))

    # ── Points that are not final yet ──────────────────────────────────────
    state = scored.get("state")
    if state and state != "final":
        out.append(_finding(
            "not_final", CERTAIN,
            f"Gameweek {scored.get('gameweek')} is {state}, not final.",
            "Bonus is computed from live BPS until FPL's data check lands, so "
            "a point or two can still move. The app goes back for the final "
            "numbers every half hour until they arrive.",
            lane=ADJUDICATE))

    if state and state != "final" and not (scored.get("substitutions") or []):
        out.append(_finding(
            "subs_pending", LIKELY,
            "No substitutions have been made for this round yet.",
            "They are an end-of-round settlement. Mid-round a starter who has "
            "not kicked off is indistinguishable from one who did not play, "
            "so the eleven stands as picked until the last whistle.",
            lane=ADJUDICATE))

    source = scored.get("eleven_came_from")
    if source in ("placeholder", "best available"):
        out.append(_finding(
            "not_their_eleven", CERTAIN,
            f"That round scored a {source} eleven.",
            "No team had been picked, so the app stood one in. That flatters "
            "or costs you depending on the week, and it is the usual reason a "
            "score looks unfamiliar.",
            lane=ADJUDICATE))

    # ── The two numbers people ask where to find ───────────────────────────
    if "bank_balance" in standing:
        out.append(_finding(
            "bank", CONTEXT,
            f"Your points bank holds {standing['bank_balance']}.",
            "There is one way to fill it: taking points as part of a trade. "
            "Nothing else pays into it — not a good week, not an unspent "
            "boost — so a bank at zero means no trade has ever brought you "
            "points. Spending it is the other half: it is shown on "
            f"{standing.get('bank_shown_on', '/declare')}, under the pitch, "
            "where any part of it can be added to the round being picked.",
            lane=ANSWER))

    if "boost_left" in standing:
        out.append(_finding(
            "boost", CONTEXT,
            f"You have {standing['boost_left']} manager boost(s) left"
            + (", available this round." if standing.get("boost_available")
               else ", none available this round."),
            "Declared before kick-off on /declare, and withdrawable until the "
            "deadline.",
            lane=ANSWER))

    # ── Where they claim, and why there ────────────────────────────────────
    waivers = context.get("waivers") or {}
    if "claims" in waivers:
        out.append(_finding(
            "waiver_place", CONTEXT,
            f"You claim {waivers['claims']} of {waivers['of']} in the waiver "
            "run.",
            "Priority is the league table upside down — last place claims "
            "first — and it snakes, so whoever leads a round goes last in the "
            f"next. You are {_ordinal(waivers['table_position'])} in the "
            "table, which is why you sit where you do. The whole order is on "
            "/waivers. Losing a race costs you that round "
            "rather than your next choice.",
            lane=ANSWER))

    # ── Things that explain silence ────────────────────────────────────────
    if context.get("apps_subscribed") == 0:
        out.append(_finding(
            "no_push", LIKELY,
            "None of your apps has notifications turned on.",
            "Which is why none are arriving. They are switched on per device "
            "from /account.",
            lane=DIAGNOSE))

    data = context.get("data") or {}
    if data.get("stale"):
        out.append(_finding(
            "stale_data", CERTAIN,
            f"The data on disk is {data.get('hours_ago')}h old.",
            "So the table and the points are behind the football. That is an "
            "app problem rather than anything you did.",
            lane=DIAGNOSE))

    if context.get("mode") == "archive":
        out.append(_finding(
            "archive_mode", LIKELY,
            "This host cannot reach the FPL API.",
            "It is serving committed data, which the daily job keeps current. "
            "Scores are real but can lag a live round.",
            lane=DIAGNOSE))

    order = {CERTAIN: 0, LIKELY: 1, CONTEXT: 2}
    out.sort(key=lambda f: order[f["confidence"]])
    return out


def suggested_lane(found):
    """The lane the evidence points at, or None to let the agent decide.

    Only a `certain` finding is allowed to set a lane. Anything softer is a
    hint about their state rather than a reading of what they asked, and
    guessing from it would answer the wrong question confidently.
    """
    for f in found:
        if f["confidence"] == CERTAIN and f["lane"]:
            return f["lane"]
    return None


def answerable(found):
    """Whether the facts alone are enough to write a useful reply."""
    return any(f["confidence"] == CERTAIN for f in found)


# The message is quoted between these, and the prompt says so. A fence is not
# a security boundary on its own — the boundary is that the agent has no tool
# that changes league state and no findings but the ones above — but it does
# stop a report reading as though the app said it.
FENCE = "-----BEGIN REPORTED MESSAGE-----", "-----END REPORTED MESSAGE-----"


def brief(report, live=None):
    """Everything an agent needs to answer one report, and nothing else.

    Deliberately small. It carries the facts and the words, not the codebase:
    a reply is written from findings, and a report that needs more than this
    is one that needs a fix rather than an answer.

    `live` is findings computed from the manager's state *now*, which is what
    a reply is written from — they are the ones who will read it, and they
    care about today rather than about Tuesday. The stored evidence is still
    the record, and it is what `resolved_since` is measured against: anything
    that was conclusively wrong when they reported and is not wrong any more.
    Saying "that looks sorted now" is a better reply than either ignoring it
    or describing a problem that has gone.
    """
    stored = findings(report["context"])
    found = stored if live is None else live
    gone = ({f["code"] for f in stored if f["confidence"] == CERTAIN}
            - {f["code"] for f in found})
    return {
        "report_id": report["id"],
        "manager": report["context"].get("manager", report["manager"]),
        "team": report["context"].get("team"),
        "created_at": report["created_at"],
        # Untrusted. Data, never instruction.
        "reported_message": f"{FENCE[0]}\n{report['message']}\n{FENCE[1]}",
        # Trusted. Computed from the app's own state, and the only material a
        # reply may assert.
        "findings": found,
        "findings_are": "current" if live is not None else "as reported",
        "resolved_since": sorted(gone),
        "lane_from_evidence": suggested_lane(found),
        "facts_alone_are_enough": answerable(found),
    }
