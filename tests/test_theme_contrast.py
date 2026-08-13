"""Colour contrast in the shipped themes.

These are the pairs a reader actually has to read. They are checked here, in
Python, against the tokens declared in the stylesheet, because the failure
they guard against is silent: nothing errors, no test goes red, and the page
looks fine to anyone who already knows what it says.

This caught a call-to-action rendering at a ratio of 1.16 — pale blue on
amber — which shipped because `.le-nav__links a` outranks `.le-btn` by one
step of specificity and quietly won.

What this file cannot see is the cascade: it checks declared values, not
which rule wins. `test_nav_links_do_not_capture_the_button` covers the one
place that has actually bitten, and a browser pass is what catches the rest.
"""

import re
from pathlib import Path

import pytest

THEME_CSS = (
    Path(__file__).resolve().parent.parent / "static" / "themes" / "river.css"
)

# WCAG 2.1 AA: 4.5 for normal text, 3.0 for large (>=24px, or >=18.66px bold).
AA_NORMAL = 4.5
AA_LARGE = 3.0


def relative_luminance(hex_colour):
    value = hex_colour.lstrip("#")
    channels = [int(value[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast(fg, bg):
    lighter, darker = sorted((relative_luminance(fg), relative_luminance(bg)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


@pytest.fixture(scope="module")
def tokens():
    """The theme's declared colours, read from the stylesheet itself.

    Parsed rather than duplicated here so that changing a token in the CSS
    is what this test measures, instead of a copy that can drift away from it.
    """
    text = THEME_CSS.read_text(encoding="utf-8")
    found = dict(re.findall(r"(--[a-z0-9-]+):\s*(#[0-9a-fA-F]{6})\s*;", text))
    assert found, "no colour tokens found — has the stylesheet moved?"
    return found


# (foreground, background, what it is, minimum). Tokens are named; literals
# are values the stylesheet uses directly at that spot.
PAIRS = [
    ("--deep-2", "--peach", "call to action, and the day/featured badges", AA_NORMAL),
    ("#ffffff", "--coral-deep", "'On now' badge", AA_NORMAL),
    ("--moss", "#ffffff", "'Free' price on a card", AA_NORMAL),
    ("--ink-2", "#ffffff", "summary and venue text on a card", AA_NORMAL),
    ("--ink-2", "--sand", "counts and ledes on the page ground", AA_NORMAL),
    ("--ink", "#ffffff", "body text on a card", AA_NORMAL),
    ("#a9c9d8", "--deep", "navigation links", AA_NORMAL),
    # --river-deep is the lightest stop of the hero and featured gradients, so
    # it is the worst case anywhere on them.
    ("#ffffff", "--river-deep", "white text at the gradients' light end", AA_NORMAL),
    ("#c6deea", "--river-deep", "featured summary at the gradients' light end", AA_NORMAL),
    ("#ffe0b5", "--river-deep", "featured date line at the gradients' light end", AA_NORMAL),
]


@pytest.mark.parametrize("fg,bg,label,minimum", PAIRS)
def test_readable_pair(tokens, fg, bg, label, minimum):
    resolve = lambda c: tokens[c] if c.startswith("--") else c  # noqa: E731
    ratio = contrast(resolve(fg), resolve(bg))
    assert ratio >= minimum, (
        f"{label}: {resolve(fg)} on {resolve(bg)} is {ratio:.2f}:1, "
        f"needs {minimum}:1"
    )


def test_nav_links_do_not_capture_the_button():
    """`.le-nav__links a` outranks `.le-btn`, so it has to exclude it.

    Without the :not(), the pale link colour wins over the button's own dark
    text and the call to action drops to 1.16:1 against its amber ground.
    """
    # Comments first: this file explains the rule in prose, and matching the
    # explanation rather than the selector is how this test first failed.
    css = re.sub(r"/\*.*?\*/", "", THEME_CSS.read_text(encoding="utf-8"), flags=re.S)

    # Only rules that actually set a colour can capture the button. A rule
    # giving nav links `white-space` shares the selector and is harmless,
    # and flagging it was this test's second false alarm.
    offenders = [
        selector.strip()
        for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css)
        if ".le-nav__links a" in selector
        and re.search(r"(^|;|\s)color\s*:", body)
        and ":not(.le-btn)" not in selector
    ]
    assert not offenders, (
        f"{offenders} set a colour on nav links without excluding .le-btn, "
        "which will override the button's own text colour"
    )

    # And the rule really is there, so a rename cannot make this vacuous.
    assert ".le-nav__links a:not(.le-btn)" in css


def srgb_mix(a, b, weight):
    """`color-mix(in srgb, a <weight>%, b)` — a plain per-channel blend."""
    pa = [int(a.lstrip("#")[i : i + 2], 16) for i in (0, 2, 4)]
    pb = [int(b.lstrip("#")[i : i + 2], 16) for i in (0, 2, 4)]
    return "#" + "".join(
        f"{round(weight * x + (1 - weight) * y):02x}" for x, y in zip(pa, pb)
    )


# The proportion the stylesheet mixes a category hue toward ink before using
# it as text. Kept beside the CSS value it mirrors.
CATEGORY_TEXT_MIX = 0.55
CATEGORY_TINT_MIX = 0.13


def category_tokens(tokens):
    return {k: v for k, v in tokens.items() if k.startswith("--cat-")}


def test_there_are_categories_to_check(tokens):
    assert len(category_tokens(tokens)) >= 10


def test_every_category_reads_on_a_card(tokens):
    """The date line takes the category hue, on a white card.

    Checked for *every* category rather than the ones a fixture happens to
    contain: this shipped broken because the local database had Music and
    Markets, which are dark enough to pass, while the live site had Family,
    which measured 2.69.
    """
    ink = tokens["--ink"]
    for name, hue in sorted(category_tokens(tokens).items()):
        text = srgb_mix(hue, ink, CATEGORY_TEXT_MIX)
        ratio = contrast(text, "#ffffff")
        assert ratio >= AA_NORMAL, f"{name}: {text} on white is {ratio:.2f}:1"


def test_every_category_reads_on_its_own_pill(tokens):
    """The pill tints its background with the same hue it writes in."""
    ink = tokens["--ink"]
    for name, hue in sorted(category_tokens(tokens).items()):
        text = srgb_mix(hue, ink, CATEGORY_TEXT_MIX)
        tint = srgb_mix(hue, "#ffffff", CATEGORY_TINT_MIX)
        ratio = contrast(text, tint)
        assert ratio >= AA_NORMAL, f"{name}: {text} on {tint} is {ratio:.2f}:1"


def test_every_declared_token_is_a_full_hex(tokens):
    """Shorthand hex would silently break the parsing above."""
    for name, value in tokens.items():
        assert re.fullmatch(r"#[0-9a-fA-F]{6}", value), f"{name} is {value}"
