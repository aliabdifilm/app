"""Hall profiles: onboarding, edit, logistics, photos, activation, delete; navigation and home."""

from __future__ import annotations

import io
import struct
import zlib

import pytest
import sqlalchemy as sa
from test_halls_support import body, day, hc, now, reload, world  # noqa: F401

from app.extensions import db
from app.models import AuditLog, Hall, HallPhoto
from app.services import nav
from app.services.constants import Role


def png(width=320, height=320):
    def chunk(kind, data):
        raw = kind + data
        return struct.pack(">I", len(data)) + raw + struct.pack(">I", zlib.crc32(raw) & 0xFFFFFFFF)

    rows = b"".join(b"\x00" + b"\x10\x20\x30" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


def upload(client, hall, *files, caption=""):
    data = {"caption": caption, "photo": [(io.BytesIO(content), name) for name, content in files]}
    return client.post(f"/hall/halls/{hall.id}/photos", data=data, content_type="multipart/form-data")


@pytest.fixture
def newcomer(make_user, client_for):
    """A hall manager who has not created a hall yet."""
    user = make_user(Role.HALL_MANAGER, full_name="تازه‌وارد")
    return user, client_for(user)


# --------------------------------------------------------------------------
# onboarding and navigation
# --------------------------------------------------------------------------


def test_registered_navigation_home_and_onboarding(world, app):
    with app.test_request_context("/"):
        assert nav.home_url(world.manager) == "/hall/"
        assert nav.onboarding_url(world.manager) is None  # already has a hall
        newbie = type("U", (), {"role": Role.HALL_MANAGER, "halls": [], "is_authenticated": True})()
        assert nav.onboarding_url(newbie) == "/hall/setup"
        labels = [item["label"] for item in nav.menu_for(world.manager, endpoint="halls.calendar", path="/hall/calendar")]
    assert labels[:4] == ["داشبورد", "مراسم‌ها", "پیشنهادها", "اتلیه‌ها"]
    assert "تالارهای من" in labels and len(labels) <= 8


def test_the_menu_marks_the_right_item_active_on_nested_pages(world, app):
    with app.test_request_context("/"):
        def active(endpoint, path):
            return [i["label"] for i in nav.menu_for(world.manager, endpoint=endpoint, path=path) if i["active"]]

        assert active("halls.event_show", "/hall/events/1") == ["مراسم‌ها"]
        assert active("halls.bulk_new", "/hall/events/bulk") == ["مراسم‌ها"]
        assert active("halls.offer_new", "/hall/offers/new") == ["پیشنهادها"]
        assert active("halls.atelier_show", "/hall/ateliers/1") == ["اتلیه‌ها"]
        assert active("halls.agreements", "/hall/agreements") == ["اتلیه‌ها"]
        assert active("halls.hall_show", "/hall/halls/1") == ["تالارهای من"]


def test_dashboard_redirect_of_the_shell_lands_on_the_hall_dashboard(hc):
    response = hc.get("/dashboard")
    assert response.status_code == 302 and response.headers["Location"].endswith("/hall/")


def test_a_new_manager_creates_the_first_hall_and_lands_on_its_page(newcomer):
    user, client = newcomer
    page = client.get("/hall/setup")
    assert page.status_code == 200 and "تالار خود را ثبت کنید" in body(page)
    response = client.post(
        "/hall/setup",
        data={"name": "تالار آفتاب", "city": "اصفهان", "address": "خیابان چهارباغ", "phone": "۰۳۱۳۲۲۲۳۳۴۴", "ceremony_types": ["wedding", "engagement"], "default_rent": "۳٬۵۰۰٬۰۰۰"},
    )
    assert response.status_code == 302
    hall = db.session.scalar(sa.select(Hall).where(Hall.manager_id == user.id))
    assert hall.name == "تالار آفتاب" and hall.phone == "03132223344"
    assert hall.default_ceremony_types == ["wedding", "engagement"] and hall.default_rent == 3_500_000
    assert response.headers["Location"].endswith(f"/hall/halls/{hall.id}")
    assert "hall.created" in db.session.scalars(sa.select(AuditLog.action)).all()
    # the setup page now sends the manager on
    assert client.get("/hall/setup").status_code == 302


def test_setup_validates_and_keeps_what_was_typed(newcomer):
    _, client = newcomer
    response = client.post("/hall/setup", data={"name": "", "city": "اصفهان", "default_rent": "abc", "map_url": "javascript:alert(1)", "ceremony_types": ["wedding", "nope"]})
    assert response.status_code == 422
    html = body(response)
    assert "نام تالار را وارد کنید" in html and "اجارهٔ پیش‌فرض نامعتبر" in html
    assert "نشانی نقشه باید با https" in html and "نوع مراسم نامعتبر" in html
    assert 'value="اصفهان"' in html


def test_two_halls_of_one_manager_cannot_share_a_name(hc, world):
    response = hc.post("/hall/halls/new", data={"name": "باغ بهار", "city": "شیراز"})
    assert response.status_code == 422 and "قبلاً ثبت کرده‌اید" in body(response)
    # another manager may use the same name
    response = hc.post("/hall/halls/new", data={"name": "باغ تازه", "city": "شیراز"})
    assert response.status_code == 302


# --------------------------------------------------------------------------
# edit
# --------------------------------------------------------------------------


def test_edit_saves_logistics_map_and_rules(hc, world):
    data = {
        "name": "تالار گل‌ها", "city": "شیراز", "address": "بلوار چمران", "map_url": "https://maps.example.org/?q=1",
        "logistics_loading": "درِ پشتی خیابان فرعی", "logistics_power": "سه پریز کنار سن", "logistics_contact_phone": "۰۹۱۲۱۲۳۴۵۶۷",
        "ceremony_types": ["wedding", "henna"], "default_rent": "۴۰۰۰۰۰۰", "single_atelier_per_day": "1", "is_active": "1",
    }
    response = hc.post(f"/hall/halls/{world.hall.id}/edit", data=data)
    assert response.status_code == 302
    hall = reload(world.hall)
    assert hall.map_url == "https://maps.example.org/?q=1" and hall.default_rent == 4_000_000
    assert hall.logistics == {"loading": "درِ پشتی خیابان فرعی", "power": "سه پریز کنار سن", "contact_phone": "09121234567"}
    assert hall.single_atelier_per_day is True and hall.is_active is True
    assert [t.value for t in hall.ceremony_types] == ["wedding", "henna"]
    page = body(hc.get(f"/hall/halls/{world.hall.id}"))
    assert "سه پریز کنار سن" in page and "باز کردن نقشه" in page and "۴٬۰۰۰٬۰۰۰" in page
    assert 'rel="noopener noreferrer"' in page


def test_edit_form_shows_the_stored_values(hc, world):
    world.hall.logistics = {"parking": "پارکینگ زیرزمین"}
    db.session.commit()
    html = body(hc.get(f"/hall/halls/{world.hall.id}/edit"))
    assert "پارکینگ زیرزمین" in html and 'value="تالار گل‌ها"' in html


def test_a_hall_can_be_deactivated_and_then_takes_no_events(hc, world):
    hc.post(f"/hall/halls/{world.hall2.id}/edit", data={"name": "باغ بهار", "city": "شیراز"})  # no is_active checkbox
    assert reload(world.hall2).is_active is False
    response = hc.post("/hall/events/new", data={"hall_id": world.hall2.id, "date": day(30).isoformat(), "ceremony_type": "wedding"})
    assert response.status_code == 422 and "غیرفعال" in body(response)
    assert "غیرفعال" in body(hc.get(f"/hall/halls/{world.hall2.id}"))


def test_a_hall_without_events_can_be_deleted_but_not_one_with_events(hc, world):
    hall2_id, hall_id = world.hall2.id, world.hall.id
    assert hc.post(f"/hall/halls/{hall2_id}/delete", data={}).status_code == 302
    assert db.session.get(Hall, hall2_id) is None
    response = hc.post(f"/hall/halls/{hall_id}/delete", data={}, follow_redirects=True)
    assert "مراسم ثبت‌شده دارد" in body(response)
    assert db.session.get(Hall, hall_id) is not None


# --------------------------------------------------------------------------
# photos
# --------------------------------------------------------------------------


def test_upload_show_serve_and_remove_a_photo(hc, world, client_for, client):
    response = upload(hc, world.hall, ("lobby.png", png()), caption="ورودی تالار")
    assert response.status_code == 302
    (photo,) = db.session.scalars(sa.select(HallPhoto)).all()
    assert photo.hall_id == world.hall.id and photo.caption == "ورودی تالار"
    assert photo.image_path.startswith("hall/") and photo.image_path.endswith(".png")
    page = body(hc.get(f"/hall/halls/{world.hall.id}"))
    assert f"/media/{photo.image_path}" in page and "ورودی تالار" in page
    # signed-in users may see it, anonymous visitors may not
    assert hc.get(f"/media/{photo.image_path}").status_code == 200
    assert client.get(f"/media/{photo.image_path}").status_code == 404
    assert client_for(world.atelier_manager).get(f"/media/{photo.image_path}").status_code == 200
    # the cover appears on the list of halls
    assert f"/media/{photo.image_path}" in body(hc.get("/hall/halls"))
    # remove
    path = photo.image_path
    assert hc.post(f"/hall/halls/{world.hall.id}/photos/{photo.id}/remove", data={}).status_code == 302
    assert db.session.scalar(sa.select(sa.func.count(HallPhoto.id))) == 0
    assert hc.get(f"/media/{path}").status_code == 404


def test_several_photos_at_once_and_bad_files_are_reported(hc, world):
    response = upload(hc, world.hall, ("a.png", png()), ("b.png", png(400, 400)), ("evil.png", b"<script>alert(1)</script>"), ("note.txt", b"hello"))
    assert response.status_code == 302
    assert db.session.scalar(sa.select(sa.func.count(HallPhoto.id))) == 2
    followed = hc.get(f"/hall/halls/{world.hall.id}")
    assert followed.status_code == 200


def test_tiny_photos_and_a_missing_file_are_refused(hc, world):
    followed = hc.post(
        f"/hall/halls/{world.hall.id}/photos",
        data={"photo": (io.BytesIO(png(100, 100)), "small.png")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert "پیکسل" in body(followed)
    assert "عکسی انتخاب نشده" in body(hc.post(f"/hall/halls/{world.hall.id}/photos", data={}, content_type="multipart/form-data", follow_redirects=True))
    assert db.session.scalar(sa.select(sa.func.count(HallPhoto.id))) == 0


def test_the_photo_limit(hc, world, monkeypatch):
    from app.modules.halls import rules

    monkeypatch.setattr(rules, "MAX_HALL_PHOTOS", 1)
    upload(hc, world.hall, ("a.png", png()))
    followed = hc.post(
        f"/hall/halls/{world.hall.id}/photos", data={"photo": (io.BytesIO(png()), "b.png")}, content_type="multipart/form-data", follow_redirects=True
    )
    assert "حداکثر" in body(followed)
    assert db.session.scalar(sa.select(sa.func.count(HallPhoto.id))) == 1


def test_a_photo_of_another_hall_cannot_be_removed_through_my_hall(hc, world, client_for):
    upload(client_for(world.outsider), world.other_hall, ("a.png", png()))
    (photo,) = db.session.scalars(sa.select(HallPhoto)).all()
    response = hc.post(f"/hall/halls/{world.hall.id}/photos/{photo.id}/remove", data={}, follow_redirects=True)
    assert "پیدا نشد" in body(response)
    assert db.session.scalar(sa.select(sa.func.count(HallPhoto.id))) == 1


def test_deleting_a_hall_removes_its_photo_files(hc, world, app):
    import os

    upload(hc, world.hall2, ("a.png", png()))
    (photo,) = db.session.scalars(sa.select(HallPhoto)).all()
    path = os.path.join(app.config["UPLOAD_DIR"], photo.image_path)
    assert os.path.exists(path)
    hc.post(f"/hall/halls/{world.hall2.id}/delete", data={})
    assert not os.path.exists(path)

