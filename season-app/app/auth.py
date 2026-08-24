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
# A season is long and nobody wants to dig out the link every month.
COOKIE_MAX_AGE = 400 * 24 * 60 * 60


def admin_keys():
    """Managers with admin rights, from the environment.

    Kept out of the database so it can't be granted by anything the app
    itself writes — changing who is admin means changing a deploy setting.
    """
    raw = os.environ.get("ADMIN_KEYS", "")
    return {k.strip().upper() for k in raw.split(",") if k.strip()}


def current(request: Request):
    """The signed-in manager, or None."""
    manager = db.manager_by_token(request.cookies.get(COOKIE))
    if manager:
        manager["is_admin"] = (bool(manager.get("is_admin"))
                               or manager["key"].upper() in admin_keys())
    return manager


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
    return response
