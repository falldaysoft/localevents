# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

# localevents

Community event listing hub for one town or region. Django 6 on the falldaysoft
stack. MIT licensed.

**The central constraint: `localevents` is the reusable product; a running site
like a specific town's events page is an *instance*.** No community's name,
coordinates, timezone, or content may appear in Python, templates, or chart
defaults. It goes in environment variables, in the admin-editable `SiteConfig`,
or in an untracked overlay under `instances/`. `tests/test_reusability.py`
enforces this — it greps the whole tree for `BANNED_SUBSTRINGS` — and will fail
the build.

`docs/STATE.md` is the working note for what is built, what is *verified against
reality* versus merely tested, and what is next. Read it before starting a phase.

## Commands

```bash
make install      # create .venv (python3.14) and install
make dev          # runserver on :8000
make worker       # background task worker — REQUIRED, see below
make stop         # kill any stray runserver/db_worker
make test         # pytest
make check        # system checks + missing-migration check
make migrate
make superuser

make deploy INSTANCE=<name>                 # from an IP-allowlisted machine; deploys HEAD
make deploy INSTANCE=<name> TAG=<full-sha>  # CI tags with the *full* 40-char SHA
```

One file, one test, or by name. **`DEBUG=true` is not optional** — without it
`DATABASE_URL` is unset and every test errors at setup with "settings.DATABASES
is improperly configured", which looks like a broken checkout and is only a
missing environment variable:

```bash
DEBUG=true .venv/bin/python -m pytest tests/test_moderation.py -q
DEBUG=true .venv/bin/python -m pytest tests/test_moderation.py::test_assignment_is_a_toggle -q
DEBUG=true .venv/bin/python -m pytest -k rising -q
```

**When asked to start the server, start both processes.** `make dev` alone
looks fine but silently does no enrichment, geocoding, feed polling, or email —
all of that runs on the queue. Run `make worker` alongside it. `db_worker`
autoreloads by default, which kills tasks mid-flight when files change; pass
`--no-reload` when a run matters.

**Restart both after a model or migration change.** A server holding a
pre-migration model definition omits the new column from its INSERT and fails
with `NOT NULL constraint failed` — which looks like a schema bug and is not.
The same goes for templates: more than once, "my fix didn't work" turned out to
be a stray `runserver` from an earlier session still bound to the port. Check
`ps aux | grep runserver` before believing a puzzling result.

`createsuperuser` produces an account that **cannot sign in** — email
confirmation is mandatory and a shell-created account has no confirmation mail
to click. Follow it with `manage.py verify_email you@example.com`, or use
`/claim/`, which verifies as it goes.

## Layout

Six apps, split by who acts rather than by data:

- **`events`** — the domain: `Venue`, `Organizer`, `Category`, `Event`,
  `Occurrence`, `Interest`, plus `GeocodeThrottle` and the geocoding tasks.
- **`submissions`** — the submitter's path: `Submission`, `SubmissionMessage`,
  `ModerationAction`, the enrichment task, and `services.save_event_from_draft`.
- **`moderation`** — **no models.** It reads `submissions`' models. Every view
  sits behind one `moderator_required` decorator, and a whole app under one rule
  is far easier to audit than a mixed one.
- **`enrichment`** — `fetcher` (the SSRF boundary), `structured` (schema.org),
  `llm`, `pipeline` (which orders those three), `EnrichmentRun` (the cost record).
- **`web`** — public browse, filters, map geojson, `/healthz`.
- **`accounts`** — custom `User` (email is the credential, username is only a
  display name), `/claim/`, profile.
- **`core`** — the `SiteConfig` and `AIConfig` singletons, the CSP middleware,
  the housekeeping command run by the Helm CronJob.

## Architecture notes

**Background tasks.** Django 6 ships the `django.tasks` API (`@task`,
`.enqueue()`); the `django-tasks` package supplies the DatabaseBackend and the
`db_worker` command. Pinned `<0.12` because 0.12 dropped the database backend.
Verified working together — `from django.tasks import task` enqueues through
the django-tasks backend.

**A single un-importable queued task kills the worker.** The backend resolves
`task_path` with `import_string` and lets the ImportError propagate, so one bad
row stops *all* background work — enrichment, geocoding, email, feed polling —
and the only symptom is a worker that won't stay up. The realistic way to hit
this is renaming or moving a task function while rows referencing the old path
are still queued. If the worker crashes on boot with an ImportError, look at
`DBTaskResult` for stale `task_path` values before anything else. Renaming a
task function is therefore a two-step deploy, or needs the old queue drained
first.

**Nominatim geocoding is throttled globally.** Their policy is 1 request/second
and it applies across every process, so an in-process sleep is not enough with
more than one worker or pod. Geocoding claims a `GeocodeThrottle` singleton row
with `select_for_update()`, waits out the remainder of the second, then calls.
A venue geocodes once and caches forever, so volume is low — the case that
stresses this is a bulk import introducing many new venues at once. The
`USER_AGENT` setting is load-bearing: Nominatim blocks requests without a
contactable UA. Note `select_for_update()` is a **no-op on SQLite**, so local
development cannot prove this one.

**A site with no superuser is unclaimed.** `/claim/` creates the first
administrator from the browser and marks the address verified as it goes,
because the person who configures SMTP cannot be gated on SMTP working. It is
first-come-first-served by choice: every gate worth having needs a secret
delivered by `kubectl exec`, which is the thing the page exists to remove. The
page 404s once any superuser exists, and `accounts.claim` latches that answer
per process — so it fails closed, and a site that loses its last superuser
stays closed until a restart.

**Two orthogonal axes on `Event`.** `listing_type` (one-off vs series) controls
*collapsing* — a weekly class must never emit 52 cards. `prominence`
(featured/listed/background) controls *placement* and is set by a moderator.
Conflating them is how a listing site turns into noise. Every public query goes
through `Occurrence`, not `Event`: "what's on this weekend" is a question about
dates. There is deliberately no concept of an online event.

**Moderator decisions go through `moderation/services.py`, never a view.** Four
things must move together — the event, the submission, the audit row, and the
mail to the submitter — and a view that did three of the four would look fine in
review and leave someone waiting forever. That mail is queued with
`transaction.on_commit`, so a decision that rolls back never mails anyone.

**`submissions.services.save_event_from_draft` creates *or updates*.** When a
moderator asks a question, the submission goes back to its owner with the event
already created, and a second confirmation has to update that event. An earlier
create-only version forked it: the queue entry pointed at an abandoned row while
the answer sat in an event nobody was looking at. It also replaces occurrences
rather than merging them, and never touches `prominence`.

**An occurrence has a duration, and every layer has to honour it.** Two
different things are called "multi-day" and they break in opposite directions.

A *span* is one occurrence running continuously across days — a festival from
Friday evening to Sunday afternoon. It disappears if any query asks "does it
*start* in this window", because on Saturday it started yesterday. So the test
is overlap, not containment: `events.models.occurrence_overlaps` is the single
definition, used by the browse filters, `active_venues` and
`Event.upcoming_occurrences`. An occurrence with no stated end counts as ending
when it starts, which keeps the ordinary single-instant case identical.

A *repeat* is several occurrences with different hours — a market open Fridays
9–2 and Saturdays 7–2. It loses its hours if the form has one "ends at" box and
a list of bare dates, which is what it had: every closing time but the first
was stored as null. Dates are now an `OccurrenceFormSet`, every row carrying
its own start, end and note, with no privileged first row.

Two consequences worth knowing. `web.filters` annotates `next_start` and
`next_end` with a **subquery**, not two `Min()` aggregates — `Min` skips nulls,
so the two aggregates straddle different occurrences the moment one has an end
and another does not, and the card prints one date's start beside another's
finish. And templates must never format an end on its own: `{% occurrence_when %}`
in `events/templatetags/event_dates.py` owns the three shapes (no end, ends the
same day, ends later), because the obvious `{{ o.end|date:"H:i" }}` renders a
weekend-long festival as "Fri 5 Sep, 18:00 – 17:00".

**The crowd nominates, a human decides.** The "Interested" count feeds a mod
*Rising* queue and breaks ties within a prominence tier; it can never move an
event between tiers. That is why anonymous interest with weak cookie/IP dedup
is acceptable — the signal's power is bounded by design. Rising ranks
`interest_count` against its own *tier's* average, floored by
`MIN_INTEREST_TO_RISE`, because a ratio over a tiny population is nonsense:
promote the one busy event out of a tier and whatever remains *becomes* that
tier's average.

**Enrichment tries structured data before the LLM.** schema.org `Event` markup
is exact, instant, and free; only pages without it cost a model call. The
endpoint and model are set in the admin (`AIConfig`), not in code, so they can
be swapped and compared without a redeploy. `EnrichmentRun` records endpoint,
model, tokens, and cost for every attempt — including the free ones and the
failures, because that is what makes "is the cheap model good enough"
answerable. There is a daily spend cap, and enrichment ships **disabled**.

**The OpenRouter path assumes nothing about response formats.** Support varies
across the models OpenRouter fronts, which is the reason to use it. Live
testing found `claude-sonnet-5` returning *no choices at all* for a strict
`json_schema` request — a 200 with an `error` object, not an HTTP error, so
naive code crashes indexing `choices[0]`. The client degrades
json_schema → json_object → no format, keeps the schema in the prompt
throughout, and validates the reply either way. What it learns is remembered on
`AIConfig.json_schema_probed_model` and retired automatically when the model
changes — that rejection measured 203 seconds, and paying it per enrichment is
not acceptable.

**Extraction is slow — budget for it.** Live runs against a busy council-style
page took 116s and 336s (~3k input tokens, ~$0.013). That is why this runs as a
background task with a polling spinner rather than in the request, and why
`STALE_ENRICHMENT_AFTER` is 20 minutes rather than something tighter. Note the
SDK `timeout` is a *read* timeout, not a wall-clock budget: a slowly streaming
response can exceed it, as one run did against a 90s setting. If a hard ceiling
is ever needed, it has to be imposed outside the client.

**Nothing an AI produced reaches a moderator unreviewed.** The submitter
confirms every extracted field first. That is what makes a cheap, imperfect
model an acceptable trade — a rough extraction costs a submitter a minute of
editing rather than putting invented-but-plausible details in front of someone
who will trust them. The load-bearing defence against the model's habit of
guessing the wrong *year* is not the prompt — a rule was added and the next live
run got it wrong anyway — it is `BaseOccurrenceFormSet.clean`, which rejects a
submission whose dates have *all* gone by. Deliberately the whole set and not
each row: both live misextractions were wrong uniformly, while a series sent
back with a moderator's question has legitimately lost a date or two by the
time its owner replies. Rejecting past rows one at a time would block that
resubmission to catch a mistake the set-level test already catches.

**The fetcher is an SSRF boundary.** The URL is user-supplied, so hostnames are
resolved and checked against private, loopback, link-local (including cloud
metadata) and reserved ranges before any request. It also honours robots.txt and
caps the download at 2 MB. Do not add a code path that fetches a user-supplied
URL without going through `enrichment.fetcher` — that includes feed importers.

**`/healthz` must not touch the database.** A database that hangs rather than
refusing would otherwise block the liveness probe, and Kubernetes would restart
a container whose only problem was upstream. `SiteHeadCSPMiddleware` skips
non-HTML responses for exactly this reason, and `tests/test_smoke.py` asserts
zero queries — not merely the absence of the `db` fixture, which the
middleware's blanket `except Exception` used to swallow.

**CI builds, it does not deploy.** The LKE API server is behind an IP allowlist
that GitHub-hosted runners cannot satisfy. CI runs the checks, the
missing-migration check and the tests, then pushes the image; `make deploy` runs
from an allowlisted machine. Images are linux/amd64 only.

## Testing

pytest with `pytest-django`, tests in top-level `tests/`, one file per concern,
never `unittest`. Four autouse fixtures in `tests/conftest.py` exist for reasons
that will bite if they are removed:

- Tasks run on the **immediate backend**, so no worker is needed and a test can
  assert on the result of a task the code under test fired off.
- `OPENROUTER_API_KEY` is **blanked**. `settings.py` loads `.env`, so a machine
  with a working key would otherwise make real, billable calls — presenting as a
  hang. A test asserts it stays blank.
- Static storage is forced to plain, because pytest-django forces `DEBUG` off
  and the manifest backend would blow up on the first `{% static %}`.
- The `/claim/` latch is reset, since it caches "a superuser exists" for the
  life of the process.

**`transaction.on_commit` never fires under pytest-django.** Each test runs in a
transaction that is rolled back, so anything queued on commit — every moderation
email — silently never happens and mail assertions pass against an empty outbox.
`tests/test_moderation.py` wraps decisions in a `decide` fixture built on
`django_capture_on_commit_callbacks(execute=True)`.

## Conventions

- Flat `localevents/settings.py`, `os.environ.get` with inline defaults, no
  settings split, `dj_database_url` (SQLite locally, Postgres in production).
- Frontend is CDN Tailwind + HTMX. **No build step.** Do not add npm.
- **Alpine is loaded in `base.html` and cannot run.** `SECURE_CSP` withholds
  `'unsafe-eval'` (correctly), and Alpine's CDN build compiles every `x-`
  expression with the `Function` constructor — so it loads, reports itself
  present, and silently does nothing. Use `<details>` or HTMX instead; the
  moderation queue was rebuilt on `<details>` for this reason and
  `tests/test_moderation.py` asserts no `x-show`/`x-data` reaches its output.
  If Alpine is ever genuinely
  wanted, the fix is `@alpinejs/csp`, **not** `'unsafe-eval'` on a site that
  renders text strangers submitted.
- Project-level `templates/`, partials as `templates/partials/_name.html`.
  Django 6 `{% partialdef %}` for HTMX fragments that belong with their page,
  rendered as `render(request, "web/index.html#results", context)`.
- Django template comments `{# #}` are **single-line only** — a multi-line one
  renders as visible page text. `tests/test_browse.py` fails the build if raw
  template syntax reaches the output.
- Never rename an applied migration. Django records the old name, so the renamed
  file looks new and re-runs.
- Prose comments explain *why* a non-obvious setting exists, not what the line
  does.
