"""app.services.security: redirects, rate limiter, passwords, OTP generation, clock."""

from __future__ import annotations

import datetime as dt
import threading

import pytest

from app.services import security
from app.services.security import RateLimiter, is_safe_next, safe_next


# --------------------------------------------------------------------------
# safe redirects
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "target",
    [
        "/",
        "/dashboard",
        "/events/12?tab=crew",
        "/jobs?city=%D8%B4%DB%8C%D8%B1%D8%A7%D8%B2#top",
        "/hall/3/events/new",
        "/search?q=a//b",
    ],
)
def test_safe_targets(target):
    assert is_safe_next(target) is True
    assert safe_next(target, "/home") == target


@pytest.mark.parametrize(
    "target",
    [
        None,
        "",
        123,
        "dashboard",
        "evil.example",
        "//evil.example",
        "///evil.example",
        "http://evil.example",
        "https://evil.example/x",
        "ftp://evil.example",
        "javascript:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "/\\evil.example",
        "\\\\evil.example",
        "\\/evil.example",
        "/\t/evil.example",
        "/\n/evil.example",
        "/\r\nLocation: https://evil.example",
        " /dashboard",
        "\x00/dashboard",
        "/auth/login",
        "/auth/verify?next=/x",
        "/" + "a" * 3000,
    ],
)
def test_unsafe_targets_fall_back(target):
    assert is_safe_next(target) is False
    assert safe_next(target, "/home") == "/home"


# --------------------------------------------------------------------------
# OTP generation
# --------------------------------------------------------------------------


def test_generate_otp_shape_and_leading_zeros():
    codes = [security.generate_otp(6) for _ in range(3000)]
    assert all(len(code) == 6 and code.isdigit() for code in codes)
    assert any(code.startswith("0") for code in codes)  # zero-padded, not an int
    assert len(set(codes)) > 2900  # not constant / not tiny space
    assert len(security.generate_otp(8)) == 8


def test_generate_otp_digits_are_roughly_uniform():
    digits = "".join(security.generate_otp(6) for _ in range(5000))
    counts = [digits.count(str(d)) for d in range(10)]
    assert min(counts) > 2700 and max(counts) < 3300  # 3000 expected each


def test_generate_otp_rejects_silly_lengths():
    with pytest.raises(ValueError):
        security.generate_otp(2)


def test_constant_time_equals():
    assert security.constant_time_equals("123456", "123456")
    assert not security.constant_time_equals("123456", "123457")
    assert not security.constant_time_equals("123456", "12345")
    assert security.constant_time_equals("۱۲۳", "۱۲۳")


def test_mask_mobile():
    assert security.mask_mobile("09121234567") == "0912***4567"
    assert security.mask_mobile("") == "***"


# --------------------------------------------------------------------------
# rate limiter
# --------------------------------------------------------------------------


class Ticker:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_limiter_allows_up_to_the_limit_then_blocks():
    ticker = Ticker()
    limiter = RateLimiter(ticker)
    results = [limiter.hit("k", 3, 60).allowed for _ in range(5)]
    assert results == [True, True, True, False, False]
    blocked = limiter.hit("k", 3, 60)
    assert blocked.retry_after == 60 and blocked.count == 3 and not blocked


def test_limiter_window_slides():
    ticker = Ticker()
    limiter = RateLimiter(ticker)
    for _ in range(3):
        limiter.hit("k", 3, 60)
        ticker.now += 10
    assert not limiter.check("k", 3, 60)
    assert limiter.check("k", 3, 60).retry_after == 30  # the oldest event leaves the window at t0 + 60
    ticker.now += 29
    assert not limiter.check("k", 3, 60)
    ticker.now += 1
    assert limiter.check("k", 3, 60)
    assert limiter.hit("k", 3, 60).allowed


def test_limiter_keys_are_independent_and_resettable():
    limiter = RateLimiter(Ticker())
    for _ in range(2):
        limiter.hit("a", 2, 60)
    assert not limiter.hit("a", 2, 60)
    assert limiter.hit("b", 2, 60)
    limiter.reset("a")
    assert limiter.hit("a", 2, 60)
    limiter.clear()
    assert limiter.check("b", 2, 60).count == 0


def test_check_does_not_count_and_record_does():
    limiter = RateLimiter(Ticker())
    for _ in range(10):
        assert limiter.check("k", 1, 60)
    limiter.record("k", 60)
    assert not limiter.check("k", 1, 60)


def test_limiter_is_thread_safe():
    limiter = RateLimiter(Ticker())
    allowed = []

    def worker():
        for _ in range(100):
            allowed.append(limiter.hit("k", 50, 3600).allowed)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum(allowed) == 50


def test_limiter_memory_is_bounded():
    limiter = RateLimiter(Ticker())
    limiter.MAX_KEYS = 100
    for index in range(500):
        limiter.record(f"key{index}", 1)
        limiter._time = lambda: 5000.0 + index  # time passes: old keys go stale
    assert len(limiter._events) < 500


def test_each_app_has_its_own_limiter(app):
    first = security.get_rate_limiter()
    assert security.get_rate_limiter() is first
    first.record("x", 60)
    from app import create_app

    other = create_app("testing")
    with other.app_context():
        assert security.get_rate_limiter() is not first
        assert security.get_rate_limiter().check("x", 1, 60)


# --------------------------------------------------------------------------
# passwords
# --------------------------------------------------------------------------


@pytest.mark.parametrize("password", ["Sup3r-secret-pass", "کلمه-عبور-خوب-۱۲", "a" * 12 + "1b"])
def test_good_passwords(password):
    assert security.validate_password(password, mobile="09121234567") == []


@pytest.mark.parametrize(
    "password",
    ["", "short1", "12345678", "00000000", "password", "Password", "aaaaaaaaaaaa", "x" * 129, " padded-pass-1", "pass09121234567"],
)
def test_bad_passwords(password):
    assert security.validate_password(password, mobile="09121234567")


def test_burn_password_check_takes_time_like_a_real_check(app):
    import time

    from werkzeug.security import generate_password_hash

    security.burn_password_check("warm-up")  # builds the dummy hash once
    start = time.perf_counter()
    security.burn_password_check("whatever")
    burned = time.perf_counter() - start
    start = time.perf_counter()
    generate_password_hash("whatever")
    real = time.perf_counter() - start
    assert burned > real / 20  # same order of magnitude (a hash, not a no-op)


# --------------------------------------------------------------------------
# clock
# --------------------------------------------------------------------------


def test_clock_defaults_to_real_utc_time(app):
    before = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    now = security.now()
    after = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    assert before <= now <= after and now.tzinfo is None


def test_frozen_clock_controls_now_and_today(app, clock):
    assert security.now() == dt.datetime(2025, 7, 17, 12, 0)
    assert security.today() == dt.date(2025, 7, 17)
    clock.advance(hours=12)  # 00:00 UTC = 03:30 in Iran -> still the 18th over there
    assert security.today() == dt.date(2025, 7, 18)
    clock.set("2025-07-17 21:00:00")  # 00:30 next day in Iran
    assert security.today() == dt.date(2025, 7, 18)
    assert security.to_timestamp(dt.datetime(1970, 1, 2)) == 86400.0


def test_clock_accepts_aware_datetimes(app):
    from app.extensions import get_state

    get_state(app).clock = lambda: dt.datetime(2025, 1, 1, 3, 30, tzinfo=dt.timezone(dt.timedelta(hours=3, minutes=30)))
    assert security.now() == dt.datetime(2025, 1, 1, 0, 0)


# --------------------------------------------------------------------------
# headers
# --------------------------------------------------------------------------


def test_csp_string():
    csp = security.content_security_policy()
    assert csp.count(";") == 11
    assert "frame-ancestors 'none'" in csp and "object-src 'none'" in csp
