"""Response helpers of the staff routes: Post-Redirect-Get with a flash, or ``{ok, data|error}`` for fetch callers."""

from __future__ import annotations

from typing import Any, Optional

from flask import flash, redirect, request, url_for
from werkzeug.exceptions import HTTPException
from werkzeug.routing import BuildError
from werkzeug.wrappers import Response

from app.extensions import db
from app.services.http import json_error, json_ok, wants_json
from app.services.security import safe_next


def back_url(endpoint: str, **values: Any) -> str:
    """Where to go after a POST: the same-site ``next`` of the form, else *endpoint*."""
    return safe_next(request.form.get("next") or request.args.get("next"), url_for(endpoint, **values))


def done(message: str, target: str, *, data: Optional[dict[str, Any]] = None, level: str = "success") -> Response:
    """Success of a POST: flash + redirect to *target*, or ``{ok: true}`` for JSON callers."""
    if wants_json():
        return json_ok({"message": message, **(data or {})})
    flash(message, level)
    return redirect(target)


def refuse(
    message: str,
    target: str,
    *,
    status: int = 409,
    code: str = "conflict",
    level: str = "error",
    fields: Optional[dict[str, str]] = None,
) -> Response:
    """A rule refused the POST: roll back, then flash (or answer JSON with the reason)."""
    db.session.rollback()
    if wants_json():
        return json_error(message, status, code, fields=fields)
    flash(message, level)
    return redirect(target)


def optional_url(endpoint: str, **values: Any) -> Optional[str]:
    """``url_for`` for an endpoint of a module that may be disabled: ``None`` when it does not exist."""
    try:
        return url_for(endpoint, **values)
    except (BuildError, HTTPException):
        return None
