"""The JSON path for agents.

What matters: a token is the only way in (a session cookie is not, because
every view is csrf_exempt), only a moderator's token works, and a listing sent
this way meets exactly the rules the confirmation form applies before landing
in the queue marked with the key that sent it.
"""

import json
from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from accounts.models import ApiToken
from events.models import Category, Event, Occurrence, Venue
from submissions.models import ModerationAction, Submission


@pytest.fixture
def issued(moderator):
    return ApiToken.issue(moderator, "Calendar scout")


@pytest.fixture
def api(client, issued):
    _, key = issued

    def call(method, name, body=None, args=None, key=key):
        headers = {"HTTP_AUTHORIZATION": f"Bearer {key}"} if key else {}
        url = reverse(name, args=args)
        if method == "get":
            return client.get(url, **headers)
        return client.post(
            url, data=json.dumps(body or {}), content_type="application/json",
            **headers,
        )

    return call


@pytest.fixture
def category(db):
    return Category.objects.create(name="Test Readings", slug="test-readings")


def _listing(**overrides):
    start = timezone.localtime() + timedelta(days=10)
    listing = {
        "title": "Poetry night at the hall",
        "summary": "Open readings.",
        "venue_name": "Parish Hall",
        "venue_address": "1 Church St",
        "venue_city": "Sampletown",
        "is_free": True,
        "source_url": "https://example.org/poetry",
        "occurrences": [
            {
                "start": start.strftime("%Y-%m-%dT19:00"),
                "end": start.strftime("%Y-%m-%dT21:00"),
            }
        ],
    }
    listing.update(overrides)
    return listing


def test_no_token_is_401(api, db):
    response = api("post", "api_submissions", _listing(), key=None)
    assert response.status_code == 401
    assert "Bearer" in response.json()["error"]


def test_a_session_cookie_is_not_enough(client, moderator):
    """The views are csrf_exempt, so a cookie alone would let any page post."""
    client.force_login(moderator)
    response = client.post(
        reverse("api_submissions"), data=json.dumps(_listing()),
        content_type="application/json",
    )
    assert response.status_code == 401
    assert Submission.objects.count() == 0


def test_a_revoked_token_is_401(api, issued):
    token, _ = issued
    token.revoked_at = timezone.now()
    token.save()
    assert api("get", "api_categories").status_code == 401


def test_a_token_stops_working_when_its_owner_stops_moderating(api, moderator):
    moderator.groups.clear()
    response = api("get", "api_categories")
    assert response.status_code == 403


def test_wrong_method_is_json(api):
    response = api("post", "api_categories")
    assert response.status_code == 405
    assert "error" in response.json()


def test_only_a_hash_is_stored(issued):
    token, key = issued
    assert key not in token.key_hash
    assert ApiToken.authenticate(key) == token
    assert ApiToken.authenticate(key + "x") is None


def test_submitting_creates_a_pending_listing_marked_with_its_token(
    api, issued, moderator, category
):
    token, _ = issued
    response = api(
        "post", "api_submissions",
        _listing(categories=["test-readings"], source_text="Poetry night, 7pm.",
                 note="Found on the parish calendar."),
    )

    assert response.status_code == 201, response.json()
    submission = Submission.objects.get()
    assert submission.status == Submission.Status.PENDING_REVIEW
    assert submission.api_token == token
    assert submission.submitted_by == moderator
    assert submission.pasted_text == "Poetry night, 7pm."
    assert submission.messages.get().body == "Found on the parish calendar."

    event = submission.event
    assert event.status == Event.Status.PENDING
    assert event.is_free
    assert event.venue.name == "Parish Hall"
    assert list(event.categories.all()) == [category]
    occurrence = event.occurrences.get()
    assert timezone.localtime(occurrence.start).hour == 19
    assert timezone.localtime(occurrence.end).hour == 21
    assert ModerationAction.objects.filter(
        submission=submission, action="submitted_for_review"
    ).exists()

    body = response.json()["submission"]
    assert body["status"] == "pending_review"
    assert body["event"]["url"] is None  # not published yet


def test_an_offset_is_honoured(api, db):
    start = (timezone.now() + timedelta(days=5)).replace(microsecond=0)
    response = api(
        "post", "api_submissions",
        _listing(occurrences=[{"start": start.isoformat()}]),
    )
    assert response.status_code == 201, response.json()
    assert Occurrence.objects.get().start == start


def test_all_past_dates_are_refused(api, db):
    response = api(
        "post", "api_submissions",
        _listing(occurrences=[{"start": "2020-05-01T19:00"}]),
    )
    assert response.status_code == 400
    assert "right year" in " ".join(response.json()["errors"]["occurrences"])
    assert Submission.objects.count() == 0


def test_end_before_start_is_reported_by_row(api, db):
    start = timezone.localtime() + timedelta(days=3)
    response = api(
        "post", "api_submissions",
        _listing(occurrences=[{
            "start": start.strftime("%Y-%m-%dT19:00"),
            "end": start.strftime("%Y-%m-%dT18:00"),
        }]),
    )
    assert response.status_code == 400
    assert "end" in response.json()["errors"]["occurrences_rows"]["0"]


def test_an_unknown_category_is_refused(api, db):
    response = api("post", "api_submissions", _listing(categories=["nope"]))
    assert response.status_code == 400
    assert "nope" in response.json()["errors"]["categories"][0]


def test_an_unknown_field_is_refused_by_name(api, db):
    response = api("post", "api_submissions", _listing(venue_adress="typo"))
    assert response.status_code == 400
    assert "venue_adress" in response.json()["error"]


def test_a_missing_title_is_refused(api, db):
    listing = _listing()
    del listing["title"]
    response = api("post", "api_submissions", listing)
    assert response.status_code == 400
    assert "title" in response.json()["errors"]


def test_dry_run_writes_nothing(api, db):
    response = api("post", "api_submissions", _listing(dry_run=True))
    assert response.status_code == 200
    assert response.json() == {"valid": True, "possible_duplicates": []}
    assert Submission.objects.count() == 0
    assert Event.objects.count() == 0
    assert Venue.objects.count() == 0


def test_duplicates_come_back(api, db):
    start = timezone.localtime() + timedelta(days=10)
    existing = Event.objects.create(
        title="Poetry Night at the Hall", status=Event.Status.PUBLISHED
    )
    Occurrence.objects.create(event=existing, start=start.replace(hour=19, minute=0))

    response = api("post", "api_submissions", _listing(dry_run=True))
    [match] = response.json()["possible_duplicates"]
    assert match["title"] == "Poetry Night at the Hall"
    assert match["url"].endswith(f"/events/{existing.slug}/")


def test_a_dry_run_resubmit_is_not_its_own_duplicate(api, db):
    created = api("post", "api_submissions", _listing()).json()["submission"]
    Submission.objects.filter(pk=created["id"]).update(
        status=Submission.Status.INFO_REQUESTED
    )
    response = api(
        "post", "api_submission", _listing(dry_run=True), args=[created["id"]]
    )
    assert response.json()["possible_duplicates"] == []


def test_someone_elses_submission_is_404(api, submitter):
    theirs = Submission.objects.create(submitted_by=submitter)
    assert api("get", "api_submission", args=[theirs.pk]).status_code == 404


def test_list_and_read_back(api, db):
    created = api("post", "api_submissions", _listing()).json()["submission"]
    listed = api("get", "api_submissions").json()["submissions"]
    assert [s["id"] for s in listed] == [created["id"]]
    read = api("get", "api_submission", args=[created["id"]]).json()
    assert read["submission"]["title"] == "Poetry night at the hall"


def test_resubmitting_needs_an_open_question(api, db):
    created = api("post", "api_submissions", _listing()).json()["submission"]
    response = api("post", "api_submission", _listing(), args=[created["id"]])
    assert response.status_code == 409


def test_answering_a_question_updates_the_same_event(api, db):
    created = api("post", "api_submissions", _listing()).json()["submission"]
    submission = Submission.objects.get(pk=created["id"])
    submission.status = Submission.Status.INFO_REQUESTED
    submission.save()

    response = api(
        "post", "api_submission",
        _listing(title="Poetry night (all ages)", note="Yes, all ages."),
        args=[submission.pk],
    )

    assert response.status_code == 200, response.json()
    assert Event.objects.count() == 1
    submission.refresh_from_db()
    assert submission.status == Submission.Status.PENDING_REVIEW
    assert submission.event.title == "Poetry night (all ages)"


def test_categories_lists_active_slugs(api, category):
    Category.objects.create(name="Retired", slug="retired", is_active=False)
    slugs = [c["slug"] for c in api("get", "api_categories").json()["categories"]]
    assert "test-readings" in slugs
    assert "retired" not in slugs


def test_index_describes_itself(api):
    body = api("get", "api_index").json()
    assert body["submit"].endswith("/api/submissions/")
    assert "occurrences" in body["fields"]


# Tokens on the profile page -------------------------------------------------


def test_a_moderator_makes_a_token_and_sees_it_once(client, moderator):
    client.force_login(moderator)
    response = client.post(reverse("api_token_create"), {"name": "Scout"})
    assert response.status_code == 302

    token = ApiToken.objects.get()
    page = client.get(reverse("profile")).content.decode()
    assert "Scout" in page
    key = page.split(token.hint, 1)[1].split("<", 1)[0]
    assert ApiToken.authenticate(token.hint + key) == token

    again = client.get(reverse("profile")).content.decode()
    assert token.hint + key not in again


def test_an_ordinary_user_cannot_make_a_token(client, submitter):
    client.force_login(submitter)
    response = client.post(reverse("api_token_create"), {"name": "Scout"})
    assert response.status_code == 403
    assert ApiToken.objects.count() == 0
    assert "API tokens" not in client.get(reverse("profile")).content.decode()


def test_an_ordinary_users_old_token_does_not_work(client, submitter):
    _, key = ApiToken.issue(submitter, "Old")
    response = client.get(
        reverse("api_categories"), HTTP_AUTHORIZATION=f"Bearer {key}"
    )
    assert response.status_code == 403


def test_revoking_from_the_profile(client, moderator, issued):
    token, key = issued
    client.force_login(moderator)
    client.post(reverse("api_token_revoke", args=[token.pk]))
    assert ApiToken.authenticate(key) is None


def test_the_queue_marks_an_agents_submission(client, moderator, api):
    api("post", "api_submissions", _listing())
    client.force_login(moderator)
    assert "Sent by an agent" in client.get(reverse("mod_queue")).content.decode()
    submission = Submission.objects.get()
    page = client.get(reverse("mod_submission", args=[submission.pk])).content.decode()
    assert "Calendar scout" in page
