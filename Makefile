PYTHON3 ?= python3.12
VENV ?= .venv
PYTHON = $(VENV)/bin/python
DJANGO = DJANGO_SETTINGS_MODULE=trovi_launcher.tests.settings $(PYTHON) -m django

$(VENV):
	$(PYTHON3) -m venv $(VENV)

.PHONY: install
install: $(VENV)
	$(PYTHON) -m pip install -e '.[chi,dev]'

.PHONY: test
test:
	$(DJANGO) test

.PHONY: lint
lint:
	$(PYTHON) -m black --check .
	$(PYTHON) -m flake8

.PHONY: migrations
migrations:
	$(DJANGO) makemigrations --check --dry-run
