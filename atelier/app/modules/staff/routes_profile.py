"""Profile, avatar, portfolio and received reviews (skills, bio, equipment, listing visibility)."""

from __future__ import annotations

from typing import Any, Optional

import sqlalchemy as sa
from flask import flash, redirect, render_template, request, url_for

from app.extensions import db
from app.models import PortfolioItem
from app.modules.staff import bp, common, profile as logic, queries, web
from app.services import audit, uploads
from app.services.constants import Role, Skill
from app.services.forms import Form
from app.services.http import paginate
from app.services.permissions import get_user, owned_or_404, role_required

STAFF = Role.STAFF
PER_PAGE = 10


# -- profile ------------------------------------------------------------------------


def _profile_context(user, form: Optional[dict] = None, errors: Optional[dict] = None) -> dict[str, Any]:
    profile = user.staff_profile
    completeness = logic.completeness(user, profile, queries.portfolio_count(user.id))
    return {
        "form": form if form is not None else logic.form_values(user, profile),
        "errors": errors or {},
        "profile": profile,
        "completeness": completeness,
        "equipment": logic.EQUIPMENT,
        "skills": Skill.choices(),
        "bio_max": logic.BIO_MAX,
    }


@bp.route("/profile", methods=["GET", "POST"])
@role_required(STAFF, allow_pending=True)
def profile():
    user = get_user()
    if request.method == "GET":
        return render_template("staff/profile.html", **_profile_context(user))

    form = Form(request.form)
    data = logic.parse_form(form)
    if data is None:
        return render_template("staff/profile.html", **_profile_context(user, form.values, form.errors)), 422
    record = logic.get_or_create_profile(user)
    before = logic.form_values(user, record)
    was_listed = record.is_listed
    logic.apply(user, record, data)
    db.session.flush()
    after = logic.form_values(user, record)
    changed = sorted(key for key in after if after[key] != before.get(key))
    audit.log(user, "staff_profile.updated", record, {"fields": changed, "skills": list(record.skills)})
    if was_listed != record.is_listed:
        audit.log(user, "staff_profile.listed" if record.is_listed else "staff_profile.unlisted", record)
    db.session.commit()
    flash("پروفایل ذخیره شد.", "success")
    return redirect(url_for("staff.profile"))


@bp.post("/profile/avatar")
@role_required(STAFF, allow_pending=True)
def avatar_upload():
    user = get_user()
    try:
        stored = uploads.save_image(request.files.get("avatar"), "avatar", min_side=100)
    except uploads.UploadError as exc:
        return render_template("staff/profile.html", **_profile_context(user, errors={"avatar": str(exc)})), 422
    old, user.avatar_path = user.avatar_path, stored.path
    audit.log(user, "staff_profile.avatar_changed", user)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        uploads.delete_image(stored.path)
        raise
    uploads.delete_image(old)  # only after the new path is safely stored
    flash("عکس پروفایل ذخیره شد.", "success")
    return redirect(url_for("staff.profile"))


@bp.post("/profile/avatar/remove")
@role_required(STAFF, allow_pending=True)
def avatar_remove():
    user = get_user()
    old, user.avatar_path = user.avatar_path, None
    if old:
        audit.log(user, "staff_profile.avatar_removed", user)
    db.session.commit()
    uploads.delete_image(old)
    flash("عکس پروفایل حذف شد.", "success")
    return redirect(url_for("staff.profile"))


# -- portfolio ----------------------------------------------------------------------


def _owned_item(item_id: int) -> PortfolioItem:
    return owned_or_404(PortfolioItem, item_id, lambda item, user: item.user_id == user.id)


def _portfolio_page(user, form: Optional[dict] = None, errors: Optional[dict] = None, status: int = 200):
    items = queries.portfolio_items(user.id)
    return (
        render_template(
            "staff/portfolio.html",
            items=items,
            form=form or {"is_public": True},
            errors=errors or {},
            max_items=logic.MAX_PORTFOLIO,
            full=len(items) >= logic.MAX_PORTFOLIO,
        ),
        status,
    )


@bp.get("/portfolio")
@role_required(STAFF, allow_pending=True)
def portfolio():
    return _portfolio_page(get_user())


@bp.post("/portfolio")
@role_required(STAFF, allow_pending=True)
def portfolio_add():
    user = get_user()
    form = Form(request.form)
    title = form.text("title", "عنوان", max_len=150, default="")
    caption = form.text("caption", "توضیح", max_len=500, multiline=True)
    is_public = form.boolean("is_public")
    if queries.portfolio_count(user.id) >= logic.MAX_PORTFOLIO:
        form.error("image", "به سقف تعداد نمونه‌کار رسیده‌اید؛ ابتدا یکی را حذف کنید.")
    stored = None
    if form.ok:
        try:
            stored = uploads.save_image(request.files.get("image"), "portfolio", min_side=300)
        except uploads.UploadError as exc:
            form.error("image", str(exc))
    if not form.ok or stored is None:
        return _portfolio_page(user, form.values, form.errors, 422)
    last = db.session.scalar(sa.select(sa.func.max(PortfolioItem.sort_order)).where(PortfolioItem.user_id == user.id))
    item = PortfolioItem(
        user_id=user.id,
        title=title or "",
        caption=caption,
        image_path=stored.path,
        sort_order=(last if last is not None else -1) + 1,
        is_public=is_public,
    )
    db.session.add(item)
    try:
        db.session.flush()
        audit.log(user, "portfolio.added", item, {"public": is_public})
        db.session.commit()
    except Exception:
        db.session.rollback()
        uploads.delete_image(stored.path)
        raise
    flash("نمونه‌کار اضافه شد.", "success")
    return redirect(url_for("staff.portfolio"))


@bp.post("/portfolio/<int:item_id>/update")
@role_required(STAFF, allow_pending=True)
def portfolio_update(item_id: int):
    user = get_user()
    item = _owned_item(item_id)
    form = Form(request.form)
    title = form.text("title", "عنوان", max_len=150, default="")
    caption = form.text("caption", "توضیح", max_len=500, multiline=True)
    is_public = form.boolean("is_public")
    if not form.ok:
        errors = {f"{key}-{item.id}": message for key, message in form.errors.items()}
        return _portfolio_page(user, {}, errors, 422)
    item.title = title or ""
    item.caption = caption
    item.is_public = is_public
    audit.log(user, "portfolio.updated", item, {"public": is_public})
    db.session.commit()
    flash("نمونه‌کار ذخیره شد.", "success")
    return redirect(url_for("staff.portfolio"))


@bp.post("/portfolio/<int:item_id>/move")
@role_required(STAFF, allow_pending=True)
def portfolio_move(item_id: int):
    user = get_user()
    item = _owned_item(item_id)
    direction = request.form.get("direction")
    items = queries.portfolio_items(user.id)
    for index, other in enumerate(items):  # normalise first: new rows all start at the same sort order
        other.sort_order = index
    position = next(index for index, other in enumerate(items) if other.id == item.id)
    swap = position - 1 if direction == "up" else position + 1 if direction == "down" else position
    if 0 <= swap < len(items) and swap != position:
        items[position].sort_order, items[swap].sort_order = swap, position
    db.session.commit()
    return redirect(url_for("staff.portfolio") + f"#item-{item.id}")


@bp.post("/portfolio/<int:item_id>/delete")
@role_required(STAFF, allow_pending=True)
def portfolio_delete(item_id: int):
    user = get_user()
    item = _owned_item(item_id)
    path = item.image_path
    audit.log(user, "portfolio.deleted", item)
    db.session.delete(item)
    db.session.commit()
    uploads.delete_image(path)
    flash("نمونه‌کار حذف شد.", "success")
    return redirect(url_for("staff.portfolio"))


# -- reviews --------------------------------------------------------------------------


@bp.get("/reviews")
@role_required(STAFF)
def reviews():
    user = get_user()
    page = paginate(queries.reviews_select(user.id), per_page=PER_PAGE)
    return render_template(
        "staff/reviews.html",
        page=page,
        profile=user.staff_profile,
        reviews_url=web.optional_url("marketplace.reviews"),
        person_label=common.person_label,
    )
