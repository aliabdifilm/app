"""Authentication flow (assumption A6): mobile number + one-time code, registration, logout."""

from __future__ import annotations

import datetime as dt
import re
from urllib.parse import urlsplit

import pytest
import sqlalchemy as sa

from app.extensions import db
from app.models import AuditLog, Notification, OtpCode, Setting, User
from app.services.constants import Role, UserStatus

MOBILE = "09121234567"


def send_code(client, mobile=MOBILE, **fields):
    return client.post("/auth/login", data={"mobile": mobile, **fields})


def verify(client, code):
    return client.post("/auth/verify", data={"code": code})


def sign_in(client, outbox, mobile=MOBILE, **fields):
    """Run the whole OTP login; returns the response of the verify step."""
    first = send_code(client, mobile, **fields)
    assert first.status_code == 302, first.get_data(as_text=True)
    return verify(client, outbox.last_code(mobile))


def path_of(response):
    return urlsplit(response.headers["Location"]).path


def current_user_id(client):
    with client.session_transaction() as session:
        return session.get("_user_id")


# --------------------------------------------------------------------------
# the happy path
# --------------------------------------------------------------------------


def test_login_page_renders(client):
    response = client.get("/auth/login")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'name="mobile"' in html and 'name="csrf_token"' in html
    assert "دریافت کد ورود" in html
    assert 'autocomplete="tel-national"' in html


def test_existing_user_signs_in_with_a_code(client, outbox, make_user):
    user = make_user("staff", mobile=MOBILE)
    response = send_code(client)
    assert response.status_code == 302 and path_of(response) == "/auth/verify"
    code = outbox.last_code(MOBILE)
    assert re.fullmatch(r"\d{6}", code)

    page = client.get("/auth/verify")
    assert page.status_code == 200
    assert "۰۹۱۲۱۲۳۴۵۶۷" in page.get_data(as_text=True)
    assert code not in page.get_data(as_text=True)  # the code is never shown (OTP_DEBUG_SHOW is off)

    done = verify(client, code)
    assert done.status_code == 302 and path_of(done) == "/dashboard"
    assert current_user_id(client) == str(user.id)
    assert client.get("/dashboard").status_code == 200


def test_login_updates_last_login_and_writes_audit(client, outbox, clock, make_user):
    user = make_user("staff", mobile=MOBILE)
    assert user.last_login_at is None
    sign_in(client, outbox)
    db.session.refresh(user)
    assert user.last_login_at == clock.now()
    actions = [row.action for row in db.session.execute(sa.select(AuditLog).order_by(AuditLog.id)).scalars()]
    assert "auth.otp_sent" in actions and "auth.login" in actions
    login_row = db.session.execute(sa.select(AuditLog).where(AuditLog.action == "auth.login")).scalar_one()
    assert login_row.actor_id == user.id and login_row.data["method"] == "otp"


def test_audit_log_never_contains_the_code_or_the_full_number(client, outbox, make_user):
    make_user("staff", mobile=MOBILE)
    sign_in(client, outbox)
    code = outbox.last_code(MOBILE)
    for row in db.session.execute(sa.select(AuditLog)).scalars():
        text = str(row.data)
        assert code not in text and MOBILE not in text


def test_otp_is_hashed_at_rest_and_expires_in_two_minutes(client, outbox, clock, make_user):
    make_user("staff", mobile=MOBILE)
    send_code(client)
    code = outbox.last_code(MOBILE)
    row = db.session.execute(sa.select(OtpCode)).scalar_one()
    assert code not in row.code_hash and len(row.code_hash) == 64
    assert row.code_hash == OtpCode.hash_code(MOBILE, code, client.application.config["SECRET_KEY"])
    assert row.expires_at - clock.now() == dt.timedelta(seconds=120)
    assert row.attempts == 0 and row.consumed_at is None


def test_the_code_is_not_logged_in_production_like_mode(client, outbox, make_user, caplog):
    make_user("staff", mobile=MOBILE)
    with caplog.at_level("DEBUG", logger="app"):
        send_code(client)
    code = outbox.last_code(MOBILE)
    assert "OTP sent to 0912***4567" in caplog.text  # the log line exists...
    assert code not in caplog.text  # ...without the code
    assert MOBILE not in caplog.text  # numbers are masked in logs


def test_a_used_code_cannot_be_used_twice(client, outbox, make_user, app):
    make_user("staff", mobile=MOBILE)
    sign_in(client, outbox)
    code = outbox.last_code(MOBILE)
    other = app.test_client()
    with other.session_transaction() as session:
        session["auth_flow"] = {"m": MOBILE, "p": "login", "at": 9e12, "next": "", "remember": False}
    response = verify(other, code)
    assert response.status_code == 422
    assert current_user_id(other) is None


def test_new_code_invalidates_the_previous_one(client, outbox, clock, make_user):
    make_user("staff", mobile=MOBILE)
    send_code(client)
    old = outbox.last_code(MOBILE)
    clock.advance(seconds=61)
    client.post("/auth/verify/resend")
    new = outbox.last_code(MOBILE)
    assert old != new
    assert verify(client, old).status_code == 422
    assert path_of(verify(client, new)) == "/dashboard"


# --------------------------------------------------------------------------
# wrong, expired and exhausted codes
# --------------------------------------------------------------------------


def test_wrong_code_is_rejected_and_counted(client, outbox, make_user):
    make_user("staff", mobile=MOBILE)
    send_code(client)
    response = verify(client, "000000" if outbox.last_code(MOBILE) != "000000" else "111111")
    assert response.status_code == 422
    assert "نادرست یا منقضی" in response.get_data(as_text=True)
    assert current_user_id(client) is None
    assert db.session.execute(sa.select(OtpCode)).scalar_one().attempts == 1


def test_five_wrong_attempts_kill_the_code_even_for_the_right_answer(client, outbox, make_user):
    make_user("staff", mobile=MOBILE)
    send_code(client)
    right = outbox.last_code(MOBILE)
    wrong = "123456" if right != "123456" else "654321"
    for _ in range(5):
        assert verify(client, wrong).status_code == 422
    response = verify(client, right)
    assert response.status_code == 422
    assert "کد جدید دریافت کنید" in response.get_data(as_text=True)
    assert current_user_id(client) is None


def test_expired_code_is_rejected(client, outbox, clock, make_user):
    make_user("staff", mobile=MOBILE)
    send_code(client)
    code = outbox.last_code(MOBILE)
    clock.advance(seconds=121)
    response = verify(client, code)
    assert response.status_code == 422 and current_user_id(client) is None


def test_code_still_valid_just_before_expiry(client, outbox, clock, make_user):
    make_user("staff", mobile=MOBILE)
    send_code(client)
    code = outbox.last_code(MOBILE)
    clock.advance(seconds=119)
    assert path_of(verify(client, code)) == "/dashboard"


@pytest.mark.parametrize("garbage", ["", "abc", "12", "1234567", "12 34", "‏"])
def test_malformed_codes_are_rejected(client, outbox, make_user, garbage):
    make_user("staff", mobile=MOBILE)
    send_code(client)
    assert verify(client, garbage).status_code == 422
    assert current_user_id(client) is None


def test_persian_and_arabic_digits_are_accepted_in_the_code(client, outbox, make_user):
    make_user("staff", mobile=MOBILE)
    send_code(client)
    code = outbox.last_code(MOBILE)
    persian = code.translate(str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹"))
    assert path_of(verify(client, f" {persian} ")) == "/dashboard"


def test_verify_without_a_started_login_goes_back_to_login(client):
    response = client.get("/auth/verify")
    assert response.status_code == 302 and path_of(response) == "/auth/login"
    assert client.post("/auth/verify", data={"code": "123456"}).status_code == 302


def test_flow_expires_after_fifteen_minutes(client, outbox, clock, make_user):
    make_user("staff", mobile=MOBILE)
    send_code(client)
    clock.advance(minutes=16)
    response = client.get("/auth/verify")
    assert response.status_code == 302 and path_of(response) == "/auth/login"


def test_constant_time_comparison_is_used(client, outbox, make_user, monkeypatch):
    import hmac

    calls = []
    real = hmac.compare_digest
    monkeypatch.setattr(hmac, "compare_digest", lambda a, b: calls.append(1) or real(a, b))
    make_user("staff", mobile=MOBILE)
    send_code(client)
    verify(client, "000000")
    assert calls, "OTP check must go through hmac.compare_digest"


# --------------------------------------------------------------------------
# mobile number handling
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "typed",
    ["09121234567", "+989121234567", "00989121234567", "9121234567", "۰۹۱۲۱۲۳۴۵۶۷", "٠٩١٢١٢٣٤٥٦٧", "0912 123 4567", "0912-123-4567"],
)
def test_iranian_mobile_formats_are_normalised(client, outbox, make_user, typed):
    user = make_user("staff", mobile=MOBILE)
    response = send_code(client, typed)
    assert response.status_code == 302, typed
    code = outbox.last_code(MOBILE)
    assert code
    assert path_of(verify(client, code)) == "/dashboard"
    assert current_user_id(client) == str(user.id)


@pytest.mark.parametrize("typed", ["", "0912", "12345678901", "08121234567", "abcdefghijk", "0912123456789"])
def test_invalid_mobile_is_a_form_error_and_sends_nothing(client, outbox, typed):
    response = send_code(client, typed)
    assert response.status_code == 422
    assert "نامعتبر" in response.get_data(as_text=True) or "وارد کنید" in response.get_data(as_text=True)
    assert outbox.outbox == []
    assert db.session.execute(sa.select(sa.func.count(OtpCode.id))).scalar_one() == 0


# --------------------------------------------------------------------------
# no account enumeration
# --------------------------------------------------------------------------


def _strip_dynamic(html):
    html = re.sub(r'(name="csrf-token" content=")[^"]*', r"\1", html)  # csrf meta
    html = re.sub(r'name="csrf_token" value="[^"]*"', 'name="csrf_token" value=""', html)
    return html


def test_login_responses_are_identical_for_known_and_unknown_numbers(app, make_user):
    make_user("staff", mobile=MOBILE)
    known, unknown = app.test_client(), app.test_client()
    a = known.post("/auth/login", data={"mobile": MOBILE})
    b = unknown.post("/auth/login", data={"mobile": "09129876543"})
    assert (a.status_code, a.headers["Location"]) == (b.status_code, b.headers["Location"])
    page_a = known.get("/auth/verify").get_data(as_text=True)
    page_b = unknown.get("/auth/verify").get_data(as_text=True)
    mask = lambda html, number: _strip_dynamic(html).replace(number.translate(str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")), "N")
    assert mask(page_a, MOBILE) == mask(page_b, "09129876543")


def test_wrong_code_responses_are_identical_for_known_and_unknown_numbers(app, make_user):
    make_user("staff", mobile=MOBILE)
    results = []
    for mobile in (MOBILE, "09129876543"):
        client = app.test_client()
        client.post("/auth/login", data={"mobile": mobile})
        response = client.post("/auth/verify", data={"code": "000000"})
        results.append((response.status_code, mask_number(_strip_dynamic(response.get_data(as_text=True)))))
    assert results[0] == results[1]


def mask_number(html):
    return re.sub(r"[۰-۹]{11}", "N", html)


def test_otp_is_sent_to_unknown_numbers_too(client, outbox):
    send_code(client, "09129876543")
    assert outbox.last_code("09129876543")


# --------------------------------------------------------------------------
# unknown number: login page continues into registration
# --------------------------------------------------------------------------


def test_unknown_number_proves_ownership_then_registers(client, outbox):
    mobile = "09129876543"
    done = sign_in(client, outbox, mobile)
    assert done.status_code == 302 and path_of(done) == "/auth/register"
    assert current_user_id(client) is None  # not signed in, no account yet
    assert db.session.execute(sa.select(User).where(User.mobile == mobile)).first() is None

    page = client.get("/auth/register")
    html = page.get_data(as_text=True)
    assert "تأیید شد" in html and 'name="mobile"' not in html  # the number is fixed now

    created = client.post("/auth/register", data={"full_name": "سارا کریمی", "role": "staff"})
    assert created.status_code == 302
    user = db.session.execute(sa.select(User).where(User.mobile == mobile)).scalar_one()
    assert user.full_name == "سارا کریمی" and user.role == Role.STAFF and user.status == UserStatus.ACTIVE
    assert current_user_id(client) == str(user.id)
    assert user.last_login_at is not None


def test_verified_number_cannot_be_swapped_in_the_register_form(client, outbox):
    sign_in(client, outbox, "09129876543")
    client.post("/auth/register", data={"full_name": "سارا کریمی", "role": "staff", "mobile": "09100000000"})
    assert db.session.execute(sa.select(User).where(User.mobile == "09100000000")).first() is None
    assert db.session.execute(sa.select(User).where(User.mobile == "09129876543")).first() is not None


# --------------------------------------------------------------------------
# registration
# --------------------------------------------------------------------------


def test_register_page_lists_the_three_self_service_roles(client):
    html = client.get("/auth/register").get_data(as_text=True)
    assert 'value="staff"' in html and 'value="hall_manager"' in html and 'value="atelier_manager"' in html
    assert 'value="admin"' not in html


@pytest.mark.parametrize("role", ["staff", "hall_manager", "atelier_manager"])
def test_registration_creates_the_chosen_role_after_verification(client, outbox, role):
    mobile = "09129876543"
    response = client.post("/auth/register", data={"full_name": "علی رضایی", "role": role, "mobile": mobile})
    assert response.status_code == 302 and path_of(response) == "/auth/verify"
    assert db.session.execute(sa.select(User).where(User.mobile == mobile)).first() is None  # not yet
    done = verify(client, outbox.last_code(mobile))
    assert done.status_code == 302
    user = db.session.execute(sa.select(User).where(User.mobile == mobile)).scalar_one()
    assert user.role.value == role and user.full_name == "علی رضایی"
    assert current_user_id(client) == str(user.id)
    assert db.session.execute(sa.select(AuditLog).where(AuditLog.action == "user.registered")).scalar_one().entity_id == user.id
    welcome = db.session.execute(sa.select(Notification).where(Notification.user_id == user.id)).scalars().all()
    assert len(welcome) == 1


def test_admin_cannot_be_chosen_when_registering(client, outbox):
    mobile = "09129876543"
    response = client.post("/auth/register", data={"full_name": "هکر", "role": "admin", "mobile": mobile})
    assert response.status_code == 422
    assert outbox.outbox == []  # rejected before any SMS
    assert db.session.execute(sa.select(User)).first() is None


def test_admin_role_cannot_be_forced_through_a_tampered_session(app, client, outbox):
    mobile = "09129876543"
    send_code(client, mobile)
    with client.session_transaction() as session:
        session["auth_flow"]["role"] = "admin"
        session["auth_flow"]["name"] = "هکر"
    # the session is signed, but even a server-side bug must not mint admins: config + code both refuse
    response = verify(client, outbox.last_code(mobile))
    assert response.status_code == 302 and path_of(response) == "/auth/register"
    assert db.session.execute(sa.select(User)).first() is None


@pytest.mark.parametrize(
    "fields,field",
    [
        ({"full_name": "", "role": "staff", "mobile": "09129876543"}, "full_name"),
        ({"full_name": "ا", "role": "staff", "mobile": "09129876543"}, "full_name"),
        ({"full_name": "x" * 121, "role": "staff", "mobile": "09129876543"}, "full_name"),
        ({"full_name": "علی", "role": "", "mobile": "09129876543"}, "role"),
        ({"full_name": "علی", "role": "wizard", "mobile": "09129876543"}, "role"),
        ({"full_name": "علی", "role": "staff", "mobile": "12"}, "mobile"),
    ],
)
def test_registration_validation(client, outbox, fields, field):
    response = client.post("/auth/register", data=fields)
    assert response.status_code == 422
    assert 'field--invalid' in response.get_data(as_text=True)
    assert outbox.outbox == []


def test_registering_with_an_existing_number_just_signs_that_user_in(client, outbox, make_user):
    existing = make_user("hall_manager", mobile=MOBILE, full_name="مدیر قدیمی")
    client.post("/auth/register", data={"full_name": "کس دیگر", "role": "staff", "mobile": MOBILE})
    done = verify(client, outbox.last_code(MOBILE))
    assert done.status_code == 302
    assert current_user_id(client) == str(existing.id)
    db.session.refresh(existing)
    assert existing.role == Role.HALL_MANAGER and existing.full_name == "مدیر قدیمی"  # nothing was overwritten
    assert db.session.execute(sa.select(sa.func.count(User.id))).scalar_one() == 1


def test_registration_can_be_closed_by_an_admin_setting(client, outbox):
    Setting.set("registration_open", False)
    db.session.commit()
    page = client.get("/auth/register")
    assert page.status_code == 403 and "موقتاً بسته" in page.get_data(as_text=True)
    assert client.post("/auth/register", data={"full_name": "علی", "role": "staff", "mobile": "09129876543"}).status_code == 403
    # existing users keep signing in; unknown numbers cannot create accounts
    send_code(client, "09129876543")
    done = verify(client, outbox.last_code("09129876543"))
    assert path_of(done) == "/auth/register"
    assert db.session.execute(sa.select(User)).first() is None


class TestApprovalRequired:
    @pytest.fixture
    def app_config(self):
        return {"REQUIRE_APPROVAL_ROLES": ("hall_manager", "atelier_manager")}

    def test_managers_start_pending_and_admins_are_told(self, client, outbox, make_user):
        admin = make_user("admin")
        client.post("/auth/register", data={"full_name": "مدیر تالار", "role": "hall_manager", "mobile": "09129876543"})
        verify(client, outbox.last_code("09129876543"))
        user = db.session.execute(sa.select(User).where(User.mobile == "09129876543")).scalar_one()
        assert user.status == UserStatus.PENDING
        titles = [n.title for n in db.session.execute(sa.select(Notification).where(Notification.user_id == admin.id)).scalars()]
        assert "درخواست تأیید حساب جدید" in titles

    def test_pending_user_lands_on_the_pending_page(self, client, outbox):
        client.post("/auth/register", data={"full_name": "مدیر", "role": "atelier_manager", "mobile": "09129876543"})
        verify(client, outbox.last_code("09129876543"))
        response = client.get("/dashboard")
        assert response.status_code == 302 and path_of(response) == "/auth/pending"
        page = client.get("/auth/pending")
        assert page.status_code == 200 and "در انتظار تأیید" in page.get_data(as_text=True)

    def test_staff_are_active_immediately(self, client, outbox):
        client.post("/auth/register", data={"full_name": "نیرو", "role": "staff", "mobile": "09129876543"})
        verify(client, outbox.last_code("09129876543"))
        user = db.session.execute(sa.select(User)).scalar_one()
        assert user.status == UserStatus.ACTIVE
        assert client.get("/dashboard").status_code == 200

    def test_register_form_warns_about_approval(self, client):
        html = client.get("/auth/register").get_data(as_text=True)
        assert html.count("پس از تأیید مدیر سامانه فعال می‌شود") == 2

    def test_approved_user_is_redirected_away_from_pending_page(self, client, login_as, make_user):
        login_as(client, make_user("staff"))
        assert path_of(client.get("/auth/pending")) == "/dashboard"


# --------------------------------------------------------------------------
# redirects after login
# --------------------------------------------------------------------------


def test_next_is_honoured_after_login(client, outbox, make_user):
    make_user("staff", mobile=MOBILE)
    page = client.get("/auth/login?next=/notifications")
    assert 'name="next" value="/notifications"' in page.get_data(as_text=True)
    send_code(client, next="/notifications")
    done = verify(client, outbox.last_code(MOBILE))
    assert path_of(done) == "/notifications"


@pytest.mark.parametrize(
    "evil",
    [
        "//evil.example/steal",
        "https://evil.example",
        "http://evil.example/x",
        "/\\evil.example",
        "\\\\evil.example",
        "javascript:alert(1)",
        "/\t/evil.example",
        " //evil.example",
        "/auth/login",
        "evil.example",
        "/ok\r\nSet-Cookie: x=1",
    ],
)
def test_unsafe_next_is_ignored(client, outbox, make_user, evil):
    make_user("staff", mobile=MOBILE)
    send_code(client, next=evil)
    done = verify(client, outbox.last_code(MOBILE))
    assert done.status_code == 302
    location = done.headers["Location"]
    assert path_of(done) == "/dashboard", location
    assert "evil" not in location


def test_signed_in_users_are_bounced_off_the_login_page(client, login_as, make_user):
    login_as(client, make_user("staff"))
    assert path_of(client.get("/auth/login")) == "/dashboard"
    assert path_of(client.get("/auth/register")) == "/dashboard"
    assert path_of(client.get("/auth/login?next=/notifications")) == "/notifications"


def test_new_accounts_go_to_onboarding_when_a_module_registers_one(app, client, outbox, empty_nav):
    from app.services import nav

    @app.get("/x/setup")
    def setup_page():
        return "setup"

    nav.register_onboarding("staff", "setup_page", lambda user: False)
    mobile = "09129876543"
    client.post("/auth/register", data={"full_name": "نیرو", "role": "staff", "mobile": mobile})
    done = verify(client, outbox.last_code(mobile))
    assert path_of(done) == "/x/setup"


def test_returning_users_do_not_get_the_onboarding_redirect(app, client, outbox, make_user, empty_nav):
    from app.services import nav

    @app.get("/x/setup")
    def setup_page():
        return "setup"

    nav.register_onboarding("staff", "setup_page", lambda user: False)
    make_user("staff", mobile=MOBILE)
    assert path_of(sign_in(client, outbox)) == "/dashboard"


# --------------------------------------------------------------------------
# suspended users
# --------------------------------------------------------------------------


def test_suspended_user_cannot_sign_in_with_a_code(client, outbox, make_user):
    make_user("staff", mobile=MOBILE, status="suspended")
    send_code(client)
    response = verify(client, outbox.last_code(MOBILE))
    assert response.status_code == 403
    assert "مسدود" in response.get_data(as_text=True)
    assert current_user_id(client) is None
    assert client.get("/dashboard").status_code == 302


def test_suspension_logs_out_an_existing_session(client, login_as, make_user):
    user = make_user("staff")
    login_as(client, user)
    assert client.get("/dashboard").status_code == 200
    user.status = UserStatus.SUSPENDED
    db.session.commit()
    response = client.get("/dashboard")
    assert response.status_code == 302 and path_of(response) == "/auth/login"


def test_suspended_users_are_told_nothing_before_proving_ownership(client, make_user):
    make_user("staff", mobile=MOBILE, status="suspended")
    response = send_code(client)
    assert response.status_code == 302 and path_of(response) == "/auth/verify"  # same as for anybody


# --------------------------------------------------------------------------
# sessions
# --------------------------------------------------------------------------


def test_login_clears_pre_login_session_data(client, outbox, make_user):
    make_user("staff", mobile=MOBILE)
    with client.session_transaction() as session:
        session["attacker_planted"] = "value"
        session["atelier_id"] = 99
    sign_in(client, outbox)
    with client.session_transaction() as session:
        assert "attacker_planted" not in session and "atelier_id" not in session
        assert "auth_flow" not in session
        assert session.permanent


def test_session_cookie_flags(client, outbox, make_user):
    make_user("staff", mobile=MOBILE)
    response = send_code(client)
    cookie = next(value for value in response.headers.getlist("Set-Cookie") if value.startswith("atelier_session="))
    assert "HttpOnly" in cookie and "SameSite=Lax" in cookie


class TestSecureCookies:
    @pytest.fixture
    def app_config(self):
        return {"SESSION_COOKIE_SECURE": True, "REMEMBER_COOKIE_SECURE": True}

    def test_secure_flag(self, client, make_user):
        response = client.post("/auth/login", data={"mobile": MOBILE}, base_url="https://localhost")
        cookie = next(value for value in response.headers.getlist("Set-Cookie") if value.startswith("atelier_session="))
        assert "Secure" in cookie


def test_remember_me_sets_a_long_lived_cookie(client, outbox, make_user):
    make_user("staff", mobile=MOBILE)
    send_code(client, remember="1")
    done = verify(client, outbox.last_code(MOBILE))
    cookies = done.headers.getlist("Set-Cookie")
    assert any(value.startswith("atelier_remember=") and "HttpOnly" in value for value in cookies)


def test_without_remember_me_there_is_no_remember_cookie(client, outbox, make_user):
    make_user("staff", mobile=MOBILE)
    done = sign_in(client, outbox)
    assert not any(value.startswith("atelier_remember=") for value in done.headers.getlist("Set-Cookie"))


# --------------------------------------------------------------------------
# logout
# --------------------------------------------------------------------------


def test_logout_post_ends_the_session(client, login_as, make_user):
    user = make_user("staff")
    login_as(client, user)
    response = client.post("/auth/logout")
    assert response.status_code == 302 and path_of(response) == "/"
    assert current_user_id(client) is None
    assert client.get("/dashboard").status_code == 302
    assert db.session.execute(sa.select(AuditLog).where(AuditLog.action == "auth.logout")).scalar_one().actor_id == user.id


def test_logout_also_forgets_the_remember_me_cookie(client, outbox, make_user):
    make_user("staff", mobile=MOBILE)
    send_code(client, remember="1")
    verify(client, outbox.last_code(MOBILE))
    with client.session_transaction() as session:
        session.clear()  # simulate a browser restart: only the long-lived cookie is left
    assert client.get("/dashboard").status_code == 200  # restored from the remember cookie
    client.post("/auth/logout")
    with client.session_transaction() as session:
        session.clear()
    assert client.get("/dashboard").status_code == 302  # the remember cookie is gone


def test_logging_in_without_remember_me_clears_a_stale_remember_cookie(app, outbox, make_user):
    """Shared phone: A (remembered) was suspended, B signs in without 'remember me', A is reactivated."""
    a = make_user("staff", mobile=MOBILE)
    make_user("staff", mobile="09127654321")
    client = app.test_client()
    send_code(client, remember="1")
    verify(client, outbox.last_code(MOBILE))
    a.status = UserStatus.SUSPENDED
    db.session.commit()
    assert client.get("/dashboard").status_code == 302  # A is out, but A's remember cookie is still in the jar
    send_code(client, "09127654321")
    assert verify(client, outbox.last_code("09127654321")).status_code == 302  # B, no "remember me"
    a.status = UserStatus.ACTIVE
    db.session.commit()
    with client.session_transaction() as session:
        session.clear()  # the phone is restarted: only long-lived cookies survive
    assert client.get("/dashboard").status_code == 302  # A is not silently signed back in


def test_logout_get_does_not_log_out(client, login_as, make_user):
    login_as(client, make_user("staff"))
    response = client.get("/auth/logout")
    assert response.status_code == 302
    assert client.get("/dashboard").status_code == 200


def test_logout_when_anonymous_is_harmless(client):
    assert client.post("/auth/logout").status_code == 302


def test_logout_button_is_in_the_shell_and_posts_with_csrf(client, login_as, make_user):
    login_as(client, make_user("staff"))
    html = client.get("/dashboard").get_data(as_text=True)
    assert re.search(r'<form method="post" action="/auth/logout">\s*<input type="hidden" name="csrf_token"', html)


# --------------------------------------------------------------------------
# account page
# --------------------------------------------------------------------------


def test_account_page_shows_the_profile(client, login_as, make_user):
    user = make_user("atelier_manager", full_name="مریم احمدی", mobile=MOBILE)
    login_as(client, user)
    html = client.get("/auth/account").get_data(as_text=True)
    assert "مریم احمدی" in html and "۰۹۱۲۱۲۳۴۵۶۷" in html and "مدیر اتلیه" in html
    assert "تعیین رمز عبور" in html


def test_account_requires_login(client):
    assert path_of(client.get("/auth/account")) == "/auth/login"
