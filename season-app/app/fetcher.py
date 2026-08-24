"""Keeping gameweek data current, and being honest when it can't.

The whole FPL pipeline has run inside GitHub Actions until now, because the
development sandbox has no route to fantasy.premierleague.com. Whether the
host does is the open question phase one exists to answer, so this module
probes it explicitly and reports the result rather than failing quietly.

Two modes, and the app works either way:

  LIVE     the host can reach the FPL API, so refresh on a schedule.
  ARCHIVE  it can't, so the committed files stand and the Actions workflow
           keeps updating them. The health page says so plainly.

Refreshes are non-destructive by design. A failed fetch leaves the last good
data in place — a league table that goes blank because an upstream API had a
bad minute is worse than one that is a few hours stale.
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from urllib.error import URLError
from urllib.request import Request, urlopen

from .engine import SHADOW

FPL_PROBE = "https://fantasy.premierleague.com/api/bootstrap-static/"
HEADERS = {"User-Agent": "Mozilla/5.0 (omtffl-season-app)"}

# Filled in by probe() and the scheduled refresh; surfaced on /health.
STATUS = {
    "reachable": None,        # None until probed
    "probed_at": None,
    "last_refresh": None,
    "last_refresh_ok": None,
    "last_refresh_detail": None,
}


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def probe(timeout=8):
    """Can this host reach the FPL API at all?

    Deliberately a real request rather than a DNS check — the sandbox
    resolves the name perfectly well and still can't fetch a byte.
    """
    try:
        req = Request(FPL_PROBE, headers=HEADERS)
        with urlopen(req, timeout=timeout) as r:
            ok = r.status == 200
            body = r.read(2048)
        detail = f"HTTP {r.status}, {len(body)} bytes read"
    except (URLError, OSError, TimeoutError) as exc:
        ok, detail = False, f"{type(exc).__name__}: {exc}"
    except Exception as exc:                      # noqa: BLE001 - report anything
        ok, detail = False, f"{type(exc).__name__}: {exc}"

    STATUS["reachable"] = ok
    STATUS["probed_at"] = _now()
    STATUS["probe_detail"] = detail
    return ok


def refresh(timeout=180):
    """Pull any new gameweek data, if this host can.

    Delegates to the existing fetcher rather than duplicating it: that script
    already handles stat corrections, provisional rounds and the polite delay
    between requests, and it is the same code the Actions workflow runs.
    """
    if STATUS["reachable"] is None:
        probe()
    if not STATUS["reachable"]:
        STATUS.update(last_refresh=_now(), last_refresh_ok=False,
                      last_refresh_detail="host cannot reach the FPL API — "
                                          "serving committed data")
        return False

    try:
        done = subprocess.run(
            [sys.executable, str(SHADOW / "fetch_gw.py")],
            cwd=str(SHADOW), capture_output=True, text=True, timeout=timeout)
        ok = done.returncode == 0
        tail = (done.stdout or done.stderr or "").strip().splitlines()
        detail = tail[-1] if tail else f"exit {done.returncode}"
    except subprocess.TimeoutExpired:
        ok, detail = False, f"fetch timed out after {timeout}s"
    except Exception as exc:                      # noqa: BLE001
        ok, detail = False, f"{type(exc).__name__}: {exc}"

    STATUS.update(last_refresh=_now(), last_refresh_ok=ok,
                  last_refresh_detail=detail)
    return ok


def mode():
    if STATUS["reachable"] is None:
        return "unknown"
    return "live" if STATUS["reachable"] else "archive"
