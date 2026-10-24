"""WCAG 2.1 contrast checks for the design tokens in app/static/css/app.css.

The design system promises AA contrast in light *and* dark mode.  This test
parses the real stylesheet (so a token tweak cannot silently break it) and
checks every foreground/background pair the components actually use:

* normal text and text-like icons: >= 4.5:1
* non-text UI (control borders, focus ring, accent bars): >= 3:1

It also checks that the two dark-mode blocks (the ``prefers-color-scheme``
media query and the explicit ``[data-theme="dark"]`` override) stay identical,
because the file has to repeat the tokens (no preprocessor).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

CSS = Path(__file__).resolve().parent.parent / "app" / "static" / "css" / "app.css"

TEXT = 4.5
UI = 3.0

HEX_RE = re.compile(r"^#([0-9a-fA-F]{6})$")


def _block(css: str, opener: str) -> str:
    """Return the body of the first ``opener { ... }`` rule (no nested braces)."""
    start = css.index(opener)
    body_start = css.index("{", start) + 1
    return css[body_start : css.index("}", body_start)]


def _tokens(body: str) -> dict[str, str]:
    return {m.group(1): m.group(2).strip() for m in re.finditer(r"(--[a-z0-9-]+)\s*:\s*([^;]+);", body)}


@pytest.fixture(scope="module")
def css() -> str:
    return CSS.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def light(css) -> dict[str, str]:
    return _tokens(_block(css, ":root {"))


@pytest.fixture(scope="module")
def dark(css, light) -> dict[str, str]:
    merged = dict(light)
    merged.update(_tokens(_block(css, ':root[data-theme="dark"] {')))
    return merged


def _rgb(value: str) -> tuple[int, int, int]:
    m = HEX_RE.match(value)
    assert m, f"not a #rrggbb colour: {value!r}"
    h = m.group(1)
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _luminance(value: str) -> float:
    def channel(c: int) -> float:
        s = c / 255
        return s / 12.92 if s <= 0.03928 else ((s + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(c) for c in _rgb(value))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(fg: str, bg: str) -> float:
    a, b = _luminance(fg), _luminance(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


# (foreground token or literal, background token or literal, minimum, why)
PAIRS = [
    # body text on every surface
    ("--text", "--bg", TEXT, "body text"),
    ("--text", "--surface", TEXT, "card text"),
    ("--text", "--surface-2", TEXT, "table header / footer text"),
    ("--text", "--surface-3", TEXT, "text on progress track"),
    ("--text-muted", "--bg", TEXT, "secondary text on page"),
    ("--text-muted", "--surface", TEXT, "secondary text on cards"),
    ("--text-muted", "--surface-2", TEXT, "secondary text on tinted rows"),
    ("--text-faint", "--surface", TEXT, "placeholders on inputs"),
    ("--text-faint", "--bg", TEXT, "faint text on page"),
    ("--bg", "--text", TEXT, "toast (inverted)"),
    # links and accent
    ("--accent-text", "--bg", TEXT, "links on page"),
    ("--accent-text", "--surface", TEXT, "links on cards"),
    ("--accent-text", "--surface-2", TEXT, "links on tinted rows"),
    ("--on-accent", "--accent", TEXT, "primary button"),
    ("--on-accent", "--accent-hover", TEXT, "primary button hover"),
    ("--on-accent", "--accent-active", TEXT, "primary button pressed"),
    ("--accent-ink", "--accent-soft", TEXT, "accent badge / selected chip"),
    ("--accent-text", "--accent-soft", TEXT, "secondary button hover"),
    # semantic surfaces
    ("--success-ink", "--success-soft", TEXT, "success badge / alert"),
    ("--warning-ink", "--warning-soft", TEXT, "warning badge / alert"),
    ("--danger-ink", "--danger-soft", TEXT, "danger badge / alert"),
    ("--info-ink", "--info-soft", TEXT, "info badge / alert"),
    ("--success", "--surface", TEXT, "success text"),
    ("--danger", "--surface", TEXT, "error text, weekend days"),
    ("--danger", "--bg", TEXT, "error text on page"),
    ("--danger", "--danger-soft", TEXT, "danger outline button hover"),
    # sidebar and landing call-to-action band
    ("--side-text", "--side-bg", TEXT, "sidebar items"),
    ("--side-muted", "--side-bg", TEXT, "sidebar secondary text"),
    ("--side-active-text", "--side-active-bg", TEXT, "active nav item"),
    # non-text UI: borders of controls, focus ring, accent marks
    ("--border-strong", "--surface", UI, "input / checkbox / stepper border"),
    ("--border-strong", "--bg", UI, "input border on page"),
    ("--focus", "--bg", UI, "focus ring on page"),
    ("--focus", "--surface", UI, "focus ring on cards"),
    ("--accent", "--bg", UI, "accent bars and icons"),
    ("--accent", "--surface", UI, "accent bars on cards"),
    ("--warning", "--surface", UI, "rating stars"),
    ("--success", "--surface-2", UI, "tick icons"),
]

# Literal colours used for filled danger buttons (see .btn--danger in app.css).
LITERAL_PAIRS_LIGHT = [("#ffffff", "--danger", TEXT, "danger button text"), ("#ffffff", "--danger-hover", TEXT, "danger button hover")]
LITERAL_PAIRS_DARK = [("#1c0f07", "--danger", TEXT, "danger button text"), ("#1c0f07", "--danger-hover", TEXT, "danger button hover")]


def _resolve(tokens: dict[str, str], ref: str) -> str:
    return tokens[ref] if ref.startswith("--") else ref


def _check(tokens: dict[str, str], pairs) -> list[str]:
    failures = []
    for fg, bg, minimum, why in pairs:
        ratio = contrast(_resolve(tokens, fg), _resolve(tokens, bg))
        if ratio < minimum:
            failures.append(
                f"{why}: {fg} {_resolve(tokens, fg)} on {bg} {_resolve(tokens, bg)} = {ratio:.2f}:1 (needs {minimum}:1)"
            )
    return failures


def test_light_theme_meets_wcag_aa(light):
    failures = _check(light, PAIRS + LITERAL_PAIRS_LIGHT)
    assert not failures, "\n".join(failures)


def test_dark_theme_meets_wcag_aa(dark):
    failures = _check(dark, PAIRS + LITERAL_PAIRS_DARK)
    assert not failures, "\n".join(failures)


def test_dark_blocks_are_identical(css):
    """The media-query copy and the [data-theme="dark"] copy of the dark tokens must not drift."""
    media = _tokens(_block(css, ":root:not([data-theme=\"light\"]) {"))
    explicit = _tokens(_block(css, ':root[data-theme="dark"] {'))
    assert media == explicit, {
        k: (media.get(k), explicit.get(k)) for k in set(media) | set(explicit) if media.get(k) != explicit.get(k)
    }


def test_every_color_token_is_defined_in_both_themes(light, dark):
    for name, value in light.items():
        if HEX_RE.match(value):
            assert name in dark, f"{name} missing in dark theme"
            assert HEX_RE.match(dark[name]) or dark[name].startswith("rgba"), name


def test_no_hard_coded_colours_outside_tokens(css):
    """Components must use tokens: only a few literal colours are allowed (toasts, danger button, brand)."""
    after_tokens = css[css.index("\n   3. Reset + base\n") :]
    literals = set(re.findall(r"#[0-9a-fA-F]{3,8}\b", after_tokens))
    allowed = {"#ffffff", "#1c0f07", "#5fcf93", "#f0b84a", "#ff8b7e", "#6cc2ec"}
    assert literals <= allowed, f"hard-coded colours outside the token blocks: {sorted(literals - allowed)}"


def test_tap_target_token_is_44px(css):
    assert re.search(r"--tap:\s*44px", css)
