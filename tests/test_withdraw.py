"""A submitter taking back their own submission (localevents.1.7).

What matters: a withdrawn submission leaves the queue and its event never
becomes public, by any path — the submitter's button, an agent's API call, or
a moderator acting from a review tab opened before the withdrawal. Once a
moderator has decided, the submission is that decision's record and stays.
"""

import json
from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from accounts.models import ApiToken
from events.models import Event, Occurrence
from moderation import services as moderation
from submissions.models import ModerationAction, Submission
from submissions.services import CannotWithdraw, withdraw


def _pending(user, title="Spring Choir Concert"):
    event = Event.objects.create(
        title=title,
        status=Event.Status.PENDING,
        source=Event.Source.SUBMISSION,
        submitted_by=user,
    )
    Occurrence.objects.create(event=event, start=timezone.now() + timedelta(days=10))
    return Submission.objects.create(
        submitted_by=user,
        status=Submission.Status.PENDING_REVIEW,
        event=event,
        source_url="https://example.org/e",
    )


# --- the service ------------------------------------------------------------


def test_withdrawing_takes_it_out_of_the_queue_and_writes_an_audit_row(submitter):
    submission = _pending(submitter)

    withdraw(submission, submitter, reason="Called off")

    submission.refresh_from_db()
    assert submission.status == Submission.Status.WITHDRAWN
    assert submission.decided_by == submitter
    assert submission.decision_note == "Called off"
    assert submission.event.status == Event.Status.DRAFT
    assert not Submission.objects.awaiting_review().filter(pk=submission.pk).exists()
    action = ModerationAction.objects.get(action="withdrawn")
    assert action.actor == submitter
    assert action.submission == submission
    assert action.event == submission.event


def test_a_withdrawn_event_is_not_public(client, submitter):
    submission = _pending(submitter, title="Riverside Barbecue")
    withdraw(submission, submitter)
    assert "Riverside Barbecue" not in client.get(reverse("index")).content.decode()


@pytest.mark.parametrize(
    "status",
    [
        Submission.Status.NEW,
        Submission.Status.ENRICHING,
        Submission.Status.AWAITING_SUBMITTER,
        Submission.Status.INFO_REQUESTED,
    ],
)
def test_any_open_submission_can_be_withdrawn(submitter, status):
    submission = Submission.objects.create(submitted_by=submitter, status=status)
    withdraw(submission, submitter)
    submission.refresh_from_db()
    assert submission.status == Submission.Status.WITHDRAWN


@pytest.mark.parametrize(
    "status", [Submission.Status.APPROVED, Submission.Status.REJECTED]
)
def test_a_decided_submission_refuses(submitter, status):
    submission = _pending(submitter)
    submission.status = status
    submission.save()

    with pytest.raises(CannotWithdraw, match="can no longer be withdrawn"):
        withdraw(submission, submitter)

    submission.refresh_from_db()
    assert submission.status == status
    assert not ModerationAction.objects.filter(action="withdrawn").exists()


def test_withdrawing_twice_refuses_the_second_time(submitter):
    submission = _pending(submitter)
    withdraw(submission, submitter)
    with pytest.raises(CannotWithdraw):
        withdraw(submission, submitter)
    assert ModerationAction.objects.filter(action="withdrawn").count() == 1


def test_a_listing_a_moderator_already_published_refuses(submitter):
    """Published from the edit form without the submission being decided."""
    submission = _pending(submitter)
    submission.event.status = Event.Status.PUBLISHED
    submission.event.save()

    with pytest.raises(CannotWithdraw, match="already on the site"):
        withdraw(submission, submitter)

    submission.event.refresh_from_db()
    assert submission.event.status == Event.Status.PUBLISHED


def test_a_read_finishing_after_a_withdrawal_does_not_reopen_it(submitter, monkeypatch):
    """Withdrawing is allowed mid-read, and the read takes minutes."""
    from enrichment.pipeline import EnrichmentResult
    from submissions import tasks

    submission = Submission.objects.create(
        submitted_by=submitter,
        source_url="https://example.org/e",
        status=Submission.Status.NEW,
    )

    def read_slowly(url, submission):
        withdraw(submission, submitter)
        return EnrichmentResult(draft=None, failed=True, message="nothing")

    monkeypatch.setattr(tasks, "enrich_url", read_slowly)
    tasks.enrich_submission.call(submission.pk)

    submission.refresh_from_db()
    assert submission.status == Submission.Status.WITHDRAWN


# --- moderators cannot decide on it afterwards --------------------------------


def test_a_stale_review_tab_cannot_approve_it(client, moderator, submitter):
    submission = _pending(submitter)
    withdraw(submission, submitter)
    client.force_login(moderator)

    client.post(
        reverse("mod_approve", args=[submission.pk]),
        {"prominence": Event.Prominence.LISTED, "listing_type": Event.ListingType.ONE_OFF},
    )

    submission.refresh_from_db()
    submission.event.refresh_from_db()
    assert submission.status == Submission.Status.WITHDRAWN
    assert submission.event.status == Event.Status.DRAFT


def test_a_stale_review_tab_cannot_reject_or_ask(client, moderator, submitter):
    submission = _pending(submitter)
    withdraw(submission, submitter)
    client.force_login(moderator)

    client.post(reverse("mod_reject", args=[submission.pk]), {"reason": "No"})
    client.post(reverse("mod_request_info", args=[submission.pk]), {"body": "When?"})

    submission.refresh_from_db()
    assert submission.status == Submission.Status.WITHDRAWN
    assert not submission.messages.exists()


def test_the_service_refuses_too(moderator, submitter):
    submission = _pending(submitter)
    withdraw(submission, submitter)
    with pytest.raises(moderation.Withdrawn):
        moderation.approve(
            submission, moderator,
            prominence=Event.Prominence.LISTED,
            listing_type=Event.ListingType.ONE_OFF,
        )


# --- the web --------------------------------------------------------------------


def test_the_button_is_on_an_open_submissions_page(client, submitter):
    submission = _pending(submitter)
    client.force_login(submitter)
    body = client.get(reverse("submission_detail", args=[submission.pk])).content.decode()
    assert reverse("withdraw_submission", args=[submission.pk]) in body


def test_the_button_is_gone_once_decided(client, submitter):
    submission = _pending(submitter)
    submission.status = Submission.Status.REJECTED
    submission.save()
    client.force_login(submitter)
    body = client.get(reverse("submission_detail", args=[submission.pk])).content.decode()
    assert reverse("withdraw_submission", args=[submission.pk]) not in body


def test_withdrawing_from_the_web(client, submitter):
    submission = _pending(submitter)
    client.force_login(submitter)

    response = client.post(
        reverse("withdraw_submission", args=[submission.pk]), {"reason": "Wrong link"}
    )

    assert response.status_code == 302
    submission.refresh_from_db()
    assert submission.status == Submission.Status.WITHDRAWN
    assert submission.decision_note == "Wrong link"


def test_the_web_refuses_a_decided_one_with_a_message(client, submitter):
    submission = _pending(submitter)
    submission.status = Submission.Status.APPROVED
    submission.save()
    client.force_login(submitter)

    response = client.post(
        reverse("withdraw_submission", args=[submission.pk]), follow=True
    )

    assert "can no longer be withdrawn" in response.content.decode()
    submission.refresh_from_db()
    assert submission.status == Submission.Status.APPROVED


def test_a_get_withdraws_nothing(client, submitter):
    submission = _pending(submitter)
    client.force_login(submitter)
    assert client.get(reverse("withdraw_submission", args=[submission.pk])).status_code == 405
    submission.refresh_from_db()
    assert submission.status == Submission.Status.PENDING_REVIEW


def test_someone_else_cannot_withdraw_it(client, submitter, moderator):
    submission = _pending(submitter)
    client.force_login(moderator)
    response = client.post(reverse("withdraw_submission", args=[submission.pk]))
    assert response.status_code == 404
    submission.refresh_from_db()
    assert submission.status == Submission.Status.PENDING_REVIEW


# --- the API --------------------------------------------------------------------


@pytest.fixture
def api(client, moderator):
    _, key = ApiToken.issue(moderator, "Calendar scout")

    def post(pk, body=None):
        return client.post(
            reverse("api_withdraw_submission", args=[pk]),
            data=json.dumps(body or {}),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {key}",
        )

    return post


def test_withdrawing_through_the_api(api, moderator):
    submission = _pending(moderator)
    response = api(submission.pk, {"reason": "Duplicate of my own"})
    assert response.status_code == 200, response.json()
    assert response.json()["submission"]["status"] == "withdrawn"
    submission.refresh_from_db()
    assert submission.event.status == Event.Status.DRAFT


def test_the_api_refuses_a_decided_one_with_409(api, moderator):
    submission = _pending(moderator)
    submission.status = Submission.Status.APPROVED
    submission.save()
    response = api(submission.pk)
    assert response.status_code == 409
    assert response.json()["submission"]["status"] == "approved"


def test_the_api_cannot_reach_someone_elses(api, submitter):
    assert api(_pending(submitter).pk).status_code == 404


def test_the_api_refuses_an_unknown_field(api, moderator):
    response = api(_pending(moderator).pk, {"why": "x"})
    assert response.status_code == 400
