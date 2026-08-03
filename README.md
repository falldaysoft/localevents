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
- Moderators approve, reject, or reply asking for more information.
- Public calendars (libraries, venues, municipalities) can be imported directly
  on a per-source trust setting.

## Status

Early. The project skeleton, accounts, deployment, and the reusability guards
are in place; the domain model and public site are being built next.

## Running it locally

Requires Python 3.12+ (3.14 is what the container uses).

```bash
make install
make migrate
make superuser
make dev        # http://localhost:8000
```

Email confirmation is mandatory, and that applies to accounts created from the
shell too — a `createsuperuser` account has no confirmation email to click and
so cannot sign in. Mark it verified once:

```bash
.venv/bin/python manage.py verify_email you@example.com
```

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

**Deploy-time settings** (environment variables, set from the Helm values file)
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

The container image is built by CI on every push to `main` and pushed to
`ghcr.io/falldaysoft/localevents`. **CI does not deploy.** The Kubernetes API
sits behind an IP allowlist that GitHub-hosted runners cannot satisfy, so
deployment runs from a machine whose IP is allowlisted:

```bash
make deploy INSTANCE=<name> TAG=<git-sha>
```

`INSTANCE` names a values file at `instances/<name>.yaml` holding the host,
namespace, and regional settings. Those files are not committed — they describe
a particular community, and this repository is the reusable product. Copy
`instances/example.yaml` to get started, and keep your real one either locally
or in your own deployment repository.

If push-to-deploy becomes worth having, the two real options are a self-hosted
runner inside the cluster, or a GitOps controller (Flux/Argo) pulling from
ghcr. Neither needs inbound access to the API server.

### First deploy of a new instance

The chart assumes two secrets already exist in the target namespace. Create
them once:

```bash
NS=<namespace>
kubectl create namespace "$NS" --dry-run=client -o yaml | kubectl apply -f -

# Image pull secret, copied from an existing namespace
kubectl get secret ghcr-secret -n bigreminders -o yaml \
  | sed "s/namespace: bigreminders/namespace: $NS/" \
  | kubectl apply -f -

# Database. Create the role and database on the shared Postgres first.
kubectl create secret generic postgres-secret --namespace "$NS" \
  --from-literal=DATABASE_URL="postgres://USER:PASSWORD@postgres-postgres.postgres.svc.cluster.local:5432/DBNAME" \
  --dry-run=client -o yaml | kubectl apply -f -

# Application secrets. Only secret-key is required.
kubectl create secret generic localevents-secrets --namespace "$NS" \
  --from-literal=secret-key="$(python -c 'from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())')" \
  --from-literal=email-host-user="..." \
  --from-literal=email-host-password="..." \
  --from-literal=anthropic-api-key="..." \
  --dry-run=client -o yaml | kubectl apply -f -
```

Migrations run automatically as a Helm pre-upgrade hook.

Then create your first admin account. Remember the verification step — without
it the account exists but cannot sign in:

```bash
kubectl exec -n "$NS" deploy/site -- python manage.py createsuperuser
kubectl exec -n "$NS" deploy/site -- python manage.py verify_email you@example.com
```

## Enrichment providers

Reading an event out of a web page tries the cheap path first: if the page
publishes schema.org `Event` markup, that is exact, instant, and free. Only
pages without it reach a language model.

Which model is configured in the admin under *AI configuration*, not in code,
so it can be changed and compared without a redeploy:

- **Anthropic** — best extraction quality; schema conformance is guaranteed by
  the API.
- **OpenRouter** — one base URL, any model, so an instance can run something
  much cheaper. Schema conformance is *not* guaranteed here, because OpenRouter
  passes the request through to the upstream model and not all of them honour
  it, so responses are validated and retried once before failing.

Every call is recorded with its provider, model, token counts, and estimated
cost, which is what lets you answer "is the cheap model good enough" with data
rather than a guess. There is a daily spend cap.

## Licence

MIT. See `LICENSE`.
