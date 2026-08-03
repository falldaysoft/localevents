"""Fetch a page and turn it into a draft listing.

The order is the whole design: structured data first because it is free and
exact, a language model only when the page does not publish any. Both paths
produce the same EventDraft, so nothing downstream needs to know which ran.

Nothing here raises on failure. A submission that could not be enriched still
has to reach the submitter — with an explanation and an empty form — because
"we couldn't read that page" is a normal outcome, not an error condition.
"""

import logging
import time

from bs4 import BeautifulSoup
from django.utils import timezone

from core.models import AIConfig
from events.models import Category

from . import llm, structured
from .fetcher import FetchError, fetch
from .models import EnrichmentRun

logger = logging.getLogger("enrichment.pipeline")


def _endpoint_of(config):
    """Host of the configured endpoint, for the cost record."""
    from urllib.parse import urlparse

    return (urlparse(config.base_url).hostname or "")[:100]


class EnrichmentResult:
    """What the submitter's confirmation page needs to know."""

    def __init__(self, draft=None, method=None, message="", failed=False):
        self.draft = draft
        self.method = method
        self.message = message
        self.failed = failed

    @property
    def succeeded(self):
        return self.draft is not None


def page_text(html):
    """Readable text from a page, with the noise removed.

    Scripts, styles, navigation and footers are mostly boilerplate that would
    crowd out the event details within the character budget.
    """
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "nav", "footer", "header", "form"]):
        tag.decompose()

    text = soup.get_text("\n", strip=True)
    lines = [line for line in text.splitlines() if line.strip()]
    return "\n".join(lines)


def enrich_url(url, submission=None):
    """Read `url` into an EventDraft, recording what it cost."""
    started = time.monotonic()

    try:
        final_url, html = fetch(url)
    except FetchError as exc:
        EnrichmentRun.objects.create(
            submission=submission,
            source_url=url,
            method=EnrichmentRun.Method.STRUCTURED,
            status=EnrichmentRun.Status.FAILED,
            error=str(exc),
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        return EnrichmentResult(message=str(exc), failed=True)

    # --- free path: the page's own event data ------------------------------
    try:
        draft = structured.extract(html, final_url)
    except Exception as exc:  # malformed markup should never break a submission
        logger.warning("structured extraction blew up on %s: %s", url, exc)
        draft = None

    if draft is not None:
        EnrichmentRun.objects.create(
            submission=submission,
            source_url=final_url,
            method=EnrichmentRun.Method.STRUCTURED,
            status=EnrichmentRun.Status.OK,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        return EnrichmentResult(
            draft=draft,
            method=EnrichmentRun.Method.STRUCTURED,
            message="We read the event details straight from the page.",
        )

    # --- paid path: a language model ---------------------------------------
    config = AIConfig.load()

    if not config.enabled:
        EnrichmentRun.objects.create(
            submission=submission, source_url=final_url,
            method=EnrichmentRun.Method.LLM,
            status=EnrichmentRun.Status.SKIPPED,
            error="AI enrichment is switched off.",
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        return EnrichmentResult(
            message="That page doesn't publish event data, so please fill in "
            "the details below yourself.",
            failed=True,
        )

    if not config.is_within_budget():
        logger.warning("daily enrichment spend cap reached; skipping %s", url)
        EnrichmentRun.objects.create(
            submission=submission, source_url=final_url,
            method=EnrichmentRun.Method.LLM,
            status=EnrichmentRun.Status.SKIPPED,
            error="Daily spend cap reached.",
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        return EnrichmentResult(
            message="We've hit today's limit for reading pages automatically. "
            "Please fill in the details below yourself.",
            failed=True,
        )

    slugs = list(
        Category.objects.filter(is_active=True).values_list("slug", flat=True)
    )

    try:
        draft, usage = llm.extract(config, page_text(html), final_url, slugs)
    except llm.LLMError as exc:
        logger.warning("llm extraction failed for %s: %s", url, exc)
        EnrichmentRun.objects.create(
            submission=submission, source_url=final_url,
            method=EnrichmentRun.Method.LLM,
            status=EnrichmentRun.Status.FAILED,
            endpoint=_endpoint_of(config), model=config.model,
            error=str(exc)[:2000],
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        return EnrichmentResult(
            message="We couldn't read that page automatically. Please fill in "
            "the details below yourself.",
            failed=True,
        )

    input_tokens = usage.get("input_tokens", 0)
    output_tokens = usage.get("output_tokens", 0)

    EnrichmentRun.objects.create(
        submission=submission, source_url=final_url,
        method=EnrichmentRun.Method.LLM,
        status=EnrichmentRun.Status.OK,
        endpoint=_endpoint_of(config), model=config.model,
        input_tokens=input_tokens, output_tokens=output_tokens,
        estimated_cost_usd=config.estimate_cost(input_tokens, output_tokens),
        duration_ms=int((time.monotonic() - started) * 1000),
    )

    return EnrichmentResult(
        draft=draft,
        method=EnrichmentRun.Method.LLM,
        message="We've filled in what we could from the page. Please check it "
        "over — especially the dates.",
    )
