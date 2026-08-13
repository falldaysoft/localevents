"""`{% theme_include %}` — an include that a theme may override.

`{% include %}` resolves a literal name, so a theme that ships its own
`partials/_nav.html` would never be reached by the shared base template. This
tag routes the name through `core.themes.template_name` first, which prefers
`themes/<slug>/<path>` and falls back to the unthemed template — so a theme
overrides the partials it cares about and inherits the rest.
"""

from django import template
from django.template.loader import render_to_string
from django.utils.safestring import mark_safe

from core.themes import get_theme, template_name

register = template.Library()


@register.simple_tag(takes_context=True)
def theme_include(context, path):
    theme = context.get("THEME") or get_theme(None)
    rendered = render_to_string(
        template_name(theme, path),
        context.flatten(),
        request=context.get("request"),
    )
    # The included template has already escaped its own content; without this
    # the whole fragment would arrive on the page as visible markup.
    return mark_safe(rendered)  # noqa: S308 — rendered template output
