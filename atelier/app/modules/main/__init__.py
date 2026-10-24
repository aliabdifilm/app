"""``main`` - the public face and the shared plumbing of the app.

Routes (no URL prefix)::

    /                       landing page (public)
    /dashboard              sends each role to its home page (nav.register_home) or shows a menu page
    /notifications          the bell: list, mark read           (POST /notifications/read)
    /healthz                liveness + database check (JSON, no auth)
    /manifest.webmanifest   PWA manifest with absolute icon URLs
    /sw.js                  the service worker, scoped to the whole site
    /media/<path>           uploaded images, with ownership / visibility checks
    /robots.txt, /favicon.ico

Also registers the error handlers (see :mod:`app.modules.main.errors`).
Other modules must not define these URLs.
"""

from flask import Blueprint

bp = Blueprint("main", __name__)

from app.modules.main import routes  # noqa: E402,F401  (attaches the routes to bp)
from app.modules.main.errors import register_error_handlers  # noqa: E402


def register(app):
    register_error_handlers(app)
