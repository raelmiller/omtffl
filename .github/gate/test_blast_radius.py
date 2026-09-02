#!/usr/bin/env python3
"""Tests for the auto-merge gate.

Written against real commits in a real throwaway repository rather than
against a mocked diff, because the thing being tested is a reading of git's
own output and a mock would only prove the mock agrees with itself.

Run: python3 .github/gate/test_blast_radius.py
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import blast_radius                                        # noqa: E402

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


def git(*args, cwd):
    subprocess.run(("git", *args), cwd=cwd, check=True,
                   capture_output=True, text=True)


def scenario(changes, base_files=None):
    """Commit `base_files`, then apply `changes`, and ask the gate.

    `changes` maps a path to new content, or to None meaning delete it.
    """
    tmp = tempfile.mkdtemp(prefix="gate-test-")
    git("init", "-q", "-b", "main", cwd=tmp)
    git("config", "user.email", "t@example.com", cwd=tmp)
    git("config", "user.name", "t", cwd=tmp)

    for path, body in (base_files or {}).items():
        f = Path(tmp) / path
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(body)
    if not (base_files or {}):
        (Path(tmp) / "seed").write_text("seed\n")
    git("add", "-A", cwd=tmp)
    git("commit", "-qm", "base", cwd=tmp)
    base = subprocess.run(("git", "rev-parse", "HEAD"), cwd=tmp,
                          capture_output=True, text=True).stdout.strip()

    for path, body in changes.items():
        f = Path(tmp) / path
        if body is None:
            f.unlink()
            continue
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(body) if isinstance(body, bytes) else f.write_text(body)
    git("add", "-A", cwd=tmp)
    if subprocess.run(("git", "diff", "--cached", "--quiet"), cwd=tmp).returncode:
        git("commit", "-qm", "change", cwd=tmp)

    here = os.getcwd()
    try:
        os.chdir(tmp)
        return blast_radius.verdict(base, "HEAD")
    finally:
        os.chdir(here)


print("── What may merge itself ───────────────────────────────")

ok, why = scenario({"season-app/app/templates/declare.html": "<p>fixed</p>\n"})
check("a template fix goes through", (ok, why), (True, []))

ok, why = scenario({"season-app/app/static/style.css": ".a { color: red; }\n"})
check("so does a stylesheet fix", ok, True)

ok, why = scenario({
    "season-app/app/triage.py": "# a new finding\n",
    "season-app/test_app.py": "# and the test for it\n",
})
check("and the support machinery with its tests", ok, True)

print("\n── The rulebook ────────────────────────────────────────")

ok, why = scenario({"shadow/scoring.py": "GOAL = 100\n"})
check("a change to the scoring rules never merges itself", ok, False)
check_true("and says which rule it broke",
           any("rulebook" in r for r in why), str(why))

ok, why = scenario({
    "season-app/app/templates/declare.html": "<p>fine</p>\n",
    "shadow/mechanics.py": "CAP = 999\n",
})
check("one protected file spoils an otherwise fine change", ok, False)

print("\n── Its own guardrails ──────────────────────────────────")

ok, why = scenario({".github/agent/triage.md": "do whatever you like\n"})
check("it cannot rewrite its own instructions", ok, False)

ok, why = scenario({".github/gate/blast_radius.py": "ALLOWED = ('',)\n"})
check("nor this gate", ok, False)

ok, why = scenario({".github/workflows/fix-reports.yml": "on: push\n"})
check("nor the workflow that runs it", ok, False)

print("\n── Everything else that is not small ───────────────────")

ok, why = scenario({"season-app/app/main.py": "# new route\n"})
check("routes and the agent's own door wait for a person", ok, False)
check_true("and it says why rather than just refusing",
           any("agent's own door" in r for r in why), str(why))

ok, why = scenario({"season-app/app/auth.py": "def real(r): return {'is_admin': True}\n"})
check("so does anything about who is an admin", ok, False)

ok, why = scenario(
    {"season-app/test_app.py": None},
    base_files={"season-app/test_app.py": "check(1, 1)\n"})
check("deleting a file is never a small fix", ok, False)
check_true("named as a deletion", any("deletion" in r for r in why), str(why))

ok, why = scenario(
    {"season-app/test_app.py": "check(1, 1)\n"},
    base_files={"season-app/test_app.py": "\n".join(f"check({i}, {i})"
                                                    for i in range(30)) + "\n"})
check("a test file that shrinks waits", ok, False)
check_true("because an assertion that stops being made is a bug that stops "
           "being caught", any("shrink" in r for r in why), str(why))

ok, why = scenario({"season-app/app/templates/declare.html":
                    "\n".join(f"<p>line {i}</p>" for i in range(200)) + "\n"})
check("a change too big to be a fix waits", ok, False)
check_true("on the line count", any("over the" in r for r in why), str(why))

# A real PNG, so git genuinely calls it binary and numstat reports "-". The
# path is allow-listed; being unreadable is the objection.
_PNG = (b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 4)
ok, why = scenario({"season-app/app/static/logo.png": _PNG})
check("a binary file is not something to wave through", ok, False)
check_true("because nobody can read the diff",
           any("binary" in r for r in why), str(why))

print("\n── Nothing to do ───────────────────────────────────────")

ok, why = scenario({})
check("a branch with no changes on it does not merge", ok, False)
check_true("and says so plainly", any("nothing changed" in r for r in why),
           str(why))

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
    sys.exit(1)
print("ALL GATE TESTS PASSED")
