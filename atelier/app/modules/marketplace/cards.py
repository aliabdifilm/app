"""View models for the lists: a job card carries everything a template needs, loaded in bulk."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

from app.models import Atelier, JobPost, StaffProfile, User
from app.modules.marketplace import common, queries
from app.services import security
from app.services.constants import CLOSED_EVENT_STATUSES, JobPostStatus, Role


@dataclass
class JobCard:
    """One job post as shown on the board, the detail page and the manage list."""

    post: JobPost
    atelier: Atelier
    verified: bool
    rating: queries.Rating
    accepted: int
    terms: dict[str, Any]
    lines: list[str]
    example: Optional[dict[str, Any]]
    hall_name: Optional[str] = None  # only for signed-in viewers
    expired: bool = False
    applications: dict[str, int] = field(default_factory=dict)

    @property
    def slots_left(self) -> int:
        return max(0, self.post.slots - self.accepted)

    @property
    def is_open(self) -> bool:
        event = self.post.event
        return (
            self.post.status == JobPostStatus.OPEN
            and not self.expired
            and self.slots_left > 0
            and (event is None or event.status not in CLOSED_EVENT_STATUSES)
        )

    @property
    def pending(self) -> int:
        return self.applications.get("pending", 0)

    @property
    def has_terms(self) -> bool:
        return not common.terms_are_empty(self.terms)


def build_cards(
    posts: Sequence[JobPost], viewer: Any = None, *, with_applications: bool = False
) -> list[JobCard]:
    """Cards for *posts* with three grouped queries at most (accepted counts, ratings, applicant counts)."""
    if not posts:
        return []
    today = security.today()
    ids = [post.id for post in posts]
    accepted = queries.accepted_counts(ids)
    ratings = queries.atelier_ratings({post.atelier_id for post in posts})
    counts = queries.application_counts(ids) if with_applications else {}
    signed_in = viewer is not None and getattr(viewer, "is_authenticated", False)
    cards = []
    for post in posts:
        terms = post.terms_dict()
        hall = post.event.hall if post.event is not None else None
        cards.append(
            JobCard(
                post=post,
                atelier=post.atelier,
                verified=bool(post.atelier.manager.is_verified),
                rating=ratings.get(post.atelier_id, queries.NO_RATING),
                accepted=accepted.get(post.id, 0),
                terms=terms,
                lines=common.terms_lines(terms),
                example=common.pay_example(terms),
                hall_name=hall.name if (hall is not None and signed_in) else None,
                expired=post.date < today,
                applications=counts.get(post.id, {}),
            )
        )
    return cards


@dataclass
class TalentCard:
    """One staff profile in the directory."""

    profile: StaffProfile
    user: User
    skills: list[str]
    worked_with_me: bool = False


def build_talent_cards(profiles: Sequence[StaffProfile], viewer: Any) -> list[TalentCard]:
    # an admin is a "partner" of everybody; the badge is only meaningful for the managers themselves
    is_admin = getattr(viewer, "role", None) == Role.ADMIN
    partners = set() if is_admin else queries.committed_partners(viewer, [p.user_id for p in profiles])
    return [
        TalentCard(
            profile=profile,
            user=profile.user,
            skills=profile.skills,
            worked_with_me=profile.user_id in partners,
        )
        for profile in profiles
    ]
