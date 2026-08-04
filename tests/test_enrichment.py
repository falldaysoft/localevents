"""Reading an event out of a page.

The parts worth protecting: that structured data is tried before anything is
spent, that the SSRF guard holds, and that every failure lands the submitter on
a usable form rather than an error page.

The model client itself is `test_llm.py`. Everything here patches `llm.extract`
whole, so this file needs no stand-in for the OpenAI client — two fakes for one
call is how one of them drifts.
"""

from datetime import datetime

import httpx
import pytest

from core.models import AIConfig
from enrichment import llm, pipeline, structured
from enrichment.fetcher import FetchError, validate_url
from enrichment.models import EnrichmentRun
from enrichment.schemas import EventDraft

# --- a page that publishes its own event data ------------------------------

JSON_LD_PAGE = """
<html><head>
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "Event",
  "name": "Spring Choir Concert",
  "description": "An evening of choral music.",
  "startDate": "2099-05-14T19:30:00",
  "endDate": "2099-05-14T21:00:00",
  "location": {
    "@type": "Place",
    "name": "Community Hall",
    "address": {
      "@type": "PostalAddress",
      "streetAddress": "12 River Road",
      "addressLocality": "Anytown"
    }
  },
  "organizer": {"@type": "Organization", "name": "Anytown Choir"},
  "offers": {"@type": "Offer", "price": "12.00", "url": "https://example.org/tickets"}
}
</script>
</head><body><p>Come along.</p></body></html>
"""

FREE_EVENT_PAGE = JSON_LD_PAGE.replace('"price": "12.00"', '"price": "0"')

GRAPH_PAGE = """
<html><head>
<script type="application/ld+json">
{"@context":"https://schema.org","@graph":[
  {"@type":"WebSite","name":"Some Venue"},
  {"@type":"MusicEvent","name":"Jazz Night","startDate":"2099-06-01T20:00:00"}
]}
</script></head><body></body></html>
"""

PLAIN_PAGE = "<html><body><h1>Craft Fair</h1><p>Saturday at the hall.</p></body></html>"


# --- structured extraction -------------------------------------------------


def test_structured_extraction_reads_the_page_verbatim():
    draft = structured.extract(JSON_LD_PAGE, "https://example.org/e")

    assert draft.title == "Spring Choir Concert"
    assert draft.venue_name == "Community Hall"
    assert draft.venue_address == "12 River Road"
    assert draft.venue_city == "Anytown"
    assert draft.organizer_name == "Anytown Choir"
    assert draft.price_min == 12.0
    assert draft.is_free is False
    assert draft.ticket_url == "https://example.org/tickets"
    assert draft.occurrences[0].start == datetime(2099, 5, 14, 19, 30)


def test_a_stated_price_of_zero_means_free():
    """Different from a page that never mentions cost, which we must not guess."""
    assert structured.extract(FREE_EVENT_PAGE, "").is_free is True
    assert structured.extract(JSON_LD_PAGE, "").is_free is False


def test_structured_extraction_walks_an_at_graph():
    draft = structured.extract(GRAPH_PAGE, "")
    assert draft.title == "Jazz Night"


def test_structured_extraction_returns_none_without_event_markup():
    """None, not an empty draft — the caller needs to know to try the model."""
    assert structured.extract(PLAIN_PAGE, "") is None


def test_malformed_json_ld_does_not_break_the_page():
    broken = '<html><script type="application/ld+json">{not json</script></html>'
    assert structured.extract(broken, "") is None


# --- fetching --------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/admin",
        "http://localhost:8000/",
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata
        "http://10.0.0.1/",
    ],
)
def test_private_addresses_are_refused(url):
    """The URL is user-supplied; without this the fetcher is an SSRF gadget
    aimed at anything the cluster can reach."""
    with pytest.raises(FetchError):
        validate_url(url)


@pytest.mark.parametrize("url", ["ftp://example.org/x", "file:///etc/passwd", "javascript:alert(1)"])
def test_only_http_urls_are_accepted(url):
    with pytest.raises(FetchError):
        validate_url(url)


def test_public_urls_are_accepted():
    assert validate_url("https://example.org/events/1").hostname == "example.org"


# --- the pipeline's ordering ----------------------------------------------


@pytest.fixture
def no_network(monkeypatch):
    """Serve a canned page instead of making a request."""

    def serve(html):
        monkeypatch.setattr(
            pipeline, "fetch", lambda url: ("https://example.org/e", html)
        )

    return serve


@pytest.mark.django_db
def test_structured_data_is_used_before_spending_anything(no_network, monkeypatch):
    """The point of the ordering: a page with markup costs nothing."""
    called = []
    monkeypatch.setattr(llm, "extract", lambda *a, **k: called.append(1))
    no_network(JSON_LD_PAGE)

    result = pipeline.enrich_url("https://example.org/e")

    assert result.succeeded
    assert result.method == EnrichmentRun.Method.STRUCTURED
    assert called == [], "model was called despite the page publishing event data"

    run = EnrichmentRun.objects.get()
    assert run.was_free
    assert run.estimated_cost_usd == 0


@pytest.mark.django_db
def test_the_model_is_used_when_the_page_has_no_markup(no_network, monkeypatch):
    config = AIConfig.load()
    config.enabled = True
    config.api_key = "test-key"
    config.save()

    draft = EventDraft(title="Craft Fair")
    monkeypatch.setattr(
        llm, "extract",
        lambda *a, **k: (draft, {"input_tokens": 1000, "output_tokens": 200}),
    )
    no_network(PLAIN_PAGE)

    result = pipeline.enrich_url("https://example.org/e")

    assert result.succeeded
    assert result.method == EnrichmentRun.Method.LLM

    run = EnrichmentRun.objects.get()
    assert run.input_tokens == 1000
    assert run.estimated_cost_usd > 0


@pytest.mark.django_db
def test_enrichment_disabled_falls_through_to_a_manual_form(no_network):
    config = AIConfig.load()
    config.enabled = False
    config.save()
    no_network(PLAIN_PAGE)

    result = pipeline.enrich_url("https://example.org/e")

    assert not result.succeeded
    assert result.failed
    assert "yourself" in result.message
    assert EnrichmentRun.objects.get().status == EnrichmentRun.Status.SKIPPED


@pytest.mark.django_db
def test_spend_cap_stops_further_model_calls(no_network, monkeypatch):
    config = AIConfig.load()
    config.enabled = True
    config.api_key = "test-key"
    config.daily_spend_cap_usd = 1
    config.save()

    EnrichmentRun.objects.create(
        method=EnrichmentRun.Method.LLM,
        status=EnrichmentRun.Status.OK,
        estimated_cost_usd=5,
    )
    called = []
    monkeypatch.setattr(llm, "extract", lambda *a, **k: called.append(1))
    no_network(PLAIN_PAGE)

    result = pipeline.enrich_url("https://example.org/e")

    assert called == []
    assert result.failed


@pytest.mark.django_db
def test_a_model_failure_still_gives_the_submitter_a_form(no_network, monkeypatch):
    config = AIConfig.load()
    config.enabled = True
    config.api_key = "test-key"
    config.save()

    monkeypatch.setattr(
        llm, "extract",
        lambda *a, **k: (_ for _ in ()).throw(llm.LLMError("model exploded")),
    )
    no_network(PLAIN_PAGE)

    result = pipeline.enrich_url("https://example.org/e")

    assert not result.succeeded
    assert "yourself" in result.message
    run = EnrichmentRun.objects.get()
    assert run.status == EnrichmentRun.Status.FAILED
    assert "exploded" in run.error


@pytest.mark.django_db
def test_a_fetch_failure_is_reported_in_words_a_person_can_act_on(monkeypatch):
    monkeypatch.setattr(
        pipeline, "fetch",
        lambda url: (_ for _ in ()).throw(
            FetchError("That page needs a login, so it can't be read automatically.")
        ),
    )
    result = pipeline.enrich_url("https://example.org/private")

    assert result.failed
    assert "login" in result.message


# --- cost accounting -------------------------------------------------------


@pytest.mark.django_db
def test_cost_estimate_uses_the_configured_rates():
    config = AIConfig.load()
    config.input_cost_per_mtok = 3
    config.output_cost_per_mtok = 15
    config.save()

    # 1M in + 1M out at $3/$15
    assert config.estimate_cost(1_000_000, 1_000_000) == 18
