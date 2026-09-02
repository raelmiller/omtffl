#!/usr/bin/env python3
"""Post what the triage agent decided, or print it and post nothing.

Kept out of the model's hands on purpose. A model asked to make its own HTTP
calls needs the secrets in its environment and needs to be trusted to honour
a dry-run flag; a model asked to write a file needs neither. So the decision
is the model's and the request is this script's, which makes DRY_RUN a fact
rather than a promise and means a malformed decision is caught here rather
than delivered to somebody.

Reads decisions.json beside briefs.json. Environment: APP_URL, AGENT_TOKEN,
and DRY_RUN.

Each decision carries two texts, and keeping them apart is the point: `text`
goes to the manager and `note` goes to the commissioner. They were one field
once, and the first held report showed its reporter the commissioner's note.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

LANES = {"answer", "diagnose", "adjudicate", "escalate"}
ACTIONS = {"reply", "hold"}


def load(path):
    try:
        with open(path) as handle:
            return json.load(handle)
    except (OSError, ValueError) as exc:
        sys.exit(f"could not read {path}: {exc}")


def post(url, token, body):
    request = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.status


def main():
    app_url = (os.environ.get("APP_URL") or "").rstrip("/")
    token = os.environ.get("AGENT_TOKEN") or ""
    dry = (os.environ.get("DRY_RUN") or "").lower() == "true"
    if not app_url or not token:
        sys.exit("APP_URL and AGENT_TOKEN must be set")

    waiting = {b["report_id"] for b in load("briefs.json")["reports"]}
    decisions = load("decisions.json").get("decisions") or []

    problems, ready = [], []
    for d in decisions:
        rid, action, lane = d.get("report_id"), d.get("action"), d.get("lane")
        text = (d.get("text") or "").strip()
        note = (d.get("note") or "").strip()
        # Every one of these is a way a decision could reach a manager as
        # something nobody meant to send, so none of them are warnings.
        if rid not in waiting:
            problems.append(f"report {rid} was not in this batch")
        elif action not in ACTIONS:
            problems.append(f"report {rid}: action {action!r} is not one of "
                            f"{sorted(ACTIONS)}")
        elif lane not in LANES:
            problems.append(f"report {rid}: lane {lane!r} is not one of "
                            f"{sorted(LANES)}")
        # A reply is the manager's only text, so it has to exist. A hold has
        # the app's own wording to fall back on, and a held report with no
        # note is one the commissioner opens knowing nothing.
        elif action == "reply" and not text:
            problems.append(f"report {rid}: nothing to say")
        elif action == "hold" and not note:
            problems.append(f"report {rid}: held with no note for the "
                            "commissioner")
        else:
            ready.append((rid, action, lane, text, note))

    missed = waiting - {r for r, _, _, _, _ in ready}
    for rid in sorted(missed):
        problems.append(f"report {rid} was waiting and got no decision")

    for rid, action, lane, text, note in ready:
        verb = "REPLY" if action == "reply" else "HOLD "
        print(f"\n{verb} #{rid}  [{lane}]")
        print(f"  to them: {text or '(the app writes its own line)'}")
        if note:
            print(f"  to you : {note}")

    if dry:
        print(f"\nDRY RUN — {len(ready)} decision(s) above, nothing posted.")
    else:
        for rid, action, lane, text, note in ready:
            body = {"lane": lane, "reply": text}
            if action == "hold":
                body["summary"] = note
            try:
                status = post(f"{app_url}/agent/reports/{rid}/{action}",
                              token, body)
                print(f"posted #{rid} -> {status}")
            except (urllib.error.URLError, OSError) as exc:
                problems.append(f"report {rid}: could not post — {exc}")

    if problems:
        print("\nProblems:", file=sys.stderr)
        for p in problems:
            print(f"  · {p}", file=sys.stderr)
        sys.exit(1)
    print(f"\n{len(ready)} report(s) handled.")


if __name__ == "__main__":
    main()
