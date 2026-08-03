# Where this got to

A working note for picking the build back up. Architecture and conventions
live in `CLAUDE.md`; the full plan lives in
`~/.claude/plans/i-want-to-create-curious-wreath.md`. This file covers only
what those two don't: what is actually done, what is proven versus merely
written, and the traps that cost time.

Last updated after Phase 3.

## Done

| Phase | State | Commit |
|---|---|---|
| 0 — skeleton, accounts, deploy | done | `ceb3451` |
| 1 — domain model, geocoding | done | `f1046f7` |
| 2 — public browse, filters, map | done | `43c6115` |
| 3 — submission + AI enrichment | done | `0e4cece`, `6ed920e`, `4e5c4c8` |
| 4 — moderation queue | **next** | |
| 5 — feed importers | not started | |
| 6 — outbound feeds (ICS/RSS/JSON) | not started | |
| 7 — interest + Rising queue | not started | |
| 8 — series lifecycle, reusability docs | not started | |

145 tests passing. `make check` clean.

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

## Not verified

- **No deploy has ever run.** The chart lints and renders and `scripts/deploy.sh`
  fails fast if the IP allowlist blocks it, but first contact with the cluster
  is still ahead. Namespace secrets do not exist yet.
- **No real email has been sent.** Console backend throughout. SES credentials
  and DKIM are unproven.
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

## Starting Phase 4

The moderation queue. Everything it needs already exists:

- `Submission` with its status machine, `SubmissionMessage` for the reply
  thread, `ModerationAction` as an append-only audit log, all in
  `submissions/models.py`.
- `accounts.User.is_moderator` (superuser or the `Moderators` group).
- Events arrive as `status=PENDING` with `prominence` at its default, waiting
  for a human.

What to build: a queue view with assign-to-self; approve (setting prominence
and categories inline, since that *is* the editorial decision); reject with a
reason; request-info, which writes a `SubmissionMessage` and emails the
submitter; and the Rising view — published events whose interest is high
relative to their tier, as promotion nominations.

Keep the shape: the crowd nominates, a human decides. Interest must never move
an event between prominence tiers on its own.

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
