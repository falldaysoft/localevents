.PHONY: help install dev worker stop test check migrate superuser deploy build

PY := .venv/bin/python
PIP := .venv/bin/pip
INSTANCE ?=
# Deliberately empty. `scripts/deploy.sh` resolves an unset tag to HEAD and is
# the only place that should decide what "no tag given" means — this used to
# default to `latest`, which quietly overrode that and shipped a mutable tag.
# See the refusal in that script for what mutable tags do to a rollout.
TAG ?=

help:
	@echo "localevents"
	@echo ""
	@echo "  make install     create .venv and install dependencies"
	@echo "  make dev         run the web server (needs 'make worker' alongside)"
	@echo "  make worker      run the background task worker"
	@echo "  make stop        stop any running server and worker"
	@echo "  make test        run the test suite"
	@echo "  make check       django system checks + missing migrations"
	@echo "  make migrate     apply migrations"
	@echo "  make superuser   create an admin user"
	@echo ""
	@echo "  make deploy INSTANCE=<name> [TAG=<sha>]"
	@echo "                   deploy from this machine (must be IP-allowlisted)"

install:
	python3.14 -m venv .venv
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt

# Two processes are needed locally: the server, and the worker that runs
# enrichment, geocoding, and outbound email. Without the worker the site looks
# fine and silently does none of that.
#
# Both kill any stragglers first — a leftover process from an earlier session
# serves stale code on the same port and produces genuinely baffling results.
dev:
	@pkill -f "manage.py runserver" 2>/dev/null || true
	DEBUG=true $(PY) manage.py runserver

worker:
	@pkill -f "manage.py db_worker" 2>/dev/null || true
	DEBUG=true $(PY) manage.py db_worker

# Handy when a puzzling result smells like stale code.
stop:
	@pkill -f "manage.py runserver" 2>/dev/null || true
	@pkill -f "manage.py db_worker" 2>/dev/null || true
	@echo "stopped any running server and worker"

test:
	DEBUG=true $(PY) -m pytest -q

check:
	DEBUG=true $(PY) manage.py check
	DEBUG=true $(PY) manage.py makemigrations --check --dry-run

migrate:
	DEBUG=true $(PY) manage.py migrate

superuser:
	DEBUG=true $(PY) manage.py createsuperuser

# `dev`, not `latest`: this builds on a laptop and never reaches the registry,
# so naming it after the tag CI publishes only invites confusion about which
# one a `docker run` picked up.
build:
	docker build -t ghcr.io/falldaysoft/localevents:$(or $(TAG),dev) .

deploy:
	@test -n "$(INSTANCE)" || (echo "usage: make deploy INSTANCE=<name> [TAG=<full-sha>]  # TAG defaults to HEAD" && exit 1)
	./scripts/deploy.sh $(INSTANCE) $(TAG)
