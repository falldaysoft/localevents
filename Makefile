.PHONY: help install dev worker stop test check migrate superuser deploy build

PY := .venv/bin/python
PIP := .venv/bin/pip
INSTANCE ?=
TAG ?= latest

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

build:
	docker build -t ghcr.io/falldaysoft/localevents:$(TAG) .

deploy:
	@test -n "$(INSTANCE)" || (echo "usage: make deploy INSTANCE=<name> [TAG=<sha>]" && exit 1)
	./scripts/deploy.sh $(INSTANCE) $(TAG)
