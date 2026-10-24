"""The atelier directory a hall manager chooses from when offering dates."""

from __future__ import annotations

from typing import Optional

import sqlalchemy as sa
from sqlalchemy.orm import contains_eager

from app.extensions import db
from app.models import Atelier, Review, User
from app.modules.halls.queries import like_pattern
from app.services.constants import UserStatus

SORTS = (
    ("rating", "بیشترین امتیاز"),
    ("name", "نام"),
    ("new", "تازه‌ترین"),
)


def _ratings_subquery() -> sa.Subquery:
    return (
        sa.select(
            Review.atelier_id.label("atelier_id"),
            sa.func.avg(Review.rating).label("avg_rating"),
            sa.func.count(Review.id).label("rating_count"),
        )
        .where(Review.atelier_id.is_not(None), Review.is_hidden.is_(False))
        .group_by(Review.atelier_id)
        .subquery()
    )


def directory(
    query: str = "",
    city: str = "",
    sort: str = "rating",
    *,
    verified_only: bool = False,
    only_ids: Optional[list[int]] = None,
    exclude_ids: Optional[list[int]] = None,
) -> sa.Select:
    """Active ateliers (with an active manager) matching the filters, best first.

    The search text matches name, city and description; the verified badge is the manager's
    ``User.is_verified`` (set by the site admin).  ``only_ids`` restricts the list and
    ``exclude_ids`` leaves some out (the ateliers a form already selected).
    """
    ratings = _ratings_subquery()
    stmt = (
        sa.select(Atelier)
        .join(User, User.id == Atelier.manager_id)
        .outerjoin(ratings, ratings.c.atelier_id == Atelier.id)
        .where(Atelier.is_active.is_(True), User.status == UserStatus.ACTIVE)
        .options(contains_eager(Atelier.manager))
    )
    text = (query or "").strip()
    if text:
        pattern = like_pattern(text)
        stmt = stmt.where(
            sa.or_(
                Atelier.name.ilike(pattern, escape="\\"),
                Atelier.city.ilike(pattern, escape="\\"),
                Atelier.description.ilike(pattern, escape="\\"),
            )
        )
    if city:
        stmt = stmt.where(Atelier.city == city)
    if verified_only:
        stmt = stmt.where(User.is_verified.is_(True))
    if only_ids is not None:
        stmt = stmt.where(Atelier.id.in_(sorted(set(only_ids))))
    if exclude_ids:
        stmt = stmt.where(Atelier.id.not_in(sorted(set(exclude_ids))))
    if sort == "name":
        return stmt.order_by(Atelier.name, Atelier.id)
    if sort == "new":
        return stmt.order_by(Atelier.id.desc())
    return stmt.order_by(
        sa.func.coalesce(ratings.c.avg_rating, 0).desc(),
        sa.func.coalesce(ratings.c.rating_count, 0).desc(),
        Atelier.name,
        Atelier.id,
    )


def cities() -> list[str]:
    """Cities that have at least one active atelier (for the filter), alphabetically."""
    rows = db.session.execute(
        sa.select(Atelier.city)
        .join(User, User.id == Atelier.manager_id)
        .where(Atelier.is_active.is_(True), User.status == UserStatus.ACTIVE, Atelier.city != "")
        .group_by(Atelier.city)
        .order_by(Atelier.city)
        .limit(200)
    )
    return [city for (city,) in rows]


def active_atelier(atelier_id: int) -> Optional[Atelier]:
    """An atelier a hall may deal with (active, manager active), or ``None``."""
    return db.session.execute(
        sa.select(Atelier)
        .join(User, User.id == Atelier.manager_id)
        .where(Atelier.id == atelier_id, Atelier.is_active.is_(True), User.status == UserStatus.ACTIVE)
        .options(contains_eager(Atelier.manager))
    ).scalar_one_or_none()


def active_ateliers(ids: list[int]) -> list[Atelier]:
    """The active ateliers among *ids* (unknown / inactive ids are silently dropped), in name order."""
    if not ids:
        return []
    return list(db.session.execute(directory(sort="name", only_ids=ids)).scalars().unique())
