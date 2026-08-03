.PHONY: help install dev worker test check migrate superuser deploy build

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
# enrichment, geocoding, and outbound email.
dev:
	DEBUG=true $(PY) manage.py runserver

worker:
	DEBUG=true $(PY) manage.py db_worker

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
