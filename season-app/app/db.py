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

def _db_path() -> Path:
    """Where the database file belongs.

    The container's filesystem is wiped on every redeploy, so this must land
    on a mounted volume. Three ways of finding one, in order of how explicit
    they are:

    1. DB_PATH, if someone set it deliberately.
    2. RAILWAY_VOLUME_MOUNT_PATH, which Railway sets by itself the moment a
       volume is attached. Attaching the volume is then the only step — there
       is no second variable to forget, and no way to typo it.
    3. A local file, so development works with no volume at all.
    """
    explicit = os.environ.get("DB_PATH")
    if explicit:
        return Path(explicit)
    mounted = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
    if mounted:
        return Path(mounted) / "matchweek.db"
    return Path(__file__).resolve().parents[1] / "matchweek.db"


DB_PATH = _db_path()

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


_schema_ready = False


@contextmanager
def connect():
    """A connection, with the schema guaranteed to exist.

    Creating the tables here rather than only in init() means the app can
    never serve a request against a database that has not been set up. That
    was not theoretical: a fresh volume plus any code path that reaches the
    database before startup has run produces "no such table", and on a first
    deploy those are the same moment.
    """
    global _schema_ready
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # A season app is overwhelmingly reads; WAL keeps the table page fast
    # while somebody is saving a lineup.
    conn.execute("PRAGMA journal_mode = WAL")
    if not _schema_ready:
        conn.executescript(SCHEMA)
        _schema_ready = True
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


def is_mount(path: Path) -> bool:
    """Whether this path sits on a mounted filesystem of its own.

    Reads the kernel's own mount table rather than trusting an environment
    variable. DB_PATH being set proves somebody intended a volume; this proves
    one is actually there, which is the difference between a league that
    survives a redeploy and one that doesn't.
    """
    try:
        mounts = Path("/proc/mounts").read_text().splitlines()
    except OSError:
        return False
    points = {line.split()[1] for line in mounts if len(line.split()) > 1}
    for parent in [path, *path.parents]:
        if str(parent) in points and str(parent) != "/":
            return True
    return False


def storage():
    """Where the database lives and whether it will survive a redeploy.

    This is the check worth having on the health page: a container's own
    filesystem is wiped on every deploy, so a database sitting on it quietly
    loses every team the league has picked.
    """
    directory = DB_PATH.parent
    explicit = os.environ.get("DB_PATH")
    railway_volume = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
    mounted = is_mount(DB_PATH)
    writable = os.access(directory, os.W_OK) if directory.exists() else False

    if mounted and writable:
        source = "DB_PATH" if explicit else "the attached Railway volume"
        verdict = f"safe — the database is on a mounted volume, found via {source}"
    elif not mounted and not explicit and not railway_volume:
        verdict = ("AT RISK — no volume attached to this service, so the "
                   "database sits on the container's own disk and every team "
                   "is lost on the next deploy. Attach one in Railway; nothing "
                   "else needs configuring.")
    elif not mounted:
        where = explicit or railway_volume
        verdict = (f"AT RISK — a volume is configured at {where} but nothing "
                   "is mounted there. Check the mount path matches, and that "
                   "the service has redeployed since it was attached.")
    else:
        verdict = f"AT RISK — {directory} exists but is not writable"

    return {
        "path": str(DB_PATH),
        "directory_exists": directory.exists(),
        "writable": writable,
        "db_path_set": bool(explicit),
        "railway_volume": railway_volume,
        "on_mounted_volume": mounted,
        "survives_redeploy": mounted and writable,
        "verdict": verdict,
    }


def stats():
    with connect() as conn:
        rows = conn.execute("SELECT COUNT(*) c FROM manager").fetchone()["c"]
        lineups = conn.execute("SELECT COUNT(*) c FROM lineup").fetchone()["c"]
    return {"managers": rows, "lineups": lineups, "storage": storage()}
