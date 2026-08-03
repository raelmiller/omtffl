#!/usr/bin/env python3
"""Build the slim players feed the OMTFFL draft auction app consumes.

Background
----------
The auction app (the Railway app at omtffl.up.railway.app) used to pull the
full FPL ``bootstrap-static`` feed live at boot. That is unreliable in the
off-season, when the official feed lags the new season's squads/prices — the
reason a hand-downloaded static file kept creeping in. This script produces a
small, stable ``players.json`` (a faithful *subset* of bootstrap-static: just
``teams`` + ``elements``) that the app fetches from one fixed URL, keeping the
live API only as a fallback. Refreshed daily by a GitHub Action.

Accepted sources (auto-detected by shape)
-----------------------------------------
* FPL bootstrap-static   -> ``{"teams": [...], "elements": [...]}``
* advisor ``fpl_data.json`` -> ``{"teams": [...], "players": [...]}``

Usage
-----
    # from the live FPL API (GitHub Actions has egress; local sandboxes may not)
    python build_players_feed.py

    # from a committed snapshot (off-season override / offline seeding)
    python build_players_feed.py path/to/bootstrap_or_fpl_data.json

Env
---
    FPL_BOOTSTRAP_URL   override the API url (default: the official endpoint)

Safety
------
If the source yields fewer than ``MIN_PLAYERS`` and a ``players.json`` already
exists, the existing file is kept — we never overwrite good data with an empty
off-season feed. That path exits 3 so a workflow can tell "kept existing" from
a real update.
"""
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

OUT = Path(__file__).resolve().parent / "players.json"
DEFAULT_URL = "https://fantasy.premierleague.com/api/bootstrap-static/"
HEADERS = {"User-Agent": "Mozilla/5.0 (omtffl-players-feed builder)"}

# A real Premier League season carries ~600+ players. Anything well below this
# means the source is empty/transitional (typical mid-off-season) — don't ship it.
MIN_PLAYERS = 300

# The only element fields the auction app needs (it derives position + team
# name itself). Kept deliberately minimal so the feed stays small and stable.
ELEMENT_FIELDS = ("id", "web_name", "first_name", "second_name", "element_type", "team")
TEAM_FIELDS = ("id", "name", "short_name")


def fetch_live(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def load_source():
    """Return (raw_dict, source_label) from a file arg or the live API."""
    if len(sys.argv) > 1:
        path = Path(sys.argv[1])
        return json.loads(path.read_text()), f"file: {path.name}"
    url = os.environ.get("FPL_BOOTSTRAP_URL", DEFAULT_URL)
    return fetch_live(url), "live FPL bootstrap-static"


def extract(raw):
    """Normalise either accepted shape into slim teams + elements lists."""
    teams = [{k: t.get(k) for k in TEAM_FIELDS} for t in raw.get("teams", [])]
    # bootstrap-static -> "elements"; advisor fpl_data.json -> "players"
    src_players = raw.get("elements")
    if src_players is None:
        src_players = raw.get("players", [])
    elements = [{k: p.get(k) for k in ELEMENT_FIELDS} for p in src_players]
    return teams, elements


def main():
    raw, source = load_source()
    teams, elements = extract(raw)

    if len(elements) < MIN_PLAYERS:
        if OUT.exists():
            print(
                f"Source yielded only {len(elements)} players (< {MIN_PLAYERS}); "
                f"keeping existing {OUT.name}.",
                file=sys.stderr,
            )
            sys.exit(3)
        print(
            f"Source yielded only {len(elements)} players (< {MIN_PLAYERS}) and no "
            f"existing feed to fall back on — refusing to write a thin feed.",
            file=sys.stderr,
        )
        sys.exit(2)

    out = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": source,
        "team_count": len(teams),
        "player_count": len(elements),
        "teams": teams,
        "elements": elements,
    }
    OUT.write_text(json.dumps(out, separators=(",", ":")))
    size_kb = OUT.stat().st_size / 1024
    print(
        f"Wrote {OUT} ({size_kb:.0f} KB) — {len(elements)} players, "
        f"{len(teams)} teams, from {source}."
    )


if __name__ == "__main__":
    main()
