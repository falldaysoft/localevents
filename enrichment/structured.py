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


def _occurrences_for(title, nodes):
    """Every date this event runs on, from every node that names it.

    A recurring listing is routinely published as one Event node per date —
    same name, same place, a different `startDate`. Reading only the first one
    turns a market that runs every Saturday into a market that ran once, and
    the submitter has no way of knowing anything was dropped.

    Matching on the title is what keeps this safe on a page that lists several
    *different* events: only the nodes naming the event we settled on
    contribute dates, and the others are ignored exactly as before.

    A node's `endDate` may fall on a later day than its start. That is not
    treated specially — it is a span, and it stays one occurrence.
    """
    seen = set()
    occurrences = []

    for node in nodes:
        if _text(node.get("name")) != title:
            continue
        start = _parse_datetime(node.get("startDate"))
        if start is None or start in seen:
            continue
        seen.add(start)

        end = _parse_datetime(node.get("endDate"))
        # A publisher that stamps endDate equal to startDate is saying nothing
        # about duration, and an end that precedes its start is a typo. Either
        # way the honest answer is "not stated" — and Occurrence will not store
        # it, since the database refuses an end that is not after the start.
        if end is not None and end <= start:
            end = None
        occurrences.append(ExtractedOccurrence(start=start, end=end))

    return sorted(occurrences, key=lambda o: o.start)


def extract(html, source_url=""):
    """Build an EventDraft from schema.org markup, or return None.

    Returns None rather than a mostly-empty draft when the page has no Event
    node, so the caller can fall through to the model. A draft with no title is
    worse than no draft.
    """
    soup = BeautifulSoup(html, "html.parser")

    nodes = [n for n in _iter_json_ld(soup) if _is_event(n)]
    if not nodes:
        return None

    node = nodes[0]
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

    occurrences = _occurrences_for(title, nodes)

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
        # Say how many dates came back. Silence here reads as "we found the
        # date", and a submitter skimming a page of prefilled fields will not
        # count the rows unless something tells them there is a count to check.
        is_series=len(occurrences) > 1,
        notes_for_submitter=(
            "Read from the page's own event data. Please check the "
            + (
                f"{len(occurrences)} dates and the venue"
                if len(occurrences) > 1
                else "date and venue"
            )
            + " before submitting."
        ),
    )
