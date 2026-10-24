"""Small HTTP conventions every module follows (see docs/ARCHITECTURE.md).

JSON responses
    success  ``{"ok": true,  "data": <anything>}``
    failure  ``{"ok": false, "error": "<Persian message>", "code": "<machine_code>", "fields": {name: message}}``
    ``error`` is a plain string because ``Atelier.fetchJSON`` shows
    ``body.error`` / ``body.message`` as the toast text.

Pagination
    ``?page=N`` (1-based) and optional ``?per_page=M`` (capped); :func:`paginate`
    returns a Flask-SQLAlchemy ``Pagination`` that the ``pagination`` macro renders.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from flask import Response, jsonify, request

from app.extensions import db
from app.services.security import is_safe_next

DEFAULT_PER_PAGE = 20
MAX_PER_PAGE = 100


# --------------------------------------------------------------------------
# JSON
# --------------------------------------------------------------------------


def json_ok(data: Any = None, status: int = 200, **extra: Any) -> Response:
    """``{"ok": true, "data": data}`` (extra keyword arguments are merged at top level)."""
    payload: dict[str, Any] = {"ok": True, "data": data}
    payload.update(extra)
    response = jsonify(payload)
    response.status_code = status
    return response


def json_error(
    message: str,
    status: int = 400,
    code: Optional[str] = None,
    fields: Optional[Mapping[str, str]] = None,
    **extra: Any,
) -> Response:
    """``{"ok": false, "error": message, "code": ..., "fields": {...}}``."""
    payload: dict[str, Any] = {"ok": False, "error": message, "code": code or _default_code(status)}
    if fields:
        payload["fields"] = dict(fields)
    payload.update(extra)
    response = jsonify(payload)
    response.status_code = status
    return response


_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "too_large",
    422: "validation_error",
    429: "rate_limited",
}


def _default_code(status: int) -> str:
    return _CODES.get(status, "server_error" if status >= 500 else "error")


def wants_json() -> bool:
    """True when the caller expects JSON: ``/api/`` paths, JSON bodies, ``Accept: application/json``, XHR."""
    if request.path.startswith("/api/") or request.is_json:
        return True
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return True
    # An explicit preference for JSON (fetch/axios send "application/json, text/plain, */*");
    # browsers list text/html and "*/*" alone must not count as JSON.
    quality = {mime: q for mime, q in request.accept_mimetypes}
    json_q = quality.get("application/json", 0)
    return json_q > 0 and json_q > quality.get("text/html", 0)


def json_body() -> dict[str, Any]:
    """The request's JSON object (``{}`` when absent or not an object)."""
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


# --------------------------------------------------------------------------
# pagination
# --------------------------------------------------------------------------


def page_args(default_per_page: int = DEFAULT_PER_PAGE, max_per_page: int = MAX_PER_PAGE) -> tuple[int, int]:
    """``(page, per_page)`` from the query string; junk falls back to the defaults."""
    try:
        page = max(1, int(request.args.get("page", 1)))
    except (TypeError, ValueError):
        page = 1
    try:
        per_page = int(request.args.get("per_page", default_per_page))
    except (TypeError, ValueError):
        per_page = default_per_page
    return page, max(1, min(per_page, max_per_page))


def paginate(stmt: Any, *, per_page: int = DEFAULT_PER_PAGE, max_per_page: int = MAX_PER_PAGE):
    """``db.paginate`` for a ``select()`` using ``?page`` / ``?per_page`` (never 404s on a bad page)."""
    page, size = page_args(per_page, max_per_page)
    return db.paginate(stmt, page=page, per_page=size, max_per_page=max_per_page, error_out=False, count=True)


# --------------------------------------------------------------------------
# redirects
# --------------------------------------------------------------------------


def referrer_path() -> Optional[str]:
    """Path (+query) of the same-site referrer, or None."""
    ref = request.referrer
    if not ref:
        return None
    from urllib.parse import urlsplit

    parts = urlsplit(ref)
    if parts.netloc and parts.netloc != request.host:
        return None
    candidate = parts.path + (f"?{parts.query}" if parts.query else "")
    return candidate if is_safe_next(candidate) else None
