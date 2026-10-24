"""SMS delivery for login codes (assumption A6: the provider is swappable).

``SMS_PROVIDER`` picks the implementation:

``console`` (default)
    :class:`ConsoleSmsProvider` writes the message to the log and keeps it in
    ``provider.outbox``.  Nothing leaves the machine.  With ``OTP_DEBUG_SHOW``
    (development only) the verify page also shows the code.
``kavenegar``
    :class:`KavenegarProvider` - the Iranian gateway, plain ``urllib`` with a
    timeout (no extra dependency).  With ``KAVENEGAR_OTP_TEMPLATE`` it uses the
    "verify/lookup" endpoint (the route SMS gateways expect for OTPs and the
    one that is delivered fastest); otherwise ``sms/send`` with
    ``KAVENEGAR_SENDER``.

To add another gateway implement :class:`SmsProvider` and return it from
:func:`get_provider` (or assign ``get_state().sms`` in tests).

Errors never leak secrets: :class:`SmsError` messages are safe to log and the
API key never appears in them.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from flask import Flask, current_app

from app.extensions import get_state
from app.models.base import normalize_mobile
from app.services.security import mask_mobile
from app.services.money import to_persian_digits

logger = logging.getLogger(__name__)

KAVENEGAR_BASE = "https://api.kavenegar.com/v1"


class SmsError(RuntimeError):
    """The gateway refused or could not be reached (message is safe to log)."""


@dataclass(frozen=True)
class SmsResult:
    ok: bool
    provider: str
    message_id: Optional[str] = None
    detail: str = ""


@dataclass
class SentMessage:
    mobile: str
    text: str
    code: Optional[str] = None


class SmsProvider:
    """Interface: send plain text, or an OTP (providers may use a template for OTPs)."""

    name = "base"

    def send(self, mobile: str, text: str) -> SmsResult:  # pragma: no cover - interface
        raise NotImplementedError

    def send_otp(self, mobile: str, code: str, *, ttl_minutes: int = 2, site_domain: Optional[str] = None) -> SmsResult:
        return self.send(mobile, otp_text(code, ttl_minutes, site_domain))


def otp_text(code: str, ttl_minutes: int = 2, site_domain: Optional[str] = None) -> str:
    """Persian OTP message.  The last line follows the Web OTP convention (``@domain #code``)."""
    text = f"کد ورود به اتلیه سیار: {code}\nاعتبار کد: {to_persian_digits(ttl_minutes)} دقیقه"
    if site_domain:
        text += f"\n\n@{site_domain} #{code}"
    return text


class ConsoleSmsProvider(SmsProvider):
    """Development / test provider: logs the message and keeps it in memory."""

    name = "console"

    def __init__(self, max_kept: int = 200) -> None:
        self.outbox: list[SentMessage] = []
        self._max = max_kept

    def _keep(self, message: SentMessage) -> None:
        self.outbox.append(message)
        del self.outbox[: -self._max]

    def send(self, mobile: str, text: str) -> SmsResult:
        logger.info("[sms:console] to %s: %s", mask_mobile(mobile), text.replace("\n", " | "))
        self._keep(SentMessage(mobile, text))
        return SmsResult(True, self.name, detail="logged")

    def send_otp(self, mobile: str, code: str, *, ttl_minutes: int = 2, site_domain: Optional[str] = None) -> SmsResult:
        text = otp_text(code, ttl_minutes, site_domain)
        # The code itself is logged in development only: the log is the "phone" of a dev machine.
        if current_app.config.get("DEBUG") or current_app.config.get("OTP_DEBUG_SHOW"):
            logger.info("[sms:console] OTP for %s is %s", mask_mobile(mobile), code)
        else:
            logger.info("[sms:console] OTP sent to %s", mask_mobile(mobile))
        self._keep(SentMessage(mobile, text, code=code))
        return SmsResult(True, self.name, detail="logged")

    def last_code(self, mobile: Optional[str] = None) -> Optional[str]:
        """Most recent OTP sent (to *mobile* if given) - for tests and the dev banner."""
        wanted = normalize_mobile(mobile) if mobile else None
        for message in reversed(self.outbox):
            if message.code and (wanted is None or message.mobile == wanted):
                return message.code
        return None


class KavenegarProvider(SmsProvider):
    """Kavenegar REST API over ``urllib`` (https, short timeout, no retries)."""

    name = "kavenegar"

    def __init__(
        self,
        api_key: str,
        *,
        template: Optional[str] = None,
        sender: Optional[str] = None,
        timeout: float = 5.0,
        base_url: str = KAVENEGAR_BASE,
        opener: Any = None,
    ) -> None:
        if not api_key:
            raise ValueError("Kavenegar needs an api key")
        self._api_key = api_key
        self.template = template
        self.sender = sender
        self.timeout = timeout
        self.base_url = base_url.rstrip("/")
        self._open = opener or urllib.request.urlopen  # replaced in tests

    # -- low level ---------------------------------------------------------------

    def _call(self, path: str, params: Mapping[str, str]) -> Mapping[str, Any]:
        url = f"{self.base_url}/{self._api_key}/{path}.json"
        body = urllib.parse.urlencode(params).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
                "Accept": "application/json",
            },
        )
        try:
            with self._open(request, timeout=self.timeout) as response:
                raw = response.read(65536)
        except urllib.error.HTTPError as exc:  # gateway answered with an error status
            detail = _error_detail(exc.read(65536) if hasattr(exc, "read") else b"")
            raise SmsError(f"kavenegar http {exc.code}{': ' + detail if detail else ''}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise SmsError(f"kavenegar unreachable ({type(reason).__name__})") from None
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise SmsError("kavenegar returned an unreadable response") from None
        status = (payload.get("return") or {}).get("status") if isinstance(payload, dict) else None
        if status != 200:
            message = (payload.get("return") or {}).get("message", "") if isinstance(payload, dict) else ""
            raise SmsError(f"kavenegar refused the request (status {status}){': ' + str(message) if message else ''}")
        return payload

    @staticmethod
    def _message_id(payload: Mapping[str, Any]) -> Optional[str]:
        entries = payload.get("entries")
        if isinstance(entries, list) and entries and isinstance(entries[0], dict):
            value = entries[0].get("messageid")
            return str(value) if value is not None else None
        return None

    # -- SmsProvider ---------------------------------------------------------------

    def send(self, mobile: str, text: str) -> SmsResult:
        params = {"receptor": mobile, "message": text}
        if self.sender:
            params["sender"] = self.sender
        payload = self._call("sms/send", params)
        return SmsResult(True, self.name, self._message_id(payload))

    def send_otp(self, mobile: str, code: str, *, ttl_minutes: int = 2, site_domain: Optional[str] = None) -> SmsResult:
        if not self.template:
            return self.send(mobile, otp_text(code, ttl_minutes, site_domain))
        payload = self._call(
            "verify/lookup",
            {"receptor": mobile, "token": code, "template": self.template},
        )
        return SmsResult(True, self.name, self._message_id(payload))


def _error_detail(raw: bytes) -> str:
    try:
        payload = json.loads(raw.decode("utf-8"))
        return str((payload.get("return") or {}).get("message", ""))[:200]
    except (ValueError, UnicodeDecodeError, AttributeError):
        return ""


# --------------------------------------------------------------------------
# factory
# --------------------------------------------------------------------------


def build_provider(config: Mapping[str, Any]) -> SmsProvider:
    name = str(config.get("SMS_PROVIDER", "console")).lower()
    if name == "console":
        return ConsoleSmsProvider()
    if name == "kavenegar":
        return KavenegarProvider(
            config["KAVENEGAR_API_KEY"],
            template=config.get("KAVENEGAR_OTP_TEMPLATE"),
            sender=config.get("KAVENEGAR_SENDER"),
            timeout=float(config.get("SMS_TIMEOUT_SECONDS", 5)),
        )
    raise ValueError(f"unknown SMS provider {name!r}")


def init_app(app: Flask) -> None:
    get_state(app).sms = build_provider(app.config)


def get_provider() -> SmsProvider:
    """The current app's provider (built lazily from the config)."""
    state = get_state()
    if state.sms is None:
        state.sms = build_provider(current_app.config)
    return state.sms


def send_otp(mobile: str, code: str) -> SmsResult:
    """Deliver a login code with the configured provider.  Raises :class:`SmsError`."""
    config = current_app.config
    ttl_minutes = max(1, round(int(config.get("OTP_TTL_SECONDS", 120)) / 60))
    return get_provider().send_otp(
        normalize_mobile(mobile),
        code,
        ttl_minutes=ttl_minutes,
        site_domain=config.get("SITE_DOMAIN"),
    )
