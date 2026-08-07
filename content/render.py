"""Markdown in, safe HTML out.

Pages are written in Markdown rather than in a rich-text editor for a reason
that is mostly about this site's CSP: every editor worth having ships a large
JavaScript bundle, and the ones that are not compiled against a strict policy
want `unsafe-eval` — the exact concession this project already declined for
Alpine. A textarea needs nothing.

The output is sanitised even though the author is a moderator. That is not
distrust of moderators; it is that "moderator" is a much wider grant than
"superuser", and `SiteConfig.head_html` — which *is* raw HTML — is restricted
to superusers precisely because it can do anything. Letting a moderator write
raw `<script>` into a page would quietly widen that grant to everyone who can
work the queue. It also means a page rendered from a row somebody edited
directly in the database is still safe to serve.

Sanitising at save rather than at render is the deliberate half of this: the
public page view is the hot path, and it should do no work beyond reading a
column. The consequence is that upgrading this module does not retroactively
change pages already stored — `Page.rerender()` exists for that, and the
migration that adds a rule should call it.
"""

import re

import markdown
import nh3

# Enough Markdown for a page a volunteer writes: headings, lists, links,
# images, tables, code. Not `attr_list` or `md_in_html`, which mostly exist to
# smuggle arbitrary attributes and classes into the output — the sanitiser
# below would strip them anyway, so allowing them would only produce pages that
# silently do not look like the preview.
EXTENSIONS = ["tables", "sane_lists", "nl2br"]

ALLOWED_TAGS = {
    "p", "br", "hr",
    "h2", "h3", "h4", "h5", "h6",
    "strong", "em", "b", "i", "u", "s", "code", "pre", "blockquote",
    "ul", "ol", "li",
    "a", "img",
    "table", "thead", "tbody", "tr", "th", "td",
    "figure", "figcaption",
}

# `h1` is absent on purpose: the page template renders the title as the
# document's one h1, and a second would be a heading-structure bug on every
# page where an author opened their Markdown with `#`, which is most of them.
# It is demoted rather than left to the sanitiser, which strips a disallowed
# tag but keeps its text — so `# Overview` would otherwise come out as a bare
# unstyled line, and the author would reasonably read that as broken.
_H1 = re.compile(r"<(/?)h1(\s[^>]*)?>", re.IGNORECASE)

ALLOWED_ATTRIBUTES = {
    "a": {"href", "title"},
    "img": {"src", "alt", "title", "width", "height", "loading"},
    "th": {"colspan", "rowspan"},
    "td": {"colspan", "rowspan"},
}

# No `data:` — a data URI is how you get an inline SVG (and therefore a script)
# past a tag allowlist that already refuses `<svg>`.
ALLOWED_SCHEMES = {"http", "https", "mailto"}


def render(text):
    """Markdown source to sanitised HTML, ready to store."""
    if not text or not text.strip():
        return ""

    html = markdown.markdown(text, extensions=EXTENSIONS, output_format="html")
    html = _H1.sub(r"<\1h2>", html)

    return nh3.clean(
        html,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        url_schemes=ALLOWED_SCHEMES,
        # An off-site link opened from a page here should not be able to reach
        # back through `window.opener`.
        link_rel="noopener noreferrer",
    )


def summarise(text, limit=160):
    """First prose of a page, flattened — for a meta description.

    Deliberately works from the Markdown source rather than the rendered HTML:
    stripping tags back out of HTML to get plain text is a well-known way to
    reintroduce exactly what the sanitiser removed.
    """
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith(("#", ">", "-", "*", "|", "!", "```")):
            continue
        if len(line) <= limit:
            return line
        return line[: limit - 1].rsplit(" ", 1)[0] + "…"
    return ""
