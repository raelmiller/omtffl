"""Who is this, and are they allowed to change it.

Fourteen people in a private league, so there is no password to reset in
August and no email provider to keep alive. A manager signs in by opening an
unguessable link, and that link is spent on arrival.

Two separate things, which is the point:

- A **sign-in link** is a one-shot. Opening it starts a session and issues a
  fresh link in its place, so a link pasted into a group chat is good until
  the manager opens it and dead afterwards. Links expire on their own as
  well — a week for one an admin hands out, minutes for one a manager mints
  for their own second device.
- A **session** is a browser. The cookie carries a secret the database only
  holds the hash of, so a copy of the database is not a set of working
  logins. Sessions can be listed and revoked one at a time or all at once,
  and one nobody has used for ninety days is dropped.

The honest limits, stated rather than glossed:

- Anyone holding an unspent link is that manager until they open it. It is
  still a bearer token, just a short-lived one.
- Anyone holding the cookie is that manager until it is revoked, which is
  what "sign out everywhere" is for.
- There is no second factor and no proof of identity beyond the link. That is
  proportionate for a fantasy league among friends, and would not be for
  anything with money or strangers in it.
"""
from __future__ import annotations

import os

from fastapi import Request

from . import db

COOKIE = "matchweek"          # holds a session secret, never a sign-in link
# Set separately from the sign-in cookie, and only ever honoured when the
# sign-in cookie belongs to an admin. Keeping them apart means the real
# identity is never overwritten, so "view as" can't become "become".
VIEW_AS = "matchweek_as"
# The browser is asked to keep the cookie for a long time; how long the
# session behind it lives is decided in the database, where it can be seen
# and revoked. A cookie that outlives its session simply stops working.
COOKIE_MAX_AGE = 400 * 24 * 60 * 60


ADMIN_VAR = "ADMIN_KEYS"


def _admin_setting():
    """The raw ADMIN_KEYS value, and which variable name it was found under.

    Environment variable names are case-sensitive on Linux, so a variable
    typed as `admin_keys` in a hosting dashboard is simply a different
    variable and the admin page 404s with nothing to explain why. Accepting
    either spelling costs nothing and removes a failure that looks like a bug.
    """
    for name, value in os.environ.items():
        if name.upper() == ADMIN_VAR and value.strip():
            return value, name
    return "", None


def admin_keys():
    """Managers with admin rights, from the environment.

    Kept out of the database so it can't be granted by anything the app
    itself writes — changing who is admin means changing a deploy setting.
    """
    raw, _ = _admin_setting()
    return {k.strip().upper() for k in raw.split(",") if k.strip()}


def admin_source():
    """Which variable name the admin setting came from, for the health page."""
    return _admin_setting()[1]


def real(request: Request):
    """Whoever actually holds the session cookie."""
    manager = db.session_manager(request.cookies.get(COOKIE))
    if manager:
        manager["is_admin"] = (bool(manager.get("is_admin"))
                               or manager["key"].upper() in admin_keys())
    return manager


def current(request: Request):
    """The manager the app should act as.

    Normally whoever signed in. An admin may look at the app as another
    manager, which is the only way to test anything two-sided — a trade, or
    two managers chasing the same free agent — before the league exists.

    The borrowed identity never gains admin rights, and the real one is kept
    alongside so every page can say plainly whose team is on screen.
    """
    me = real(request)
    if not me or not me["is_admin"]:
        return me
    borrowed = request.cookies.get(VIEW_AS)
    if not borrowed or borrowed == me["key"]:
        return me
    other = db.manager_by_key(borrowed)
    if not other:
        return me
    other["is_admin"] = False
    other["viewed_by"] = me["team"]
    return other


def sign_in(response, secret):
    response.set_cookie(
        COOKIE, secret,
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        # Railway terminates TLS, so the cookie should never travel in clear.
        # Left off in development, where there is no HTTPS to speak of.
        secure=bool(os.environ.get("RAILWAY_ENVIRONMENT")),
    )
    return response


def sign_out(response, request=None):
    """Drop this browser's session, and the cookie that pointed at it.

    Deleting the row matters more than deleting the cookie: a cookie the
    holder declines to throw away is only worth something while the session
    behind it is alive.
    """
    if request is not None:
        db.end_session(request.cookies.get(COOKIE))
    response.delete_cookie(COOKIE)
    response.delete_cookie(VIEW_AS)
    return response


def view_as(response, key):
    """Look at the app as another manager, or stop."""
    if key:
        response.set_cookie(VIEW_AS, key, httponly=True, samesite="lax",
                            secure=bool(os.environ.get("RAILWAY_ENVIRONMENT")))
    else:
        response.delete_cookie(VIEW_AS)
    return response
