"""Safe image uploads for avatars, atelier logos, portfolio pictures and friends.

The pipeline (one function for every module, so the rules cannot drift)::

    from app.services import uploads

    try:
        stored = uploads.save_image(request.files.get("avatar"), "avatar")
    except uploads.UploadError as exc:
        form.error("avatar", str(exc))          # Persian message, safe to show
    else:
        old, user.avatar_path = user.avatar_path, stored.path   # store the *relative* path
        uploads.delete_image(old)                               # after db.session.commit()

and in a template ``<img src="{{ media_url(user.avatar_path) }}">``.

What ``save_image`` guarantees
* the declared extension must be an image extension **and** the bytes must be a
  real JPEG / PNG / WebP (magic bytes + a parsed header with sane dimensions);
  SVG, GIF, HEIC bytes, scripts renamed to ``.jpg`` and truncated files are rejected;
* a size cap (``MAX_UPLOAD_BYTES``, default 5 MiB) enforced while reading, and a
  pixel cap (``MAX_IMAGE_PIXELS``) against decompression bombs;
* the original file name is discarded: the file is stored as
  ``<UPLOAD_DIR>/<kind>/<2 hex>/<32 random hex>.<ext>`` with the extension taken
  from the *content*; the returned ``path`` is that part below ``UPLOAD_DIR``;
* files live **outside** ``static/`` and are only reachable through
  ``/media/<path>``, which re-checks visibility on every request and answers
  404 (never 403) when you may not see a file.

Visibility per ``kind`` (see :func:`register_kind` for your own kinds)
=========  =====================================================================
avatar     any logged-in user; anonymous visitors only if the owner has a public profile
logo       everyone (atelier branding)
portfolio  owner and admins; otherwise only ``is_public`` items of public profiles
           (or any logged-in user, like avatars)
other      logged-in users, unless you register a checker
=========  =====================================================================

EXIF metadata is **not** stripped (rotating phone photos depend on the
orientation tag); do not promise location privacy to users.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from flask import Response, abort, current_app, send_file, url_for
from flask_login import current_user

from app.extensions import db

logger = logging.getLogger(__name__)

__all__ = [
    "UploadError",
    "StoredImage",
    "ImageInfo",
    "sniff_image",
    "save_image",
    "delete_image",
    "media_url",
    "register_kind",
    "can_view",
    "serve",
]

DEFAULT_MAX_BYTES = 5 * 1024 * 1024
DEFAULT_MAX_PIXELS = 64_000_000

# extensions a browser may send; the *content* decides what is stored
_DECLARED_EXTENSIONS = {
    "jpg": "jpg", "jpeg": "jpg", "jpe": "jpg", "jfif": "jpg",
    "png": "png",
    "webp": "webp",
    "heic": None, "heif": None,  # iOS keeps these names after transcoding to JPEG
}
_MIME = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}
_KIND_RE = re.compile(r"^[a-z][a-z0-9_]{0,30}$")
_PATH_RE = re.compile(
    r"^(?P<kind>[a-z][a-z0-9_]{0,30})/(?P<shard>[0-9a-f]{2})/(?P<name>[0-9a-f]{32})\.(?P<ext>jpg|png|webp)$"
)


class UploadError(ValueError):
    """The upload was rejected; ``str(exc)`` is a Persian message fit for the form."""


@dataclass(frozen=True)
class ImageInfo:
    ext: str  # 'jpg' | 'png' | 'webp'
    mime: str
    width: int
    height: int


@dataclass(frozen=True)
class StoredImage:
    path: str  # relative to UPLOAD_DIR, e.g. 'avatar/3f/3f9c...e1.jpg' (store this in the database)
    size: int
    mime: str
    width: int
    height: int

    @property
    def url(self) -> str:
        return media_url(self.path) or ""


# --------------------------------------------------------------------------
# sniffing
# --------------------------------------------------------------------------

_SOF_MARKERS = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}


def _jpeg_size(data: bytes) -> Optional[tuple[int, int]]:
    """(width, height) of a JPEG, or None if the header is malformed."""
    if not data.startswith(b"\xff\xd8\xff"):
        return None
    pos = 2
    length = len(data)
    while pos + 4 <= length:
        if data[pos] != 0xFF:
            return None
        while pos < length and data[pos] == 0xFF:  # fill bytes
            pos += 1
        if pos >= length:
            return None
        marker = data[pos]
        pos += 1
        if marker in (0x01, 0xD8) or 0xD0 <= marker <= 0xD7:  # no payload
            continue
        if marker == 0xD9:  # EOI before any frame header
            return None
        if pos + 2 > length:
            return None
        (seg_len,) = struct.unpack(">H", data[pos : pos + 2])
        if seg_len < 2:
            return None
        if marker in _SOF_MARKERS:
            if pos + 7 > length:
                return None
            height, width = struct.unpack(">HH", data[pos + 3 : pos + 7])
            return (width, height) if width and height else None
        if marker == 0xDA:  # scan data starts without a frame header
            return None
        pos += seg_len
    return None


def _png_size(data: bytes) -> Optional[tuple[int, int]]:
    if len(data) < 33 or not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return None
    length, ctype = struct.unpack(">I4s", data[8:16])
    if ctype != b"IHDR" or length != 13:
        return None
    width, height = struct.unpack(">II", data[16:24])
    return (width, height) if width and height else None


def _webp_size(data: bytes) -> Optional[tuple[int, int]]:
    if len(data) < 30 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        return None
    kind = data[12:16]
    if kind == b"VP8 ":
        if data[23:26] != b"\x9d\x01\x2a":
            return None
        width = struct.unpack("<H", data[26:28])[0] & 0x3FFF
        height = struct.unpack("<H", data[28:30])[0] & 0x3FFF
    elif kind == b"VP8L":
        if data[20] != 0x2F:
            return None
        bits = struct.unpack("<I", data[21:25])[0]
        width = (bits & 0x3FFF) + 1
        height = ((bits >> 14) & 0x3FFF) + 1
    elif kind == b"VP8X":
        width = int.from_bytes(data[24:27], "little") + 1
        height = int.from_bytes(data[27:30], "little") + 1
    else:
        return None
    return (width, height) if width and height else None


def sniff_image(data: bytes) -> Optional[ImageInfo]:
    """Identify JPEG / PNG / WebP by magic bytes and read the dimensions; None if it is none of them."""
    for ext, parser in (("jpg", _jpeg_size), ("png", _png_size), ("webp", _webp_size)):
        size = parser(data)
        if size:
            return ImageInfo(ext, _MIME[ext], size[0], size[1])
    return None


# --------------------------------------------------------------------------
# paths
# --------------------------------------------------------------------------


def _upload_root() -> Path:
    return Path(current_app.config["UPLOAD_DIR"])


def _resolve(relpath: str) -> Optional[Path]:
    """Absolute path of a stored file, or None if *relpath* is not one of ours."""
    if not isinstance(relpath, str) or not _PATH_RE.match(relpath):
        return None
    root = _upload_root().resolve()
    candidate = (root / relpath).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate


def media_url(relpath: Optional[str]) -> Optional[str]:
    """URL of a stored file (``None`` for an empty path) - also a Jinja global."""
    if not relpath:
        return None
    return url_for("main.media", relpath=relpath)


# --------------------------------------------------------------------------
# saving and deleting
# --------------------------------------------------------------------------


def _limits() -> tuple[int, int]:
    config = current_app.config
    return int(config.get("MAX_UPLOAD_BYTES", DEFAULT_MAX_BYTES)), int(
        config.get("MAX_IMAGE_PIXELS", DEFAULT_MAX_PIXELS)
    )


def _megabytes(size: int) -> str:
    from app.services.money import to_persian_digits

    return to_persian_digits(f"{size / (1024 * 1024):g}")


def save_image(
    file: Any,
    kind: str,
    *,
    max_bytes: Optional[int] = None,
    min_side: Optional[int] = None,
) -> StoredImage:
    """Validate and store an uploaded image (a werkzeug ``FileStorage``).

    Raises :class:`UploadError` (Persian message) when the file is missing,
    not an acceptable image, too big or too small (*min_side* pixels).
    """
    if not _KIND_RE.match(kind or ""):
        raise ValueError(f"invalid upload kind {kind!r}")
    filename = getattr(file, "filename", None)
    if file is None or not filename:
        raise UploadError("تصویری انتخاب نشده است.")
    declared = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if declared not in _DECLARED_EXTENSIONS:
        raise UploadError("فقط تصویر با فرمت JPG، PNG یا WebP پذیرفته می‌شود.")

    limit, max_pixels = _limits()
    if max_bytes is not None:
        limit = min(limit, int(max_bytes))
    data = file.stream.read(limit + 1)
    if len(data) > limit:
        raise UploadError(f"حجم تصویر نباید بیشتر از {_megabytes(limit)} مگابایت باشد.")
    if not data:
        raise UploadError("فایل انتخاب‌شده خالی است.")

    info = sniff_image(data)
    if info is None:
        if data[4:12] in (b"ftypheic", b"ftypheix", b"ftypmif1", b"ftypheif"):
            raise UploadError("فرمت HEIC پشتیبانی نمی‌شود؛ عکس را به JPG تبدیل کنید یا در تنظیمات دوربین «سازگارترین» را بزنید.")
        raise UploadError("این فایل یک تصویر معتبر JPG، PNG یا WebP نیست.")
    expected = _DECLARED_EXTENSIONS[declared]
    if expected is not None and expected != info.ext:
        raise UploadError("نوع فایل با پسوند آن همخوانی ندارد.")
    if info.width * info.height > max_pixels:
        raise UploadError("ابعاد تصویر بیش از حد بزرگ است.")
    if min_side and min(info.width, info.height) < min_side:
        raise UploadError(f"کوچک‌ترین ضلع تصویر باید دست‌کم {min_side} پیکسل باشد.")

    name = secrets.token_hex(16)
    relpath = f"{kind}/{name[:2]}/{name}.{info.ext}"
    target = _resolve(relpath)
    if target is None:  # cannot happen with a validated kind; keep the guard anyway
        raise UploadError("ذخیرهٔ تصویر ممکن نیست.")
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".part")
    try:
        with open(tmp, "wb") as handle:
            handle.write(data)
        os.chmod(tmp, 0o640)
        os.replace(tmp, target)
    except OSError:
        logger.exception("could not store upload %s", relpath)
        try:
            tmp.unlink()
        except OSError:
            pass
        raise UploadError("ذخیرهٔ تصویر ممکن نشد؛ دوباره تلاش کنید.") from None
    return StoredImage(relpath, len(data), info.mime, info.width, info.height)


def delete_image(relpath: Optional[str]) -> bool:
    """Remove a stored file; ignores empty / foreign paths.  Returns whether a file was deleted."""
    if not relpath:
        return False
    target = _resolve(relpath)
    if target is None:
        return False
    try:
        target.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        logger.exception("could not delete %s", relpath)
        return False


# --------------------------------------------------------------------------
# visibility
# --------------------------------------------------------------------------

Checker = Callable[[str, Any], bool]


@dataclass(frozen=True)
class MediaKind:
    name: str
    check: Optional[Checker] = None  # (relpath, user) -> bool; None = any logged-in user
    public: bool = False  # everyone may see every file of the kind (long public caching)


_KINDS: dict[str, MediaKind] = {}


def register_kind(name: str, check: Optional[Checker] = None, *, public: bool = False) -> MediaKind:
    """Declare who may fetch files of upload *kind* through ``/media``.

    ``check(relpath, user)`` receives the stored relative path and the current
    user (possibly anonymous) and returns True to allow.  Look the file up by
    its path in your table::

        def can_see_receipt(relpath, user):
            row = db.session.scalar(select(Receipt).where(Receipt.image_path == relpath))
            return row is not None and row.atelier_id in my_atelier_ids(user)

        uploads.register_kind("receipt", can_see_receipt)
    """
    if not _KIND_RE.match(name):
        raise ValueError(f"invalid upload kind {name!r}")
    kind = MediaKind(name, check, public)
    _KINDS[name] = kind
    return kind


def _authenticated(user: Any) -> bool:
    return bool(getattr(user, "is_authenticated", False))


def _is_admin(user: Any) -> bool:
    return _authenticated(user) and getattr(getattr(user, "role", None), "value", None) == "admin"


def _check_avatar(relpath: str, user: Any) -> bool:
    import sqlalchemy as sa

    from app.models import User

    owner = db.session.execute(sa.select(User).where(User.avatar_path == relpath)).scalars().first()
    if owner is None:
        return False
    return _authenticated(user) or bool(owner.is_public_profile)


def _check_logo(relpath: str, user: Any) -> bool:
    import sqlalchemy as sa

    from app.models import Atelier

    return db.session.execute(sa.select(Atelier.id).where(Atelier.logo_path == relpath)).first() is not None


def _check_portfolio(relpath: str, user: Any) -> bool:
    import sqlalchemy as sa

    from app.models import PortfolioItem

    item = db.session.execute(sa.select(PortfolioItem).where(PortfolioItem.image_path == relpath)).scalars().first()
    if item is None:
        return False
    if _is_admin(user) or (_authenticated(user) and user.id == item.user_id):
        return True
    if not item.is_public:
        return False
    return _authenticated(user) or bool(item.user.is_public_profile)


register_kind("avatar", _check_avatar)
register_kind("logo", _check_logo, public=True)
register_kind("portfolio", _check_portfolio)


def can_view(relpath: str, user: Any = None) -> bool:
    """May *user* (default: the current one) fetch the stored file *relpath*?"""
    match = _PATH_RE.match(relpath or "")
    if not match:
        return False
    if user is None:
        user = current_user
    kind = _KINDS.get(match.group("kind"))
    if kind is None:
        return _authenticated(user)
    if kind.check is None:
        return _authenticated(user)
    try:
        return bool(kind.check(relpath, user))
    except Exception:
        logger.exception("media checker for %s failed", kind.name)
        return False


def serve(relpath: str) -> Response:
    """Response for ``GET /media/<relpath>``: the file if it exists and may be seen, else 404."""
    match = _PATH_RE.match(relpath or "")
    target = _resolve(relpath)
    if match is None or target is None or not target.is_file() or not can_view(relpath):
        abort(404)
    kind = _KINDS.get(match.group("kind"))
    public = bool(kind and kind.public)
    response = send_file(
        target,
        mimetype=_MIME[match.group("ext")],
        conditional=True,
        etag=True,
        max_age=86400 if public else 3600,
        as_attachment=False,
    )
    response.headers["Cache-Control"] = ("public" if public else "private") + f", max-age={86400 if public else 3600}"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Content-Security-Policy"] = "default-src 'none'; sandbox"
    response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
    if not public:
        response.vary.add("Cookie")
    return response
