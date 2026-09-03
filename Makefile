# Regulator developer entry points. `make help` lists targets.
.DEFAULT_GOAL := help
SHELL := /bin/bash

PYTHON     ?= python3
VENV       ?= .venv
VENV_PY    := $(VENV)/bin/python
IMAGE      ?= regulator-worker:dev

.PHONY: help venv install test server-test scenarios lint smoke docker-build docker-build-server docker-build-browser docker-smoke clean

help: ## Show this help
	@grep -hE '^[a-zA-Z0-9_-]+:.*?## ' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "} {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

venv: ## Create $(VENV) with an up-to-date pip
	$(PYTHON) -m venv $(VENV)
	$(VENV_PY) -m pip install --upgrade pip

install: venv ## Install worker runtime + test dependencies into $(VENV)
	@if [ -f worker/requirements.txt ]; then \
		$(VENV_PY) -m pip install -r worker/requirements.txt; \
	else \
		echo "worker/requirements.txt not present yet; skipping runtime deps"; \
	fi
	$(VENV_PY) -m pip install pytest pytest-timeout

test: install ## Run unit tests (worker + tools) in $(VENV)
	$(VENV_PY) -m pytest worker/tests tools/tests -q

server-test: install ## Run the control-plane tests in $(VENV)
	$(VENV_PY) -m pip install -r server/requirements.txt
	$(VENV_PY) -m pytest server/tests -q

scenarios: ## Regenerate the per-pack scenario library from tools/build_pack_scenarios.py
	$(VENV_PY) tools/build_pack_scenarios.py

# ruff is deliberately not in requirements.txt: it is a developer tool, not a
# runtime dependency, and adding it there would put it in the worker image.
# So the target has to cope with it being absent rather than failing the build
# of someone who only wanted to run the tests.
lint: ## Lint with ruff if it is installed, otherwise say so and pass
	@if $(VENV_PY) -m ruff --version >/dev/null 2>&1; then \
		$(VENV_PY) -m ruff check .; \
	elif command -v ruff >/dev/null 2>&1; then \
		ruff check .; \
	else \
		echo "ruff not installed; skipping lint (pip install ruff to enable)"; \
	fi

smoke: ## Run the local end-to-end smoke against the bundled fake splunkd
	tools/smoke.sh local

docker-build: ## Build the worker image locally (single arch, loaded into docker)
	docker buildx build --load -f worker/Dockerfile -t $(IMAGE) .

docker-build-server: ## Build the control-plane image locally
	docker buildx build --load -f server/Dockerfile -t regulator:dev .

docker-build-browser: ## Build the browser worker image locally (amd64 only)
	docker buildx build --load -f worker/Dockerfile.browser -t regulator-worker:browser-dev .

docker-smoke: docker-build ## Build then smoke the image
	tools/smoke.sh docker $(IMAGE)

clean: ## Remove build, cache and local run artefacts (leaves $(VENV) alone)
	rm -rf .pytest_cache build dist results
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
	find . -name '*.egg-info' -type d -prune -exec rm -rf {} +
	rm -f .coverage
