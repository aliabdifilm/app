"""app.services.uploads: validation, storage layout and the /media route."""

from __future__ import annotations

import io
import os
import re
import stat
import struct
import zlib

import pytest
from werkzeug.datastructures import FileStorage

from app.extensions import db
from app.models import PortfolioItem
from app.services import uploads
from app.services.uploads import UploadError, sniff_image


# --------------------------------------------------------------------------
# tiny valid images
# --------------------------------------------------------------------------


def png(width=2, height=3, claim=None):
    """A real PNG of width x height; ``claim=(w, h)`` writes other (huge) dimensions into the header only."""

    def chunk(kind, data):
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + b"\x00\x00\x00" * width for _ in range(height))
    if claim:
        width, height = claim
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def jpeg(width=4, height=5):
    sof = b"\x08" + struct.pack(">HH", height, width) + b"\x01\x01\x11\x00"
    return (
        b"\xff\xd8"
        + b"\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
        + b"\xff\xdb\x00\x43\x00" + bytes(64)
        + b"\xff\xc0" + struct.pack(">H", len(sof) + 2) + sof
        + b"\xff\xda\x00\x08\x01\x01\x00\x00\x3f\x00" + b"\x00" * 8
        + b"\xff\xd9"
    )


def webp_lossless(width=6, height=7):
    bits = (width - 1) | ((height - 1) << 14)
    payload = b"\x2f" + struct.pack("<I", bits) + b"\x00" * 8
    return b"RIFF" + struct.pack("<I", 4 + 8 + len(payload)) + b"WEBP" + b"VP8L" + struct.pack("<I", len(payload)) + payload


def webp_lossy(width=8, height=9):
    payload = b"\x00\x00\x00" + b"\x9d\x01\x2a" + struct.pack("<HH", width, height) + b"\x00" * 8
    return b"RIFF" + struct.pack("<I", 4 + 8 + len(payload)) + b"WEBP" + b"VP8 " + struct.pack("<I", len(payload)) + payload


def webp_extended(width=10, height=11):
    payload = b"\x00\x00\x00\x00" + (width - 1).to_bytes(3, "little") + (height - 1).to_bytes(3, "little")
    return b"RIFF" + struct.pack("<I", 4 + 8 + len(payload)) + b"WEBP" + b"VP8X" + struct.pack("<I", len(payload)) + payload


def upload(data, filename="photo.jpg"):
    return FileStorage(stream=io.BytesIO(data), filename=filename)


# --------------------------------------------------------------------------
# sniffing
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "data,expected",
    [
        (png(2, 3), ("png", 2, 3)),
        (jpeg(4, 5), ("jpg", 4, 5)),
        (webp_lossless(6, 7), ("webp", 6, 7)),
        (webp_lossy(8, 9), ("webp", 8, 9)),
        (webp_extended(10, 11), ("webp", 10, 11)),
    ],
)
def test_sniff_recognises_images_and_reads_dimensions(data, expected):
    info = sniff_image(data)
    assert (info.ext, info.width, info.height) == expected
    assert info.mime == {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp"}[info.ext]


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"GIF89a\x01\x00\x01\x00\x00\x00\x00;",
        b"<svg xmlns='http://www.w3.org/2000/svg'/>",
        b"<?xml version='1.0'?><svg/>",
        b"<html><script>alert(1)</script></html>",
        b"\xff\xd8\xff",  # JPEG magic only
        b"\xff\xd8\xff\xe0\x00\x10JFIF\x00",  # truncated before any frame header
        b"\x89PNG\r\n\x1a\n",  # PNG magic only
        b"\x89PNG\r\n\x1a\n" + b"\x00" * 8 + b"IHDR" + b"\x00" * 30,  # wrong chunk length
        b"RIFF\x00\x00\x00\x00WEBP",
        b"MZ\x90\x00" + b"\x00" * 100,
        b"%PDF-1.7\n",
        b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic",
        png(1, 1, claim=(0, 5)),
    ],
)
def test_sniff_rejects_everything_else(data):
    assert sniff_image(data) is None


def test_polyglot_html_in_a_jpeg_is_still_only_a_jpeg():
    data = jpeg() + b"<script>alert(1)</script>"
    assert sniff_image(data).ext == "jpg"  # fine to store: served as image/jpeg + nosniff + CSP sandbox


# --------------------------------------------------------------------------
# save_image
# --------------------------------------------------------------------------


def test_save_png_layout_and_permissions(app):
    stored = uploads.save_image(upload(png(), "my photo.PNG"), "avatar")
    assert re.fullmatch(r"avatar/[0-9a-f]{2}/[0-9a-f]{32}\.png", stored.path)
    assert stored.path.split("/")[1] == stored.path.split("/")[2][:2]
    assert (stored.mime, stored.width, stored.height, stored.size) == ("image/png", 2, 3, len(png()))
    target = os.path.join(app.config["UPLOAD_DIR"], stored.path)
    assert open(target, "rb").read() == png()
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o640
    assert not [name for name in os.listdir(os.path.dirname(target)) if name.endswith(".part")]


def test_original_filename_is_discarded(app):
    stored = uploads.save_image(upload(jpeg(), "../../etc/passwd<script>.jpg"), "portfolio")
    assert "passwd" not in stored.path and ".." not in stored.path


def test_names_are_random(app):
    paths = {uploads.save_image(upload(jpeg()), "avatar").path for _ in range(20)}
    assert len(paths) == 20


def test_extension_comes_from_the_content_not_the_name(app):
    assert uploads.save_image(upload(jpeg(), "x.jpeg"), "avatar").path.endswith(".jpg")
    assert uploads.save_image(upload(jpeg(), "x.JFIF"), "avatar").path.endswith(".jpg")
    assert uploads.save_image(upload(webp_lossy(), "x.webp"), "avatar").path.endswith(".webp")


def test_ios_heic_name_with_transcoded_jpeg_bytes_is_accepted(app):
    assert uploads.save_image(upload(jpeg(), "IMG_0001.HEIC"), "avatar").path.endswith(".jpg")


@pytest.mark.parametrize(
    "data,filename,message",
    [
        (None, None, "انتخاب نشده"),
        (b"", "a.jpg", "خالی"),
        (png(), "a.svg", "JPG، PNG یا WebP"),
        (png(), "a.gif", "JPG، PNG یا WebP"),
        (png(), "a.php", "JPG، PNG یا WebP"),
        (png(), "a", "JPG، PNG یا WebP"),
        (b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>", "a.jpg", "تصویر معتبر"),
        (b"<?php system($_GET['c']); ?>", "shell.jpg", "تصویر معتبر"),
        (b"GIF89a" + b"\x00" * 20, "a.png", "تصویر معتبر"),
        (b"\xff\xd8\xff" + b"\x00" * 20, "a.jpg", "تصویر معتبر"),
        (png(), "a.jpg", "همخوانی"),
        (jpeg(), "a.png", "همخوانی"),
        (webp_lossy(), "a.jpg", "همخوانی"),
        (b"\x00\x00\x00\x18ftypheic" + b"\x00" * 20, "a.heic", "HEIC"),
        (png(claim=(70000, 70000)), "a.png", "بزرگ"),
    ],
)
def test_rejections(app, data, filename, message):
    file = None if data is None else upload(data, filename)
    with pytest.raises(UploadError, match=message):
        uploads.save_image(file, "avatar")
    avatars = os.path.join(app.config["UPLOAD_DIR"], "avatar")
    leftovers = [f for _, _, files in os.walk(avatars) for f in files] if os.path.isdir(avatars) else []
    assert leftovers == []  # a rejected upload leaves nothing on disk


def test_size_cap_from_config_and_per_call(app):
    app.config["MAX_UPLOAD_BYTES"] = len(png()) - 1
    with pytest.raises(UploadError, match="مگابایت"):
        uploads.save_image(upload(png(), "a.png"), "avatar")
    app.config["MAX_UPLOAD_BYTES"] = 10 * 1024 * 1024
    big = jpeg() + b"\x00" * (1024 * 1024)
    uploads.save_image(upload(big), "avatar")
    with pytest.raises(UploadError):
        uploads.save_image(upload(big), "avatar", max_bytes=1024)


def test_size_cap_is_enforced_while_reading(app):
    class Endless(io.RawIOBase):
        reads = 0

        def readable(self):
            return True

        def read(self, size=-1):
            Endless.reads += size if size > 0 else 10**9
            return b"\xff" * (size if size > 0 else 0)

    app.config["MAX_UPLOAD_BYTES"] = 1000
    with pytest.raises(UploadError):
        uploads.save_image(FileStorage(stream=Endless(), filename="a.jpg"), "avatar")
    assert Endless.reads <= 1001  # never slurps an unbounded stream


def test_pixel_cap_and_min_side(app):
    app.config["MAX_IMAGE_PIXELS"] = 100
    with pytest.raises(UploadError, match="ابعاد"):
        uploads.save_image(upload(png(11, 10), "a.png"), "avatar")
    uploads.save_image(upload(png(10, 10), "a.png"), "avatar")
    with pytest.raises(UploadError, match="300"):
        uploads.save_image(upload(png(10, 10), "a.png"), "avatar", min_side=300)


@pytest.mark.parametrize("kind", ["", "../x", "Avatar", "a/b", "1abc", "x" * 40, "a b"])
def test_bad_kinds_are_programming_errors(app, kind):
    with pytest.raises(ValueError):
        uploads.save_image(upload(png(), "a.png"), kind)


def test_files_live_outside_static(app):
    stored = uploads.save_image(upload(png(), "a.png"), "avatar")
    assert not os.path.exists(os.path.join(app.static_folder, stored.path))
    assert os.path.realpath(app.config["UPLOAD_DIR"]).startswith(os.path.realpath(str(app.config["UPLOAD_DIR"])))
    assert not os.path.realpath(app.config["UPLOAD_DIR"]).startswith(os.path.realpath(app.static_folder))


# --------------------------------------------------------------------------
# delete
# --------------------------------------------------------------------------


def test_delete_image(app):
    stored = uploads.save_image(upload(png(), "a.png"), "avatar")
    target = os.path.join(app.config["UPLOAD_DIR"], stored.path)
    assert uploads.delete_image(stored.path) is True
    assert not os.path.exists(target)
    assert uploads.delete_image(stored.path) is False  # already gone
    assert uploads.delete_image(None) is False and uploads.delete_image("") is False


@pytest.mark.parametrize(
    "path",
    ["../../etc/passwd", "/etc/passwd", "avatar/../../x.png", "avatar/zz/abc.png", "avatar/ab/" + "g" * 32 + ".png", "avatar/ab/" + "a" * 32 + ".svg", "avatar\\ab\\x.png"],
)
def test_delete_ignores_foreign_paths(app, path, tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me")
    assert uploads.delete_image(path) is False
    assert victim.exists()


# --------------------------------------------------------------------------
# /media
# --------------------------------------------------------------------------


@pytest.fixture
def avatar(app, make_user):
    owner = make_user("staff")
    stored = uploads.save_image(upload(jpeg()), "avatar")
    owner.avatar_path = stored.path
    db.session.commit()
    return owner, stored


def test_media_url_helper_and_template_global(app, avatar):
    owner, stored = avatar
    with app.test_request_context("/"):
        assert uploads.media_url(stored.path) == f"/media/{stored.path}"
        assert uploads.media_url(None) is None
        from flask import render_template_string

        assert render_template_string("{{ media_url(p) }}", p=stored.path) == f"/media/{stored.path}"
        assert stored.url == f"/media/{stored.path}"


def test_avatar_is_visible_to_signed_in_users_only(app, avatar, make_user, client_for):
    owner, stored = avatar
    anonymous = app.test_client()
    assert anonymous.get(f"/media/{stored.path}").status_code == 404
    other = client_for(make_user("hall_manager"))
    response = other.get(f"/media/{stored.path}")
    assert response.status_code == 200
    assert response.data == jpeg()
    assert response.mimetype == "image/jpeg"


def test_public_profile_avatar_is_visible_to_everyone(app, avatar):
    owner, stored = avatar
    owner.is_public_profile = True
    db.session.commit()
    assert app.test_client().get(f"/media/{stored.path}").status_code == 200


def test_media_response_headers(app, avatar, client_for):
    owner, stored = avatar
    response = client_for(owner).get(f"/media/{stored.path}")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Content-Security-Policy"] == "default-src 'none'; sandbox"
    assert response.headers["Cache-Control"] == "private, max-age=3600"
    assert "Cookie" in response.headers["Vary"]
    assert response.headers["Content-Type"] == "image/jpeg"
    assert "attachment" not in response.headers.get("Content-Disposition", "")
    etag = response.headers["ETag"]
    again = client_for(owner).get(f"/media/{stored.path}", headers={"If-None-Match": etag})
    assert again.status_code == 304


def test_content_type_follows_the_stored_extension_not_the_content(app, make_user, client_for):
    user = make_user("staff")
    stored = uploads.save_image(upload(jpeg() + b"<html><script>alert(1)</script></html>"), "avatar")
    user.avatar_path = stored.path
    db.session.commit()
    response = client_for(user).get(f"/media/{stored.path}")
    assert response.mimetype == "image/jpeg" and response.headers["X-Content-Type-Options"] == "nosniff"


def test_logo_is_public(app, make_atelier):
    atelier = make_atelier()
    stored = uploads.save_image(upload(png(), "logo.png"), "logo")
    atelier.logo_path = stored.path
    db.session.commit()
    response = app.test_client().get(f"/media/{stored.path}")
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "public, max-age=86400"


def test_unreferenced_files_are_not_served(app, make_user, client_for):
    stored = uploads.save_image(upload(png(), "a.png"), "avatar")  # nobody's avatar
    assert client_for(make_user("admin")).get(f"/media/{stored.path}").status_code == 404


def test_portfolio_visibility(app, make_user, client_for):
    owner = make_user("staff")
    public_file = uploads.save_image(upload(png(), "a.png"), "portfolio")
    private_file = uploads.save_image(upload(png(), "b.png"), "portfolio")
    db.session.add_all(
        [
            PortfolioItem(user_id=owner.id, image_path=public_file.path, is_public=True),
            PortfolioItem(user_id=owner.id, image_path=private_file.path, is_public=False),
        ]
    )
    db.session.commit()
    anonymous, stranger = app.test_client(), client_for(make_user("atelier_manager"))
    assert anonymous.get(f"/media/{public_file.path}").status_code == 404  # profile is not public
    assert stranger.get(f"/media/{public_file.path}").status_code == 200
    assert stranger.get(f"/media/{private_file.path}").status_code == 404
    assert client_for(owner).get(f"/media/{private_file.path}").status_code == 200
    assert client_for(make_user("admin")).get(f"/media/{private_file.path}").status_code == 200
    owner.is_public_profile = True
    db.session.commit()
    assert anonymous.get(f"/media/{public_file.path}").status_code == 200
    assert anonymous.get(f"/media/{private_file.path}").status_code == 404


@pytest.mark.parametrize(
    "path",
    [
        "../../../etc/passwd",
        "..%2f..%2fetc/passwd",
        "avatar/../../app/config.py",
        "avatar/ab/../../../x.png",
        "%2e%2e/%2e%2e/etc/passwd",
        "avatar/ab/" + "a" * 32 + ".png/../../..",
        "avatar/ab/nothere.png",
        "avatar/ab/" + "a" * 32 + ".png",
        "avatar/ab/" + "a" * 32 + ".html",
        "unknownkind/ab/" + "a" * 32 + ".png",
        "",
    ],
)
def test_path_traversal_and_junk_are_404(app, make_user, client_for, path):
    client = client_for(make_user("admin"))
    assert client.get(f"/media/{path}").status_code == 404


def test_symlink_escape_is_blocked(app, tmp_path, make_user, client_for):
    outside = tmp_path / "secret.png"
    outside.write_bytes(png())
    shard = os.path.join(app.config["UPLOAD_DIR"], "avatar", "ab")
    os.makedirs(shard, exist_ok=True)
    link = os.path.join(shard, "a" * 32 + ".png")
    os.symlink(outside, link)
    user = make_user("staff", avatar_path="avatar/ab/" + "a" * 32 + ".png")
    assert client_for(user).get(f"/media/avatar/ab/{'a' * 32}.png").status_code == 404


def test_custom_kind_with_a_checker(app, make_user, client_for):
    allowed = make_user("staff")
    stored = uploads.save_image(upload(png(), "r.png"), "receipt")
    seen = []

    def check(relpath, user):
        seen.append((relpath, user.is_authenticated))
        return user.is_authenticated and user.id == allowed.id

    uploads.register_kind("receipt", check)
    try:
        assert client_for(allowed).get(f"/media/{stored.path}").status_code == 200
        assert client_for(make_user("staff")).get(f"/media/{stored.path}").status_code == 404
        assert app.test_client().get(f"/media/{stored.path}").status_code == 404
        assert seen and seen[0][0] == stored.path
    finally:
        uploads._KINDS.pop("receipt", None)


def test_unregistered_kinds_default_to_signed_in_users(app, make_user, client_for):
    stored = uploads.save_image(upload(png(), "r.png"), "misc")
    assert client_for(make_user("staff")).get(f"/media/{stored.path}").status_code == 200
    assert app.test_client().get(f"/media/{stored.path}").status_code == 404


def test_a_crashing_checker_means_no(app, make_user, client_for):
    stored = uploads.save_image(upload(png(), "r.png"), "buggy")
    uploads.register_kind("buggy", lambda relpath, user: 1 / 0)
    try:
        assert client_for(make_user("admin")).get(f"/media/{stored.path}").status_code == 404
    finally:
        uploads._KINDS.pop("buggy", None)


def test_register_kind_validates_the_name():
    with pytest.raises(ValueError):
        uploads.register_kind("../bad")
