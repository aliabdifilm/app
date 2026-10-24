"""``auth`` - sign-in, registration and the account page (assumption A6).

Mobile number + one-time code is the primary way in; a password is optional.

======================  ==========================================================================
``/auth/login``         enter the mobile number -> a 6-digit code is sent by SMS
``/auth/verify``        enter the code (also ``/auth/verify/resend``)
``/auth/login/password``  optional password sign-in
``/auth/register``      choose a role (staff / hall manager / atelier manager) and a name;
                        the account is created once the code is verified
``/auth/logout``        POST only (CSRF protected)
``/auth/pending``       "waiting for admin approval" page
``/auth/account``       who am I, set / change the password
``/auth/password``      set or change the password
======================  ==========================================================================

Design notes (details in docs/ARCHITECTURE.md, "Authentication"):

* login and registration are **one pipeline**: whether a number has an account is only
  revealed after its owner proved ownership with the code (no account enumeration);
* codes are hashed at rest, expire after 2 minutes, allow 5 attempts, are limited per
  mobile (cooldown + hourly cap) and per IP using the ``otp_codes`` table (works with
  any number of workers, no Redis);
* the session is cleared on every login (no fixation), ``next`` is same-site only,
  suspended users are refused, everything important is audited.

Other modules use :func:`app.services.permissions.role_required` / ``login_required``
and never touch this package's internals.
"""

from flask import Blueprint

from app.services import nav

bp = Blueprint("auth", __name__, url_prefix="/auth")

# The account page is available to every role; modules add their own profile pages.
nav.register(None, "حساب کاربری", "auth.account", icon="user", order=98)

from app.modules.auth import routes  # noqa: E402,F401  (attaches the routes to bp)
