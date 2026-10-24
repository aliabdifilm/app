"""SMS providers: console (dev/test) and Kavenegar (urllib, mocked at the opener)."""

from __future__ import annotations

import io
import json
import urllib.error
from urllib.parse import parse_qs

import pytest

from app.services import sms


class FakeResponse:
    def __init__(self, payload, status=200):
        self._body = json.dumps(payload).encode() if not isinstance(payload, bytes) else payload
        self.status = status

    def read(self, size=-1):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def __call__(self, request, timeout=None):
        self.calls.append((request, timeout))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


OK = {"return": {"status": 200, "message": "تایید شد"}, "entries": [{"messageid": 8792343, "cost": 120}]}


def sent_fields(opener):
    request, _ = opener.calls[0]
    return request.full_url, parse_qs(request.data.decode())


# --------------------------------------------------------------------------
# otp_text
# --------------------------------------------------------------------------


def test_otp_text_is_persian_and_has_the_code():
    text = sms.otp_text("123456", 2)
    assert "123456" in text and "کد ورود" in text and "۲ دقیقه" in text
    assert "@" not in text


def test_otp_text_adds_the_web_otp_line_when_a_domain_is_known():
    text = sms.otp_text("123456", 2, "atelier.example.ir")
    assert text.splitlines()[-1] == "@atelier.example.ir #123456"


# --------------------------------------------------------------------------
# console provider
# --------------------------------------------------------------------------


def test_console_provider_logs_and_keeps_messages(app, caplog):
    provider = sms.ConsoleSmsProvider()
    with caplog.at_level("DEBUG", logger="app"):
        result = provider.send("09121234567", "سلام\nدنیا")
    assert result.ok and result.provider == "console"
    assert provider.outbox[-1].text == "سلام\nدنیا" and provider.outbox[-1].code is None
    assert "0912***4567" in caplog.text and "09121234567" not in caplog.text


def test_console_otp_records_the_code_for_tests(app):
    provider = sms.ConsoleSmsProvider()
    provider.send_otp("09121234567", "654321")
    provider.send_otp("09127654321", "111111")
    assert provider.last_code() == "111111"
    assert provider.last_code("09121234567") == "654321"
    assert provider.last_code("۰۹۱۲۱۲۳۴۵۶۷") == "654321"
    assert provider.last_code("09120000000") is None


def test_console_provider_logs_the_code_only_in_debug_mode(app, caplog):
    provider = sms.ConsoleSmsProvider()
    with caplog.at_level("DEBUG", logger="app"):
        provider.send_otp("09121234567", "654321")
    assert "654321" not in caplog.text
    app.config["OTP_DEBUG_SHOW"] = True
    caplog.clear()
    with caplog.at_level("DEBUG", logger="app"):
        provider.send_otp("09121234567", "654321")
    assert "654321" in caplog.text


def test_console_outbox_is_capped():
    provider = sms.ConsoleSmsProvider(max_kept=3)
    for index in range(10):
        provider.send("09121234567", str(index))
    assert [m.text for m in provider.outbox] == ["7", "8", "9"]


# --------------------------------------------------------------------------
# kavenegar
# --------------------------------------------------------------------------


def make_provider(result, **kwargs):
    opener = FakeOpener(result)
    return sms.KavenegarProvider("SECRET-KEY-123", opener=opener, **kwargs), opener


def test_kavenegar_uses_the_verify_lookup_template_for_otps():
    provider, opener = make_provider(FakeResponse(OK), template="atelier-login")
    result = provider.send_otp("09121234567", "123456")
    url, fields = sent_fields(opener)
    assert url == "https://api.kavenegar.com/v1/SECRET-KEY-123/verify/lookup.json"
    assert fields == {"receptor": ["09121234567"], "token": ["123456"], "template": ["atelier-login"]}
    assert result.ok and result.message_id == "8792343" and result.provider == "kavenegar"


def test_kavenegar_without_template_sends_plain_sms_with_the_sender_line():
    provider, opener = make_provider(FakeResponse(OK), sender="10004346")
    provider.send_otp("09121234567", "123456", ttl_minutes=2, site_domain="atelier.example.ir")
    url, fields = sent_fields(opener)
    assert url.endswith("/sms/send.json")
    assert fields["receptor"] == ["09121234567"] and fields["sender"] == ["10004346"]
    assert "123456" in fields["message"][0] and "@atelier.example.ir #123456" in fields["message"][0]


def test_kavenegar_request_is_a_post_with_a_timeout():
    provider, opener = make_provider(FakeResponse(OK), template="t", timeout=3)
    provider.send_otp("09121234567", "123456")
    request, timeout = opener.calls[0]
    assert request.get_method() == "POST" and timeout == 3
    assert request.get_header("Content-type").startswith("application/x-www-form-urlencoded")


@pytest.mark.parametrize(
    "result,fragment",
    [
        (FakeResponse({"return": {"status": 418, "message": "نامعتبر"}}), "status 418"),
        (FakeResponse(b"<html>gateway</html>"), "unreadable"),
        (FakeResponse({"unexpected": True}), "status None"),
        (urllib.error.URLError(TimeoutError("timed out")), "unreachable"),
        (TimeoutError("slow"), "unreachable"),
        (ConnectionResetError("reset"), "unreachable"),
        (urllib.error.HTTPError("https://x", 500, "boom", {}, io.BytesIO(b'{"return":{"message":"internal"}}')), "http 500"),
    ],
)
def test_kavenegar_errors_become_smserror_without_leaking_the_key(result, fragment):
    provider, _ = make_provider(result, template="t")
    with pytest.raises(sms.SmsError) as info:
        provider.send_otp("09121234567", "123456")
    assert fragment in str(info.value)
    assert "SECRET-KEY-123" not in str(info.value)
    assert "SECRET-KEY-123" not in repr(info.value.__cause__)


def test_kavenegar_needs_a_key():
    with pytest.raises(ValueError):
        sms.KavenegarProvider("")


# --------------------------------------------------------------------------
# factory + facade
# --------------------------------------------------------------------------


def test_build_provider_by_name():
    assert isinstance(sms.build_provider({"SMS_PROVIDER": "console"}), sms.ConsoleSmsProvider)
    provider = sms.build_provider(
        {"SMS_PROVIDER": "kavenegar", "KAVENEGAR_API_KEY": "k", "KAVENEGAR_OTP_TEMPLATE": "t", "SMS_TIMEOUT_SECONDS": 2}
    )
    assert isinstance(provider, sms.KavenegarProvider) and provider.template == "t" and provider.timeout == 2.0
    with pytest.raises(ValueError):
        sms.build_provider({"SMS_PROVIDER": "pigeon"})


def test_send_otp_facade_uses_the_app_provider(app, outbox):
    sms.send_otp("+989121234567", "424242")
    assert outbox.last_code("09121234567") == "424242"
    assert "۲ دقیقه" in outbox.outbox[-1].text


def test_app_with_kavenegar_config_builds_that_provider(tmp_path):
    from app import create_app
    from app.extensions import get_state

    app = create_app("testing", UPLOAD_DIR=str(tmp_path), SMS_PROVIDER="kavenegar", KAVENEGAR_API_KEY="k")
    assert isinstance(get_state(app).sms, sms.KavenegarProvider)
