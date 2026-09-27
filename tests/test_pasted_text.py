"""Submitting an event by pasting its text.

For the pages the fetcher cannot open — a Facebook event serves a login wall to
anything but a signed-in browser — the submitter copies the post and pastes it.
What matters: the text is read instead of the link, never alongside it; a read
that cannot happen still leaves the text in the form rather than discarding
what the submitter pasted to save typing; and the moderator can see the words
the listing came from, since the link behind them usually will not open.
"""

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from core.models import AIConfig
from enrichment import llm, pipeline
from enrichment.models import EnrichmentRun
from enrichment.schemas import EventDraft, ExtractedOccurrence
from submissions.models import Submission
from submissions.tasks import enrich_submission

FB_URL = "https://www.facebook.com/events/1234567890/"
POST = (
    "Fall Fair at the Legion Hall\n"
    "Saturday, October 10 at 9 AM – 3 PM\n"
    "Crafts, baking and a silent auction. Free admission."
)


@pytest.fixture
def signed_in(client, submitter):
    client.force_login(submitter)
    return client


@pytest.fixture
def enqueued(monkeypatch):
    queued = []
    monkeypatch.setattr(
        "submissions.views.enrich_submission",
        type("T", (), {"enqueue": staticmethod(lambda pk: queued.append(pk))}),
    )
    return queued


@pytest.fixture
def ai_on():
    config = AIConfig.load()
    config.enabled = True
    config.api_key = "test-key"
    config.save()
    return config


@pytest.fixture
def unfetchable(monkeypatch):
    """Fail the test if anything tries to fetch the link."""

    def refuse(*args, **kwargs):
        raise AssertionError("pasted text must not fetch the link")

    monkeypatch.setattr(pipeline, "fetch", refuse)


@pytest.mark.django_db
def test_the_start_page_offers_a_box_to_paste_into(signed_in):
    body = signed_in.get(reverse("submit")).content.decode()
    assert 'name="pasted_text"' in body


@pytest.mark.django_db
def test_pasted_text_alongside_a_link_is_queued_for_reading(signed_in, enqueued):
    signed_in.post(reverse("submit"), {"source_url": FB_URL, "pasted_text": POST})

    submission = Submission.objects.get()
    assert submission.status == Submission.Status.NEW
    assert submission.source_url == FB_URL
    assert submission.pasted_text == POST
    assert enqueued == [submission.pk]


@pytest.mark.django_db
def test_pasted_text_needs_no_link(signed_in, enqueued):
    signed_in.post(reverse("submit"), {"pasted_text": POST})

    submission = Submission.objects.get()
    assert submission.status == Submission.Status.NEW
    assert submission.source_url == ""
    assert enqueued == [submission.pk]


@pytest.mark.django_db
def test_the_text_is_read_instead_of_the_link(
    submitter, ai_on, unfetchable, monkeypatch
):
    seen = {}

    def fake_extract(config, text, source_url, slugs, pasted=False):
        seen.update(text=text, source_url=source_url, pasted=pasted)
        draft = EventDraft(
            title="Fall Fair",
            occurrences=[
                ExtractedOccurrence(start=timezone.now() + timedelta(days=12))
            ],
        )
        return draft, {"input_tokens": 400, "output_tokens": 150}

    monkeypatch.setattr(llm, "extract", fake_extract)
    submission = Submission.objects.create(
        submitted_by=submitter, source_url=FB_URL, pasted_text=POST
    )

    enrich_submission.call(submission.pk)

    submission.refresh_from_db()
    assert seen == {"text": POST, "source_url": FB_URL, "pasted": True}
    assert submission.status == Submission.Status.AWAITING_SUBMITTER
    assert not submission.enrichment_failed
    assert submission.draft["title"] == "Fall Fair"
    assert EnrichmentRun.objects.get().status == EnrichmentRun.Status.OK


@pytest.mark.django_db
def test_with_reading_switched_off_the_text_lands_in_the_description(
    signed_in, submitter, unfetchable
):
    """Nothing to extract with, but the submitter's paste is not thrown away."""
    submission = Submission.objects.create(
        submitted_by=submitter, source_url=FB_URL, pasted_text=POST
    )

    enrich_submission.call(submission.pk)

    submission.refresh_from_db()
    assert submission.enrichment_failed
    assert "description" in submission.enrichment_message
    assert submission.draft == {"description": POST}

    body = signed_in.get(
        reverse("submission_detail", args=[submission.pk])
    ).content.decode()
    assert "silent auction" in body


@pytest.mark.django_db
def test_a_failed_model_call_keeps_the_text(submitter, ai_on, monkeypatch):
    monkeypatch.setattr(
        llm, "extract",
        lambda *a, **k: (_ for _ in ()).throw(llm.LLMError("model exploded")),
    )
    submission = Submission.objects.create(submitted_by=submitter, pasted_text=POST)

    enrich_submission.call(submission.pk)

    submission.refresh_from_db()
    assert submission.enrichment_failed
    assert submission.draft == {"description": POST}


@pytest.mark.django_db
def test_a_stranded_read_keeps_the_text(submitter):
    submission = Submission.objects.create(
        submitted_by=submitter, pasted_text=POST,
        status=Submission.Status.ENRICHING,
    )

    submission.recover_from_stranding()

    submission.refresh_from_db()
    assert submission.draft == {"description": POST}


@pytest.mark.django_db
def test_the_prompt_says_what_today_is():
    """Without it "the next occurrence of that date" cannot be worked out, and
    a pasted post saying "Saturday, October 10" has no year to go on."""
    prompt = llm._user_prompt(POST, "", ["markets"], pasted=True)

    assert f"Today's date: {timezone.localdate().isoformat()}" in prompt
    assert "Text pasted by the submitter" in prompt
    assert POST in prompt


@pytest.mark.django_db
def test_the_moderator_sees_what_was_pasted(client, moderator, submitter):
    submission = Submission.objects.create(
        submitted_by=submitter, source_url=FB_URL, pasted_text=POST,
        status=Submission.Status.PENDING_REVIEW,
    )
    client.force_login(moderator)

    body = client.get(reverse("mod_submission", args=[submission.pk])).content.decode()

    assert "Text the submitter pasted" in body
    assert "silent auction" in body
