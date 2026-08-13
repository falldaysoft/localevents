"""The look an instance chooses, and the registry of looks on offer.

A theme is a stylesheet plus, optionally, a set of template overrides. It is
deliberately *not* an instance's identity: a theme carries no town name, no
coordinates and no prose, so two communities can share one and still look like
themselves. Identity stays in settings and `SiteConfig`; a theme decides only
how that identity is dressed.

Themes are template *overlays*, not replacements. `template_name()` prefers
`themes/<slug>/<path>` and falls back to `<path>`, so a theme overrides only
the pages it cares about and inherits the rest. That is what keeps adding a
theme from touching any existing code: a directory and a stylesheet, and the
default theme carries on rendering exactly as it did.

The default is `classic` on purpose. Upgrading the product must never restyle
a running site — an instance opts into a new look by changing one admin field,
which is a decision its administrators make rather than one a deploy makes for
them.
"""

from dataclasses import dataclass

from django.template.loader import select_template


@dataclass(frozen=True)
class Theme:
    slug: str
    name: str
    description: str
    # Static path, relative to STATIC_URL. None means "the built-in look",
    # which is styled by the utility classes already in the templates.
    stylesheet: str | None = None
    # A full <link> href for a webfont, or None to use the built-in family.
    # Kept as a URL rather than a family name because the theme's stylesheet
    # is what names the family, and the two have to agree.
    font_css: str | None = None

    @property
    def has_templates(self) -> bool:
        """Whether this theme ships template overrides."""
        return self.slug != DEFAULT_THEME


DEFAULT_THEME = "classic"

THEMES: dict[str, Theme] = {
    "classic": Theme(
        slug="classic",
        name="Classic",
        description=(
            "The built-in look: white cards on a pale grey ground, one "
            "typeface, no ornament. Plain and legible."
        ),
    ),
    "river": Theme(
        slug="river",
        name="River",
        description=(
            "Warm sand ground, rounded cards on a category-coloured spine, "
            "and listings grouped under the day they fall on."
        ),
        stylesheet="themes/river.css",
        font_css=(
            "https://fonts.googleapis.com/css2"
            "?family=Figtree:wght@400;500;600;700;800"
            "&family=Manrope:wght@400;500;600&display=swap"
        ),
    ),
}


def get_theme(slug: str | None) -> Theme:
    """The named theme, or the default when it is missing or unknown.

    Falls back rather than raising: a theme removed from the registry while an
    instance still names it in the database would otherwise 500 every page,
    and a site that renders plainly is a far better failure than one that does
    not render at all.
    """
    return THEMES.get(slug or "", THEMES[DEFAULT_THEME])


def theme_for(request) -> Theme:
    """The theme this request should render in.

    Views need this before `render()`, which is earlier than the context
    processor runs. Imported locally because `core.models` imports this module
    for its field choices, and a module-level import would close the loop.
    """
    from .models import site_config_for

    try:
        return get_theme(site_config_for(request).theme)
    except Exception:
        return get_theme(None)


def theme_choices():
    """`(slug, label)` pairs for a model field or a form."""
    return [(theme.slug, theme.name) for theme in THEMES.values()]


def template_name(theme: Theme, path: str) -> str:
    """`path` as rendered by `theme`, falling back to the unthemed template.

    Returns a name rather than a Template so callers can keep using `render()`
    and its partial syntax (`web/index.html#results`) unchanged.
    """
    if not theme.has_templates:
        return path
    # A partial reference has to keep its fragment: the loader resolves
    # "a.html#results" by finding a.html and then the partial inside it, so
    # splitting on "#" and reassembling is what lets a theme override a page
    # that is rendered as a fragment by HTMX.
    base, _, fragment = path.partition("#")
    candidates = [f"themes/{theme.slug}/{base}", base]
    chosen = select_template(candidates).template.name
    return f"{chosen}#{fragment}" if fragment else chosen
