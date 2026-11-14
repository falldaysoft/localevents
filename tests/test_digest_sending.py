"""Sending the weekly email (localevents.14.3).

What matters here: it goes out by itself on the configured local day and hour
and never twice in one week; one reader's failure does not stop the rest;
every email carries a one-click unsubscribe that says which week it came
from; and the moderator's test send reaches only the moderator and is
counted nowhere.
"""

import email.policy
from datetime import datetime, timedelta

import pytest
from django.core import mail
from django.core.cache import cache
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from core.models import SiteConfig
from digest import content, sending
from digest.models import DigestDelivery, DigestRun, Subscriber
from events.models import Event, Occurrence, Venue


@pytest.fixture(autouse=True)
def fresh_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def config(db):
    config = SiteConfig.load()
    config.digest_enabled = True
    config.digest_weekday = 3  # Thursday
    config.digest_hour = 7
    config.digest_min_events = 1
    config.save()
    return config


def at(day, hour):
    """A local time in the week of Monday 16 November 2026."""
    return datetime(2026, 11, 16 + day, hour, tzinfo=timezone.get_current_timezone())


THURSDAY_8AM = at(3, 8)


@pytest.fixture
def week(db):
    hall = Venue.objects.create(name="Community Hall", city="Anytown")
    soon = timezone.localtime().replace(hour=19, minute=0) + timedelta(days=1)
    for i, title in enumerate(["Lantern Walk", "Craft Fair", "Choir Concert"]):
        event = Event.objects.create(
            title=title, venue=hall, status=Event.Status.PUBLISHED,
            prominence=Event.Prominence.LISTED,
        )
        Occurrence.objects.create(event=event, start=soon + timedelta(days=i))


def subscriber(email, **fields):
    fields.setdefault("confirmed_at", timezone.now())
    return Subscriber.objects.create(email=email, source="form", **fields)


@pytest.fixture
def start(django_capture_on_commit_callbacks):
    def start(now=THURSDAY_8AM):
        with django_capture_on_commit_callbacks(execute=True):
            return sending.start_due_run(now)

    return start


# --- when --------------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize(
    "now, enabled, due",
    [
        (THURSDAY_8AM, True, True),
        (at(3, 7), True, True),
        (at(3, 23), True, True),
        (at(3, 6), True, False),
        (at(2, 8), True, False),
        (THURSDAY_8AM, False, False),
    ],
)
def test_due_on_the_configured_local_day_from_the_hour(config, now, enabled, due):
    config.digest_enabled = enabled
    assert sending.is_due(now, config) is due


@pytest.mark.django_db
def test_nothing_happens_when_it_is_not_due(config, start, week):
    subscriber("a@example.com")
    assert start(at(2, 8)) is None
    assert not DigestRun.objects.exists()
    assert mail.outbox == []


# --- the run -----------------------------------------------------------------


@pytest.mark.django_db
def test_a_run_mails_every_active_subscriber_once(config, start, week):
    subscriber("a@example.com")
    subscriber("b@example.com")
    subscriber("pending@example.com", confirmed_at=None)
    subscriber("gone@example.com", unsubscribed_at=timezone.now())

    run = start()

    assert sorted(m.to[0] for m in mail.outbox) == ["a@example.com", "b@example.com"]
    run.refresh_from_db()
    assert run.finished_at is not None
    assert run.active_subscribers == 2
    deliveries = run.deliveries.all()
    assert {d.status for d in deliveries} == {DigestDelivery.Status.SENT}
    assert all(d.event_count == 3 and d.subject for d in deliveries)
    assert Subscriber.objects.get(email="a@example.com").last_sent_at is not None


@pytest.mark.django_db
def test_a_second_call_the_same_day_sends_nothing(config, start, week):
    subscriber("a@example.com")
    start()
    start(at(3, 9))
    start(at(3, 22))

    assert len(mail.outbox) == 1
    assert DigestRun.objects.count() == 1


@pytest.mark.django_db
def test_a_batch_run_twice_sends_each_email_once(config, start, week):
    subscriber("a@example.com")
    run = start()
    ids = list(run.deliveries.values_list("pk", flat=True))

    sending.send_batch(run.pk, ids)

    assert len(mail.outbox) == 1


@pytest.mark.django_db
def test_a_reader_who_left_after_the_run_began_is_skipped(
    config, week, django_capture_on_commit_callbacks
):
    reader = subscriber("a@example.com")
    with django_capture_on_commit_callbacks(execute=False) as callbacks:
        run = sending.start_due_run(THURSDAY_8AM)
    reader.deactivate()
    for callback in callbacks:
        callback()

    assert mail.outbox == []
    delivery = run.deliveries.get()
    assert delivery.status == DigestDelivery.Status.SKIPPED
    assert "Unsubscribed" in delivery.detail


@pytest.mark.django_db
def test_a_quiet_week_is_skipped_per_reader(config, start, week):
    config.digest_min_events = 5
    config.save()
    subscriber("a@example.com")

    run = start()

    assert mail.outbox == []
    delivery = run.deliveries.get()
    assert delivery.status == DigestDelivery.Status.SKIPPED
    assert delivery.event_count == 3
    assert "minimum is 5" in delivery.detail


@pytest.mark.django_db
def test_one_failure_is_recorded_and_the_rest_still_go(
    config, start, week, monkeypatch
):
    subscriber("a@example.com")
    subscriber("broken@example.com")
    real = content.render_for

    def render_for(sub, **kwargs):
        if sub.email == "broken@example.com":
            raise RuntimeError("template exploded")
        return real(sub, **kwargs)

    monkeypatch.setattr(content, "render_for", render_for)

    run = start()

    assert [m.to[0] for m in mail.outbox] == ["a@example.com"]
    failed = run.deliveries.get(email="broken@example.com")
    assert failed.status == DigestDelivery.Status.FAILED
    assert "template exploded" in failed.detail
    run.refresh_from_db()
    assert run.finished_at is not None


@pytest.mark.django_db
def test_a_stranded_run_is_picked_up_again(
    config, week, django_capture_on_commit_callbacks
):
    subscriber("a@example.com")
    # The batch is queued and lost with its worker.
    with django_capture_on_commit_callbacks(execute=False):
        run = sending.start_due_run(THURSDAY_8AM)
    assert mail.outbox == []

    # Picked up by the next hour's housekeeping — and by the next day's, when
    # the email is no longer due.
    DigestRun.objects.filter(pk=run.pk).update(
        started_at=THURSDAY_8AM - timedelta(hours=1)
    )
    with django_capture_on_commit_callbacks(execute=True):
        sending.start_due_run(at(4, 1))

    assert len(mail.outbox) == 1
    run.refresh_from_db()
    assert run.finished_at is not None


# --- the email as it leaves -------------------------------------------------


@pytest.mark.django_db
def test_every_email_carries_one_click_unsubscribe_naming_its_week(
    config, start, week
):
    reader = subscriber("a@example.com")
    run = start()
    delivery = run.deliveries.get()
    message = mail.outbox[0]

    expected = f"/digest/unsubscribe/{reader.token}/?d={delivery.pk}"
    assert message.extra_headers["List-Unsubscribe"].endswith(f"{expected}>")
    assert message.extra_headers["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert message.reply_to
    assert expected in message.body
    # Never RFC 2047-encoded, however long the URL: providers parse it raw.
    long_host = "https://" + "a-very-long-instance-name." * 3 + "example.org"
    raw = sending.build_message(
        reader, content.render_for(reader), f"{long_host}{expected}"
    ).message(policy=email.policy.SMTP).as_bytes()
    assert f"List-Unsubscribe: <{long_host}{expected}>\r\n".encode() in raw
    html, mimetype = message.alternatives[0]
    assert mimetype == "text/html" and expected.replace("&", "&amp;") in html


@pytest.mark.django_db
def test_one_click_unsubscribe_records_which_email_it_came_from(config, start, week):
    reader = subscriber("a@example.com")
    delivery = start().deliveries.get()

    Client(enforce_csrf_checks=True).post(
        reverse("digest_unsubscribe", args=[reader.token]) + f"?d={delivery.pk}",
        {"List-Unsubscribe": "One-Click"},
    )

    reader.refresh_from_db()
    assert reader.status == "unsubscribed"
    assert reader.unsubscribed_via == delivery


@pytest.mark.django_db
def test_a_delivery_id_that_is_not_theirs_is_ignored(config, start, week):
    a = subscriber("a@example.com")
    b = subscriber("b@example.com")
    b_delivery = start().deliveries.get(email="b@example.com")

    Client().post(reverse("digest_unsubscribe", args=[a.token]) + f"?d={b_delivery.pk}")

    a.refresh_from_db()
    assert a.status == "unsubscribed"
    assert a.unsubscribed_via is None


@pytest.mark.django_db
def test_housekeeping_starts_the_run(config, week, monkeypatch):
    from core.management.commands.run_housekeeping import Command

    subscriber("a@example.com")
    monkeypatch.setattr(timezone, "now", lambda: THURSDAY_8AM)

    Command().send_weekly_email()

    assert DigestRun.objects.filter(send_date=THURSDAY_8AM.date()).exists()


# --- the moderator's test send ----------------------------------------------


@pytest.mark.django_db
def test_a_moderator_can_send_themselves_a_test(client, moderator, week):
    client.force_login(moderator)

    response = client.post(reverse("mod_digest_test"))

    assert response.status_code == 302
    assert len(mail.outbox) == 1
    message = mail.outbox[0]
    assert message.to == ["mod@example.com"]
    assert message.subject.startswith("[Test] ")
    assert "Lantern Walk" in message.body
    # Counted nowhere, and no subscription made.
    assert not DigestRun.objects.exists()
    assert not DigestDelivery.objects.exists()
    assert not Subscriber.objects.exists()


@pytest.mark.django_db
def test_a_test_is_sent_even_when_the_email_is_off_and_the_week_is_empty(
    client, moderator
):
    client.force_login(moderator)
    client.post(reverse("mod_digest_test"))
    assert "Nothing is listed" in mail.outbox[0].body


@pytest.mark.django_db
def test_a_moderators_test_uses_their_own_subscription(client, moderator, week):
    mine = subscriber("mod@example.com")
    client.force_login(moderator)

    client.post(reverse("mod_digest_test"))

    assert f"/digest/unsubscribe/{mine.token}/" in mail.outbox[0].body


@pytest.mark.django_db
def test_only_moderators_can_send_a_test(client, submitter):
    assert client.post(reverse("mod_digest_test")).status_code == 302  # to login
    client.force_login(submitter)
    assert client.post(reverse("mod_digest_test")).status_code == 403
    assert client.get(reverse("mod_digest")).status_code == 403
    assert mail.outbox == []


@pytest.mark.django_db
def test_test_sends_are_capped(client, moderator, monkeypatch):
    from digest import mod_views

    monkeypatch.setattr(mod_views, "TESTS_PER_HOUR", 2)
    client.force_login(moderator)
    for _ in range(4):
        client.post(reverse("mod_digest_test"))
    assert len(mail.outbox) == 2


@pytest.mark.django_db
def test_the_moderator_page_shows_the_schedule(client, moderator, config):
    subscriber("a@example.com")
    client.force_login(moderator)

    response = client.get(reverse("mod_digest"))

    assert response.status_code == 200
    assert b"Thursday" in response.content
    assert b"Send me a test" in response.content
