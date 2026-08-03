# Where this got to

A working note for picking the build back up. Architecture and conventions
live in `CLAUDE.md`; the full plan lives in
`~/.claude/plans/i-want-to-create-curious-wreath.md`. This file covers only
what those two don't: what is actually done, what is proven versus merely
written, and the traps that cost time.

Last updated after Phase 4.

## Done

| Phase | State | Commit |
|---|---|---|
| 0 — skeleton, accounts, deploy | done | `ceb3451` |
| 1 — domain model, geocoding | done | `f1046f7` |
| 2 — public browse, filters, map | done | `43c6115` |
| 3 — submission + AI enrichment | done | `0e4cece`, `6ed920e`, `4e5c4c8` |
| 4 — moderation queue, Rising | done | this commit |
| 5 — feed importers | **next** | |
| 6 — outbound feeds (ICS/RSS/JSON) | not started | |
| 7 — interest button (Rising itself is done) | not started | |
| 8 — series lifecycle, reusability docs | not started | |

169 tests passing. `make check` clean.

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

## Not verified

- **No deploy has ever run.** The chart lints and renders and `scripts/deploy.sh`
  fails fast if the IP allowlist blocks it, but first contact with the cluster
  is still ahead. Namespace secrets do not exist yet.
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

What actually catches it is `EventDraftForm.clean_starts_at`, which rejects
anything more than a day in the past with *"That date has already passed. Is it
the right year?"*. That is the load-bearing defence and it has a test.

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
