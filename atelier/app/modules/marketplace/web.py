"""Response helpers shared by the marketplace routes (HTML redirects with flashes, or JSON for fetch callers)."""

from __future__ import annotations

from typing import Any, Optional

from flask import flash, redirect, url_for
from werkzeug.wrappers import Response

from app.extensions import db
from app.modules.marketplace.services import MarketplaceError
from app.services.http import json_error, json_ok, wants_json


def done(message: str, endpoint: str, *, data: Optional[dict[str, Any]] = None, **values: Any) -> Response:
    """Success of a POST: a flash + redirect (Post-Redirect-Get), or ``{ok: true}`` for JSON callers."""
    if wants_json():
        return json_ok({"message": message, **(data or {})})
    flash(message, "success")
    return redirect(url_for(endpoint, **values))


def refuse(exc: MarketplaceError, endpoint: str, **values: Any) -> Response:
    """A rule refused the POST: roll the transaction back, then flash (or answer JSON) with the reason."""
    db.session.rollback()
    if wants_json():
        return json_error(str(exc), exc.status, exc.code)
    flash(str(exc), "error")
    return redirect(url_for(endpoint, **values))


def fail(message: str, endpoint: str, status: int = 409, code: str = "conflict", **values: Any) -> Response:
    """Like :func:`refuse` for a plain message (a concurrent change, a lost race)."""
    db.session.rollback()
    if wants_json():
        return json_error(message, status, code)
    flash(message, "error")
    return redirect(url_for(endpoint, **values))


def _route_exists(path: str) -> bool:
    from flask import current_app
    from werkzeug.exceptions import HTTPException
    from werkzeug.routing import RequestRedirect

    try:
        current_app.url_map.bind("localhost").match(path, method="GET")
    except RequestRedirect:
        return True
    except HTTPException:
        return False
    return True


def staff_profile_url() -> Optional[str]:
    """Where a staff member edits their profile (skills!), resolved defensively: the ``staff`` module is
    built independently and may be disabled, so the link is ``None`` when it does not exist."""
    for endpoint in ("staff.profile", "staff.profile_edit", "staff.edit_profile", "staff.index"):
        try:
            return url_for(endpoint)
        except Exception:  # noqa: BLE001 - BuildError: no such endpoint
            continue
    return "/me/profile" if _route_exists("/me/profile") else None


def optional_url(endpoint: str, **values: Any) -> Optional[str]:
    """``url_for`` for an endpoint of another module that may be disabled: ``None`` when it does not exist."""
    try:
        return url_for(endpoint, **values)
    except Exception:  # noqa: BLE001 - BuildError
        return None
