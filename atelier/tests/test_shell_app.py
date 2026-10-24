"""App shell: factory, config, headers, public pages, error handling, filters."""

from __future__ import annotations

import datetime as dt
import json

import pytest

from app import create_app
from app.config import ConfigError, load_config
from app.extensions import get_state


# --------------------------------------------------------------------------
# factory + config
# --------------------------------------------------------------------------


def test_create_app_testing_defaults(app):
    assert app.config["TESTING"] is True
    assert app.config["WTF_CSRF_ENABLED"] is False
    assert app.config["SQLALCHEMY_DATABASE_URI"] in ("sqlite://",) or "postgresql" in app.config["SQLALCHEMY_DATABASE_URI"]
    assert app.config["SMS_PROVIDER"] == "console"
    assert app.config["OTP_DEBUG_SHOW"] is False
    assert app.json.ensure_ascii is False


def test_create_app_can_be_called_repeatedly(tmp_path):
    first = create_app("testing", UPLOAD_DIR=str(tmp_path / "a"))
    second = create_app("testing", UPLOAD_DIR=str(tmp_path / "b"))
    assert first is not second
    assert get_state(first).rate_limiter is not get_state(second).rate_limiter
    assert first.test_client().get("/healthz").status_code == 200
    assert second.test_client().get("/healthz").status_code == 200


def test_overrides_via_kwargs(tmp_path):
    app = create_app("testing", PLATFORM_NAME="اتلیه آزمایشی", UPLOAD_DIR=str(tmp_path))
    assert app.config["PLATFORM_NAME"] == "اتلیه آزمایشی"


def test_a_mapping_is_treated_as_overrides_on_the_environment_config(tmp_path):
    cfg = load_config({"PLATFORM_NAME": "الف", "OTP_TTL_SECONDS": 30}, {"PLATFORM_NAME": "ب"}, instance_path=tmp_path, environ={"ATELIER_ENV": "testing"})
    assert cfg["TESTING"] is True and cfg["PLATFORM_NAME"] == "ب" and cfg["OTP_TTL_SECONDS"] == 30


def test_testing_environment_ignores_ambient_variables(tmp_path):
    cfg = load_config("testing", {}, instance_path=tmp_path, environ={"SMS_PROVIDER": "kavenegar", "DATABASE_URL": "postgresql://x/y"})
    assert cfg["SMS_PROVIDER"] == "console" and cfg["SQLALCHEMY_DATABASE_URI"] == "sqlite://"


def test_creating_a_testing_app_leaves_no_folders_behind():
    import pathlib

    instance = pathlib.Path(__file__).resolve().parent.parent / "instance"
    if instance.exists():
        pytest.skip("instance/ already exists (created by something else); nothing to assert")
    app = create_app("testing")
    assert pathlib.Path(app.instance_path) == instance
    assert not instance.exists()  # in-memory database, lazy upload folder: no side effects on disk


def test_production_requires_a_real_secret_key(tmp_path):
    with pytest.raises(ConfigError, match="SECRET_KEY"):
        load_config("production", {}, instance_path=tmp_path, environ={})
    with pytest.raises(ConfigError, match="SECRET_KEY"):
        load_config("production", {}, instance_path=tmp_path, environ={"SECRET_KEY": "change-me"})
    with pytest.raises(ConfigError, match="SECRET_KEY"):
        load_config("production", {}, instance_path=tmp_path, environ={"SECRET_KEY": "short"})


def test_production_settings(tmp_path):
    env = {"SECRET_KEY": "x" * 40, "DATABASE_URL": "postgres://u:p@db/atelier"}
    cfg = load_config("production", {}, instance_path=tmp_path, environ=env)
    assert cfg["SESSION_COOKIE_SECURE"] is True
    assert cfg["REMEMBER_COOKIE_SECURE"] is True
    assert cfg["SESSION_COOKIE_HTTPONLY"] is True
    assert cfg["SESSION_COOKIE_SAMESITE"] == "Lax"
    assert cfg["SQLALCHEMY_DATABASE_URI"].startswith("postgresql://")
    assert cfg["OTP_DEBUG_SHOW"] is False
    assert cfg["DEBUG"] is False
    assert cfg["HSTS_SECONDS"] > 0
    assert cfg["AUTO_CREATE_DB"] is False


def test_production_refuses_otp_debug_show(tmp_path):
    env = {"SECRET_KEY": "x" * 40, "OTP_DEBUG_SHOW": "1"}
    with pytest.raises(ConfigError, match="OTP_DEBUG_SHOW"):
        load_config("production", {}, instance_path=tmp_path, environ=env)


def test_development_defaults_and_remembered_secret(tmp_path):
    first = load_config("development", {}, instance_path=tmp_path, environ={})
    second = load_config("development", {}, instance_path=tmp_path, environ={})
    assert first["OTP_DEBUG_SHOW"] is True
    assert first["SQLALCHEMY_DATABASE_URI"].endswith("atelier.db")
    assert len(first["SECRET_KEY"]) >= 32
    assert first["SECRET_KEY"] == second["SECRET_KEY"]  # kept in instance/secret_key
    assert (tmp_path / "secret_key").exists()


def test_environment_variables_are_read(tmp_path):
    env = {
        "SECRET_KEY": "k" * 40,
        "ATELIER_ENV": "development",
        "SMS_PROVIDER": "kavenegar",
        "KAVENEGAR_API_KEY": "abc",
        "UPLOAD_DIR": str(tmp_path / "up"),
        "REQUIRE_APPROVAL_ROLES": "hall_manager, atelier_manager",
        "MAX_CONTENT_LENGTH": "1048576",
    }
    cfg = load_config(None, {}, instance_path=tmp_path, environ=env)
    assert cfg["SMS_PROVIDER"] == "kavenegar"
    assert cfg["REQUIRE_APPROVAL_ROLES"] == ("hall_manager", "atelier_manager")
    assert cfg["MAX_CONTENT_LENGTH"] == 1048576
    assert cfg["UPLOAD_DIR"].endswith("up")


def test_kavenegar_needs_api_key(tmp_path):
    with pytest.raises(ConfigError, match="KAVENEGAR_API_KEY"):
        load_config("development", {}, instance_path=tmp_path, environ={"SMS_PROVIDER": "kavenegar"})
    with pytest.raises(ConfigError, match="SMS_PROVIDER"):
        load_config("development", {}, instance_path=tmp_path, environ={"SMS_PROVIDER": "pigeon"})


def test_unknown_environment_name(tmp_path):
    with pytest.raises(ConfigError):
        load_config("staging", {}, instance_path=tmp_path, environ={})


def test_upload_dir_is_outside_static(app):
    from pathlib import Path

    assert Path(app.config["UPLOAD_DIR"]).resolve() != Path(app.static_folder).resolve()
    assert Path(app.static_folder).resolve() not in Path(app.config["UPLOAD_DIR"]).resolve().parents


# --------------------------------------------------------------------------
# public endpoints
# --------------------------------------------------------------------------


def test_landing_page_is_public_persian_rtl(client):
    response = client.get("/")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'lang="fa" dir="rtl"' in html
    assert "اتلیه سیار" in html
    assert 'href="/auth/login"' in html
    assert 'href="/auth/register"' in html


def test_healthz(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.get_json() == {"status": "ok", "database": "ok"}
    assert response.headers["Cache-Control"] == "no-store"
    assert "Set-Cookie" not in response.headers  # a probe must not create sessions


def test_healthz_reports_a_broken_database(client, monkeypatch):
    from app.extensions import db

    def boom(*args, **kwargs):
        raise RuntimeError("database is down")

    monkeypatch.setattr(db.session, "execute", boom)
    response = client.get("/healthz")
    assert response.status_code == 503
    assert response.get_json()["database"] == "error"


def test_manifest_served_from_root_with_absolute_icons(client):
    response = client.get("/manifest.webmanifest")
    assert response.status_code == 200
    assert response.mimetype == "application/manifest+json"
    data = json.loads(response.get_data(as_text=True))
    assert data["dir"] == "rtl" and data["lang"] == "fa"
    assert data["start_url"] == "/"
    assert data["icons"] and all(icon["src"].startswith("/static/") for icon in data["icons"])
    for icon in data["icons"]:
        assert client.get(icon["src"]).status_code == 200


def test_service_worker_scope_header(client):
    for path in ("/sw.js", "/static/js/sw.js"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert response.headers["Service-Worker-Allowed"] == "/", path
        assert "javascript" in response.mimetype
    assert client.get("/sw.js").headers["Cache-Control"] == "no-cache"


def test_robots_and_favicon(client):
    robots = client.get("/robots.txt")
    assert robots.status_code == 200
    assert "Disallow: /auth/" in robots.get_data(as_text=True)
    favicon = client.get("/favicon.ico")
    assert favicon.status_code == 301
    assert favicon.headers["Location"].endswith("/static/img/favicon.svg")


def test_base_template_points_the_service_worker_at_the_site_root(client):
    html = client.get("/").get_data(as_text=True)
    assert '<meta name="sw-url" content="/sw.js">' in html


# --------------------------------------------------------------------------
# security headers
# --------------------------------------------------------------------------


def test_security_headers_on_html(client):
    response = client.get("/")
    headers = response.headers
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Referrer-Policy"] == "same-origin"
    assert headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    assert headers["Cache-Control"] == "no-store"
    assert "camera=()" in headers["Permissions-Policy"]


def test_csp_has_no_external_hosts_and_no_unsafe_inline(client):
    csp = client.get("/").headers["Content-Security-Policy"]
    assert "http://" not in csp and "https://" not in csp
    assert "unsafe-inline" not in csp and "unsafe-eval" not in csp
    for directive in ("default-src 'self'", "script-src 'self'", "style-src 'self'", "object-src 'none'",
                      "base-uri 'self'", "form-action 'self'"):
        assert directive in csp


def test_pages_ship_no_inline_script_or_external_assets(client):
    import re

    for url in ("/", "/auth/login", "/auth/register"):
        html = client.get(url).get_data(as_text=True)
        assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html), url
        assert " style=" not in html, url
        assert not re.search(r'(?:src|href)="https?://', html), url


def test_static_files_also_get_headers(client):
    response = client.get("/static/css/app.css")
    assert response.status_code == 200
    assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_hsts_only_over_https_in_production(tmp_path):
    app = create_app(
        "testing",
        UPLOAD_DIR=str(tmp_path),
        HSTS_SECONDS=31536000,
        SESSION_COOKIE_SECURE=True,
    )
    client = app.test_client()
    assert "Strict-Transport-Security" not in client.get("/").headers
    secure = client.get("/", base_url="https://localhost")
    assert secure.headers["Strict-Transport-Security"].startswith("max-age=31536000")


# --------------------------------------------------------------------------
# errors
# --------------------------------------------------------------------------


def test_404_page_for_visitors(client):
    response = client.get("/no/such/page")
    assert response.status_code == 404
    html = response.get_data(as_text=True)
    assert "صفحه‌ای که دنبالش بودید پیدا نشد" in html
    assert 'class="pub"' in html  # public layout for anonymous visitors


def test_404_json_for_api_callers(client):
    response = client.get("/api/nothing", headers={"Accept": "application/json"})
    assert response.status_code == 404
    body = response.get_json()
    assert body["ok"] is False and body["code"] == "not_found"
    assert isinstance(body["error"], str) and body["error"]


def test_404_keeps_the_app_shell_for_signed_in_users(client, make_user, login_as):
    user = make_user("staff")
    login_as(client, user)
    html = client.get("/no/such/page").get_data(as_text=True)
    assert 'id="sidebar"' in html and user.full_name in html


def test_405_uses_generic_error_page(client):
    response = client.post("/healthz")
    assert response.status_code == 405
    assert "این عملیات مجاز نیست" in response.get_data(as_text=True)


def test_custom_abort_message_is_shown(app, client):
    from flask import abort

    @app.get("/boom-403")
    def boom_403():
        abort(403, "فقط مدیر تالار می‌تواند این کار را بکند.")

    response = client.get("/boom-403")
    assert response.status_code == 403
    assert "فقط مدیر تالار می‌تواند این کار را بکند" in response.get_data(as_text=True)
    json_response = client.get("/boom-403", headers={"Accept": "application/json"})
    assert json_response.get_json()["error"] == "فقط مدیر تالار می‌تواند این کار را بکند."


def test_500_page_does_not_leak_and_does_not_need_the_database(app, client, monkeypatch):
    app.config["PROPAGATE_EXCEPTIONS"] = False

    @app.get("/boom-500")
    def boom_500():
        raise RuntimeError("secret internals: password=hunter2")

    response = client.get("/boom-500")
    assert response.status_code == 500
    html = response.get_data(as_text=True)
    assert "مشکلی در سرور پیش آمد" in html
    assert "hunter2" not in html and "Traceback" not in html
    assert '<body class="layout-bare">' in html


def test_500_json(app, client):
    app.config["PROPAGATE_EXCEPTIONS"] = False

    @app.get("/boom-json")
    def boom_json():
        raise RuntimeError("nope")

    response = client.get("/boom-json", headers={"Accept": "application/json"})
    assert response.status_code == 500
    assert response.get_json() == {"ok": False, "error": "خطایی در سرور رخ داد؛ کمی بعد دوباره تلاش کنید.", "code": "server_error"}


def test_stale_data_error_becomes_409(app, client):
    from sqlalchemy.orm.exc import StaleDataError

    @app.get("/stale")
    def stale():
        raise StaleDataError("row changed")

    response = client.get("/stale")
    assert response.status_code == 409
    assert "هم‌زمان" in response.get_data(as_text=True)


def test_payload_too_large_is_413(app, client):
    app.config["MAX_CONTENT_LENGTH"] = 100
    response = client.post("/auth/login", data={"mobile": "0" * 500})
    assert response.status_code == 413
    assert "حجم" in response.get_data(as_text=True)


# --------------------------------------------------------------------------
# template filters
# --------------------------------------------------------------------------


@pytest.fixture
def render(app):
    from flask import render_template_string

    def _render(source, **context):
        with app.test_request_context("/"):
            return render_template_string(source, **context)

    return _render


def test_filter_toman_and_fa(render):
    assert render("{{ 1870000|toman }}") == "۱٬۸۷۰٬۰۰۰"
    assert render("{{ 1870000|toman(suffix=True) }}") == "۱٬۸۷۰٬۰۰۰ تومان"
    assert render("{{ none|toman }}") == "۰"
    assert render("{{ 12345|fa }}") == "۱۲۳۴۵"
    assert render("{{ none|fa }}") == ""


def test_filter_jdate_and_weekday(render):
    day = dt.date(2025, 7, 17)  # 1404/04/26, Thursday
    assert render("{{ d|jdate }}", d=day) == "۱۴۰۴/۰۴/۲۶"
    assert render("{{ d|jdate('long') }}", d=day) == "۲۶ تیر ۱۴۰۴"
    assert render("{{ d|jweekday }}", d=day) == "پنجشنبه"
    assert render("{{ none|jdate }}") == ""
    assert render("{{ none|jweekday }}") == ""


def test_filter_jdatetime_converts_utc_to_iran_time(render):
    stamp = dt.datetime(2025, 7, 17, 20, 45)  # 20:45 UTC = 00:15 next day in Iran (UTC+3:30)
    assert render("{{ t|jdatetime }}", t=stamp) == "۱۴۰۴/۰۴/۲۷ ۰۰:۱۵"
    # jdate on a datetime also uses the Iran day
    assert render("{{ t|jdate }}", t=stamp) == "۱۴۰۴/۰۴/۲۷"


def test_enums_are_template_globals(render):
    assert "staff" in render("{{ Role.choices()|map(attribute=0)|join(',') }}")
    assert render("{{ 'staff'|label_fa('role') }}") == "نیرو"
