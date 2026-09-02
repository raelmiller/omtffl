# Fixing a reported bug

You have a report a manager filed, which the answering job could not settle
from facts and judged to be a real defect. Your job is one small, correct fix
with a test that would have caught it — or, if it is not small or not clear,
no fix at all.

## Before you write anything

Reproduce it. The report's `context` says what the app's state was, and the
`findings` say what it noticed. Write a failing test first, from `season-app/test_app.py`'s
existing style, and watch it fail. **A fix you have not seen fail is a guess.**

If you cannot reproduce it, do not fix it. Post what you found and hand it over:

```
curl -X POST -H "Authorization: Bearer $AGENT_TOKEN" -H "Content-Type: application/json" \
  -d '{"lane":"escalate","summary":"could not reproduce: <what you tried>"}' \
  "$APP_URL/agent/reports/<id>/hold"
```

## What may merge itself

`.github/gate/blast_radius.py` decides, and it is a list, not a judgement. Run
it before you open anything:

```
python3 .github/gate/blast_radius.py origin/main HEAD
```

It allows changes to templates, the stylesheet, `triage.py`, `evidence.py`,
`notify.py`, `test_app.py` and the README, under 120 changed lines, with no
deletions and no test file that shrinks.

**Do not try to get around it.** If your fix needs `main.py`, `engine.py`,
`db.py`, `auth.py` or anything under `shadow/`, that is fine — write it
properly and open the pull request anyway. It will be labelled for a person
and it will wait. That is the system working. What is not acceptable is
reshaping a fix to squeeze under the gate, or touching the gate itself.

**Never modify `shadow/`.** It is the league's rulebook. If the correct fix is
a scoring change, write it, say so plainly in the pull request, and let it
wait. Never fix a rule to satisfy a complaint.

## Running the tests

All of them, before you open anything:

```
cd season-app && python3 test_app.py
cd .. && for t in mechanics scoring lineups; do python3 shadow/test_$t.py; done
python3 .github/gate/test_blast_radius.py
```

Engine suites run from the repository root — they take paths relative to it.

## Opening it

Branch as `agent/fix-<report id>`, and put **`Fixes report #<id>`** in the body
on its own line. The merge job reads that to tell the manager when it is live;
without it they hear nothing.

The body should say what was wrong, what you changed, and how you know — name
the test. If the gate says it will wait, say which rule and why the fix needs
it, so whoever reads it starts with the answer.

Then record it against the report:

```
curl -X POST -H "Authorization: Bearer $AGENT_TOKEN" -H "Content-Type: application/json" \
  -d '{"pr": <number>}' "$APP_URL/agent/reports/<id>/bug"
```

## One report at a time

Take the oldest. A pull request per report keeps the gate's judgement about
one thing, and keeps a revert cheap. If there are several, do one and let the
next run take the next.
