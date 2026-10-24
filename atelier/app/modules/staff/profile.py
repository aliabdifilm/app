"""The profile form: parsing, saving and the "how complete is it" meter."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional

from app.extensions import db
from app.models import StaffProfile, User
from app.services.constants import Skill
from app.services.forms import Form

MAX_PORTFOLIO = 24
BIO_MAX = 1000
BIO_GOOD = 40  # a bio shorter than this does not count as "written"

#: (column, Persian label) of the equipment a person owns - ateliers see it in the talent directory.
EQUIPMENT: tuple[tuple[str, str], ...] = (
    ("owns_camera", "دوربین عکاسی"),
    ("owns_flash", "فلاش"),
    ("owns_lens", "لنز"),
    ("owns_phone_light", "فون عکاسی (نور)"),
    ("owns_printer", "پرینتر"),
    ("owns_laptop", "لپ‌تاپ"),
)

_INSTAGRAM = re.compile(r"^[A-Za-z0-9._]{1,30}$")
_INSTAGRAM_URL = re.compile(r"^(?:https?://)?(?:www\.)?instagram\.com/", re.IGNORECASE)

MAX_EXPECTED_BASE = 1_000_000_000
MAX_EXPECTED_COMMISSION = 100_000_000


def get_profile(user: User) -> Optional[StaffProfile]:
    return user.staff_profile


def get_or_create_profile(user: User) -> StaffProfile:
    """The person's marketplace profile; created (empty, unlisted) the first time it is needed."""
    profile = user.staff_profile
    if profile is None:
        profile = StaffProfile(user=user, city=user.city)
        db.session.add(profile)
        db.session.flush()
    return profile


def clean_instagram(text: str) -> Optional[str]:
    """``@name``, ``instagram.com/name/`` or ``name`` -> ``name``; ``None`` when it is not a valid handle."""
    value = (text or "").strip()
    value = _INSTAGRAM_URL.sub("", value).split("?")[0].strip("/ ").lstrip("@")
    return value if _INSTAGRAM.match(value) else None


def form_values(user: User, profile: Optional[StaffProfile]) -> dict[str, Any]:
    """The current data in the shape the template's inputs expect (also used to prefill)."""
    values: dict[str, Any] = {
        "full_name": user.full_name,
        "city": (profile.city if profile and profile.city else user.city) or "",
        "bio": profile.bio if profile else "",
        "experience_years": profile.experience_years if profile else 0,
        "instagram": (profile.instagram if profile else "") or "",
        "expected_base": profile.expected_base if profile else None,
        "expected_commission_per_unit": profile.expected_commission_per_unit if profile else None,
        "skills": list(profile.skills) if profile else [],
        "is_listed": bool(profile and profile.is_listed),
        "is_public_profile": bool(user.is_public_profile),
    }
    for column, _label in EQUIPMENT:
        values[column] = bool(profile and getattr(profile, column))
    return values


@dataclass
class ProfileData:
    full_name: str
    city: Optional[str]
    bio: str
    experience_years: int
    instagram: Optional[str]
    expected_base: Optional[int]
    expected_commission_per_unit: Optional[int]
    skills: list[str]
    equipment: dict[str, bool]
    is_listed: bool
    is_public_profile: bool


def parse_form(form: Form) -> Optional[ProfileData]:
    """Read the profile form; ``None`` (with ``form.errors`` filled) when something is invalid."""
    full_name = form.text("full_name", "نام و نام خانوادگی", required=True, min_len=2, max_len=120)
    city = form.text("city", "شهر", max_len=80)
    bio = form.text("bio", "درباره من", max_len=BIO_MAX, multiline=True, default="")
    years = form.integer("experience_years", "سابقه (سال)", min=0, max=60, default=0)
    instagram_raw = form.text("instagram", "اینستاگرام", max_len=120)
    instagram = None
    if instagram_raw:
        instagram = clean_instagram(instagram_raw)
        if instagram is None:
            form.error("instagram", "نام کاربری اینستاگرام نامعتبر است؛ مثلاً @atelier_star.")
    base = form.money("expected_base", "پایهٔ مورد انتظار", max=MAX_EXPECTED_BASE)
    commission = form.money(
        "expected_commission_per_unit", "پورسانت مورد انتظار هر عکس", max=MAX_EXPECTED_COMMISSION
    )
    skills: list[str] = []
    for text in form.getlist("skills"):
        if Skill.is_valid(text):
            value = Skill.coerce(text).value
            if value not in skills:
                skills.append(value)
        else:
            form.error("skills", "یکی از توانایی‌های انتخاب‌شده نامعتبر است.")
    form.values["skills"] = skills
    equipment = {column: form.boolean(column) for column, _label in EQUIPMENT}
    is_listed = form.boolean("is_listed")
    is_public = form.boolean("is_public_profile")
    if is_listed and form.ok and (not skills or not city):
        form.error("is_listed", "برای نمایش در جستجوی اتلیه‌ها دست‌کم یک توانایی و شهر را مشخص کنید.")
    if not form.ok:
        return None
    return ProfileData(
        full_name=full_name or "",
        city=city,
        bio=bio or "",
        experience_years=years or 0,
        instagram=instagram,
        expected_base=base,
        expected_commission_per_unit=commission,
        skills=skills,
        equipment=equipment,
        is_listed=is_listed,
        is_public_profile=is_public,
    )


def apply(user: User, profile: StaffProfile, data: ProfileData) -> None:
    """Write validated data to the user and the profile (the caller audits and commits)."""
    user.full_name = data.full_name
    user.city = data.city
    user.is_public_profile = data.is_public_profile
    profile.city = data.city
    profile.bio = data.bio
    profile.experience_years = data.experience_years
    profile.instagram = data.instagram
    profile.expected_base = data.expected_base
    profile.expected_commission_per_unit = data.expected_commission_per_unit
    profile.skills = data.skills
    for column, value in data.equipment.items():
        setattr(profile, column, value)
    profile.is_listed = data.is_listed


# -- completeness ------------------------------------------------------------------


@dataclass(frozen=True)
class Step:
    key: str
    label: str
    hint: str
    weight: int
    done: bool
    endpoint: str = "staff.profile"
    anchor: str = ""


@dataclass(frozen=True)
class Completeness:
    percent: int
    steps: list[Step]

    @property
    def missing(self) -> list[Step]:
        return [step for step in self.steps if not step.done]

    @property
    def complete(self) -> bool:
        return self.percent >= 100


def completeness(user: User, profile: Optional[StaffProfile], portfolio_count: int) -> Completeness:
    """Weighted checklist (sums to 100) of what ateliers look at when they pick someone."""
    name = (user.full_name or "").strip()
    steps = [
        Step("name", "نام و نام خانوادگی", "نام واقعی‌تان را بنویسید تا اتلیه‌ها شما را بشناسند.", 10, len(name) >= 2 and not name.isdigit(), anchor="f-full_name"),
        Step("avatar", "عکس پروفایل", "یک عکس واضح از چهره، اعتماد را بیشتر می‌کند.", 10, bool(user.avatar_path), anchor="avatar"),
        Step("city", "شهر", "شهر محل کار، شما را در جستجوی شهرها نشان می‌دهد.", 10, bool(profile and profile.city), anchor="f-city"),
        Step("skills", "توانایی‌ها", "عکاس، ادیتور، پرزنتور یا فروشنده؟ حتماً مشخص کنید.", 20, bool(profile and profile.skill_rows), anchor="skills"),
        Step("bio", "درباره من", "چند جمله دربارهٔ تجربه و سبک کارتان بنویسید.", 10, bool(profile and len((profile.bio or "").strip()) >= BIO_GOOD), anchor="f-bio"),
        Step("terms", "توافق مورد انتظار", "پایه و پورسانت دلخواه را به‌عنوان راهنما بنویسید (الزام‌آور نیست).", 10, bool(profile and (profile.expected_base or profile.expected_commission_per_unit)), anchor="f-expected_base"),
        Step("equipment", "تجهیزات شخصی", "اگر دوربین یا لپ‌تاپ دارید، اتلیه‌ها بدانند.", 10, bool(profile and any(getattr(profile, column) for column, _ in EQUIPMENT)), anchor="equipment"),
        Step("portfolio", "نمونه‌کار", "حداقل یک نمونه‌کار اضافه کنید.", 20, portfolio_count > 0, endpoint="staff.portfolio"),
    ]
    percent = sum(step.weight for step in steps if step.done)
    return Completeness(percent=percent, steps=steps)


def onboarding_complete(user: User) -> bool:
    """The onboarding step "complete your profile" is done once the person picked at least one skill."""
    profile = user.staff_profile
    return bool(profile is not None and profile.skill_rows)
