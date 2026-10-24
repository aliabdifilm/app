"""Atelier onboarding and settings: profile, logo, rent per hall, default pay presets."""

from __future__ import annotations

import io
import struct
import zlib

import sqlalchemy as sa
from test_ateliers_support import body, mc, now, reload, world  # noqa: F401

from app.extensions import db
from app.models import Atelier, AtelierHall, AuditLog
from app.services.constants import Role


def png(side=80):
    def chunk(kind, data):
        payload = kind + data
        return struct.pack(">I", len(data)) + payload + struct.pack(">I", zlib.crc32(payload) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + b"\xff\xff\xff" * side for _ in range(side))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", side, side, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


# --------------------------------------------------------------------------
# onboarding
# --------------------------------------------------------------------------


def test_new_manager_creates_the_atelier_and_lands_on_the_dashboard(app, make_user, client_for):
    manager = make_user(Role.ATELIER_MANAGER)
    client = client_for(manager)
    assert client.get("/atelier/setup").status_code == 200
    response = client.post(
        "/atelier/setup",
        data={"name": "اتلیه نو", "city": "اصفهان", "phone": "۰۳۱۳۲۲۲۱۱۱۱", "default_venue_rent": "۳٬۰۰۰٬۰۰۰"},
    )
    assert response.status_code == 302 and response.headers["Location"].endswith("/atelier/")
    atelier = db.session.scalar(sa.select(Atelier).where(Atelier.manager_id == manager.id))
    assert atelier.name == "اتلیه نو" and atelier.default_venue_rent == 3_000_000 and atelier.phone == "03132221111"
    assert db.session.scalar(sa.select(AuditLog).where(AuditLog.action == "atelier.created")) is not None
    assert client.get("/atelier/").status_code == 200
    assert client.get("/atelier/setup").status_code == 302  # already has one: goes to settings


def test_setup_validation(app, make_user, client_for):
    client = client_for(make_user(Role.ATELIER_MANAGER))
    response = client.post("/atelier/setup", data={"name": "", "city": "", "default_venue_rent": "abc", "phone": "x"})
    assert response.status_code == 422
    page = body(response)
    assert "نام اتلیه را وارد کنید" in page and "شهر را وارد کنید" in page and "نامعتبر" in page
    assert db.session.scalar(sa.select(sa.func.count(Atelier.id))) == 0


def test_duplicate_atelier_name_is_a_form_error(world, mc):
    response = mc.post("/atelier/setup?new=1", data={"name": "اتلیه ستاره", "city": "شیراز"})
    assert response.status_code == 422 and "قبلاً ثبت کرده‌اید" in body(response)
    assert mc.post("/atelier/setup?new=1", data={"name": "شعبهٔ دوم", "city": "شیراز"}).status_code == 302
    assert db.session.scalar(sa.select(sa.func.count(Atelier.id)).where(Atelier.manager_id == world.manager.id)) == 2


# --------------------------------------------------------------------------
# profile
# --------------------------------------------------------------------------


def test_profile_is_saved(world, mc):
    response = mc.post(
        "/atelier/settings/profile",
        data={
            "name": "اتلیه ستارهٔ نو",
            "city": "شیراز",
            "phone": "07136000000",
            "description": "عکاسی عروسی",
            "default_venue_rent": "۴٬۵۰۰٬۰۰۰",
            "block_double_booking": "1",
        },
    )
    assert response.status_code == 302
    atelier = reload(world.atelier)
    assert atelier.name == "اتلیه ستارهٔ نو" and atelier.default_venue_rent == 4_500_000
    assert atelier.block_double_booking is True and atelier.description == "عکاسی عروسی"
    assert mc.post("/atelier/settings/profile", data={"name": "الف", "city": "ب"}).status_code == 302
    assert reload(world.atelier).block_double_booking is False  # an unticked box means off


def test_profile_validation_keeps_typed_values(world, mc):
    response = mc.post("/atelier/settings/profile", data={"name": "", "city": "تهران", "default_venue_rent": "-4"})
    assert response.status_code == 422
    page = body(response)
    assert "نام اتلیه را وارد کنید" in page and 'value="تهران"' in page
    assert reload(world.atelier).name == "اتلیه ستاره"


def test_logo_upload_replaces_and_removes_the_file(world, mc, app):
    def upload(content, name="logo.png"):
        return mc.post(
            "/atelier/settings/profile",
            data={"name": "اتلیه ستاره", "city": "شیراز", "logo": (io.BytesIO(content), name)},
            content_type="multipart/form-data",
        )

    assert upload(png()).status_code == 302
    first = reload(world.atelier).logo_path
    assert first and first.startswith("logo/")
    assert f"/media/{first}" in body(mc.get("/atelier/settings"))
    assert upload(png(90)).status_code == 302
    second = reload(world.atelier).logo_path
    assert second != first
    from app.services import uploads

    assert uploads._resolve(first) is None or not uploads._resolve(first).exists()  # the old file is gone
    removed = mc.post("/atelier/settings/profile", data={"name": "اتلیه ستاره", "city": "شیراز", "remove_logo": "1"})
    assert removed.status_code == 302 and reload(world.atelier).logo_path is None


def test_bad_logo_is_refused_and_nothing_is_stored(world, mc):
    response = mc.post(
        "/atelier/settings/profile",
        data={"name": "اتلیه ستاره", "city": "شیراز", "logo": (io.BytesIO(b"<script>alert(1)</script>"), "logo.png")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 422 and "تصویر معتبر" in body(response)
    assert reload(world.atelier).logo_path is None
    tiny = mc.post(
        "/atelier/settings/profile",
        data={"name": "اتلیه ستاره", "city": "شیراز", "logo": (io.BytesIO(png(20)), "logo.png")},
        content_type="multipart/form-data",
    )
    assert tiny.status_code == 422


# --------------------------------------------------------------------------
# rent per hall
# --------------------------------------------------------------------------


def test_rent_per_hall_falls_back_to_the_default(world, mc):
    world.atelier.default_venue_rent = 3_000_000
    db.session.commit()
    assert world.atelier.rent_for_hall(world.hall) == 3_000_000
    response = mc.post("/atelier/settings/halls", data={"new_hall_id": world.hall.id, "new_rent": "۲٬۵۰۰٬۰۰۰"})
    assert response.status_code == 302
    link = db.session.scalar(sa.select(AtelierHall).where(AtelierHall.atelier_id == world.atelier.id))
    assert link.rent_amount == 2_500_000 and reload(world.atelier).rent_for_hall(world.hall) == 2_500_000
    # blank = "use the default" again
    assert mc.post("/atelier/settings/halls", data={f"rent_{link.id}": ""}).status_code == 302
    db.session.expire_all()
    assert db.session.get(AtelierHall, link.id).rent_amount is None
    assert world.atelier.rent_for_hall(world.hall) == 3_000_000


def test_rent_validation_and_hall_choice(world, mc, make_hall):
    make_hall(city="تبریز", name="تالار دور")
    page = body(mc.get("/atelier/settings"))
    assert "تالار گل‌ها" in page and "تالار دور" not in page  # same city or worked with
    bad = mc.post("/atelier/settings/halls", data={"new_hall_id": world.hall.id, "new_rent": "abc"})
    assert bad.status_code == 422 and "نامعتبر" in body(bad)
    gone = mc.post("/atelier/settings/halls", data={"new_hall_id": 9999, "new_rent": "1"})
    assert gone.status_code == 422
    assert db.session.scalar(sa.select(sa.func.count(AtelierHall.id))) == 0


def test_hall_rent_can_be_removed(world, mc):
    link = AtelierHall(atelier=world.atelier, hall=world.hall, rent_amount=1_000_000)
    db.session.add(link)
    db.session.commit()
    assert mc.post(f"/atelier/settings/halls/{link.id}/delete").status_code == 302
    assert db.session.scalar(sa.select(sa.func.count(AtelierHall.id))) == 0


def test_duplicate_hall_is_not_added_twice(world, mc):
    mc.post("/atelier/settings/halls", data={"new_hall_id": world.hall.id, "new_rent": "5"})
    again = mc.post("/atelier/settings/halls", data={"new_hall_id": world.hall.id, "new_rent": "6"})
    assert again.status_code == 422
    assert db.session.scalar(sa.select(sa.func.count(AtelierHall.id))) == 1


# --------------------------------------------------------------------------
# default pay presets
# --------------------------------------------------------------------------


def test_presets_are_saved_per_role_with_tiers(world, mc):
    response = mc.post(
        "/atelier/settings/presets",
        data={
            "base__seller": "800000",
            "commission__seller": "6000",
            "tier_threshold__seller[]": ["50", "۱۰۰"],
            "tier_amount__seller[]": ["100000", "۳۰۰٬۰۰۰"],
            "base__editor": "",
            "commission__editor": "",
        },
    )
    assert response.status_code == 302
    presets = reload(world.atelier).default_pay_presets
    assert presets["seller"] == {
        "base": 800_000,
        "commission_per_unit": 6_000,
        "bonus_tiers": [{"threshold": 50, "amount": 100_000}, {"threshold": 100, "amount": 300_000}],
    }
    assert "photographer" not in presets and "editor" not in presets  # blank roles keep no preset
    assert db.session.scalar(sa.select(AuditLog).where(AuditLog.action == "atelier.presets_changed")) is not None


def test_preset_validation_names_the_role(world, mc):
    response = mc.post(
        "/atelier/settings/presets",
        data={
            "base__photographer": "1000",
            "tier_threshold__photographer[]": ["5", "5"],
            "tier_amount__photographer[]": ["1", "2"],
            "commission__seller": "x",
        },
    )
    assert response.status_code == 422
    page = body(response)
    assert "عکاس" in page and "دو پلهٔ پاداش" in page and "فروشنده" in page
    assert reload(world.atelier).default_pay_presets["photographer"]["base"] == 1_000_000  # unchanged


def test_settings_form_shows_existing_presets(world, mc):
    page = body(mc.get("/atelier/settings"))
    assert 'value="1000000"' in page and 'value="۷۰"' in page and "150000" in page
