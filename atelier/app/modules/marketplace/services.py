"""Marketplace rules: posting, applying, accepting, inviting and reviewing.

Contract (same as ``app.services.staffing``): functions here use ``db.session`` only, ``add`` +
``flush`` but **never commit** - the route owns the transaction.  A rule violation raises
:class:`MarketplaceError` whose ``str()`` is a Persian message that is safe to show; ``code`` is a
stable machine word and ``status`` the HTTP status a route should answer with.

Everything that creates an afish row goes through ``staffing.assign_person`` (terms snapshot,
conflict check, audit, notification), pay maths is ``payroll`` - nothing is re-implemented here.

Bus events published (all payloads are plain ids):

``application.changed``   ``application_id, post_id, user_id, status, actor_id``
``review.created``        ``review_id, assignment_id, author_id, subject_id, rating``
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Any, Optional

import sqlalchemy as sa
from flask import url_for

from app.extensions import db
from app.models import (
    Assignment,
    Atelier,
    AuditLog,
    Event,
    JobApplication,
    JobPost,
    Review,
    StaffProfile,
    User,
)
from app.modules.marketplace import common, forms, queries
from app.services import audit, eventbus, notifications, security, staffing
from app.services.money import to_persian_digits
from app.services.constants import (
    ACTIVE_ASSIGNMENT_STATUSES,
    APPLICATION_TRANSITIONS,
    CLOSED_EVENT_STATUSES,
    ApplicationStatus,
    AssignmentSource,
    AssignmentStatus,
    JobPostStatus,
    NotificationKind,
    Role,
    Skill,
    UserStatus,
    is_legal_transition,
)

logger = logging.getLogger(__name__)

DAY = dt.timedelta(hours=24)


class MarketplaceError(Exception):
    """A rule was violated; ``str(exc)`` is a Persian message fit for the user."""

    def __init__(self, message: str, code: str = "blocked", status: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


# --------------------------------------------------------------------------
# plumbing
# --------------------------------------------------------------------------


def _url(endpoint: str, **values: Any) -> Optional[str]:
    try:
        return url_for(endpoint, **values)
    except Exception:  # noqa: BLE001 - a notification link is cosmetic
        return None


def _notify(user_id: Optional[int], kind: NotificationKind, title: str, body: str, url: Optional[str], entity: Any) -> None:
    """Best effort: a failing notification must never undo the change it announces."""
    if user_id is None:
        return
    try:
        notifications.notify(user_id, kind, title, body, url, entity=entity)
    except Exception:  # noqa: BLE001
        logger.exception("marketplace: notification failed")


def _publish_application(application: JobApplication, actor: Any) -> None:
    eventbus.publish(
        "application.changed",
        application_id=application.id,
        post_id=application.post_id,
        user_id=application.user_id,
        status=ApplicationStatus.coerce(application.status).value,
        actor_id=audit.actor_id(actor),
    )


def describe_staffing_error(exc: Exception) -> str:
    """Persian message for an exception raised by ``app.services.staffing``."""
    if isinstance(exc, staffing.TermsLocked):
        return "شرایط این افیش پس از ثبت فاکتور قفل شده است؛ ابتدا فاکتور را بازگشایی کنید."
    if isinstance(exc, staffing.DuplicateAssignment):
        return "این نفر همین نقش را از قبل در این مراسم دارد."
    if isinstance(exc, staffing.AssignmentConflict):
        return "این نفر در همان روز در مراسم دیگری افیش شده است."
    if isinstance(exc, staffing.EventNotStaffable):
        return "این مراسم دیگر قابل افیش‌کردن نیست."
    if isinstance(exc, staffing.InvalidTransition):
        return "این تغییر وضعیت در حالت فعلی مجاز نیست."
    if isinstance(exc, staffing.StaffingPermissionError):
        return "اجازهٔ انجام این کار را ندارید."
    if isinstance(exc, staffing.StaffingError):
        return "عملیات افیش انجام نشد؛ اطلاعات را بررسی کنید."
    return "عملیات انجام نشد."


def _staffing_failure(exc: staffing.StaffingError) -> MarketplaceError:
    code = {
        staffing.AssignmentConflict: "conflict",
        staffing.DuplicateAssignment: "duplicate",
        staffing.TermsLocked: "locked",
    }.get(type(exc), "staffing")
    return MarketplaceError(describe_staffing_error(exc), code)


# --------------------------------------------------------------------------
# spam protection: counted from the database, so it is exact across workers
# --------------------------------------------------------------------------


def _quota(name: str, used: int, what: str) -> None:
    cap = common.limit(name)
    if used >= cap:
        raise MarketplaceError(
            f"در ۲۴ ساعت گذشته بیش از حد مجاز ({to_persian_digits(cap)} مورد) {what} ثبت کرده‌اید؛ بعداً دوباره تلاش کنید.",
            "rate_limited",
            429,
        )


def check_post_quota(manager: User) -> None:
    since = security.now() - DAY
    used = db.session.scalar(
        sa.select(sa.func.count(JobPost.id))
        .join(Atelier, Atelier.id == JobPost.atelier_id)
        .where(Atelier.manager_id == manager.id, JobPost.created_at >= since)
    )
    _quota("MARKETPLACE_MAX_POSTS_PER_DAY", int(used or 0), "آگهی")


def check_application_quota(user: User) -> None:
    since = security.now() - DAY
    used = db.session.scalar(
        sa.select(sa.func.count(JobApplication.id)).where(
            JobApplication.user_id == user.id, JobApplication.created_at >= since
        )
    )
    _quota("MARKETPLACE_MAX_APPLICATIONS_PER_DAY", int(used or 0), "درخواست همکاری")


def check_invite_quota(manager: User) -> None:
    since = security.now() - DAY
    used = db.session.scalar(
        sa.select(sa.func.count(Assignment.id)).where(
            Assignment.created_by_id == manager.id,
            Assignment.source == AssignmentSource.MARKETPLACE,
            Assignment.job_application_id.is_(None),
            Assignment.created_at >= since,
        )
    )
    _quota("MARKETPLACE_MAX_INVITES_PER_DAY", int(used or 0), "دعوت")


# --------------------------------------------------------------------------
# posts
# --------------------------------------------------------------------------


def sync_post_status(post: JobPost) -> JobPost:
    """``open`` <-> ``filled`` according to the number of accepted applications.

    ``closed`` and ``hidden`` are decisions of a person and are left alone.
    """
    if post.status not in (JobPostStatus.OPEN, JobPostStatus.FILLED):
        return post
    wanted = JobPostStatus.FILLED if queries.count_accepted(post.id) >= post.slots else JobPostStatus.OPEN
    if post.status != wanted:
        post.status = wanted
        db.session.flush()
    return post


def post_is_locked(post: JobPost) -> bool:
    """Applications exist: role, date, event and pay terms are frozen (applicants saw them)."""
    return bool(
        db.session.scalar(
            sa.select(JobApplication.id)
            .where(
                JobApplication.post_id == post.id,
                JobApplication.status.in_([ApplicationStatus.PENDING, ApplicationStatus.ACCEPTED]),
            )
            .limit(1)
        )
    )


def create_post(manager: User, data: forms.JobInput) -> JobPost:
    check_post_quota(manager)
    if data.event is not None:
        duplicate = db.session.scalar(
            sa.select(JobPost.id)
            .where(
                JobPost.event_id == data.event.id,
                JobPost.role == data.role,
                JobPost.status.in_([JobPostStatus.OPEN, JobPostStatus.FILLED]),
            )
            .limit(1)
        )
        if duplicate:
            raise MarketplaceError(
                f"برای این مراسم آگهیِ باز «{data.role.label}» دارید؛ تعداد نیرو را در همان آگهی بیشتر کنید.", "duplicate"
            )
    post = JobPost(
        atelier=data.atelier,
        event=data.event,
        role=data.role,
        date=data.date,
        city=data.city,
        title=data.title,
        description=data.description,
        slots=data.slots,
        status=JobPostStatus.OPEN,
        created_at=security.now(),
    )
    post.apply_terms(data.terms)
    db.session.add(post)
    db.session.flush()
    audit.log(
        manager,
        "job_post.created",
        post,
        {"atelier_id": data.atelier.id, "role": data.role, "date": data.date, "slots": data.slots, "terms": data.terms},
    )
    return post


def update_post(post: JobPost, data: forms.JobInput, actor: User) -> JobPost:
    """Apply an edit.  The caller has already kept the frozen fields unchanged when applications exist."""
    before = {"title": post.title, "slots": post.slots, "city": post.city, "date": post.date, "terms": post.terms_dict()}
    post.title = data.title
    post.description = data.description
    post.city = data.city
    post.slots = data.slots
    post.role = data.role
    post.date = data.date
    post.event = data.event
    if data.terms != post.terms_dict():
        post.apply_terms(data.terms)
    db.session.flush()
    sync_post_status(post)
    audit.log(
        actor,
        "job_post.updated",
        post,
        {"before": before, "after": {"title": post.title, "slots": post.slots, "city": post.city, "date": post.date, "terms": post.terms_dict()}},
    )
    return post


def close_post(post: JobPost, actor: User, *, reject_pending: bool = True) -> int:
    """Close a post; by default every still-pending applicant is told and rejected.  Returns how many."""
    if post.status not in (JobPostStatus.OPEN, JobPostStatus.FILLED):
        raise MarketplaceError("این آگهی قبلاً بسته شده است.", "already")
    rejected = 0
    if reject_pending:
        pending = db.session.scalars(
            sa.select(JobApplication).where(
                JobApplication.post_id == post.id, JobApplication.status == ApplicationStatus.PENDING
            )
        ).all()
        for application in pending:
            reject_application(application, actor, "آگهی بسته شد و نیروی مورد نیاز تأمین شد.", _post=post)
            rejected += 1
    post.status = JobPostStatus.CLOSED
    db.session.flush()
    audit.log(actor, "job_post.closed", post, {"rejected_pending": rejected})
    return rejected


def reopen_post(post: JobPost, actor: User) -> JobPost:
    if post.status == JobPostStatus.HIDDEN:
        raise MarketplaceError("این آگهی توسط مدیر سایت مخفی شده و قابل بازگشایی نیست.", "hidden", 403)
    if post.status != JobPostStatus.CLOSED:
        raise MarketplaceError("این آگهی بسته نیست.", "already")
    if post.date < security.today():
        raise MarketplaceError("تاریخ این آگهی گذشته است؛ آگهی تازه ثبت کنید.", "expired")
    if post.event is not None and post.event.status in CLOSED_EVENT_STATUSES:
        raise MarketplaceError("مراسم مربوط به این آگهی دیگر برگزار نمی‌شود.", "event_closed")
    post.status = JobPostStatus.OPEN
    db.session.flush()
    sync_post_status(post)
    audit.log(actor, "job_post.reopened", post, {"status": post.status})
    return post


# --------------------------------------------------------------------------
# applying
# --------------------------------------------------------------------------


def check_can_apply(post: JobPost, user: User) -> None:
    """Raise :class:`MarketplaceError` unless *user* may apply to *post* right now.

    The same checks drive the apply button of the job page (it shows the reason instead of a form)
    and :func:`apply_to_post`.  Duplicates and the daily quota are checked by the latter only.
    """
    today = security.today()
    if user.role != Role.STAFF:
        raise MarketplaceError("فقط نیروها می‌توانند برای آگهی‌ها درخواست بدهند.", "forbidden", 403)
    if post.status == JobPostStatus.FILLED:
        raise MarketplaceError("ظرفیت این آگهی تکمیل شده است.", "filled")
    if post.status != JobPostStatus.OPEN or not post.atelier.is_active:
        raise MarketplaceError("این آگهی بسته شده است.", "closed")
    if post.date < today:
        raise MarketplaceError("تاریخ این همکاری گذشته است.", "expired")
    if post.event is not None and post.event.status in CLOSED_EVENT_STATUSES:
        raise MarketplaceError("مراسم مربوط به این آگهی دیگر برگزار نمی‌شود.", "event_closed")
    profile = user.staff_profile
    if profile is None or not profile.has_skill(post.role):
        raise MarketplaceError(
            f"برای درخواست باید مهارت «{post.role.label}» را در پروفایل خود ثبت کرده باشید.", "skill_required", 422
        )
    if staffing.conflicts_for(post.date, user_id=user.id):
        raise MarketplaceError(
            "شما در این تاریخ در مراسم دیگری افیش شده‌اید؛ برای همان روز نمی‌توانید درخواست بدهید.", "conflict"
        )
    if staffing.is_unavailable([user.id], post.date) is not None:
        raise MarketplaceError(
            "این روز را «در دسترس نیستم» ثبت کرده‌اید؛ ابتدا وضعیت آن روز را در تقویم خود تغییر دهید.", "unavailable"
        )


def apply_to_post(post: JobPost, user: User, message: Optional[str]) -> JobApplication:
    """A staff member applies.  Blocked (with a clear Persian reason) when the post is not open, the day
    has passed, the person lacks the role skill, is busy or marked the day unavailable, or applied too often."""
    check_can_apply(post, user)
    existing = db.session.scalar(
        sa.select(JobApplication).where(JobApplication.post_id == post.id, JobApplication.user_id == user.id)
    )
    if existing is not None:
        status = ApplicationStatus.coerce(existing.status)
        if status == ApplicationStatus.PENDING:
            raise MarketplaceError("درخواست شما برای این آگهی قبلاً ثبت شده و در انتظار بررسی است.", "duplicate")
        if status == ApplicationStatus.ACCEPTED:
            raise MarketplaceError("درخواست شما برای این آگهی پذیرفته شده است.", "duplicate")
        if status == ApplicationStatus.REJECTED:
            raise MarketplaceError("درخواست شما برای این آگهی رد شده است و دوباره قابل ارسال نیست.", "rejected")
    check_application_quota(user)

    now = security.now()
    if existing is not None:  # withdrawn earlier: bring the same row back (the pair is unique)
        application = existing
        application.status = ApplicationStatus.PENDING
        application.decided_at = None
        application.created_at = now
    else:
        application = JobApplication(post=post, user=user, status=ApplicationStatus.PENDING, created_at=now)
        db.session.add(application)
    application.message = message
    db.session.flush()
    audit.log(user, "job_application.created", application, {"post_id": post.id, "role": post.role})
    _publish_application(application, user)
    _notify(
        post.atelier.manager_id,
        NotificationKind.APPLICATION_RECEIVED,
        "درخواست همکاری تازه",
        f"{common.person_name(user)} برای آگهی «{post.title}» ({post.role.label}) درخواست داد.",
        _url("marketplace.manage_application", application_id=application.id),
        application,
    )
    return application


def withdraw_application(application: JobApplication, actor: User) -> JobApplication:
    """The applicant steps back.  An accepted application also cancels its afish row."""
    status = ApplicationStatus.coerce(application.status)
    if status not in (ApplicationStatus.PENDING, ApplicationStatus.ACCEPTED):
        raise MarketplaceError("این درخواست دیگر قابل انصراف نیست.", "already")
    post = application.post
    if status == ApplicationStatus.ACCEPTED:
        assignment = db.session.scalar(
            sa.select(Assignment).where(
                Assignment.job_application_id == application.id,
                Assignment.status.in_(list(ACTIVE_ASSIGNMENT_STATUSES)),
            )
        )
        if assignment is not None:
            if assignment.status == AssignmentStatus.COMPLETED:
                raise MarketplaceError("این همکاری انجام شده است و قابل انصراف نیست.", "completed")
            try:
                staffing.transition(assignment, AssignmentStatus.CANCELLED, actor, reason="انصراف نیرو از آگهی")
            except staffing.StaffingError as exc:
                raise _staffing_failure(exc) from exc
    application.status = ApplicationStatus.WITHDRAWN  # (the bus subscriber may already have done this)
    application.decided_at = security.now()
    db.session.flush()
    sync_post_status(post)
    audit.log(actor, "job_application.withdrawn", application, {"post_id": post.id, "was": status})
    _publish_application(application, actor)
    if status == ApplicationStatus.PENDING:  # for an accepted one staffing already told the manager
        _notify(
            post.atelier.manager_id,
            NotificationKind.APPLICATION_DECIDED,
            "انصراف از درخواست",
            f"{common.person_name(application.user)} درخواست خود را برای آگهی «{post.title}» پس گرفت.",
            _url("marketplace.job_applicants", post_id=post.id),
            application,
        )
    return application


def reject_application(
    application: JobApplication, actor: User, message: Optional[str] = None, *, _post: Optional[JobPost] = None
) -> JobApplication:
    status = ApplicationStatus.coerce(application.status)
    if status != ApplicationStatus.PENDING:
        raise MarketplaceError("فقط درخواست‌های در انتظار بررسی را می‌توان رد کرد.", "already")
    post = _post or application.post
    application.status = ApplicationStatus.REJECTED
    application.decided_at = security.now()
    db.session.flush()
    audit.log(actor, "job_application.rejected", application, {"post_id": post.id, "message": message})
    _publish_application(application, actor)
    body = f"درخواست شما برای آگهی «{post.title}» ({post.role.label}) پذیرفته نشد."
    if message:
        body += f"\nپیام اتلیه: {message}"
    _notify(
        application.user_id,
        NotificationKind.APPLICATION_DECIDED,
        "نتیجهٔ درخواست همکاری",
        body,
        _url("marketplace.my_applications"),
        application,
    )
    return application


def accept_application(application: JobApplication, event: Optional[Event], actor: User) -> Assignment:
    """Accept: create the afish row with the **post's terms as the snapshot** (source: marketplace).

    The applicant said yes by applying, so the assignment starts ``confirmed``.  The usual staffing
    checks apply (double booking, duplicate role, event still open for staffing) and the post's
    ``filled`` status follows the number of accepted applications.
    """
    # Serialise accepts of one post (SELECT ... FOR UPDATE on PostgreSQL; SQLite has one writer anyway).
    post = db.session.execute(
        sa.select(JobPost).where(JobPost.id == application.post_id).with_for_update().execution_options(populate_existing=True)
    ).scalar_one()
    db.session.refresh(application)
    status = ApplicationStatus.coerce(application.status)
    if status == ApplicationStatus.ACCEPTED:
        raise MarketplaceError("این درخواست قبلاً پذیرفته شده است.", "already")
    if status != ApplicationStatus.PENDING or not is_legal_transition(
        APPLICATION_TRANSITIONS, status, ApplicationStatus.ACCEPTED
    ):
        raise MarketplaceError("فقط درخواست‌های در انتظار بررسی را می‌توان پذیرفت.", "already")
    if post.status == JobPostStatus.FILLED or queries.count_accepted(post.id) >= post.slots:
        raise MarketplaceError("ظرفیت این آگهی تکمیل شده است.", "filled")
    if post.status != JobPostStatus.OPEN:
        raise MarketplaceError("این آگهی بسته است؛ ابتدا آن را دوباره باز کنید.", "closed")
    if post.date < security.today():
        raise MarketplaceError("تاریخ این همکاری گذشته است.", "expired")
    if event is None:
        raise MarketplaceError("مراسمی را که این نیرو در آن کار می‌کند انتخاب کنید.", "event_required", 422)
    if event.atelier_id != post.atelier_id or event.date != post.date:
        raise MarketplaceError("مراسم انتخاب‌شده معتبر نیست.", "event_invalid", 422)
    if post.event_id is not None and event.id != post.event_id:
        raise MarketplaceError("این آگهی برای مراسم دیگری است.", "event_invalid", 422)
    applicant = application.user
    if applicant.status == UserStatus.SUSPENDED:
        raise MarketplaceError("حساب این نیرو معلق است و افیش نمی‌شود.", "suspended")

    try:
        assignment = staffing.assign_person(
            event,
            post.role,
            user=applicant,
            terms=post.terms_dict(),  # the snapshot: exactly what the applicant saw
            actor=actor,
            source=AssignmentSource.MARKETPLACE,
            status=AssignmentStatus.CONFIRMED,
            job_application=application,
        )
    except staffing.StaffingError as exc:
        raise _staffing_failure(exc) from exc

    application.status = ApplicationStatus.ACCEPTED
    application.decided_at = security.now()
    db.session.flush()
    sync_post_status(post)
    audit.log(
        actor,
        "job_application.accepted",
        application,
        {"post_id": post.id, "assignment_id": assignment.id, "event_id": event.id, "terms": assignment.terms_dict()},
    )
    _publish_application(application, actor)
    _notify(
        application.user_id,
        NotificationKind.APPLICATION_DECIDED,
        "درخواست شما پذیرفته شد",
        f"اتلیه {post.atelier.name} شما را برای «{post.title}» ({post.role.label}) پذیرفت. "
        f"شرایط: {common.terms_text(assignment.terms_dict())}",
        _url("marketplace.my_applications"),
        application,
    )
    return assignment


def on_assignment_changed(assignment_id: int, change: str = "", new_status: Optional[str] = None, **_: Any) -> None:
    """Bus subscriber: an afish row that came from an application was cancelled / declined.

    The application goes back to ``withdrawn`` and the post may be ``open`` again, so the fill count
    and the status stay true when the atelier cancels from its afish board or the event is cancelled.
    """
    if change != "status" or new_status not in (AssignmentStatus.CANCELLED.value, AssignmentStatus.DECLINED.value):
        return
    with db.session.begin_nested():
        assignment = db.session.get(Assignment, assignment_id)
        if assignment is None or assignment.job_application_id is None:
            return
        application = db.session.get(JobApplication, assignment.job_application_id)
        if application is None or application.status != ApplicationStatus.ACCEPTED:
            return
        application.status = ApplicationStatus.WITHDRAWN
        application.decided_at = security.now()
        db.session.flush()
        sync_post_status(application.post)


# --------------------------------------------------------------------------
# invitations
# --------------------------------------------------------------------------


def invite_talent(
    manager: User,
    talent: User,
    event: Event,
    role: Skill,
    terms: dict[str, Any],
    notes: Optional[str],
    *,
    allow_conflict: bool = False,
) -> Assignment:
    """Invite a listed (or already connected) staff member to one role of one event.

    The invitation *is* an assignment in status ``invited`` (the person accepts or declines it from
    ``/jobs/invitations`` or from their afish page); ``source`` is ``marketplace``.
    """
    check_invite_quota(manager)
    if event.atelier is None or event.atelier.manager_id != manager.id:
        raise MarketplaceError("مراسم انتخاب‌شده معتبر نیست.", "event_invalid", 422)
    if event.date < security.today():
        raise MarketplaceError("تاریخ این مراسم گذشته است.", "expired")
    profile: Optional[StaffProfile] = talent.staff_profile
    if profile is None or not profile.has_skill(role):
        raise MarketplaceError(f"این نیرو مهارت «{role.label}» را ثبت نکرده است.", "skill_required", 422)
    try:
        assignment = staffing.assign_person(
            event,
            role,
            user=talent,
            terms=terms,
            actor=manager,
            source=AssignmentSource.MARKETPLACE,
            notes=notes,
            allow_conflict=allow_conflict,
        )
    except staffing.StaffingError as exc:
        raise _staffing_failure(exc) from exc
    audit.log(
        manager,
        "talent.invited",
        assignment,
        {"event_id": event.id, "role": role, "user_id": talent.id, "terms": terms, "override_conflict": allow_conflict},
    )
    return assignment


def respond_to_invitation(assignment: Assignment, user: User, accept: bool) -> Assignment:
    """The invited person answers; the staffing state machine and its notifications do the rest."""
    target = AssignmentStatus.ACCEPTED if accept else AssignmentStatus.DECLINED
    if assignment.status != AssignmentStatus.INVITED:
        raise MarketplaceError("به این دعوت قبلاً پاسخ داده‌اید.", "already")
    try:
        staffing.transition(assignment, target, user)
    except staffing.StaffingError as exc:
        if isinstance(exc, staffing.AssignmentConflict):
            raise MarketplaceError("در همین تاریخ مراسم قطعی دیگری دارید؛ این دعوت را نمی‌توانید بپذیرید.", "conflict") from exc
        raise _staffing_failure(exc) from exc
    return assignment


# --------------------------------------------------------------------------
# reviews
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ReviewContext:
    """Who reviews whom for one assignment."""

    side: str  # 'atelier' (the manager reviews the person) | 'staff' (the person reviews the atelier)
    subject_id: int
    subject_name: str
    atelier_name: str


def review_context(assignment: Assignment, user: User) -> Optional[ReviewContext]:
    """The review *user* may write for *assignment*, or ``None`` if they are not a party to it."""
    event = assignment.event
    atelier = event.atelier
    if atelier is None:
        return None
    person_id = assignment.person_user_id
    if atelier.manager_id == user.id and user.role == Role.ATELIER_MANAGER:
        if person_id is None:
            return None
        person = assignment.user or (assignment.roster_member.user if assignment.roster_member else None)
        return ReviewContext("atelier", person_id, common.person_name(person) if person else "", atelier.name)
    if person_id == user.id:
        return ReviewContext("staff", atelier.manager_id, atelier.name, atelier.name)
    return None


def create_review(assignment: Assignment, author: User, rating: int, comment: Optional[str]) -> Review:
    """One review per author per completed assignment; recomputes the subject's staff rating."""
    context = review_context(assignment, author)
    if context is None:
        raise MarketplaceError("شما در این همکاری نقشی ندارید.", "forbidden", 403)
    if assignment.status != AssignmentStatus.COMPLETED:
        raise MarketplaceError("فقط پس از برگزاری مراسم می‌توان نظر داد.", "not_completed")
    existing = db.session.scalar(
        sa.select(Review.id).where(Review.assignment_id == assignment.id, Review.author_id == author.id)
    )
    if existing is not None:
        raise MarketplaceError("برای این همکاری قبلاً نظر داده‌اید.", "duplicate")
    review = Review(
        assignment=assignment,
        author=author,
        subject_id=context.subject_id,
        rating=rating,
        comment=comment,
        created_at=security.now(),
    )
    db.session.add(review)
    db.session.flush()
    profile = db.session.scalar(sa.select(StaffProfile).where(StaffProfile.user_id == context.subject_id))
    if profile is not None:
        profile.refresh_rating()
        db.session.flush()
    audit.log(author, "review.created", review, {"assignment_id": assignment.id, "rating": rating, "side": context.side})
    eventbus.publish(
        "review.created",
        review_id=review.id,
        assignment_id=assignment.id,
        author_id=author.id,
        subject_id=context.subject_id,
        rating=rating,
    )
    _notify(
        context.subject_id,
        NotificationKind.REVIEW_RECEIVED,
        "نظر تازه دریافت کردید",
        f"{to_persian_digits(rating)} از ۵ ستاره برای همکاری در مراسم {assignment.event.hall.name if assignment.event.hall else ''}".strip(),
        _url("marketplace.reviews", tab="received"),
        review,
    )
    return review


# --------------------------------------------------------------------------
# trust and safety: reports (the admin module decides: hide a post / a review)
# --------------------------------------------------------------------------

REPORT_REASONS: tuple[tuple[str, str], ...] = (
    ("fake", "آگهی یا نظر جعلی و فریبنده"),
    ("money", "درخواست پول یا پیش‌پرداخت"),
    ("abuse", "توهین یا محتوای نامناسب"),
    ("privacy", "انتشار اطلاعات شخصی"),
    ("other", "سایر"),
)
REPORT_ACTIONS = ("job_post.reported", "review.reported")
MAX_ADMINS_NOTIFIED = 20


def report_entity(user: User, action: str, entity: Any, reason: str, details: Optional[str], url: Optional[str]) -> None:
    """Record a report in the audit log and tell the admins.

    One report per person per entity, and a daily cap per person.  Nothing is hidden automatically: an admin
    reads the report and uses the admin module (``JobPost.status = hidden`` / ``Review.is_hidden``).
    """
    if action not in REPORT_ACTIONS:
        raise ValueError(action)
    if reason not in dict(REPORT_REASONS):
        raise MarketplaceError("دلیل گزارش را انتخاب کنید.", "validation_error", 422)
    table = entity.__tablename__
    already = db.session.scalar(
        sa.select(AuditLog.id).where(
            AuditLog.actor_id == user.id, AuditLog.action == action, AuditLog.entity_type == table, AuditLog.entity_id == entity.id
        ).limit(1)
    )
    if already is not None:
        raise MarketplaceError("پیش‌تر این مورد را گزارش داده‌اید؛ مدیر سایت بررسی می‌کند.", "duplicate")
    used = db.session.scalar(
        sa.select(sa.func.count(AuditLog.id)).where(
            AuditLog.actor_id == user.id, AuditLog.action.in_(REPORT_ACTIONS), AuditLog.created_at >= security.now() - DAY
        )
    )
    _quota("MARKETPLACE_MAX_REPORTS_PER_DAY", int(used or 0), "گزارش")
    audit.log(user, action, entity, {"reason": reason, "details": details})
    admins = db.session.scalars(
        sa.select(User.id).where(User.role == Role.ADMIN, User.status == UserStatus.ACTIVE).order_by(User.id).limit(MAX_ADMINS_NOTIFIED)
    ).all()
    label = dict(REPORT_REASONS)[reason]
    what = "آگهی" if action == "job_post.reported" else "نظر"
    for admin_id in admins:
        _notify(admin_id, NotificationKind.SYSTEM, f"گزارش {what}", f"دلیل: {label}" + (f"\n{details}" if details else ""), url, entity)
