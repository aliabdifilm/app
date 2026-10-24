"""Security helpers shared by the app shell and the modules.

* **Clock** - :func:`now` / :func:`today` read the per-app clock, so tests can
  freeze time (``clock`` fixture).  New code should call these instead of
  ``datetime.utcnow()``.
* **Redirects** - :func:`safe_next` only ever returns a same-site path.
* **One-time codes** - :func:`generate_otp` (``secrets``), :func:`constant_time_equals`.
* **Rate limiting** without Redis - :class:`RateLimiter`, an in-process
  sliding window kept in ``app.extensions["atelier"]``.  It is per worker
  process: with N gunicorn workers the effective limit is at most N times the
  configured one.  OTP *sending* limits do not use it (they count ``otp_codes``
  rows, so they are exact across workers).
* **Passwords** - :func:`validate_password`, :func:`burn_password_check` (equalises
  timing for unknown accounts).
* **HTTP headers** - :func:`content_security_policy` and
  :func:`init_security_headers` (CSP without external hosts, nosniff,
  frame-ancestors none, referrer policy, HSTS in production, no-store for HTML).
"""

from __future__ import annotations

import datetime as dt
import hmac
import re
import secrets
import threading
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

from flask import Flask, Response, has_request_context, request
from werkzeug.security import check_password_hash, generate_password_hash

from app.extensions import get_state
from app.models.base import to_naive_utc, utcnow
from app.services import jalali

# --------------------------------------------------------------------------
# clock
# --------------------------------------------------------------------------

_EPOCH = dt.datetime(1970, 1, 1)


def now() -> dt.datetime:
    """Current time as naive UTC (the storage convention), honouring the test clock."""
    try:
        clock = get_state().clock
    except RuntimeError:  # no application context
        clock = None
    if clock is None:
        return utcnow()
    value = clock() if callable(clock) else clock
    return to_naive_utc(value) or utcnow()


def today() -> dt.date:
    """Today's date in Iran time (what the user calls "today")."""
    return jalali.to_iran_time(now(), assume_utc=True).date()


def to_timestamp(value: dt.datetime) -> float:
    """Seconds since the epoch of a naive-UTC datetime."""
    return (to_naive_utc(value) - _EPOCH).total_seconds()


def timestamp() -> float:
    """Seconds since the epoch of :func:`now` (UTC)."""
    return to_timestamp(now())


# --------------------------------------------------------------------------
# request facts
# --------------------------------------------------------------------------


def client_ip() -> str:
    """Remote address (after ProxyFix when ``TRUST_PROXY_HOPS`` is set); '' outside a request."""
    if has_request_context():
        return (request.remote_addr or "")[:45]
    return ""


def mask_mobile(mobile: str) -> str:
    """``09123456789`` -> ``0912***6789`` for logs and audit data."""
    text = str(mobile or "")
    if len(text) < 8:
        return "***"
    return f"{text[:4]}***{text[-4:]}"


def constant_time_equals(a: str, b: str) -> bool:
    """Timing-safe string comparison."""
    return hmac.compare_digest(str(a).encode(), str(b).encode())


def generate_otp(length: int = 6) -> str:
    """A uniformly random numeric code of *length* digits (leading zeros kept)."""
    if length < 4:
        raise ValueError("OTP codes must have at least 4 digits")
    return "".join(str(secrets.randbelow(10)) for _ in range(length))


# --------------------------------------------------------------------------
# safe redirects
# --------------------------------------------------------------------------

_CONTROL = re.compile(r"[\x00-\x20\x7f\\]")
# Pages that must never be a post-login destination (loop / confusing).
_NEXT_DENY_PREFIXES = ("/auth/",)


def is_safe_next(target: Any) -> bool:
    """True when *target* is a same-site relative path that is safe to redirect to.

    Rejects absolute URLs, scheme-relative ``//host``, backslash tricks
    (``/\\host`` is ``//host`` in browsers), control characters / whitespace
    (a tab inside ``/\\t/host`` is stripped by browsers) and the auth pages.
    """
    if not isinstance(target, str) or not target or len(target) > 2000:
        return False
    if not target.startswith("/") or target.startswith("//"):
        return False
    if _CONTROL.search(target):
        return False
    parts = urlsplit(target)
    if parts.scheme or parts.netloc:
        return False
    return not any(parts.path.startswith(prefix) for prefix in _NEXT_DENY_PREFIXES)


def safe_next(target: Any, fallback: str = "/dashboard") -> str:
    """*target* when it is a safe same-site path, else *fallback*."""
    return target if is_safe_next(target) else fallback


def request_next(default: str = "/dashboard") -> str:
    """The safe ``next`` of the current request (query string or form field)."""
    if not has_request_context():
        return default
    return safe_next(request.values.get("next"), default)


# --------------------------------------------------------------------------
# rate limiting (in-process, no Redis)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RateDecision:
    allowed: bool
    retry_after: int = 0  # seconds until a slot frees up (0 when allowed)
    count: int = 0

    def __bool__(self) -> bool:  # `if limiter.check(...)` reads naturally
        return self.allowed


class RateLimiter:
    """Thread-safe sliding-window counter keyed by arbitrary strings.

    ``check`` looks, ``record`` counts, ``hit`` does both atomically::

        decision = limiter.hit(f"pw:{ip}:{mobile}", limit=5, window=900)
        if not decision:
            abort(429)

    Keys expire on their own; memory is bounded by pruning stale keys.
    """

    MAX_KEYS = 20_000

    def __init__(self, time_fn: Optional[Callable[[], float]] = None) -> None:
        self._events: dict[str, deque[float]] = {}
        self._lock = threading.Lock()
        self._time = time_fn or timestamp

    # internal --------------------------------------------------------------

    def _prune(self, key: str, window: float, now_: float) -> deque[float]:
        events = self._events.get(key)
        if events is None:
            events = self._events[key] = deque()
        cutoff = now_ - window
        while events and events[0] <= cutoff:
            events.popleft()
        return events

    def _gc(self, now_: float, window: float) -> None:
        if len(self._events) <= self.MAX_KEYS:
            return
        for key in [k for k, v in self._events.items() if not v or v[-1] <= now_ - window]:
            self._events.pop(key, None)

    # public ------------------------------------------------------------------

    def check(self, key: str, limit: int, window: float) -> RateDecision:
        """Would one more event be allowed?  Does not count anything."""
        now_ = self._time()
        with self._lock:
            events = self._prune(key, window, now_)
            if len(events) < limit:
                return RateDecision(True, 0, len(events))
            retry = max(1, int(events[0] + window - now_ + 0.999))
            return RateDecision(False, retry, len(events))

    def record(self, key: str, window: float = 3600) -> None:
        now_ = self._time()
        with self._lock:
            self._prune(key, window, now_).append(now_)
            self._gc(now_, window)

    def hit(self, key: str, limit: int, window: float) -> RateDecision:
        """Count one event unless the limit is already reached."""
        now_ = self._time()
        with self._lock:
            events = self._prune(key, window, now_)
            if len(events) >= limit:
                retry = max(1, int(events[0] + window - now_ + 0.999))
                return RateDecision(False, retry, len(events))
            events.append(now_)
            self._gc(now_, window)
            return RateDecision(True, 0, len(events))

    def reset(self, key: str) -> None:
        with self._lock:
            self._events.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._events.clear()


def get_rate_limiter() -> RateLimiter:
    """The current app's limiter (created on first use)."""
    state = get_state()
    if state.rate_limiter is None:
        state.rate_limiter = RateLimiter()
    return state.rate_limiter


# --------------------------------------------------------------------------
# passwords
# --------------------------------------------------------------------------

MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 128
_COMMON_PASSWORDS = frozenset(
    {
        "password", "password1", "passw0rd", "qwertyui", "qwerty123", "iloveyou",
        "abcd1234", "abc12345", "11111111", "12345678", "123456789", "1234567890",
        "atelier123", "atelier1234", "admin1234", "letmein1",
    }
)
_DUMMY_HASH: Optional[str] = None
_DUMMY_LOCK = threading.Lock()


def validate_password(password: str, *, mobile: Optional[str] = None) -> list[str]:
    """Persian error messages for an unacceptable password (empty list = fine)."""
    errors: list[str] = []
    password = password or ""
    if len(password) < MIN_PASSWORD_LENGTH:
        errors.append(f"رمز عبور باید دست‌کم {MIN_PASSWORD_LENGTH} نویسه باشد.")
    if len(password) > MAX_PASSWORD_LENGTH:
        errors.append(f"رمز عبور نباید بیش از {MAX_PASSWORD_LENGTH} نویسه باشد.")
    if password and password.strip() != password:
        errors.append("رمز عبور نباید با فاصله شروع یا تمام شود.")
    if password and password.isdigit():
        errors.append("رمز عبور نباید فقط عدد باشد.")
    lowered = password.lower()
    if lowered in _COMMON_PASSWORDS:
        errors.append("این رمز عبور بسیار رایج است؛ رمز دیگری انتخاب کنید.")
    if mobile and (mobile in password or (len(mobile) > 1 and mobile[1:] in password)):
        errors.append("رمز عبور نباید شامل شمارهٔ موبایل باشد.")
    if password and len(set(password)) <= 2 and len(password) >= MIN_PASSWORD_LENGTH:
        errors.append("رمز عبور بیش از حد ساده است.")
    return errors


def burn_password_check(password: str) -> None:
    """Spend the same time as a real password check (for unknown / password-less accounts)."""
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        with _DUMMY_LOCK:
            if _DUMMY_HASH is None:
                _DUMMY_HASH = generate_password_hash(secrets.token_hex(16))
    check_password_hash(_DUMMY_HASH, password or "")


# --------------------------------------------------------------------------
# HTTP security headers
# --------------------------------------------------------------------------


def content_security_policy() -> str:
    """The app-wide CSP: everything same-origin, no inline script or style, no framing."""
    directives = {
        "default-src": "'self'",
        "script-src": "'self'",
        "style-src": "'self'",
        "img-src": "'self' data: blob:",
        "font-src": "'self'",
        "connect-src": "'self'",
        "manifest-src": "'self'",
        "worker-src": "'self'",
        "object-src": "'none'",
        "base-uri": "'self'",
        "form-action": "'self'",
        "frame-ancestors": "'none'",
    }
    return "; ".join(f"{name} {value}" for name, value in directives.items())


_PERMISSIONS_POLICY = (
    "accelerometer=(), camera=(), geolocation=(), gyroscope=(), magnetometer=(), "
    "microphone=(), payment=(), usb=()"
)
SERVICE_WORKER_STATIC_PATH = "/static/js/sw.js"


def init_security_headers(app: Flask) -> None:
    """Register the ``after_request`` hook that adds the security headers."""
    csp = content_security_policy()

    @app.after_request
    def _security_headers(response: Response) -> Response:
        headers = response.headers
        headers.setdefault("X-Content-Type-Options", "nosniff")
        headers.setdefault("Referrer-Policy", "same-origin")
        headers.setdefault("X-Frame-Options", "DENY")
        headers.setdefault("Content-Security-Policy", csp)
        headers.setdefault("Permissions-Policy", _PERMISSIONS_POLICY)
        headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
        hsts = int(app.config.get("HSTS_SECONDS") or 0)
        if hsts and request.is_secure:
            headers.setdefault("Strict-Transport-Security", f"max-age={hsts}; includeSubDomains")
        # Pages carry personal and financial data: never keep them in shared / back-button caches.
        if response.mimetype == "text/html" and "Cache-Control" not in headers:
            headers["Cache-Control"] = "no-store"
        if request.path == SERVICE_WORKER_STATIC_PATH:
            # lets the worker served from /static/js/ control the whole site (docs/UI_GUIDE.md section 12)
            headers["Service-Worker-Allowed"] = "/"
        return response
