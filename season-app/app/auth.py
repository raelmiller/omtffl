"""Who is this, and are they allowed to change it.

Fourteen people in a private league, so the whole scheme is one unguessable
link per manager. Open it once, it sets a cookie, and you never think about
it again. There is no password to reset in August and no email provider to
keep alive.

The honest limits, stated rather than glossed:

- Anyone holding the link is that manager. It is a bearer token, so treat it
  like a house key: share it and you have shared your team.
- If one leaks, the admin page issues a new one and the old link stops
  working immediately.
- The cookie is the same token, so signing out on a shared device matters
  more than it would with a password.

That is proportionate for a fantasy league among friends. It would not be for
anything with money or strangers in it.
"""
from __future__ import annotations

import os

from fastapi import Request

from . import db

COOKIE = "matchweek"
# Set separately from the sign-in cookie, and only ever honoured when the
# sign-in cookie belongs to an admin. Keeping them apart means the real
# identity is never overwritten, so "view as" can't become "become".
VIEW_AS = "matchweek_as"
# A season is long and nobody wants to dig out the link every month.
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
    """Whoever actually holds the sign-in cookie."""
    manager = db.manager_by_token(request.cookies.get(COOKIE))
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


def sign_in(response, token):
    response.set_cookie(
        COOKIE, token,
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        # Railway terminates TLS, so the cookie should never travel in clear.
        # Left off in development, where there is no HTTPS to speak of.
        secure=bool(os.environ.get("RAILWAY_ENVIRONMENT")),
    )
    return response


def sign_out(response):
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
