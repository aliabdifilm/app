"""Domain constants: enums, Persian labels, state machines, product catalog.

Single source of truth for every enumerated value stored in the database.
All enums are ``str`` based (``enum.StrEnum``) so a member compares equal to,
hashes like and JSON-encodes as its plain value::

    Role.ADMIN == "admin"            # True
    LABELS_FA["role"]["admin"]       # 'ادمین سایت'
    Role.ADMIN.label                 # same, via the member

Labels live in :data:`LABELS_FA`, grouped by the snake_case name of the enum
class (``UserStatus`` -> ``"user_status"``).  Use :func:`label_fa` when you
only have a raw string.

This module has no Flask / SQLAlchemy dependency, so it can be imported from
anywhere (models, services, templates, scripts, tests).
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Any, Iterable, Mapping

# --------------------------------------------------------------------------
# Enum base
# --------------------------------------------------------------------------


def _group_name(cls: type) -> str:
    """``UserStatus`` -> ``user_status`` (key into :data:`LABELS_FA`)."""
    return re.sub(r"(?<!^)(?=[A-Z])", "_", cls.__name__).lower()


class LabeledEnum(StrEnum):
    """``StrEnum`` with Persian labels and a few form-friendly helpers."""

    @property
    def label(self) -> str:
        """Persian label of this member."""
        return LABELS_FA[_group_name(type(self))][self.value]

    @classmethod
    def group(cls) -> str:
        return _group_name(cls)

    @classmethod
    def values(cls) -> list[str]:
        return [m.value for m in cls]

    @classmethod
    def choices(cls) -> list[tuple[str, str]]:
        """``[(value, persian_label), ...]`` in declaration order (for <select>)."""
        return [(m.value, m.label) for m in cls]

    @classmethod
    def coerce(cls, value: Any) -> "LabeledEnum":
        """Return the member for *value* (a member or its string value).

        Raises ``ValueError`` for anything unknown.
        """
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value).strip())
        except ValueError:
            raise ValueError(
                f"{value!r} is not a valid {cls.__name__} "
                f"(expected one of {', '.join(cls.values())})"
            ) from None

    @classmethod
    def is_valid(cls, value: Any) -> bool:
        try:
            cls.coerce(value)
        except ValueError:
            return False
        return True


# --------------------------------------------------------------------------
# Identity and people
# --------------------------------------------------------------------------


class Role(LabeledEnum):
    """Platform role of a user account (exactly one per user)."""

    ADMIN = "admin"
    HALL_MANAGER = "hall_manager"
    ATELIER_MANAGER = "atelier_manager"
    STAFF = "staff"


class UserStatus(LabeledEnum):
    PENDING = "pending"
    ACTIVE = "active"
    SUSPENDED = "suspended"


class Skill(LabeledEnum):
    """Job post on an event.  Also used as ``Assignment.role``.

    Declaration order follows the business description; use
    :data:`ROLE_DISPLAY_ORDER` for the on-site workflow order.
    """

    PHOTOGRAPHER = "photographer"
    EDITOR = "editor"
    PRESENTER = "presenter"
    SELLER = "seller"


# Alias: an assignment's role is one of the skills.
JobRole = Skill

# On-site workflow (AGENTS.md): presenter -> photographer -> seller -> editor.
ROLE_DISPLAY_ORDER: tuple[Skill, ...] = (
    Skill.PRESENTER,
    Skill.PHOTOGRAPHER,
    Skill.SELLER,
    Skill.EDITOR,
)

# Default head-count per role for a new event (editable per event).
DEFAULT_STAFFING_NEEDS: dict[str, int] = {m.value: 1 for m in ROLE_DISPLAY_ORDER}


class AvailabilityStatus(LabeledEnum):
    UNAVAILABLE = "unavailable"
    AVAILABLE = "available"


# --------------------------------------------------------------------------
# Events and staffing
# --------------------------------------------------------------------------


class CeremonyType(LabeledEnum):
    WEDDING = "wedding"
    ENGAGEMENT = "engagement"
    HENNA = "henna"
    BIRTHDAY = "birthday"
    PARTY = "party"
    OTHER = "other"


class OfferMode(LabeledEnum):
    """How a hall manager hands an event to ateliers."""

    OPEN = "open"      # visible to every atelier (marketplace)
    DIRECT = "direct"  # offered to chosen ateliers only
    NONE = "none"      # not offered (assigned manually / private)


class EventStatus(LabeledEnum):
    DRAFT = "draft"
    OPEN = "open"
    OFFERED = "offered"
    CONFIRMED = "confirmed"
    DONE = "done"
    INVOICED = "invoiced"
    CANCELLED = "cancelled"


class OfferStatus(LabeledEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    DECLINED = "declined"
    WITHDRAWN = "withdrawn"


class AssignmentStatus(LabeledEnum):
    INVITED = "invited"
    ACCEPTED = "accepted"
    DECLINED = "declined"
    CONFIRMED = "confirmed"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class AssignmentSource(LabeledEnum):
    DIRECT = "direct"            # manager picked a platform user
    ROSTER = "roster"            # atelier's fixed staff
    MARKETPLACE = "marketplace"  # came through a job post application


# --------------------------------------------------------------------------
# Marketplace
# --------------------------------------------------------------------------


class JobPostStatus(LabeledEnum):
    OPEN = "open"
    FILLED = "filled"
    CLOSED = "closed"
    HIDDEN = "hidden"


class ApplicationStatus(LabeledEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"


# --------------------------------------------------------------------------
# Accounting
# --------------------------------------------------------------------------


class InvoiceStatus(LabeledEnum):
    DRAFT = "draft"
    SUBMITTED = "submitted"
    SETTLED = "settled"  # every payroll line paid
    VOID = "void"


class ProductCategory(LabeledEnum):
    PRINT = "print"
    RAW_FILE = "raw_file"
    EDITED_FILE = "edited_file"
    FRAME = "frame"
    CHASSIS = "chassis"


class PaymentMethod(LabeledEnum):
    """How a customer paid (income side)."""

    CARD_READER = "card_reader"
    CARD_TO_CARD = "card_to_card"
    CASH = "cash"


class PayoutMethod(LabeledEnum):
    """How a staff member was paid (payroll side)."""

    CASH = "cash"
    CARD_TO_CARD = "card_to_card"
    BANK_TRANSFER = "bank_transfer"
    OTHER = "other"


class ExpenseKind(LabeledEnum):
    VENUE_RENT = "venue_rent"
    STAFF_PAY = "staff_pay"
    EQUIPMENT_RENTAL = "equipment_rental"
    MATERIAL = "material"
    OTHER = "other"


# --------------------------------------------------------------------------
# Inventory
# --------------------------------------------------------------------------


class StockKind(LabeledEnum):
    PAPER = "paper"
    INK = "ink"
    FRAME = "frame"
    CHASSIS = "chassis"
    CELLOPHANE = "cellophane"
    OTHER = "other"


class PaperSize(LabeledEnum):
    A4 = "A4"
    A3 = "A3"


class MovementReason(LabeledEnum):
    PURCHASE = "purchase"
    CONSUMPTION = "consumption"
    ADJUSTMENT = "adjustment"
    WASTE = "waste"
    RETURN = "return"


class EquipmentKind(LabeledEnum):
    CAMERA = "camera"
    FLASH = "flash"
    LENS = "lens"
    PHONE_LIGHT = "phone_light"
    PRINTER = "printer"
    LAPTOP_EDIT = "laptop_edit"
    LAPTOP_SALES = "laptop_sales"
    OTHER = "other"


class EquipmentOwnership(LabeledEnum):
    OWNED = "owned"
    RENTED = "rented"


class EquipmentCondition(LabeledEnum):
    GOOD = "good"
    FAIR = "fair"
    POOR = "poor"
    IN_REPAIR = "in_repair"


# --------------------------------------------------------------------------
# Platform
# --------------------------------------------------------------------------


class AnnouncementAudience(LabeledEnum):
    ALL = "all"
    ADMIN = "admin"
    HALL_MANAGER = "hall_manager"
    ATELIER_MANAGER = "atelier_manager"
    STAFF = "staff"


class AnnouncementLevel(LabeledEnum):
    INFO = "info"
    SUCCESS = "success"
    WARNING = "warning"


class NotificationKind(LabeledEnum):
    """Well-known ``Notification.kind`` values.

    The column is a free string (modules may add kinds); these are the ones the
    shared services emit.
    """

    ASSIGNMENT_INVITED = "assignment_invited"
    ASSIGNMENT_CHANGED = "assignment_changed"
    ASSIGNMENT_RESPONSE = "assignment_response"
    OFFER_RECEIVED = "offer_received"
    OFFER_RESPONSE = "offer_response"
    APPLICATION_RECEIVED = "application_received"
    APPLICATION_DECIDED = "application_decided"
    EVENT_STATUS = "event_status"
    INVOICE_SUBMITTED = "invoice_submitted"
    PAYROLL_PAID = "payroll_paid"
    REVIEW_RECEIVED = "review_received"
    LOW_STOCK = "low_stock"
    ACCOUNT = "account"
    SYSTEM = "system"


# --------------------------------------------------------------------------
# Persian labels
# --------------------------------------------------------------------------

LABELS_FA: dict[str, dict[str, str]] = {
    "role": {
        "admin": "ادمین سایت",
        "hall_manager": "مدیر تالار",
        "atelier_manager": "مدیر اتلیه",
        "staff": "نیرو",
    },
    "user_status": {
        "pending": "در انتظار تأیید",
        "active": "فعال",
        "suspended": "معلق",
    },
    "skill": {
        "photographer": "عکاس",
        "editor": "ادیتور",
        "presenter": "پرزنتور",
        "seller": "فروشنده",
    },
    "availability_status": {
        "unavailable": "در دسترس نیستم",
        "available": "در دسترس هستم",
    },
    "ceremony_type": {
        "wedding": "عروسی",
        "engagement": "نامزدی",
        "henna": "حنابندی",
        "birthday": "تولد",
        "party": "جشن و مهمانی",
        "other": "سایر",
    },
    "offer_mode": {
        "open": "عمومی (همه اتلیه‌ها)",
        "direct": "پیشنهاد مستقیم",
        "none": "بدون پیشنهاد",
    },
    "event_status": {
        "draft": "پیش‌نویس",
        "open": "باز",
        "offered": "پیشنهاد شده",
        "confirmed": "قطعی",
        "done": "برگزار شده",
        "invoiced": "فاکتور شده",
        "cancelled": "لغو شده",
    },
    "offer_status": {
        "pending": "در انتظار پاسخ",
        "accepted": "پذیرفته شده",
        "declined": "رد شده",
        "withdrawn": "پس گرفته شده",
    },
    "assignment_status": {
        "invited": "دعوت شده",
        "accepted": "پذیرفته",
        "declined": "رد شده",
        "confirmed": "قطعی",
        "completed": "انجام شده",
        "cancelled": "لغو شده",
    },
    "assignment_source": {
        "direct": "انتخاب مستقیم",
        "roster": "نیروی ثابت",
        "marketplace": "بازار کار",
    },
    "job_post_status": {
        "open": "باز",
        "filled": "تکمیل شده",
        "closed": "بسته",
        "hidden": "مخفی",
    },
    "application_status": {
        "pending": "در انتظار بررسی",
        "accepted": "پذیرفته شده",
        "rejected": "رد شده",
        "withdrawn": "انصراف داده",
    },
    "invoice_status": {
        "draft": "پیش‌نویس",
        "submitted": "ثبت شده",
        "settled": "تسویه شده",
        "void": "باطل شده",
    },
    "product_category": {
        "print": "چاپ",
        "raw_file": "فایل خام",
        "edited_file": "فایل ادیت",
        "frame": "قاب",
        "chassis": "شاسی",
    },
    "payment_method": {
        "card_reader": "کارتخوان",
        "card_to_card": "کارت به کارت",
        "cash": "نقد",
    },
    "payout_method": {
        "cash": "نقد",
        "card_to_card": "کارت به کارت",
        "bank_transfer": "حواله بانکی",
        "other": "سایر",
    },
    "expense_kind": {
        "venue_rent": "اجارهٔ باغ/تالار",
        "staff_pay": "حقوق نیرو",
        "equipment_rental": "اجارهٔ تجهیزات",
        "material": "متریال",
        "other": "سایر هزینه‌ها",
    },
    "stock_kind": {
        "paper": "کاغذ عکس",
        "ink": "جوهر",
        "frame": "قاب",
        "chassis": "شاسی",
        "cellophane": "سلفون",
        "other": "سایر",
    },
    "paper_size": {
        "A4": "A4",
        "A3": "A3",
    },
    "movement_reason": {
        "purchase": "خرید",
        "consumption": "مصرف",
        "adjustment": "اصلاح موجودی",
        "waste": "ضایعات",
        "return": "مرجوعی",
    },
    "equipment_kind": {
        "camera": "دوربین عکاسی",
        "flash": "فلاش",
        "lens": "لنز",
        "phone_light": "فون عکاسی (نور)",
        "printer": "پرینتر",
        "laptop_edit": "لپ‌تاپ ادیت",
        "laptop_sales": "لپ‌تاپ فروشنده",
        "other": "سایر",
    },
    "equipment_ownership": {
        "owned": "ملکی",
        "rented": "اجاره‌ای",
    },
    "equipment_condition": {
        "good": "سالم",
        "fair": "متوسط",
        "poor": "فرسوده",
        "in_repair": "در تعمیر",
    },
    "announcement_audience": {
        "all": "همه کاربران",
        "admin": "ادمین‌ها",
        "hall_manager": "مدیران تالار",
        "atelier_manager": "مدیران اتلیه",
        "staff": "نیروها",
    },
    "announcement_level": {
        "info": "اطلاع‌رسانی",
        "success": "خبر خوب",
        "warning": "هشدار",
    },
    "notification_kind": {
        "assignment_invited": "دعوت به افیش",
        "assignment_changed": "تغییر در افیش",
        "assignment_response": "پاسخ نیرو به افیش",
        "offer_received": "پیشنهاد مراسم",
        "offer_response": "پاسخ به پیشنهاد مراسم",
        "application_received": "درخواست همکاری",
        "application_decided": "نتیجهٔ درخواست همکاری",
        "event_status": "تغییر وضعیت مراسم",
        "invoice_submitted": "ثبت فاکتور",
        "payroll_paid": "پرداخت حقوق",
        "review_received": "نظر جدید",
        "low_stock": "کمبود موجودی",
        "account": "حساب کاربری",
        "system": "اطلاعیهٔ سیستم",
    },
}


def label_fa(group: str, value: Any, default: str | None = None) -> str:
    """Persian label for *value* in *group* (e.g. ``label_fa("role", "admin")``).

    Unknown values fall back to *default*, or to the raw value so a UI never
    crashes on legacy data.
    """
    key = str(value.value if hasattr(value, "value") else value)
    try:
        return LABELS_FA[group][key]
    except KeyError:
        return default if default is not None else key


ALL_ENUMS: tuple[type[LabeledEnum], ...] = tuple(
    dict.fromkeys(  # de-duplicate aliases such as JobRole -> Skill
        obj
        for obj in list(globals().values())
        if isinstance(obj, type) and issubclass(obj, LabeledEnum) and obj is not LabeledEnum
    )
)


# --------------------------------------------------------------------------
# State machines (legal transitions)
# --------------------------------------------------------------------------

EVENT_TRANSITIONS: dict[EventStatus, frozenset[EventStatus]] = {
    EventStatus.DRAFT: frozenset(
        {EventStatus.OPEN, EventStatus.OFFERED, EventStatus.CONFIRMED, EventStatus.CANCELLED}
    ),
    EventStatus.OPEN: frozenset(
        {EventStatus.DRAFT, EventStatus.OFFERED, EventStatus.CONFIRMED, EventStatus.CANCELLED}
    ),
    EventStatus.OFFERED: frozenset(
        {EventStatus.OPEN, EventStatus.CONFIRMED, EventStatus.CANCELLED}
    ),
    EventStatus.CONFIRMED: frozenset(
        {EventStatus.OPEN, EventStatus.DONE, EventStatus.CANCELLED}
    ),
    EventStatus.DONE: frozenset({EventStatus.CONFIRMED, EventStatus.INVOICED}),
    EventStatus.INVOICED: frozenset({EventStatus.DONE}),
    EventStatus.CANCELLED: frozenset({EventStatus.DRAFT}),
}

ASSIGNMENT_TRANSITIONS: dict[AssignmentStatus, frozenset[AssignmentStatus]] = {
    AssignmentStatus.INVITED: frozenset(
        {
            AssignmentStatus.ACCEPTED,
            AssignmentStatus.DECLINED,
            AssignmentStatus.CONFIRMED,
            AssignmentStatus.CANCELLED,
        }
    ),
    AssignmentStatus.ACCEPTED: frozenset(
        {
            AssignmentStatus.CONFIRMED,
            AssignmentStatus.COMPLETED,
            AssignmentStatus.DECLINED,
            AssignmentStatus.CANCELLED,
        }
    ),
    AssignmentStatus.CONFIRMED: frozenset(
        {AssignmentStatus.COMPLETED, AssignmentStatus.CANCELLED}
    ),
    AssignmentStatus.DECLINED: frozenset(
        {AssignmentStatus.INVITED, AssignmentStatus.CANCELLED}
    ),
    AssignmentStatus.COMPLETED: frozenset(),
    AssignmentStatus.CANCELLED: frozenset(),
}

OFFER_TRANSITIONS: dict[OfferStatus, frozenset[OfferStatus]] = {
    OfferStatus.PENDING: frozenset(
        {OfferStatus.ACCEPTED, OfferStatus.DECLINED, OfferStatus.WITHDRAWN}
    ),
    OfferStatus.ACCEPTED: frozenset({OfferStatus.WITHDRAWN}),
    OfferStatus.DECLINED: frozenset({OfferStatus.PENDING}),
    OfferStatus.WITHDRAWN: frozenset({OfferStatus.PENDING}),
}

APPLICATION_TRANSITIONS: dict[ApplicationStatus, frozenset[ApplicationStatus]] = {
    ApplicationStatus.PENDING: frozenset(
        {ApplicationStatus.ACCEPTED, ApplicationStatus.REJECTED, ApplicationStatus.WITHDRAWN}
    ),
    ApplicationStatus.ACCEPTED: frozenset({ApplicationStatus.WITHDRAWN}),
    ApplicationStatus.REJECTED: frozenset({ApplicationStatus.PENDING}),
    ApplicationStatus.WITHDRAWN: frozenset({ApplicationStatus.PENDING}),
}

INVOICE_TRANSITIONS: dict[InvoiceStatus, frozenset[InvoiceStatus]] = {
    InvoiceStatus.DRAFT: frozenset({InvoiceStatus.SUBMITTED, InvoiceStatus.VOID}),
    InvoiceStatus.SUBMITTED: frozenset(
        {InvoiceStatus.DRAFT, InvoiceStatus.SETTLED, InvoiceStatus.VOID}
    ),
    InvoiceStatus.SETTLED: frozenset({InvoiceStatus.SUBMITTED}),
    InvoiceStatus.VOID: frozenset(),
}

# Assignment states in which the person is still "on" the event.  A person with
# an assignment in one of these states on a date is busy that day.  ``invited``
# counts because an unanswered invitation is a soft hold we do not want to
# double-book.  declined / cancelled free the person.
ACTIVE_ASSIGNMENT_STATUSES: frozenset[AssignmentStatus] = frozenset(
    {
        AssignmentStatus.INVITED,
        AssignmentStatus.ACCEPTED,
        AssignmentStatus.CONFIRMED,
        AssignmentStatus.COMPLETED,
    }
)

# Hard commitments: the person (or the manager) said yes.
COMMITTED_ASSIGNMENT_STATUSES: frozenset[AssignmentStatus] = frozenset(
    {
        AssignmentStatus.ACCEPTED,
        AssignmentStatus.CONFIRMED,
        AssignmentStatus.COMPLETED,
    }
)

# Statuses that fill a staffing slot (``invited`` only reserves one).
FILLED_ASSIGNMENT_STATUSES = COMMITTED_ASSIGNMENT_STATUSES

# Events in these states can no longer be (re)staffed.
CLOSED_EVENT_STATUSES: frozenset[EventStatus] = frozenset(
    {EventStatus.DONE, EventStatus.INVOICED, EventStatus.CANCELLED}
)


def is_legal_transition(table: Mapping[Any, Iterable[Any]], old: Any, new: Any) -> bool:
    """True if *old* -> *new* appears in a transition *table* above."""
    try:
        return new in table[old]
    except KeyError:
        return False


# --------------------------------------------------------------------------
# Product catalog (invoice popup) - AGENTS.md "پاپ‌آپ ثبت فاکتور"
# --------------------------------------------------------------------------

STANDARD_SIZES: tuple[str, ...] = ("16x21", "20x30", "30x40")

# Canonical size label: lowercase latin ``x`` with no spaces, e.g. ``16x21``.
_SIZE_SEPARATORS = re.compile(r"\s*[xX×*✕✖]\s*")
_DIGITS_FA = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def normalize_size_label(value: Any) -> str:
    """Canonicalise a size typed by a user: ``'۱۶ × ۲۱'`` -> ``'16x21'``.

    Raises ``ValueError`` unless the result is ``<int>x<int>`` with positive
    parts.
    """
    text = str(value if value is not None else "").translate(_DIGITS_FA).strip()
    parts = _SIZE_SEPARATORS.split(text)
    if len(parts) != 2 or not all(p.isdigit() and int(p) > 0 for p in parts):
        raise ValueError(f"invalid size label: {value!r} (expected e.g. 16x21)")
    return f"{int(parts[0])}x{int(parts[1])}"


def format_size(size_label: str | None) -> str:
    """Display form: ``'16x21'`` -> ``'16×21'`` (pipe through ``fa`` for digits)."""
    if not size_label:
        return ""
    return size_label.replace("x", "×")


PRODUCT_CATALOG: dict[ProductCategory, dict[str, Any]] = {
    ProductCategory.PRINT: {
        "label": "چاپ",
        "standard_sizes": STANDARD_SIZES,
        "allows_custom_sizes": True,
        "custom_label": "چاپ ابعاد دیگر",
        "requires_size": True,
        "stock_kind": StockKind.PAPER,
        "default_counts_for_commission": True,
    },
    ProductCategory.RAW_FILE: {
        "label": "فایل خام",
        "standard_sizes": (),
        "allows_custom_sizes": False,
        "custom_label": None,
        "requires_size": False,
        "stock_kind": None,
        "default_counts_for_commission": True,
    },
    ProductCategory.EDITED_FILE: {
        "label": "فایل ادیت",
        "standard_sizes": (),
        "allows_custom_sizes": False,
        "custom_label": None,
        "requires_size": False,
        "stock_kind": None,
        "default_counts_for_commission": True,
    },
    ProductCategory.FRAME: {
        "label": "قاب",
        "standard_sizes": STANDARD_SIZES,
        "allows_custom_sizes": True,
        "custom_label": "قاب ابعاد دیگر",
        "requires_size": True,
        "stock_kind": StockKind.FRAME,
        "default_counts_for_commission": True,
    },
    ProductCategory.CHASSIS: {
        "label": "شاسی",
        "standard_sizes": STANDARD_SIZES,
        "allows_custom_sizes": True,
        "custom_label": "شاسی ابعاد دیگر",
        "requires_size": True,
        "stock_kind": StockKind.CHASSIS,
        "default_counts_for_commission": True,
    },
}

# Catalog labels must equal LABELS_FA["product_category"] (checked in tests).


def standard_invoice_rows() -> list[dict[str, Any]]:
    """The fixed rows of the invoice popup, in display order.

    Each row: ``{"category", "size_label", "is_custom_size": False, "label"}``;
    ``size_label`` is ``""`` for file categories.  Custom ("+ ابعاد دیگر")
    rows are added by the user on top of these.
    """
    rows: list[dict[str, Any]] = []
    for category, meta in PRODUCT_CATALOG.items():
        sizes = meta["standard_sizes"] or ("",)
        for size in sizes:
            rows.append(
                {
                    "category": category.value,
                    "size_label": size,
                    "is_custom_size": False,
                    "label": meta["label"] + (f" {format_size(size)}" if size else ""),
                }
            )
    return rows


# --------------------------------------------------------------------------
# Business defaults
# --------------------------------------------------------------------------

# A4 default print yield (AGENTS.md A4): sheet -> {size: pieces per sheet}.
# Sizes are never combined on one sheet.  A size missing from a sheet type
# cannot be printed on it.  Editable per atelier (Atelier.print_yield) or
# globally (Setting "print_yield").
DEFAULT_PRINT_YIELD: dict[str, dict[str, int]] = {
    "A4": {"16x21": 2, "20x30": 1},
    "A3": {"16x21": 4, "20x30": 2, "30x40": 1},
}

# Defaults for Setting rows that do not exist yet.
SETTING_DEFAULTS: dict[str, Any] = {
    "print_yield": DEFAULT_PRINT_YIELD,
    # A1: only the highest reached bonus tier applies (tiers do not stack).
    "bonus_policy": "highest_tier",
    "site_name": "اتلیه سیار",
}

MAX_RATING = 5
MIN_RATING = 1
