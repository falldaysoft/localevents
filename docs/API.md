# Submitting events through the API

For an agent or script that has already read an event's details and wants to
send them to the moderators without going through the web form. Everything
here goes into the same queue as a person's submission, and a moderator still
approves each one before anything is published.

## Getting a token

Only moderators can hold one. Sign in, open your profile (click your name),
and under **API tokens** name the token after whatever will use it. The token
is shown once, so copy it then. Revoke it on the same page. It stops working
by itself if you stop being a moderator.

Send it on every request:

```
Authorization: Bearer le_…
```

A browser session doesn't count. Without the header every endpoint except
`/api/` returns 401.

`GET /api/` needs no token. It describes the fields, rules and workflows, and
lists the categories, so an agent given only the site's address can start
there. If you send a token it also reports how many submissions you have left
today.

## Endpoints

All of them take and return JSON. Errors are JSON too, with an `error`
message and sometimes `errors`, which is keyed by field.

| Method | Path | What it does |
|---|---|---|
| GET | `/api/` | Describes the API and lists categories. No token needed |
| GET | `/api/categories/` | The active categories as `{slug, name}` |
| POST | `/api/extract/` | Have the site read a page for you (see below) |
| GET | `/api/submissions/` | Your submissions, newest first |
| POST | `/api/submissions/` | Submit a listing (or check one with `dry_run`) |
| GET | `/api/submissions/<id>/` | One of yours: its status, the decision, and moderators' messages |
| POST | `/api/submissions/<id>/` | Send the listing for a submission that's waiting on you |

## A listing

```json
{
  "title": "Poetry night",
  "summary": "Open readings, all welcome.",
  "description": "Longer text. Plain text; line breaks are kept.",
  "occurrences": [
    {"start": "2026-11-06T19:00", "end": "2026-11-06T21:00"},
    {"start": "2026-11-13T19:00", "end": "2026-11-13T21:00", "note": "Guest reader"}
  ],
  "venue_name": "Parish Hall",
  "venue_address": "1 Church St",
  "venue_city": "Sampletown",
  "organizer_name": "Sampletown Writers",
  "categories": ["learning", "community"],
  "is_series": false,
  "is_free": true,
  "price_note": "",
  "is_family_friendly": true,
  "accessibility_notes": "Step-free entrance.",
  "source_url": "https://example.org/events/poetry-night",
  "ticket_url": "",
  "source_text": "The event's own words, as you read them.",
  "note": "Found on the county calendar; the page can't be fetched."
}
```

Only `title` and at least one occurrence with a `start` are required. The
rules:

- **Times.** ISO 8601. A time without an offset is taken as the site's local
  time. `2026-11-06T19:00` means 7pm where the events are, not UTC. A time
  with an offset (`…T19:00-05:00`) is used exactly as given.
- **Dates.** At most 60. Each date has its own `end` and `note`. A festival
  running Friday to Sunday is one occurrence whose `end` falls on a later
  day, not three. A listing whose dates have *all* passed is refused, since
  the usual cause is the wrong year. The same start listed twice is refused.
- **Categories** are slugs from `/api/categories/`. An unknown slug is refused.
- **Unknown fields are refused by name**, so a typo like `venue_adress` is
  reported rather than dropped.
- **`is_series`** marks a weekly class, club or market. A moderator decides
  how it is listed.
- **`source_text`** (up to 10,000 characters) is kept for the moderator to
  check the listing against. Please send it, because nobody confirmed these
  details on the site. It matters most when `source_url` can't be opened
  without a login.
- **`note`** becomes a message to the moderators on the submission.

## Letting the site read the page

If you only have a link, the site can read it for you the same way it does for
someone using the web form:

```
POST /api/extract/
{"url": "https://example.org/events/poetry-night"}
```

This returns `202` with a submission whose status is `new`. Reading takes a
while (minutes, not seconds), so poll `GET /api/submissions/<id>/` until the
status is `awaiting_submitter`. The submission then carries an `extraction`:

```json
"extraction": {
  "failed": false,
  "message": "…",
  "notes": "What the reader was unsure of, e.g. the year isn't stated.",
  "listing": {"title": "…", "occurrences": […], "categories": […], …}
}
```

`extraction.listing` uses the same format as a listing you'd write yourself.
Check it against the page, correct anything wrong, and POST it to
`/api/submissions/<id>/`. **Nothing reaches the moderators until you do.** A
model's reading of a page is often nearly right, and the date is what it gets
wrong most. Someone has to check that before a moderator is asked to trust it,
and on this path that's you.

If the page needs a login or JavaScript (Facebook, for example), it will read
badly or not at all. Send `{"source_text": "…"}` with the event's own words
instead, with or without the `url`. A read that fails still lands at
`awaiting_submitter`, with `failed: true` and a mostly empty listing for you to
fill in.

## Check before you send

Add `"dry_run": true` to validate a listing and look for events already on the
site, without writing anything:

```json
{"valid": true, "possible_duplicates": [
  {"title": "Poetry Night at the Hall", "status": "published",
   "reason": "Similar title on the same dates", "url": "https://…/events/…/"}
]}
```

A match counts as a duplicate when the title is similar and the dates fall
within a day, or when the source page is the same. It checks both published
listings and ones waiting in the queue. A real submission returns the same
`possible_duplicates` and is accepted anyway, because two events can look
alike and both be real. If you find a match, skip the event or say why in
`note`.

## Responses

A submission returns `201`:

```json
{"submission": {
  "id": 42, "status": "pending_review", "status_display": "Awaiting review",
  "title": "Poetry night", "created_at": "…", "decision_note": "",
  "event": {"status": "pending", "url": null},
  "messages": [], "review_url": "https://…/submit/42/", "api": "https://…/api/submissions/42/"
}, "possible_duplicates": []}
```

`event.url` stays empty until the listing is published. The statuses you will
see are `new` and `enriching` (the site is reading a page), `awaiting_submitter`
(read, and waiting for you to send it), `pending_review`, `info_requested` (a moderator asked something, which
appears under `messages`), `approved`, `rejected` and `withdrawn`.

When the status is `info_requested`, POST the whole listing to
`/api/submissions/<id>/` with your answer in `note`. That updates the same
event and puts it back in the queue. At any other status that POST returns 409.

| Status | Meaning |
|---|---|
| 400 | Not JSON, an unknown field, or the listing failed validation (see `errors`) |
| 401 | No token, or a revoked one |
| 403 | The token's owner is no longer a moderator |
| 404 | Not one of your submissions |
| 405 | Wrong method |
| 409 | That submission isn't waiting on you |
| 429 | Daily submission or page-read limit reached (moderators are exempt) |

`errors.occurrences` holds problems with the set of dates.
`errors.occurrences_rows` holds per-date problems, keyed by position in your
list, starting from `"0"`.
