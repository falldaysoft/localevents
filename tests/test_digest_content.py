"""What one reader's weekly email says (localevents.14.2).

What matters here: the seven days are the front page's seven days — a weekly
series once, Featured above, Quiet folded — and the reader's starred events
that are on this weekend lead the email under their own heading, from their
account if they have one and their device's cookie if not. Everything is
pinned to a Wednesday so "this weekend" means the same thing on any day the
suite runs.
"""

from datetime import datetime, timedelta

import pytest
from django.core.management import call_command
from django.utils import timezone

from core.models import SiteConfig
from digest import content
from digest.models import Subscriber
from events.models import Event, Interest, Occurrence, Venue


@pytest.fixture
def now():
    # A Wednesday mid-morning: Saturday and Sunday are days 3 and 4.
    return datetime(2026, 11, 18, 10, 0, tzinfo=timezone.get_current_timezone())


@pytest.fixture
def hall(db):
    return Venue.objects.create(name="Community Hall", city="Anytown")


@pytest.fixture
def make(hall, now):
    def make(title, *days, prominence=Event.Prominence.LISTED, status=None, **fields):
        event = Event.objects.create(
            title=title, venue=hall, status=status or Event.Status.PUBLISHED,
            prominence=prominence, **fields,
        )
        for day in days:
            Occurrence.objects.create(
                event=event, start=now.replace(hour=19) + timedelta(days=day)
            )
        return event

    return make


@pytest.fixture
def reader(db):
    return Subscriber.objects.create(
        email="reader@example.com", source="form", fingerprint="device-1",
        confirmed_at=timezone.now(),
    )


def star(event, fingerprint="device-1", user=None):
    if user is not None:
        return Interest.objects.create(event=event, user=user)
    return Interest.objects.create(event=event, fingerprint=fingerprint)


# --- the seven days --------------------------------------------------------


@pytest.mark.django_db
def test_the_week_is_the_front_pages_week(make, reader, now):
    make("Lantern Walk", 1)
    make("Big Fair", 2, prominence=Event.Prominence.FEATURED)
    make("Book Club", 1, prominence=Event.Prominence.QUIET)
    make("Yoga", 1, 2, 3, 4, 5, listing_type=Event.ListingType.SERIES)
    make("Drop-in Chess", 2, prominence=Event.Prominence.BACKGROUND)
    make("Too Far Off", 9)

    digest = content.render_for(reader, now=now)

    assert "FEATURED" in digest.text
    assert digest.text.index("Big Fair") < digest.text.index("Lantern Walk")
    assert digest.text.count("Yoga") == 1
    assert "Also: Book Club" in digest.text
    assert "EVERY WEEK" in digest.text and "Drop-in Chess" in digest.text
    assert "Too Far Off" not in digest.text
    assert digest.event_count == 5
    assert "YOUR WEEKEND" not in digest.text
    assert digest.favorites_count == 0
    assert digest.subject.startswith("What's on this week: Big Fair")


@pytest.mark.django_db
def test_unpublished_listings_never_appear(make, reader, now):
    make("Pending Thing", 1, status=Event.Status.PENDING)
    make("Taken Down", 1, status=Event.Status.UNPUBLISHED)

    digest = content.render_for(reader, now=now)

    assert digest.event_count == 0
    assert "Nothing is listed for the coming week yet" in digest.text
    assert digest.subject == "What's on this week — " + content.settings.SITE_NAME


@pytest.mark.django_db
def test_a_long_week_is_cut_and_counted(make, reader, now, monkeypatch):
    monkeypatch.setattr(content, "MAX_CARDS", 2)
    for day, title in enumerate(["One", "Two", "Three", "Four"], start=1):
        make(title, day)

    digest = content.render_for(reader, now=now)

    assert "One" in digest.text and "Two" in digest.text
    assert "Three" not in digest.text
    assert "and 2 more this week" in digest.text
    assert "?when=week" in digest.text


# --- the reader's weekend --------------------------------------------------


@pytest.mark.django_db
def test_starred_events_this_weekend_lead_the_email(make, reader, now):
    make("Lantern Walk", 1)
    fair = make("Craft Fair", 3)
    star(fair)

    digest = content.render_for(reader, now=now)

    assert digest.favorites_count == 1
    assert digest.text.index("YOUR WEEKEND") < digest.text.index("Lantern Walk")
    assert digest.text.index("Craft Fair") < digest.text.index("Lantern Walk")
    # Starred again where it appears under its day.
    assert "★ Craft Fair" in digest.text
    assert "Your weekend" in digest.html
    assert digest.subject.startswith("This weekend: Craft Fair")


@pytest.mark.django_db
def test_a_starred_event_later_in_the_week_is_starred_but_does_not_lead(
    make, reader, now
):
    star(make("Tuesday Talk", 6))

    digest = content.render_for(reader, now=now)

    assert digest.favorites_count == 0
    assert "YOUR WEEKEND" not in digest.text
    assert "★ Tuesday Talk" in digest.text


@pytest.mark.django_db
def test_any_tier_counts_as_a_favorite(make, reader, now):
    star(make("Library Talk", 4, prominence=Event.Prominence.QUIET))
    star(make("Saturday Class", 3, prominence=Event.Prominence.BACKGROUND))

    digest = content.render_for(reader, now=now)

    assert digest.favorites_count == 2
    assert "and 1 more of yours" in digest.subject


@pytest.mark.django_db
def test_another_devices_stars_are_not_this_readers(make, reader, now):
    star(make("Craft Fair", 3), fingerprint="someone-else")

    assert content.render_for(reader, now=now).favorites_count == 0


@pytest.mark.django_db
def test_an_account_reader_gets_the_accounts_stars(
    make, reader, now, django_user_model
):
    user = django_user_model.objects.create_user(
        username="r", email="reader@example.com", password="pw-12345678"
    )
    reader.user = user
    reader.save()
    star(make("Device Pick", 3))  # the old cookie's mark, not merged
    star(make("Account Pick", 4), user=user)

    digest = content.render_for(reader, now=now)

    assert digest.favorites_count == 1
    assert digest.subject.startswith("This weekend: Account Pick")


@pytest.mark.django_db
def test_a_starred_event_that_was_taken_down_is_not_a_favorite(make, reader, now):
    star(make("Gone", 3, status=Event.Status.UNPUBLISHED))

    assert content.render_for(reader, now=now).favorites_count == 0


# --- the parts every email carries -----------------------------------------


@pytest.mark.django_db
def test_the_footer_carries_unsubscribe_and_the_postal_address(make, reader, now):
    config = SiteConfig.load()
    config.postal_address = "12 Example Street\nSomewhere"
    config.save()

    digest = content.render_for(reader, now=now)

    url = f"/digest/unsubscribe/{reader.token}/"
    assert url in digest.text and url in digest.html
    assert "12 Example Street" in digest.text
    assert "12 Example Street<br>Somewhere" in digest.html


@pytest.mark.django_db
def test_text_is_not_html_escaped_and_html_is(make, reader, now):
    make("Fish & Chips <Night>", 1)

    digest = content.render_for(reader, now=now)

    assert "Fish & Chips <Night>" in digest.text
    assert "Fish &amp; Chips &lt;Night&gt;" in digest.html
    assert "\n\n\n" not in digest.text


@pytest.mark.django_db
def test_a_base_built_once_serves_many_readers(make, reader, now):
    star(make("Craft Fair", 3))
    other = Subscriber.objects.create(email="b@example.com", source="form")
    base = content.base_listing(content.default_filter(), now)

    mine = content.render_for(reader, now=now, base=base)
    theirs = content.render_for(other, now=now, base=base)

    assert mine.favorites_count == 1 and theirs.favorites_count == 0
    assert "★" not in theirs.text


@pytest.mark.django_db
def test_preview_prints_without_saving(make, now, capsys):
    make("Lantern Walk", 1)

    call_command("preview_digest", "nobody@example.com")

    out = capsys.readouterr()
    assert "Subject:" in out.err
    assert not Subscriber.objects.exists()
