"""Steering submissions toward primary sources.

A listing is more useful, and ages better, when it points at whoever is
actually putting the event on — the hall, the library, the band, the church —
rather than at a ticketing platform's page about them. Aggregator links rot
when a sale ends, bury the organiser, and tell a reader less.

This is a nudge, not a rule. Some organisers genuinely have no other presence,
and refusing those would lose real local events. So a submitter is told what we
would prefer and then left to decide.

There is a second reason to prefer primary sources: reading a page on a
person's behalf is a different act from harvesting a commercial platform's
catalogue. Staying pointed at the organiser keeps this a courtesy, and keeps us
out of territory a community project has no business in.
"""

from urllib.parse import urlparse

# Ticketing platforms, social networks, and event aggregators. Matched on the
# registrable domain, so subdomains are covered.
AGGREGATOR_DOMAINS = {
    "eventbrite.com", "eventbrite.ca", "eventbrite.co.uk",
    "ticketmaster.com", "ticketmaster.ca",
    "meetup.com",
    "facebook.com", "fb.com", "fb.me",
    "instagram.com",
    "x.com", "twitter.com",
    "linkedin.com",
    "songkick.com", "bandsintown.com",
    "ticketweb.ca", "ticketweb.com",
    "showpass.com",
    "universe.com",
    "allevents.in",
    "patch.com",
}

PRIMARY_SOURCE_NOTE = (
    "That looks like a ticketing or social media page. If the organiser has "
    "their own website, a link to that usually makes a better listing — it "
    "stays useful after tickets sell out. If this is genuinely the only place "
    "the event is published, carry on; that's fine."
)


def registrable_domain(url):
    """The last two labels of the hostname, lowercased.

    Deliberately crude. It over-matches multi-part suffixes like .co.uk, which
    for this purpose is harmless — the worst outcome is showing a suggestion to
    someone who did not need it.
    """
    hostname = (urlparse(url).hostname or "").lower()
    if hostname.startswith("www."):
        hostname = hostname[4:]
    parts = hostname.split(".")
    if len(parts) >= 3 and parts[-2] in {"co", "com", "org", "net", "gov"}:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:]) if len(parts) >= 2 else hostname


def is_aggregator(url):
    if not url:
        return False
    domain = registrable_domain(url)
    return domain in AGGREGATOR_DOMAINS or any(
        domain.endswith("." + known) for known in AGGREGATOR_DOMAINS
    )


def advice_for(url):
    """A note to show the submitter, or empty if the link looks fine."""
    return PRIMARY_SOURCE_NOTE if is_aggregator(url) else ""
