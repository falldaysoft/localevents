"""Moderators are told when something lands in the queue.

Before this, a listing a stranger submitted sat unseen until a moderator
happened to open /moderate/ — the site mailed the submitter about every
decision and nobody about the thing to decide.
"""

from datetime import timedelta

import pytest
from django.core import mail
from django.utils import timezone

from accounts.models import ApiToken
from moderation import services as moderation_services
from submissions.models import Submission
from submissions.services import save_event_from_draft, withdraw


def _data(title="Spring Choir Concert"):
    return {
        "title": title,
        "venue_name": "Memorial Hall",
        "occurrences": [{"start": timezone.now() + timedelta(days=10)}],
    }


@pytest.fixture
def submit(django_capture_on_commit_callbacks):
    """Confirm a draft and let the mail it queues on commit actually go."""

    def _submit(user, data=None, **fields):
        submission = fields.pop("submission", None) or Submission.objects.create(
            submitted_by=user, source_url="https://example.org/e", **fields
        )
        with django_capture_on_commit_callbacks(execute=True):
            save_event_from_draft(submission, data or _data())
        return submission

    return _submit


@pytest.mark.django_db
def test_a_new_submission_emails_the_moderators(submit, moderator, submitter, settings):
    settings.SITE_BASE_URL = "https://events.example.org"

    submission = submit(submitter)

    [message] = mail.outbox
    assert message.to == ["mod@example.com"]
    assert "Spring Choir Concert" in message.subject
    assert "submitter@example.com" in message.body
    assert "Memorial Hall" in message.body
    assert f"https://events.example.org/moderate/{submission.pk}/" in message.body
    assert "{%" not in message.body and "{{" not in message.body


@pytest.mark.django_db
def test_superusers_are_moderators_too_and_each_gets_their_own_copy(
    submit, moderator, submitter, django_user_model
):
    django_user_model.objects.create_superuser(
        username="admin", email="admin@example.com", password="pw"
    )
    django_user_model.objects.create_user(
        username="reader", email="reader@example.com", password="pw"
    )

    submit(submitter)

    assert sorted(m.to for m in mail.outbox) == [
        ["admin@example.com"], ["mod@example.com"],
    ]


@pytest.mark.django_db
def test_a_moderator_is_not_told_about_their_own_submission(submit, moderator):
    submit(moderator)

    assert mail.outbox == []


@pytest.mark.django_db
def test_an_agents_submission_sends_no_mail(submit, moderator, django_user_model):
    owner = django_user_model.objects.create_superuser(
        username="admin", email="admin@example.com", password="pw"
    )
    token, _ = ApiToken.issue(owner, "scout")

    submit(owner, api_token=token)

    assert mail.outbox == []


@pytest.mark.django_db
def test_an_answer_to_a_moderators_question_is_announced_as_a_resubmission(
    submit, moderator, submitter, django_capture_on_commit_callbacks
):
    submission = submit(submitter)
    with django_capture_on_commit_callbacks(execute=True):
        moderation_services.request_info(submission, moderator, body="What time?")
    mail.outbox.clear()

    submit(submitter, submission=submission)

    [message] = mail.outbox
    assert message.to == ["mod@example.com"]
    assert "Sent back for review" in message.subject
    assert "back for review" in message.body


@pytest.mark.django_db
def test_nothing_is_sent_for_a_submission_withdrawn_before_the_worker_ran(
    moderator, submitter
):
    from moderation.tasks import email_moderators

    submission = Submission.objects.create(submitted_by=submitter)
    save_event_from_draft(submission, _data())
    withdraw(submission, submitter)

    email_moderators.enqueue(submission.pk, "new")

    assert mail.outbox == []
