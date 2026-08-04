"""The moderators' side.

Two properties carry this file. First, a decision is never partial: the event,
the submission, the audit row and the submitter's email move together or not at
all — a queue that publishes an event and forgets to close the submission looks
fine until a moderator decides the same thing twice.

Second, and structural: the crowd nominates and a human decides. Interest may
rank an event inside the Rising queue and may never, on its own, move it
between prominence tiers. That bound is the reason anonymous interest with weak
deduplication is a safe signal to collect at all, so it is tested directly
rather than left as a comment.
"""

from datetime import timedelta

import pytest
from django.core import mail
from django.urls import reverse
from django.utils import timezone

from events.models import Category, Event, Interest, Occurrence, Venue
from moderation import services
from submissions.models import ModerationAction, Submission, SubmissionMessage


@pytest.fixture
def signed_in_mod(client, moderator):
    client.force_login(moderator)
    return client


@pytest.fixture
def decide(signed_in_mod, django_capture_on_commit_callbacks):
    """POST a decision, and let the callbacks it queues actually run.

    A decision queues the submitter's email with `transaction.on_commit`, so a
    decision that rolls back never emails anyone. pytest-django wraps each test
    in a transaction it never commits — so without this, the mail assertions
    below would pass vacuously against an empty outbox.
    """

    def _decide(name, target, data=None):
        pk = getattr(target, "pk", target)
        with django_capture_on_commit_callbacks(execute=True):
            return signed_in_mod.post(reverse(name, args=[pk]), data or {})

    return _decide


def _pending(submitter, title="Spring Choir Concert", **event_kwargs):
    """A submission that has been confirmed and is waiting for a decision."""
    event = Event.objects.create(
        title=title,
        status=Event.Status.PENDING,
        source=Event.Source.SUBMISSION,
        submitted_by=submitter,
        **event_kwargs,
    )
    Occurrence.objects.create(event=event, start=timezone.now() + timedelta(days=10))
    return Submission.objects.create(
        submitted_by=submitter,
        status=Submission.Status.PENDING_REVIEW,
        event=event,
        source_url="https://example.org/e",
    )


# --- who may look -----------------------------------------------------------


@pytest.mark.django_db
def test_the_queue_is_closed_to_anonymous_visitors(client):
    response = client.get(reverse("mod_queue"))
    assert response.status_code == 302
    assert "login" in response["Location"]


@pytest.mark.django_db
def test_a_signed_in_non_moderator_is_refused(client, submitter):
    client.force_login(submitter)
    assert client.get(reverse("mod_queue")).status_code == 403


@pytest.mark.django_db
def test_a_submitter_cannot_approve_their_own_event(client, submitter):
    """The obvious attack, and the reason every view is behind one decorator."""
    submission = _pending(submitter)
    client.force_login(submitter)

    response = client.post(
        reverse("mod_approve", args=[submission.pk]),
        {"prominence": Event.Prominence.FEATURED, "listing_type": "one_off"},
    )

    assert response.status_code == 403
    submission.event.refresh_from_db()
    assert submission.event.status == Event.Status.PENDING


@pytest.mark.django_db
def test_a_moderator_sees_the_pending_queue(signed_in_mod, submitter):
    _pending(submitter, title="Riverside Barbecue")
    body = signed_in_mod.get(reverse("mod_queue")).content.decode()
    assert "Riverside Barbecue" in body


# --- assignment -------------------------------------------------------------


@pytest.mark.django_db
def test_assignment_is_a_toggle(signed_in_mod, moderator, submitter):
    submission = _pending(submitter)

    signed_in_mod.post(reverse("mod_assign", args=[submission.pk]))
    submission.refresh_from_db()
    assert submission.assigned_to == moderator

    signed_in_mod.post(reverse("mod_assign", args=[submission.pk]))
    submission.refresh_from_db()
    assert submission.assigned_to is None


@pytest.mark.django_db
def test_someone_elses_assignment_never_blocks_a_decision(
    signed_in_mod, submitter, django_user_model
):
    """A queue that can be locked by a volunteer who then goes on holiday is
    worse than one that occasionally doubles up."""
    other = django_user_model.objects.create_user(
        username="other-mod", email="other-mod@example.com", password="pw"
    )
    submission = _pending(submitter)
    submission.assigned_to = other
    submission.save()

    signed_in_mod.post(
        reverse("mod_approve", args=[submission.pk]),
        {"prominence": Event.Prominence.LISTED, "listing_type": "one_off"},
    )

    submission.refresh_from_db()
    assert submission.status == Submission.Status.APPROVED


# --- approving --------------------------------------------------------------


@pytest.mark.django_db
def test_approving_publishes_at_the_moderators_prominence(
    decide, moderator, submitter
):
    submission = _pending(submitter)
    category = Category.objects.filter(is_active=True).first()  # seeded

    decide(
        "mod_approve",
        submission,
        {
            "prominence": Event.Prominence.FEATURED,
            "listing_type": Event.ListingType.SERIES,
            "categories": [category.pk],
        },
    )

    event = Event.objects.get()
    assert event.status == Event.Status.PUBLISHED
    assert event.prominence == Event.Prominence.FEATURED
    assert event.published_at is not None
    assert list(event.categories.all()) == [category]

    submission.refresh_from_db()
    assert submission.status == Submission.Status.APPROVED
    assert submission.decided_by == moderator
    assert submission.decided_at is not None


@pytest.mark.django_db
def test_approving_a_series_generates_its_renewal_token(signed_in_mod, submitter):
    """Event.save() derives the token. A partial save would compute and drop it,
    leaving a series that can never be renewed in one click."""
    submission = _pending(submitter)

    signed_in_mod.post(
        reverse("mod_approve", args=[submission.pk]),
        {
            "prominence": Event.Prominence.BACKGROUND,
            "listing_type": Event.ListingType.SERIES,
        },
    )

    assert Event.objects.get().renewal_token


@pytest.mark.django_db
def test_approving_writes_an_audit_row_and_emails_the_submitter(
    decide, moderator, submitter
):
    submission = _pending(submitter)

    decide(
        "mod_approve",
        submission,
        {"prominence": Event.Prominence.LISTED, "listing_type": "one_off"},
    )

    action = ModerationAction.objects.filter(action="approved").get()
    assert action.actor == moderator
    assert action.event_id == submission.event_id

    assert len(mail.outbox) == 1
    assert mail.outbox[0].to == [submitter.email]
    assert "Spring Choir Concert" in mail.outbox[0].body


@pytest.mark.django_db
def test_a_submission_with_no_event_cannot_be_approved(decide, submitter):
    """Someone who started a submission and never finished it has nothing to
    publish. Approving that is a mistake, not a decision."""
    submission = Submission.objects.create(
        submitted_by=submitter, status=Submission.Status.PENDING_REVIEW
    )

    decide(
        "mod_approve",
        submission,
        {"prominence": Event.Prominence.LISTED, "listing_type": "one_off"},
    )

    submission.refresh_from_db()
    assert submission.status == Submission.Status.PENDING_REVIEW
    assert Event.objects.count() == 0
    assert mail.outbox == []


# --- declining --------------------------------------------------------------


@pytest.mark.django_db
def test_rejecting_never_publishes_and_carries_the_reason(decide, submitter):
    submission = _pending(submitter)

    decide(
        "mod_reject", submission, {"reason": "This is next county over, sorry."}
    )

    submission.refresh_from_db()
    assert submission.status == Submission.Status.REJECTED
    assert submission.event.status == Event.Status.REJECTED
    assert Event.objects.published().count() == 0
    assert "next county" in submission.decision_note
    assert "next county" in mail.outbox[0].body


@pytest.mark.django_db
def test_a_rejection_without_a_reason_is_refused(decide, submitter):
    """A bare 'no' from a community site reads as arbitrary."""
    submission = _pending(submitter)

    decide("mod_reject", submission, {"reason": ""})

    submission.refresh_from_db()
    assert submission.status == Submission.Status.PENDING_REVIEW
    assert mail.outbox == []


# --- asking a question, and the round trip ----------------------------------


@pytest.mark.django_db
def test_requesting_info_hands_the_submission_back_with_the_question(
    decide, moderator, submitter
):
    submission = _pending(submitter)

    decide(
        "mod_request_info", submission, {"body": "Which entrance should people use?"}
    )

    submission.refresh_from_db()
    assert submission.status == Submission.Status.INFO_REQUESTED
    assert submission.is_editable_by_submitter

    message = SubmissionMessage.objects.get()
    assert message.is_from_moderator and message.author == moderator
    assert message.emailed_at is not None  # the task ran and stamped it

    assert "Which entrance" in mail.outbox[0].body


@pytest.mark.django_db
def test_the_submitter_sees_the_question_and_answering_does_not_fork_the_event(
    client, signed_in_mod, submitter
):
    """The bug this guards against is silent and expensive.

    A second confirmation used to create a *second* event, leaving the
    moderator's queue entry pointing at the abandoned first one — so the answer
    to their question landed in a row nobody was looking at.
    """
    submission = _pending(submitter, title="Spring Choir Concert")
    original_event_id = submission.event_id

    signed_in_mod.post(
        reverse("mod_request_info", args=[submission.pk]),
        {"body": "Which entrance should people use?"},
    )

    client.force_login(submitter)
    page = client.get(reverse("submission_detail", args=[submission.pk]))
    assert "Which entrance" in page.content.decode()
    # Prefilled from the event, not the (empty) original extraction.
    assert page.context["form"].initial["title"] == "Spring Choir Concert"

    start = timezone.now() + timedelta(days=12)
    client.post(
        reverse("submission_detail", args=[submission.pk]),
        {
            "title": "Spring Choir Concert",
            "summary": "",
            "description": "",
            "venue_name": "Community Hall",
            "venue_address": "",
            "venue_city": "Anytown",
            "organizer_name": "",
            "price_note": "",
            "accessibility_notes": "",
            "source_url": "",
            "ticket_url": "",
            "reply": "The side door on River Road.",
            "dates-TOTAL_FORMS": "1",
            "dates-INITIAL_FORMS": "1",
            "dates-MIN_NUM_FORMS": "0",
            "dates-MAX_NUM_FORMS": "60",
            "dates-0-start": start.strftime("%Y-%m-%d %H:%M"),
            "dates-0-end": "",
            "dates-0-note": "",
        },
    )

    assert Event.objects.count() == 1
    submission.refresh_from_db()
    assert submission.event_id == original_event_id
    assert submission.status == Submission.Status.PENDING_REVIEW
    assert submission.event.venue.name == "Community Hall"

    # The corrected date replaces the old one rather than joining it.
    assert Occurrence.objects.filter(event_id=original_event_id).count() == 1

    reply = SubmissionMessage.objects.filter(is_from_moderator=False).get()
    assert "side door" in reply.body


# --- the region gate --------------------------------------------------------


@pytest.mark.django_db
def test_a_venue_outside_the_region_is_flagged_not_blocked(
    signed_in_mod, submitter, settings
):
    """Plenty of communities care about something just over the county line."""
    settings.MAP_BBOX = [0.0, 0.0, 1.0, 1.0]
    venue = Venue.objects.create(
        name="Far Hall", latitude=50.0, longitude=50.0,
        geocode_status=Venue.GeocodeStatus.OK,
    )
    submission = _pending(submitter, venue=venue)

    body = signed_in_mod.get(
        reverse("mod_submission", args=[submission.pk])
    ).content.decode()
    assert "Outside the region" in body

    signed_in_mod.post(
        reverse("mod_approve", args=[submission.pk]),
        {"prominence": Event.Prominence.LISTED, "listing_type": "one_off"},
    )
    assert Event.objects.get().status == Event.Status.PUBLISHED


# --- Rising -----------------------------------------------------------------


def _published(title, prominence, interest, days=5):
    event = Event.objects.create(
        title=title, status=Event.Status.PUBLISHED, prominence=prominence
    )
    Occurrence.objects.create(event=event, start=timezone.now() + timedelta(days=days))
    for n in range(interest):
        Interest.objects.create(event=event, fingerprint=f"{title}-{n}")
    event.refresh_from_db()
    return event


# The bound this queue relies on — that interest cannot move an event between
# tiers by itself — is `test_events.py::test_interest_cannot_change_an_events_
# prominence`. What is tested here is the ranking built on top of it.


@pytest.mark.django_db
def test_rising_ranks_within_a_tier_not_across_tiers(db):
    """Raw counts would only rediscover that featured events get more attention.

    The background event here has far *fewer* marks than the listed one, but is
    doing much better for where it sits — which is the promotion a moderator
    should be shown.
    """
    _published("Quiet Program A", Event.Prominence.BACKGROUND, interest=1)
    _published("Quiet Program B", Event.Prominence.BACKGROUND, interest=1)
    standout = _published("Knitting Group", Event.Prominence.BACKGROUND, interest=30)
    _published("Big Concert", Event.Prominence.LISTED, interest=40)
    _published("Other Concert", Event.Prominence.LISTED, interest=40)

    ranked = services.rising_events()

    assert ranked[0] == standout
    assert ranked[0].rising_score > 1
    # The two quiet programs sit below their tier average, so they are not
    # rising and do not belong under this heading at all.
    assert "Quiet Program A" not in [e.title for e in ranked]


@pytest.mark.django_db
def test_a_handful_of_marks_never_reaches_the_queue(db):
    """The small-sample artifact, seen in a live run.

    Promote the one busy event out of a tier and whatever remains *becomes*
    that tier's average — so the next event with two marks reads as "twice the
    average". The ratio is right and the conclusion is nonsense, so an absolute
    floor sits in front of it.
    """
    _published("Thursday Chess Club", Event.Prominence.BACKGROUND, interest=2)

    assert services.rising_events() == []


@pytest.mark.django_db
def test_rising_leaves_out_featured_and_finished_events(db):
    _published("Already Featured", Event.Prominence.FEATURED, interest=50)
    past = _published("Last Month's Fair", Event.Prominence.LISTED, interest=50)
    past.occurrences.update(start=timezone.now() - timedelta(days=30))

    assert services.rising_events() == []


@pytest.mark.django_db
def test_promoting_moves_exactly_one_tier_and_is_recorded(
    signed_in_mod, moderator
):
    event = _published("Knitting Group", Event.Prominence.BACKGROUND, interest=30)

    signed_in_mod.post(reverse("mod_promote", args=[event.pk]))
    event.refresh_from_db()
    assert event.prominence == Event.Prominence.LISTED

    signed_in_mod.post(reverse("mod_promote", args=[event.pk]))
    event.refresh_from_db()
    assert event.prominence == Event.Prominence.FEATURED

    # And no further — there is nowhere above featured.
    signed_in_mod.post(reverse("mod_promote", args=[event.pk]))
    event.refresh_from_db()
    assert event.prominence == Event.Prominence.FEATURED

    actions = ModerationAction.objects.filter(action="promoted")
    assert actions.count() == 2
    assert all(a.actor == moderator for a in actions)


# --- the log ----------------------------------------------------------------


@pytest.mark.django_db
def test_the_audit_log_shows_who_decided_what(signed_in_mod, moderator, submitter):
    submission = _pending(submitter, title="Riverside Barbecue")
    signed_in_mod.post(
        reverse("mod_approve", args=[submission.pk]),
        {"prominence": Event.Prominence.LISTED, "listing_type": "one_off"},
    )

    body = signed_in_mod.get(reverse("mod_audit")).content.decode()
    assert "approved" in body
    assert moderator.display_name in body
    assert "Riverside Barbecue" in body


# --- rendering hygiene ------------------------------------------------------


@pytest.mark.django_db
def test_no_template_syntax_leaks_into_a_moderation_page(signed_in_mod, submitter):
    """The same guard `tests/test_browse.py` puts on the public pages.

    Django's `{# ... #}` is single-line only, and one wrapped over two lines
    renders as visible body text. It happened on the public side once and
    happened again on this page during the build — a check per page area is
    cheaper than remembering.
    """
    submission = _pending(submitter)
    pages = [
        reverse("mod_queue"),
        reverse("mod_rising"),
        reverse("mod_audit"),
        reverse("mod_submission", args=[submission.pk]),
    ]

    for url in pages:
        body = signed_in_mod.get(url).content.decode()
        for marker in ("{#", "#}", "{% ", " %}"):
            assert marker not in body, f"raw template syntax {marker!r} in {url}"


@pytest.mark.django_db
def test_the_decision_panels_need_no_javascript(signed_in_mod, submitter):
    """Alpine cannot run here and the queue must not quietly depend on it.

    This project's CSP withholds 'unsafe-eval'; Alpine's standard build
    compiles every `x-` expression with the Function constructor, so it loads
    and then silently does nothing. Native <details> is what actually works,
    and three disclosure panels are not worth opening eval to script on a site
    that renders text strangers submitted.
    """
    submission = _pending(submitter)
    body = signed_in_mod.get(
        reverse("mod_submission", args=[submission.pk])
    ).content.decode()

    assert "x-show" not in body and "x-data" not in body
    assert body.count("<details") == 3


# --- email content ----------------------------------------------------------


@pytest.mark.django_db
def test_the_approval_email_links_to_the_live_listing(decide, submitter, settings):
    """A background task has no request to derive a hostname from, so a wrong
    SITE_BASE_URL produces links that silently go nowhere."""
    settings.SITE_BASE_URL = "https://events.example.org"
    submission = _pending(submitter)

    decide(
        "mod_approve",
        submission,
        {"prominence": Event.Prominence.LISTED, "listing_type": "one_off"},
    )

    event = Event.objects.get()
    assert f"https://events.example.org/events/{event.slug}/" in mail.outbox[0].body
