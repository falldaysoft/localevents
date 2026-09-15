# localevents

A community event listing hub for a single town or region. Open source, MIT
licensed, and deliberately not tied to any particular place — the code is the
product, a running site is an *instance* of it.

The goal is a community service that mostly runs itself, with human moderators
overseeing: the coverage of a large event platform, without the engagement
mechanics.

- Anyone can browse, filter, and see events on a map without an account.
- Registered users (confirmed email) submit an event by pasting a URL. The site
  reads the page — structured data first, an AI model only if needed — fills in
  the details, and asks the submitter to confirm before anything reaches a
  moderator.
- Moderators work a purpose-built queue at `/moderate/` — approve (choosing
  placement and categories, which is the actual editorial decision), decline
  with a reason, or ask the submitter a question and hand it back to them.
  Every decision is emailed and recorded in an append-only log.
- Visitor interest feeds a *Rising* queue of promotion candidates and nothing
  else: it can never move an event up on its own. The crowd nominates, a human
  decides. (The moderators' side of this is built; the public "Interested"
  button is not yet.)
- Public calendars (libraries, venues, municipalities) can be imported directly
  on a per-source trust setting.

## Status

Early but working end to end. Accounts, the public site with map and filters,
the AI-assisted submission flow, and the moderation queue are in place. Feed
importers are next.

## Running it locally

Requires Python 3.12+ (3.14 is what the container uses).

```bash
make install
make migrate
make dev        # http://localhost:8000
```

A site with no superuser is *unclaimed*: every page says so, and `/claim/`
offers a form that creates the first administrator. Open it and fill it in.
The page 404s the moment anyone does, so it cannot be used twice.

`make superuser` still works if you prefer the shell, but it needs a second
step. Email confirmation is mandatory and applies to accounts created there
too — a `createsuperuser` account has no confirmation email to click and so
cannot sign in:

```bash
make superuser
.venv/bin/python manage.py verify_email you@example.com
```

Claiming does not need that step; it marks the address verified as it goes,
which is the point — the first administrator is the person who configures the
mail relay, so requiring working email to create them would be circular.
`verify_email` remains useful when a later moderator's confirmation mail
bounces.

Background work — AI enrichment, geocoding, feed polling, outbound email — runs
on a queue, not in the request. **You need a second process** or those things
silently never happen:

```bash
make worker
```

Email in development goes to the console, confirmation links included, so you
can register an account without configuring SMTP.

```bash
make test       # full suite
make check      # system checks + missing-migration check
```

## Running this for your own town

Nothing about a specific community is compiled in. To stand up your own
instance you change configuration, never code:

**Deploy-time settings** (environment variables, set in the instance's `.env`)
cover the identity that has to be known before the database is reachable:
`SITE_NAME`, `SITE_TAGLINE`, `CONTACT_EMAIL`, `SITE_TIMEZONE`, the map centre
and zoom, `MAP_BBOX`, and the tile server. See `.env.example` for the full list
with explanations.

`MAP_BBOX` is worth a moment's thought. Besides framing the map it acts as a
**region gate**: an event that geocodes outside those bounds is flagged for a
moderator rather than published. Draw it generously enough to include the
surrounding area people actually travel to.

**Run-time content** — the About text, submission guidelines, code of conduct,
and footer links — is edited in the admin under *Site configuration*, so a
moderator can revise it without a deploy.

**Categories** are a curated list, also editable in the admin. Keep it short.
The filters are only useful because the vocabulary is small.

A test (`tests/test_reusability.py`) fails the build if a place name leaks into
the code, templates, or chart defaults. If you fork this for your town, add
your own place name to `BANNED_SUBSTRINGS` there — it is a guard against
gradual erosion, and it only works if it knows what to look for.

## Deployment

An instance is a directory on a VM: `~/apps/<instance>/` holding
`deploy/docker-compose.yml`, `deploy/deploy.sh`, and a `.env` that names the
community. Three containers share one image — the web server (which migrates
on start), the task worker, and an hourly housekeeping one-shot run from
crontab — behind a Traefik that terminates TLS, on a shared Postgres. Uploaded
images are database rows, so the database backup is the whole backup.

Every push to `main` builds the container image for amd64 and arm64, pushes
it to `ghcr.io/falldaysoft/localevents` tagged with the commit SHA, and
deploys it to each instance you have told the workflow about. Deployment is
configured in GitHub, not in this repository, because the repository is the
reusable product and an instance is a particular community:

1. Create a GitHub **environment** named after the instance (say `mytown`).
2. Give it variables `DEPLOY_HOST` (`user@host` of the VM) and
   `DEPLOY_HOST_KEY` (the VM's host key as a `known_hosts` line, from
   `ssh-keyscan -t ed25519 <host>`).
3. Give it a secret `DEPLOY_KEY`: a private key whose public half is in the
   VM's `authorized_keys` with a forced command of `~/apps/mytown/deploy.sh`
   (see below). The key can run that script and nothing else.
4. Set the repository variable `DEPLOY_INSTANCES` to a JSON list of the
   environments to deploy, e.g. `["mytown"]`. Until it is set, CI builds and
   stops.

A deploy by hand takes the same route, through your own key — useful for a
rollback:

```bash
make deploy INSTANCE=<name> TAG=<full-git-sha>
```

Both refuse mutable tags and short SHAs.

### First deploy of a new instance

On the VM, with Docker, a Traefik on `infra-network` and a `postgres`
container already there:

```bash
I=mytown
mkdir -p ~/apps/$I ~/backups/$I

# The overlay. Everything that names the community goes here and nowhere
# else; see instances/example.env for what each line means.
cp instances/example.env ~/apps/$I/.env && chmod 600 ~/apps/$I/.env && $EDITOR ~/apps/$I/.env

cp deploy/docker-compose.yml deploy/deploy.sh deploy/restore-from-dump.sh ~/apps/$I/

# Database: a role and a database, both named after the instance.
docker exec postgres psql -U postgres -c "create role $I login password '<DB_PASSWORD from .env>'"
docker exec postgres psql -U postgres -c "create database $I owner $I"

# The CI deploy key, bound to the deploy script and nothing else.
ssh-keygen -t ed25519 -N "" -f $I-deploy   # private half -> the DEPLOY_KEY secret
echo "command=\"/home/$USER/apps/$I/deploy.sh\",no-port-forwarding,no-X11-forwarding,no-agent-forwarding,no-pty,restrict $(cat $I-deploy.pub)" >> ~/.ssh/authorized_keys

# Housekeeping, hourly. `run` rather than `up`: the service is under a
# profile precisely so that `up -d` never starts it.
(crontab -l; echo "17 * * * * cd ~/apps/$I && docker compose run --rm housekeeping >> ~/apps/$I/housekeeping.log 2>&1") | crontab -
```

Then push to `main`, or `make deploy INSTANCE=$I` from a checkout that has
`instances/$I.env`. Migrations run when the web container starts; the worker
waits for it to be healthy.

Then **open `https://<host>/claim/` and claim the site**. Every page of a
freshly deployed instance carries a banner saying it has no administrator, and
that page hands the first person to fill it in a superuser account that can
sign in immediately. It stops existing as soon as someone does.

Do it now rather than later. Claiming is first-come-first-served — there is no
token, because delivering one would mean the `docker exec` round-trip this
replaces — so between the deploy and your claim, the site belongs to whoever
loads it. That window is yours to keep short. If you lose the race, delete the
intruder's account and reclaim:

```bash
docker exec $I-web python manage.py shell -c \
  "from django.contrib.auth import get_user_model; get_user_model().objects.all().delete()"
docker compose -f ~/apps/$I/docker-compose.yml restart   # the unclaimed check is latched per process
```

The shell route still works if you would rather not race at all — create the
account before the DNS record points anywhere:

```bash
docker exec -it $I-web python manage.py createsuperuser
docker exec $I-web python manage.py verify_email you@example.com
```

(`-it` matters: `createsuperuser` prompts, and without a TTY it skips itself.)

### Moving an instance, or restoring a backup

The nightly `pg_dump` on the VM covers everything. To move an instance from
elsewhere, dump there with `pg_dump -Fc --no-owner --no-acl`, copy the file to
`~/backups/<instance>/`, and run `~/apps/<instance>/restore-from-dump.sh
<file>` — it stops the app, restores with `--clean`, and starts it again.
Carry `SECRET_KEY` over in `.env` so sessions and password-reset links survive
the move.

## Reading event pages

When someone submits a link, the cheap path is tried first: if the page
publishes schema.org `Event` markup, that is exact, instant, and free. Only
pages without it reach a language model.

**Prefer primary sources.** A link to the organiser's own page — the hall, the
library, the band — makes a better listing than a ticketing platform's page
about them: it stays useful after tickets sell out, and it credits whoever is
actually doing the work. Submitting an aggregator link is allowed, because
sometimes it genuinely is the only place an event is published, but the
submitter is shown a note suggesting otherwise. See `submissions/sources.py`.

Fetching is deliberately modest: one page at a time, on a signed-in person's
behalf, with a contactable User-Agent, honouring `robots.txt`, capped at 2 MB,
and refusing any address that resolves to a private network. There is no
headless browser and no JavaScript execution. Measured against real sites,
plain HTTP returned usable text from every one that permitted it — small
community and municipal sites are server-rendered precisely because they need
to be found in search.

The model is configured in the admin under *AI configuration*, not in code, so
it can be changed and compared without a redeploy. Any OpenAI-compatible
endpoint works; OpenRouter is the default because it reaches every model worth
using behind a single URL. Schema conformance is *not* guaranteed there —
OpenRouter forwards the request to the upstream model, which may ignore it — so
replies are validated locally and retried once with the error fed back.

Every attempt is recorded with its method, model, endpoint, token counts,
duration, and estimated cost, including the free ones and the failures. That
record is what makes "is the cheaper model good enough" a question with an
answer. There is a daily spend cap.

Expect extraction to be slow — a measured run against a busy page took nearly
two minutes — which is why it happens on a background worker with a progress
page rather than in the request.

## Licence

MIT. See `LICENSE`.
