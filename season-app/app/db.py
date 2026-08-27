"""Storage for the things people declare.

The engine computes everything else. This holds only what a manager typed —
who they are and which eleven they picked — because squads, scores, banks and
the table are all recomputed from declarations on demand. Nothing derived is
stored, so nothing derived can drift away from the rules.

SQLite, single file, on a Railway volume. A season is a few thousand rows.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
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
    token       TEXT NOT NULL UNIQUE,    -- their next sign-in link, single use
    is_admin    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    token_expires TEXT                   -- when that link stops working
);

-- One row per signed-in browser. The cookie carries a secret this table only
-- ever sees the hash of, so a copy of the database is not a set of working
-- logins — which the old scheme, where the cookie was the row, could not say.
CREATE TABLE IF NOT EXISTS session (
    id          TEXT PRIMARY KEY,        -- sha256 of the cookie secret
    manager     TEXT NOT NULL REFERENCES manager(key) ON DELETE CASCADE,
    created_at  TEXT NOT NULL,
    last_seen   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS session_manager ON session(manager);

-- Which Premier League club's manager each team drafted. The boost is tied
-- to that club: its league position sets the size, its result decides the
-- payout, and a sacking ends the remaining uses.
CREATE TABLE IF NOT EXISTS manager_club (
    manager     TEXT PRIMARY KEY REFERENCES manager(key),
    club_id     INTEGER NOT NULL,
    sacked_from INTEGER,                 -- gameweek, or NULL while in the job
    assigned_at TEXT NOT NULL
);

-- Everything a manager declares that isn't a lineup: boosts now, bank spends
-- and waiver claims later. One row per manager per gameweek per kind, with
-- the payload shaped exactly as the rules engine already reads it.
CREATE TABLE IF NOT EXISTS declaration (
    manager     TEXT NOT NULL REFERENCES manager(key),
    gameweek    INTEGER NOT NULL,
    kind        TEXT NOT NULL,
    payload     TEXT NOT NULL DEFAULT '{}',
    declared_at TEXT NOT NULL,
    PRIMARY KEY (manager, gameweek, kind)
);

-- Free-agency moves, made one at a time in the window between the waiver run
-- and the gameweek deadline. A declaration row won't do: it is one per
-- manager per gameweek per kind, and a manager may make as many of these as
-- they like. `made_at` is not bookkeeping — first come, first served is the
-- whole rule, so the clock is what decides a race for the same player.
CREATE TABLE IF NOT EXISTS free_agent_move (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    gameweek    INTEGER NOT NULL,
    manager     TEXT NOT NULL REFERENCES manager(key),
    dropped     TEXT NOT NULL,           -- JSON: the player going out
    added       TEXT NOT NULL,           -- JSON: the player coming in
    made_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS fam_gameweek ON free_agent_move(gameweek);

-- Trades are the one thing here with two sides and a life of its own, so
-- they get a table rather than a declaration row: proposed, then accepted or
-- declined, and when points are attached, published for the league to object.
CREATE TABLE IF NOT EXISTS trade (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    gameweek    INTEGER NOT NULL,
    proposer    TEXT NOT NULL REFERENCES manager(key),
    receiver    TEXT NOT NULL REFERENCES manager(key),
    players_out TEXT NOT NULL,           -- JSON: what the proposer gives up
    players_in  TEXT NOT NULL,           -- JSON: what they get back
    points      INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL,           -- proposed|accepted|published|declined|withdrawn
    note        TEXT,
    created_at  TEXT NOT NULL,
    resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS trade_veto (
    trade_id    INTEGER NOT NULL REFERENCES trade(id),
    manager     TEXT NOT NULL REFERENCES manager(key),
    created_at  TEXT NOT NULL,
    PRIMARY KEY (trade_id, manager)
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
    """A connection, with the schema guaranteed to exist.

    Creating the tables here rather than only in init() means the app can
    never serve a request against a database that has not been set up. That
    was not theoretical: a fresh volume plus any code path that reaches the
    database before startup has run produces "no such table", and on a first
    deploy those are the same moment.

    Done on every connection rather than once per process. A flag would be
    faster and would also lie the moment the file underneath it changed — a
    deleted database left a running process convinced the tables were still
    there. CREATE TABLE IF NOT EXISTS against an existing schema is cheap
    enough that the flag was never worth the failure mode.
    """
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # A season app is overwhelmingly reads; WAL keeps the table page fast
    # while somebody is saving a lineup.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    _migrate(conn)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _migrate(conn):
    """Columns added after a database already existed.

    CREATE TABLE IF NOT EXISTS is a no-op against a table that is already
    there, so a column added later has to be added by hand. Cheap, idempotent
    and in the same place as the schema it patches.
    """
    have = {r["name"] for r in conn.execute("PRAGMA table_info(manager)")}
    if "token_expires" not in have:
        conn.execute("ALTER TABLE manager ADD COLUMN token_expires TEXT")


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
        # Seeded links expire like any other. The admin's own is reissued at
        # startup if it has, so the deploy log is always a way back in.
        conn.execute(
            "UPDATE manager SET token_expires = ? WHERE token_expires IS NULL",
            ((datetime.now(timezone.utc) + timedelta(days=LINK_DAYS))
             .isoformat(timespec="seconds"),))


def managers():
    with connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM manager ORDER BY team")]


def manager_by_token(token):
    """The manager a sign-in link belongs to, if it is still good for one use.

    An expired link is treated exactly like a wrong one. The caller can't tell
    the difference and shouldn't be able to.
    """
    if not token:
        return None
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM manager WHERE token = ?", (token,)).fetchone()
    if not row:
        return None
    expires = row["token_expires"]
    if expires and expires < now():
        return None
    return dict(row)


def spend_token(token):
    """Sign in with a link, and start its clock running.

    A link that stays valid is a password that never changes, sitting in
    whatever chat it was pasted into. But killing it outright on first use
    breaks the way links are actually opened: tapped in a messaging app, they
    open in that app's own browser, and the manager who then opens the app
    properly in Safari finds a link that has already been spent.

    So first use doesn't spend the link, it shortens it — to an hour, or to
    whatever it had left if that was less. Long enough to open it again on the
    browser you actually use; short enough that a leaked link is exposed for
    an hour after its owner opens it rather than for the season.

    Later uses inside that hour don't extend it. The window is measured from
    the first time anyone used the link, not the last.

    Returns (manager, session_secret), or (None, None) if the link has run
    out or was never real.
    """
    manager = manager_by_token(token)
    if not manager:
        return None, None
    grace = (datetime.now(timezone.utc) + timedelta(minutes=LINK_GRACE_MINUTES)
             ).isoformat(timespec="seconds")
    if manager["token_expires"] is None or manager["token_expires"] > grace:
        with connect() as conn:
            conn.execute(
                "UPDATE manager SET token_expires = ? WHERE key = ?",
                (grace, manager["key"]))
    return manager, start_session(manager["key"])


# Long enough for anything anyone has actually called a team — the longest in
# the league is twenty characters — and short enough not to break the places a
# name has to fit: a dropdown that sizes itself to its widest option, a column
# in the standings, a heading on a phone.
TEAM_NAME_MAX = 40


def rename_team(key, name):
    """Change a team's name, or say why not.

    Returns (name, None) on success and (None, reason) on refusal. The only
    rules are the ones a page can't cope with: something has to be there, it
    has to fit, and it has to be one line.
    """
    cleaned = " ".join((name or "").split())
    if not cleaned:
        return None, "A team needs a name."
    if len(cleaned) > TEAM_NAME_MAX:
        return None, (f"That is {len(cleaned)} characters; "
                      f"{TEAM_NAME_MAX} is as much as the table can hold.")
    with connect() as conn:
        conn.execute("UPDATE manager SET team = ? WHERE key = ?", (cleaned, key))
    return cleaned, None


def team_names():
    """What every manager currently calls their team."""
    with connect() as conn:
        return {r["key"]: r["team"]
                for r in conn.execute("SELECT key, team FROM manager")}


def manager_by_key(key):
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM manager WHERE key = ?", (key,)).fetchone()
        return dict(row) if row else None


def rotate_token(key, days=None, minutes=None):
    """Issue a new link, invalidating the old one.

    `days` or `minutes` set how long the new one lasts. A link handed out by
    an admin gets a week, because it may be sent before anyone is ready to
    use it. One a manager mints for their own second device gets minutes,
    because they are about to open it.
    """
    token = secrets.token_hex(16)
    if days is None and minutes is None:
        # Defaulting to "forever" would mean one forgetful call site is enough
        # to put an immortal link back in the database.
        days = LINK_DAYS
    expires = (datetime.now(timezone.utc)
               + timedelta(days=days or 0, minutes=minutes or 0)
               ).isoformat(timespec="seconds")
    with connect() as conn:
        conn.execute(
            "UPDATE manager SET token = ?, token_expires = ? WHERE key = ?",
            (token, expires, key))
    return token


# ── Sessions ───────────────────────────────────────────────────────────────
# A session is a browser that has signed in. The cookie holds a secret; this
# table holds only its hash, so reading the database gives you nobody's login.
LINK_DAYS = 7           # how long an admin-issued link stays good for
LINK_MINUTES = 15       # how long a self-issued one does
LINK_GRACE_MINUTES = 60  # how much longer it lasts once somebody has used it
SESSION_DAYS = 90       # how long a browser can go unused before it is dropped


def _fingerprint(secret):
    return hashlib.sha256(secret.encode()).hexdigest()


def start_session(key):
    """Sign a browser in, and hand back the secret its cookie should carry."""
    secret = secrets.token_urlsafe(32)
    with connect() as conn:
        conn.execute(
            "INSERT INTO session (id, manager, created_at, last_seen)"
            " VALUES (?, ?, ?, ?)",
            (_fingerprint(secret), key, now(), now()))
    return secret


def session_manager(secret):
    """Who a cookie belongs to, if the session is still alive.

    Touches last_seen, which is what makes the ninety days a sliding window
    rather than a hard stop: a manager who uses the app never gets logged
    out, and one who stops is dropped whether or not their browser still has
    the cookie.
    """
    if not secret:
        return None
    fingerprint = _fingerprint(secret)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=SESSION_DAYS)
              ).isoformat(timespec="seconds")
    with connect() as conn:
        row = conn.execute(
            "SELECT m.* FROM session s JOIN manager m ON m.key = s.manager"
            " WHERE s.id = ? AND s.last_seen >= ?", (fingerprint, cutoff)
        ).fetchone()
        if not row:
            return None
        conn.execute("UPDATE session SET last_seen = ? WHERE id = ?",
                     (now(), fingerprint))
        return dict(row)


def end_session(secret):
    if not secret:
        return
    with connect() as conn:
        conn.execute("DELETE FROM session WHERE id = ?", (_fingerprint(secret),))


def end_all_sessions(key):
    """Sign out everywhere — the answer to a laptop left in an office."""
    with connect() as conn:
        cur = conn.execute("DELETE FROM session WHERE manager = ?", (key,))
        return cur.rowcount


def sessions_for(key, secret=None):
    """Every browser signed in as this manager, newest first."""
    current = _fingerprint(secret) if secret else None
    cutoff = (datetime.now(timezone.utc) - timedelta(days=SESSION_DAYS)
              ).isoformat(timespec="seconds")
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM session WHERE manager = ? AND last_seen >= ?"
            " ORDER BY last_seen DESC", (key, cutoff)).fetchall()
    return [{"created_at": r["created_at"], "last_seen": r["last_seen"],
             "this_one": r["id"] == current} for r in rows]


def prune_sessions():
    """Drop sessions nobody has used inside the window."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=SESSION_DAYS)
              ).isoformat(timespec="seconds")
    with connect() as conn:
        return conn.execute("DELETE FROM session WHERE last_seen < ?",
                            (cutoff,)).rowcount


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


# ── Drafted managers ───────────────────────────────────────────────────────
def manager_clubs():
    """{team key: {"club": id, "sacked_from": gw or None}}."""
    with connect() as conn:
        return {r["manager"]: {"club": r["club_id"],
                               "sacked_from": r["sacked_from"]}
                for r in conn.execute("SELECT * FROM manager_club")}


def set_manager_club(key, club_id, sacked_from=None):
    with connect() as conn:
        conn.execute(
            "INSERT INTO manager_club (manager, club_id, sacked_from, assigned_at)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT(manager) DO UPDATE SET"
            "   club_id = excluded.club_id,"
            "   sacked_from = excluded.sacked_from,"
            "   assigned_at = excluded.assigned_at",
            (key, club_id, sacked_from, now()))


def assign_clubs_randomly(club_ids, seed=None):
    """Give every team a different club's manager.

    For testing before a real manager draft happens. Distinct clubs, because
    two teams sharing one would make their boosts move together and hide any
    bug that depends on them differing.
    """
    rng = secrets.SystemRandom() if seed is None else __import__("random").Random(seed)
    teams = [m["key"] for m in managers()]
    pool = list(club_ids)
    if len(pool) < len(teams):
        raise ValueError(f"{len(pool)} clubs for {len(teams)} teams")
    rng.shuffle(pool)
    for key, club in zip(teams, pool):
        set_manager_club(key, club)
    return dict(zip(teams, pool))


# ── Declarations ───────────────────────────────────────────────────────────
def declare(manager, gameweek, kind, payload=None):
    with connect() as conn:
        conn.execute(
            "INSERT INTO declaration (manager, gameweek, kind, payload, declared_at)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(manager, gameweek, kind) DO UPDATE SET"
            "   payload = excluded.payload, declared_at = excluded.declared_at",
            (manager, gameweek, kind, json.dumps(payload or {}), now()))


def withdraw(manager, gameweek, kind):
    with connect() as conn:
        conn.execute("DELETE FROM declaration"
                     " WHERE manager = ? AND gameweek = ? AND kind = ?",
                     (manager, gameweek, kind))


def declaration(manager, gameweek, kind):
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM declaration"
            " WHERE manager = ? AND gameweek = ? AND kind = ?",
            (manager, gameweek, kind)).fetchone()
    return dict(row) if row else None


def declarations(kind=None, manager=None):
    sql = "SELECT * FROM declaration"
    where, args = [], []
    if kind:
        where.append("kind = ?"); args.append(kind)
    if manager:
        where.append("manager = ?"); args.append(manager)
    if where:
        sql += " WHERE " + " AND ".join(where)
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql + " ORDER BY gameweek", args)]


def transactions():
    """Every declaration, in the shape the rules engine consumes.

    Waivers are the exception to one-row-one-transaction: the engine resolves
    a gameweek's claims as a single run so it can apply the snake priority, so
    every manager's claims for a round are gathered into one `waiver_run`.
    Emitting them individually would ask the engine to process each in
    isolation and lose the ordering the rule is built on.
    """
    out, runs = [], {}
    for row in declarations():
        payload = json.loads(row["payload"])
        if row["kind"] == "waiver":
            runs.setdefault(row["gameweek"], {})[row["manager"]] = \
                payload.get("claims", [])
            continue
        out.append({"type": row["kind"], "gameweek": row["gameweek"],
                    "team": row["manager"], "declared_at": row["declared_at"],
                    **payload})
    for gameweek, claims in runs.items():
        out.append({"type": "waiver_run", "gameweek": gameweek, "claims": claims})
    for row in free_agent_moves():
        out.append({"type": "free_agent", "gameweek": row["gameweek"],
                    "team": row["manager"], "made_at": row["made_at"],
                    "drop": json.loads(row["dropped"]),
                    "add": json.loads(row["added"])})
    return out


# ── Free agency ────────────────────────────────────────────────────────────
def take_free_agent(gameweek, manager, dropped, added):
    """Record a free-agency move. The clock on it is what settles a race."""
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO free_agent_move (gameweek, manager, dropped, added,"
            " made_at) VALUES (?, ?, ?, ?, ?)",
            (gameweek, manager, json.dumps(dropped), json.dumps(added), now()))
        return cur.lastrowid


def undo_free_agent(move_id):
    """Drop a move that lost a race. Only ever used the moment after it was
    written, when the engine says somebody else got there first."""
    with connect() as conn:
        conn.execute("DELETE FROM free_agent_move WHERE id = ?", (move_id,))


def free_agent_moves(gameweek=None, manager=None):
    sql = "SELECT * FROM free_agent_move"
    where, args = [], []
    if gameweek is not None:
        where.append("gameweek = ?"); args.append(gameweek)
    if manager:
        where.append("manager = ?"); args.append(manager)
    if where:
        sql += " WHERE " + " AND ".join(where)
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql + " ORDER BY made_at, id", args)]


# ── Trades ─────────────────────────────────────────────────────────────────
def propose_trade(gameweek, proposer, receiver, players_out, players_in,
                  points=0, note=None):
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO trade (gameweek, proposer, receiver, players_out,"
            " players_in, points, status, note, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, 'proposed', ?, ?)",
            (gameweek, proposer, receiver, json.dumps(players_out),
             json.dumps(players_in), int(points), note, now()))
        return cur.lastrowid


def _row_to_trade(row, vetoes):
    return {
        "id": row["id"], "gameweek": row["gameweek"],
        "proposer": row["proposer"], "receiver": row["receiver"],
        "players_out": json.loads(row["players_out"]),
        "players_in": json.loads(row["players_in"]),
        "points": row["points"], "status": row["status"], "note": row["note"],
        "created_at": row["created_at"], "resolved_at": row["resolved_at"],
        "vetoes": vetoes.get(row["id"], []),
    }


def trades(status=None, manager=None):
    sql = "SELECT * FROM trade"
    where, args = [], []
    if status:
        where.append("status = ?"); args.append(status)
    if manager:
        where.append("(proposer = ? OR receiver = ?)"); args += [manager, manager]
    if where:
        sql += " WHERE " + " AND ".join(where)
    with connect() as conn:
        rows = list(conn.execute(sql + " ORDER BY id DESC", args))
        vetoes = {}
        for v in conn.execute("SELECT * FROM trade_veto"):
            vetoes.setdefault(v["trade_id"], []).append(v["manager"])
    return [_row_to_trade(r, vetoes) for r in rows]


def trade(trade_id):
    with connect() as conn:
        row = conn.execute("SELECT * FROM trade WHERE id = ?", (trade_id,)).fetchone()
        if not row:
            return None
        vetoes = {trade_id: [v["manager"] for v in conn.execute(
            "SELECT manager FROM trade_veto WHERE trade_id = ?", (trade_id,))]}
    return _row_to_trade(row, vetoes)


def set_trade_status(trade_id, status):
    with connect() as conn:
        conn.execute("UPDATE trade SET status = ?, resolved_at = ? WHERE id = ?",
                     (status, now(), trade_id))


def veto_trade(trade_id, manager):
    with connect() as conn:
        conn.execute("INSERT OR IGNORE INTO trade_veto (trade_id, manager, created_at)"
                     " VALUES (?, ?, ?)", (trade_id, manager, now()))


def unveto_trade(trade_id, manager):
    with connect() as conn:
        conn.execute("DELETE FROM trade_veto WHERE trade_id = ? AND manager = ?",
                     (trade_id, manager))
