"""Optional password sign-in, setting / changing a password, throttling."""

from __future__ import annotations

from urllib.parse import urlsplit

import pytest
import sqlalchemy as sa

from app.extensions import db
from app.models import AuditLog

MOBILE = "09121234567"
PASSWORD = "Sup3r-secret-pass"
GENERIC = "شمارهٔ موبایل یا رمز عبور نادرست است."


def path_of(response):
    return urlsplit(response.headers["Location"]).path


def pw_login(client, mobile=MOBILE, password=PASSWORD, **fields):
    return client.post("/auth/login/password", data={"mobile": mobile, "password": password, **fields})


def user_id(client):
    with client.session_transaction() as session:
        return session.get("_user_id")


# --------------------------------------------------------------------------
# signing in
# --------------------------------------------------------------------------


def test_password_login_page(client):
    html = client.get("/auth/login/password").get_data(as_text=True)
    assert 'name="password"' in html and 'type="password"' in html and 'autocomplete="current-password"' in html


def test_password_login_works(client, make_user):
    user = make_user("hall_manager", mobile=MOBILE, password=PASSWORD)
    response = pw_login(client)
    assert response.status_code == 302 and path_of(response) == "/dashboard"
    assert user_id(client) == str(user.id)
    db.session.refresh(user)
    assert user.last_login_at is not None
    row = db.session.execute(sa.select(AuditLog).where(AuditLog.action == "auth.login")).scalar_one()
    assert row.data["method"] == "password"


def test_password_login_accepts_any_mobile_format_and_next(client, make_user):
    make_user("staff", mobile=MOBILE, password=PASSWORD)
    response = client.post(
        "/auth/login/password", data={"mobile": "۰۹۱۲۱۲۳۴۵۶۷", "password": PASSWORD, "next": "/notifications"}
    )
    assert path_of(response) == "/notifications"


@pytest.mark.parametrize("next_url", ["//evil.example", "https://evil.example", "/\\evil.example"])
def test_password_login_ignores_unsafe_next(client, make_user, next_url):
    make_user("staff", mobile=MOBILE, password=PASSWORD)
    response = pw_login(client, next=next_url)
    assert path_of(response) == "/dashboard" and "evil" not in response.headers["Location"]


def test_wrong_password_unknown_user_and_no_password_look_the_same(app, make_user):
    make_user("staff", mobile=MOBILE, password=PASSWORD)
    make_user("staff", mobile="09127654321")  # has no password at all
    seen = set()
    for mobile, password in [(MOBILE, "wrong-password"), ("09129999999", PASSWORD), ("09127654321", PASSWORD)]:
        client = app.test_client()
        response = pw_login(client, mobile, password)
        assert response.status_code == 422
        assert GENERIC in response.get_data(as_text=True)
        assert user_id(client) is None
        seen.add(response.status_code)
    assert seen == {422}


def test_missing_fields_are_form_errors(client):
    response = client.post("/auth/login/password", data={"mobile": "", "password": ""})
    assert response.status_code == 422
    html = response.get_data(as_text=True)
    assert "field--invalid" in html


def test_failed_attempts_are_audited_without_the_password(client, make_user):
    make_user("staff", mobile=MOBILE, password=PASSWORD)
    pw_login(client, password="Wrong-guess-123")
    rows = db.session.execute(sa.select(AuditLog).where(AuditLog.action == "auth.login_failed")).scalars().all()
    assert len(rows) == 1
    assert "Wrong-guess-123" not in str(rows[0].data) and MOBILE not in str(rows[0].data)


def test_suspended_user_with_the_right_password_is_refused(client, make_user):
    make_user("staff", mobile=MOBILE, password=PASSWORD, status="suspended")
    response = pw_login(client)
    assert response.status_code == 403 and "مسدود" in response.get_data(as_text=True)
    assert user_id(client) is None


def test_suspended_user_with_a_wrong_password_just_sees_the_generic_error(client, make_user):
    make_user("staff", mobile=MOBILE, password=PASSWORD, status="suspended")
    response = pw_login(client, password="nope-nope-nope")
    assert response.status_code == 422 and "مسدود" not in response.get_data(as_text=True)


def test_password_login_can_be_switched_off(tmp_path):
    from app import create_app

    app = create_app("testing", UPLOAD_DIR=str(tmp_path), PASSWORD_LOGIN_ENABLED=False)
    client = app.test_client()
    assert path_of(client.get("/auth/login/password")) == "/auth/login"
    assert "ورود با رمز عبور" not in client.get("/auth/login").get_data(as_text=True)


# --------------------------------------------------------------------------
# throttling
# --------------------------------------------------------------------------


class TestThrottle:
    @pytest.fixture
    def app_config(self):
        return {"PASSWORD_MAX_FAILURES": 3, "PASSWORD_MAX_FAILURES_PER_IP": 5}

    def test_account_is_throttled_after_repeated_failures(self, client, make_user, clock):
        make_user("staff", mobile=MOBILE, password=PASSWORD)
        for _ in range(3):
            assert pw_login(client, password="bad-bad-bad1").status_code == 422
        blocked = pw_login(client)  # even the right password is refused now
        assert blocked.status_code == 429
        assert int(blocked.headers["Retry-After"]) > 0
        assert "دقیقهٔ دیگر" in blocked.get_data(as_text=True)
        assert user_id(client) is None

    def test_throttle_lifts_after_the_window(self, client, make_user, clock):
        make_user("staff", mobile=MOBILE, password=PASSWORD)
        for _ in range(3):
            pw_login(client, password="bad-bad-bad1")
        assert pw_login(client).status_code == 429
        clock.advance(minutes=16)
        assert pw_login(client).status_code == 302

    def test_success_resets_the_counter(self, app, make_user):
        make_user("staff", mobile=MOBILE, password=PASSWORD)
        client = app.test_client()
        for _ in range(2):
            pw_login(client, password="bad-bad-bad1")
        assert pw_login(client).status_code == 302
        client.post("/auth/logout")
        for _ in range(2):
            assert pw_login(client, password="bad-bad-bad1").status_code == 422

    def test_one_ip_guessing_many_accounts_is_throttled(self, app, make_user):
        make_user("staff", mobile=MOBILE, password=PASSWORD)
        client = app.test_client()
        statuses = [pw_login(client, mobile=f"091200000{i:02d}", password="bad-bad-bad1").status_code for i in range(7)]
        assert statuses[:5] == [422] * 5 and statuses[5:] == [429, 429]
        assert pw_login(client).status_code == 429  # the real account is cut off too, from this IP

    def test_other_ips_are_not_affected(self, app, make_user):
        make_user("staff", mobile=MOBILE, password=PASSWORD)
        client = app.test_client()
        for _ in range(3):
            client.post("/auth/login/password", data={"mobile": MOBILE, "password": "bad-bad-bad1"},
                        environ_overrides={"REMOTE_ADDR": "10.9.9.9"})
        ok = client.post("/auth/login/password", data={"mobile": MOBILE, "password": PASSWORD},
                         environ_overrides={"REMOTE_ADDR": "10.8.8.8"})
        assert ok.status_code == 302


# --------------------------------------------------------------------------
# setting and changing a password
# --------------------------------------------------------------------------


def test_password_pages_require_login(client):
    assert path_of(client.get("/auth/password")) == "/auth/login"
    assert path_of(client.post("/auth/password", data={"new_password": PASSWORD})) == "/auth/login"
    assert client.post("/auth/password/code").status_code == 302


def test_first_password_needs_a_fresh_code(client, outbox, make_user, login_as):
    user = make_user("staff", mobile=MOBILE)
    login_as(client, user)

    page = client.get("/auth/password")
    assert "ابتدا شمارهٔ خود را تأیید کنید" in page.get_data(as_text=True)
    assert 'name="new_password"' not in page.get_data(as_text=True)

    # trying to skip the code step fails and sets nothing
    skipped = client.post("/auth/password", data={"new_password": PASSWORD, "confirm_password": PASSWORD})
    assert skipped.status_code == 422
    db.session.refresh(user)
    assert user.password_hash is None

    sent = client.post("/auth/password/code")
    assert sent.status_code == 302 and path_of(sent) == "/auth/verify"
    done = client.post("/auth/verify", data={"code": outbox.last_code(MOBILE)})
    assert path_of(done) == "/auth/password"

    form = client.get("/auth/password").get_data(as_text=True)
    assert 'name="new_password"' in form and 'name="current_password"' not in form
    saved = client.post("/auth/password", data={"new_password": PASSWORD, "confirm_password": PASSWORD})
    assert saved.status_code == 302 and path_of(saved) == "/auth/account"
    db.session.refresh(user)
    assert user.password_hash and user.password_hash != PASSWORD and user.check_password(PASSWORD)
    assert db.session.execute(sa.select(AuditLog).where(AuditLog.action == "auth.password_changed")).scalar_one().actor_id == user.id

    # the reauthorisation is single use
    again = client.post("/auth/password", data={"current_password": "x", "new_password": PASSWORD + "2", "confirm_password": PASSWORD + "2"})
    assert again.status_code == 422


def test_password_code_flow_is_bound_to_my_own_number(client, outbox, make_user, login_as):
    me = make_user("staff", mobile=MOBILE)
    login_as(client, me)
    client.post("/auth/password/code")
    with client.session_transaction() as session:
        session["auth_flow"]["m"] = "09129999999"  # tampering is pointless: no code exists for that number
    assert client.post("/auth/verify", data={"code": outbox.last_code(MOBILE)}).status_code == 422


def test_password_flow_requires_the_signed_in_user(app, outbox, make_user, login_as):
    me = make_user("staff", mobile=MOBILE)
    owner = app.test_client()
    login_as(owner, me)
    owner.post("/auth/password/code")
    stranger = app.test_client()
    with stranger.session_transaction() as session:
        session["auth_flow"] = {"m": MOBILE, "p": "password", "at": 9e12, "next": "", "remember": False}
    response = stranger.post("/auth/verify", data={"code": outbox.last_code(MOBILE)})
    assert path_of(response) == "/auth/login"
    assert user_id(stranger) is None


def test_change_password_needs_the_current_one(client, make_user, login_as):
    user = make_user("staff", mobile=MOBILE, password=PASSWORD)
    login_as(client, user)
    page = client.get("/auth/password").get_data(as_text=True)
    assert 'name="current_password"' in page
    wrong = client.post("/auth/password", data={"current_password": "wrong-wrong-1", "new_password": "An0ther-pass-9", "confirm_password": "An0ther-pass-9"})
    assert wrong.status_code == 422 and "فعلی نادرست" in wrong.get_data(as_text=True)
    ok = client.post("/auth/password", data={"current_password": PASSWORD, "new_password": "An0ther-pass-9", "confirm_password": "An0ther-pass-9"})
    assert ok.status_code == 302
    db.session.refresh(user)
    assert user.check_password("An0ther-pass-9") and not user.check_password(PASSWORD)


@pytest.mark.parametrize(
    "new,confirm,expected",
    [
        ("short1", "short1", "دست‌کم"),
        ("12345678", "12345678", "فقط عدد"),
        ("password", "password", "رایج"),
        ("Sup3r-secret-pass", "different-pass-1", "یکی نیست"),
        ("x09121234567x", "x09121234567x", "شمارهٔ موبایل"),
        ("Sup3r-secret-pass", "Sup3r-secret-pass", "فرق داشته باشد"),
        (" leading-space-1", " leading-space-1", "فاصله"),
    ],
)
def test_password_rules(client, make_user, login_as, new, confirm, expected):
    user = make_user("staff", mobile=MOBILE, password=PASSWORD)
    login_as(client, user)
    response = client.post("/auth/password", data={"current_password": PASSWORD, "new_password": new, "confirm_password": confirm})
    assert response.status_code == 422
    assert expected in response.get_data(as_text=True)
    db.session.refresh(user)
    assert user.check_password(PASSWORD)


def test_change_password_attempts_are_throttled(app, make_user, login_as):
    user = make_user("staff", mobile=MOBILE, password=PASSWORD)
    client = login_as(app.test_client(), user)
    for _ in range(5):
        client.post("/auth/password", data={"current_password": "bad-bad-bad1", "new_password": "An0ther-pass-9", "confirm_password": "An0ther-pass-9"})
    response = client.post("/auth/password", data={"current_password": PASSWORD, "new_password": "An0ther-pass-9", "confirm_password": "An0ther-pass-9"})
    assert response.status_code == 422 and "تلاش‌های ناموفق" in response.get_data(as_text=True)
    db.session.refresh(user)
    assert user.check_password(PASSWORD)


def test_password_is_set_only_for_the_signed_in_user(app, make_user, login_as):
    me, other = make_user("staff", password=PASSWORD), make_user("staff", password=PASSWORD)
    client = login_as(app.test_client(), me)
    client.post("/auth/password", data={"current_password": PASSWORD, "new_password": "An0ther-pass-9", "confirm_password": "An0ther-pass-9", "user_id": other.id})
    db.session.refresh(other)
    assert other.check_password(PASSWORD)


def test_set_password_then_use_it_to_sign_in(app, outbox, make_user, login_as):
    user = make_user("staff", mobile=MOBILE)
    client = login_as(app.test_client(), user)
    client.post("/auth/password/code")
    client.post("/auth/verify", data={"code": outbox.last_code(MOBILE)})
    client.post("/auth/password", data={"new_password": PASSWORD, "confirm_password": PASSWORD})
    fresh = app.test_client()
    assert pw_login(fresh).status_code == 302
    assert user_id(fresh) == str(user.id)
