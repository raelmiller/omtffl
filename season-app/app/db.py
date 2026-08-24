"""Storage for the things people declare.

The engine computes everything else. This holds only what a manager typed —
who they are and which eleven they picked — because squads, scores, banks and
the table are all recomputed from declarations on demand. Nothing derived is
stored, so nothing derived can drift away from the rules.

SQLite, single file, on a Railway volume. A season is a few thousand rows.
"""
from __future__ import annotations

import json
import os
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .engine import DATA

# The container's filesystem is wiped on every redeploy, so the database must
# live on a mounted volume. Falling back to a local file keeps development
# working without one; the health page reports which is in use.
DB_PATH = Path(os.environ.get("DB_PATH")
               or Path(__file__).resolve().parents[1] / "matchweek.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS manager (
    key         TEXT PRIMARY KEY,        -- initials, matching the squad file
    team        TEXT NOT NULL,
    token       TEXT NOT NULL UNIQUE,    -- their personal sign-in link
    is_admin    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS lineup (
    manager     TEXT NOT NULL REFERENCES manager(key),
    gameweek    INTEGER NOT NULL,
    xi          TEXT NOT NULL,           -- JSON array of player ids
    bench       TEXT NOT NULL,           -- JSON array, in substitution order
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (manager, gameweek)
);
"""


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # A season app is overwhelmingly reads; WAL keeps the table page fast
    # while somebody is saving a lineup.
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init():
    """Create the schema, and seed managers from the squad file on first run.

    The squad file is the roster of record — it comes out of the auction — so
    the app never invents a manager. Anyone already present keeps their token,
    which means re-running this is safe and never breaks a bookmarked link.
    """
    with connect() as conn:
        conn.executescript(SCHEMA)

        squads_file = DATA / "squads.json"
        if not squads_file.exists():
            return
        squads = json.loads(squads_file.read_text())
        existing = {r["key"] for r in conn.execute("SELECT key FROM manager")}
        for team in squads["teams"]:
            if team["key"] in existing:
                continue
            conn.execute(
                "INSERT INTO manager (key, team, token, is_admin, created_at)"
                " VALUES (?, ?, ?, 0, ?)",
                (team["key"], team.get("team", team["key"]),
                 secrets.token_hex(16), now()))


def managers():
    with connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM manager ORDER BY team")]


def manager_by_token(token):
    if not token:
        return None
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM manager WHERE token = ?", (token,)).fetchone()
        return dict(row) if row else None


def manager_by_key(key):
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM manager WHERE key = ?", (key,)).fetchone()
        return dict(row) if row else None


def rotate_token(key):
    """Issue a new link, invalidating the old one."""
    token = secrets.token_hex(16)
    with connect() as conn:
        conn.execute("UPDATE manager SET token = ? WHERE key = ?", (token, key))
    return token


def set_admin(key, is_admin=True):
    with connect() as conn:
        conn.execute("UPDATE manager SET is_admin = ? WHERE key = ?",
                     (1 if is_admin else 0, key))


def save_lineup(manager, gameweek, xi, bench):
    with connect() as conn:
        conn.execute(
            "INSERT INTO lineup (manager, gameweek, xi, bench, updated_at)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(manager, gameweek) DO UPDATE SET"
            "   xi = excluded.xi, bench = excluded.bench,"
            "   updated_at = excluded.updated_at",
            (manager, gameweek, json.dumps(list(xi)), json.dumps(list(bench)),
             now()))


def get_lineup(manager, gameweek):
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM lineup WHERE manager = ? AND gameweek = ?",
            (manager, gameweek)).fetchone()
    if not row:
        return None
    return {"xi": json.loads(row["xi"]), "bench": json.loads(row["bench"]),
            "updated_at": row["updated_at"]}


def all_lineups():
    """Every submission, shaped exactly as the engine already expects.

    `{gameweek: {manager: {"xi": [...], "bench": [...], "submitted_at": ...}}}`
    — the same structure lineups.json holds, so the engine needs no changes to
    read from a database instead of a file.
    """
    out = {}
    with connect() as conn:
        for row in conn.execute("SELECT * FROM lineup"):
            out.setdefault(row["gameweek"], {})[row["manager"]] = {
                "xi": json.loads(row["xi"]),
                "bench": json.loads(row["bench"]),
                "submitted_at": row["updated_at"],
            }
    return out


def stats():
    with connect() as conn:
        rows = conn.execute("SELECT COUNT(*) c FROM manager").fetchone()["c"]
        lineups = conn.execute("SELECT COUNT(*) c FROM lineup").fetchone()["c"]
    return {
        "path": str(DB_PATH),
        "on_volume": bool(os.environ.get("DB_PATH")),
        "managers": rows,
        "lineups": lineups,
    }
