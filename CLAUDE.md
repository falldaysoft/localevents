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

## Architecture notes

**Background tasks.** Django 6 ships the `django.tasks` API (`@task`,
`.enqueue()`); the `django-tasks` package supplies the DatabaseBackend and the
`db_worker` command. Pinned `<0.12` because 0.12 dropped the database backend.
Verified working together — `from django.tasks import task` enqueues through
the django-tasks backend.

**Nominatim geocoding is throttled globally.** Their policy is 1 request/second
and it applies across every process, so an in-process sleep is not enough with
more than one worker or pod. Geocoding claims a `GeocodeThrottle` singleton row
with `select_for_update()`, waits out the remainder of the second, then calls.
A venue geocodes once and caches forever, so volume is low — the case that
stresses this is a bulk import introducing many new venues at once. The
`USER_AGENT` setting is load-bearing: Nominatim blocks requests without a
contactable UA.

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
provider, model, tokens, and cost per call — that record is what makes "is the
cheap model good enough" answerable.

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
