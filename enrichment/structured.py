"""Reading a page's own event markup.

Libraries, municipalities, ticketing platforms and most CMS event plugins
publish schema.org Event data. When it is there it is exact and already parsed
— dates, venue, price — so asking a language model to re-read the same page in
prose would be slower, costlier, and less accurate.

This runs first. The model handles everything else, which is still most pages.
"""

import json
import logging
from datetime import datetime

from bs4 import BeautifulSoup

from .schemas import EventDraft, ExtractedOccurrence

logger = logging.getLogger("enrichment.structured")


def _iter_json_ld(soup):
    """Yield every JSON-LD object on the page, flattening @graph and lists."""
    for tag in soup.find_all("script", type="application/ld+json"):
        raw = tag.string or tag.get_text()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            # Malformed JSON-LD is common in the wild; skip it rather than
            # abandoning the whole page.
            continue

        candidates = data if isinstance(data, list) else [data]
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            if "@graph" in candidate and isinstance(candidate["@graph"], list):
                for node in candidate["@graph"]:
                    if isinstance(node, dict):
                        yield node
            else:
                yield candidate


def _is_event(node):
    node_type = node.get("@type", "")
    types = node_type if isinstance(node_type, list) else [node_type]
    return any(
        isinstance(t, str) and t.lower().endswith("event") for t in types
    )


def _text(value):
    """schema.org values arrive as strings, dicts, or lists. Flatten to text."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return _text(value.get("name") or value.get("@value") or "")
    if isinstance(value, list) and value:
        return _text(value[0])
    return ""


def _parse_datetime(value):
    raw = _text(value)
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _offers(node):
    """Return (is_free, price_min, price_max, ticket_url)."""
    offers = node.get("offers")
    if not offers:
        return False, None, None, ""

    entries = offers if isinstance(offers, list) else [offers]
    prices = []
    url = ""
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        url = url or _text(entry.get("url"))
        raw_price = entry.get("price", entry.get("lowPrice"))
        if raw_price in (None, ""):
            continue
        try:
            prices.append(float(str(raw_price).replace(",", "")))
        except ValueError:
            continue

    if not prices:
        return False, None, None, url

    low, high = min(prices), max(prices)
    # A stated price of zero is a positive assertion that it's free — quite
    # different from a page that simply never mentions cost.
    if low == 0 and high == 0:
        return True, None, None, url
    return False, low, (high if high != low else None), url


def extract(html, source_url=""):
    """Build an EventDraft from schema.org markup, or return None.

    Returns None rather than a mostly-empty draft when the page has no Event
    node, so the caller can fall through to the model. A draft with no title is
    worse than no draft.
    """
    soup = BeautifulSoup(html, "html.parser")

    node = next((n for n in _iter_json_ld(soup) if _is_event(n)), None)
    if node is None:
        return None

    title = _text(node.get("name"))
    if not title:
        return None

    location = node.get("location") or {}
    if isinstance(location, list):
        location = location[0] if location else {}
    if not isinstance(location, dict):
        location = {}

    address = location.get("address") or {}
    if isinstance(address, str):
        street, city = address, ""
    elif isinstance(address, dict):
        street = _text(address.get("streetAddress"))
        city = _text(address.get("addressLocality"))
    else:
        street, city = "", ""

    is_free, price_min, price_max, ticket_url = _offers(node)

    occurrences = []
    start = _parse_datetime(node.get("startDate"))
    if start:
        occurrences.append(
            ExtractedOccurrence(start=start, end=_parse_datetime(node.get("endDate")))
        )

    image = node.get("image")
    image_url = _text(image) if not isinstance(image, dict) else _text(image.get("url"))

    return EventDraft(
        title=title[:250],
        summary=_text(node.get("description"))[:300],
        description=_text(node.get("description")),
        venue_name=_text(location.get("name"))[:200],
        venue_address=street[:300],
        venue_city=city[:120],
        organizer_name=_text(node.get("organizer"))[:200],
        is_free=is_free,
        price_min=price_min,
        price_max=price_max,
        ticket_url=(ticket_url or "")[:500],
        image_url=(image_url or "")[:500],
        occurrences=occurrences,
        notes_for_submitter=(
            "Read from the page's own event data. Please check the date and "
            "venue before submitting."
        ),
    )
