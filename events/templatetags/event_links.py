"""Saying where an outbound link goes.

The source page is usually the organiser's own listing — the one place with
details we do not carry — so the button names its destination rather than
reading as a generic "more". A reader deciding whether to leave the site
trusts a domain they recognise more than a label.
"""

from urllib.parse import urlsplit

from django import template

register = template.Library()


@register.filter
def hostname(url):
    host = urlsplit(url or "").hostname or ""
    return host.removeprefix("www.")
