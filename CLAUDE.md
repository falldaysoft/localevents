# localevents

Community event listing hub for one town or region. Django 6 on the falldaysoft
stack. MIT licensed.

**The central constraint: `localevents` is the reusable product; a running site
like a specific town's events page is an *instance*.** No community's name,
coordinates, timezone, or content may appear in Python, templates, or chart
defaults. It goes in environment variables, in the admin-editable `SiteConfig`,
or in an untracked overlay under `instances/`. `tests/test_reusability.py`
enforces this and will fail the build.

## Commands

```bash
make install      # create .venv (python3.14) and install
make dev          # runserver on :8000
make worker       # background task worker — REQUIRED, see below
make test         # pytest
make check        # system checks + missing-migration check
make migrate
make superuser

make deploy INSTANCE=<name> TAG=<git-sha>   # from an IP-allowlisted machine
```

**When asked to start the server, start both processes.** `make dev` alone
looks fine but silently does no enrichment, geocoding, feed polling, or email —
all of that runs on the queue. Run `make worker` alongside it.

**Restart both after a model or migration change.** A server holding a
pre-migration model definition omits the new column from its INSERT and fails
with `NOT NULL constraint failed` — which looks like a schema bug and is not.
The same goes for templates: more than once, "my fix didn't work" turned out to
be a stray `runserver` from an earlier session still bound to the port. Check
`ps aux | grep runserver` before believing a puzzling result.

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
contactable UA.

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
Conflating them is how a listing site turns into noise.

**The crowd nominates, a human decides.** The "Interested" count feeds a mod
*Rising* queue and breaks ties within a prominence tier; it can never move an
event between tiers. That is why anonymous interest with weak cookie/IP dedup
is acceptable — the signal's power is bounded by design.

**Enrichment tries structured data before the LLM.** schema.org `Event` markup
is exact, instant, and free; only pages without it cost a model call. Provider
(Anthropic or OpenRouter) and model are set in the admin, not in code, so they
can be swapped and compared without a redeploy. `EnrichmentRun` records
provider, model, tokens, and cost for every attempt — including the free ones
and the failures, because that is what makes "is the cheap model good enough"
answerable.

**The OpenRouter path assumes nothing about response formats.** Support varies
across the models OpenRouter fronts, which is the reason to use it. Live
testing found `claude-sonnet-5` returning *no choices at all* for a strict
`json_schema` request — a 200 with an `error` object, not an HTTP error, so
naive code crashes indexing `choices[0]`. The client now degrades
json_schema → json_object → no format, keeps the schema in the prompt
throughout, and validates the reply either way.

**Extraction is slow — budget for it.** A live run against a busy council-style
page took 116 seconds end to end (~3k input tokens, ~$0.013). That is why this
runs as a background task with a polling spinner rather than in the request.
Note the SDK `timeout` is a *read* timeout, not a wall-clock budget: a slowly
streaming response can exceed it, as that run did against a 90s setting. If a
hard ceiling is ever needed, it has to be imposed outside the client.

**Nothing an AI produced reaches a moderator unreviewed.** The submitter
confirms every extracted field first. That is what makes a cheap, imperfect
model an acceptable trade — a rough extraction costs a submitter a minute of
editing rather than putting invented-but-plausible details in front of someone
who will trust them.

**The fetcher is an SSRF boundary.** The URL is user-supplied, so hostnames are
resolved and checked against private, loopback, link-local (including cloud
metadata) and reserved ranges before any request. Do not add a code path that
fetches a user-supplied URL without going through `enrichment.fetcher`.

**CI builds, it does not deploy.** The LKE API server is behind an IP allowlist
that GitHub-hosted runners cannot satisfy. CI pushes the image; `make deploy`
runs from an allowlisted machine.

## Conventions

- Flat `localevents/settings.py`, `os.environ.get` with inline defaults, no
  settings split, `dj_database_url` (SQLite locally, Postgres in production).
- Frontend is CDN Tailwind + Alpine + HTMX. **No build step.** Do not add npm.
- Project-level `templates/`, partials as `templates/partials/_name.html`.
  Django 6 `{% partialdef %}` for HTMX fragments that belong with their page.
- Tests live in top-level `tests/`, one file per concern. pytest, not
  `unittest`.
- Prose comments explain *why* a non-obvious setting exists, not what the line
  does.
