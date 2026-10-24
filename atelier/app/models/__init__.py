"""Relational model of Atelier Sayyar.

Importing this package registers every table on ``db.metadata`` (so
``db.create_all()`` and migrations see them) and re-exports the models::

    from app.models import User, Event, Assignment

See ``docs/DATA_MODEL.md`` for the ER overview and the conventions.
"""

from app.models.accounting import (
    ExpenseLine,
    Invoice,
    InvoiceLine,
    InvoicePayment,
    PayrollLine,
)
from app.models.assignment import Assignment, TermsLockedError
from app.models.base import (
    BaseModel,
    TimestampMixin,
    UTCDateTime,
    check_int,
    check_money,
    coerce_int,
    normalize_mobile,
    to_latin_digits,
    utcnow,
)
from app.models.event import Event, EventOffer, normalize_staffing_needs
from app.models.inventory import Equipment, EquipmentUsage, StockItem, StockMovement
from app.models.marketplace import JobApplication, JobPost, Review
from app.models.platform import (
    Announcement,
    AuditLog,
    AuditLogImmutableError,
    Notification,
    Setting,
)
from app.models.staff import PortfolioItem, StaffAvailability, StaffProfile, StaffSkill
from app.models.terms import (
    TermsMixin,
    normalize_bonus_tiers,
    normalize_skills,
    normalize_terms,
    normalize_terms_by_role,
)
from app.models.user import OtpCode, User, load_user
from app.models.venue import Atelier, AtelierHall, Hall, HallAgreement, HallPhoto, RosterMember

ALL_MODELS = (
    User,
    OtpCode,
    StaffProfile,
    StaffSkill,
    PortfolioItem,
    StaffAvailability,
    Hall,
    HallPhoto,
    HallAgreement,
    Atelier,
    AtelierHall,
    RosterMember,
    Event,
    EventOffer,
    Assignment,
    JobPost,
    JobApplication,
    Review,
    Invoice,
    InvoiceLine,
    InvoicePayment,
    ExpenseLine,
    PayrollLine,
    StockItem,
    StockMovement,
    Equipment,
    EquipmentUsage,
    Notification,
    AuditLog,
    Setting,
    Announcement,
)

__all__ = [
    "ALL_MODELS",
    "Announcement",
    "Assignment",
    "Atelier",
    "AtelierHall",
    "AuditLog",
    "AuditLogImmutableError",
    "BaseModel",
    "Equipment",
    "EquipmentUsage",
    "Event",
    "EventOffer",
    "ExpenseLine",
    "Hall",
    "HallAgreement",
    "HallPhoto",
    "Invoice",
    "InvoiceLine",
    "InvoicePayment",
    "JobApplication",
    "JobPost",
    "Notification",
    "OtpCode",
    "PayrollLine",
    "PortfolioItem",
    "Review",
    "RosterMember",
    "Setting",
    "StaffAvailability",
    "StaffProfile",
    "StaffSkill",
    "StockItem",
    "StockMovement",
    "TermsLockedError",
    "TermsMixin",
    "TimestampMixin",
    "UTCDateTime",
    "User",
    "check_int",
    "check_money",
    "coerce_int",
    "load_user",
    "normalize_bonus_tiers",
    "normalize_mobile",
    "normalize_skills",
    "normalize_staffing_needs",
    "normalize_terms",
    "normalize_terms_by_role",
    "to_latin_digits",
    "utcnow",
]
