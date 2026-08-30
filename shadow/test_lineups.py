#!/usr/bin/env python3
"""Unit tests for weekly lineup submission.

The interesting cases are the ones that decide whether a manager gets robbed:
an autosub that would break the formation, a keeper who can only be replaced
by a keeper, a rolled-over lineup naming someone since traded away, and a
lineup submitted after the deadline.

Run: python3 shadow/test_lineups.py
"""
import sys

from lineups import (
    apply_autosubs, effective_lineup, legal_formation, round_is_over,
    suggest_lineup, validate,
)

FAILS = []


def check(name, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")
    if not ok:
        FAILS.append(name)


def check_true(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}{(' — ' + detail) if detail else ''}")
    if not cond:
        FAILS.append(name)


def p(pid, pos, name=None):
    return {"id": pid, "position": pos, "name": name or f"{pos}{pid}"}


# A legal 15: 2 GK, 5 DEF, 5 MID, 3 FWD
SQUAD = ([p(1, "GK"), p(2, "GK")]
         + [p(10 + i, "DEF") for i in range(5)]
         + [p(20 + i, "MID") for i in range(5)]
         + [p(30 + i, "FWD") for i in range(3)])
BY_ID = {x["id"]: x for x in SQUAD}


def ids(players):
    return [x["id"] for x in players]


print("── Formation ───────────────────────────────────────────")

XI_442 = [BY_ID[i] for i in (1, 10, 11, 12, 13, 20, 21, 22, 23, 30, 31)]
check("a legal 4-4-2 passes", legal_formation(XI_442), None)
check_true("ten players is rejected",
           "10 players" in (legal_formation(XI_442[:10]) or ""))
check_true("two keepers is rejected",
           "GK" in (legal_formation([BY_ID[2]] + XI_442[:10]) or ""),
           str(legal_formation([BY_ID[2]] + XI_442[:10])))
check_true("two defenders is rejected",
           "DEF" in (legal_formation(
               [BY_ID[i] for i in (1, 10, 11, 20, 21, 22, 23, 24, 30, 31, 32)]) or ""))

print("\n── Validation ──────────────────────────────────────────")

good = {"xi": ids(XI_442), "bench": [2, 14, 24, 32]}
errors, warnings = validate(good, SQUAD)
check("a good submission has no errors", errors, [])

dup = {"xi": ids(XI_442)[:-1] + [20], "bench": []}
errors, _ = validate(dup, SQUAD)
check_true("a duplicated player is caught",
           any("twice" in e for e in errors), str(errors))

foreign = {"xi": ids(XI_442)[:-1] + [999], "bench": []}
errors, _ = validate(foreign, SQUAD)
check_true("picking someone you don't own is caught",
           any("not in the squad" in e for e in errors), str(errors))

both = {"xi": ids(XI_442), "bench": [30]}
errors, _ = validate(both, SQUAD)
check_true("naming a player twice across XI and bench is caught",
           any("both starting and benched" in e for e in errors), str(errors))

late = {"xi": ids(XI_442), "bench": [], "submitted_at": "2025-08-15T18:00:00Z"}
errors, _ = validate(late, SQUAD, deadline="2025-08-15T17:30:00Z")
check_true("a late submission is rejected",
           any("after the" in e for e in errors), str(errors))

intime = {"xi": ids(XI_442), "bench": [], "submitted_at": "2025-08-15T17:00:00Z"}
errors, _ = validate(intime, SQUAD, deadline="2025-08-15T17:30:00Z")
check("in time is accepted", errors, [])

_, warnings = validate({"xi": ids(XI_442), "bench": []}, SQUAD, deadline="2025-08-15T17:30:00Z")
check_true("an untimed submission warns rather than failing",
           any("can't be checked" in w for w in warnings), str(warnings))

print("\n── Autosubs ────────────────────────────────────────────")

BENCH = [BY_ID[i] for i in (14, 24, 32, 2)]

# Everyone played: nothing happens.
mins = {x["id"]: 90 for x in SQUAD}
final, subs = apply_autosubs(XI_442, BENCH, mins)
check("no subs when everyone plays", subs, [])

# One midfielder blanks; the first bench player who played comes on.
mins = {x["id"]: 90 for x in SQUAD}
mins[20] = 0
final, subs = apply_autosubs(XI_442, BENCH, mins)
check("one blank brings on one sub", len(subs), 1)
check("the right player comes off", subs[0][0]["id"], 20)
check_true("a legal replacement comes on", legal_formation(final) is None)

# A bench player who also didn't play is skipped.
mins = {x["id"]: 90 for x in SQUAD}
mins[20] = 0
mins[14] = 0
final, subs = apply_autosubs(XI_442, BENCH, mins)
check("a bench player who blanked is skipped", subs[0][1]["id"], 24)

# The keeper blanks: only the other keeper can replace them.
mins = {x["id"]: 90 for x in SQUAD}
mins[1] = 0
final, subs = apply_autosubs(XI_442, BENCH, mins)
check("a blanking keeper is replaced by the reserve keeper", subs[0][1]["id"], 2)

# An outfielder must never be replaced by the reserve keeper.
mins = {x["id"]: 0 for x in SQUAD}
mins[2] = 90  # only the reserve keeper played
final, subs = apply_autosubs(XI_442, BENCH, mins)
check_true("the reserve keeper never covers an outfielder",
           all(on["position"] == "GK" for _, on in subs), str([(o['id'], n['id']) for o, n in subs]))

# A 3-at-the-back XI can't drop to two defenders, so a defender blanking
# with only defenders benched is coverable, but a lone forward is not.
XI_352 = [BY_ID[i] for i in (1, 10, 11, 12, 20, 21, 22, 23, 24, 30, 31)]
BENCH_D = [BY_ID[i] for i in (13, 14, 32, 2)]
mins = {x["id"]: 90 for x in SQUAD}
mins[10] = 0
final, subs = apply_autosubs(XI_352, BENCH_D, mins)
check("a defender is replaced by a defender", subs[0][1]["position"], "DEF")
check_true("the formation stays legal", legal_formation(final) is None)

# Only a forward on the bench, and a defender blanks in a 3-at-the-back
# side: bringing the forward on would leave two defenders, so it can't happen.
BENCH_F = [BY_ID[i] for i in (32, 2)]
mins = {x["id"]: 90 for x in SQUAD}
mins[10] = 0
final, subs = apply_autosubs(XI_352, BENCH_F, mins)
check("an illegal substitution is refused", subs, [])
check_true("and the XI is left a man light rather than made illegal",
           len([x for x in final if mins.get(x["id"], 0)]) == 10)

# ── Only once the round is over ────────────────────────────────────────────
# Saturday evening: some of the eleven have played, one has a Monday fixture.
# His zero minutes mean "hasn't kicked off", not "didn't play", and the two
# are indistinguishable from the minutes alone. Substituting him now hands
# his shirt to whoever happened to play first, and the swap unwinds itself on
# Monday night — which is what a manager saw happen to their team.
mins = {x["id"]: 90 for x in SQUAD}
mins[20] = 0                       # Monday fixture, not yet kicked off
final, subs = apply_autosubs(XI_442, BENCH, mins, settled=False)
check("nothing is substituted while the round is still being played", subs, [])
check("and the eleven stands exactly as it was picked",
      ids(final), ids(XI_442))

final, subs = apply_autosubs(XI_442, BENCH, mins, settled=True)
check("the same eleven and the same minutes settle normally", len(subs), 1)
check("taking off the player who really didn't play", subs[0][0]["id"], 20)

check_true("settling is the default, so old callers are unchanged",
           apply_autosubs(XI_442, BENCH, mins)[1] == subs)

# What the flag is read from: FPL says a round is over two different ways.
check("a round nobody has played is not over", round_is_over({}), False)
check("the last whistle is enough", round_is_over({"finished": True}), True)
check("and so is the bonus check that follows it",
      round_is_over({"data_checked": True}), True)
check("a round in progress is not over",
      round_is_over({"finished": False, "data_checked": False}), False)

print("\n── Rollover ────────────────────────────────────────────")

LINEUPS = {1: {"A": {"xi": ids(XI_442), "bench": [2, 14, 24, 32]}}}

xi, bench, source = effective_lineup("A", 1, LINEUPS, SQUAD)
check("gameweek 1 uses its own submission", source, "submitted")
check("and the named XI", sorted(ids(xi)), sorted(ids(XI_442)))

xi, bench, source = effective_lineup("A", 2, LINEUPS, SQUAD)
check_true("a missing submission rolls last week's over",
           source.startswith("rolled over from GW1"), source)
check("with the same eleven", sorted(ids(xi)), sorted(ids(XI_442)))

xi, bench, source = effective_lineup("B", 1, LINEUPS, SQUAD)
check("a team that has never submitted gets nothing", source, "none")

# Someone in the rolled-over XI has since been traded away.
TRADED = [x for x in SQUAD if x["id"] != 20] + [p(40, "MID", "NewMid")]
xi, bench, source = effective_lineup("A", 2, LINEUPS, TRADED)
check("a short XI is topped back up to eleven", len(xi), 11)
check_true("and stays legal", legal_formation(xi) is None, str(legal_formation(xi)))
check_true("the replacement is noted", "filled from the bench" in source, source)
check_true("the traded player is gone", 20 not in ids(xi))

print("\n── Suggested lineup (no hindsight) ─────────────────────")

form = {x["id"]: 0 for x in SQUAD}
for i, x in enumerate(SQUAD):
    form[x["id"]] = i  # later in the squad = better form
xi, bench = suggest_lineup(SQUAD, form)
check("a suggestion is eleven players", len(xi), 11)
check("with four on the bench", len(bench), 4)
check_true("and a legal formation", legal_formation(xi) is None, str(legal_formation(xi)))
check_true("no one is in both", not (set(ids(xi)) & set(ids(bench))))
check_true("the best forward starts", 32 in ids(xi))
check_true("the reserve keeper is last on the bench", bench[-1]["position"] == "GK")

# Form that would tempt an illegal XI: the five best are all forwards.
skew = {x["id"]: (100 if x["position"] == "FWD" else 1) for x in SQUAD}
xi, bench = suggest_lineup(SQUAD, skew)
check_true("form can't push the XI past three forwards",
           legal_formation(xi) is None, str(legal_formation(xi)))

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
    sys.exit(1)
print("ALL LINEUP TESTS PASSED")
