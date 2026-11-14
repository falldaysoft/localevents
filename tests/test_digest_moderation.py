"""The weekly email's master switch and moderator screens (localevents.14.4).

What matters here: switched off, the email can't be joined, sends nothing,
not even the rest of a run already under way, yet anyone already receiving
it can still leave; the send log's figures come from what each run actually
did; a moderator can add or remove someone who asked outside the site, and
each such change carries a note and lands in the moderation log; and a
signup nobody confirmed is forgotten after 30 days.
"""

from datetime import datetime, timedelta

import pytest
from django.core import mail
from django.core.cache import cache
from django.urls import reverse
from django.utils import timezone

from core.models import SiteConfig
from digest import sending, services, stats
from digest.models import DigestDelivery, DigestRun, Subscriber
from events.models import Event, Occurrence, Venue
from submissions.models import ModerationAction


@pytest.fixture(autouse=True)
def fresh_cache():
    cache.clear()
    yield
    cache.clear()


def switch(on):
    config = SiteConfig.load()
    config.digest_enabled = on
    config.digest_weekday = 3
    config.digest_hour = 7
    config.digest_min_events = 1
    config.save()


THURSDAY_8AM = datetime(2026, 11, 19, 8, tzinfo=timezone.get_current_timezone())


@pytest.fixture
def week(db):
    hall = Venue.objects.create(name="Community Hall", city="Anytown")
    event = Event.objects.create(
        title="Lantern Walk", venue=hall, status=Event.Status.PUBLISHED,
        prominence=Event.Prominence.LISTED,
    )
    Occurrence.objects.create(event=event, start=timezone.now() + timedelta(days=1))


def subscriber(email, **fields):
    fields.setdefault("confirmed_at", timezone.now())
    return Subscriber.objects.create(email=email, source="form", **fields)


# --- the master switch -----------------------------------------------------


@pytest.mark.django_db
def test_off_hides_the_way_in(client):
    switch(False)

    home = client.get(reverse("index"))
    assert reverse("digest_signup").encode() not in home.content

    page = client.get(reverse("digest_signup"))
    assert page.status_code == 200
    assert b"isn't being sent" in page.content
    assert b'name="email"' not in page.content

    client.post(reverse("digest_signup"), {"email": "a@example.com"})
    assert not Subscriber.objects.exists()
    assert mail.outbox == []


@pytest.mark.django_db
def test_on_shows_it(client):
    switch(True)
    assert reverse("digest_signup").encode() in client.get(reverse("index")).content


@pytest.mark.django_db
def test_off_still_lets_a_reader_leave(client):
    switch(False)
    reader = subscriber("a@example.com")

    client.post(reverse("digest_unsubscribe", args=[reader.token]))

    reader.refresh_from_db()
    assert reader.status == "unsubscribed"


@pytest.mark.django_db
def test_off_hides_the_profile_switch_unless_already_receiving(client, django_user_model):
    from allauth.account.models import EmailAddress

    switch(False)
    user = django_user_model.objects.create_user(
        username="r", email="r@example.com", password="pw-12345678"
    )
    EmailAddress.objects.create(user=user, email=user.email, primary=True, verified=True)
    client.force_login(user)

    assert b"Turn on" not in client.get(reverse("profile")).content
    client.post(reverse("digest_profile_toggle"), {"on": "1"})
    assert not Subscriber.objects.exists()

    subscriber("r@example.com", user=user)
    assert b"Turn off" in client.get(reverse("profile")).content


@pytest.mark.django_db
def test_switching_off_mid_run_skips_what_is_left(week, django_capture_on_commit_callbacks):
    switch(True)
    subscriber("a@example.com")
    subscriber("b@example.com")
    with django_capture_on_commit_callbacks(execute=False) as callbacks:
        run = sending.start_due_run(THURSDAY_8AM)
    switch(False)
    for callback in callbacks:
        callback()

    assert mail.outbox == []
    assert set(run.deliveries.values_list("status", flat=True)) == {
        DigestDelivery.Status.SKIPPED
    }
    assert "switched off" in run.deliveries.first().detail
    run.refresh_from_db()
    assert run.finished_at is not None


@pytest.mark.django_db
def test_a_moderator_flips_the_switch_and_it_is_logged(client, moderator):
    switch(False)
    client.force_login(moderator)

    client.post(reverse("mod_digest_switch"), {"on": "1"})
    assert SiteConfig.load().digest_enabled
    client.post(reverse("mod_digest_switch"), {"on": "0"})
    assert not SiteConfig.load().digest_enabled

    assert list(
        ModerationAction.objects.order_by("created_at").values_list("action", flat=True)
    ) == ["weekly email: switched on", "weekly email: switched off"]


@pytest.mark.django_db
def test_only_moderators_reach_the_screens(client, submitter):
    reader = subscriber("a@example.com")
    run = DigestRun.objects.create(send_date=THURSDAY_8AM.date())
    client.force_login(submitter)
    for url in [
        reverse("mod_digest_subscribers"),
        reverse("mod_digest_subscriber", args=[reader.pk]),
        reverse("mod_digest_run", args=[run.pk]),
    ]:
        assert client.get(url).status_code == 403
    assert client.post(reverse("mod_digest_switch"), {"on": "1"}).status_code == 403
    assert not SiteConfig.load().digest_enabled


# --- the send log ----------------------------------------------------------


@pytest.fixture
def last_week(db):
    """A finished run: two sent (one with picks), one skipped, one failed."""
    earlier = DigestRun.objects.create(send_date=THURSDAY_8AM.date() - timedelta(days=7))
    DigestRun.objects.filter(pk=earlier.pk).update(
        started_at=THURSDAY_8AM - timedelta(days=7)
    )
    run = DigestRun.objects.create(
        send_date=THURSDAY_8AM.date(), active_subscribers=4,
        finished_at=THURSDAY_8AM,
    )
    DigestRun.objects.filter(pk=run.pk).update(started_at=THURSDAY_8AM)
    S = DigestDelivery.Status
    rows = {}
    for email, status, events, picks in [
        ("a@example.com", S.SENT, 10, 2),
        ("b@example.com", S.SENT, 6, 0),
        ("c@example.com", S.SKIPPED, 0, 0),
        ("d@example.com", S.FAILED, 0, 0),
    ]:
        sub = subscriber(email)
        rows[email] = DigestDelivery.objects.create(
            run=run, subscriber=sub, email=email, status=status,
            event_count=events, favorites_count=picks,
            detail="SMTPException: boom" if status == S.FAILED else "",
        )
    # b left using this week's link; e joined during the week before.
    rows["b@example.com"].subscriber.deactivate(via=rows["b@example.com"])
    subscriber("e@example.com", confirmed_at=THURSDAY_8AM - timedelta(days=2))
    Subscriber.objects.exclude(email="e@example.com").update(
        confirmed_at=THURSDAY_8AM - timedelta(days=30)
    )
    return DigestRun.objects.get(pk=run.pk)


@pytest.mark.django_db
def test_the_log_counts_what_each_run_did(last_week):
    run = next(r for r in stats.run_log() if r.pk == last_week.pk)

    assert (run.total, run.sent, run.skipped, run.failed, run.waiting) == (4, 2, 1, 1, 0)
    assert run.personal == 1
    assert run.avg_events == 8
    assert run.unsubscribes == 1
    assert run.joined == 1


@pytest.mark.django_db
def test_the_log_and_drilldown_render(client, moderator, last_week):
    client.force_login(moderator)

    page = client.get(reverse("mod_digest"))
    assert page.status_code == 200
    assert reverse("mod_digest_run", args=[last_week.pk]).encode() in page.content

    detail = client.get(reverse("mod_digest_run", args=[last_week.pk]))
    assert b"d@example.com" in detail.content
    assert b"SMTPException: boom" in detail.content
    assert b"left from this email" in detail.content

    failed = client.get(reverse("mod_digest_run", args=[last_week.pk]) + "?status=failed")
    assert b"d@example.com" in failed.content
    assert b"a@example.com" not in failed.content


# --- subscribers -------------------------------------------------------------


@pytest.mark.django_db
def test_a_moderator_subscribes_someone_who_asked(client, moderator):
    client.force_login(moderator)

    response = client.post(
        reverse("mod_digest_subscribers"),
        {"email": "Neighbour@Example.com", "note": "asked at the library stall"},
    )

    reader = Subscriber.objects.get()
    assert response.url == reverse("mod_digest_subscriber", args=[reader.pk])
    assert reader.email == "neighbour@example.com"
    assert reader.is_active
    assert reader.source == Subscriber.Source.MODERATOR
    assert "Added by mod: asked at the library stall" in reader.consent_note
    assert mail.outbox == []  # no confirmation: the moderator vouched
    action = ModerationAction.objects.get()
    assert action.actor == moderator
    assert action.action == "weekly email: subscribed"
    assert "neighbour@example.com" in action.detail


@pytest.mark.django_db
def test_a_note_is_required(client, moderator):
    client.force_login(moderator)

    response = client.post(reverse("mod_digest_subscribers"), {"email": "a@example.com"})

    assert response.status_code == 400
    assert not Subscriber.objects.exists()


@pytest.mark.django_db
def test_subscribing_an_address_already_receiving_changes_nothing(moderator):
    reader = subscriber("a@example.com", consent_note="original")

    _, changed = services.moderator_subscribe(moderator, "a@example.com", "again")

    reader.refresh_from_db()
    assert not changed
    assert reader.source == "form" and reader.consent_note == "original"
    assert not ModerationAction.objects.exists()


@pytest.mark.django_db
def test_a_moderator_added_address_finds_its_account(moderator, django_user_model):
    from allauth.account.models import EmailAddress

    user = django_user_model.objects.create_user(
        username="r", email="r@example.com", password="pw-12345678"
    )
    EmailAddress.objects.create(user=user, email=user.email, primary=True, verified=True)

    reader, _ = services.moderator_subscribe(moderator, "r@example.com", "by phone")

    assert reader.user == user


@pytest.mark.django_db
def test_a_moderator_unsubscribes_and_resubscribes(client, moderator):
    reader = subscriber("a@example.com")
    client.force_login(moderator)
    url = reverse("mod_digest_subscriber", args=[reader.pk])

    assert client.post(url, {"action": "unsubscribe"}).status_code == 400
    reader.refresh_from_db()
    assert reader.is_active

    client.post(url, {"action": "unsubscribe", "note": "phoned to stop"})
    reader.refresh_from_db()
    assert reader.status == "unsubscribed"
    assert "Removed by mod: phoned to stop" in reader.consent_note

    client.post(url, {"action": "subscribe", "note": "changed their mind"})
    reader.refresh_from_db()
    assert reader.is_active
    assert list(
        ModerationAction.objects.order_by("created_at").values_list("action", flat=True)
    ) == ["weekly email: unsubscribed", "weekly email: subscribed"]


@pytest.mark.django_db
def test_the_subscriber_list_filters_and_searches(client, moderator):
    subscriber("alice@example.com")
    subscriber("bob@example.com")
    subscriber("pending@example.com", confirmed_at=None)
    client.force_login(moderator)

    active = client.get(reverse("mod_digest_subscribers")).content
    assert b"alice@example.com" in active and b"pending@example.com" not in active

    found = client.get(reverse("mod_digest_subscribers") + "?status=all&q=bob").content
    assert b"bob@example.com" in found and b"alice@example.com" not in found

    pending = client.get(reverse("mod_digest_subscribers") + "?status=pending").content
    assert b"pending@example.com" in pending


@pytest.mark.django_db
def test_the_subscriber_page_shows_the_record(client, moderator, last_week):
    reader = Subscriber.objects.get(email="a@example.com")
    client.force_login(moderator)

    page = client.get(reverse("mod_digest_subscriber", args=[reader.pk]))

    assert page.status_code == 200
    assert reverse("mod_digest_run", args=[last_week.pk]).encode() in page.content
    assert b"2 of theirs" in page.content


# --- forgetting unconfirmed signups -----------------------------------------


@pytest.mark.django_db
def test_unconfirmed_signups_are_forgotten_after_30_days():
    old = timezone.now() - timedelta(days=31)
    stale = Subscriber.objects.create(email="stale@example.com", source="form")
    asked_again = Subscriber.objects.create(
        email="again@example.com", source="form", confirmation_sent_at=timezone.now()
    )
    left = Subscriber.objects.create(
        email="left@example.com", source="form",
        confirmed_at=old, unsubscribed_at=old,
    )
    fresh = Subscriber.objects.create(email="fresh@example.com", source="form")
    Subscriber.objects.exclude(pk=fresh.pk).update(created_at=old)

    from core.management.commands.run_housekeeping import Command

    Command().forget_unconfirmed_signups()

    assert set(Subscriber.objects.values_list("email", flat=True)) == {
        asked_again.email, left.email, fresh.email,
    }
    assert not Subscriber.objects.filter(pk=stale.pk).exists()
