"""Auth hardening: OTP rate limits (per mobile, per IP, cooldown), CSRF, SMS failures."""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from app.extensions import db, get_state
from app.models import OtpCode
from app.services import sms

MOBILE = "09121234567"


def post_login(client, mobile=MOBILE, ip=None, **fields):
    kwargs = {"environ_overrides": {"REMOTE_ADDR": ip}} if ip else {}
    return client.post("/auth/login", data={"mobile": mobile, **fields}, **kwargs)


def otp_count():
    return db.session.execute(sa.select(sa.func.count(OtpCode.id))).scalar_one()


# --------------------------------------------------------------------------
# cooldown
# --------------------------------------------------------------------------


def test_resend_cooldown_blocks_a_second_code(client, make_user, clock):
    make_user("staff", mobile=MOBILE)
    assert post_login(client).status_code == 302
    second = post_login(client)
    assert second.status_code == 429
    assert 0 < int(second.headers["Retry-After"]) <= 60
    assert "ثانیهٔ دیگر صبر کنید" in second.get_data(as_text=True)
    assert otp_count() == 1


def test_cooldown_ends_after_sixty_seconds(client, outbox, make_user, clock):
    make_user("staff", mobile=MOBILE)
    post_login(client)
    clock.advance(seconds=59)
    assert post_login(client).status_code == 429
    clock.advance(seconds=2)
    assert post_login(client).status_code == 302
    assert otp_count() == 2


def test_resend_button_obeys_the_same_cooldown(client, outbox, make_user, clock):
    make_user("staff", mobile=MOBILE)
    post_login(client)
    response = client.post("/auth/verify/resend", follow_redirects=True)
    assert "صبر کنید" in response.get_data(as_text=True)
    assert otp_count() == 1
    clock.advance(seconds=61)
    client.post("/auth/verify/resend")
    assert otp_count() == 2


def test_verify_page_shows_the_remaining_cooldown(client, make_user, clock):
    post_login(client)
    html = client.get("/auth/verify").get_data(as_text=True)
    assert 'data-resend-seconds="60"' in html and "disabled" in html
    clock.advance(seconds=61)
    html = client.get("/auth/verify").get_data(as_text=True)
    assert 'data-resend-seconds="0"' in html


# --------------------------------------------------------------------------
# per mobile / per IP caps
# --------------------------------------------------------------------------


class TestPerMobileCap:
    @pytest.fixture
    def app_config(self):
        return {"OTP_RESEND_COOLDOWN_SECONDS": 0, "OTP_MAX_PER_MOBILE_PER_HOUR": 3}

    def test_hourly_cap_per_number(self, client, clock):
        for _ in range(3):
            assert post_login(client).status_code == 302
            clock.advance(seconds=5)
        blocked = post_login(client)
        assert blocked.status_code == 429
        assert "دقیقهٔ دیگر" in blocked.get_data(as_text=True)
        assert int(blocked.headers["Retry-After"]) > 3000
        assert otp_count() == 3

    def test_window_slides(self, client, clock):
        for _ in range(3):
            post_login(client)
            clock.advance(minutes=1)
        assert post_login(client).status_code == 429
        clock.advance(minutes=58, seconds=30)  # the first code is now more than an hour old
        assert post_login(client).status_code == 302

    def test_other_numbers_are_unaffected(self, client, clock):
        for _ in range(3):
            post_login(client)
        assert post_login(client).status_code == 429
        assert post_login(client, mobile="09129876543").status_code == 302


class TestPerIpCap:
    @pytest.fixture
    def app_config(self):
        return {"OTP_RESEND_COOLDOWN_SECONDS": 0, "OTP_MAX_PER_IP_PER_HOUR": 4, "OTP_MAX_PER_MOBILE_PER_HOUR": 99}

    def test_one_ip_cannot_spray_many_numbers(self, client):
        for index in range(4):
            assert post_login(client, mobile=f"091200000{index:02d}", ip="10.1.1.1").status_code == 302
        blocked = post_login(client, mobile="09120000099", ip="10.1.1.1")
        assert blocked.status_code == 429
        assert otp_count() == 4

    def test_another_ip_is_not_blocked(self, client):
        for index in range(4):
            post_login(client, mobile=f"091200000{index:02d}", ip="10.1.1.1")
        assert post_login(client, mobile="09120000050", ip="10.2.2.2").status_code == 302

    def test_limit_is_independent_of_the_account_state(self, client, make_user):
        make_user("staff", mobile="09120000000")
        results = [post_login(client, mobile=f"091200000{i:02d}", ip="10.3.3.3").status_code for i in range(6)]
        assert results == [302, 302, 302, 302, 429, 429]


def test_rate_limit_message_does_not_depend_on_the_account(app, make_user):
    make_user("staff", mobile=MOBILE)
    messages = []
    for mobile in (MOBILE, "09129876543"):
        client = app.test_client()
        post_login(client, mobile=mobile)
        messages.append(post_login(client, mobile=mobile).get_data(as_text=True).count("ثانیهٔ دیگر صبر کنید"))
    assert messages[0] == messages[1] == 1


# --------------------------------------------------------------------------
# SMS gateway failure
# --------------------------------------------------------------------------


class BrokenProvider(sms.SmsProvider):
    name = "broken"

    def send(self, mobile, text):
        raise sms.SmsError("gateway timeout")


def test_sms_failure_is_reported_and_leaves_no_code_behind(app, client, make_user, caplog):
    get_state(app).sms = BrokenProvider()
    response = post_login(client)
    assert response.status_code == 503
    assert "ارسال پیامک انجام نشد" in response.get_data(as_text=True)
    assert otp_count() == 0  # nothing to verify, nothing to rate-limit against
    assert "gateway timeout" in caplog.text


def test_failed_send_keeps_the_previous_code_valid(app, client, outbox, make_user, clock):
    make_user("staff", mobile=MOBILE)
    post_login(client)
    code = outbox.last_code(MOBILE)
    clock.advance(seconds=61)
    original = get_state(app).sms
    get_state(app).sms = BrokenProvider()
    assert client.post("/auth/verify/resend").status_code == 302
    get_state(app).sms = original
    assert client.post("/auth/verify", data={"code": code}).status_code == 302
    assert client.get("/dashboard").status_code == 200


# --------------------------------------------------------------------------
# CSRF
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path,data",
    [
        ("/auth/login", {"mobile": MOBILE}),
        ("/auth/register", {"full_name": "علی", "role": "staff", "mobile": MOBILE}),
        ("/auth/verify", {"code": "123456"}),
        ("/auth/login/password", {"mobile": MOBILE, "password": "x"}),
        ("/auth/logout", {}),
    ],
)
def test_auth_posts_need_a_csrf_token(csrf_client, path, data):
    response = csrf_client.post(path, data=data)
    assert response.status_code == 400
    assert "اعتبار این فرم تمام شده" in response.get_data(as_text=True)


def test_login_with_a_csrf_token_works(csrf_app, csrf_client, get_csrf, make_user):
    make_user("staff", mobile=MOBILE)
    token = get_csrf(csrf_client)
    response = csrf_client.post("/auth/login", data={"mobile": MOBILE, "csrf_token": token})
    assert response.status_code == 302
    assert otp_count() == 1


def test_csrf_token_header_is_accepted_for_json_clients(csrf_client, get_csrf, make_user, login_as):
    user = make_user("staff")
    login_as(csrf_client, user)
    token = get_csrf(csrf_client, "/dashboard")
    denied = csrf_client.post("/notifications/read", headers={"Accept": "application/json"})
    assert denied.status_code == 400 and denied.get_json()["ok"] is False
    allowed = csrf_client.post("/notifications/read", headers={"Accept": "application/json", "X-CSRFToken": token})
    assert allowed.status_code == 200 and allowed.get_json()["ok"] is True


def test_csrf_failure_does_not_reveal_or_send_anything(csrf_client, outbox):
    csrf_client.post("/auth/login", data={"mobile": MOBILE})
    assert outbox.outbox == [] and otp_count() == 0
