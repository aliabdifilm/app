"""Routes of the ``main`` module."""

from __future__ import annotations

import json
from urllib.parse import urlsplit

import sqlalchemy as sa
from flask import (
    Response,
    current_app,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    url_for,
)

from app.extensions import db
from app.models import Notification
from app.modules.main import bp
from app.services import nav, uploads
from app.services import notifications as notif
from app.services.http import json_ok, paginate, wants_json
from app.services.permissions import get_user, login_required
from app.services.security import is_safe_next

# --------------------------------------------------------------------------
# public pages
# --------------------------------------------------------------------------


@bp.get("/")
def landing():
    return render_template("public/landing.html")


@bp.get("/robots.txt")
def robots():
    lines = ["User-agent: *"]
    for prefix in ("auth", "admin", "accounting", "atelier", "hall", "me", "inventory", "media"):
        lines.append(f"Disallow: /{prefix}/")
    lines += ["Disallow: /dashboard", "Disallow: /notifications", "Disallow: /healthz", ""]
    return Response("\n".join(lines), mimetype="text/plain")


@bp.get("/favicon.ico")
def favicon():
    return redirect(url_for("static", filename="img/favicon.svg"), code=301)


# --------------------------------------------------------------------------
# operations
# --------------------------------------------------------------------------


@bp.get("/healthz")
def healthz():
    """Liveness probe: 200 when the app and its database answer, 503 otherwise."""
    database = "ok"
    try:
        db.session.execute(sa.text("SELECT 1"))
    except Exception:  # any driver error means "not healthy"
        current_app.logger.exception("health check: database unreachable")
        database = "error"
        try:
            db.session.rollback()
        except Exception:  # pragma: no cover
            pass
    payload = {"status": "ok" if database == "ok" else "degraded", "database": database}
    response = jsonify(payload)
    response.status_code = 200 if database == "ok" else 503
    response.headers["Cache-Control"] = "no-store"
    return response


# --------------------------------------------------------------------------
# PWA
# --------------------------------------------------------------------------


@bp.get("/manifest.webmanifest")
def manifest():
    """The static manifest with its relative icon paths made absolute (it is served from /)."""
    path = current_app.static_folder + "/manifest.webmanifest"
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    for icon in data.get("icons", []):
        src = icon.get("src", "")
        if src and not src.startswith(("/", "http")):
            icon["src"] = url_for("static", filename=src)
    data["start_url"] = "/"
    data["scope"] = "/"
    data["id"] = "/"
    data["name"] = current_app.config.get("PLATFORM_NAME", data.get("name"))
    response = Response(json.dumps(data, ensure_ascii=False), mimetype="application/manifest+json")
    response.headers["Cache-Control"] = "public, max-age=86400"
    return response


@bp.get("/sw.js")
def service_worker():
    """Serve the worker from the site root so its scope is the whole site."""
    response = send_from_directory(
        current_app.static_folder, "js/sw.js", mimetype="application/javascript", max_age=0
    )
    response.headers["Cache-Control"] = "no-cache"
    response.headers["Service-Worker-Allowed"] = "/"
    return response


@bp.get("/media/<path:relpath>")
def media(relpath: str):
    """Uploaded images; visibility is decided per kind (see app.services.uploads)."""
    return uploads.serve(relpath)


# --------------------------------------------------------------------------
# dashboard
# --------------------------------------------------------------------------


@bp.get("/dashboard")
@login_required(allow_pending=True)
def dashboard():
    user = get_user()
    if not user.is_approved:
        return redirect(url_for("auth.pending"))
    home = nav.home_url(user)
    if home and urlsplit(home).path.rstrip("/") != request.path.rstrip("/"):
        return redirect(home)
    return render_template(
        "main/dashboard.html",
        user=user,
        onboarding_url=nav.onboarding_url(user),
    )


# --------------------------------------------------------------------------
# notifications
# --------------------------------------------------------------------------


@bp.get("/notifications")
@login_required(allow_pending=True)
def notifications():
    user = get_user()
    stmt = (
        sa.select(Notification)
        .where(Notification.user_id == user.id)
        .order_by(Notification.created_at.desc(), Notification.id.desc())
    )
    page = paginate(stmt, per_page=20)
    unread = notif.unread_count(user)
    if wants_json():
        return json_ok(
            {
                "unread": unread,
                "items": [
                    {
                        "id": n.id,
                        "kind": n.kind,
                        "title": n.title,
                        "body": n.body,
                        "url": n.url if is_safe_next(n.url) else None,
                        "read": n.is_read,
                        "created_at": n.created_at.isoformat(),
                    }
                    for n in page.items
                ],
            }
        )
    return render_template("main/notifications.html", page=page, unread=unread, is_safe_url=is_safe_next)


@bp.post("/notifications/read")
@login_required(allow_pending=True)
def notifications_mark_read():
    user = get_user()
    raw_ids = request.form.getlist("ids") or (request.get_json(silent=True) or {}).get("ids") or []
    ids = []
    for value in raw_ids:
        try:
            ids.append(int(value))
        except (TypeError, ValueError):
            continue
    changed = notif.mark_read(user, ids) if raw_ids else notif.mark_all_read(user)
    db.session.commit()
    if wants_json():
        return json_ok({"changed": changed, "unread": notif.unread_count(user)})
    return redirect(url_for("main.notifications", page=request.args.get("page", type=int)))
