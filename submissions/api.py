"""The submitter's path for agents: JSON in, a pending listing out.

An agent scouting a council calendar used to drive `/submit/` through a
browser, typing into form fields by their position on the page — slow, and
broken by any change to the template. This takes the same fields as JSON and
sends them through the same `EventDraftForm`, `OccurrenceFormSet` and
`save_event_from_draft` the confirmation page uses, so every rule a person
filling the form meets (the all-past-dates guard, end after start, no date
listed twice, the daily cap) applies here unchanged.

A submission here skips enrichment: the agent has already read the page, and
paying a model to read it again would buy nothing. It follows that nobody
confirmed these details on the site the way a submitter confirms an
extraction, so the moderator is told which key sent each one
(`Submission.api_token`) and the agent may attach the text it read
(`source_text`) for them to check against. An agent with only a link uses
`extract` instead, which is the web form's enrichment step and leaves the
agent to confirm the result.

Authentication is a bearer token read by `token_required` below, and only
there. Only moderators may hold one — see accounts.views.create_api_token. Every view is csrf_exempt, so a browser session must never be enough —
otherwise any page on the internet could post listings as whoever happened to
be signed in.
"""

import json
from functools import wraps

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.db import transaction
from django.http import JsonResponse
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from accounts.models import ApiToken
from events.duplicates import find_duplicates
from events.models import Category, Event

from .forms import MAX_OCCURRENCE_ROWS, EventDraftForm, OccurrenceFormSet
from .models import Submission, SubmissionMessage, SubmissionQuota
from .services import save_event_from_draft
from .sources import advice_for
from .tasks import enrich_submission

# The fields an agent may send, beyond `occurrences` and `categories`, which
# need translating. Anything else is refused by name: an agent that misspells
# `venue_adress` should hear about it, not publish a listing with no address.
TEXT_FIELDS = (
    "title", "summary", "description", "venue_name", "venue_address",
    "venue_city", "organizer_name", "price_note", "accessibility_notes",
    "source_url", "ticket_url",
)
BOOLEAN_FIELDS = ("is_series", "is_free", "is_family_friendly")
OTHER_FIELDS = ("occurrences", "categories", "source_text", "note", "dry_run")
ACCEPTED_FIELDS = set(TEXT_FIELDS + BOOLEAN_FIELDS + OTHER_FIELDS)

# Matches the form's cap on pasted text, for the same reason.
MAX_SOURCE_TEXT = 10_000


def _error(status, message, **extra):
    return JsonResponse({"error": message, **extra}, status=status)


def token_required(*methods):
    """Authenticate by bearer token and refuse other methods, as JSON.

    Django's own decorators answer with a login redirect or an HTML 405, and
    an agent reading either as JSON gets a parse error that says nothing about
    what went wrong.
    """

    def decorator(view):
        @csrf_exempt
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            if request.method not in methods:
                response = _error(405, f"Use {' or '.join(methods)}.")
                response["Allow"] = ", ".join(methods)
                return response

            token, problem = _authenticate(request)
            if problem:
                return problem

            ApiToken.objects.filter(pk=token.pk).update(last_used_at=timezone.now())
            request.user = token.user
            request.api_token = token
            return view(request, *args, **kwargs)

        return wrapped

    return decorator


def _authenticate(request):
    """The request's token, or the JSON error saying why there is none."""
    scheme, _, key = request.headers.get("Authorization", "").partition(" ")
    token = ApiToken.authenticate(key.strip()) if scheme.lower() == "bearer" else None
    if token is None:
        return None, _error(
            401,
            "Send an API token as 'Authorization: Bearer <token>'. "
            "Tokens are made on your profile page. "
            f"GET {_absolute(reverse('api_index'))} describes this API.",
        )
    # Checked per request, not only when the token was made: someone
    # removed from the moderators keeps their tokens in the database,
    # and those must stop working with the role.
    if not token.user.is_moderator:
        return None, _error(403, "API tokens only work for moderators.")
    return token, None


def _absolute(path):
    return f"{settings.SITE_BASE_URL.rstrip('/')}{path}"


@csrf_exempt
def index(request):
    """What this API takes, for an agent that has only been given the URL.

    The one view without a token, because a 401 is all an agent without one
    would otherwise learn, and nothing here is private: field names, the
    rules the form applies, and categories the public site already lists. A
    token, if sent, adds what is particular to its holder.
    """
    if request.method != "GET":
        response = _error(405, "Use GET.")
        response["Allow"] = "GET"
        return response

    submit = _absolute(reverse("api_submissions"))
    body = {
        "about": "Submit events to this site's moderation queue as JSON. "
        "A moderator approves each one before it is published. "
        f"This is {settings.SITE_BASE_URL.rstrip('/')}; a time without an "
        f"offset is its local time ({settings.TIME_ZONE}).",
        "authentication": {
            "header": "Authorization: Bearer <token>",
            "how_to_get_one": "Only moderators can hold a token. Sign in, open "
            "your profile, and create one under 'API tokens'. It is shown once. "
            "Every endpoint except this one needs it; a browser session does "
            "not count.",
        },
        "endpoints": [
            {"method": "GET", "url": _absolute(reverse("api_index")),
             "does": "This description. No token needed."},
            {"method": "GET", "url": _absolute(reverse("api_categories")),
             "does": "Active categories as {slug, name}."},
            {"method": "POST", "url": _absolute(reverse("api_extract")),
             "does": "Have the site read a page for you: {url} or "
             "{source_text}. 202 with a submission; see 'workflows'."},
            {"method": "GET", "url": submit,
             "does": "Your submissions, newest first."},
            {"method": "POST", "url": submit,
             "does": "Submit a listing, or check one with dry_run. 201 on success."},
            {"method": "GET", "url": submit + "<id>/",
             "does": "One of your submissions: status, decision, moderators' messages."},
            {"method": "POST", "url": submit + "<id>/",
             "does": "Send the whole listing for a submission waiting on you: "
             "after a page read ('awaiting_submitter') or a moderator's "
             "question ('info_requested', answer in 'note'). Otherwise 409."},
        ],
        "workflows": {
            "you_read_the_page": "POST the listing to the submit URL with "
            "dry_run: true, then again without it.",
            "the_site_reads_the_page": "POST {url} to the extract URL. Poll "
            "the returned submission's 'api' URL until status is "
            "'awaiting_submitter' (minutes, not seconds). Check "
            "extraction.listing against the page, fix what is wrong, and POST "
            "it to that same 'api' URL. Nothing reaches the moderators until "
            "you do. Pages that need a login or JavaScript usually read "
            "badly; send source_text with the event's words instead.",
        },
        "listing": {
            "title": "string, required",
            "occurrences": "required: list of {start, end?, note?}. ISO 8601; "
            f"a time without an offset is local ({settings.TIME_ZONE}). At "
            f"most {MAX_OCCURRENCE_ROWS}. A festival running Friday to Sunday "
            "is one occurrence whose end is on a later day, not three. A "
            "listing whose dates have all passed is refused (usually the "
            "wrong year), as is the same start twice.",
            "summary": "string, one line",
            "description": "string, plain text; line breaks are kept",
            "venue_name": "string",
            "venue_address": "string, street address",
            "venue_city": "string",
            "organizer_name": "string",
            "categories": "list of slugs, from 'categories' below",
            "is_series": "boolean: a weekly class, club or market",
            "is_free": "boolean",
            "price_note": "string",
            "is_family_friendly": "boolean",
            "accessibility_notes": "string",
            "source_url": "string, the page the event was read from",
            "ticket_url": "string",
            "source_text": f"string up to {MAX_SOURCE_TEXT} characters: the "
            "event's own words as you read them, for the moderator to check "
            "the listing against. Please send it.",
            "note": "string, a message to the moderators",
            "dry_run": "boolean: validate and look for duplicates, write nothing",
        },
        "rules": [
            "Unknown fields are refused by name, so check spelling.",
            "Before submitting, POST with dry_run: true. If "
            "possible_duplicates is not empty, skip the event or explain in "
            "'note' why it is different.",
            "Errors come back as JSON with 'error' and, for a listing that "
            "failed validation, 'errors' keyed by field. Per-date problems "
            "are under errors.occurrences_rows, keyed by position from \"0\".",
        ],
        "example": {
            "title": "Poetry night",
            "summary": "Open readings, all welcome.",
            "occurrences": [
                {"start": "2026-11-06T19:00", "end": "2026-11-06T21:00"},
                {"start": "2026-11-13T19:00", "end": "2026-11-13T21:00",
                 "note": "Guest reader"},
            ],
            "venue_name": "Parish Hall",
            "venue_address": "1 Church St",
            "venue_city": "Sampletown",
            "is_free": True,
            "source_url": "https://example.org/events/poetry-night",
            "source_text": "The event's own words, as you read them.",
        },
        "statuses": {
            "new": "queued for the site to read",
            "enriching": "the site is reading the page",
            "awaiting_submitter": "read; waiting for you to check and send it",
            "pending_review": "waiting for a moderator",
            "info_requested": "a moderator asked something; see 'messages' and resubmit",
            "approved": "published; event.url is its page",
            "rejected": "see decision_note",
            "withdrawn": "withdrawn by the submitter",
        },
        "categories": [
            {"slug": c.slug, "name": c.name}
            for c in Category.objects.filter(is_active=True)
        ],
    }

    # Read but never required. A bad token is ignored rather than refused:
    # this page answers everyone, and the next request will report it.
    token, _ = _authenticate(request)
    if token is not None:
        body["token"] = {
            "name": token.name,
            "submissions_left_today": _submissions_left(token.user),
        }
    return JsonResponse(body)


@token_required("GET")
def categories(request):
    return JsonResponse(
        {
            "categories": [
                {"slug": c.slug, "name": c.name}
                for c in Category.objects.filter(is_active=True)
            ]
        }
    )


@token_required("GET", "POST")
def submissions(request):
    if request.method == "GET":
        mine = Submission.objects.filter(submitted_by=request.user).select_related(
            "event"
        )[:100]
        return JsonResponse({"submissions": [_submission_json(s) for s in mine]})

    payload, problem = _payload(request)
    if problem:
        return problem

    cleaned, errors = _validate(payload)
    if errors:
        return _error(400, "The listing did not validate.", errors=errors)

    duplicates = _duplicates(cleaned)
    if payload.get("dry_run"):
        return JsonResponse({"valid": True, "possible_duplicates": duplicates})

    if not SubmissionQuota.for_user(request.user).may_submit():
        return _error(429, "You have reached today's submission limit.")

    with transaction.atomic():
        # Created already past enrichment: the draft is empty because nothing
        # was extracted, and the event itself is what the moderator reviews.
        submission = Submission.objects.create(
            submitted_by=request.user,
            api_token=request.api_token,
            source_url=cleaned.get("source_url", ""),
            pasted_text=payload.get("source_text", ""),
            source_advice=advice_for(cleaned.get("source_url", "")),
            status=Submission.Status.AWAITING_SUBMITTER,
        )
        _save(submission, cleaned, payload)

    return JsonResponse(
        {
            "submission": _submission_json(submission),
            "possible_duplicates": duplicates,
        },
        status=201,
    )


@token_required("POST")
def extract(request):
    """Have the site read a page, for an agent that has only the link.

    This is the web form's first step, not a shortcut past its second: the
    extraction is parked on the submission exactly as it is for a person, and
    nothing reaches the queue until the agent sends the listing back to the
    submission's own URL. The agent stands where the submitter would, checking
    a model's reading before a moderator is asked to trust it.
    """
    try:
        payload = json.loads(request.body or b"{}")
    except (ValueError, UnicodeDecodeError):
        return _error(400, "The body must be JSON.")
    if not isinstance(payload, dict):
        return _error(400, "The body must be a JSON object.")

    unknown = sorted(set(payload) - {"url", "source_text"})
    if unknown:
        return _error(
            400, f"Unknown fields: {', '.join(unknown)}.",
            accepted=["url", "source_text"],
        )

    url = payload.get("url", "")
    text = payload.get("source_text", "")
    if not isinstance(url, str) or not isinstance(text, str):
        return _error(400, "'url' and 'source_text' must be strings.")
    url, text = url.strip(), text.strip()
    if not (url or text):
        return _error(400, "Send a 'url' to read, or the event's 'source_text'.")
    if url:
        try:
            URLValidator(schemes=["http", "https"])(url)
        except ValidationError:
            return _error(400, "'url' must be an http or https link.")
    if len(text) > MAX_SOURCE_TEXT:
        return _error(400, f"'source_text' is limited to {MAX_SOURCE_TEXT} characters.")

    quota = SubmissionQuota.for_user(request.user)
    if not quota.may_submit():
        return _error(429, "You have reached today's submission limit.")
    if not quota.may_enrich():
        return _error(429, "You have used today's page reads.")

    submission = Submission.objects.create(
        submitted_by=request.user,
        api_token=request.api_token,
        source_url=url,
        pasted_text=text,
        source_advice=advice_for(url),
        status=Submission.Status.NEW,
    )
    enrich_submission.enqueue(submission.pk)
    return JsonResponse({"submission": _submission_json(submission)}, status=202)


@token_required("GET", "POST")
def submission_detail(request, pk):
    """Read one of your submissions, or answer a moderator's question.

    A POST carries the whole listing again, exactly as a first submission
    does — it is the confirmation page's resubmit, which updates the event
    rather than making a second one.
    """
    submission = (
        Submission.objects.filter(pk=pk, submitted_by=request.user)
        .select_related("event")
        .first()
    )
    if submission is None:
        return _error(404, "No submission of yours has that id.")
    if submission.is_stranded:
        submission.recover_from_stranding()

    if request.method == "GET":
        return JsonResponse({"submission": _submission_json(submission)})

    if not submission.is_editable_by_submitter:
        return _error(
            409,
            f"This submission is '{submission.status}', and can only be "
            "sent while it is waiting on you: 'awaiting_submitter' after a "
            "page read, or 'info_requested' after a moderator's question.",
            submission=_submission_json(submission),
        )

    payload, problem = _payload(request)
    if problem:
        return problem
    cleaned, errors = _validate(payload)
    if errors:
        return _error(400, "The listing did not validate.", errors=errors)
    if payload.get("dry_run"):
        return JsonResponse(
            {
                "valid": True,
                # Its own event is not a duplicate of itself.
                "possible_duplicates": _duplicates(cleaned, exclude=submission.event_id),
            }
        )

    with transaction.atomic():
        if "source_text" in payload:
            submission.pasted_text = payload["source_text"]
            submission.save(update_fields=["pasted_text", "updated_at"])
        _save(submission, cleaned, payload)

    return JsonResponse({"submission": _submission_json(submission)})


def _payload(request):
    """The request body as a dict, or a 400 saying what is wrong with it."""
    try:
        payload = json.loads(request.body or b"{}")
    except (ValueError, UnicodeDecodeError):
        return None, _error(400, "The body must be JSON.")
    if not isinstance(payload, dict):
        return None, _error(400, "The body must be a JSON object.")

    unknown = sorted(set(payload) - ACCEPTED_FIELDS)
    if unknown:
        return None, _error(
            400, f"Unknown fields: {', '.join(unknown)}.",
            accepted=sorted(ACCEPTED_FIELDS),
        )

    for name in ("source_text", "note"):
        if not isinstance(payload.get(name, ""), str):
            return None, _error(400, f"'{name}' must be a string.")
    if len(payload.get("source_text", "")) > MAX_SOURCE_TEXT:
        return None, _error(
            400, f"'source_text' is limited to {MAX_SOURCE_TEXT} characters."
        )
    return payload, None


def _validate(payload):
    """Run the payload through the confirmation page's own form and formset.

    Returns (cleaned data, None) or (None, errors). The payload is translated
    into what a browser would post, rather than validated separately, so the
    two paths cannot disagree about what a valid listing is.
    """
    errors = {}

    data = {}
    for name in TEXT_FIELDS:
        value = payload.get(name)
        if value is None:
            continue
        if not isinstance(value, str):
            errors[name] = ["Must be a string."]
            continue
        data[name] = value
    for name in BOOLEAN_FIELDS:
        value = payload.get(name, False)
        if not isinstance(value, bool):
            errors[name] = ["Must be true or false."]
        elif value:
            # A checkbox posts something when ticked and nothing when not.
            data[name] = "on"

    slugs = payload.get("categories") or []
    if not isinstance(slugs, list) or not all(isinstance(s, str) for s in slugs):
        errors["categories"] = ["Must be a list of category slugs."]
    else:
        found = dict(
            Category.objects.filter(slug__in=slugs, is_active=True).values_list(
                "slug", "pk"
            )
        )
        missing = [s for s in slugs if s not in found]
        if missing:
            errors["categories"] = [
                f"Unknown category: {', '.join(missing)}. "
                "See the categories endpoint for the list."
            ]
        data["categories"] = [str(found[s]) for s in slugs if s in found]

    rows = payload.get("occurrences")
    if not isinstance(rows, list) or not rows:
        errors["occurrences"] = ["Give at least one date: [{\"start\": ...}]."]
        rows = []
    elif len(rows) > MAX_OCCURRENCE_ROWS:
        # The formset's max_num only limits the rows it renders; bound data
        # beyond it would be accepted without validate_max.
        errors["occurrences"] = [f"No more than {MAX_OCCURRENCE_ROWS} dates."]
        rows = []
    elif not all(isinstance(row, dict) for row in rows):
        errors["occurrences"] = ["Each date must be an object with a 'start'."]
        rows = []

    dates_data = {
        "dates-TOTAL_FORMS": str(len(rows)),
        "dates-INITIAL_FORMS": "0",
        "dates-MIN_NUM_FORMS": "0",
        "dates-MAX_NUM_FORMS": str(MAX_OCCURRENCE_ROWS),
    }
    for index, row in enumerate(rows):
        for key in ("start", "end", "note"):
            value = row.get(key)
            if value is not None:
                dates_data[f"dates-{index}-{key}"] = str(value)

    form = EventDraftForm(data)
    dates = OccurrenceFormSet(dates_data, prefix="dates")

    if not form.is_valid():
        for name, messages in form.errors.items():
            errors.setdefault(name, []).extend(messages)
    if rows and not dates.is_valid():
        row_errors = {
            str(index): row
            for index, row in enumerate(dates.errors)
            if row
        }
        if row_errors:
            errors["occurrences_rows"] = row_errors
        if dates.non_form_errors():
            errors.setdefault("occurrences", []).extend(dates.non_form_errors())

    if errors:
        return None, errors
    return {**form.cleaned_data, "occurrences": dates.dates()}, None


def _save(submission, cleaned, payload):
    note = payload.get("note", "").strip()
    if note:
        SubmissionMessage.objects.create(
            submission=submission, author=submission.submitted_by, body=note
        )
    save_event_from_draft(submission, cleaned)
    submission.refresh_from_db()


def _duplicates(cleaned, exclude=None):
    matches = find_duplicates(
        cleaned["title"],
        cleaned.get("source_url", ""),
        [(row["start"], row.get("end")) for row in cleaned["occurrences"]],
        exclude_pk=exclude,
    )
    return [
        {
            "title": match["event"].title,
            "status": match["event"].status,
            "reason": match["reason"],
            "url": _event_url(match["event"]),
        }
        for match in matches
    ]


def _event_url(event):
    """The public page, only once there is one to see."""
    if event.status != Event.Status.PUBLISHED:
        return None
    return _absolute(reverse("event_detail", args=[event.slug]))


def _submissions_left(user):
    quota = SubmissionQuota.for_user(user)
    if quota.is_exempt:
        return None
    return max(quota.submissions_per_day - quota.submissions_today(), 0)


def _submission_json(submission):
    event = submission.event
    body = {
        "id": submission.pk,
        "status": submission.status,
        "status_display": submission.get_status_display(),
        "title": submission.display_title,
        "created_at": submission.created_at.isoformat(),
        "decision_note": submission.decision_note,
        "event": event and {
            "status": event.status,
            "url": _event_url(event),
        },
        "messages": [
            {
                "from": "moderator" if m.is_from_moderator else "submitter",
                "body": m.body,
                "created_at": m.created_at.isoformat(),
            }
            for m in submission.messages.all()
        ],
        "review_url": _absolute(reverse("submission_detail", args=[submission.pk])),
        "api": _absolute(reverse("api_submission", args=[submission.pk])),
    }
    if submission.status in {Submission.Status.NEW, Submission.Status.ENRICHING}:
        body["next"] = (
            "The site is reading the page. Poll this submission's 'api' URL; "
            "a read can take several minutes."
        )
    elif submission.status == Submission.Status.AWAITING_SUBMITTER and not event:
        body["extraction"] = {
            "failed": submission.enrichment_failed,
            "message": submission.enrichment_message,
            # What the model was unsure of, for the agent rather than the
            # listing: posted back as a field it would be refused.
            "notes": (submission.draft or {}).get("notes_for_submitter", ""),
            "listing": _listing_from_draft(submission),
        }
        body["next"] = (
            "Check extraction.listing against the page, correct it, and POST "
            "it to this submission's 'api' URL to send it to the moderators. "
            "Nothing is queued until you do."
        )
    return body


def _listing_from_draft(submission):
    """An extracted draft in the shape the submit endpoints take.

    So the agent's confirmation is a round trip: read it, fix it, post it
    back. Fields the draft has but a listing does not (prices, ages, the
    image) are left out rather than refused on the way back in.
    """
    draft = submission.draft or {}
    listing = {
        name: draft[name]
        for name in TEXT_FIELDS + BOOLEAN_FIELDS
        if draft.get(name) not in (None, "")
    }
    # An extraction says what a page contains, never where it was.
    if submission.source_url:
        listing.setdefault("source_url", submission.source_url)
    slugs = draft.get("category_slugs") or []
    listing["categories"] = list(
        Category.objects.filter(slug__in=slugs, is_active=True).values_list(
            "slug", flat=True
        )
    )
    listing["occurrences"] = [
        {key: row[key] for key in ("start", "end", "note") if row.get(key)}
        for row in draft.get("occurrences") or []
        if row.get("start")
    ]
    return listing
