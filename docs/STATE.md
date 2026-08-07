# Where this got to

A working note for picking the build back up. Architecture and conventions
live in `CLAUDE.md`; the full plan lives in
`~/.claude/plans/i-want-to-create-curious-wreath.md`. This file covers only
what those two don't: what is actually done, what is proven versus merely
written, and the traps that cost time.

Last updated after the multi-day work that followed Phase 4.

## Done

| Phase | State | Commit |
|---|---|---|
| 0 — skeleton, accounts, deploy | done | `ceb3451` |
| 1 — domain model, geocoding | done | `f1046f7` |
| 2 — public browse, filters, map | done | `43c6115` |
| 3 — submission + AI enrichment | done | `0e4cece`, `6ed920e`, `4e5c4c8` |
| 4 — moderation queue, Rising | done | this commit |
| multi-day events | done | this commit |
| refresh from source, editable dates | done | this commit |
| pages and media (small CMS) | done | this commit |
| 5 — feed importers | **next** | |
| 6 — outbound feeds (ICS/RSS/JSON) | not started | |
| 7 — interest button (Rising itself is done) | not started | |
| 8 — series lifecycle, reusability docs | not started | |

373 tests passing. `make check` clean.

Phase 7 shrank: the `Interest` model, the Rising ranking and the promotion
action all landed with Phase 4, because the mod queue is where they are read.
What is left of it is the public "Interested" button and its cookie/IP
fingerprint dedup.

## Verified against reality, not just tests

- **Signup → email confirmation → login by email.** Username is a display name
  and is rejected as a credential, server-side as well as by the input type.
- **Browse, filters, map.** Narrowing to one category took the map from two
  pins to one with no page reload and no lost filter state — the geojson
  endpoint reads the same query string as the list.
- **A live model call through the real queue**, twice. OpenRouter,
  `claude-sonnet-5`, a real library page: ~3k input tokens, ~$0.013 a call. It
  pulled the event name correctly and honestly reported in
  `notes_for_submitter` that the page listed several events without details.
  Both runs got the **year** wrong (2025 for a 2026 event) — see "The year
  problem" below.
- **Manual submission end to end.** Event created as `pending` with prominence
  left for a moderator, an existing venue reused rather than duplicated, audit
  record written.
- **Fetch reliability across eight real sites.** Plain HTTP returned usable
  text from every one that permitted it. See "No headless browser" below.
- **The whole moderation loop, in a browser, through the real worker.** Ask a
  question → the submitter gets the email with a working link → they answer and
  resubmit → approve as Featured with categories → the listing appears on the
  public feed and the approval email links to it. Both emails went out through
  `db_worker`, not inline. The audit log shows all five steps with actors.
- **Rising ranks within a tier, not across.** With live data: 34 marks in a
  tier averaging 12 outranked 42 marks in a tier averaging 27. Promoting moved
  exactly one tier and re-ranked the queue.
- **A page written and an image uploaded, in a browser, over real HTTP.** A
  93KB 3000x2000 JPEG carrying an orientation flag and a GPS block was stored
  as a 3KB WebP at 1067x1600 — 3.3% of the original, *rotated upright*, with
  every EXIF tag gone. The page embedded it, the `<script>` and the
  `javascript:` link in its Markdown did not survive the save, the footer
  picked the page up, and a second request for the image returned 304 with
  zero bytes. The nonce on the moderator screens' one script matches the
  header, so it actually runs where Alpine could not.

## Not verified

- **No deploy has ever run.** The chart lints and renders and `scripts/deploy.sh`
  fails fast if the IP allowlist blocks it, but first contact with the cluster
  is still ahead. Namespace secrets do not exist yet.

  CI *is* proven as of `c5879ee`: the workflow runs green and
  `ghcr.io/falldaysoft/localevents` now holds a `latest` and a per-SHA tag. The
  image was also run locally — migrations applied, `/healthz` 200, browse 200,
  `/moderate/` 302 to login, static files served by whitenoise.

  **The ghcr package is private.** A workflow-created package does not inherit
  the repository's public visibility. That is fine — every workload in the
  chart already references a `ghcr-secret` pull secret and `deploy.sh` refuses
  to run without it — but it is the first thing that will bite if that secret
  is missing or copied from the wrong namespace. Making the package public in
  the repo's package settings is the other option.

  CI builds **linux/amd64 only**, which is right for LKE and means the image
  cannot run on an Apple Silicon machine without `--platform linux/amd64`.
- **No real email has been sent.** Console backend throughout — the moderation
  mail is proven to *render and dispatch*, not to arrive. SES credentials and
  DKIM are unproven, and `SITE_BASE_URL` (new, in settings) must be set per
  instance or every link in those emails points at localhost.
- **Postgres is unproven.** Everything so far is SQLite. Watch for anything
  relying on SQLite's laxness — the `select_for_update()` in `GeocodeThrottle`
  is a no-op on SQLite and only does its job on Postgres.

## Known gaps

- **Facebook events cannot be read.** Their robots.txt forbids it and we honour
  that. A lot of small-town events live only on Facebook, so those are
  manual-entry only. Worth considering a "paste the text instead of a link"
  option, which would also cover paywalls and PDFs cheaply.
- **Extraction is slow and the variance is large.** Two runs against the *same*
  page took 116s and 336s. Tolerable only because it runs on a worker behind a
  progress page, and the reason `STALE_ENRICHMENT_AFTER` is 20 minutes rather
  than something tighter. Worth measuring against a typical single-event page
  before launch — a library homepage is close to the worst case, being long and
  full of unrelated content.
- **SDK timeouts are read timeouts, not wall-clock budgets.** A slowly
  streaming reply exceeded a 90s setting. A hard ceiling would have to be
  imposed outside the client.
- **Cost estimates use rates typed into the admin.** They are not fetched, so
  they go stale silently when a model's pricing changes.

## Alpine is dead weight — the CSP forbids it

`base.html` loads Alpine on every page and it **cannot run**. `SECURE_CSP`
withholds `'unsafe-eval'` (correctly), and Alpine's standard CDN build compiles
every `x-` expression with the `Function` constructor. It loads, reports
`typeof window.Alpine === "object"`, and then silently does nothing.

Nothing had used `x-data` before Phase 4, so this sat latent since the
scaffold. The moderation queue's three action panels were built on `x-show`,
looked fine in tests, and rendered all three panels open in a real browser.

They are now native `<details>` — no JS, no CSP concession, and a test asserts
the page contains no `x-` attributes so this cannot creep back.

If Alpine is genuinely wanted later, the fix is `@alpinejs/csp` (which requires
expressions to live in `Alpine.data()` components), **not** adding
`'unsafe-eval'` to a site that renders text strangers submitted. Removing the
CDN tag from `base.html` is also reasonable — it currently costs a request and
buys nothing.

## The year problem

Both live runs dated a 2026 event to 2025. The page gives a day and month with
no year, and the model fills in the wrong one.

A prompt rule was added specifically to stop this — *"If the page shows a date
with no year, use the next occurrence of that date in the future — never a past
one"* — and the second run happened with that rule in force and got it wrong
anyway. **Do not assume the prompt fixed it.**

What actually catches it is `BaseOccurrenceFormSet.clean`, which rejects a
submission whose dates have *all* gone by with *"That date has already passed.
Is it the right year?"*. That is the load-bearing defence and it has a test.

It moved from a single field to the whole set when dates became a formset, and
the "all" is deliberate. Both misextractions were wrong uniformly — every date
a year back — while a series returning from a moderator's question has
legitimately lost a date or two by the time its owner answers. A per-row test
would catch the same mistake and block that resubmission as well.

If this is worth attacking further, the promising direction is not more
prompting but post-processing: when an extracted date lands in the past and the
page gave no year, roll it forward to the next occurrence before showing the
submitter. Cheap, deterministic, and testable.

## No headless browser — and why

Measured against eight real sites. Plain `httpx` returned usable text from
every one that allowed fetching, including the JavaScript-heavy platforms:
event pages are SEO-critical, so operators server-render them. The sites that
matter for a local listing — libraries, councils, church halls, small WordPress
and Squarespace venue sites — are the *easiest* of the lot.

A headless browser would add several hundred MB to the image and real memory
pressure on the worker to solve a problem this population mostly does not
have. If a specific site later proves to need it, add it for that site rather
than as a default.

Related: `submissions/sources.py` nudges submitters toward the organiser's own
page rather than a ticketing platform. That is partly about listing quality and
partly about staying out of territory a community project has no business in —
reading one page on a person's behalf is not harvesting a commercial catalogue.

## Traps that cost time

- **Stale processes.** A leftover `runserver` served old templates once and old
  models another time, the latter presenting as `NOT NULL constraint failed` on
  a column that existed and was migrated. `make dev` and `make worker` now kill
  stragglers first; `make stop` exists. Check `ps aux | grep runserver` before
  believing a puzzling result.
- **`db_worker` autoreloads by default.** Editing files under a running worker
  kills tasks mid-flight. Use `--no-reload` when a run matters.
- **One un-importable queued task kills the worker.** Renaming a task function
  while rows reference the old path takes down *all* background work. Two-step
  deploy, or drain first.
- **Never rename an applied migration.** Django records the old name; the
  renamed file then looks new and re-runs. Cost a manual `django_migrations`
  repair.
- **Multi-line `{# #}` renders as visible page text.** Django's is single-line
  only. `tests/test_browse.py` fails the build if raw template syntax reaches
  the output.
- **`.env` loading reaches the test suite.** It once made tests do real billable
  API calls, presenting as a hang. An autouse fixture blanks the key and a test
  asserts it stays blank — don't remove either.
- **`transaction.on_commit` never fires under pytest-django.** Each test runs in
  a transaction that is rolled back, so anything queued on commit — every
  moderation email — silently never happens and mail assertions pass against an
  empty outbox. `tests/test_moderation.py` wraps decisions in a `decide` fixture
  built on `django_capture_on_commit_callbacks(execute=True)`.
- **A ratio over a tiny population is nonsense.** Promote the one busy event out
  of a prominence tier and whatever remains *becomes* that tier's average, so
  the next event with two marks reads as "twice the average". Seen live.
  `MIN_INTEREST_TO_RISE` is the floor in front of it.

## How Phase 4 is put together

A separate `moderation` app with **no models** — `Submission`,
`SubmissionMessage` and `ModerationAction` stay in `submissions`, because the
submission flow creates them and a moderator only reads them later. The split
is about access: every view in `moderation` sits behind one
`moderator_required` decorator, and a whole app under one rule is far easier to
audit than a mixed one.

Every decision goes through `moderation/services.py`, never a view. Three
things have to move together — the event, the submission, and the audit row —
and the submitter has to be told. A view that did three of those four would
look fine in review and leave someone waiting forever.

The submitter's email is queued with `transaction.on_commit`, so a decision
that rolls back never mails anyone.

**One real bug this phase surfaced and fixed.** Answering a moderator's
question used to *fork the event*: `create_event_from_draft` always created,
so a second confirmation left the queue entry pointing at an abandoned row with
the answer sitting in an event nobody was looking at. It is now
`save_event_from_draft`, which creates or updates, replaces occurrences rather
than merging them, and never touches prominence. `tests/test_moderation.py`
guards it.

Rising is ranked by `interest_count / tier average`, floored by
`MIN_INTEREST_TO_RISE`. Raw counts across tiers would only rediscover that
featured events get more attention.

## Multi-day events

Prompted by the Brantford Farmers' Market — open Fridays 9–2 and Saturdays 7–2,
year round. The model read it correctly; the application threw the answer away.
Two separate faults, and it is worth keeping them apart because the fix for one
does nothing for the other.

**Extra dates lost their hours.** The confirmation form had one *Starts* and one
*Ends*, then a textarea of bare timestamps for everything else, and
`save_event_from_draft` wrote `end` onto the first occurrence and null onto the
rest. So the market published as a Saturday morning with no closing time.
Dates are now an `OccurrenceFormSet` — every row has its own start, end and
note, and no row is privileged. "Add more dates" is a plain submit button that
re-renders the page with more blank rows; it deliberately does not validate,
because someone asking for more space has not said they are finished.

**A span vanished while it was running.** Every query asked whether an
occurrence *starts* in the window, so a festival from Friday to Sunday was
absent from Today on Saturday, from This Weekend, from the map, from the feed
and from its own detail page — at exactly the moment it was worth showing. The
test is now overlap, defined once in `events.models.occurrence_overlaps`.

Three things this turned up that were not the point but were real:

- **A `datetime-local` input silently discards a value it cannot parse.** The
  draft holds ISO strings and `_initial_from_draft` handed them to the widget
  as `"2026-09-05 09:00"` — space, not `T`. The field rendered empty. So the
  extracted time was being lost between the model reading it and the submitter
  confirming it, on the URL path, without a word. Draft datetimes are parsed to
  real `datetime`s now and the widget formats them itself.
- **`Min(start)` and `Min(end)` do not describe the same occurrence.** `Min`
  skips nulls, so an event with one dated-but-open occurrence and one with an
  end annotates the first one's start beside the second one's finish. It is a
  subquery now.
- **schema.org pages publish one `Event` node per date.** Reading only the
  first turned a weekly market into a single date, with nothing to tell the
  submitter anything had been dropped.

Not attempted: **recurrence rules.** A market open every Friday and Saturday
indefinitely is still entered as explicit dates, capped at 60 rows, and renewed
through the existing series lifecycle. That is the honest limit of this change
— an RRULE, its expansion, and what editing one means for occurrences that have
already been published is a piece of work in its own right, and nothing here
forecloses it.

Verified locally against the real Brantford page: it publishes no schema.org
markup, so it takes the model path, and the prompt now states the Friday/Saturday
case explicitly. A two-date draft renders as two populated rows with all four
times. **Not verified against a live model run** — see below.

## Refreshing a listing from its source

Prompted by two problems that turn out to be one. A published listing goes
stale when the page moves on, and it also carries whatever *we* could
understand of that page on the day it was submitted — so events entered before
occurrences had their own end times hold one date where the page always listed
six. And there was no way to fix that: dates were not editable outside the
Django admin, which means a staff account.

Two screens, both under `/moderate/event/<pk>/`:

- **`dates/`** — an `OccurrenceFormSet` for an existing event. Same rows as the
  submitter's form plus a **Cancelled** box, which is a genuinely different
  action from **Remove this date**: cancelled keeps the date listed and struck
  through for someone who already has it in their calendar, removed takes it
  off as though it was never real. The formset's wrong-year guard is switched
  off here (`reject_all_past = False`) — it is aimed at an extraction a
  submitter is rubber-stamping, and a moderator correcting the record of an
  event that already happened is doing something ordinary.
- **`refresh/`** — re-reads `event.source_url` through the same pipeline, on
  the worker, and shows what the page now disagrees with: current value beside
  new, one checkbox each.

The refresh's design is entirely in what it refuses to do. It never writes to
an event on its own. After publication there is no submitter in the loop, so an
auto-applying refresh would let a rewritten page silently replace a listing a
human had already checked, and the first anyone would know is a reader turning
up on the wrong evening. Four more restraints, each with a test:

- **Never propose emptying a field.** A page redesigned into a JavaScript shell
  extracts as a title and nothing else, and reading that silence as "the
  description is gone now" would gut every listing whose source moved.
- **Never propose a boolean as False.** `EventDraft` defaults them, so a False
  is indistinguishable from "the page didn't mention it".
- **Never touch placement** — status, prominence, listing type, slug. A page
  has no opinion about where an event sits.
- **Only replace dates from today onward.** A source page describes what is
  coming. Without `keep_before`, re-reading a weekly class in its ninth month
  would delete every date it had ever run.

Categories are added to rather than replaced, because a moderator's
categorisation is a judgement made with the whole site in view and an
extraction that recognised one slug is not grounds for dropping the other two.

`EventRefresh` lives in `events` and stores the proposal; storing rather than
diffing on the fly is not bookkeeping, it is that extraction takes 116–336s and
the moderator arrives minutes after it finished. `EnrichmentRun` gained an
`event` FK so a refresh's cost lands in the same table as a submission's —
otherwise they are orphan rows nobody can account for.

For the backlog case there is an `EventAdmin` action that queues a re-read for
every selected listing, and a `/moderate/refreshes/` tab so the results are
somewhere a moderator would look. The daily `AIConfig` spend cap is the only
thing between that action and a large bill; selecting two hundred events is
allowed, paying for two hundred extractions in one afternoon is not.

`events/services.py` is new and holds `venue_for`, `organizer_for` and
`set_occurrences` — previously private to `submissions.services`, now shared by
all three write paths, because a second path creating venues its own way is how
the map grows two pins for one hall.

**Verified in a browser against the real server**, not only in tests: a
proposal with three changes, one unticked, applied — summary and dates written,
venue left alone, status/prominence/slug untouched, both audit rows present.
Then the dates screen, where a cancellation and a per-date note round-tripped.
**Not verified against a live model run**; the read was seeded, so what is
unproven is the same thing that was already unproven — extraction quality — not
the plumbing around it.

Two things this turned up that were not the point:

- **A two-line `{# #}` was rendering as body text on the moderator's edit
  form**, and had been since that form was written. The guard against exactly
  this exists in `tests/test_moderation.py` — it simply did not list the event
  URLs. It does now.
- **`runserver --noreload` serves stale templates.** Django caches compiled
  templates in development and drops that cache from the autoreloader, so with
  `--noreload` a fixed template keeps rendering its old text. That is how the
  comment above was found *after* being fixed, and it looks exactly like a fix
  not working.

## Pages and media — the small CMS, and why it is small

A site needs an About page, house rules, and the ability to put a photograph on
them. The question asked first was whether that wants a headless CMS or
Wagtail; the answer was neither, and the reasoning is worth keeping because it
will come up again the first time someone wants a richer page.

Wagtail supports Django 6 and would drop in. It also brings 22 packages, a
second admin, a shared `django-tasks` dependency next to the pin this project
holds at `<0.12` for the database backend, and an admin that
[has needed a relaxed CSP since 2015](https://github.com/wagtail/wagtail/issues/1288)
— inline scripts without nonces and `eval()` in its modals. This project
already declined `unsafe-eval` for Alpine, on a site that renders text
strangers submitted. The decisive part was subtler: Wagtail's image renditions
write several derived files per upload, which does not remove the "where do
bytes live" problem, it just rules out the cheapest answer to it.

**So images are rows.** There is no volume mounted; the container filesystem is
scratch that a deploy discards, and a file written to `MEDIA_ROOT` becomes a
broken image on a published page about a week later — the worst failure shape
available, because it looks fine at first. Object storage is right at volume;
this is not volume. A town's site accumulates a few dozen images, and
`content.images` normalises each to a couple of hundred KB, so the whole
library is a rounding error beside the events table — and the database backup
already covers it. **If this ever grows a photo gallery, revisit rather than
scale**: the exit is a storage backend and a data migration, and it is far
easier while the library is small.

The cost of that decision is one rule: **nothing may load an image row
casually.** `Image.objects.light()` defers both blob columns and every listing
uses it. The test for this asserts that the media page's query count *does not
grow with the library* rather than that it equals some number — a deferred blob
is fetched lazily, so the failure shape is one extra query per row, and a magic
constant would break on the next change to the page chrome for no reason.

**What is stored is what is served.** The original upload is discarded. Every
step in `content/images.py` is there because of something a real upload does:
EXIF orientation is applied *then* all metadata dropped (applied because a
phone photo is landscape-plus-a-flag; dropped because the same block carries
GPS, and a photo taken at a volunteer's home would otherwise publish their
address). SVG is refused outright — it is the one image format that is also a
document, and a reader following a direct link to it would run its `<script>`
as same-origin. Dimensions are bounded from the header, before any decode.
HEIC is not supported: iOS Safari converts to JPEG through a file input, so it
mostly arrives from a desktop drag-and-drop, and the error message names it.

Pages are Markdown, sanitised with `nh3` **at save**, not at render — the
public view should read one column and nothing else. The consequence is that
tightening `content/render.py` does not reach pages already stored, which is
what `Page.rerender()` is for. Sanitising at all, given the author is a
moderator, is not distrust: `SiteConfig.head_html` is raw HTML *restricted to
superusers*, and letting a moderator write `<script>` into a page would quietly
widen that grant to everyone who can work the queue.

Two smaller things that were deliberate. The upload form uses a plain
`FileField`, not `ImageField`, because `ImageField`'s own Pillow check runs
first and fails with "Upload a valid image" — shadowing every specific
explanation the normaliser gives. And a page's slug is *rejected* on collision
when typed but silently suffixed when derived, the opposite of an event's
random suffix: nobody reads an event slug, whereas a moderator picks a page's
address and can see it.

## Starting Phase 5

Feed importers. The plan's `imports` app: `ImportSource` (url, kind, default
organizer/venue/categories, `auto_publish`, `default_prominence`, poll
interval), `ImportedItem` (dedup by external UID + content hash), and polling
wired into `core/management/commands/run_housekeeping.py` — the `poll_import_sources`
step is already stubbed there and called by the CronJob.

Two things Phase 4 leaves ready for it. Sources that are *not* `auto_publish`
should drop their items into the same queue: `queue_for()` in
`moderation/services.py` is where a tab for them goes. And `ModerationAction`
already takes an `event` without a `submission`, which is the shape an imported
item needs.

Watch the fetcher boundary — `enrichment.fetcher` is the SSRF check, and an
importer pulling a user-configured feed URL must go through it too.

## Local setup

```bash
make install && make migrate && make superuser
.venv/bin/python manage.py verify_email you@example.com   # or you can't log in
make dev      # terminal 1
make worker   # terminal 2 — without it nothing background happens
```

A test account exists locally: `resident@example.com` / `verysecret-pw-123`,
and `admin@example.com` / `adminpass`.

`.env` holds `OPENROUTER_API_KEY` and `OPENROUTER_MODEL`; it is gitignored.
Turn enrichment on in the admin under *AI configuration* — it ships disabled.
