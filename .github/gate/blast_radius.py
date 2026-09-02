#!/usr/bin/env python3
"""Whether a change is small enough to merge without a person looking.

The agent that writes a fix must not be the thing that decides the fix is
safe. So this is not a judgement, it is a list: a diff either touches only
paths on the allow-list and obeys four hard rules, or it waits for a human.
No model runs here, and nothing in it can be argued with.

Why an allow-list rather than a list of forbidden things: a deny-list is a
promise that you thought of everything, and the cost of being wrong is a
change to how the league scores merging itself at four in the morning. The
allow-list starts narrow on purpose. Widen it as the thing earns trust —
that is one edit, in one obvious place, and it is the only knob here.

    python3 .github/gate/blast_radius.py <base-ref> <head-ref>

Exit 0 means it may merge itself. Exit 1 means it waits, and says why.
"""
from __future__ import annotations

import subprocess
import sys

# Paths a fix may touch on its own. Everything here is the app's surface or
# the support machinery — the places a "the button is greyed out" or "this
# column is off screen" bug actually lives.
ALLOWED = (
    "season-app/app/templates/",
    "season-app/app/static/",
    "season-app/app/triage.py",
    "season-app/app/evidence.py",
    "season-app/app/notify.py",
    "season-app/test_app.py",
    "season-app/README.md",
)

# Named separately from "not on the allow-list" so the refusal can say which
# rule was hit. These are the paths where being wrong is expensive rather
# than annoying, and each one is a different kind of expensive.
PROTECTED = {
    "shadow/": "the rules engine decides what a point is — the league's rulebook",
    ".github/": "the agent's own instructions, workflows and this gate",
    "season-app/app/auth.py": "who is who, and who is an admin",
    "season-app/app/db.py": "the schema, sessions and sign-in links",
    "season-app/app/engine.py": "assembles every score the table shows",
    "season-app/app/main.py": "the routes, including the agent's own door",
    "season-app/app/push.py": "holds the signing key notifications depend on",
    "season-app/app/fetcher.py": "what data the app trusts, and from where",
    "season-app/app/live.py": "reads the live FPL feed",
    "season-app/requirements.txt": "what code gets installed at deploy time",
    "season-app/Dockerfile": "how the app is built",
    "railway.json": "how the app is deployed",
}

# A fix that runs to hundreds of lines is not a fix, it is a rewrite, and a
# rewrite is worth reading. Counted as added plus removed across the diff.
LINE_LIMIT = 120


def _git(*args):
    return subprocess.run(("git", *args), capture_output=True, text=True,
                          check=True).stdout


def _protected(path):
    for prefix, why in PROTECTED.items():
        if path == prefix or path.startswith(prefix):
            return prefix, why
    return None, None


def verdict(base, head):
    """(ok, reasons) — reasons are why it waits, and are empty when it may go."""
    names = _git("diff", "--name-status", f"{base}...{head}").splitlines()
    stats = _git("diff", "--numstat", f"{base}...{head}").splitlines()
    reasons = []

    if not names:
        return False, ["nothing changed, so there is nothing to merge"]

    # 1. Deletions and renames are never a small fix. Deleting a file is how a
    #    failing test stops failing, which is the one repair nobody wants.
    for line in names:
        parts = line.split("\t")
        status = parts[0]
        if status.startswith("D"):
            reasons.append(f"deletes {parts[1]} — a deletion is never a small fix")
        elif status.startswith("R"):
            reasons.append(f"renames {parts[1]} → {parts[-1]}, which needs reading")

    changed = [p.split("\t")[-1] for p in names]

    # 2. Protected first, so the refusal names the real reason rather than
    #    "not on the allow-list", which tells nobody anything.
    for path in changed:
        prefix, why = _protected(path)
        if prefix:
            reasons.append(f"touches {path} — {why}")

    # 3. Then the allow-list.
    for path in changed:
        if _protected(path)[0]:
            continue
        if not any(path == a or path.startswith(a) for a in ALLOWED):
            reasons.append(f"touches {path}, which is not on the allow-list")

    # 4. Tests may grow. They may not shrink: an assertion that stops being
    #    made is indistinguishable from a bug that stopped being caught.
    total = 0
    for line in stats:
        added, removed, path = (line.split("\t") + ["", "", ""])[:3]
        if added == "-" or removed == "-":       # binary
            reasons.append(f"changes the binary file {path}")
            continue
        added, removed = int(added), int(removed)
        total += added + removed
        if "test" in path.rsplit("/", 1)[-1] and removed > added:
            reasons.append(
                f"removes more from {path} than it adds "
                f"(-{removed}/+{added}) — tests may grow, not shrink")

    # 5. Size.
    if total > LINE_LIMIT:
        reasons.append(f"changes {total} lines, over the {LINE_LIMIT} that "
                       "counts as small")

    # Deduplicated, because one path can trip two rules and saying it twice
    # reads like two problems.
    seen, unique = set(), []
    for r in reasons:
        if r not in seen:
            seen.add(r)
            unique.append(r)
    return not unique, unique


def main(argv):
    if len(argv) != 3:
        print(__doc__.strip())
        return 2
    ok, reasons = verdict(argv[1], argv[2])
    if ok:
        print("MERGE — within the blast radius")
        return 0
    print("WAIT — this one needs a person:")
    for r in reasons:
        print(f"  · {r}")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
