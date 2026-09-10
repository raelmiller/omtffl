"""Turning a draft export into the league's roster of record.

`shadow/import_squads.py` has done this from the command line since the first
season: read the auction's export, resolve every drafted player to their FPL
element id, and write squads.json. This is the same job with the parts that
made it a developer's errand removed — no spreadsheet library, no file on
somebody's laptop, no commit and redeploy before anyone can see it.

Two things are kept from that script because both were learned the hard way.
A player who cannot be resolved is reported rather than dropped: a silently
missing player is a team that mysteriously scores less all season, found in
about November. And duplicate surnames are real, so a name that matches more
than one player is narrowed on the position and club the draft recorded.

Nothing here touches the database or the web. It reads text and returns what
it found, so the admin page can show it before anything is written.
"""
from __future__ import annotations

import csv
import io
import re
from collections import defaultdict

# FPL's element_type, which is what players.json stores.
POS = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}
SQUAD_SIZE = 15
SHAPE = {"GK": 2, "DEF": 5, "MID": 5, "FWD": 3}

# What each column might be called. The export is a spreadsheet somebody may
# have re-saved, so the header is matched on meaning rather than on an exact
# string — and the order is not assumed at all.
COLUMNS = {
    "name": ("name", "player", "playername", "player name", "web_name"),
    "position": ("position", "pos", "element_type"),
    "club": ("club", "team", "side"),
    "owner": ("owner", "manager", "buyer", "drafted by", "draftedby", "won by"),
    "price": ("price", "cost", "paid", "value", "amount", "bid"),
}


def _norm(s):
    return re.sub(r"[^a-z0-9]+", "", str(s or "").strip().lower())


def _dialect(text):
    """Comma or tab, decided by the header rather than guessed at.

    Pasting from a spreadsheet gives tabs; a saved CSV gives commas; a CSV
    written by a European locale can give semicolons. Sniffing the first line
    covers all three without asking anybody which they have.
    """
    first = text.splitlines()[0] if text.splitlines() else ""
    counts = {d: first.count(d) for d in (",", "\t", ";")}
    return max(counts, key=counts.get) if any(counts.values()) else ","


def parse(text):
    """Rows of {name, position, club, owner, price} from pasted or uploaded text.

    Returns (rows, problems). A problem is a sentence for a person, not a
    stack trace — this runs on something somebody exported five minutes ago
    and the useful answer is which column is missing.
    """
    text = (text or "").strip()
    if not text:
        return [], ["Nothing to read — paste the rows or choose a file."]

    reader = csv.reader(io.StringIO(text), delimiter=_dialect(text))
    try:
        raw = [r for r in reader if any(str(c).strip() for c in r)]
    except csv.Error as exc:
        return [], [f"That could not be read as a table: {exc}"]
    if len(raw) < 2:
        return [], ["That has a header but no rows, or no header at all."]

    header = [_norm(c) for c in raw[0]]
    index = {}
    for field, names in COLUMNS.items():
        for i, cell in enumerate(header):
            if cell in {_norm(n) for n in names}:
                index[field] = i
                break

    missing = [f for f in ("name", "owner") if f not in index]
    if missing:
        return [], [
            f"No {' or '.join(missing)} column. Found: "
            + ", ".join(c for c in raw[0] if str(c).strip())
            + ". The export needs a column for the player and one for who "
              "bought them."]

    rows, problems = [], []
    for n, line in enumerate(raw[1:], start=2):
        def cell(field):
            i = index.get(field)
            return str(line[i]).strip() if i is not None and i < len(line) else ""

        if not cell("name") or not cell("owner"):
            problems.append(f"Row {n} has no player or no owner — skipped.")
            continue
        price = cell("price").replace("£", "").replace(",", "")
        try:
            price = float(price) if price else 0.0
        except ValueError:
            problems.append(f"Row {n}: {cell('price')!r} is not a price — "
                            "counted as 0.")
            price = 0.0
        rows.append({"name": cell("name"), "position": cell("position").upper(),
                     "club": cell("club").upper(), "owner": cell("owner"),
                     "price": price})
    return rows, problems


def index_players(names, positions, player_clubs, clubs):
    """Everything needed to turn a drafted name into an FPL id.

    Built from the app's own players.json rather than a second copy of the
    feed, so the ids resolved here are the ids the scoring engine uses.
    """
    by_name = defaultdict(list)
    for pid, name in names.items():
        pid = int(pid)
        by_name[_norm(name)].append({
            "id": pid,
            "name": name,
            "position": POS.get(int(positions.get(str(pid), 0) or 0)),
            "club": (clubs.get(int(player_clubs.get(str(pid), 0) or 0))
                     or {}).get("short"),
        })
    return by_name


def resolve(row, by_name):
    """The FPL player a drafted row refers to, or None if it is ambiguous.

    Position first, then club. Positions are what the scoring engine cares
    about and almost never change; a club can change between the draft and
    whenever this is run, so it is the weaker signal and used second.
    """
    cands = by_name.get(_norm(row["name"]), [])
    if len(cands) == 1:
        return cands[0]
    if not cands:
        return None
    if row["position"]:
        narrowed = [c for c in cands if c["position"] == row["position"]]
        if len(narrowed) == 1:
            return narrowed[0]
        cands = narrowed or cands
    if row["club"]:
        narrowed = [c for c in cands if c["club"] == row["club"]]
        if len(narrowed) == 1:
            return narrowed[0]
    return None


def owners(rows):
    """Every owner the export names, in the order they first appear."""
    seen = []
    for r in rows:
        if r["owner"] not in seen:
            seen.append(r["owner"])
    return seen


def build(rows, by_name, identities):
    """A squads file, plus everything wrong with it.

    `identities` maps the export's owner names to {key, team}. The export
    records whoever ran the draft typed in, which is usually a first name,
    and a person's name is not something the rest of the app should carry —
    so the mapping happens here and nothing downstream ever sees it.
    """
    teams, unresolved = defaultdict(list), []
    for row in rows:
        who = identities.get(row["owner"])
        if not who:
            continue
        found = resolve(row, by_name)
        if found is None:
            unresolved.append({**row, "team": who["team"]})
            continue
        teams[who["key"]].append({
            "id": found["id"],
            "name": found["name"],
            "position": found["position"] or row["position"],
            "club": found["club"] or row["club"],
            "price": row["price"],
        })

    squads = {
        "source": "imported",
        "teams": [
            {"key": who["key"], "team": who["team"],
             "squad": sorted(teams[who["key"]], key=lambda p: p["position"])}
            for who in sorted(identities.values(), key=lambda w: w["key"])
            if who["key"] in teams
        ],
    }
    return squads, unresolved


def audit(squads):
    """What is wrong with each squad, in the words the admin page shows.

    Warnings rather than refusals. A draft that ended a player short is a
    real thing that happens, and the person importing it knows whether it is
    expected far better than this does.
    """
    out = []
    seen = {}
    for team in squads["teams"]:
        counts = defaultdict(int)
        notes = []
        for p in team["squad"]:
            counts[p["position"]] += 1
            if p["id"] in seen and seen[p["id"]] != team["key"]:
                notes.append(f"{p['name']} is also in {seen[p['id']]}'s squad")
            seen[p["id"]] = team["key"]
        if len(team["squad"]) != SQUAD_SIZE:
            notes.append(f"{len(team['squad'])} players, not {SQUAD_SIZE}")
        for pos, want in SHAPE.items():
            if counts[pos] != want:
                notes.append(f"{counts[pos]} {pos}, not {want}")
        out.append({"key": team["key"], "team": team["team"],
                    "count": len(team["squad"]),
                    "shape": " ".join(f"{p}{counts[p]}"
                                      for p in ("GK", "DEF", "MID", "FWD")),
                    "notes": notes})
    return out
