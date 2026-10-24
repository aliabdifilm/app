"""The public job board: what visitors see, filters, sorting, pagination, privacy and escaping."""

from __future__ import annotations

import re

import pytest
from test_marketplace_support import body, day, now, reload, set_status, world  # noqa: F401

from app.extensions import db
from app.models import Review
from app.services.constants import EventStatus, JobPostStatus, Role, Skill, UserStatus
from app.services.money import to_persian_digits

EXAMPLE = "اگر ۸۰ عکس فروخته شود: ۱٬۸۷۰٬۰۰۰ تومان"


def titles(html: str) -> list[str]:
    """Post titles in page order (the links inside the card headings)."""
    return re.findall(r'<h2 class="mk-job__name"[^>]*><a [^>]*>([^<]*)</a>', html)


def forms_of_mobile(number: str) -> list[str]:
    return [number, to_persian_digits(number), "+98" + number[1:], number[1:]]


# --------------------------------------------------------------------------
# what a card shows
# --------------------------------------------------------------------------


def test_card_shows_atelier_badge_city_date_role_terms_and_the_worked_example(app, world):
    html = body(app.test_client().get("/jobs"))
    assert "عکاس برای عروسی" in html
    assert "اتلیه ستاره" in html
    assert "تأییدشده" in html  # the manager is verified
    assert "شیراز" in html
    assert "پنجشنبه ۲۶ تیر ۱۴۰۴" in html  # Jalali with the weekday
    assert "پایهٔ ثابت ۱٬۰۰۰٬۰۰۰ تومان" in html
    assert "به‌ازای هر عکس فروخته‌شده ۹٬۰۰۰ تومان پورسانت" in html
    assert "پاداش: با فروش ۷۰ عکس به بالا، ۱۵۰٬۰۰۰ تومان" in html
    assert EXAMPLE in html  # the AGENTS.md test vector, computed by payroll.compute_pay
    assert "۱ جای خالی از ۱" in html


def test_the_example_equals_payroll_for_other_terms(app, world, make_job_post):
    make_job_post(world.atelier, Skill.EDITOR, date=day(12), title="ادیتور", base_pay=500_000, commission_per_unit=4_000,
                  bonus_tiers=[{"threshold": 50, "amount": 100_000}, {"threshold": 100, "amount": 300_000}])
    html = body(app.test_client().get("/jobs?role=editor"))
    # 500,000 + 80*4,000 + 100,000 (the 50-photo tier; the 100 tier is not reached)
    assert "اگر ۸۰ عکس فروخته شود: ۹۲۰٬۰۰۰ تومان" in html
    assert "پله‌های پاداش جمع نمی‌شوند" in html


def test_terms_that_promise_nothing_say_so(app, world, make_job_post):
    make_job_post(world.atelier, Skill.SELLER, date=day(13), title="فروشنده توافقی", base_pay=0, commission_per_unit=0)
    html = body(app.test_client().get("/jobs?role=seller"))
    assert "مبلغ با توافق تعیین می‌شود" in html
    assert "اگر ۸۰ عکس" not in html


def test_atelier_rating_is_shown_from_visible_reviews_only(app, world, make_assignment):
    assignment = make_assignment(world.event, "photographer", user=world.staff, status="completed")
    db.session.add_all([
        Review(assignment=assignment, author=world.staff, subject=world.manager, rating=5),
        Review(assignment=make_assignment(world.event, "seller", user=world.other, status="completed"),
               author=world.other, subject=world.manager, rating=2, is_hidden=True),
    ])
    db.session.commit()
    html = body(app.test_client().get("/jobs"))
    assert 'aria-label="امتیاز ۵٫۰ از ۵"' in html  # the hidden 2-star review does not count
    assert "(۱)" in html


# --------------------------------------------------------------------------
# privacy
# --------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/jobs", "/jobs/{post}"])
def test_anonymous_pages_never_contain_a_mobile_number(app, world, make_user, path):
    nameless = make_user(Role.ATELIER_MANAGER, full_name="", mobile="09123334455")
    world.atelier.manager.mobile  # noqa: B018 - loaded
    html = body(app.test_client().get(path.format(post=world.post.id)))
    numbers = [world.manager.mobile, world.staff.mobile, world.other.mobile, world.hall_manager.mobile, nameless.mobile, "07112345678"]
    for number in numbers:
        for form in forms_of_mobile(number):
            assert form not in html, (path, form)


def test_a_nameless_atelier_manager_never_leaks_the_number_through_the_display_name(app, world, make_atelier, make_user, make_job_post, client_for):
    nameless = make_user(Role.ATELIER_MANAGER, full_name="", mobile="09127778899")
    atelier = make_atelier(nameless, name="اتلیه ناشناس", city="شیراز")
    make_job_post(atelier, Skill.PRESENTER, date=day(15), title="پرزنتور")
    for client in (app.test_client(), client_for(world.staff), client_for(world.hall_manager)):
        html = body(client.get("/jobs"))
        assert "09127778899" not in html and "۰۹۱۲۷۷۷۸۸۹۹" not in html


def test_hall_name_is_hidden_from_anonymous_visitors_but_shown_to_members(app, world, client_for):
    anonymous = body(app.test_client().get("/jobs"))
    signed_in = body(client_for(world.staff).get("/jobs"))
    assert "تالار گل‌ها" not in anonymous
    assert "تالار گل‌ها" in signed_in


# --------------------------------------------------------------------------
# which posts are listed
# --------------------------------------------------------------------------


def test_only_open_future_posts_of_active_ateliers_are_listed(app, world, make_job_post, make_atelier, make_user, make_event):
    closed = make_job_post(world.atelier, date=day(20), title="بسته", status=JobPostStatus.CLOSED)
    filled = make_job_post(world.atelier, date=day(21), title="تکمیل", status=JobPostStatus.FILLED)
    hidden = make_job_post(world.atelier, date=day(22), title="مخفی", status=JobPostStatus.HIDDEN)
    past = make_job_post(world.atelier, date=day(-2), title="گذشته")
    make_job_post(world.atelier, date=day(0), title="امروز")
    suspended_manager = make_user(Role.ATELIER_MANAGER, status=UserStatus.SUSPENDED)
    gone = make_job_post(make_atelier(suspended_manager), date=day(23), title="مدیر معلق")
    inactive = make_atelier(make_user(Role.ATELIER_MANAGER), is_active=False)
    off = make_job_post(inactive, date=day(24), title="اتلیه غیرفعال")
    cancelled_event = make_event(world.hall, world.atelier, date=day(25), status=EventStatus.CANCELLED)
    cancelled = make_job_post(world.atelier, date=day(25), title="مراسم لغو", event_id=cancelled_event.id)
    listed = titles(body(app.test_client().get("/jobs")))
    assert set(listed) == {"عکاس برای عروسی", "عکاس رقیب", "امروز"}
    for hidden_post in (closed, filled, hidden, past, gone, off, cancelled):
        assert hidden_post.title not in listed


def test_empty_board_explains_the_next_step(app, world):
    for post in (world.post, world.rival_post):
        set_status(post, "closed")
    html = body(app.test_client().get("/jobs"))
    assert "فعلاً آگهی بازی نیست" in html


# --------------------------------------------------------------------------
# filters and sorting
# --------------------------------------------------------------------------


def test_city_and_role_filters(app, world, make_job_post):
    make_job_post(world.atelier, Skill.EDITOR, date=day(12), title="ادیتور شیراز", city="شیراز")
    client = app.test_client()
    assert set(titles(body(client.get("/jobs?city=تهران")))) == {"عکاس رقیب"}
    assert set(titles(body(client.get("/jobs?role=editor")))) == {"ادیتور شیراز"}
    assert set(titles(body(client.get("/jobs?city=شیراز&role=photographer")))) == {"عکاس برای عروسی"}
    html = body(client.get("/jobs?city=مشهد"))
    assert "آگهی‌ای با این فیلترها پیدا نشد" in html


def test_jalali_date_range_filter(app, world, make_job_post):
    make_job_post(world.atelier, date=day(30), title="دیرتر")
    client = app.test_client()
    # day(7) is 1404/04/26 and day(9) is 1404/04/28; day(30) is 1404/05/18
    assert set(titles(body(client.get("/jobs?date_from=1404/04/27&date_to=1404/04/31")))) == {"عکاس رقیب"}
    assert set(titles(body(client.get("/jobs?date_from=۱۴۰۴/۰۴/۲۶&date_to=۱۴۰۴/۰۴/۲۶")))) == {"عکاس برای عروسی"}
    assert set(titles(body(client.get(f"/jobs?date_from={day(20).isoformat()}")))) == {"دیرتر"}
    assert set(titles(body(client.get(f"/jobs?date_to={day(7).isoformat()}")))) == {"عکاس برای عروسی"}


def test_minimum_pay_uses_the_bonus_too(app, world, make_job_post):
    make_job_post(world.atelier, Skill.EDITOR, date=day(12), title="ارزان", base_pay=300_000, commission_per_unit=1_000)
    client = app.test_client()
    # rival: 1,000,000 + 80*9,000 = 1,720,000; ours: 1,870,000 (with the bonus); cheap: 380,000
    assert set(titles(body(client.get("/jobs?min_pay=1800000")))) == {"عکاس برای عروسی"}
    assert set(titles(body(client.get("/jobs?min_pay=۱٬۷۰۰٬۰۰۰")))) == {"عکاس برای عروسی", "عکاس رقیب"}
    assert set(titles(body(client.get("/jobs?min_pay=1"))))  == {"عکاس برای عروسی", "عکاس رقیب", "ارزان"}


def test_sort_by_date_pay_and_newest(app, world, make_job_post):
    make_job_post(world.atelier, Skill.EDITOR, date=day(8), title="ارزان", base_pay=300_000, commission_per_unit=1_000)
    client = app.test_client()
    assert titles(body(client.get("/jobs"))) == ["عکاس برای عروسی", "ارزان", "عکاس رقیب"]  # by date
    assert titles(body(client.get("/jobs?sort=pay"))) == ["عکاس برای عروسی", "عکاس رقیب", "ارزان"]
    assert titles(body(client.get("/jobs?sort=new")))[0] == "ارزان"  # created last


def test_invalid_filters_are_ignored_with_a_warning_not_an_error(app, world):
    client = app.test_client()
    html = body(client.get("/jobs?role=astronaut&date_from=banana&min_pay=-5&sort=zzz&city=" + "x" * 200))
    assert client.get("/jobs?role=astronaut").status_code == 200
    assert "بعضی فیلترها نادیده گرفته شد" in html
    assert "عکاس برای عروسی" in html  # the rest of the board is still there


def test_reversed_date_range_is_reported(app, world):
    html = body(app.test_client().get("/jobs?date_from=1404/05/01&date_to=1404/04/01"))
    assert "نباید قبل از" in html


# --------------------------------------------------------------------------
# pagination
# --------------------------------------------------------------------------


@pytest.fixture
def many(world, make_job_post):
    for number in range(25):
        make_job_post(world.atelier, date=day(30 + number), title=f"آگهی {number:02d}", city="شیراز")
    return world


def test_pagination_pages_and_totals(app, many):
    client = app.test_client()
    first = titles(body(client.get("/jobs")))
    second = titles(body(client.get("/jobs?page=2")))
    third = titles(body(client.get("/jobs?page=3")))
    assert len(first) == 12 and len(second) == 12 and len(third) == 3  # 27 open posts in total
    assert len(set(first + second + third)) == 27
    assert "۲۷ آگهی باز" in body(client.get("/jobs"))


def test_pagination_keeps_filters_in_the_links(app, many):
    html = body(app.test_client().get("/jobs?city=شیراز&role=photographer&sort=new"))
    links = re.findall(r'href="(/jobs\?[^"]*page=2[^"]*)"', html)
    assert links
    assert "city=" in links[0] and "role=photographer" in links[0] and "sort=new" in links[0]


def test_pagination_works_on_the_python_pay_path_too(app, many):
    client = app.test_client()
    pages = [titles(body(client.get(f"/jobs?sort=pay&page={n}"))) for n in (1, 2, 3)]
    assert [len(p) for p in pages] == [12, 12, 3]
    assert len({t for page in pages for t in page}) == 27
    filtered = titles(body(client.get("/jobs?min_pay=1000000&page=3")))
    assert len(filtered) == 3


def test_a_page_past_the_end_is_just_empty_and_junk_is_page_one(app, many):
    client = app.test_client()
    assert client.get("/jobs?page=99").status_code == 200
    assert titles(body(client.get("/jobs?page=99"))) == []
    assert len(titles(body(client.get("/jobs?page=abc")))) == 12
    assert len(titles(body(client.get("/jobs?page=0")))) == 12


def test_board_uses_a_constant_number_of_queries(app, many):
    from sqlalchemy import event

    counter = {"n": 0}

    def count(conn, cursor, statement, *rest):  # noqa: ANN001
        counter["n"] += 1

    client = app.test_client()
    client.get("/jobs")  # warm up
    event.listen(db.engine, "before_cursor_execute", count)
    try:
        client.get("/jobs")
        small = counter["n"]
        counter["n"] = 0
        client.get("/jobs?page=2")
        other = counter["n"]
    finally:
        event.remove(db.engine, "before_cursor_execute", count)
    assert small <= 8 and other <= 8  # list + count + cities + accepted + ratings, never one per card


# --------------------------------------------------------------------------
# the detail page
# --------------------------------------------------------------------------


def test_detail_page_explains_the_pay_with_scenarios(app, world):
    html = body(app.test_client().get(f"/jobs/{world.post.id}"))
    assert EXAMPLE in html
    assert "۱٬۸۷۰٬۰۰۰" in html and "۱٬۳۶۰٬۰۰۰" in html  # the 80 and the 40 photo rows
    assert "حقوق و شرایط پرداخت" in html


def test_detail_of_closed_and_expired_posts_explains_instead_of_404(app, world, make_job_post):
    closed = make_job_post(world.atelier, date=day(20), title="بسته", status=JobPostStatus.CLOSED)
    past = make_job_post(world.atelier, date=day(-3), title="گذشته")
    client = app.test_client()
    assert "این آگهی بسته شده است" in body(client.get(f"/jobs/{closed.id}"))
    assert "تاریخ این همکاری گذشته است" in body(client.get(f"/jobs/{past.id}"))


def test_hidden_posts_are_404_for_everybody_but_the_owner_and_admins(app, world, make_job_post, client_for):
    hidden = make_job_post(world.atelier, date=day(20), title="مخفی", status=JobPostStatus.HIDDEN)
    url = f"/jobs/{hidden.id}"
    assert app.test_client().get(url).status_code == 404
    assert client_for(world.staff).get(url).status_code == 404
    assert client_for(world.rival_manager).get(url).status_code == 404
    assert client_for(world.manager).get(url).status_code == 200
    assert client_for(world.admin).get(url).status_code == 200


def test_unknown_post_is_404(app, world):
    assert app.test_client().get("/jobs/99999").status_code == 404


def test_detail_prompts_visitors_to_log_in_and_comes_back(app, world):
    html = body(app.test_client().get(f"/jobs/{world.post.id}"))
    assert f"/auth/login?next=/jobs/{world.post.id}" in html or f"next=%2Fjobs%2F{world.post.id}" in html
    assert "ورود و درخواست" in html
    assert 'name="message"' not in html  # no apply form for visitors


# --------------------------------------------------------------------------
# escaping of user text
# --------------------------------------------------------------------------

PAYLOAD = '<script>alert("x")</script><img src=x onerror=alert(1)>'


def test_user_text_is_escaped_on_the_board_and_the_detail_page(app, world, make_job_post):
    post = make_job_post(world.atelier, Skill.SELLER, date=day(11), title=PAYLOAD, description=PAYLOAD, city=PAYLOAD)
    for url in ("/jobs", f"/jobs/{post.id}"):
        html = body(app.test_client().get(url))
        assert "<script>alert" not in html
        assert "<img src=x" not in html
        assert "&lt;script&gt;alert" in html
    world.atelier.name = PAYLOAD
    db.session.commit()
    assert "<script>alert" not in body(app.test_client().get("/jobs"))


def test_pages_ship_no_inline_script_or_style(app, world, client_for):
    for url in ("/jobs", f"/jobs/{world.post.id}"):
        html = body(client_for(world.staff).get(url))
        assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html), url
        assert " style=" not in html, url
        assert " onclick=" not in html, url
        assert not re.search(r'(?:src|href|action)="https?://', html), url  # nothing is loaded from another origin
