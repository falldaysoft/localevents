"""The submitter's journey.

The property that matters most: nothing an AI produced reaches a moderator
without the submitter having confirmed it. That is what makes a cheap,
imperfect model an acceptable trade — a rough extraction costs a submitter a
minute of editing instead of putting invented-but-plausible details in front of
someone who will trust them.
"""

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from core.models import AIConfig
from enrichment import pipeline
from enrichment.models import EnrichmentRun
from enrichment.schemas import EventDraft, ExtractedOccurrence
from events.models import Event, Occurrence, Venue
from submissions.models import ModerationAction, Submission, SubmissionQuota
from submissions.tasks import enrich_submission


@pytest.fixture
def submitter(db, django_user_model):
    from allauth.account.models import EmailAddress

    user = django_user_model.objects.create_user(
        username="resident", email="resident@example.com", password="pw-12345678"
    )
    EmailAddress.objects.create(
        user=user, email=user.email, primary=True, verified=True
    )
    return user


@pytest.fixture
def signed_in(client, submitter):
    client.force_login(submitter)
    return client


def _valid_form_data(**overrides):
    start = timezone.now() + timedelta(days=10)
    data = {
        "title": "Spring Choir Concert",
        "summary": "An evening of choral music.",
        "description": "",
        "venue_name": "Community Hall",
        "venue_address": "12 River Road",
        "venue_city": "Anytown",
        "organizer_name": "Anytown Choir",
        "starts_at": start.strftime("%Y-%m-%d %H:%M"),
        "ends_at": "",
        "additional_dates": "",
        "price_note": "",
        "accessibility_notes": "",
        "source_url": "",
        "ticket_url": "",
    }
    data.update(overrides)
    return data


# --- access ----------------------------------------------------------------


@pytest.mark.django_db
def test_submitting_requires_an_account(client):
    response = client.get(reverse("submit"))
    assert response.status_code == 302
    assert "login" in response["Location"]


@pytest.mark.django_db
def test_a_submitter_cannot_open_someone_elses_submission(
    signed_in, django_user_model
):
    other = django_user_model.objects.create_user(
        username="other", email="other@example.com", password="pw-12345678"
    )
    theirs = Submission.objects.create(
        submitted_by=other, status=Submission.Status.AWAITING_SUBMITTER
    )
    assert signed_in.get(
        reverse("submission_detail", args=[theirs.pk])
    ).status_code == 404


# --- the URL path ----------------------------------------------------------


@pytest.mark.django_db
def test_pasting_a_url_queues_enrichment(signed_in, submitter, monkeypatch):
    enqueued = []
    monkeypatch.setattr(
        "submissions.views.enrich_submission",
        type("T", (), {"enqueue": staticmethod(lambda pk: enqueued.append(pk))}),
    )

    response = signed_in.post(
        reverse("submit"), {"source_url": "https://example.org/event"}
    )

    submission = Submission.objects.get()
    assert submission.status == Submission.Status.NEW
    assert submission.source_url == "https://example.org/event"
    assert enqueued == [submission.pk]
    assert response["Location"] == reverse("submission_detail", args=[submission.pk])


@pytest.mark.django_db
def test_a_submission_with_no_url_goes_straight_to_the_form(signed_in):
    """Many community events have no web page at all.

    Requiring a link would quietly exclude exactly the small local things this
    site exists for.
    """
    signed_in.post(reverse("submit"), {"manual": "1"})

    submission = Submission.objects.get()
    assert submission.status == Submission.Status.AWAITING_SUBMITTER
    assert submission.source_url == ""


@pytest.mark.django_db
def test_start_form_rejects_an_empty_submission(signed_in):
    response = signed_in.post(reverse("submit"), {})
    assert response.status_code == 200
    assert Submission.objects.count() == 0


# --- enrichment lands the submitter on a usable form -----------------------


@pytest.mark.django_db
def test_enrichment_populates_the_draft(submitter, monkeypatch):
    submission = Submission.objects.create(
        submitted_by=submitter, source_url="https://example.org/e"
    )
    draft = EventDraft(
        title="Spring Choir Concert",
        venue_name="Community Hall",
        occurrences=[ExtractedOccurrence(start=timezone.now() + timedelta(days=9))],
    )
    # Patch where the name is used, not where it is defined — tasks.py binds
    # enrich_url at import time.
    monkeypatch.setattr(
        "submissions.tasks.enrich_url",
        lambda url, submission=None: pipeline.EnrichmentResult(
            draft=draft, method=EnrichmentRun.Method.LLM, message="Filled in."
        ),
    )

    enrich_submission.call(submission.pk)

    submission.refresh_from_db()
    assert submission.status == Submission.Status.AWAITING_SUBMITTER
    assert submission.draft["title"] == "Spring Choir Concert"


@pytest.mark.django_db
def test_a_crash_during_enrichment_does_not_strand_the_submission(
    submitter, monkeypatch
):
    """Left in 'reading the page' the submitter would poll forever."""
    submission = Submission.objects.create(
        submitted_by=submitter, source_url="https://example.org/e"
    )
    monkeypatch.setattr(
        "submissions.tasks.enrich_url",
        lambda url, submission=None: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    enrich_submission.call(submission.pk)  # must not raise

    submission.refresh_from_db()
    assert submission.status == Submission.Status.AWAITING_SUBMITTER
    assert submission.enrichment_failed


@pytest.mark.django_db
def test_the_form_is_prefilled_from_the_draft(signed_in, submitter):
    start = timezone.now() + timedelta(days=9)
    submission = Submission.objects.create(
        submitted_by=submitter,
        status=Submission.Status.AWAITING_SUBMITTER,
        draft=EventDraft(
            title="Spring Choir Concert",
            venue_name="Community Hall",
            is_free=True,
            occurrences=[ExtractedOccurrence(start=start)],
        ).model_dump(mode="json"),
    )

    response = signed_in.get(reverse("submission_detail", args=[submission.pk]))
    initial = response.context["form"].initial

    assert initial["title"] == "Spring Choir Concert"
    assert initial["venue_name"] == "Community Hall"
    assert initial["is_free"] is True
    assert initial["starts_at"].startswith(start.strftime("%Y-%m-%d"))


# --- the confirmation step -------------------------------------------------


@pytest.mark.django_db
def test_confirming_creates_an_event_awaiting_review_not_a_published_one(
    signed_in, submitter
):
    """The submitter confirms the details are right, not that it belongs here.

    Publishing is a moderator's decision, and prominence is theirs too.
    """
    submission = Submission.objects.create(
        submitted_by=submitter, status=Submission.Status.AWAITING_SUBMITTER
    )

    signed_in.post(
        reverse("submission_detail", args=[submission.pk]), _valid_form_data()
    )

    event = Event.objects.get()
    assert event.status == Event.Status.PENDING
    assert event.prominence == Event.Prominence.LISTED  # the default, untouched
    assert event.source == Event.Source.SUBMISSION
    assert event.submitted_by == submitter

    submission.refresh_from_db()
    assert submission.status == Submission.Status.PENDING_REVIEW
    assert submission.event == event


@pytest.mark.django_db
def test_confirming_reuses_an_existing_venue(signed_in, submitter):
    """Three events at the hall should share one pin, and one geocode."""
    existing = Venue.objects.create(name="Community Hall", city="Anytown")
    submission = Submission.objects.create(
        submitted_by=submitter, status=Submission.Status.AWAITING_SUBMITTER
    )

    signed_in.post(
        reverse("submission_detail", args=[submission.pk]), _valid_form_data()
    )

    assert Venue.objects.count() == 1
    assert Event.objects.get().venue == existing


@pytest.mark.django_db
def test_a_series_submission_records_every_date(signed_in, submitter):
    first = timezone.now() + timedelta(days=7)
    extra = [first + timedelta(days=7 * n) for n in (1, 2)]
    submission = Submission.objects.create(
        submitted_by=submitter, status=Submission.Status.AWAITING_SUBMITTER
    )

    signed_in.post(
        reverse("submission_detail", args=[submission.pk]),
        _valid_form_data(
            starts_at=first.strftime("%Y-%m-%d %H:%M"),
            is_series="on",
            additional_dates="\n".join(d.strftime("%Y-%m-%d %H:%M") for d in extra),
        ),
    )

    event = Event.objects.get()
    assert event.listing_type == Event.ListingType.SERIES
    assert Occurrence.objects.filter(event=event).count() == 3


@pytest.mark.django_db
def test_a_past_date_is_rejected_with_a_useful_message(signed_in, submitter):
    submission = Submission.objects.create(
        submitted_by=submitter, status=Submission.Status.AWAITING_SUBMITTER
    )
    response = signed_in.post(
        reverse("submission_detail", args=[submission.pk]),
        _valid_form_data(starts_at=(timezone.now() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M")),
    )

    assert Event.objects.count() == 0
    assert "already passed" in str(response.context["form"].errors)


@pytest.mark.django_db
def test_a_bad_extra_date_names_the_line_that_failed(signed_in, submitter):
    """A series can carry a dozen dates; 'invalid format' would mean hunting."""
    submission = Submission.objects.create(
        submitted_by=submitter, status=Submission.Status.AWAITING_SUBMITTER
    )
    good = (timezone.now() + timedelta(days=14)).strftime("%Y-%m-%d %H:%M")
    response = signed_in.post(
        reverse("submission_detail", args=[submission.pk]),
        _valid_form_data(additional_dates=f"{good}\nnext tuesday"),
    )

    errors = str(response.context["form"].errors)
    assert "Line 2" in errors and "next tuesday" in errors


@pytest.mark.django_db
def test_a_misextracted_year_is_caught_at_the_confirmation_step(
    signed_in, submitter
):
    """The safety net, observed working against a live model.

    A real extraction from a library page returned the right event name with
    the wrong year — a date in the past. The submitter is the check on that,
    and the form has to make the mistake obvious rather than accept it.
    """
    submission = Submission.objects.create(
        submitted_by=submitter,
        status=Submission.Status.AWAITING_SUBMITTER,
        draft=EventDraft(
            title="Tween Freeze Fest",
            occurrences=[
                ExtractedOccurrence(start=timezone.now() - timedelta(days=365))
            ],
        ).model_dump(mode="json"),
    )

    response = signed_in.post(
        reverse("submission_detail", args=[submission.pk]),
        _valid_form_data(
            title="Tween Freeze Fest",
            starts_at=(timezone.now() - timedelta(days=365)).strftime("%Y-%m-%d %H:%M"),
        ),
    )

    assert Event.objects.count() == 0
    assert "right year" in str(response.context["form"].errors)


@pytest.mark.django_db
def test_a_manual_submission_is_not_shown_as_untitled(signed_in, submitter):
    """The draft is empty when there was nothing to extract.

    Falling back to the event's own title is what keeps the submitter's list
    readable — this showed as "Untitled" in the browser before the fix.
    """
    submission = Submission.objects.create(
        submitted_by=submitter, status=Submission.Status.AWAITING_SUBMITTER
    )
    signed_in.post(
        reverse("submission_detail", args=[submission.pk]),
        _valid_form_data(title="Riverside Community Barbecue"),
    )

    submission.refresh_from_db()
    assert submission.display_title == "Riverside Community Barbecue"

    body = signed_in.get(reverse("my_submissions")).content.decode()
    assert "Riverside Community Barbecue" in body
    assert "Untitled" not in body


@pytest.mark.django_db
def test_confirming_writes_an_audit_record(signed_in, submitter):
    submission = Submission.objects.create(
        submitted_by=submitter, status=Submission.Status.AWAITING_SUBMITTER
    )
    signed_in.post(
        reverse("submission_detail", args=[submission.pk]), _valid_form_data()
    )

    action = ModerationAction.objects.get()
    assert action.action == "submitted_for_review"
    assert action.actor == submitter


# --- quotas ----------------------------------------------------------------


@pytest.mark.django_db
def test_daily_submission_cap_protects_the_queue(signed_in, submitter):
    quota = SubmissionQuota.for_user(submitter)
    quota.submissions_per_day = 2
    quota.save()

    for _ in range(2):
        Submission.objects.create(submitted_by=submitter)

    response = signed_in.get(reverse("submit"))
    assert "today's limit" in response.content.decode()


@pytest.mark.django_db
def test_running_out_of_page_reads_still_allows_a_manual_submission(
    signed_in, submitter
):
    """The two limits protect different things.

    Submissions protect the moderators' attention; page reads protect the API
    bill. Exhausting the second must not block the first.
    """
    quota = SubmissionQuota.for_user(submitter)
    quota.enrichments_per_day = 0
    quota.save()

    signed_in.post(reverse("submit"), {"source_url": "https://example.org/e"})

    submission = Submission.objects.get()
    assert submission.status == Submission.Status.AWAITING_SUBMITTER
    assert submission.enrichment_failed
    assert "yourself" in submission.enrichment_message


# --- the polling page ------------------------------------------------------


@pytest.mark.django_db
def test_status_fragment_redirects_once_the_read_is_done(signed_in, submitter):
    submission = Submission.objects.create(
        submitted_by=submitter, status=Submission.Status.AWAITING_SUBMITTER
    )
    response = signed_in.get(reverse("submission_status", args=[submission.pk]))
    assert response["HX-Redirect"] == reverse(
        "submission_detail", args=[submission.pk]
    )


@pytest.mark.django_db
def test_status_fragment_keeps_polling_while_working(signed_in, submitter):
    submission = Submission.objects.create(
        submitted_by=submitter, status=Submission.Status.ENRICHING
    )
    response = signed_in.get(reverse("submission_status", args=[submission.pk]))
    assert "HX-Redirect" not in response
    assert "hx-get" in response.content.decode()


# --- primary sources -------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://www.eventbrite.ca/e/some-event-tickets-123",
        "https://www.facebook.com/events/123456",
        "https://meetup.com/some-group/events/123/",
        "https://TICKETMASTER.com/event/abc",
    ],
)
def test_aggregator_links_get_a_suggestion(url):
    from submissions.sources import advice_for

    advice = advice_for(url)
    assert advice
    assert "organiser" in advice


@pytest.mark.parametrize(
    "url",
    [
        "https://www.somelibrary.ca/en/events.aspx",
        "https://stpaulschurch.org/whats-on",
        "https://thelocalvenue.co.uk/gigs",
        "",
    ],
)
def test_primary_source_links_are_left_alone(url):
    from submissions.sources import advice_for

    assert advice_for(url) == ""


@pytest.mark.django_db
def test_an_aggregator_link_is_never_blocked(signed_in, submitter, monkeypatch):
    """Nudge, not a rule.

    Some organisers genuinely have no other presence, and refusing those would
    lose real local events — which is the opposite of the point.
    """
    monkeypatch.setattr(
        "submissions.views.enrich_submission",
        type("T", (), {"enqueue": staticmethod(lambda pk: None)}),
    )

    signed_in.post(
        reverse("submit"),
        {"source_url": "https://www.eventbrite.ca/e/village-fete-tickets-99"},
    )

    submission = Submission.objects.get()
    assert submission.source_url  # accepted
    assert submission.source_advice  # but advised


# --- surviving a dead worker -----------------------------------------------


@pytest.mark.django_db
def test_a_stranded_submission_is_released_to_its_owner(signed_in, submitter):
    """A worker that dies mid-task never releases its claim.

    Observed for real: restarting the worker left two rows stuck in RUNNING and
    the submission in "reading the page" with nothing to move it on. Without
    recovery the submitter watches a spinner until they give up.
    """
    from submissions.models import STALE_ENRICHMENT_AFTER

    submission = Submission.objects.create(
        submitted_by=submitter,
        source_url="https://example.org/e",
        status=Submission.Status.ENRICHING,
    )
    # Backdate past the threshold. auto_now blocks assignment, so update().
    Submission.objects.filter(pk=submission.pk).update(
        updated_at=timezone.now() - STALE_ENRICHMENT_AFTER - timedelta(minutes=1)
    )
    submission.refresh_from_db()
    assert submission.is_stranded

    response = signed_in.get(reverse("submission_detail", args=[submission.pk]))

    submission.refresh_from_db()
    assert submission.status == Submission.Status.AWAITING_SUBMITTER
    assert submission.enrichment_failed
    assert "yourself" in submission.enrichment_message
    # ...and they land on a usable form, not another spinner.
    assert response.context["form"] is not None


@pytest.mark.django_db
def test_a_slow_but_live_enrichment_is_left_alone(signed_in, submitter):
    """A model call legitimately takes a couple of minutes. Don't cut it off."""
    submission = Submission.objects.create(
        submitted_by=submitter,
        source_url="https://example.org/e",
        status=Submission.Status.ENRICHING,
    )
    assert not submission.is_stranded

    signed_in.get(reverse("submission_detail", args=[submission.pk]))
    submission.refresh_from_db()
    assert submission.status == Submission.Status.ENRICHING


@pytest.mark.django_db
def test_housekeeping_sweeps_stranded_submissions(submitter):
    """The view only helps people who come back; this catches the rest.

    Matters most right after a deploy, which is exactly when workers get
    restarted mid-task.
    """
    from django.core.management import call_command

    from submissions.models import STALE_ENRICHMENT_AFTER

    submission = Submission.objects.create(
        submitted_by=submitter, status=Submission.Status.ENRICHING
    )
    Submission.objects.filter(pk=submission.pk).update(
        updated_at=timezone.now() - STALE_ENRICHMENT_AFTER - timedelta(minutes=1)
    )

    call_command("run_housekeeping")

    submission.refresh_from_db()
    assert submission.status == Submission.Status.AWAITING_SUBMITTER
