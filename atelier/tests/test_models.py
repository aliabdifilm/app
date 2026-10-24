"""Tests for the relational model: schema rules, constraints, snapshots, helpers.

Each test gets a throwaway Flask app on an in-memory SQLite database (the real
application factory is built by another module).  Foreign keys are enforced
because ``app.models.base`` switches the SQLite pragma on for every engine.
"""

import datetime as dt
import itertools
import os
import re
import time

import pytest
import sqlalchemy as sa
from flask import Flask
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.schema import CreateIndex, CreateTable

from app import models as m
from app.extensions import Base, db, login_manager
from app.services import audit, notifications
from app.services import constants as c

# --------------------------------------------------------------------------
# fixtures and factories
# --------------------------------------------------------------------------


# Set ATELIER_TEST_DB_URL (e.g. a throwaway PostgreSQL database) to run the same
# tests against another database; the default is in-memory SQLite.
TEST_DB_URL = os.environ.get("ATELIER_TEST_DB_URL", "sqlite://")
ON_SQLITE = TEST_DB_URL.startswith("sqlite")


@pytest.fixture
def model_app():
    app = Flask(__name__)
    app.config.update(SQLALCHEMY_DATABASE_URI=TEST_DB_URL, TESTING=True, SECRET_KEY="test")
    db.init_app(app)
    login_manager.init_app(app)
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()
        db.engine.dispose()


_seq = itertools.count(1)


def make_user(role="staff", status="active", **kwargs):
    n = next(_seq)
    kwargs.setdefault("full_name", f"کاربر {n}")
    user = m.User(mobile=f"0912{n:07d}", role=role, status=status, **kwargs)
    db.session.add(user)
    db.session.flush()
    return user


def make_world(**event_kwargs):
    """hall manager + hall, atelier manager + atelier, one confirmed event."""
    hall_manager = make_user("hall_manager")
    atelier_manager = make_user("atelier_manager")
    hall = m.Hall(manager=hall_manager, name="تالار آرمان", city="تهران")
    atelier = m.Atelier(
        manager=atelier_manager, name="اتلیه سیار", city="تهران", default_venue_rent=2_000_000
    )
    event_kwargs.setdefault("date", dt.date(2025, 6, 20))
    event_kwargs.setdefault("status", "confirmed")
    event_kwargs.setdefault("atelier", atelier)
    event = m.Event(hall=hall, **event_kwargs)
    db.session.add_all([hall, atelier, event])
    db.session.flush()
    return event, hall, atelier, hall_manager, atelier_manager


def make_assignment(event, user=None, roster=None, role="photographer", **kwargs):
    kwargs.setdefault("base_pay", 1_000_000)
    kwargs.setdefault("commission_per_unit", 9_000)
    kwargs.setdefault("bonus_tiers", [{"threshold": 70, "amount": 150_000}])
    assignment = m.Assignment(
        event=event, role=role, user=user, roster_member=roster, **kwargs
    )
    db.session.add(assignment)
    db.session.flush()
    return assignment


def expect_integrity_error():
    """Context manager: the flush must fail with IntegrityError; session is rolled back."""

    class _Ctx:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            assert exc_type is not None, "expected an IntegrityError, nothing was raised"
            assert issubclass(exc_type, IntegrityError), f"got {exc_type.__name__}: {exc}"
            db.session.rollback()
            return True

    return _Ctx()


# --------------------------------------------------------------------------
# schema-level rules (metadata only)
# --------------------------------------------------------------------------

EXPECTED_TABLES = {
    "users", "otp_codes", "staff_profiles", "staff_skills", "portfolio_items",
    "staff_availability", "halls", "hall_photos", "hall_agreements", "ateliers", "atelier_halls", "roster_members",
    "events", "event_offers", "assignments", "job_posts", "job_applications",
    "reviews", "invoices", "invoice_lines", "invoice_payments", "expense_lines",
    "payroll_lines", "stock_items", "stock_movements", "equipment",
    "equipment_usages", "notifications", "audit_logs", "settings", "announcements",
}


def test_every_expected_table_is_registered_by_importing_the_package():
    assert set(db.metadata.tables) == EXPECTED_TABLES
    assert {model.__tablename__ for model in m.ALL_MODELS} == EXPECTED_TABLES
    assert db.Model.metadata is Base.metadata


def test_create_all_and_drop_all_work(model_app):
    names = set(sa.inspect(db.engine).get_table_names())
    assert EXPECTED_TABLES <= names


@pytest.mark.skipif(not ON_SQLITE, reason="SQLite-specific pragma")
def test_sqlite_foreign_keys_are_enforced(model_app):
    assert db.session.execute(sa.text("PRAGMA foreign_keys")).scalar() == 1


def _leading_columns(table):
    """Sets of column names that lead an index / unique constraint / PK of *table*."""
    leads = {tuple(col.name for col in table.primary_key.columns)[0]}
    for index in table.indexes:
        leads.add(index.columns.keys()[0] if hasattr(index.columns, "keys") else None)
    for constraint in table.constraints:
        if isinstance(constraint, sa.UniqueConstraint):
            leads.add(constraint.columns.keys()[0])
    return leads


def test_every_foreign_key_column_is_indexed():
    missing = []
    for table in db.metadata.sorted_tables:
        leads = _leading_columns(table)
        for fk in table.foreign_keys:
            if fk.parent.name not in leads:
                missing.append(f"{table.name}.{fk.parent.name}")
    assert not missing, f"foreign keys without a leading index: {missing}"


def test_money_columns_are_bigint_with_non_negative_checks():
    money_name = re.compile(r"(amount|base|commission|bonus|total|cost|rent|pay)")
    not_money = {"units_total", "commission_weight"}
    problems = []
    for table in db.metadata.sorted_tables:
        checks = " ".join(
            str(con.sqltext)
            for con in table.constraints
            if isinstance(con, sa.CheckConstraint)
        )
        for col in table.columns:
            if col.primary_key or col.foreign_keys:
                continue
            is_int = isinstance(col.type, sa.Integer)
            if is_int and money_name.search(col.name) and col.name not in not_money:
                if not isinstance(col.type, sa.BigInteger):
                    problems.append(f"{table.name}.{col.name} should be BigInteger")
                if col.name != "net_profit" and f"{col.name} >= 0" not in checks:
                    problems.append(f"{table.name}.{col.name} lacks CHECK >= 0")
    assert not problems, problems


def test_no_float_columns_except_rating_average():
    floats = {
        (table.name, col.name)
        for table in db.metadata.sorted_tables
        for col in table.columns
        if isinstance(col.type, (sa.Float, sa.Numeric))
    }
    assert floats == {("staff_profiles", "rating_avg")}


def test_enum_columns_are_portable_varchar_with_named_checks():
    for table in db.metadata.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, sa.Enum):
                assert not col.type.native_enum, f"{table.name}.{col.name} is a native enum"
                assert any(
                    isinstance(con, sa.CheckConstraint) and con.name
                    for con in table.constraints
                ), f"{table.name} has no CHECK for {col.name}"


def test_schema_compiles_for_postgresql_with_short_identifiers():
    dialect = postgresql.dialect()
    ddl = []
    for table in db.metadata.sorted_tables:
        ddl.append(str(CreateTable(table).compile(dialect=dialect)))
        for index in table.indexes:
            ddl.append(str(CreateIndex(index).compile(dialect=dialect)))
    text = "\n".join(ddl)
    names = re.findall(r"CONSTRAINT (\w+)", text) + re.findall(
        r"CREATE (?:UNIQUE )?INDEX (\w+)", text
    )
    assert names
    too_long = [n for n in names if len(n) > 63]
    assert not too_long, too_long
    assert "AUTOINCREMENT" not in text and "sqlite" not in text.lower()
    # partial indexes are expressed for PostgreSQL as well
    assert "WHERE" in text


def test_every_table_has_timestamps_or_is_a_plain_child_row():
    child_rows = {"staff_skills", "invoice_lines", "invoice_payments", "expense_lines"}
    for table in db.metadata.sorted_tables:
        if table.name in child_rows:
            continue
        assert "created_at" in table.c or "updated_at" in table.c, table.name


# --------------------------------------------------------------------------
# timestamps, UTC handling, defaults at construction
# --------------------------------------------------------------------------


def test_timestamps_are_naive_utc_and_updated_at_moves(model_app):
    user = make_user()
    assert user.created_at.tzinfo is None
    now = m.utcnow()
    assert abs((now - user.created_at).total_seconds()) < 5
    before = user.updated_at
    time.sleep(0.01)
    user.full_name = "نام جدید"
    db.session.flush()
    assert user.updated_at > before
    assert user.created_at <= user.updated_at


def test_aware_datetimes_are_converted_to_utc_not_truncated(model_app):
    tehran = dt.timezone(dt.timedelta(hours=3, minutes=30))
    otp = m.OtpCode(
        mobile="09121112233",
        code_hash="x",
        expires_at=dt.datetime(2025, 1, 1, 12, 0, tzinfo=tehran),
    )
    db.session.add(otp)
    db.session.commit()
    db.session.expire_all()
    assert db.session.get(m.OtpCode, otp.id).expires_at == dt.datetime(2025, 1, 1, 8, 30)


def test_python_defaults_are_available_before_flush(model_app):
    event = m.Event()
    assert event.status == c.EventStatus.DRAFT
    assert event.offer_mode == c.OfferMode.NONE
    assert event.staffing_needs == {"presenter": 1, "photographer": 1, "seller": 1, "editor": 1}
    assert event.created_at is not None
    user = m.User(mobile="09123334455")
    assert (user.role, user.status, user.is_verified) == ("staff", "pending", False)


def test_default_dicts_are_not_shared_between_instances(model_app):
    a, b = m.Event(), m.Event()
    a.staffing_needs = {**a.staffing_needs, "photographer": 3}
    assert b.staffing_needs["photographer"] == 1


# --------------------------------------------------------------------------
# User, mobile numbers, OTP
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "09123456789",
        "۰۹۱۲۳۴۵۶۷۸۹",
        "٠٩١٢٣٤٥٦٧٨٩",
        "0912 345 6789",
        "0912-345-6789",
        "+989123456789",
        "00989123456789",
        "989123456789",
        "9123456789",
        "‏۰۹۱۲۳۴۵۶۷۸۹‌",
    ],
)
def test_normalize_mobile_accepts_all_common_spellings(raw):
    assert m.normalize_mobile(raw) == "09123456789"


@pytest.mark.parametrize("raw", ["", "123", "0812345678", "0912345678", "091234567890", "abc", None])
def test_normalize_mobile_rejects_garbage(raw):
    with pytest.raises(ValueError):
        m.normalize_mobile(raw)


def test_user_mobile_is_canonicalised_and_unique(model_app):
    user = m.User(mobile="۰۹۱۲۱۱۱۲۲۳۳", role="staff")
    db.session.add(user)
    db.session.flush()
    assert user.mobile == "09121112233"
    db.session.add(m.User(mobile="+98 912 111 2233"))
    with expect_integrity_error():
        db.session.flush()


def test_invalid_mobile_is_rejected_at_assignment():
    with pytest.raises(ValueError):
        m.User(mobile="not a phone")


def test_invalid_enum_values_fail_immediately(model_app):
    with pytest.raises(ValueError, match="not a valid Role"):
        m.User(mobile="09125550001", role="boss")
    user = m.User(mobile="09125550002")
    with pytest.raises(ValueError, match="not a valid UserStatus"):
        user.status = "zombie"
    # ...and the database refuses them too if the ORM is bypassed
    with pytest.raises(IntegrityError):
        db.session.execute(
            sa.text(
                "INSERT INTO users (mobile, full_name, role, status, is_verified, is_public_profile,"
                " created_at, updated_at) VALUES ('09125550003', '', 'boss', 'active', :f, :f,"
                " '2025-01-01 00:00:00', '2025-01-01 00:00:00')"
            ),
            {"f": False},
        )
    db.session.rollback()


def test_enum_attributes_hold_members_even_when_assigned_strings(model_app):
    event = m.Event(status="confirmed", ceremony_type="henna")
    assert event.status is c.EventStatus.CONFIRMED
    assert event.ceremony_type is c.CeremonyType.HENNA
    assert event.status.label == "قطعی"  # works before any flush
    event.status = c.EventStatus.DONE
    assert event.status is c.EventStatus.DONE
    event.status = "cancelled"
    assert event.status.value == "cancelled"


def test_enum_columns_load_as_str_compatible_members(model_app):
    user = make_user(role="admin", status="active")
    db.session.commit()
    db.session.expire_all()
    loaded = db.session.get(m.User, user.id)
    assert loaded.role == "admin" == c.Role.ADMIN
    assert isinstance(loaded.role, str)
    assert loaded.role.label == "ادمین سایت"


def test_flask_login_contract_and_suspension(model_app):
    user = make_user(status="pending")
    assert user.is_authenticated and not user.is_anonymous
    assert user.is_active  # pending users may sign in...
    assert not user.is_approved  # ...but are not approved yet
    assert user.get_id() == str(user.id)
    user.status = "active"
    assert user.is_approved
    user.status = "suspended"
    assert not user.is_active


def test_users_without_id_are_not_equal(model_app):
    a, b = m.User(mobile="09120000001"), m.User(mobile="09120000002")
    assert a != b and len({a, b}) == 2


def test_load_user_ignores_suspended_and_bad_ids(model_app):
    user = make_user()
    assert m.load_user(str(user.id)) is user
    assert m.load_user("999999") is None
    assert m.load_user("abc") is None
    assert m.load_user(None) is None
    user.status = "suspended"
    assert m.load_user(str(user.id)) is None


def test_password_helpers(model_app):
    user = make_user()
    assert user.check_password("anything") is False  # no password set
    user.set_password("correct horse")
    assert user.password_hash and user.password_hash != "correct horse"
    assert user.check_password("correct horse")
    assert not user.check_password("wrong")
    assert not user.check_password("")
    with pytest.raises(ValueError):
        user.set_password("short")


def test_has_role_and_display_name(model_app):
    user = make_user("hall_manager", full_name="")
    assert user.has_role("hall_manager", "admin")
    assert not user.has_role("staff")
    with pytest.raises(ValueError):
        user.has_role("nonsense")
    assert user.display_name == user.mobile


def test_otp_hash_is_keyed_and_bound_to_the_mobile(model_app):
    h1 = m.OtpCode.hash_code("09121234567", "123456", "secret")
    assert h1 != m.OtpCode.hash_code("09121234567", "123456", "other-secret")
    assert h1 != m.OtpCode.hash_code("09121234568", "123456", "secret")
    assert h1 == m.OtpCode.hash_code("۰۹۱۲۱۲۳۴۵۶۷", "123456", "secret")
    assert "123456" not in h1


def test_otp_verify_lifecycle(model_app):
    now = dt.datetime(2025, 1, 1, 12, 0, 0)
    otp = m.OtpCode(
        mobile="09121234567",
        code_hash=m.OtpCode.hash_code("09121234567", "123456", "s"),
        expires_at=now + dt.timedelta(minutes=2),
    )
    db.session.add(otp)
    db.session.flush()
    assert otp.verify("000000", "s", now=now) is False and otp.attempts == 1
    assert otp.verify("123456", "s", now=now) is True
    assert otp.consumed_at == now
    assert otp.verify("123456", "s", now=now) is False  # one time only


def test_otp_expiry_and_attempt_limit(model_app):
    now = dt.datetime(2025, 1, 1, 12, 0, 0)
    code_hash = m.OtpCode.hash_code("09121234567", "111111", "s")
    expired = m.OtpCode(mobile="09121234567", code_hash=code_hash, expires_at=now)
    assert expired.is_expired(now) and not expired.verify("111111", "s", now=now)
    limited = m.OtpCode(
        mobile="09121234567", code_hash=code_hash, expires_at=now + dt.timedelta(minutes=2)
    )
    for _ in range(3):
        assert not limited.verify("999999", "s", max_attempts=3, now=now)
    assert not limited.verify("111111", "s", max_attempts=3, now=now)  # locked out
    assert limited.attempts == 3


# --------------------------------------------------------------------------
# Staff profile, skills, availability, portfolio
# --------------------------------------------------------------------------


def test_staff_skills_property_syncs_child_rows(model_app):
    user = make_user()
    profile = m.StaffProfile(user=user, skills=["seller", "photographer", "photographer"])
    db.session.add(profile)
    db.session.flush()
    assert profile.skills == ["photographer", "seller"]  # canonical order, de-duplicated
    profile.skills = ["editor", "photographer"]
    db.session.flush()
    assert profile.skills == ["photographer", "editor"]
    assert db.session.scalar(sa.select(sa.func.count(m.StaffSkill.id))) == 2
    assert profile.has_skill("editor") and not profile.has_skill("seller")
    with pytest.raises(ValueError):
        profile.skills = ["wizard"]


def test_staff_skill_is_unique_per_profile_and_filterable(model_app):
    user_a, user_b = make_user(), make_user()
    pa = m.StaffProfile(user=user_a, skills=["photographer"], city="تهران", is_listed=True)
    pb = m.StaffProfile(user=user_b, skills=["editor"], city="تهران", is_listed=True)
    db.session.add_all([pa, pb])
    db.session.flush()
    found = db.session.scalars(sa.select(m.StaffProfile).where(m.StaffProfile.with_skill("editor"))).all()
    assert found == [pb]
    db.session.add(m.StaffSkill(profile_id=pa.id, skill="photographer"))
    with expect_integrity_error():
        db.session.flush()


def test_one_profile_per_user(model_app):
    user = make_user()
    db.session.add(m.StaffProfile(user=user))
    db.session.flush()
    db.session.add(m.StaffProfile(user=user))
    with expect_integrity_error():
        db.session.flush()


def test_profile_validation_and_defaults(model_app):
    user = make_user()
    profile = m.StaffProfile(user=user, expected_base="۱٬۵۰۰٬۰۰۰", expected_commission_per_unit=8000)
    assert profile.expected_base == 1_500_000
    assert profile.expected_terms() == {"base": 1_500_000, "commission_per_unit": 8000}
    assert (profile.is_listed, profile.rating_avg, profile.rating_count) == (False, 0.0, 0)
    with pytest.raises(ValueError):
        profile.expected_base = -5
    with pytest.raises(ValueError):
        profile.experience_years = -1


def test_availability_is_unique_per_user_and_day(model_app):
    user = make_user()
    day = dt.date(2025, 7, 1)
    db.session.add(m.StaffAvailability(user=user, date=day, note="سفر"))
    db.session.flush()
    db.session.add(m.StaffAvailability(user=user, date=day, status="available"))
    with expect_integrity_error():
        db.session.flush()


def test_user_deletion_cascades_to_personal_data_only(model_app):
    user = make_user()
    profile = m.StaffProfile(user=user, skills=["editor"])
    db.session.add_all(
        [
            profile,
            m.PortfolioItem(user=user, image_path="uploads/a.jpg"),
            m.StaffAvailability(user=user, date=dt.date(2025, 1, 1)),
            m.Notification(user=user, kind="system", title="hi"),
        ]
    )
    db.session.commit()
    db.session.delete(user)
    db.session.commit()
    for model in (m.StaffProfile, m.StaffSkill, m.PortfolioItem, m.StaffAvailability, m.Notification):
        assert db.session.scalar(sa.select(sa.func.count()).select_from(model)) == 0


# --------------------------------------------------------------------------
# Halls, ateliers, rent fallback, roster
# --------------------------------------------------------------------------


def test_hall_and_atelier_names_are_unique_per_manager(model_app):
    manager = make_user("hall_manager")
    db.session.add(m.Hall(manager=manager, name="الف", city="تهران"))
    db.session.flush()
    db.session.add(m.Hall(manager=manager, name="الف", city="کرج"))
    with expect_integrity_error():
        db.session.flush()


def test_deleting_a_manager_with_halls_is_blocked(model_app):
    event, hall, atelier, hall_manager, atelier_manager = make_world()
    db.session.commit()
    db.session.delete(hall_manager)
    with expect_integrity_error():
        db.session.flush()


def test_rent_falls_back_to_atelier_default(model_app):
    event, hall, atelier, *_ = make_world()
    other_hall = m.Hall(manager=hall.manager, name="باغ گلستان", city="تهران")
    db.session.add(other_hall)
    db.session.flush()
    assert atelier.rent_for_hall(hall) == 2_000_000  # no AtelierHall row
    db.session.add(m.AtelierHall(atelier=atelier, hall=hall, rent_amount=3_500_000))
    db.session.flush()
    assert atelier.rent_for_hall(hall) == 3_500_000
    assert atelier.rent_for_hall(hall.id) == 3_500_000
    assert atelier.rent_for_hall(other_hall) == 2_000_000
    db.session.add(m.AtelierHall(atelier=atelier, hall=other_hall, rent_amount=None))
    db.session.flush()
    assert atelier.rent_for_hall(other_hall) == 2_000_000  # NULL row also falls back


def test_atelier_hall_pair_is_unique_and_rent_non_negative(model_app):
    event, hall, atelier, *_ = make_world()
    db.session.add(m.AtelierHall(atelier=atelier, hall=hall, rent_amount=1))
    db.session.flush()
    db.session.add(m.AtelierHall(atelier=atelier, hall=hall, rent_amount=2))
    with expect_integrity_error():
        db.session.flush()
    with pytest.raises(ValueError):
        m.AtelierHall(rent_amount=-1)
    with pytest.raises(ValueError):
        atelier.default_venue_rent = -10
    with pytest.raises(TypeError):
        atelier.default_venue_rent = 1.5  # money is never a float


def test_atelier_presets_are_validated_and_normalised(model_app):
    event, hall, atelier, *_ = make_world()
    atelier.default_pay_presets = {
        "photographer": {
            "base": "۱٬۰۰۰٬۰۰۰",
            "commission_per_unit": 9000,
            "bonus_tiers": [(70, 150_000), {"threshold": 40, "amount": 50_000}],
        }
    }
    assert atelier.preset_for("photographer") == {
        "base": 1_000_000,
        "commission_per_unit": 9000,
        "bonus_tiers": [
            {"threshold": 40, "amount": 50_000},
            {"threshold": 70, "amount": 150_000},
        ],
    }
    assert atelier.preset_for("editor") is None
    with pytest.raises(ValueError):
        atelier.default_pay_presets = {"astronaut": {"base": 1}}


def test_roster_member_without_account(model_app):
    event, hall, atelier, *_ = make_world()
    member = m.RosterMember(
        atelier=atelier, display_name="علی کریمی", mobile="۰۹۱۲۳۴۵۶۷۸۹", skills=["seller", "photographer"]
    )
    db.session.add(member)
    db.session.flush()
    assert member.user_id is None
    assert member.mobile == "09123456789"
    assert member.skills == ["photographer", "seller"]
    assert member.can_do("seller") and not member.can_do("editor")
    assert member.is_active is True


def test_roster_member_validation(model_app):
    event, hall, atelier, *_ = make_world()
    with pytest.raises(ValueError):
        m.RosterMember(atelier=atelier, display_name="x", mobile="12")
    assert m.RosterMember(atelier=atelier, display_name="x", mobile="  ").mobile is None
    with pytest.raises(ValueError):
        m.RosterMember(atelier=atelier, display_name="x", skills=["juggler"])


def test_one_roster_entry_per_user_and_atelier_but_many_without_account(model_app):
    event, hall, atelier, *_ = make_world()
    user = make_user()
    db.session.add_all(
        [
            m.RosterMember(atelier=atelier, display_name="a"),
            m.RosterMember(atelier=atelier, display_name="b"),  # NULL user twice is fine
            m.RosterMember(atelier=atelier, display_name="c", user=user),
        ]
    )
    db.session.flush()
    db.session.add(m.RosterMember(atelier=atelier, display_name="d", user=user))
    with expect_integrity_error():
        db.session.flush()


def test_roster_terms_fall_back_to_atelier_preset(model_app):
    event, hall, atelier, *_ = make_world()
    atelier.default_pay_presets = {"editor": {"base": 700_000, "commission_per_unit": 4000}}
    member = m.RosterMember(
        atelier=atelier,
        display_name="ادیتور ثابت",
        skills=["editor", "seller"],
        default_terms={"seller": {"base": 500_000, "commission_per_unit": 3000}},
    )
    db.session.add(member)
    db.session.flush()
    assert member.terms_for("seller")["base"] == 500_000  # own default wins
    assert member.terms_for("editor")["base"] == 700_000  # atelier preset
    assert member.terms_for("presenter") is None


# --------------------------------------------------------------------------
# Events and offers
# --------------------------------------------------------------------------


def test_event_staffing_needs_validation(model_app):
    event, *_ = make_world()
    event.staffing_needs = {"photographer": 2, "seller": "۱"}
    assert event.staffing_needs == {"photographer": 2, "seller": 1}
    assert event.needed("photographer") == 2 and event.needed("editor") == 0
    with pytest.raises(ValueError):
        event.staffing_needs = {"photographer": -1}
    with pytest.raises(ValueError):
        event.staffing_needs = {"ghost": 1}
    with pytest.raises(ValueError):
        event.guest_count_estimate = -3


def test_several_events_may_share_hall_and_date(model_app):
    event, hall, atelier, *_ = make_world()
    second = m.Event(hall=hall, date=event.date, start_time=dt.time(20, 0), atelier=atelier)
    db.session.add(second)
    db.session.flush()
    assert second.id != event.id


def test_offer_is_unique_per_event_and_atelier(model_app):
    event, hall, atelier, *_ = make_world(status="open", atelier=None)
    other = m.Atelier(manager=make_user("atelier_manager"), name="دیگری", city="کرج")
    db.session.add(other)
    db.session.add(m.EventOffer(event=event, atelier=atelier))
    db.session.flush()
    db.session.add(m.EventOffer(event=event, atelier=atelier))
    with expect_integrity_error():
        db.session.flush()


def test_only_one_offer_per_event_can_be_accepted(model_app):
    event, hall, atelier, *_ = make_world()
    other = m.Atelier(manager=make_user("atelier_manager"), name="دیگری", city="کرج")
    db.session.add(other)
    db.session.flush()
    db.session.add_all(
        [
            m.EventOffer(event=event, atelier=atelier, status="accepted"),
            m.EventOffer(event=event, atelier=other, status="declined"),
        ]
    )
    db.session.flush()
    offer = db.session.scalar(sa.select(m.EventOffer).where(m.EventOffer.atelier_id == other.id))
    offer.status = "accepted"
    with expect_integrity_error():
        db.session.flush()


def test_hall_with_events_cannot_be_deleted(model_app):
    event, hall, *_ = make_world()
    db.session.commit()
    db.session.delete(hall)
    with expect_integrity_error():
        db.session.flush()


# --------------------------------------------------------------------------
# Assignment: one person, terms snapshot, locking
# --------------------------------------------------------------------------


def test_assignment_needs_exactly_one_person(model_app):
    event, hall, atelier, *_ = make_world()
    user = make_user()
    roster = m.RosterMember(atelier=atelier, display_name="ثابت")
    db.session.add(roster)
    db.session.flush()

    db.session.add(m.Assignment(event=event, role="seller", user=user, roster_member=roster))
    with expect_integrity_error():
        db.session.flush()

    event, hall, atelier, *_ = make_world()
    db.session.add(m.Assignment(event=event, role="seller"))
    with expect_integrity_error():
        db.session.flush()


def test_assignment_with_user_or_roster_person(model_app):
    event, hall, atelier, *_ = make_world()
    user = make_user(full_name="سارا")
    roster = m.RosterMember(atelier=atelier, display_name="رضا", mobile="09129998877")
    db.session.add(roster)
    a1 = make_assignment(event, user=user, role="photographer")
    a2 = make_assignment(event, roster=roster, role="seller")
    assert (a1.person_name, a1.person_user_id, a1.person_mobile) == ("سارا", user.id, user.mobile)
    assert (a2.person_name, a2.person_user_id, a2.person_mobile) == ("رضا", None, "09129998877")
    assert a1.status == "invited" and a1.source == "direct"


def test_roster_member_with_account_is_visible_to_that_user(model_app):
    event, hall, atelier, *_ = make_world()
    user = make_user()
    roster = m.RosterMember(atelier=atelier, display_name="x", user=user)
    db.session.add(roster)
    db.session.flush()
    via_roster = make_assignment(event, roster=roster, role="editor")
    mine = db.session.scalars(
        sa.select(m.Assignment).where(m.Assignment.involving_user(user.id))
    ).all()
    assert mine == [via_roster]
    assert via_roster.person_user_id == user.id


def test_active_duplicate_of_same_person_and_role_is_blocked_but_history_is_not(model_app):
    event, *_ = make_world()
    user = make_user()
    first = make_assignment(event, user=user, role="photographer")
    db.session.add(m.Assignment(event=event, role="photographer", user=user))
    with expect_integrity_error():
        db.session.flush()

    event, *_ = make_world()
    user = make_user()
    old = make_assignment(event, user=user, role="photographer", status="declined")
    again = make_assignment(event, user=user, role="photographer")  # allowed: old one is declined
    other_role = make_assignment(event, user=user, role="seller")  # same person, other role
    assert {old.id, again.id, other_role.id} and old.status == "declined"


def test_terms_validation(model_app):
    event, *_ = make_world()
    user = make_user()
    a = make_assignment(event, user=user, base_pay="۱٬۰۰۰٬۰۰۰", commission_per_unit="9000")
    assert (a.base_pay, a.commission_per_unit) == (1_000_000, 9000)
    with pytest.raises(ValueError):
        a.base_pay = -1
    with pytest.raises(TypeError):
        a.base_pay = 1000.5
    with pytest.raises(TypeError):
        a.commission_per_unit = True
    with pytest.raises(ValueError):
        a.base_pay = "abc"


def test_bonus_tiers_are_normalised_sorted_and_validated(model_app):
    event, *_ = make_world()
    a = make_assignment(
        event,
        user=make_user(),
        bonus_tiers=[{"threshold": "۷۰", "amount": "۱۵۰٬۰۰۰"}, (30, 20_000)],
    )
    assert a.bonus_tiers == [
        {"threshold": 30, "amount": 20_000},
        {"threshold": 70, "amount": 150_000},
    ]
    with pytest.raises(ValueError):
        a.bonus_tiers = [{"threshold": 10, "amount": 1}, {"threshold": 10, "amount": 2}]
    with pytest.raises(ValueError):
        a.bonus_tiers = [{"threshold": 10, "amount": -5}]
    with pytest.raises(TypeError):
        a.bonus_tiers = "70:150000"
    a.bonus_tiers = None
    assert a.bonus_tiers == []


def test_terms_dict_shape_matches_payroll(model_app):
    event, *_ = make_world()
    a = make_assignment(event, user=make_user())
    assert a.terms_dict() == {
        "base": 1_000_000,
        "commission_per_unit": 9_000,
        "bonus_tiers": [{"threshold": 70, "amount": 150_000}],
    }
    copy = a.terms_dict()
    copy["bonus_tiers"].append({"threshold": 1, "amount": 1})
    assert len(a.bonus_tiers) == 1  # terms_dict returns a deep copy


def test_pay_terms_matches_payroll_and_the_agents_md_test_vector(model_app):
    payroll = pytest.importorskip("app.services.payroll")
    event, *_ = make_world()
    a = make_assignment(event, user=make_user())
    terms = a.pay_terms()
    assert isinstance(terms, payroll.PayTerms)
    assert terms.base == 1_000_000 and terms.commission_per_unit == 9_000
    assert terms.bonus_tiers == (payroll.BonusTier(70, 150_000),)
    assert payroll.terms_from_dict(a.terms_dict()) == terms
    # 80 photos sold -> 1,000,000 + 720,000 + 150,000
    assert payroll.compute_pay(terms, 80).total == 1_870_000
    assert payroll.compute_pay(terms, 69).total == 1_000_000 + 69 * 9_000  # below the tier


def test_changing_defaults_never_changes_an_existing_snapshot(model_app):
    event, hall, atelier, *_ = make_world()
    user = make_user()
    profile = m.StaffProfile(user=user, expected_base=900_000, expected_commission_per_unit=5000)
    atelier.default_pay_presets = {"photographer": {"base": 1_000_000, "commission_per_unit": 9000}}
    roster = m.RosterMember(
        atelier=atelier,
        display_name="ثابت",
        default_terms={"seller": {"base": 800_000, "commission_per_unit": 6000}},
    )
    db.session.add_all([profile, roster])
    a_user = make_assignment(event, user=user, role="photographer")
    a_roster = make_assignment(event, roster=roster, role="seller", base_pay=800_000, commission_per_unit=6000)
    snapshot_user, snapshot_roster = a_user.terms_dict(), a_roster.terms_dict()

    profile.expected_base = 5_000_000
    profile.expected_commission_per_unit = 99_000
    atelier.default_pay_presets = {"photographer": {"base": 9_000_000, "commission_per_unit": 1}}
    roster.default_terms = {"seller": {"base": 1, "commission_per_unit": 1}}
    db.session.commit()
    db.session.expire_all()

    assert db.session.get(m.Assignment, a_user.id).terms_dict() == snapshot_user
    assert db.session.get(m.Assignment, a_roster.id).terms_dict() == snapshot_roster


def test_locked_terms_cannot_be_changed_by_any_code_path(model_app):
    event, *_ = make_world()
    a = make_assignment(event, user=make_user())
    a.terms_locked_at = m.utcnow()
    db.session.commit()

    a.base_pay = 1  # direct attribute edit
    with pytest.raises(m.TermsLockedError):
        db.session.flush()
    db.session.rollback()
    assert db.session.get(m.Assignment, a.id).base_pay == 1_000_000

    a = db.session.get(m.Assignment, a.id)
    a.bonus_tiers = []
    with pytest.raises(m.TermsLockedError):
        db.session.flush()
    db.session.rollback()

    a = db.session.get(m.Assignment, a.id)
    a.role = "seller"  # identity is frozen as well
    with pytest.raises(m.TermsLockedError):
        db.session.flush()
    db.session.rollback()


def test_locked_assignment_still_accepts_unrelated_updates_and_equal_terms(model_app):
    event, *_ = make_world()
    a = make_assignment(event, user=make_user())
    a.terms_locked_at = m.utcnow()
    db.session.flush()
    a.notes = "ساعت حضور ۱۷"
    a.status = "completed"
    a.base_pay = 1_000_000  # same value: not a change
    db.session.flush()
    assert db.session.get(m.Assignment, a.id).notes == "ساعت حضور ۱۷"


def test_unlocking_allows_edits_again(model_app):
    event, *_ = make_world()
    a = make_assignment(event, user=make_user())
    a.terms_locked_at = m.utcnow()
    db.session.flush()
    a.terms_locked_at = None
    db.session.flush()
    a.base_pay = 1_200_000
    db.session.flush()
    assert a.base_pay == 1_200_000


def test_assignment_of_a_user_cannot_be_orphaned(model_app):
    event, *_ = make_world()
    user = make_user()
    make_assignment(event, user=user)
    db.session.commit()
    db.session.delete(user)
    with expect_integrity_error():
        db.session.flush()


# --------------------------------------------------------------------------
# Marketplace
# --------------------------------------------------------------------------


def test_job_post_terms_and_slots(model_app):
    event, hall, atelier, *_ = make_world()
    post = m.JobPost(
        atelier=atelier, role="photographer", date=event.date, title="عکاس عروسی", city="تهران",
        base_pay=900_000, commission_per_unit=8000, bonus_tiers=[(50, 100_000)], slots=2,
    )
    db.session.add(post)
    db.session.flush()
    assert post.terms_dict()["bonus_tiers"] == [{"threshold": 50, "amount": 100_000}]
    assert post.status == "open" and not post.is_full
    with pytest.raises(ValueError):
        post.slots = 0
    db.session.expire(post)


def test_job_application_is_unique_per_post_and_user(model_app):
    event, hall, atelier, *_ = make_world()
    post = m.JobPost(atelier=atelier, role="editor", date=event.date, title="ادیتور")
    user = make_user()
    db.session.add(post)
    db.session.add(m.JobApplication(post=post, user=user, message="سلام"))
    db.session.flush()
    db.session.add(m.JobApplication(post=post, user=user))
    with expect_integrity_error():
        db.session.flush()


def test_job_post_fills_by_accepted_applications(model_app):
    event, hall, atelier, *_ = make_world()
    post = m.JobPost(atelier=atelier, role="editor", date=event.date, title="ادیتور", slots=1)
    db.session.add(post)
    db.session.add(m.JobApplication(post=post, user=make_user(), status="rejected"))
    db.session.flush()
    assert post.accepted_count == 0
    db.session.add(m.JobApplication(post=post, user=make_user(), status="accepted"))
    db.session.flush()
    db.session.refresh(post)
    assert post.accepted_count == 1 and post.is_full


def _review_setup():
    event, hall, atelier, hall_manager, atelier_manager = make_world()
    worker = make_user()
    assignment = make_assignment(event, user=worker, status="completed")
    return assignment, worker, atelier_manager


def test_review_is_unique_per_assignment_and_author(model_app):
    assignment, worker, boss = _review_setup()
    db.session.add(m.Review(assignment=assignment, author=boss, subject=worker, rating=5))
    db.session.flush()
    db.session.add(m.Review(assignment=assignment, author=boss, subject=worker, rating=1))
    with expect_integrity_error():
        db.session.flush()
    # the other party may review the same assignment
    assignment, worker, boss = _review_setup()
    db.session.add_all(
        [
            m.Review(assignment=assignment, author=boss, subject=worker, rating=4),
            m.Review(assignment=assignment, author=worker, subject=boss, rating=5),
        ]
    )
    db.session.flush()


def test_review_rating_bounds_and_self_review(model_app):
    assignment, worker, boss = _review_setup()
    for bad in (0, 6, -1):
        with pytest.raises(ValueError):
            m.Review(assignment=assignment, author=boss, subject=worker, rating=bad)
    assert m.Review(assignment=assignment, author=boss, subject=worker, rating="۵").rating == 5
    db.session.add(m.Review(assignment=assignment, author=boss, subject=boss, rating=3))
    with expect_integrity_error():
        db.session.flush()
    # the database enforces the range as well, bypassing the ORM validators
    assignment, worker, boss = _review_setup()
    db.session.flush()
    with pytest.raises(IntegrityError):
        db.session.execute(
            sa.text(
                "INSERT INTO reviews (assignment_id, author_id, subject_id, rating, is_hidden,"
                " created_at, updated_at) VALUES (:a, :b, :c, 9, :h, :t, :t)"
            ),
            {"a": assignment.id, "b": boss.id, "c": worker.id, "h": False, "t": "2025-01-01 00:00:00"},
        )
    db.session.rollback()


def test_refresh_rating_ignores_hidden_reviews(model_app):
    assignment, worker, boss = _review_setup()
    profile = m.StaffProfile(user=worker)
    db.session.add(profile)
    other_boss = make_user("atelier_manager")
    db.session.add_all(
        [
            m.Review(assignment=assignment, author=boss, subject=worker, rating=5),
            m.Review(assignment=assignment, author=other_boss, subject=worker, rating=4),
        ]
    )
    db.session.flush()
    profile.refresh_rating()
    assert (profile.rating_count, profile.rating_avg) == (2, 4.5)
    hidden = db.session.scalar(sa.select(m.Review).where(m.Review.author_id == other_boss.id))
    hidden.is_hidden = True
    db.session.flush()
    profile.refresh_rating()
    assert (profile.rating_count, profile.rating_avg) == (1, 5.0)
    hidden_all = db.session.scalar(sa.select(m.Review).where(m.Review.author_id == boss.id))
    hidden_all.is_hidden = True
    db.session.flush()
    profile.refresh_rating()
    assert (profile.rating_count, profile.rating_avg) == (0, 0.0)


# --------------------------------------------------------------------------
# Accounting
# --------------------------------------------------------------------------


def make_invoice(event, atelier):
    invoice = m.Invoice(event=event, atelier=atelier)
    db.session.add(invoice)
    db.session.flush()
    return invoice


def test_one_invoice_per_event(model_app):
    event, hall, atelier, *_ = make_world()
    make_invoice(event, atelier)
    db.session.add(m.Invoice(event=event, atelier=atelier))
    with expect_integrity_error():
        db.session.flush()


def test_invoice_totals_snapshot_follows_the_documented_formulas(model_app):
    event, hall, atelier, *_ = make_world()
    invoice = make_invoice(event, atelier)
    invoice.lines = [
        m.InvoiceLine(category="print", size_label="16x21", qty=50),
        m.InvoiceLine(category="print", size_label="۲۰×۳۰", qty=20),
        m.InvoiceLine(category="frame", size_label="30x40", qty=5, commission_weight=2),
        m.InvoiceLine(category="raw_file", qty=3, counts_for_commission=False),
        m.InvoiceLine(category="chassis", size_label="50 x 70", is_custom_size=True, qty=0),
    ]
    invoice.payments = [
        m.InvoicePayment(method="card_reader", amount=3_000_000),
        m.InvoicePayment(method="cash", amount="۱٬۲۰۰٬۰۰۰"),
    ]
    invoice.expenses = [
        m.ExpenseLine(kind="venue_rent", label="اجاره", amount=2_000_000, is_auto=True),
        m.ExpenseLine(kind="other", label="ناهار", amount=300_000),
    ]
    invoice.refresh_totals()
    db.session.flush()
    assert invoice.units_total == 50 + 20 + 5 * 2  # raw_file does not count
    assert invoice.income_total == 4_200_000
    assert invoice.expense_total == 2_300_000
    assert invoice.net_profit == 1_900_000
    assert invoice.lines[1].size_label == "20x30"  # canonicalised
    assert invoice.lines[4].size_label == "50x70"


def test_net_profit_may_be_negative_but_totals_may_not(model_app):
    event, hall, atelier, *_ = make_world()
    invoice = make_invoice(event, atelier)
    invoice.expenses = [m.ExpenseLine(kind="staff_pay", label="x", amount=500)]
    invoice.refresh_totals()
    db.session.flush()
    assert invoice.net_profit == -500
    with pytest.raises(IntegrityError):
        db.session.execute(sa.text("UPDATE invoices SET income_total = -1"))
    db.session.rollback()


def test_invoice_lines_feed_payroll_directly(model_app):
    payroll = pytest.importorskip("app.services.payroll")
    event, hall, atelier, *_ = make_world()
    invoice = make_invoice(event, atelier)
    invoice.lines = [
        m.InvoiceLine(category="print", size_label="16x21", qty=60),
        m.InvoiceLine(category="frame", size_label="20x30", qty=10, commission_weight=1),
        m.InvoiceLine(category="edited_file", qty=10),
        m.InvoiceLine(category="raw_file", qty=99, counts_for_commission=False),
        m.InvoiceLine(category="print", size_label="40x60", is_custom_size=True, qty=4, commission_weight=0),
    ]
    assert payroll.units_from_lines(invoice.lines) == invoice.compute_units() == 80
    terms = make_assignment(event, user=make_user()).pay_terms()
    assert payroll.compute_pay(terms, invoice.compute_units()).total == 1_870_000


def test_invoice_line_size_rules(model_app):
    event, hall, atelier, *_ = make_world()
    invoice = make_invoice(event, atelier)
    # sized categories need a size, file categories must not have one
    db.session.add(m.InvoiceLine(invoice=invoice, category="print", qty=1))
    with expect_integrity_error():
        db.session.flush()
    event, hall, atelier, *_ = make_world()
    invoice = make_invoice(event, atelier)
    db.session.add(m.InvoiceLine(invoice=invoice, category="raw_file", size_label="16x21", qty=1))
    with expect_integrity_error():
        db.session.flush()
    with pytest.raises(ValueError):
        m.InvoiceLine(category="print", size_label="big", qty=1)
    with pytest.raises(ValueError):
        m.InvoiceLine(category="print", size_label="16x21", qty=-1)
    assert m.InvoiceLine(category="raw_file", size_label=None).size_label == ""


def test_payment_per_method_is_unique(model_app):
    event, hall, atelier, *_ = make_world()
    invoice = make_invoice(event, atelier)
    db.session.add(m.InvoicePayment(invoice=invoice, method="cash", amount=1))
    db.session.flush()
    db.session.add(m.InvoicePayment(invoice=invoice, method="cash", amount=2))
    with expect_integrity_error():
        db.session.flush()


def test_expense_links_are_only_allowed_on_matching_kinds(model_app):
    event, hall, atelier, *_ = make_world()
    invoice = make_invoice(event, atelier)
    assignment = make_assignment(event, user=make_user())
    ok = m.ExpenseLine(invoice=invoice, kind="staff_pay", label="حقوق", amount=10, assignment=assignment, is_auto=True)
    db.session.add(ok)
    db.session.flush()
    db.session.add(m.ExpenseLine(invoice=invoice, kind="other", label="bad", amount=5, assignment=assignment))
    with expect_integrity_error():
        db.session.flush()

    event, hall, atelier, *_ = make_world()
    invoice = make_invoice(event, atelier)
    assignment = make_assignment(event, user=make_user())
    db.session.add_all(
        [
            m.ExpenseLine(invoice=invoice, kind="staff_pay", label="a", amount=1, assignment=assignment),
            m.ExpenseLine(invoice=invoice, kind="staff_pay", label="b", amount=1, assignment=assignment),
        ]
    )
    with expect_integrity_error():
        db.session.flush()  # one expense line per assignment and invoice


def test_payroll_line_total_must_equal_the_sum(model_app):
    event, hall, atelier, *_ = make_world()
    invoice = make_invoice(event, atelier)
    assignment = make_assignment(event, user=make_user(full_name="سارا"))
    line = m.PayrollLine(
        invoice=invoice, units=80, base=1_000_000, commission=720_000, bonus=150_000,
        total=1_870_000, applied_tier={"threshold": 70, "amount": 150_000},
    )
    line.snapshot_person(assignment)
    db.session.add(line)
    db.session.flush()
    assert (line.role, line.person_name, line.is_paid) == ("photographer", "سارا", False)
    line.total = 1_870_001
    with expect_integrity_error():
        db.session.flush()


def test_payroll_line_payment_fields_and_uniqueness(model_app):
    event, hall, atelier, *_ = make_world()
    invoice = make_invoice(event, atelier)
    assignment = make_assignment(event, user=make_user())
    db.session.add(
        m.PayrollLine(invoice=invoice, assignment=assignment, base=5, total=5)
    )
    db.session.flush()
    db.session.add(m.PayrollLine(invoice=invoice, assignment=assignment, base=1, total=1))
    with expect_integrity_error():
        db.session.flush()

    event, hall, atelier, *_ = make_world()
    invoice = make_invoice(event, atelier)
    assignment = make_assignment(event, user=make_user())
    db.session.add(
        m.PayrollLine(invoice=invoice, assignment=assignment, base=5, total=5, paid_method="cash")
    )
    with expect_integrity_error():
        db.session.flush()  # a payment method without a payment date makes no sense


def test_payroll_history_blocks_deleting_the_assignment(model_app):
    event, hall, atelier, *_ = make_world()
    invoice = make_invoice(event, atelier)
    assignment = make_assignment(event, user=make_user())
    db.session.add(m.PayrollLine(invoice=invoice, assignment=assignment, base=5, total=5))
    db.session.commit()
    db.session.delete(assignment)
    with expect_integrity_error():
        db.session.flush()


def test_invoice_children_cascade_and_event_delete_is_restricted(model_app):
    event, hall, atelier, *_ = make_world()
    invoice = make_invoice(event, atelier)
    invoice.lines = [m.InvoiceLine(category="raw_file", qty=1)]
    invoice.payments = [m.InvoicePayment(method="cash", amount=1)]
    invoice.expenses = [m.ExpenseLine(kind="other", label="x", amount=1)]
    db.session.commit()
    db.session.delete(event)
    with expect_integrity_error():
        db.session.flush()  # invoices.event_id is RESTRICT

    invoice = db.session.scalar(sa.select(m.Invoice))
    db.session.delete(invoice)
    db.session.commit()
    for model in (m.InvoiceLine, m.InvoicePayment, m.ExpenseLine):
        assert db.session.scalar(sa.select(sa.func.count()).select_from(model)) == 0


# --------------------------------------------------------------------------
# Inventory and equipment
# --------------------------------------------------------------------------


def make_stock(atelier, **kwargs):
    kwargs.setdefault("kind", "paper")
    kwargs.setdefault("name", "کاغذ گلاسه")
    item = m.StockItem(atelier=atelier, **kwargs)
    db.session.add(item)
    db.session.flush()
    return item


def test_stock_movements_explain_the_quantity(model_app):
    event, hall, atelier, hall_manager, boss = make_world()
    paper = make_stock(
        atelier, paper_size="A4", sheets_per_pack=50, unit="بسته", quantity=10, low_stock_threshold=2
    )
    assert paper.sheets_available == 500 and not paper.is_low
    purchase = paper.record_movement(5, "purchase", actor=boss, note="خرید", unit_cost=900_000)
    consumption = paper.record_movement(-13, c.MovementReason.CONSUMPTION, event=event, is_auto=True)
    db.session.flush()
    assert paper.quantity == 2 and paper.is_low
    assert (purchase.actor_id, consumption.event_id, consumption.is_auto) == (boss.id, event.id, True)
    ledger = db.session.scalars(
        sa.select(m.StockMovement).where(m.StockMovement.item_id == paper.id).order_by(m.StockMovement.id)
    ).all()
    assert sum(mv.delta for mv in ledger) + 10 == paper.quantity
    assert paper.version >= 2  # optimistic-lock counter moves on every update


def test_stock_movement_validation(model_app):
    event, hall, atelier, *_ = make_world()
    item = make_stock(atelier, kind="ink", name="جوهر", quantity=3)
    with pytest.raises(ValueError):
        item.record_movement(0, "adjustment")
    with pytest.raises(ValueError):
        item.record_movement(1, "teleport")
    with pytest.raises(TypeError):
        item.record_movement(1.5, "purchase")
    with pytest.raises(ValueError):
        item.unit_cost = -1
    with pytest.raises(ValueError):
        item.sheets_per_pack = 0
    assert item.quantity == 3  # failed attempts changed nothing
    db.session.add(m.StockMovement(item=item, delta=0, reason="adjustment"))
    with expect_integrity_error():
        db.session.flush()


def test_stock_quantity_may_go_negative_for_late_data_entry(model_app):
    event, hall, atelier, *_ = make_world()
    item = make_stock(atelier, kind="frame", name="قاب ۲۰×۳۰", quantity=1)
    item.record_movement(-3, "consumption")
    db.session.flush()
    assert item.quantity == -2 and item.is_low


def test_stock_item_with_movements_cannot_be_deleted(model_app):
    event, hall, atelier, *_ = make_world()
    item = make_stock(atelier, quantity=1)
    item.record_movement(1, "purchase")
    db.session.commit()
    with pytest.raises(IntegrityError):
        db.session.execute(sa.text("DELETE FROM stock_items WHERE id = :i"), {"i": item.id})
    db.session.rollback()


def test_equipment_rent_is_snapshotted_on_usage(model_app):
    event, hall, atelier, *_ = make_world()
    rented = m.Equipment(
        atelier=atelier, name="دوربین کرایه‌ای", kind="camera", ownership="rented", rent_cost_per_event=400_000
    )
    owned = m.Equipment(atelier=atelier, name="فلاش", kind="flash", rent_cost_per_event=123)
    db.session.add_all([rented, owned])
    db.session.flush()
    assert rented.event_cost == 400_000
    assert owned.event_cost == 0  # owned gear is free even if a rent figure is stored
    usage = m.EquipmentUsage.snapshot(event, rented)
    db.session.add(usage)
    db.session.add(m.EquipmentUsage.snapshot(event, owned))
    db.session.flush()
    rented.rent_cost_per_event = 900_000  # price change later
    db.session.flush()
    assert usage.rent_cost_snapshot == 400_000
    db.session.add(m.EquipmentUsage.snapshot(event, rented))
    with expect_integrity_error():
        db.session.flush()  # the same gear cannot be attached twice


def test_equipment_with_usage_history_cannot_be_deleted(model_app):
    event, hall, atelier, *_ = make_world()
    gear = m.Equipment(atelier=atelier, name="لپ‌تاپ", kind="laptop_edit")
    db.session.add(gear)
    db.session.flush()
    db.session.add(m.EquipmentUsage.snapshot(event, gear))
    db.session.commit()
    db.session.delete(gear)
    with expect_integrity_error():
        db.session.flush()


# --------------------------------------------------------------------------
# Platform: notifications, audit, settings, announcements
# --------------------------------------------------------------------------


def test_notify_unread_count_and_mark_read(model_app):
    user, other = make_user(), make_user()
    n1 = notifications.notify(user, "system", "سلام", "متن")
    n2 = notifications.notify(user.id, c.NotificationKind.LOW_STOCK, "کمبود کاغذ", url="/inventory")
    n3 = notifications.notify(other, "system", "برای دیگری")
    assert (n1.is_read, n2.kind, n2.url) == (False, "low_stock", "/inventory")
    assert notifications.unread_count(user) == 2
    assert notifications.unread_count(other.id) == 1
    assert notifications.mark_read(user, [n1.id, n3.id]) == 1  # n3 belongs to someone else
    assert notifications.unread_count(user) == 1 and notifications.unread_count(other) == 1
    assert notifications.mark_read(user, n2.id) == 1
    assert notifications.mark_read(user, [n1.id]) == 0  # already read
    assert notifications.mark_read(other) == 1  # None -> everything
    assert notifications.unread_count(other) == 0


def test_notify_without_a_user_is_a_silent_noop(model_app):
    assert notifications.notify(None, "system", "x") is None
    assert notifications.unread_count(None) == 0
    assert notifications.mark_read(None) == 0
    assert notifications.notify_many([None, None], "system", "x") == []


def test_notify_many_skips_duplicates_and_recent_orders_newest_first(model_app):
    a, b = make_user(), make_user()
    rows = notifications.notify_many([a, b, a.id, None], "system", "اطلاعیه")
    assert len(rows) == 2
    notifications.notify(a, "system", "دوم")
    titles = [n.title for n in notifications.recent(a, limit=5)]
    assert titles == ["دوم", "اطلاعیه"]
    assert [n.title for n in notifications.recent(a, unread_only=True)] == ["دوم", "اطلاعیه"]
    notifications.mark_all_read(a)
    assert notifications.recent(a, unread_only=True) == []


def test_notify_validation_and_url_resolver(model_app):
    user = make_user()
    with pytest.raises(ValueError):
        notifications.notify(user, "", "title")
    with pytest.raises(ValueError):
        notifications.notify(user, "system", "   ")
    notifications.set_url_resolver(lambda kind, etype, eid: f"/{etype}/{eid}" if etype else None)
    try:
        event, *_ = make_world()
        row = notifications.notify(user, "event_status", "تغییر", entity=event)
        assert (row.url, row.entity_type, row.entity_id) == (f"/events/{event.id}", "events", event.id)
        explicit = notifications.notify(user, "system", "x", url="/custom", entity=event)
        assert explicit.url == "/custom"  # explicit url beats the resolver
    finally:
        notifications.set_url_resolver(None)


def test_notify_survives_a_broken_url_resolver(model_app):
    user = make_user()
    notifications.set_url_resolver(lambda *a: 1 / 0)
    try:
        row = notifications.notify(user, "system", "still delivered")
        assert row is not None and row.url is None
    finally:
        notifications.set_url_resolver(None)


def test_audit_log_records_actor_entity_and_data(model_app):
    event, *_ = make_world()
    actor = make_user("admin")
    row = audit.log(actor, "event.cancelled", event, {"reason": "طلاق", "when": dt.date(2025, 1, 2)})
    assert (row.actor_id, row.action, row.entity_type, row.entity_id) == (
        actor.id, "event.cancelled", "events", event.id,
    )
    assert row.data == {"reason": "طلاق", "when": "2025-01-02"}
    assert row.ip is None  # outside a request
    system = audit.log(None, "cron.cleanup", ("otp_codes", 5))
    assert (system.actor_id, system.entity_type, system.entity_id) == (None, "otp_codes", 5)


def test_audit_data_is_json_safe_and_redacts_secrets(model_app):
    actor = make_user()
    row = audit.log(
        actor,
        "user.updated",
        actor,
        {
            "password": "hunter2",
            "nested": {"code": "123456", "token": "abc", "ok": 1},
            "role": c.Role.ADMIN,
            "tags": {"a"},
            "when": dt.datetime(2025, 1, 1, 10, 0),
            "who": actor,
        },
    )
    db.session.commit()
    db.session.expire_all()
    data = db.session.get(m.AuditLog, row.id).data
    assert data["password"] == "***"
    assert data["nested"] == {"code": "***", "token": "***", "ok": 1}
    assert data["role"] == "admin" and data["tags"] == ["a"]
    assert data["when"] == "2025-01-01T10:00:00"
    assert data["who"] == {"type": "users", "id": actor.id}


def test_audit_picks_up_the_request_ip(model_app):
    with model_app.test_request_context("/", environ_base={"REMOTE_ADDR": "203.0.113.9"}):
        assert audit.log(None, "login.failed").ip == "203.0.113.9"
        assert audit.log(None, "login.failed", ip="10.0.0.1").ip == "10.0.0.1"


def test_audit_actor_id_handles_odd_actors(model_app):
    user = make_user()
    assert audit.actor_id(user) == user.id
    assert audit.actor_id(7) == 7
    assert audit.actor_id(None) is None
    anonymous = type("Anonymous", (), {"is_authenticated": False})()
    assert audit.actor_id(anonymous) is None
    with pytest.raises(ValueError):
        audit.log(None, "  ")
    with pytest.raises(TypeError):
        audit.log(None, "x", object())


def test_audit_log_is_append_only(model_app):
    row = audit.log(None, "something")
    db.session.commit()
    row.action = "tampered"
    with pytest.raises(m.AuditLogImmutableError):
        db.session.flush()
    db.session.rollback()
    row = db.session.get(m.AuditLog, row.id)
    db.session.delete(row)
    with pytest.raises(m.AuditLogImmutableError):
        db.session.flush()
    db.session.rollback()
    assert db.session.get(m.AuditLog, row.id).action == "something"


def test_audit_survives_actor_deletion_with_null_actor(model_app):
    actor = make_user()
    row = audit.log(actor, "x")
    db.session.commit()
    db.session.delete(actor)
    db.session.commit()
    db.session.expire_all()
    assert db.session.get(m.AuditLog, row.id).actor_id is None


def test_settings_get_set_and_defaults(model_app):
    assert m.Setting.get("print_yield") == c.DEFAULT_PRINT_YIELD  # built-in default
    assert m.Setting.get("missing") is None
    assert m.Setting.get("missing", 5) == 5
    admin = make_user("admin")
    m.Setting.set("print_yield", {"A4": {"16x21": 3}}, actor=admin)
    db.session.flush()
    assert m.Setting.get("print_yield") == {"A4": {"16x21": 3}}
    m.Setting.set("print_yield", {"A4": {"16x21": 4}})
    db.session.flush()
    assert m.Setting.get("print_yield")["A4"]["16x21"] == 4
    assert db.session.scalar(sa.select(sa.func.count(m.Setting.id))) == 1
    db.session.add(m.Setting(key="print_yield", value=1))
    with expect_integrity_error():
        db.session.flush()


def test_announcements_target_roles_and_time_windows(model_app):
    now = dt.datetime(2025, 5, 1, 12, 0)
    db.session.add_all(
        [
            m.Announcement(title="همه"),
            m.Announcement(title="فقط نیروها", audience="staff"),
            m.Announcement(title="مدیران اتلیه", audience="atelier_manager"),
            m.Announcement(title="منتشر نشده", is_published=False),
            m.Announcement(title="هنوز شروع نشده", starts_at=now + dt.timedelta(days=1)),
            m.Announcement(title="تمام شده", ends_at=now - dt.timedelta(hours=1)),
        ]
    )
    db.session.flush()
    staff_titles = {a.title for a in db.session.scalars(m.Announcement.active_for("staff", now))}
    assert staff_titles == {"همه", "فقط نیروها"}
    boss_titles = {a.title for a in db.session.scalars(m.Announcement.active_for(c.Role.ATELIER_MANAGER, now))}
    assert boss_titles == {"همه", "مدیران اتلیه"}


# --------------------------------------------------------------------------
# Constants: labels, catalog, transition tables
# --------------------------------------------------------------------------

_PERSIAN = re.compile("[؀-ۿ]")


def test_every_enum_member_has_a_persian_label():
    assert c.ALL_ENUMS
    for enum_cls in c.ALL_ENUMS:
        labels = c.LABELS_FA[enum_cls.group()]
        assert set(labels) == set(enum_cls.values()), enum_cls.__name__
        for member in enum_cls:
            assert member.label == labels[member.value]
            if enum_cls is not c.PaperSize:
                assert _PERSIAN.search(member.label), f"{enum_cls.__name__}.{member.name}"
    assert set(c.LABELS_FA) == {e.group() for e in c.ALL_ENUMS}


def test_enum_helpers():
    assert c.Role.coerce("admin") is c.Role.ADMIN
    assert c.Role.coerce(c.Role.STAFF) is c.Role.STAFF
    with pytest.raises(ValueError):
        c.Role.coerce("root")
    assert c.Role.is_valid("staff") and not c.Role.is_valid("x")
    assert ("photographer", "عکاس") in c.Skill.choices()
    assert c.label_fa("role", "hall_manager") == "مدیر تالار"
    assert c.label_fa("role", "unknown") == "unknown"
    assert c.label_fa("role", c.Role.ADMIN) == "ادمین سایت"


def test_product_catalog_matches_agents_md():
    assert [meta["label"] for meta in c.PRODUCT_CATALOG.values()] == [
        "چاپ", "فایل خام", "فایل ادیت", "قاب", "شاسی",
    ]
    assert list(c.PRODUCT_CATALOG) == list(c.ProductCategory)
    for category, meta in c.PRODUCT_CATALOG.items():
        assert meta["label"] == c.LABELS_FA["product_category"][category.value]
    assert c.STANDARD_SIZES == ("16x21", "20x30", "30x40")
    for category in ("print", "frame", "chassis"):
        meta = c.PRODUCT_CATALOG[category]
        assert meta["standard_sizes"] == c.STANDARD_SIZES and meta["allows_custom_sizes"]
        assert meta["custom_label"].endswith("ابعاد دیگر")
    for category in ("raw_file", "edited_file"):
        meta = c.PRODUCT_CATALOG[category]
        assert meta["standard_sizes"] == () and not meta["allows_custom_sizes"]


def test_standard_invoice_rows():
    rows = c.standard_invoice_rows()
    assert [(r["category"], r["size_label"]) for r in rows] == [
        ("print", "16x21"), ("print", "20x30"), ("print", "30x40"),
        ("raw_file", ""), ("edited_file", ""),
        ("frame", "16x21"), ("frame", "20x30"), ("frame", "30x40"),
        ("chassis", "16x21"), ("chassis", "20x30"), ("chassis", "30x40"),
    ]
    assert rows[0]["label"] == "چاپ 16×21"


@pytest.mark.parametrize(
    "raw,expected",
    [("16x21", "16x21"), ("۱۶×۲۱", "16x21"), (" 20 X 30 ", "20x30"), ("30*40", "30x40"), ("٥٠x٧٠", "50x70")],
)
def test_normalize_size_label(raw, expected):
    assert c.normalize_size_label(raw) == expected


@pytest.mark.parametrize("raw", ["", None, "16", "16x", "x21", "16x21x3", "0x5", "a x b", "16-21"])
def test_normalize_size_label_rejects_garbage(raw):
    with pytest.raises(ValueError):
        c.normalize_size_label(raw)
    assert c.format_size("16x21") == "16×21" and c.format_size("") == ""


def test_default_print_yield_matches_assumption_a4():
    assert c.DEFAULT_PRINT_YIELD["A4"] == {"16x21": 2, "20x30": 1}
    assert c.DEFAULT_PRINT_YIELD["A3"] == {"16x21": 4, "20x30": 2, "30x40": 1}


@pytest.mark.parametrize(
    "table",
    [c.EVENT_TRANSITIONS, c.ASSIGNMENT_TRANSITIONS, c.OFFER_TRANSITIONS,
     c.APPLICATION_TRANSITIONS, c.INVOICE_TRANSITIONS],
)
def test_transition_tables_are_total_and_closed(table):
    enum_cls = type(next(iter(table)))
    assert set(table) == set(enum_cls), "every status needs an entry (even if terminal)"
    for old, targets in table.items():
        assert old not in targets, "no self transitions"
        assert all(isinstance(t, enum_cls) for t in targets)


def test_assignment_state_machine_shape():
    t = c.ASSIGNMENT_TRANSITIONS
    assert t[c.AssignmentStatus.COMPLETED] == frozenset() == t[c.AssignmentStatus.CANCELLED]
    assert c.AssignmentStatus.ACCEPTED in t[c.AssignmentStatus.INVITED]
    assert c.AssignmentStatus.INVITED in t[c.AssignmentStatus.DECLINED]  # re-invite
    assert c.ACTIVE_ASSIGNMENT_STATUSES.isdisjoint(
        {c.AssignmentStatus.DECLINED, c.AssignmentStatus.CANCELLED}
    )
    assert c.COMMITTED_ASSIGNMENT_STATUSES < c.ACTIVE_ASSIGNMENT_STATUSES
    assert c.is_legal_transition(t, c.AssignmentStatus.INVITED, c.AssignmentStatus.ACCEPTED)
    assert not c.is_legal_transition(t, c.AssignmentStatus.COMPLETED, c.AssignmentStatus.INVITED)
    assert not c.is_legal_transition(t, "nonsense", "worse")


def test_staffing_defaults_follow_the_on_site_workflow():
    assert [r.value for r in c.ROLE_DISPLAY_ORDER] == ["presenter", "photographer", "seller", "editor"]
    assert c.DEFAULT_STAFFING_NEEDS == {"presenter": 1, "photographer": 1, "seller": 1, "editor": 1}
